import ast
import asyncio
import contextlib
import os
import re
from dataclasses import dataclass


_RESULT_RE = re.compile(r"^(\d+)\^(.*)$")
_KV_STRING_RE = re.compile(r'([a-zA-Z0-9_-]+)="((?:\\.|[^"\\])*)"')


def _decode_mi_c_string(raw: str) -> str:
    if not raw:
        return ""
    try:
        return ast.literal_eval(raw)
    except Exception:
        return raw.strip('"')


def _extract_kv(line: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in _KV_STRING_RE.finditer(line):
        key = match.group(1)
        value = bytes(match.group(2), "utf-8").decode("unicode_escape", errors="replace")
        values[key] = value
    return values


def _extract_list_payload(line: str, key: str) -> str:
    marker = f"{key}=["
    start = line.find(marker)
    if start < 0:
        return ""

    i = start + len(marker)
    payload_start = i
    depth = 1
    in_string = False
    escaped = False

    while i < len(line):
        ch = line[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return line[payload_start:i]
        i += 1

    return ""


def _split_top_level_objects(payload: str) -> list[str]:
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for i, ch in enumerate(payload):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                start = i + 1
            depth += 1
            continue

        if ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(payload[start:i])
                start = -1

    return objects


@dataclass
class DebugStopEvent:
    reason: str = ""
    file_path: str = ""
    line: int = 0
    function: str = ""
    exit_code: int | None = None
    signal_name: str = ""
    raw: str = ""


class GdbMIController:
    def __init__(self, binary_path: str, env: dict[str, str] | None = None):
        self.binary_path = binary_path
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[str]] = {}
        self._token = 1
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stop_events: asyncio.Queue[DebugStopEvent] = asyncio.Queue()
        self._command_lock = asyncio.Lock()
        self._target_output_callback = None
        self._console_output_callback = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "gdb",
            "--quiet",
            "--interpreter=mi2",
            self.binary_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    def set_target_output_callback(self, callback) -> None:
        self._target_output_callback = callback

    def set_console_output_callback(self, callback) -> None:
        self._console_output_callback = callback

    async def configure(self) -> None:
        await self._send_command("-gdb-set pagination off")
        await self._send_command("-gdb-set confirm off")
        await self._send_command("-gdb-set print pretty on")
        await self._send_command("-gdb-set disassemble-next-line off")
        await self._send_command('-interpreter-exec console "set step-mode on"')
        for path in (
            "/usr/*",
            "/lib/*",
            "/lib64/*",
            "/opt/*",
            "/nix/*",
        ):
            await self._send_command(
                f'-interpreter-exec console "skip file {path}"'
            )

    async def break_main_and_run(self) -> DebugStopEvent:
        await self._send_command("-break-insert main")
        return await self._run_until_stop("-exec-run")

    async def next(self) -> DebugStopEvent:
        return await self._run_until_stop("-exec-next")

    async def step_in(self) -> DebugStopEvent:
        return await self._run_until_stop("-exec-step")

    async def step_out(self) -> DebugStopEvent:
        return await self._run_until_stop("-exec-finish")

    async def continue_run(self) -> DebugStopEvent:
        return await self._run_until_stop("-exec-continue")

    async def interrupt(self) -> DebugStopEvent:
        return await self._run_until_stop("-exec-interrupt")

    async def list_simple_variables(self, timeout: float = 2.0) -> list[tuple[str, str]]:
        line = await self._send_command("-stack-list-variables --simple-values", timeout=timeout)
        if "^error" in line:
            return []

        variables: list[tuple[str, str]] = []
        payload = _extract_list_payload(line, "variables")
        if not payload:
            return variables

        for obj in _split_top_level_objects(payload):
            fields = _extract_kv(obj)
            name = fields.get("name")
            if not name:
                continue
            value = fields.get("value", "?")
            if len(value) > 120:
                value = value[:117] + "..."
            variables.append((name, value))

        return variables

    async def shutdown(self) -> None:
        if self.proc is None:
            return
        try:
            await self._send_command("-gdb-exit", timeout=1.0)
        except Exception:
            pass

        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

    async def _run_until_stop(self, command: str, timeout: float = 30.0) -> DebugStopEvent:
        async with self._command_lock:
            result_line = await self._send_command(command, timeout=timeout)
            if "^error" in result_line:
                return DebugStopEvent(reason="error", raw=result_line)
            try:
                return await asyncio.wait_for(self._stop_events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return DebugStopEvent(reason="timeout", raw=result_line)

    async def _send_command(self, command: str, timeout: float = 10.0) -> str:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("gdb process not started")

        token = self._token
        self._token += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[token] = future

        payload = f"{token}{command}\n".encode()
        self.proc.stdin.write(payload)
        await self.proc.stdin.drain()

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(token, None)

    async def _read_stdout(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            return

        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return

            text = line.decode(errors="replace").rstrip("\r\n")
            if not text:
                continue

            match = _RESULT_RE.match(text)
            if match:
                token = int(match.group(1))
                future = self._pending.get(token)
                if future is not None and not future.done():
                    future.set_result(text)
                continue

            if text.startswith("*stopped"):
                await self._stop_events.put(self._parse_stop_event(text))
                continue

            if text.startswith("@"):
                chunk = _decode_mi_c_string(text[1:])
                if self._target_output_callback is not None and chunk:
                    self._target_output_callback(chunk)
                continue

            if text.startswith("~") or text.startswith("&"):
                chunk = _decode_mi_c_string(text[1:])
                if self._console_output_callback is not None and chunk:
                    self._console_output_callback(chunk)
                continue

            if self._console_output_callback is not None:
                self._console_output_callback(text)

    async def _read_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            if text and self._console_output_callback is not None:
                self._console_output_callback(text)

    def _parse_stop_event(self, text: str) -> DebugStopEvent:
        values = _extract_kv(text)
        reason = values.get("reason", "")
        file_path = values.get("fullname") or values.get("file", "")
        function = values.get("func", "")
        line_value = values.get("line", "0")
        signal_name = values.get("signal-name", "")
        exit_code = None
        exit_code_raw = values.get("exit-code")
        if exit_code_raw is not None:
            try:
                exit_code = int(exit_code_raw, 0)
            except ValueError:
                exit_code = None

        try:
            line_number = int(line_value)
        except ValueError:
            line_number = 0

        if file_path and not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)

        return DebugStopEvent(
            reason=reason,
            file_path=file_path,
            line=line_number,
            function=function,
            exit_code=exit_code,
            signal_name=signal_name,
            raw=text,
        )


def stop_event_is_terminal(stop_event: DebugStopEvent) -> bool:
    return stop_event.reason.startswith("exited") or stop_event.reason in {
        "signal-received",
        "error",
        "timeout",
    }
