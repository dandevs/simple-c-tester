"""Compatibility shim — delegates to ``core.build`` using a RunnerState/Config
bridge backed by the legacy global ``state`` module.

The canonical implementation now lives in :mod:`core.build` and takes explicit
``RunnerState`` + ``RunnerConfig`` parameters.  This shim keeps the legacy
global-mutating call sites (``main.py``, ``app.py``, ``execute.py``) working
unchanged during the refactor by:

  * sharing the live ``state`` (AppState) and ``dep_index`` dict by reference,
  * reading the scalar globals into a transient ``RunnerState`` before each
    call and writing them back afterwards.

It will be removed once all call sites migrate to the API layer.
"""

import state as global_state
from state import state, dep_index

from core.config import RunnerConfig
from core.state import RunnerState
from core.build import (
    clear_debug_line as _clear_debug_line,
    generate_makefile as _generate_makefile,
    build_project_sources as _build_project_sources,
    hydrate_dependencies_from_db as _hydrate_dependencies_from_db,
    load_dependency_db as _load_dependency_db,
    persist_user_preferences as _persist_user_preferences,
    rebuild_dep_index as _rebuild_dep_index,
    refresh_dependency_graph as _refresh_dependency_graph,
    save_debug_line as _save_debug_line,
    save_dependency_db as _save_dependency_db,
    save_story_annotations as _save_story_annotations,
    update_dep_graph_readiness as _update_dep_graph_readiness,
)


def _bridge() -> RunnerState:
    """Build a RunnerState view backed by the live globals.

    ``app_state`` and ``dep_index`` are shared by reference (mutations to the
    dict and the test list propagate).  Scalar globals are copied in; callers
    must :func:`_sync_back` after invoking any core function that mutates them.
    """
    rs = RunnerState(app_state=state, dep_index=dep_index)
    rs.dep_graph_ready = global_state.dep_graph_ready
    rs.dep_graph_reason = global_state.dep_graph_reason
    rs.app_active = global_state.app_active
    rs.debug_line = global_state.debug_line
    rs.default_debug_precision_mode = global_state.debug_precision_mode_preference
    rs.default_story_filter_profile = global_state.story_filter_profile_preference
    return rs


def _sync_back(rs: RunnerState) -> None:
    """Write the scalar RunnerState fields back to the legacy globals."""
    global_state.dep_graph_ready = rs.dep_graph_ready
    global_state.dep_graph_reason = rs.dep_graph_reason
    global_state.app_active = rs.app_active
    global_state.debug_line = rs.debug_line
    global_state.debug_precision_mode_preference = rs.default_debug_precision_mode
    global_state.story_filter_profile_preference = rs.default_story_filter_profile


def _config() -> RunnerConfig:
    return RunnerConfig(
        cflags=global_state.cflags,
        debug_build=global_state.debug_build_enabled,
        sanitize=global_state.sanitize_enabled,
    )


def update_dep_graph_readiness() -> None:
    rs = _bridge()
    _update_dep_graph_readiness(rs)
    _sync_back(rs)


def rebuild_dep_index() -> None:
    rs = _bridge()
    _rebuild_dep_index(rs)


def load_dependency_db() -> dict[str, dict]:
    rs = _bridge()
    result = _load_dependency_db(rs)
    _sync_back(rs)
    return result


def save_dependency_db(changed_test_keys: set[str] | None = None) -> None:
    rs = _bridge()
    _save_dependency_db(rs, changed_test_keys=changed_test_keys)
    _sync_back(rs)


def persist_user_preferences() -> None:
    rs = _bridge()
    _persist_user_preferences(rs)
    _sync_back(rs)


def save_debug_line(file_path: str, line_number: int) -> None:
    rs = _bridge()
    _save_debug_line(rs, file_path, line_number)
    _sync_back(rs)


def clear_debug_line() -> None:
    rs = _bridge()
    _clear_debug_line(rs)
    _sync_back(rs)


def save_story_annotations(test_key: str, annotations: dict[str, list[list]]) -> None:
    rs = _bridge()
    _save_story_annotations(rs, test_key, annotations)
    _sync_back(rs)


def hydrate_dependencies_from_db() -> None:
    rs = _bridge()
    _hydrate_dependencies_from_db(rs)
    _sync_back(rs)


def refresh_dependency_graph() -> None:
    rs = _bridge()
    _refresh_dependency_graph(rs)
    _sync_back(rs)


def generate_makefile() -> None:
    rs = _bridge()
    _generate_makefile(rs, _config(), terminal_width=global_state.subprocess_columns)
    _sync_back(rs)


def build_project_sources() -> None:
    rs = _bridge()
    _build_project_sources(rs)
    _sync_back(rs)


# Re-export pure helpers that need no state bridge.
from core.build import (  # noqa: F401,E402
    DB_PATH,
    SRC_DIR,
    discover_project_sources as _discover_project_sources_core,
    normalize_dep_path,
    resolve_include_dirs,
)


def discover_project_sources() -> list[str]:
    """State-aware wrapper around the core helper."""
    rs = _bridge()
    return _discover_project_sources_core(rs)
