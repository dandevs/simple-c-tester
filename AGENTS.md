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
src/models/        Test, Suite, AppState, TestState, TestRun, DwarfCache
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
- `src/runner/dwarf_core/` — reusable DWARF resolver core for line-table lookup, source-expression parsing, inline variable annotations, and lexical scope extraction. All functions accept an optional `cache` parameter (a `DwarfCache` instance) instead of using module-level caches.
- `src/runner/story_filters/` — profile config (`minimal`/`balanced`/`all`), trigger matcher set (function enter/exit, branch, loop milestones, goto, assert, anomaly, sync, first-hit), and decision engine
- `src/runner/debugger.py` — gdb MI controller used for Test Story capture and variable expansion
- `src/runner/state.py` — helpers for checking completion state
- All intra-src imports are bare (no `src.` prefix) — the package is not installed, `main.py` adds its own directory to `sys.path`

## Code Navigation Guide

### `src/runner/execute.py` — Orchestration Layer
Core async runner and debug/session lifecycle. ~1650 lines.
- **`run_test(test, on_complete)`** — Entry point. Creates a fresh `TestRun`, checks binary mtime against `test.dwarf_cache`, resets binary caches if the binary changed, always resets runtime caches, compiles via `make`, then runs plain binary or auto debug trace.
- **`_run_auto_debug_trace(test, binary_path, proc_env)`** — Starts `GdbMIController`, configures it, creates a `StoryFilterEngine`, and runs the capture loop.
  - Inner **`_capture_story_stop(stop_event)`** — Called at **every** gdb stop.
    - Always calls `_capture_scope_variables_fast()` and `resolve_line_annotations()`.
    - Always merges annotations into `run.annotation_cache` via `merge_line_annotations_into_cache()`.
    - Uses `StoryFilterEngine.evaluate_without_variables()` / `evaluate_with_variables()` to decide matches.
    - Only calls `_record_stop_event()` (creates a visible card) when `matches` is non-empty.
  - Loop calls `_auto_trace_step(..., always_step_in=True)` so auto trace enters every function.
- **`_auto_trace_step(controller, stop_event, binary_path, always_step_in=False)`** — Chooses next gdb command. When `always_step_in=True`, unconditionally steps into user-code functions.
- **`_record_stop_event(..., line_annotations=None)`** — Appends a `TimelineEvent`. Accepts pre-computed annotations to avoid double work.
- **`start_debug_session(test, precision_mode)`** / **`stop_debug_session(test)`** — Manual debug lifecycle. Creates fresh `TestRun`, checks binary mtime against `test.dwarf_cache`, resets caches as needed, sets up breakpoints, initial stop, and teardown.
- **`_debug_step(test, action)`** — Handles manual actions (`next`, `auto`, `step_in`, `step_out`, `continue`, `interrupt`). `auto` uses `_auto_trace_step` with heuristic stepping.
- **`_capture_scope_variables(controller, ...)`** — Deep recursive variable expansion (gdb `var_create` / `var_list_children`).
- **`_capture_scope_variables_fast(controller, ...)`** — Lightweight frame variables without deep expansion. Used for cache population and synthetic events.
- **`_capture_global_variables(controller, binary_path, locals)`** — Evaluates global/static variables via `evaluate_global()`.
- **`state_changed()`** — Synchronous dispatcher. Schedules `run_test()` coroutines via `asyncio.ensure_future()`.
- **`update_annotation_cache(test, event)`** — Wraps `_merge_event_annotations_into()`.

### `src/runner/debugger.py` — GDB MI Controller
Async wrapper around gdb's MI2 interpreter.
- **`GdbMIController`** class:
  - `start()` — launches `gdb --quiet --interpreter=mi2`.
  - `configure()` — pagination off, confirm off, pretty print, skip system files.
  - `configure_manual_stepping()` — sets `scheduler-locking step` (used in `precise` mode).
  - Stepping: `next()`, `step_in()`, `step_out()`, `continue_run()`, `interrupt()`.
  - Breakpoints: `break_main_and_run()`, `insert_breakpoint()`, `delete_breakpoint()`, `list_breakpoints()`.
  - Variables: `list_simple_variables()`, `list_all_variables()`, `var_create()`, `var_list_children()`, `var_delete()`, `var_evaluate()`, `evaluate_expression()`.
  - `shutdown()` — graceful exit + terminate/kill.
