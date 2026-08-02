from __future__ import annotations

import json

import run_state
from build_daily_market_pack import _transition
from error_classifier import classify_error
from repair_selector import select_repair_plan


KNOWN_ERRORS = {
    "empty_response": "MODEL_OUTPUT",
    "json_parse_failed": "MODEL_OUTPUT",
    "empty_required_field": "SCHEMA_FORMAT",
    "date_mismatch": "QUALITY_CHECK",
    "market_data_missing": "DATA_SOURCE",
    "timeout": "MODEL_OUTPUT",
    "provider_unavailable": "MODEL_OUTPUT",
    "provider_http_error": "MODEL_OUTPUT",
    "content_provider_chain_exhausted": "MODEL_OUTPUT",
    "schema_validation_failed": "SCHEMA_FORMAT",
    "quality_gate_failed": "QUALITY_CHECK",
}


def test_all_known_errors_have_required_classification_fields() -> None:
    required = {"error_code", "category", "recoverable", "severity", "recommended_action", "retry_step", "fallback_target"}
    for error_code, expected_category in KNOWN_ERRORS.items():
        result = classify_error(error_code)
        assert required <= result.keys()
        assert result["error_code"] == error_code
        assert result["category"] == expected_category
        assert isinstance(result["recoverable"], bool)


def test_unknown_error_is_safe_and_classified_as_unknown() -> None:
    result = classify_error("future_error_code")
    assert result["error_code"] == "future_error_code"
    assert result["category"] == "UNKNOWN"
    assert result["recoverable"] is False
    assert result["recommended_action"] == "manual_triage_required"
    assert classify_error(None)["category"] == "UNKNOWN"


def test_existing_semantic_context_can_classify_numeric_process_code() -> None:
    result = classify_error("1", {"step": "collect_market_quotes", "error_type": "market_data_incomplete"})
    assert result["error_code"] == "market_data_missing"
    assert result["category"] == "DATA_SOURCE"


def test_subprocess_message_can_recover_the_semantic_error_code() -> None:
    result = classify_error("1", {"step": "generate_content", "message": "stopped: json_parse_failed: invalid JSON"})
    assert result["error_code"] == "json_parse_failed"
    assert result["category"] == "MODEL_OUTPUT"


def test_repair_selector_returns_deferred_plan_for_known_failure() -> None:
    classification = classify_error("timeout")
    plan = select_repair_plan(classification, {"step": "collect_sources"})
    assert plan["strategy_id"] == "MODEL_OUTPUT_RETRY_THEN_FALLBACK"
    assert plan["execution_mode"] == "deferred"
    assert plan["retry_step"] == "collect_sources"
    assert plan["validation_step"] == "validate_content_consistency"
    assert plan["requires_human_approval"] is False


def test_repair_selector_blocks_unknown_failure() -> None:
    plan = select_repair_plan(classify_error("future_error_code"))
    assert plan["strategy_id"] == "UNKNOWN_MANUAL_TRIAGE"
    assert plan["execution_mode"] == "manual"
    assert plan["requires_human_approval"] is True


def test_transition_writes_structured_failure_event(tmp_path) -> None:
    state_root = tmp_path / "runtime"
    output_root = tmp_path / "outputs"
    state = run_state.create("market_20260720_1400", "evening_premarket_watch", state_root, output_root)
    log_path = tmp_path / "steps.jsonl"

    _transition(
        state,
        "generate_content",
        "failed",
        state_root,
        log_path,
        error={"error_type": "model_error", "error_code": "json_parse_failed", "message": "invalid JSON"},
    )

    event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["run_id"] == "market_20260720_1400"
    assert event["step"] == "generate_content"
    assert event["error_code"] == "json_parse_failed"
    assert event["category"] == "MODEL_OUTPUT"
    assert event["recoverable"] is True
    assert event["recommended_action"]
    assert event["retry_step"] == "generate_content"
    assert event["fallback_target"]
    assert event["raw_message"] == "invalid JSON"
    assert event["repair_selection"]["strategy_id"] == "MODEL_OUTPUT_RETRY_THEN_FALLBACK"
    assert event["repair_selection"]["execution_mode"] == "deferred"
    assert state["steps"]["generate_content"]["error"]["category"] == "MODEL_OUTPUT"
