import errno
import os
import shutil
import time
import asyncio
import re
from pathlib import Path
from typing import Callable

import state as global_state
from state import state, active_processes, subprocess_columns
from models import Test, TestState, TimelineEvent
from .makefile import (
    refresh_dependency_graph,
    generate_makefile,
    build_project_sources,
)
from .artifacts import test_binary_path
from .debugger import GdbMIController, DebugStopEvent, stop_event_is_terminal


MAX_TIMELINE_EVENTS = 12000
MAX_DEBUG_LOG_LINES = 4000
_debug_sessions: dict[str, GdbMIController] = {}
_active_run_tasks: dict[str, asyncio.Task] = {}
_source_line_cache: dict[str, list[str]] = {}
_user_function_cache: set[str] = set()
_user_function_cache_key: tuple[tuple[str, int], ...] | None = None

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


def _looks_pointer_value(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped in {"0x0", "(nil)", "nullptr"}:
        return False
    return stripped.startswith("0x")


def _test_key(test: Test) -> str:
    return os.path.abspath(test.source_path)


def _append_debug_log(test: Test, message: str) -> None:
    if not message:
        return
    for line in message.replace("\r", "").split("\n"):
        if line:
            test.debug_logs.append(line)
    if len(test.debug_logs) > MAX_DEBUG_LOG_LINES:
        test.debug_logs = test.debug_logs[-MAX_DEBUG_LOG_LINES:]


def _append_timeline_event(
    test: Test,
    kind: str,
    message: str,
    file_path: str = "",
    line: int = 0,
    function: str = "",
    stream: str = "",
    variables: list[tuple[str, str]] | None = None,
) -> None:
    event = TimelineEvent(
        index=len(test.timeline_events) + 1,
        timestamp=time.monotonic(),
        kind=kind,
        message=message,
        file_path=file_path,
        line=line,
        function=function,
        stream=stream,
        variables=list(variables or []),
    )
    test.timeline_events.append(event)
    if len(test.timeline_events) > MAX_TIMELINE_EVENTS:
        test.timeline_events = test.timeline_events[-MAX_TIMELINE_EVENTS:]


def _start_timeline_run(test: Test, reason: str) -> None:
    run_index = (
        sum(1 for event in test.timeline_events if event.kind == "run_start") + 1
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


def _load_source_lines(file_path: str) -> list[str]:
    cached = _source_line_cache.get(file_path)
    if cached is not None:
        return cached

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []

    _source_line_cache[file_path] = lines
    return lines


def _line_text(file_path: str, line_number: int) -> str:
    if not file_path or line_number <= 0:
        return ""
    lines = _load_source_lines(file_path)
    if line_number > len(lines):
        return ""
    return lines[line_number - 1]


def _line_has_likely_call(file_path: str, line_number: int) -> bool:
    line = _line_text(file_path, line_number)
    if not line:
        return False
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False

    call_names = [
        match.group(1)
        for match in _CALL_RE.finditer(stripped)
        if match.group(1) not in _CONTROL_WORDS
    ]
    if not call_names:
        return False

    user_functions = _discover_user_function_names()
    if not user_functions:
        return False

    return any(name in user_functions for name in call_names)


def _discover_user_function_names() -> set[str]:
    global _user_function_cache_key
    global _user_function_cache

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


async def _auto_trace_step(controller: GdbMIController, stop_event: DebugStopEvent) -> DebugStopEvent:
    if stop_event_is_terminal(stop_event):
        return stop_event

    if not stop_event.file_path or stop_event.line <= 0:
        return await controller.next()

    in_user_code = _is_user_code_path(stop_event.file_path)
    if not in_user_code:
        return await controller.step_out()

    wants_step_in = _line_has_likely_call(stop_event.file_path, stop_event.line)
    next_event = await (controller.step_in() if wants_step_in else controller.next())

    if wants_step_in and not stop_event_is_terminal(next_event) and not _is_user_code_path(next_event.file_path):
        return await controller.step_out()

    return next_event


def _stop_has_source_location(stop_event: DebugStopEvent) -> bool:
    return bool(stop_event.file_path) and stop_event.line > 0


async def _capture_scope_variables(controller: GdbMIController) -> list[tuple[str, str]]:
    async def _expand_children(
        var_name: str,
        label_prefix: str,
        depth: int,
        max_depth: int,
    ) -> list[tuple[str, str]]:
        if depth > max_depth:
            return []

        children = await controller.var_list_children(var_name)
        expanded: list[tuple[str, str]] = []
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
            expanded.append((label, child_value))

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
        max_depth: int,
    ) -> list[tuple[str, str]]:
        base = [(name, value)]

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
        flattened: list[tuple[str, str]] = []
        for name, value in simple_vars:
            flattened.extend(await _expand_variable(name, value, max_depth))

        seen: set[str] = set()
        deduped: list[tuple[str, str]] = []
        for name, value in flattened:
            if name in seen:
                continue
            seen.add(name)
            deduped.append((name, value))

        return deduped[:250]
    except Exception:
        return []


class _SequentialVarCaptureDecider:
    def __init__(self, skip_seq: int):
        self.skip_seq = max(1, skip_seq)
        self.prev_abs_path = ""
        self.prev_function = ""
        self.prev_line = 0
        self.seq_since_emit = 0

    def should_capture(self, stop_event: DebugStopEvent) -> bool:
        if not _stop_has_source_location(stop_event):
            return False

        current_abs_path = os.path.abspath(stop_event.file_path)
        if self.prev_line <= 0:
            self.prev_abs_path = current_abs_path
            self.prev_function = stop_event.function
            self.prev_line = stop_event.line
            self.seq_since_emit = 0
            return True

        same_file = current_abs_path == self.prev_abs_path
        same_function = stop_event.function == self.prev_function
        is_sequential = same_file and same_function and stop_event.line == (self.prev_line + 1)

        include = False
        if is_sequential:
            self.seq_since_emit += 1
            if self.seq_since_emit >= self.skip_seq:
                include = True
                self.seq_since_emit = 0
        else:
            include = True
            self.seq_since_emit = 0

        self.prev_abs_path = current_abs_path
        self.prev_function = stop_event.function
        self.prev_line = stop_event.line
        return include


def _record_stop_event(
    test: Test,
    stop_event: DebugStopEvent,
    variables: list[tuple[str, str]] | None = None,
) -> None:
    _append_timeline_event(
        test,
        "step",
        _stop_reason_message(stop_event),
        file_path=stop_event.file_path,
        line=stop_event.line,
        function=stop_event.function,
        variables=variables,
    )


def _apply_terminal_stop(test: Test, stop_event: DebugStopEvent) -> None:
    code = stop_event.exit_code
    if stop_event.reason == "exited-normally":
        code = 0 if code is None else code
    test.debug_exited = True
    test.debug_running = False
    test.debug_exit_code = code

    if stop_event.reason.startswith("exited") and (code is None or code == 0):
        test.state = TestState.PASSED
    else:
        test.state = TestState.FAILED
    test.time_state_changed = time.monotonic()


def _debug_callbacks(test: Test):
    def _on_target_output(chunk: str) -> None:
        test.stdout += chunk
        test.stdout_raw += chunk.encode(errors="replace")
        for line in chunk.replace("\r", "").split("\n"):
            if line:
                _append_timeline_event(test, "stdout", line, stream="stdout")

    def _on_console_output(chunk: str) -> None:
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
        test.compile_err = make_stderr.decode(errors="replace")
        test.compile_err_raw = make_stderr
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "compile errors present"
        _append_timeline_event(test, "compile_failed", "compile failed")
        return False, binary_path

    test.compile_err = ""
    test.compile_err_raw = b""
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
                test.stderr = f"test executable missing: ./{binary_path}"
                test.stderr_raw = b""
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

    test.stdout = ""
    test.stdout_raw = b""
    test.stderr = ""
    test.stderr_raw = b""
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
                test.stdout += decoded_line
                test.stdout_raw += line
                _append_timeline_event(test, "stdout", decoded_line.rstrip("\n"), stream="stdout")
            else:
                test.stderr += decoded_line
                test.stderr_raw += line
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

    test.stdout = "".join(stdout_parts)
    test.stdout_raw = b"".join(stdout_raw_parts)
    test.stderr = "".join(stderr_parts)
    test.stderr_raw = b"".join(stderr_raw_parts)

    if run_proc.returncode == 0:
        test.state = TestState.PASSED
        _append_timeline_event(test, "run_exit", "exited 0")
    else:
        test.state = TestState.FAILED
        _append_timeline_event(test, "run_exit", f"exited {run_proc.returncode}")
    test.time_state_changed = time.monotonic()


async def _run_auto_debug_trace(test: Test, binary_path: str, proc_env: dict[str, str]) -> None:
    process_key = _test_key(test)
    controller = GdbMIController(f"./{binary_path}", env=proc_env)
    target_callback, console_callback = _debug_callbacks(test)
    controller.set_target_output_callback(target_callback)
    controller.set_console_output_callback(console_callback)

    test.debug_running = True
    test.debug_exited = False
    test.debug_exit_code = None
    _append_timeline_event(test, "debug_start", f"gdb trace start: {binary_path}")

    await controller.start()
    if controller.proc is not None:
        active_processes[process_key] = controller.proc
    await controller.configure()
    var_capture = _SequentialVarCaptureDecider(global_state.tsv_skip_seq_lines)

    stop_event = await controller.break_main_and_run()
    vars_for_event = await _capture_scope_variables(controller) if var_capture.should_capture(stop_event) else []
    _record_stop_event(test, stop_event, vars_for_event)

    max_steps = 50000
    step_count = 0
    while not stop_event_is_terminal(stop_event) and step_count < max_steps:
        if test.state == TestState.CANCELLED:
            _append_timeline_event(test, "debug_cancelled", "cancelled while tracing")
            return
        stop_event = await _auto_trace_step(controller, stop_event)
        vars_for_event = await _capture_scope_variables(controller) if var_capture.should_capture(stop_event) else []
        _record_stop_event(test, stop_event, vars_for_event)
        step_count += 1

    if step_count >= max_steps and not stop_event_is_terminal(stop_event):
        _append_timeline_event(test, "debug_limit", f"step cap reached ({max_steps})")
        test.state = TestState.FAILED
        test.debug_running = False
        test.debug_exited = True
        test.time_state_changed = time.monotonic()
        return

    if stop_event_is_terminal(stop_event):
        _apply_terminal_stop(test, stop_event)
        _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))

    await controller.shutdown()
    if active_processes.get(process_key) is controller.proc:
        active_processes.pop(process_key, None)


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

        _start_timeline_run(test, "scheduled")
        compiled, binary_path = await _compile_binary_for_test(test, proc_env)
        if test.state == TestState.CANCELLED:
            return
        if not compiled:
            return

        test.time_start = time.monotonic()
        test.stdout = ""
        test.stdout_raw = b""
        test.stderr = ""
        test.stderr_raw = b""
        test.debug_logs = []

        if test.timeline_capture_enabled or global_state.timeline_capture_enabled:
            await _run_auto_debug_trace(test, binary_path, proc_env)
        else:
            await _run_plain_binary(test, binary_path, proc_env)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if test.state != TestState.CANCELLED:
            test.stderr = f"runner error: {e}"
            test.stderr_raw = b""
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
            global_state.dep_graph_ready = False
            global_state.dep_graph_reason = "runner error"
            _append_timeline_event(test, "runner_error", str(e))
    finally:
        if current_task is not None and _active_run_tasks.get(process_key) is current_task:
            _active_run_tasks.pop(process_key, None)
        if test.state != TestState.CANCELLED:
            active_processes.pop(process_key, None)
        on_complete()


