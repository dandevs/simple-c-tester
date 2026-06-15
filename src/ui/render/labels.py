import time

from rich.text import Text

from core.models import Test, Suite, TestState
from .styles import (
    SUITE_LABEL_STYLE,
    SUITE_FOLD_STYLE,
    TEST_PASSED_STYLE,
    TEST_FAILED_STYLE,
    TEST_RUNNING_STYLE,
    TEST_PENDING_STYLE,
    TEST_DEFAULT_STYLE,
    TREE_META_STYLE,
    SEARCH_HIGHLIGHT_STYLE,
    ICON_PASS,
    ICON_FAIL,
    ICON_PENDING,
    SUITE_HEADER_SEPARATOR,
    BADGE_PASS_STYLE,
    BADGE_FAIL_STYLE,
)


def test_elapsed_seconds(test: Test, now: float) -> float:
    if test.time_start <= 0:
        return 0.0

    if test.state in (TestState.PASSED, TestState.FAILED):
        end_time = test.time_state_changed or now
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
        end_time = now
    else:
        return 0.0

    return max(0.0, end_time - test.time_start)


def suite_elapsed_seconds(suite: Suite, now: float) -> float:
    total = sum(test_elapsed_seconds(test, now) for test in suite.tests)
    for child in suite.children:
        total += suite_elapsed_seconds(child, now)
    return total


def suite_counts(suite: Suite) -> tuple[int, int]:
    """Return (passed, failed) counts recursively."""
    passed = sum(1 for t in suite.tests if t.state == TestState.PASSED)
    failed = sum(1 for t in suite.tests if t.state == TestState.FAILED)
    for child in suite.children:
        cp, cf = suite_counts(child)
        passed += cp
        failed += cf
    return passed, failed


def suite_label(suite: Suite, now: float, collapsed: bool = False) -> Text:
    elapsed_ms = int(suite_elapsed_seconds(suite, now) * 1000)
    indicator = Text("\u25b8 " if collapsed else "\u25be ", style=SUITE_FOLD_STYLE)
    text = Text(suite.name, style=SUITE_LABEL_STYLE)
    text.append(" \u2500\u2500 ", style=SUITE_HEADER_SEPARATOR)

    passed, failed = suite_counts(suite)
    if passed:
        text.append(f"{ICON_PASS} {passed}  ", style=BADGE_PASS_STYLE)
    if failed:
        text.append(f"{ICON_FAIL} {failed}  ", style=BADGE_FAIL_STYLE)
    text.append(f"{elapsed_ms}ms", style=TREE_META_STYLE)
    return indicator + text


def highlight_search(text: Text, query: str) -> Text:
    """Return a copy of ``text`` with all case-insensitive matches of
    ``query`` highlighted.  If ``query`` is empty, returns ``text`` unchanged.
    """
    if not query:
        return text
    result = text.copy()
    plain = result.plain.lower()
    lower_query = query.lower()
    pos = 0
    while True:
        idx = plain.find(lower_query, pos)
        if idx == -1:
            break
        result.stylize(SEARCH_HIGHLIGHT_STYLE, idx, idx + len(query))
        pos = idx + len(query)
    return result


def test_label(test: Test, now: float, search_query: str = "") -> Text:
    label = _test_label_base(test, now)
    return highlight_search(label, search_query) if search_query else label


def _test_label_base(test: Test, now: float) -> Text:
    elapsed_seconds = test_elapsed_seconds(test, now)
    elapsed_ms = int(elapsed_seconds * 1000)
    spinner_frames = ("\u280b", "\u2819", "\u2839", "\u2838", "\u283c",
                      "\u2834", "\u2826", "\u2827", "\u2807", "\u280f")
    spinner = spinner_frames[int(now * 12) % len(spinner_frames)]

    if test.state == TestState.PENDING:
        text = Text(f"{ICON_PENDING} ", style=TEST_PENDING_STYLE)
        text.append(test.name, style=TEST_PENDING_STYLE)
        text.append(" [pending]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.RUNNING and test.time_start <= 0:
        text = Text(f"{spinner} ", style=TEST_RUNNING_STYLE)
        text.append(test.name, style=TEST_RUNNING_STYLE)
        text.append(" [compiling]", style=TREE_META_STYLE)
        return text
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
        text = Text(f"{spinner} ", style=TEST_RUNNING_STYLE)
        text.append(test.name, style=TEST_RUNNING_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.PASSED:
        text = Text(f"{ICON_PASS} ", style=TEST_PASSED_STYLE)
        text.append(test.name, style=TEST_PASSED_STYLE)
        timing_note = f"[{elapsed_ms}ms]"
        timing_style = TREE_META_STYLE
        if len(test.timing_history) >= 3:
            prior = (
                test.timing_history[:-1]
                if len(test.timing_history) > 1
                else test.timing_history
            )
            avg = sum(prior) / max(1, len(prior))
            if elapsed_ms > avg * 2 and avg > 0:
                timing_note = f"[{elapsed_ms}ms avg {int(avg)}ms \u26a0]"
                timing_style = "yellow"
        text.append(f" {timing_note}", style=timing_style)
        return text
    elif test.state == TestState.FAILED:
        run = test.current_run
        sig = run.signal_name if run is not None else ""
        if sig:
            text = Text(f"{ICON_FAIL} ", style=TEST_FAILED_STYLE)
            text.append(test.name, style=TEST_FAILED_STYLE)
            text.append(f" [{sig}]", style="bold red")  # e.g. [SIGSEGV]
        else:
            text = Text(f"{ICON_FAIL} ", style=TEST_FAILED_STYLE)
            text.append(test.name, style=TEST_FAILED_STYLE)
            timing_note = f"[{elapsed_ms}ms]"
            timing_style = TREE_META_STYLE
            if len(test.timing_history) >= 3:
                prior = (
                    test.timing_history[:-1]
                    if len(test.timing_history) > 1
                    else test.timing_history
                )
                avg = sum(prior) / max(1, len(prior))
                if elapsed_ms > avg * 2 and avg > 0:
                    timing_note = f"[{elapsed_ms}ms avg {int(avg)}ms \u26a0]"
                    timing_style = "yellow"
            text.append(f" {timing_note}", style=timing_style)
        return text

    text = Text(test.name, style=TEST_DEFAULT_STYLE)
    text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
    return text
