import argparse
import asyncio
import errno
import os
import threading
import time
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, RichLog, Tree
from textual.widgets.tree import TreeNode
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from models import Test, Suite, AppState, TestState

state = AppState()
dep_index: dict[str, list[Test]] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=4, help="Number of parallel workers"
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


def _test_label(test: Test, now: float) -> Text:
    elapsed_seconds = _test_elapsed_seconds(test, now)
    spinner_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    spinner = spinner_frames[int(now * 12) % len(spinner_frames)]

    if test.state == TestState.PENDING:
        return _with_tag_text(f"{spinner} {test.name}", "yellow", "pending")
    elif test.state == TestState.RUNNING and test.time_start <= 0:
        return _with_tag_text(f"{spinner} {test.name}", "yellow", "compiling")
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
        return _with_elapsed_text(f"{spinner} {test.name}", "yellow", elapsed_seconds)
    elif test.state == TestState.PASSED:
        return _with_elapsed_text(test.name, "green", elapsed_seconds)
    elif test.state == TestState.FAILED:
        return _with_elapsed_text(test.name, "red", elapsed_seconds)

    return _with_elapsed_text(test.name, "white", elapsed_seconds)


class TestRunnerApp(App[None]):
    CSS = """
    #body {
        height: 1fr;
    }

    #suite-tree {
        width: 45%;
        border: solid $primary;
    }

    #details {
        width: 55%;
        border: solid $primary;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, watch: bool):
        super().__init__()
        self.watch_mode = watch
        self.observer = None
        self.last_signature: tuple | None = None
        self.tree_widget = None
        self.details_widget = None
        self.test_nodes: dict[str, TreeNode] = {}
        self.selected_test_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield Tree("root", id="suite-tree")
            yield RichLog(id="details", wrap=True, markup=False, highlight=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.tree_widget = self.query_one("#suite-tree", Tree)
        self.details_widget = self.query_one("#details", RichLog)

        assert self.tree_widget is not None
        self.tree_widget.show_root = True
        self._rebuild_tree()
        self._render_selected_details()

        if self.watch_mode:
            loop = asyncio.get_running_loop()
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
            self.observer = observer

        state_changed()
        self.set_interval(0.1, self._tick)

    async def action_quit(self) -> None:
        self.exit()

    def stop_observer(self) -> None:
        if self.observer is None:
            return
        self.observer.stop()
        self.observer.join()
        self.observer = None

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if isinstance(data, Test):
            self.selected_test_key = os.path.abspath(data.source_path)
        else:
            self.selected_test_key = None
        self._render_selected_details()

    def _tick(self) -> None:
        has_active = _has_active_tests()
        signature = _display_state_signature()
        if has_active or signature != self.last_signature:
            self._rebuild_tree()
            self._render_selected_details()
            self.last_signature = signature

        if not self.watch_mode and _all_tests_finished():
            self.exit()

    def _rebuild_tree(self) -> None:
        if self.tree_widget is None:
            return

        selected_key = self.selected_test_key
        tree = self.tree_widget
        tree.clear()
        self.test_nodes.clear()

        now = time.monotonic()
        root = tree.root
        root.set_label(_suite_label(state.root_suite, now))
        root.data = state.root_suite
        self._populate_suite_node(root, state.root_suite, now)
        root.expand()

        if selected_key is not None:
            selected_node = self.test_nodes.get(selected_key)
            if selected_node is not None:
                tree.select_node(selected_node)

    def _populate_suite_node(self, parent_node, suite: Suite, now: float) -> None:
        for test in suite.tests:
            node = parent_node.add(_test_label(test, now), data=test)
            key = os.path.abspath(test.source_path)
            self.test_nodes[key] = node

        for child in suite.children:
            child_node = parent_node.add(_suite_label(child, now), data=child)
            self._populate_suite_node(child_node, child, now)

    def _render_selected_details(self) -> None:
        if self.details_widget is None:
            return

        details = self.details_widget
        details.clear()

        if self.selected_test_key is None:
            passed = sum(1 for t in state.all_tests if t.state == TestState.PASSED)
            failed = sum(1 for t in state.all_tests if t.state == TestState.FAILED)
            running = sum(
                1
                for t in state.all_tests
                if t.state in (TestState.RUNNING, TestState.CANCELLED)
            )
            pending = sum(1 for t in state.all_tests if t.state == TestState.PENDING)
            details.write("Select a test from the tree to inspect details.")
            details.write(
                f"Summary: passed={passed} failed={failed} running={running} pending={pending}"
            )
            return

        test = None
        for item in state.all_tests:
            if os.path.abspath(item.source_path) == self.selected_test_key:
                test = item
                break

        if test is None:
            self.selected_test_key = None
            details.write("Selected test no longer exists.")
            return

        now = time.monotonic()
        elapsed_ms = int(_test_elapsed_seconds(test, now) * 1000)
        details.write(f"Test: {test.name}")
        details.write(f"Source: {test.source_path}")
        details.write(f"State: {test.state.value}  Elapsed: {elapsed_ms}ms")

        compile_text = None
        if test.compile_err_raw:
            compile_text = Text.from_ansi(test.compile_err_raw.decode(errors="replace"))
        elif test.compile_err.strip():
            compile_text = Text(test.compile_err)

        stderr_text = None
        if test.stderr_raw:
            stderr_text = Text.from_ansi(test.stderr_raw.decode(errors="replace"))
        elif test.stderr.strip():
            stderr_text = Text(test.stderr)

        stdout_text = None
        if test.stdout_raw:
            stdout_text = Text.from_ansi(test.stdout_raw.decode(errors="replace"))
        elif test.stdout.strip():
            stdout_text = Text(test.stdout)

        if compile_text is not None and compile_text.plain.strip():
            details.write("\n--- compile stderr ---")
            details.write(compile_text)

        if stderr_text is not None and stderr_text.plain.strip():
            details.write("\n--- runtime stderr ---")
            details.write(stderr_text)

        if stdout_text is not None and stdout_text.plain.strip():
            details.write("\n--- stdout ---")
            details.write(stdout_text)


def _all_tests_finished() -> bool:
    done_states = {TestState.PASSED, TestState.FAILED}
    return all(test.state in done_states for test in state.all_tests)


def _has_active_tests() -> bool:
    active_states = {TestState.PENDING, TestState.RUNNING, TestState.CANCELLED}
    return any(test.state in active_states for test in state.all_tests)


def _display_state_signature() -> tuple:
    return tuple(
        (
            test.name,
            test.state,
            test.time_start,
            test.time_state_changed,
            test.stdout,
            test.stderr,
            test.compile_err,
        )
        for test in state.all_tests
    )


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
    state.populate_suites("c/tests")
    generate_makefile()
    state.available_runners = args.parallel

    app = TestRunnerApp(args.watch)
    try:
        await app.run_async()
    finally:
        app.stop_observer()
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
                    test.stderr = f"test executable missing: ./test_build/{test.name}"
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

        async def _read_stream(
            stream: asyncio.StreamReader | None,
            dest_str: list[str],
            dest_raw: list[bytes],
        ):
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                dest_str.append(line.decode(errors="replace"))
                dest_raw.append(line)

        stdout_parts: list[str] = []
        stdout_raw_parts: list[bytes] = []
        stderr_parts: list[str] = []
        stderr_raw_parts: list[bytes] = []
        await asyncio.gather(
            _read_stream(run_proc.stdout, stdout_parts, stdout_raw_parts),
            _read_stream(run_proc.stderr, stderr_parts, stderr_raw_parts),
            run_proc.wait(),
        )
        if active_processes.get(process_key) is run_proc:
            active_processes.pop(process_key, None)

        if test.state == TestState.CANCELLED:
            return

        test.stdout = "".join(stdout_parts)
        test.stdout_raw = b"".join(stdout_raw_parts)
        test.stderr = "".join(stderr_parts)
        test.stderr_raw = b"".join(stderr_raw_parts)

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
