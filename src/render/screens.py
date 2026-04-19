import shutil
import subprocess
import os
import asyncio
import time
from rich.console import Group
from rich.syntax import Syntax

from rich.text import Text
from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog, Static

import state as global_state
from models import Test, TestState
from .output import get_test_output
from .styles import TREE_META_STYLE
from runner import (
    start_debug_session,
    stop_debug_session,
    debug_step_next,
    debug_step_in,
    debug_step_out,
    debug_continue,
    debug_interrupt,
    debug_continue_auto_trace,
    is_debug_active,
    state_changed,
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
    #controls-footer {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: transparent;
        color: ansi_bright_black;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("ctrl+c", "close", "Go Back", priority=True),
    ]

    def __init__(self, test: Test):
        super().__init__()
        self.test = test
        self.log_widget: RichLog | None = None
        self.footer_widget: Static | None = None
        self.last_signature: tuple | None = None
        self._render_lines: list[Text] = []
        self._plain_lines: list[str] = []
        self._selection_anchor: tuple[int, int] | None = None
        self._selection_cursor: tuple[int, int] | None = None
        self._selection_active = False
        self._footer_timer = None

    def compose(self) -> ComposeResult:
        yield RichLog(
            id="output-full",
            wrap=False,
            markup=False,
            highlight=False,
            auto_scroll=False,
        )
        yield Static("", id="controls-footer")

    async def on_mount(self) -> None:
        self.log_widget = self.query_one("#output-full", RichLog)
        self.footer_widget = self.query_one("#controls-footer", Static)
        self._set_footer_text()
        self._render_output(force=True)
        self.set_interval(0.1, self._tick)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1 or self.log_widget is None:
            return

        position = self._event_to_position(event)
        if position is None:
            return

        self._selection_anchor = position
        self._selection_cursor = position
        self._selection_active = True
        self._render_output(force=True)
        event.prevent_default()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._selection_active or self.log_widget is None:
            return

        position = self._event_to_position(event)
        if position is None or position == self._selection_cursor:
            return

        self._selection_cursor = position
        self._render_output(force=True)
        event.prevent_default()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._selection_active:
            return

        position = self._event_to_position(event)
        if position is not None:
            self._selection_cursor = position

        selected_text = self._extract_selected_text()
        self._selection_active = False
        self._render_output(force=True)

        if not selected_text:
            event.prevent_default()
            event.stop()
            return

        if self._copy_to_clipboard(selected_text):
            self._set_footer_text("Copied selection to clipboard.")
        else:
            self._set_footer_text(
                "Clipboard unavailable. Install pyperclip, wl-copy, or xclip.",
                warning=True,
            )

        if self._footer_timer is not None:
            self._footer_timer.stop()
        self._footer_timer = self.set_timer(2.0, self._clear_footer_message)

        event.prevent_default()
        event.stop()

    async def action_close(self) -> None:
        if is_debug_active(self.test):
            await stop_debug_session(self.test)
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

    def _base_footer_text(self) -> str:
        return f"Output: {self.test.name}  |  Drag: Select + Copy  |  Ctrl+C/Esc: Go Back"

    def _set_footer_text(self, message: str | None = None, warning: bool = False) -> None:
        if self.footer_widget is None:
            return
        if message is None:
            self.footer_widget.update(Text(self._base_footer_text(), style="bright_black"))
            return

        style = "yellow" if warning else "bright_black"
        self.footer_widget.update(Text(message, style=style))

    def _clear_footer_message(self) -> None:
        self._footer_timer = None
        self._set_footer_text()

    def _clear_selection(self) -> None:
        self._selection_anchor = None
        self._selection_cursor = None
        self._selection_active = False

    def _build_output_lines(self) -> list[Text]:
        lines: list[Text] = []

        title = Text("Output: ", style="bold")
        title.append(self.test.name, style="bold")
        title.append(f" [{self.test.state.value}]", style=TREE_META_STYLE)
        lines.append(title)
        lines.append(Text(self.test.source_path, style=TREE_META_STYLE))
        lines.append(Text())

        output_lines = get_test_output(self.test)
        if output_lines:
            lines.extend(output_lines)
        else:
            lines.append(Text("No output.", style=TREE_META_STYLE))

        return lines

    def _event_to_position(self, event: events.MouseEvent) -> tuple[int, int] | None:
        if self.log_widget is None or not self._plain_lines:
            return None

        offset = event.get_content_offset(self.log_widget)
        if offset is None:
            return None

        virtual_x = max(0, int(offset.x + self.log_widget.scroll_x))
        virtual_y = max(0, int(offset.y + self.log_widget.scroll_y))

        line_index = min(virtual_y, len(self._plain_lines) - 1)
        line_text = self._plain_lines[line_index]
        column_index = self._visual_column_to_index(line_text, virtual_x)
        return (line_index, column_index)

    def _visual_column_to_index(self, line: str, visual_column: int) -> int:
        if visual_column <= 0 or not line:
            return 0

        current = 0
        for index, char in enumerate(line):
            width = max(1, cell_len(char))
            next_column = current + width
            if visual_column < next_column:
                return index
            if visual_column == next_column:
                return index + 1
            current = next_column

        return len(line)

    def _clamp_position(self, position: tuple[int, int]) -> tuple[int, int] | None:
        if not self._plain_lines:
            return None

        line_index = min(max(0, position[0]), len(self._plain_lines) - 1)
        line_length = len(self._plain_lines[line_index])
        column_index = min(max(0, position[1]), line_length)
        return (line_index, column_index)

    def _selection_bounds(
        self,
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self._selection_anchor is None or self._selection_cursor is None:
            return None

        start = self._clamp_position(self._selection_anchor)
        end = self._clamp_position(self._selection_cursor)
        if start is None or end is None or start == end:
            return None

        return (start, end) if start <= end else (end, start)

    def _extract_selected_text(self) -> str:
        selection = self._selection_bounds()
        if selection is None:
            return ""

        (start_line, start_col), (end_line, end_col) = selection
        if start_line == end_line:
            return self._plain_lines[start_line][start_col:end_col]

        selected_lines: list[str] = []
        for line_index in range(start_line, end_line + 1):
            line = self._plain_lines[line_index]
            if line_index == start_line:
                selected_lines.append(line[start_col:])
            elif line_index == end_line:
                selected_lines.append(line[:end_col])
            else:
                selected_lines.append(line)

        return "\n".join(selected_lines)

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            import pyperclip

            pyperclip.copy(text)
            return True
        except Exception:
            pass

        for command in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            executable = command[0]
            if shutil.which(executable) is None:
                continue
            try:
                result = subprocess.run(
                    command,
                    input=text,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                continue

        return False

    def _lines_with_selection(self) -> list[Text]:
        display_lines = [line.copy() for line in self._render_lines]
        if not self._selection_active:
            return display_lines

        selection = self._selection_bounds()
        if selection is None:
            return display_lines

        (start_line, start_col), (end_line, end_col) = selection
        for line_index in range(start_line, end_line + 1):
            line_length = len(self._plain_lines[line_index])
            range_start = 0
            range_end = line_length

            if line_index == start_line:
                range_start = start_col
            if line_index == end_line:
                range_end = end_col

            if range_start < range_end:
                display_lines[line_index].stylize("reverse", range_start, range_end)

        return display_lines

    def _render_output(self, force: bool = False) -> None:
        if self.log_widget is None:
            return

        signature = self._signature()
        if not force and signature == self.last_signature:
            return

        if signature != self.last_signature:
            self._clear_selection()
            self._render_lines = self._build_output_lines()
            self._plain_lines = [line.plain for line in self._render_lines]

        log = self.log_widget
        previous_scroll_y = log.scroll_y
        near_bottom = (log.max_scroll_y - log.scroll_y) <= 1

        log.clear()
        for line in self._lines_with_selection():
            log.write(line)

        if near_bottom:
            log.scroll_end(animate=False, immediate=True)
        else:
            log.scroll_to(y=previous_scroll_y, animate=False, immediate=True)

        self.last_signature = signature


class TestDebuggerScreen(Screen[None]):
    CSS = """
    #debug-header {
        height: 1;
        min-height: 1;
        padding: 0 1;
        text-style: bold;
    }
    #timeline-overview {
        height: 2;
        min-height: 2;
        padding: 0 1;
    }
    #timeline-detail {
        height: 2;
        min-height: 2;
        padding: 0 1;
    }
    #timeline-meta {
        height: 3;
        min-height: 3;
        padding: 0 1;
        color: #8f96a3;
    }
    #story-code {
        height: 1fr;
        border: none;
        padding: 0 1;
    }
    #vars-panel {
        height: 4;
        min-height: 3;
        padding: 0 1;
        border: none;
    }
    #debug-footer {
        height: 1;
        min-height: 1;
        padding: 0 1;
        color: #8f96a3;
    }
    """

    STORY_BAR_BASE = "#2e3440"
    STORY_BAR_WINDOW = "#4c566a"
    STORY_BAR_ACTIVE = "#6ea8fe"
    STORY_BAR_SELECTED = "#ffd166"
    STORY_META_HIGHLIGHT = "#89dceb"
    STORY_META_SELECTED = "#ffd166"
    STORY_HELP = "#7f8a9d"
    STORY_LINE_MARKER = "#ff6b6b"
    STORY_CODE_BG = "#272822"
    STORY_CURRENT_LINE = "#34352d"
    STORY_CURRENT_LINE_SELECTED = "#49483e"

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("ctrl+c", "close", "Back", priority=True),
        Binding("d", "toggle_debug", "Debug"),
        Binding("t", "toggle_timeline", "Timeline"),
        Binding("r", "rerun_test", "Rerun"),
        Binding("n", "step_next", "Next"),
        Binding("i", "step_in", "Step In"),
        Binding("o", "step_out", "Step Out"),
        Binding("c", "continue_run", "Continue"),
        Binding("k", "interrupt_run", "Interrupt"),
        Binding("a", "auto_trace", "Auto Trace"),
        Binding("plus", "zoom_in", "Zoom In"),
        Binding("minus", "zoom_out", "Zoom Out"),
        Binding("left", "timeline_prev", "Prev Step"),
        Binding("right", "timeline_next", "Next Step"),
        Binding("ctrl+left", "timeline_prev_10", "-10 Steps"),
        Binding("ctrl+right", "timeline_next_10", "+10 Steps"),
    ]

    def __init__(self, test: Test):
        super().__init__()
        self.test = test
        self.header_widget: Static | None = None
        self.timeline_overview_widget: Static | None = None
        self.timeline_detail_widget: Static | None = None
        self.timeline_meta_widget: Static | None = None
        self.code_widget: Static | None = None
        self.vars_widget: Static | None = None
        self.footer_widget: Static | None = None
        self.last_signature: tuple | None = None
        self.selected_frame_index = -1
        self.zoom_level = 1
        self._source_cache: dict[str, list[str]] = {}
        self._line_frames_cache_key: tuple | None = None
        self._line_frames_cache: list = []
        self._footer_timer = None
        self._action_task: asyncio.Task | None = None
        self._last_log_count = -1

    def compose(self) -> ComposeResult:
        yield Static("", id="debug-header")
        yield Static("", id="timeline-overview")
        yield Static("", id="timeline-detail")
        yield Static("", id="timeline-meta")
        yield Static("", id="story-code")
        yield Static("", id="vars-panel")
        yield Static("", id="debug-footer")

    async def on_mount(self) -> None:
        self.header_widget = self.query_one("#debug-header", Static)
        self.timeline_overview_widget = self.query_one("#timeline-overview", Static)
        self.timeline_detail_widget = self.query_one("#timeline-detail", Static)
        self.timeline_meta_widget = self.query_one("#timeline-meta", Static)
        self.code_widget = self.query_one("#story-code", Static)
        self.vars_widget = self.query_one("#vars-panel", Static)
        self.footer_widget = self.query_one("#debug-footer", Static)
        self.test.timeline_capture_enabled = True
        self._set_footer_text()
        self._refresh_view(force=True)
        if not self._line_frames() and self.test.state != TestState.RUNNING and not is_debug_active(self.test):
            self._set_footer_text("No Test Story yet. Recording is on; press R to run.")
        self.set_interval(0.1, self._tick)

    async def action_close(self) -> None:
        self.app.pop_screen()

    async def action_toggle_timeline(self) -> None:
        self.test.timeline_capture_enabled = not self.test.timeline_capture_enabled
        mode = "enabled" if self.test.timeline_capture_enabled else "disabled"
        self._set_footer_text(f"Timeline capture {mode} for {self.test.name}.")

    async def action_rerun_test(self) -> None:
        if self.test.debug_running or is_debug_active(self.test):
            await self._run_action(self._restart_debug_session(), "Debugger restarted.")
            return

        self._reset_story_state()
        self.test.state = TestState.PENDING
        self.test.time_start = 0.0
        self.test.time_state_changed = time.monotonic()
        state_changed()
        self._set_footer_text("Queued test rerun.")

    async def _restart_debug_session(self) -> None:
        await stop_debug_session(self.test)
        self._reset_story_state()
        await start_debug_session(self.test, auto_trace=False)

    def _reset_story_state(self) -> None:
        self.test.timeline_events = []
        self.test.debug_logs = []
        self.test.stdout = ""
        self.test.stdout_raw = b""
        self.test.stderr = ""
        self.test.stderr_raw = b""
        self.test.compile_err = ""
        self.test.compile_err_raw = b""
        self._line_frames_cache_key = None
        self._line_frames_cache = []
        self.selected_frame_index = -1

    async def action_toggle_debug(self) -> None:
        if is_debug_active(self.test):
            await self._run_action(stop_debug_session(self.test), "Debugger stopped.")
            return
        await self._run_action(
            start_debug_session(self.test, auto_trace=False),
            "Debugger started at main().",
        )

    async def action_step_next(self) -> None:
        if not self._ensure_debug_active("Step"):
            return
        await self._run_action(debug_step_next(self.test), "Stepped over.")

    async def action_step_in(self) -> None:
        if not self._ensure_debug_active("Step-in"):
            return
        await self._run_action(debug_step_in(self.test), "Stepped in.")

    async def action_step_out(self) -> None:
        if not self._ensure_debug_active("Step-out"):
            return
        await self._run_action(debug_step_out(self.test), "Stepped out.")

    async def action_continue_run(self) -> None:
        if not self._ensure_debug_active("Continue"):
            return
        await self._run_action(debug_continue(self.test), "Continued execution.")

    async def action_interrupt_run(self) -> None:
        if not self._ensure_debug_active("Interrupt"):
            return
        await self._run_action(debug_interrupt(self.test), "Sent interrupt.")

    async def action_auto_trace(self) -> None:
        if not self._ensure_debug_active("Auto trace"):
            return
        await self._run_action(debug_continue_auto_trace(self.test), "Auto trace complete.")

    def action_zoom_in(self) -> None:
        self.zoom_level = min(64, self.zoom_level * 2)
        self._refresh_view(force=True)

    def action_zoom_out(self) -> None:
        self.zoom_level = max(1, self.zoom_level // 2)
        self._refresh_view(force=True)

    def action_timeline_prev(self) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self._ensure_selected_frame_index()
        self.selected_frame_index = max(0, self.selected_frame_index - 1)
        self._refresh_view(force=True)

    def action_timeline_next(self) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self._ensure_selected_frame_index()
        self.selected_frame_index = min(len(frames) - 1, self.selected_frame_index + 1)
        self._refresh_view(force=True)

    def action_timeline_prev_10(self) -> None:
        self._timeline_jump(-10)

    def action_timeline_next_10(self) -> None:
        self._timeline_jump(10)

    def _timeline_jump(self, offset: int) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self._ensure_selected_frame_index()
        self.selected_frame_index = min(
            len(frames) - 1,
            max(0, self.selected_frame_index + offset),
        )
        self._refresh_view(force=True)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        if self.timeline_overview_widget is None or self.timeline_detail_widget is None:
            return

        if event.widget == self.timeline_overview_widget:
            index = self._mouse_to_overview_index(event)
            if index is not None:
                self.selected_frame_index = index
                self._refresh_view(force=True)
                event.prevent_default()
                event.stop()
            return

        if event.widget == self.timeline_detail_widget:
            index = self._mouse_to_detail_index(event)
            if index is not None:
                self.selected_frame_index = index
                self._refresh_view(force=True)
                event.prevent_default()
                event.stop()

    def _mouse_to_overview_index(self, event: events.MouseDown) -> int | None:
        frames = self._line_frames()
        if not frames or self.timeline_overview_widget is None:
            return None
        offset = event.get_content_offset(self.timeline_overview_widget)
        if offset is None:
            return None
        width = max(1, self.timeline_overview_widget.size.width - 2)
        x = min(max(int(offset.x), 0), width - 1)
        denom = max(1, width - 1)
        return int((x / denom) * (len(frames) - 1))

    def _mouse_to_detail_index(self, event: events.MouseDown) -> int | None:
        frames = self._line_frames()
        if not frames or self.timeline_detail_widget is None:
            return None
        offset = event.get_content_offset(self.timeline_detail_widget)
        if offset is None:
            return None
        window_start, window_end = self._timeline_window(len(frames))
        count = max(1, window_end - window_start)
        width = max(1, self.timeline_detail_widget.size.width - 2)
        x = min(max(int(offset.x), 0), width - 1)
        denom = max(1, width - 1)
        return window_start + int((x / denom) * (count - 1))

    async def _run_action(self, action_coro, success_message: str) -> None:
        if self._action_task is not None and not self._action_task.done():
            closer = getattr(action_coro, "close", None)
            if callable(closer):
                closer()
            self._set_footer_text("A debug action is already running.", warning=True)
            return

        async def _runner() -> None:
            try:
                await action_coro
                self._set_footer_text(success_message)
            except Exception as error:
                self._set_footer_text(f"Debug action failed: {error}", warning=True)
            finally:
                self._action_task = None

        self._action_task = asyncio.create_task(_runner())

    def _tick(self) -> None:
        self._refresh_view()

    def _ensure_debug_active(self, action_label: str) -> bool:
        if is_debug_active(self.test):
            return True
        self._set_footer_text(
            f"Debugger idle. Press D to start before {action_label.lower()}.",
            warning=True,
        )
        return False

    def _signature(self) -> tuple:
        last_event = self.test.timeline_events[-1] if self.test.timeline_events else None
        last_event_sig = (
            last_event.kind,
            last_event.timestamp,
            last_event.file_path,
            last_event.line,
            last_event.message,
        ) if last_event else ()
        return (
            self.test.state,
            self.test.time_state_changed,
            len(self.test.timeline_events),
            self.test.debug_running,
            self.test.debug_exited,
            self.test.debug_exit_code,
            self.test.timeline_capture_enabled,
            len(self.test.debug_logs),
            last_event_sig,
            self.selected_frame_index,
            self.zoom_level,
        )

    def _base_footer_text(self) -> str:
        if not self._line_frames():
            return (
                "No story yet. Press R to run. Scrub: <-/-> or Ctrl+<- / Ctrl+->  D: debug"
            )
        return (
            "Scrub: click or <-/-> or Ctrl+<- / Ctrl+->  Steps: N/I/O/C/K  A: auto trace  R: rerun/restart debug  D: debug"
        )

    def _set_footer_text(self, message: str | None = None, warning: bool = False) -> None:
        if self.footer_widget is None:
            return
        if message is None:
            self.footer_widget.update(Text(self._base_footer_text(), style="bright_black"))
            return

        style = "yellow" if warning else "bright_black"
        self.footer_widget.update(Text(message, style=style))
        if self._footer_timer is not None:
            self._footer_timer.stop()
        self._footer_timer = self.set_timer(2.0, self._clear_footer_message)

    def _clear_footer_message(self) -> None:
        self._footer_timer = None
        self._set_footer_text()

    def _line_frames(self):
        skip_seq = max(1, int(global_state.tsv_skip_seq_lines))
        debug_mode = is_debug_active(self.test) or self.test.debug_running
        cache_key = (
            id(self.test.timeline_events),
            len(self.test.timeline_events),
            self.test.time_state_changed,
            skip_seq,
            debug_mode,
        )
        if self._line_frames_cache_key == cache_key:
            return self._line_frames_cache

        frames = [
            event
            for event in self.test.timeline_events
            if self._event_has_useful_source_line(event.file_path, event.line)
        ]

        if len(frames) <= 1:
            self._line_frames_cache_key = cache_key
            self._line_frames_cache = frames
            return frames

        if debug_mode:
            self._line_frames_cache_key = cache_key
            self._line_frames_cache = frames
            return frames

        if skip_seq <= 1:
            self._line_frames_cache_key = cache_key
            self._line_frames_cache = frames
            return frames

        filtered = [frames[0]]
        seq_since_emit = 0
        prev = frames[0]
        prev_abs_path = os.path.abspath(prev.file_path)

        for frame in frames[1:]:
            frame_abs_path = os.path.abspath(frame.file_path)
            same_file = frame_abs_path == prev_abs_path
            same_function = frame.function == prev.function
            is_sequential = same_file and same_function and frame.line == (prev.line + 1)

            if is_sequential:
                seq_since_emit += 1
                if seq_since_emit >= skip_seq:
                    filtered.append(frame)
                    seq_since_emit = 0
            else:
                filtered.append(frame)
                seq_since_emit = 0

            prev = frame
            prev_abs_path = frame_abs_path

        if filtered[-1] != frames[-1]:
            filtered.append(frames[-1])

        self._line_frames_cache_key = cache_key
        self._line_frames_cache = filtered
        return filtered

    def _event_has_useful_source_line(self, file_path: str, line_number: int) -> bool:
        if not file_path or line_number <= 0:
            return False
        source_path = os.path.abspath(file_path)
        lines = self._load_source_lines(source_path)
        if not lines or line_number > len(lines):
            return False
        return bool(lines[line_number - 1].strip())

    def _ensure_selected_frame_index(self) -> None:
        total = len(self._line_frames())
        if total <= 0:
            self.selected_frame_index = -1
            return

        if self.selected_frame_index < 0 or self.selected_frame_index >= total:
            self.selected_frame_index = total - 1

    def _display_path(self, file_path: str) -> str:
        if not file_path:
            return ""

        abs_path = os.path.abspath(file_path)
        try:
            rel_path = os.path.relpath(abs_path, os.getcwd())
            if rel_path.startswith(".."):
                return abs_path
            return rel_path
        except ValueError:
            return abs_path

    def _detect_language(self, file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in {".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}:
            return "cpp"
        return "c"

    def _timeline_window(self, total: int) -> tuple[int, int]:
        if total <= 0:
            return (0, 0)

        self._ensure_selected_frame_index()
        window_size = max(8, total // max(1, self.zoom_level))
        window_size = min(total, window_size)

        center = self.selected_frame_index
        start = max(0, center - (window_size // 2))
        end = start + window_size
        if end > total:
            end = total
            start = max(0, end - window_size)
        return (start, end)

    def _build_overview_text(self, width: int, start: int, end: int) -> Text:
        total = len(self._line_frames())
        line = Text()
        if total == 0:
            line.append("(no line-execution frames yet)", style=self.STORY_HELP)
            return line

        for column in range(width):
            bucket_start = int((column / width) * total)
            bucket_end = int(((column + 1) / width) * total)
            bucket_end = max(bucket_end, bucket_start + 1)
            inside_window = not (bucket_end <= start or bucket_start >= end)
            style = self.STORY_BAR_WINDOW if inside_window else self.STORY_BAR_BASE
            if bucket_start <= self.selected_frame_index < bucket_end:
                style = self.STORY_BAR_SELECTED
            line.append("█", style=style)

        return line

    def _build_detail_text(self, width: int, start: int, end: int) -> Text:
        total = len(self._line_frames())
        line = Text()
        if total == 0 or end <= start:
            line.append("(no frame window)", style=self.STORY_HELP)
            return line

        count = end - start
        for column in range(width):
            event_start = start + int((column / width) * count)
            event_end = start + int(((column + 1) / width) * count)
            event_end = max(event_end, event_start + 1)
            event_end = min(event_end, end)
            style = self.STORY_BAR_ACTIVE
            selected_here = event_start <= self.selected_frame_index < event_end
            if selected_here:
                style = f"{self.STORY_BAR_SELECTED} bold"
            line.append("█", style=style)

        return line

    def _load_source_lines(self, file_path: str) -> list[str]:
        cached = self._source_cache.get(file_path)
        if cached is not None:
            return cached

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            lines = []

        self._source_cache[file_path] = lines
        return lines

    def _frame_cards_window(self, total: int) -> tuple[int, int]:
        if total <= 0:
            return (0, 0)

        self._ensure_selected_frame_index()
        height = 1
        if self.code_widget is not None:
            height = max(1, self.code_widget.size.height)

        lines_above = max(0, int(global_state.tsv_lines_above))
        lines_below = max(0, int(global_state.tsv_lines_below))
        code_line_count = 1 + lines_above + lines_below
        card_height = 1 + code_line_count
        card_count = max(1, (height + 1) // (card_height + 1))
        card_count = min(total, card_count)

        center = self.selected_frame_index
        start = max(0, center - (card_count // 2))
        end = start + card_count
        if end > total:
            end = total
            start = max(0, end - card_count)
        return (start, end)

    def _build_frame_snippet(
        self,
        source_path: str,
        source_lines: list[str],
        line_number: int,
        snippet_start: int,
        snippet_end: int,
        selected: bool,
        code_width: int,
    ) -> Syntax:
        padded_width = max(1, code_width)
        snippet_lines = [
            source_lines[line_no - 1].ljust(padded_width)
            for line_no in range(snippet_start, snippet_end + 1)
        ]
        snippet_text = "\n".join(snippet_lines)
        line_count = len(snippet_lines)
        syntax = Syntax(
            snippet_text,
            self._detect_language(source_path),
            line_numbers=True,
            start_line=snippet_start,
            highlight_lines={line_number},
            code_width=padded_width,
            word_wrap=False,
            theme="monokai",
            background_color=self.STORY_CODE_BG,
        )

        if not selected:
            for local_line in range(1, line_count + 1):
                syntax.stylize_range(
                    "#9aa0a6",
                    (local_line, 0),
                    (local_line, padded_width),
                )

        local_line = (line_number - snippet_start) + 1
        line_length = padded_width
        highlight_bg = self.STORY_CURRENT_LINE_SELECTED if selected else self.STORY_CURRENT_LINE
        syntax.stylize_range(
            f"on {highlight_bg}",
            (local_line, 0),
            (local_line, line_length),
        )
        if selected:
            syntax.stylize_range(
                "bold",
                (local_line, 0),
                (local_line, line_length),
            )

        return syntax

    def _render_variables_panel(self, selected_event) -> None:
        if self.vars_widget is None:
            return

        if selected_event is None:
            self.vars_widget.update(Text("Variables: (no selected frame)", style=self.STORY_HELP))
            return

        vars_list = list(selected_event.variables or [])
        if not vars_list:
            self.vars_widget.update(Text("Variables: (none captured for this frame)", style=self.STORY_HELP))
            return

        panel_width = max(20, self.vars_widget.size.width - 2)
        panel_height = max(1, self.vars_widget.size.height)

        items: list[Text] = []
        max_item_width = 0
        for name, value in vars_list:
            entry = Text()
            entry.append(f"{name}", style=self.STORY_META_HIGHLIGHT)
            entry.append("=", style=self.STORY_HELP)
            entry.append(value, style="#f8f8f2")
            if len(entry.plain) > 48:
                entry = Text(entry.plain[:45] + "...", style="#f8f8f2")
            max_item_width = max(max_item_width, len(entry.plain))
            items.append(entry)

        col_width = max(12, min(48, max_item_width + 2))
        cols = max(1, panel_width // col_width)
        data_rows = max(1, panel_height - 1)
        capacity = cols * data_rows

        shown = items[:capacity]
        hidden_count = max(0, len(items) - len(shown))

        output = Text()
        output.append("Variables", style=f"bold {self.STORY_META_SELECTED}")
        output.append(f"  ({len(items)} in scope)", style=self.STORY_HELP)
        output.append("\n")

        for row in range(data_rows):
            row_entries: list[Text] = []
            for col in range(cols):
                idx = row * cols + col
                if idx >= len(shown):
                    break
                cell = shown[idx].copy()
                pad = col_width - len(cell.plain)
                if pad > 0:
                    cell.append(" " * pad)
                row_entries.append(cell)

            if row_entries:
                for cell in row_entries:
                    output.append_text(cell)
                output.append("\n")

        if hidden_count > 0:
            output.append(f"+{hidden_count} more", style=self.STORY_HELP)

        self.vars_widget.update(output)

    def _render_code_panel(self) -> None:
        if self.code_widget is None:
            return

        frames = self._line_frames()
        total = len(frames)
        if total == 0:
            hint = Text()
            hint.append("No Test Story frames yet. ", style=self.STORY_HELP)
            hint.append("Recording is on. ", style=self.STORY_META_HIGHLIGHT)
            hint.append("Press R to run and capture a story.", style=f"bold {self.STORY_META_SELECTED}")
            self.code_widget.update(hint)
            return

        self._ensure_selected_frame_index()
        start_index, end_index = self._frame_cards_window(total)
        width = max(8, self.code_widget.size.width - 2)
        renderables = []
        lines_above = max(0, int(global_state.tsv_lines_above))
        lines_below = max(0, int(global_state.tsv_lines_below))

        for index in range(start_index, end_index):
            event = frames[index]
            source_path = os.path.abspath(event.file_path)
            source_lines = self._load_source_lines(source_path)
            if not source_lines:
                continue

            line_number = event.line
            snippet_start = max(1, line_number - lines_above)
            snippet_end = min(len(source_lines), line_number + lines_below)
            selected = index == self.selected_frame_index
            path_text = self._display_path(source_path)

            title = Text()
            if selected:
                title.append(">> ", style=f"bold {self.STORY_META_SELECTED}")
                title.append(path_text, style=f"bold {self.STORY_META_SELECTED}")
            else:
                title.append("   ", style="#7f868d")
                title.append(path_text, style="#95a3aa")
            title.append(":")
            if selected:
                title.append(str(line_number), style=self.STORY_META_HIGHLIGHT)
            else:
                title.append(str(line_number), style="#95a3aa")
            if event.function:
                if selected:
                    title.append(f"  fn={event.function}", style=self.STORY_HELP)
                else:
                    title.append(f"  fn={event.function}", style="#7f868d")

            number_width = len(str(max(1, snippet_end)))
            code_width = max(1, width - (number_width + 5))
            snippet_text = self._build_frame_snippet(
                source_path,
                source_lines,
                line_number,
                snippet_start,
                snippet_end,
                selected,
                code_width,
            )

            renderables.append(title)
            renderables.append(snippet_text)
            if index < end_index - 1:
                sep_style = self.STORY_BAR_BASE if selected else "#3a3f4b"
                renderables.append(Text("-" * width, style=sep_style))

        if not renderables:
            self.code_widget.update(Text("No renderable story frames.", style=self.STORY_HELP))
            return

        self.code_widget.update(Group(*renderables))

    def _refresh_view(self, force: bool = False) -> None:
        signature = self._signature()
        if not force and signature == self.last_signature:
            return

        frames = self._line_frames()
        total = len(frames)
        if total > 0 and is_debug_active(self.test):
            self.selected_frame_index = total - 1
        self._ensure_selected_frame_index()
        window_start, window_end = self._timeline_window(total)
        selected = None
        if 0 <= self.selected_frame_index < total:
            selected = frames[self.selected_frame_index]

        if self.header_widget is not None:
            status = self.test.state.value
            debug_status = "active" if is_debug_active(self.test) else "idle"
            timeline_status = "on" if self.test.timeline_capture_enabled or global_state.timeline_capture_enabled else "off"
            self.header_widget.update(
                Text(
                    f"Test Story: {self.test.name} [{status}]  Debug: {debug_status}  Recording: {timeline_status}  Zoom: {self.zoom_level}x"
                )
            )

        overview_width = 80
        if self.timeline_overview_widget is not None:
            overview_width = max(8, self.timeline_overview_widget.size.width - 2)
            overview = self._build_overview_text(overview_width, window_start, window_end)
            self.timeline_overview_widget.update(overview)

        if self.timeline_detail_widget is not None:
            detail_width = max(8, self.timeline_detail_widget.size.width - 2)
            detail = self._build_detail_text(detail_width, window_start, window_end)
            self.timeline_detail_widget.update(detail)

        if self.timeline_meta_widget is not None:
            meta = Text()
            meta.append(f"Steps {window_start + 1 if total else 0}-{window_end} / {total}    ")
            if selected is not None:
                meta.append(
                    f"selected {self.selected_frame_index + 1}/{total}",
                    style=f"bold {self.STORY_META_SELECTED}",
                )
                meta.append("\n")
                meta.append(
                    f"{self._display_path(selected.file_path)}:{selected.line}",
                    style=self.STORY_META_HIGHLIGHT,
                )
                if selected.function:
                    meta.append(f"  fn={selected.function}", style=self.STORY_META_HIGHLIGHT)
            else:
                meta.append("No line frame selected. Press R to run.", style=self.STORY_HELP)
            meta.append("\n")
            meta.append(
                "Help: click bars or <-/-> or Ctrl+<- / Ctrl+-> to scrub, N/I/O/C/K to step, A auto-trace, R rerun/restart debug, D debug toggle",
                style=self.STORY_HELP,
            )
            self.timeline_meta_widget.update(meta)

        self._render_code_panel()
        self._render_variables_panel(selected)
        self.last_signature = signature