def _get_debug_session(test: Test) -> GdbMIController | None:
    return _debug_sessions.get(_test_key(test))


async def start_debug_session(test: Test, auto_trace: bool = True) -> None:
    test_key = _test_key(test)
    existing = _debug_sessions.get(test_key)
    if existing is not None:
        return

    if global_state.active_debug_test_key and global_state.active_debug_test_key != test_key:
        return

    _ensure_debug_build_mode(True)

    test.state = TestState.RUNNING
    test.time_start = time.monotonic()
    test.time_state_changed = test.time_start
    test.stdout = ""
    test.stdout_raw = b""
    test.stderr = ""
    test.stderr_raw = b""
    test.debug_logs = []
    test.debug_running = True
    test.debug_exited = False
    test.debug_exit_code = None
    _start_timeline_run(test, "manual debug")

    proc_env = os.environ.copy()
    proc_env["COLUMNS"] = str(max(20, subprocess_columns))

    compiled, binary_path = await _compile_binary_for_test(test, proc_env)
    if not compiled:
        test.debug_running = False
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
        var_capture = _SequentialVarCaptureDecider(1)
        initial_stop = await controller.break_main_and_run()
        vars_for_event = await _capture_scope_variables(controller) if var_capture.should_capture(initial_stop) else []
        _record_stop_event(test, initial_stop, vars_for_event)

        if stop_event_is_terminal(initial_stop):
            _apply_terminal_stop(test, initial_stop)
            _append_timeline_event(test, "debug_end", _stop_reason_message(initial_stop))
            await stop_debug_session(test)
            return

        if auto_trace:
            await debug_continue_auto_trace(test)
    except Exception as e:
        _append_timeline_event(test, "debug_error", str(e))
        test.stderr = f"debug error: {e}"
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        await stop_debug_session(test)


