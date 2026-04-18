import os
import re
import subprocess
import json

from state import state, dep_index
import state as global_state


_MISSING_HEADER_RE = re.compile(r"fatal error:\s+(\S+):\s+No such file or directory")
SRC_DIR = os.path.abspath("src")
DB_PATH = os.path.join("test_build", "db.json")


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


def _parse_dep_file(dep_file: str) -> list[str]:
    if not os.path.exists(dep_file):
        return []

    with open(dep_file, "r", encoding="utf-8", errors="replace") as f:
        dep_content = f.read()

    if ":" not in dep_content:
        return []

    deps_part = dep_content.split(":", 1)[1].replace("\\\n", " ")
    deps: set[str] = set()
    for part in deps_part.split():
        candidate = part[:-1] if part.endswith("\\") else part
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


def load_dependency_db() -> dict[str, dict[str, list[str]]]:
    if not os.path.exists(DB_PATH):
        return {}

    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    tests_data = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests_data, dict):
        return {}

    hydrated: dict[str, dict[str, list[str]]] = {}
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


def save_dependency_db() -> None:
    os.makedirs("test_build", exist_ok=True)
    tests_payload: dict[str, dict[str, list[str]]] = {}
    for test in state.all_tests:
        test_key = _normalize_dep_path(test.source_path)
        tests_payload[test_key] = {
            "collected_dependencies": sorted(set(test.dependencies))
        }

    payload = {"tests": tests_payload}
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def hydrate_dependencies_from_db() -> None:
    db = load_dependency_db()
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
    project_deps = _collect_project_dependencies()
    for test in state.all_tests:
        test_dep_file = os.path.join("test_build", f"{test.name}.d")
        current = set(test.dependencies)
        current.update(_parse_dep_file(test_dep_file))
        current.update(project_deps)
        test.dependencies = sorted(current)

    rebuild_dep_index()
    update_dep_graph_readiness()
    save_dependency_db()


def generate_makefile():
    os.makedirs("test_build", exist_ok=True)
    for test in state.all_tests:
        if not test.include_dirs:
            test.include_dirs = resolve_include_dirs(test.source_path)

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
                f"\tgcc {include_flags} -fdiagnostics-color=always -fmessage-length=$${{COLUMNS:-80}} -MMD -MP -MF {dep_path} -c $< -o $@"
            )
            lines.append("")

        lines.append(f"{lib_target}: {' '.join(obj_files)}")
        lines.append(f"\tar rcs $@ $^")
        lines.append("")

    for test in state.all_tests:
        target = f"test_build/{test.name}"
        source = test.source_path
        dep_file = f"test_build/{test.name}.d"
        test_include_flags = " ".join(f"-I{d}" for d in test.include_dirs)
        if project_sources:
            lines.append(f"{target}: {source} {lib_target}")
            lines.append(
                f"\tgcc {test_include_flags} -fdiagnostics-color=always -fmessage-length=$${{COLUMNS:-80}} -MMD -MP -MF {dep_file} -o {target} {source} {lib_target}"
            )
        else:
            lines.append(f"{target}: {source}")
            lines.append(
                f"\tgcc {test_include_flags} -fdiagnostics-color=always -fmessage-length=$${{COLUMNS:-80}} -MMD -MP -MF {dep_file} -o {target} {source}"
            )
        lines.append("")

    with open("test_build/Makefile", "w") as f:
        f.write("\n".join(lines))
