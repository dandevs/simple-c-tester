import errno
import os
import shutil
import time
import asyncio
import re
from typing import Callable

import state as global_state
from state import state, active_processes, subprocess_columns
from models import Test, TestState, TimelineEvent
from .makefile import (
    refresh_dependency_graph,
    generate_makefile,
    build_project_sources,
)
from .artifacts import test_binary_path
from .debugger import GdbMIController, DebugStopEvent, stop_event_is_terminal


MAX_TIMELINE_EVENTS = 12000
MAX_DEBUG_LOG_LINES = 4000
_debug_sessions: dict[str, GdbMIController] = {}
_source_line_cache: dict[str, list[str]] = {}

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_CONTROL_WORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "catch",
}
_NON_USER_PREFIXES = (
    "/usr/",
    "/lib/",
    "/lib64/",
    "/opt/",
    "/nix/",
)


def _test_key(test: Test) -> str:
    return os.path.abspath(test.source_path)


def _append_debug_log(test: Test, message: str) -> None:
    if not message:
        return
    for line in message.replace("\r", "").split("\n"):
        if line:
            test.debug_logs.append(line)
    if len(test.debug_logs) > MAX_DEBUG_LOG_LINES:
        test.debug_logs = test.debug_logs[-MAX_DEBUG_LOG_LINES:]


def _append_timeline_event(
    test: Test,
    kind: str,
    message: str,
    file_path: str = "",
    line: int = 0,
    function: str = "",
    stream: str = "",
) -> None:
    event = TimelineEvent(
        index=len(test.timeline_events) + 1,
        timestamp=time.monotonic(),
        kind=kind,
        message=message,
        file_path=file_path,
        line=line,
        function=function,
        stream=stream,
    )
    test.timeline_events.append(event)
    if len(test.timeline_events) > MAX_TIMELINE_EVENTS:
        test.timeline_events = test.timeline_events[-MAX_TIMELINE_EVENTS:]


def _start_timeline_run(test: Test, reason: str) -> None:
    run_index = (
        sum(1 for event in test.timeline_events if event.kind == "run_start") + 1
    )
    _append_timeline_event(test, "run_start", f"run {run_index}: {reason}")


def _stop_reason_message(stop_event: DebugStopEvent) -> str:
    location = ""
    if stop_event.file_path and stop_event.line > 0:
        location = f" at {stop_event.file_path}:{stop_event.line}"
    elif stop_event.file_path:
        location = f" at {stop_event.file_path}"

    function = f" [{stop_event.function}]" if stop_event.function else ""
    if stop_event.reason.startswith("exited"):
        if stop_event.exit_code is None:
            return stop_event.reason
        return f"{stop_event.reason} ({stop_event.exit_code})"
    if stop_event.reason == "signal-received" and stop_event.signal_name:
        return f"signal: {stop_event.signal_name}{location}{function}"
    if stop_event.reason == "end-stepping-range":
        return f"step{location}{function}"
    if stop_event.reason == "breakpoint-hit":
        return f"breakpoint{location}{function}"
    if stop_event.reason == "function-finished":
        return f"step-out complete{location}{function}"
    if stop_event.reason:
        return f"{stop_event.reason}{location}{function}"
    return stop_event.raw or "debug stop"


def _load_source_lines(file_path: str) -> list[str]:
    cached = _source_line_cache.get(file_path)
    if cached is not None:
        return cached

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []

    _source_line_cache[file_path] = lines
    return lines


def _line_text(file_path: str, line_number: int) -> str:
    if not file_path or line_number <= 0:
        return ""
    lines = _load_source_lines(file_path)
    if line_number > len(lines):
        return ""
    return lines[line_number - 1]


def _line_has_likely_call(file_path: str, line_number: int) -> bool:
    line = _line_text(file_path, line_number)
    if not line:
        return False
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False

    for match in _CALL_RE.finditer(stripped):
        name = match.group(1)
        if name not in _CONTROL_WORDS:
            return True
    return False


def _is_user_code_path(file_path: str) -> bool:
    if not file_path:
        return False
    abs_path = os.path.abspath(file_path)
    if any(abs_path.startswith(prefix) for prefix in _NON_USER_PREFIXES):
        return False

    cwd = os.path.abspath(".") + os.sep
    tests_root = os.path.abspath("tests") + os.sep
    src_root = os.path.abspath("src") + os.sep
    return abs_path.startswith(cwd) or abs_path.startswith(tests_root) or abs_path.startswith(src_root)


