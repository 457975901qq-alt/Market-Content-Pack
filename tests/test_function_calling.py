from __future__ import annotations

import time

from function_calling.function_executor import FunctionExecutor
from function_calling.registry import build_registry
from function_calling.tool_call import FunctionCall, FunctionStatus


def call(tool: str, **arguments: object) -> FunctionCall:
    return FunctionCall.model_validate({"call_id": f"call_{tool}", "tool_name": tool, "step": tool, "arguments": arguments})


def base() -> dict[str, object]:
    return {"run_id": "market_20260719_1200", "edition": "morning_close_review"}


def test_registered_function_validates_and_executes() -> None:
    executor = FunctionExecutor()
    result = executor.execute(call("collect_market_data", **base(), symbols=["SPX"]))
    assert result.status is FunctionStatus.success


def test_unknown_publish_function_and_extra_arguments_are_rejected() -> None:
    executor = FunctionExecutor()
    unknown = executor.execute(call("deliver", **base()))
    assert unknown.status is FunctionStatus.rejected
    extra = executor.execute(call("collect_news", **base(), sources=["rss"], unexpected=True))
    assert extra.status is FunctionStatus.rejected
    assert extra.error and extra.error.error_code == "invalid_arguments"


def test_duplicate_call_id_is_rejected() -> None:
    executor = FunctionExecutor()
    first = executor.execute(call("collect_news", **base(), sources=["rss"]))
    second = executor.execute(call("collect_news", **base(), sources=["rss"]))
    assert first.status is FunctionStatus.success
    assert second.status is FunctionStatus.rejected
    assert second.error and second.error.error_code == "duplicate_call_id"


def test_timeout_is_normalized() -> None:
    def slow(_: object) -> dict[str, object]:
        time.sleep(0.05)
        return {}

    registry = build_registry({"collect_news": slow})
    result = FunctionExecutor(registry).execute(call("collect_news", **base(), sources=["rss"]), timeout_seconds=0.001)
    assert result.status is FunctionStatus.timeout


def test_unknown_publish_function_and_unknown_step_are_rejected() -> None:
    executor = FunctionExecutor(allowed_steps={"collect_news"})
    delivery = executor.execute(FunctionCall.model_validate({"call_id": "blocked_1", "tool_name": "publish", "step": "collect_news", "arguments": base()}))
    unknown_step = executor.execute(FunctionCall.model_validate({"call_id": "blocked_2", "tool_name": "collect_news", "step": "shell", "arguments": {**base(), "sources": ["rss"]}}))
    assert delivery.status is FunctionStatus.rejected
    assert delivery.error and delivery.error.error_code == "unknown_tool"
    assert unknown_step.status is FunctionStatus.rejected
    assert unknown_step.error and unknown_step.error.error_code == "step_not_allowed"
