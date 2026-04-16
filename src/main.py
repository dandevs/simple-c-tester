import argparse
import asyncio
from dataclasses import dataclass
import errno
import os
import shutil
import threading
import time
from typing import Callable

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, RichLog
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from models import Test, Suite, AppState, TestState

state = AppState()
dep_index: dict[str, list[Test]] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}
stdbuf_path = shutil.which("stdbuf")
subprocess_columns = 80

SUITE_LABEL_STYLE = "bold bright_white"
TEST_PENDING_STYLE = "bold bright_yellow"
TEST_PASSED_STYLE = "bold bright_green"
TEST_FAILED_STYLE = "bold bright_red"
TEST_DEFAULT_STYLE = "bright_white"
TREE_META_STYLE = "white"
TREE_GUIDE_STYLE = "bright_black"
OUTPUT_BOX_PASS_BORDER_STYLE = "white"


@dataclass
class OutputBoxRenderMeta:
    rendered_lines: int
    left_col: int
    right_col: int


@dataclass
class OutputBoxRegion:
    test_key: str
    start_line: int
    end_line: int
    left_col: int
    right_col: int


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=4, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    parser.add_argument(
        "--output-lines",
        type=int,
        default=25,
        help="Maximum number of output lines to show per info box",
    )
    parser.add_argument(
        "--theme",
        choices=["ansi", "default"],
        default="ansi",
        help="UI theme (default: ansi)",
    )
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
            f"\tgcc -fdiagnostics-color=always -fmessage-length=$${{COLUMNS:-80}} -MMD -MP -MF {dep_file} -o {target} {source}"
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


def _suite_label(suite: Suite, now: float) -> Text:
    elapsed_ms = int(_suite_elapsed_seconds(suite, now) * 1000)
    text = Text(suite.name, style=SUITE_LABEL_STYLE)
    text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
    return text


