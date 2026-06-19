"""Build system — makefile generation, include resolution, dependency graph,
and db.json persistence.

This is the ``core``-layer replacement for the legacy ``runner/makefile.py``.
Every function that needs runtime state takes a :class:`core.state.RunnerState`
explicitly, and every function that needs configuration takes a
:class:`core.config.RunnerConfig` explicitly.  Nothing is read from the legacy
global ``state`` module.

The test tree is sourced from ``rs.app_state``; the dependency index and
graph-readiness flags live on ``rs``; build flags (``cflags``, ``debug_build``)
come from ``config``; persistence payloads (``app_active``, ``debug_line``,
preference defaults) live on ``rs``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from typing import TYPE_CHECKING

from .config import RunnerConfig
from .state import RunnerState
from .story.filters import normalized_story_filter_profile
from .artifacts import test_artifact_stem, test_binary_path, test_dep_path, test_map_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


_MISSING_HEADER_RE = re.compile(r"fatal error:\s+(\S+):\s+No such file or directory")
SRC_DIR = os.path.abspath("src")
DB_PATH = os.path.join("test_build", "db.json")
DB_TMP_PATH = DB_PATH + ".tmp"
DB_BAK_PATH = DB_PATH + ".bak"
_last_db_mtime_ns: int | None = None

# Read-block size for _hash_file.  64 KiB matches the typical page-cache
# chunk and keeps the inner loop tight on large source files.
_HASH_CHUNK = 65536

# Last-known sha1 of each dependency path, plus the mtime_ns observed at the
# time of hashing.  Used to short-circuit watch events: when a file's
# ``modified`` event fires but its content matches the cached hash, the test
# runner can skip the affected rebuild.  Populated by refresh_dependency_graph
# and persisted to db.json so the cache survives restarts.
_DEP_HASH_CACHE: dict[str, tuple[int, str]] = {}

# Serialises db.json writes.  refresh_dependency_graph runs in a worker
# thread (via asyncio.to_thread) and fires once per test compile, so under
# the default --parallel 4 several saves can race on the shared .tmp file.
# The lock keeps each save atomic end-to-end; without it the .tmp rename
# fails spuriously with ENOENT when a concurrent caller consumed it first.
_DB_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers (no state)
# ---------------------------------------------------------------------------

def _hash_file(path: str) -> str | None:
    """Return sha1 hex of ``path``'s bytes, or ``None`` on read failure."""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _refresh_hash_cache_for(paths: set[str]) -> None:
    """Update ``_DEP_HASH_CACHE`` for each path.

    Reuses the cached hash when ``mtime_ns`` is unchanged so we don't rehash
    files that haven't actually been touched since the last refresh.  Paths
    that no longer exist are dropped from the cache so future ``modified``
    events on them aren't falsely suppressed.
    """
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            _DEP_HASH_CACHE.pop(path, None)
            continue
        cached = _DEP_HASH_CACHE.get(path)
        if cached is not None and cached[0] == st.st_mtime_ns:
            continue
        digest = _hash_file(path)
        if digest is not None:
            _DEP_HASH_CACHE[path] = (st.st_mtime_ns, digest)


