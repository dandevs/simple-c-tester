from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog

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

        output_lines = get_test_output(self.test)
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
