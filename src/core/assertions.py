"""Parser for the CTester assertion wire format.

Parses ``[CTEST:v] FAIL`` lines emitted by ``ctest.h`` assertion macros into
structured :class:`AssertionFailure` objects for rich UI rendering.

Wire format (single line, newline-terminated)::

    [CTEST:1] FAIL tests/test_factorial.c:9 ASSERT_EQ(120, factorial(5)) expected=120 actual=60

The parser is pure (no state, no I/O) and safe to call on any string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class AssertionFailure:
    """One parsed assertion-failure line from test stderr."""

    file: str        # source file as printed (relative path)
    line: int        # source line number
    macro: str       # e.g. "ASSERT_EQ"
    args: str        # stringified macro arguments
    expected: str    # stringified expected value
    actual: str      # stringified actual value
    raw: str         # the full wire-format line (for fallback rendering)


_ASSERTION_RE = re.compile(
    r"\[CTEST:(\d+)\] FAIL (\S+):(\d+) (\w+)\(([^)]*)\) expected=(.*?) actual=(.*)$",
    re.MULTILINE,
)


def parse_assertion_failures(text: str) -> list[AssertionFailure]:
    """Extract all assertion failures from ``text`` (typically test stderr).

    Returns a list in the order they appear.  Returns an empty list when
    ``text`` contains no matching lines.
    """
    failures: list[AssertionFailure] = []
    for match in _ASSERTION_RE.finditer(text):
        failures.append(
            AssertionFailure(
                file=match.group(2),
                line=int(match.group(3)),
                macro=match.group(4),
                args=match.group(5),
                expected=match.group(6),
                actual=match.group(7),
                raw=match.group(0),
            )
        )
    return failures


def is_assertion_line(text: str) -> bool:
    """True if ``text`` (a single line) is a CTEST assertion-failure line."""
    return bool(_ASSERTION_RE.search(text))
