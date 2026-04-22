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
# DWARF scope-index path
# ---------------------------------------------------------------------------

_dwarf_core_api: DwarfCoreApi = create_dwarf_core_api()


def _compute_scope_annotations(
    test: Test,
    captured_vars: dict[str, str],
) -> dict[str, dict[int, list[str]]]:
    """Use DWARF scope index to build full-file variable annotations."""
    result: dict[str, dict[int, list[str]]] = {}
    if not captured_vars:
        return result

    binary_path = test_binary_path(test.source_path)
    if not os.path.isfile(binary_path):
        return result

    try:
        from .dwarf_core import DwarfLoaderRequest
        loader_response = _dwarf_core_api.load(DwarfLoaderRequest(binary_path=binary_path))
    except Exception:
        return result

    if not loader_response.ok or not loader_response.scope_index.file_lines:
        return result

    for file_path, line_map in loader_response.scope_index.file_lines.items():
        abs_path = os.path.abspath(file_path)
        source_lines = _load_source_lines(abs_path)
        if not source_lines:
            continue

        for line_no, alive_names in line_map.items():
            if line_no <= 0 or line_no > len(source_lines):
                continue

            matched_vars: list[tuple[str, str]] = []
            for var_name in alive_names:
                normalized = _normalize_expr(var_name)
                if normalized in captured_vars:
                    matched_vars.append((var_name, captured_vars[normalized]))
                for cap_name, cap_value in captured_vars.items():
                    if cap_name.startswith(normalized + "."):
                        matched_vars.append((cap_name, cap_value))

            if not matched_vars:
                continue

            line_text = source_lines[line_no - 1]
            annotation = _build_line_annotations(line_text, matched_vars)
            if not annotation:
                continue

            if abs_path not in result:
                result[abs_path] = {}
            result[abs_path].setdefault(line_no, []).append(annotation)

    return result


# ---------------------------------------------------------------------------
# Main annotation pipeline
# ---------------------------------------------------------------------------

def _compute_story_annotations(test: Test) -> dict[str, dict[int, list[str]]]:
    """Compute inline annotations for all source lines based on timeline events."""
    annotations_by_file: dict[str, dict[int, list[str]]] = {}
    events = test.timeline_events
    aggregate = getattr(test, "aggregate_annotations", True)
    boundary = test.timeline_selected_event_index
    if not aggregate and boundary >= 0:
        events = events[: boundary + 1]

    # Build captured variable map from relevant events
    captured_vars: dict[str, str] = {}
    for ev in events:
        if ev.kind != "step":
            continue
        for name, value in (ev.variables or []):
            captured_vars[_normalize_expr(name)] = value

    # DWARF scope index path
    scope_result = _compute_scope_annotations(test, captured_vars)
    for file_path, line_map in scope_result.items():
        if file_path not in annotations_by_file:
            annotations_by_file[file_path] = {}
        for line_no, arr in line_map.items():
            annotations_by_file[file_path].setdefault(line_no, []).extend(arr)

    # Fallback / supplement: snippet-window regex-based annotations
    merged_vars: list[tuple[str, str]] | None = None
    if aggregate:
        merged_vars = list(captured_vars.items()) if captured_vars else None

    lines_above = max(0, int(global_state.tsv_lines_above))
    lines_below = max(0, int(global_state.tsv_lines_below))

    for event in events:
        if event.kind != "step":
            continue
        if not event.file_path or event.line <= 0:
            continue

        file_path = os.path.abspath(event.file_path)
        source_lines = _load_source_lines(file_path)
        if not source_lines:
            continue

        snippet_start = max(1, event.line - lines_above)
        snippet_end = min(len(source_lines), event.line + lines_below)
        vars_for_line = merged_vars if aggregate else event.variables

        for line_no in range(snippet_start, snippet_end + 1):
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

    # Deduplicate per line while preserving order
    for line_map in annotations_by_file.values():
        for line_no in list(line_map.keys()):
            line_map[line_no] = list(dict.fromkeys(line_map[line_no]))

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
