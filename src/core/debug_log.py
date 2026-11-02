"""Optional diagnostic log for scheduling / cancel behaviour.

Enabled by ``--debug-log`` (writes ``test_build/log.txt``).  When disabled
(the default) :func:`debug_log` is a near-zero-cost no-op, so call sites can
be left in place without affecting normal runs.

Thread-safe: a module lock serialises writes so call sites in worker threads
(e.g. ``asyncio.to_thread`` build calls) and the event loop never interleave.

This module is part of the ``core`` layer: it imports nothing from ``api``,
``ui``, ``runner``, or the global ``state`` module, so it is safe to import
from anywhere.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_lock = threading.Lock()
_enabled = False
_handle = None


def enable_debug_log(path: str) -> None:
    """Open ``path`` for writing and enable :func:`debug_log`.

    Truncates the file on open.  Best-effort: a failure to open (e.g. read-only
    filesystem) silently leaves logging disabled rather than crashing the app.
    """
    global _enabled, _handle
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass
    try:
        _handle = open(path, "w", encoding="utf-8")
        _enabled = True
        _handle.write(
            f"ctester debug log opened {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        _handle.flush()
    except OSError:
        _enabled = False
        _handle = None


def debug_log(message: str, **fields: Any) -> None:
    """Append one monotonic-timestamped line.  No-op when disabled."""
    if not _enabled or _handle is None:
        return
    parts = [message]
    for key, value in fields.items():
        parts.append(f"{key}={value!r}")
    line = f"{time.monotonic():.4f}  " + "  ".join(parts) + "\n"
    with _lock:
        try:
            _handle.write(line)
            _handle.flush()
        except OSError:
            pass


__all__ = ["enable_debug_log", "debug_log"]
