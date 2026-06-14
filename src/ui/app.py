import asyncio
import os

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import Input, RichLog, Static

import state as global_state
from state import state
from core.models import Test, Suite, TestState
from api import TestRunner
from ui.render import (
    TestOutputScreen,
    TestDebuggerScreen,
    render_tree,
    OutputBoxRegion,
    TestRowRegion,
    SuiteRowRegion,
)
from ui.render.styles import (
    STATUS_BASE_STYLE,
    STATUS_FAIL_STYLE,
    STATUS_PASS_STYLE,
    STATUS_PENDING_STYLE,
    STATUS_RUN_STYLE,
    SEPARATOR_STYLE,
)
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


# ---------------------------------------------------------------------------
# Pure helpers (suite-key construction mirrors tree.py)
# ---------------------------------------------------------------------------


def _suite_key(parent_key: str, suite_name: str) -> str:
    return f"{parent_key}/{suite_name}" if parent_key else suite_name


def _collect_all_suite_keys(suite: Suite, parent_key: str = "") -> set[str]:
    """Return keys for every child suite (recursively) under ``suite``."""
    keys: set[str] = set()
    for child in suite.children:
        key = _suite_key(parent_key, child.name)
        keys.add(key)
        keys.update(_collect_all_suite_keys(child, key))
    return keys


def _build_status_line() -> Text:
    """Build the one-line status summary from current test states."""
    tests = state.all_tests
    total = len(tests)
    passed = sum(1 for t in tests if t.state == TestState.PASSED)
    failed = sum(1 for t in tests if t.state == TestState.FAILED)
    running = sum(1 for t in tests if t.state == TestState.RUNNING)
    pending = sum(1 for t in tests if t.state == TestState.PENDING)

    text = Text()
    text.append(" C Tester ", style="bold reverse")
    text.append(f"  {total} tests", style=STATUS_BASE_STYLE)

    has_counts = passed or failed or running or pending
    if has_counts:
        text.append("  \u2502", style=SEPARATOR_STYLE)

    if passed:
        text.append(f"  \u2713 {passed}", style=STATUS_PASS_STYLE)
    if failed:
        text.append(f"  \u2717 {failed}", style=STATUS_FAIL_STYLE)
    if running:
        text.append(f"  \u21bb {running}", style=STATUS_RUN_STYLE)
    if pending:
        text.append(f"  \u22ef {pending}", style=STATUS_PENDING_STYLE)

    width = getattr(global_state, "subprocess_columns", 0) or 80
    pad = max(0, width - len(text.plain))
    if pad > 0:
        text.append(" " * pad, style="reverse")

    return text


def _footer_text(watch_mode: bool) -> Text:
    sep = Text(" \u2502 ", style=SEPARATOR_STYLE)

    text = Text()
    text.append("/", style="bold")
    text.append(" Search", style="dim")
    text.append(sep)
    text.append("F", style="bold")
    text.append(" Fold", style="dim")
    text.append(sep)
    text.append("U", style="bold")
    text.append(" Unfold", style="dim")
    text.append(sep)
    text.append("Enter", style="bold")
    text.append(" Story", style="dim")
    text.append(sep)
    text.append("Click", style="bold")
    text.append(" Output", style="dim")
    if watch_mode:
        text.append(sep)
        text.append("Auto-rerun", style="dim")
    text.append(sep)
    text.append("Ctrl+C", style="bold")
    text.append(" Exit", style="dim")
    return text