def dep_content_unchanged(path: str) -> bool:
    """``True`` when ``path``'s current sha1 matches the cached hash from the
    last successful :func:`refresh_dependency_graph`.

    Watch mode calls this on ``modified`` events to skip redundant reruns
    when a file's mtime bumped but its bytes didn't change (editor
    touch-without-save, atomic-save with identical content, ``git checkout``
    restoring the same content).  Updates the cache in place when it
    re-validates so subsequent events on the same mtime skip the rehash.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    cached = _DEP_HASH_CACHE.get(path)
    if cached is None:
        return False
    cached_mtime, cached_hash = cached
    if cached_mtime == st.st_mtime_ns:
        return True
    # mtime bumped — rehash and compare to see if content actually changed.
    current = _hash_file(path)
    if current is None or current != cached_hash:
        return False
    _DEP_HASH_CACHE[path] = (st.st_mtime_ns, current)
    return True


def _atomic_write_db(content: str) -> None:
    """Write ``content`` to ``DB_PATH`` atomically.

    Writes to a sibling ``.tmp`` file first, then ``os.replace``s it into
    place — on POSIX this is atomic so a crash mid-write never leaves a
    truncated db.json.  Before overwriting, the previous DB is rotated to
    ``.bak`` so a corrupt or accidentally-clobbered write can be recovered
    by hand.  Rotation failures are swallowed: losing the backup is bad but
    losing the save is worse.

    Serialised by ``_DB_WRITE_LOCK``: under ``--parallel N`` several test
    workers can call ``refresh_dependency_graph`` (and therefore this
    function) at once, and without serialisation they would race on the
    shared ``.tmp`` path.
    """
    with _DB_WRITE_LOCK:
        os.makedirs("test_build", exist_ok=True)
        if os.path.exists(DB_PATH):
            try:
                os.replace(DB_PATH, DB_BAK_PATH)
            except OSError:
                pass
        with open(DB_TMP_PATH, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(DB_TMP_PATH, DB_PATH)


def _load_db_json() -> dict | None:
    """Load and parse db.json.  Falls back to the ``.bak`` if the main file
    is missing or corrupt.  Returns ``None`` if neither is usable."""
    for candidate in (DB_PATH, DB_BAK_PATH):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _parse_missing_header(stderr: str) -> str | None:
    match = _MISSING_HEADER_RE.search(stderr)
    return match.group(1) if match else None


def _find_header_dir(header_name: str) -> str | None:
    for root, _, files in os.walk("."):
        parts = root.split(os.sep)
        if "test_build" in parts:
            continue
        candidate = os.path.join(root, header_name)
        if os.path.isfile(candidate):
            return os.path.normpath(root)
    return None


def _normalize_dep_path(path: str) -> str:
    """Canonicalise a dependency path for use as a dep_index / db.json key.

    Uses :func:`os.path.realpath` so that the same physical file reached via
    different paths (e.g. a project tree accessed both directly and through
    a symlink, common on macOS Homebrew setups and in dev containers)
    collapses to a single key.  Without this, ``dep_index`` could carry two
    entries for the same file and only one of them would match a watch event,
    silently breaking precision reruns for the affected tests.

    ``realpath`` resolves directory symlinks on the path even when the leaf
    does not exist, so phantom paths from stale ``.d`` files still normalise
    consistently.
    """
    return os.path.realpath(os.path.normpath(path))


def normalize_dep_path(path: str) -> str:
    """Public alias for :func:`_normalize_dep_path`.

    Call sites outside ``core.build`` (notably watch-mode event matching in
    ``watch.handler``) should use this so dep_index keys and lookup keys stay
    in lock-step.
    """
    return _normalize_dep_path(path)


def _normalized_precision_mode(value) -> str:
    if isinstance(value, str) and value.lower() == "loose":
        return "loose"
    return "precise"


def _parse_dep_file(dep_file: str) -> list[str]:
    if not os.path.exists(dep_file):
        return []

    with open(dep_file, "r", encoding="utf-8", errors="replace") as f:
        dep_content = f.read()

    deps: set[str] = set()

    logical_lines: list[str] = []
    current = ""
    for line in dep_content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        logical_lines.append(current)
        current = ""
    if current:
        logical_lines.append(current)

    for line in logical_lines:
        if ":" not in line:
            continue
        _, deps_part = line.split(":", 1)
        for candidate in deps_part.split():
            if candidate:
                deps.add(_normalize_dep_path(candidate))

    return sorted(deps)


def resolve_include_dirs(source_path: str) -> list[str]:
    """Iteratively resolve include directories via ``gcc -E``.

    Pure: depends only on the filesystem and ``gcc``.
    """
    include_dirs: list[str] = []
    for _ in range(20):
        cmd = ["gcc", "-E"]
        for d in include_dirs:
            cmd.extend(["-I", d])
        cmd.append(source_path)
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            break
        missing = _parse_missing_header(result.stderr.decode(errors="replace"))
        if missing is None:
            break
        found_dir = _find_header_dir(missing)
        if found_dir is None or found_dir in include_dirs:
            break
        include_dirs.append(found_dir)
    return include_dirs


def _preprocessor_headers(source_path: str, include_dirs: list[str]) -> list[str]:
    """Transitive user-header closure of ``source_path`` via ``gcc -MM``.

    Returns existing, non-system header/source paths (``-MM`` already excludes
    system headers), sorted.  Paths come back as gcc emits them (relative when
    ``include_dirs`` are relative).  Pure: invokes gcc only.
    """
    cmd = ["gcc", "-MM"]
    for d in include_dirs:
        cmd.extend(["-I", d])
    cmd.append(source_path)
    result = subprocess.run(cmd, capture_output=True)
    out = result.stdout.decode(errors="replace")
    # Join make-style "\" continuation lines into logical dependency lines.
    joined = out.replace("\\\n", " ")
    headers: set[str] = set()
    for line in joined.splitlines():
        if ":" not in line:
            continue
        _, deps_part = line.split(":", 1)
        for cand in deps_part.split():
            if not cand or cand == source_path:
                continue
            if os.path.exists(cand):
                headers.add(cand)
    return sorted(headers)


def get_file_dependencies(source_path: str) -> dict:
    """Resolve dependency information for ``source_path`` without building.

    Returns a dict with three keys:

      * ``"include_dirs"`` — resolved ``-I`` directories (:func:`resolve_include_dirs`);
      * ``"headers"`` — transitive user-header closure (``gcc -MM``);
      * ``"project_sources"`` — ``.c`` sources that would be compiled into
        ``libproject.a`` for these include dirs plus the ``src/`` tree.

    Pure-ish: invokes ``gcc -E`` / ``gcc -MM``; writes no build artifacts.
    """
    include_dirs = resolve_include_dirs(source_path)
    return {
        "include_dirs": include_dirs,
        "headers": _preprocessor_headers(source_path, include_dirs),
        "project_sources": _project_sources_for(include_dirs),
    }


def analyze_test_build(
    test, discovered_sources: list[str], skipped_sources: list[str]
) -> dict:
    """Read a built test's true dependencies from its build artifacts.

    ``test`` must already be compiled and linked so its ``.d`` (transitive
    header closure) and ``.map`` (linker archive members) exist.  This reads
    the same artifacts :func:`refresh_dependency_graph` uses for watch mode,
    so the result reflects what the linker actually pulled — not the static
    discovery set.

    Returns a dict:

      * ``include_dirs``       — the test's resolved ``-I`` directories
      * ``headers``            — transitive user headers, from the ``.d`` file
      * ``linked_sources``     — ``.c`` sources the linker actually pulled
                                 from ``libproject.a`` (the bare minimum),
                                 resolved from ``.map`` archive members
      * ``discovered_sources`` — every ``.c`` discovered (the contrast set)
      * ``skipped_sources``    — sources dropped by skip-on-error
      * ``built``              — whether the test binary exists on disk
    """
    # Headers: transitive #include closure recorded in the .d file.
    headers = [
        dep
        for dep in _parse_dep_file(test_dep_path(test.source_path))
        if dep.endswith(".h") and os.path.exists(dep)
    ]
    # Linked sources: archive members the linker pulled, resolved to .c paths.
    archive_path = os.path.join("test_build", "libproject.a")
    members = _parse_linked_archive_members_from_map(
        test_map_path(test.source_path), archive_path
    )
    obj_to_src = {_obj_name(s): s for s in discovered_sources}
    linked: list[str] = []
    if members:
        for member in members:  # e.g. "src__interpreter__interpreter.o"
            stem = member[:-2] if member.endswith(".o") else member
            src = obj_to_src.get(stem)
            if src:
                linked.append(src)
    return {
        "include_dirs": list(test.include_dirs),
        "headers": sorted(set(headers)),
        "linked_sources": sorted(set(linked)),
        "discovered_sources": list(discovered_sources),
        "skipped_sources": list(skipped_sources),
        "built": os.path.exists(test_binary_path(test.source_path)),
    }


def _obj_name(source_path: str) -> str:
    rel = os.path.normpath(source_path).replace(os.sep, "__")
    return os.path.splitext(rel)[0]


def _parse_linked_archive_members_from_map(
    map_path: str, archive_path: str
) -> set[str] | None:
    if not os.path.exists(map_path):
        return None

    try:
        with open(map_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    if not content.strip():
        return None

    archive_basename = re.escape(os.path.basename(archive_path))
    archive_normalized = re.escape(os.path.normpath(archive_path))
    archive_absolute = re.escape(os.path.abspath(archive_path))
    path_pattern = rf"(?:[^\s()]*[/\\])?(?:{archive_absolute}|{archive_normalized}|{archive_basename})"
    member_pattern = r"([^)\\/]+\.o)"
    pattern = re.compile(rf"{path_pattern}\({member_pattern}\)")

    return {match.group(1) for match in pattern.finditer(content)}


def _collect_linked_member_dependencies(linked_members: set[str]) -> set[str]:
    deps: set[str] = set()
    for member in linked_members:
        member_base = os.path.basename(member)
        if not member_base.endswith(".o"):
            continue
        dep_name = os.path.splitext(member_base)[0]
        dep_path = os.path.join("test_build", "obj", f"{dep_name}.d")
        deps.update(_parse_dep_file(dep_path))
    return deps


# ---------------------------------------------------------------------------
# State-reading helpers
# ---------------------------------------------------------------------------

def _collect_c_sources(directory: str) -> list[str]:
    """Walk ``directory`` and return non-test, non-``main.c`` ``.c`` paths.

    Skips anything under a ``test_build`` directory and the ``tests``
    directory.  Paths come back in the same form as ``directory`` (relative
    when ``directory`` is relative), matching how sources are written into
    the generated Makefile.  Pure: depends only on the filesystem.
    """
    sources: list[str] = []
    if not os.path.isdir(directory):
        return sources
    for root, dirs, files in os.walk(directory):
        parts = root.split(os.sep)
        if "test_build" in parts:
            continue
        if root.startswith("tests") or root.startswith("tests/"):
            continue
        dirs[:] = [d for d in dirs if d != "test_build"]
        for f in sorted(files):
            if f.endswith(".c") and f != "main.c":
                sources.append(os.path.join(root, f))
    return sources


def discover_project_sources(rs: RunnerState) -> list[str]:
    """Discover non-test ``.c`` sources to compile into ``libproject.a``.

    Sources are collected from two places so both common C layouts work:

      * every resolved test include directory — picks up co-located
        ``.c``/``.h`` trees such as bundled libraries (``libs/libft``);
      * the conventional ``src/`` implementation tree — so a project that
        separates ``include/`` headers from ``src/`` sources (incl. nested
        modules like ``src/interpreter/``) still links its implementation.

    ``main.c`` and anything under ``tests/`` or ``test_build/`` is excluded.
    """
    include_dirs = {d for test in rs.app_state.all_tests for d in test.include_dirs}
    return _project_sources_for(include_dirs)


def _project_sources_for(include_dirs: list[str]) -> list[str]:
    """Project ``.c`` sources for a given set of include directories.

    Scans the include dirs (co-located ``.c``/``.h`` trees) plus the
    conventional ``src/`` implementation tree, dedups, and returns sorted
    paths in the same form as ``include_dirs`` (relative when those are
    relative).  Pure: depends only on the filesystem.
    """
    scan_dirs = sorted(set(include_dirs)) + ["src"]
    sources: set[str] = set()
    for directory in scan_dirs:
        sources.update(_collect_c_sources(directory))
    return sorted(sources)


def _collect_project_dependencies(rs: RunnerState) -> set[str]:
    """Union of project-source paths + every header/source referenced by any
    ``.d`` file under ``test_build/obj/``.

    Filters out paths that no longer exist on disk — orphaned ``.d`` files
    (left behind by ``git mv`` and similar) would otherwise pollute the broad
    dependency set with phantom paths, causing precision reruns to fire on
    edits to files that no test actually uses anymore.
    """
    deps: set[str] = set()

    for src in discover_project_sources(rs):
        deps.add(_normalize_dep_path(src))

    obj_dir = os.path.join("test_build", "obj")
    if os.path.isdir(obj_dir):
        for root, _, files in os.walk(obj_dir):
            for file_name in files:
                if not file_name.endswith(".d"):
                    continue
                dep_path = os.path.join(root, file_name)
                for dep in _parse_dep_file(dep_path):
                    if os.path.exists(dep):
                        deps.add(dep)

    return deps


# Files at the top level of ``test_build/`` that are not test artifacts and
# must be left alone by the orphan GC.
_PRESERVED_TEST_BUILD_FILES = {"Makefile", "db.json", "db.json.bak", "db.json.tmp"}


def _gc_orphaned_build_artifacts(rs: RunnerState) -> int:
    """Remove orphaned build artifacts from ``test_build/``.

    Two categories are cleaned:

    1. ``test_build/obj/*.{o,d}`` whose stem no longer maps to a discovered
       project source.  These accumulate when project sources are renamed,
       moved, or deleted: ``make`` regenerates the Makefile without rules for
       the old names but leaves the old object/dep files on disk, where they
       leak phantom dependency paths into ``_collect_project_dependencies``.
    2. Top-level ``test_build/{stem}{,.d,.map}`` whose ``stem`` no longer
       matches any current test.  These accumulate when test sources are
       renamed (the new stem produces new files; the old ones linger).

    Returns the number of files removed.  Idempotent — safe to call on every
    ``refresh_dependency_graph`` pass.
    """
    if not os.path.isdir("test_build"):
        return 0

    removed = 0

    obj_dir = os.path.join("test_build", "obj")
    if os.path.isdir(obj_dir):
        expected_obj_stems = {_obj_name(src) for src in discover_project_sources(rs)}
        for entry in os.listdir(obj_dir):
            stem, ext = os.path.splitext(entry)
            if ext not in (".o", ".d"):
                continue
            if stem in expected_obj_stems:
                continue
            try:
                os.remove(os.path.join(obj_dir, entry))
                removed += 1
            except OSError:
                pass

    expected_test_stems = {
        test_artifact_stem(t.source_path) for t in rs.app_state.all_tests
    }
    for entry in os.listdir("test_build"):
        if entry in _PRESERVED_TEST_BUILD_FILES:
            continue
        full = os.path.join("test_build", entry)
        if not os.path.isfile(full):
            continue
        base, ext = os.path.splitext(entry)
        if ext in (".d", ".map"):
            stem_to_check = base
        elif ext == "":
            stem_to_check = entry
        else:
            continue
        if stem_to_check in expected_test_stems:
            continue
        try:
            os.remove(full)
            removed += 1
        except OSError:
            pass

    return removed


def build_project_sources(rs: RunnerState) -> None:
    """Build ``test_build/libproject.a`` from discovered project sources.

    Skip-on-error: a project source that fails to compile is dropped from
    the archive (its path is recorded in ``rs.skipped_sources``) so one
    broken WIP file cannot block every test.  The generated Makefile ignores
    per-object compile failures and archives only the objects that built.
    Tests that genuinely need a skipped symbol then surface a precise linker
    "undefined reference" error instead of a project-wide build failure.

    The gcc stderr from the archive build is captured into ``rs.build_stderr``
    so callers (headless warning, TUI banner) can surface the actual compile
    errors rather than silently swallowing them.
    """
    sources = discover_project_sources(rs)
    rs.skipped_sources = []
    rs.build_stderr = ""
    if not sources:
        return
    makefile = "test_build/Makefile"
    if not os.path.exists(makefile):
        return
    result = subprocess.run(
        ["make", "-f", makefile, "test_build/libproject.a"],
        capture_output=True,
    )
    rs.build_stderr = result.stderr.decode(errors="replace")
    obj_dir = os.path.join("test_build", "obj")
    rs.skipped_sources = [
        src
        for src in sources
        if not os.path.exists(os.path.join(obj_dir, _obj_name(src) + ".o"))
    ]


# ---------------------------------------------------------------------------
# Dependency-graph state mutation (on RunnerState)
# ---------------------------------------------------------------------------

def update_dep_graph_readiness(rs: RunnerState) -> None:
    """Recompute ``rs.dep_graph_ready`` / ``rs.dep_graph_reason`` from tests."""
    tests = rs.app_state.all_tests
    if not tests:
        rs.dep_graph_ready = False
        rs.dep_graph_reason = "no tests discovered"
        return

    if any(
        (test.current_run.compile_err.strip() if test.current_run is not None else "")
        for test in tests
    ):
        rs.dep_graph_ready = False
        rs.dep_graph_reason = "compile errors present"
        return

    if any(len(test.dependencies) == 0 for test in tests):
        rs.dep_graph_ready = False
        rs.dep_graph_reason = "tests missing dependencies"
        return

    has_src_dependency = any(
        dep == SRC_DIR or dep.startswith(f"{SRC_DIR}{os.sep}")
        for test in tests
        for dep in test.dependencies
    )
    if not has_src_dependency:
        rs.dep_graph_ready = False
        rs.dep_graph_reason = "no src dependencies collected"
        return

    rs.dep_graph_ready = True
    rs.dep_graph_reason = "ready"


def rebuild_dep_index(rs: RunnerState) -> None:
    """Rebuild ``rs.dep_index`` (source-path -> dependent tests)."""
    rs.dep_index.clear()
    for test in rs.app_state.all_tests:
        for dep in test.dependencies:
            rs.dep_index.setdefault(dep, []).append(test)


# ---------------------------------------------------------------------------
# db.json persistence
# ---------------------------------------------------------------------------

def load_dependency_db(rs: RunnerState) -> dict[str, dict]:
    """Load db.json, hydrate runner-wide preferences on ``rs``, return the
    per-test dependency map."""
    global _last_db_mtime_ns
    data = _load_db_json()
    if data is None:
        _last_db_mtime_ns = None
        return {}

    tests_data = data.get("tests")
    if not isinstance(tests_data, dict):
        tests_data = {}

    # Note: user preferences (debug_precision_mode, story_filter_profile, and
    # all Options-menu settings) are loaded from ~/.config/ctester/config.json
    # via core.userconfig, not from this file.  Any stale "preferences" key in
    # an old db.json is ignored.

    try:
        _last_db_mtime_ns = os.stat(DB_PATH).st_mtime_ns
    except OSError:
        _last_db_mtime_ns = None

    # Hydrate the content-hash cache.  We don't trust the persisted mtimes
    # (cross-machine, cross-filesystem), so we store mtime=0 which forces
    # dep_content_unchanged to rehash on first access; if the bytes still
    # match, suppression kicks in for subsequent events.
    hashes_data = data.get("dep_hashes")
    if isinstance(hashes_data, dict):
        for path, digest in hashes_data.items():
            if isinstance(path, str) and isinstance(digest, str):
                _DEP_HASH_CACHE[path] = (0, digest)

    hydrated: dict[str, dict] = {}
    for test_key, payload in tests_data.items():
        if not isinstance(test_key, str) or not isinstance(payload, dict):
            continue
        deps = payload.get("collected_dependencies", [])
        if not isinstance(deps, list):
            continue
        normalized = []
        for dep in deps:
            if isinstance(dep, str):
                normalized.append(_normalize_dep_path(dep))
        hydrated_entry: dict = {
            "collected_dependencies": sorted(set(normalized))
        }
        timing = payload.get("timing_history", [])
        if isinstance(timing, list):
            hydrated_entry["timing_history"] = [
                float(t)
                for t in timing
                if isinstance(t, (int, float)) and t >= 0
            ][-10:]
        hydrated[_normalize_dep_path(test_key)] = hydrated_entry

    return hydrated


def save_dependency_db(
    rs: RunnerState, changed_test_keys: set[str] | None = None
) -> None:
    """Persist the test dependency map + preferences + debug line to db.json."""
    global _last_db_mtime_ns

    if changed_test_keys is not None and not changed_test_keys:
        try:
            if os.stat(DB_PATH).st_mtime_ns == _last_db_mtime_ns:
                return
        except OSError:
            pass

    tests_payload: dict[str, dict] = {}
    for test in rs.app_state.all_tests:
        test_key = _normalize_dep_path(test.source_path)
        entry: dict = {
            "collected_dependencies": sorted(set(test.dependencies))
        }
        if getattr(test, "story_annotations", None):
            entry["story_annotations"] = test.story_annotations
        if test.timing_history:
            entry["timing_history"] = test.timing_history[-10:]
        tests_payload[test_key] = entry

    payload = {
        "tests": tests_payload,
        "active": rs.app_active,
        # Persist the content-hash cache so watch-mode modified-event
        # suppression survives process restarts.  mtime is not persisted —
        # the cache is revalidated lazily on first access via
        # dep_content_unchanged, which rehashes when mtime differs.
        "dep_hashes": {p: h for p, (_, h) in _DEP_HASH_CACHE.items()},
    }
    if rs.debug_line is not None:
        payload["debugLine"] = rs.debug_line
    new_content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write_db(new_content)
    try:
        _last_db_mtime_ns = os.stat(DB_PATH).st_mtime_ns
    except OSError:
        _last_db_mtime_ns = None


def persist_user_preferences(rs: RunnerState) -> None:
    save_dependency_db(rs, changed_test_keys=None)


def save_debug_line(rs: RunnerState, file_path: str, line_number: int) -> None:
    rs.debug_line = {
        "filePath": file_path,
        "lineNumber": line_number,
    }
    save_dependency_db(rs, changed_test_keys=None)


def clear_debug_line(rs: RunnerState) -> None:
    rs.debug_line = None
    save_dependency_db(rs, changed_test_keys=None)


def save_story_annotations(
    rs: RunnerState, test_key: str, annotations: dict[str, list[list]]
) -> None:
    """Persist full-file annotations for a test.  ``annotations`` is
    ``{abs_path: [[lineText, line, [str, ...]], ...]}``."""
    for test in rs.app_state.all_tests:
        if _normalize_dep_path(test.source_path) == test_key:
            test.story_annotations = dict(annotations)
            break
    save_dependency_db(rs, changed_test_keys=None)


def hydrate_dependencies_from_db(rs: RunnerState) -> None:
    db = load_dependency_db(rs)
    for test in rs.app_state.all_tests:
        test.debug_precision_mode = _normalized_precision_mode(
            rs.default_debug_precision_mode
        )
        test.story_filter_profile = normalized_story_filter_profile(
            rs.default_story_filter_profile
        )
    if not db:
        update_dep_graph_readiness(rs)
        return

    for test in rs.app_state.all_tests:
        test_key = _normalize_dep_path(test.source_path)
        cached = db.get(test_key)
        if cached is None:
            continue
        deps = cached.get("collected_dependencies", [])
        if deps:
            test.dependencies = sorted(set(deps))
        timing = cached.get("timing_history", [])
        if isinstance(timing, list):
            test.timing_history = [float(t) for t in timing][-10:]

    rebuild_dep_index(rs)
    update_dep_graph_readiness(rs)


def refresh_dependency_graph(rs: RunnerState) -> None:
    """Recompute every test's dependencies from the ``.d`` files + project
    archive map, rebuild the index, and persist."""
    # GC orphaned artifacts first so we don't parse stale .d files left behind
    # by renames/moves/deletes — those would re-introduce phantom deps below.
    _gc_orphaned_build_artifacts(rs)

    project_sources = discover_project_sources(rs)
    has_project_sources = bool(project_sources)
    archive_path = os.path.join("test_build", "libproject.a")
    broad_project_deps: set[str] | None = None
    changed_test_keys: set[str] = set()
    for test in rs.app_state.all_tests:
        test_dep_file = test_dep_path(test.source_path)
        previous = sorted(set(test.dependencies))
        current: set[str] = set()
        for dep in _parse_dep_file(test_dep_file):
            if os.path.exists(dep):
                current.add(dep)
        if has_project_sources:
            map_path = test_map_path(test.source_path)
            linked_members = _parse_linked_archive_members_from_map(map_path, archive_path)
            if linked_members is None:
                if broad_project_deps is None:
                    broad_project_deps = _collect_project_dependencies(rs)
                current.update(broad_project_deps)
            else:
                for dep in _collect_linked_member_dependencies(linked_members):
                    if os.path.exists(dep):
                        current.add(dep)
        updated = sorted(current)
        test.dependencies = updated
        if updated != previous:
            changed_test_keys.add(_normalize_dep_path(test.source_path))

    # Refresh the content-hash cache for every known dep so watch-mode
    # modified-event suppression has a baseline to compare against.  Also
    # covers test source paths themselves so edits that don't actually
    # change bytes (touch, atomic-rewrite) can skip reruns.
    all_deps: set[str] = set()
    for test in rs.app_state.all_tests:
        all_deps.update(test.dependencies)
        all_deps.add(_normalize_dep_path(test.source_path))
    _refresh_hash_cache_for(all_deps)

    rebuild_dep_index(rs)
    update_dep_graph_readiness(rs)
    save_dependency_db(rs, changed_test_keys)


# ---------------------------------------------------------------------------
# Makefile generation
# ---------------------------------------------------------------------------

def generate_makefile(
    rs: RunnerState,
    config: RunnerConfig,
    terminal_width: int | None = None,
) -> None:
    """Generate ``test_build/Makefile`` for every discovered test.

    ``terminal_width`` controls gcc's ``-fmessage-length`` (compiler message
    wrapping); defaults to ``80`` when ``None`` (matches the legacy default).
    """
    os.makedirs("test_build", exist_ok=True)
    for test in rs.app_state.all_tests:
        if not test.include_dirs:
            test.include_dirs = resolve_include_dirs(test.source_path)

    message_length = max(20, int(terminal_width if terminal_width is not None else 80))
    debug_flags = "-g -O0 -fno-omit-frame-pointer" if config.debug_build else ""
    sanitize_flags = "-fsanitize=address,undefined" if config.sanitize else ""
    # Statically link the sanitizer runtimes so they initialise before any other
    # library. Avoids the "ASan runtime does not come first in initial library
    # list" error regardless of how the binary is launched (direct exec / gdb).
    # Only meaningful on the link lines, not the -c compile line.
    sanitize_link_flags = "-static-libasan -static-libubsan" if config.sanitize else ""
    cflags = config.cflags

    project_sources = discover_project_sources(rs)
    all_include_dirs = set()
    for test in rs.app_state.all_tests:
        for d in test.include_dirs:
            all_include_dirs.add(d)
    include_flags = " ".join(f"-I{d}" for d in sorted(all_include_dirs))

    obj_dir = "test_build/obj"
    lib_target = "test_build/libproject.a"

    lines = ["-include test_build/*.d", ""]

    if project_sources:
        obj_files = []
        for src in project_sources:
            obj_name = _obj_name(src)
            obj_path = f"{obj_dir}/{obj_name}.o"
            dep_path = f"{obj_dir}/{obj_name}.d"
            obj_files.append(obj_path)
            lines.append(f"{obj_path}: {src}")
            lines.append(f"\t@mkdir -p {obj_dir}")
            # Skip-on-error: a broken project source (e.g. WIP) must not
            # block the whole library.  The leading "-" tells make to ignore
            # a compile failure; "|| rm -f $@" clears any stale object so the
            # archive never ships an out-of-date build of a now-broken file.
            lines.append(
                f"\t-gcc {include_flags} {debug_flags} {sanitize_flags} -fdiagnostics-color=always -fmessage-length={message_length} -MMD -MP -MF {dep_path} -c $< -o $@ {cflags} || rm -f $@"
            )
            lines.append("")

        # PROJECT_OBJS lets the archive recipe gather only the objects that
        # actually compiled (existing files), skipping any failures above.
        lines.append(f"PROJECT_OBJS := {' '.join(obj_files)}")
        lines.append(f"{lib_target}: $(PROJECT_OBJS)")
        lines.append(
            "\tar rcs $@ $$(for o in $(PROJECT_OBJS); do [ -f \"$$o\" ] && printf '%s\\n' \"$$o\"; done)"
        )
        lines.append("")

    for test in rs.app_state.all_tests:
        target = test_binary_path(test.source_path)
        source = test.source_path
        dep_file = test_dep_path(test.source_path)
        map_file = test_map_path(test.source_path)
        test_include_flags = " ".join(f"-I{d}" for d in test.include_dirs)
        if project_sources:
            lines.append(f"{target}: {source} {lib_target}")
            lines.append(
                f"\tgcc {test_include_flags} {debug_flags} {sanitize_flags} {sanitize_link_flags} -fdiagnostics-color=always -fmessage-length={message_length} -MMD -MP -MF {dep_file} -Wl,-Map,{map_file} -o {target} {source} {lib_target} {cflags}"
            )
        else:
            lines.append(f"{target}: {source}")
            lines.append(
                f"\tgcc {test_include_flags} {debug_flags} {sanitize_flags} {sanitize_link_flags} -fdiagnostics-color=always -fmessage-length={message_length} -MMD -MP -MF {dep_file} -o {target} {source} {cflags}"
            )
        lines.append("")

    with open("test_build/Makefile", "w") as f:
        f.write("\n".join(lines))


__all__ = [
    "DB_PATH",
    "DB_TMP_PATH",
    "DB_BAK_PATH",
    "SRC_DIR",
    "build_project_sources",
    "clear_debug_line",
    "dep_content_unchanged",
    "discover_project_sources",
    "generate_makefile",
    "get_file_dependencies",
    "analyze_test_build",
    "hydrate_dependencies_from_db",
    "load_dependency_db",
    "normalize_dep_path",
    "persist_user_preferences",
    "rebuild_dep_index",
    "refresh_dependency_graph",
    "resolve_include_dirs",
    "save_debug_line",
    "save_dependency_db",
    "save_story_annotations",
    "update_dep_graph_readiness",
]
