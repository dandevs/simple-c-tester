"""Typed event system for the C test runner.

Replaces the legacy 100 ms polling tick (``display_state_signature``) with an
explicit publish/subscribe model.  The runner/debugger *emit* events; the UI
and any headless consumer *subscribe* to them.

Design rules (enforced by layering):

* This module is part of ``core`` — it imports only from ``core.models`` and
  the standard library.  It MUST NOT import ``api``, ``ui``, ``textual``, or
  the legacy global ``state`` module.
* Events are *frozen* dataclasses (immutable snapshots).  Subscribers must not
  mutate them.
* The :class:`EventBus` is synchronous and not thread-safe by design.  It can
  be constructed and owned by anyone (typically :class:`TestRunner`).  When an
  emitter runs on a background thread (e.g. the watchdog observer), it is the
  emitter's responsibility to marshal onto the right loop before calling
  :meth:`EventBus.emit`, OR the subscriber's responsibility to do so in its
  callback (e.g. via ``loop.call_soon_threadsafe``).  This keeps the bus tiny
  and dependency-free.

Each event class exposes an :attr:`Event.event_type` string (derived from the
class name, CamelCase → snake_case) so subscribers can register by name:

    bus.subscribe("test_state_changed", on_change)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Test, TestState


# ---------------------------------------------------------------------------
# Event base
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _type_name(cls: type) -> str:
    """Convert ``TestStateChanged`` → ``test_state_changed``."""
    return _CAMEL_RE.sub("_", cls.__name__).lower()


@dataclass(frozen=True)
class Event:
    """Base class for all events.

    Subclasses are frozen dataclasses carrying a payload snapshot.  The
    :attr:`event_type` string is derived from the class name and used as the
    subscription key.
    """

    @property
    def event_type(self) -> str:
        return _type_name(type(self))


# ---------------------------------------------------------------------------
# Discovery / suite lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestDiscovered(Event):
    """New tests were discovered (watch mode add/create)."""

    tests: tuple[Test, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TestRemoved(Event):
    """Tests were removed (watch mode delete/move)."""

    test_keys: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SuiteChanged(Event):
    """The suite/test tree structure changed and should be re-rendered.

    Carries the full :class:`AppState` so subscribers can rebuild their view
    of the tree without re-reading globals.
    """

    all_tests: tuple[Test, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Test execution lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestStateChanged(Event):
    """A test transitioned between states (PENDING → RUNNING → PASSED …).

    Subscribers typically use this to refresh a single row in the tree.
    """

    test_key: str = ""
    old_state: TestState = TestState.PENDING
    new_state: TestState = TestState.PENDING
    test: Test | None = None


@dataclass(frozen=True)
class TestFinished(Event):
    """A test reached a terminal state (PASSED or FAILED)."""

    test_key: str = ""
    passed: bool = False
    test: Test | None = None


@dataclass(frozen=True)
class TestOutputUpdated(Event):
    """A test's stdout/stderr/compile_err changed (live output boxes)."""

    test_key: str = ""
    test: Test | None = None


@dataclass(frozen=True)
class CompileError(Event):
    """A test failed to compile."""

    test_key: str = ""
    message: str = ""
    test: Test | None = None


# ---------------------------------------------------------------------------
# Test Story / timeline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimelineUpdated(Event):
    """A test's timeline events changed (Test Story capture)."""

    test_key: str = ""
    event_count: int = 0
    test: Test | None = None


# ---------------------------------------------------------------------------
# Debug session
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DebugStateChanged(Event):
    """A debug session's running/exited status changed.

    Replaces polling of ``run.debug_running`` / ``run.debug_exited`` /
    ``run.debug_exit_code``.
    """

    test_key: str = ""
    running: bool = False
    exited: bool = False
    exit_code: int | None = None
    test: Test | None = None


@dataclass(frozen=True)
class DebugStopped(Event):
    """The debugger stopped at a source location (manual step or auto trace).

    ``stop`` is a ``DebugStopEvent`` instance at runtime; typed loosely here to
    avoid coupling ``core.events`` to ``core.debugger`` (which is built in a
    later batch).
    """

    test_key: str = ""
    stop: Any = None
    test: Test | None = None


@dataclass(frozen=True)
class DebugExited(Event):
    """The debug session for a test has terminated."""

    test_key: str = ""
    exit_code: int | None = None
    test: Test | None = None


# ---------------------------------------------------------------------------
# Build / dependency graph
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DepGraphStatus(Event):
    """The dependency-graph readiness changed (watch-mode warning banner)."""

    ready: bool = False
    reason: str = ""


@dataclass(frozen=True)
class BuildProgress(Event):
    """Generic build progress notification."""

    message: str = ""


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

#: Type alias for a subscriber callback.
Subscriber = Callable[[Event], None]


class EventBus:
    """A minimal synchronous publish/subscribe bus.

    Subscribers register by event-type name (the snake_case form, e.g.
    ``"test_state_changed"``) and receive every emitted event of that type.
    A subscriber registered under the special name ``"*"`` receives all
    events regardless of type (useful for logging/debugging).

    The bus stores weak-ish references (plain callbacks); callers should keep
    their subscriber alive and use :meth:`unsubscribe` to avoid leaks.
    """

    def __init__(self) -> None:
        # name -> list of callbacks.  We store in a dict of lists for O(1)
        # dispatch by name and simple append/remove semantics.
        self._subscribers: dict[str, list[Subscriber]] = {}

    def subscribe(self, event_type: str, callback: Subscriber) -> None:
        """Register ``callback`` for events whose ``event_type`` matches.

        ``event_type`` is the snake_case name (e.g. ``"test_state_changed"``)
        or ``"*"`` to receive every event.
        """
        self._subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: str, callback: Subscriber) -> None:
        """Remove a previously-registered callback.  No-op if absent."""
        subs = self._subscribers.get(event_type)
        if not subs:
            return
        # Remove all identity-equal references (defensive against dupes).
        self._subscribers[event_type] = [s for s in subs if s is not callback]

    def emit(self, event: Event) -> None:
        """Dispatch ``event`` to all matching subscribers.

        Exceptions raised by a subscriber are caught and reported on stderr so
        one bad subscriber cannot take down the runner.  The traceback is
        printed for visibility; the bus continues dispatching to the rest.
        """
        name = event.event_type
        # Specific subscribers first, then wildcard.
        for callback in list(self._subscribers.get(name, ())):
            self._safe_call(callback, event)
        for callback in list(self._subscribers.get("*", ())):
            self._safe_call(callback, event)

    def clear(self) -> None:
        """Remove all subscribers."""
        self._subscribers.clear()

    @property
    def subscriber_counts(self) -> dict[str, int]:
        """Diagnostic: number of subscribers per event type."""
        return {name: len(subs) for name, subs in self._subscribers.items() if subs}

    @staticmethod
    def _safe_call(callback: Subscriber, event: Event) -> None:
        try:
            callback(event)
        except Exception:  # noqa: BLE001 — intentionally broad for a bus
            import traceback

            traceback.print_exc()


__all__ = [
    "Event",
    "EventBus",
    "Subscriber",
    "TestDiscovered",
    "TestRemoved",
    "SuiteChanged",
    "TestStateChanged",
    "TestFinished",
    "TestOutputUpdated",
    "CompileError",
    "TimelineUpdated",
    "DebugStateChanged",
    "DebugStopped",
    "DebugExited",
    "DepGraphStatus",
    "BuildProgress",
]
