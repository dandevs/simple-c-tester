import errno
import os
import shutil
import time
import asyncio
import re
import json
from pathlib import Path
from typing import Callable
from urllib import parse as urllib_parse

import state as global_state
from state import state, active_processes, subprocess_columns
from models import ScopeBucket, Test, TestState, TimelineEvent
from .expression_tokenizer import tokenize_line
from .makefile import (
    refresh_dependency_graph,
    generate_makefile,
    build_project_sources,
    save_debug_line,
)
from .artifacts import test_binary_path
from .debugger import GdbMIController, DebugStopEvent, stop_event_is_terminal
from .dwarf_core import (
    get_function_index,
    FunctionIndex,
    load_dwarf_data,
    resolve_variable_type,
)
from .dwarf_core.global_index import get_global_variables, evaluate_global
from .story_filters import StoryFilterEngine, TriggerMatch, normalized_story_filter_profile
from .story_annotations import (
    get_story_annotations,
    format_story_annotations_for_db,
    merge_line_annotations_into_cache,
    _merge_event_annotations_into,
)
from .annotation_resolver import resolve_line_annotations


MAX_TIMELINE_EVENTS = 12000
MAX_DEBUG_LOG_LINES = 4000
_debug_sessions: dict[str, GdbMIController] = {}
_active_run_tasks: dict[str, asyncio.Task] = {}
_source_line_cache: dict[str, list[str]] = {}
_user_function_cache: set[str] = set()
_user_function_cache_key: object | None = None

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_FUNC_DEF_RE = re.compile(
    r"^\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?"
    r"[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{",
    re.MULTILINE,
)
_CONTROL_WORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "catch",
}
_NON_USER_PREFIXES = (
    "/usr/",
    "/lib/",
    "/lib64/",
    "/opt/",
    "/nix/",
)
_BREAKPOINT_SOURCE_EXTENSIONS = {".c", ".cpp"}
_BREAKPOINTS_FILE_PATH = os.environ.get(
    "CTESTER_BREAKPOINTS_FILE",
    os.path.join("test_build", "breakpoints.json"),
)
_editor_breakpoints_cache: list[tuple[str, int]] = []
_editor_breakpoints_mtime_ns: int | None = None
_annotation_persist_tasks: dict[str, asyncio.Task] = {}


