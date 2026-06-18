"""Compatibility shim — re-exports the DWARF resolver surface from ``core.dwarf``.

The canonical home is now :mod:`core.dwarf`.  This package shim keeps the
legacy ``from runner.dwarf_core import ...`` imports working during the
refactor.  Deep imports (``runner.dwarf_core.<module>``) should be migrated to
``core.dwarf.<module>``.
"""

from core.dwarf import *  # noqa: F401,F403
from core.dwarf import __all__  # noqa: F401
