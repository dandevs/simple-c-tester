import asyncio
import os
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import Input, RichLog, Static

import state as global_state
from state import state
from core.models import Test, Suite, TestState, TestRun
from api import TestRunner
from ui.render import (
    TestOutputScreen,
    TestDebuggerScreen,
    OptionsScreen,
    render_tree,
    OutputBoxRegion,
    TestRowRegion,
    SuiteRowRegion,
)
from core.userconfig import save_user_config
from ui.render.styles import (
    STATUS_BASE_STYLE,
    STATUS_FAIL_STYLE,
    STATUS_PASS_STYLE,
    STATUS_PENDING_STYLE,
    STATUS_RUN_STYLE,
    SEPARATOR_STYLE,
    MUTED_STYLE,
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


def _build_status_line(
    run_complete: bool = False,
    total_elapsed: float = 0.0,
    running_names: list[str] | None = None,
) -> Text:
    """Build the one-line status summary from current test states."""
    tests = state.all_tests
    total = len(tests)
    passed = sum(1 for t in tests if t.state == TestState.PASSED)
    failed = sum(1 for t in tests if t.state == TestState.FAILED)
    running = sum(1 for t in tests if t.state == TestState.RUNNING)
    pending = sum(1 for t in tests if t.state == TestState.PENDING)
    cancelled = sum(1 for t in tests if t.state == TestState.CANCELLED)

    text = Text()
    text.append("C Tester", style="bold")
    text.append(f"  {total} tests", style="bold default")

    has_counts = passed or failed or running or pending
    if has_counts:
        text.append("  ", style=SEPARATOR_STYLE)
        text.append("\u2502", style=SEPARATOR_STYLE)
        text.append("  ", style=SEPARATOR_STYLE)

    if passed:
        text.append(f"\u2713 {passed}", style=STATUS_PASS_STYLE)
    if failed:
        text.append(f"  \u2717 {failed}", style=STATUS_FAIL_STYLE)
    if running:
        text.append(f"  \u21bb {running}", style=STATUS_RUN_STYLE)
    if pending:
        text.append(f"  \u22ef {pending}", style=STATUS_PENDING_STYLE)

    if run_complete:
        text.append("  ", style=SEPARATOR_STYLE)
        text.append("\u2502", style=SEPARATOR_STYLE)
        text.append("  ", style=SEPARATOR_STYLE)
        if failed:
            text.append(f"DONE \u2717 {failed} failed", style=STATUS_FAIL_STYLE)
        else:
            text.append("DONE \u2713 all passed", style=STATUS_PASS_STYLE)
        text.append(f"  {total_elapsed:.1f}s", style=MUTED_STYLE)
    else:
        # Live progress feedback while a run is in flight.
        # Show completion ratio, currently-running test names, and a
        # ticking elapsed timer.  Only emit once the run has started
        # (some test has left PENDING) so the initial mount stays clean.
        completed = passed + failed + cancelled
        run_active = bool(running or passed or failed)
        if run_active and total:
            text.append("  ", style=SEPARATOR_STYLE)
            text.append("\u2502", style=SEPARATOR_STYLE)
            text.append("  ", style=SEPARATOR_STYLE)
            text.append(f"{completed}/{total}", style=MUTED_STYLE)

            names = list(running_names or [])
            if names:
                text.append("  ", style=SEPARATOR_STYLE)
                text.append("\u2502", style=SEPARATOR_STYLE)
                text.append("  ", style=SEPARATOR_STYLE)
                text.append("running:", style=MUTED_STYLE)
                text.append(" ", style=MUTED_STYLE)
                shown = names[:2]
                text.append(", ".join(shown), style=STATUS_RUN_STYLE)
                extra = len(names) - len(shown)
                if extra > 0:
                    text.append(f" +{extra} more", style=MUTED_STYLE)

            if total_elapsed > 0:
                text.append(f"  {total_elapsed:.1f}s", style=MUTED_STYLE)

    return text


def _footer_text(watch_mode: bool) -> Text:
    sep = Text(" \u2502 ", style=SEPARATOR_STYLE)

    text = Text()
    text.append("\u2191\u2193", style="bold")
    text.append(" Nav", style=MUTED_STYLE)
    text.append(sep)
    text.append("/", style="bold")
    text.append(" Search", style=MUTED_STYLE)
    text.append(sep)
    text.append("Enter", style="bold")
    text.append(" Story", style=MUTED_STYLE)
    text.append(sep)
    text.append("o", style="bold")
    text.append(" Options", style=MUTED_STYLE)
    text.append(sep)
    text.append("O", style="bold")
    text.append(" Output", style=MUTED_STYLE)
    text.append(sep)
    text.append("r", style="bold")
    text.append(" Rerun", style=MUTED_STYLE)
    text.append(sep)
    text.append("R", style="bold")
    text.append(" Rerun All", style=MUTED_STYLE)
    if not watch_mode:
        text.append(sep)
        text.append("n/p", style="bold")
        text.append(" Failures", style=MUTED_STYLE)
    text.append(sep)
    text.append("Q", style="bold")
    text.append(" Quit", style=MUTED_STYLE)
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
        Binding("q", "quit", "Quit"),
        Binding("up", "nav_up", "Prev"),
        Binding("down", "nav_down", "Next"),
        Binding("k", "nav_up", "Prev"),
        Binding("j", "nav_down", "Next"),
        Binding("enter", "open_story_selected", "Story"),
        Binding("o", "open_options", "Options"),
        Binding("O", "open_output_selected", "Output"),
        Binding("r", "rerun_selected", "Rerun"),
        Binding("R", "rerun_all", "Rerun All"),
        Binding("n", "next_failure", "Next Fail"),
        Binding("p", "prev_failure", "Prev Fail"),
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
        user_config: dict | None = None,
        cli_overrides: set[str] | None = None,
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
        self._last_tree_render = 0.0  # monotonic timestamp of last full render
        self._makefile_regen_in_progress = False
        self.collapsed_suites: set[str] = set()
        self.search_query: str = ""
        self._last_visible_keys: set[str] = set()
        self._last_search_active: bool = False
        self._prioritize_in_progress = False
        # Keyboard cursor navigation
        self.selected_test_key: str | None = None
        # Run lifecycle tracking
        self._run_complete: bool = False
        self._run_start_time: float = 0.0
        self._run_end_time: float = 0.0
        # Persisted user config (mutable in-memory mirror of the on-disk file).
        # The Options screen edits this; apply_option persists on each change.
        self.user_config: dict = dict(user_config or {})
        self.cli_overrides: set[str] = set(cli_overrides or ())
        self._apply_theme(theme_name)

    def _apply_theme(self, theme_name: str) -> None:
        """Register + select the ANSI theme when requested; otherwise default."""
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
        self._init_selection()
        self._dirty = True  # force re-render with selection highlight

        # The search Input is the first focusable widget and would otherwise
        # auto-focus on mount, swallowing letter keybindings (o, r, n, p, ...).
        # Blur it after the layout pass so those bindings work from the tree
        # view; users press "/" to focus search explicitly.
        def _release_initial_focus() -> None:
            if self.search_widget is not None and self.search_widget.has_focus:
                self.search_widget.blur()

        self.call_after_refresh(_release_initial_focus)

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

    # ----- Keyboard navigation ------------------------------------------

    def _visible_test_keys(self) -> list[str]:
        """Return test source-paths in tree order (from the last render)."""
        return [r.test_key for r in self.rendered_test_rows]

    def _init_selection(self) -> None:
        """Select the first visible test if nothing is selected, or fix the
        selection if the currently selected test was filtered out by search."""
        keys = self._visible_test_keys()
        if not keys:
            return
        if self.selected_test_key is None or self.selected_test_key not in keys:
            self.selected_test_key = keys[0]

    def action_nav_up(self) -> None:
        if not self._on_main_screen():
            return
        keys = self._visible_test_keys()
        if not keys:
            return
        if self.selected_test_key is None or self.selected_test_key not in keys:
            self.selected_test_key = keys[-1]
        else:
            idx = keys.index(self.selected_test_key)
            self.selected_test_key = keys[max(0, idx - 1)]
        self._dirty = True

    def action_nav_down(self) -> None:
        if not self._on_main_screen():
            return
        keys = self._visible_test_keys()
        if not keys:
            return
        if self.selected_test_key is None or self.selected_test_key not in keys:
            self.selected_test_key = keys[0]
        else:
            idx = keys.index(self.selected_test_key)
            self.selected_test_key = keys[min(len(keys) - 1, idx + 1)]
        self._dirty = True

    def action_open_story_selected(self) -> None:
        if not self._on_main_screen() or self.selected_test_key is None:
            return
        test = self._get_test_by_key(self.selected_test_key)
        if test is not None:
            self.push_screen(TestDebuggerScreen(test))

    def action_open_output_selected(self) -> None:
        if not self._on_main_screen() or self.selected_test_key is None:
            return
        test = self._get_test_by_key(self.selected_test_key)
        if test is not None:
            self.push_screen(TestOutputScreen(test))

    def action_open_options(self) -> None:
        if not self._on_main_screen():
            return
        self.push_screen(
            OptionsScreen(
                values=dict(self.user_config),
                cli_overrides=set(self.cli_overrides),
                on_change=self.apply_option,
            )
        )

    # ----- Options screen live-application -----------------------------
    # Invoked by OptionsScreen on every change.  Applies the new value to the
    # live engine/UI state immediately and persists it to the user config file.
    def apply_option(self, key: str, value) -> None:
        import state as gs

        gs_map = {
            "tsv_lines_above": "tsv_lines_above",
            "tsv_lines_below": "tsv_lines_below",
            "tsv_skip_seq_lines": "tsv_skip_seq_lines",
            "tsv_vars_depth": "tsv_vars_depth",
            "tsv_variables_height": "tsv_variables_height",
            "tsv_show_reason_about": "tsv_show_reason_about",
            "timeline": "timeline_capture_enabled",
        }
        if key in gs_map:
            setattr(gs, gs_map[key], value)
        elif key == "parallel":
            running = sum(
                1 for t in state.all_tests if t.state == TestState.RUNNING
            )
            state.available_runners = max(0, int(value) - running)
        elif key == "output_lines":
            self.output_max_lines = max(1, int(value))
            self._dirty = True
        elif key == "theme":
            self._apply_theme("ansi" if value == "ansi" else "default")
        elif key == "story_filter_profile":
            gs.story_filter_profile_preference = value
            for t in state.all_tests:
                t.story_filter_profile = value
        elif key == "debug_precision_mode":
            gs.debug_precision_mode_preference = value
            for t in state.all_tests:
                t.debug_precision_mode = value

        # Persist (best-effort; read-only HOME never crashes the TUI).
        self.user_config[key] = value
        try:
            save_user_config({key: value})
        except Exception:
            pass

    def action_next_failure(self) -> None:
        if not self._on_main_screen():
            return
        self._jump_failure(forward=True)

    def action_prev_failure(self) -> None:
        if not self._on_main_screen():
            return
        self._jump_failure(forward=False)

    def _jump_failure(self, forward: bool) -> None:
        keys = self._visible_test_keys()
        if not keys:
            return
        failed = []
        for k in keys:
            t = self._get_test_by_key(k)
            if t is not None and t.state == TestState.FAILED:
                failed.append(k)
        if not failed:
            return
        if self.selected_test_key is None or self.selected_test_key not in keys:
            self.selected_test_key = failed[0]
            self._dirty = True
            return
        idx = keys.index(self.selected_test_key)
        if forward:
            candidates = [k for k in failed if keys.index(k) > idx]
        else:
            candidates = [k for k in reversed(failed) if keys.index(k) < idx]
        if candidates:
            self.selected_test_key = candidates[0]
            self._dirty = True

    # ----- Run controls ------------------------------------------------

    def action_rerun_selected(self) -> None:
        if not self._on_main_screen() or self.selected_test_key is None:
            return
        test = self._get_test_by_key(self.selected_test_key)
        if test is None:
            return
        self._reset_test_for_rerun(test)
        self._dirty = True

    def action_rerun_all(self) -> None:
        if not self._on_main_screen():
            return
        for test in state.all_tests:
            self._reset_test_for_rerun(test)
        self._dirty = True

    def _reset_test_for_rerun(self, test: Test) -> None:
        """Reset a test to PENDING so the scheduler picks it up again."""
        test.state = TestState.PENDING
        test.current_run = TestRun()
        test.time_state_changed = time.monotonic()
        self._run_complete = False
        self._run_start_time = 0.0
        self.runner.schedule_run()

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
        """If the visible test set or search state changed, ask the runner to prioritize them."""
        if self._prioritize_in_progress:
            return
        visible = self._compute_visible_keys()
        search_active = bool(self.search_query)
        if visible == self._last_visible_keys and search_active == self._last_search_active:
            return
        self._last_visible_keys = visible
        self._last_search_active = search_active
        if not visible:
            return
        self._prioritize_in_progress = True
        asyncio.ensure_future(self._run_prioritize(visible, search_active))

    async def _run_prioritize(self, visible: set[str], search_active: bool = False) -> None:
        try:
            await self.runner.prioritize(visible, search_active=search_active)
        except Exception:
            pass  # best-effort; never crash the UI over scheduling
        finally:
            self._prioritize_in_progress = False

    # ----- Paint loop ---------------------------------------------------

    # Cosmetic-only renders (spinner animation, elapsed-time counter) are
    # throttled to every 300ms.  Real state-change events set _dirty and
    # render immediately.  This cuts render frequency ~70% during long
    # compiles where nothing but the spinner changes.
    COSMETIC_RENDER_INTERVAL = 0.3

    def _paint_tick(self) -> None:
        """Housekeeping + dirty-flag paint loop."""
        refresh_editor_breakpoints_cache()

        if (
            self._pending_makefile_regen
            and not has_active_tests()
            and not self._makefile_regen_in_progress
        ):
            self._pending_makefile_regen = False
            self._makefile_regen_in_progress = True
            asyncio.ensure_future(self._regen_makefile())

        if self.watch_mode:
            self._update_dep_warning()
            self._flush_deferred_watch_changes()

        # Track run start (first time we see running tests)
        if not self._run_complete and self._run_start_time == 0.0:
            if any(t.state == TestState.RUNNING for t in state.all_tests):
                self._run_start_time = time.monotonic()

        # Detect run completion (all modes — watch mode resets via
        # _reset_test_for_rerun when a new run is triggered).
        if not self._run_complete:
            if all_tests_finished() and state.all_tests:
                self._run_complete = True
                self._run_end_time = time.monotonic()

        # Status header always refreshes (cheap) so counters stay live.
        if self.status_widget is not None:
            elapsed = 0.0
            if self._run_complete:
                elapsed = self._run_end_time - self._run_start_time
            elif self._run_start_time > 0:
                elapsed = time.monotonic() - self._run_start_time
            running_names = [
                t.name for t in state.all_tests if t.state == TestState.RUNNING
            ]
            self.status_widget.update(
                _build_status_line(
                    run_complete=self._run_complete,
                    total_elapsed=elapsed,
                    running_names=running_names,
                )
            )

        now = time.monotonic()
        if self._dirty:
            self._init_selection()
            self._render_tree()
            self._dirty = False
            self._last_tree_render = now
            self._ensure_selected_visible()
        elif has_active_tests() and (
            now - self._last_tree_render
        ) >= self.COSMETIC_RENDER_INTERVAL:
            self._render_tree()
            self._last_tree_render = now

        # Visibility-priority: preempt non-visible running tests so visible
        # pending tests get slots.  Only fires when the visible set changes.
        self._maybe_prioritize()

    async def _regen_makefile(self) -> None:
        """Regenerate the Makefile in a background thread.

        ``generate_makefile()`` calls ``resolve_include_dirs()`` which
        invokes ``gcc -E`` via ``subprocess.run`` (synchronous).  Running
        it on the event loop blocks all input/widget updates for ~80ms+
        per pass.  Offloading to a thread keeps the UI responsive.
        """
        try:
            await asyncio.to_thread(generate_makefile)
        except Exception:
            pass  # best-effort; never crash the UI over makefile regen
        finally:
            self._makefile_regen_in_progress = False

    def _render_tree(self) -> None:
        if self.log_widget is None:
            return

        # Empty state: no tests discovered.
        if not state.all_tests:
            text = Text()
            text.append("\n  No tests found.\n\n", style="bold yellow")
            text.append("  Create a test file:\n", style="default")
            text.append("    ctester new my_test\n\n", style="cyan")
            text.append(
                "  Or manually add a .c file to tests/:\n", style="bright_black"
            )
            text.append('    #include "ctest.h"\n', style="bright_black")
            text.append("    int main(void) {\n", style="bright_black")
            text.append("      ASSERT_EQ(1 + 1, 2);\n", style="bright_black")
            text.append("      return 0;\n", style="bright_black")
            text.append("    }\n", style="bright_black")
            self.log_widget.clear()
            self.log_widget.write(text)
            self.rendered_output_boxes = []
            self.rendered_test_rows = []
            self.rendered_suite_rows = []
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
                selected_test_key=self.selected_test_key,
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

    def _ensure_selected_visible(self) -> None:
        """Scroll the tree so the selected test row is in view."""
        if self.log_widget is None or self.selected_test_key is None:
            return
        for row in self.rendered_test_rows:
            if row.test_key != self.selected_test_key:
                continue
            top = self.log_widget.scroll_y
            height = self.log_widget.size.height or 1
            bottom = top + height
            if row.line < top or row.line >= bottom:
                target = max(0, row.line - height // 3)
                self.log_widget.scroll_to(
                    y=target, animate=False, immediate=True
                )
            return

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
