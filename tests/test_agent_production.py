from __future__ import annotations

from types import SimpleNamespace

from agent import AgentAction, AgentState, AgentCheckpointStore, DailyMarketAgent, FinishPolicy, RuleBasedAgentPlanner
from agent.production import ProductionToolExecutor, _count_content_sections, build_production_bindings
from function_calling.business_bindings import BusinessContext
from build_daily_market_pack import _agent_runtime_state, execute


def _defaults() -> dict[str, object]:
    return {
        "run_id": "market_20260819_2300",
        "edition": "evening_premarket_watch",
        "provider": "rule_template",
        "symbols": ["SPX", "NDX", "DJI"],
        "sources": ["rss"],
        "source_path": "/tmp/source.json",
        "market_data_path": "/tmp/market.json",
        "content_path": "/tmp/content.json",
        "qa_path": "/tmp/qa.json",
        "report_path": "/tmp/report.json",
    }


def _bindings() -> dict[str, object]:
    return {
        "collect_news": lambda args: {"status": "success", "evidence": [{"source_id": "s1"}]},
        "collect_market_data": lambda args: {"status": "success", "market_data_version": "v1", "market_data_complete": True},
        "validate_market_data": lambda args: {"status": "pass", "market_data_complete": True},
        "crosscheck_market_quote": lambda args: {"status": "success", "market_data_complete": True, "crosschecked_ticker": args.ticker, "missing_information": [], "conflicts": []},
        "generate_content": lambda args: {"status": "success", "required_sections": 15, "market_data_version": "v1"},
        "validate_content_consistency": lambda args: {"status": "pass", "schema_valid": True, "grounding_valid": True},
        "final_quality_gate": lambda args: {"status": "pass", "schema_valid": True, "grounding_valid": True},
        "review_content": lambda args: {"status": "success", "review_approved": True, "review_feedback": [{"decision": "approve", "issues": []}]},
        "reviewer_gate": lambda args: {"status": "pass", "review_approved": True, "review_feedback": [{"decision": "approve", "issues": []}]},
        "build_html_report": lambda args: {"status": "success"},
        "build_markdown_report": lambda args: {"status": "success"},
        "save_report": lambda args: {"status": "success", "report_generated": True},
    }


def test_production_executor_runs_dynamic_action_and_normalizes_observation() -> None:
    executor = ProductionToolExecutor(_bindings(), defaults=_defaults())
    result = executor.execute(AgentAction(action_id="crosscheck_1", tool_name="crosscheck_market_quote", arguments={"ticker": "SPX"}))
    assert result["success"] is True
    assert result["result"]["crosschecked_ticker"] == "SPX"
    assert result["tool_name"] == "crosscheck_market_quote"


def test_production_agent_normal_path_is_agent_controlled(tmp_path) -> None:
    executor = ProductionToolExecutor(_bindings(), defaults=_defaults())
    state = AgentState(goal="生成每日市场内容包", run_id="market_20260819_2301", edition="evening_premarket_watch")
    state.require_market_validation = True
    state.require_reviewer_gate = True
    agent = DailyMarketAgent(RuleBasedAgentPlanner(provider="rule_template"), executor, FinishPolicy(), AgentCheckpointStore(tmp_path / "agent_checkpoint.json"))
    result = agent.run(state.goal, state)
    assert result.status == "finished"
    assert [item["action"]["tool_name"] for item in result.tool_history] == [
        "collect_news", "collect_market_data", "validate_market_data", "generate_content",
        "validate_content_consistency", "final_quality_gate", "review_content", "reviewer_gate",
        "build_html_report", "build_markdown_report", "save_report",
    ]


def test_production_unknown_missing_changes_runtime_action(tmp_path) -> None:
    bindings = _bindings()
    calls = {"market": 0}

    def market(args):
        calls["market"] += 1
        if calls["market"] == 1:
            return {"status": "failed", "missing_information": ["market_data:DJI"]}
        return {"status": "success", "market_data_version": "v2", "market_data_complete": True}

    bindings["collect_market_data"] = market
    executor = ProductionToolExecutor(bindings, defaults=_defaults())
    state = AgentState(goal="生成每日市场内容包", run_id="market_20260819_2302", edition="evening_premarket_watch")
    state.require_market_validation = True
    state.require_reviewer_gate = True
    agent = DailyMarketAgent(RuleBasedAgentPlanner(provider="rule_template"), executor, FinishPolicy(), AgentCheckpointStore(tmp_path / "agent_checkpoint.json"))
    result = agent.run(state.goal, state)
    names = [item["action"]["tool_name"] for item in result.tool_history]
    assert "crosscheck_market_quote" in names
    assert names.index("crosscheck_market_quote") < names.index("generate_content")


def test_production_runtime_conflict_replans_to_crosscheck(tmp_path) -> None:
    executor = ProductionToolExecutor(_bindings(), defaults=_defaults())
    state = AgentState(
        goal="生成每日市场内容包",
        run_id="market_20260819_2303",
        edition="evening_premarket_watch",
        conflicts=[{"field": "SPX", "severity": "high"}],
    )
    state.require_market_validation = True
    state.require_reviewer_gate = True
    agent = DailyMarketAgent(RuleBasedAgentPlanner(provider="rule_template"), executor, FinishPolicy(), AgentCheckpointStore(tmp_path / "agent_checkpoint.json"))
    result = agent.run(state.goal, state)
    assert result.status == "finished"
    assert result.tool_history[0]["action"]["tool_name"] == "crosscheck_market_quote"