def _looks_pointer_value(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped in {"0x0", "(nil)", "nullptr"}:
        return False
    return stripped.startswith("0x")


def _format_dwarf_type(type_info) -> str:
    if type_info is None:
        return ""
    kind = type_info.kind
    name = type_info.name or ""
    if kind == "enum":
        return f"enum {name}" if name else "enum"
    if kind == "struct":
        return f"struct {name}" if name else "struct"
    if kind == "array":
        dims = "".join(
            f"[{upper - lower + 1}]"
            for lower, upper in type_info.dimensions
            if upper >= lower
        )
        element = _format_dwarf_type(type_info.element_type)
        base = element or name or "?"
        return f"{base}{dims}"
    if kind == "pointer":
        pointed = _format_dwarf_type(type_info.pointed_to_type)
        base = pointed or name or "?"
        return f"{base}*"
    return name


def _test_key(test: Test) -> str:
    return os.path.abspath(test.source_path)


def _append_debug_log(test: Test, message: str) -> None:
    if not message:
        return
    run = test.current_run
    if run is None:
        return
    for line in message.replace("\r", "").split("\n"):
        if line:
            run.debug_logs.append(line)
    if len(run.debug_logs) > MAX_DEBUG_LOG_LINES:
        run.debug_logs = run.debug_logs[-MAX_DEBUG_LOG_LINES:]


def _append_timeline_event(
    test: Test,
    kind: str,
    message: str,
    file_path: str = "",
    line: int = 0,
    function: str = "",
    stream: str = "",
    variables: list[tuple[str, str, str]] | None = None,
    program_counter: int = 0,
    line_annotations: dict[int, list[str]] | None = None,
    primary_trigger: str = "",
    trigger_ids: list[str] | None = None,
    trigger_label: str = "",
    trigger_message: str = "",
) -> None:
    run = test.current_run
    if run is None:
        return
    event = TimelineEvent(
        index=len(run.timeline_events) + 1,
        timestamp=time.monotonic(),
        kind=kind,
        message=message,
        file_path=file_path,
        line=line,
        function=function,
        stream=stream,
        variables=list(variables or []),
        program_counter=program_counter,
        line_annotations=dict(line_annotations or {}),
        primary_trigger=primary_trigger,
        trigger_ids=list(trigger_ids or []),
        trigger_label=trigger_label,
        trigger_message=trigger_message,
    )
    run.timeline_events.append(event)
    if len(run.timeline_events) > MAX_TIMELINE_EVENTS:
        run.timeline_events = run.timeline_events[-MAX_TIMELINE_EVENTS:]


def _is_manual_debug_mode(test: Test) -> bool:
    run = test.current_run
    if run is None:
        return False
    for event in reversed(run.timeline_events):
        if event.kind == "run_start":
            return "manual debug" in event.message.lower()
    return False


def _persist_story_annotations(test: Test) -> None:
    annotations = get_story_annotations(test, cache=test.dwarf_cache)
    db_formatted = format_story_annotations_for_db(annotations, cache=test.dwarf_cache)
    from .makefile import save_story_annotations
    save_story_annotations(_test_key(test), db_formatted)


async def _persist_story_annotations_after_delay(test: Test) -> None:
    test_key = _test_key(test)
    try:
        await asyncio.sleep(0.1)
        _persist_story_annotations(test)
    except asyncio.CancelledError:
        pass
    finally:
        _annotation_persist_tasks.pop(test_key, None)


def _schedule_story_annotations_persist(test: Test) -> None:
    test_key = _test_key(test)
    existing = _annotation_persist_tasks.get(test_key)
    if existing is not None and not existing.done():
        existing.cancel()
    task = asyncio.ensure_future(_persist_story_annotations_after_delay(test))
    _annotation_persist_tasks[test_key] = task


def cancel_pending_story_annotations_persist(test: Test) -> None:
    test_key = _test_key(test)
    existing = _annotation_persist_tasks.pop(test_key, None)
    if existing is not None and not existing.done():
        existing.cancel()


async def _emit_skipped_standalone_exprs(
    test: Test,
    stop_event: DebugStopEvent,
    story_filters: StoryFilterEngine,
    controller: GdbMIController,
    variables: list[tuple[str, str, str]],
    binary_path: str,
) -> None:
    """Create synthetic timeline events for standalone expression lines skipped by gdb next()."""
    prev = story_filters.previous_stop
    if prev is None:
        return
    if not prev.file_path or prev.line <= 0:
        return
    if not stop_event.file_path or stop_event.line <= 0:
        return
    if os.path.abspath(prev.file_path) != os.path.abspath(stop_event.file_path):
        return
    if (prev.function or "") != (stop_event.function or ""):
        return
    if stop_event.line <= prev.line + 1:
        return

    from .story_filters import _is_standalone_expression_line

    vars_for_synthetic = list(variables)
    if not vars_for_synthetic:
        vars_for_synthetic = await _capture_scope_variables_fast(
            controller,
            binary_path=binary_path,
            file_path=stop_event.file_path,
            line=stop_event.line,
            cache=test.dwarf_cache,
        )

    for line_num in range(prev.line + 1, stop_event.line):
        line_text = _line_text(stop_event.file_path, line_num, cache=test.dwarf_cache)
        if _is_standalone_expression_line(line_text):
            synthetic_stop = DebugStopEvent(
                file_path=stop_event.file_path,
                line=line_num,
                function=stop_event.function,
                program_counter=stop_event.program_counter,
            )
            await _record_stop_event(
                test,
                synthetic_stop,
                binary_path=binary_path,
                variables=vars_for_synthetic,
                trigger_matches=[
                    TriggerMatch(
                        trigger_id="standalone_expr",
                        label="Expr",
                        message=f"standalone expression at L{line_num}",
                    )
                ],
                debugger=controller,
            )
            run = test.current_run
            if run is not None and run.timeline_events:
                event = run.timeline_events[-1]
                scope_index = await _get_or_load_lexical_scope_index(binary_path, cache=test.dwarf_cache)
                if scope_index is not None and synthetic_stop.program_counter:
                    try:
                        scope_chain = scope_index.get_scope_chain(synthetic_stop.program_counter)
                        if not scope_chain and event.file_path and event.line > 0:
                            scope_chain = scope_index.get_scope_chain_by_line(event.file_path, event.line)
                        if scope_chain:
                            _update_scope_buckets(test, event, scope_chain, is_synthetic=True)
                    except Exception:
                        pass


def _start_timeline_run(test: Test, reason: str) -> None:
    run = test.current_run
    if run is None:
        return
    run_index = (
        sum(1 for event in run.timeline_events if event.kind == "run_start") + 1
    )
    _append_timeline_event(test, "run_start", f"run {run_index}: {reason}")


def _stop_reason_message(stop_event: DebugStopEvent) -> str:
    location = ""
    if stop_event.file_path and stop_event.line > 0:
        location = f" at {stop_event.file_path}:{stop_event.line}"
    elif stop_event.file_path:
        location = f" at {stop_event.file_path}"

    function = f" [{stop_event.function}]" if stop_event.function else ""
    if stop_event.reason.startswith("exited"):
        if stop_event.exit_code is None:
            return stop_event.reason
        return f"{stop_event.reason} ({stop_event.exit_code})"
    if stop_event.reason == "signal-received" and stop_event.signal_name:
        return f"signal: {stop_event.signal_name}{location}{function}"
    if stop_event.reason == "end-stepping-range":
        return f"step{location}{function}"
    if stop_event.reason == "breakpoint-hit":
        return f"breakpoint{location}{function}"
    if stop_event.reason == "function-finished":
        return f"step-out complete{location}{function}"
    if stop_event.reason:
        return f"{stop_event.reason}{location}{function}"
    return stop_event.raw or "debug stop"


def _load_source_lines(file_path: str, cache=None) -> list[str]:
    line_cache = cache.source_line_cache if cache is not None else _source_line_cache
    cached = line_cache.get(file_path)
    if cached is not None:
        return cached

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []

    line_cache[file_path] = lines
    return lines


def _line_text(file_path: str, line_number: int, cache=None) -> str:
    if not file_path or line_number <= 0:
        return ""
    lines = _load_source_lines(file_path, cache=cache)
    if line_number > len(lines):
        return ""
    return lines[line_number - 1]


def _extract_call_names_tokenized(line: str) -> set[str]:
    tokens = tokenize_line(line)
    calls: set[str] = set()
    for i, tok in enumerate(tokens):
        if tok.type == "identifier" and tok.value not in _CONTROL_WORDS:
            if i + 1 < len(tokens) and tokens[i + 1].value == "(":
                calls.add(tok.value)
    return calls


async def _line_has_likely_call(file_path: str, line_number: int, binary_path: str = "", cache=None) -> bool:
    content = _line_text(file_path, line_number, cache=cache)
    if not content:
        return False

    call_names = _extract_call_names_tokenized(content)
    if not call_names:
        return False

    user_functions = await _discover_user_function_names(binary_path, cache=cache)
    if not user_functions:
        return False

    return any(name in user_functions for name in call_names)


async def _discover_user_function_names(binary_path: str = "", cache=None) -> set[str]:
    global _user_function_cache_key
    global _user_function_cache

    if binary_path:
        try:
            mtime = int(os.path.getmtime(binary_path))
        except OSError:
            mtime = 0
        cache_key = (binary_path, mtime)
        if _user_function_cache_key == cache_key:
            return _user_function_cache
        try:
            index = await asyncio.to_thread(get_function_index, binary_path, cache=cache)
            if index.user_function_names:
                _user_function_cache_key = cache_key
                _user_function_cache = set(index.user_function_names)
                return _user_function_cache
        except Exception:
            pass

    tracked_files: list[tuple[str, int]] = []
    source_files: list[str] = []
    for root in ("src", "tests"):
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for file_path in root_path.rglob("*.c"):
            abs_path = str(file_path.resolve())
            source_files.append(abs_path)
            try:
                tracked_files.append((abs_path, int(file_path.stat().st_mtime_ns)))
            except OSError:
                tracked_files.append((abs_path, 0))

    tracked_files.sort()
    cache_key = tuple(tracked_files)
    if _user_function_cache_key == cache_key:
        return _user_function_cache

    discovered: set[str] = set()
    for abs_path in source_files:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue

        for match in _FUNC_DEF_RE.finditer(content):
            name = match.group(1)
            if name and name not in _CONTROL_WORDS:
                discovered.add(name)

    _user_function_cache_key = cache_key
    _user_function_cache = discovered
    return discovered


def _is_user_code_path(file_path: str) -> bool:
    if not file_path:
        return False
    abs_path = os.path.abspath(file_path)
    if any(abs_path.startswith(prefix) for prefix in _NON_USER_PREFIXES):
        return False

    cwd = os.path.abspath(".") + os.sep
    tests_root = os.path.abspath("tests") + os.sep
    src_root = os.path.abspath("src") + os.sep
    return abs_path.startswith(cwd) or abs_path.startswith(tests_root) or abs_path.startswith(src_root)


def _looks_like_windows_abs_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")


def _normalize_breakpoint_path(file_path: str) -> str:
    value = file_path.strip()
    if not value:
        return ""

    if value.lower().startswith("file://"):
        parsed = urllib_parse.urlparse(value)
        value = urllib_parse.unquote(parsed.path or "")
        if os.name == "nt" and len(value) >= 3 and value[0] == "/" and value[2] == ":":
            value = value[1:]

    if _looks_like_windows_abs_path(value):
        return value.replace("\\", "/")

    if not os.path.isabs(value):
        direct = os.path.abspath(value)
        if os.path.exists(direct):
            value = direct
        else:
            parent_based = os.path.abspath(os.path.join("..", value))
            value = parent_based if os.path.exists(parent_based) else direct

    return os.path.normpath(value)


def _normalized_abs_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def editor_breakpoints_file_path() -> str:
    configured = _BREAKPOINTS_FILE_PATH.strip()
    if not configured:
        configured = os.path.join("test_build", "breakpoints.json")
    return os.path.abspath(configured)


def is_editor_breakpoints_file_path(path: str) -> bool:
    return _normalized_abs_path(path) == _normalized_abs_path(editor_breakpoints_file_path())


def _parse_editor_breakpoints_payload(payload) -> tuple[list[tuple[str, int]], str]:
    if not isinstance(payload, list):
        return [], "breakpoints file returned unexpected payload"

    breakpoints: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        filename = item.get("filepath") or item.get("filename")
        line_number = item.get("line_number")
        if not isinstance(filename, str):
            continue
        try:
            line = int(line_number)
        except (TypeError, ValueError):
            continue
        if line <= 0:
            continue

        normalized = _normalize_breakpoint_path(filename)
        if not normalized:
            continue
        ext = os.path.splitext(normalized)[1].lower()
        if ext not in _BREAKPOINT_SOURCE_EXTENSIONS:
            continue
        if not os.path.exists(normalized):
            continue

        key = (normalized, line)
        if key in seen:
            continue
        seen.add(key)
        breakpoints.append(key)

    breakpoints.sort(key=lambda item: (item[0], item[1]))
    return breakpoints, ""


def refresh_editor_breakpoints_cache(force: bool = False) -> tuple[list[tuple[str, int]], str]:
    global _editor_breakpoints_cache
    global _editor_breakpoints_mtime_ns

    breakpoints_file = editor_breakpoints_file_path()

    try:
        file_stat = os.stat(breakpoints_file)
        file_mtime_ns = int(file_stat.st_mtime_ns)
    except FileNotFoundError:
        _editor_breakpoints_cache = []
        _editor_breakpoints_mtime_ns = None
        return [], f"breakpoints file not found: {breakpoints_file}"
    except OSError as error:
        return list(_editor_breakpoints_cache), f"unable to stat breakpoints file: {error}"

    if not force and _editor_breakpoints_mtime_ns == file_mtime_ns:
        return list(_editor_breakpoints_cache), ""

    try:
        with open(breakpoints_file, "r", encoding="utf-8", errors="replace") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return list(_editor_breakpoints_cache), "breakpoints file contains invalid JSON"
    except OSError as error:
        return list(_editor_breakpoints_cache), f"unable to read breakpoints file: {error}"

    parsed_breakpoints, parse_error = _parse_editor_breakpoints_payload(payload)
    _editor_breakpoints_cache = parsed_breakpoints
    _editor_breakpoints_mtime_ns = file_mtime_ns
    return list(_editor_breakpoints_cache), parse_error


def prime_editor_breakpoints_cache() -> None:
    refresh_editor_breakpoints_cache(force=True)


async def sync_editor_breakpoints_for_active_debug() -> None:
    active_key = global_state.active_debug_test_key
    if active_key is None:
        return

    controller = _debug_sessions.get(active_key)
    if controller is None:
        return

    editor_breakpoints, fetch_error = refresh_editor_breakpoints_cache()
    if fetch_error and not editor_breakpoints:
        return

    desired = {(fp, line) for fp, line in editor_breakpoints}
    current = set(controller._breakpoints.keys())

    to_remove = current - desired
    to_add = desired - current

    removed_count = 0
    for file_path, line in to_remove:
        try:
            if await controller.delete_breakpoint(file_path, line):
                removed_count += 1
        except Exception:
            pass

    added_count = 0
    for file_path, line in to_add:
        try:
            if await controller.insert_breakpoint(file_path, line):
                added_count += 1
        except Exception:
            pass

    if removed_count or added_count:
        test = _test_from_key(active_key)
        if test is not None:
            parts: list[str] = []
            if added_count:
                parts.append(f"+{added_count}")
            if removed_count:
                parts.append(f"-{removed_count}")
            _append_timeline_event(
                test,
                "debug_info",
                f"breakpoints updated ({', '.join(parts)})",
            )


def _test_from_key(test_key: str) -> Test | None:
    for test in state.all_tests:
        if _test_key(test) == test_key:
            return test
    return None


async def _auto_trace_step(
    controller: GdbMIController,
    stop_event: DebugStopEvent,
    binary_path: str = "",
    always_step_in: bool = False,
    same_line_count: int = 0,
    cache=None,
) -> tuple[DebugStopEvent, int]:
    if stop_event_is_terminal(stop_event):
        return stop_event, 0

    if not stop_event.file_path or stop_event.line <= 0:
        return await controller.next(), 0

    async def _step_out_until_moved(current: DebugStopEvent) -> DebugStopEvent:
        for _ in range(5):
            if stop_event_is_terminal(current):
                break
            nxt = await controller.step_out()
            if stop_event_is_terminal(nxt):
                return nxt
            if nxt.file_path != current.file_path or nxt.line != current.line:
                return nxt
            current = nxt
        return current

    in_user_code = _is_user_code_path(stop_event.file_path)
    if not in_user_code:
        return await _step_out_until_moved(stop_event), 0

    if always_step_in:
        next_event = await controller.step_in()
        if not stop_event_is_terminal(next_event):
            if not _is_user_code_path(next_event.file_path):
                return await _step_out_until_moved(next_event), 0
            # Detect getting stuck on the same line after repeated step-ins
            if (next_event.file_path == stop_event.file_path and
                next_event.line == stop_event.line and
                next_event.line > 0):
                same_line_count += 1
                if same_line_count >= 3:
                    return await _step_out_until_moved(next_event), 0
            else:
                same_line_count = 0
            entered_new_function = (next_event.function or "").strip() != (stop_event.function or "").strip()
            if entered_new_function and not _line_text(next_event.file_path, next_event.line, cache=cache).strip():
                return await _step_out_until_moved(next_event), 0
        return next_event, same_line_count

    wants_step_in = await _line_has_likely_call(stop_event.file_path, stop_event.line, binary_path, cache=cache)
    next_event = await (controller.step_in() if wants_step_in else controller.next())

    if wants_step_in and not stop_event_is_terminal(next_event) and not _is_user_code_path(next_event.file_path):
        return await _step_out_until_moved(next_event), 0

    return next_event, 0


def _latest_source_timeline_event(test: Test) -> TimelineEvent | None:
    run = test.current_run
    if run is None:
        return None
    for event in reversed(run.timeline_events):
        if event.file_path and event.line > 0:
            return event
    return None


def _stop_has_source_location(stop_event: DebugStopEvent) -> bool:
    return bool(stop_event.file_path) and stop_event.line > 0


async def _enrich_variable_types(
    variables: list[tuple[str, str, str]],
    binary_path: str,
    file_path: str,
    line: int,
    cache=None,
) -> list[tuple[str, str, str]]:
    if not binary_path or not file_path or line <= 0:
        return variables
    enriched: list[tuple[str, str, str]] = []
    for var_tuple in variables:
        if len(var_tuple) >= 3:
            name, value, _ = var_tuple
        else:
            name, value = var_tuple
        try:
            type_info = await asyncio.to_thread(
                resolve_variable_type, binary_path, name, file_path, line, cache=cache
            )
            type_hint = _format_dwarf_type(type_info) if type_info else ""
        except Exception:
            type_hint = ""
        enriched.append((name, value, type_hint))
    return enriched


async def _capture_global_variables(
    controller: GdbMIController,
    binary_path: str,
    local_variables: list[tuple[str, str, str]],
    cache=None,
) -> list[tuple[str, str, str]]:
    if not binary_path:
        return []
    try:
        index = await asyncio.to_thread(get_global_variables, binary_path, cache=cache)
    except Exception:
        return []
    if not index:
        return []

    local_names = {name for name, _, _ in local_variables}
    results: list[tuple[str, str, str]] = []
    for entry in index.values():
        try:
            if entry.name in local_names:
                continue
            value = await evaluate_global(controller, entry.name)
            if value is None:
                continue
            prefix = "[global]" if entry.linkage_name else "[static]"
            type_info = await asyncio.to_thread(
                resolve_variable_type, binary_path, entry.name, entry.file_path, entry.line, cache=cache
            )
            type_hint = _format_dwarf_type(type_info) if type_info else ""
            results.append((f"{prefix} {entry.name}", value, type_hint))
        except Exception:
            continue
    return results


async def _capture_scope_variables(
    controller: GdbMIController,
    binary_path: str = "",
    file_path: str = "",
    line: int = 0,
    cache=None,
) -> list[tuple[str, str, str]]:
    async def _expand_children(
        var_name: str,
        label_prefix: str,
        depth: int,
        max_depth: int,
    ) -> list[tuple[str, str, str]]:
        if depth > max_depth:
            return []

        children = await controller.var_list_children(var_name)
        expanded: list[tuple[str, str, str]] = []
        for child in children:
            child_var_name = str(child.get("name", ""))
            child_exp = str(child.get("exp", "") or child_var_name)
            if not child_var_name or not child_exp:
                continue

            child_value = str(child.get("value", "?"))
            if child_value in {"?", "", "{...}"}:
                evaluated = await controller.var_evaluate(child_var_name, timeout=1.0)
                if evaluated is not None:
                    child_value = evaluated
            if child_value in {"", "{...}"}:
                child_value = "?"
            label = f"{label_prefix}.{child_exp}"
            child_type = str(child.get("type", ""))
            expanded.append((label, child_value, child_type))

            child_numchild = int(child.get("numchild", 0))
            if child_numchild > 0 and depth < max_depth:
                expanded.extend(
                    await _expand_children(
                        child_var_name,
                        label,
                        depth + 1,
                        max_depth,
                    )
                )

        return expanded

    async def _expand_variable(
        name: str,
        value: str,
        type_hint: str,
        max_depth: int,
    ) -> list[tuple[str, str, str]]:
        base = [(name, value, type_hint)]

        should_expand = value == "?" or _looks_pointer_value(value)
        if not should_expand or max_depth <= 0:
            return base

        expression = name if value == "?" else f"*({name})"
        created = await controller.var_create(expression, frame="*")
        if created is None:
            return base

        try:
            numchild = int(created.get("numchild", 0))
            if numchild <= 0:
                return base
            created_type = str(created.get("type", ""))
            expanded_children = await _expand_children(
                str(created.get("name", "")),
                name,
                depth=1,
                max_depth=max_depth,
            )
            return base + expanded_children if expanded_children else base
        finally:
            await controller.var_delete(str(created.get("name", "")))

    try:
        simple_vars = await controller.list_simple_variables(timeout=1.5)
        if not simple_vars:
            simple_vars = await controller.list_all_variables(timeout=1.5)

        max_depth = max(1, int(global_state.tsv_vars_depth))
        flattened: list[tuple[str, str, str]] = []
        for var_tuple in simple_vars:
            if len(var_tuple) >= 3:
                name, value, type_hint = var_tuple
            else:
                name, value = var_tuple
                type_hint = ""
            flattened.extend(await _expand_variable(name, value, type_hint, max_depth))

        seen: set[str] = set()
        deduped: list[tuple[str, str, str]] = []
        for var_tuple in flattened:
            if len(var_tuple) >= 3:
                name, value, _type_hint = var_tuple
            else:
                name, value = var_tuple
            if name in seen:
                continue
            seen.add(name)
            deduped.append(var_tuple)

        enriched = await _enrich_variable_types(deduped, binary_path, file_path, line, cache=cache)
        return enriched[:250]
    except Exception:
        return []


async def _capture_scope_variables_fast(
    controller: GdbMIController,
    binary_path: str = "",
    file_path: str = "",
    line: int = 0,
    cache=None,
) -> list[tuple[str, str, str]]:
    """Capture lightweight frame variables without deep expansion.

    This is used for card rendering paths where we want variable visibility
    without paying the full recursive pointer/child expansion cost.
    """
    try:
        simple_vars = await controller.list_simple_variables(timeout=1.0)
        if not simple_vars:
            simple_vars = await controller.list_all_variables(timeout=1.0)

        seen: set[str] = set()
        deduped: list[tuple[str, str, str]] = []
        for var_tuple in simple_vars:
            if len(var_tuple) >= 3:
                name, value, _type_hint = var_tuple
            else:
                name, value = var_tuple
            if name in seen:
                continue
            seen.add(name)
            deduped.append(var_tuple)

        enriched = await _enrich_variable_types(deduped, binary_path, file_path, line, cache=cache)
        return enriched[:120]
    except Exception:
        return []


def _merge_trigger_matches(
    base_matches: list[TriggerMatch],
    extra_matches: list[TriggerMatch],
) -> list[TriggerMatch]:
    merged: list[TriggerMatch] = list(base_matches)
    seen = {(match.trigger_id, match.message) for match in merged}
    for match in extra_matches:
        key = (match.trigger_id, match.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(match)
    return merged


def update_annotation_cache(test: Test, event: TimelineEvent) -> None:
    """Merge event line_annotations into the current run's annotation_cache (Store A)."""
    run = test.current_run
    if run is not None:
        _merge_event_annotations_into(run.annotation_cache, event)


async def _record_stop_event(
    test: Test,
    stop_event: DebugStopEvent,
    binary_path: str = "",
    variables: list[tuple[str, str, str]] | None = None,
    trigger_matches: list[TriggerMatch] | None = None,
    debugger: GdbMIController | None = None,
    line_annotations: dict[int, list[str]] | None = None,
) -> None:
    runtime_variables = list(variables or [])
    resolved_annotations: dict[int, list[str]] = {}
    if line_annotations is not None:
        resolved_annotations = line_annotations
    elif debugger is not None and stop_event.file_path and stop_event.line > 0:
        source_line = _line_text(stop_event.file_path, stop_event.line, cache=test.dwarf_cache)
        if source_line:
            try:
                resolved_annotations = await resolve_line_annotations(
                    source_line, stop_event.line, debugger
                )
            except Exception:
                pass
    matches = list(trigger_matches or [])
    primary = matches[0] if matches else None
    message = primary.message if primary is not None else _stop_reason_message(stop_event)
    _append_timeline_event(
        test,
        "step",
        message,
        file_path=stop_event.file_path,
        line=stop_event.line,
        function=stop_event.function,
        variables=runtime_variables,
        program_counter=stop_event.program_counter,
        line_annotations=resolved_annotations,
        primary_trigger=primary.trigger_id if primary is not None else "",
        trigger_ids=[match.trigger_id for match in matches],
        trigger_label=primary.label if primary is not None else "",
        trigger_message=primary.message if primary is not None else "",
    )
    run = test.current_run
    if run is not None:
        event = run.timeline_events[-1]
        update_annotation_cache(test, event)


async def _get_or_load_lexical_scope_index(
    binary_path: str,
    cache=None,
):
    """Return the LexicalScopeIndex for *binary_path*, loading+caching on first use."""
    if not binary_path:
        return None
    abs_binary = os.path.abspath(binary_path)
    cache_dict = cache.lexical_scope_cache if cache is not None else {}
    cached = cache_dict.get(abs_binary)
    if cached is not None:
        return cached
    try:
        response = await asyncio.to_thread(
            load_dwarf_data, __import__("runner.dwarf_core.models", fromlist=["DwarfLoaderRequest"]).DwarfLoaderRequest(binary_path=abs_binary)
        )
    except Exception:
        return None
    if not getattr(response, "ok", False):
        return None
    index = getattr(response, "lexical_scope_index", None)
    if cache is not None:
        cache.lexical_scope_cache[abs_binary] = index
    return index


def _update_scope_buckets(
    test: Test,
    event: TimelineEvent,
    scope_chain: list,
    is_synthetic: bool = False,
) -> None:
    """Place *event* into the deepest scope bucket matching its file + line range.

    While execution stays inside the same deepest bucket, the last entry in
    ``latest_events`` is replaced.  When the PC leaves the bucket (or the
    current line reaches the bucket's ``end_line``) and later returns, a new
    entry is appended.

    *is_synthetic* stops (e.g. standalone-expression fabrications) never
    trigger bucket closure so that a skipped-over ``end_line`` does not
    spuriously close the bucket.
    """
    run = test.current_run
    if run is None or not event.file_path or not scope_chain:
        return
    abs_path = os.path.abspath(event.file_path)
    root = run.scope_buckets.get(abs_path)

    # Close the previously open bucket if execution left its range
    prev_open = run.open_scope_bucket
    if prev_open is not None:
        pc_left = not (prev_open.low_pc <= event.program_counter < prev_open.high_pc)
        line_at_end = event.line >= prev_open.end_line
        if pc_left or (line_at_end and not is_synthetic):
            run.open_scope_bucket = None

    # Ensure the root bucket exists (function / subprogram scope)
    func_block = scope_chain[0]
    if root is None:
        root = ScopeBucket(
            start_line=func_block.start_loc.line,
            end_line=func_block.end_loc.line,
            low_pc=func_block.low_pc,
            high_pc=func_block.high_pc,
        )
        run.scope_buckets[abs_path] = root

    current = root
    # Walk inner blocks, creating children as needed
    for block in scope_chain[1:]:
        start_line = block.start_loc.line
        end_line = block.end_loc.line
        child = None
        for c in current.children:
            if c.start_line == start_line and c.end_line == end_line:
                child = c
                break
        if child is None:
            child = ScopeBucket(
                start_line=start_line,
                end_line=end_line,
                low_pc=block.low_pc,
                high_pc=block.high_pc,
                parent=current,
            )
            current.children.append(child)
        current = child

    # Replace or append depending on whether this bucket is still open
    if run.open_scope_bucket is current:
        if current.latest_events:
            current.latest_events[-1] = event
        else:
            current.latest_events.append(event)
    else:
        current.latest_events.append(event)
        run.open_scope_bucket = current


_FAILURE_STDERR_INDICATORS = (
    "free():", "malloc():", "realloc():", "calloc():",
    "double free", "corruption", "invalid pointer", "invalid size",
    "segmentation fault", "sigsegv", "sigabrt", "assertion",
    "error:", "abort", "heap", "asan", "sanitizer",
    "munmap_chunk", "glibc detected",
)


_USER_SPACE_INDICATORS = (
    "free():", "malloc():", "realloc():", "calloc():",
    "double free", "corruption", "invalid pointer", "invalid size",
    "heap", "asan", "sanitizer", "munmap_chunk", "glibc detected",
    "assertion", "error:",
)

_SYSTEM_INDICATORS = (
    "segmentation fault", "sigsegv", "sigabrt", "abort",
    "received signal", "aborted", "fatal",
)


def _extract_failure_stderr(test: Test) -> tuple[str, str]:
    """Pull stderr / debug-log lines that explain why a test died.

    Returns (user_space_message, system_message).
    """
    run = test.current_run
    if run is None:
        return "", ""
    candidates: list[str] = []

    # Timeline stderr events (plain-binary mode)
    for event in run.timeline_events:
        if event.kind == "stderr" and event.message:
            candidates.append(event.message.strip())

    # Plain binary stderr buffer
    if run.stderr.strip():
        lines = [l.strip() for l in run.stderr.strip().splitlines() if l.strip()]
        candidates.extend(lines)

    # GDB console / log output (auto-trace mode) – target stderr arrives as &"..." records
    if run.debug_logs:
        logs = [l.strip() for l in run.debug_logs if l.strip()]
        candidates.extend(logs)

    user_errors: list[str] = []
    system_errors: list[str] = []
    for c in candidates:
        c_lower = c.lower()
        if any(ind in c_lower for ind in _USER_SPACE_INDICATORS):
            user_errors.append(c)
        elif any(ind in c_lower for ind in _SYSTEM_INDICATORS):
            system_errors.append(c)
        else:
            # Fallback: if it has any failure indicator, treat as user space
            if any(ind in c_lower for ind in _FAILURE_STDERR_INDICATORS):
                user_errors.append(c)

    user_msg = " | ".join(dict.fromkeys(user_errors))
    system_msg = " | ".join(dict.fromkeys(system_errors))
    if not user_msg and not system_msg and candidates:
        # No classified errors – treat last few candidates as system
        system_msg = " | ".join(dict.fromkeys(candidates[-3:]))
    return user_msg, system_msg


def _apply_terminal_stop(test: Test, stop_event: DebugStopEvent) -> None:
    code = stop_event.exit_code
    if stop_event.reason == "exited-normally":
        code = 0 if code is None else code
    run = test.current_run
    if run is not None:
        run.debug_exited = True
        run.debug_running = False
        run.debug_exit_code = code

    if stop_event.reason.startswith("exited") and (code is None or code == 0):
        test.state = TestState.PASSED
    else:
        test.state = TestState.FAILED
        reason = _stop_reason_message(stop_event)
        user_err, system_err = _extract_failure_stderr(test)
        parts: list[str] = []
        if user_err:
            parts.append(user_err)
        if system_err:
            parts.append(system_err)
        if parts:
            reason = f"{reason}\n{'─' * 40}\n{'\n'.join(parts)}"
        _append_timeline_event(
            test,
            "test_failed",
            f"FAIL ({stop_event.reason}): {reason}",
        )
    test.time_state_changed = time.monotonic()


def _debug_callbacks(test: Test):
    captured_run = test.current_run

    def _on_target_output(chunk: str) -> None:
        if test.current_run is not captured_run or captured_run is None:
            return
        captured_run.stdout += chunk
        captured_run.stdout_raw += chunk.encode(errors="replace")
        for line in chunk.replace("\r", "").split("\n"):
            if line:
                _append_timeline_event(test, "stdout", line, stream="stdout")

    def _on_console_output(chunk: str) -> None:
        if test.current_run is not captured_run or captured_run is None:
            return
        _append_debug_log(test, chunk)

    return _on_target_output, _on_console_output


def _ensure_debug_build_mode(enabled: bool) -> None:
    desired = bool(enabled)
    current = bool(global_state.debug_build_enabled)
    global_state.debug_build_enabled = desired

    if desired == current and not desired:
        return

    generate_makefile()
    build_project_sources()
    refresh_dependency_graph()


def restore_normal_build_mode() -> None:
    _ensure_debug_build_mode(False)


async def _terminate_active_processes() -> None:
    for controller in list(_debug_sessions.values()):
        try:
            await controller.shutdown()
        except Exception:
            pass
    _debug_sessions.clear()
    global_state.active_debug_test_key = None

    processes = {proc for proc in active_processes.values() if proc.returncode is None}
    for proc in processes:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

    if processes:
        await asyncio.gather(
            *(proc.wait() for proc in processes), return_exceptions=True
        )

    active_processes.clear()


async def _compile_binary_for_test(test: Test, proc_env: dict[str, str]) -> tuple[bool, str]:
    process_key = _test_key(test)
    binary_path = test_binary_path(test.source_path)

    _append_timeline_event(test, "compile_start", f"compiling {binary_path}")
    make_args = ["make", "-f", "test_build/Makefile"]
    force_rebuild = test.force_rebuild_once
    if global_state.debug_build_enabled or force_rebuild:
        make_args.append("-B")
    make_args.append(binary_path)
    make_proc = await asyncio.create_subprocess_exec(
        *make_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    if force_rebuild:
        test.force_rebuild_once = False
    active_processes[process_key] = make_proc
    _, make_stderr = await make_proc.communicate()
    if active_processes.get(process_key) is make_proc:
        active_processes.pop(process_key, None)

    refresh_dependency_graph()

    if make_proc.returncode != 0:
        if test.state == TestState.CANCELLED:
            return False, binary_path
        run = test.current_run
        if run is not None:
            run.compile_err = make_stderr.decode(errors="replace")
            run.compile_err_raw = make_stderr
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "compile errors present"
        _append_timeline_event(test, "compile_failed", "compile failed")
        return False, binary_path

    run = test.current_run
    if run is not None:
        run.compile_err = ""
        run.compile_err_raw = b""
    _append_timeline_event(test, "compile_ok", "compile succeeded")
    return True, binary_path


async def _run_plain_binary(test: Test, binary_path: str, proc_env: dict[str, str]) -> None:
    run_cmd = [f"./{binary_path}"]
    stdbuf_path = shutil.which("stdbuf")
    if stdbuf_path:
        run_cmd = [stdbuf_path, "-oL", "-eL", *run_cmd]

    run_proc = None
    for _ in range(10):
        try:
            run_proc = await asyncio.create_subprocess_exec(
                *run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
            break
        except OSError as e:
            if e.errno == errno.ETXTBSY:
                await asyncio.sleep(0.05)
                continue
            if e.errno == errno.ENOENT:
                run = test.current_run
                if run is not None:
                    run.stderr = f"test executable missing: ./{binary_path}"
                    run.stderr_raw = b""
                test.state = TestState.FAILED
                test.time_state_changed = time.monotonic()
                return
            raise

    if run_proc is None:
        run_proc = await asyncio.create_subprocess_exec(
            *run_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )

    process_key = _test_key(test)
    active_processes[process_key] = run_proc

    run = test.current_run
    if run is not None:
        run.stdout = ""
        run.stdout_raw = b""
        run.stderr = ""
        run.stderr_raw = b""
    _append_timeline_event(test, "run_start", f"running {binary_path}")

    async def _read_stream(
        stream: asyncio.StreamReader | None,
        dest_str: list[str],
        dest_raw: list[bytes],
        is_stdout: bool,
    ):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded_line = line.decode(errors="replace")
            dest_str.append(decoded_line)
            dest_raw.append(line)

            if is_stdout:
                if run is not None:
                    run.stdout += decoded_line
                    run.stdout_raw += line
                _append_timeline_event(test, "stdout", decoded_line.rstrip("\n"), stream="stdout")
            else:
                if run is not None:
                    run.stderr += decoded_line
                    run.stderr_raw += line
                _append_timeline_event(test, "stderr", decoded_line.rstrip("\n"), stream="stderr")

    stdout_parts: list[str] = []
    stdout_raw_parts: list[bytes] = []
    stderr_parts: list[str] = []
    stderr_raw_parts: list[bytes] = []
    await asyncio.gather(
        _read_stream(run_proc.stdout, stdout_parts, stdout_raw_parts, True),
        _read_stream(run_proc.stderr, stderr_parts, stderr_raw_parts, False),
        run_proc.wait(),
    )
    if active_processes.get(process_key) is run_proc:
        active_processes.pop(process_key, None)

    if run is not None:
        run.stdout = "".join(stdout_parts)
        run.stdout_raw = b"".join(stdout_raw_parts)
        run.stderr = "".join(stderr_parts)
        run.stderr_raw = b"".join(stderr_raw_parts)

    if test.state == TestState.CANCELLED:
        return

    if run_proc.returncode == 0:
        test.state = TestState.PASSED
        _append_timeline_event(test, "run_exit", "exited 0")
    else:
        test.state = TestState.FAILED
        _append_timeline_event(test, "run_exit", f"exited {run_proc.returncode}")
    test.time_state_changed = time.monotonic()


async def _cancel_active_run_for_manual_debug(test: Test) -> None:
    test_key = _test_key(test)
    run_task = _active_run_tasks.get(test_key)
    if run_task is None or run_task.done():
        return

    process = active_processes.get(test_key)
    if process is not None and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        finally:
            active_processes.pop(test_key, None)

    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _run_auto_debug_trace(test: Test, binary_path: str, proc_env: dict[str, str]) -> None:
    process_key = _test_key(test)
    controller = GdbMIController(f"./{binary_path}", env=proc_env)
    target_callback, console_callback = _debug_callbacks(test)
    controller.set_target_output_callback(target_callback)
    controller.set_console_output_callback(console_callback)

    run = test.run()
    run.debug_running = True
    run.debug_exited = False
    run.debug_exit_code = None
    run.timeline_selected_event_index = -1
    _append_timeline_event(test, "debug_start", f"gdb trace start: {binary_path}")

    await controller.start()
    if controller.proc is not None:
        active_processes[process_key] = controller.proc
    await controller.configure()
    test.story_filter_profile = normalized_story_filter_profile(test.story_filter_profile)
    story_filters = StoryFilterEngine.from_profile(test.story_filter_profile)

    async def _capture_story_stop(stop_event: DebugStopEvent) -> None:
        if not _stop_has_source_location(stop_event):
            story_filters.mark_processed(stop_event)
            return

        # Always collect lightweight variables and line annotations for the cache
        fast_variables = await _capture_scope_variables_fast(
            controller,
            binary_path=binary_path,
            file_path=stop_event.file_path,
            line=stop_event.line,
            cache=test.dwarf_cache,
        )
        line_annotations: dict[int, list[str]] = {}
        if stop_event.file_path and stop_event.line > 0:
            source_line = _line_text(stop_event.file_path, stop_event.line, cache=test.dwarf_cache)
            if source_line:
                try:
                    line_annotations = await resolve_line_annotations(
                        source_line, stop_event.line, controller
                    )
                except Exception:
                    pass

        # Merge annotations into the DWARF cache even for non-triggered stops
        if line_annotations:
            merge_line_annotations_into_cache(
                run.annotation_cache,
                stop_event.file_path,
                stop_event.function or "unknown",
                line_annotations,
            )

        early_decision = story_filters.evaluate_without_variables(stop_event)
        matches = list(early_decision.matches)
        variables = list(fast_variables)

        if early_decision.need_variables:
            variables = await _capture_scope_variables(
                controller,
                binary_path=binary_path,
                file_path=stop_event.file_path,
                line=stop_event.line,
                cache=test.dwarf_cache,
            )
            var_decision = story_filters.evaluate_with_variables(stop_event, variables)
            matches = _merge_trigger_matches(matches, var_decision.matches)

        if matches:
            global_vars = await _capture_global_variables(controller, binary_path, variables, cache=test.dwarf_cache)
            variables = variables + global_vars
            await _record_stop_event(
                test,
                stop_event,
                binary_path=binary_path,
                variables=variables,
                trigger_matches=matches,
                debugger=controller,
                line_annotations=line_annotations,
            )
            # Update scope buckets for the newly recorded event
            event = run.timeline_events[-1]
            scope_index = await _get_or_load_lexical_scope_index(binary_path, cache=test.dwarf_cache)
            if scope_index is not None and stop_event.program_counter:
                try:
                    scope_chain = scope_index.get_scope_chain(stop_event.program_counter)
                    if not scope_chain and stop_event.file_path and stop_event.line > 0:
                        scope_chain = scope_index.get_scope_chain_by_line(stop_event.file_path, stop_event.line)
                    if scope_chain:
                        _update_scope_buckets(test, event, scope_chain, is_synthetic=False)
                except Exception:
                    pass
        await _emit_skipped_standalone_exprs(
            test, stop_event, story_filters, controller, fast_variables, binary_path
        )
        story_filters.mark_processed(stop_event)

    stop_event = await controller.break_main_and_run()
    await _capture_story_stop(stop_event)

    max_steps = 50000
    step_count = 0
    while not stop_event_is_terminal(stop_event) and step_count < max_steps:
        if test.state == TestState.CANCELLED:
            _append_timeline_event(test, "debug_cancelled", "cancelled while tracing")
            return
        stop_event, _ = await _auto_trace_step(
            controller, stop_event, binary_path, always_step_in=False, cache=test.dwarf_cache
        )
        await _capture_story_stop(stop_event)
        step_count += 1

    if step_count >= max_steps and not stop_event_is_terminal(stop_event):
        _append_timeline_event(test, "debug_limit", f"step cap reached ({max_steps})")
        user_err, system_err = _extract_failure_stderr(test)
        reason = f"auto trace exceeded {max_steps} steps"
        parts: list[str] = []
        if user_err:
            parts.append(user_err)
        if system_err:
            parts.append(system_err)
        if parts:
            reason = f"{reason}\n{'─' * 40}\n{'\n'.join(parts)}"
        _append_timeline_event(
            test,
            "test_failed",
            f"FAIL (step-limit): {reason}",
        )
        test.state = TestState.FAILED
        run.debug_running = False
        run.debug_exited = True
        test.time_state_changed = time.monotonic()
        _persist_story_annotations(test)
        return

    if test.state == TestState.CANCELLED:
        _append_timeline_event(test, "debug_cancelled", "cancelled after trace")
        return

    if stop_event_is_terminal(stop_event):
        _apply_terminal_stop(test, stop_event)
        _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))

    await controller.shutdown()
    if active_processes.get(process_key) is controller.proc:
        active_processes.pop(process_key, None)

    _persist_story_annotations(test)


async def run_test(test: Test, on_complete: Callable[[], None]):
    process_key = _test_key(test)
    current_task = asyncio.current_task()
    try:
        if test.state == TestState.CANCELLED:
            return

        proc_env = os.environ.copy()
        proc_env["COLUMNS"] = str(max(20, subprocess_columns))

        if test.timeline_capture_enabled or global_state.timeline_capture_enabled:
            _ensure_debug_build_mode(True)

        from models import TestRun
        test.current_run = TestRun()
        binary_path = test_binary_path(test.source_path)
        try:
            current_mtime = int(os.path.getmtime(binary_path)) if os.path.exists(binary_path) else 0
        except OSError:
            current_mtime = 0
        cache = test.dwarf_cache
        if cache.last_binary_path != binary_path or cache.last_binary_mtime != current_mtime:
            cache.reset_binary_caches()
            cache.last_binary_path = binary_path
            cache.last_binary_mtime = current_mtime
        cache.reset_runtime_caches()
        _start_timeline_run(test, "scheduled")
        compiled, binary_path = await _compile_binary_for_test(test, proc_env)
        if test.state == TestState.CANCELLED:
            return
        if not compiled:
            return

        test.time_start = time.monotonic()
        run = test.run()
        run.time_start = test.time_start

        if test.timeline_capture_enabled or global_state.timeline_capture_enabled:
            await _run_auto_debug_trace(test, binary_path, proc_env)
        else:
            await _run_plain_binary(test, binary_path, proc_env)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if test.state != TestState.CANCELLED:
            run = test.current_run
            if run is not None:
                run.stderr = f"runner error: {e}"
                run.stderr_raw = b""
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
            global_state.dep_graph_ready = False
            global_state.dep_graph_reason = "runner error"
            _append_timeline_event(test, "runner_error", str(e))
            _append_timeline_event(
                test,
                "test_failed",
                f"FAIL (runner-error): {e}",
            )
    finally:
        if current_task is not None and _active_run_tasks.get(process_key) is current_task:
            _active_run_tasks.pop(process_key, None)
        if test.state != TestState.CANCELLED:
            active_processes.pop(process_key, None)
        on_complete()


def _get_debug_session(test: Test) -> GdbMIController | None:
    return _debug_sessions.get(_test_key(test))


async def start_debug_session(test: Test, precision_mode: str = "loose") -> None:
    test_key = _test_key(test)
    existing = _debug_sessions.get(test_key)
    if existing is not None:
        return

    if global_state.active_debug_test_key and global_state.active_debug_test_key != test_key:
        return

    await _cancel_active_run_for_manual_debug(test)

    _ensure_debug_build_mode(True)

    from models import TestRun
    test.current_run = TestRun()
    binary_path = test_binary_path(test.source_path)
    try:
        current_mtime = int(os.path.getmtime(binary_path)) if os.path.exists(binary_path) else 0
    except OSError:
        current_mtime = 0
    cache = test.dwarf_cache
    if cache.last_binary_path != binary_path or cache.last_binary_mtime != current_mtime:
        cache.reset_binary_caches()
        cache.last_binary_path = binary_path
        cache.last_binary_mtime = current_mtime
    cache.reset_runtime_caches()
    run = test.run()

    test.state = TestState.RUNNING
    test.time_start = time.monotonic()
    test.time_state_changed = test.time_start
    run.time_start = test.time_start
    run.debug_running = True
    run.debug_exited = False
    run.debug_exit_code = None
    test.debug_precision_mode = "precise" if precision_mode == "precise" else "loose"
    _start_timeline_run(test, "manual debug")

    proc_env = os.environ.copy()
    proc_env["COLUMNS"] = str(max(20, subprocess_columns))

    compiled, binary_path = await _compile_binary_for_test(test, proc_env)
    if not compiled:
        run.debug_running = False
        return

    controller = GdbMIController(f"./{binary_path}", env=proc_env)
    target_callback, console_callback = _debug_callbacks(test)
    controller.set_target_output_callback(target_callback)
    controller.set_console_output_callback(console_callback)

    global_state.active_debug_test_key = test_key
    _debug_sessions[test_key] = controller

    try:
        await controller.start()
        if controller.proc is not None:
            active_processes[test_key] = controller.proc
        await controller.configure()
        if test.debug_precision_mode == "precise":
            await controller.configure_manual_stepping()
        editor_breakpoints, fetch_error = refresh_editor_breakpoints_cache()
        inserted_breakpoints = 0
        for file_path, line in editor_breakpoints:
            try:
                inserted = await controller.insert_breakpoint(file_path, line)
            except Exception:
                inserted = False
            if inserted:
                inserted_breakpoints += 1

        if inserted_breakpoints > 0:
            _append_timeline_event(
                test,
                "debug_info",
                f"loaded {inserted_breakpoints} editor breakpoints",
            )
            initial_stop = await controller.run()
        else:
            if editor_breakpoints:
                _append_timeline_event(
                    test,
                    "debug_info",
                    "editor breakpoints found, but none were valid in gdb; starting at main()",
                )
            elif fetch_error:
                _append_timeline_event(
                    test,
                    "debug_info",
                    f"{fetch_error}; starting at main()",
                )
            else:
                _append_timeline_event(
                    test,
                    "debug_info",
                    "no editor breakpoints found; starting at main()",
                )
            initial_stop = await controller.break_main_and_run()
        vars_for_event: list[tuple[str, str, str]] = []
        if _stop_has_source_location(initial_stop):
            vars_for_event = await _capture_scope_variables(
                controller,
                binary_path=binary_path,
                file_path=initial_stop.file_path,
                line=initial_stop.line,
                cache=test.dwarf_cache,
            )
            global_vars = await _capture_global_variables(controller, binary_path, vars_for_event, cache=test.dwarf_cache)
            vars_for_event = vars_for_event + global_vars
        await _record_stop_event(
            test,
            initial_stop,
            binary_path=binary_path,
            variables=vars_for_event,
            debugger=controller,
        )
        if run.timeline_events:
            event = run.timeline_events[-1]
            scope_index = await _get_or_load_lexical_scope_index(binary_path, cache=test.dwarf_cache)
            if scope_index is not None and initial_stop.program_counter:
                try:
                    scope_chain = scope_index.get_scope_chain(initial_stop.program_counter)
                    if not scope_chain and initial_stop.file_path and initial_stop.line > 0:
                        scope_chain = scope_index.get_scope_chain_by_line(initial_stop.file_path, initial_stop.line)
                    if scope_chain:
                        _update_scope_buckets(test, event, scope_chain, is_synthetic=False)
                except Exception:
                    pass
        if _stop_has_source_location(initial_stop):
            save_debug_line(initial_stop.file_path, initial_stop.line)
        run.timeline_selected_event_index = -1
        _schedule_story_annotations_persist(test)

        if stop_event_is_terminal(initial_stop):
            _apply_terminal_stop(test, initial_stop)
            _append_timeline_event(test, "debug_end", _stop_reason_message(initial_stop))
            await stop_debug_session(test)
            return

    except Exception as e:
        _append_timeline_event(test, "debug_error", str(e))
        if test.state != TestState.CANCELLED:
            run.stderr = f"debug error: {e}"
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
        await stop_debug_session(test)


async def stop_debug_session(test: Test, persist_annotations: bool = True) -> None:
    test_key = _test_key(test)
    controller = _debug_sessions.pop(test_key, None)
    if controller is not None:
        await controller.shutdown()
        if active_processes.get(test_key) is controller.proc:
            active_processes.pop(test_key, None)

    run = test.current_run
    if run is not None:
        run.debug_running = False
        if not run.debug_exited:
            run.debug_exited = True
            run.debug_exit_code = None
    _append_timeline_event(test, "debug_end", "debug session stopped")

    pending = _annotation_persist_tasks.pop(test_key, None)
    if pending is not None and not pending.done():
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass

    if persist_annotations:
        _persist_story_annotations(test)

    if global_state.active_debug_test_key == test_key:
        global_state.active_debug_test_key = None


async def cancel_test_and_restore_normal_build(test: Test) -> None:
    test_key = _test_key(test)
    run_task = _active_run_tasks.get(test_key)
    has_active_run_task = run_task is not None and not run_task.done()

    test.timeline_capture_enabled = False
    if not any(t.timeline_capture_enabled for t in state.all_tests):
        global_state.timeline_capture_enabled = False

    test.cancelled_by_user = True
    test.rerun_after_user_cancel = True
    test.force_rebuild_once = True

    await stop_debug_session(test, persist_annotations=False)

    process = active_processes.get(test_key)
    if process is not None and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        finally:
            active_processes.pop(test_key, None)

    if has_active_run_task:
        test.state = TestState.CANCELLED
        test.time_state_changed = time.monotonic()
        run_task.cancel()
    else:
        test.state = TestState.PENDING
        test.time_start = 0.0
        test.time_state_changed = time.monotonic()

    run = test.current_run
    if run is not None:
        run.debug_running = False
        if not run.debug_exited:
            run.debug_exited = True
            run.debug_exit_code = None

    restore_normal_build_mode()

    if not has_active_run_task:
        state_changed()


async def _debug_step(test: Test, action: str) -> DebugStopEvent | None:
    controller = _get_debug_session(test)
    if controller is None:
        return

    precise_mode = test.debug_precision_mode == "precise"

    if action == "next":
        stop_event = await controller.next()
    elif action == "auto":
        if precise_mode:
            stop_event = await controller.next()
        else:
            latest_event = _latest_source_timeline_event(test)
            current_stop = DebugStopEvent(
                file_path=latest_event.file_path if latest_event is not None else "",
                line=latest_event.line if latest_event is not None else 0,
                program_counter=latest_event.program_counter if latest_event is not None else 0,
                function=latest_event.function if latest_event is not None else "",
            )
            stop_event, _ = await _auto_trace_step(controller, current_stop, controller.binary_path, cache=test.dwarf_cache)
    elif action == "step_in":
        stop_event = await controller.step_in()
    elif action == "step_out":
        stop_event = await controller.step_out()
    elif action == "continue":
        stop_event = await controller.continue_run()
    elif action == "interrupt":
        stop_event = await controller.interrupt()
    else:
        return None

    vars_for_event: list[tuple[str, str, str]] = []
    if _stop_has_source_location(stop_event):
        vars_for_event = await _capture_scope_variables(
            controller,
            binary_path=controller.binary_path,
            file_path=stop_event.file_path,
            line=stop_event.line,
            cache=test.dwarf_cache,
        )
        global_vars = await _capture_global_variables(controller, controller.binary_path, vars_for_event, cache=test.dwarf_cache)
        vars_for_event = vars_for_event + global_vars
    await _record_stop_event(
        test,
        stop_event,
        binary_path=controller.binary_path,
        variables=vars_for_event,
        debugger=controller,
    )
    # Update scope buckets for the newly recorded event
    run = test.current_run
    if run is not None and run.timeline_events:
        event = run.timeline_events[-1]
        scope_index = await _get_or_load_lexical_scope_index(controller.binary_path, cache=test.dwarf_cache)
        if scope_index is not None and stop_event.program_counter:
            try:
                scope_chain = scope_index.get_scope_chain(stop_event.program_counter)
                if not scope_chain and stop_event.file_path and stop_event.line > 0:
                    scope_chain = scope_index.get_scope_chain_by_line(stop_event.file_path, stop_event.line)
                if scope_chain:
                    _update_scope_buckets(test, event, scope_chain, is_synthetic=False)
            except Exception:
                pass
    if _stop_has_source_location(stop_event):
        save_debug_line(stop_event.file_path, stop_event.line)
    if run is not None:
        run.timeline_selected_event_index = -1
    _schedule_story_annotations_persist(test)
    if stop_event_is_terminal(stop_event):
        _apply_terminal_stop(test, stop_event)
        _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))
        await stop_debug_session(test)
    return stop_event


