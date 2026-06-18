"""UI layer — Textual TUI (depends on ``api`` + ``core``).

This package contains the rendering helpers and Textual screens.  It is the
only layer that imports ``textual``.  Importing ``ui`` (or ``ui.render``)
pulls in the TUI; the engine (``api``) and domain logic (``core``) can be used
headlessly without ever touching this package.
"""
