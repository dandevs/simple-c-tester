import time

from rich.text import Text

from models import Test, Suite, TestState
from render.styles import (
    SUITE_LABEL_STYLE,
    TEST_PENDING_STYLE,
    TEST_PASSED_STYLE,
    TEST_FAILED_STYLE,
    TEST_DEFAULT_STYLE,
    TREE_META_STYLE,
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


def suite_label(suite: Suite, now: float) -> Text:
    elapsed_ms = int(suite_elapsed_seconds(suite, now) * 1000)
    text = Text(suite.name, style=SUITE_LABEL_STYLE)
    text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
    return text


def test_label(test: Test, now: float) -> Text:
    elapsed_seconds = test_elapsed_seconds(test, now)
    elapsed_ms = int(elapsed_seconds * 1000)
    spinner_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    spinner = spinner_frames[int(now * 12) % len(spinner_frames)]

    if test.state == TestState.PENDING:
        text = Text(f"{spinner} {test.name}", style=TEST_PENDING_STYLE)
        text.append(" [pending]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.RUNNING and test.time_start <= 0:
        text = Text(f"{spinner} {test.name}", style=TEST_PENDING_STYLE)
        text.append(" [compiling]", style=TREE_META_STYLE)
        return text
    elif test.state in (TestState.RUNNING, TestState.CANCELLED):
        text = Text(f"{spinner} {test.name}", style=TEST_PENDING_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.PASSED:
        text = Text(test.name, style=TEST_PASSED_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text
    elif test.state == TestState.FAILED:
        text = Text(test.name, style=TEST_FAILED_STYLE)
        text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
        return text

    text = Text(test.name, style=TEST_DEFAULT_STYLE)
    text.append(f" [{elapsed_ms}ms]", style=TREE_META_STYLE)
    return text
