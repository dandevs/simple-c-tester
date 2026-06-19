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
- `--parallel N` (default 4) — parallel worker count
- `--watch` — file change monitoring, re-runs affected tests
- `--get-dependencies FILE` — print dependency info for FILE and exit. For a **test** file, runs the real build phases (generate Makefile → build `libproject.a` → link test) and reads the actual deps from the resulting `.d`/`.map` artifacts (include dirs, transitive headers, sources actually linked, skipped-sources). For a **non-test** file, falls back to static `gcc -MM` analysis (no link step). Label clearly distinguishes the two.
- `--output-lines N` (default 10) — max lines in inline output boxes
- `--theme ansi|default` (default `ansi`)
- `--timeline` — globally enable per-line Test Story capture with gdb for all tests in the main list
- `--debug-build` — compile tests with debug flags (`-g -O0`)
- `--no-sanitize` — disable AddressSanitizer + UndefinedBehaviorSanitizer compile/link flags (on by default)
- `--leak-sanitizer` — enable LeakSanitizer runtime detection (`ASAN_OPTIONS detect_leaks`). Off by default. Also toggleable in the Options menu (`o`), persisted to user config.
- `--story-filter-profile minimal|balanced|all` (default `balanced`) — selects Test Story stop/filter profile
- `--tsv-lines-above N` (default 4) — source lines shown above current frame
- `--tsv-lines-below N` (default 4) — source lines shown below current frame
- `--tsv-skip-seq-lines N` (default 10) — thin out sequential same-file frames in record mode
- `--tsv-vars-depth N` (default 2) — variable expansion depth in the Test Story viewer
- `--tsv-variables-height N` (default 10) — minimum height for the variables panel
- `--tsv-show-reason-about` — shows verbose trigger "reason/about" detail text in card titles (hidden by default)
- `--tsv-var-history N` (default 3) — max historical values shown per variable on a single source line in Test Story cards; when a line is visited multiple times, values are compressed into `[i=9,8,7]` instead of `[i=9] [i=8] [i=7]`
- `--debug-log` — write a timestamped scheduling/cancel diagnostic trace to `test_build/log.txt`. Zero overhead when absent. Used to debug runaway rerun loops (e.g. Running↔Cancelled cycling). See `src/core/debug_log.py`.
- `--cflags` — extra compiler/linker flags (e.g. `-lreadline -Wextra -Werror`)

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
src/main.py                 entry, argparse, asyncio.run
src/state.py                global mutable state singleton (legacy)
src/core/                   build system, config, domain models, user settings, debug log
src/core/build.py           generate_makefile, discover_project_sources, build_project_sources,
                            dependency graph (dep_index, dep_graph_ready), db.json persistence,
                            analyze_test_build (artifact-based dependency read)
src/core/config.py          RunnerConfig dataclass (compile flags, sanitizer, UI—resolved each run)
src/core/userconfig.py      persisted user settings via OptionField spec + ~/.config/ctester/config.json
src/core/debug_log.py       optional diagnostic logger (--debug-log), zero-overhead when off
src/core/state.py           RunnerState dataclass (the "real" state — legacy globals mirror it)
src/core/models/models.py   Test, Suite, AppState, TestRun, DwarfCache, has_main_definition
src/core/debugger.py        GdbMIController (gdb MI2 wrapper)
src/core/assertions.py      assertion failure parser
src/core/events.py          simple event bus
src/api/                    runner engine (async test execution, debug orchestration)
src/api/__init__.py         TestRunnerApi — public API, visibility-priority scheduling
src/api/_runner.py          run_test, state_changed, _run_auto_debug_trace, start_debug_session,
                            cancel_test_and_restore_normal_build, _preempt_test, per-run env setup
