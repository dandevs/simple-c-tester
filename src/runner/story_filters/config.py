from dataclasses import dataclass


VALID_STORY_FILTER_PROFILES = ("minimal", "balanced", "all")


def normalized_story_filter_profile(value: str | None) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in VALID_STORY_FILTER_PROFILES:
            return lowered
    return "balanced"


def story_filter_profile_triggers(profile: str) -> tuple[str, ...]:
    normalized = normalized_story_filter_profile(profile)
    if normalized == "minimal":
        return (
            "function_enter",
            "function_exit",
            "assert_failure",
            "anomaly",
        )
    if normalized == "all":
        return (
            "function_enter",
            "function_exit",
            "branch_decision",
            "loop_milestone",
            "goto_jump",
            "assert_line",
            "assert_failure",
            "anomaly",
            "sync_event",
            "first_hit_function",
            "first_hit_line",
            "standalone_expr",
            "return_statement",
        )
    return (
        "function_enter",
        "function_exit",
        "branch_decision",
        "loop_milestone",
        "goto_jump",
        "assert_line",
        "assert_failure",
        "anomaly",
        "sync_event",
        "first_hit_function",
        "standalone_expr",
        "return_statement",
    )


@dataclass(frozen=True)
class StoryFilterConfig:
    profile: str
    enabled_triggers: tuple[str, ...]
    loop_every_n: int = 10
    anomaly_sample_every: int = 4


def config_for_story_profile(profile: str) -> StoryFilterConfig:
    normalized = normalized_story_filter_profile(profile)
    return StoryFilterConfig(
        profile=normalized,
        enabled_triggers=story_filter_profile_triggers(normalized),
    )
