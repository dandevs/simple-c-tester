import os
import re
from dataclasses import dataclass, field

from ..debugger import DebugStopEvent


_BRANCH_RE = re.compile(r"\b(if|else\s+if|switch|\?)\b")
_LOOP_RE = re.compile(r"^\s*(for|while)\s*\(")
_DO_RE = re.compile(r"^\s*do\b")
_GOTO_RE = re.compile(r"\bgoto\s+([A-Za-z_]\w*)\s*;")
_LABEL_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(?:$|//|/\*)")
_ASSERT_RE = re.compile(r"\b(assert|ASSERT|EXPECT|CHECK)\b")
_SYNC_RE = re.compile(
    r"\b("
    r"pthread_mutex_lock|pthread_mutex_unlock|"
    r"pthread_cond_wait|pthread_cond_signal|pthread_cond_broadcast|"
    r"pthread_join|pthread_create|"
    r"sem_wait|sem_post|"
    r"atomic_thread_fence"
    r")\b"
)
_STANDALONE_IGNORE_RE = re.compile(
    r"\b(return|break|continue|goto|if|else|for|while|do|switch|case|default|sizeof|typeof)\b"
)
_CONTROL_FLOW_RE = re.compile(r"^\s*(return|break|continue|goto|if|else|for|while|do|switch|case|default)\b")
_RETURN_RE = re.compile(r"\breturn\b")
_ANOMALOUS_LITERAL_VALUES = {
    "0x0",
    "(nil)",
    "nullptr",
    "nan",
    "-nan",
    "inf",
    "+inf",
    "-inf",
    "0xdeadbeef",
    "0xbaadf00d",
    "0xcccccccc",
    "0xcdcdcdcd",
}
_FAIL_SIGNALS = {"SIGABRT", "SIGSEGV", "SIGFPE", "SIGILL", "SIGBUS"}


@dataclass(frozen=True)
class TriggerMatch:
    trigger_id: str
    label: str
    message: str


@dataclass
class StoryStopContext:
    stop_event: DebugStopEvent
    previous_stop: DebugStopEvent | None
    line_text: str
    previous_line_text: str
    variables: list[tuple[str, str, str]]


@dataclass
class StoryFilterRuntimeState:
    step_index: int = 0
    loop_hits: dict[tuple[str, str, int], int] = field(default_factory=dict)
    seen_functions: set[tuple[str, str]] = field(default_factory=set)
    seen_lines: set[tuple[str, int]] = field(default_factory=set)
    last_anomaly_keys: set[str] = field(default_factory=set)


def trigger_needs_variables(trigger_id: str) -> bool:
    return trigger_id == "anomaly"


def evaluate_trigger(
    trigger_id: str,
    ctx: StoryStopContext,
    runtime_state: StoryFilterRuntimeState,
    loop_every_n: int,
) -> TriggerMatch | None:
    if trigger_id == "function_enter":
        return _function_enter(ctx)
    if trigger_id == "function_exit":
        return _function_exit(ctx)
    if trigger_id == "branch_decision":
        return _branch_decision(ctx)
    if trigger_id == "loop_milestone":
        return _loop_milestone(ctx, runtime_state, loop_every_n)
    if trigger_id == "goto_jump":
        return _goto_jump(ctx)
    if trigger_id == "assert_line":
        return _assert_line(ctx)
    if trigger_id == "assert_failure":
        return _assert_failure(ctx)
    if trigger_id == "anomaly":
        return _anomaly(ctx, runtime_state)
    if trigger_id == "sync_event":
        return _sync_event(ctx)
    if trigger_id == "first_hit_function":
        return _first_hit_function(ctx, runtime_state)
    if trigger_id == "first_hit_line":
        return _first_hit_line(ctx, runtime_state)
    if trigger_id == "standalone_expr":
        return _standalone_expr(ctx)
    if trigger_id == "return_statement":
        return _return_statement(ctx)
    return None


def _function_enter(ctx: StoryStopContext) -> TriggerMatch | None:
    function = (ctx.stop_event.function or "").strip()
    if not function:
        return None
    previous = (ctx.previous_stop.function or "").strip() if ctx.previous_stop else ""
    if function == previous:
        return None
    return TriggerMatch(
        trigger_id="function_enter",
        label="Enter",
        message=f"entered {function}()",
    )


def _function_exit(ctx: StoryStopContext) -> TriggerMatch | None:
    if ctx.stop_event.reason == "function-finished":
        previous = (ctx.previous_stop.function or "").strip() if ctx.previous_stop else ""
        name = previous or (ctx.stop_event.function or "").strip() or "function"
        return TriggerMatch(
            trigger_id="function_exit",
            label="Exit",
            message=f"returned from {name}()",
        )

    if ctx.previous_stop is None:
        return None
    previous = (ctx.previous_stop.function or "").strip()
    current = (ctx.stop_event.function or "").strip()
    if previous and current and previous != current:
        return TriggerMatch(
            trigger_id="function_exit",
            label="Exit",
            message=f"left {previous}()",
        )
    return None


def _branch_decision(ctx: StoryStopContext) -> TriggerMatch | None:
    prev = ctx.previous_stop
    if prev is None:
        return None
    if not prev.file_path or prev.line <= 0:
        return None
    if os.path.abspath(prev.file_path) != os.path.abspath(ctx.stop_event.file_path):
        return None
    if (prev.function or "") != (ctx.stop_event.function or ""):
        return None
    if not _BRANCH_RE.search(ctx.previous_line_text):
        return None
    if prev.line == ctx.stop_event.line:
        return None
    return TriggerMatch(
        trigger_id="branch_decision",
        label="Branch",
        message=f"branch at L{prev.line} -> L{ctx.stop_event.line}",
    )


def _loop_milestone(
    ctx: StoryStopContext,
    runtime_state: StoryFilterRuntimeState,
    loop_every_n: int,
) -> TriggerMatch | None:
    line = ctx.line_text
    if not (_LOOP_RE.search(line) or _DO_RE.search(line)):
        return None

    file_path = os.path.abspath(ctx.stop_event.file_path)
    function = ctx.stop_event.function or ""
    key = (file_path, function, ctx.stop_event.line)
    count = runtime_state.loop_hits.get(key, 0) + 1
    runtime_state.loop_hits[key] = count

    if count == 1:
        return TriggerMatch(
            trigger_id="loop_milestone",
            label="Loop",
            message=f"loop enter L{ctx.stop_event.line}",
        )
    if count == 2:
        return TriggerMatch(
            trigger_id="loop_milestone",
            label="Loop",
            message=f"loop first iteration L{ctx.stop_event.line}",
        )
    if count % max(1, int(loop_every_n)) == 0:
        return TriggerMatch(
            trigger_id="loop_milestone",
            label="Loop",
            message=f"loop iteration #{count} at L{ctx.stop_event.line}",
        )
    return None


def _goto_jump(ctx: StoryStopContext) -> TriggerMatch | None:
    prev = ctx.previous_stop
    if prev is None:
        return None
    if os.path.abspath(prev.file_path or "") != os.path.abspath(ctx.stop_event.file_path or ""):
        return None
    if (prev.function or "") != (ctx.stop_event.function or ""):
        return None

    goto_match = _GOTO_RE.search(ctx.previous_line_text)
    if goto_match is None:
        return None

    label = goto_match.group(1)
    label_match = _LABEL_RE.search(ctx.line_text)
    arrived = label_match is not None and label_match.group(1) == label
    if arrived:
        detail = f"goto {label} -> label L{ctx.stop_event.line}"
    else:
        detail = f"goto {label} jump -> L{ctx.stop_event.line}"
    return TriggerMatch(
        trigger_id="goto_jump",
        label="Goto",
        message=detail,
    )


def _assert_line(ctx: StoryStopContext) -> TriggerMatch | None:
    if not _ASSERT_RE.search(ctx.line_text):
        return None
    return TriggerMatch(
        trigger_id="assert_line",
        label="Assert",
        message=f"assert/expect at L{ctx.stop_event.line}",
    )


def _return_statement(ctx: StoryStopContext) -> TriggerMatch | None:
    if not _RETURN_RE.search(ctx.line_text):
        return None
    return TriggerMatch(
        trigger_id="return_statement",
        label="Return",
        message=f"return at L{ctx.stop_event.line}",
    )


def _assert_failure(ctx: StoryStopContext) -> TriggerMatch | None:
    reason = ctx.stop_event.reason
    signal_name = (ctx.stop_event.signal_name or "").upper()

    if reason == "signal-received" and signal_name in _FAIL_SIGNALS:
        near_assert = _ASSERT_RE.search(ctx.line_text) or _ASSERT_RE.search(ctx.previous_line_text)
        if signal_name == "SIGABRT" or near_assert:
            return TriggerMatch(
                trigger_id="assert_failure",
                label="Fail",
                message=f"failure edge ({signal_name})",
            )
    return None


def _normalized_value(value: str) -> str:
    return value.strip().lower()


def _anomaly_keys(variables: list[tuple[str, str, str]]) -> set[str]:
    keys: set[str] = set()
    for var_tuple in variables:
        if len(var_tuple) >= 3:
            name, value, _type_hint = var_tuple
        else:
            name, value = var_tuple
        normalized = _normalized_value(value)
        if not normalized:
            continue
        if normalized in _ANOMALOUS_LITERAL_VALUES:
            keys.add(f"{name}:literal")
            continue
        if normalized.startswith("0x") and normalized in _ANOMALOUS_LITERAL_VALUES:
            keys.add(f"{name}:sentinel")
            continue
        if normalized.endswith("nan") or normalized.endswith("inf"):
            keys.add(f"{name}:float")
    return keys


def _anomaly(
    ctx: StoryStopContext,
    runtime_state: StoryFilterRuntimeState,
) -> TriggerMatch | None:
    current_keys = _anomaly_keys(ctx.variables)
    if not current_keys:
        runtime_state.last_anomaly_keys.clear()
        return None

    new_keys = current_keys - runtime_state.last_anomaly_keys
    runtime_state.last_anomaly_keys = current_keys
    if not new_keys:
        return None

    sample = sorted(new_keys)[0]
    return TriggerMatch(
        trigger_id="anomaly",
        label="Anomaly",
        message=f"value anomaly ({sample})",
    )


def _sync_event(ctx: StoryStopContext) -> TriggerMatch | None:
    match = _SYNC_RE.search(ctx.line_text)
    if match is None:
        return None
    api_name = match.group(1)
    return TriggerMatch(
        trigger_id="sync_event",
        label="Sync",
        message=f"sync event {api_name}()",
    )


def _is_standalone_expression_line(line_text: str) -> bool:
    """Check if a source line is a standalone expression statement."""
    if not line_text:
        return False
    line = line_text.split("//")[0].split("/*")[0]
    line = line.strip()
    if not line or not line.endswith(";"):
        return False
    body = line[:-1].strip()
    if not body:
        return False
    if not re.search(r"[A-Za-z_]\w*(?:\s*(?:->|\.|\[))?", body):
        return False
    if "=" in body:
        return False
    if re.search(r"[A-Za-z_]\w*\s*\(", body):
        return False
    if _CONTROL_FLOW_RE.search(line_text):
        return False
    if _STANDALONE_IGNORE_RE.search(body):
        return False
    if _ASSERT_RE.search(line_text):
        return False
    return True


def _standalone_expr(ctx: StoryStopContext) -> TriggerMatch | None:
    if not _is_standalone_expression_line(ctx.line_text):
        return None
    return TriggerMatch(
        trigger_id="standalone_expr",
        label="Expr",
        message=f"standalone expression at L{ctx.stop_event.line}",
    )


def _first_hit_function(
    ctx: StoryStopContext,
    runtime_state: StoryFilterRuntimeState,
) -> TriggerMatch | None:
    if not ctx.stop_event.file_path or not ctx.stop_event.function:
        return None
    key = (os.path.abspath(ctx.stop_event.file_path), ctx.stop_event.function)
    if key in runtime_state.seen_functions:
        return None
    runtime_state.seen_functions.add(key)
    return TriggerMatch(
        trigger_id="first_hit_function",
        label="First",
        message=f"first hit of {ctx.stop_event.function}()",
    )


def _first_hit_line(
    ctx: StoryStopContext,
    runtime_state: StoryFilterRuntimeState,
) -> TriggerMatch | None:
    if not ctx.stop_event.file_path or ctx.stop_event.line <= 0:
        return None
    key = (os.path.abspath(ctx.stop_event.file_path), int(ctx.stop_event.line))
    if key in runtime_state.seen_lines:
        return None
    runtime_state.seen_lines.add(key)
    return TriggerMatch(
        trigger_id="first_hit_line",
        label="First",
        message=f"first hit of L{ctx.stop_event.line}",
    )