src/api/_variable_tree.py   gdb-backed variable tree builder
src/api/_headless_smoke.py  headless smoke test runner
src/runner/                 thin shim/wrapper layer bridging API calls to legacy state module
src/runner/execute.py       shim over api/_runner.py for backwards compat
src/runner/makefile.py      shim over core/build.py (RunnerState ↔ legacy globals)
src/runner/debugger.py      GdbMIController shim (re-exports from core/debugger.py)
src/runner/story_annotations.py  annotation cache & pipeline
src/runner/story_filters/   modular Test Story stop/filter engine + trigger heuristics
src/render/                 tree/box rendering, labels, screen classes, Test Story UI
src/render/test_output_screen.py   TestOutputScreen — scrollable log with drag-select
src/render/test_debugger_screen.py  TestDebuggerScreen — Test Story / debug UI
src/render/test_debugger_screen_utils/  utilities: source_utils, frame_utils, render_utils
src/render/options_screen.py  data-driven settings screen (driven by OPTION_FIELDS)
src/watch/                  watchdog debounce handler, change processing, file-loop breakpoints
```

- All intra-src imports are bare (no `src.` prefix) — the package is not installed, `main.py` adds its own directory to `sys.path`
- `src/core/build.py` is the single home for build logic. `src/runner/makefile.py` is a thin shim that bridges `RunnerState` ↔ the legacy `state` module (reads globals into a transient RunnerState before each call and writes scalars back).
- `src/api/_runner.py` is the single home for async test orchestration. `src/runner/execute.py` re-exports for backward compat.
- `src/core/userconfig.py` owns the Options-driver spec (`OPTION_FIELDS`) and persistence (`~/.config/ctester/config.json`). The menu screen (`OptionsScreen` in `render/options_screen.py`) derives from it — one place to add or retune a setting.

## Code Navigation Guide

### `src/api/_runner.py` — Orchestration Layer
Core async runner and debug/session lifecycle. ~2030 lines.
- **`run_test(test, on_complete)`** — Entry point. Creates a fresh `TestRun`, checks binary mtime against `test.dwarf_cache`, resets binary caches if the binary changed, always resets runtime caches, compiles via `make`, then runs plain binary or auto debug trace.
- **`_run_auto_debug_trace(test, binary_path, proc_env)`** — Starts `GdbMIController`, configures it, creates a `StoryFilterEngine`, and runs the capture loop. Checks `test.state == CANCELLED` at the top of every step and at exit — returns early if cancelled (which triggers `on_complete` re-queue via `rerun_after_user_cancel`).
  - Inner **`_capture_story_stop(stop_event)`** — Called at **every** gdb stop.
    - Always calls `_capture_scope_variables_fast()` and `resolve_line_annotations()`.
    - Always merges annotations into `run.annotation_cache` via `merge_line_annotations_into_cache()`.
    - Uses `StoryFilterEngine.evaluate_without_variables()` / `evaluate_with_variables()` to decide matches.
    - Only calls `_record_stop_event()` (creates a visible card) when `matches` is non-empty.
  - Loop calls `_auto_trace_step(..., always_step_in=True)` so auto trace enters every function.
- **`_auto_trace_step(controller, stop_event, binary_path, always_step_in=False)`** — Chooses next gdb command. When `always_step_in=True`, unconditionally steps into user-code functions.
- **`_record_stop_event(..., line_annotations=None)`** — Appends a `TimelineEvent`. Accepts pre-computed annotations to avoid double work.
- **`start_debug_session(test, precision_mode)`** / **`stop_debug_session(test)`** — Manual debug lifecycle. Creates fresh `TestRun`, sets `active_debug_test_key` (which gates watch-handler deferral), sets up breakpoints, initial stop, and teardown.
- **`_debug_step(test, action)`** — Handles manual actions (`next`, `auto`, `step_in`, `step_out`, `continue`, `interrupt`). `auto` uses `_auto_trace_step` with heuristic stepping.
- **`_capture_scope_variables(controller, ...)`** — Deep recursive variable expansion (gdb `var_create` / `var_list_children`).
- **`_capture_scope_variables_fast(controller, ...)`** — Lightweight frame variables without deep expansion.
- **`_capture_global_variables(controller, binary_path, locals)`** — Evaluates global/static variables.
- **`state_changed()`** — Synchronous dispatcher. Picks pending tests (sorted by `time_state_changed`, smallest first / FIFO), sets them RUNNING, fires `run_test()` coroutines via `asyncio.ensure_future()`. Each coroutine carries an `on_complete` closure that frees a runner slot, handles CANCELLED→PENDING re-queue (via `rerun_after_user_cancel` or `cancelled_by_user` state), and calls `state_changed()` again.
- **`_build_proc_env()`** — Builds subprocess environment: sets `COLUMNS` and injects `ASAN_OPTIONS=... detect_leaks=<0|1>` explicitly (appended last to win over ambient env). Leak sanitizer is OFF by default; toggleable via `--leak-sanitizer` or the Options menu.
- **`cancel_test_and_restore_normal_build(test)`** — Cancels a running test, sets `rerun_after_user_cancel=True` (so `on_complete` re-queues it), stops debug session, restores normal build mode. Called on screen unmount and explicit cancel.
- **`_preempt_test(test)`** — Lighter cancel for search-mode priority preemption (sets `rerun_after_user_cancel=True`, does not stop debug or rebuild). Called by visibility-priority scheduling when search is active.
- **`_ensure_debug_build_mode(enabled)`** — Toggles `debug_build_enabled` and regenerates Makefile + project sources + dep graph. Called on every timeline-captured run.
- **`on_complete(completed_test)`** — Frees a runner slot and, if the test is CANCELLED, re-queues it as PENDING based on `rerun_after_user_cancel` / `cancelled_by_user`. Then calls `state_changed()`.

### `src/runner/debugger.py` — GDB MI Controller
Async wrapper around gdb's MI2 interpreter. Same as `src/core/debugger.py` (shim).
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

### `src/core/debugger.py` — GDB MI Controller (canonical)
Re-exports from `src/runner/debugger.py` for the core layer. All gdb MI logic lives here.

### `src/runner/story_annotations.py` — Annotation Cache & Pipeline
Builds inline `[expr=value]` annotations for the story viewer.
- **`get_story_annotations(test, event_boundary=None, cache=None)`** — Public API. Returns `{abs_path: {line: [annotation_strs]}}`. Uses `cache.annotation_cache` when provided; otherwise uses a temporary dict.
- **`_compute_story_annotations(test)`** — Reads `run.annotation_cache` when `aggregate=True`, or slices events up to `timeline_selected_event_index` when `aggregate=False`.
- **`merge_line_annotations_into_cache(cache, file_path, function, line_annotations)`** — Injects resolved annotations directly into cache **without** requiring a `TimelineEvent`.
- **`_merge_event_annotations_into(cache, event)`** — Merges a `TimelineEvent`'s `line_annotations` into the cache.
- **`format_story_annotations_for_db(annotations, cache=None)`** — Converts dict to db.json list format.
- **`invalidate_story_annotation_cache(test, cache=None)`** — Clears annotation cache for a test.

### `src/runner/expression_tokenizer.py` / `src/runner/annotation_resolver.py`
Expression extraction and per-line C expression evaluation for inline annotations.

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

### `src/core/dwarf_core/` — DWARF Resolver
Provides DWARF-backed liveness and inline annotations.
- **`function_index.py`** — `get_function_index(binary_path, cache=None)`: Builds a `FunctionIndex` from DWARF. Uses `cache.function_index_cache` when provided.
- **`global_index.py`** — `get_global_variables(binary_path, cache=None)`: Builds a `GlobalVariableIndex` from DWARF.
- **`type_resolver.py`** — `resolve_variable_type(binary_path, variable_name, file_path, line, cache=None)`: Resolves variable type info from DWARF.
- **`variable_scopes.py`** — `build_scope_index(line_index, dwarf_info)`:
  - Walks DWARF DIEs for `DW_TAG_variable` / `DW_TAG_formal_parameter`.
  - Parses `DW_AT_location` (exprloc or loclist) into `DwarfVariableLiveRange`s.
  - Maps live PC ranges to source lines via `line_index` → `DwarfScopeIndex`.
- **`resolver.py`** — `resolve_inline_annotations()`:
  - `_resolve_with_loaded_data()` → `_resolve_location()` + `_build_runtime_variable_map()` + `_build_annotations()`.
  - `_check_liveness()` — consults optional `liveness_checker` (the scope index).
- **`models.py`** — Dataclasses: `DwarfLoaderResponse`, `DwarfScopeIndex`, `DwarfResolveRequest`, `ResolvedVariableAnnotation`, `DwarfLineIndex`, etc.
- **`loader.py`** — `load_dwarf_data()` → parses ELF/DWARF, builds line index and scope index.
- **`line_index.py`** — `lookup_address()` for PC-to-source mapping.
- **`api.py`** — Previously housed `DwarfCoreApi` and `create_dwarf_core_api`; both removed as dead code.

### `src/render/test_debugger_screen.py` — Test Story / Debug UI
Textual `Screen` subclass for the debug/story view.
- **`TestDebuggerScreen`**:
  - `on_mount()` — enables `timeline_capture_enabled`, starts debug session if idle (via `action_rerun_test` → `_queue_story_capture` if auto, or `_restart_debug_session` if manual). Registers 100ms tick.
  - `on_unmount()` — cancels debug task, calls `cancel_test_and_restore_normal_build()`, clears story annotations and debugLine from db.json.
  - `_maybe_refresh_dwarf_cache()` — Checks binary mtime against `test.dwarf_cache.last_binary_mtime`. If binary changed, resets binary caches and updates tracking fields. Always resets runtime caches.
  - `_line_frames()` — Builds visible frame list from `run.timeline_events`. Applies sequential-line thinning (`tsv_skip_seq_lines`) only in non-debug mode.
  - `_render_code_panel()` — Renders full-file view or card stack. Uses `get_story_annotations(test, cache=test.dwarf_cache)`.
  - `_render_variables_panel(selected_event)` — Shows variables. Uses `_variables_cache` for expanded vars when debug is active; falls back to event.variables otherwise.
  - `_fetch_expanded_variables_for_frame()` — Async deep expansion via `_capture_scope_variables()`.
  - Actions: `action_step_next()`, `action_step_in()`, `action_step_out()`, `action_continue_run()`, `action_interrupt_run()`, `action_toggle_precision()`, `action_toggle_full_file_view()`, `action_toggle_timeline()`, timeline scrub (`left`/`right`), etc.
- **`DebugControlsModal`** — Modal screen listing keybindings and a 3-button story filter profile selector (`Minimal`/`Balanced`/`All`).

### `src/render/test_debugger_screen_utils/`
- **`source_utils.py`** — `load_source_lines()`, `display_path()`, `detect_language()`.
- **`frame_utils.py`** — `ensure_selected_frame_index()`, `compute_frame_cards_window()`, `event_has_useful_source_line()`.
- **`render_utils.py`** — `build_frame_snippet()`, `build_variables_tree()`, `render_code_panel()`, `render_full_file_panel()`. `render_code_panel` passes `cache=test.dwarf_cache` to `get_story_annotations()`.

### `src/render/test_output_screen.py` — Output / Clipboard Screen
Textual `Screen` showing full stdout/stderr for a single test.
- **Drag-to-select**: implements `on_mouse_down`/`on_mouse_move`/`on_mouse_up` for text selection. On release, copies selected text to clipboard via `copy_to_clipboard()` (supports pyperclip, wl-copy, xclip).

  **Throttling (`SELECTION_RENDER_INTERVAL = 0.02`)**: Every mouse-move event during drag updates the cursor immediately (cheap) but defers the full RichLog re-render via a coalescing timer. This prevents O(lines) clear+rewrite from flooding the Textual message queue and freezing the TUI on large outputs. See `test_output_screen.py` for details.
- **Footer**: shows "Drag Select + Copy" and "Ctrl+C/Esc Go Back".

### `src/render/options_screen.py` — Options Menu
Data-driven settings screen driven by `OPTION_FIELDS` from `core/userconfig.py`. Opened with `o`. Renders steppers (numeric +/-), cycles (rotating choices), and toggles (boolean). CLI-overridden fields are displayed at their effective value and locked for the session. `on_change(key, value)` callback notifies `app.py:apply_option()` which mutates live state + persists.

### `src/core/userconfig.py` — Persisted User Config
Single source of truth for user-editable settings. `OPTION_FIELDS` tuple declares every field with key, label, group, kind, default, min/max, choices. Saved to `~/.config/ctester/config.json` (XDG-aware). CLI resolution: cli > userconfig > builtin default. See `main.py:_build_config()`'s `resolve()`.

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
- Compilation goes through `make -f test_build/Makefile`, not direct `gcc` — enables incremental builds via `.d` dependency files.
- `.d` files are parsed after each make run to populate the dependency index for watch mode; persisted to `test_build/db.json` (which also stores user preferences like debug precision mode).
- Project `.c` files (excluding `main.c`, `tests/`, `test_build/`) are auto-discovered from resolved include dirs **and the conventional `src/` implementation tree** (so projects that separate `include/` headers from `src/` sources — incl. nested modules like `src/interpreter/` — link correctly), compiled into `test_build/libproject.a`, and linked into each test — pre-built synchronously to avoid parallel race conditions.
- **Skip-on-error**: a project source that fails to compile is dropped from `libproject.a` rather than failing the whole archive, so one broken WIP file (e.g. a module the project's own Makefile excludes) cannot block every test. The generated Makefile uses error-tolerant object rules (`-gcc … || rm -f $@`) and an archive recipe that gathers only the objects that actually compiled. Skipped sources are recorded in `RunnerState.skipped_sources` (mirrored to global `state.skipped_sources` via the legacy `runner/makefile.py` shim); headless prints a stderr warning. Tests that genuinely need a skipped symbol still surface a precise linker "undefined reference" (captured in `run.compile_err`).
- **Non-test `.c` filtering**: test discovery (`populate_suites` / `_build_suite` and watch-mode new-file path) now skips `.c` files that don't define a valid `int main(...)` entry point. The `has_main_definition()` helper strips C comments first and uses a whitespace-tolerant regex with trailing `{` to distinguish definitions from prototypes/call sites. Conservative on read error: unreadable files are treated as tests so failures surface visibly. Filtered files are skipped silently.
- **`--get-dependencies`** for test files runs the real build phases (generate_makefile → build_project_sources → link test) and reads `.d`/`.map` artifacts to report the **actually-linked** source count (the bare minimum from the linker map) contrasted with the full discovery set and any skip-on-error drops. Non-test files use a static `gcc -MM` fallback, clearly labeled.
- Artifact names use a readable + hash scheme: `test_artifact_stem()` in `src/runner/artifacts.py`.
- UI redraws the full tree every 100ms tick when state changes (single `RichLog` widget).
- `state_changed()` is sync, uses `asyncio.ensure_future()` to schedule async work. Each dispatched test carries an `on_complete` closure that manages `available_runners` and handles CANCELLED re-queue state.
- Test Story opens a per-test debug page with code frames and a variables tree; exiting a running story cancels the test, restores normal build mode, and reruns it normally.
- Opening the Test Story page enables capture for that test even without `--timeline`; `T` toggles capture for the selected test, while `--timeline` enables it globally for all tests.
- The debug page has two stepping precisions: `loose` uses smart/heuristic stepping, while `precise` keeps the older scheduler-locking style; `P` toggles precision and restarts the debugger.
- Selected precision mode (`loose`/`precise`) is persisted in `test_build/db.json` under `preferences.debug_precision_mode`; default `precise`.
- Manual debug startup loads breakpoints from `test_build/breakpoints.json` (override with `CTESTER_BREAKPOINTS_FILE`), filters to `.c`/`.cpp`, and if any are valid starts at the first breakpoint hit; otherwise falls back to `main`.
- `R` force-restarts a running debugger; `K` interrupts even while a debug action is pending.
- After a manual debug session exits, pressing `R` restarts manual debug mode (not auto story capture).
- `Ctrl+Enter` toggles full-file code view; `?` opens a controls modal.
- Controls modal includes a 3-button story filter profile row (`Minimal`/`Balanced`/`All`) that updates defaults live and persists to `test_build/db.json` as `preferences.story_filter_profile`.
- In auto story capture mode, cards are emitted from modular trigger decisions; capture records only trigger-matching source stops.
- Trigger badges shown on cards; verbose "reason/about" text hidden by default (`--tsv-show-reason-about` to show).
- Aggregate variable annotations merge all captured variables from all timeline events into a single pool, shown only on card 0 in auto mode after the test completes; per-card variables shown for cards 1+ and for manual debug mode.
- `run.aggregate_annotations` (bool, default `True`) controls whether `_compute_story_annotations()` processes all events or respects `timeline_selected_event_index`.
- When a test fails with a compile error in auto story mode, the TSV card view is replaced with the gcc compile error output (ANSI colors preserved); manual debug mode keeps normal card view.
- The `on_unmount()` hook on `TestDebuggerScreen` clears both `story_annotations` (saved as empty dict) and `debugLine` (removed) from db.json.
- A `debugLine` root-level entry is written to `test_build/db.json` in manual debug mode tracking the currently selected card's source location; updated on card navigation and debugger steps; cleared on screen unmount.
- **Leak sanitizer** is OFF by default. Enable via `--leak-sanitizer` CLI flag or the `o` Options menu (Execution group, persisted to `~/.config/ctester/config.json`). `_build_proc_env()` sets `ASAN_OPTIONS=... detect_leaks=<0|1>` explicitly (appended last to win over ambient env), so `--leak-sanitizer` reliably enables LSan even if the shell exports `detect_leaks=0`.
- **Drag-to-select throttling**: the output screen (`TestOutputScreen`) coalesces mouse-move redraws via `SELECTION_RENDER_INTERVAL` (0.02s ≈ 50fps). Without this, each RichLog re-render is O(lines) and a fast drag across large output freezes the TUI.
- **Options menu**: press `o` in the main view to open. Fields are driven by `OPTION_FIELDS` in `core/userconfig.py`. Categories: Execution (parallel, leak sanitizer), Output (output lines, theme), Test Story (story filter profile, debug precision, TSV display settings). CLI-overridden fields are locked and marked "(CLI)". Changes apply immediately to live state and persist to user config.

## Per-Run State Isolation (TestRun + DwarfCache)
- **`TestRun`** dataclass holds all mutable state for a single test execution (`timeline_events`, `annotation_cache`, `debug_logs`, `stdout`, `stderr`, `compile_err`, `debug_running`, `debug_exited`, `debug_exit_code`, `aggregate_annotations`, `timeline_selected_event_index`). A fresh `TestRun()` is created on every `run_test()` and `start_debug_session()`.
- **`DwarfCache`** dataclass holds all DWARF/annotation caches plus binary tracking fields (`last_binary_path`, `last_binary_mtime`). Owned by `Test` and persists across runs.
- **Binary metadata caches** (dwarf_loader, function_index, global_index, type_index) are expensive to rebuild; they persist when the binary is unchanged (detected via mtime comparison).
- **Runtime caches** (source_line, annotation) are cheap and depend on execution behavior; they are reset on every run.
- `_debug_callbacks()` captures the `TestRun` instance at setup time so old async tasks from previous runs silently drop data if a newer run has superseded them.

## Watch Mode Details
- Observes repo root (`.`) recursively — no need to pre-build watched directory lists.
- File change handling is serialized via an `asyncio.Lock` to prevent overlapping/racy requeue passes during rapid saves.
- DebounceHandler tracks event kinds per-path (`dict[str, set[str]]`) and supports `modified`, `created`, `deleted`, `moved` events.
- Directory-only `modified` events are filtered as noise (editors touching directory metadata should not trigger reruns).
- `test_build/breakpoints.json` updates refresh the in-memory editor-breakpoint cache for manual debug without forcing project rebuilds.
- `tests/*.c` changes use precision reruns:
  - existing test file edited → rerun only that test
  - new test file created → add and run only that test
  - test file deleted → remove that test from state and suite tree
  - test file moved → treated as delete at old path + create at new path
- **Mass-change threshold (`_MASS_CHANGE_THRESHOLD = 25`)**: the threshold is now computed over **non-`test_build` paths only**. A normal build emits dozens of artifact writes (`.o`/`.d`/`libproject.a`/binary/`.map`/`Makefile`/`db.json`) — without this exclusion, every build would trip the threshold, rerun_all the running test, cancel it, requeue → build → cancel → ... (self-feeding infinite loop). Only genuine bulk operations on *sources* (`git checkout`, directory moves) trigger the mass-change fast path.
- **Dependency graph readiness** (`dep_graph_ready`) is set to `False` when: no tests exist, any test has compile errors, tests have zero dependencies, or no test has src/ dependencies. It's recomputed on every Makefile regeneration / build call. `False` prevents stale "ready" state from suppressing necessary rebuilds during error recovery.
- If a Test Story page is open and the running test is exited, the app cancels the active run/debug session, restores normal build flags, and requeues that test for a normal rebuild/rerun.
- **Watch event deferral**: when `active_debug_test_key` is set (manual debug mode), non-breakpoint changes are deferred (`_deferred_changes`) instead of being applied immediately. This prevents the watch handler from cancelling the running debug session on every build artifact write. Auto story capture (`_run_auto_debug_trace`) does **not** set `active_debug_test_key`, so changes during auto capture are applied immediately (and may cancel + requeue the running trace). When `debug_auto_restart` is ON (manual debug only), a `has_relevant_changes` check filters out test_build noise to prevent an infinite restart loop.

## Gotchas
- `requirements.txt` includes `pyperclip` for clipboard support in the output screen, but it's not listed in `pyproject.toml` dependencies — the app handles `ImportError` gracefully.
- `pygdbmi` is required for Test Story/debug capture and must be available in the PEX/runtime environment.
- `pyelftools` powers DWARF-backed inline annotation resolution; if unavailable, resolver calls degrade gracefully.
- Inline annotations in the story viewer are resolver-backed when DWARF is available; card frames now carry `resolved_annotations` alongside the existing raw captured variables.
- The DWARF scope index is built lazily inside the per-Test `DwarfCache`; if DWARF is unavailable, `_compute_story_annotations()` falls back to snippet-window regex.
- The pex entry point is `main:entry` (not `src.main:entry`).
- On Linux with PEP 668 (`externally-managed-environment`), install deps in a virtualenv for local source runs.
- `--debug-log` writes to `test_build/log.txt`. The watch handler filters test_build paths from the log via a guard that skips logging when the event batch is entirely test_build paths — this prevents the diagnostic itself from feeding back into an infinite logging loop.
- A `.c` file without `int main(...) {` is silently skipped at discovery. If you drop a helper `.c` into `tests/`, it won't appear in the test tree and won't be compiled. To make it a test, add a `main()`.
- `--leak-sanitizer` is purely runtime (`ASAN_OPTIONS detect_leaks`), not a compile flag. Toggling it in the Options menu takes effect on the next test run with no rebuild. It is OFF by default.
- `--get-dependencies` for a test file **builds** it (generates Makefile, builds `libproject.a`, links the test) and reads `.d`/`.map` artifacts. It's accurate (the linker tells us the truth) but slower (it's a real build). Non-test files use fast static `gcc -MM`.
