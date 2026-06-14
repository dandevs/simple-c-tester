import asyncio
import os

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import RichLog, Static

import state as global_state
from state import state
from core.models import Test
from api import TestRunner
from ui.render import TestOutputScreen, TestDebuggerScreen, render_tree, OutputBoxRegion, TestRowRegion
from runner import (
    generate_makefile,
    all_tests_finished,
    has_active_tests,
    refresh_editor_breakpoints_cache,
)
from watch import DebounceHandler


def _ansi_theme() -> Theme:
    """Return an ANSI-terminal-respecting theme.

    Textual >=8.2.5 replaced the monolithic ``textual-ansi`` builtin with
    ``ansi-dark`` and ``ansi-light`` (which properly respect terminal colors
    under the new theme engine).  Older textual has ``textual-ansi`` directly.
    Pick whichever is available in ``BUILTIN_THEMES``.
    """
    for name in ("ansi-dark", "ansi-light", "textual-ansi"):
        if name in BUILTIN_THEMES:
            return BUILTIN_THEMES[name]

    # Belt-and-suspenders: reconstruct the old textual-ansi definition for
    # very old textual releases that have none of the above.
    return Theme(
        name="textual-ansi",
        primary="ansi_blue",
        secondary="ansi_cyan",
        warning="ansi_yellow",
        error="ansi_red",
        success="ansi_green",
        accent="ansi_bright_blue",
        foreground="ansi_default",
        background="ansi_default",
        surface="ansi_default",
        panel="ansi_default",
        boost="ansi_default",
        dark=False,
        luminosity_spread=0.15,
        text_alpha=0.95,
        variables={
            "block-cursor-text-style": "b",
            "block-cursor-blurred-text-style": "i",
            "input-selection-background": "ansi_blue",
            "input-cursor-text-style": "reverse",
            "scrollbar": "ansi_blue",
            "border-blurred": "ansi_blue",
            "border": "ansi_bright_blue",
        },
    )


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
    #controls-footer {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: transparent;
        color: ansi_bright_black;
    }
    #dep-warning {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: transparent;
        color: ansi_yellow;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Exit", priority=True),
    ]

    def __init__(
        self,
        runner: TestRunner,
        watch: bool,
        output_max_lines: int,
        theme_name: str,
        timeline_enabled: bool = False,
    ):
        super().__init__()
        self.runner = runner
        self.watch_mode = watch
        self.timeline_enabled = timeline_enabled
        self.observer = None
        self.log_widget: RichLog | None = None
        self.output_max_lines = max(1, output_max_lines)
        self.rendered_output_boxes: list[OutputBoxRegion] = []
        self.rendered_test_rows: list[TestRowRegion] = []
        self._watch_flush_task: asyncio.Task | None = None
        self._pending_makefile_regen = False
        self._last_makefile_columns = 0
        self._dirty = False  # set by event subscribers, consumed by _paint_tick
        if theme_name == "ansi":
            theme = _ansi_theme()
            self.register_theme(theme)
            self.theme = theme.name

    def compose(self) -> ComposeResult:
        yield RichLog(
            id="tree-view",
            wrap=False,
            markup=False,
            highlight=False,
            auto_scroll=False,
        )
        if self.watch_mode:
            yield Static("", id="dep-warning")
            yield Static("Tests  |  Ctrl+C: Exit", id="controls-footer")

    async def on_mount(self) -> None:
        self.log_widget = self.query_one("#tree-view", RichLog)
        self._set_subprocess_columns_from_ui()
        self._render_tree()

        if self.watch_mode:
            from watchdog.observers import Observer

            loop = asyncio.get_running_loop()
            handler = DebounceHandler(loop)
            observer = Observer()
            observer.schedule(handler, os.path.abspath("."), recursive=True)
            observer.daemon = True
            observer.start()
            self.observer = observer

        self._update_dep_warning()

        # Event-driven rendering: subscribe to the runner's event bus and mark
        # the view dirty on any relevant change.  The paint tick below only
        # re-renders when dirty — no more state-signature polling.
        self.runner.events.subscribe("test_state_changed", self._on_engine_event)
        self.runner.events.subscribe("test_finished", self._on_engine_event)
        self.runner.events.subscribe("suite_changed", self._on_engine_event)
        self.runner.events.subscribe("test_output_updated", self._on_engine_event)
        self.runner.events.subscribe("timeline_updated", self._on_engine_event)
        self.runner.events.subscribe("dep_graph_status", self._on_engine_event)

        # Start the runner's background emitter (polls test states, emits
        # events) and dispatch the initial run via the public API.
        self.runner.start_emitter()
        self.runner.schedule_run()

        # Paint + maintenance loop.  This is NOT state polling — it only
        # flushes the dirty flag set by events, plus periodic housekeeping
        # (breakpoint cache, watch flush, makefile regen, completion check).
        self.set_interval(0.1, self._paint_tick)

    def _on_engine_event(self, _event) -> None:
        """Mark the tree dirty so the next paint re-renders."""
        self._dirty = True

    def on_resize(self, event: events.Resize) -> None:
        _ = event
        self._set_subprocess_columns_from_ui()

    async def action_quit(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        self.runner.stop_emitter()
        self.exit()

    def _find_output_box_at(self, x: int, y: int) -> OutputBoxRegion | None:
        for box in self.rendered_output_boxes:
            if (
                box.start_line <= y <= box.end_line
                and box.left_col <= x <= box.right_col
            ):
                return box
        return None

    def _find_test_row_at(self, x: int, y: int) -> TestRowRegion | None:
        for row in self.rendered_test_rows:
            if row.line == y and row.left_col <= x <= row.right_col:
                return row
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

    def _get_mouse_row_key(self, event: events.MouseEvent) -> str | None:
        if self.log_widget is None:
            return None
        offset = event.get_content_offset(self.log_widget)
        if offset is None:
            return None

        virtual_x = int(offset.x + self.log_widget.scroll_x)
        virtual_y = int(offset.y + self.log_widget.scroll_y)
        row = self._find_test_row_at(virtual_x, virtual_y)
        return row.test_key if row is not None else None

    def _get_test_by_key(self, test_key: str) -> Test | None:
        for test in state.all_tests:
            if test.source_path == test_key:
                return test
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if len(self.screen_stack) > 1:
            return

        row_key = self._get_mouse_row_key(event)
        if row_key is not None:
            row_test = self._get_test_by_key(row_key)
            if row_test is not None:
                self.push_screen(TestDebuggerScreen(row_test))
                event.prevent_default()
                event.stop()
                return

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

    def _paint_tick(self) -> None:
        """Housekeeping + dirty-flag paint loop.

        Replaces the old polling tick: rendering is now triggered by events
        (``_on_engine_event`` sets ``_dirty``); this method only flushes that
        flag plus runs periodic maintenance (breakpoint cache, watch flush,
        makefile regen, completion check).
        """
        refresh_editor_breakpoints_cache()

        if self._pending_makefile_regen and not has_active_tests():
            generate_makefile()
            self._pending_makefile_regen = False

        if self.watch_mode:
            self._update_dep_warning()
            self._flush_deferred_watch_changes()

        # Event-driven re-render: paint only when an event marked us dirty,
        # or while tests are actively running (so elapsed-time counters update).
        if self._dirty or has_active_tests():
            self._render_tree()
            self._dirty = False

        if not self.watch_mode and all_tests_finished():
            self.exit()

    def _render_tree(self) -> None:
        if self.log_widget is None:
            return

        log = self.log_widget
        previous_scroll_x = log.scroll_x
        previous_scroll_y = log.scroll_y
        near_bottom = (log.max_scroll_y - log.scroll_y) <= 1

        log.clear()
        self.rendered_output_boxes, self.rendered_test_rows = render_tree(
            log, self.output_max_lines, max(20, log.size.width or self.size.width or 80)
        )

        if near_bottom:
            log.scroll_end(animate=False, immediate=True)
        else:
            log.scroll_to(
                x=previous_scroll_x,
                y=previous_scroll_y,
                animate=False,
                immediate=True,
            )

    def _update_dep_warning(self) -> None:
        if not self.watch_mode:
            return

        warning = self.query_one("#dep-warning", Static)
        if global_state.dep_graph_ready:
            warning.update("")
            return

        warning.update(
            "Dependency graph incomplete (fresh build or compile errors). Run one clean pass for precise selective reruns."
        )

    def _flush_deferred_watch_changes(self) -> None:
        if global_state.active_debug_test_key is not None:
            return
        if self._watch_flush_task is not None and not self._watch_flush_task.done():
            return

        from watch import flush_deferred_changes

        self._watch_flush_task = asyncio.create_task(flush_deferred_changes())

    def _set_subprocess_columns_from_ui(self) -> None:
        width = 80
        if self.log_widget is not None:
            width = self.log_widget.size.width or self.size.width or 80
        else:
            width = self.size.width or 80

        width = max(20, width)
        global_state.subprocess_columns = width
        if width != self._last_makefile_columns:
            self._last_makefile_columns = width
            self._pending_makefile_regen = True
