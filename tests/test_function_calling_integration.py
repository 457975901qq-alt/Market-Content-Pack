from __future__ import annotations

from pathlib import Path

from execution_planner import ExecutionPlanner
from function_calling.business_bindings import BusinessContext, build_business_bindings
from function_calling.function_executor import FunctionExecutor
from function_calling.registry import build_registry
from function_calling.tool_call import FunctionCall, FunctionError, FunctionStatus
from tool_router import ToolRouter


def _args(tool: str, run_id: str = "market_20260720_1400") -> dict[str, object]:
    common = {"run_id": run_id, "edition": "evening_premarket_watch"}
    return {
        "collect_market_data": {**common, "symbols": ["SPX"]},
        "collect_news": {**common, "sources": ["rss"]},
        "extract_web_content": {**common, "urls": ["https://example.test/source"]},
        "generate_content": {**common, "input_path": "/tmp/input.json", "provider": "rule_template"},
        "validate_market_data": {**common, "market_data_path": "/tmp/quotes.json"},
        "validate_content_consistency": {**common, "content_path": "/tmp/content.json", "source_path": "/tmp/source.json"},
        "final_quality_gate": {**common, "validation_paths": ["/tmp/qa.json"]},
    }[tool]


def test_quality_gate_does_not_require_image_artifacts(tmp_path: Path) -> None:
    from function_calling.business_bindings import _final_quality_gate
    from function_calling.arguments import FinalQualityGateArgs

    content_dir = tmp_path / "content"
    content = content_dir / "market_content.json"
    qa = tmp_path / "qa.json"
    content_dir.mkdir()
    content.write_text('{"edition":"evening_premarket_watch"}', encoding="utf-8")
    qa.write_text('{"status":"pass","mode":"text"}', encoding="utf-8")
    context = BusinessContext(
        "market_20260720_1402",
        "evening_premarket_watch",
        {"content": content_dir, "market_quotes": tmp_path / "quotes", "logs": tmp_path / "logs"},
        {},
        "rule_template",
    )
    result = _final_quality_gate(context, FinalQualityGateArgs(
        run_id=context.run_id,
        edition=context.edition,
        validation_paths=[str(qa)],
    ))
    assert result["status"] == "pass"


def test_final_validation_does_not_require_image_artifacts(tmp_path: Path) -> None:
    from build_daily_market_pack import _final_validation_result

    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / "market_content.json").write_text('{"edition":"evening_premarket_watch"}', encoding="utf-8")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "qa_report.json").write_text('{"status":"pass"}', encoding="utf-8")
    paths = {"content": content_dir, "logs": logs_dir}

    assert _final_validation_result(paths, True) == (True, "text content and text QA passed")
    assert _final_validation_result(paths, False)[0] is False


def test_planner_function_call_executor_full_mock_chain() -> None:
    router = ToolRouter({"services": {"ollama": {"status": "unhealthy"}, "gemini": {"status": "unavailable"}}})
    plan = ExecutionPlanner(router).build(
        run_id="market_20260720_1400",
        edition="evening_premarket_watch",
        state={"steps": {}},
        preferred_provider="auto",
    )
    calls: list[str] = []

    def binding(args):
        calls.append(type(args).__name__)
        return {"status": "success"}

    registry = build_registry({name: binding for name in (
        "collect_market_data", "validate_market_data", "generate_content",
        "validate_content_consistency", "final_quality_gate",
    )})
    executor = FunctionExecutor(registry)
    chain = ["collect_market_data", "validate_market_data", "generate_content", "validate_content_consistency", "final_quality_gate"]
    results = [executor.execute(FunctionCall(call_id=f"call_{index}", tool_name=tool, step=tool, arguments=_args(tool))) for index, tool in enumerate(chain)]
    assert all(result.status is FunctionStatus.success for result in results)
    assert len(calls) == len(chain)
    assert plan["selected_provider"] == "rule_template"
    assert next(item for item in plan["steps"] if item["step"] == "final_quality_gate")["mandatory"] is True


def test_executor_state_events_and_recovery_retry_current_call() -> None:
    events: list[tuple[str, str]] = []
    attempts = {"count": 0}

    def flaky(_args):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary timeout")
        return {"status": "success", "attempt": attempts["count"]}

    def recovery(call, error: FunctionError):
        assert call.tool_name == "collect_news"
        assert error.error_code == "execution_error"
        return {"result": {"status": "repair_succeeded", "repair_action_succeeded": True}}

    def hook(call, status, _result):
        events.append((call.tool_name, status))

    executor = FunctionExecutor(
        build_registry({"collect_news": flaky}),
        recovery_handler=recovery,
        state_hook=hook,
        max_calls=4,
        max_calls_per_step=3,
    )
    result = executor.execute(FunctionCall(call_id="recover_001", tool_name="collect_news", step="collect_news", arguments=_args("collect_news")))
    assert result.status is FunctionStatus.success
    assert attempts["count"] == 2
    assert events == [("collect_news", "running"), ("collect_news", "success")]
    assert result.data["recovery"]["result"]["status"] == "repair_succeeded"


def test_recovery_retries_with_router_selected_provider_arguments() -> None:
    providers: list[str] = []

    def flaky(args):
        providers.append(args.provider)
        if len(providers) == 1:
            raise RuntimeError("provider HTTP 503")
        return {"status": "success", "provider": args.provider}

    original = _args("generate_content")

    def recovery(call, _error: FunctionError):
        assert call.tool_name == "generate_content"
        return {
            "result": {
                "status": "repair_succeeded",
                "repair_action_succeeded": True,
                "retry_arguments": {**original, "provider": "rule_template"},
            }
        }

    executor = FunctionExecutor(
        build_registry({"generate_content": flaky}),
        recovery_handler=recovery,
        max_calls=4,
        max_calls_per_step=3,
    )
    result = executor.execute(FunctionCall(
        call_id="provider_switch_001",
        tool_name="generate_content",
        step="generate_content",
        arguments={**original, "provider": "gemini"},
    ))

    assert result.status is FunctionStatus.success
    assert providers == ["gemini", "rule_template"]


def test_real_registry_uses_fixed_business_bindings(tmp_path: Path) -> None:
    paths = {
        "content": tmp_path / "content",
        "sources": tmp_path / "sources",
        "market_quotes": tmp_path / "sources" / "market_quotes.json",
        "logs": tmp_path / "logs",
    }
    context = BusinessContext("market_20260720_1401", "morning_close_review", paths, {}, "rule_template")
    bindings = build_business_bindings(context)
    registry = build_registry(bindings)
    assert registry["collect_market_data"].callable.__name__ == "<lambda>"
    assert registry["generate_content"].result_model.__name__ == "FunctionResult"
