"""Immutable runner configuration.

``RunnerConfig`` replaces the module-level globals that previously lived in the
legacy ``state`` module (``parallel``, ``debug_build_enabled``,
``timeline_capture_enabled``, the ``tsv_*`` display preferences, ``cflags``,
``story_filter_profile_preference``, ``debug_precision_mode_preference``).

Design notes:

* It is a *frozen* dataclass — once constructed it cannot be mutated.  All
  runtime-mutable state lives in :class:`core.state.RunnerState` instead.
  This satisfies the code-quality standard of separating configuration
  (immutable, supplied once) from state (mutable, changes during a run).
* Field names drop the historical ``_preference``/``_enabled`` suffixes where
  doing so is unambiguous (``timeline`` not ``timeline_capture_enabled``).
* The historical default values are preserved so behaviour is unchanged.

This module is part of the ``core`` layer: it imports nothing from ``api``,
``ui``, or the legacy global ``state`` module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class RunnerConfig:
    """All knobs a caller can tune when constructing a :class:`TestRunner`.

    Construct it once (typically from parsed CLI arguments) and pass it down.
    Use :meth:`replace` to derive a variant rather than mutating.
    """

    # --- execution -------------------------------------------------------
    #: Number of tests that may execute concurrently.
    parallel: int = 4

    # --- build / capture modes ------------------------------------------
    #: Compile tests with debug flags (``-g -O0``) and enable debug paths.
    debug_build: bool = False
    #: Globally enable per-line Test Story capture with gdb for every test.
    timeline: bool = False

    # --- sanitizer --------------------------------------------------------
    #: Compile and link with -fsanitize=address,undefined.
    sanitize: bool = False

    # --- UI / output -----------------------------------------------------
    #: Maximum output lines shown per inline output box.
    output_lines: int = 10
    #: UI theme name.
    theme: str = "ansi"

    # --- Test Story viewer display preferences --------------------------
    tsv_lines_above: int = 4
    tsv_lines_below: int = 4
    tsv_skip_seq_lines: int = 10
    tsv_vars_depth: int = 2
    tsv_variables_height: int = 10
    tsv_show_reason_about: bool = False

    # --- compiler / linker ----------------------------------------------
    #: Extra compiler/linker flags (e.g. ``-lreadline -Wextra``).
    cflags: str = ""

    # --- story / debug defaults -----------------------------------------
    #: Default story filter profile for newly discovered tests
    #: (``minimal``/``balanced``/``all``).
    story_filter_profile: str = "balanced"
    #: Default debug stepping precision for newly discovered tests
    #: (``loose``/``precise``).
    debug_precision_mode: str = "precise"

    # --- watch -----------------------------------------------------------
    #: Whether watch mode (file-change monitoring) is active.
    watch: bool = False

    def with_overrides(self, **changes) -> "RunnerConfig":
        """Return a copy of this config with the given fields replaced.

        Mirrors :func:`dataclasses.replace`; provided as a readable alias.
        """
        return replace(self, **changes)


# Canonical default config — equivalent to running with no CLI flags.
DEFAULT_CONFIG = RunnerConfig()

__all__ = ["RunnerConfig", "DEFAULT_CONFIG"]
