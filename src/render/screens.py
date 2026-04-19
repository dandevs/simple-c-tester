import shutil
import subprocess

from rich.text import Text
from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog, Static

from models import Test
from .output import get_test_output
from .styles import TREE_META_STYLE


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
            self.footer_widget.update(Text(self._base_footer_text(), style="ansi_bright_black"))
            return

        style = "ansi_yellow" if warning else "ansi_bright_black"
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