- **`DebugStopEvent`** — dataclass: `reason`, `file_path`, `line`, `program_counter`, `function`, `exit_code`, `signal_name`, `raw`.
- **`stop_event_is_terminal()`** — returns `True` for `exited-*` or `signal-received` (except `SIGINT`).

### `src/runner/story_filters/` — Filter Engine
Decides which debug stops become visible timeline cards.
- **`config.py`** — `StoryFilterConfig` dataclass. Profiles: `minimal` (4 triggers), `balanced` (10 triggers), `all` (12 triggers). `normalized_story_filter_profile()`.
- **`engine.py`** — `StoryFilterEngine`:
  - `evaluate_without_variables(stop_event)` → `StoryFilterDecision(emit, matches, need_variables)`.
  - `evaluate_with_variables(stop_event, variables)` → adds variable-dependent triggers (e.g. `anomaly`).
  - `mark_processed(stop_event)` — updates `previous_stop` and `step_index`.
- **`triggers.py`** — Individual matchers:
  - `function_enter`, `function_exit`, `branch_decision`, `loop_milestone`, `goto_jump`, `assert_line`, `assert_failure`, `anomaly`, `sync_event`, `first_hit_function`, `first_hit_line`, `standalone_expr`.
  - `trigger_needs_variables()` — only `anomaly` requires runtime variable values.

### `src/runner/story_annotations.py` — Annotation Cache & Pipeline
Builds inline `[expr=value]` annotations for the story viewer.
- **`get_story_annotations(test, event_boundary=None, cache=None)`** — Public API. Returns `{abs_path: {line: [annotation_strs]}}`. Uses `cache.annotation_cache` when provided; otherwise uses a temporary dict.
- **`_compute_story_annotations(test)`** — Reads `run.annotation_cache` when `aggregate=True`, or slices events up to `timeline_selected_event_index` when `aggregate=False`.
- **`merge_line_annotations_into_cache(cache, file_path, function, line_annotations)`** — Injects resolved annotations directly into the Store A cache **without** requiring a `TimelineEvent`. Used by `_capture_story_stop` for non-triggered stops.
- **`_merge_event_annotations_into(cache, event)`** — Merges a `TimelineEvent`'s `line_annotations` into the cache. Delegates to `merge_line_annotations_into_cache()` internally.
- **`format_story_annotations_for_db(annotations, cache=None)`** — Converts dict to db.json list format `[[lineText, lineNo, [str...]], ...]`. Uses `cache.source_line_cache` when provided.
- **`invalidate_story_annotation_cache(test, cache=None)`** — Clears annotation cache for a test (called on screen unmount or before new runs).

### `src/runner/annotation_resolver.py` — Per-Line Expression Evaluation
- **`resolve_line_annotations(line_text, line_number, debugger)`** — Extracts C expressions from source via `extract_expressions()` and evaluates each via `debugger.evaluate_expression()`. Returns `{line_number: ["[expr=val]", ...]}`.
- **`extract_expressions()`** lives in `src/runner/expression_tokenizer.py`.

### `src/runner/dwarf_core/` — DWARF Resolver
Provides DWARF-backed liveness and inline annotations.
- **`function_index.py`** — `get_function_index(binary_path, cache=None)`: Builds a `FunctionIndex` from DWARF. Uses `cache.function_index_cache` when provided; falls back to a module-level fallback dict for backward compatibility.
- **`global_index.py`** — `get_global_variables(binary_path, cache=None)`: Builds a `GlobalVariableIndex` from DWARF. Uses `cache.global_index_cache` when provided.
- **`type_resolver.py`** — `resolve_variable_type(binary_path, variable_name, file_path, line, cache=None)`: Resolves variable type info from DWARF. Uses `cache.type_index_cache` when provided.
- **`variable_scopes.py`** — `build_scope_index(line_index, dwarf_info)`:
  - Walks DWARF DIEs for `DW_TAG_variable` / `DW_TAG_formal_parameter`.
  - Parses `DW_AT_location` (exprloc or loclist) into `DwarfVariableLiveRange`s.
  - Maps live PC ranges to source lines via `line_index` → `DwarfScopeIndex(file_lines={abs_path: {line: (var_names,)}})`.
