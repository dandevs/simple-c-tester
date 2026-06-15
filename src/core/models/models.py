"""Domain models for the C test runner.

This module is the single home for the data model.  It is part of the
``core`` layer and has NO dependency on the legacy global ``state`` module,
``api``, or ``ui``.  All configuration that the models need is passed in
explicitly (dependency injection), never read from globals.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .enum import TestState

# Defaults used when no explicit configuration is supplied.  These mirror the
# historical global defaults from the legacy ``state`` module so behaviour is
# unchanged when callers omit the parameters.
DEFAULT_DEBUG_PRECISION_MODE = "precise"
DEFAULT_STORY_FILTER_PROFILE = "balanced"


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
    line_annotations: dict[int, dict[str, str]] = field(default_factory=dict)
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
    signal_name: str = ""  # e.g. "SIGSEGV" when the test process was killed by a signal
    # {func_name: {abs_file_path: {line_no: {var_name: value}}}}
    annotation_cache: dict[str, dict[str, dict[int, dict[str, str]]]] = field(
        default_factory=dict
    )
    # UI render cache: keyed by (width, max_lines, prefix_len) ->
    # (content_signature, rendered_lines, render_meta).  Automatically
    # invalidated when a fresh TestRun is created (every run).
    output_box_cache: dict[tuple, tuple] = field(default_factory=dict)


@dataclass
class DwarfCache:
    """Caches for DWARF metadata and runtime annotations.

    Binary metadata caches (dwarf_loader, function_index, global_index,
    type_index) are expensive to build and persist across runs when the binary
    is unchanged. Runtime caches (source_line, annotation) are reset every run.
    """

    dwarf_loader_cache: dict[str, Any] = field(default_factory=dict)
    function_index_cache: dict[str, Any] = field(default_factory=dict)
    global_index_cache: dict[str, Any] = field(default_factory=dict)
    type_index_cache: dict[str, Any] = field(default_factory=dict)
    source_line_cache: dict[str, list[str]] = field(default_factory=dict)
    annotation_cache: dict[tuple, dict] = field(default_factory=dict)
    last_binary_path: str = ""
    last_binary_mtime: int = 0

    def reset_binary_caches(self) -> None:
        """Clear caches tied to binary metadata."""
        self.dwarf_loader_cache.clear()
        self.function_index_cache.clear()
        self.global_index_cache.clear()
        self.type_index_cache.clear()

    def reset_runtime_caches(self) -> None:
        """Clear caches tied to runtime source/annotation data."""
        self.source_line_cache.clear()
        self.annotation_cache.clear()


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
    debug_precision_mode: str = DEFAULT_DEBUG_PRECISION_MODE
    story_filter_profile: str = DEFAULT_STORY_FILTER_PROFILE
    # persisted full-file annotations (db.json)
    story_annotations: dict[str, list[list]] = field(default_factory=dict)
    # All per-run mutable state lives here.  Replaced on every run.
    current_run: TestRun | None = None
    dwarf_cache: DwarfCache = field(default_factory=DwarfCache)

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
    """The discovered test tree plus the runner pool size.

    ``populate_suites`` no longer reads global defaults; the desired default
    debug-precision mode and story-filter profile must be passed in by the
    caller (typically from a :class:`~core.config.RunnerConfig`).
    """

    root_suite: Suite = field(default_factory=lambda: Suite(name="root"))
    all_suites: list[Suite] = field(default_factory=list)
    all_tests: list[Test] = field(default_factory=list)
    available_runners = 0

    def populate_suites(
        self,
        path: str,
        *,
        debug_precision_mode: str = DEFAULT_DEBUG_PRECISION_MODE,
        story_filter_profile: str = DEFAULT_STORY_FILTER_PROFILE,
    ) -> None:
        """Discover ``*.c`` test files under ``path``.

        ``debug_precision_mode`` and ``story_filter_profile`` are the defaults
        applied to every newly discovered test; callers should source them from
        a :class:`~core.config.RunnerConfig`.
        """
        root = Path(path)
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                self.root_suite.children.append(
                    self._build_suite(
                        entry,
                        debug_precision_mode=debug_precision_mode,
                        story_filter_profile=story_filter_profile,
                    )
                )
            elif entry.suffix == ".c":
                test = self._make_test(
                    entry,
                    debug_precision_mode=debug_precision_mode,
                    story_filter_profile=story_filter_profile,
                )
                self.root_suite.tests.append(test)
                self.all_tests.append(test)

    def _build_suite(
        self,
        dir_path: Path,
        *,
        debug_precision_mode: str,
        story_filter_profile: str,
    ) -> Suite:
        suite = Suite(name=dir_path.name)
        self.all_suites.append(suite)
        for entry in sorted(dir_path.iterdir()):
            if entry.is_dir():
                suite.children.append(
                    self._build_suite(
                        entry,
                        debug_precision_mode=debug_precision_mode,
                        story_filter_profile=story_filter_profile,
                    )
                )
            elif entry.suffix == ".c":
                test = self._make_test(
                    entry,
                    debug_precision_mode=debug_precision_mode,
                    story_filter_profile=story_filter_profile,
                )
                suite.tests.append(test)
                self.all_tests.append(test)
        return suite

    @staticmethod
    def _make_test(
        entry: Path,
        *,
        debug_precision_mode: str,
        story_filter_profile: str,
    ) -> Test:
        return Test(
            name=entry.stem,
            source_path=str(entry),
            debug_precision_mode=debug_precision_mode,
            story_filter_profile=story_filter_profile,
        )
