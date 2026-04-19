import asyncio
import os
import threading
import time

from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler

import state as global_state
from state import state, dep_index, active_processes
from models import Test, TestState, Suite
from runner.makefile import generate_makefile, build_project_sources, refresh_dependency_graph
from runner.execute import state_changed


_change_lock = asyncio.Lock()


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


async def handle_file_changes(changed_paths: dict[str, set[str]]):
    async with _change_lock:
        affected: dict[str, Test] = {}
        rerun_all = False
        src_dir = os.path.abspath("src")
        tests_dir = os.path.abspath("tests")
        removed_tests: set[str] = set()

        for path, event_kinds in changed_paths.items():
            abs_path = os.path.abspath(path)
            if _is_in_test_build(abs_path):
                continue

            in_src = _is_under(abs_path, src_dir)
            in_tests = _is_under(abs_path, tests_dir)
            is_directory_event = "directory" in event_kinds

            if _is_directory_modified_noise(event_kinds):
                continue

            mapped_for_path = False
            for test in dep_index.get(abs_path, []):
                affected[test.source_path] = test
                mapped_for_path = True
            for test in state.all_tests:
                if os.path.abspath(test.source_path) == abs_path:
                    affected[test.source_path] = test
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

        for test in affected.values():
            test.include_dirs = []
            if test.state == TestState.RUNNING:
                test.state = TestState.CANCELLED
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
            if abs_path in existing_sources:
                continue

            source_path = os.path.relpath(abs_path)
            test = Test(
                name=os.path.splitext(os.path.basename(abs_path))[0],
                source_path=source_path,
            )
            state.root_suite.tests.append(test)
            state.all_tests.append(test)

        generate_makefile()
        build_project_sources()
        refresh_dependency_graph()
        state_changed()


class DebounceHandler(FileSystemEventHandler):
    def __init__(self, loop):
        self._loop = loop
        self._timer: threading.Timer | None = None
        self._changed: dict[str, set[str]] = {}
        self._lock = threading.Lock()

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
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(0.1, self._schedule)
            self._timer.daemon = True
            self._timer.start()

    def _schedule(self):
        with self._lock:
            changed = {path: set(kinds) for path, kinds in self._changed.items()}
            self._changed.clear()
            self._timer = None

        if not changed:
            return

        self._loop.call_soon_threadsafe(
            asyncio.create_task, handle_file_changes(changed)
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
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(0.1, self._schedule)
            self._timer.daemon = True
            self._timer.start()
