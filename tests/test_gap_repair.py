from __future__ import annotations

import json
from pathlib import Path

import pytest

from self_healing.gap_analyzer import analyze_gap
from self_healing.repair_planner import ALLOWED_STEPS, RepairPlanner


def test_missing_index_gap_is_repairable_and_resets_market_dependents(tmp_path: Path) -> None:
    gap = analyze_gap(
        validation_errors=[
            {
                "step": "validate_market_data",
                "field": "major_indexes.SPX.current_price",
                "message": "missing index price",
                "artifact": str(tmp_path / "market_data.json"),
            }
        ],
        current_state={"steps": {"collect_news": {"status": "success"}}, "missing_symbols": ["SPX"]},
        run_id="market_20260720_1200",
    )
    assert gap["error_type"] == "missing_upstream_data"
    assert gap["gap_kind"] == "missing_market_data"
    assert gap["repairable"] is True
    assert gap["repair_step"] == "collect_market_quotes"
    assert "generate_content" in gap["downstream_steps_to_reset"]
    assert "SPX" in gap["missing_fields"]


def test_repair_planner_preserves_unrelated_success_and_writes_atomic_plan(tmp_path: Path) -> None:
    run_id = "market_20260720_1202"
    gap = analyze_gap(validation_errors=[{"message": "missing SPX price", "field": "SPX.current_price"}], run_id=run_id)
    planner = RepairPlanner(tmp_path, max_attempts=2)
    plan = planner.build(
        run_id=run_id,
        trigger_error="missing SPX price",
        gap=gap,
        current_state={
            "steps": {
                "collect_news": {"status": "success"},
                "generate_content": {"status": "success"},
            }
        },
        selected_tools=["collect_market_data", "deliver", "unknown_tool"],
    )
    path = tmp_path / run_id / "repair_plan_1.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == plan
    assert "collect_news" in plan["preserved_steps"]
    assert "generate_content" not in plan["preserved_steps"]
    assert "collect_market_data" in plan["selected_tools"]
    assert "deliver" not in plan["selected_tools"]
    assert set(plan["reset_steps"]).issubset(ALLOWED_STEPS)


def test_non_repairable_gap_cannot_create_plan(tmp_path: Path) -> None:
    gap = analyze_gap(run_id="market_20260720_1203")
    with pytest.raises(ValueError, match="repair_plan_not_allowed_or_empty"):
        RepairPlanner(tmp_path).build(run_id="market_20260720_1203", trigger_error="unknown", gap=gap)


def test_gap_analyzer_routes_provider_failure_to_bounded_repair() -> None:
    result = analyze_gap(validation_errors=[{"step": "generate_content", "message": "Ollama unavailable"}], run_id="market_20260720_1204")
    assert result["gap_kind"] == "dependency_failure"
    assert result["repair_step"] == "generate_content"
    assert result["repairable"] is True
