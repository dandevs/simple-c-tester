import os


_source_mtime_cache: dict[str, int | None] = {}


def display_path(file_path: str) -> str:
    if not file_path:
        return ""

    abs_path = os.path.abspath(file_path)
    try:
        rel_path = os.path.relpath(abs_path, os.getcwd())
        if rel_path.startswith(".."):
            return abs_path
        return rel_path
    except ValueError:
        return abs_path


def detect_language(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in {".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}:
        return "cpp"
    return "c"


def load_source_lines(file_path: str, source_cache: dict[str, list[str]]) -> list[str]:
    source_path = os.path.abspath(file_path)
    try:
        mtime_ns: int | None = os.stat(source_path).st_mtime_ns
    except OSError:
        mtime_ns = None

    cached = source_cache.get(source_path)
    if cached is not None and _source_mtime_cache.get(source_path) == mtime_ns:
        return cached

    try:
        with open(source_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []

    source_cache[source_path] = lines
    _source_mtime_cache[source_path] = mtime_ns
    return lines


def event_has_useful_source_line(
    file_path: str, line_number: int, source_cache: dict[str, list[str]]
) -> bool:
    if not file_path or line_number <= 0:
        return False
    source_path = os.path.abspath(file_path)
    lines = load_source_lines(source_path, source_cache)
    if not lines or line_number > len(lines):
        return False
    return bool(lines[line_number - 1].strip())