class TestRunnerApp(App[None]):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #status-header {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: transparent;
    }
    #search-input {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
        background: transparent;
        color: ansi_default;
    }
    #search-input:focus {
        border: none;
        background: transparent;
    }
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
    #dep-warning {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: transparent;
        color: ansi_yellow;
    }
    #controls-footer {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: transparent;
        color: ansi_white;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Exit", priority=True),
        Binding("/", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear"),
        Binding("f", "fold_all", "Fold All"),
        Binding("u", "unfold_all", "Unfold All"),
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
        self.search_widget: Input | None = None
        self.status_widget: Static | None = None
        self.footer_widget: Static | None = None
        self.output_max_lines = max(1, output_max_lines)
        self.rendered_output_boxes: list[OutputBoxRegion] = []
        self.rendered_test_rows: list[TestRowRegion] = []
        self.rendered_suite_rows: list[SuiteRowRegion] = []
        self._watch_flush_task: asyncio.Task | None = None
        self._pending_makefile_regen = False
        self._last_makefile_columns = 0
        self._dirty = False
        self.collapsed_suites: set[str] = set()
        self.search_query: str = ""
        self._last_visible_keys: set[str] = set()
        self._prioritize_in_progress = False
        if theme_name == "ansi":
            theme = _ansi_theme()
            self.register_theme(theme)
            self.theme = theme.name

    def compose(self) -> ComposeResult:
        yield Static(_build_status_line(), id="status-header", markup=False)
        yield Input(placeholder="/ to search tests \u2014 type to filter", id="search-input")
        if self.watch_mode:
            yield Static("", id="dep-warning")
        yield RichLog(
            id="tree-view",
            wrap=False,
            markup=False,
            highlight=False,
            auto_scroll=False,
        )
        yield Static(_footer_text(self.watch_mode), id="controls-footer", markup=False)

    async def on_mount(self) -> None:
        self.status_widget = self.query_one("#status-header", Static)
        self.search_widget = self.query_one("#search-input", Input)
        self.log_widget = self.query_one("#tree-view", RichLog)
        self.footer_widget = self.query_one("#controls-footer", Static)

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

        # Event-driven rendering: subscribe to the runner's event bus.
        self.runner.events.subscribe("test_state_changed", self._on_engine_event)
        self.runner.events.subscribe("test_finished", self._on_engine_event)
        self.runner.events.subscribe("suite_changed", self._on_engine_event)
        self.runner.events.subscribe("test_output_updated", self._on_engine_event)
        self.runner.events.subscribe("timeline_updated", self._on_engine_event)
        self.runner.events.subscribe("dep_graph_status", self._on_engine_event)

        # Start the runner's background emitter and dispatch initial run.
        self.runner.start_emitter()
        self.runner.schedule_run()

        # Paint + maintenance loop.
        self.set_interval(0.1, self._paint_tick)

    # ----- Input / search -----------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self.search_query = event.value
            self._dirty = True

    def _on_main_screen(self) -> bool:
        """True when no sub-screen (story view, output, modal) is active."""
        return len(self.screen_stack) <= 1

    def action_focus_search(self) -> None:
        if not self._on_main_screen():
            return
        if self.search_widget is not None:
            self.search_widget.focus()

    def action_clear_search(self) -> None:
        if not self._on_main_screen():
            return
        if self.search_widget is None:
            return
        if self.search_widget.value:
            self.search_widget.value = ""
            self.search_query = ""
            self._dirty = True
        else:
            self.search_widget.blur()

    # ----- Fold / unfold ------------------------------------------------

    def action_fold_all(self) -> None:
        if not self._on_main_screen():
            return
        self.collapsed_suites = _collect_all_suite_keys(state.root_suite)
        self._dirty = True

    def action_unfold_all(self) -> None:
        if not self._on_main_screen():
            return
        self.collapsed_suites.clear()
        self._dirty = True

    def _toggle_suite_fold(self, suite_key: str) -> None:
        if suite_key in self.collapsed_suites:
            self.collapsed_suites.discard(suite_key)
        else:
            self.collapsed_suites.add(suite_key)
        self._dirty = True

    # ----- Engine event handling ----------------------------------------

    def _on_engine_event(self, _event) -> None:
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

    # ----- Mouse → region mapping ---------------------------------------

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

    def _find_suite_row_at(self, x: int, y: int) -> SuiteRowRegion | None:
        for row in self.rendered_suite_rows:
            if row.line == y and row.left_col <= x <= row.right_col:
                return row
        return None

    def _virtual_coords(self, event: events.MouseEvent) -> tuple[int, int] | None:
        """Map a mouse event to (virtual_x, virtual_y) within the RichLog."""
        if self.log_widget is None:
            return None
        offset = event.get_content_offset(self.log_widget)
        if offset is None:
            return None
        vx = int(offset.x + self.log_widget.scroll_x)
        vy = int(offset.y + self.log_widget.scroll_y)
        return vx, vy

    def _get_test_by_key(self, test_key: str) -> Test | None:
        for test in state.all_tests:
            if test.source_path == test_key:
                return test
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if len(self.screen_stack) > 1:
            return

        coords = self._virtual_coords(event)
        if coords is None:
            return
        vx, vy = coords

        # Suite row → toggle fold.
        suite_row = self._find_suite_row_at(vx, vy)
        if suite_row is not None:
            self._toggle_suite_fold(suite_row.suite_key)
            event.prevent_default()
            event.stop()
            return

        # Test row → open story view.
        row_key = self._get_mouse_row_key(event)
        if row_key is not None:
            row_test = self._get_test_by_key(row_key)
            if row_test is not None:
                self.push_screen(TestDebuggerScreen(row_test))
                event.prevent_default()
                event.stop()
                return

        # Output box → open output view.
        box_key = self._get_mouse_box_key(event)
        if box_key is None:
            return
        test = self._get_test_by_key(box_key)
        if test is None:
            return

        self.push_screen(TestOutputScreen(test))
        event.prevent_default()
        event.stop()

    def _get_mouse_box_key(self, event: events.MouseEvent) -> str | None:
        coords = self._virtual_coords(event)
        if coords is None:
            return None
        vx, vy = coords
        box = self._find_output_box_at(vx, vy)
        return box.test_key if box is not None else None

    def _get_mouse_row_key(self, event: events.MouseEvent) -> str | None:
        coords = self._virtual_coords(event)
        if coords is None:
            return None
        vx, vy = coords
        row = self._find_test_row_at(vx, vy)
        return row.test_key if row is not None else None

    # ----- Lifecycle helpers --------------------------------------------

    def stop_observer(self) -> None:
        if self.observer is None:
            return
        self.observer.stop()
        self.observer.join()
        self.observer = None

    # ----- Visibility-priority scheduling --------------------------------

    def _compute_visible_keys(self) -> set[str]:
        """Return the set of test source-paths currently visible in the viewport."""
        if self.log_widget is None:
            return set()
        top = self.log_widget.scroll_y
        height = self.log_widget.size.height or 0
        bottom = top + height
        return {
            row.test_key
            for row in self.rendered_test_rows
            if top <= row.line < bottom
        }

    def _maybe_prioritize(self) -> None:
        """If the visible test set changed, ask the runner to prioritize them."""
        if self._prioritize_in_progress:
            return
        visible = self._compute_visible_keys()
        if visible == self._last_visible_keys:
            return
        self._last_visible_keys = visible
        if not visible:
            return
        self._prioritize_in_progress = True
        asyncio.ensure_future(self._run_prioritize(visible))

    async def _run_prioritize(self, visible: set[str]) -> None:
        try:
            await self.runner.prioritize(visible)
        except Exception:
            pass  # best-effort; never crash the UI over scheduling
        finally:
            self._prioritize_in_progress = False

    # ----- Paint loop ---------------------------------------------------

    def _paint_tick(self) -> None:
        """Housekeeping + dirty-flag paint loop."""
        refresh_editor_breakpoints_cache()

        if self._pending_makefile_regen and not has_active_tests():
            generate_makefile()
            self._pending_makefile_regen = False

        if self.watch_mode:
            self._update_dep_warning()
            self._flush_deferred_watch_changes()

        # Status header always refreshes (cheap) so counters stay live.
        if self.status_widget is not None:
            self.status_widget.update(_build_status_line())

        if self._dirty or has_active_tests():
            self._render_tree()
            self._dirty = False

        # Visibility-priority: preempt non-visible running tests so visible
        # pending tests get slots.  Only fires when the visible set changes.
        self._maybe_prioritize()

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
        self.rendered_output_boxes, self.rendered_test_rows, self.rendered_suite_rows = (
            render_tree(
                log,
                self.output_max_lines,
                max(20, log.size.width or self.size.width or 80),
                collapsed_suites=self.collapsed_suites,
                search_query=self.search_query,
            )
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
            "Dependency graph incomplete (fresh build or compile errors)."
            " Run one clean pass for precise selective reruns."
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
