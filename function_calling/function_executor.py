from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import ValidationError

from .registry import RegisteredFunction, build_registry
from .tool_call import FunctionCall, FunctionError, FunctionResult, FunctionStatus


class FunctionExecutor:
    """Execute only fixed registry bindings; no eval, shell, or dynamic imports."""

    def __init__(
        self,
        registry: dict[str, RegisteredFunction] | None = None,
        max_calls: int = 30,
        max_calls_per_step: int = 5,
        recovery_handler: Callable[[FunctionCall, FunctionError], dict[str, Any] | None] | None = None,
        state_hook: Callable[[FunctionCall, str, FunctionResult | None], None] | None = None,
        blocked_tools: set[str] | None = None,
        allowed_steps: set[str] | None = None,
    ) -> None:
        self.registry = registry or build_registry()
        self.max_calls = max_calls
        self.max_calls_per_step = max_calls_per_step
        self._calls = 0
        self._by_step: dict[str, int] = {}
        self._completed_ids: set[str] = set()
        self.recovery_handler = recovery_handler
        self.state_hook = state_hook
        self.blocked_tools = blocked_tools or set()
        self.allowed_steps = allowed_steps

    @staticmethod
    def parse(raw: str | dict[str, Any]) -> list[FunctionCall]:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            calls = data.get("calls") if isinstance(data, dict) else None
            if not isinstance(calls, list):
                raise ValueError("expected object with calls list")
            return [FunctionCall.model_validate(item) for item in calls]
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            raise ValueError(f"schema_error:{exc}") from exc

    def execute(self, call: FunctionCall, timeout_seconds: float | None = None) -> FunctionResult:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        if call.call_id in self._completed_ids:
            return self._rejected(call, "duplicate_call_id", "call_id has already been executed", started, t0)
        binding = self.registry.get(call.tool_name)
        if binding is None:
            return self._rejected(call, "unknown_tool", "tool is not registered", started, t0)
        if call.tool_name in self.blocked_tools:
            return self._rejected(call, "tool_blocked", "tool is blocked by policy", started, t0)
        if self.allowed_steps is not None and call.step not in self.allowed_steps:
            return self._rejected(call, "step_not_allowed", "step is not allowed by Function Calling policy", started, t0)
        if not binding.schema.enabled:
            return self._rejected(call, "tool_disabled", "registered tool is disabled", started, t0)
        if call.step not in binding.schema.supported_steps:
            return self._rejected(call, "step_not_allowed", "tool is not allowed in this step", started, t0)
        if self._calls >= self.max_calls:
            return self._rejected(call, "max_calls_exceeded", "run call limit reached", started, t0)
        if self._by_step.get(call.step, 0) >= self.max_calls_per_step:
            return self._rejected(call, "max_calls_per_step_exceeded", "step call limit reached", started, t0)
        try:
            args = binding.argument_model.model_validate(call.arguments)
        except ValidationError as exc:
            return self._rejected(call, "invalid_arguments", str(exc), started, t0)
        limit = timeout_seconds or binding.schema.timeout_seconds
        if self.state_hook is not None:
            self.state_hook(call, FunctionStatus.running.value, None)
        result = self._invoke(call, binding, args, limit, started, t0, allow_recovery=True)
        if self.state_hook is not None:
            self.state_hook(call, result.status.value, result)
        return result

    def _invoke(self, call: FunctionCall, binding: RegisteredFunction, args: Any, limit: float, started: datetime, t0: float, *, allow_recovery: bool) -> FunctionResult:
        self._calls += 1
        self._by_step[call.step] = self._by_step.get(call.step, 0) + 1
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(binding.callable, args)
            data = future.result(timeout=limit)
            pool.shutdown(wait=True)
            self._completed_ids.add(call.call_id)
            return FunctionResult(call_id=call.call_id, tool_name=call.tool_name, status=FunctionStatus.success, data=data, started_at=started, completed_at=datetime.now(timezone.utc), duration_ms=int((time.monotonic() - t0) * 1000))
        except FutureTimeout:
            future.cancel()
            # Do not use the executor context manager here: its implicit
            # shutdown(wait=True) defeats the timeout contract for a stuck
            # model or network call. The isolated worker may finish later, but
            # this run is released to the recovery controller immediately.
            pool.shutdown(wait=False, cancel_futures=True)
            failed = self._failed(call, FunctionStatus.timeout, "timeout", "tool execution exceeded timeout", started, t0, True)
        except Exception as exc:
            pool.shutdown(wait=False, cancel_futures=True)
            error_type = str(getattr(exc, "error_type", "code_error"))
            error_code = str(getattr(exc, "error_code", "execution_error"))
            retryable = bool(getattr(exc, "retryable", False))
            failed = self._failed(call, FunctionStatus.failed, error_code, str(exc), started, t0, retryable, error_type=error_type)

        if not allow_recovery or self.recovery_handler is None or self._calls >= self.max_calls or self._by_step.get(call.step, 0) >= self.max_calls_per_step:
            return failed
        if failed.error is None:
            return failed
        try:
            recovery = self.recovery_handler(call, failed.error)
        except Exception as exc:  # recovery must not hide the original failure
            recovery = {"status": "repair_failed", "error_type": type(exc).__name__, "message": str(exc)[:500]}
        if not self._recovery_succeeded(recovery):
            return failed.model_copy(update={"data": {"recovery": recovery}})
        # A recovery controller may have selected a new provider or otherwise
        # produced a validated replacement argument set. Re-validate that
        # payload through the registry model before retrying; never trust a
        # model-supplied callable or bypass the function argument contract.
        retry_args = args
        recovery_payload = recovery.get("result") if isinstance(recovery.get("result"), dict) else recovery
        retry_arguments = recovery_payload.get("retry_arguments") if isinstance(recovery_payload, dict) else None
        if isinstance(retry_arguments, dict):
            try:
                retry_args = binding.argument_model.model_validate(retry_arguments)
            except ValidationError as exc:
                return failed.model_copy(update={"data": {"recovery": recovery, "retry_argument_error": str(exc)}})
        retried = self._invoke(call, binding, retry_args, limit, started, t0, allow_recovery=False)
        return retried.model_copy(update={"data": {**retried.data, "recovery": recovery}})

    @staticmethod
    def _recovery_succeeded(value: dict[str, Any] | None) -> bool:
        if not isinstance(value, dict):
            return False
        result = value.get("result") if isinstance(value.get("result"), dict) else value
        return result.get("status") == "repair_succeeded" and bool(result.get("repair_action_succeeded", True))

    def _rejected(self, call: FunctionCall, code: str, message: str, started: datetime, t0: float) -> FunctionResult:
        return FunctionResult(call_id=call.call_id, tool_name=call.tool_name, status=FunctionStatus.rejected, error=FunctionError(error_type="configuration_error", error_code=code, message=message, retryable=False), started_at=started, completed_at=datetime.now(timezone.utc), duration_ms=int((time.monotonic() - t0) * 1000))

    def _failed(self, call: FunctionCall, status: FunctionStatus, code: str, message: str, started: datetime, t0: float, retryable: bool, error_type: str | None = None) -> FunctionResult:
        return FunctionResult(call_id=call.call_id, tool_name=call.tool_name, status=status, error=FunctionError(error_type=error_type or ("transient_error" if retryable else "code_error"), error_code=code, message=message[:1000], retryable=retryable, remediation_step=call.step), started_at=started, completed_at=datetime.now(timezone.utc), duration_ms=int((time.monotonic() - t0) * 1000))
