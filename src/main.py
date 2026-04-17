import argparse
import asyncio
import shutil
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from models import Test, Suite, AppState
from state import state, active_processes
from render import TestOutputScreen, render_tree_stdout
from runner import state_changed, generate_makefile, _terminate_active_processes
from app import TestRunnerApp


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=4, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    parser.add_argument(
        "--output-lines",
        type=int,
        default=25,
        help="Maximum number of output lines to show per info box",
    )
    parser.add_argument(
        "--theme",
        choices=["ansi", "default"],
        default="ansi",
        help="UI theme (default: ansi)",
    )
    return parser.parse_args()


async def _main():
    args = parse_args()
    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print(f"Error: test directory not found: {tests_dir}", file=sys.stderr)
        sys.exit(1)
    state.populate_suites(str(tests_dir))
    generate_makefile()
    state.available_runners = args.parallel

    app = TestRunnerApp(args.watch, args.output_lines, args.theme)
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
