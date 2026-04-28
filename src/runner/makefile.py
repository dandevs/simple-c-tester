import os
import re
import subprocess
import json

from state import state, dep_index
import state as global_state
from .artifacts import test_binary_path, test_dep_path, test_map_path
from .story_filters.config import normalized_story_filter_profile


_MISSING_HEADER_RE = re.compile(r"fatal error:\s+(\S+):\s+No such file or directory")
SRC_DIR = os.path.abspath("src")
DB_PATH = os.path.join("test_build", "db.json")
_last_db_mtime_ns: int | None = None


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
    return os.path.abspath(os.path.normpath(path))


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


def discover_project_sources() -> list[str]:
    include_dirs = set()
    for test in state.all_tests:
        for d in test.include_dirs:
            include_dirs.add(d)
    sources = []
    for inc_dir in sorted(include_dirs):
        for root, dirs, files in os.walk(inc_dir):
            parts = root.split(os.sep)
            if "test_build" in parts:
                continue
            if root.startswith("tests") or root.startswith("tests/"):
                continue
            dirs[:] = [d for d in dirs if d != "test_build"]
            for f in sorted(files):
                if f.endswith(".c") and f != "main.c":
                    sources.append(os.path.join(root, f))
    return sorted(set(sources))


def _obj_name(source_path: str) -> str:
    rel = os.path.normpath(source_path).replace(os.sep, "__")
    return os.path.splitext(rel)[0]


def _collect_project_dependencies() -> set[str]:
    deps: set[str] = set()

    for src in discover_project_sources():
        deps.add(_normalize_dep_path(src))

    obj_dir = os.path.join("test_build", "obj")
    if os.path.isdir(obj_dir):
        for root, _, files in os.walk(obj_dir):
            for file_name in files:
                if not file_name.endswith(".d"):
                    continue
                dep_path = os.path.join(root, file_name)
                deps.update(_parse_dep_file(dep_path))

    return deps


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


def build_project_sources():
    sources = discover_project_sources()
    if not sources:
        return
    makefile = "test_build/Makefile"
    if not os.path.exists(makefile):
        return
    result = subprocess.run(
        ["make", "-f", makefile, "test_build/libproject.a"],
        capture_output=True,
    )
    if result.returncode != 0:
        pass


def update_dep_graph_readiness() -> None:
    tests = state.all_tests
    if not tests:
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "no tests discovered"
        return

    if any(
        (test.current_run.compile_err.strip() if test.current_run is not None else "")
        for test in tests
    ):
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "compile errors present"
        return

    if any(len(test.dependencies) == 0 for test in tests):
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "tests missing dependencies"
        return

    has_src_dependency = any(
        dep == SRC_DIR or dep.startswith(f"{SRC_DIR}{os.sep}")
        for test in tests
        for dep in test.dependencies
    )
    if not has_src_dependency:
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "no src dependencies collected"
        return

    global_state.dep_graph_ready = True
    global_state.dep_graph_reason = "ready"


def rebuild_dep_index():
    dep_index.clear()
    for test in state.all_tests:
        for dep in test.dependencies:
            dep_index.setdefault(dep, []).append(test)


def load_dependency_db() -> dict[str, dict]:
    global _last_db_mtime_ns
    if not os.path.exists(DB_PATH):
        _last_db_mtime_ns = None
        return {}

    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        _last_db_mtime_ns = None
        return {}

    if not isinstance(data, dict):
        _last_db_mtime_ns = None
        return {}

    tests_data = data.get("tests")
    if not isinstance(tests_data, dict):
        tests_data = {}

    prefs_payload = data.get("preferences")
    if isinstance(prefs_payload, dict):
        global_state.debug_precision_mode_preference = _normalized_precision_mode(
            prefs_payload.get("debug_precision_mode")
        )
        global_state.story_filter_profile_preference = normalized_story_filter_profile(
            prefs_payload.get("story_filter_profile")
        )
    else:
        global_state.debug_precision_mode_preference = "precise"
        global_state.story_filter_profile_preference = "balanced"

    try:
        _last_db_mtime_ns = os.stat(DB_PATH).st_mtime_ns
    except OSError:
        _last_db_mtime_ns = None

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
        hydrated[_normalize_dep_path(test_key)] = {
            "collected_dependencies": sorted(set(normalized))
        }

    return hydrated


def save_dependency_db(changed_test_keys: set[str] | None = None) -> None:
    global _last_db_mtime_ns

    if changed_test_keys is not None and not changed_test_keys:
        try:
            if os.stat(DB_PATH).st_mtime_ns == _last_db_mtime_ns:
                return
        except OSError:
            pass

    tests_payload: dict[str, dict] = {}
    for test in state.all_tests:
        test_key = _normalize_dep_path(test.source_path)
        entry: dict = {
            "collected_dependencies": sorted(set(test.dependencies))
        }
        if getattr(test, "story_annotations", None):
            entry["story_annotations"] = test.story_annotations
        tests_payload[test_key] = entry

    payload = {"tests": tests_payload, "active": global_state.app_active}
    payload["preferences"] = {
        "debug_precision_mode": _normalized_precision_mode(
            global_state.debug_precision_mode_preference
        ),
        "story_filter_profile": normalized_story_filter_profile(
            global_state.story_filter_profile_preference
        ),
    }
    if global_state.debug_line is not None:
        payload["debugLine"] = global_state.debug_line
    new_content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    os.makedirs("test_build", exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)
    try:
        _last_db_mtime_ns = os.stat(DB_PATH).st_mtime_ns
    except OSError:
        _last_db_mtime_ns = None