def _test_label(test: Test, now: float) -> Text:
    elapsed_seconds = _test_elapsed_seconds(test, now)
    elapsed_ms = int(elapsed_seconds * 1000)
    spinner_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    spinner = spinner_frames[int(now * 12) % len(spinner_frames)]

    if test.state == TestState.PENDING:
        text = Text(f"{spinner} {test.name}", style=TEST_PENDING_STYLE)
        text.append(" [pending]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.RUNNING and test.time_start <= 0:
        text = Text(f"{spinner} {test.name}", style=TEST_PENDING_STYLE)
        text.append(" [compiling]", style=TREE_META_STYLE)
        return text
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
        text = Text(f"{spinner} {test.name}", style=TEST_PENDING_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.PASSED:
        text = Text(test.name, style=TEST_PASSED_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.FAILED:
        text = Text(test.name, style=TEST_FAILED_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text

    text = Text(test.name, style=TEST_DEFAULT_STYLE)
    text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
    return text


def _get_test_output(test: Test) -> list[Text] | None:
    sections: list[Text] = []

    def _to_text(raw: bytes, plain: str) -> Text | None:
        if raw:
            return Text.from_ansi(raw.decode(errors="replace"))
        if plain.strip():
            return Text(plain)
        return None

    if test.state == TestState.FAILED:
        if test.compile_err_raw or test.compile_err.strip():
            compile_text = _to_text(test.compile_err_raw, test.compile_err)
            if compile_text and compile_text.plain.strip():
                for line in compile_text.split(allow_blank=True):
                    sections.append(line)
            return _strip_trailing(sections)

        stderr_text = _to_text(test.stderr_raw, test.stderr)
        stdout_text = _to_text(test.stdout_raw, test.stdout)
        if stderr_text and stderr_text.plain.strip():
            for line in stderr_text.split(allow_blank=True):
                sections.append(line)
        if stdout_text and stdout_text.plain.strip():
            if sections:
                sections.append(Text())
            for line in stdout_text.split(allow_blank=True):
                sections.append(line)
        return _strip_trailing(sections)

    stdout_text = _to_text(test.stdout_raw, test.stdout)
    if stdout_text and stdout_text.plain.strip():
        for line in stdout_text.split(allow_blank=True):
            sections.append(line)
    return _strip_trailing(sections)


def _strip_trailing(sections: list[Text]) -> list[Text] | None:
    while sections and not sections[-1].plain.strip():
        sections.pop()
    return sections if sections else None


def _text_visual_width(text: Text) -> int:
    return max((len(line) for line in text.split(allow_blank=True)), default=0)


def _wrap_output_lines(
    output_lines: list[Text], max_content_width: int, log: RichLog
) -> list[Text]:
    width = max(1, max_content_width)
    wrapped: list[Text] = []
    console = getattr(log.app, "console", None)

    for line in output_lines:
        source = line.copy()
        if not source.plain:
            wrapped.append(Text())
            continue

        if console is None:
            if len(source) <= width:
                wrapped.append(source)
            else:
                offsets = list(range(width, len(source), width))
                wrapped.extend(source.divide(offsets))
            continue

        wrapped.extend(
            source.wrap(
                console,
                width,
                justify="left",
                overflow="fold",
                no_wrap=False,
            )
        )

    return wrapped if wrapped else [Text()]


def _render_output_box(
    output_lines: list[Text],
    test: Test,
    child_prefix: str,
    log: RichLog,
    max_lines: int,
    max_total_width: int,
) -> OutputBoxRenderMeta:
    max_lines = max(1, max_lines)

    # Box rows are: child_prefix + border/content + border.
    # Fill the available width and clamp content so no horizontal scrolling is needed.
    border_overhead = 6  # "└── ╭" + "╮" (equivalently "    │ " + " │")
    available_inner_width = max(
        2, max_total_width - len(child_prefix) - border_overhead
    )
    box_inner_width = available_inner_width
    max_content_width = max(0, box_inner_width - 2)
    wrapped_lines = _wrap_output_lines(output_lines, max_content_width, log)
    visible_lines = wrapped_lines[-max_lines:]

    border_style = (
        TEST_FAILED_STYLE
        if test.state == TestState.FAILED
        else OUTPUT_BOX_PASS_BORDER_STYLE
    )
    dashes = "─" * box_inner_width
    top_plain = child_prefix + "└── ╭" + dashes + "╮"

    top = Text(top_plain, style=border_style)
    log.write(top)

    for line in visible_lines:
        padded = line.copy()
        pad_count = max(0, max_content_width - _text_visual_width(padded))
        if pad_count > 0:
            padded.append(" " * pad_count)

        content_line = Text(child_prefix + "    ")
        content_line.append("│ ", style=border_style)
        content_line.append(padded)
        content_line.append(" │", style=border_style)
        log.write(content_line)

    bottom = Text(child_prefix + "    ╰" + dashes + "╯", style=border_style)
    log.write(bottom)

    return OutputBoxRenderMeta(
        rendered_lines=len(visible_lines) + 2,
        left_col=len(child_prefix),
        right_col=max(0, len(top_plain) - 1),
    )


class TestOutputScreen(Screen[None]):
    CSS = """
    #output-full {
        height: 1fr;
        border: none;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
        scrollbar-color: transparent;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("ctrl+c", "close", "Back", priority=True),
    ]

    def __init__(self, test: Test):
        super().__init__()
        self.test = test
        self.log_widget: RichLog | None = None
        self.last_signature: tuple | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(
            id="output-full",
            wrap=False,
            markup=False,
            highlight=False,
            auto_scroll=False,
        )
        yield Footer(show_command_palette=False)

    async def on_mount(self) -> None:
        self.log_widget = self.query_one("#output-full", RichLog)
        self._render_output(force=True)
        self.set_interval(0.1, self._tick)

    async def action_close(self) -> None:
        self.app.pop_screen()

    def _signature(self) -> tuple:
        return (
            self.test.state,
            self.test.time_state_changed,
            self.test.stdout,
            self.test.stderr,
            self.test.compile_err,
        )

    def _tick(self) -> None:
        self._render_output()

    def _render_output(self, force: bool = False) -> None:
        if self.log_widget is None:
            return

        signature = self._signature()
        if not force and signature == self.last_signature:
            return

        log = self.log_widget
        previous_scroll_y = log.scroll_y
        near_bottom = (log.max_scroll_y - log.scroll_y) <= 1

        log.clear()
        title = Text("Output: ", style="bold")
        title.append(self.test.name, style="bold")
        title.append(f" [{self.test.state.value}]", style=TREE_META_STYLE)
        log.write(title)
        log.write(Text(self.test.source_path, style=TREE_META_STYLE))
        log.write(Text())

        output_lines = _get_test_output(self.test)
        if output_lines:
            for line in output_lines:
                log.write(line)
        else:
            log.write(Text("No output.", style=TREE_META_STYLE))

        if near_bottom:
            log.scroll_end(animate=False, immediate=True)
        else:
            log.scroll_to(y=previous_scroll_y, animate=False, immediate=True)

        self.last_signature = signature


class TestRunnerApp(App[None]):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #tree-view {
        height: 1fr;
        border: none;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
        scrollbar-color: transparent;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
    ]

    def __init__(self, watch: bool, output_max_lines: int, theme_name: str):
        super().__init__()
        self.watch_mode = watch
        self.observer = None
        self.last_signature: tuple | None = None
        self.log_widget: RichLog | None = None
        self.output_max_lines = max(1, output_max_lines)
        self.rendered_output_boxes: list[OutputBoxRegion] = []
        self._tree_line_cursor = 0
        if theme_name == "ansi":
            self.theme = "textual-ansi"

    def compose(self) -> ComposeResult:
        yield RichLog(id="tree-view", wrap=False, markup=False, highlight=False)
        yield Footer(show_command_palette=False)

    async def on_mount(self) -> None:
        self.log_widget = self.query_one("#tree-view", RichLog)
        self._render_tree()

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
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        self.exit()

    def _find_output_box_at(self, x: int, y: int) -> OutputBoxRegion | None:
        for box in self.rendered_output_boxes:
            if (
                box.start_line <= y <= box.end_line
                and box.left_col <= x <= box.right_col
            ):
                return box
        return None

    def _get_mouse_box_key(self, event: events.MouseEvent) -> str | None:
        if self.log_widget is None:
            return None
        offset = event.get_content_offset(self.log_widget)
        if offset is None:
            return None

        virtual_x = int(offset.x + self.log_widget.scroll_x)
        virtual_y = int(offset.y + self.log_widget.scroll_y)
        box = self._find_output_box_at(virtual_x, virtual_y)
        return box.test_key if box is not None else None

    def _get_test_by_key(self, test_key: str) -> Test | None:
        for test in state.all_tests:
            if test.source_path == test_key:
                return test
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        box_key = self._get_mouse_box_key(event)
        if box_key is None:
            return
        test = self._get_test_by_key(box_key)
        if test is None:
            return

        self.push_screen(TestOutputScreen(test))
        event.prevent_default()
        event.stop()

    def stop_observer(self) -> None:
        if self.observer is None:
            return
        self.observer.stop()
        self.observer.join()
        self.observer = None

    def _tick(self) -> None:
        has_active = _has_active_tests()
        signature = _display_state_signature()
        if has_active or signature != self.last_signature:
            self._render_tree()
            self.last_signature = signature

        if not self.watch_mode and _all_tests_finished():
            self.exit()

    def _render_tree(self) -> None:
        if self.log_widget is None:
            return

        global subprocess_columns

        log = self.log_widget
        subprocess_columns = max(20, log.size.width or self.size.width or 80)
        log.clear()
        now = time.monotonic()
        self._tree_line_cursor = 0
        self.rendered_output_boxes = []

        root = state.root_suite
        self._write_tree_line(log, _suite_label(root, now))

        children = list(root.tests) + list(root.children)
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            self._render_node(child, "", is_last, log, now)

    def _write_tree_line(self, log: RichLog, line: Text) -> None:
        log.write(line)
        self._tree_line_cursor += 1

    def _render_node(
        self, node: Test | Suite, prefix: str, is_last: bool, log: RichLog, now: float
    ) -> None:
        connector = "└── " if is_last else "├── "
        continuation = "    " if is_last else "│   "
        child_prefix = prefix + continuation

        if isinstance(node, Test):
            guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
            self._write_tree_line(log, guide + _test_label(node, now))

            output = _get_test_output(node)
            if output:
                start_line = self._tree_line_cursor
                render_meta = _render_output_box(
                    output,
                    node,
                    child_prefix,
                    log,
                    self.output_max_lines,
                    subprocess_columns,
                )
                self._tree_line_cursor += render_meta.rendered_lines

                self.rendered_output_boxes.append(
                    OutputBoxRegion(
                        test_key=node.source_path,
                        start_line=start_line,
                        end_line=start_line + render_meta.rendered_lines - 1,
                        left_col=render_meta.left_col,
                        right_col=render_meta.right_col,
                    )
                )
        else:
            guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
            self._write_tree_line(log, guide + _suite_label(node, now))

            children = list(node.tests) + list(node.children)
            for i, child in enumerate(children):
                is_child_last = i == len(children) - 1
                self._render_node(child, child_prefix, is_child_last, log, now)


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

    app = TestRunnerApp(args.watch, args.output_lines, args.theme)
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

        proc_env = os.environ.copy()
        proc_env["COLUMNS"] = str(max(20, subprocess_columns))

        make_proc = await asyncio.create_subprocess_exec(
            "make",
            "-f",
            "test_build/Makefile",
            f"test_build/{test.name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
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

        run_cmd = [f"./test_build/{test.name}"]
        if stdbuf_path:
            run_cmd = [stdbuf_path, "-oL", "-eL", *run_cmd]

        run_proc = None
        for _ in range(10):
            try:
                run_proc = await asyncio.create_subprocess_exec(
                    *run_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
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
                *run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )

        test.time_start = time.monotonic()
        active_processes[process_key] = run_proc

        test.stdout = ""
        test.stdout_raw = b""
        test.stderr = ""
        test.stderr_raw = b""

        async def _read_stream(
            stream: asyncio.StreamReader | None,
            dest_str: list[str],
            dest_raw: list[bytes],
            is_stdout: bool,
        ):
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded_line = line.decode(errors="replace")
                dest_str.append(decoded_line)
                dest_raw.append(line)

                # Keep output fields updated during execution so UI can render live logs.
                if is_stdout:
                    test.stdout += decoded_line
                    test.stdout_raw += line
                else:
                    test.stderr += decoded_line
                    test.stderr_raw += line

        stdout_parts: list[str] = []
        stdout_raw_parts: list[bytes] = []
        stderr_parts: list[str] = []
        stderr_raw_parts: list[bytes] = []
        await asyncio.gather(
            _read_stream(run_proc.stdout, stdout_parts, stdout_raw_parts, True),
            _read_stream(run_proc.stderr, stderr_parts, stderr_raw_parts, False),
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
        test.state = TestState.RUNNING
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