async def stop_debug_session(test: Test) -> None:
    test_key = _test_key(test)
    controller = _debug_sessions.pop(test_key, None)
    if controller is not None:
        await controller.shutdown()
        if active_processes.get(test_key) is controller.proc:
            active_processes.pop(test_key, None)

    test.debug_running = False
    if not test.debug_exited:
        test.debug_exited = True
        test.debug_exit_code = None
        _append_timeline_event(test, "debug_end", "debug session stopped")
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

    await stop_debug_session(test)

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

    test.debug_running = False
    if not test.debug_exited:
        test.debug_exited = True
        test.debug_exit_code = None

    restore_normal_build_mode()

    if not has_active_run_task:
        state_changed()


async def _debug_step(test: Test, action: str) -> None:
    controller = _get_debug_session(test)
    if controller is None:
        return

    if action == "next":
        stop_event = await controller.next()
    elif action == "step_in":
        stop_event = await controller.step_in()
    elif action == "step_out":
        stop_event = await controller.step_out()
    elif action == "continue":
        stop_event = await controller.continue_run()
    elif action == "interrupt":
        stop_event = await controller.interrupt()
    else:
        return

    vars_for_event = await _capture_scope_variables(controller)
    _record_stop_event(test, stop_event, vars_for_event)
    if stop_event_is_terminal(stop_event):
        _apply_terminal_stop(test, stop_event)
        _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))
        await stop_debug_session(test)


