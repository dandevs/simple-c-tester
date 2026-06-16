import os
import asyncio
import time
from rich.console import Console, Group
from rich.text import Text
from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.screen import Screen
from textual.widgets import Button, Static, Tree as TextualTree

import state as global_state
from state import state
from core.models import Test, TestState, TestRun
from runner import (
    start_debug_session,
    stop_debug_session,
    debug_step_next,
    debug_step_in,
    debug_step_out,
    debug_continue,
    debug_interrupt,
    debug_interrupt_nowait,
    is_debug_active,
    get_debug_session,
    cancel_test_and_restore_normal_build,
    state_changed,
    save_story_annotations,
    save_debug_line,
    clear_debug_line,
    _schedule_story_annotations_persist,
    cancel_pending_story_annotations_persist,
)
from core.userconfig import save_user_config
from api._variable_tree import (
    build_variable_tree,
    build_array_subtree,
    build_tree_from_captured,
)
from runner.story_filters import normalized_story_filter_profile
from runner.story_annotations import invalidate_story_annotation_cache
from .clipboard import copy_to_clipboard
from .labels import test_elapsed_seconds
from .variable_tree_screen import VariableTreeScreen
from .test_debugger_screen_utils import (
    display_path,
    detect_language,
    load_source_lines,
    event_has_useful_source_line,
    ensure_selected_frame_index,
    compute_frame_cards_window,
    build_frame_snippet,
    build_variables_tree,
    render_code_panel,
    render_full_file_panel,
    STORY_META_HIGHLIGHT,
    STORY_META_SELECTED,
    STORY_HELP,
    STORY_CODE_BG,
    STORY_CURRENT_LINE,
    STORY_CURRENT_LINE_SELECTED,
    STORY_BAR_BASE,
)


