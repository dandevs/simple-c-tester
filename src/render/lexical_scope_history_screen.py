import os
from dataclasses import dataclass

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Static, Tree as TextualTree

from models import Test, ScopeBucket, TimelineEvent
from .test_debugger_screen_utils import (
    display_path,
    load_source_lines,
    build_frame_title,
    build_frame_snippet,
    STORY_META_SELECTED,
    STORY_HELP,
)


@dataclass
class _BucketTreeData:
    """Data attached to each Tree node: reference to the ScopeBucket."""

    bucket: ScopeBucket | None = None


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
    #scope-tree {
        width: 40%;
        min-width: 20;
        border: none;
        padding: 0 1;
    }
    #event-cards {
        width: 60%;
        border: none;
        padding: 0 1;
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
        Binding("j", "cursor_down", ""),
        Binding("k", "cursor_up", ""),
    ]

    SNIPPET_CONTEXT = 2

    def __init__(self, test: Test):
        super().__init__()
        self.test = test
        self.header_widget: Static | None = None
        self.tree_widget: TextualTree | None = None
        self.cards_widget: Static | None = None
        self.footer_widget: Static | None = None
        self.last_signature: tuple | None = None
        self._source_cache: dict[str, list[str]] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="history-header")
        with Container(id="history-body"):
            yield TextualTree("Scopes", id="scope-tree")
            yield Static("", id="event-cards")
        yield Static("", id="history-footer")

    async def on_mount(self) -> None:
        self.header_widget = self.query_one("#history-header", Static)
        self.tree_widget = self.query_one("#scope-tree", TextualTree)
        self.cards_widget = self.query_one("#event-cards", Static)
        self.footer_widget = self.query_one("#history-footer", Static)

        if self.tree_widget is not None:
            self.tree_widget.show_root = False
            self.tree_widget.guide_depth = 2

        self._populate_tree()
        self._refresh_view(force=True)
        self.set_interval(0.1, self._tick)

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        if self.tree_widget is not None:
            self.tree_widget.action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.tree_widget is not None:
            self.tree_widget.action_cursor_up()

    def _signature(self) -> tuple:
        run = self.test.current_run
        tree_sig = self._tree_signature()
        return (
            self.test.state,
            self.test.time_state_changed,
            tree_sig,
            run.timeline_events if run is not None else [],
        )

    def _tree_signature(self) -> tuple:
        run = self.test.current_run
        if run is None:
            return ()
        sig: list[tuple[str, int, int]] = []
        for abs_path in sorted(run.scope_buckets.keys()):
            root = run.scope_buckets[abs_path]
            sig.append((abs_path, len(root.latest_events), self._count_bucket_nodes(root)))
        return tuple(sig)

    def _count_bucket_nodes(self, bucket: ScopeBucket) -> int:
        return 1 + sum(self._count_bucket_nodes(c) for c in bucket.children)

    def _tick(self) -> None:
        self._refresh_view()

    def _refresh_view(self, force: bool = False) -> None:
        signature = self._signature()
        if not force and signature == self.last_signature:
            return
        self.last_signature = signature

        self._update_header()
        self._update_footer()
        self._render_selected_bucket_cards()

    def _update_header(self) -> None:
        if self.header_widget is None:
            return
        text = Text()
        text.append("History (Lexical Scope)", style=f"bold {STORY_META_SELECTED}")
        text.append("  |  ")
        text.append(self.test.name, style="bold")
        self.header_widget.update(text)

    def _update_footer(self) -> None:
        if self.footer_widget is None:
            return
        self.footer_widget.update(
            Text(
                "↑/↓ or j/k: Navigate tree  |  Enter: Toggle expand  |  Esc: Back",
                style=STORY_HELP,
            )
        )

    def _populate_tree(self) -> None:
        if self.tree_widget is None:
            return
        self.tree_widget.clear()
        run = self.test.current_run
        if run is None:
            return

        first_leaf = None
        for abs_path in sorted(run.scope_buckets.keys()):
            root = run.scope_buckets[abs_path]
            file_label = self._file_node_label(abs_path, root)
            file_node = self.tree_widget.root.add(file_label, data=_BucketTreeData(bucket=root))
            leaf = self._add_bucket_children(file_node, root)
            if first_leaf is None and leaf is not None:
                first_leaf = leaf

        self.tree_widget.root.expand()

        # Auto-select first leaf so cards show immediately
        if first_leaf is not None:
            self.tree_widget.select_node(first_leaf)
            self.tree_widget.cursor_node = first_leaf

    def _file_node_label(self, abs_path: str, root: ScopeBucket) -> str:
        display = display_path(abs_path)
        event_count = len(root.latest_events) + self._sum_child_events(root)
        return f"{display}  ({event_count} events)"

    def _sum_child_events(self, bucket: ScopeBucket) -> int:
        return len(bucket.latest_events) + sum(
            self._sum_child_events(c) for c in bucket.children
        )

    def _add_bucket_children(self, parent_node, bucket: ScopeBucket):
        first_leaf = None
        for child in bucket.children:
            label = self._bucket_node_label(child)
            child_node = parent_node.add(label, data=_BucketTreeData(bucket=child))
            leaf = self._add_bucket_children(child_node, child)
            if first_leaf is None:
                first_leaf = leaf if leaf is not None else child_node
        return first_leaf

    def _bucket_node_label(self, bucket: ScopeBucket) -> str:
        event_count = len(bucket.latest_events) + self._sum_child_events(bucket)
        if bucket.parent is None or not bucket.parent.children:
            # Function scope
            return f"fn  lines {bucket.start_line}-{bucket.end_line}  ({event_count} events)"
        return f"{{}}  lines {bucket.start_line}-{bucket.end_line}  ({event_count} events)"

    def on_tree_node_selected(self, event: TextualTree.NodeSelected) -> None:
        self._render_selected_bucket_cards()

    def _render_selected_bucket_cards(self) -> None:
        if self.cards_widget is None:
            return

        # Always show debug diagnostics at the top
        debug_lines = self._build_debug_info()

        selected_node = None
        if self.tree_widget is not None:
            selected_node = self.tree_widget.cursor_node

        if selected_node is None or selected_node.data is None:
            debug_lines.append(Text("[DEBUG] No tree node selected (cursor_node is None)", style="bold yellow"))
            self.cards_widget.update(Group(*debug_lines))
            return

        data: _BucketTreeData = selected_node.data
        bucket = data.bucket
        if bucket is None:
            debug_lines.append(Text("[DEBUG] Selected node has no bucket data", style="bold yellow"))
            self.cards_widget.update(Group(*debug_lines))
            return

        events = self._collect_bucket_events(bucket)
        if not events:
            debug_lines.append(Text(f"[DEBUG] Selected bucket has 0 events (direct: {len(bucket.latest_events)}, children: {len(bucket.children)})", style="bold yellow"))
            self.cards_widget.update(Group(*debug_lines))
            return

        renderables = self._build_event_cards(events)
        self.cards_widget.update(Group(*(debug_lines + renderables)))

    def _collect_bucket_events(self, bucket: ScopeBucket) -> list[TimelineEvent]:
        """Collect all latest_events from this bucket and all descendants."""
        events = list(bucket.latest_events)
        for child in bucket.children:
            events.extend(self._collect_bucket_events(child))
        # Sort by timestamp to maintain chronological order
        events.sort(key=lambda e: e.timestamp)
        return events

    def _build_debug_info(self) -> list[Text]:
        """Build diagnostic lines showing internal state."""
        lines: list[Text] = []
        lines.append(Text("═" * 40, style="#2e3440"))
        lines.append(Text("DEBUG  Lexical Scope History", style="bold #ffd166"))
        lines.append(Text("═" * 40, style="#2e3440"))

        run = self.test.current_run
        if run is None:
            lines.append(Text("[DEBUG] test.current_run is None", style="bold red"))
            return lines

        lines.append(Text(f"Test: {self.test.name}", style="bold"))
        lines.append(Text(f"State: {self.test.state.value}"))
        lines.append(Text(f"Timeline events (total): {len(run.timeline_events)}"))
        lines.append(Text(f"Scope buckets (files): {len(run.scope_buckets)}"))

        if not run.scope_buckets:
            lines.append(Text("[DEBUG] No scope buckets! _update_scope_buckets was never called or scope_chain was empty.", style="bold red"))
            return lines

        for abs_path, root in sorted(run.scope_buckets.items()):
            total_events = len(root.latest_events) + self._sum_child_events(root)
            node_count = self._count_bucket_nodes(root)
            lines.append(Text(f"  File: {display_path(abs_path)} → {total_events} events, {node_count} buckets"))

        lines.append(Text(f"Open bucket: {run.open_scope_bucket is not None}"))
        if run.open_scope_bucket is not None:
            ob = run.open_scope_bucket
            lines.append(Text(f"  Open: lines {ob.start_line}-{ob.end_line}, events={len(ob.latest_events)}"))

        # Tree state
        if self.tree_widget is not None:
            lines.append(Text(f"Tree cursor_node: {self.tree_widget.cursor_node is not None}"))
            if self.tree_widget.cursor_node is not None:
                cn = self.tree_widget.cursor_node
                lines.append(Text(f"  Node label: {cn.label.plain if hasattr(cn.label, 'plain') else str(cn.label)}"))
                lines.append(Text(f"  Node data: {cn.data is not None}"))
        else:
            lines.append(Text("[DEBUG] tree_widget is None", style="bold red"))

        lines.append(Text("─" * 40, style="#2e3440"))
        return lines

    def _build_event_cards(self, events: list[TimelineEvent]) -> list:
        renderables: list = []
        total = len(events)
        code_width = max(40, int(self.size.width * 0.55))

        for idx, event in enumerate(events):
            title = build_frame_title(event, selected=False)
            renderables.append(title)

            if event.file_path and event.line > 0:
                snippet = self._build_event_snippet(event, code_width)
                if snippet is not None:
                    renderables.append(snippet)

            if event.message and event.kind in ("debug_info", "test_failed"):
                style = "bold red" if event.kind == "test_failed" else STORY_HELP
                renderables.append(Text(event.message, style=style))

            # Separator
            if idx < total - 1:
                renderables.append(Text("─" * code_width, style="#2e3440"))

        return renderables

    def _build_event_snippet(self, event: TimelineEvent, code_width: int):
        if not event.file_path or event.line <= 0:
            return None

        abs_path = os.path.abspath(event.file_path)
        source_lines = load_source_lines(abs_path, self._source_cache)
        if not source_lines:
            return None

        line_number = event.line
        total_lines = len(source_lines)
        snippet_start = max(1, line_number - self.SNIPPET_CONTEXT)
        snippet_end = min(total_lines, line_number + self.SNIPPET_CONTEXT)

        return build_frame_snippet(
            abs_path,
            source_lines,
            line_number,
            snippet_start,
            snippet_end,
            selected=False,
            code_width=code_width,
            line_annotations=event.line_annotations,
        )
