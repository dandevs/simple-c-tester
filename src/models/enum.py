from enum import Enum


class TestState(Enum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    CANCELLED = "cancelled"