from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Callable

from function_calling.function_executor import FunctionExecutor
from function_calling.tool_call import FunctionCall, FunctionStatus

from agent.action import AgentAction


class ToolExecutor:
    """Adapter over the existing FunctionExecutor and fixed local adapters."""

    BLOCKED = {"deliver", "canary_deliver", "shell", "exec_shell"}

    def __init__(self, function_executor: FunctionExecutor | None = None, *, local_adapters: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None, blocked_tools: set[str] | None = None, timeout_seconds: float = 120) -> None:
        self.function_executor = function_executor
        self.local_adapters = local_adapters or {}
        self.blocked_tools = self.BLOCKED | (blocked_tools or set())
        self.timeout_seconds = timeout_seconds

    def execute(self, action: AgentAction) -> dict[str, Any]:
        if action.tool_name in self.blocked_tools:
            return {"success": False, "tool_name": action.tool_name, "error_type": "tool_blocked", "error": "tool is blocked by policy"}
        if self.function_executor is not None:
            call = FunctionCall(
                call_id=action.action_id,
                tool_name=action.tool_name,
                arguments=action.arguments,
                requested_by="agent_planner",
                step=action.tool_name,
            )
            result = self.function_executor.execute(call, timeout_seconds=self.timeout_seconds)
            if result.status is FunctionStatus.success:
                return {"success": True, "tool_name": action.tool_name, "result": result.data, "duration_ms": result.duration_ms}
            return {"success": False, "tool_name": action.tool_name, "error_type": result.error.error_type if result.error else "tool_failed", "error": result.error.message if result.error else "tool failed", "error_code": result.error.error_code if result.error else "tool_failed"}
        adapter = self.local_adapters.get(action.tool_name)
        if adapter is None:
            return {"success": False, "tool_name": action.tool_name, "error_type": "tool_not_found", "error": "tool is not registered"}
        started = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            result = pool.submit(adapter, dict(action.arguments)).result(timeout=self.timeout_seconds)
            pool.shutdown(wait=True)
            if isinstance(result, dict) and result.get("success") is False:
                return {**result, "tool_name": action.tool_name, "duration_ms": int((time.monotonic() - started) * 1000)}
            return {"success": True, "tool_name": action.tool_name, "result": result, "duration_ms": int((time.monotonic() - started) * 1000)}
        except FutureTimeout:
            pool.shutdown(wait=False, cancel_futures=True)
            return {"success": False, "tool_name": action.tool_name, "error_type": "timeout", "error": "tool execution timed out"}
        except Exception as exc:
            pool.shutdown(wait=False, cancel_futures=True)
            return {"success": False, "tool_name": action.tool_name, "error_type": type(exc).__name__, "error": str(exc)[:1000]}
