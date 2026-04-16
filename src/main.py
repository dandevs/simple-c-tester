import argparse
import asyncio
import errno
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


def generate_makefile():
    os.makedirs("test_build", exist_ok=True)
    lines = ["-include test_build/*.d", ""]
    for test in state.all_tests:
        target = f"test_build/{test.name}"
        source = test.source_path
        dep_file = f"test_build/{test.name}.d"
        lines.append(f"{target}: {source}")
        lines.append(
            f"\tgcc -fdiagnostics-color=always -MMD -MP -MF {dep_file} -o {target} {source}"
        )
        lines.append("")
    with open("test_build/Makefile", "w") as f:
        f.write("\n".join(lines))


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
            name=os.path.splitext(os.path.basename(abs_path))[0],
            source_path=source_path,
        )
        state.root_suite.tests.append(test)
        state.all_tests.append(test)

    generate_makefile()
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

        self._loop.call_soon_threadsafe(
            asyncio.create_task, handle_file_changes(changed)
        )

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


def _with_tag_text(name: str, name_style: str, tag: str) -> Text:
    text = Text(name, style=name_style)
    text.append(f" [{tag}]", style="bright_black")
    return text


def _suite_label(suite: Suite, now: float) -> Text:
    return _with_elapsed_text(suite.name, "bold", _suite_elapsed_seconds(suite, now))


def _add_test_node(tree, test: Test, now: float):
    elapsed_seconds = _test_elapsed_seconds(test, now)
    if test.state == TestState.PENDING:
        label = _get_test_spinner(test)
        label.text = _with_tag_text(test.name, "yellow", "pending")
    elif test.state == TestState.RUNNING and test.time_start <= 0:
        label = _get_test_spinner(test)
        label.text = _with_tag_text(test.name, "yellow", "compiling")
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
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
        raw = test.compile_err_raw or test.stderr_raw or b""
        if raw:
            error_text = Text.from_ansi(raw.decode(errors="replace").strip())
        else:
            error_text = Text((test.compile_err or test.stderr or "").strip())
        node.add(Panel(error_text, border_style="red"))


def build_display() -> Tree:
    now = time.monotonic()
    root = Tree(_suite_label(state.root_suite, now))
    for test in state.root_suite.tests:
        _add_test_node(root, test, now)
    for suite in state.root_suite.children:
        root.add(build_suite_tree(suite, now))
    return root


def _all_tests_finished() -> bool:
    done_states = {TestState.PASSED, TestState.FAILED}
    return all(test.state in done_states for test in state.all_tests)


async def _terminate_active_processes() -> None:
    processes = {proc for proc in active_processes.values() if proc.returncode is None}
    for proc in processes:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

    if processes:
        await asyncio.gather(
            *(proc.wait() for proc in processes), return_exceptions=True
        )

    active_processes.clear()


async def main():
    args = parse_args()
    loop = asyncio.get_running_loop()
    state.populate_suites("c/tests")
    generate_makefile()
    state.available_runners = args.parallel
    state_changed()

    observer = None
    try:
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
                if not args.watch and _all_tests_finished():
                    break
                await asyncio.sleep(0.1)
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
        await _terminate_active_processes()


async def run_test(test: Test, on_complete: Callable[[], None]):
    process_key = os.path.abspath(test.source_path)
    try:
        if test.state == TestState.CANCELLED:
            return

        make_proc = await asyncio.create_subprocess_exec(
            "make",
            "-f",
            "test_build/Makefile",
            f"test_build/{test.name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        active_processes[process_key] = make_proc
        _, make_stderr = await make_proc.communicate()
        if active_processes.get(process_key) is make_proc:
            active_processes.pop(process_key, None)

        dep_file = f"test_build/{test.name}.d"
        if os.path.exists(dep_file):
            with open(dep_file, "r") as f:
                dep_content = f.read()
            if ":" in dep_content:
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
            return

        if make_proc.returncode != 0:
            test.compile_err = make_stderr.decode(errors="replace")
            test.compile_err_raw = make_stderr
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
            return

        run_proc = None
        for _ in range(10):
            try:
                run_proc = await asyncio.create_subprocess_exec(
                    f"./test_build/{test.name}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                break
            except OSError as e:
                if e.errno == errno.ETXTBSY:
                    await asyncio.sleep(0.05)
                    continue
                if e.errno == errno.ENOENT:
                    test.stderr = (
                        f"test executable missing: ./test_build/{test.name}"
                    )
                    test.stderr_raw = b""
                    test.state = TestState.FAILED
                    test.time_state_changed = time.monotonic()
                    return
                raise

        if run_proc is None:
            run_proc = await asyncio.create_subprocess_exec(
                f"./test_build/{test.name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        test.time_start = time.monotonic()
        active_processes[process_key] = run_proc
        run_stdout, run_stderr = await run_proc.communicate()
        if active_processes.get(process_key) is run_proc:
            active_processes.pop(process_key, None)

        if test.state == TestState.CANCELLED:
            return

        test.stdout = run_stdout.decode()
        test.stderr = run_stderr.decode(errors="replace")
        test.stderr_raw = run_stderr

        if run_proc.returncode == 0:
            test.state = TestState.PASSED
        else:
            test.state = TestState.FAILED

        test.time_state_changed = time.monotonic()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if test.state != TestState.CANCELLED:
            test.stderr = f"runner error: {e}"
            test.stderr_raw = b""
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
    finally:
        if test.state != TestState.CANCELLED:
            active_processes.pop(process_key, None)
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
        # Mark as running before scheduling so this test cannot be enqueued twice.
        test.state = TestState.RUNNING
        # Execution timer should only include binary runtime, not compilation.
        test.time_start = 0.0
        test.time_state_changed = time.monotonic()
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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
