import time

from rich.text import Text
from textual.widgets import RichLog

from state import state
from models import Test, Suite
from .styles import TREE_GUIDE_STYLE, OutputBoxRegion
from .labels import suite_label, test_label
from .output import get_test_output, render_output_box


def render_tree(
    log: RichLog,
    output_max_lines: int,
    total_width: int,
) -> list[OutputBoxRegion]:
    rendered_boxes: list[OutputBoxRegion] = []
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
        rendered_boxes.extend(boxes)

    return rendered_boxes


def render_node(
    node: Test | Suite,
    prefix: str,
    is_last: bool,
    log: RichLog,
    now: float,
    tree_line_cursor: int,
    output_max_lines: int,
    total_width: int,
) -> tuple[int, list[OutputBoxRegion]]:
    connector = "└── " if is_last else "├── "
    continuation = "    " if is_last else "│   "
    child_prefix = prefix + continuation

    rendered_boxes: list[OutputBoxRegion] = []

    if isinstance(node, Test):
        guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
        log.write(guide + test_label(node, now))
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
            rendered_boxes.extend(boxes)

    return tree_line_cursor, rendered_boxes
