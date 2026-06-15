from dataclasses import dataclass

# Status icons (unicode)
ICON_PASS = "\u2713"
ICON_FAIL = "\u2717"
ICON_RUNNING = "\u25cf"
ICON_PENDING = "\u25cb"

# ---------------------------------------------------------------------------
# Muted / structural
# Use bright_black (a dark-gray *color*) — never "dim" (an ANSI attribute),
# because the dim attribute bleeds into subsequent Text segments.
# ---------------------------------------------------------------------------
MUTED_STYLE = "bright_black"
SEPARATOR_STYLE = "bright_black"
TREE_GUIDE_STYLE = "bright_black"          # ├── │   tree guides
TREE_META_STYLE = "bright_black"            # [12ms] elapsed times
SUITE_HEADER_SEPARATOR = "bright_black"     # ── in suite headers

# ---------------------------------------------------------------------------
# Test label styles — icon and name share the same color
# ---------------------------------------------------------------------------
TEST_PASSED_STYLE = "bold green"
TEST_FAILED_STYLE = "bold red"
TEST_RUNNING_STYLE = "bold yellow"
TEST_PENDING_STYLE = "cyan"
TEST_DEFAULT_STYLE = "default"

# ---------------------------------------------------------------------------
# Suite headers
# ---------------------------------------------------------------------------
SUITE_LABEL_STYLE = "bold white"
SUITE_FOLD_STYLE = "bold cyan"

# Badge styles (inline pass/fail counts in suite headers)
BADGE_PASS_STYLE = "green"
BADGE_FAIL_STYLE = "red"
BADGE_RUN_STYLE = "yellow"
BADGE_PENDING_STYLE = "cyan"

# ---------------------------------------------------------------------------
# Status header (top-line live counts)
# ---------------------------------------------------------------------------
STATUS_PASS_STYLE = "bold green"
STATUS_FAIL_STYLE = "bold red"
STATUS_RUN_STYLE = "bold yellow"
STATUS_PENDING_STYLE = "cyan"
STATUS_BASE_STYLE = "bold default"

# ---------------------------------------------------------------------------
# Output box borders
# ---------------------------------------------------------------------------
OUTPUT_FAIL_BORDER_STYLE = "red"
OUTPUT_PASS_BORDER_STYLE = "bright_black"
OUTPUT_BOX_PASS_BORDER_STYLE = "bright_black"

# ---------------------------------------------------------------------------
# Search highlight
# ---------------------------------------------------------------------------
SEARCH_HIGHLIGHT_STYLE = "black on yellow"

# ---------------------------------------------------------------------------
# Accents
# ---------------------------------------------------------------------------
ACCENT_STYLE = "bold blue"
ACCENT_CYAN_STYLE = "cyan"


@dataclass
class OutputBoxRenderMeta:
    rendered_lines: int
    left_col: int
    right_col: int


@dataclass
class OutputBoxRegion:
    test_key: str
    start_line: int
    end_line: int
    left_col: int
    right_col: int


@dataclass
class TestRowRegion:
    test_key: str
    line: int
    left_col: int
    right_col: int


@dataclass
class SuiteRowRegion:
    """A clickable suite header row in the tree (for fold toggling)."""
    suite_key: str
    line: int
    left_col: int
    right_col: int
