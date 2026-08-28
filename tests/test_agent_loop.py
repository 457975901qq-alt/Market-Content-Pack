from pathlib import Path

import pytest

from agent_loop import AgentAction, AgentLoopConfig, ControlledAgentLoop, load_agent_policy


def _loop(tmp_path: Path, **overrides) -> ControlledAgentLoop:
    config = AgentLoopConfig(
        mandatory_gates=["validate_market_data", "validate_content_consistency", "final_quality_gate", "reviewer_gate"],
        blocked_tools=["deliver", "canary_deliver", "shell"],
        **overrides,
    )
    return ControlledAgentLoop(config, audit_path=tmp_path / "agent_loop.jsonl", run_id="market_20260819_1200")


def _state(statuses: dict[str, str]) -> dict:
    return {"run_id": "market_20260819_1200", "steps": {step: {"status": status} for step, status in statuses.items()}}


def test_agent_selects_dependency_ready_steps_and_records_audit(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    state = _state({"health_check": "pending", "collect_sources": "pending", "collect_market_quotes": "pending", "generate_content": "pending"})
    first = loop.select_next_step(state, ["health_check", "collect_sources", "collect_market_quotes", "generate_content"])
    assert first.action is AgentAction.execute
    assert first.selected_step == "health_check"
    state["steps"]["health_check"]["status"] = "success"
    second = loop.select_next_step(state, ["health_check", "collect_sources", "collect_market_quotes", "generate_content"], last_step="health_check", last_status="success")
    assert second.selected_step == "collect_sources"
    assert (tmp_path / "agent_loop.jsonl").read_text(encoding="utf-8").count("\n") == 2


def test_mandatory_quality_gate_is_never_skipped(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    statuses = {
        "health_check": "success",
        "collect_sources": "success",
        "collect_market_quotes": "success",
        "generate_content": "success",
        "final_validation": "pending",
        "reviewer_agent": "pending",
        "reviewer_gate": "pending",
    }
    state = _state(statuses)
    decision = loop.select_next_step(state, list(statuses))
    assert decision.selected_step == "final_validation"
    assert decision.mandatory is True
    assert "final_validation" in loop.mandatory_steps


def test_failure_causes_bounded_replan(tmp_path: Path) -> None:
    loop = _loop(tmp_path, max_replans=1)
    state = _state({"health_check": "success", "collect_sources": "pending"})
    decision = loop.select_next_step(state, list(state["steps"]), last_step="collect_sources", last_status="failed")
    assert decision.action is AgentAction.replan
    assert decision.selected_step == "collect_sources"
    blocked = loop.select_next_step(state, list(state["steps"]), last_step="collect_sources", last_status="failed")
    assert blocked.action is AgentAction.stop
    assert blocked.reason == "max_replans_exceeded"


def test_budget_and_stagnation_fail_closed(tmp_path: Path) -> None:
    loop = _loop(tmp_path, max_iterations=1)
    state = _state({"health_check": "pending"})
    loop.select_next_step(state, ["health_check"])
    blocked = loop.select_next_step(state, ["health_check"])
    assert blocked.action is AgentAction.stop
    assert blocked.reason == "max_iterations_exceeded"


def test_delivery_and_shell_are_blocked_at_agent_layer(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    assert loop.tool_allowed("collect_market_data")
    assert not loop.tool_allowed("deliver")
    assert not loop.tool_allowed("shell")
    with pytest.raises(PermissionError):
        loop.assert_tool_allowed("canary_deliver")


def test_policy_file_is_strict_and_enables_controlled_agent() -> None:
    policy = load_agent_policy(Path("config/agent_policy.json"))
    assert policy.enabled is True
    assert policy.mode == "controlled_agent"
    assert "final_quality_gate" in policy.mandatory_gates
    assert "deliver" in policy.blocked_tools
