from dataclasses import dataclass

SUITE_LABEL_STYLE = "bold bright_white"
TEST_PENDING_STYLE = "bold bright_yellow"
TEST_PASSED_STYLE = "bold bright_green"
TEST_FAILED_STYLE = "bold bright_red"
TEST_DEFAULT_STYLE = "bright_white"
TREE_META_STYLE = "white"
TREE_GUIDE_STYLE = "white"
OUTPUT_BOX_PASS_BORDER_STYLE = "white"


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
