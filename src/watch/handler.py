import asyncio
import os
import threading
import time

from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler

import state as global_state
from state import state, dep_index, active_processes
from core.models import Test, TestState, Suite, has_main_definition
from runner.makefile import generate_makefile, build_project_sources, refresh_dependency_graph, normalize_dep_path, dep_content_unchanged
from runner.execute import (
    state_changed,
    is_editor_breakpoints_file_path,
    refresh_editor_breakpoints_cache,
    sync_editor_breakpoints_for_active_debug,
)


_change_lock = asyncio.Lock()
_deferred_changes: dict[str, set[str]] = {}
# Moves deferred while a manual debug session is active (no auto-restart).
# Each entry is (src_path, dest_path).  Flushed by flush_deferred_changes.
_deferred_moves: list[tuple[str, str]] = []

# Pre-lock coalescing buffers.  handle_file_changes deposits its events here
# before acquiring _change_lock.  The first task to acquire the lock becomes
# the processor and drains these in a loop, absorbing events from concurrent
# calls that would otherwise each run their own full rebuild pass.
_pending_changes: dict[str, set[str]] = {}
_pending_moves: list[tuple[str, str]] = []
_pending_breakpoints: dict[str, set[str]] = {}

# Debounce tuning.  Per-event delay stays at BASE_DEBOUNCE (100 ms) so brief
# edit bursts collapse into one batch.  Under mass changes (git checkout,
# large refactors) events can stream for seconds; if we kept resetting the
# timer by BASE_DEBOUNCE forever the batch could starve indefinitely.  The
# MAX_DEBOUNCE cap forces a flush after ~1.5 s of continuous activity, which
# bounds the worst case to one batch per ~1.5 s window during big operations.
_BASE_DEBOUNCE = 0.1
_MAX_DEBOUNCE = 1.5


