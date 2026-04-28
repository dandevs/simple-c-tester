import asyncio

from models import Test, Suite, AppState

state = AppState()
dep_index: dict[str, list[Test]] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}
subprocess_columns = 80
dep_graph_ready = False
dep_graph_reason = "dependency graph not initialized"
debug_build_enabled = False
timeline_capture_enabled = False
active_debug_test_key: str | None = None
tsv_lines_above = 4
tsv_lines_below = 4
tsv_skip_seq_lines = 10
tsv_vars_depth = 2
tsv_variables_height = 10
tsv_show_reason_about = False
debug_precision_mode_preference = "precise"
story_filter_profile_preference = "balanced"
debug_line: dict | None = None
app_active = False
cflags: str = ""
