from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from configuration import configuration_report, load_runtime_policy
from execution_planner import ExecutionPlanner
import run_state
from run_state import mark_logical
from tool_router import ToolRouter
from build_daily_market_pack import run_command


def test_runtime_policy_has_no_media_generation_switch() -> None:
    policy = load_runtime_policy()
    assert "allow_image_generation" not in policy
    assert policy["step_timeout_seconds"] > 0


def test_configuration_report_does_not_expose_secret_values() -> None:
    report = configuration_report(provider="gemini", environ={"GEMINI_API_KEY": "secret-value"})
    assert report["status"] == "healthy"
    assert "secret-value" not in json.dumps(report)
    assert report["missing_environment"] == []


def test_configuration_report_identifies_missing_provider_credentials() -> None:
    report = configuration_report(provider="gemini", environ={"GEMINI_API_KEY": ""})
    assert report["status"] == "unavailable"
    assert report["missing_environment"] == ["GEMINI_API_KEY"]


def test_run_command_returns_timeout_without_hanging(tmp_path: Path) -> None:
    with patch("build_daily_market_pack.subprocess.run", side_effect=subprocess.TimeoutExpired(["fake"], 1)):
        code, stdout, stderr = run_command(["fake"], {"STEP_TIMEOUT_SECONDS": "1"}, tmp_path / "commands.jsonl")
    assert code == 124
    assert "step_timeout_after_1s" in stderr
    event = json.loads((tmp_path / "commands.jsonl").read_text(encoding="utf-8"))
    assert event["status"] == "timeout"


def test_logical_steps_keep_independent_statuses(tmp_path: Path) -> None:
    state = run_state.create("market_20260805_0901", "morning_close_review", tmp_path / "runtime", tmp_path / "outputs")
    mark_logical(state, "collect_news", "success", tmp_path / "runtime")
    mark_logical(state, "extract_web_content", "failed", tmp_path / "runtime", error={"message": "jina unavailable"})
    assert state["logical_steps"]["collect_news"]["status"] == "success"
    assert state["logical_steps"]["extract_web_content"]["status"] == "failed"
    assert state["steps"]["collect_sources"]["status"] == "pending"


def test_planner_preserves_news_and_web_fallback_chains() -> None:
    router = ToolRouter({"services": {}, "sources": {}})
    state = {"steps": {step: {"status": "pending"} for step in run_state.STEPS}}
    plan = ExecutionPlanner(router).build(run_id="market_20260805_0902", edition="morning_close_review", state=state)
    assert plan["news_fallback_chain"]
    assert plan["web_fallback_chain"]
