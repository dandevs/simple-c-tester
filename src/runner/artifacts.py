import os
import re
import hashlib


_UNSAFE_NAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_part(part: str) -> str:
    cleaned = _UNSAFE_NAME_CHARS_RE.sub("_", part).strip("_")
    return cleaned or "x"


def test_artifact_stem(source_path: str) -> str:
    abs_source = os.path.abspath(source_path)
    tests_root = os.path.abspath("tests")
    rel_source = os.path.relpath(abs_source, tests_root)

    if rel_source == "." or rel_source == ".." or rel_source.startswith(f"..{os.sep}"):
        rel_source = os.path.basename(abs_source)

    rel_no_ext = os.path.splitext(rel_source)[0]
    parts = [part for part in re.split(r"[\\/]+", rel_no_ext) if part and part != "."]
    if not parts:
        parts = ["test"]

    readable = "_".join(_sanitize_part(part) for part in parts)
    rel_for_hash = rel_source.replace("\\", "/")
    digest = hashlib.sha1(rel_for_hash.encode("utf-8")).hexdigest()[:8]
    return f"{readable}_{digest}"


def test_binary_path(source_path: str) -> str:
    return os.path.join("test_build", test_artifact_stem(source_path))


def test_dep_path(source_path: str) -> str:
    return os.path.join("test_build", f"{test_artifact_stem(source_path)}.d")


def test_map_path(source_path: str) -> str:
    return os.path.join("test_build", f"{test_artifact_stem(source_path)}.map")
