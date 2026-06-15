"""Pure layout + rendering functions for the variable tree view.

Given a :class:`VarTreeNode` tree (from ``api._variable_tree``), these
functions compute 2-D character-grid positions for every node and render
the result as a Rich :class:`Group` clipped to a viewport with pan offsets.

Layout algorithm (two-pass):
  1. **Post-order** — compute ``subtree_width`` for each node.
  2. **Pre-order** — assign ``(x, y)`` positions; children are laid out
     left-to-right below the parent, parent is centred over children.

Rendering builds a flat ``(char, style)`` grid, draws connectors first,
then node boxes on top, and converts to Rich ``Text`` lines.

All functions are **pure** — same inputs always produce the same output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from rich.console import Group
from rich.text import Text

from api._variable_tree import VarTreeNode, VarField

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

V_GAP = 4   # rows between a parent's bottom border and a child's top border
H_GAP = 4   # columns between adjacent sibling subtrees
MAX_VAL_LEN = 28  # truncate displayed values longer than this

# Style constants
_BORDER_NORMAL = "cyan"
_BORDER_NULL = "dim red"
_BORDER_CYCLE = "yellow"
_STYLE_NAME = "bold cyan"
_STYLE_VAL = "green"
_STYLE_VAL_NULL = "red"
_STYLE_VAL_CYCLE = "yellow"
_STYLE_TYPE = "dim magenta"
_STYLE_FIELD_NAME = "cyan"
_STYLE_FIELD_VAL = "green"
_STYLE_MARKER = "dim"
_STYLE_CONNECTOR = "dim"


# ---------------------------------------------------------------------------
# Positioned node (layout wrapper around VarTreeNode)
# ---------------------------------------------------------------------------


@dataclass
class PositionedNode:
    """A :class:`VarTreeNode` augmented with computed grid coordinates."""

    node: VarTreeNode
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    subtree_width: int = 0
    children: list["PositionedNode"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(value: str, limit: int = MAX_VAL_LEN) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _short_name(name: str) -> str:
    """Show only the last path component for readability."""
    if "." in name:
        return name.rsplit(".", 1)[-1]
    return name


def _content_lines(node: VarTreeNode) -> list[list[tuple[str, str]]]:
    """Build the styled text segments for each content line inside the box."""
    lines: list[list[tuple[str, str]]] = []

    # Line 0: name = value
    val_style = _STYLE_VAL
    if node.is_null:
        val_style = _STYLE_VAL_NULL
    elif node.is_cycle:
        val_style = _STYLE_VAL_CYCLE
    lines.append([(_short_name(node.name) + " = ", _STYLE_NAME),
                  (_truncate(node.value), val_style)])

    # Type hint
    if node.type_hint:
        lines.append([("[" + node.type_hint + "]", _STYLE_TYPE)])

    # Markers
    if node.is_null:
        lines.append([("(null)", _STYLE_MARKER)])
    if node.is_cycle:
        lines.append([("(cycle)", _STYLE_MARKER)])

    # Scalar fields
    for f in node.fields:
        lines.append([(f.name + " = ", _STYLE_FIELD_NAME),
                      (_truncate(f.value), _STYLE_FIELD_VAL)])

    return lines


def _node_width(node: VarTreeNode) -> int:
    lines = _content_lines(node)
    content_w = max((sum(len(text) for text, _ in segs) for segs in lines), default=1)
    return content_w + 4  # +2 padding, +2 borders


def _node_height(node: VarTreeNode) -> int:
    return len(_content_lines(node)) + 2  # +2 top/bottom border


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def compute_layout(root: VarTreeNode) -> PositionedNode:
    """Assign grid positions to every node in the tree.

    Returns a :class:`PositionedNode` tree with ``x``, ``y`` coordinates
    normalised so the minimum is ``(0, 0)``.
    """
    positioned = _build_positioned(root)
    _compute_widths(positioned)
    _assign_positions(positioned, 0, 0)
    _normalize(positioned)
    return positioned


def _build_positioned(node: VarTreeNode) -> PositionedNode:
    pn = PositionedNode(
        node=node,
        width=_node_width(node),
        height=_node_height(node),
    )
    pn.children = [_build_positioned(c) for c in node.children]
    return pn


def _compute_widths(pn: PositionedNode) -> int:
    """Post-order: compute subtree_width for each node."""
    if not pn.children:
        pn.subtree_width = pn.width
        return pn.width

    total = 0
    for child in pn.children:
        total += _compute_widths(child)
    total += H_GAP * max(0, len(pn.children) - 1)
    pn.subtree_width = max(pn.width, total)
    return pn.subtree_width


def _assign_positions(pn: PositionedNode, x: int, y: int) -> None:
    """Pre-order: assign (x, y), children left-to-right, parent centred."""
    pn.y = y

    if not pn.children:
        pn.x = x + (pn.subtree_width - pn.width) // 2
        return

    child_x = x
    child_y = y + pn.height + V_GAP
    for child in pn.children:
        _assign_positions(child, child_x, child_y)
        child_x += child.subtree_width + H_GAP

    # Centre parent over children
    first_cx = pn.children[0].x + pn.children[0].width // 2
    last_cx = pn.children[-1].x + pn.children[-1].width // 2
    center = (first_cx + last_cx) // 2
    pn.x = center - pn.width // 2


def _normalize(root: PositionedNode) -> None:
    all_nodes = _collect(root)
    if not all_nodes:
        return
    min_x = min(pn.x for pn in all_nodes)
    min_y = min(pn.y for pn in all_nodes)
    for pn in all_nodes:
        pn.x -= min_x
        pn.y -= min_y


def _collect(root: PositionedNode) -> list[PositionedNode]:
    result = [root]
    for child in root.children:
        result.extend(_collect(child))
    return result


def _grid_dims(root: PositionedNode) -> tuple[int, int]:
    all_nodes = _collect(root)
    if not all_nodes:
        return (0, 0)
    w = max(pn.x + pn.width for pn in all_nodes)
    h = max(pn.y + pn.height for pn in all_nodes)
    return (w, h)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_tree(
    root: PositionedNode,
    pan_x: int = 0,
    pan_y: int = 0,
    viewport_w: int = 80,
    viewport_h: int = 24,
) -> Group:
    """Render the positioned tree as a Rich ``Group`` clipped to a viewport.

    ``pan_x`` / ``pan_y`` specify the top-left corner of the visible region
    in grid coordinates.  The result always has exactly ``viewport_h`` lines
    (padded with blank lines if the tree is smaller).
    """
    grid_w, grid_h = _grid_dims(root)
    if grid_w == 0 or grid_h == 0:
        return Group(Text("(empty tree)", style="dim"))

    chars: list[list[str]] = [[" "] * grid_w for _ in range(grid_h)]
    styles: list[list[str]] = [[""] * grid_w for _ in range(grid_h)]

    all_nodes = _collect(root)

    # Draw connectors first so node boxes overwrite overlaps
    for pn in all_nodes:
        if pn.children:
            _draw_connectors(chars, styles, pn)

    # Draw node boxes
    for pn in all_nodes:
        _draw_box(chars, styles, pn)

    # Fixup pass: mark each child's top-border centre with ┴ so the
    # incoming connector visibly joins the box (drawn after boxes win).
    for pn in all_nodes:
        for child in pn.children:
            cx = child.x + child.width // 2
            child_node = child.node
            if child_node.is_null:
                border = _BORDER_NULL
            elif child_node.is_cycle:
                border = _BORDER_CYCLE
            else:
                border = _BORDER_NORMAL
            _set(chars, styles, cx, child.y, "\u2534", border)

    # Clip to viewport and convert to Rich Text lines
    lines: list[Text] = []
    for y in range(pan_y, min(grid_h, pan_y + viewport_h)):
        line_chars = []
        line_styles = []
        for x in range(pan_x, min(grid_w, pan_x + viewport_w)):
            line_chars.append(chars[y][x])
            line_styles.append(styles[y][x])
        lines.append(_build_text_line(line_chars, line_styles))

    # Pad to viewport height
    while len(lines) < viewport_h:
        lines.append(Text(""))

    return Group(*lines)


def _build_text_line(chars: list[str], styles: list[str]) -> Text:
    """Group consecutive characters with the same style into Text spans."""
    text = Text(no_wrap=True)
    i = 0
    n = len(chars)
    while i < n:
        st = styles[i]
        j = i + 1
        while j < n and styles[j] == st:
            j += 1
        segment = "".join(chars[i:j])
        text.append(segment, style=st if st else None)
        i = j
    return text


def _set(chars, styles, x: int, y: int, ch: str, style: str) -> None:
    if 0 <= y < len(chars) and 0 <= x < len(chars[y]):
        chars[y][x] = ch
        styles[y][x] = style


# ---------------------------------------------------------------------------
# Connector drawing
# ---------------------------------------------------------------------------


def _draw_connectors(
    chars: list[list[str]],
    styles: list[list[str]],
    pn: PositionedNode,
) -> None:
    parent_cx = pn.x + pn.width // 2
    parent_bottom = pn.y + pn.height - 1
    bus_y = pn.y + pn.height + V_GAP // 2

    child_centers = [c.x + c.width // 2 for c in pn.children]
    if not child_centers:
        return

    bus_left = min(child_centers)
    bus_right = max(child_centers)

    # Vertical drop from parent bottom border
    for y in range(parent_bottom + 1, bus_y):
        _set(chars, styles, parent_cx, y, "\u2502", _STYLE_CONNECTOR)

    # Horizontal bus
    for x in range(bus_left, bus_right + 1):
        _set(chars, styles, x, bus_y, "\u2500", _STYLE_CONNECTOR)

    # Bus intersection: parent joins from above.
    # When parent centre coincides with a child centre, use a 4-way cross.
    _bus_intersection(
        chars, styles, parent_cx, bus_y, bus_left, bus_right,
        from_above=True, has_below=parent_cx in child_centers,
    )

    # Child drops from bus
    child_top = pn.children[0].y
    for cx in child_centers:
        if cx != parent_cx:
            _bus_intersection(chars, styles, cx, bus_y, bus_left, bus_right, from_above=False)
        for y in range(bus_y + 1, child_top):
            _set(chars, styles, cx, y, "\u2502", _STYLE_CONNECTOR)


def _bus_intersection(
    chars, styles, x: int, y: int, left: int, right: int,
    from_above: bool, has_below: bool = False,
) -> None:
    """Draw the correct box-drawing char at a bus intersection point."""
    has_left = x > left
    has_right = x < right
    if has_below and has_left and has_right:
        ch = "\u253c"  # ┼ — four-way cross
    elif has_below and not has_left and not has_right:
        ch = "\u2502"  # │ — straight through
    elif has_left and has_right:
        ch = "\u2534" if from_above else "\u252c"  # ┴ or ┬
    elif has_left and not has_right:
        ch = "\u2518" if from_above else "\u2510"  # ┘ or ┐
    elif has_right and not has_left:
        ch = "\u2514" if from_above else "\u250c"  # └ or ┌
    else:
        ch = "\u2502"  # │ — straight through (single child aligned with parent)
    _set(chars, styles, x, y, ch, _STYLE_CONNECTOR)


# ---------------------------------------------------------------------------
# Node box drawing
# ---------------------------------------------------------------------------


def _draw_box(
    chars: list[list[str]],
    styles: list[list[str]],
    pn: PositionedNode,
) -> None:
    x, y, w, h = pn.x, pn.y, pn.width, pn.height
    node = pn.node

    if node.is_null:
        border = _BORDER_NULL
    elif node.is_cycle:
        border = _BORDER_CYCLE
    else:
        border = _BORDER_NORMAL

    has_children = bool(pn.children)
    center_x = x + w // 2

    # Top border
    _set(chars, styles, x, y, "\u250c", border)  # ┌
    for i in range(1, w - 1):
        _set(chars, styles, x + i, y, "\u2500", border)  # ─
    _set(chars, styles, x + w - 1, y, "\u2510", border)  # ┐

    # Bottom border (with tee if has children)
    _set(chars, styles, x, y + h - 1, "\u2514", border)  # └
    for i in range(1, w - 1):
        ch = "\u252c" if (has_children and x + i == center_x) else "\u2500"
        _set(chars, styles, x + i, y + h - 1, ch, border)
    _set(chars, styles, x + w - 1, y + h - 1, "\u2518", border)  # ┘

    # Content lines
    segments_list = _content_lines(node)
    for row_idx, segments in enumerate(segments_list):
        row = y + 1 + row_idx
        _set(chars, styles, x, row, "\u2502", border)  # │ left
        col = x + 1  # 1-char left padding
        for text, style in segments:
            for ch in text:
                if col < x + w - 1:
                    _set(chars, styles, col, row, ch, style)
                    col += 1
        _set(chars, styles, x + w - 1, row, "\u2502", border)  # │ right


# ---------------------------------------------------------------------------
# Grid info (for scroll/pan bounds)
# ---------------------------------------------------------------------------


def grid_size(root: PositionedNode) -> tuple[int, int]:
    """Return the ``(width, height)`` of the full character grid."""
    return _grid_dims(root)


__all__ = [
    "PositionedNode",
    "compute_layout",
    "render_tree",
    "grid_size",
]