async def debug_step_next(test: Test) -> DebugStopEvent | None:
    return await _debug_step(test, "next")


async def debug_step_in(test: Test) -> DebugStopEvent | None:
    return await _debug_step(test, "step_in")


async def debug_step_out(test: Test) -> DebugStopEvent | None:
    return await _debug_step(test, "step_out")


async def debug_continue(test: Test) -> DebugStopEvent | None:
    return await _debug_step(test, "continue")


async def debug_interrupt(test: Test) -> DebugStopEvent | None:
    return await _debug_step(test, "interrupt")


async def debug_interrupt_nowait(test: Test) -> None:
    controller = _get_debug_session(test)
    if controller is None:
        return
    await controller.interrupt_nowait()


def is_debug_active(test: Test) -> bool:
    return _get_debug_session(test) is not None


def get_debug_session(test: Test) -> GdbMIController | None:
    return _get_debug_session(test)


def state_changed():
    tests_to_run: list[Test] = []
    pending_tests = sorted(
        [test for test in state.all_tests if test.state == TestState.PENDING],
        key=lambda t: t.time_state_changed,
    )

    while state.available_runners > 0 and len(pending_tests) > 0:
        test = pending_tests.pop()
        state.available_runners -= 1
        test.state = TestState.RUNNING
        test.time_start = 0.0
        test.time_state_changed = time.monotonic()
        tests_to_run.append(test)

    for test in tests_to_run:

        def on_complete(completed_test: Test = test):
            state.available_runners += 1
            if completed_test.state == TestState.CANCELLED:
                if completed_test.rerun_after_user_cancel:
                    completed_test.state = TestState.PENDING
                    completed_test.cancelled_by_user = False
                    completed_test.rerun_after_user_cancel = False
                    completed_test.time_start = 0.0
                    completed_test.time_state_changed = time.monotonic()
                elif completed_test.cancelled_by_user:
                    completed_test.cancelled_by_user = False
                    completed_test.time_start = 0.0
                    completed_test.time_state_changed = time.monotonic()
                else:
                    completed_test.state = TestState.PENDING
                    completed_test.time_start = 0.0
                    completed_test.time_state_changed = time.monotonic()
            state_changed()

        run_task = asyncio.ensure_future(run_test(test, on_complete))
        _active_run_tasks[_test_key(test)] = run_task

    if len(tests_to_run) > 0:
        state_changed()
