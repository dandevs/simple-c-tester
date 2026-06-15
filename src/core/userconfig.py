"""Persisted user configuration for the C tester TUI.

This module owns the *user-editable, session-persistent* settings that the
Options screen exposes.  It is the single source of truth for those settings
across runs.

Design notes
------------
* Settings live in ``~/.config/ctester/config.json`` (XDG; honours
  ``XDG_CONFIG_HOME``).  This survives clean rebuilds (unlike the historical
  ``test_build/db.json`` location) and is independent of any project checkout.
* ``OPTION_FIELDS`` is the canonical field spec.  Both the persistence layer
  (validation/defaults) and the Options UI (rendering) derive from it, so there
  is exactly one place to add or retune a setting.
* CLI flags are *not* persisted here.  ``main.py`` resolves each field as
  ``cli_arg if explicitly passed else userconfig_value else builtin_default``.
* All public functions are pure w.r.t. their arguments; the only side effect is
  filesystem I/O in ``save_user_config``.

This module is part of the ``core`` layer: it imports nothing from ``api``,
``ui``, or the legacy global ``state`` module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Field specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionField:
    """Declarative description of one user-editable setting.

    Attributes:
        key:     stable identifier persisted to disk and read by the engine.
        label:   human-readable label shown in the Options screen.
        group:   grouping header in the Options screen.
        kind:    ``"stepper"`` (numeric +/-), ``"cycle"`` (rotate choices), or
                 ``"toggle"`` (boolean).
        default: builtin default used when neither CLI nor disk supply a value.
        min/max: bounds for ``"stepper"`` fields.
        choices: ordered values for ``"cycle"`` fields.
    """

    key: str
    label: str
    group: str
    kind: str
    default: object
    min: int = 0
    max: int = 0
    choices: tuple = ()


# Render order is significant: groups appear in first-seen order.
OPTION_FIELDS: tuple[OptionField, ...] = (
    OptionField("parallel", "Parallel workers", "Execution", "stepper", 4, 1, 32),
    OptionField(
        "output_lines", "Inline output lines", "Output", "stepper", 10, 1, 200
    ),
    OptionField(
        "theme", "Theme", "Output", "cycle", "ansi", choices=("ansi", "default")
    ),
    OptionField(
        "story_filter_profile",
        "Story filter profile",
        "Test Story",
        "cycle",
        "balanced",
        choices=("minimal", "balanced", "all"),
    ),
    OptionField(
        "debug_precision_mode",
        "Debug precision",
        "Test Story",
        "cycle",
        "precise",
        choices=("loose", "precise"),
    ),
    OptionField(
        "tsv_vars_depth",
        "Variable expansion depth",
        "Test Story",
        "stepper",
        2,
        1,
        5,
    ),
    OptionField(
        "tsv_lines_above", "Lines above frame", "Test Story", "stepper", 4, 0, 20
    ),
    OptionField(
        "tsv_lines_below", "Lines below frame", "Test Story", "stepper", 4, 0, 20
    ),
    OptionField(
        "tsv_skip_seq_lines",
        "Skip sequential lines",
        "Test Story",
        "stepper",
        10,
        1,
        50,
    ),
    OptionField(
        "tsv_variables_height",
        "Variables panel height",
        "Test Story",
        "stepper",
        10,
        3,
        40,
    ),
    OptionField(
        "tsv_show_reason_about",
        "Show trigger reason/about",
        "Test Story",
        "toggle",
        False,
    ),
    OptionField(
        "timeline", "Timeline capture (global)", "Test Story", "toggle", False
    ),
)


def _field_by_key() -> dict[str, OptionField]:
    return {f.key: f for f in OPTION_FIELDS}


# ---------------------------------------------------------------------------
# Defaults + validation
# ---------------------------------------------------------------------------


def default_config() -> dict:
    """Builtin defaults for every option field (no disk / CLI involved)."""
    return {f.key: f.default for f in OPTION_FIELDS}


def _coerce(field: OptionField, raw):
    """Validate + coerce a raw persisted/CLI value for ``field``.

    Returns the coerced value, or raises ``ValueError``/``TypeError`` to signal
    rejection (caller falls back to the default).
    """
    if field.kind == "toggle":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int,)) and not isinstance(raw, bool):
            return bool(raw)
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise ValueError(f"invalid bool for {field.key}: {raw!r}")
    if field.kind == "stepper":
        value = int(raw)
        if value < field.min or value > field.max:
            raise ValueError(f"{field.key} out of bounds: {value}")
        return value
    if field.kind == "cycle":
        value = str(raw)
        if value not in field.choices:
            raise ValueError(f"{field.key} not in choices: {value!r}")
        return value
    raise ValueError(f"unknown field kind: {field.kind}")


# ---------------------------------------------------------------------------
# Path
# ---------------------------------------------------------------------------


def config_path() -> Path:
    """Return the absolute path to the user config file (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "ctester" / "config.json"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_user_config() -> dict:
    """Load + validate the user config, filling defaults for missing fields.

    Returns a fresh dict keyed by field key.  Corrupt or missing fields fall
    back to their builtin defaults, so the result is always complete.
    """
    fields = _field_by_key()
    result = default_config()
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(raw, dict):
        return result
    for key, field in fields.items():
        if key not in raw:
            continue
        try:
            result[key] = _coerce(field, raw[key])
        except (TypeError, ValueError):
            pass  # keep builtin default for the bad value
    return result


def save_user_config(updates: dict) -> None:
    """Merge ``updates`` into the on-disk config (read-modify-write).

    Best-effort: filesystem errors are swallowed so a read-only HOME never
    crashes the TUI.  Unknown keys are dropped; values are validated.
    """
    fields = _field_by_key()
    merged = load_user_config()
    for key, value in updates.items():
        field = fields.get(key)
        if field is None:
            continue
        try:
            merged[key] = _coerce(field, value)
        except (TypeError, ValueError):
            continue
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: merged[k] for k in fields}  # stable, field-ordered keys
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


__all__ = [
    "OptionField",
    "OPTION_FIELDS",
    "default_config",
    "config_path",
    "load_user_config",
    "save_user_config",
]
