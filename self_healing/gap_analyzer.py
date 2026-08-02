"""Deterministic upstream-gap analysis for bounded self-healing.

This module only describes a repair. It never fetches data, edits artifacts, or
executes a tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


REPAIRABLE_TYPES = {"missing_upstream_data"}


def _items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"message": str(item)} for item in value]
    return [{"message": str(value)}]


def _text(errors: list[dict[str, Any]]) -> str:
    return " ".join(str(item.get("message") or item.get("error") or item.get("reason") or "") for item in errors).lower()


def _paths(errors: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for item in errors:
        for key in ("artifact", "path", "file", "artifact_path"):
            value = item.get(key)
            if value:
                found.append(str(value))
    for value in manifest.get("affected_artifacts", []) if isinstance(manifest, dict) else []:
        if value:
            found.append(str(value))
    return list(dict.fromkeys(found))


def _fields(errors: list[dict[str, Any]], context: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for item in errors:
        value = item.get("field") or item.get("field_path") or item.get("missing_field")
        if value:
            found.append(str(value))
        for field in item.get("missing_fields", []) if isinstance(item.get("missing_fields"), list) else []:
            found.append(str(field))
    for key in ("missing_fields", "missing_symbols", "missing_tickers", "invalid_fields"):
        value = context.get(key, [])
        if isinstance(value, list):
            found.extend(str(item) for item in value)
    return list(dict.fromkeys(found))


def _downstream(kind: str) -> list[str]:
    if kind in {"missing_market_data", "stale_market_data", "missing_upstream_data"}:
        return [
            "collect_market_quotes",
            "validate_market_data",
            "generate_content",
            "validate_content_consistency",
            "final_quality_gate",
            "reviewer_gate",
        ]
    if kind == "missing_source":
        return ["collect_news", "extract_web_content", "generate_content", "validate_content_consistency", "final_quality_gate", "reviewer_gate"]
    if kind in {"dependency_failure", "model_output_failure"}:
        return ["generate_content", "validate_content_consistency", "final_quality_gate", "reviewer_gate"]
    if kind == "network_failure":
        return ["collect_news", "extract_web_content", "generate_content", "validate_content_consistency", "final_quality_gate", "reviewer_gate"]
    return []


def analyze_gap(
    validation_errors: Any = None,
    current_state: dict[str, Any] | None = None,
    artifact_manifest: dict[str, Any] | None = None,
    tool_decision_history: Any = None,
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """Return a fixed, explainable gap contract without performing repair."""
    validation = _items(validation_errors)
    errors = validation
    context = current_state or {}
    manifest = artifact_manifest or {}
    combined = _text(errors)
    failed_step = next((str(item.get("step")) for item in errors if item.get("step")), str(context.get("failed_step") or ""))

    if any(token in combined for token in ("ollama", "gemini", "provider_unavailable", "model unavailable")):
        gap_kind = "dependency_failure"
        repair_step = "generate_content"
        reason = "the selected model/provider failed; use bounded healthcheck and configured fallback"
    elif any(token in combined for token in ("timeout", "http 5", "503", "connection")):
        gap_kind = "network_failure"
        repair_step = failed_step or "collect_news"
        reason = "a collector or web route failed transiently; retry only that route"
    elif any(token in combined for token in ("json parse", "invalid json", "schema_error", "truncated")):
        gap_kind = "model_output_failure"
        repair_step = "generate_content"
        reason = "model output was not valid structured JSON; use bounded repair/fallback"
    elif any(token in combined for token in ("source missing", "missing source", "source_url", "source_id", "provenance")):
        gap_kind = "missing_source"
        repair_step = "collect_news"
        reason = "a required event or claim has no traceable source"
    elif any(token in combined for token in ("timestamp", "stale", "freshness", "expired")):
        gap_kind = "stale_market_data"
        repair_step = "collect_market_quotes"
        reason = "market data is outside the edition freshness window"
    elif any(token in combined for token in ("price_series", "ticker", "index", "market", "quote", "price")) or context.get("missing_symbols") or context.get("missing_tickers"):
        gap_kind = "missing_market_data"
        repair_step = "collect_market_quotes"
        reason = "required structured market fields are incomplete"
    elif errors:
        gap_kind = "missing_upstream_data"
        repair_step = failed_step or "collect_market_quotes"
        reason = "validation reported an upstream artifact gap"
    else:
        gap_kind = "unknown_failure"
        repair_step = failed_step
        reason = "no deterministic upstream gap was identified"

    affected = _paths(errors, manifest)
    fields = _fields(errors, context)
    repairable = gap_kind in {"missing_market_data", "stale_market_data", "missing_source", "missing_upstream_data", "dependency_failure", "network_failure", "model_output_failure"} and bool(repair_step)
    error_type = "missing_upstream_data" if repairable else "unknown_failure"
    return {
        "error_type": error_type,
        "gap_kind": gap_kind,
        "affected_artifacts": affected,
        "missing_fields": fields,
        "invalid_fields": list(dict.fromkeys(str(item) for item in context.get("invalid_fields", []) if item)),
        "repairable": repairable,
        "repair_step": repair_step,
        "repair_scope": [repair_step] if repair_step else [],
        "downstream_steps_to_reset": _downstream(gap_kind),
        "reason": reason,
        "run_id": run_id,
        "failed_step": failed_step,
        "tool_decision_history_available": bool(tool_decision_history),
        "current_state_available": bool(current_state),
    }


class GapAnalyzer:
    """Small object wrapper for integrations that prefer an injectable analyzer."""

    def analyze(self, **kwargs: Any) -> dict[str, Any]:
        return analyze_gap(**kwargs)


__all__ = ["GapAnalyzer", "analyze_gap", "REPAIRABLE_TYPES"]
