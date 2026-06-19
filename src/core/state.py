"""Mutable runner state — the replacement for the legacy global singleton.

The historical design kept all runtime state as module-level globals in
``state.py`` (``state``, ``dep_index``, ``active_processes``,
``active_debug_test_key``, ``dep_graph_ready``, …).  That made the code hard to
test, created hidden coupling, and prevented headless use.

:class:`RunnerState` collects that mutable state into a single object that is
*owned* by a :class:`TestRunner` (built in a later batch) and passed
explicitly to every function that needs it (dependency injection).  It is NOT a
module-level singleton and carries no configuration — configuration is
immutable and lives in :class:`core.config.RunnerConfig`.

This module is part of the ``core`` layer: it imports only from ``core.models``
and the standard library.  It MUST NOT import ``api``, ``ui``, ``textual``,
or the legacy global ``state`` module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .models import AppState, Test, TestState

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------

@dataclass
class RunnerState:
    """All mutable runtime state for a single runner instance.

    Held by :class:`TestRunner`; passed explicitly to runner/build/watch
    functions.  A fresh instance per :class:`TestRunner` keeps concurrent or
    sequential runs isolated.

    Note: ``available_runners`` lives on the nested :class:`AppState` (for
    backward compatibility with the historical layout) and is set there.
    """

    #: The discovered test tree + runner pool size.
    app_state: AppState = field(default_factory=AppState)

    #: source-path -> dependent tests.  Populated from the ``.d`` dependency
    #: files; drives selective reruns in watch mode.
    dep_index: dict[str, list[Test]] = field(default_factory=dict)

    #: test-key -> live subprocess.  Tracks running test binaries so they can
    #: be terminated on shutdown.
    active_processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)

    #: The test-key currently occupying the manual debug session, or ``None``.
    active_debug_test_key: str | None = None

    #: Whether the dependency graph is ready for precise selective reruns.
    dep_graph_ready: bool = False
    #: Human-readable reason when ``dep_graph_ready`` is False.
    dep_graph_reason: str = "dependency graph not initialized"

    #: Whether the runner/app is currently active (persisted to db.json).
    app_active: bool = False

    #: The current debug cursor ``{filePath, lineNumber}`` persisted to db.json,
    #: or ``None`` when no debug line is tracked.
    debug_line: dict | None = None

    #: Runner-wide default debug stepping precision for newly discovered tests.
    #: initialised from :class:`RunnerConfig` and mutated at runtime via the UI.
    default_debug_precision_mode: str = "precise"
    #: Runner-wide default story filter profile for newly discovered tests.
    default_story_filter_profile: str = "balanced"

    #: Project sources dropped from libproject.a by skip-on-error on the last
    #: build (their compile failed).  Empty when all sources compiled.
    #: Populated by :func:`build_project_sources`; read by headless output and
    #: the TUI to surface "skipped" warnings.
    skipped_sources: list[str] = field(default_factory=list)

    #: gcc stderr from the last ``build_project_sources`` archive build.  When
    #: ``skipped_sources`` is non-empty this holds the actual compile errors
    #: (file:line: error: ...) so callers can show *why* sources were skipped
    #: instead of just the file names.  Empty when nothing was built or all
    #: sources compiled cleanly.
    build_stderr: str = ""

    # ----- convenient accessors -----------------------------------------

    @property
    def all_tests(self) -> list[Test]:
        """All discovered tests (flat list)."""
        return self.app_state.all_tests

    @property
    def root_suite(self):
        """The root suite of the discovered tree."""
        return self.app_state.root_suite

    def get_test(self, test_key: str) -> Test | None:
        """Look up a test by its ``source_path`` key, or ``None``."""
        for test in self.app_state.all_tests:
            if test.source_path == test_key:
                return test
        return None

    # ----- derived queries (replacing runner/state.py helpers) ----------

    def all_tests_finished(self) -> bool:
        """True when every test is in a terminal state (PASSED or FAILED)."""
        done = {TestState.PASSED, TestState.FAILED}
        return all(t.state in done for t in self.app_state.all_tests)

    def has_active_tests(self) -> bool:
        """True when at least one test is pending/running/cancelled."""
        active = {TestState.PENDING, TestState.RUNNING, TestState.CANCELLED}
        return any(t.state in active for t in self.app_state.all_tests)

    def display_state_signature(self) -> tuple:
        """A tuple snapshot of every test's render-relevant fields.

        Historically the UI compared this every 100 ms to decide whether to
        re-render.  Under the new event-driven model the UI subscribes to
        events instead; this method is retained for transitional use and for
        headless consumers that want a cheap change-detection hash.
        """
        sigs = []
        for test in self.app_state.all_tests:
            run = test.current_run
            sigs.append(
                (
                    test.name,
                    test.state,
                    test.time_start,
                    test.time_state_changed,
                    run.stdout if run is not None else "",
                    run.stderr if run is not None else "",
                    run.compile_err if run is not None else "",
                    len(run.timeline_events) if run is not None else 0,
                    run.debug_running if run is not None else False,
                    run.debug_exited if run is not None else False,
                    run.debug_exit_code if run is not None else None,
                )
            )
        return tuple(sigs)


__all__ = ["RunnerState"]
