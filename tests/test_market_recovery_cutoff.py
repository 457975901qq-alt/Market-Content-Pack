from pathlib import Path

from agent import AgentState, RuleBasedAgentPlanner
from observability import STEP_TO_STAGE, stable_error_code
from self_healing.agent import RepairAdapters, RepairController, RepairStatus, classify_failure


def _run_id() -> str:
    return "market_20260819_2300_recovery"


def test_market_data_not_validated_is_not_unknown() -> None:
    result = classify_failure(_run_id(), "f1", "collect_market_data", "market_data_not_validated")
    assert result.failure_category == "market_data_not_validated"
    assert result.failure_category != "unknown_failure"


def test_market_data_missing_classification() -> None:
    result = classify_failure(
        _run_id(),
        "f2",
        "collect_market_data",
        "market_data_not_validated",
        {"validation_errors": [{"symbol": "NDX", "error_type": "market_data_missing"}]},
    )
    assert result.failure_category == "market_data_missing"
    assert result.details["symbols"] == ["NDX"]


def test_market_data_conflict_classification() -> None:
    result = classify_failure(
        _run_id(),
        "f3",
        "collect_market_data",
        "market_data_not_validated",
        {"validation_errors": [{"symbol": "SPX", "error_type": "source_conflict"}]},
    )
    assert result.failure_category == "market_data_conflict"


def test_market_data_stale_classification() -> None:
    result = classify_failure(
        _run_id(),
        "f4",
        "collect_market_data",
        "market_data_not_validated",
        {"validation_errors": [{"symbol": "DJI", "error_type": "stale_market_data", "message": "staleness_window_exceeded"}]},
    )
    assert result.failure_category == "market_data_stale"


def test_market_data_after_cutoff_classification() -> None:
    result = classify_failure(
        _run_id(),
        "f5",
        "collect_market_data",
        "market_data_not_validated",
        {
            "cutoff": "2026-08-19T17:30:00+09:00",
            "validation_errors": [{"symbol": "SPX", "error_type": "stale_market_data", "message": "future_timestamp"}],
        },
    )
    assert result.failure_category == "market_data_future"
    assert result.recoverable is False
    assert result.details["cutoff"] == "2026-08-19T17:30:00+09:00"


def test_market_provider_unavailable_classification() -> None:
    result = classify_failure(_run_id(), "f6", "collect_market_data", "market_provider_unavailable")
    assert result.failure_category == "market_provider_unavailable"


def test_after_cutoff_does_not_retry_same_latest_action(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    controller = RepairController(
        _run_id(),
        tmp_path / "canary",
        RepairAdapters(collect_market_quotes=lambda symbols: calls.append(symbols) or {"status": "success"}),
        sleep=lambda _: None,
    )
    result = controller.repair(
        "f7",
        "collect_market_data",
        "market_data_not_validated",
        {
            "cutoff": "2026-08-19T17:30:00+09:00",
            "validation_errors": [{"symbol": "SPX", "error_type": "stale_market_data", "message": "future_timestamp"}],
        },
    )
    assert result["classification"]["failure_category"] == "market_data_future"
    assert result["result"]["status"] == RepairStatus.repair_failed.value
    assert calls == []


def test_agent_state_and_planner_block_non_retryable_market_failure() -> None:
    state = AgentState(goal="test", run_id=_run_id(), edition="evening_premarket_watch")
    state.apply_observation(
        {
            "success": False,
            "tool_name": "collect_market_data",
            "data": {
                "failure": {
                    "failure_category": "market_data_future",
                    "details": {"symbols": ["SPX"]},
                }
            },
        }
    )
    assert state.failure["failure_category"] == "market_data_future"
    assert RuleBasedAgentPlanner(provider="rule_template").next_action(state) is None


def test_market_failure_stage_and_trace_code_are_specific() -> None:
    assert STEP_TO_STAGE["collect_market_quotes"] == "market_data_collection"
    assert STEP_TO_STAGE["validate_market_data"] == "market_data_validation"
    assert stable_error_code({"failure": {"failure_category": "market_data_future"}}, "market_data_collection") == "MARKET_DATA_FUTURE"
