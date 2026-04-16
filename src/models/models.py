from dataclasses import dataclass
from .enum import TestState


@dataclass
class Test:
    name: str
    time_start: float
    state = TestState.PENDING

@dataclass
class Suite:
    tests: list[Test]
