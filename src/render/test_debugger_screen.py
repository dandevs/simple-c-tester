import os
import asyncio
import time
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Tree as TextualTree

import state as global_state
from models import Test, TestState
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
    get_debug_session,
    cancel_test_and_restore_normal_build,
    state_changed,
)
from .test_debugger_screen_utils import (
    display_path,
    detect_language,
    load_source_lines,
    event_has_useful_source_line,
    filter_line_frames,
    ensure_selected_frame_index,
    compute_frame_cards_window,
    build_frame_snippet,
    build_variables_tree,
    render_code_panel,
    STORY_META_HIGHLIGHT,
    STORY_META_SELECTED,
    STORY_HELP,
    STORY_CODE_BG,
    STORY_CURRENT_LINE,
    STORY_CURRENT_LINE_SELECTED,
    STORY_BAR_BASE,
)


class TestDebuggerScreen(Screen[None]):
    CSS = """
    #debug-header {
        height: 1;
        min-height: 1;
        padding: 0 1;
        text-style: bold;
    }
    #story-code {
        height: 1fr;
        min-height: 1;
        border: none;
        padding: 0 1;
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
        Binding("left", "timeline_prev", "Prev Step"),
        Binding("right", "timeline_next", "Next Step"),
        Binding("ctrl+left", "timeline_prev_10", "-10 Steps"),
        Binding("ctrl+right", "timeline_next_10", "+10 Steps"),
        Binding("v", "toggle_variables", "Variables"),
    ]

    def __init__(self, test: Test):
        super().__init__()
        self.test = test
        self.header_widget: Static | None = None
        self.code_widget: Static | None = None
        self.vars_widget: Static | None = None
        self.vars_tree_widget: TextualTree | None = None
        self.footer_widget: Static | None = None
        self.last_signature: tuple | None = None
        self.selected_frame_index = -1
        self._source_cache: dict[str, list[str]] = {}
        self._line_frames_cache_key: tuple | None = None
        self._line_frames_cache: list = []
        self._variables_cache: dict[tuple[int, str, int], list[tuple[str, str]]] = {}
        self._variables_task: asyncio.Task | None = None
        self._vars_tree_signature: tuple | None = None
        self._vars_tree_scroll_by_frame: dict[tuple[int, str, int], float] = {}
        self._vars_tree_current_key: tuple[int, str, int] | None = None
        self.variables_visible = True
        self._footer_timer = None
        self._action_task: asyncio.Task | None = None
        self._last_log_count = -1

    def compose(self) -> ComposeResult:
        yield Static("", id="debug-header")
        yield Static("", id="story-code")
        yield Static(Text("Variables", style=f"bold {self.STORY_META_SELECTED}"), id="vars-panel")
        yield TextualTree("Variables", id="vars-tree")
        yield Static("", id="debug-footer")

    async def on_mount(self) -> None:
        self.header_widget = self.query_one("#debug-header", Static)
        self.code_widget = self.query_one("#story-code", Static)
        self.vars_widget = self.query_one("#vars-panel", Static)
        self.vars_tree_widget = self.query_one("#vars-tree", TextualTree)
        self.footer_widget = self.query_one("#debug-footer", Static)
        if self.vars_tree_widget is not None:
            self.vars_tree_widget.show_root = False
        self.test.timeline_capture_enabled = True
        self._set_footer_text()
        self._refresh_view(force=True)
        if not self._line_frames() and self.test.state != TestState.RUNNING and not is_debug_active(self.test):
            self._set_footer_text("No Test Story yet. Recording is on; press R to run.")
        self.set_interval(0.1, self._tick)

    async def action_close(self) -> None:
        if self._variables_task is not None and not self._variables_task.done():
            self._variables_task.cancel()

        await cancel_test_and_restore_normal_build(self.test)
        self._set_footer_text("Cancelled test debug/recording and restored normal build mode.")
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
        self._variables_cache.clear()
        self._vars_tree_signature = None
        self._vars_tree_scroll_by_frame.clear()
        self._vars_tree_current_key = None
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

    def action_timeline_prev(self) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self.selected_frame_index = ensure_selected_frame_index(
            max(0, self.selected_frame_index - 1), len(frames)
        )
        self._refresh_view(force=True)

    def action_timeline_next(self) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self.selected_frame_index = ensure_selected_frame_index(
            min(len(frames) - 1, self.selected_frame_index + 1), len(frames)
        )
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

    def _timeline_jump(self, offset: int) -> None:
        frames = self._line_frames()
        if not frames:
            return
        self.selected_frame_index = ensure_selected_frame_index(
            min(len(frames) - 1, max(0, self.selected_frame_index + offset)),
            len(frames),
        )
        self._refresh_view(force=True)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        pass

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
            len(self._variables_cache),
            int(global_state.tsv_variables_height),
            self.variables_visible,
        )

    def _base_footer_text(self) -> str:
        if not self._line_frames():
            return (
                "No story yet. Press R to run. Scrub: <-/-> or Ctrl+<- / Ctrl+->  D: debug"
            )
        return (
            "Scrub: click or <-/-> or Ctrl+<- / Ctrl+->  Steps: N/I/O/C/K  A: auto trace  R: rerun/restart debug  D: debug  V: variables"
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

    def _ensure_vars_panel_height(self) -> None:
        if self.vars_widget is None or self.vars_tree_widget is None:
            return
        min_target = max(3, int(global_state.tsv_variables_height))

        if not self.variables_visible:
            if self.vars_widget.styles.display != "none":
                self.vars_widget.styles.display = "none"
            if self.vars_tree_widget.styles.display != "none":
                self.vars_tree_widget.styles.display = "none"
            return

        if self.vars_widget.styles.display == "none":
            self.vars_widget.styles.display = "block"
        if self.vars_tree_widget.styles.display == "none":
            self.vars_tree_widget.styles.display = "block"

        if self.vars_widget.styles.height != 1:
            self.vars_widget.styles.height = 1

        desired = min_target
        if self.vars_tree_widget.styles.height != desired:
            self.vars_tree_widget.styles.height = desired

        available = self.size.height
        fixed = 1 + 2 + 2 + 3 + 1 + 1
        story_min = 8
        max_tree_for_layout = max(min_target, available - fixed - story_min)
        if desired > max_tree_for_layout:
            desired = max(3, max_tree_for_layout)
            if self.vars_tree_widget.styles.height != desired:
                self.vars_tree_widget.styles.height = desired

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

        frames = filter_line_frames(
            self.test.timeline_events,
            self.test.time_state_changed,
            self._source_cache,
            debug_mode,
            cache_key,
            self._line_frames_cache_key,
            self._line_frames_cache,
        )

        self._line_frames_cache_key = cache_key
        self._line_frames_cache = frames
        return frames

    async def _fetch_expanded_variables_for_frame(self, selected_event) -> None:
        if selected_event is None:
            return

        event_key = (selected_event.index, selected_event.file_path, selected_event.line)
        if event_key in self._variables_cache:
            return

        if not is_debug_active(self.test):
            return

        self._variables_cache[event_key] = list(selected_event.variables or [])
        self._refresh_view(force=True)

    def _render_code_panel(self) -> None:
        frames = self._line_frames()
        render_code_panel(
            self.code_widget,
            frames,
            self.selected_frame_index,
            self._source_cache,
        )

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
            vars_list = list(self._variables_cache.get(event_key, selected_event.variables or []))
        else:
            vars_list = list(selected_event.variables or [])

        target_scroll = self._vars_tree_scroll_by_frame.get(event_key, 0.0)
        if self._vars_tree_signature == tuple(vars_list):
            self.vars_tree_widget.scroll_to(y=target_scroll, animate=False, immediate=True)
            self._vars_tree_current_key = event_key
            return

        if not vars_list:
            self.vars_widget.update(Text("Variables (none captured for this frame)", style=self.STORY_HELP))
            tree = self.vars_tree_widget
            tree.root.set_label("Variables")
            tree.root.remove_children()
            tree.root.expand()
            tree.refresh()
            self._vars_tree_current_key = event_key
            self._vars_tree_scroll_by_frame[event_key] = 0.0
            self._vars_tree_signature = None
            return

        self.vars_widget.update(
            Text.assemble(
                ("Variables", f"bold {STORY_META_SELECTED}"),
                (f" ({len(vars_list)} vars)", STORY_HELP),
            )
        )

        self._vars_tree_current_key = event_key
        self._vars_tree_scroll_by_frame[event_key] = 0.0

        self._vars_tree_signature = build_variables_tree(
            vars_list, self.vars_tree_widget, self.vars_widget
        )
        self.vars_tree_widget.scroll_to(y=target_scroll, animate=False, immediate=True)

    def _refresh_view(self, force: bool = False) -> None:
        signature = self._signature()
        if not force and signature == self.last_signature:
            return

        self._ensure_vars_panel_height()

        frames = self._line_frames()
        total = len(frames)
        if total > 0 and is_debug_active(self.test):
            self.selected_frame_index = total - 1
        self.selected_frame_index = ensure_selected_frame_index(
            self.selected_frame_index, total
        )
        selected = None
        if 0 <= self.selected_frame_index < total:
            selected = frames[self.selected_frame_index]

        if self.header_widget is not None:
            status = self.test.state.value
            debug_status = "active" if is_debug_active(self.test) else "idle"
            timeline_status = "on" if self.test.timeline_capture_enabled or global_state.timeline_capture_enabled else "off"
            self.header_widget.update(
                Text(
                    f"Test Story: {self.test.name} [{status}]  Debug: {debug_status}  Recording: {timeline_status}"
                )
            )

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
