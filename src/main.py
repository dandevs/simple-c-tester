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

# Handle "ctester new <name>" before any heavy imports — scaffolds a new
# test file under tests/ from a small template.
if len(_sys.argv) > 2 and _sys.argv[1] == "new":
    from pathlib import Path
    import re as _re

    _test_name = _sys.argv[2]
    if not _test_name.endswith(".c"):
        _test_name += ".c"
    # sanitize: only allow alphanumeric + underscore + hyphen before ".c"
    if not _re.match(r"^[\w\-]+\.c$", _test_name):
        print(
            f"Error: invalid test name '{_test_name}'. "
            "Use letters, digits, hyphens, underscores.",
            file=_sys.stderr,
        )
        _sys.exit(1)

    _tests_dir = Path("tests")
    _tests_dir.mkdir(exist_ok=True)
    _target_file = _tests_dir / _test_name
    if _target_file.exists():
        print(f"Error: {_target_file} already exists.", file=_sys.stderr)
        _sys.exit(1)

    _template = (
        '#include "ctest.h"\n'
        "\n"
        "int main(void) {\n"
        "    ASSERT_TRUE(1 == 1);\n"
        "    return 0;\n"
        "}\n"
    )
    _target_file.write_text(_template)
    print(f"Created {_target_file}")
    print("  Run ctester to execute it.")
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
    # Menu-editable flags default to None so we can tell "not passed" from an
    # explicit value.  Resolution order at startup is:
    #   cli_arg (if not None) > user-config value > builtin default.
    parser.add_argument(
        "--parallel", type=int, default=None, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    parser.add_argument(
        "--output-lines",
        type=int,
        default=None,
        help="Maximum number of output lines to show per info box",
    )
    parser.add_argument(
        "--theme",
        choices=["ansi", "default"],
        default=None,
        help="UI theme (default: ansi)",
    )
    parser.add_argument(
        "--timeline",
        action="store_true",
        default=None,
        help="Enable per-line timeline capture with gdb",
    )
    parser.add_argument(
        "--debug-build",
        action="store_true",
        help="Compile tests with debug flags (-g -O0)",
    )
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="Compile with AddressSanitizer + UndefinedBehaviorSanitizer",
    )
    parser.add_argument(
        "--story-filter-profile",
        choices=["minimal", "balanced", "all"],
        default=None,
        help="Test Story card filter profile (default: balanced)",
    )
    parser.add_argument(
        "--tsv-lines-above",
        type=int,
        default=None,
        help="Test Story viewer lines shown above current line (default: 4)",
    )
    parser.add_argument(
        "--tsv-lines-below",
        type=int,
        default=None,
        help="Test Story viewer lines shown below current line (default: 4)",
    )
    parser.add_argument(
        "--tsv-skip-seq-lines",
        type=int,
        default=None,
        help="Skip sequential same-file line frames in Test Story (default: 10)",
    )
    parser.add_argument(
        "--tsv-vars-depth",
        type=int,
        default=None,
        help="Variable expansion depth for Test Story viewer (default: 2)",
    )
    parser.add_argument(
        "--tsv-variables-height",
        type=int,
        default=None,
        help="Variables panel height in Test Story viewer (default: 10)",
    )
    parser.add_argument(
        "--tsv-show-reason-about",
        action="store_true",
        default=None,
        help="Show [Reason] About details in Test Story cards",
    )
    parser.add_argument(
        "--cflags",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra compiler/linker flags (e.g. -lreadline -Wextra -Werror)",
    )
    return parser.parse_args()


# Argparse dest names that correspond to Options-menu fields.  Used to compute
# which settings the user overrode on the command line this session.
_MENU_ARG_KEYS = (
    "parallel",
    "output_lines",
    "theme",
    "timeline",
    "story_filter_profile",
    "tsv_lines_above",
    "tsv_lines_below",
    "tsv_skip_seq_lines",
    "tsv_vars_depth",
    "tsv_variables_height",
    "tsv_show_reason_about",
)


def _cli_overrides(args) -> set[str]:
    """Return the set of menu-field keys explicitly passed on the CLI."""
    return {k for k in _MENU_ARG_KEYS if getattr(args, k, None) is not None}


def _build_config(args, user_config: dict) -> RunnerConfig:
    """Resolve the effective RunnerConfig.

    For each menu field: ``cli_arg`` wins if explicitly passed, else the
    persisted ``user_config`` value, else the builtin default.  Non-menu flags
    (watch, debug-build, sanitize, cflags) keep their plain CLI/default values.
    """

    def resolve(key: str, attr: str, default, coerce=lambda v: v):
        cli = getattr(args, attr)
        if cli is not None:
            return coerce(cli)
        if key in user_config:
            return user_config[key]
        return default

    timeline = bool(resolve("timeline", "timeline", False))
    return RunnerConfig(
        parallel=int(resolve("parallel", "parallel", 4)),
        watch=args.watch,
        output_lines=max(1, int(resolve("output_lines", "output_lines", 10))),
        theme=resolve("theme", "theme", "ansi"),
        timeline=timeline,
        debug_build=bool(args.debug_build or timeline),
        sanitize=bool(args.sanitize),
        story_filter_profile=normalized_story_filter_profile(
            resolve("story_filter_profile", "story_filter_profile", "balanced")
        ),
        tsv_lines_above=max(0, int(resolve("tsv_lines_above", "tsv_lines_above", 4))),
        tsv_lines_below=max(0, int(resolve("tsv_lines_below", "tsv_lines_below", 4))),
        tsv_skip_seq_lines=max(
            1, int(resolve("tsv_skip_seq_lines", "tsv_skip_seq_lines", 10))
        ),
        tsv_vars_depth=max(1, int(resolve("tsv_vars_depth", "tsv_vars_depth", 2))),
        tsv_variables_height=max(
            3, int(resolve("tsv_variables_height", "tsv_variables_height", 10))
        ),
        tsv_show_reason_about=bool(
            resolve("tsv_show_reason_about", "tsv_show_reason_about", False)
        ),
        cflags=" ".join(args.cflags),
    )


async def _main():
    args = parse_args()
    from core.userconfig import load_user_config

    user_config = load_user_config()
    config = _build_config(args, user_config)
    cli_overrides = _cli_overrides(args)

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
        user_config=user_config,
        cli_overrides=cli_overrides,
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
