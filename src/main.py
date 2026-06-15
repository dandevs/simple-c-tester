"""Thin CLI entry point.

Parses arguments, builds an immutable :class:`RunnerConfig`, constructs a
:class:`TestRunner` (the public API), discovers tests, prepares the build, and
launches the Textual TUI.  The engine is driven entirely through the API;
``main.py`` no longer mutates the legacy global ``state`` module directly.

Run from a project root containing a ``tests/`` directory (e.g. ``c/``)::

    python3 ../src/main.py
"""

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(__file__))

# Handle "ctester init" before any heavy imports — pygdbmi/textual may not be
# installed in environments that only need the init scaffolding command.
if len(_sys.argv) > 1 and _sys.argv[1] == "init":
    from pathlib import Path

    _tests_path = Path("tests")
    _created_dir = not _tests_path.exists()
    _tests_path.mkdir(exist_ok=True)

    _bundled = Path(_os.path.dirname(__file__)) / "api" / "resources" / "ctest.h"
    _bundled_text = _bundled.read_text()
    _target = _tests_path / "ctest.h"

    if _target.exists():
        if _target.read_text() == _bundled_text:
            _action = "up to date"
        else:
            _target.write_text(_bundled_text)
            _action = "updated"
    else:
        _target.write_text(_bundled_text)
        _action = "created"

    print("Initializing CTester project...")
    if _created_dir:
        print(f"  Created directory: {_tests_path}/")
    print(f"  ctest.h: {_action} at {_target}")
    print(
        "\n  Add to your tests:\n"
        '    #include "ctest.h"\n'
        "\n"
        "  Assertions (fatal - return 1 on failure):\n"
        "    ASSERT_EQ(expected, actual)   ASSERT_TRUE(cond)\n"
        "    ASSERT_STREQ(a, b)            ASSERT_NULL(ptr)\n"
        "    ASSERT_GT(a, b)   ASSERT_LT(a, b)   ...\n"
        "\n"
        "  Soft checks (report and continue):\n"
        "    EXPECT_EQ(expected, actual)   EXPECT_TRUE(cond)\n"
        "    return TEST_RESULT();\n"
    )
    _sys.exit(0)

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from api import TestRunner, RunnerConfig
from core.config import RunnerConfig as _RunnerConfig  # noqa: F401 (re-export clarity)
from core.story import normalized_story_filter_profile
from ui.app import TestRunnerApp
from ui.render import render_tree_stdout


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
        "--cflags",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra compiler/linker flags (e.g. -lreadline -Wextra -Werror)",
    )
    return parser.parse_args()


def _build_config(args) -> RunnerConfig:
    return RunnerConfig(
        parallel=args.parallel,
        watch=args.watch,
        output_lines=args.output_lines,
        theme=args.theme,
        timeline=args.timeline,
        debug_build=bool(args.debug_build or args.timeline),
        story_filter_profile=normalized_story_filter_profile(args.story_filter_profile),
        tsv_lines_above=max(0, int(args.tsv_lines_above)),
        tsv_lines_below=max(0, int(args.tsv_lines_below)),
        tsv_skip_seq_lines=max(1, int(args.tsv_skip_seq_lines)),
        tsv_vars_depth=max(1, int(args.tsv_vars_depth)),
        tsv_variables_height=max(3, int(args.tsv_variables_height)),
        tsv_show_reason_about=bool(args.tsv_show_reason_about),
        cflags=" ".join(args.cflags),
    )


async def _main():
    args = parse_args()
    config = _build_config(args)

    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        print(f"Error: test directory not found: {tests_dir}", file=sys.stderr)
        sys.exit(1)

    # The engine is driven entirely through the public API.
    runner = TestRunner(config)
    runner.discover(str(tests_dir))
    runner.prepare_build()
    runner.save_db()

    app = TestRunnerApp(
        runner,
        watch=config.watch,
        output_max_lines=config.output_lines,
        theme_name=config.theme,
        timeline_enabled=config.timeline,
    )
    try:
        await app.run_async()
    finally:
        runner.stop_emitter()
        runner.save_db()
        app.stop_observer()
        from api._runner import _terminate_active_processes

        await _terminate_active_processes()
        if not config.watch:
            render_tree_stdout(
                config.output_lines, shutil.get_terminal_size().columns
            )


def entry():
    # "ctester init" is handled at the top of this module before heavy imports.
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entry()
