"""Public API for the C test runner — headless-usable.

This is the clean surface that separates the engine (systems) from the UI.
A script can construct a :class:`TestRunner`, discover tests, run them, and
drive a debug session — with zero dependency on Textual or any render code.

Architecture
------------
``core``      pure domain logic (models, build, debugger, dwarf, story, …)
``api``       orchestration + public facade  (this package)
``ui``        Textual TUI (depends on ``api``)

The orchestration implementation lives in :mod:`api._runner` (relocated from
the legacy ``runner/execute.py``).  During the refactor it still reads the
transitional global ``state`` module; :class:`TestRunner` owns a
:class:`~core.state.RunnerState` and installs it into that module so the
engine operates on the runner's objects.

Example (headless)::

    import asyncio
    from api import TestRunner, RunnerConfig

    async def main():
        runner = TestRunner(RunnerConfig(parallel=4))
        runner.discover("tests")
        runner.prepare_build()
        runner.events.subscribe("test_state_changed",
                                lambda e: print(e.test_key, e.new_state))
        await runner.run_all()
        passed = sum(1 for t in runner.tests if t.state.name == "PASSED")
        print(f"{passed}/{len(runner.tests)} passed")

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from core.config import DEFAULT_CONFIG, RunnerConfig
from core.events import EventBus, SuiteChanged, TestFinished, TestStateChanged
from core.models import (
    AppState,
    DwarfCache,
    Suite,
    Test,
    TestRun,
    TestState,
    TimelineEvent,
)
from core.state import RunnerState
from core.story import StoryFilterEngine

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


__all__ = [
    # facade
    "TestRunner",
    "DebugSession",
    # re-exported core types
    "RunnerConfig",
    "RunnerState",
    "EventBus",
    "AppState",
    "Suite",
    "Test",
    "TestRun",
    "TestState",
    "TimelineEvent",
    "DwarfCache",
    "StoryFilterEngine",
]


def _install_state(runner_state: RunnerState) -> None:
    """Publish ``runner_state``'s objects into the transitional global ``state``
    module so the legacy engine in :mod:`api._runner` (which reads globals)
    operates on the runner's objects.

    The ``AppState``, ``dep_index`` and ``active_processes`` are adopted by
    reference (the engine captured them at import time, so we reuse the very
    same objects rather than replacing them).  Config-derived scalars are
    written as module attributes, which the engine reads via attribute access.
    """
    import state as gs

    # Adopt the module-level objects the engine already bound at import time.
    # Re-point the RunnerState at these exact objects so mutations propagate.
    runner_state.app_state = gs.state
    runner_state.dep_index = gs.dep_index
    runner_state.active_processes = gs.active_processes
    # Scalar runtime fields are mirrored onto the module.
    gs.dep_graph_ready = runner_state.dep_graph_ready
    gs.dep_graph_reason = runner_state.dep_graph_reason
    gs.app_active = runner_state.app_active
    gs.debug_line = runner_state.debug_line
    gs.debug_precision_mode_preference = runner_state.default_debug_precision_mode
    gs.story_filter_profile_preference = runner_state.default_story_filter_profile


def _apply_config(config: RunnerConfig) -> None:
    """Write config-derived scalars into the transitional global ``state``
    module so the engine picks them up."""
    import state as gs

    gs.debug_build_enabled = bool(config.debug_build or config.timeline)
    gs.timeline_capture_enabled = config.timeline
    gs.cflags = config.cflags
    gs.tsv_lines_above = config.tsv_lines_above
    gs.tsv_lines_below = config.tsv_lines_below
    gs.tsv_skip_seq_lines = config.tsv_skip_seq_lines
    gs.tsv_vars_depth = config.tsv_vars_depth
    gs.tsv_variables_height = config.tsv_variables_height
    gs.tsv_show_reason_about = config.tsv_show_reason_about
    gs.story_filter_profile_preference = config.story_filter_profile
    gs.debug_precision_mode_preference = config.debug_precision_mode
    gs.app_active = True
    if not gs.subprocess_columns:
        gs.subprocess_columns = 80


def _sync_state_back(runner_state: RunnerState) -> None:
    """Read the mutable scalar globals back into ``runner_state``.

    The engine writes scalars (``dep_graph_ready``, ``app_active``, …) as
    module attributes.  Call this after engine calls to refresh the
    :class:`RunnerState` view.
    """
    import state as gs

    runner_state.dep_graph_ready = gs.dep_graph_ready
    runner_state.dep_graph_reason = gs.dep_graph_reason
    runner_state.app_active = gs.app_active
    runner_state.debug_line = gs.debug_line
    runner_state.default_debug_precision_mode = gs.debug_precision_mode_preference
    runner_state.default_story_filter_profile = gs.story_filter_profile_preference


class TestRunner:
    """Headless-callable test runner.

    Owns an immutable :class:`RunnerConfig`, a mutable :class:`RunnerState`,
    and an :class:`EventBus`.  Subscribers register on ``runner.events`` to
    observe discovery, state transitions, and completion.
    """

    def __init__(self, config: RunnerConfig | None = None) -> None:
        self.config = config or DEFAULT_CONFIG
        self.events = EventBus()
        self.state = RunnerState()
        # Wire our objects into the transitional global state module.
        _install_state(self.state)
        _apply_config(self.config)
        # The parallel runner-pool size lives on AppState (the engine reads
        # ``state.available_runners`` in state_changed()).
        self.state.app_state.available_runners = self.config.parallel
        self._debug_sessions: dict[str, DebugSession] = {}

    # ----- discovery ----------------------------------------------------

    @property
    def tests(self) -> list[Test]:
        """All discovered tests (flat)."""
        return self.state.app_state.all_tests

    def discover(self, tests_dir: str = "tests") -> list[Test]:
        """Discover ``*.c`` test files under ``tests_dir`` and populate the
        test tree.  Returns the flat list of discovered tests."""
        if not os.path.isdir(tests_dir):
            raise FileNotFoundError(f"test directory not found: {tests_dir}")
        self.state.app_state.populate_suites(
            tests_dir,
            debug_precision_mode=self.config.debug_precision_mode,
            story_filter_profile=self.config.story_filter_profile,
        )
        self.events.emit(SuiteChanged(all_tests=tuple(self.tests)))
        return list(self.tests)

    def get_test(self, test_key: str) -> Test | None:
        """Look up a test by its source path, or ``None``."""
        return self.state.get_test(test_key)

    # ----- build preparation -------------------------------------------

    def prepare_build(self) -> None:
        """Run the build setup: hydrate deps from db.json, generate the
        Makefile, build the project library, and refresh the dependency
        graph.  Equivalent to the legacy ``main.py`` pre-run setup."""
        from runner.makefile import (
            build_project_sources,
            generate_makefile,
            hydrate_dependencies_from_db,
            refresh_dependency_graph,
        )
        from api._runner import prime_editor_breakpoints_cache

        hydrate_dependencies_from_db()
        generate_makefile()
        build_project_sources()
        refresh_dependency_graph()
        prime_editor_breakpoints_cache()
        _sync_state_back(self.state)

    # ----- execution ----------------------------------------------------

    def schedule_run(self) -> None:
        """Schedule all pending tests for execution (non-blocking).

        Mirrors the legacy ``state_changed()``: dispatches via
        ``asyncio.ensure_future`` so tests run concurrently within the running
        event loop up to ``config.parallel`` workers.  Use :meth:`run_all` for
        an awaitable form.
        """
        from api._runner import state_changed

        state_changed()
        _sync_state_back(self.state)

    async def run_all(self, poll_interval: float = 0.05) -> None:
        """Schedule every pending test and await completion.

        Polls :meth:`RunnerState.all_tests_finished` until all tests reach a
        terminal state.  State transitions are visible to ``events``
        subscribers via the polling emitter below.
        """
        self._emit_discovered_states()
        self.schedule_run()
        previous = {t.source_path: t.state for t in self.tests}
        while not self.state.all_tests_finished():
            await asyncio.sleep(poll_interval)
            self._emit_state_diffs(previous)
        # final diff to catch terminal transitions
        self._emit_state_diffs(previous)
        _sync_state_back(self.state)

    async def run_test(self, test: Test) -> None:
        """Run a single test and await its completion."""
        from api._runner import run_test

        await run_test(test, lambda *_: None)
        self._emit_state_for(test)
        _sync_state_back(self.state)

    # ----- debug --------------------------------------------------------

    async def start_debug(self, test: Test) -> "DebugSession":
        """Start a manual debug session for ``test`` and return a handle."""
        session = DebugSession(self, test)
        self._debug_sessions[test.source_path] = session
        await session._start()
        return session

    async def cancel(self, test: Test) -> None:
        """Cancel a running/debug test and restore normal build mode."""
        from api._runner import cancel_test_and_restore_normal_build

        await cancel_test_and_restore_normal_build(test)
        _sync_state_back(self.state)

    async def prioritize(self, visible_keys: set[str]) -> None:
        """Preempt non-visible running tests so visible pending tests run first.

        If there are visible pending tests and not enough free worker slots,
        cancel the **minimum** number of non-visible running tests to free
        capacity.  Cancelled tests are re-queued automatically (via
        ``rerun_after_user_cancel``) and resume in the background once the
        visible tests finish.

        Visible pending tests get their ``time_state_changed`` bumped so the
        FIFO scheduler (which pops most-recent first) picks them before the
        re-queued non-visible ones.
        """
        import time as _time

        tests = self.tests
        visible_pending = [
            t for t in tests
            if t.source_path in visible_keys and t.state == TestState.PENDING
        ]
        if not visible_pending:
            return

        free_slots = self.state.app_state.available_runners
        if free_slots >= len(visible_pending):
            return  # enough capacity — normal scheduling handles it

        needed = len(visible_pending) - free_slots
        non_visible_running = [
            t for t in tests
            if t.source_path not in visible_keys and t.state == TestState.RUNNING
        ]
        if not non_visible_running:
            return  # nothing to preempt

        # Cancel the minimum needed — prefer most-recently-started (least work
        # invested) to minimise wasted computation.
        non_visible_running.sort(key=lambda t: t.time_start, reverse=True)
        to_cancel = non_visible_running[:needed]

        # Bump visible pending timestamps so the scheduler picks them first
        # after the cancellations free slots.  (+1s keeps them ahead of the
        # cancelled tests whose timestamp is set to "now" in on_complete.)
        bump = _time.monotonic() + 1.0
        for t in visible_pending:
            t.time_state_changed = bump

        for test in to_cancel:
            await self.cancel(test)

    # ----- lifecycle ----------------------------------------------------

    def terminate(self) -> None:
        """Terminate all active subprocesses (shutdown helper)."""
        from api._runner import _terminate_active_processes

        asyncio.ensure_future(_terminate_active_processes())

    def save_db(self) -> None:
        """Persist the dependency db (db.json)."""
        from runner.makefile import save_dependency_db

        save_dependency_db()
        _sync_state_back(self.state)

    # ----- background event emitter ------------------------------------

    def start_emitter(self, interval: float = 0.1) -> None:
        """Start a background task that polls test states and emits
        :class:`TestStateChanged` / :class:`TestFinished` events.

        This bridges the legacy engine (which mutates test states directly but
        does not emit events) to the event bus so UI subscribers can react
        without polling the engine themselves.  The polling lives in the API
        layer; the UI is purely reactive.
        """
        if getattr(self, "_emitting", False):
            return
        self._emitting = True
        self._emitter_previous = {t.source_path: t.state for t in self.tests}
        self._emitter_interval = interval
        self._emitter_task = asyncio.ensure_future(self._emitter_loop())

    async def _emitter_loop(self) -> None:
        import state as gs

        previous = self._emitter_previous
        interval = self._emitter_interval
        while getattr(self, "_emitting", False):
            await asyncio.sleep(interval)
            # refresh runner-state view from globals (engine mutates globals)
            _sync_state_back(self.state)
            self._emit_state_diffs(previous)
            # keep subprocess_columns in sync with whatever the UI set
            _ = gs

    def stop_emitter(self) -> None:
        """Stop the background event emitter."""
        self._emitting = False
        task = getattr(self, "_emitter_task", None)
        if task is not None and not task.done():
            task.cancel()

    # ----- event emission (transitional polling-based) ------------------

    def _emit_discovered_states(self) -> None:
        for t in self.tests:
            self.events.emit(
                TestStateChanged(
                    test_key=t.source_path,
                    old_state=TestState.PENDING,
                    new_state=t.state,
                    test=t,
                )
            )

    def _emit_state_for(self, test: Test) -> None:
        self.events.emit(
            TestStateChanged(
                test_key=test.source_path,
                old_state=test.state,
                new_state=test.state,
                test=test,
            )
        )
        if test.state in (TestState.PASSED, TestState.FAILED):
            self.events.emit(
                TestFinished(
                    test_key=test.source_path,
                    passed=test.state == TestState.PASSED,
                    test=test,
                )
            )

    def _emit_state_diffs(self, previous: dict[str, TestState]) -> None:
        for t in self.tests:
            key = t.source_path
            old = previous.get(key)
            if old is not t.state:
                self.events.emit(
                    TestStateChanged(
                        test_key=key,
                        old_state=old or TestState.PENDING,
                        new_state=t.state,
                        test=t,
                    )
                )
                if t.state in (TestState.PASSED, TestState.FAILED):
                    self.events.emit(
                        TestFinished(
                            test_key=key,
                            passed=t.state == TestState.PASSED,
                            test=t,
                        )
                    )
                previous[key] = t.state


class DebugSession:
    """Handle to a manual debug session for a single test, bound to a
    :class:`TestRunner`.

    Wraps the legacy ``start_debug_session`` / ``debug_step_*`` functions.
    The step methods are coroutines because the underlying gdb MI calls are
    async; ``await`` them in order.
    """

    def __init__(self, runner: TestRunner, test: Test) -> None:
        self.runner = runner
        self.test = test

    async def _start(self) -> None:
        from api._runner import start_debug_session

        await start_debug_session(self.test)
        _sync_state_back(self.runner.state)

    async def step_next(self):
        from api._runner import debug_step_next

        await debug_step_next(self.test)
        _sync_state_back(self.runner.state)

    async def step_in(self):
        from api._runner import debug_step_in

        await debug_step_in(self.test)

    async def step_out(self):
        from api._runner import debug_step_out

        await debug_step_out(self.test)

    async def continue_run(self):
        from api._runner import debug_continue

        await debug_continue(self.test)

    async def interrupt(self):
        from api._runner import debug_interrupt

        await debug_interrupt(self.test)

    def is_active(self) -> bool:
        from api._runner import is_debug_active

        return is_debug_active(self.test)

    async def stop(self) -> None:
        from api._runner import stop_debug_session

        await stop_debug_session(self.test)
        _sync_state_back(self.runner.state)
