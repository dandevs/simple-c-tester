import time

from rich.console import Console
from rich.text import Text
from textual.widgets import RichLog

from state import state
from models import Test, Suite
from .styles import TREE_GUIDE_STYLE, OutputBoxRegion, TestRowRegion
from .labels import suite_label, test_label
from .output import get_test_output, render_output_box


class _ConsoleApp:
    def __init__(self, console: Console):
        self.console = console


class ConsoleWriter:
    def __init__(self, console: Console):
        self.console = console
        self.app = _ConsoleApp(console)

    def write(self, text):
        self.console.print(text)


def render_tree_stdout(output_max_lines: int, width: int):
    console = Console()
    writer = ConsoleWriter(console)
    render_tree(writer, output_max_lines, width)


def render_tree(
    log: RichLog,
    output_max_lines: int,
    total_width: int,
) -> tuple[list[OutputBoxRegion], list[TestRowRegion]]:
    rendered_boxes: list[OutputBoxRegion] = []
    rendered_rows: list[TestRowRegion] = []
    tree_line_cursor = 0

    root = state.root_suite
    label = suite_label(root, time.monotonic())
    log.write(label)
    tree_line_cursor += 1

    children = list(root.tests) + list(root.children)
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        tree_line_cursor, boxes = render_node(
            child,
            "",
            is_last,
            log,
            time.monotonic(),
            tree_line_cursor,
            output_max_lines,
            total_width,
        )
        rendered_rows.extend(boxes[1])
        rendered_boxes.extend(boxes[0])

    return rendered_boxes, rendered_rows


def render_node(
    node: Test | Suite,
    prefix: str,
    is_last: bool,
    log: RichLog,
    now: float,
    tree_line_cursor: int,
    output_max_lines: int,
    total_width: int,
) -> tuple[int, tuple[list[OutputBoxRegion], list[TestRowRegion]]]:
    connector = "└── " if is_last else "├── "
    continuation = "    " if is_last else "│   "
    child_prefix = prefix + continuation

    rendered_boxes: list[OutputBoxRegion] = []
    rendered_rows: list[TestRowRegion] = []

    if isinstance(node, Test):
        guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
        label = test_label(node, now)
        row = guide + label
        log.write(row)
        rendered_rows.append(
            TestRowRegion(
                test_key=node.source_path,
                line=tree_line_cursor,
                left_col=0,
                right_col=max(0, len(row.plain) - 1),
            )
        )
        tree_line_cursor += 1

        output = get_test_output(node)
        if output:
            start_line = tree_line_cursor
            render_meta = render_output_box(
                output,
                node,
                child_prefix,
                log,
                output_max_lines,
                total_width,
            )
            tree_line_cursor += render_meta.rendered_lines

            rendered_boxes.append(
                OutputBoxRegion(
                    test_key=node.source_path,
                    start_line=start_line,
                    end_line=start_line + render_meta.rendered_lines - 1,
                    left_col=render_meta.left_col,
                    right_col=render_meta.right_col,
                )
            )
    else:
        guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
        log.write(guide + suite_label(node, now))
        tree_line_cursor += 1

        children = list(node.tests) + list(node.children)
        for i, child in enumerate(children):
            is_child_last = i == len(children) - 1
            tree_line_cursor, boxes = render_node(
                child,
                child_prefix,
                is_child_last,
                log,
                now,
                tree_line_cursor,
                output_max_lines,
                total_width,
            )
            rendered_boxes.extend(boxes[0])
            rendered_rows.extend(boxes[1])

    return tree_line_cursor, (rendered_boxes, rendered_rows)
