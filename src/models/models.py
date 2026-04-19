from dataclasses import dataclass, field
from pathlib import Path
from .enum import TestState


@dataclass
class TimelineEvent:
    index: int = 0
    timestamp: float = 0.0
    kind: str = ""
    message: str = ""
    file_path: str = ""
    line: int = 0
    function: str = ""
    stream: str = ""


@dataclass
class Test:
    name: str = ""
    time_start: float = 0.0
    time_state_changed: float = 0.0
    state: TestState = TestState.PENDING
    qeueued: bool = False
    dependencies: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    source_path: str = ""
    stdout: str = ""
    stdout_raw: bytes = b""
    stderr: str = ""
    compile_err: str = ""
    compile_err_raw: bytes = b""
    stderr_raw: bytes = b""
    timeline_events: list[TimelineEvent] = field(default_factory=list)
    timeline_capture_enabled: bool = False
    debug_logs: list[str] = field(default_factory=list)
    debug_running: bool = False
    debug_exited: bool = False
    debug_exit_code: int | None = None


@dataclass
class Suite:
    name: str = ""
    tests: list[Test] = field(default_factory=list)
    children: list["Suite"] = field(default_factory=list)


@dataclass
class AppState:
    root_suite: Suite = field(default_factory=lambda: Suite(name="root"))
    all_suites: list[Suite] = field(default_factory=list)
    all_tests: list[Test] = field(default_factory=list)
    available_runners = 0

    def populate_suites(self, path: str) -> None:
        root = Path(path)
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                self.root_suite.children.append(self._build_suite(entry, path))
            elif entry.suffix == ".c":
                test = Test(name=entry.stem, source_path=str(entry))
                self.root_suite.tests.append(test)
                self.all_tests.append(test)

    def _build_suite(self, dir_path: Path, base_path: str) -> Suite:
        suite = Suite(name=dir_path.name)
        self.all_suites.append(suite)
        for entry in sorted(dir_path.iterdir()):
            if entry.is_dir():
                suite.children.append(self._build_suite(entry, base_path))
            elif entry.suffix == ".c":
                test = Test(name=entry.stem, source_path=str(entry))
                suite.tests.append(test)
                self.all_tests.append(test)
        return suite
