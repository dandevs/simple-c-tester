import os

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text

import state as global_state
from .source_utils import display_path, detect_language, load_source_lines


STORY_META_HIGHLIGHT = "#89dceb"
STORY_META_SELECTED = "#ffd166"
STORY_HELP = "#7f8a9d"
STORY_CODE_BG = "#272822"
STORY_CURRENT_LINE = "#34352d"
STORY_CURRENT_LINE_SELECTED = "#49483e"
STORY_BAR_BASE = "#2e3440"


def build_frame_snippet(
    source_path,
    source_lines,
    line_number,
    snippet_start,
    snippet_end,
    selected,
    code_width,
):
    padded_width = max(1, code_width)
    snippet_lines = [
        source_lines[line_no - 1].ljust(padded_width)
        for line_no in range(snippet_start, snippet_end + 1)
    ]
    snippet_text = "\n".join(snippet_lines)
    line_count = len(snippet_lines)
    syntax = Syntax(
        snippet_text,
        detect_language(source_path),
        line_numbers=True,
        start_line=snippet_start,
        highlight_lines={line_number},
        code_width=padded_width,
        word_wrap=False,
        theme="monokai",
        background_color=STORY_CODE_BG,
    )

    if not selected:
        for local_line in range(1, line_count + 1):
            syntax.stylize_range(
                "#9aa0a6",
                (local_line, 0),
                (local_line, padded_width),
            )

    local_line = (line_number - snippet_start) + 1
    line_length = padded_width
    highlight_bg = STORY_CURRENT_LINE_SELECTED if selected else STORY_CURRENT_LINE
    syntax.stylize_range(
        f"on {highlight_bg}",
        (local_line, 0),
        (local_line, line_length),
    )
    if selected:
        syntax.stylize_range(
            "bold",
            (local_line, 0),
            (local_line, line_length),
        )

    return syntax


def build_frame_title(event, selected):
    path_text = display_path(os.path.abspath(event.file_path))
    line_number = event.line

    title = Text()
    if selected:
        title.append(">> ", style=f"bold {STORY_META_SELECTED}")
        title.append(path_text, style=f"bold {STORY_META_SELECTED}")
    else:
        title.append("   ", style="#7f868d")
        title.append(path_text, style="#95a3aa")
    title.append(":")
    if selected:
        title.append(str(line_number), style=STORY_META_HIGHLIGHT)
    else:
        title.append(str(line_number), style="#95a3aa")
    if event.function:
        if selected:
            title.append(f"  fn={event.function}", style=STORY_HELP)
        else:
            title.append(f"  fn={event.function}", style="#7f868d")

    return title


def render_code_panel(
    code_widget,
    frames,
    selected_frame_index,
    source_cache,
):
    if code_widget is None:
        return

    total = len(frames)
    if total == 0:
        hint = Text()
        hint.append("No Test Story frames yet. ", style=STORY_HELP)
        hint.append("Recording is on. ", style=STORY_META_HIGHLIGHT)
        hint.append(
            "Press R to run and capture a story.", style=f"bold {STORY_META_SELECTED}"
        )
        code_widget.update(hint)
        return

    code_widget_height = code_widget.size.height
    start_index, end_index = _compute_frame_cards_window(
        selected_frame_index, total, code_widget_height
    )
    width = max(8, code_widget.size.width - 2)
    renderables = []
    lines_above = max(0, int(global_state.tsv_lines_above))
    lines_below = max(0, int(global_state.tsv_lines_below))

    for index in range(start_index, end_index):
        event = frames[index]
        source_path = os.path.abspath(event.file_path)
        source_lines = load_source_lines(source_path, source_cache)
        if not source_lines:
            continue

        line_number = event.line
        snippet_start = max(1, line_number - lines_above)
        snippet_end = min(len(source_lines), line_number + lines_below)
        selected = index == selected_frame_index

        title = build_frame_title(event, selected)

        number_width = len(str(max(1, snippet_end)))
        code_width = max(1, width - (number_width + 5))
        snippet_text = build_frame_snippet(
            source_path,
            source_lines,
            line_number,
            snippet_start,
            snippet_end,
            selected,
            code_width,
        )

        renderables.append(title)
        renderables.append(snippet_text)
        if index < end_index - 1:
            sep_style = STORY_BAR_BASE if selected else "#3a3f4b"
            renderables.append(Text("-" * width, style=sep_style))

    if not renderables:
        code_widget.update(Text("No renderable story frames.", style=STORY_HELP))
        return

    code_widget.update(Group(*renderables))


def _compute_frame_cards_window(selected_frame_index, total, height):
    if total <= 0:
        return (0, 0)

    lines_above = max(0, int(global_state.tsv_lines_above))
    lines_below = max(0, int(global_state.tsv_lines_below))
    code_line_count = 1 + lines_above + lines_below
    card_height = 1 + code_line_count
    card_count = max(1, (height + 1) // (card_height + 1))
    card_count = min(total, card_count)

    center = selected_frame_index
    start = max(0, center - (card_count // 2))
    end = start + card_count
    if end > total:
        end = total
        start = max(0, end - card_count)
    return (start, end)


def build_variables_tree(vars_list, vars_tree_widget, vars_widget):
    if not vars_list:
        vars_widget.update(
            Text("Variables (none captured for this frame)", style=STORY_HELP)
        )
        tree = vars_tree_widget
        tree.root.set_label("Variables")
        tree.root.remove_children()
        tree.root.expand()
        tree.refresh()
        return None

    class _Node:
        __slots__ = ("name", "value", "children")

        def __init__(self, name: str):
            self.name = name
            self.value = ""
            self.children: dict[str, "_Node"] = {}

    root: dict[str, _Node] = {}
    for full_name, value in vars_list:
        parts = [part for part in full_name.split(".") if part]
        if not parts:
            continue

        head = parts[0]
        node = root.get(head)
        if node is None:
            node = _Node(head)
            root[head] = node

        current = node
        for part in parts[1:]:
            nxt = current.children.get(part)
            if nxt is None:
                nxt = _Node(part)
                current.children[part] = nxt
            current = nxt

        current.value = value if value not in {"", "{...}"} else "?"

    vars_widget.update(
        Text.assemble(
            ("Variables", f"bold {STORY_META_SELECTED}"),
            (f" ({len(root)} roots)", STORY_HELP),
        )
    )

    tree = vars_tree_widget
    tree.root.set_label("Variables")
    tree.root.remove_children()
    tree.root.expand()

    def _label(node: _Node) -> Text:
        label = Text()
        label.append(node.name, style=STORY_META_HIGHLIGHT)
        if node.value:
            val = node.value
            if len(val) > 80:
                val = val[:77] + "..."
            label.append(" = ", style=STORY_HELP)
            label.append(val, style="#f8f8f2")
        return label

    def _append(tree_node, item: _Node) -> None:
        allow_expand = bool(item.children)
        label = _label(item)
        child_tree = tree_node.add(
            label,
            expand=True,
            allow_expand=allow_expand,
        )
        for name in sorted(item.children.keys()):
            _append(child_tree, item.children[name])

    for name in sorted(root.keys()):
        _append(tree.root, root[name])

    tree.root.expand_all()
    tree.refresh()

    return tuple(vars_list)