async def _auto_trace_step(controller: GdbMIController, stop_event: DebugStopEvent) -> DebugStopEvent:
    if stop_event_is_terminal(stop_event):
        return stop_event

    if not stop_event.file_path or stop_event.line <= 0:
        return await controller.next()

    in_user_code = _is_user_code_path(stop_event.file_path)
    if not in_user_code:
        return await controller.step_out()

    wants_step_in = _line_has_likely_call(stop_event.file_path, stop_event.line)
    next_event = await (controller.step_in() if wants_step_in else controller.next())

    if wants_step_in and not stop_event_is_terminal(next_event) and not _is_user_code_path(next_event.file_path):
        return await controller.step_out()

    return next_event


def _record_stop_event(test: Test, stop_event: DebugStopEvent) -> None:
    _append_timeline_event(
        test,
        "step",
        _stop_reason_message(stop_event),
        file_path=stop_event.file_path,
        line=stop_event.line,
        function=stop_event.function,
    )


def _apply_terminal_stop(test: Test, stop_event: DebugStopEvent) -> None:
    code = stop_event.exit_code
    if stop_event.reason == "exited-normally":
        code = 0 if code is None else code
    test.debug_exited = True
    test.debug_running = False
    test.debug_exit_code = code

    if stop_event.reason.startswith("exited") and (code is None or code == 0):
        test.state = TestState.PASSED
    else:
        test.state = TestState.FAILED
    test.time_state_changed = time.monotonic()


def _debug_callbacks(test: Test):
    def _on_target_output(chunk: str) -> None:
        test.stdout += chunk
        test.stdout_raw += chunk.encode(errors="replace")
        for line in chunk.replace("\r", "").split("\n"):
            if line:
                _append_timeline_event(test, "stdout", line, stream="stdout")

    def _on_console_output(chunk: str) -> None:
        _append_debug_log(test, chunk)

    return _on_target_output, _on_console_output


def _ensure_debug_build_mode(enabled: bool) -> None:
    if global_state.debug_build_enabled == enabled:
        return
    global_state.debug_build_enabled = enabled
    generate_makefile()
    build_project_sources()
    refresh_dependency_graph()


async def _terminate_active_processes() -> None:
    for controller in list(_debug_sessions.values()):
        try:
            await controller.shutdown()
        except Exception:
            pass
    _debug_sessions.clear()
    global_state.active_debug_test_key = None

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