def test_same_goal_different_observation_selects_different_action() -> None:
    planner = RuleBasedAgentPlanner(provider="rule_template")
    complete = AgentState(goal="生成每日市场内容包", completed_actions=[{"tool_name": "collect_news"}])
    missing = AgentState(
        goal="生成每日市场内容包",
        completed_actions=[{"tool_name": "collect_news"}, {"tool_name": "collect_market_data"}],
        missing_information=["market_data:DJI"],
        tool_history=[{"action": {"tool_name": "collect_market_data"}, "observation": {"success": False}}],
    )
    assert planner.next_action(complete).tool_name == "collect_market_data"
    assert planner.next_action(missing).tool_name == "crosscheck_market_quote"


def test_production_checkpoint_restores_agent_observation_state(tmp_path) -> None:
    state = AgentState(
        goal="生成每日市场内容包",
        run_id="market_20260819_2304",
        missing_information=["market_data:SPX"],
        conflicts=[{"field": "SPX", "severity": "high"}],
        available_evidence=[{"source_id": "s1"}],
        review_feedback=[{"decision": "needs_revision", "section": "macro"}],
        tool_history=[{"action": {"tool_name": "collect_market_data"}, "observation": {"success": False}}],
    )
    store = AgentCheckpointStore(tmp_path / "agent_checkpoint.json")
    store.save(state)
    restored = store.load()
    assert restored.missing_information == ["market_data:SPX"]
    assert restored.conflicts[0]["field"] == "SPX"
    assert restored.available_evidence == [{"source_id": "s1"}]
    assert restored.review_feedback[0]["decision"] == "needs_revision"


def test_new_run_does_not_restore_checkpoint_from_another_run(tmp_path) -> None:
    old_state = AgentState(
        goal="生成每日市场内容包",
        run_id="market_20260819_2250_real",
        edition="evening_premarket_watch",
        failure={"failure_category": "market_data_future"},
        failure_reason="non_retryable_failure:market_data_future",
    )
    AgentCheckpointStore(tmp_path / "agent_checkpoint.json").save(old_state)

    current = _agent_runtime_state(
        {"run_id": "market_20260820_0631_new", "edition": "morning_close_review", "steps": {}},
        tmp_path,
    )

    assert current.run_id == "market_20260820_0631_new"
    assert current.edition == "morning_close_review"
    assert current.failure is None
    assert current.failure_reason is None


def test_production_section_count_uses_daily_modules() -> None:
    content = {"daily_sections": [{"section_id": str(index)} for index in range(15)], "analysis_text": {"sections": [{"heading": "summary"}]}}
    assert _count_content_sections(content) == 15


def test_production_market_action_passes_cutoff_to_existing_market_tool(monkeypatch, tmp_path) -> None:
    captured: list[dict[str, object]] = []

    def fake_collect(edition: str, *, symbols: list[str], as_of=None, **kwargs):
        captured.append({"edition": edition, "symbols": symbols, "as_of": as_of})
        return {
            "status": "success",
            "market_data_version": "asof-v1",
            "quotes": [],
            "required_symbols": [],
        }

    monkeypatch.setattr("market_quotes.collect_quotes", fake_collect)
    paths = {
        "content": tmp_path / "content",
        "sources": tmp_path / "sources",
        "market_quotes": tmp_path / "market_quotes.json",
        "review": tmp_path / "review",
    }
    context = BusinessContext(
        run_id="market_20260820_0800",
        edition="evening_premarket_watch",
        paths=paths,
        environment={},
        provider="rule_template",
    )
    bindings = build_production_bindings(context)
    cutoff = "2026-08-20T17:30:00+09:00"
    executor = ProductionToolExecutor(
        bindings,
        defaults={**_defaults(), "run_id": context.run_id, "edition": context.edition, "cutoff_at": cutoff, "symbols": ["SPX"]},
    )
    result = executor.execute(AgentAction(action_id="quote_asof", tool_name="get_market_quote", arguments={"symbol": "SPX"}))
    assert result["success"] is True
    assert captured[0]["as_of"].isoformat() == "2026-08-20T08:30:00+00:00"
    assert result["result"]["requested_as_of"] == "2026-08-20T08:30:00+00:00"


def test_production_executor_denies_image_action() -> None:
    executor = ProductionToolExecutor(_bindings(), defaults=_defaults())
    result = executor.execute(AgentAction(action_id="image_1", tool_name="generate_images"))
    assert result["success"] is False
    assert result["error_type"] == "tool_blocked"


def test_production_executor_routes_failures_to_bounded_recovery() -> None:
    bindings = _bindings()
    attempts = {"count": 0}

    def flaky(args):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary provider failure")
        return {"status": "success", "required_sections": 15, "market_data_version": "v1"}

    bindings["generate_content"] = flaky
    recovery_calls = []

    def recover(call, error):
        recovery_calls.append(call.tool_name)
        return {"result": {"status": "repair_succeeded", "repair_action_succeeded": True}}

    executor = ProductionToolExecutor(bindings, defaults=_defaults(), recovery_handler=recover)
    result = executor.execute(AgentAction(action_id="generate_1", tool_name="generate_content"))
    assert result["success"] is True
    assert recovery_calls == ["generate_content"]
    assert attempts["count"] == 2


def test_production_entry_delegates_to_agent_runner(monkeypatch) -> None:
    monkeypatch.setattr("build_daily_market_pack._execute_agent", lambda args: 17)
    monkeypatch.setattr("build_daily_market_pack._legacy_execute", lambda args: (_ for _ in ()).throw(AssertionError("legacy controller called")))
    assert execute(SimpleNamespace()) == 17


def test_production_executor_denies_publish_action() -> None:
    executor = ProductionToolExecutor(_bindings(), defaults=_defaults())
    result = executor.execute(AgentAction(action_id="publish_1", tool_name="publish_content"))
    assert result["success"] is False
    assert result["error_type"] == "tool_blocked"
