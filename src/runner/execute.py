import errno
import os
import shutil
import time
import asyncio
from typing import Callable

from state import state, active_processes, subprocess_columns
from models import Test, TestState
from .makefile import refresh_dependency_graph


async def _terminate_active_processes() -> None:
    processes = {proc for proc in active_processes.values() if proc.returncode is None}
    for proc in processes:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

    if processes:
        await asyncio.gather(
            *(proc.wait() for proc in processes), return_exceptions=True
        )

    active_processes.clear()


async def run_test(test: Test, on_complete: Callable[[], None]):
    process_key = os.path.abspath(test.source_path)
    try:
        if test.state == TestState.CANCELLED:
            return

        proc_env = os.environ.copy()
        proc_env["COLUMNS"] = str(max(20, subprocess_columns))

        make_proc = await asyncio.create_subprocess_exec(
            "make",
            "-f",
            "test_build/Makefile",
            f"test_build/{test.name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
        active_processes[process_key] = make_proc
        _, make_stderr = await make_proc.communicate()
        if active_processes.get(process_key) is make_proc:
            active_processes.pop(process_key, None)

        refresh_dependency_graph()

        if test.state == TestState.CANCELLED:
            return

        if make_proc.returncode != 0:
            test.compile_err = make_stderr.decode(errors="replace")
            test.compile_err_raw = make_stderr
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
            return

        test.compile_err = ""
        test.compile_err_raw = b""

        run_cmd = [f"./test_build/{test.name}"]
        stdbuf_path = shutil.which("stdbuf")
        if stdbuf_path:
            run_cmd = [stdbuf_path, "-oL", "-eL", *run_cmd]

        run_proc = None
        for _ in range(10):
            try:
                run_proc = await asyncio.create_subprocess_exec(
                    *run_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                )
                break
            except OSError as e:
                if e.errno == errno.ETXTBSY:
                    await asyncio.sleep(0.05)
                    continue
                if e.errno == errno.ENOENT:
                    test.stderr = f"test executable missing: ./test_build/{test.name}"
                    test.stderr_raw = b""
                    test.state = TestState.FAILED
                    test.time_state_changed = time.monotonic()
                    return
                raise

        if run_proc is None:
            run_proc = await asyncio.create_subprocess_exec(
                *run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )

        test.time_start = time.monotonic()
        active_processes[process_key] = run_proc

        test.stdout = ""
        test.stdout_raw = b""
        test.stderr = ""
        test.stderr_raw = b""

        async def _read_stream(
            stream: asyncio.StreamReader | None,
            dest_str: list[str],
            dest_raw: list[bytes],
            is_stdout: bool,
        ):
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded_line = line.decode(errors="replace")
                dest_str.append(decoded_line)
                dest_raw.append(line)

                if is_stdout:
                    test.stdout += decoded_line
                    test.stdout_raw += line
                else:
                    test.stderr += decoded_line
                    test.stderr_raw += line

        stdout_parts: list[str] = []
        stdout_raw_parts: list[bytes] = []
        stderr_parts: list[str] = []
        stderr_raw_parts: list[bytes] = []
        await asyncio.gather(
            _read_stream(run_proc.stdout, stdout_parts, stdout_raw_parts, True),
            _read_stream(run_proc.stderr, stderr_parts, stderr_raw_parts, False),
            run_proc.wait(),
        )
        if active_processes.get(process_key) is run_proc:
            active_processes.pop(process_key, None)

        if test.state == TestState.CANCELLED:
            return

        test.stdout = "".join(stdout_parts)
        test.stdout_raw = b"".join(stdout_raw_parts)
        test.stderr = "".join(stderr_parts)
        test.stderr_raw = b"".join(stderr_raw_parts)

        if run_proc.returncode == 0:
            test.state = TestState.PASSED
        else:
            test.state = TestState.FAILED

        test.time_state_changed = time.monotonic()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if test.state != TestState.CANCELLED:
            test.stderr = f"runner error: {e}"
            test.stderr_raw = b""
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
    finally:
        if test.state != TestState.CANCELLED:
            active_processes.pop(process_key, None)
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
        test.state = TestState.RUNNING
        test.time_start = 0.0
        test.time_state_changed = time.monotonic()
        tests_to_run.append(test)

    for test in tests_to_run:

        def on_complete(completed_test: Test = test):
            state.available_runners += 1
            if completed_test.state == TestState.CANCELLED:
                completed_test.state = TestState.PENDING
                completed_test.time_start = 0.0
                completed_test.time_state_changed = time.monotonic()
            state_changed()

        asyncio.ensure_future(run_test(test, on_complete))

    if len(tests_to_run) > 0:
        state_changed()
