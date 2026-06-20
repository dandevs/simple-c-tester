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
leak_sanitizer_enabled = False
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
# When True, debugLine db.json updates are suppressed so the user's IDE
# doesn't jump to source lines while inspecting the variable tree view.
# Set by VariableTreeScreen.on_mount / on_unmount.
debug_line_suppressed: bool = False
# Project sources dropped from libproject.a by skip-on-error (compile failed).
# Populated by build_project_sources(); read by headless output and the TUI.
skipped_sources: list = []
# gcc stderr from the last build_project_sources archive build. When
# skipped_sources is non-empty this holds the actual compile errors so callers
# can show why sources were skipped, not just the file names.
build_stderr: str = ""
# Per-source gcc stderr for each skipped source (path -> that source's isolated
# compile output). Used to show a test only the errors of the sources it links.
source_errors: dict = {}
