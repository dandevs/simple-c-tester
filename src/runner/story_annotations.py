import os
import re

import state as global_state
from models import Test
from .dwarf_core import DwarfCoreApi, DwarfLoaderRequest, create_dwarf_core_api
from .artifacts import test_binary_path

_C_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "return", "goto", "sizeof", "typeof",
    "int", "char", "float", "double", "void", "long", "short",
    "signed", "unsigned", "const", "static", "extern", "inline",
    "struct", "union", "enum", "typedef", "volatile", "register",
    "auto", "restrict", "_Bool", "_Complex", "_Imaginary",
    "NULL", "true", "false",
}

_VAR_EXPR_RE = re.compile(r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*")


# ---------------------------------------------------------------------------
# Normalisation / extraction helpers
# ---------------------------------------------------------------------------

def _normalize_expr(expr: str) -> str:
    """Normalise a C expression for lookup (e.g. table->count -> table.count)."""
    expr = re.sub(r"\s*->\s*", ".", expr)
    expr = re.sub(r"\s*\.\s*", ".", expr)
    return expr


def _extract_variable_expressions(line: str) -> list[str]:
    """Extract potential variable/member expressions from a C source line."""
    seen: set[str] = set()
    expressions: list[str] = []
    for match in _VAR_EXPR_RE.finditer(line):
        expr = match.group(0)
        normalized = _normalize_expr(expr)
        root = normalized.split(".")[0]
        if root in _C_KEYWORDS:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        expressions.append(expr)
    return expressions


# ---------------------------------------------------------------------------
# Annotation string builders
# ---------------------------------------------------------------------------

def _build_line_annotations(line: str, variables: list[tuple[str, str]]) -> str:
    """Build inline annotation string for a source line, e.g. '[table.count=5] [count=5]'."""
    if not variables:
        return ""

    expressions = _extract_variable_expressions(line)
    if not expressions:
        return ""

    var_map: dict[str, str] = {}
    for name, value in variables:
        var_map[_normalize_expr(name)] = value

    annotations: list[str] = []
    seen: set[str] = set()
    for expr in expressions:
        normalized = _normalize_expr(expr)
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized in var_map:
            value = var_map[normalized]
            display_value = value if len(value) <= 40 else value[:37] + "..."
            annotations.append(f"[{expr}={display_value}]")

    return " ".join(annotations)


def _build_resolved_annotations(
    resolved_annotations: list[tuple[str, str, str]],
) -> str:
    """Build inline annotation string from resolver output for current frame."""
    if not resolved_annotations:
        return ""

    annotations: list[str] = []
    seen: set[str] = set()
    for name, value, _availability in resolved_annotations:
        if not name or name in seen:
            continue
        seen.add(name)
        display_value = value if len(value) <= 40 else value[:37] + "..."
        annotations.append(f"[{name}={display_value}]")

    return " ".join(annotations)


# ---------------------------------------------------------------------------
# Source-line helpers (self-contained cache)
# ---------------------------------------------------------------------------

_source_line_cache: dict[str, list[str]] = {}


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


# ---------------------------------------------------------------------------
# Manual-debug detection
# ---------------------------------------------------------------------------

def _is_manual_debug_mode(test: Test) -> bool:
    for event in reversed(test.timeline_events):
        if event.kind == "run_start":
            return "manual debug" in event.message.lower()
    return False


# ---------------------------------------------------------------------------
# Annotatable-line gating
# ---------------------------------------------------------------------------

def _annotatable_lines(events: list) -> dict[tuple[str, str], set[int]]:
    """Determine which lines per (file, function) should be annotated.

    Returns a mapping of (abs_path, function_name) -> set of line numbers.

    Rules:
      - Auto story: per (file, function), all lines from min event line
        to max event line (inclusive), so gaps between visited events
        are also annotated.
      - Manual debug: the current (file, function) additionally gets
        all lines from 1 up to the latest event line in that function.
    """
    # Per-function event range
    func_range: dict[tuple[str, str], tuple[int, int]] = {}
    latest_event: dict[tuple[str, str], int] = {}

    for ev in events:
        if ev.kind != "step":
            continue
        if not ev.file_path or ev.line <= 0:
            continue
        key = (os.path.abspath(ev.file_path), ev.function or "")
        min_line, max_line = func_range.get(key, (ev.line, ev.line))
        func_range[key] = (min(min_line, ev.line), max(max_line, ev.line))
        latest_event[key] = max(latest_event.get(key, 0), ev.line)

    result: dict[tuple[str, str], set[int]] = {}
    for key, (lo, hi) in func_range.items():
        result[key] = set(range(lo, hi + 1))

    return result


# ---------------------------------------------------------------------------
# DWARF scope-index enrichment (returns flat variable list, not lines)
# ---------------------------------------------------------------------------

_dwarf_core_api: DwarfCoreApi = create_dwarf_core_api()


def _compute_scope_enriched_vars(
    test: Test,
    captured_vars: dict[str, str],
) -> dict[str, str]:
    """Use DWARF scope index to find additional captured expressions.

    For each top-level variable that DWARF says is alive anywhere in the
    binary, include all captured gdb expressions rooted at that variable.
    Returns a flat mapping of normalised_name -> value.
    """
    enriched = dict(captured_vars)
    if not captured_vars:
        return enriched

    binary_path = test_binary_path(test.source_path)
    if not os.path.isfile(binary_path):
        return enriched

    try:
        from .dwarf_core import DwarfLoaderRequest
        loader_response = _dwarf_core_api.load(DwarfLoaderRequest(binary_path=binary_path))
    except Exception:
        return enriched

    if not loader_response.ok or not loader_response.scope_index.file_lines:
        return enriched

    alive_roots: set[str] = set()
    for line_map in loader_response.scope_index.file_lines.values():
        for alive_names in line_map.values():
            for var_name in alive_names:
                alive_roots.add(_normalize_expr(var_name))

    for root in alive_roots:
        for cap_name, cap_value in captured_vars.items():
            if cap_name.startswith(root + ".") and cap_name not in enriched:
                enriched[cap_name] = cap_value

    return enriched


# ---------------------------------------------------------------------------
# Variable-history compression
# ---------------------------------------------------------------------------

def _parse_annotation(ann: str) -> tuple[str, str] | None:
    """Parse '[expr=value]' -> (expr, value).  Returns None for non-standard shapes."""
    if not ann.startswith("[") or not ann.endswith("]"):
        return None
    inner = ann[1:-1]
    idx = inner.find("=")
    if idx < 0:
        return None
    return inner[:idx], inner[idx + 1 :]


def _compress_var_history(annotations: list[str], max_history: int) -> list[str]:
    """Collapse multiple [var=val] entries for the same variable into
    [var=latest,prev,prev2].  Non-standard annotations pass through untouched.
    """
    if max_history <= 0:
        return annotations

    parsed: list[tuple[str | None, str]] = []
    for ann in annotations:
        p = _parse_annotation(ann)
        if p is None:
            parsed.append((None, ann))
        else:
            parsed.append(p)

    # Group by variable name, preserving chronological order
    groups: dict[str, list[str]] = {}
    other: list[str] = []
    for name, value in parsed:
        if name is None:
            other.append(value)
            continue
        groups.setdefault(name, []).append(value)

    result: list[str] = []
    for name, values in groups.items():
        if len(values) > max_history:
            # newest first
            kept = values[-max_history:][::-1]
            result.append(f"[{name}={','.join(kept)}]")
        else:
            for v in values:
                result.append(f"[{name}={v}]")

    result.extend(other)
    return result


# ---------------------------------------------------------------------------
# Main annotation pipeline
# ---------------------------------------------------------------------------

def _compute_story_annotations(test: Test) -> dict[str, dict[int, list[str]]]:
    """Compute inline annotations gated by execution history."""
    annotations_by_file: dict[str, dict[int, list[str]]] = {}
    events = test.timeline_events
    aggregate = getattr(test, "aggregate_annotations", True)
    boundary = test.timeline_selected_event_index
    if not aggregate and boundary >= 0:
        events = events[: boundary + 1]

    is_manual = _is_manual_debug_mode(test)

    # ------------------------------------------------------------------
    # Manual debug: single source of truth = latest event.
    # Annotate all lines from file-start up to current PC in the current
    # function.  Do NOT iterate historical events (their resolved_anno
    # would leak stale values onto their original lines).
    # ------------------------------------------------------------------
    if is_manual:
        latest_event = None
        for ev in reversed(events):
            if ev.kind == "step" and ev.file_path and ev.line > 0:
                latest_event = ev
                break
        if latest_event is None:
            return {}

        file_path = os.path.abspath(latest_event.file_path)
        func = latest_event.function or ""
        key = (file_path, func)
        source_lines = _load_source_lines(file_path)
        if not source_lines:
            return {}

        # annotatable = every line in this function up to current PC
        annotatable_lines: set[int] = set()
        max_line = 0
        for ev in events:
            if ev.kind != "step":
                continue
            if not ev.file_path or ev.line <= 0:
                continue
            if os.path.abspath(ev.file_path) == file_path and (ev.function or "") == func:
                annotatable_lines.add(ev.line)
        if annotatable_lines:
            max_line = max(annotatable_lines)
            annotatable_lines = set(range(1, max_line + 1))

        # Build variable pool from the latest event only
        pool: dict[str, str] = {}
        for name, value in (latest_event.variables or []):
            pool[_normalize_expr(name)] = value
        enriched = _compute_scope_enriched_vars(test, pool)

        vars_for_line: list[tuple[str, str]] = []
        seen_vars: set[str] = set()
        for name, value in pool.items():
            if name not in seen_vars:
                seen_vars.add(name)
                vars_for_line.append((name, value))
        for name, value in enriched.items():
            if name not in seen_vars:
                root = name.split(".")[0]
                if root in pool:
                    seen_vars.add(name)
                    vars_for_line.append((name, value))

        for line_no in annotatable_lines:
            line_text = source_lines[line_no - 1]
            annotation = ""
            if line_no == latest_event.line and latest_event.resolved_annotations:
                annotation = _build_resolved_annotations(latest_event.resolved_annotations)
            if not annotation and vars_for_line:
                annotation = _build_line_annotations(line_text, vars_for_line)
            if not annotation:
                continue
            annotations_by_file.setdefault(file_path, {})[line_no] = [annotation]

        max_hist = max(1, int(global_state.tsv_var_history))
        for line_map in annotations_by_file.values():
            for line_no in list(line_map.keys()):
                line_map[line_no] = _compress_var_history(line_map[line_no], max_hist)

        return annotations_by_file

    # ------------------------------------------------------------------
    # Auto story: per-event annotation (each card shows its own snapshot).
    # ------------------------------------------------------------------

    # Determine which lines are annotatable per (file, function)
    annotatable = _annotatable_lines(events)

    # Build per-function variable pools from captured events
    function_vars: dict[tuple[str, str], dict[str, str]] = {}
    for ev in events:
        if ev.kind != "step":
            continue
        if not ev.file_path or ev.line <= 0:
            continue
        key = (os.path.abspath(ev.file_path), ev.function or "")
        pool = function_vars.setdefault(key, {})
        for name, value in (ev.variables or []):
            pool[_normalize_expr(name)] = value

    flat_captured: dict[str, str] = {}
    for pool in function_vars.values():
        flat_captured.update(pool)
    enriched = _compute_scope_enriched_vars(test, flat_captured)

    lines_above = max(0, int(global_state.tsv_lines_above))
    lines_below = max(0, int(global_state.tsv_lines_below))

    for event in events:
        if event.kind != "step":
            continue
        if not event.file_path or event.line <= 0:
            continue

        file_path = os.path.abspath(event.file_path)
        func = event.function or ""
        key = (file_path, func)
        annotatable_lines = annotatable.get(key, set())
        if not annotatable_lines:
            continue

        source_lines = _load_source_lines(file_path)
        if not source_lines:
            continue

        snippet_start = max(1, event.line - lines_above)
        snippet_end = min(len(source_lines), event.line + lines_below)
        pool = function_vars.get(key, {})

        vars_for_line: list[tuple[str, str]] = []
        seen_vars: set[str] = set()
        for name, value in pool.items():
            if name not in seen_vars:
                seen_vars.add(name)
                vars_for_line.append((name, value))
        for name, value in enriched.items():
            if name not in seen_vars:
                root = name.split(".")[0]
                if root in pool:
                    seen_vars.add(name)
                    vars_for_line.append((name, value))

        if aggregate:
            for other_key, other_pool in function_vars.items():
                if other_key[0] == file_path and other_key != key:
                    for name, value in other_pool.items():
                        if name not in seen_vars:
                            seen_vars.add(name)
                            vars_for_line.append((name, value))

        for line_no in range(snippet_start, snippet_end + 1):
            if line_no not in annotatable_lines:
                continue

            annotation = ""
            if line_no == event.line and event.resolved_annotations:
                annotation = _build_resolved_annotations(event.resolved_annotations)
            if not annotation and vars_for_line:
                line_text = source_lines[line_no - 1]
                annotation = _build_line_annotations(line_text, vars_for_line)
            if not annotation:
                continue
            if file_path not in annotations_by_file:
                annotations_by_file[file_path] = {}
            annotations_by_file[file_path].setdefault(line_no, []).append(annotation)

    # Compress variable history + deduplicate per line
    max_hist = max(1, int(global_state.tsv_var_history))
    for line_map in annotations_by_file.values():
        for line_no in list(line_map.keys()):
            compressed = _compress_var_history(line_map[line_no], max_hist)
            line_map[line_no] = list(dict.fromkeys(compressed))

    return annotations_by_file


# ---------------------------------------------------------------------------
# Public API with caching
# ---------------------------------------------------------------------------

_MAX_ANNOTATION_CACHE_SIZE = 256
_annotation_cache: dict[tuple[str, int, bool, int], dict[str, dict[int, list[str]]]] = {}


def get_story_annotations(test: Test) -> dict[str, dict[int, list[str]]]:
    """Return cached inline annotations for the test.

    The result is a mapping of:
        absolute_file_path -> line_number -> [annotation_strings]
    """
    test_key = os.path.abspath(test.source_path)
    event_count = len(test.timeline_events)
    aggregate = getattr(test, "aggregate_annotations", True)
    boundary = test.timeline_selected_event_index

    cache_key = (test_key, event_count, aggregate, boundary)
    cached = _annotation_cache.get(cache_key)
    if cached is not None:
        return cached

    if len(_annotation_cache) >= _MAX_ANNOTATION_CACHE_SIZE:
        _annotation_cache.clear()

    annotations = _compute_story_annotations(test)
    _annotation_cache[cache_key] = annotations
    return annotations


def invalidate_story_annotation_cache(test: Test) -> None:
    """Remove cached annotations for a test (e.g. after new events)."""
    test_key = os.path.abspath(test.source_path)
    keys_to_remove = [k for k in _annotation_cache if k[0] == test_key]
    for key in keys_to_remove:
        del _annotation_cache[key]


# ---------------------------------------------------------------------------
# Format helpers for persistence / consumers
# ---------------------------------------------------------------------------

def format_story_annotations_for_db(
    annotations: dict[str, dict[int, list[str]]]
) -> dict[str, list[list]]:
    """Convert annotation dict to db.json list format.

    Output shape: {abs_path: [[lineText, lineNo, [str, ...]], ...]}
    """
    result: dict[str, list[list]] = {}
    for file_path, line_map in annotations.items():
        sorted_lines = sorted(line_map.items())
        result[file_path] = [
            [_line_text(file_path, line).strip(), line, list(arr)]
            for line, arr in sorted_lines
        ]
    return result
