"""L5-1 error classification for the daily market content pipeline.

This module only identifies a failure and recommends the next recovery
direction.  It deliberately does not retry, switch providers, or mutate any
pipeline state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


UNKNOWN_CATEGORY = "UNKNOWN"

# The values are intentionally plain strings so the classification payload can
# be written directly to JSONL logs and persisted in the existing run state.
_RULES: dict[str, dict[str, Any]] = {
    "empty_response": {
        "category": "MODEL_OUTPUT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "retry_model_request",
        "retry_step": "generate_content",
        "fallback_target": "gemini_or_rule_template",
    },
    "json_parse_failed": {
        "category": "MODEL_OUTPUT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "retry_with_strict_json_prompt",
        "retry_step": "generate_content",
        "fallback_target": "gemini_or_rule_template",
    },
    "empty_required_field": {
        "category": "SCHEMA_FORMAT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "regenerate_missing_required_fields",
        "retry_step": "generate_content",
        "fallback_target": "rule_template",
    },
    "date_mismatch": {
        "category": "QUALITY_CHECK",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "regenerate_with_current_edition_metadata",
        "retry_step": "generate_content",
        "fallback_target": "rule_template",
    },
    "market_data_missing": {
        "category": "DATA_SOURCE",
        "recoverable": True,
        "severity": "critical",
        "recommended_action": "retry_market_source_and_cross_check",
        "retry_step": "collect_market_quotes",
        "fallback_target": "secondary_market_source",
    },
    "timeout": {
        "category": "MODEL_OUTPUT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "retry_with_timeout_backoff",
        "retry_step": "same_failed_step",
        "fallback_target": "alternate_provider_or_source",
    },
    "provider_unavailable": {
        "category": "MODEL_OUTPUT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "check_provider_health_and_switch",
        "retry_step": "generate_content",
        "fallback_target": "alternate_provider",
    },
    "provider_http_error": {
        "category": "MODEL_OUTPUT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "switch_provider_after_transient_http_error",
        "retry_step": "generate_content",
        "fallback_target": "alternate_provider_or_rule_template",
    },
    "content_provider_chain_exhausted": {
        "category": "MODEL_OUTPUT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "select_next_configured_content_provider",
        "retry_step": "generate_content",
        "fallback_target": "alternate_provider_or_rule_template",
    },
    "schema_validation_failed": {
        "category": "SCHEMA_FORMAT",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "validate_schema_and_regenerate",
        "retry_step": "generate_content",
        "fallback_target": "rule_template",
    },
    "quality_gate_failed": {
        "category": "QUALITY_CHECK",
        "recoverable": True,
        "severity": "high",
        "recommended_action": "inspect_text_quality_findings_and_regenerate_content",
        "retry_step": "final_validation",
        "fallback_target": "rule_template",
    },
}

_ALIASES = {
    "provider_empty_response": "empty_response",
    "market_data_incomplete": "market_data_missing",
}


def _text(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def _resolve_error_code(error_code: Any, context: dict[str, Any]) -> str:
    candidate = _text(error_code)
    if candidate in _RULES:
        return candidate
    if candidate in _ALIASES:
        return _ALIASES[candidate]

    # Existing callers sometimes provide a process return code and keep the
    # semantic failure in error_type.  Prefer that semantic code when present.
    for value in (context.get("error_type"), context.get("failure_reason")):
        semantic = _text(value)
        if semantic in _RULES:
            return semantic
        if semantic in _ALIASES:
            return _ALIASES[semantic]

    # A subprocess may only return exit code 1 while writing the semantic
    # error code to stderr. Recover that code for the parent run log without
    # making message parsing part of the recovery implementation.
    message = _text(context.get("message") or context.get("raw_message"))
    for known_code in (*_RULES, *_ALIASES):
        if known_code in message:
            return _ALIASES.get(known_code, known_code)

    step = _text(context.get("step"))
    if step in {"collect_market_quotes", "collect_market_data", "validate_market_data"}:
        return "market_data_missing"
    return candidate or "unknown_error"


def classify_error(error_code: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a stable classification payload without raising for unknown input."""

    safe_context = context if isinstance(context, dict) else {}
    resolved_code = _resolve_error_code(error_code, safe_context)
    rule = _RULES.get(resolved_code)
    if rule is None:
        return {
            "error_code": str(error_code).strip() if error_code is not None and str(error_code).strip() else "unknown_error",
            "category": UNKNOWN_CATEGORY,
            "recoverable": False,
            "severity": "unknown",
            "recommended_action": "manual_triage_required",
            "retry_step": None,
            "fallback_target": None,
        }
    return {"error_code": resolved_code, **deepcopy(rule)}


__all__ = ["classify_error"]
