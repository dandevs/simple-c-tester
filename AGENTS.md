# AGENTS.md

## Project
C test runner with a Textual TUI. Discovers `*.c` files under `c/tests/`, compiles each via a generated Makefile, runs the binary, and renders live results in a Unicode tree view.

## Running
```bash
# Must run from c/ — the app resolves tests/ and test_build/ relative to CWD
cd c
python3 ../src/main.py
```
- Do **NOT** use `python3 -m src.main` — imports are bare (`from models import ...`) and rely on `sys.path` manipulation in `main.py`
- No positional source-dir argument; test path is hardcoded as `tests`

## CLI Flags
- `--parallel N` (default 4)
- `--watch` — file change monitoring, re-runs affected tests
- `--output-lines N` (default 10) — max lines in inline output boxes
- `--theme ansi|default` (default `ansi`)
- `--timeline` — globally enable per-line Test Story capture with gdb for all tests in the main list
- `--debug-build` — compile tests with debug flags (`-g -O0`)
- `--story-filter-profile minimal|balanced|all` (default `balanced`) — selects Test Story stop/filter profile
- `--tsv-lines-above N` (default 4) — source lines shown above current frame
- `--tsv-lines-below N` (default 4) — source lines shown below current frame
- `--tsv-skip-seq-lines N` (default 10) — thin out sequential same-file frames in record mode
- `--tsv-vars-depth N` (default 2) — variable expansion depth in the Test Story viewer
- `--tsv-variables-height N` (default 10) — minimum height for the variables panel
- `--tsv-show-reason-about` — shows verbose trigger "reason/about" detail text in card titles (hidden by default)
- `--tsv-var-history N` (default 3) — max historical values shown per variable on a single source line in Test Story cards; when a line is visited multiple times, values are compressed into `[i=9,8,7]` instead of `[i=9] [i=8] [i=7]`

## Setup
```bash
pip install -r requirements.txt
```
Requires Python 3.9+, gcc, and make on PATH.
On Linux with PEP 668 / externally-managed Python, install dependencies in a virtualenv instead:
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Build (PEX)
```bash
./build.sh   # → out/ctester.pex
```
Cross-platform (Linux/macOS/Windows, CPython 3.9). Run from `c/`: `../out/ctester.pex`.

## Lint / Typecheck / Tests
- No linter, formatter, or typechecker is configured in this repo
- No Python test suite exists — the "tests" are C files in `c/tests/`
- Verification: run the app itself against the C tests

## Architecture
```
src/main.py        entry, argparse, asyncio.run
src/app.py         TestRunnerApp (Textual)
src/state.py       global mutable state singleton
src/models/        Test, Suite, AppState, TestState
src/render/        tree/box rendering, labels, screen classes, Test Story UI
src/render/test_output_screen.py   TestOutputScreen class
src/render/test_debugger_screen.py  TestDebuggerScreen class
src/render/test_debugger_screen_utils/  utilities: source_utils, frame_utils, render_utils
src/runner/        makefile generation, test execution, dep graph, gdb/MI debugger
src/runner/story_filters/ modular Test Story stop/filter engine + trigger heuristics
src/watch/         watchdog debounce handler
src/runner/artifacts.py  path/name mangling for build artifacts
```

- `src/runner/makefile.py` — `generate_makefile()`, include path resolution (`resolve_include_dirs` via iterative `gcc -E`), project source discovery and `libproject.a` build
- `src/runner/execute.py` — `run_test()` invokes `make` then the binary; `state_changed()` dispatches tests via `asyncio.ensure_future()`; also owns Test Story/debug session orchestration, editor-breakpoint cache loading, and cancellation/rebuild restore flow
- `src/runner/dwarf_core/` — reusable DWARF resolver core for line-table lookup, source-expression parsing, and inline variable annotations
- `src/runner/story_filters/` — profile config (`minimal`/`balanced`/`all`), trigger matcher set (function enter/exit, branch, loop milestones, goto, assert, anomaly, sync, first-hit), and decision engine
- `src/runner/debugger.py` — gdb MI controller used for Test Story capture and variable expansion
- `src/runner/state.py` — helpers for checking completion state
- All intra-src imports are bare (no `src.` prefix) — the package is not installed, `main.py` adds its own directory to `sys.path`