async def debug_step_next(test: Test) -> None:
    await _debug_step(test, "next")


async def debug_step_in(test: Test) -> None:
    await _debug_step(test, "step_in")


async def debug_step_out(test: Test) -> None:
    await _debug_step(test, "step_out")


async def debug_continue(test: Test) -> None:
    await _debug_step(test, "continue")


async def debug_interrupt(test: Test) -> None:
    await _debug_step(test, "interrupt")


async def debug_continue_auto_trace(test: Test) -> None:
    def _latest_line_event() -> TimelineEvent | None:
        for event in reversed(test.timeline_events):
            if event.file_path and event.line > 0:
                return event
        return None

    max_steps = 50000
    var_capture = _SequentialVarCaptureDecider(global_state.tsv_skip_seq_lines)
    for _ in range(max_steps):
        if not test.debug_running:
            return
        controller = _get_debug_session(test)
        if controller is None:
            return
        latest_event = _latest_line_event()
        current_stop = DebugStopEvent(
            file_path=latest_event.file_path if latest_event is not None else "",
            line=latest_event.line if latest_event is not None else 0,
            function=latest_event.function if latest_event is not None else "",
        )
        stop_event = await _auto_trace_step(controller, current_stop)
        vars_for_event = await _capture_scope_variables(controller) if var_capture.should_capture(stop_event) else []
        _record_stop_event(test, stop_event, vars_for_event)
        if stop_event_is_terminal(stop_event):
            _apply_terminal_stop(test, stop_event)
            _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))
            await stop_debug_session(test)
            return

    _append_timeline_event(test, "debug_limit", f"step cap reached ({max_steps})")
    test.state = TestState.FAILED
    test.time_state_changed = time.monotonic()
    await stop_debug_session(test)


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
