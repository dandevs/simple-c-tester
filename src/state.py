import asyncio

from models import Test, Suite, AppState

state = AppState()
dep_index: dict[str, list[Test]] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}
subprocess_columns = 80