class DebugControlsModal(ModalScreen[None]):
    CSS = """
    DebugControlsModal {
        align: center middle;
    }
    #controls-modal {
        width: 76;
        max-width: 92vw;
        height: auto;
        max-height: 85vh;
        border: round ansi_blue;
        background: transparent;
        padding: 1 2;
    }
    #controls-title {
        text-style: bold;
        color: ansi_yellow;
        margin: 0 0 1 0;
    }
    #controls-body {
    }
    #controls-hint {
        text-style: dim;
        margin: 1 0 0 0;
    }
    #profile-title {
        text-style: bold;
        color: ansi_yellow;
        margin: 1 0 0 0;
    }
    #profile-row {
        layout: horizontal;
        height: auto;
        margin: 0 0 1 0;
    }
    .profile-button {
        width: 1fr;
        margin: 0 1 0 0;
    }
    #profile-all {
        margin: 0;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]

    def __init__(self, selected_profile: str, on_profile_selected):
        super().__init__()
        self.selected_profile = normalized_story_filter_profile(selected_profile)
        self.on_profile_selected = on_profile_selected

    def _button_label(self, profile: str) -> str:
        name = profile.capitalize()
        return f"[*] {name}" if profile == self.selected_profile else f"[ ] {name}"

    def _refresh_profile_buttons(self) -> None:
        for profile in ("minimal", "balanced", "all"):
            button = self.query_one(f"#profile-{profile}", Button)
            button.label = self._button_label(profile)
            button.variant = "primary" if profile == self.selected_profile else "default"

    def compose(self) -> ComposeResult:
        controls = [
            ("D", "start/stop debug"),
            ("r", "restart debug/story (manual)"),
            ("R", "toggle auto-restart (file changes restart debug)"),
            ("N", "step next"),
            ("I", "step in"),
            ("O", "step out"),
            ("C", "continue"),
            ("K", "interrupt"),
            ("P", "toggle precision (loose/precise)"),
            ("<- / ->", "scrub one frame"),
            ("Ctrl+<- / Ctrl+->", "scrub ten frames"),
            ("V", "toggle variables panel"),
            ("Shift+V", "toggle variable diff"),
            ("T", "variable tree view (on selected var)"),
            ("t", "toggle timeline capture"),
            ("Ctrl+Enter", "toggle full-file view"),
            ("Esc or Ctrl+C", "back to test list"),
        ]

        key_width = max(len(key) for key, _ in controls)
        body = Text()
        for index, (key, description) in enumerate(controls):
            body.append(key.ljust(key_width), style="bold cyan")
            body.append("    ")
            body.append(description, style="default")
            if index < len(controls) - 1:
                body.append("\n")

        yield Container(
            Static("Debug Controls", id="controls-title"),
            Static(body, id="controls-body"),
            Static("Story Filter Profile", id="profile-title"),
            Container(
                Button(self._button_label("minimal"), id="profile-minimal", classes="profile-button"),
                Button(self._button_label("balanced"), id="profile-balanced", classes="profile-button"),
                Button(self._button_label("all"), id="profile-all", classes="profile-button"),
                id="profile-row",
            ),
            Static("Press Esc, Enter, or ? to close", id="controls-hint"),
            id="controls-modal",
        )

    def on_mount(self) -> None:
        self._refresh_profile_buttons()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("profile-"):
            return
        profile = button_id.replace("profile-", "", 1)
        normalized = normalized_story_filter_profile(profile)
        self.selected_profile = normalized
        self._refresh_profile_buttons()
        if callable(self.on_profile_selected):
            self.on_profile_selected(normalized)

    def action_close(self) -> None:
        self.app.pop_screen()


class TestDebuggerScreen(Screen[None]):
    CSS = """
    #debug-header {
        height: 1;
        min-height: 1;
        padding: 0 1;
        text-style: bold;
    }
    #debug-body {
        height: 1fr;
        min-height: 1;
        layout: vertical;
    }
    #story-code {
        height: 1fr;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    #vars-column {
        layout: vertical;
        width: 1fr;
    }
    #vars-panel {
        height: 1;
        min-height: 1;
        padding: 0 1;
        border: none;
    }
    #vars-tree {
        height: 10;
        min-height: 3;
        border: none;
        padding: 0 1;
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
    }
    #debug-footer {
        height: 1;
        min-height: 1;
        padding: 0 1;
        color: ansi_white;
    }
    """

    STORY_BAR_BASE = "dim"
    STORY_BAR_WINDOW = "dim"
    STORY_BAR_ACTIVE = "blue"
    STORY_BAR_SELECTED = "yellow"
    STORY_META_HIGHLIGHT = "cyan"
    STORY_META_SELECTED = "yellow"
    STORY_HELP = "dim"
    STORY_LINE_MARKER = "red"
    STORY_CODE_BG = "#272822"
    STORY_CURRENT_LINE = "#34352d"
    STORY_CURRENT_LINE_SELECTED = "#49483e"
    SIDE_VARS_MIN_WIDTH = 100

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("ctrl+c", "close", "Back", priority=True),
        Binding("question_mark", "show_controls", "Controls"),
        Binding("d", "toggle_debug", "Debug"),
        Binding("t", "toggle_timeline", "Timeline"),
        Binding("r", "rerun_test", "Restart"),
        Binding("R", "toggle_auto_restart", "Auto-Restart"),
        Binding("n", "step_next", "Next"),
        Binding("i", "step_in", "Step In"),
        Binding("o", "step_out", "Step Out"),
        Binding("c", "continue_run", "Continue"),
        Binding("k", "interrupt_run", "Interrupt"),
        Binding("left", "timeline_prev", "Prev Step"),
        Binding("right", "timeline_next", "Next Step"),
        Binding("ctrl+left", "timeline_prev_10", "-10 Steps"),
        Binding("ctrl+right", "timeline_next_10", "+10 Steps"),
        Binding("v", "toggle_variables", "Variables"),
        Binding("V", "toggle_variables_diff", "Var Diff"),
        Binding("T", "open_variable_tree", "Var Tree"),
        Binding("p", "toggle_precision", "Precision"),
        Binding("ctrl+enter", "toggle_full_file_view", "Full File"),
        Binding("a", "jump_to_assertion", "Assertion"),
        Binding("C", "toggle_coverage", "Coverage"),
        Binding("ctrl+j", "toggle_full_file_view", "", show=False),
        Binding("ctrl+m", "toggle_full_file_view", "", show=False),
        Binding("enter", "toggle_full_file_view", "", show=False),
    ]

    def __init__(self, test: Test):
        super().__init__()
        self.test = test
        self.header_widget: Static | None = None
        self.body_widget: Container | None = None
        self.code_widget: Static | None = None
        self.vars_container: Container | None = None
        self.vars_widget: Static | None = None
        self.vars_tree_widget: TextualTree | None = None
        self.footer_widget: Static | None = None
        self.last_signature: tuple | None = None
        self.selected_frame_index = -1
        self._source_cache: dict[str, list[str]] = {}
        self._line_frames_cache_key: tuple | None = None
        self._line_frames_cache: list = []
        self._line_frames_last_event_count = 0
        self._line_frames_last_skip_seq = max(1, int(global_state.tsv_skip_seq_lines))
        self._line_frames_last_debug_mode = False
        self._line_frames_last_time_start = 0.0
        self._line_frames_last_events_id = 0
        self._variables_cache: dict[tuple[int, str, int], list[tuple[str, str, str]]] = {}
        self._variables_task: asyncio.Task | None = None
        self._vars_tree_signature: tuple | None = None
        self._vars_tree_scroll_by_frame: dict[tuple[int, str, int], float] = {}
        self._vars_tree_current_key: tuple[int, str, int] | None = None
        # Collapsed variable paths (dotted-key tuples) survive tree rebuilds
        # because they are content-keyed, not frame-keyed.
        self._collapsed_var_paths: set[tuple[str, ...]] = set()
        self.variables_visible = True
        self.variables_diff_mode = False
        self._footer_timer = None
        self._action_task: asyncio.Task | None = None
        self._action_label: str | None = None
        self._last_log_count = -1
        self.full_file_view = False
        # Feature K: coverage overlay in full-file view
        self.coverage_view = False
        # Feature F: click-to-set breakpoints in full-file view
        self._breakpoints: set[tuple[str, int]] = set()
        self._fullfile_source_path: str = ""
        self._fullfile_line_map: dict[int, int] = {}
        self._follow_latest_frame = True
        self._mouse_dragging = False
        # Code panel text selection (click-drag to select + copy)
        self._code_plain_lines: list[str] = []
        self._code_selection_anchor: tuple[int, int] | None = None
        self._code_selection_cursor: tuple[int, int] | None = None
        self._code_selection_active = False
        # Assertion failures from the previous run (cached before rerun)
        self._assertion_failures: list = []
        self._assertion_view = False
        # Shift-click on a variables tree node opens the variable tree view.
        self._mouse_shift: bool = False
        self._vtree_loading: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="debug-header")
        with Container(id="debug-body"):
            yield Static("", id="story-code")
            with Container(id="vars-column"):
                yield Static(
                    Text("Variables", style=f"bold {self.STORY_META_SELECTED}"),
                    id="vars-panel",
                )
                yield TextualTree("Variables", id="vars-tree")
        yield Static("", id="debug-footer")

    async def on_mount(self) -> None:
        self.header_widget = self.query_one("#debug-header", Static)
        self.body_widget = self.query_one("#debug-body", Container)
        self.code_widget = self.query_one("#story-code", Static)
        self.vars_container = self.query_one("#vars-column", Container)
        self.vars_widget = self.query_one("#vars-panel", Static)
        self.vars_tree_widget = self.query_one("#vars-tree", TextualTree)
        self.footer_widget = self.query_one("#debug-footer", Static)
        if self.vars_tree_widget is not None:
            self.vars_tree_widget.show_root = False
        self.test.timeline_capture_enabled = True

        # Cache assertion failures from the previous run before rerunning
        # (the rerun creates a fresh TestRun, losing the old stderr).
        run = self.test.current_run
        if run is not None:
            from core.assertions import parse_assertion_failures

            combined = (run.stderr or "") + "\n" + (run.compile_err or "")
            self._assertion_failures = parse_assertion_failures(combined)

        # Feature F: load current editor breakpoints for click-to-toggle.
        from api._runner import refresh_editor_breakpoints_cache

        bps, _ = refresh_editor_breakpoints_cache()
        self._breakpoints = {(os.path.abspath(f), n) for f, n in bps}

        self._set_footer_text()
        self._refresh_view(force=True)
        if self.test.state != TestState.RUNNING and not is_debug_active(self.test):
            await self.action_rerun_test()
        self.set_interval(0.1, self._tick)

    def on_unmount(self) -> None:
        if self._variables_task is not None and not self._variables_task.done():
            self._variables_task.cancel()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(cancel_test_and_restore_normal_build(self.test))
        except RuntimeError:
            pass

        cancel_pending_story_annotations_persist(self.test)
        save_story_annotations(os.path.abspath(self.test.source_path), {})
        clear_debug_line()

    # ----- Variables tree collapse/expand persistence ------------------
    # The tree is rebuilt on most ticks; these handlers keep user collapse
    # state in a content-keyed set so it survives rebuilds and frame scrubbing.
    def on_tree_node_collapsed(self, event) -> None:
        path = getattr(getattr(event, "node", None), "data", None)
        if isinstance(path, tuple):
            self._collapsed_var_paths.add(path)

    def on_tree_node_expanded(self, event) -> None:
        path = getattr(getattr(event, "node", None), "data", None)
        if isinstance(path, tuple):
            self._collapsed_var_paths.discard(path)

    def on_tree_node_selected(self, event) -> None:
        """Shift-click on a variables tree node opens the variable tree view."""
        if not self._mouse_shift:
            return
        # Consume the flag so keyboard-driven NodeSelected doesn't retrigger.
        self._mouse_shift = False
        if self._vtree_loading:
            return
        if not is_debug_active(self.test):
            self._set_footer_text(
                "Variable tree view requires an active debug session "
                "(press D to start debugging)"
            )
            return
        path = getattr(getattr(event, "node", None), "data", None)
        if not isinstance(path, tuple) or not path:
            return
        # The first path element is the C variable name in the current frame.
        var_name = path[0]
        self._open_variable_tree(var_name)

    def _open_variable_tree(self, var_name: str) -> None:
        """Asynchronously build a VarTreeNode tree via gdb and push the screen."""
        controller = get_debug_session(self.test)
        if controller is None:
            return

        self._vtree_loading = True
        self._set_footer_text(f"Building tree for '{var_name}'...")

        async def _build_and_show() -> None:
            try:
                tree = await build_variable_tree(controller, var_name)
                if tree is not None:
                    on_restart = self._make_tree_restart_callback(var_name)
                    on_expand_node = self._make_tree_expand_callback()
                    self.app.push_screen(
                        VariableTreeScreen(
                            tree,
                            var_name,
                            on_restart=on_restart,
                            on_expand_node=on_expand_node,
                        )
                    )
                else:
                    self._set_footer_text(f"Cannot expand '{var_name}'")
            except Exception:
                self._set_footer_text(f"Error expanding '{var_name}'")
            finally:
                self._vtree_loading = False

        asyncio.ensure_future(_build_and_show())

    def _make_tree_restart_callback(self, var_name: str):
        """Create an async callback that restarts debug + rebuilds the tree."""

        async def _on_restart():
            # Force restart: stop current debug, recompile, start fresh.
            if self._action_task is not None and not self._action_task.done():
                self._action_task.cancel()
                try:
                    await self._action_task
                except asyncio.CancelledError:
                    pass
                self._action_task = None

            await stop_debug_session(self.test)
            self._maybe_refresh_dwarf_cache()
            self._reset_story_state()
            run = self.test.current_run
            if run is not None:
                run.aggregate_annotations = False
            await start_debug_session(
                self.test, precision_mode=self.test.debug_precision_mode
            )
            controller = get_debug_session(self.test)
            if controller is None:
                return None
            return await build_variable_tree(controller, var_name)

        return _on_restart

    def _make_tree_expand_callback(self):
        """Create a per-variable async callback for the ``a`` expand action.

        Receives a variable's gdb identity (expr, display, value, type_hint)
        and a count, and rebuilds it as ``*(expr)@count`` via the live gdb
        controller (no debug restart). Works for both a node's title line
        (re-expands the node) and an inlined field (promotes it to a box).
        """

        async def _on_expand_node(expr, display, value, type_hint, count: int):
            controller = get_debug_session(self.test)
            if controller is None:
                return None
            return await build_array_subtree(
                controller, expr, display, value, type_hint, count
            )

        return _on_expand_node

    def action_open_variable_tree(self) -> None:
        """Open the variable tree view for the currently selected variable.

        Uses live gdb when a debug session is active (deep expansion).
        Falls back to pre-captured variables from the current frame when
        in auto-story mode (limited by the capture depth at trace time).
        """
        if self.vars_tree_widget is None:
            return
        node = self.vars_tree_widget.cursor_node
        if node is None:
            self._set_footer_text("Select a variable first (click or arrow keys).")
            return
        path = getattr(node, "data", None)
        if not isinstance(path, tuple) or not path:
            return
        if self._vtree_loading:
            return
        var_name = path[0]

        if is_debug_active(self.test):
            self._open_variable_tree(var_name)
        else:
            self._open_variable_tree_from_captured(var_name)

    def _open_variable_tree_from_captured(self, var_name: str) -> None:
        """Build a tree from pre-captured frame variables (no gdb needed)."""
        frames = self._line_frames()
        if not frames:
            self._set_footer_text("No story frames available.")
            return
        idx = self.selected_frame_index
        if idx < 0 or idx >= len(frames):
            self._set_footer_text("Select a frame first.")
            return
        event = frames[idx]
        raw_vars = event.variables or []
        vars_list: list[tuple[str, str, str]] = []
        for vt in raw_vars:
            if len(vt) >= 3:
                vars_list.append(vt)
            elif len(vt) == 2:
                vars_list.append((vt[0], vt[1], ""))

        tree = build_tree_from_captured(vars_list, var_name)
        if tree is None:
            self._set_footer_text(
                f"No captured data for '{var_name}'. "
                "Start a debug session (D) for live expansion."
            )
            return
        self.app.push_screen(VariableTreeScreen(tree, var_name))

    async def action_close(self) -> None:
        self.app.pop_screen()

    async def action_toggle_timeline(self) -> None:
        self.test.timeline_capture_enabled = not self.test.timeline_capture_enabled
        mode = "enabled" if self.test.timeline_capture_enabled else "disabled"
        self._set_footer_text(f"Timeline capture {mode} for {self.test.name}.")

    def _maybe_refresh_dwarf_cache(self) -> None:
        from runner.artifacts import test_binary_path
        binary_path = test_binary_path(self.test.source_path)
        try:
            current_mtime = int(os.path.getmtime(binary_path)) if os.path.exists(binary_path) else 0
        except OSError:
            current_mtime = 0
        cache = self.test.dwarf_cache
        if cache.last_binary_path != binary_path or cache.last_binary_mtime != current_mtime:
            cache.reset_binary_caches()
            cache.last_binary_path = binary_path
            cache.last_binary_mtime = current_mtime
        cache.reset_runtime_caches()

    async def action_rerun_test(self) -> None:
        is_manual = self._is_manual_debug_story()

        run = self.test.current_run
        debug_running = run.debug_running if run is not None else False
        if debug_running or is_debug_active(self.test):
            if is_manual:
                running_action = self._action_task is not None and not self._action_task.done()
                if running_action:
                    await self._force_restart_debug_session()
                    self._set_footer_text("Debugger force-restarted from beginning.")
                    return
                await self._run_action(self._restart_debug_session(), "Debugger restarted.")
                return
            else:
                from runner.execute import _cancel_active_run_for_manual_debug
                await _cancel_active_run_for_manual_debug(self.test)
                self._queue_story_capture("Story capture restarted.")
                return

        if is_manual:
            await self._run_action(
                self._restart_debug_session(),
                "Debugger restarted.",
            )
            return

        self._queue_story_capture("Auto-started story capture.")

    def _queue_story_capture(self, footer_message: str | None = None) -> None:
        self._maybe_refresh_dwarf_cache()
        self._reset_story_state()
        run = self.test.current_run
        if run is not None:
            run.aggregate_annotations = True
        self._follow_latest_frame = False
        clear_debug_line()
        self.test.state = TestState.PENDING
        self.test.time_start = 0.0
        self.test.time_state_changed = time.monotonic()
        state_changed()
        if footer_message:
            self._set_footer_text(footer_message)

    async def _stop_debug_and_resume_story_capture(self) -> None:
        await stop_debug_session(self.test)
        self._queue_story_capture()

    async def _restart_debug_session(self) -> None:
        await stop_debug_session(self.test)
        self._maybe_refresh_dwarf_cache()
        self._reset_story_state()
        run = self.test.current_run
        if run is not None:
            run.aggregate_annotations = False
        await start_debug_session(self.test, precision_mode=self.test.debug_precision_mode)

    async def _force_restart_debug_session(self) -> None:
        if self._action_task is not None and not self._action_task.done():
            self._action_task.cancel()
            try:
                await self._action_task
            except asyncio.CancelledError:
                pass
            self._action_task = None
            self._action_label = None

        if self._variables_task is not None and not self._variables_task.done():
            self._variables_task.cancel()
            self._variables_task = None

        if is_debug_active(self.test):
            try:
                await debug_interrupt_nowait(self.test)
            except Exception:
                pass

        await stop_debug_session(self.test)
        self._maybe_refresh_dwarf_cache()
        self._reset_story_state()
        run = self.test.current_run
        if run is not None:
            run.aggregate_annotations = False
        self._refresh_view(force=True)
        self._set_footer_text("Force restarting debugger...")
        await start_debug_session(self.test, precision_mode=self.test.debug_precision_mode)

    def _reset_story_state(self) -> None:
        self._maybe_refresh_dwarf_cache()
        self.test.current_run = TestRun()
        invalidate_story_annotation_cache(self.test, self.test.dwarf_cache)
        self._line_frames_cache_key = None
        self._line_frames_cache = []
        self._line_frames_last_event_count = 0
        self._line_frames_last_skip_seq = max(1, int(global_state.tsv_skip_seq_lines))
        self._line_frames_last_debug_mode = False
        self._line_frames_last_time_start = 0.0
        self._line_frames_last_events_id = 0
        self._source_cache.clear()
        self._variables_cache.clear()
        self._vars_tree_signature = None
        self._vars_tree_scroll_by_frame.clear()
        self._vars_tree_current_key = None
        self._collapsed_var_paths.clear()
        self.last_signature = None
        self.selected_frame_index = -1
        self._follow_latest_frame = True

    async def action_toggle_debug(self) -> None:
        if is_debug_active(self.test):
            await self._run_action(
                self._stop_debug_and_resume_story_capture(),
                "Debugger stopped. Auto-started story capture.",
            )
            return

        if self._variables_task is not None and not self._variables_task.done():
            self._variables_task.cancel()
            self._variables_task = None
        self._maybe_refresh_dwarf_cache()
        self._reset_story_state()
        run = self.test.current_run
        if run is not None:
            run.aggregate_annotations = False
        self._refresh_view(force=True)
        self._set_footer_text("Starting debugger...")

        await self._run_action(
            start_debug_session(self.test, precision_mode=self.test.debug_precision_mode),
            "Debugger started.",
        )

    async def action_step_next(self) -> None:
        if not self._ensure_debug_active("Step"):
            return
        self._follow_latest_frame = True
        success = (
            "Stepped over."
            if self.test.debug_precision_mode == "precise"
            else "Advanced one smart step."
        )
        await self._run_action(
            debug_step_next(self.test),
            success,
            action_label="Stepping",
        )

    async def action_step_in(self) -> None:
        if not self._ensure_debug_active("Step-in"):
            return
        self._follow_latest_frame = True
        await self._run_action(
            debug_step_in(self.test),
            "Stepped in.",
            action_label="Stepping",
        )

    async def action_step_out(self) -> None:
        if not self._ensure_debug_active("Step-out"):
            return
        self._follow_latest_frame = True
        await self._run_action(
            debug_step_out(self.test),
            "Stepped out.",
            action_label="Stepping",
        )

    async def action_continue_run(self) -> None:
        if not self._ensure_debug_active("Continue"):
            return
        self._follow_latest_frame = True
        await self._run_action(
            debug_continue(self.test),
            "Continued execution.",
            action_label="Running",
        )

    async def action_interrupt_run(self) -> None:
        if not self._ensure_debug_active("Interrupt"):
            return
        self._follow_latest_frame = True
        running_action = self._action_task is not None and not self._action_task.done()
        if running_action:
            await debug_interrupt_nowait(self.test)
            self._set_footer_text("Interrupt requested. Waiting for debugger to stop...", warning=True)
            return

        await self._run_action(debug_interrupt(self.test), "Sent interrupt.")

    def action_show_controls(self) -> None:
        self.app.push_screen(
            DebugControlsModal(
                selected_profile=self.test.story_filter_profile,
                on_profile_selected=self._set_story_filter_profile,
            )
        )

    def _set_story_filter_profile(self, profile: str) -> None:
        normalized = normalized_story_filter_profile(profile)
        global_state.story_filter_profile_preference = normalized
        for test in state.all_tests:
            test.story_filter_profile = normalized
        save_user_config({"story_filter_profile": normalized})
        self._set_footer_text(f"Story filter profile set to {normalized}.")
        self._refresh_view(force=True)

    async def action_toggle_precision(self) -> None:
        if not is_debug_active(self.test):
            self._set_footer_text("Precision toggle is available only while debug is active.", warning=True)
            return

        self.test.debug_precision_mode = (
            "precise" if self.test.debug_precision_mode != "precise" else "loose"
        )
        global_state.debug_precision_mode_preference = self.test.debug_precision_mode
        save_user_config({"debug_precision_mode": self.test.debug_precision_mode})
        mode = self.test.debug_precision_mode

        if self._variables_task is not None and not self._variables_task.done():
            self._variables_task.cancel()
            self._variables_task = None

        self._reset_story_state()
        self._refresh_view(force=True)
        self._set_footer_text(f"Switching precision to {mode} and restarting debugger...")
        await self._run_action(
            self._restart_debug_session(),
            f"Precision set to {mode}. Debugger restarted.",
        )

    def _update_selected_event_index_from_frame(self, frame) -> None:
        run = self.test.current_run
        if run is None:
            return
        try:
            idx = run.timeline_events.index(frame)
        except ValueError:
            idx = -1
        is_manual = self._is_manual_debug_story()
        if not is_manual and self.selected_frame_index == 0:
            run.timeline_selected_event_index = -1
            run.aggregate_annotations = True
        else:
            run.timeline_selected_event_index = idx
            run.aggregate_annotations = False
        if is_manual and frame.file_path and frame.line > 0:
            save_debug_line(frame.file_path, frame.line)
        _schedule_story_annotations_persist(self.test)

    def action_timeline_prev(self) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self._follow_latest_frame = False
        self.selected_frame_index = ensure_selected_frame_index(
            max(0, self.selected_frame_index - 1), len(frames)
        )
        self._update_selected_event_index_from_frame(frames[self.selected_frame_index])
        self._refresh_view(force=True)

    def action_timeline_next(self) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self._follow_latest_frame = False
        self.selected_frame_index = ensure_selected_frame_index(
            min(len(frames) - 1, self.selected_frame_index + 1), len(frames)
        )
        self._update_selected_event_index_from_frame(frames[self.selected_frame_index])
        self._refresh_view(force=True)

    def action_timeline_prev_10(self) -> None:
        self._timeline_jump(-10)

    def action_timeline_next_10(self) -> None:
        self._timeline_jump(10)

    def action_toggle_variables(self) -> None:
        self.variables_visible = not self.variables_visible
        if self.variables_visible:
            self._set_footer_text("Variables panel shown.")
        else:
            self._set_footer_text("Variables panel hidden.")
        self._refresh_view(force=True)

    def action_toggle_variables_diff(self) -> None:
        self.variables_diff_mode = not self.variables_diff_mode
        # Invalidate the per-panel tree signature so the label always refreshes,
        # even if the diffed content happens to equal the previous full listing.
        self._vars_tree_signature = None
        mode = "diff" if self.variables_diff_mode else "full"
        self._set_footer_text(f"Variables: {mode} mode.")
        self._refresh_view(force=True)

    def action_toggle_full_file_view(self) -> None:
        self.full_file_view = not self.full_file_view
        self._assertion_view = False
        if self.full_file_view:
            self._set_footer_text("Full-file view enabled.")
        else:
            self._set_footer_text("Timeline cards view enabled.")
        self._refresh_view(force=True)

    def action_toggle_coverage(self) -> None:
        self.coverage_view = not self.coverage_view
        if self.coverage_view:
            self.full_file_view = True  # coverage only makes sense in full-file view
            self._set_footer_text("Coverage overlay enabled (green=executed, dim=not reached).")
        else:
            self.full_file_view = False  # return to card-based story view
            self._set_footer_text("Coverage overlay disabled.")
        self._refresh_view(force=True)

    def action_jump_to_assertion(self) -> None:
        """Jump the code panel to the first assertion failure's source line."""
        if not self._assertion_failures:
            self._set_footer_text("No assertion failures captured.", warning=True)
            return
        self._assertion_view = True
        self.full_file_view = False
        af = self._assertion_failures[0]
        self._set_footer_text(
            f"Assertion: {af.macro}({af.args}) at {af.file}:{af.line}"
        )
        self._refresh_view(force=True)

    def _timeline_jump(self, offset: int) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self._follow_latest_frame = False
        self.selected_frame_index = ensure_selected_frame_index(
            min(len(frames) - 1, max(0, self.selected_frame_index + offset)),
            len(frames),
        )
        self._update_selected_event_index_from_frame(frames[self.selected_frame_index])
        self._refresh_view(force=True)

    def on_resize(self, event: events.Resize) -> None:
        self._line_frames_cache_key = None
        self._refresh_view(force=True)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._mouse_shift = event.shift
        self._handle_timeline_click(event)
        if self._mouse_dragging:
            return
        # Feature F: in full-file view, clicks toggle breakpoints instead of
        # starting a text selection.
        if self.full_file_view:
            self._handle_fullfile_breakpoint_click(event)
            return
        self._handle_code_selection_start(event)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._mouse_dragging:
            self._handle_timeline_drag(event)
        elif self._code_selection_active:
            self._handle_code_selection_extend(event)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._mouse_dragging:
            self._mouse_dragging = False
        elif self._code_selection_active:
            self._handle_code_selection_finish(event)

    def _index_from_column(self, col: int, width: int, total: int) -> int:
        if total <= 1:
            return 0
        if width <= 1:
            return 0
        ratio = col / (width - 1)
        return max(0, min(total - 1, int(round(ratio * (total - 1)))))

    def _handle_timeline_click(self, event: events.MouseDown) -> None:
        frames = self._line_frames()
        if not frames:
            return

        if self.code_widget is None:
            return

        content_region = self.code_widget.content_region
        sx, sy = event.screen_x, event.screen_y

        if content_region.height == 0:
            return
        if self.code_widget.scroll_y > 0:
            return

        if sy != content_region.y:
            return

        bar_width = max(8, content_region.width - 2)
        col = sx - content_region.x
        if col < 0 or col >= bar_width:
            return

        total = len(frames)
        new_index = self._index_from_column(col, bar_width, total)

        self._follow_latest_frame = False
        self._mouse_dragging = True
        self.selected_frame_index = ensure_selected_frame_index(new_index, total)
        self._update_selected_event_index_from_frame(frames[self.selected_frame_index])
        self._refresh_view(force=True)

    def _handle_timeline_drag(self, event: events.MouseMove) -> None:
        frames = self._line_frames()
        if not frames:
            return

        if self.code_widget is None:
            return

        content_region = self.code_widget.content_region
        sx, sy = event.screen_x, event.screen_y

        if sy != content_region.y:
            return

        bar_width = max(8, content_region.width - 2)
        col = sx - content_region.x
        if col < 0 or col >= bar_width:
            return

        total = len(frames)
        new_index = self._index_from_column(col, bar_width, total)

        self.selected_frame_index = ensure_selected_frame_index(new_index, total)
        self._update_selected_event_index_from_frame(frames[self.selected_frame_index])
        self._refresh_view(force=True)

    # ----- Feature F: click-to-toggle breakpoints (full-file view) ------

    def _handle_fullfile_breakpoint_click(self, event: events.MouseDown) -> None:
        """Toggle a breakpoint at the clicked source line in full-file view."""
        if self.code_widget is None or not self._fullfile_line_map:
            return
        offset = event.get_content_offset(self.code_widget)
        if offset is None:
            return
        panel_line = int(offset.y + self.code_widget.scroll_y)
        source_line = self._fullfile_line_map.get(panel_line)
        if source_line is None or source_line <= 0:
            return
        if not self._fullfile_source_path:
            return
        abs_path = os.path.abspath(self._fullfile_source_path)
        self._toggle_breakpoint(abs_path, source_line)
        event.prevent_default()
        event.stop()

    def _toggle_breakpoint(self, abs_path: str, line: int) -> None:
        from api._runner import save_editor_breakpoints

        key = (abs_path, line)
        if key in self._breakpoints:
            self._breakpoints.discard(key)
            action = "removed"
        else:
            self._breakpoints.add(key)
            action = "set"
        save_editor_breakpoints(list(self._breakpoints))
        self._set_footer_text(
            f"Breakpoint {action}: {os.path.basename(abs_path)}:{line}"
        )
        self._refresh_view(force=True)

    # ----- Code panel text selection (click-drag to copy) ---------------

    def _handle_code_selection_start(self, event: events.MouseDown) -> None:
        """Begin a text selection in the code panel (below the timeline bar)."""
        if self.code_widget is None:
            return
        pos = self._code_mouse_to_position(event)
        if pos is None or pos[0] == 0:
            return  # line 0 is the timeline bar — skip
        self._capture_code_plain_text()
        self._code_selection_anchor = pos
        self._code_selection_cursor = pos
        self._code_selection_active = True
        event.prevent_default()
        event.stop()

    def _handle_code_selection_extend(self, event: events.MouseMove) -> None:
        """Extend the selection during drag."""
        pos = self._code_mouse_to_position(event)
        if pos is None or pos == self._code_selection_cursor:
            return
        self._code_selection_cursor = pos
        self._render_code_with_selection()

    def _handle_code_selection_finish(self, event: events.MouseUp) -> None:
        """Finish selection: copy to clipboard and restore normal rendering."""
        pos = self._code_mouse_to_position(event)
        if pos is not None:
            self._code_selection_cursor = pos
        selected_text = self._extract_code_selection()
        self._code_selection_active = False
        self._code_selection_anchor = None
        self._code_selection_cursor = None
        self._refresh_view(force=True)
        if selected_text:
            if copy_to_clipboard(selected_text):
                self._set_footer_text(f"Copied {len(selected_text)} chars to clipboard.")
            else:
                self._set_footer_text(
                    "Clipboard unavailable. Install pyperclip, wl-copy, or xclip.",
                    warning=True,
                )
        event.prevent_default()
        event.stop()

    def _code_mouse_to_position(self, event: events.MouseEvent) -> tuple[int, int] | None:
        """Map a mouse event to a (line_index, char_index) in the code panel."""
        if self.code_widget is None or not self._code_plain_lines:
            return None
        offset = event.get_content_offset(self.code_widget)
        if offset is None:
            return None
        vy = max(0, int(offset.y + self.code_widget.scroll_y))
        vx = max(0, int(offset.x + self.code_widget.scroll_x))
        line_idx = min(vy, len(self._code_plain_lines) - 1)
        line_text = self._code_plain_lines[line_idx]
        col = self._visual_column_to_index(line_text, vx)
        return (line_idx, col)

    def _capture_code_plain_text(self) -> None:
        """Export the current code widget renderable to plain text lines."""
        if self.code_widget is None:
            self._code_plain_lines = []
            return
        renderable = getattr(self.code_widget, "_renderable", None)
        if renderable is None:
            self._code_plain_lines = []
            return
        width = max(20, self.code_widget.size.width or 80)
        console = Console(record=True, width=width, force_terminal=False, color_system=None)
        console.print(renderable)
        text = console.export_text()
        self._code_plain_lines = text.rstrip("\n").split("\n")

    def _code_selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self._code_selection_anchor is None or self._code_selection_cursor is None:
            return None
        start = self._clamp_code_position(self._code_selection_anchor)
        end = self._clamp_code_position(self._code_selection_cursor)
        if start is None or end is None or start == end:
            return None
        return (start, end) if start <= end else (end, start)

    def _clamp_code_position(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        if not self._code_plain_lines:
            return None
        li = min(max(0, pos[0]), len(self._code_plain_lines) - 1)
        length = len(self._code_plain_lines[li])
        ci = min(max(0, pos[1]), length)
        return (li, ci)

    def _extract_code_selection(self) -> str:
        selection = self._code_selection_bounds()
        if selection is None:
            return ""
        (sl, sc), (el, ec) = selection
        if sl == el:
            return self._code_plain_lines[sl][sc:ec]
        parts = [self._code_plain_lines[sl][sc:]]
        for li in range(sl + 1, el):
            parts.append(self._code_plain_lines[li])
        parts.append(self._code_plain_lines[el][:ec])
        return "\n".join(parts)

    def _render_code_with_selection(self) -> None:
        """Re-render the code panel as plain text with selection highlights."""
        if self.code_widget is None or not self._code_plain_lines:
            return
        self._capture_code_plain_text()
        lines = [Text(line) for line in self._code_plain_lines]
        selection = self._code_selection_bounds()
        if selection is not None:
            (sl, sc), (el, ec) = selection
            for li in range(sl, el + 1):
                if li >= len(lines):
                    break
                rs = sc if li == sl else 0
                re = ec if li == el else len(lines[li].plain)
                if rs < re:
                    lines[li].stylize("reverse", rs, re)
        self.code_widget.update(Group(*lines))

    @staticmethod
    def _visual_column_to_index(line: str, visual_column: int) -> int:
        """Map a visual column offset to a character index (handles wide chars)."""
        if visual_column <= 0 or not line:
            return 0
        current = 0
        for index, char in enumerate(line):
            width = max(1, cell_len(char))
            next_col = current + width
            if visual_column < next_col:
                return index
            if visual_column == next_col:
                return index + 1
            current = next_col
        return len(line)

    async def _run_action(
        self,
        action_coro,
        success_message: str,
        action_label: str | None = None,
    ) -> None:
        if self._action_task is not None and not self._action_task.done():
            closer = getattr(action_coro, "close", None)
            if callable(closer):
                closer()
            self._set_footer_text("A debug action is already running.", warning=True)
            return

        async def _runner() -> None:
            try:
                stop_event = await action_coro
                if stop_event is not None and stop_event.reason == "timeout":
                    self._set_footer_text(
                        "Debugger action timed out. Session still active; press K to interrupt or retry.",
                        warning=True,
                    )
                    return
                self._set_footer_text(success_message)
            except Exception as error:
                self._set_footer_text(f"Debug action failed: {error}", warning=True)
            finally:
                self._action_task = None
                self._action_label = None

        self._action_label = action_label
        if action_label is not None:
            self._set_footer_text(
                f"{action_label}... press K to interrupt",
                warning=True,
                auto_clear=False,
            )
        self._action_task = asyncio.create_task(_runner())

    def action_toggle_auto_restart(self) -> None:
        """Toggle auto-restart: when ON, file changes in watch mode restart the debugger."""
        global_state.debug_auto_restart = not global_state.debug_auto_restart
        if global_state.debug_auto_restart:
            self._set_footer_text("Auto-restart ON — file changes will restart the debugger.")
        else:
            self._set_footer_text("Auto-restart OFF.")
        self._refresh_view(force=True)

    def _tick(self) -> None:
        self._check_auto_restart()
        self._refresh_view()

    def _check_auto_restart(self) -> None:
        """If the watch handler flagged a restart, restart the debug session."""
        pending = global_state.debug_auto_restart_pending
        if not pending:
            return
        from api._runner import _test_key

        if _test_key(self.test) != pending:
            return
        global_state.debug_auto_restart_pending = None
        if self._action_task is not None and not self._action_task.done():
            return
        # If the variable tree view is open, let it handle the restart so
        # the tree is rebuilt with fresh values.
        if len(self.app.screen_stack) > 1:
            top = self.app.screen_stack[-1]
            if hasattr(top, "action_restart") and not getattr(top, "_restarting", False):
                if hasattr(top, "_on_restart") and top._on_restart is not None:
                    top.action_restart()
                    return
        # Only auto-restart if this test is still in debug mode. We check
        # active_debug_test_key (not is_debug_active) because a previous
        # compile failure leaves gdb inactive but the key set — the restart
        # callback will recompile and start a fresh session.
        if global_state.active_debug_test_key != _test_key(self.test):
            return
        self._run_action(
            self._restart_debug_session(),
            "Auto-restart: recompiled, restarting debugger.",
        )

    def _ensure_debug_active(self, action_label: str) -> bool:
        if is_debug_active(self.test):
            return True
        self._set_footer_text(
            f"Debugger idle. Press D to start before {action_label.lower()}.",
            warning=True,
        )
        return False

    def _signature(self) -> tuple:
        run = self.test.current_run
        timeline_events = run.timeline_events if run is not None else []
        last_event = timeline_events[-1] if timeline_events else None
        last_event_sig = (
            last_event.kind,
            last_event.timestamp,
            last_event.file_path,
            last_event.line,
            last_event.message,
        ) if last_event else ()
        debug_running = run.debug_running if run is not None else False
        debug_exited = run.debug_exited if run is not None else False
        debug_exit_code = run.debug_exit_code if run is not None else None
        debug_logs_len = len(run.debug_logs) if run is not None else 0
        compile_err = run.compile_err if run is not None else ""
        return (
            self.test.state,
            self.test.time_state_changed,
            len(timeline_events),
            debug_running,
            debug_exited,
            debug_exit_code,
            self.test.timeline_capture_enabled,
            debug_logs_len,
            last_event_sig,
            self.selected_frame_index,
            len(self._variables_cache),
            int(global_state.tsv_variables_height),
            self.variables_visible,
            self.variables_diff_mode,
            int(self.size.width),
            int(self.size.height),
            self.full_file_view,
            self.coverage_view,
            self._assertion_view,
            self.test.story_filter_profile,
            compile_err,
        )

    def _base_footer_text(self) -> Text:
        text = Text()
        if not self._line_frames():
            text.append("No story yet. Press ")
            text.append("r", style="bold")
            text.append(" to run.", style="dim")

        text.append("  ")
        text.append("r", style="bold")
        text.append(" Restart", style="dim")
        text.append("  ")
        text.append("R", style="bold")
        if global_state.debug_auto_restart:
            text.append(" Auto:ON", style="bold green")
        else:
            text.append(" Auto:OFF", style="dim")

        if self._assertion_failures:
            text.append("  ")
            text.append("a", style="bold")
            text.append(" Assertion", style="dim")

        text.append("  ")
        text.append("?", style="bold")
        text.append(" - Help", style="dim")
        return text

    def _set_footer_text(
        self,
        message: str | None = None,
        warning: bool = False,
        auto_clear: bool = True,
    ) -> None:
        if self.footer_widget is None:
            return
        if message is None:
            self.footer_widget.update(self._base_footer_text())
            return

        style = "yellow" if warning else "bright_black"
        self.footer_widget.update(Text(message, style=style))
        if self._footer_timer is not None:
            self._footer_timer.stop()
            self._footer_timer = None
        if not auto_clear:
            return
        self._footer_timer = self.set_timer(2.0, self._clear_footer_message)

    def _clear_footer_message(self) -> None:
        self._footer_timer = None
        self._set_footer_text()

    def _ensure_vars_panel_height(self) -> None:
        if (
            self.body_widget is None
            or self.code_widget is None
            or self.vars_container is None
            or self.vars_widget is None
            or self.vars_tree_widget is None
        ):
            return

        side_layout = int(self.size.width) >= self.SIDE_VARS_MIN_WIDTH
        desired_layout = "horizontal" if side_layout else "vertical"
        if self.body_widget.styles.layout != desired_layout:
            self.body_widget.styles.layout = desired_layout
            self._line_frames_cache_key = None

        if not self.variables_visible:
            if self.vars_container.styles.display != "none":
                self.vars_container.styles.display = "none"
            self.code_widget.styles.height = "1fr"
            self.code_widget.styles.width = "1fr"
            return

        if self.vars_container.styles.display == "none":
            self.vars_container.styles.display = "block"

        if self.vars_widget.styles.height != 1:
            self.vars_widget.styles.height = 1

        available = max(4, int(self.size.height))
        header_height = 1
        footer_height = 1
        vars_label_height = 1
        body_height = max(3, available - header_height - footer_height)

        if side_layout:
            total_width = max(40, int(self.size.width))
            vars_width = max(30, int(total_width * 0.34))
            max_vars_width = max(30, total_width - 40)
            vars_width = min(vars_width, max_vars_width)

            self.code_widget.styles.height = "1fr"
            self.code_widget.styles.width = "1fr"
            if self.vars_container.styles.width != vars_width:
                self.vars_container.styles.width = vars_width
            self.vars_container.styles.height = "1fr"

            vars_tree_height = max(3, body_height - vars_label_height)
            if self.vars_tree_widget.styles.height != vars_tree_height:
                self.vars_tree_widget.styles.height = vars_tree_height
            return

        self.code_widget.styles.width = "1fr"
        self.vars_container.styles.width = "1fr"
        self.vars_container.styles.height = "auto"

        story_height = max(1, int(body_height * 0.7))
        vars_total_height = max(2, body_height - story_height)
        vars_tree_height = max(3, vars_total_height - vars_label_height)

        max_tree_for_layout = max(3, body_height - vars_label_height - 1)
        vars_tree_height = min(vars_tree_height, max_tree_for_layout)
        story_height = max(1, body_height - vars_label_height - vars_tree_height)

        if self.code_widget is not None and self.code_widget.styles.height != story_height:
            self.code_widget.styles.height = story_height
        if self.vars_tree_widget.styles.height != vars_tree_height:
            self.vars_tree_widget.styles.height = vars_tree_height

    def _line_frames(self):
        skip_seq = max(1, int(global_state.tsv_skip_seq_lines))
        run = self.test.current_run
        debug_running = run.debug_running if run is not None else False
        debug_mode = (
            is_debug_active(self.test)
            or debug_running
            or self._is_manual_debug_story()
        )
        events = run.timeline_events if run is not None else []
        event_count = len(events)

        same_settings = (
            self._line_frames_last_skip_seq == skip_seq
            and self._line_frames_last_debug_mode == debug_mode
            and self._line_frames_last_time_start == self.test.time_start
            and self._line_frames_last_events_id == id(events)
        )

        if same_settings and event_count == self._line_frames_last_event_count:
            return self._line_frames_cache

        if not same_settings or event_count < self._line_frames_last_event_count:
            frames = [
                event
                for event in events
                if event.kind == "test_failed"
                or event_has_useful_source_line(event.file_path, event.line, self._source_cache)
            ]
            if not debug_mode and skip_seq > 1 and len(frames) > 1:
                filtered = [frames[0]]
                seq_since_emit = 0
                prev = frames[0]
                prev_abs_path = os.path.abspath(prev.file_path)
                for frame in frames[1:]:
                    frame_abs_path = os.path.abspath(frame.file_path)
                    same_file = frame_abs_path == prev_abs_path
                    same_function = frame.function == prev.function
                    if frame.trigger_ids or prev.trigger_ids:
                        filtered.append(frame)
                        seq_since_emit = 0
                        prev = frame
                        prev_abs_path = frame_abs_path
                        continue
                    is_sequential = (
                        same_file and same_function and frame.line == (prev.line + 1)
                    )

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
                frames = filtered

            self._line_frames_cache = frames
            self._line_frames_last_event_count = event_count
            self._line_frames_last_skip_seq = skip_seq
            self._line_frames_last_debug_mode = debug_mode
            self._line_frames_last_time_start = self.test.time_start
            self._line_frames_last_events_id = id(events)
            return frames

        appended = events[self._line_frames_last_event_count :]
        if not appended:
            return self._line_frames_cache

        frames = self._line_frames_cache
        for event in appended:
            is_fail_event = event.kind == "test_failed"
            if not is_fail_event and not event_has_useful_source_line(event.file_path, event.line, self._source_cache):
                continue

            if debug_mode or skip_seq <= 1 or not frames:
                frames.append(event)
                continue

            prev = frames[-1]
            prev_abs_path = os.path.abspath(prev.file_path)
            event_abs_path = os.path.abspath(event.file_path)
            same_file = event_abs_path == prev_abs_path
            same_function = event.function == prev.function
            if event.trigger_ids or prev.trigger_ids:
                frames.append(event)
                continue
            is_sequential = same_file and same_function and event.line == (prev.line + 1)
            if not is_sequential:
                frames.append(event)
                continue

            # Keep sequence thinning lightweight while ensuring progress.
            if len(frames) % skip_seq == 0:
                frames.append(event)

        self._line_frames_cache = frames
        self._line_frames_last_event_count = event_count
        self._line_frames_last_skip_seq = skip_seq
        self._line_frames_last_debug_mode = debug_mode
        self._line_frames_last_time_start = self.test.time_start
        self._line_frames_last_events_id = id(events)
        return frames

    def _is_manual_debug_story(self) -> bool:
        run = self.test.current_run
        if run is None:
            return False
        for event in reversed(run.timeline_events):
            if event.kind != "run_start":
                continue
            message = event.message.lower()
            return "manual debug" in message or "debug" in message
        return False

    async def _fetch_expanded_variables_for_frame(self, selected_event) -> None:
        if selected_event is None:
            return

        event_key = (selected_event.index, selected_event.file_path, selected_event.line)
        if event_key in self._variables_cache:
            return

        if not is_debug_active(self.test):
            return

        raw_vars = selected_event.variables or []
        normalized_vars: list[tuple[str, str, str]] = []
        for var_tuple in raw_vars:
            if len(var_tuple) >= 3:
                normalized_vars.append(var_tuple)
            else:
                name, value = var_tuple
                normalized_vars.append((name, value, ""))
        self._variables_cache[event_key] = normalized_vars
        self._refresh_view(force=True)

    def _collect_covered_lines(self) -> dict[str, set[int]]:
        """Collect {abs_source_path: {line_numbers}} from timeline events and
        annotation cache.  This is a lightweight approximation of execution
        coverage based on gdb-traced stops."""
        covered: dict[str, set[int]] = {}
        run = self.test.current_run
        if run is None:
            return covered

        # From timeline events (filtered stops that became cards)
        for event in run.timeline_events:
            if event.file_path and event.line > 0:
                abs_path = os.path.abspath(event.file_path)
                covered.setdefault(abs_path, set()).add(event.line)

        # From annotation cache (broader — every stop's annotations are merged,
        # not just "interesting" ones).  Structure:
        # {func_name: {abs_file_path: {line_no: {var_name: value}}}}
        for func_map in run.annotation_cache.values():
            for file_path, lines in func_map.items():
                covered.setdefault(file_path, set()).update(lines.keys())

        return covered

    def _render_code_panel(self) -> None:
        run = self.test.current_run
        compile_err = run.compile_err if run is not None else ""
        has_compile_err = bool(compile_err.strip())
        is_active = is_debug_active(self.test) or self.test.state == TestState.RUNNING
        is_manual = self._is_manual_debug_story()
        if has_compile_err and not is_active and not is_manual:
            if self.code_widget is not None:
                self.code_widget.update(Text.from_ansi(compile_err))
            return

        # Assertion view: show source at the failing assertion line
        if self._assertion_view and self._assertion_failures:
            self._render_assertion_source()
            return

        frames = self._line_frames()

        if self.full_file_view:
            from runner.story_annotations import get_story_annotations
            annotations = get_story_annotations(self.test, cache=self.test.dwarf_cache)
            covered_lines = {}
            if self.coverage_view:
                covered_lines = self._collect_covered_lines()
            meta = render_full_file_panel(
                self.code_widget,
                frames,
                self.selected_frame_index,
                self._source_cache,
                annotations=annotations,
                active_breakpoints=self._breakpoints,
                covered_lines=covered_lines if self.coverage_view else None,
            )
            # Feature F: record the source path + panel-line→source-line mapping
            # so click handlers can toggle breakpoints accurately.
            if meta is not None:
                source_path, snippet_start, snippet_end = meta
                self._fullfile_source_path = source_path
                self._fullfile_line_map = {
                    (i + 1): snippet_start + i
                    for i in range(snippet_end - snippet_start + 1)
                }
            else:
                self._fullfile_source_path = ""
                self._fullfile_line_map = {}
        else:
            self._fullfile_source_path = ""
            self._fullfile_line_map = {}
            render_code_panel(
                self.code_widget,
                frames,
                self.selected_frame_index,
                self._source_cache,
                test=self.test,
            )

        # If text selection is active, overlay selection highlights on the
        # freshly rendered content.
        if self._code_selection_active:
            self._render_code_with_selection()

    def _render_assertion_source(self) -> None:
        """Render the source file at the assertion failure line with a
        coloured diff header."""
        if self.code_widget is None or not self._assertion_failures:
            return
        af = self._assertion_failures[0]
        source_path = os.path.abspath(af.file)
        source_lines = load_source_lines(source_path, self._source_cache)
        if not source_lines:
            self.code_widget.update(
                Text("Source unavailable for assertion location.", style=self.STORY_HELP)
            )
            return

        line_number = max(1, min(len(source_lines), af.line))
        width = max(8, self.code_widget.size.width - 2)
        available_height = max(3, self.code_widget.size.height)
        code_height = max(1, available_height - 4)  # leave room for header

        half = code_height // 2
        snippet_start = max(1, line_number - half)
        snippet_end = min(len(source_lines), snippet_start + code_height - 1)

        number_width = len(str(max(1, snippet_end)))
        code_width = max(1, width - (number_width + 3))

        snippet = build_frame_snippet(
            source_path,
            source_lines,
            line_number,
            snippet_start,
            snippet_end,
            True,
            code_width,
        )

        header = Text()
        header.append("\u2717 ", style="bold red")
        header.append(f"{af.macro}({af.args})", style="bold red")
        diff = Text()
        diff.append("  expected: ", style="bright_black")
        diff.append(af.expected, style="green")
        diff.append("  actual: ", style="bright_black")
        diff.append(af.actual, style="red")
        loc = Text(f"  at {af.file}:{af.line}", style="bright_black")

        self.code_widget.update(Group(header, diff, loc, Text(), snippet))

    def _render_variables_panel(self, selected_event) -> None:
        if self.vars_widget is None or self.vars_tree_widget is None:
            return

        if not self.variables_visible:
            return

        previous_key = self._vars_tree_current_key
        if previous_key is not None:
            self._vars_tree_scroll_by_frame[previous_key] = float(self.vars_tree_widget.scroll_y)

        if selected_event is None:
            self.vars_widget.update(Text("Variables (no selected frame)", style=self.STORY_HELP))
            tree = self.vars_tree_widget
            tree.root.set_label("Variables")
            tree.root.remove_children()
            tree.root.expand()
            tree.refresh()
            self._vars_tree_current_key = None
            self._vars_tree_signature = None
            return

        event_key = (selected_event.index, selected_event.file_path, selected_event.line)
        if is_debug_active(self.test):
            raw_vars = self._variables_cache.get(event_key, selected_event.variables or [])
        else:
            raw_vars = selected_event.variables or []
        vars_list: list[tuple[str, str, str]] = []
        for var_tuple in raw_vars:
            if len(var_tuple) >= 3:
                vars_list.append(var_tuple)
            else:
                name, value = var_tuple
                vars_list.append((name, value, ""))

        # Feature G: variable diff mode — show only changed variables.
        diff_has_previous = True
        diff_map: dict[str, tuple[str, str | None]] = {}
        if self.variables_diff_mode and vars_list:
            vars_list, diff_map, diff_has_previous = self._compute_variable_diff(
                vars_list, selected_event
            )

        target_scroll = self._vars_tree_scroll_by_frame.get(event_key, 0.0)
        if self._vars_tree_signature == tuple(vars_list):
            self.vars_tree_widget.scroll_to(y=target_scroll, animate=False, immediate=True)
            self._vars_tree_current_key = event_key
            return

        if not vars_list:
            if self.variables_diff_mode:
                if diff_has_previous:
                    empty_msg = "Variables (no changes from previous frame)"
                else:
                    empty_msg = "Variables (diff: no previous frame)"
            else:
                empty_msg = "Variables (none captured for this frame)"
            self.vars_widget.update(Text(empty_msg, style=self.STORY_HELP))
            tree = self.vars_tree_widget
            tree.root.set_label("Variables")
            tree.root.remove_children()
            tree.root.expand()
            tree.refresh()
            self._vars_tree_current_key = event_key
            self._vars_tree_scroll_by_frame[event_key] = 0.0
            self._vars_tree_signature = None
            return

        if self.variables_diff_mode:
            self.vars_widget.update(self._variables_diff_label(diff_has_previous, len(vars_list)))
        else:
            self.vars_widget.update(
                Text.assemble(
                    ("Variables", f"bold {STORY_META_SELECTED}"),
                    (f" ({len(vars_list)} vars)", STORY_HELP),
                )
            )

        self._vars_tree_current_key = event_key
        self._vars_tree_scroll_by_frame[event_key] = 0.0

        self._vars_tree_signature = build_variables_tree(
            vars_list,
            self.vars_tree_widget,
            self.vars_widget,
            collapsed_paths=self._collapsed_var_paths,
            diff_map=diff_map,
        )
        # build_variables_tree overwrites the panel label; re-assert diff label.
        if self.variables_diff_mode:
            self.vars_widget.update(self._variables_diff_label(diff_has_previous, len(vars_list)))
        self.vars_tree_widget.scroll_to(y=target_scroll, animate=False, immediate=True)

    def _variables_diff_label(self, diff_has_previous: bool, changed_count: int) -> Text:
        """Build the variables panel label for diff mode."""
        label = Text()
        label.append("Variables (diff)", style=f"bold {STORY_META_SELECTED}")
        if diff_has_previous:
            label.append(f" ({changed_count} changed)", style="bright_black")
        else:
            label.append(" (no previous frame)", style="bright_black")
        return label

    def _compute_variable_diff(
        self,
        current_vars: list[tuple[str, str, str]],
        selected_event,
    ) -> tuple[list[tuple[str, str, str]], dict[str, tuple[str, str | None]], bool]:
        """Diff current frame variables against the previous frame.

        Returns ``(diffed_vars, diff_map, has_previous_frame)``. ``diff_map``
        maps a variable name to ``(status, old_value)`` where status is
        ``"added"`` / ``"removed"`` / ``"changed"``. When there is no previous
        frame, the original ``current_vars`` is returned unchanged with an
        empty diff map so the panel falls back to showing everything.
        """
        frames = self._line_frames()
        if not frames:
            return list(current_vars), {}, False
        # Locate the selected frame by identity (robust against dataclass
        # value-equality collisions).
        sel_idx = -1
        for i, fr in enumerate(frames):
            if fr is selected_event:
                sel_idx = i
                break
        if sel_idx < 0:
            sel_idx = self.selected_frame_index
        prev_idx = sel_idx - 1
        if prev_idx < 0 or prev_idx >= len(frames):
            return list(current_vars), {}, False

        prev_event = frames[prev_idx]
        prev_key = (prev_event.index, prev_event.file_path, prev_event.line)
        if is_debug_active(self.test):
            prev_raw = self._variables_cache.get(prev_key, prev_event.variables or [])
        else:
            prev_raw = prev_event.variables or []

        # Previous frame: name -> (value, type_hint)
        prev_map: dict[str, tuple[str, str]] = {}
        for vt in prev_raw:
            if len(vt) >= 3:
                prev_map[vt[0]] = (vt[1], vt[2])
            elif len(vt) == 2:
                prev_map[vt[0]] = (vt[1], "")
        # `current_vars` is already normalized to 3-tuples.
        curr_map: dict[str, tuple[str, str]] = {
            name: (value, type_hint) for name, value, type_hint in current_vars
        }

        diffed: list[tuple[str, str, str]] = []
        diff_map: dict[str, tuple[str, str | None]] = {}

        # Changed / newly in-scope variables (iterate in current order for stability).
        for name, value, type_hint in current_vars:
            if name not in prev_map:
                diffed.append((name, value, type_hint))
                diff_map[name] = ("added", None)
            elif prev_map[name][0] != value:
                diffed.append((name, value, type_hint))
                diff_map[name] = ("changed", prev_map[name][0])

        # Removed variables (present in previous frame, absent now) so they
        # stay visible in the tree, struck-through red.
        for name, (value, type_hint) in prev_map.items():
            if name not in curr_map:
                diffed.append((name, value, type_hint))
                diff_map[name] = ("removed", None)

        return diffed, diff_map, True

    def _build_header_text(self) -> Text:
        """Build the two-line story/debug header with status badge."""
        now = time.monotonic()
        elapsed_ms = int(test_elapsed_seconds(self.test, now) * 1000)

        if self.test.state == TestState.PASSED:
            icon, label, badge_style = "\u2713", "PASSED", "green"
        elif self.test.state == TestState.FAILED:
            icon, label, badge_style = "\u2717", "FAILED", "red"
        elif self.test.state == TestState.RUNNING:
            icon, label, badge_style = "\u25cf", "RUNNING", "yellow"
        else:
            icon, label, badge_style = "\u25cb", self.test.state.value, "dim"

        header = Text()
        header.append(self.test.name, style="bold")
        header.append(" ", style="dim")
        header.append(f"{icon} {label}", style=badge_style)
        if elapsed_ms > 0:
            header.append(f" \u00b7 {elapsed_ms}ms", style="dim")

        debug_status = "active" if is_debug_active(self.test) else "idle"
        precision = self.test.debug_precision_mode
        timeline_status = "on" if self.test.timeline_capture_enabled or global_state.timeline_capture_enabled else "off"
        profile = self.test.story_filter_profile

        header.append(
            f"  Debug: {debug_status} ({precision}) \u2502 Recording: {timeline_status} \u2502 Filter: {profile}",
            style="dim",
        )
        return header

    def _refresh_view(self, force: bool = False) -> None:
        signature = self._signature()
        if not force and signature == self.last_signature:
            return

        self._ensure_vars_panel_height()

        frames = self._line_frames()
        total = len(frames)
        if total > 0 and self._follow_latest_frame and is_debug_active(self.test):
            self.selected_frame_index = total - 1
            run = self.test.current_run
            if run is not None:
                run.timeline_selected_event_index = -1
        self.selected_frame_index = ensure_selected_frame_index(
            self.selected_frame_index, total
        )
        selected = None
        if 0 <= self.selected_frame_index < total:
            selected = frames[self.selected_frame_index]
            if self._follow_latest_frame and is_debug_active(self.test) and selected.file_path and selected.line > 0:
                save_debug_line(selected.file_path, selected.line)

        if self.header_widget is not None:
            header = self._build_header_text()
            self.header_widget.update(header)

        self._render_code_panel()
        self._render_variables_panel(selected)

        if selected is not None:
            event_key = (selected.index, selected.file_path, selected.line)
            if event_key not in self._variables_cache:
                if self._variables_task is None or self._variables_task.done():
                    self._variables_task = asyncio.create_task(
                        self._fetch_expanded_variables_for_frame(selected)
                    )

        self.last_signature = signature
