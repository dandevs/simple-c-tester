import os

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text

import state as global_state
from runner.story_annotations import get_story_annotations
from .source_utils import display_path, detect_language, load_source_lines

STORY_META_HIGHLIGHT = "cyan"
STORY_META_SELECTED = "yellow"
STORY_HELP = "dim"
STORY_CODE_BG = "#272822"
STORY_CURRENT_LINE = "#34352d"
STORY_CURRENT_LINE_SELECTED = "#49483e"
STORY_BAR_BASE = "dim"


def build_frame_snippet(
    source_path,
    source_lines,
    line_number,
    snippet_start,
    snippet_end,
    selected,
    code_width,
    line_annotations=None,
    breakpoint_lines=None,
    covered_lines=None,
):
    """Build a syntax-highlighted snippet with pre-computed inline annotations."""
    padded_width = max(1, code_width)
    snippet_lines = []
    for line_no in range(snippet_start, snippet_end + 1):
        line_text = source_lines[line_no - 1]
        annotation = ""
        annotation_strs = line_annotations.get(line_no, []) if line_annotations else []
        if annotation_strs:
            annotation = " ".join(annotation_strs)
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
                "dim",
                (local_line, 0),
                (local_line, padded_width),
            )

    # Feature F: tint breakpoint lines (subtle dark-red) before the current-line
    # highlight so the active line keeps its own background.
    bp_lines = breakpoint_lines or set()
    for bp_line in bp_lines:
        if snippet_start <= bp_line <= snippet_end:
            local_bp = (bp_line - snippet_start) + 1
            syntax.stylize_range(
                "on #3a1e1e",
                (local_bp, 0),
                (local_bp, padded_width),
            )

    # Feature K: coverage overlay — dim uncovered lines to bright_black (dark
    # gray) when coverage data is provided.  Covered lines stay at normal
    # brightness so they stand out.  Applied before the current-line highlight
    # so the active line keeps its own background.
    if covered_lines is not None:
        for snip_line in range(snippet_start, snippet_end + 1):
            if snip_line not in covered_lines:
                local = (snip_line - snippet_start) + 1
                syntax.stylize_range(
                    "bright_black",
                    (local, 0),
                    (local, padded_width),
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
        title.append("\u25b6 ", style=f"bold {STORY_META_SELECTED}")
        title.append(path_text, style=f"bold {STORY_META_HIGHLIGHT}")
    else:
        title.append("  ", style="dim")
        title.append(path_text, style="dim")
    title.append(":", style="dim")
    if selected:
        title.append(str(line_number), style=STORY_META_HIGHLIGHT)
    else:
        title.append(str(line_number), style="dim")
    if event.function:
        if selected:
            title.append(f"  fn={event.function}", style=STORY_HELP)
        else:
            title.append(f"  fn={event.function}", style="dim")

    if event.trigger_label:
        badge_style = f"bold {STORY_META_SELECTED}" if selected else "dim"
        title.append("  [", style=STORY_HELP)
        title.append(event.trigger_label, style=badge_style)
        title.append("]", style=STORY_HELP)

    if event.trigger_message and bool(global_state.tsv_show_reason_about):
        detail_style = STORY_HELP if selected else "dim"
        title.append(f"  {event.trigger_message}", style=detail_style)

    return title


def render_code_panel(
    code_widget,
    frames,
    selected_frame_index,
    source_cache,
    test,
):
    """Render the card-based code panel with per-card annotation history."""
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
        selected = index == selected_frame_index

        if event.kind == "test_failed":
            fail_text = Text(event.message, style="bold red")
            renderables.append(fail_text)
            if index < end_index - 1:
                renderables.append(Text("\u2500" * width, style="dim"))
            continue

        source_path = os.path.abspath(event.file_path)
        source_lines = load_source_lines(source_path, source_cache)
        if not source_lines:
            continue

        line_number = event.line
        snippet_start = max(1, line_number - lines_above)
        snippet_end = min(len(source_lines), line_number + lines_below)

        title = build_frame_title(event, selected)

        number_width = len(str(max(1, snippet_end)))
        code_width = max(1, width - (number_width + 3))
        # Each card shows its own accumulated annotation history up to this event
        card_annotations = get_story_annotations(test, event_boundary=event.index, cache=test.dwarf_cache)
        file_annotations = card_annotations.get(source_path, {}) if card_annotations else {}
        snippet_text = build_frame_snippet(
            source_path,
            source_lines,
            line_number,
            snippet_start,
            snippet_end,
            selected,
            code_width,
            line_annotations=file_annotations,
        )

        renderables.append(title)
        renderables.append(snippet_text)
        if index < end_index - 1:
            renderables.append(Text("\u2500" * width, style="dim"))

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
            bar.append("\u25c6", style=f"bold {STORY_META_SELECTED}")
        elif window_start_col <= col <= window_end_col:
            bar.append("\u2501", style="blue")
        else:
            bar.append("\u2500", style="dim")

    return bar


def render_full_file_panel(
    code_widget,
    frames,
    selected_frame_index,
    source_cache,
    annotations=None,
    active_breakpoints=None,
    covered_lines=None,
):
    """Render the full-file code panel using pre-computed annotations.

    Returns ``(source_path, snippet_start, snippet_end)`` when a source snippet
    is rendered (used for click-to-toggle breakpoint mapping), otherwise None.
    """
    if code_widget is None:
        return None

    total = len(frames)
    if total == 0:
        hint = Text()
        hint.append("No Test Story frames yet. ", style=STORY_HELP)
        hint.append("Recording is on. ", style=STORY_META_HIGHLIGHT)
        hint.append(
            "Press R to run and capture a story.", style=f"bold {STORY_META_SELECTED}"
        )
        code_widget.update(hint)
        return None

    selected = selected_frame_index
    if selected < 0 or selected >= total:
        selected = total - 1
    event = frames[selected]

    if event.kind == "test_failed":
        code_widget.update(Text(event.message, style="bold red"))
        return None

    source_path = os.path.abspath(event.file_path)
    source_lines = load_source_lines(source_path, source_cache)
    if not source_lines:
        code_widget.update(Text("Source unavailable for selected frame.", style=STORY_HELP))
        return None

    # Feature K: per-file covered line set for the coverage overlay.
    file_covered = set()
    if covered_lines is not None:
        file_covered = covered_lines.get(source_path, set())

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
    file_annotations = annotations.get(source_path, {}) if annotations else {}
    # Feature F: breakpoint lines for the currently shown source file.
    bp_set = active_breakpoints or set()
    bp_lines = {line for (path, line) in bp_set if os.path.abspath(path) == source_path}
    snippet = build_frame_snippet(
        source_path,
        source_lines,
        line_number,
        snippet_start,
        snippet_end,
        True,
        code_width,
        line_annotations=file_annotations,
        breakpoint_lines=bp_lines,
        covered_lines=file_covered if covered_lines is not None else None,
    )

    title = Text()
    title.append("\u25b6 ", style=f"bold {STORY_META_SELECTED}")
    title.append("Full File ", style=f"bold {STORY_META_SELECTED}")
    title.append(display_path(source_path), style=f"bold {STORY_META_HIGHLIGHT}")
    title.append(":", style="bright_black")
    title.append(str(line_number), style=STORY_META_HIGHLIGHT)
    if event.function:
        title.append(f"  fn={event.function}", style="bright_black")
    if bp_lines:
        title.append(f"  \u25cf{len(bp_lines)} bp", style="bold red")
    if covered_lines is not None:
        total_lines = snippet_end - snippet_start + 1
        hit = len(file_covered & set(range(snippet_start, snippet_end + 1)))
        title.append(f"  cov {hit}/{total_lines}", style="green")
    title.append("  (click a line to toggle breakpoint)", style="bright_black")

    code_widget.update(Group(title, snippet))
    return (source_path, snippet_start, snippet_end)


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
        __slots__ = ("name", "value", "type_hint", "children")

        def __init__(self, name: str):
            self.name = name
            self.value = ""
            self.type_hint = ""
            self.children: dict[str, "_Node"] = {}

    root: dict[str, _Node] = {}
    for var_tuple in vars_list:
        if len(var_tuple) >= 3:
            full_name, value, _type_hint = var_tuple
        else:
            full_name, value = var_tuple
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
        current.type_hint = _type_hint if len(var_tuple) >= 3 else ""

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
        label.append(node.name, style="cyan")
        if node.value:
            val = node.value
            if len(val) > 80:
                val = val[:77] + "..."
            label.append(" = ", style="dim")
            label.append(val, style="default")
        if node.type_hint:
            label.append(f" [{node.type_hint}]", style="dim")
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
