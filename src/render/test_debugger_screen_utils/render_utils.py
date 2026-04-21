import os
import re

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text

import state as global_state
from .source_utils import display_path, detect_language, load_source_lines

_C_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "return", "goto", "sizeof", "typeof",
    "int", "char", "float", "double", "void", "long", "short",
    "signed", "unsigned", "const", "static", "extern", "inline",
    "struct", "union", "enum", "typedef", "volatile", "register",
    "auto", "restrict", "_Bool", "_Complex", "_Imaginary",
    "NULL", "true", "false",
}

_VAR_EXPR_RE = re.compile(r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*")


def _normalize_expr(expr: str) -> str:
    """Normalize a C expression for lookup (e.g. table->count -> table.count)."""
    expr = re.sub(r"\s*->\s*", ".", expr)
    expr = re.sub(r"\s*\.\s*", ".", expr)
    return expr


def _extract_variable_expressions(line: str) -> list[str]:
    """Extract potential variable/member expressions from a C source line."""
    seen: set[str] = set()
    expressions: list[str] = []
    for match in _VAR_EXPR_RE.finditer(line):
        expr = match.group(0)
        normalized = _normalize_expr(expr)
        root = normalized.split(".")[0]
        if root in _C_KEYWORDS:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        expressions.append(expr)
    return expressions


def _build_line_annotations(line: str, variables: list[tuple[str, str]]) -> str:
    """Build inline annotation string for a source line, e.g. '[table.count=5] [count=5]'."""
    if not variables:
        return ""

    expressions = _extract_variable_expressions(line)
    if not expressions:
        return ""

    var_map = {}
    for name, value in variables:
        var_map[_normalize_expr(name)] = value

    annotations: list[str] = []
    seen: set[str] = set()
    for expr in expressions:
        normalized = _normalize_expr(expr)
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized in var_map:
            value = var_map[normalized]
            display_value = value if len(value) <= 40 else value[:37] + "..."
            annotations.append(f"[{expr}={display_value}]")

    return " ".join(annotations)


def _build_resolved_annotations(
    resolved_annotations: list[tuple[str, str, str]],
) -> str:
    """Build inline annotation string from resolver output for current frame."""
    if not resolved_annotations:
        return ""

    annotations: list[str] = []
    seen: set[str] = set()
    for name, value, _availability in resolved_annotations:
        if not name or name in seen:
            continue
        seen.add(name)
        display_value = value if len(value) <= 40 else value[:37] + "..."
        annotations.append(f"[{name}={display_value}]")

    return " ".join(annotations)


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
    variables=None,
    resolved_annotations=None,
):
    padded_width = max(1, code_width)
    snippet_lines = []
    for line_no in range(snippet_start, snippet_end + 1):
        line_text = source_lines[line_no - 1]
        if resolved_annotations is not None:
            annotation = (
                _build_resolved_annotations(resolved_annotations)
                if line_no == line_number
                else ""
            )
        else:
            annotation = _build_line_annotations(line_text, variables or [])
        if annotation:
            line_text = f"{line_text}  {annotation}"
        snippet_lines.append(line_text.ljust(padded_width))
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

    if event.trigger_label:
        badge_style = f"bold {STORY_META_SELECTED}" if selected else "#9cb9c7"
        title.append("  [", style=STORY_HELP if selected else "#7f868d")
        title.append(event.trigger_label, style=badge_style)
        title.append("]",
            style=STORY_HELP if selected else "#7f868d",
        )

    if event.trigger_message and bool(global_state.tsv_show_reason_about):
        detail_style = STORY_HELP if selected else "#7f868d"
        title.append(f"  {event.trigger_message}", style=detail_style)

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
    renderables = [_build_timeline_progress_bar(total, selected_frame_index, start_index, end_index, width)]
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
        code_width = max(1, width - (number_width + 3))
        snippet_text = build_frame_snippet(
            source_path,
            source_lines,
            line_number,
            snippet_start,
            snippet_end,
            selected,
            code_width,
            variables=event.variables,
            resolved_annotations=event.resolved_annotations,
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


def _build_timeline_progress_bar(total, selected_index, start_index, end_index, width):
    if width <= 0:
        return Text()

    selected = max(0, min(total - 1, selected_index)) if total > 0 else 0
    visible_start = max(0, start_index)
    visible_end = max(visible_start, end_index - 1)

    def _idx_to_col(index):
        if total <= 1:
            return 0
        ratio = index / (total - 1)
        return max(0, min(width - 1, int(round(ratio * (width - 1)))))

    selected_col = _idx_to_col(selected)
    window_start_col = _idx_to_col(visible_start)
    window_end_col = _idx_to_col(visible_end)

    bar = Text()
    for col in range(width):
        if col == selected_col:
            bar.append("◆", style=f"bold {STORY_META_SELECTED}")
        elif window_start_col <= col <= window_end_col:
            bar.append("━", style="#6ea8fe")
        else:
            bar.append("─", style=STORY_BAR_BASE)

    return bar


def render_full_file_panel(
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

    selected = selected_frame_index
    if selected < 0 or selected >= total:
        selected = total - 1
    event = frames[selected]

    source_path = os.path.abspath(event.file_path)
    source_lines = load_source_lines(source_path, source_cache)
    if not source_lines:
        code_widget.update(Text("Source unavailable for selected frame.", style=STORY_HELP))
        return

    line_number = max(1, min(len(source_lines), int(event.line)))
    width = max(8, code_widget.size.width - 2)
    available_height = max(3, code_widget.size.height)
    code_height = max(1, available_height - 1)

    half = code_height // 2
    snippet_start = max(1, line_number - half)
    snippet_end = min(len(source_lines), snippet_start + code_height - 1)
    if (snippet_end - snippet_start + 1) < code_height:
        snippet_start = max(1, snippet_end - code_height + 1)

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
        variables=event.variables,
        resolved_annotations=event.resolved_annotations,
    )

    title = Text()
    title.append("Full File ", style=f"bold {STORY_META_SELECTED}")
    title.append(display_path(source_path), style=f"bold {STORY_META_SELECTED}")
    title.append(":", style=STORY_HELP)
    title.append(str(line_number), style=STORY_META_HIGHLIGHT)
    if event.function:
        title.append(f"  fn={event.function}", style=STORY_HELP)

    code_widget.update(Group(title, snippet))


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
