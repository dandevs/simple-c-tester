# core package — pure domain logic layer.
#
# Layering rule (inviolable): modules under ``core`` MUST NOT import from
# ``api`` or ``ui`` (``render``, ``app``, ``textual``). ``core`` also MUST NOT
# import the legacy global ``state`` module. Core depends only on itself, the
# standard library, and approved third-party libraries (pyelftools, pygdbmi).
