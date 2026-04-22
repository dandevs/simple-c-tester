import os
import re

import state as global_state
from models import Test


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
_ANNOTATION_TOKEN_RE = re.compile(r"\[([^=\]]+)=([^\]]*)\]")


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


def _merge_latest_annotations(annotation_strs: list[str]) -> list[str]:
    """Parse annotation tokens and keep only the latest value per expression, preserving last-seen order."""
    latest: dict[str, str] = {}
    order: list[str] = []
    for s in annotation_strs:
        for match in _ANNOTATION_TOKEN_RE.finditer(s):
            expr = match.group(1)
            value = match.group(2)
            if expr not in latest:
                order.append(expr)
            latest[expr] = value
    if not latest:
        return []
    return [" ".join([f"[{expr}={latest[expr]}]" for expr in order])]


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
# Annotation Cache (Store A)
# ---------------------------------------------------------------------------
# Per-function, per-file, per-line latest variable values.
# Structure: {func_name: {abs_file_path: {line_no: {var_name: value}}}}


def update_annotation_cache(test: Test, event) -> None:
    """Update Store (A) after a debugger step.

    Each variable's value is stored at the exact source line where the
    debugger stopped, keyed by the current function so that ``main``'s ``i``
    does not clobber ``foo``'s ``i``.
    """
    if event.kind != "step" or not event.file_path or event.line <= 0:
        return

    func = event.function or "global"
    file_path = os.path.abspath(event.file_path)
    line_no = event.line

    func_cache = test.annotation_cache.setdefault(func, {})
    file_cache = func_cache.setdefault(file_path, {})
    line_cache = file_cache.setdefault(line_no, {})

    for name, value in (event.variables or []):
        line_cache[_normalize_expr(name)] = value


def _cache_to_annotations(
    cache: dict[str, dict[str, dict[int, dict[str, str]]]],
) -> dict[str, dict[int, list[str]]]:
    """Convert Store (A) into the legacy annotation dict format.

    Merges across all function scopes for a given file/line, keeping the
    latest value per variable name.
    """
    merged: dict[str, dict[int, dict[str, str]]] = {}
    for func_map in cache.values():
        for file_path, line_map in func_map.items():
            if file_path not in merged:
                merged[file_path] = {}
            for line_no, var_map in line_map.items():
                merged_line = merged[file_path].setdefault(line_no, {})
                for var_name, value in var_map.items():
                    merged_line[var_name] = value

    result: dict[str, dict[int, list[str]]] = {}
    for file_path, line_map in merged.items():
        result[file_path] = {}
        for line_no, var_map in line_map.items():
            if not var_map:
                continue
            line_text = _line_text(file_path, line_no)
            annotation = _build_line_annotations(line_text, list(var_map.items()))
            if annotation:
                result[file_path][line_no] = [annotation]

    return result


def _replay_events_to_cache(
    events,
) -> dict[str, dict[str, dict[int, dict[str, str]]]]:
    """Replay a slice of events into a temporary Store (A) snapshot."""
    cache: dict[str, dict[str, dict[int, dict[str, str]]]] = {}
    for event in events:
        if event.kind != "step" or not event.file_path or event.line <= 0:
            continue
        func = event.function or "global"
        file_path = os.path.abspath(event.file_path)
        line_no = event.line
        func_cache = cache.setdefault(func, {})
        file_cache = func_cache.setdefault(file_path, {})
        line_cache = file_cache.setdefault(line_no, {})
        for name, value in (event.variables or []):
            line_cache[_normalize_expr(name)] = value
    return cache


# ---------------------------------------------------------------------------
# Main annotation pipeline
# ---------------------------------------------------------------------------

def _compute_story_annotations(test: Test) -> dict[str, dict[int, list[str]]]:
    """Compute inline annotations from Store (A) or by replaying events."""
    aggregate = getattr(test, "aggregate_annotations", True)
    boundary = test.timeline_selected_event_index

    if aggregate:
        # Aggregate mode: display Store (A) as-is — only lines where the
        # debugger actually stopped are annotated.  No DWARF supplementation.
        cache = test.annotation_cache
        return _cache_to_annotations(cache)

    # Per-frame mode: replay events up to the selected card into a
    # temporary Store (A), then annotate ONLY lines that have an explicit
    # cache entry.  Unvisited lines in the snippet window remain blank.
    events = test.timeline_events
    if boundary >= 0:
        events = events[: boundary + 1]
    cache = _replay_events_to_cache(events)

    # Ensure the current frame's line is present with its latest values
    target_event = None
    if events:
        target_event = events[-1]
    if target_event and target_event.kind == "step" and target_event.file_path and target_event.line > 0:
        func = target_event.function or "global"
        file_path = os.path.abspath(target_event.file_path)
        line_no = target_event.line
        func_cache = cache.setdefault(func, {})
        file_cache = func_cache.setdefault(file_path, {})
        line_cache = file_cache.setdefault(line_no, {})
        for name, value in (target_event.variables or []):
            line_cache[_normalize_expr(name)] = value

    return _cache_to_annotations(cache)


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
