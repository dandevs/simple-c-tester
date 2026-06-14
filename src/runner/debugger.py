"""Compatibility shim — re-exports the gdb MI controller from ``core.debugger``.

The canonical home is now :mod:`core.debugger`.  This shim keeps the legacy
``from runner.debugger import ...`` imports working during the refactor.
"""

from core.debugger import (  # noqa: F401
    DebugStopEvent,
    GdbMIController,
    stop_event_is_terminal,
)