def _merge_changed_paths(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for path, kinds in source.items():
        bucket = target.setdefault(path, set())
        bucket.update(kinds)


def _is_in_test_build(abs_path: str) -> bool:
    return "test_build" in abs_path.split(os.sep)


def _is_under(abs_path: str, root: str) -> bool:
    return abs_path == root or abs_path.startswith(f"{root}{os.sep}")


def _looks_like_c_dependency(abs_path: str) -> bool:
    _, ext = os.path.splitext(abs_path)
    return ext in {".c", ".h"}


def _is_directory_modified_noise(event_kinds: set[str]) -> bool:
    return "directory" in event_kinds and event_kinds <= {"directory", "modified"}


def _remove_test_from_suite(suite: Suite, abs_source_path: str) -> None:
    suite.tests = [
        test
        for test in suite.tests
        if os.path.abspath(test.source_path) != abs_source_path
    ]
    for child in suite.children:
        _remove_test_from_suite(child, abs_source_path)


def _remove_test(abs_source_path: str) -> None:
    matching_tests = [
        test
        for test in state.all_tests
        if os.path.abspath(test.source_path) == abs_source_path
    ]
    if not matching_tests:
        return

    for test in matching_tests:
        if test in state.all_tests:
            state.all_tests.remove(test)

    _remove_test_from_suite(state.root_suite, abs_source_path)

    process = active_processes.get(abs_source_path)
    if process is not None and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    active_processes.pop(abs_source_path, None)


# When a single debounce batch contains this many or more changed paths we
# assume a mass change is in progress (git checkout, directory move, bulk
# refactor).  The per-path dep_index lookup is wasted precision work in that
# case because everything will rerun anyway, so we flip rerun_all eagerly and
# skip the mapping.  Test-add / test-remove detection still runs.
_MASS_CHANGE_THRESHOLD = 25


def _apply_test_renames(moves: list[tuple[str, str]]) -> dict[str, Test]:
    """Detect test renames in ``moves`` and repoint the matching ``Test``
    objects in place.

    Watchdog's ``on_moved`` decomposes a rename into ``deleted@src`` +
    ``created@dst``; without intervention that becomes delete-old-Test +
    create-new-Test and the per-test identity is lost (timing_history,
    story_annotations, debug_precision_mode / story_filter_profile
    preferences, the owned ``DwarfCache``, etc.).  By repointing
    ``test.source_path`` before the main per-path loop runs, the existing
    delete/create logic sees the repointed test as already at the new path
    and treats the events as no-ops (the deleted@src lookup matches nothing;
    the created@dst lookup matches the repointed test and feeds it into
    ``affected``).

    Moves whose source is not a current test, or whose destination is not
    under ``tests/`` / not a ``.c`` file, are left for the regular
    delete+create path to handle.

    Returns a ``{new_source_path: Test}`` dict of repointed tests.
    """
    renamed: dict[str, Test] = {}
    if not moves:
        return renamed

    tests_dir = os.path.abspath("tests")
    for src, dst in moves:
        src_canon = normalize_dep_path(src)
        dst_abs = os.path.abspath(dst)
        if not _is_under(dst_abs, tests_dir) or not dst_abs.endswith(".c"):
            continue

        matching: Test | None = None
        for test in state.all_tests:
            if normalize_dep_path(test.source_path) == src_canon:
                matching = test
                break
        if matching is None:
            continue

        # Don't repoint onto an existing test (rename collision / overwrite).
        dst_canon = normalize_dep_path(dst)
        if any(
            normalize_dep_path(t.source_path) == dst_canon
            for t in state.all_tests
            if t is not matching
        ):
            continue

        matching.source_path = os.path.relpath(dst_abs)
        matching.name = os.path.splitext(os.path.basename(dst_abs))[0]
        matching.include_dirs = []
        matching.dependencies = []
        renamed[matching.source_path] = matching

    return renamed


async def _apply_file_changes(
    changed_paths: dict[str, set[str]],
    moves: list[tuple[str, str]] | None = None,
) -> None:
    from core.debug_log import debug_log

    if moves is None:
        moves = []

    # Diagnostic entry log.  Guard: only write when the batch has at least one
    # path OUTSIDE test_build.  Without this, log.txt writes (this diagnostic
    # itself) would feed back: write -> watch event -> entry log -> write ...,
    # producing an infinite logging loop.  test_build-only batches are filtered
    # from reruns anyway, so they're not interesting to log.
    relevant_for_log = [
        p for p in changed_paths if not _is_in_test_build(os.path.abspath(p))
    ]
    if relevant_for_log:
        debug_log(
            "apply_file_changes entry",
            paths=sorted(relevant_for_log),
            moves=len(moves),
        )

    breakpoints_changed = False
    relevant_code_changes = False
    affected: dict[str, Test] = {}
    rerun_all = False
    src_dir = os.path.abspath("src")
    tests_dir = os.path.abspath("tests")
    removed_tests: set[str] = set()

    # Phase 0: repoint renamed tests in place so their identity survives the
    # delete+create decomposition.  Runs before mass-change detection and the
    # per-path loop.
    renamed_tests = _apply_test_renames(moves)

    # Mass-change detection.  Must ignore test_build writes: a single build
    # emits dozens of artifact writes (.o/.d/libproject.a/binary/.map/Makefile/
    # db.json) that would otherwise trip the threshold, cancel the running test
    # via rerun_all, and feed an infinite run -> build -> cancel loop.  Only a
    # bulk change to *sources* (git checkout, directory move) should trigger it.
    non_test_build_changes = [
        p for p in changed_paths if not _is_in_test_build(os.path.abspath(p))
    ]
    if len(non_test_build_changes) >= _MASS_CHANGE_THRESHOLD:
        rerun_all = True

    for path, event_kinds in changed_paths.items():
        abs_path = os.path.abspath(path)
        # dep_index keys are realpath-canonicalised (see core.build.normalize_dep_path)
        # so the lookup key must be too — otherwise symlinks in the project tree
        # break precision reruns (the same physical file would appear under two
        # keys and only one would match).
        dep_key = normalize_dep_path(path)

        if is_editor_breakpoints_file_path(abs_path):
            if "deleted" in event_kinds:
                refresh_editor_breakpoints_cache(force=True)
            elif event_kinds & {"created", "modified", "moved"}:
                refresh_editor_breakpoints_cache(force=True)
            breakpoints_changed = True
            continue

        if _is_in_test_build(abs_path):
            continue

        relevant_code_changes = True

        in_src = _is_under(abs_path, src_dir)
        in_tests = _is_under(abs_path, tests_dir)
        is_directory_event = "directory" in event_kinds

        if _is_directory_modified_noise(event_kinds):
            continue

        if rerun_all:
            # Mass-change fast path: we're going to rebuild everything anyway,
            # so skip per-path dep_index mapping.  Still detect test removals
            # so the suite tree stays correct.
            if "deleted" in event_kinds and in_tests and abs_path.endswith(".c"):
                removed_tests.add(abs_path)
            continue

        mapped_for_path = False
        # For pure ``modified`` events (no create/delete), check the content
        # hash before scheduling a rerun.  This catches editor touches,
        # atomic-saves with identical bytes, and git operations that restore
        # the same content — none of which need a rebuild.
        modified_only = event_kinds == {"modified"} or (
            event_kinds <= {"modified", "directory"} and "modified" in event_kinds
        )
        content_unchanged = modified_only and dep_content_unchanged(dep_key)
        if not content_unchanged:
            for test in dep_index.get(dep_key, []):
                affected[test.source_path] = test
                mapped_for_path = True
            for test in state.all_tests:
                if normalize_dep_path(test.source_path) == dep_key:
                    affected[test.source_path] = test
                    mapped_for_path = True
        else:
            # File's bytes match the cached hash; still mark mapped_for_path
            # so we don't fall through to the unmapped rerun_all fallback
            # (the path IS mapped, we just decided not to act on it).
            if dep_index.get(dep_key) or any(
                normalize_dep_path(t.source_path) == dep_key
                for t in state.all_tests
            ):
                mapped_for_path = True

        if "deleted" in event_kinds and in_tests and abs_path.endswith(".c"):
            removed_tests.add(abs_path)

        if mapped_for_path:
            continue

        if in_tests:
            continue

        if not global_state.dep_graph_ready:
            if in_src or _looks_like_c_dependency(abs_path):
                rerun_all = True
        elif in_src or _looks_like_c_dependency(abs_path):
            rerun_all = True
        elif is_directory_event and in_src:
            rerun_all = True

    for removed_test in removed_tests:
        _remove_test(removed_test)
        affected.pop(os.path.relpath(removed_test), None)

    if rerun_all:
        for test in state.all_tests:
            affected[test.source_path] = test

    # Only log when something actually happens; an empty result would feed back
    # via log.txt writes on no-op (test_build-only) batches.
    if affected or rerun_all:
        debug_log(
            "apply_file_changes result",
            rerun_all=rerun_all,
            affected=[t.name for t in affected.values()],
            running=[t.name for t in affected.values() if t.state == TestState.RUNNING],
        )

    for test in affected.values():
        test.include_dirs = []
        if test.state == TestState.RUNNING:
            debug_log("watch-cancel", test=test.name, rerun_all=rerun_all)
            test.state = TestState.CANCELLED
            test.cancelled_by_user = False
            test.time_state_changed = time.monotonic()
            process = active_processes.get(os.path.abspath(test.source_path))
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        elif test.state in (TestState.PASSED, TestState.FAILED):
            test.state = TestState.PENDING
            test.time_state_changed = time.monotonic()

    existing_sources = {os.path.abspath(t.source_path) for t in state.all_tests}
    added_tests = 0
    for path, event_kinds in changed_paths.items():
        if "deleted" in event_kinds:
            continue

        abs_path = os.path.abspath(path)
        if _is_in_test_build(abs_path):
            continue
        if not _is_under(abs_path, tests_dir):
            continue
        if not abs_path.endswith(".c"):
            continue
        if not has_main_definition(abs_path):
            continue
        if abs_path in existing_sources:
            continue

        source_path = os.path.relpath(abs_path)
        test = Test(
            name=os.path.splitext(os.path.basename(abs_path))[0],
            source_path=source_path,
            debug_precision_mode=global_state.debug_precision_mode_preference,
            story_filter_profile=global_state.story_filter_profile_preference,
        )
        state.root_suite.tests.append(test)
        state.all_tests.append(test)
        added_tests += 1

    has_rebuild_inputs = bool(affected) or rerun_all or bool(removed_tests) or added_tests > 0
    if has_rebuild_inputs:
        await asyncio.to_thread(generate_makefile)
        await asyncio.to_thread(build_project_sources)
        await asyncio.to_thread(refresh_dependency_graph)

    if breakpoints_changed or relevant_code_changes:
        state_changed()
        return
    state_changed()


async def handle_file_changes(
    changed_paths: dict[str, set[str]],
    moves: list[tuple[str, str]] | None = None,
):
    """Coalescing entry point for file-change events.

    Each call deposits its events into shared ``_pending_*`` buffers
    *before* acquiring ``_change_lock``.  The first call to acquire the
    lock becomes the processor and drains the buffers in a loop; events
    arriving while it runs (deposited by the pre-lock merge of other
    concurrent calls) trigger another iteration, so a burst that lands
    N separate ``handle_file_changes`` tasks collapses into as few
    rebuild cycles as possible — ideally one.  Without this, every
    debounced 100 ms window during a multi-second mass change would
    queue its own serialised make+build+refresh pass behind the lock.
    """
    if moves is None:
        moves = []
    for path, event_kinds in changed_paths.items():
        abs_path = os.path.abspath(path)
        if is_editor_breakpoints_file_path(abs_path):
            _merge_changed_paths(_pending_breakpoints, {path: event_kinds})
        else:
            _merge_changed_paths(_pending_changes, {path: event_kinds})
    if moves:
        _pending_moves.extend(moves)

    async with _change_lock:
        # Drain pending in a loop.  Each iteration runs one batch; new events
        # arriving while we run land in _pending_* (via the pre-lock merge of
        # other handle_file_changes tasks) and trigger another iteration.
        while _pending_changes or _pending_moves or _pending_breakpoints:
            batch_paths = {
                p: set(k) for p, k in _pending_changes.items()
            }
            batch_moves = list(_pending_moves)
            batch_bps = {p: set(k) for p, k in _pending_breakpoints.items()}
            _pending_changes.clear()
            _pending_moves.clear()
            _pending_breakpoints.clear()
            await _drain_one_batch(batch_paths, batch_moves, batch_bps)


async def _drain_one_batch(
    non_breakpoint_paths: dict[str, set[str]],
    moves: list[tuple[str, str]],
    breakpoint_paths: dict[str, set[str]],
) -> None:
    """Process one (possibly coalesced) batch.  Caller holds ``_change_lock``."""
    if breakpoint_paths:
        for path, event_kinds in breakpoint_paths.items():
            abs_path = os.path.abspath(path)
            if "deleted" in event_kinds:
                refresh_editor_breakpoints_cache(force=True)
            elif event_kinds & {"created", "modified", "moved"}:
                refresh_editor_breakpoints_cache(force=True)

        if global_state.active_debug_test_key is not None:
            await sync_editor_breakpoints_for_active_debug()
        else:
            state_changed()

    if not non_breakpoint_paths and not moves:
        return

    if global_state.active_debug_test_key is not None:
        if global_state.debug_auto_restart:
            # Auto-restart: apply changes now and signal the debug screen
            # to restart the session with the recompiled binary.
            # Only signal if there are real source changes — build
            # artifacts written to test_build/ (and the directory-modified
            # noise they generate on parent dirs) would otherwise cause an
            # infinite restart loop.
            has_relevant_changes = any(
                not _is_in_test_build(os.path.abspath(p))
                and not _is_directory_modified_noise(kinds)
                for p, kinds in non_breakpoint_paths.items()
            )
            if has_relevant_changes:
                global_state.debug_auto_restart_pending = (
                    global_state.active_debug_test_key
                )
            await _apply_file_changes(non_breakpoint_paths, moves)
            return
        _merge_changed_paths(_deferred_changes, non_breakpoint_paths)
        _deferred_moves.extend(moves)
        return

    await _apply_file_changes(non_breakpoint_paths, moves)


async def flush_deferred_changes() -> None:
    async with _change_lock:
        if global_state.active_debug_test_key is not None:
            return
        if not _deferred_changes and not _deferred_moves:
            return
        queued = {path: set(kinds) for path, kinds in _deferred_changes.items()}
        queued_moves = list(_deferred_moves)
        _deferred_changes.clear()
        _deferred_moves.clear()
        await _apply_file_changes(queued, queued_moves)


class DebounceHandler(FileSystemEventHandler):
    def __init__(self, loop):
        self._loop = loop
        self._timer: threading.Timer | None = None
        self._changed: dict[str, set[str]] = {}
        # Captured (src, dst) pairs from on_moved so test renames can be
        # detected as moves rather than delete+create pairs.
        self._moves: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        # monotonic timestamp of the first event in the current burst; None when
        # no burst is in progress.  Used to cap total debounce wait at
        # _MAX_DEBOUNCE even if events keep arriving faster than _BASE_DEBOUNCE.
        self._burst_start: float | None = None

    def _record_change(
        self,
        path: str,
        event_type: str,
        is_directory: bool,
    ) -> None:
        decoded_path = os.fsdecode(path)
        kinds = self._changed.setdefault(decoded_path, set())
        kinds.add(event_type)
        if is_directory:
            kinds.add("directory")

    def _queue_event(self, event: FileSystemEvent, event_type: str) -> None:
        with self._lock:
            self._record_change(event.src_path, event_type, event.is_directory)
            self._arm_timer_locked()

    def _arm_timer_locked(self) -> None:
        """(Re)arm the debounce timer.  Caller must hold ``self._lock``."""
        now = time.monotonic()
        if self._burst_start is None:
            self._burst_start = now
        # Default to a full BASE_DEBOUNCE delay; but if we've already been
        # holding events for nearly MAX_DEBOUNCE, shrink the delay so the
        # batch flushes at the cap instead of running past it.
        elapsed = now - self._burst_start
        remaining_budget = _MAX_DEBOUNCE - elapsed
        delay = min(_BASE_DEBOUNCE, remaining_budget)
        if delay <= 0:
            # Budget exhausted — fire immediately.
            delay = 0.0
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(delay, self._schedule)
        self._timer.daemon = True
        self._timer.start()

    def _schedule(self):
        with self._lock:
            changed = {path: set(kinds) for path, kinds in self._changed.items()}
            moves = list(self._moves)
            self._changed.clear()
            self._moves.clear()
            self._timer = None
            self._burst_start = None

        if not changed and not moves:
            return

        self._loop.call_soon_threadsafe(
            asyncio.create_task, handle_file_changes(changed, moves)
        )

    def on_modified(self, event: FileSystemEvent):
        self._queue_event(event, "modified")

    def on_created(self, event: FileSystemEvent):
        self._queue_event(event, "created")

    def on_deleted(self, event: FileSystemEvent):
        self._queue_event(event, "deleted")

    def on_moved(self, event: FileSystemEvent):
        with self._lock:
            self._record_change(event.src_path, "deleted", event.is_directory)
            self._record_change(event.dest_path, "created", event.is_directory)
            # Keep the (src, dst) pair so test renames can preserve identity.
            # The split delete+create events above are still recorded so
            # non-test moves fall through to the normal path.
            self._moves.append(
                (os.fsdecode(event.src_path), os.fsdecode(event.dest_path))
            )
            self._arm_timer_locked()
