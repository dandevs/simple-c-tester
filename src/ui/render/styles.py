from dataclasses import dataclass

SUITE_LABEL_STYLE = "bold default"
SUITE_FOLD_STYLE = "bold bright_cyan"
TEST_PENDING_STYLE = "bold bright_yellow"
TEST_PASSED_STYLE = "bold bright_green"
TEST_FAILED_STYLE = "bold bright_red"
TEST_DEFAULT_STYLE = "default"
TREE_META_STYLE = "dim default"
TREE_GUIDE_STYLE = "default"
OUTPUT_BOX_PASS_BORDER_STYLE = "default"
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
