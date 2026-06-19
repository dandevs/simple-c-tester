import asyncio

from core.models import Test, Suite, AppState

state = AppState()
dep_index: dict[str, list[Test]] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}
subprocess_columns = 80
dep_graph_ready = False
dep_graph_reason = "dependency graph not initialized"
debug_build_enabled = False
timeline_capture_enabled = False
sanitize_enabled = True
leak_sanitizer_enabled = True
active_debug_test_key: str | None = None
debug_auto_restart: bool = False
debug_auto_restart_pending: str | None = None
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
# Project sources dropped from libproject.a by skip-on-error (compile failed).
# Populated by build_project_sources(); read by headless output and the TUI.
skipped_sources: list = []