def persist_user_preferences() -> None:
    save_dependency_db(changed_test_keys=None)


def save_debug_line(file_path: str, line_number: int) -> None:
    global_state.debug_line = {
        "filePath": file_path,
        "lineNumber": line_number,
    }
    save_dependency_db(changed_test_keys=None)


def clear_debug_line() -> None:
    global_state.debug_line = None
    save_dependency_db(changed_test_keys=None)


def save_story_annotations(test_key: str, annotations: dict[str, list[list]]) -> None:  # {abs_path: [[lineText, line, [str, ...]], ...]}
    for test in state.all_tests:
        if _normalize_dep_path(test.source_path) == test_key:
            test.story_annotations = dict(annotations)
            break
    save_dependency_db(changed_test_keys=None)


def hydrate_dependencies_from_db() -> None:
    db = load_dependency_db()
    for test in state.all_tests:
        test.debug_precision_mode = _normalized_precision_mode(
            global_state.debug_precision_mode_preference
        )
        test.story_filter_profile = normalized_story_filter_profile(
            global_state.story_filter_profile_preference
        )
    if not db:
        update_dep_graph_readiness()
        return

    for test in state.all_tests:
        test_key = _normalize_dep_path(test.source_path)
        cached = db.get(test_key)
        if cached is None:
            continue
        deps = cached.get("collected_dependencies", [])
        if deps:
            test.dependencies = sorted(set(deps))

    rebuild_dep_index()
    update_dep_graph_readiness()


def refresh_dependency_graph() -> None:
    project_sources = discover_project_sources()
    has_project_sources = bool(project_sources)
    archive_path = os.path.join("test_build", "libproject.a")
    broad_project_deps: set[str] | None = None
    changed_test_keys: set[str] = set()
    for test in state.all_tests:
        test_dep_file = test_dep_path(test.source_path)
        previous = sorted(set(test.dependencies))
        current: set[str] = set()
        current.update(_parse_dep_file(test_dep_file))
        if has_project_sources:
            map_path = test_map_path(test.source_path)
            linked_members = _parse_linked_archive_members_from_map(map_path, archive_path)
            if linked_members is None:
                if broad_project_deps is None:
                    broad_project_deps = _collect_project_dependencies()
                current.update(broad_project_deps)
            else:
                current.update(_collect_linked_member_dependencies(linked_members))
        updated = sorted(current)
        test.dependencies = updated
        if updated != previous:
            changed_test_keys.add(_normalize_dep_path(test.source_path))

    rebuild_dep_index()
    update_dep_graph_readiness()
    save_dependency_db(changed_test_keys)


def generate_makefile():
    os.makedirs("test_build", exist_ok=True)
    for test in state.all_tests:
        if not test.include_dirs:
            test.include_dirs = resolve_include_dirs(test.source_path)

    message_length = max(20, int(global_state.subprocess_columns))
    debug_flags = "-g -O0 -fno-omit-frame-pointer" if global_state.debug_build_enabled else ""
    cflags = global_state.cflags

    project_sources = discover_project_sources()
    all_include_dirs = set()
    for test in state.all_tests:
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
            lines.append(
                f"\tgcc {include_flags} {debug_flags} -fdiagnostics-color=always -fmessage-length={message_length} -MMD -MP -MF {dep_path} -c $< -o $@ {cflags}"
            )
            lines.append("")

        lines.append(f"{lib_target}: {' '.join(obj_files)}")
        lines.append(f"\tar rcs $@ $^")
        lines.append("")

    for test in state.all_tests:
        target = test_binary_path(test.source_path)
        source = test.source_path
        dep_file = test_dep_path(test.source_path)
        map_file = test_map_path(test.source_path)
        test_include_flags = " ".join(f"-I{d}" for d in test.include_dirs)
        if project_sources:
            lines.append(f"{target}: {source} {lib_target}")
            lines.append(
                f"\tgcc {test_include_flags} {debug_flags} -fdiagnostics-color=always -fmessage-length={message_length} -MMD -MP -MF {dep_file} -Wl,-Map,{map_file} -o {target} {source} {lib_target} {cflags}"
            )
        else:
            lines.append(f"{target}: {source}")
            lines.append(
                f"\tgcc {test_include_flags} {debug_flags} -fdiagnostics-color=always -fmessage-length={message_length} -MMD -MP -MF {dep_file} -o {target} {source} {cflags}"
            )
        lines.append("")

    with open("test_build/Makefile", "w") as f:
        f.write("\n".join(lines))
