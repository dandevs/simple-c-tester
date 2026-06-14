import time

from rich.console import Console
from rich.text import Text
from textual.widgets import RichLog

from state import state
from core.models import Test, Suite
from .styles import TREE_GUIDE_STYLE, OutputBoxRegion, TestRowRegion, SuiteRowRegion
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


# ---------------------------------------------------------------------------
# Search predicates (pure functions)
# ---------------------------------------------------------------------------


def _test_matches(test: Test, query: str) -> bool:
    """True if ``test`` should be shown given ``query`` (case-insensitive)."""
    return not query or query.lower() in test.name.lower()


def _suite_has_matches(suite: Suite, query: str) -> bool:
    """True if ``suite`` or any descendant matches ``query``."""
    if not query:
        return True
    if query.lower() in suite.name.lower():
        return True
    return (
        any(_test_matches(t, query) for t in suite.tests)
        or any(_suite_has_matches(c, query) for c in suite.children)
    )


def _suite_key(parent_key: str, suite_name: str) -> str:
    """Build a stable hierarchical key for a suite (for fold state)."""
    return f"{parent_key}/{suite_name}" if parent_key else suite_name


def _visible_children(
    node: Suite, query: str
) -> list[Test | Suite]:
    """Return tests + child suites that pass the search filter, in tree order."""
    tests = [t for t in node.tests if _test_matches(t, query)]
    children = [c for c in node.children if _suite_has_matches(c, query)]
    return tests + children


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_tree(
    log: RichLog,
    output_max_lines: int,
    total_width: int,
    collapsed_suites: set[str] | None = None,
    search_query: str = "",
) -> tuple[list[OutputBoxRegion], list[TestRowRegion], list[SuiteRowRegion]]:
    """Render the full test tree.

    Returns ``(output_box_regions, test_row_regions, suite_row_regions)``.
    Suite rows are clickable headers used for fold toggling.
    """
    collapsed = collapsed_suites or set()
    query = search_query.strip()

    rendered_boxes: list[OutputBoxRegion] = []
    rendered_test_rows: list[TestRowRegion] = []
    rendered_suite_rows: list[SuiteRowRegion] = []

    ctx = _Ctx(
        log=log,
        now=time.monotonic(),
        output_max_lines=output_max_lines,
        total_width=total_width,
        collapsed=collapsed,
        query=query,
        boxes=rendered_boxes,
        test_rows=rendered_test_rows,
        suite_rows=rendered_suite_rows,
    )

    root = state.root_suite
    label = suite_label(root, ctx.now, collapsed=False)
    log.write(label)
    cursor = 1

    children = _visible_children(root, query)
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        cursor = _render_node(child, "", "", is_last, ctx, cursor)
        if isinstance(child, Suite) and not is_last:
            ctx.log.write(Text())
            cursor += 1

    return rendered_boxes, rendered_test_rows, rendered_suite_rows


class _Ctx:
    """Mutable render context passed through the recursion (avoids 10+ params)."""

    __slots__ = (
        "log", "now", "output_max_lines", "total_width",
        "collapsed", "query", "boxes", "test_rows", "suite_rows",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])


def _render_node(
    node: Test | Suite,
    prefix: str,
    parent_suite_key: str,
    is_last: bool,
    ctx: _Ctx,
    cursor: int,
) -> int:
    """Render one node; return the updated line cursor."""
    connector = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
    continuation = "    " if is_last else "\u2502   "
    child_prefix = prefix + continuation

    if isinstance(node, Test):
        return _render_test(node, prefix, connector, child_prefix, ctx, cursor)
    return _render_suite(node, prefix, connector, child_prefix, parent_suite_key, is_last, ctx, cursor)


def _render_test(
    test: Test,
    prefix: str,
    connector: str,
    child_prefix: str,
    ctx: _Ctx,
    cursor: int,
) -> int:
    guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
    label = test_label(test, ctx.now, ctx.query)
    row = guide + label
    ctx.log.write(row)
    ctx.test_rows.append(
        TestRowRegion(
            test_key=test.source_path,
            line=cursor,
            left_col=0,
            right_col=max(0, len(row.plain) - 1),
        )
    )
    cursor += 1

    output = get_test_output(test)
    if output:
        start_line = cursor
        render_meta = render_output_box(
            output, test, child_prefix, ctx.log,
            ctx.output_max_lines, ctx.total_width,
        )
        cursor += render_meta.rendered_lines
        ctx.boxes.append(
            OutputBoxRegion(
                test_key=test.source_path,
                start_line=start_line,
                end_line=start_line + render_meta.rendered_lines - 1,
                left_col=render_meta.left_col,
                right_col=render_meta.right_col,
            )
        )
    return cursor


def _render_suite(
    suite: Suite,
    prefix: str,
    connector: str,
    child_prefix: str,
    parent_suite_key: str,
    is_last: bool,
    ctx: _Ctx,
    cursor: int,
) -> int:
    suite_key = _suite_key(parent_suite_key, suite.name)
    is_collapsed = suite_key in ctx.collapsed

    guide = Text(prefix + connector, style=TREE_GUIDE_STYLE)
    label = suite_label(suite, ctx.now, collapsed=is_collapsed)
    row = guide + label
    ctx.log.write(row)

    # Track the suite name portion for click detection (exclude guide).
    name_start = len(prefix + connector)
    ctx.suite_rows.append(
        SuiteRowRegion(
            suite_key=suite_key,
            line=cursor,
            left_col=name_start,
            right_col=max(name_start, len(row.plain) - 1),
        )
    )
    cursor += 1

    if is_collapsed:
        return cursor

    children = _visible_children(suite, ctx.query)
    for i, child in enumerate(children):
        child_is_last = i == len(children) - 1
        cursor = _render_node(
            child, child_prefix, suite_key, child_is_last, ctx, cursor
        )
    return cursor
