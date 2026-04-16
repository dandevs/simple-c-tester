# AGENTS.md

## Project
C test runner written in Python. Scans `c/tests/` for `*.c` files, compiles each with gcc via a generated Makefile, runs the executable, and reports results. Uses Rich for live terminal UI.

## Commands
- **Run**: `python3 src/main.py` from repo root
- **NOT** `python3 -m src.main` — imports use `from models import ...` which only works when running the file directly (Python adds `src/` to `sys.path`)

## CLI Flags
- `--parallel N` — number of concurrent test runners (default 4)
- `--watch` — watch for file changes, re-run affected tests

There is **no** positional source-dir argument; the test path is hardcoded as `c/tests`.

## Architecture
- `src/main.py` — entry point, async test dispatch, Rich Live display, watchdog integration
- `src/models/` — `Test`, `Suite`, `AppState` dataclasses and `TestState` enum
- `test_build/` — compiled executables, `.d` dependency files, and a generated `Makefile` (build artifact, gitignored)

## Compilation Flow
- `generate_makefile()` writes `test_build/Makefile` with one rule per test. Called after `populate_suites()` and when new tests are discovered in watch mode.
- `run_test()` calls `make -f test_build/Makefile test_build/<name>` — **not** direct `gcc`. Make handles incremental builds: if the binary is newer than its source and all `.d`-tracked header dependencies, compilation is skipped.
- `.d` files are parsed **after** make returns — dependencies populate regardless of pass/fail and feed the `dep_index` used by watch mode.
- Exit code 0 from make + exit code 0 from binary = PASSED. Non-zero from either = FAILED.

## Concurrency Notes
- `state_changed()` is a **sync** function (not async) — it uses `asyncio.ensure_future()` to schedule `run_test()` and recurses to drain the pending queue
- `available_runners` counter (not a semaphore) limits dispatch
- Watchdog handler uses `threading.Timer` / `threading.Lock` for debouncing, then calls `loop.call_soon_threadsafe` back into the async loop

## Tooling
- **Rich** for live terminal tree output
- **gcc** for C compilation (invoked via generated Makefile)
- **watchdog** for file system watching
- No `pyproject.toml`, `requirements.txt`, or Python test framework configured
