import os
import re
import subprocess

from state import state, dep_index


_MISSING_HEADER_RE = re.compile(r"fatal error:\s+(\S+):\s+No such file or directory")


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


def rebuild_dep_index():
    global dep_index
    dep_index = {}
    for test in state.all_tests:
        for dep in test.dependencies:
            dep_index.setdefault(dep, []).append(test)


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
