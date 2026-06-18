"""Compatibility shim — re-exports the annotation resolver from ``core.story``."""

from core.story.annotation_resolver import (  # noqa: F401
    _evaluate_single_expression,
    _has_side_effects,
    resolve_line_annotations,
    resolve_line_annotations_sync,
)