async def _compile_binary_for_test(test: Test, proc_env: dict[str, str]) -> tuple[bool, str]:
    process_key = _test_key(test)
    binary_path = test_binary_path(test.source_path)

    _append_timeline_event(test, "compile_start", f"compiling {binary_path}")
    make_proc = await asyncio.create_subprocess_exec(
        "make",
        "-f",
        "test_build/Makefile",
        binary_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    active_processes[process_key] = make_proc
    _, make_stderr = await make_proc.communicate()
    if active_processes.get(process_key) is make_proc:
        active_processes.pop(process_key, None)

    refresh_dependency_graph()

    if make_proc.returncode != 0:
        test.compile_err = make_stderr.decode(errors="replace")
        test.compile_err_raw = make_stderr
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        global_state.dep_graph_ready = False
        global_state.dep_graph_reason = "compile errors present"
        _append_timeline_event(test, "compile_failed", "compile failed")
        return False, binary_path

    test.compile_err = ""
    test.compile_err_raw = b""
    _append_timeline_event(test, "compile_ok", "compile succeeded")
    return True, binary_path


async def _run_plain_binary(test: Test, binary_path: str, proc_env: dict[str, str]) -> None:
    run_cmd = [f"./{binary_path}"]
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
                test.stderr = f"test executable missing: ./{binary_path}"
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

    process_key = _test_key(test)
    active_processes[process_key] = run_proc

    test.stdout = ""
    test.stdout_raw = b""
    test.stderr = ""
    test.stderr_raw = b""
    _append_timeline_event(test, "run_start", f"running {binary_path}")

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
                _append_timeline_event(test, "stdout", decoded_line.rstrip("\n"), stream="stdout")
            else:
                test.stderr += decoded_line
                test.stderr_raw += line
                _append_timeline_event(test, "stderr", decoded_line.rstrip("\n"), stream="stderr")

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

    test.stdout = "".join(stdout_parts)
    test.stdout_raw = b"".join(stdout_raw_parts)
    test.stderr = "".join(stderr_parts)
    test.stderr_raw = b"".join(stderr_raw_parts)

    if run_proc.returncode == 0:
        test.state = TestState.PASSED
        _append_timeline_event(test, "run_exit", "exited 0")
    else:
        test.state = TestState.FAILED
        _append_timeline_event(test, "run_exit", f"exited {run_proc.returncode}")
    test.time_state_changed = time.monotonic()


async def _run_auto_debug_trace(test: Test, binary_path: str, proc_env: dict[str, str]) -> None:
    process_key = _test_key(test)
    controller = GdbMIController(f"./{binary_path}", env=proc_env)
    target_callback, console_callback = _debug_callbacks(test)
    controller.set_target_output_callback(target_callback)
    controller.set_console_output_callback(console_callback)

    test.debug_running = True
    test.debug_exited = False
    test.debug_exit_code = None
    _append_timeline_event(test, "debug_start", f"gdb trace start: {binary_path}")

    await controller.start()
    if controller.proc is not None:
        active_processes[process_key] = controller.proc
    await controller.configure()

    stop_event = await controller.break_main_and_run()
    _record_stop_event(test, stop_event)

    max_steps = 50000
    step_count = 0
    while not stop_event_is_terminal(stop_event) and step_count < max_steps:
        if test.state == TestState.CANCELLED:
            _append_timeline_event(test, "debug_cancelled", "cancelled while tracing")
            return
        stop_event = await _auto_trace_step(controller, stop_event)
        _record_stop_event(test, stop_event)
        step_count += 1

    if step_count >= max_steps and not stop_event_is_terminal(stop_event):
        _append_timeline_event(test, "debug_limit", f"step cap reached ({max_steps})")
        test.state = TestState.FAILED
        test.debug_running = False
        test.debug_exited = True
        test.time_state_changed = time.monotonic()
        return

    if stop_event_is_terminal(stop_event):
        _apply_terminal_stop(test, stop_event)
        _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))

    await controller.shutdown()
    if active_processes.get(process_key) is controller.proc:
        active_processes.pop(process_key, None)


async def run_test(test: Test, on_complete: Callable[[], None]):
    process_key = _test_key(test)
    try:
        if test.state == TestState.CANCELLED:
            return

        proc_env = os.environ.copy()
        proc_env["COLUMNS"] = str(max(20, subprocess_columns))

        if test.timeline_capture_enabled or global_state.timeline_capture_enabled:
            _ensure_debug_build_mode(True)

        _start_timeline_run(test, "scheduled")
        compiled, binary_path = await _compile_binary_for_test(test, proc_env)
        if test.state == TestState.CANCELLED:
            return
        if not compiled:
            return

        test.time_start = time.monotonic()
        test.stdout = ""
        test.stdout_raw = b""
        test.stderr = ""
        test.stderr_raw = b""
        test.debug_logs = []

        if test.timeline_capture_enabled or global_state.timeline_capture_enabled:
            await _run_auto_debug_trace(test, binary_path, proc_env)
        else:
            await _run_plain_binary(test, binary_path, proc_env)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if test.state != TestState.CANCELLED:
            test.stderr = f"runner error: {e}"
            test.stderr_raw = b""
            test.state = TestState.FAILED
            test.time_state_changed = time.monotonic()
            global_state.dep_graph_ready = False
            global_state.dep_graph_reason = "runner error"
            _append_timeline_event(test, "runner_error", str(e))
    finally:
        if test.state != TestState.CANCELLED:
            active_processes.pop(process_key, None)
        on_complete()


def _get_debug_session(test: Test) -> GdbMIController | None:
    return _debug_sessions.get(_test_key(test))


