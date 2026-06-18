"""Compatibility shim — re-exports the story annotation pipeline from ``core.story``."""

from core.story.annotations import (  # noqa: F401
    _compute_story_annotations,
    _format_annotation,
    _line_text,
    _load_source_lines,
    _merge_event_annotations_into,
    _normalize_expr,
    format_story_annotations_for_db,
    get_story_annotations,
    invalidate_story_annotation_cache,
    merge_line_annotations_into_cache,
)