- **`lexical_scopes.py`** — `build_lexical_scope_index(line_index, dwarf_info)`:
  - Walks DWARF DIEs for `DW_TAG_subprogram`, `DW_TAG_lexical_block`, and `DW_TAG_inlined_subroutine`.
  - Extracts `DW_AT_low_pc`/`DW_AT_high_pc` for each block and maps to source lines.
  - Builds a `LexicalScopeIndex` containing `DwarfScopeBlock` entries with parent-child relationships.
  - `LexicalScopeIndex.get_scope_chain(pc)` returns all enclosing blocks outermost-first.
- **`resolver.py`** — `resolve_inline_annotations()`:
  - `_resolve_with_loaded_data()` → `_resolve_location()` + `_build_runtime_variable_map()` + `_build_annotations()`.
  - `_check_liveness()` — consults optional `liveness_checker` (the scope index).
- **`models.py`** — Dataclasses: `DwarfLoaderResponse`, `DwarfScopeIndex`, `DwarfScopeBlock`, `LexicalScopeIndex`, `DwarfResolveRequest`, `ResolvedVariableAnnotation`, `DwarfLineIndex`, etc.
- **`loader.py`** — `load_dwarf_data()` → parses ELF/DWARF, builds line index, scope index, and lexical scope index.
- **`line_index.py`** — `lookup_address()` for PC-to-source mapping.
- **`api.py`** — Previously housed `DwarfCoreApi` and `create_dwarf_core_api`; both were removed as dead code. The module now only contains a docstring.

### `src/render/test_debugger_screen.py` — Test Story / Debug UI
Textual `Screen` subclass for the debug/story view.
- **`TestDebuggerScreen`**:
  - `on_mount()` — enables `timeline_capture_enabled`, starts debug session if idle.
  - `on_unmount()` — cancels debug, clears `story_annotations` and `debugLine` from db.json.
  - `_maybe_refresh_dwarf_cache()` — Checks binary mtime against `test.dwarf_cache.last_binary_mtime`. If the binary changed, resets binary caches and updates tracking fields. Always resets runtime caches. Called before every user-initiated restart.
  - `_line_frames()` — Builds visible frame list from `run.timeline_events`. Applies sequential-line thinning (`tsv_skip_seq_lines`) only in non-debug mode.
  - `_render_code_panel()` — Renders full-file view (`render_full_file_panel`) or card stack (`render_code_panel`). Uses `get_story_annotations(test, cache=test.dwarf_cache)` for inline annotations.
  - `_render_variables_panel(selected_event)` — Shows variables. Uses `_variables_cache` for expanded vars when debug is active; falls back to `event.variables` otherwise.
  - `_fetch_expanded_variables_for_frame()` — Async deep expansion via `_capture_scope_variables()` for the selected frame.
  - Actions: `action_step_next()`, `action_step_in()`, `action_step_out()`, `action_continue_run()`, `action_interrupt_run()`, `action_toggle_precision()`, `action_toggle_full_file_view()`, `action_toggle_timeline()`, timeline scrub (`left`/`right`), etc.
- **`DebugControlsModal`** — Modal screen listing keybindings and a 3-button profile selector (`Minimal`/`Balanced`/`All`).

### `src/render/test_debugger_screen_utils/`
- **`source_utils.py`** — `load_source_lines()`, `display_path()`, `detect_language()`.
- **`frame_utils.py`** — `ensure_selected_frame_index()`, `compute_frame_cards_window()`, `event_has_useful_source_line()`.
- **`render_utils.py`** — `build_frame_snippet()`, `build_variables_tree()`, `render_code_panel()`, `render_full_file_panel()`. `render_code_panel` passes `cache=test.dwarf_cache` to `get_story_annotations()`.

### Data Flow Summary (Auto Trace)
1. `run_test()` creates fresh `TestRun`, checks binary mtime against `test.dwarf_cache`, resets caches, compiles binary → `_run_auto_debug_trace()` starts `GdbMIController`.
2. Loop: `_auto_trace_step(always_step_in=True)` advances gdb into every function.
3. At every stop, `_capture_story_stop()`:
   a. Captures lightweight variables + resolves line annotations.
   b. Merges annotations into `run.annotation_cache` unconditionally.
   c. Runs `StoryFilterEngine` to decide if this stop is "interesting".
   d. If matches exist: captures full variables + globals → `_record_stop_event()` → `run.timeline_events.append()`.
