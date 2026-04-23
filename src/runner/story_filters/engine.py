import os

from ..debugger import DebugStopEvent
from .config import StoryFilterConfig, config_for_story_profile
from .triggers import (
    StoryFilterRuntimeState,
    StoryStopContext,
    TriggerMatch,
    evaluate_trigger,
    trigger_needs_variables,
)


def _load_source_lines(file_path: str, source_cache: dict[str, list[str]]) -> list[str]:
    cached = source_cache.get(file_path)
    if cached is not None:
        return cached
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    source_cache[file_path] = lines
    return lines


def _line_text(file_path: str, line_number: int, source_cache: dict[str, list[str]]) -> str:
    if not file_path or line_number <= 0:
        return ""
    abs_path = os.path.abspath(file_path)
    lines = _load_source_lines(abs_path, source_cache)
    if line_number > len(lines):
        return ""
    return lines[line_number - 1]


class StoryFilterDecision:
    __slots__ = ("emit", "matches", "need_variables")

    def __init__(self, emit: bool, matches: list[TriggerMatch], need_variables: bool) -> None:
        self.emit = emit
        self.matches = matches
        self.need_variables = need_variables


class StoryFilterEngine:
    def __init__(self, config: StoryFilterConfig):
        self.config = config
        self.runtime_state = StoryFilterRuntimeState()
        self.previous_stop: DebugStopEvent | None = None
        self.source_cache: dict[str, list[str]] = {}

    @classmethod
    def from_profile(cls, profile: str) -> "StoryFilterEngine":
        return cls(config_for_story_profile(profile))

    def _build_context(
        self,
        stop_event: DebugStopEvent,
        variables: list[tuple[str, str, str]] | None,
    ) -> StoryStopContext:
        previous = self.previous_stop
        line_text = _line_text(stop_event.file_path, stop_event.line, self.source_cache)
        previous_line_text = ""
        if previous is not None:
            previous_line_text = _line_text(
                previous.file_path,
                previous.line,
                self.source_cache,
            )
        return StoryStopContext(
            stop_event=stop_event,
            previous_stop=previous,
            line_text=line_text,
            previous_line_text=previous_line_text,
            variables=list(variables or []),
        )

    def evaluate_without_variables(self, stop_event: DebugStopEvent) -> StoryFilterDecision:
        self.runtime_state.step_index += 1
        context = self._build_context(stop_event, variables=[])
        matches: list[TriggerMatch] = []
        needs_vars = False

        for trigger_id in self.config.enabled_triggers:
            if trigger_needs_variables(trigger_id):
                if self.runtime_state.step_index % max(1, self.config.anomaly_sample_every) == 0:
                    needs_vars = True
                continue
            match = evaluate_trigger(
                trigger_id,
                context,
                self.runtime_state,
                self.config.loop_every_n,
            )
            if match is not None:
                matches.append(match)

        return StoryFilterDecision(emit=bool(matches), matches=matches, need_variables=needs_vars)

    def evaluate_with_variables(
        self,
        stop_event: DebugStopEvent,
        variables: list[tuple[str, str, str]],
    ) -> StoryFilterDecision:
        context = self._build_context(stop_event, variables=variables)
        matches: list[TriggerMatch] = []
        for trigger_id in self.config.enabled_triggers:
            if not trigger_needs_variables(trigger_id):
                continue
            match = evaluate_trigger(
                trigger_id,
                context,
                self.runtime_state,
                self.config.loop_every_n,
            )
            if match is not None:
                matches.append(match)
        return StoryFilterDecision(emit=bool(matches), matches=matches, need_variables=False)

    def mark_processed(self, stop_event: DebugStopEvent) -> None:
        self.previous_stop = stop_event
