import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import state as global_state
from state import state
from render import TestOutputScreen, render_tree_stdout
from runner import (
    state_changed,
    generate_makefile,
    build_project_sources,
    hydrate_dependencies_from_db,
    refresh_dependency_graph,
    prime_editor_breakpoints_cache,
    _terminate_active_processes,
)
from app import TestRunnerApp
from runner.story_filters import normalized_story_filter_profile


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=4, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    parser.add_argument(
        "--output-lines",
        type=int,
        default=10,
        help="Maximum number of output lines to show per info box",
    )
    parser.add_argument(
        "--theme",
        choices=["ansi", "default"],
        default="ansi",
        help="UI theme (default: ansi)",
    )
    parser.add_argument(
        "--timeline",
        action="store_true",
        help="Enable per-line timeline capture with gdb",
    )
    parser.add_argument(
        "--debug-build",
        action="store_true",
        help="Compile tests with debug flags (-g -O0)",
    )
    parser.add_argument(
        "--story-filter-profile",
        choices=["minimal", "balanced", "all"],
        default="balanced",
        help="Test Story card filter profile (default: balanced)",
    )
    parser.add_argument(
        "--tsv-lines-above",
        type=int,
        default=4,
        help="Test Story viewer lines shown above current line (default: 4)",
    )
    parser.add_argument(
        "--tsv-lines-below",
        type=int,
        default=4,
        help="Test Story viewer lines shown below current line (default: 4)",
    )
    parser.add_argument(
        "--tsv-skip-seq-lines",
        type=int,
        default=10,
        help="Skip sequential same-file line frames in Test Story (default: 10)",
    )
    parser.add_argument(
        "--tsv-vars-depth",
        type=int,
        default=2,
        help="Variable expansion depth for Test Story viewer (default: 2)",
    )
    parser.add_argument(
        "--tsv-variables-height",
        type=int,
        default=10,
        help="Variables panel height in Test Story viewer (default: 10)",
    )
    parser.add_argument(
        "--tsv-show-reason-about",
        action="store_true",
        help="Show [Reason] About details in Test Story cards",
    )
    parser.add_argument(
        "--tsv-var-history",
        type=int,
        default=3,
        help="Max historical values shown per variable on a line (default: 3)",
    )
    return parser.parse_args()


async def _main():
    args = parse_args()
    global_state.timeline_capture_enabled = bool(args.timeline)
    global_state.debug_build_enabled = bool(args.debug_build or args.timeline)
    global_state.tsv_lines_above = max(0, int(args.tsv_lines_above))
    global_state.tsv_lines_below = max(0, int(args.tsv_lines_below))
    global_state.tsv_skip_seq_lines = max(1, int(args.tsv_skip_seq_lines))
    global_state.tsv_vars_depth = max(1, int(args.tsv_vars_depth))
    global_state.tsv_variables_height = max(3, int(args.tsv_variables_height))
    global_state.tsv_show_reason_about = bool(args.tsv_show_reason_about)
    global_state.tsv_var_history = max(1, int(args.tsv_var_history))
    global_state.story_filter_profile_preference = normalized_story_filter_profile(
        args.story_filter_profile
    )

    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print(f"Error: test directory not found: {tests_dir}", file=sys.stderr)
        sys.exit(1)
    state.populate_suites(str(tests_dir))
    hydrate_dependencies_from_db()
    generate_makefile()
    build_project_sources()
    refresh_dependency_graph()
    prime_editor_breakpoints_cache()
    state.available_runners = args.parallel

    app = TestRunnerApp(args.watch, args.output_lines, args.theme, args.timeline)
    try:
        await app.run_async()
    finally:
        app.stop_observer()
        await _terminate_active_processes()
        if not args.watch:
            render_tree_stdout(args.output_lines, shutil.get_terminal_size().columns)


def entry():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entry()
