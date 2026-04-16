import argparse
import os
import subprocess
import time
import threading
from typing import Callable

from rich.console import Console
from rich.tree import Tree

from models import Test, Suite, AppState, TestState

state = AppState()
active_threads = 0
thread_lock = threading.Lock()

def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=1, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    return parser.parse_args()


def build_tree(suite: Suite) -> Tree:
    tree = Tree(suite.name)
    for test in suite.tests:
        tree.add(test.name)
    for child in suite.children:
        tree.add(build_tree(child))
    return tree


def main():
    args = parse_args()
    state.populate_suites("c/tests")
    state.available_runners = args.parallel
    console = Console()
    root = Tree(state.root_suite.name)
    for test in state.root_suite.tests:
        root.add(test.name)
    for suite in state.root_suite.children:
        root.add(build_tree(suite))
    console.print(root)
    state_changed()

    while True:
        time.sleep(0.1)


def run_test(test: Test, on_complete: Callable[[], None]):
    global active_threads
    print(f"Dispatching test: {test.name}")
    test.state = TestState.RUNNING
    test.time_state_changed = time.monotonic()

    def finish():
        global active_threads
        os.makedirs("test_build", exist_ok=True)

        compile_result = subprocess.run(
            [
                "gcc",
                "-MMD",
                "-MP",
                "-MF",
                f"test_build/{test.name}.d",
                "-o",
                f"test_build/{test.name}",
                test.source_path,
            ],
            capture_output=True,
            text=True,
        )

        if compile_result.returncode != 0:
            test.compile_err = compile_result.stderr
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
            on_complete()
            with thread_lock:
                active_threads -= 1
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

        run_result = subprocess.run(
            [f"./test_build/{test.name}"],
            capture_output=True,
            text=True,
        )

        test.stdout = run_result.stdout
        test.stderr = run_result.stderr

        if run_result.returncode == 0:
            test.state = TestState.PASSED
        else:
            test.state = TestState.FAILED

        test.time_state_changed = time.monotonic()
        on_complete()
        with thread_lock:
            active_threads -= 1

    threading.Thread(target=finish, daemon=True).start()


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

        run_test(test, on_complete)

    if len(tests_to_run) > 0:
        state_changed()


if __name__ == "__main__":
    main()