## Key Behaviors
- Compilation goes through `make -f test_build/Makefile`, not direct `gcc` — enables incremental builds via `.d` dependency files
- `.d` files are parsed after each make run to populate the dependency index for watch mode; persisted to `test_build/db.json` (which also stores user preferences like debug precision mode)
- Project `.c` files (excluding `main.c`, `tests/`, `test_build/`) are auto-discovered from resolved include dirs, compiled into `test_build/libproject.a`, and linked into each test — pre-built synchronously to avoid parallel race conditions
- Artifact names use a readable + hash scheme: `test_artifact_stem()` in `src/runner/artifacts.py`
- UI redraws the full tree every 100ms tick when state changes (single `RichLog` widget)
- `state_changed()` is sync, uses `asyncio.ensure_future()` to schedule async work
- Test Story opens a per-test debug page with code frames and a variables tree; exiting a running story cancels the test, restores normal build mode, and reruns it normally
- Opening the Test Story page enables capture for that test even without `--timeline`; `T` toggles capture for the selected test, while `--timeline` enables it globally for all tests
- The debug page now has two stepping precisions: `loose` uses smart/heuristic stepping, while `precise` keeps the older scheduler-locking style; `P` toggles precision and restarts the debugger from the beginning
- The selected precision mode (`loose`/`precise`) is persisted in `test_build/db.json` under `preferences.debug_precision_mode`; the default is `precise` (used for manual debug mode), applied on startup, and used as the default for newly discovered tests
- Manual debug startup loads breakpoints from `test_build/breakpoints.json` (override with `CTESTER_BREAKPOINTS_FILE`), filters to `.c`/`.cpp`, and if any are valid starts at the first breakpoint hit; otherwise it falls back to `main`
- `R` force-restarts a running debugger from the beginning if a step is in flight; `K` can interrupt even while another debug action is pending
- After a manual debug session exits, pressing `R` restarts manual debug mode (it no longer falls back to auto story capture)
- `Ctrl+Enter` toggles full-file code view, replacing the timeline card stack with an editor-style view centered on the selected line
- `?` opens a controls modal in the debug page; the footer now keeps only a short `? - Help` hint
- Controls modal now includes a 3-button story filter profile row (`Minimal`, `Balanced`, `All`) that updates defaults live and persists to `test_build/db.json` as `preferences.story_filter_profile`
- In auto story capture mode, cards are now emitted from modular trigger decisions instead of only line-thinning; capture records only trigger-matching source stops
- Trigger badges are shown on cards; verbose trigger "reason/about" text is hidden by default and can be shown with `--tsv-show-reason-about`
- In debug mode, left/right history navigation is still available, and debug steps re-follow the latest frame while arrow scrubbing keeps the current history position
- Variables expansion is driven by gdb MI (`pygdbmi`) and is frame-aware; expand/collapse state and per-frame scroll position are preserved in the viewer
- In non-manual story mode, initial frame selection now starts at the first frame/card (index `0`) when no prior selection is valid
- Aggregate variable annotations merge all captured variables from all timeline events into a single pool, shown only on card 0 in auto mode after the test completes; per-card variables are shown for cards 1+ and for manual debug mode
- `test.aggregate_annotations` (bool, default `True`) controls whether `_compute_story_annotations()` processes all events or respects `timeline_selected_event_index`; it is toggled based on card selection (True on card 0 in auto mode, False otherwise), ensuring db.json annotations match the currently selected card scope
- When a test fails with a compile error in auto story mode, the TSV card view is replaced with the gcc compile error output (with ANSI colors preserved) in the debug screen; manual debug mode keeps the normal card view behavior
- The `on_unmount()` lifecycle hook on `TestDebuggerScreen` clears both `story_annotations` (saved as empty dict to db.json) and `debugLine` (removed from db.json), ensuring no stale state persists after exiting the screen regardless of exit path
- A `debugLine` root-level entry is written to `test_build/db.json` in manual debug mode, tracking the currently selected card's source location (`{"filePath": "...", "lineNumber": N}`); it is updated on every card navigation (arrow keys, mouse click, drag) and on every debugger step; it is cleared on screen unmount

## Watch Mode Details
- Observes repo root (`.`) recursively — no need to pre-build watched directory lists
- File change handling is serialized via an `asyncio.Lock` in `handle_file_changes()` to prevent overlapping/racy requeue passes during rapid saves
- DebounceHandler tracks event kinds per-path (`dict[str, set[str]]`) and supports `modified`, `created`, `deleted`, `moved` events
- Directory-only `modified` events are filtered as noise (editors touching directory metadata should not trigger reruns)
- `test_build/breakpoints.json` updates refresh the in-memory editor-breakpoint cache for manual debug without forcing pro
ject rebuilds
- `tests/*.c` changes use precision reruns:
  - existing test file edited → rerun only that test (via dependency mapping or direct source match)
  - new test file created → add and run only that test
  - test file deleted → remove that test from state and suite tree
  - test file moved → treated as delete at old path + create at new path
- Conservative rerun-all fallback only for genuinely uncertain dependency cases:
  - unmapped changes under `src/`
  - unmapped `.c`/`.h` files outside `tests/`
  - directory create/delete/move events under `src/`
- Dependency graph readiness (`dep_graph_ready`) is invalidated when:
  - any test has a compile error (checked in `update_dep_graph_readiness()` and set in `run_test()`)
  - a runner error occurs
  - This prevents stale "ready" state from suppressing necessary rebuilds during error recovery
- If a Test Story page is open and the running test is exited, the app cancels the active run/debug session, restores normal build flags, and requeues that test for a normal rebuild/rerun

## Gotchas
- `requirements.txt` includes `pyperclip` for clipboard support in the output screen, but it's not listed in `pyproject.toml` dependencies — the app handles `ImportError` gracefully
- `pygdbmi` is required for Test Story/debug capture and must be available in the PEX/runtime environment
- `pyelftools` powers DWARF-backed inline annotation resolution; if it's unavailable (or a binary has no DWARF info), resolver calls degrade gracefully to no inline annotations without breaking test execution or UI rendering
- Inline annotations in the story viewer are resolver-backed when DWARF is available; card frames now carry `resolved_annotations` alongside the existing raw captured variables
- Full-file variable annotations in db.json are driven by a DWARF scope index that parses per-variable location lists (`DW_AT_location`) to determine exact PC live ranges, then maps those ranges to source lines via the line index; annotations only appear on lines where a captured variable is both alive (per DWARF) and referenced by name (per regex)
- The DWARF scope index is built lazily and cached inside `DwarfCoreApi`; if DWARF is unavailable or parsing fails, `_compute_story_annotations()` falls back to the previous snippet-window regex approach
- Card frames use a fast variable-capture path by default so story startup stays responsive; deeper recursive variable expansion is reserved for the heavier anomaly/debug paths
- The pex entry point is `main:entry` (not `src.main:entry`)
- On Linux systems with PEP 668 (`externally-managed-environment`), install deps in a virtualenv (`python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt`) for local source runs
