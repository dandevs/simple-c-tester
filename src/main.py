import argparse
import asyncio
import os
import time
from typing import Callable

from rich.live import Live
from rich.panel import Panel
from rich.tree import Tree

from models import Test, Suite, AppState, TestState

state = AppState()


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=1, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    return parser.parse_args()


def build_suite_tree(suite: Suite) -> Tree:
    tree = Tree(suite.name)
    for test in suite.tests:
        _add_test_node(tree, test)
    for child in suite.children:
        tree.add(build_suite_tree(child))
    return tree


def _add_test_node(tree, test: Test):
    node = tree.add(test.name)
    if test.state == TestState.FAILED:
        error = test.compile_err or test.stderr or ""
        node.add(Panel(error.strip(), border_style="red"))


def build_display() -> Tree:
    root = Tree(state.root_suite.name)
    for test in state.root_suite.tests:
        _add_test_node(root, test)
    for suite in state.root_suite.children:
        root.add(build_suite_tree(suite))
    return root


async def main():
    args = parse_args()
    state.populate_suites("c/tests")
    state.available_runners = args.parallel
    state_changed()

    with Live(build_display(), refresh_per_second=10) as live:
        while True:
            live.update(build_display())
            await asyncio.sleep(0.1)


async def run_test(test: Test, on_complete: Callable[[], None]):
    print(f"Dispatching test: {test.name}")
    test.state = TestState.RUNNING
    test.time_state_changed = time.monotonic()

    os.makedirs("test_build", exist_ok=True)

    compile_proc = await asyncio.create_subprocess_exec(
        "gcc",
        "-MMD",
        "-MP",
        "-MF",
        f"test_build/{test.name}.d",
        "-o",
        f"test_build/{test.name}",
        test.source_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    compile_stdout, compile_stderr = await compile_proc.communicate()

    if compile_proc.returncode != 0:
        test.compile_err = compile_stderr.decode()
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        on_complete()
        return

    dep_file = f"test_build/{test.name}.d"
    if os.path.exists(dep_file):
        with open(dep_file, "r") as f:
            dep_content = f.read()
        colon_idx = dep_content.index(":")
        deps_str = dep_content[colon_idx + 1 :].strip()
        parts = deps_str.split()
        deps = []
        for part in parts:
            if part.endswith("\\"):
                part = part[:-1]
            if part:
                deps.append(os.path.abspath(part))
        test.dependencies = deps

    run_proc = await asyncio.create_subprocess_exec(
        f"./test_build/{test.name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    run_stdout, run_stderr = await run_proc.communicate()

    test.stdout = run_stdout.decode()
    test.stderr = run_stderr.decode()

    if run_proc.returncode == 0:
        test.state = TestState.PASSED
    else:
        test.state = TestState.FAILED

    test.time_state_changed = time.monotonic()
    on_complete()


def state_changed():
    tests_to_run: list[Test] = []
    pending_tests = sorted(
        [test for test in state.all_tests if test.state == TestState.PENDING],
        key=lambda t: t.time_state_changed,
    )

    while state.available_runners > 0 and len(pending_tests) > 0:
        test = pending_tests.pop()
        state.available_runners -= 1
        tests_to_run.append(test)

    for test in tests_to_run:

        def on_complete():
            state.available_runners += 1
            state_changed()

        asyncio.ensure_future(run_test(test, on_complete))

    if len(tests_to_run) > 0:
        state_changed()


if __name__ == "__main__":
    asyncio.run(main())
