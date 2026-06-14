"""Compatibility shim — the test-execution + debug orchestration now lives in
``api._runner``.

The canonical implementation was relocated to :mod:`api._runner` (part of the
API/UI separation refactor).  This shim keeps the legacy
``from runner.execute import ...`` call sites — in ``runner/__init__.py``,
``render/test_debugger_screen.py`` and ``watch/handler.py`` — working
unchanged.

It will be removed once those call sites migrate to the public
:class:`api.TestRunner` API.
"""

from api._runner import (  # noqa: F401
    _cancel_active_run_for_manual_debug,
    _schedule_story_annotations_persist,
    _terminate_active_processes,
    cancel_pending_story_annotations_persist,
    cancel_test_and_restore_normal_build,
    debug_continue,
    debug_interrupt,
    debug_interrupt_nowait,
    debug_step_in,
    debug_step_next,
    debug_step_out,
    get_debug_session,
    is_debug_active,
    is_editor_breakpoints_file_path,
    prime_editor_breakpoints_cache,
    refresh_editor_breakpoints_cache,
    restore_normal_build_mode,
    run_test,
    start_debug_session,
    state_changed,
    stop_debug_session,
    sync_editor_breakpoints_for_active_debug,
)
