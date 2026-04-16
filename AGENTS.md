# AGENTS.md

## Project
C test runner written in Python. Scans `c/tests/` for `*.c` files, compiles each with gcc via a generated Makefile, runs the executable, and reports results. Uses Textual TUI for interactive terminal display with scrollable details and test tree navigation.

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

There is **no** positional source-dir argument; the test path is hardcoded as `c/tests`.

## Architecture
- `src/main.py` — entry point, async test dispatch, Textual TUI app with tree + scrollable details pane, watchdog integration
- `src/models/` — `Test`, `Suite`, `AppState` dataclasses and `TestState` enum
- `test_build/` — compiled executables, `.d` dependency files, and a generated `Makefile` (build artifact, gitignored)

## Textual UI
- Left pane: suite/test tree with status (pending/running/compiling/passed/failed)
- Right pane: scrollable details view (select test from tree to inspect compile/runtime output)
- Header: app title and status
- Footer: keyboard bindings (q to quit)
- Spinner animation for pending/running tests using Unicode braille characters
- Elapsed timing updates on all nodes

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
