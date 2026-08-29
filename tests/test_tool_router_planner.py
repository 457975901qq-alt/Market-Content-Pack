from __future__ import annotations

import json

from execution_planner import ExecutionPlanner
from tool_router import ToolRouter


def _health(**services):
    return {"services": services}


def test_unhealthy_x_is_rejected_but_rss_is_selected() -> None:
    router = ToolRouter({"sources": {"x": {"status": "unhealthy", "reason": "x_not_connected"}}})
    decision = router.choose("news_discovery")
    assert decision["selected_tool"] == "rss"
    assert {item["tool"] for item in decision["rejected_tools"]} == {"x"}


def test_unavailable_jina_selects_rss_summary() -> None:
    router = ToolRouter({"sources": {"jina": {"status": "unavailable", "reason": "jina_offline"}}})
    decision = router.choose("web_extraction")
    assert decision["selected_tool"] == "rss_summary"
    assert decision["rejected_tools"] == [{"tool": "jina", "reason": "jina_offline"}]


def test_model_routes_ollama_to_gemini_then_template() -> None:
    gemini = ToolRouter(_health(ollama={"status": "unhealthy"}, gemini={"status": "healthy"}))
    assert gemini.choose("content_model")["selected_tool"] == "gemini"
    template = ToolRouter(_health(ollama={"status": "unhealthy"}, gemini={"status": "unavailable"}))
    assert template.choose("content_model")["selected_tool"] == "rule_template"


def test_planner_keeps_text_quality_gates_and_archive() -> None:
    router = ToolRouter(_health(ollama={"status": "healthy"}, gemini={"status": "healthy"}))
    plan = ExecutionPlanner(router).build(
        run_id="market_20260720_1200",
        edition="evening_premarket_watch",
        state={"steps": {}},
        preferred_provider="auto",
    )
    assert set(plan["mandatory_gates"]) == {"validate_market_data", "validate_content_consistency", "final_quality_gate"}
    quality = next(item for item in plan["steps"] if item["step"] == "final_quality_gate")
    assert quality["mandatory"] is True
    assert quality["executor_step"] == "final_validation"
    assert plan["constraints"]["allow_delivery"] is False
    assert [item["step"] for item in plan["steps"] if item["mandatory"]] == [
        "collect_market_data",
        "validate_market_data",
        "generate_content",
        "validate_content_consistency",
        "final_quality_gate",
    ]


def test_planner_resume_preserves_success_and_marks_pending_replan() -> None:
    router = ToolRouter(_health(ollama={"status": "unhealthy"}, gemini={"status": "healthy"}))
    state = {"steps": {"collect_sources": {"status": "success"}, "generate_content": {"status": "failed"}}}
    old = {"selected_provider": "ollama", "steps": [{"step": "collect_sources", "selected_tool": "rss"}]}
    plan = ExecutionPlanner(router).build(
        run_id="market_20260720_1201",
        edition="evening_premarket_watch",
        state=state,
        preferred_provider="auto",
        prior_plan=old,
    )
    sources = next(item for item in plan["steps"] if item["step"] == "collect_news")
    content = next(item for item in plan["steps"] if item["step"] == "generate_content")
    assert sources["status"] == "success"
    assert content["status"] == "pending"
    assert plan["resumed_from_plan"] is True


def test_plan_is_text_only_and_has_no_media_tools() -> None:
    router = ToolRouter(_health(ollama={"status": "healthy"}, gemini={"status": "healthy"}))
    plan = ExecutionPlanner(router).build(
        run_id="market_20260720_1202",
        edition="evening_premarket_watch",
        state={"steps": {}},
        preferred_provider="ollama",
        text_only=True,
    )
    assert plan["text_only"] is True
    assert plan["mandatory_gates"]
    assert all("image" not in json.dumps(item, ensure_ascii=False).lower() for item in plan["steps"])
    assert all("image" not in json.dumps(item, ensure_ascii=False).lower() for item in router.decisions)


def test_router_is_stable_for_same_health_snapshot() -> None:
    health = _health(ollama={"status": "unhealthy"}, gemini={"status": "healthy"})
    left = ToolRouter(health).choose("content_model")
    right = ToolRouter(health).choose("content_model")
    assert (left["selected_tool"], left["fallback_chain"]) == (right["selected_tool"], right["fallback_chain"])


def test_runtime_failure_removes_provider_from_next_decision() -> None:
    router = ToolRouter(_health(ollama={"status": "healthy"}, gemini={"status": "healthy"}))
    assert router.choose("content_model")["selected_tool"] == "ollama"
    router.mark_runtime_failure("ollama", "temporary_timeout")
    decision = router.choose("content_model")
    assert decision["selected_tool"] == "gemini"
    assert {item["tool"] for item in decision["rejected_tools"]} == {"ollama"}


def test_unknown_preferred_tool_is_rejected() -> None:
    router = ToolRouter(_health())
    try:
        router.choose("content_model", preferred="shell")
    except Exception as exc:
        assert "no_available_tool" in str(exc)
    else:
        raise AssertionError("unknown tool must be rejected")


def test_source_switches_are_reflected_before_collection() -> None:
    import os

    old = {key: os.environ.get(key) for key in ("SOURCE_ROUTER_LIVE", "JINA_ENRICH", "RSS_FEEDS")}
    try:
        os.environ["SOURCE_ROUTER_LIVE"] = "false"
        os.environ["JINA_ENRICH"] = "false"
        os.environ["RSS_FEEDS"] = ""
        router = ToolRouter(_health())
        assert router.choose("news_discovery")["selected_tool"] == "github"
        assert router.choose("web_extraction")["selected_tool"] == "rss_summary"
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
