"""Thin CLI entry point.

Parses arguments, builds an immutable :class:`RunnerConfig`, constructs a
:class:`TestRunner` (the public API), discovers tests, prepares the build,
and either runs all tests headlessly (printing the result tree and exiting)
or — with ``--watch`` — launches the interactive Textual TUI.  The engine is
driven entirely through the API; ``main.py`` no longer mutates the legacy
global ``state`` module directly.

Run from a project root containing a ``tests/`` directory (e.g. ``c/``)::

    python3 ../src/main.py                 # run all tests, print, exit
    python3 ../src/main.py tests/foo.c     # run only the named file(s)
    python3 ../src/main.py --watch         # interactive TUI
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
import os
import shutil
import sys
from pathlib import Path

from api import TestRunner, RunnerConfig
from core.config import RunnerConfig as _RunnerConfig  # noqa: F401 (re-export clarity)
from core.models import Suite, TestState
from core.story import normalized_story_filter_profile
from ui.app import TestRunnerApp
from ui.render import render_tree_stdout


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run C tests, print the result tree, and exit. Pass specific test"
            " files to run a subset. Use --watch to open the interactive TUI."
            " Use --get-dependencies FILE to inspect a file's dependencies."
        ),
        epilog=(
            "examples:\n"
            "  ctester                    Run all tests, print results, exit\n"
            "  ctester tests/foo.c        Run only the named test file(s)\n"
            "  ctester --watch            Open the interactive TUI (with file monitoring)\n"
            "  ctester --parallel 8       Run all tests with 8 parallel workers\n"
            "  ctester --get-dependencies tests/foo.c   Print deps for a file, exit\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help=(
            "Specific test files to run, e.g. tests/foo.c. Omit to run all"
            " tests. Cannot be combined with --watch."
        ),
    )
    # Menu-editable flags default to None so we can tell "not passed" from an
    # explicit value.  Resolution order at startup is:
    #   cli_arg (if not None) > user-config value > builtin default.
    parser.add_argument(
        "--parallel", type=int, default=None, help="Number of parallel workers"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Open the interactive TUI and watch for file changes",
    )
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
        "--no-sanitize",
        action="store_true",
        help="Disable AddressSanitizer + UndefinedBehaviorSanitizer (on by default)",
    )
    parser.add_argument(
        "--no-leak-sanitizer",
        action="store_true",
        help="Disable LeakSanitizer via ASAN_OPTIONS=detect_leaks=0 (on by default)",
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
    parser.add_argument(
        "--get-dependencies",
        metavar="FILE",
        default=None,
        help=(
            "Resolve and print dependency info for FILE (include dirs,"
            " transitive headers, project sources linked into libproject.a)"
            " and exit. No tests are run. e.g. --get-dependencies tests/foo.c"
        ),
    )
    args = parser.parse_args()
    if args.watch and args.files:
        parser.error("--watch cannot be combined with file arguments")
    return args


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
    (watch, debug-build, no-sanitize, no-leak-sanitizer, cflags) keep their
    plain CLI/default values.
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
        sanitize=not args.no_sanitize,
        leak_sanitizer=not args.no_leak_sanitizer,
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


# ---------------------------------------------------------------------------
# Headless selection: map user-supplied file paths to discovered tests
# ---------------------------------------------------------------------------


def _match_path(source_path: str, arg: str) -> bool:
    """True if a discovered test's source path corresponds to ``arg``.

    Matching is prefix-tolerant so each of these resolves the same test:
    ``tests/foo.c``, ``./tests/foo.c``, ``foo.c``, ``sub/bar.c``.
    """
    sp = os.path.normpath(source_path)
    a = os.path.normpath(arg)
    if sp == a:
        return True
    return sp.endswith("/" + a) or a.endswith("/" + sp)


def _select_tests(all_tests, files):
    """Split ``files`` into ``(matched_tests, unmatched_args)``.

    Pure: reads ``all_tests`` and ``files`` only. Results are de-duplicated by
    source path while preserving discovery order.
    """
    matched, unmatched = [], []
    seen = set()
    for arg in files:
        hits = [t for t in all_tests if _match_path(t.source_path, arg)]
        if not hits:
            unmatched.append(arg)
            continue
        for t in hits:
            if t.source_path not in seen:
                seen.add(t.source_path)
                matched.append(t)
    return matched, unmatched


def _prune_suite(suite, keep):
    """Return a new :class:`Suite` retaining only tests whose source path is
    in ``keep``. Empty child suites are dropped; the root suite is always
    returned (possibly with no children)."""
    pruned = Suite(name=suite.name)
    pruned.tests = [t for t in suite.tests if t.source_path in keep]
    for child in suite.children:
        pruned_child = _prune_suite(child, keep)
        if pruned_child.tests or pruned_child.children:
            pruned.children.append(pruned_child)
    return pruned


def _apply_selection(runner, matched):
    """Restrict ``runner``'s eligible tests to ``matched`` (in place).

    Runs after :meth:`TestRunner.prepare_build` so the generated Makefile and
    dependency graph still cover the full project; only the set of tests that
    may execute (and that the result tree renders) is narrowed.  Mutates the
    shared :class:`AppState` so the engine scheduler and the stdout renderer
    both observe the trimmed set.
    """
    keep = {t.source_path for t in matched}
    app_state = runner.state.app_state
    app_state.root_suite = _prune_suite(app_state.root_suite, keep)
    app_state.all_tests = list(matched)


def _print_dependencies(source_path) -> int:
    """Resolve and print dependency info for ``source_path``, then exit.

    Uses the real build machinery (include resolution, ``gcc -MM``, project
    source discovery) but writes no build artifacts.  Returns an exit code.
    """
    from core.build import get_file_dependencies

    if not os.path.isfile(source_path):
        print(f"Error: file not found: {source_path}", file=sys.stderr)
        return 1

    deps = get_file_dependencies(source_path)
    norm = os.path.normpath(source_path)
    print(f"Dependencies for: {norm}")
    print()
    print("Include directories:")
    if deps["include_dirs"]:
        for d in deps["include_dirs"]:
            print(f"  -I{d}")
    else:
        print("  (none)")
    print()
    print(f"Header dependencies ({len(deps['headers'])}):")
    for h in deps["headers"]:
        print(f"  {h}")
    print()
    print(
        f"Project sources linked into libproject.a ({len(deps['project_sources'])}):"
    )
    for s in deps["project_sources"]:
        print(f"  {s}")
    return 0


async def _main():
    args = parse_args()
    if args.get_dependencies:
        return _print_dependencies(args.get_dependencies)
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

    if config.watch:
        return await _run_tui(runner, config, user_config, cli_overrides)
    return await _run_headless(runner, config, args.files)


async def _run_tui(runner, config, user_config, cli_overrides) -> int:
    """Launch the interactive Textual TUI (watch mode)."""
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
    return 0


async def _run_headless(runner, config, files) -> int:
    """Run tests without the TUI, print the result tree, return an exit code."""
    if files:
        matched, unmatched = _select_tests(runner.tests, files)
        if unmatched:
            for arg in unmatched:
                print(f"Error: no test file matched: {arg}", file=sys.stderr)
            return 1
        _apply_selection(runner, matched)

    await runner.run_all()
    try:
        runner.save_db()
    finally:
        from api._runner import _terminate_active_processes

        await _terminate_active_processes()

    # Surface skip-on-error warnings: project sources that failed to compile
    # and were dropped from libproject.a.  (Printed to stderr; headless only
    # — the TUI reads the same list from global state to avoid corrupting
    # the screen.)
    import state as _gstate

    if _gstate.skipped_sources:
        print(
            f"Warning: {len(_gstate.skipped_sources)} project source(s) skipped"
            " (compile failed, not linked into libproject.a):",
            file=sys.stderr,
        )
        for s in _gstate.skipped_sources:
            print(f"  - {s}", file=sys.stderr)
        print(file=sys.stderr)

    render_tree_stdout(config.output_lines, shutil.get_terminal_size().columns)

    failed = any(t.state != TestState.PASSED for t in runner.tests)
    return 1 if failed else 0


def entry():
    # "ctester init" / "ctester new" are handled at the top of this module
    # before heavy imports.
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entry()
