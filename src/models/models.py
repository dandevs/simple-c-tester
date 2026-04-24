from dataclasses import dataclass, field
from pathlib import Path
from .enum import TestState


@dataclass
class TimelineEvent:
    index: int = 0
    timestamp: float = 0.0
    kind: str = ""
    message: str = ""
    file_path: str = ""
    line: int = 0
    program_counter: int = 0
    function: str = ""
    stream: str = ""
    variables: list[tuple[str, str, str]] = field(default_factory=list)
    line_annotations: dict[int, list[str]] = field(default_factory=dict)
    primary_trigger: str = ""
    trigger_ids: list[str] = field(default_factory=list)
    trigger_label: str = ""
    trigger_message: str = ""


@dataclass
class TestRun:
    """Mutable state for a single test execution.  A fresh instance is created
    for every run so that stale async tasks from previous runs cannot corrupt
    the current run's data."""

    time_start: float = 0.0
    time_state_changed: float = 0.0
    stdout: str = ""
    stdout_raw: bytes = b""
    stderr: str = ""
    stderr_raw: bytes = b""
    compile_err: str = ""
    compile_err_raw: bytes = b""
    timeline_events: list[TimelineEvent] = field(default_factory=list)
    debug_logs: list[str] = field(default_factory=list)
    debug_running: bool = False
    debug_exited: bool = False
    debug_exit_code: int | None = None
    aggregate_annotations: bool = True
    timeline_selected_event_index: int = -1
    # {func_name: {abs_file_path: {line_no: {var_name: value}}}}
    annotation_cache: dict[str, dict[str, dict[int, dict[str, str]]]] = field(
        default_factory=dict
    )


@dataclass
class Test:
    name: str = ""
    time_start: float = 0.0
    time_state_changed: float = 0.0
    state: TestState = TestState.PENDING
    qeueued: bool = False
    dependencies: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    source_path: str = ""
    # timeline_capture_enabled is a user preference, not per-run state
    timeline_capture_enabled: bool = False
    cancelled_by_user: bool = False
    rerun_after_user_cancel: bool = False
    force_rebuild_once: bool = False
    debug_precision_mode: str = "precise"
    story_filter_profile: str = "balanced"
    # persisted full-file annotations (db.json)
    story_annotations: dict[str, list[list]] = field(default_factory=dict)
    # All per-run mutable state lives here.  Replaced on every run.
    current_run: TestRun | None = None

    def run(self) -> TestRun:
        """Return the active TestRun, raising if none exists.

        Callers that create a run (``run_test``, ``start_debug_session``)
        must assign ``test.current_run = TestRun()`` before calling any
        downstream helpers.
        """
        if self.current_run is None:
            raise RuntimeError(
                f"Test {self.source_path!r} has no current_run. "
                "Ensure a fresh TestRun() is created before starting execution."
            )
        return self.current_run


@dataclass
class Suite:
    name: str = ""
    tests: list[Test] = field(default_factory=list)
    children: list["Suite"] = field(default_factory=list)


@dataclass
class AppState:
    root_suite: Suite = field(default_factory=lambda: Suite(name="root"))
    all_suites: list[Suite] = field(default_factory=list)
    all_tests: list[Test] = field(default_factory=list)
    available_runners = 0

    def populate_suites(self, path: str) -> None:
        root = Path(path)
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                self.root_suite.children.append(self._build_suite(entry, path))
            elif entry.suffix == ".c":
                import state as global_state

                test = Test(
                    name=entry.stem,
                    source_path=str(entry),
                    debug_precision_mode=global_state.debug_precision_mode_preference,
                    story_filter_profile=global_state.story_filter_profile_preference,
                )
                self.root_suite.tests.append(test)
                self.all_tests.append(test)

    def _build_suite(self, dir_path: Path, base_path: str) -> Suite:
        suite = Suite(name=dir_path.name)
        self.all_suites.append(suite)
        for entry in sorted(dir_path.iterdir()):
            if entry.is_dir():
                suite.children.append(self._build_suite(entry, base_path))
            elif entry.suffix == ".c":
                import state as global_state

                test = Test(
                    name=entry.stem,
                    source_path=str(entry),
                    debug_precision_mode=global_state.debug_precision_mode_preference,
                    story_filter_profile=global_state.story_filter_profile_preference,
                )
                suite.tests.append(test)
                self.all_tests.append(test)
        return suite
