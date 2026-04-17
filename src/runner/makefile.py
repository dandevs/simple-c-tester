import os

from state import state, dep_index


def rebuild_dep_index():
    global dep_index
    dep_index = {}
    for test in state.all_tests:
        for dep in test.dependencies:
            dep_index.setdefault(dep, []).append(test)


def generate_makefile():
    os.makedirs("test_build", exist_ok=True)
    lines = ["-include test_build/*.d", ""]
    for test in state.all_tests:
        target = f"test_build/{test.name}"
        source = test.source_path
        dep_file = f"test_build/{test.name}.d"
        lines.append(f"{target}: {source}")
        lines.append(
            f"\tgcc -fdiagnostics-color=always -fmessage-length=$${{COLUMNS:-80}} -MMD -MP -MF {dep_file} -o {target} {source}"
        )
        lines.append("")
    with open("test_build/Makefile", "w") as f:
        f.write("\n".join(lines))
