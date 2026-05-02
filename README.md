<p align="center"><strong>ctester</strong></p>

<p align="center">
  <video src="assets/ctester.webm" controls width="720"></video>
</p>

# simple-c-tester

simple-c-tester is a C test runner with a Textual TUI.

It scans a tests directory for .c files, builds each test via a generated Makefile, runs the compiled binaries, and renders live results in a tree view.

## What You Get

- Recursive test discovery from a tests directory.
- Parallel test execution.
- Incremental C builds with dependency tracking via .d files.
- Rich TUI output with Unicode tree guides and inline output boxes.
- Click any inline output box to open a full output screen.
- Optional file watching to re-run only affected tests.

## Requirements

- Python 3.9+
- gcc
- make

## Quick Start

From repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd c
python3 ../src/main.py
```

Important: run the app from the c directory.
The runner expects relative paths tests/ and test_build/ in the current working directory.

## CLI Options

```text
python3 ../src/main.py [--parallel N] [--watch] [--output-lines N] [--theme ansi|default]
```

| Option | Default | Description |
|---|---|---|
| --parallel N | 4 | Number of concurrent test workers. |
| --watch | off | Watch for file changes and re-run affected tests. |
| --output-lines N | 10 | Max visible lines in each inline output box. |
| --theme ansi\|default | ansi | ansi uses terminal-native ANSI styling; default uses Textual default theme. |

## TUI Interaction

- Ctrl+C closes the app.
- Mouse click on an inline output box opens full output for that test.
- In the full output screen:
  - Escape returns to the tree.
  - Ctrl+C also returns to the tree.
- In the Test Story screen:
  - `D` starts/stops manual debug for the selected test.
  - `P` toggles stepping precision (`loose`/`precise`) and persists that preference in `test_build/db.json`.
  - `R` restarts manual debug after a manual session exits.

Precision modes:

- `loose`: smart stepping optimized for source-level flow; it heuristically chooses step-over vs step-in and quickly steps out of non-user/library code.
- `precise`: stricter GDB stepping behavior (`scheduler-locking step`) for line-by-line control with fewer heuristics.

## Test Layout

The app scans tests/ recursively for .c files. Directory structure becomes suite structure in the UI.

Example:

```text
c/
  tests/
    test_addition.c
    a/
      test_array_access.c
      b/
        test_factorial.c
```

## How It Works

1. Discover test files and build suite tree.
2. Resolve include directories per test using iterative gcc -E checks.
3. Generate test_build/Makefile.
4. Optionally prebuild test_build/libproject.a from discovered project .c files.
5. Run make -f test_build/Makefile test_build/<test_name> per test.
6. Execute the produced binary and capture stdout/stderr.
7. Parse .d files and update dependency index for watch mode.

Pass/fail rules:

- Compile step non-zero exit status: test fails with compile error output.
- Compile success + test binary exit code 0: pass.
- Compile success + test binary non-zero exit: fail.

## Watch Mode

With --watch, the runner monitors:

- tests directory
- discovered include directories
- dependency file directories tracked by prior runs

On change, affected tests are re-queued. If a test is currently running, it is cancelled and restarted.

When `test_build/breakpoints.json` changes, the app refreshes editor breakpoint data used by manual debug (without forcing a full project rebuild).

## Manual Debug Breakpoints

Manual debug can read breakpoints exported from VS Code extension tooling via:

- `test_build/breakpoints.json` (default)
- `CTESTER_BREAKPOINTS_FILE` (optional override path)

To enable breakpoint support from VS Code, install the [vsc-simple-c-test-support](https://github.com/dandevs/vsc-simple-c-test-support) extension.

Expected JSON shape:

```json
[
  { "filepath": "/abs/path/to/file.c", "line_number": 42 }
]
```

Behavior:

- Only `.c` and `.cpp` breakpoints are used.
- If any breakpoints are valid, manual debug starts with normal run and stops at the first breakpoint hit.
- If none are valid/available, manual debug falls back to `main`.

## Project Structure

```text
src/
  main.py              Entry point and CLI parsing
  app.py               Textual app and event handling
  state.py             Shared mutable runtime state
  models/              Test, Suite, AppState, TestState
  render/              Tree rendering, labels, output formatting, screens
  runner/              Makefile generation, execution, state transitions
  watch/               File watcher debounce and change handling

c/
  tests/               C test files (recursive)
  test_build/          Generated Makefile, binaries, .d files
```

## Packaging As A PEX

Build a portable executable archive:

```bash
pip install pex
./build.sh
```

Output:

```text
out/ctester.pex
```

The build script targets multiple platforms (Linux/macOS/Windows) for CPython 3.9.

Run it from c (same working-directory requirement):

```bash
cd c
../out/ctester.pex
```

## Common Issues

### Error: test directory not found: tests

Cause: running from repository root instead of c.

Fix:

```bash
cd c
python3 ../src/main.py
```

### gcc or make not found

Install system build tools and verify both commands are available in PATH.

### Import errors with python -m src.main

Do not use module mode here.
Use python3 ../src/main.py (from c) or python3 src/main.py only if your working directory already has tests/ and test_build/.

## Development Notes

- Python dependencies are in requirements.txt.
- Packaging metadata is in pyproject.toml.
- Console script entry point is simple-c-tester = main:entry.
