from state import state
from models import TestState


def all_tests_finished() -> bool:
    done_states = {TestState.PASSED, TestState.FAILED}
    return all(test.state in done_states for test in state.all_tests)


def has_active_tests() -> bool:
    active_states = {TestState.PENDING, TestState.RUNNING, TestState.CANCELLED}
    return any(test.state in active_states for test in state.all_tests)


def display_state_signature() -> tuple:
    sigs = []
    for test in state.all_tests:
        run = test.current_run
        sigs.append(
            (
                test.name,
                test.state,
                test.time_start,
                test.time_state_changed,
                run.stdout if run is not None else "",
                run.stderr if run is not None else "",
                run.compile_err if run is not None else "",
                len(run.timeline_events) if run is not None else 0,
                run.debug_running if run is not None else False,
                run.debug_exited if run is not None else False,
                run.debug_exit_code if run is not None else None,
            )
        )
    return tuple(sigs)
