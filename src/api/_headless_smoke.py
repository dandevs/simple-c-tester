"""Headless smoke test for the ``api`` package.

Run from a project root that contains a ``tests/`` directory (e.g. ``c/``)::

    cd c
    ../.venv/bin/python ../src/api/_headless_smoke.py

Verifies that a :class:`TestRunner` can discover tests, prepare the build,
run them, and report results — with NO Textual import anywhere in the call
path.  This is the proof that the API/systems layer is fully separable from
the UI.
"""

import asyncio
import sys
import os

# Ensure bare imports resolve (same hack as main.py).  This file lives in
# src/api/, so go up two levels to add src/ to sys.path.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


async def main() -> int:
    from api import TestRunner, RunnerConfig

    runner = TestRunner(RunnerConfig(parallel=4))

    transitions: list[str] = []
    finished: list[str] = []

    def on_change(event):
        transitions.append(f"{event.test_key} -> {event.new_state.name}")

    def on_finish(event):
        finished.append(f"{event.test_key}: {'PASS' if event.passed else 'FAIL'}")

    runner.events.subscribe("test_state_changed", on_change)
    runner.events.subscribe("test_finished", on_finish)

    print("[smoke] discovering tests...")
    tests = runner.discover("tests")
    print(f"[smoke] discovered {len(tests)} tests")

    print("[smoke] preparing build...")
    runner.prepare_build()

    print("[smoke] running all tests...")
    await runner.run_all()

    passed = sum(1 for t in runner.tests if t.state.name == "PASSED")
    failed = sum(1 for t in runner.tests if t.state.name == "FAILED")
    print(f"[smoke] results: {passed} passed, {failed} failed, {len(runner.tests)} total")
    print(f"[smoke] events: {len(transitions)} state-changed, {len(finished)} finished")

    # Demonstrate single-test run via the API.
    if tests:
        sample = tests[0]
        print(f"[smoke] single-test rerun: {sample.name}")
        await runner.run_test(sample)
        print(f"[smoke]   final state: {sample.state.name}")

    runner.save_db()
    print("[smoke] DONE — headless API works with zero Textual dependency")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
