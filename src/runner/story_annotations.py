import os
import re

from models import Test


_ANNOTATION_TOKEN_RE = re.compile(r"\[([^=\]]+)=([^\]]*)\]")


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------

def _normalize_expr(expr: str) -> str:
    """Normalise a C expression for lookup (e.g. table->count -> table.count)."""
    expr = re.sub(r"\s*->\s*", ".", expr)
    expr = re.sub(r"\s*\.\s*", ".", expr)
    return expr


# ---------------------------------------------------------------------------
# Annotation string builders
# ---------------------------------------------------------------------------

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
# Cache helpers
# ---------------------------------------------------------------------------

def _parse_annotation_tokens(annotation_strs: list[str]) -> dict[str, str]:
    """Parse '[expr=value]' strings into a mapping of expr -> value."""
    result: dict[str, str] = {}
    for s in annotation_strs:
        for match in _ANNOTATION_TOKEN_RE.finditer(s):
            result[match.group(1)] = match.group(2)
    return result


def _merge_event_annotations_into(
    cache: dict[str, dict[str, dict[int, dict[str, str]]]],
    event,
) -> None:
    """Merge a single event's line_annotations into a Store A cache."""
    if event.kind != "step" or not event.file_path or event.line <= 0:
        return
    func_name = event.function or "unknown"
    file_path = os.path.abspath(event.file_path)
    func_cache = cache.setdefault(func_name, {})
    file_cache = func_cache.setdefault(file_path, {})
    for line_no, annotation_strs in (event.line_annotations or {}).items():
        line_cache = file_cache.setdefault(line_no, {})
        line_cache.update(_parse_annotation_tokens(annotation_strs))


def _cache_to_annotations(
    cache: dict[str, dict[str, dict[int, dict[str, str]]]]
) -> dict[str, dict[int, list[str]]]:
    """Convert Store A cache to the public annotation format."""
    result: dict[str, dict[int, list[str]]] = {}
    for func_cache in cache.values():
        for file_path, line_map in func_cache.items():
            file_result = result.setdefault(file_path, {})
            for line_no, expr_map in line_map.items():
                if not expr_map:
                    continue
                file_result[line_no] = [
                    " ".join([f"[{expr}={expr_map[expr]}]" for expr in expr_map])
                ]
    return result


# ---------------------------------------------------------------------------
# Main annotation pipeline
# ---------------------------------------------------------------------------

def _compute_story_annotations(test: Test) -> dict[str, dict[int, list[str]]]:
    """Compute inline annotations from timeline event line_annotations."""
    aggregate = getattr(test, "aggregate_annotations", True)

    if aggregate and test.annotation_cache:
        return _cache_to_annotations(test.annotation_cache)

    boundary = test.timeline_selected_event_index
    events = test.timeline_events
    if not aggregate and boundary >= 0:
        events = events[: boundary + 1]

    temp_cache: dict[str, dict[str, dict[int, dict[str, str]]]] = {}
    for event in events:
        _merge_event_annotations_into(temp_cache, event)

    return _cache_to_annotations(temp_cache)


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
