"""Full-page variable tree view screen.

Opened from the debug variable inspector by pressing **T** on a selected
variable.  Renders the variable's data structure as a visual top-to-bottom
tree with Unicode box-drawing nodes and connector lines.

Interaction
-----------
* **Mouse drag** — pan the view (content follows cursor).
* **Hover** — moving the cursor (no button) highlights the specific variable
  line under it (a box's title or one of its fields); that's the variable
  ``a`` will act on.
* **a** — expand the *hovered* variable as an artificial array of *N*
  elements (prompts for the count). A field line is promoted into its own
  child box; a box's title re-expands that node. Needs a live debug session.
* **R** — restart the debug/story session and rebuild the tree with fresh
  values (the variable name is remembered).
* **Escape / ``v``** — close and return to the debug screen.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.screen import Screen
from textual.widgets import Input, Static

import state as global_state
from api._variable_tree import DEFAULT_EXPAND_COUNT, VarTreeNode, VarField
from .variable_tree_layout import (
    LineTarget,
    PositionedNode,
    compute_layout,
    render_tree,
    grid_size,
    hit_test_line,
)

PAN_STEP = 4       # cells per arrow-key press

FOOTER_BASE = "  drag=pan   \u2190\u2192\u2191\u2193=pan   a=expand   r=restart   v/Esc=close"


def _short(name: str) -> str:
    """Last dotted-path segment of a node name (for short labels)."""
    return name.rsplit(".", 1)[-1] if "." in name else name


def _find_node(root: VarTreeNode, name: str) -> VarTreeNode | None:
    """Find a node by its dotted-path name (names are unique in the tree)."""
    if root.name == name:
        return root
    for child in root.children:
        found = _find_node(child, name)
        if found is not None:
            return found
    return None


def _find_field(node: VarTreeNode, expr: str) -> VarField | None:
    """Find a field in *node* by its gdb expr (unique per variable)."""
    for f in node.fields:
        if f.expr == expr:
            return f
    return None


def _splice_into(target: VarTreeNode, replacement: VarTreeNode) -> None:
    """Overwrite *target*'s expandable content with *replacement* in place.

    Used when re-expanding a node's title (same node, fresh element count).
    The name is preserved (it's how the node was located and stays
    highlightable); everything that changes on re-expansion is copied over.
    """
    target.fields = replacement.fields
    target.children = replacement.children
    target.value = replacement.value
    target.type_hint = replacement.type_hint
    target.is_expanded_array = True
    target.is_null = replacement.is_null
    target.is_cycle = replacement.is_cycle
    target.address = replacement.address
    target.expr = replacement.expr


def _promote_field_into_child(
    node: VarTreeNode, field: VarField, replacement: VarTreeNode
) -> None:
    """Turn an inlined *field* of *node* into a child box in place.

    Removes the field from ``node.fields`` and appends *replacement* (the
    expanded array subtree) to ``node.children``. The field's slot in the box
    disappears; a new sub-box takes its place in the graph.
    """
    node.fields = [f for f in node.fields if f.expr != field.expr]
    node.children.append(replacement)


class ArrayCountModal(ModalScreen[int | None]):
    """Prompt for the number of array elements to expand a pointer as.

    Dismisses with a positive ``int`` (Enter) or ``None`` (Esc / invalid).
    """

    CSS = """
    ArrayCountModal {
        align: center middle;
    }
    #array-count-box {
        width: 52;
        height: auto;
        border: round ansi_cyan;
        background: $surface;
        padding: 1 2;
    }
    #array-count-label {
        color: ansi_yellow;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #array-count-input {
        margin: 0 0 1 0;
    }
    #array-count-hint {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, var_name: str = "", default_count: int = 0):
        super().__init__()
        self._var_name = var_name
        self._default = default_count

    def compose(self) -> ComposeResult:
        label = f"Expand '{self._var_name}' as array \u2014 elements:"
        initial = str(self._default) if self._default > 0 else ""
        yield Container(
            Static(label, id="array-count-label", markup=False),
            Input(value=initial, placeholder="count (e.g. 8)", id="array-count-input"),
            Static("Enter = expand    Esc = cancel", id="array-count-hint", markup=False),
            id="array-count-box",
        )

    def on_mount(self) -> None:
        self.query_one("#array-count-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        try:
            count = int(text, 10)
        except ValueError:
            self.dismiss(None)
            return
        self.dismiss(count if count > 0 else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class VariableTreeScreen(Screen[None]):
    """Visualise a :class:`VarTreeNode` as a pannable tree diagram."""

    CSS = """
    VariableTreeScreen {
        background: $surface;
    }
    #vtree-container {
        width: 1fr;
        height: 1fr;
        padding: 0;
    }
    #vtree-canvas {
        width: 1fr;
        height: 1fr;
        background: $surface;
        padding: 0 1;
    }
    #vtree-header {
        height: 1;
        dock: top;
        color: ansi_yellow;
        text-style: bold;
        padding: 0 1;
        background: $boost;
    }
    #vtree-footer {
        height: 1;
        dock: bottom;
        color: $text-muted;
        padding: 0 1;
        background: $boost;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("v", "close", "Close"),
        Binding("r", "restart", "Restart"),
        Binding("a", "expand_node", "Expand"),
        Binding("left", "pan_left", "Pan Left", show=False),
        Binding("right", "pan_right", "Pan Right", show=False),
        Binding("up", "pan_up", "Pan Up", show=False),
        Binding("down", "pan_down", "Pan Down", show=False),
        Binding("h", "pan_left", "", show=False),
        Binding("l", "pan_right", "", show=False),
        Binding("k", "pan_up", "", show=False),
        Binding("j", "pan_down", "", show=False),
    ]

    def __init__(
        self,
        tree: VarTreeNode,
        var_name: str = "",
        on_restart: Callable[[], Awaitable[VarTreeNode | None]] | None = None,
        on_expand_node: Callable[[str, str, str, str, int], Awaitable[VarTreeNode | None]] | None = None,
    ):
        super().__init__()
        self._tree = tree
        self._var_name = var_name or tree.name
        self._on_restart = on_restart
        self._on_expand_node = on_expand_node
        self._positioned: PositionedNode | None = None
        self._pan_x: int = 0
        self._pan_y: int = 0
        self._max_pan_x: int = 0
        self._max_pan_y: int = 0
        # Drag state
        self._dragging: bool = False
        self._drag_origin: tuple[int, int] = (0, 0)
        self._drag_pan_origin: tuple[int, int] = (0, 0)
        # Widgets
        self._canvas: Static | None = None
        self._centered: bool = False
        self._restarting: bool = False
        self._footer_widget: Static | None = None
        # Hovered variable line: the containing node's name + the line target.
        self._hovered_node_name: str | None = None
        self._hovered_target: LineTarget | None = None
        # Variable targeted by the last `a` prompt (captured at modal-open).
        self._expand_node_name: str | None = None
        self._expand_target: LineTarget | None = None
        # Last array element count used (pre-fills the next prompt)
        self._array_count: int = 0

    # ----- lifecycle ----------------------------------------------------

    def compose(self) -> ComposeResult:
        header_text = f"  Variable Tree: {self._var_name}"
        yield Static(header_text, id="vtree-header", markup=False)
        with Container(id="vtree-container"):
            yield Static("", id="vtree-canvas", markup=False)
        yield Static(
            FOOTER_BASE,
            id="vtree-footer",
            markup=False,
        )

    def on_mount(self) -> None:
        self._canvas = self.query_one("#vtree-canvas", Static)
        self._footer_widget = self.query_one("#vtree-footer", Static)
        self._positioned = compute_layout(self._tree)
        self._reset_footer()
        self._refresh()
        # Suppress debugLine updates so the IDE doesn't jump while we're
        # inspecting the tree.  Clear the current line immediately so any
        # IDE watcher stops tracking the old position right away.
        global_state.debug_line_suppressed = True
        try:
            from runner import clear_debug_line
            clear_debug_line()
        except Exception:
            pass

    def on_unmount(self) -> None:
        # Restore debugLine updates for the parent debug screen.
        global_state.debug_line_suppressed = False

    def _refresh(self) -> None:
        if self._canvas is None or self._positioned is None:
            return

        region = self._canvas.content_region
        vw = max(1, region.width)
        vh = max(1, region.height)

        gw, gh = grid_size(self._positioned)
        self._max_pan_x = max(0, gw - vw)
        self._max_pan_y = max(0, gh - vh)

        if not self._centered and vw > 1:
            root_cx = self._positioned.x + self._positioned.width // 2
            self._pan_x = max(0, min(root_cx - vw // 2, self._max_pan_x))
            self._centered = True

        self._pan_x = max(0, min(self._pan_x, self._max_pan_x))
        self._pan_y = max(0, min(self._pan_y, self._max_pan_y))

        hovered_expr = (
            self._hovered_target.expr if self._hovered_target is not None else None
        )
        group = render_tree(
            self._positioned,
            pan_x=self._pan_x,
            pan_y=self._pan_y,
            viewport_w=vw,
            viewport_h=vh,
            highlight_expr=hovered_expr,
        )
        self._canvas.update(group)

    def on_resize(self, event: events.Resize) -> None:
        self._refresh()

    def _set_footer(self, message: str) -> None:
        if self._footer_widget is not None:
            self._footer_widget.update(message)

    def _reset_footer(self) -> None:
        indicator = ""
        if global_state.debug_auto_restart:
            indicator = "   [Auto-Restart: ON]"
        self._set_footer(f"{FOOTER_BASE}{indicator}")

    # ----- mouse drag pan -----------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._drag_origin = (event.x, event.y)
        self._drag_pan_origin = (self._pan_x, self._pan_y)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            dx = event.x - self._drag_origin[0]
            dy = event.y - self._drag_origin[1]
            self._pan_x = max(0, min(self._max_pan_x, self._drag_pan_origin[0] - dx))
            self._pan_y = max(0, min(self._max_pan_y, self._drag_pan_origin[1] - dy))
            self._refresh()
            return
        # Hover (no button held) — highlight the variable line under the cursor.
        # event.x/y are widget-relative; screen_x/y are screen-absolute, which
        # is the frame content_region is expressed in.
        self._update_hover(event.screen_x, event.screen_y)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False

    # ----- hover hit-testing --------------------------------------------

    def _grid_coords(self, screen_x: int, screen_y: int) -> tuple[int, int] | None:
        """Map screen-absolute coords to grid coords, or None if outside.

        Callers must pass ``screen_x``/``screen_y`` (not the widget-relative
        ``x``/``y``) so the canvas's screen offset (e.g. the docked header)
        is accounted for via ``content_region``.
        """
        if self._canvas is None:
            return None
        region = self._canvas.content_region
        rel_x = screen_x - region.x
        rel_y = screen_y - region.y
        if rel_x < 0 or rel_y < 0 or rel_x >= region.width or rel_y >= region.height:
            return None
        return (rel_x + self._pan_x, rel_y + self._pan_y)

    def _update_hover(self, screen_x: int, screen_y: int) -> None:
        if self._positioned is None:
            return
        grid = self._grid_coords(screen_x, screen_y)
        if grid is None:
            self._clear_hover_if_any()
            return
        hit = hit_test_line(self._positioned, grid[0], grid[1])
        if hit is None:
            self._clear_hover_if_any()
            return
        pn, target = hit
        new_expr = target.expr
        old_expr = self._hovered_target.expr if self._hovered_target else None
        if pn.node.name != self._hovered_node_name or new_expr != old_expr:
            self._hovered_node_name = pn.node.name
            self._hovered_target = target
            self._refresh()

    def _clear_hover_if_any(self) -> None:
        if self._hovered_target is not None:
            self._hovered_node_name = None
            self._hovered_target = None
            self._refresh()

    # ----- keyboard pan -------------------------------------------------

    def action_pan_left(self) -> None:
        self._pan_x = max(0, self._pan_x - PAN_STEP)
        self._refresh()

    def action_pan_right(self) -> None:
        self._pan_x = min(self._max_pan_x, self._pan_x + PAN_STEP)
        self._refresh()

    def action_pan_up(self) -> None:
        self._pan_y = max(0, self._pan_y - PAN_STEP)
        self._refresh()

    def action_pan_down(self) -> None:
        self._pan_y = min(self._max_pan_y, self._pan_y + PAN_STEP)
        self._refresh()

    # ----- restart ------------------------------------------------------

    def action_restart(self) -> None:
        """Restart the debug session and rebuild the tree with fresh values."""
        if self._restarting:
            return
        if self._on_restart is None:
            self._set_footer("  Restart not available in this mode.")
            return

        self._restarting = True
        self._set_footer(f"  Restarting debug session for '{self._var_name}'...")

        async def _do_restart() -> None:
            try:
                new_tree = await self._on_restart()
                if new_tree is not None:
                    self._tree = new_tree
                    self._positioned = compute_layout(self._tree)
                    self._centered = False
                    self._pan_x = 0
                    self._pan_y = 0
                    self._refresh()
                    self._set_footer(f"  Tree refreshed for '{self._var_name}'.")
                else:
                    if global_state.debug_auto_restart:
                        self._set_footer(
                            f"  '{self._var_name}' not found "
                            f"(compile error or variable removed). "
                            f"Fix code — tree will auto-refresh on next change."
                        )
                    else:
                        self._set_footer(
                            f"  '{self._var_name}' not found "
                            f"(compile error or variable removed). "
                            f"Fix code and press r to retry."
                        )
            except Exception:
                self._set_footer("  Restart failed.")
            finally:
                self._restarting = False

        asyncio.ensure_future(_do_restart())

    # ----- per-variable array expansion ---------------------------------

    def action_expand_node(self) -> None:
        """Expand the *hovered variable line* as an array of N elements.

        A box's title line re-expands that node; a field line is promoted
        into its own child box. Captures the target at modal-open so a later
        mouse move can't retarget it mid-prompt.
        """
        if self._on_expand_node is None:
            self._set_footer(
                "  Array expansion needs a live debug session "
                "(start one with D, then re-open with T)."
            )
            return
        if self._restarting:
            return
        if self._hovered_target is None or self._hovered_node_name is None:
            self._set_footer("  Hover over a variable line, then press a.")
            return
        target = self._hovered_target
        if not target.is_pointer:
            self._set_footer("  Hovered variable isn't a pointer \u2014 nothing to expand.")
            return
        # Resolve the variable's identity (title vs field) for the default.
        label, default = self._expand_defaults(target)
        self._expand_node_name = self._hovered_node_name
        self._expand_target = target
        self.app.push_screen(
            ArrayCountModal(label, default_count=default),
            self._on_count_chosen,
        )

    def _expand_defaults(self, target: LineTarget) -> tuple[str, int]:
        """Short label + pre-fill count for the modal."""
        if target.is_title:
            node = _find_node(self._tree, self._hovered_node_name or "")
            label = _short(node.name) if node is not None else "node"
            if node is not None and node.is_expanded_array and node.fields:
                return label, len(node.fields)
        else:
            label = _short(target.expr)
        default = self._array_count if self._array_count > 0 else DEFAULT_EXPAND_COUNT
        return label, default

    def _on_count_chosen(self, result: int | None) -> None:
        """Modal dismiss callback \u2014 kicks off the async rebuild."""
        node_name = self._expand_node_name
        target = self._expand_target
        if not result or result <= 0 or node_name is None or target is None:
            self._reset_footer()
            return
        self._array_count = result
        if target.is_title:
            self._expand_title(node_name, result)
        else:
            self._expand_field(node_name, target, result)

    def _expand_title(self, node_name: str, count: int) -> None:
        """Re-expand a node's own variable (title line) with a fresh count."""
        if self._on_expand_node is None:
            return
        node = _find_node(self._tree, node_name)
        if node is None:
            return
        self._run_expand(
            label=_short(node_name),
            expr=node.expr,
            display=node.name,
            value=node.value,
            type_hint=node.type_hint,
            count=count,
            on_built=lambda rebuilt: _splice_into(node, rebuilt),
        )

    def _expand_field(self, node_name: str, target: LineTarget, count: int) -> None:
        """Promote an inlined field into its own expanded child box."""
        if self._on_expand_node is None:
            return
        node = _find_node(self._tree, node_name)
        if node is None:
            return
        field = _find_field(node, target.expr)
        if field is None:
            self._set_footer("  That field is no longer present in the tree.")
            return
        display = f"{node.name}.{field.name}"
        self._run_expand(
            label=_short(field.name),
            expr=field.expr,
            display=display,
            value=field.value,
            type_hint=field.type_hint,
            count=count,
            on_built=lambda rebuilt: _promote_field_into_child(node, field, rebuilt),
        )

    def _run_expand(
        self,
        *,
        label: str,
        expr: str,
        display: str,
        value: str,
        type_hint: str,
        count: int,
        on_built: Callable[[VarTreeNode], None],
    ) -> None:
        """Shared async driver for title/field expansion."""
        if self._on_expand_node is None:
            return
        # Reuse the restart busy-flag to mutually exclude concurrent rebuilds.
        self._restarting = True
        self._set_footer(f"  Expanding {label} as array of {count}...")

        async def _do_expand() -> None:
            try:
                rebuilt = await self._on_expand_node(expr, display, value, type_hint, count)
                if rebuilt is not None:
                    on_built(rebuilt)
                    self._positioned = compute_layout(self._tree)
                    self._refresh()
                    self._set_footer(f"  {label} expanded to {count} elements.")
                else:
                    self._set_footer(
                        f"  Can't expand {label} (gdb rejected the expression)."
                    )
            except Exception:
                self._set_footer("  Array expansion failed.")
            finally:
                self._restarting = False

        asyncio.ensure_future(_do_expand())

    # ----- close --------------------------------------------------------

    def action_close(self) -> None:
        self.app.pop_screen()


__all__ = ["VariableTreeScreen"]
