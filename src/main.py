import argparse
import asyncio
import os
import threading
import time
from typing import Callable

from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.tree import Tree
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from models import Test, Suite, AppState, TestState

state = AppState()
dep_index: dict[str, list[Test]] = {}
test_spinners: dict[str, Spinner] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=1, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    return parser.parse_args()


def rebuild_dep_index():
    global dep_index
    dep_index = {}
    for test in state.all_tests:
        for dep in test.dependencies:
            dep_index.setdefault(dep, []).append(test)


async def handle_file_changes(changed_paths: set[str]):
    affected: dict[str, Test] = {}
    for path in changed_paths:
        abs_path = os.path.abspath(path)
        for test in dep_index.get(abs_path, []):
            affected[test.source_path] = test
        for test in state.all_tests:
            if os.path.abspath(test.source_path) == abs_path:
                affected[test.source_path] = test

    for test in affected.values():
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
    tests_dir = os.path.abspath("c/tests")
    for path in changed_paths:
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(tests_dir):
            continue
        if not abs_path.endswith(".c"):
            continue
        if abs_path in existing_sources:
            continue
        # Keep source paths relative for consistency with initially discovered tests.
        source_path = os.path.relpath(abs_path)
        test = Test(
            name=os.path.splitext(os.path.basename(abs_path))[0], source_path=source_path
        )
        state.root_suite.tests.append(test)
        state.all_tests.append(test)

    state_changed()


class DebounceHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._timer: threading.Timer | None = None
        self._changed: set[str] = set()
        self._lock = threading.Lock()

    def _schedule(self):
        with self._lock:
            changed = set(self._changed)
            self._changed.clear()
            self._timer = None

        if not changed:
            return

        self._loop.call_soon_threadsafe(asyncio.create_task, handle_file_changes(changed))

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        src_path = os.fsdecode(event.src_path)
        with self._lock:
            self._changed.add(src_path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(0.1, self._schedule)
            self._timer.daemon = True
            self._timer.start()

    def on_created(self, event: FileSystemEvent):
        self.on_modified(event)


def build_suite_tree(suite: Suite, now: float) -> Tree:
    tree = Tree(_suite_label(suite, now))
    for test in suite.tests:
        _add_test_node(tree, test, now)
    for child in suite.children:
        tree.add(build_suite_tree(child, now))
    return tree


def _get_test_spinner(test: Test) -> Spinner:
    key = os.path.abspath(test.source_path)
    spinner = test_spinners.get(key)
    if spinner is None:
        spinner = Spinner("dots", text=Text(test.name, style="yellow"), style="yellow")
        test_spinners[key] = spinner
    return spinner


def _test_elapsed_seconds(test: Test, now: float) -> float:
    if test.time_start <= 0:
        return 0.0

    if test.state in (TestState.PASSED, TestState.FAILED):
        end_time = test.time_state_changed or now
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
        end_time = now
    else:
        return 0.0

    return max(0.0, end_time - test.time_start)


def _suite_elapsed_seconds(suite: Suite, now: float) -> float:
    total = sum(_test_elapsed_seconds(test, now) for test in suite.tests)
    for child in suite.children:
        total += _suite_elapsed_seconds(child, now)
    return total


def _with_elapsed_text(name: str, name_style: str, elapsed_seconds: float) -> Text:
    elapsed_ms = int(elapsed_seconds * 1000)
    text = Text(name, style=name_style)
    text.append(f" [{elapsed_ms}ms]", style="bright_black")
    return text


def _suite_label(suite: Suite, now: float) -> Text:
    return _with_elapsed_text(suite.name, "bold", _suite_elapsed_seconds(suite, now))


def _add_test_node(tree, test: Test, now: float):
    elapsed_seconds = _test_elapsed_seconds(test, now)
    if test.state in (TestState.PENDING, TestState.RUNNING, TestState.CANCELLED):
        label = _get_test_spinner(test)
        label.text = _with_elapsed_text(test.name, "yellow", elapsed_seconds)
    elif test.state == TestState.PASSED:
        test_spinners.pop(os.path.abspath(test.source_path), None)
        label = _with_elapsed_text(test.name, "green", elapsed_seconds)
    elif test.state == TestState.FAILED:
        test_spinners.pop(os.path.abspath(test.source_path), None)
        label = _with_elapsed_text(test.name, "red", elapsed_seconds)
    else:
        test_spinners.pop(os.path.abspath(test.source_path), None)
        label = _with_elapsed_text(test.name, "white", elapsed_seconds)

    node = tree.add(label)
    if test.state == TestState.FAILED:
        error = test.compile_err or test.stderr or ""
        node.add(Panel(error.strip(), border_style="red"))


def build_display() -> Tree:
    now = time.monotonic()
    root = Tree(_suite_label(state.root_suite, now))
    for test in state.root_suite.tests:
        _add_test_node(root, test, now)
    for suite in state.root_suite.children:
        root.add(build_suite_tree(suite, now))
    return root


async def main():
    args = parse_args()
    loop = asyncio.get_running_loop()
    state.populate_suites("c/tests")
    state.available_runners = args.parallel
    state_changed()

    observer = None
    if args.watch:
        handler = DebounceHandler(loop)
        observer = Observer()
        watched_dirs = set()
        tests_dir = os.path.abspath("c/tests")
        watched_dirs.add(tests_dir)
        for test in state.all_tests:
            for dep in test.dependencies:
                dep_dir = os.path.dirname(dep)
                if dep_dir not in watched_dirs:
                    watched_dirs.add(dep_dir)
        for d in watched_dirs:
            observer.schedule(handler, d, recursive=True)
        observer.daemon = True
        observer.start()

    with Live(build_display(), refresh_per_second=10) as live:
        while True:
            live.update(build_display())
            await asyncio.sleep(0.1)


async def run_test(test: Test, on_complete: Callable[[], None]):
    # print(f"Dispatching test: {test.name}")
    test.state = TestState.RUNNING
    test.time_start = time.monotonic()
    test.time_state_changed = time.monotonic()

    os.makedirs("test_build", exist_ok=True)
    process_key = os.path.abspath(test.source_path)

    compile_proc = await asyncio.create_subprocess_exec(
        "gcc",
        "-MMD",
        "-MP",
        "-MF",
        f"test_build/{test.name}.d",
        "-o",
        f"test_build/{test.name}",
        test.source_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    active_processes[process_key] = compile_proc
    _compile_stdout, compile_stderr = await compile_proc.communicate()
    if active_processes.get(process_key) is compile_proc:
        active_processes.pop(process_key, None)

    dep_file = f"test_build/{test.name}.d"
    if os.path.exists(dep_file):
        with open(dep_file, "r") as f:
            dep_content = f.read()
        colon_idx = dep_content.index(":")
        deps_str = dep_content[colon_idx + 1 :].strip()
        parts = deps_str.split()
        deps = []
        for part in parts:
            if part.endswith("\\"):
                part = part[:-1]
            if part:
                deps.append(os.path.abspath(part))
        test.dependencies = deps
        rebuild_dep_index()

    if test.state == TestState.CANCELLED:
        on_complete()
        return

    if compile_proc.returncode != 0:
        test.compile_err = compile_stderr.decode()
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        on_complete()
        return

    run_proc = await asyncio.create_subprocess_exec(
        f"./test_build/{test.name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    active_processes[process_key] = run_proc
    run_stdout, run_stderr = await run_proc.communicate()
    if active_processes.get(process_key) is run_proc:
        active_processes.pop(process_key, None)

    if test.state == TestState.CANCELLED:
        on_complete()
        return

    test.stdout = run_stdout.decode()
    test.stderr = run_stderr.decode()

    if run_proc.returncode == 0:
        test.state = TestState.PASSED
    else:
        test.state = TestState.FAILED

    test.time_state_changed = time.monotonic()
    on_complete()


def state_changed():
    tests_to_run: list[Test] = []
    pending_tests = sorted(
        [test for test in state.all_tests if test.state == TestState.PENDING],
        key=lambda t: t.time_state_changed,
    )

    while state.available_runners > 0 and len(pending_tests) > 0:
        test = pending_tests.pop()
        state.available_runners -= 1
        tests_to_run.append(test)

    for test in tests_to_run:

        def on_complete(completed_test: Test = test):
            state.available_runners += 1
            if completed_test.state == TestState.CANCELLED:
                completed_test.state = TestState.PENDING
                completed_test.time_start = 0.0
                completed_test.time_state_changed = time.monotonic()
            state_changed()

        asyncio.ensure_future(run_test(test, on_complete))

    if len(tests_to_run) > 0:
        state_changed()


if __name__ == "__main__":
    asyncio.run(main())
