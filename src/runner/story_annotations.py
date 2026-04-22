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
# Accumulator formatting
# ---------------------------------------------------------------------------

def _format_var_history(values: list[str], max_history: int) -> str:
    """Format a variable's history into '[name=v1,v2,v3]'."""
    if not values:
        return ""
    if max_history <= 0:
        display = values
    elif len(values) > max_history:
        display = values[-max_history:]
    else:
        display = values
    return ",".join(display)


def _format_accumulator_for_display(
    accumulator: dict[str, dict[str, dict[int, dict[str, list[str]]]]]
) -> dict[str, dict[int, list[str]]]:
    """Convert the raw accumulator into the display format.

    Input:  {file_path: {function: {line: {var: [values]}}}}
    Output: {file_path: {line: ["[var=v1,v2]", ...]}}
    """
    result: dict[str, dict[int, list[str]]] = {}
    max_hist = max(1, int(global_state.tsv_var_history))

    for file_path, func_map in accumulator.items():
        file_result: dict[int, list[str]] = {}
        for _func, line_map in func_map.items():
            for line_no, var_map in line_map.items():
                annotations: list[str] = []
                for var_name, values in var_map.items():
                    history_str = _format_var_history(values, max_hist)
                    if history_str:
                        annotations.append(f"[{var_name}={history_str}]")
                if annotations:
                    file_result.setdefault(line_no, []).extend(annotations)
        if file_result:
            result[file_path] = file_result

    return result


# ---------------------------------------------------------------------------
# Formatted annotation cache
#
# Snapshots are immutable append-only. Once formatted, a snapshot never
# changes, so cache entries never need invalidation.
# ---------------------------------------------------------------------------

_MAX_FORMATTED_CACHE_SIZE = 512
_formatted_cache: dict[tuple[str, int], dict[str, dict[int, list[str]]]] = {}


def _cache_key_for_test(test: Test) -> str:
    return os.path.abspath(test.source_path)


def _get_cached_formatted(
    test: Test, snapshot_index: int
) -> dict[str, dict[int, list[str]]] | None:
    test_key = _cache_key_for_test(test)
    return _formatted_cache.get((test_key, snapshot_index))


def _set_cached_formatted(
    test: Test, snapshot_index: int, formatted: dict[str, dict[int, list[str]]]
) -> None:
    test_key = _cache_key_for_test(test)
    if len(_formatted_cache) >= _MAX_FORMATTED_CACHE_SIZE:
        _formatted_cache.clear()
    _formatted_cache[(test_key, snapshot_index)] = formatted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_story_annotations(test: Test) -> dict[str, dict[int, list[str]]]:
    """Return inline annotations from the current accumulator state."""
    cached = _get_cached_formatted(test, -1)
    if cached is not None:
        return cached
    result = _format_accumulator_for_display(test.annotations_accumulator)
    _set_cached_formatted(test, -1, result)
    return result


def get_story_annotations_for_snapshot(
    test: Test, snapshot_index: int
) -> dict[str, dict[int, list[str]]]:
    """Return inline annotations from a specific snapshot."""
    if snapshot_index < 0 or snapshot_index >= len(test.annotation_snapshots):
        return {}
    cached = _get_cached_formatted(test, snapshot_index)
    if cached is not None:
        return cached
    result = _format_accumulator_for_display(test.annotation_snapshots[snapshot_index])
    _set_cached_formatted(test, snapshot_index, result)
    return result


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
