"""Full-page variable tree view screen.

Opened from the debug variable inspector by pressing **T** on a selected
variable.  Renders the variable's data structure as a visual top-to-bottom
tree with Unicode box-drawing nodes and connector lines.

Interaction
-----------
* **Mouse drag** — pan the view (content follows cursor).
* **Arrow keys / hjkl** — pan.
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
from textual.screen import Screen
from textual.widgets import Static

import state as global_state
from api._variable_tree import VarTreeNode
from .variable_tree_layout import (
    PositionedNode,
    compute_layout,
    render_tree,
    grid_size,
)

PAN_STEP = 4       # cells per arrow-key press


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
    ):
        super().__init__()
        self._tree = tree
        self._var_name = var_name or tree.name
        self._on_restart = on_restart
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

    # ----- lifecycle ----------------------------------------------------

    def compose(self) -> ComposeResult:
        header_text = f"  Variable Tree: {self._var_name}"
        yield Static(header_text, id="vtree-header", markup=False)
        with Container(id="vtree-container"):
            yield Static("", id="vtree-canvas", markup=False)
        yield Static(
            "  drag=pan   \u2190\u2192\u2191\u2193=pan   r=restart   v/Esc=close",
            id="vtree-footer",
            markup=False,
        )

    def on_mount(self) -> None:
        self._canvas = self.query_one("#vtree-canvas", Static)
        self._footer_widget = self.query_one("#vtree-footer", Static)
        self._positioned = compute_layout(self._tree)
        self._reset_footer()
        self._refresh()

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

        group = render_tree(
            self._positioned,
            pan_x=self._pan_x,
            pan_y=self._pan_y,
            viewport_w=vw,
            viewport_h=vh,
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
        self._set_footer(
            f"  drag=pan   \u2190\u2192\u2191\u2193=pan   r=restart   v/Esc=close{indicator}"
        )

    # ----- mouse drag pan -----------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._drag_origin = (event.x, event.y)
        self._drag_pan_origin = (self._pan_x, self._pan_y)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        dx = event.x - self._drag_origin[0]
        dy = event.y - self._drag_origin[1]
        self._pan_x = max(0, min(self._max_pan_x, self._drag_pan_origin[0] - dx))
        self._pan_y = max(0, min(self._max_pan_y, self._drag_pan_origin[1] - dy))
        self._refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False

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

    # ----- close --------------------------------------------------------

    def action_close(self) -> None:
        self.app.pop_screen()


__all__ = ["VariableTreeScreen"]
