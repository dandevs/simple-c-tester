import os

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Static, Tree as TextualTree

from models import Test, ScopeBucket, TimelineEvent
from .test_debugger_screen_utils import (
    display_path,
    render_full_file_panel,
    build_variables_tree,
    STORY_META_SELECTED,
    STORY_META_HIGHLIGHT,
    STORY_HELP,
)


class LexicalScopeHistoryScreen(Screen[None]):
    CSS = """
    #history-header {
        height: 1;
        min-height: 1;
        padding: 0 1;
        text-style: bold;
    }
    #history-body {
        height: 1fr;
        min-height: 1;
        layout: horizontal;
    }
    #history-code {
        width: 1fr;
        height: 1fr;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    #history-vars-column {
        layout: vertical;
        width: 25;
        min-width: 15;
        height: 1fr;
    }
    #history-vars-panel {
        height: 1;
        min-height: 1;
        padding: 0 1;
        border: none;
    }
    #history-vars-tree {
        height: 1fr;
        border: none;
        padding: 0 1;
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
    }
    #history-footer {
        height: 1;
        min-height: 1;
        padding: 0 1;
        color: #8f96a3;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("ctrl+c", "close", "Back", priority=True),
        Binding("left", "timeline_prev", "Prev"),
        Binding("right", "timeline_next", "Next"),
    ]

    def __init__(self, test: Test, focused_event: TimelineEvent | None = None):
        super().__init__()
        self.test = test
        self.focused_event = focused_event
        self.header_widget: Static | None = None
        self.code_widget: Static | None = None
        self.vars_widget: Static | None = None
        self.vars_tree_widget: TextualTree | None = None
        self.footer_widget: Static | None = None
        self.last_signature: tuple | None = None
        self._source_cache: dict[str, list[str]] = {}

        # Scope events from the deepest matching bucket
        self.scope_events: list[TimelineEvent] = []
        self.scope_event_index = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="history-header")
        with Container(id="history-body"):
            yield Static("", id="history-code")
            with Container(id="history-vars-column"):
                yield Static(
                    Text("Variables", style=f"bold {STORY_META_SELECTED}"),
                    id="history-vars-panel",
                )
                yield TextualTree("Variables", id="history-vars-tree")
        yield Static("", id="history-footer")

    async def on_mount(self) -> None:
        self.header_widget = self.query_one("#history-header", Static)
        self.code_widget = self.query_one("#history-code", Static)
        self.vars_widget = self.query_one("#history-vars-panel", Static)
        self.vars_tree_widget = self.query_one("#history-vars-tree", TextualTree)
        self.footer_widget = self.query_one("#history-footer", Static)

        if self.vars_tree_widget is not None:
            self.vars_tree_widget.show_root = False

        self._build_scope_events()
        self._refresh_view(force=True)
        self.set_interval(0.1, self._tick)

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_timeline_prev(self) -> None:
        if self.scope_events and self.scope_event_index > 0:
            self.scope_event_index -= 1
            self._refresh_view(force=True)

    def action_timeline_next(self) -> None:
        if self.scope_events and self.scope_event_index < len(self.scope_events) - 1:
            self.scope_event_index += 1
            self._refresh_view(force=True)

    def _build_scope_events(self) -> None:
        """Find the deepest bucket for the focused event and use its latest_events."""
        run = self.test.current_run
        if run is None or self.focused_event is None or not self.focused_event.file_path:
            self.scope_events = []
            self.scope_event_index = 0
            return

        bucket = run.event_scope_bucket_by_event_id.get(id(self.focused_event))
        if bucket is None:
            bucket = self._find_deepest_bucket(
                self.focused_event.file_path,
                self.focused_event.line,
            )

        while bucket is not None and not bucket.latest_events:
            bucket = bucket.parent

        if bucket is None:
            self.scope_events = []
            self.scope_event_index = 0
            return

        self.scope_events = list(bucket.latest_events)

        # Try to find the focused event's index within the bucket's events
        if self.focused_event is not None:
            for i, ev in enumerate(self.scope_events):
                if ev.index == self.focused_event.index:
                    self.scope_event_index = i
                    break

    def _find_deepest_bucket(self, file_path: str, line: int) -> ScopeBucket | None:
        """Return the deepest ScopeBucket whose line range contains *line*."""
        run = self.test.current_run
        if run is None:
            return None
        abs_path = os.path.abspath(file_path)
        root = run.scope_buckets.get(abs_path)
        if root is None:
            return None
        return self._find_deepest_bucket_for_line(root, line)

    def _find_deepest_bucket_for_line(self, bucket: ScopeBucket, line: int) -> ScopeBucket | None:
        if not (bucket.start_line <= line <= bucket.end_line):
            return None
        for child in sorted(bucket.children, key=lambda c: (c.start_line, c.end_line, c.scope_kind)):
            match = self._find_deepest_bucket_for_line(child, line)
            if match is not None:
                return match
        return bucket

    def _signature(self) -> tuple:
        return (
            self.test.state,
            self.test.time_state_changed,
            len(self.scope_events),
            self.scope_event_index,
        )

    def _tick(self) -> None:
        self._refresh_view()

    def _refresh_view(self, force: bool = False) -> None:
        signature = self._signature()
        if not force and signature == self.last_signature:
            return
        self.last_signature = signature

        self._update_header()
        self._render_code()
        self._render_variables()
        self._update_footer()

    def _update_header(self) -> None:
        if self.header_widget is None:
            return
        text = Text()
        text.append("Scope History", style=f"bold {STORY_META_SELECTED}")
        text.append("  |  ")
        text.append(self.test.name, style="bold")
        self.header_widget.update(text)

    def _render_code(self) -> None:
        if self.code_widget is None:
            return
        if not self.scope_events or not (0 <= self.scope_event_index < len(self.scope_events)):
            self.code_widget.update(Text("No events in scope.", style=STORY_HELP))
            return

        current_event = self.scope_events[self.scope_event_index]
        # Find the event's index in the global timeline so that
        # get_story_annotations can accumulate annotations up to this point.
        run = self.test.current_run
        event_boundary = -1
        if run is not None:
            try:
                event_boundary = run.timeline_events.index(current_event)
            except ValueError:
                pass

        from runner.story_annotations import get_story_annotations
        annotations = get_story_annotations(
            self.test,
            event_boundary=event_boundary if event_boundary >= 0 else None,
            cache=self.test.dwarf_cache,
        )

        render_full_file_panel(
            self.code_widget,
            [current_event],
            0,
            self._source_cache,
            annotations=annotations,
        )

    def _render_variables(self) -> None:
        if self.vars_widget is None or self.vars_tree_widget is None:
            return
        if not self.scope_events or not (0 <= self.scope_event_index < len(self.scope_events)):
            self.vars_widget.update(Text("Variables (no event)", style=STORY_HELP))
            tree = self.vars_tree_widget
            tree.root.set_label("Variables")
            tree.root.remove_children()
            tree.root.expand()
            tree.refresh()
            return

        event = self.scope_events[self.scope_event_index]
        build_variables_tree(event.variables, self.vars_tree_widget, self.vars_widget)

    def _update_footer(self) -> None:
        if self.footer_widget is None:
            return
        if self.scope_events:
            ev = self.scope_events[self.scope_event_index]
            footer = Text()
            footer.append(
                f"Event {self.scope_event_index + 1}/{len(self.scope_events)}",
                style="bold",
            )
            footer.append("  |  ")
            footer.append(f"{display_path(ev.file_path)}:{ev.line}", style=STORY_META_HIGHLIGHT)
            footer.append("  |  ")
            footer.append("← →: Scrub  |  Esc: Back", style=STORY_HELP)
        else:
            footer = Text("← →: Scrub  |  Esc: Back", style=STORY_HELP)
        self.footer_widget.update(footer)
