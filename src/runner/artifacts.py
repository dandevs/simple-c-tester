"""Compatibility shim — re-exports artifact path helpers from ``core.artifacts``."""

from core.artifacts import (  # noqa: F401
    _sanitize_part,
    _UNSAFE_NAME_CHARS_RE,
    test_artifact_stem,
    test_binary_path,
    test_dep_path,
    test_map_path,
)
