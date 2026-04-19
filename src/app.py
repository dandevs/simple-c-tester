import asyncio
import os

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog, Static

import state as global_state
from state import state
from models import Test
from render import TestOutputScreen, render_tree, OutputBoxRegion
from runner import (
    generate_makefile,
    state_changed,
    all_tests_finished,
    has_active_tests,
    display_state_signature,
)
from watch import DebounceHandler


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

    def __init__(self, watch: bool, output_max_lines: int, theme_name: str):
        super().__init__()
        self.watch_mode = watch
        self.observer = None
        self.last_signature: tuple | None = None
        self.log_widget: RichLog | None = None
        self.output_max_lines = max(1, output_max_lines)
        self.rendered_output_boxes: list[OutputBoxRegion] = []
        self._pending_makefile_regen = False
        self._last_makefile_columns = 0
        if theme_name == "ansi":
            self.theme = "textual-ansi"

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
            watched_dirs = set()
            tests_dir = os.path.abspath("tests")
            src_dir = os.path.abspath("src")
            watched_dirs.add(tests_dir)
            watched_dirs.add(src_dir)
            for test in state.all_tests:
                for dep in test.dependencies:
                    dep_dir = os.path.dirname(dep)
                    if dep_dir not in watched_dirs:
                        watched_dirs.add(dep_dir)
                for inc_dir in test.include_dirs:
                    abs_inc = os.path.abspath(inc_dir)
                    if abs_inc not in watched_dirs:
                        watched_dirs.add(abs_inc)
            for d in watched_dirs:
                if not os.path.isdir(d):
                    continue
                observer.schedule(handler, d, recursive=True)
            observer.daemon = True
            observer.start()
            self.observer = observer

        self._update_dep_warning()

        state_changed()
        self.set_interval(0.1, self._tick)

    def on_resize(self, event: events.Resize) -> None:
        _ = event
        self._set_subprocess_columns_from_ui()

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
        if len(self.screen_stack) > 1:
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

    def _tick(self) -> None:
        if self._pending_makefile_regen and not has_active_tests():
            generate_makefile()
            self._pending_makefile_regen = False

        if self.watch_mode:
            self._update_dep_warning()

        has_active = has_active_tests()
        signature = display_state_signature()
        if has_active or signature != self.last_signature:
            self._render_tree()
            self.last_signature = signature

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
        self.rendered_output_boxes = render_tree(
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
