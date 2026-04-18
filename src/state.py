import asyncio

from models import Test, Suite, AppState

state = AppState()
dep_index: dict[str, list[Test]] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}
subprocess_columns = 80
dep_graph_ready = False
dep_graph_reason = "dependency graph not initialized"
