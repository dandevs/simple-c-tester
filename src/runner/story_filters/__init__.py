"""Compatibility shim — re-exports the story filter engine from ``core.story.filters``."""

from core.story.filters import (  # noqa: F401
    VALID_STORY_FILTER_PROFILES,
    StoryFilterConfig,
    StoryFilterDecision,
    StoryFilterEngine,
    TriggerMatch,
    _is_standalone_expression_line,
    config_for_story_profile,
    normalized_story_filter_profile,
    story_filter_profile_triggers,
)
