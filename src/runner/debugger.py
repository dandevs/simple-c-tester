import asyncio
import contextlib
import os
from dataclasses import dataclass

from pygdbmi.gdbmiparser import parse_response


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_token(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        self._pending: dict[int, asyncio.Future[dict]] = {}
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
            await self._send_command(f'-interpreter-exec console "skip file {path}"')

    async def configure_manual_stepping(self) -> None:
        await self._send_command('-interpreter-exec console "set scheduler-locking step"')

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

    async def interrupt_nowait(self) -> None:
        await self._send_command_no_wait("-exec-interrupt")

    async def list_simple_variables(self, timeout: float = 2.0) -> list[tuple[str, str]]:
        result = await self._send_command("-stack-list-variables --simple-values", timeout=timeout)
        return self._parse_variable_list(result)

    async def list_all_variables(self, timeout: float = 2.0) -> list[tuple[str, str]]:
        result = await self._send_command("-stack-list-variables --all-values", timeout=timeout)
        return self._parse_variable_list(result)

    def _parse_variable_list(self, result: dict) -> list[tuple[str, str]]:
        if result.get("message") == "error":
            return []

        payload = result.get("payload")
        if not isinstance(payload, dict):
            return []

        variables = payload.get("variables")
        if not isinstance(variables, list):
            return []

        parsed: list[tuple[str, str]] = []
        for item in variables:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            value = item.get("value", "?")
            if not isinstance(value, str):
                value = str(value)
            if len(value) > 120:
                value = value[:117] + "..."
            parsed.append((name, value))
        return parsed

    async def var_create(
        self,
        expression: str,
        frame: str = "*",
        timeout: float = 2.0,
    ) -> dict | None:
        escaped = expression.replace("\\", "\\\\").replace('"', '\\"')
        result = await self._send_command(
            f'-var-create - {frame} "{escaped}"',
            timeout=timeout,
        )
        if result.get("message") == "error":
            return None

        payload = result.get("payload")
        if not isinstance(payload, dict):
            return None

        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return None

        value = payload.get("value", "?")
        if not isinstance(value, str):
            value = str(value)

        return {
            "name": name,
            "numchild": _as_int(payload.get("numchild"), 0),
            "value": value,
            "type": str(payload.get("type", "")),
        }

    async def var_list_children(
        self,
        var_name: str,
        timeout: float = 2.0,
    ) -> list[dict]:
        result = await self._send_command(
            f"-var-list-children --all-values {var_name}",
            timeout=timeout,
        )
        if result.get("message") == "error":
            return []

        payload = result.get("payload")
        if not isinstance(payload, dict):
            return []

        children = payload.get("children")
        if not isinstance(children, list):
            return []

        normalized: list[dict] = []
        for item in children:
            if not isinstance(item, dict):
                continue
            child = item.get("child") if isinstance(item.get("child"), dict) else item
            if not isinstance(child, dict):
                continue

            child_name = child.get("name")
            if not isinstance(child_name, str) or not child_name:
                continue

            value = child.get("value", "?")
            if not isinstance(value, str):
                value = str(value)
            if len(value) > 120:
                value = value[:117] + "..."

            normalized.append(
                {
                    "name": child_name,
                    "exp": str(child.get("exp", "")),
                    "value": value,
                    "numchild": _as_int(child.get("numchild"), 0),
                    "type": str(child.get("type", "")),
                }
            )

        return normalized

    async def var_delete(self, var_name: str, timeout: float = 1.0) -> None:
        if not var_name:
            return
        try:
            await self._send_command(f"-var-delete {var_name}", timeout=timeout)
        except Exception:
            pass

    async def var_evaluate(self, var_name: str, timeout: float = 1.5) -> str | None:
        if not var_name:
            return None
        result = await self._send_command(
            f"-var-evaluate-expression {var_name}",
            timeout=timeout,
        )
        if result.get("message") == "error":
            return None

        payload = result.get("payload")
        if not isinstance(payload, dict):
            return None

        value = payload.get("value")
        if not isinstance(value, str):
            return None
        if len(value) > 120:
            value = value[:117] + "..."
        return value

    async def evaluate_expression(self, expression: str, timeout: float = 1.5) -> str | None:
        escaped = expression.replace("\\", "\\\\").replace('"', '\\"')
        result = await self._send_command(
            f'-data-evaluate-expression "{escaped}"',
            timeout=timeout,
        )
        if result.get("message") == "error":
            return None

        payload = result.get("payload")
        if not isinstance(payload, dict):
            return None

        value = payload.get("value")
        if not isinstance(value, str):
            return None
        if len(value) > 120:
            value = value[:117] + "..."
        return value

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
            try:
                result = await self._send_command(command, timeout=timeout)
            except asyncio.TimeoutError:
                return DebugStopEvent(reason="timeout", raw=f"{command} timed out")
            if result.get("message") == "error":
                return DebugStopEvent(reason="error", raw=str(result))
            try:
                return await asyncio.wait_for(self._stop_events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return DebugStopEvent(reason="timeout", raw=str(result))

    async def _send_command_no_wait(self, command: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("gdb process not started")

        token = self._token
        self._token += 1
        payload = f"{token}{command}\n".encode()
        self.proc.stdin.write(payload)
        await self.proc.stdin.drain()

    async def _send_command(self, command: str, timeout: float = 10.0) -> dict:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("gdb process not started")

        token = self._token
        self._token += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
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
            if not text or text == "(gdb)":
                continue

            try:
                record = parse_response(text)
            except Exception:
                record = None

            if not isinstance(record, dict):
                if self._console_output_callback is not None:
                    self._console_output_callback(text)
                continue

            rtype = record.get("type")
            message = record.get("message")
            token = _as_token(record.get("token"))
            payload = record.get("payload")

            if rtype == "result":
                future = self._pending.get(token) if token is not None else None
                if future is not None and not future.done():
                    future.set_result(record)
                continue

            if rtype == "notify" and message == "stopped":
                stop_event = self._parse_stop_event(payload, text)
                await self._stop_events.put(stop_event)
                continue

            if rtype in {"target", "output"}:
                if self._target_output_callback is not None and isinstance(payload, str) and payload:
                    self._target_output_callback(payload)
                continue

            if rtype in {"console", "log", "notify"}:
                if self._console_output_callback is not None:
                    if isinstance(payload, str) and payload:
                        self._console_output_callback(payload)
                    elif message:
                        self._console_output_callback(str(message))
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

    def _parse_stop_event(self, payload, raw: str) -> DebugStopEvent:
        payload_dict = payload if isinstance(payload, dict) else {}
        reason = str(payload_dict.get("reason", ""))
        signal_name = str(payload_dict.get("signal-name", ""))

        frame = payload_dict.get("frame") if isinstance(payload_dict.get("frame"), dict) else {}
        file_path = str(frame.get("fullname") or frame.get("file") or payload_dict.get("fullname") or payload_dict.get("file") or "")
        function = str(frame.get("func") or payload_dict.get("func") or "")
        line_number = _as_int(frame.get("line") or payload_dict.get("line"), 0)

        exit_code = None
        exit_code_raw = payload_dict.get("exit-code")
        if exit_code_raw is not None:
            try:
                exit_code = int(str(exit_code_raw), 0)
            except ValueError:
                exit_code = None

        if file_path and not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)

        return DebugStopEvent(
            reason=reason,
            file_path=file_path,
            line=line_number,
            function=function,
            exit_code=exit_code,
            signal_name=signal_name,
            raw=raw,
        )


def stop_event_is_terminal(stop_event: DebugStopEvent) -> bool:
    if stop_event.reason == "signal-received" and stop_event.signal_name == "SIGINT":
        return False
    return stop_event.reason.startswith("exited") or stop_event.reason in {
        "signal-received",
        "error",
    }
