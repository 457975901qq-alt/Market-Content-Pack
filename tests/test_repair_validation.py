from __future__ import annotations

from repair_validation import validate_repair_result


def test_market_repair_requires_data_validation_and_pipeline_resume() -> None:
    result = validate_repair_result(
        {"failure_category": "market_data_incomplete"},
        {
            "repair_action_succeeded": True,
            "original_failure_resolved": True,
            "resume_succeeded": True,
            "validation_passed": True,
            "market_data": {"status": "success"},
            "validation_result": {"status": "pass"},
            "resume_result": {"status": "success"},
        },
    )
    assert result["status"] == "pass"
    assert all(item["passed"] for item in result["checks"])


def test_repair_is_rejected_when_post_validation_fails() -> None:
    result = validate_repair_result(
        {"failure_category": "market_data_incomplete"},
        {
            "repair_action_succeeded": True,
            "original_failure_resolved": True,
            "resume_succeeded": True,
            "validation_passed": True,
            "market_data": {"status": "success"},
            "validation_result": {"status": "fail"},
            "resume_result": {"status": "success"},
        },
    )
    assert result["status"] == "fail"
    assert any(item["name"] == "market_data_validation" and not item["passed"] for item in result["checks"])


def test_model_repair_requires_structured_output_or_template_fallback() -> None:
    result = validate_repair_result(
        {"failure_category": "gemini_json_parse_failure"},
        {
            "repair_action_succeeded": True,
            "original_failure_resolved": True,
            "resume_succeeded": True,
            "validation_passed": True,
        },
    )
    assert result["status"] == "fail"
