from pathlib import Path

from agent import AgentAction, AgentCheckpointStore, AgentState, DailyMarketAgent, FinishPolicy, RuleBasedAgentPlanner
from runtime.executor import ToolExecutor
from runtime.recovery import RecoveryPolicy


def _successful_adapters():
    def content(args):
        return {"required_sections": 15}

    return {
        "collect_news": lambda args: {"evidence": [{"source_id": "s1"}]},
        "collect_market_data": lambda args: {"market_data_complete": True},
        "crosscheck_market_quote": lambda args: {"market_data_complete": True, "conflicts": [], "missing_information": []},
        "generate_content": content,
        "validate_content_consistency": lambda args: {"schema_valid": True, "grounding_valid": True},
        "final_quality_gate": lambda args: {},
        "review_content": lambda args: {"review_approved": True, "review_feedback": [{"decision": "approve", "issues": []}]},
        "build_html_report": lambda args: {},
        "build_markdown_report": lambda args: {},
        "save_report": lambda args: {"report_generated": True},
    }


def _agent(adapters, tmp_path, *, max_steps=50, retry_budget=3):
    state = AgentState(goal="生成每日市场内容包", run_id="market_20260819_1300", edition="evening_premarket_watch", max_steps=max_steps, retry_budget=retry_budget)
    return DailyMarketAgent(
        RuleBasedAgentPlanner(),
        ToolExecutor(local_adapters=adapters),
        FinishPolicy(),
        AgentCheckpointStore(tmp_path / "agent_checkpoint.json"),
    ), state


def test_agent_normal_run(tmp_path: Path) -> None:
    agent, state = _agent(_successful_adapters(), tmp_path)
    result = agent.run(state.goal, state)
    assert result.status == "finished"
    assert result.final_result["delivered"] is False
    assert (tmp_path / "agent_checkpoint.json").exists()


def test_missing_market_data_replan_adds_crosscheck_action(tmp_path: Path) -> None:
    calls = {"market": 0, "crosscheck": 0}

    def market(args):
        calls["market"] += 1
        if calls["market"] == 1:
            return {"success": False, "missing_information": ["market_data:NDX"], "error_type": "market_data_incomplete"}
        return {"market_data_complete": True}

    adapters = _successful_adapters()
    adapters["collect_market_data"] = market
    adapters["crosscheck_market_quote"] = lambda args: (calls.__setitem__("crosscheck", calls["crosscheck"] + 1) or {"market_data_complete": True, "missing_information": [], "conflicts": []})
    agent, state = _agent(adapters, tmp_path)
    result = agent.run(state.goal, state)
    assert result.status == "finished"
    assert calls["crosscheck"] == 1
    assert any(item["action"]["tool_name"] == "crosscheck_market_quote" for item in result.tool_history)


def test_conflicting_quote_replan(tmp_path: Path) -> None:
    adapters = _successful_adapters()
    state = AgentState(goal="resolve quote", run_id="market_20260819_1301", conflicts=[{"field": "NDX", "severity": "high"}], max_steps=10)
    agent = DailyMarketAgent(RuleBasedAgentPlanner(), ToolExecutor(local_adapters=adapters), FinishPolicy(), AgentCheckpointStore(tmp_path / "checkpoint.json"))
    result = agent.run(state.goal, state)
    assert any(item["action"]["tool_name"] == "crosscheck_market_quote" for item in result.tool_history)


def test_provider_failure_fallback(tmp_path: Path) -> None:
    seen = []
    adapters = _successful_adapters()

    def content(args):
        seen.append(args.get("provider"))
        if len(seen) == 1:
            raise RuntimeError("provider unavailable")
        return {"required_sections": 15}

    adapters["generate_content"] = content
    agent, state = _agent(adapters, tmp_path)
    result = agent.run(state.goal, state)
    assert result.status == "finished"
    assert seen[1] == "gemini"


def test_json_failure_uses_deterministic_fallback(tmp_path: Path) -> None:
    seen = []
    adapters = _successful_adapters()

    def content(args):
        seen.append(args.get("provider"))
        if len(seen) == 1:
            raise ValueError("json parse failed")
        return {"required_sections": 15}

    adapters["generate_content"] = content
    agent, state = _agent(adapters, tmp_path)
    assert agent.run(state.goal, state).status == "finished"
    assert seen[1] == "rule_template"


def test_reviewer_reject_repair(tmp_path: Path) -> None:
    adapters = _successful_adapters()
    seen = {"review": 0}

    def review(args):
        seen["review"] += 1
        if seen["review"] == 1:
            return {"review_feedback": [{"decision": "reject", "issues": [{"severity": "low"}], "recommended_actions": [{"tool": "repair_section", "arguments": {"section": "macro"}}]}]}
        return {"review_approved": True, "review_feedback": [{"decision": "approve", "issues": []}]}

    adapters["review_content"] = review
    adapters["repair_section"] = lambda args: {"review_feedback": []}
    agent, state = _agent(adapters, tmp_path)
    result = agent.run(state.goal, state)
    assert result.status == "finished"
    assert seen["review"] == 2
    assert any(item["action"]["tool_name"] == "repair_section" for item in result.tool_history)


def test_high_severity_issue_blocks_finish(tmp_path: Path) -> None:
    state = AgentState(goal="blocked", review_feedback=[{"decision": "reject", "issues": [{"severity": "high"}]}])
    result = FinishPolicy().evaluate(state)
    assert result.finished is False
    assert "high_severity_issues" in result.missing_conditions


def test_max_steps_stops_agent(tmp_path: Path) -> None:
    agent, state = _agent({"collect_news": lambda args: {}}, tmp_path, max_steps=1)
    result = agent.run(state.goal, state)
    assert result.status == "blocked"
    assert result.failure_reason == "max_steps_exceeded"


def test_retry_budget_stops_agent(tmp_path: Path) -> None:
    agent, state = _agent({"collect_news": lambda args: (_ for _ in ()).throw(RuntimeError("temporary"))}, tmp_path, retry_budget=1)
    result = agent.run(state.goal, state)
    assert result.status == "blocked"
    assert result.failure_reason == "retry_budget_exceeded"


def test_checkpoint_resume(tmp_path: Path) -> None:
    store = AgentCheckpointStore(tmp_path / "resume.json")
    state = AgentState(goal="resume", run_id="market_20260819_1302", step_count=4, status="running")
    store.save(state)
    restored = store.load()
    assert restored.run_id == state.run_id
    assert restored.step_count == 4
    assert restored.state_hash == state.state_hash


def test_external_publish_still_blocked_and_media_policy_removed(tmp_path: Path) -> None:
    executor = ToolExecutor(local_adapters={"deliver": lambda args: {"sent": True}})
    blocked = executor.execute(AgentAction(action_id="deliver_1", tool_name="deliver"))
    assert blocked["success"] is False
    assert blocked["error_type"] == "tool_blocked"
    policy = RecoveryPolicy.from_config(Path("config/self_healing_policy.json"))
    assert policy.allow_external_publish is False
