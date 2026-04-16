import argparse
import asyncio
import os
import time
from typing import Callable

from rich.console import Console
from rich.tree import Tree
from rich.panel import Panel
from rich.live import Live
from rich.rule import Rule
from rich.text import Text
from rich.console import Group

from models import Test, Suite, AppState, TestState

state = AppState()
start_time = 0.0


def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=1, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    return parser.parse_args()


def status_icon(test: Test) -> str:
    if test.state == TestState.PASSED:
        return "[green]✓[/]"
    elif test.state == TestState.FAILED:
        return "[red]✗[/]"
    elif test.state == TestState.RUNNING:
        return "[yellow]⏳[/]"
    return "○"


def elapsed_ms(test: Test) -> str:
    if test.time_start > 0 and test.time_state_changed > 0:
        ms = (test.time_state_changed - test.time_start) * 1000
        return f"[dim]{int(ms)}ms[/]"
    return ""


def build_test_node(test: Test) -> Tree:
    label = f"{status_icon(test)} {test.name}  {elapsed_ms(test)}"
    node = Tree(label)

    if test.state == TestState.FAILED:
        error_content = ""
        title = "Error"
        if test.compile_err:
            title = "Compilation Error"
            error_content = test.compile_err.rstrip()
        elif test.stderr:
            title = "Runtime Error"
            error_content = test.stderr.rstrip()

        if error_content:
            panel = Panel(
                Text(error_content),
                title=f"[red]{title}[/]",
                border_style="red",
                padding=(0, 1),
            )
            node.add(panel)

    return node


def build_suite_tree(suite: Suite) -> Tree:
    tree = Tree(suite.name)
    for test in suite.tests:
        tree.add(build_test_node(test))
    for child in suite.children:
        tree.add(build_suite_tree(child))
    return tree


def build_renderable() -> tuple:
    root = Tree(f"[bold]{state.root_suite.name}[/] ── [dim]{elapsed_total()}[/]")
    for test in state.root_suite.tests:
        root.add(build_test_node(test))
    for suite in state.root_suite.children:
        root.add(build_suite_tree(suite))

    passed = sum(1 for t in state.all_tests if t.state == TestState.PASSED)
    failed = sum(1 for t in state.all_tests if t.state == TestState.FAILED)
    running = sum(1 for t in state.all_tests if t.state == TestState.RUNNING)
    pending = sum(1 for t in state.all_tests if t.state == TestState.PENDING)
    total = len(state.all_tests)

    if total == 0:
        summary = Text("No tests found.", style="dim")
    else:
        parts = []
        if passed:
            parts.append(f"[green]{passed}/{total} passed[/]")
        if failed:
            parts.append(f"[red]{failed} failed[/]")
        if running:
            parts.append(f"[yellow]{running} running[/]")
        if pending:
            parts.append(f"{pending} pending")
        summary = Text("  ".join(parts))

    return root, Rule(), summary


def elapsed_total() -> str:
    ms = (time.monotonic() - start_time) * 1000
    return f"[dim]{int(ms)}ms[/]"


def parse_deps(test: Test) -> None:
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


async def run_test(test: Test, on_complete: Callable[[], None]) -> None:
    test.state = TestState.RUNNING
    test.time_start = time.monotonic()
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
    _, compile_stderr = await compile_proc.communicate()

    parse_deps(test)

    if compile_proc.returncode != 0:
        test.compile_err = compile_stderr.decode("utf-8", errors="replace")
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        on_complete()
        return

    run_proc = await asyncio.create_subprocess_exec(
        f"./test_build/{test.name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    run_stdout, run_stderr = await run_proc.communicate()

    test.stdout = run_stdout.decode("utf-8", errors="replace")
    test.stderr = run_stderr.decode("utf-8", errors="replace")
    test.time_state_changed = time.monotonic()

    if run_proc.returncode == 0:
        test.state = TestState.PASSED
    else:
        test.state = TestState.FAILED

    on_complete()


def refresh_live(live: Live) -> None:
    tree, rule, summary = build_renderable()
    live.update(Group(tree, rule, summary))
    live.refresh()


async def state_changed(live: Live) -> None:
    pending_tests = sorted(
        [test for test in state.all_tests if test.state == TestState.PENDING],
        key=lambda t: t.time_state_changed,
    )

    tasks = []
    for test in pending_tests:
        if state.available_runners <= 0:
            break
        state.available_runners -= 1

        def make_on_complete(t: Test):
            def on_complete():
                state.available_runners += 1
                refresh_live(live)

            return on_complete

        task = asyncio.create_task(run_test(test, make_on_complete(test)))
        tasks.append(task)

    if tasks:
        await asyncio.gather(*tasks)

    still_pending = [t for t in state.all_tests if t.state == TestState.PENDING]
    if still_pending:
        await state_changed(live)


async def main():
    global start_time
    start_time = time.monotonic()

    args = parse_args()
    state.populate_suites("c/tests")
    state.available_runners = args.parallel

    console = Console()
    root, rule, summary = build_renderable()

    with Live(Group(root, rule, summary), console=console, auto_refresh=False) as live:
        await state_changed(live)
        tree, rule, summary = build_renderable()
        live.update(Group(tree, rule, summary))
        live.refresh()


if __name__ == "__main__":
    asyncio.run(main())