4. UI (`TestDebuggerScreen`) reads `run.timeline_events` for cards and `run.annotation_cache` (via `get_story_annotations(test, cache=test.dwarf_cache)`) for full-file inline annotations.

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
- `run.aggregate_annotations` (bool, default `True`) controls whether `_compute_story_annotations()` processes all events or respects `timeline_selected_event_index`; it is toggled based on card selection (True on card 0 in auto mode, False otherwise), ensuring db.json annotations match the currently selected card scope
- When a test fails with a compile error in auto story mode, the TSV card view is replaced with the gcc compile error output (with ANSI colors preserved) in the debug screen; manual debug mode keeps the normal card view behavior
- The `on_unmount()` lifecycle hook on `TestDebuggerScreen` clears both `story_annotations` (saved as empty dict to db.json) and `debugLine` (removed from db.json), ensuring no stale state persists after exiting the screen regardless of exit path
- A `debugLine` root-level entry is written to `test_build/db.json` in manual debug mode, tracking the currently selected card's source location (`{"filePath": "...", "lineNumber": N}`); it is updated on every card navigation (arrow keys, mouse click, drag) and on every debugger step; it is cleared on screen unmount

## Per-Run State Isolation (TestRun + DwarfCache)
- **`TestRun`** dataclass holds all mutable state for a single test execution (`timeline_events`, `annotation_cache`, `scope_buckets`, `debug_logs`, `stdout`, `stderr`, `compile_err`, `debug_running`, `debug_exited`, `debug_exit_code`, `aggregate_annotations`, `timeline_selected_event_index`). A fresh `TestRun()` is created on every `run_test()` and `start_debug_session()`.
- **`DwarfCache`** dataclass holds all DWARF/annotation caches (`dwarf_loader_cache`, `function_index_cache`, `global_index_cache`, `type_index_cache`, `lexical_scope_cache`, `source_line_cache`, `annotation_cache`) plus binary tracking fields (`last_binary_path`, `last_binary_mtime`). Owned by `Test` and persists across runs.
- **Binary metadata caches** (dwarf_loader, function_index, global_index, type_index, lexical_scope) are expensive to rebuild; they persist when the binary is unchanged (detected via mtime comparison).
- **Runtime caches** (source_line, annotation) are cheap and depend on execution behavior; they are reset on every run.
- `_debug_callbacks()` in `execute.py` captures the `TestRun` instance at setup time so old async tasks from previous runs silently drop data if a newer run has superseded them.
- **Scope buckets**: Each `TimelineEvent` is placed into a nested tree of `ScopeBucket` objects (one tree per source file). Buckets represent DWARF lexical blocks mapped to source line ranges. The `latest_event` field on each bucket holds the most recent event whose PC fell inside that block (and its deepest matching child). This enables block-aware navigation and filtering in the story viewer.

## Watch Mode Details
- Observes repo root (`.`) recursively — no need to pre-build watched directory lists
- File change handling is serialized via an `asyncio.Lock` in `handle_file_changes()` to prevent overlapping/racy requeue passes during rapid saves
- DebounceHandler tracks event kinds per-path (`dict[str, set[str]]`) and supports `modified`, `created`, `deleted`, `moved` events
- Directory-only `modified` events are filtered as noise (editors touching directory metadata should not trigger reruns)
- `test_build/breakpoints.json` updates refresh the in-memory editor-breakpoint cache for manual debug without forcing project rebuilds
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
- The DWARF scope index is built lazily inside the per-Test `DwarfCache`; if DWARF is unavailable or parsing fails, `_compute_story_annotations()` falls back to the previous snippet-window regex approach
- Card frames use a fast variable-capture path by default so story startup stays responsive; deeper recursive variable expansion is reserved for the heavier anomaly/debug paths
- The pex entry point is `main:entry` (not `src.main:entry`)
- On Linux systems with PEP 668 (`externally-managed-environment`), install deps in a virtualenv (`python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt`) for local source runs
