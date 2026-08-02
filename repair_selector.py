"""L5-2 repair strategy selection.

The selector produces a bounded, declarative plan. It does not execute a
retry, switch a provider, or modify artifacts; execution remains owned by the
existing fixed RepairController callbacks.
"""

from __future__ import annotations

from typing import Any


_STRATEGIES: dict[str, dict[str, Any]] = {
    "DATA_SOURCE": {
        "strategy_id": "DATA_SOURCE_RETRY_THEN_CROSS_CHECK",
        "actions": ["retry_primary_source", "use_secondary_source", "validate_market_data"],
        "validation_step": "validate_market_data",
    },
    "MODEL_OUTPUT": {
        "strategy_id": "MODEL_OUTPUT_RETRY_THEN_FALLBACK",
        "actions": ["retry_failed_model_step", "switch_provider", "use_rule_template"],
        "validation_step": "validate_content_consistency",
    },
    "SCHEMA_FORMAT": {
        "strategy_id": "SCHEMA_FORMAT_REGENERATE_THEN_VALIDATE",
        "actions": ["regenerate_structured_output", "validate_schema"],
        "validation_step": "validate_content_consistency",
    },
    "QUALITY_CHECK": {
        "strategy_id": "QUALITY_CHECK_REGENERATE_THEN_QA",
        "actions": ["regenerate_affected_content", "run_final_validation"],
        "validation_step": "final_validation",
    },
}


def select_repair_plan(
    classification: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
    *,
    execution_mode: str = "deferred",
) -> dict[str, Any]:
    """Select a safe repair plan from a classifier result without executing it."""

    classification = classification if isinstance(classification, dict) else {}
    context = context if isinstance(context, dict) else {}
    category = str(classification.get("category") or "UNKNOWN")
    recoverable = bool(classification.get("recoverable", False))
    strategy = _STRATEGIES.get(category)
    if strategy is None or not recoverable:
        return {
            "strategy_id": "UNKNOWN_MANUAL_TRIAGE",
            "category": "UNKNOWN",
            "execution_mode": "manual",
            "actions": ["collect_context", "request_human_approval"],
            "retry_step": None,
            "validation_step": None,
            "fallback_target": None,
            "max_attempts": 0,
            "requires_human_approval": True,
        }

    retry_step = classification.get("retry_step")
    if retry_step == "same_failed_step":
        retry_step = context.get("step") or "same_failed_step"
    return {
        "strategy_id": strategy["strategy_id"],
        "category": category,
        "execution_mode": execution_mode,
        "actions": list(strategy["actions"]),
        "retry_step": retry_step,
        "validation_step": strategy["validation_step"],
        "fallback_target": classification.get("fallback_target"),
        "max_attempts": 2,
        "requires_human_approval": False,
    }


__all__ = ["select_repair_plan"]
