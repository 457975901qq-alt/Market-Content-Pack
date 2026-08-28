from __future__ import annotations

from agent import AgentState, ModelAssistedAgentPlanner, RuleBasedAgentPlanner


def _planner(response: str, allowed: set[str] | None = None) -> ModelAssistedAgentPlanner:
    return ModelAssistedAgentPlanner(
        provider="ollama",
        allowed_tools=allowed or {"collect_news", "collect_market_data", "validate_content_consistency", "final_quality_gate"},
        fallback=RuleBasedAgentPlanner(provider="ollama"),
        call_model=lambda _prompt: response,
    )


def test_model_planner_accepts_one_registered_action() -> None:
    planner = _planner('{"actions":[{"tool_name":"collect_news","arguments":{"sources":["rss"]}}]}')
    action = planner.next_action(AgentState(goal="market pack"))
    assert action is not None
    assert action.tool_name == "collect_news"
    assert action.arguments == {"sources": ["rss"]}


def test_unknown_model_tool_falls_back_to_deterministic_plan() -> None:
    planner = _planner('{"actions":[{"tool_name":"unknown_tool","arguments":{}}]}')
    action = planner.next_action(AgentState(goal="market pack"))
    assert action is not None
    assert action.tool_name == "collect_news"
    assert planner.last_error == "planner_action_rejected"


def test_model_cannot_select_delivery_or_shell() -> None:
    planner = _planner('{"actions":[{"tool_name":"deliver","arguments":{}}]}', {"deliver", "collect_news"})
    action = planner.next_action(AgentState(goal="market pack"))
    assert action is not None
    assert action.tool_name == "collect_news"


def test_model_cannot_skip_mandatory_gate_order() -> None:
    planner = _planner('{"actions":[{"tool_name":"final_quality_gate","arguments":{}}]}')
    action = planner.next_action(AgentState(goal="market pack"))
    assert action is not None
    assert action.tool_name == "collect_news"


def test_invalid_model_json_uses_rule_fallback() -> None:
    planner = _planner("not json")
    action = planner.next_action(AgentState(goal="market pack"))
    assert action is not None
    assert action.tool_name == "collect_news"
    assert planner.last_error == "PlannerModelError:planner_invalid_json"


def test_rule_provider_does_not_call_model() -> None:
    calls = []
    planner = ModelAssistedAgentPlanner(
        provider="rule_template",
        allowed_tools={"collect_news"},
        fallback=RuleBasedAgentPlanner(),
        call_model=lambda prompt: calls.append(prompt) or "{}",
    )
    action = planner.next_action(AgentState(goal="market pack"))
    assert action is not None
    assert calls == []
