from state import state
from models import TestState


def all_tests_finished() -> bool:
    done_states = {TestState.PASSED, TestState.FAILED}
    return all(test.state in done_states for test in state.all_tests)


def has_active_tests() -> bool:
    active_states = {TestState.PENDING, TestState.RUNNING, TestState.CANCELLED}
    return any(test.state in active_states for test in state.all_tests)


def display_state_signature() -> tuple:
    return tuple(
        (
            test.name,
            test.state,
            test.time_start,
            test.time_state_changed,
            test.stdout,
            test.stderr,
            test.compile_err,
            len(test.timeline_events),
            test.debug_running,
            test.debug_exited,
            test.debug_exit_code,
        )
        for test in state.all_tests
    )
