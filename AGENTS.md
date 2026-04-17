# AGENTS.md

## Project
C test runner written in Python. Scans `c/tests/` for `*.c` files, compiles each with gcc via a generated Makefile, runs the executable, and reports results. Uses Textual TUI with a single RichLog widget that renders the full test tree as styled text — suites and tests shown with Unicode box-drawing characters (`├──`, `└──`, `│`), and test output displayed inline in bordered boxes (`╭─╮`, `│`, `╰─╯`) beneath each test that has output.

## Installation
```bash
pip install -r requirements.txt
```

## Commands
- **Run**: `python3 src/main.py` from repo root
- **NOT** `python3 -m src.main` — imports use `from models import ...` which only works when running the file directly (Python adds `src/` to `sys.path`)

## CLI Flags
- `--parallel N` — number of concurrent test runners (default 4)
- `--watch` — watch for file changes, re-run affected tests
- `--theme ansi|default` — UI theme (default: `ansi`). `ansi` uses Textual's `textual-ansi` theme which blends with the terminal's native colors. `default` uses Textual's standard `textual-dark` theme.

There is **no** positional source-dir argument; the test path is hardcoded as `c/tests`.

## Architecture
- `src/main.py` — entry point: `parse_args()`, `main()`, sys.path setup
- `src/state.py` — shared mutable state: `state` (AppState), `dep_index`, `active_processes`, `subprocess_columns`
- `src/app.py` — `TestRunnerApp` Textual TUI class (tree rendering delegates to `render/`, test dispatch delegates to `runner/`, file watching delegates to `watch/`)
- `src/models/` — `Test`, `Suite`, `AppState` dataclasses and `TestState` enum
- `src/render/` — all UI rendering logic
  - `styles.py` — style constant strings, `OutputBoxRenderMeta`, `OutputBoxRegion` dataclasses
  - `labels.py` — elapsed time helpers, `suite_label()`, `test_label()` with spinner
  - `output.py` — `get_test_output()`, `render_output_box()`, text wrapping/stripping helpers
  - `tree.py` — `render_tree()`, `render_node()` standalone functions that walk the suite tree and write to RichLog
  - `screens.py` — `TestOutputScreen` for full test output view
- `src/runner/` — test execution and build logic
  - `makefile.py` — `generate_makefile()`, `rebuild_dep_index()`
  - `execute.py` — `run_test()`, `state_changed()`, `_terminate_active_processes()`
  - `state.py` — `all_tests_finished()`, `has_active_tests()`, `display_state_signature()`
- `src/watch/` — file system watching
  - `handler.py` — `DebounceHandler` (watchdog), `handle_file_changes()`
- `test_build/` — compiled executables, `.d` dependency files, and a generated `Makefile` (build artifact, gitignored)

## Textual UI
- Single full-screen `RichLog` widget rendering the test tree with Unicode box-drawing characters
- Suite and test nodes displayed with `├──`/`└──`/`│` tree guides
- Inline output boxes (`╭─╮`, `│`, `╰─╯`) beneath tests that have compile errors, stderr, or stdout
- Box borders colored red for failures, dim for passes; tree guides styled dim
- Test names colored green (passed), bold red (failed), yellow with spinner (pending/running)
- Elapsed time `[Xms]` shown after each node in `bright_black`
- Header: app title and status
- Footer: keyboard bindings (q to quit)
- Spinner animation for pending/running tests using Unicode braille characters
- Elapsed timing updates on all nodes
- `render_tree()` clears and redraws the full tree on each tick (100ms) when state changes
- `render_node()` recursively walks suites/tests, computing tree prefix continuations
- `render_output_box()` draws the bordered output box with proper tree continuation lines
- `get_test_output()` collects compile_err, stderr, stdout as Rich Text lines (preserves ANSI colors)

## Compilation Flow
- `generate_makefile()` writes `test_build/Makefile` with one rule per test. Called after `populate_suites()` and when new tests are discovered in watch mode.
- `run_test()` calls `make -f test_build/Makefile test_build/<name>` — **not** direct `gcc`. Make handles incremental builds: if the binary is newer than its source and all `.d`-tracked header dependencies, compilation is skipped.
- `.d` files are parsed **after** make returns — dependencies populate regardless of pass/fail and feed the `dep_index` used by watch mode.
- Exit code 0 from make + exit code 0 from binary = PASSED. Non-zero from either = FAILED.

## Concurrency Notes
- `state_changed()` is a **sync** function (not async) — it uses `asyncio.ensure_future()` to schedule `run_test()` and recurses to drain the pending queue
- `available_runners` counter (not a semaphore) limits dispatch
- Watchdog handler uses `threading.Timer` / `threading.Lock` for debouncing, then calls `loop.call_soon_threadsafe` back into the async loop
- Textual app runs in async context; `_tick()` callback refreshes UI at 100ms intervals when active or state changes

## Tooling
- **Textual** (>=0.68.0) for interactive TUI with scrollable widgets
- **Rich** (>=13.7.0) for ANSI/markup text styling in Textual widgets
- **gcc** for C compilation (invoked via generated Makefile)
- **watchdog** (>=3.0.0) for file system watching
- Dependencies listed in `requirements.txt`
