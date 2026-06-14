from dataclasses import dataclass

# Status icons (unicode)
ICON_PASS = "\u2713"
ICON_FAIL = "\u2717"
ICON_RUNNING = "\u25cf"
ICON_PENDING = "\u25cb"

# Unified palette
ACCENT_STYLE = "bold ansi_blue"
ACCENT_CYAN_STYLE = "ansi_cyan"
MUTED_STYLE = "dim"
SEPARATOR_STYLE = "dim"
SUITE_HEADER_SEPARATOR = "dim"

# Status badge styles (for inline counts)
BADGE_PASS_STYLE = "bright_green"
BADGE_FAIL_STYLE = "bright_red"
BADGE_RUN_STYLE = "bright_yellow"
BADGE_PENDING_STYLE = "dim bright_cyan"

# Output box borders
OUTPUT_FAIL_BORDER_STYLE = "bright_red"
OUTPUT_PASS_BORDER_STYLE = "dim"

SUITE_LABEL_STYLE = "bold default"
SUITE_FOLD_STYLE = "bold bright_cyan"
TEST_PENDING_STYLE = "bold bright_yellow"
TEST_PASSED_STYLE = "bold bright_green"
TEST_FAILED_STYLE = "bold bright_red"
TEST_DEFAULT_STYLE = "default"
TREE_META_STYLE = "bright_black"
TREE_GUIDE_STYLE = "dim"
OUTPUT_BOX_PASS_BORDER_STYLE = "dim"
SEARCH_HIGHLIGHT_STYLE = "black on bright_yellow"
STATUS_PASS_STYLE = "bold bright_green"
STATUS_FAIL_STYLE = "bold bright_red"
STATUS_RUN_STYLE = "bold bright_yellow"
STATUS_PENDING_STYLE = "dim bright_cyan"
STATUS_BASE_STYLE = "bold default"


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
