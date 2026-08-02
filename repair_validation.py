"""Post-repair validation for the bounded self-healing flow."""

from __future__ import annotations

from typing import Any


def _check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def validate_repair_result(
    classification: dict[str, Any] | None,
    result: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a repair result without executing another repair action."""

    classification = classification if isinstance(classification, dict) else {}
    result = result if isinstance(result, dict) else {}
    context = context if isinstance(context, dict) else {}
    checks: list[dict[str, Any]] = []

    _check(checks, "repair_action_succeeded", result.get("repair_action_succeeded") is True, "repair callback reported success")
    _check(checks, "original_failure_resolved", result.get("original_failure_resolved") is True, "repair callback marked the original failure resolved")
    _check(checks, "resume_succeeded", result.get("resume_succeeded") is True, "failed step or dependent pipeline was resumed")
    _check(checks, "callback_validation_passed", result.get("validation_passed") is True, "repair callback validation flag is true")

    category = str(classification.get("failure_category") or classification.get("category") or "unknown_failure")
    if category == "market_data_incomplete" or category == "DATA_SOURCE":
        data = result.get("market_data")
        validation = result.get("validation_result")
        resume = result.get("resume_result")
        _check(checks, "market_data_status", isinstance(data, dict) and data.get("status") == "success", "repaired market data is successful")
        _check(checks, "market_data_validation", isinstance(validation, dict) and validation.get("status") == "pass", "market data validator passed")
        _check(checks, "market_pipeline_resume", isinstance(resume, dict) and resume.get("status") == "success", "market pipeline resume passed")
    elif category == "gemini_json_parse_failure" or category == "MODEL_OUTPUT":
        parsed = result.get("parsed_output")
        fallback = result.get("selected_fallback")
        _check(
            checks,
            "structured_output_available",
            isinstance(parsed, dict) or fallback == "rule_template",
            "repaired structured output or approved template fallback is available",
        )
    elif category == "temporary_network_failure":
        _check(checks, "bounded_retry_recorded", isinstance(result.get("retry_count"), int) and result.get("retry_count", 0) > 0, "bounded retry count is recorded")
    elif category == "unknown_failure":
        _check(checks, "human_gate", False, "unknown failure requires human approval")

    passed = all(item["passed"] for item in checks)
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "category": category,
        "validation_step": context.get("validation_step") or "repair_validation",
        "checks": checks,
    }


__all__ = ["validate_repair_result"]
