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
# Source-line helpers
# ---------------------------------------------------------------------------

def _load_source_lines(file_path: str, cache=None) -> list[str]:
    line_cache = cache.source_line_cache if cache is not None else {}
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


def merge_line_annotations_into_cache(
    cache: dict[str, dict[str, dict[int, dict[str, str]]]],
    file_path: str,
    function: str,
    line_annotations: dict[int, list[str]],
) -> None:
    """Merge raw line_annotations into a Store A cache without a TimelineEvent."""
    if not file_path or not line_annotations:
        return
    abs_path = os.path.abspath(file_path)
    func_cache = cache.setdefault(function or "unknown", {})
    file_cache = func_cache.setdefault(abs_path, {})
    for line_no, annotation_strs in line_annotations.items():
        line_cache = file_cache.setdefault(line_no, {})
        line_cache.update(_parse_annotation_tokens(annotation_strs))


def _merge_event_annotations_into(
    cache: dict[str, dict[str, dict[int, dict[str, str]]]],
    event,
) -> None:
    """Merge a single event's line_annotations into a Store A cache."""
    if event.kind != "step" or not event.file_path or event.line <= 0:
        return
    merge_line_annotations_into_cache(
        cache,
        event.file_path,
        event.function or "unknown",
        event.line_annotations or {},
    )


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

def _compute_story_annotations(test: Test, event_boundary: int | None = None) -> dict[str, dict[int, list[str]]]:
    """Compute inline annotations from timeline event line_annotations."""
    run = test.current_run
    if run is None:
        return {}

    if event_boundary is not None:
        aggregate = False
        boundary = event_boundary
    else:
        aggregate = run.aggregate_annotations
        boundary = run.timeline_selected_event_index

    if aggregate and run.annotation_cache:
        return _cache_to_annotations(run.annotation_cache)

    events = run.timeline_events
    if not aggregate and boundary >= 0:
        events = events[: boundary + 1]

    temp_cache: dict[str, dict[str, dict[int, dict[str, str]]]] = {}
    for event in events:
        _merge_event_annotations_into(temp_cache, event)

    return _cache_to_annotations(temp_cache)


# ---------------------------------------------------------------------------
# Public API with caching
# ---------------------------------------------------------------------------

def get_story_annotations(test: Test, event_boundary: int | None = None, cache=None) -> dict[str, dict[int, list[str]]]:
    """Return cached inline annotations for the test.

    The result is a mapping of:
        absolute_file_path -> line_number -> [annotation_strings]

    When ``event_boundary`` is provided, annotations are computed only from
    timeline events up to (and including) that index, regardless of the
    test's ``aggregate_annotations`` setting.  This is used in card-stack
    mode so that each visible card shows its own accumulated annotation
    history.
    """
    run = test.current_run
    test_key = os.path.abspath(test.source_path)
    event_count = len(run.timeline_events) if run is not None else 0
    if event_boundary is not None:
        aggregate = False
        boundary = event_boundary
    else:
        aggregate = run.aggregate_annotations if run is not None else True
        boundary = run.timeline_selected_event_index if run is not None else -1

    cache_key = (test_key, event_count, aggregate, boundary)
    ann_cache = cache.annotation_cache if cache is not None else {}
    cached = ann_cache.get(cache_key)
    if cached is not None:
        return cached

    annotations = _compute_story_annotations(test, event_boundary=event_boundary)
    ann_cache[cache_key] = annotations
    return annotations


def invalidate_story_annotation_cache(test: Test, cache=None) -> None:
    """Remove cached annotations for a test (e.g. after new events)."""
    if cache is not None:
        cache.annotation_cache.clear()


# ---------------------------------------------------------------------------
# Format helpers for persistence / consumers
# ---------------------------------------------------------------------------

def format_story_annotations_for_db(
    annotations: dict[str, dict[int, list[str]]],
    cache=None,
) -> dict[str, list[list]]:
    """Convert annotation dict to db.json list format.

    Output shape: {abs_path: [[lineText, lineNo, [str, ...]], ...]}
    """
    result: dict[str, list[list]] = {}
    for file_path, line_map in annotations.items():
        sorted_lines = sorted(line_map.items())
        result[file_path] = [
            [_line_text(file_path, line, cache=cache).strip(), line, list(arr)]
            for line, arr in sorted_lines
        ]
    return result