async def start_debug_session(test: Test, auto_trace: bool = True) -> None:
    test_key = _test_key(test)
    existing = _debug_sessions.get(test_key)
    if existing is not None:
        return

    if global_state.active_debug_test_key and global_state.active_debug_test_key != test_key:
        return

    _ensure_debug_build_mode(True)

    test.state = TestState.RUNNING
    test.time_start = time.monotonic()
    test.time_state_changed = test.time_start
    test.stdout = ""
    test.stdout_raw = b""
    test.stderr = ""
    test.stderr_raw = b""
    test.debug_logs = []
    test.debug_running = True
    test.debug_exited = False
    test.debug_exit_code = None
    _start_timeline_run(test, "manual debug")

    proc_env = os.environ.copy()
    proc_env["COLUMNS"] = str(max(20, subprocess_columns))

    compiled, binary_path = await _compile_binary_for_test(test, proc_env)
    if not compiled:
        test.debug_running = False
        return

    controller = GdbMIController(f"./{binary_path}", env=proc_env)
    target_callback, console_callback = _debug_callbacks(test)
    controller.set_target_output_callback(target_callback)
    controller.set_console_output_callback(console_callback)

    global_state.active_debug_test_key = test_key
    _debug_sessions[test_key] = controller

    try:
        await controller.start()
        if controller.proc is not None:
            active_processes[test_key] = controller.proc
        await controller.configure()
        initial_stop = await controller.break_main_and_run()
        _record_stop_event(test, initial_stop)

        if stop_event_is_terminal(initial_stop):
            _apply_terminal_stop(test, initial_stop)
            _append_timeline_event(test, "debug_end", _stop_reason_message(initial_stop))
            await stop_debug_session(test)
            return

        if auto_trace:
            await debug_continue_auto_trace(test)
    except Exception as e:
        _append_timeline_event(test, "debug_error", str(e))
        test.stderr = f"debug error: {e}"
        test.state = TestState.FAILED
        test.time_state_changed = time.monotonic()
        await stop_debug_session(test)


async def stop_debug_session(test: Test) -> None:
    test_key = _test_key(test)
    controller = _debug_sessions.pop(test_key, None)
    if controller is not None:
        await controller.shutdown()
        if active_processes.get(test_key) is controller.proc:
            active_processes.pop(test_key, None)

    test.debug_running = False
    if not test.debug_exited:
        test.debug_exited = True
        test.debug_exit_code = None
        _append_timeline_event(test, "debug_end", "debug session stopped")
    if global_state.active_debug_test_key == test_key:
        global_state.active_debug_test_key = None


async def _debug_step(test: Test, action: str) -> None:
    controller = _get_debug_session(test)
    if controller is None:
        return

    if action == "next":
        stop_event = await controller.next()
    elif action == "step_in":
        stop_event = await controller.step_in()
    elif action == "step_out":
        stop_event = await controller.step_out()
    elif action == "continue":
        stop_event = await controller.continue_run()
    elif action == "interrupt":
        stop_event = await controller.interrupt()
    else:
        return

    _record_stop_event(test, stop_event)
    if stop_event_is_terminal(stop_event):
        _apply_terminal_stop(test, stop_event)
        _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))
        await stop_debug_session(test)


async def debug_step_next(test: Test) -> None:
    await _debug_step(test, "next")


async def debug_step_in(test: Test) -> None:
    await _debug_step(test, "step_in")


async def debug_step_out(test: Test) -> None:
    await _debug_step(test, "step_out")


async def debug_continue(test: Test) -> None:
    await _debug_step(test, "continue")


async def debug_interrupt(test: Test) -> None:
    await _debug_step(test, "interrupt")


async def debug_continue_auto_trace(test: Test) -> None:
    def _latest_line_event() -> TimelineEvent | None:
        for event in reversed(test.timeline_events):
            if event.file_path and event.line > 0:
                return event
        return None

    max_steps = 50000
    for _ in range(max_steps):
        if not test.debug_running:
            return
        controller = _get_debug_session(test)
        if controller is None:
            return
        latest_event = _latest_line_event()
        current_stop = DebugStopEvent(
            file_path=latest_event.file_path if latest_event is not None else "",
            line=latest_event.line if latest_event is not None else 0,
            function=latest_event.function if latest_event is not None else "",
        )
        stop_event = await _auto_trace_step(controller, current_stop)
        _record_stop_event(test, stop_event)
        if stop_event_is_terminal(stop_event):
            _apply_terminal_stop(test, stop_event)
            _append_timeline_event(test, "debug_end", _stop_reason_message(stop_event))
            await stop_debug_session(test)
            return

    _append_timeline_event(test, "debug_limit", f"step cap reached ({max_steps})")
    test.state = TestState.FAILED
    test.time_state_changed = time.monotonic()
    await stop_debug_session(test)


def is_debug_active(test: Test) -> bool:
    return _get_debug_session(test) is not None


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
