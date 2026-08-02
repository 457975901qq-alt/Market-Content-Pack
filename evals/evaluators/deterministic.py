from __future__ import annotations

import json
import re
from typing import Any


def _result(score: float, label: str, explanation: str, evidence: list[Any]) -> dict[str, Any]:
    return {"score": score, "label": label, "explanation": explanation, "evidence": evidence}


def _payload(case: dict[str, Any], candidate_output: dict[str, Any] | None) -> dict[str, Any]:
    return candidate_output if isinstance(candidate_output, dict) else case.get("input", {})


def _text(payload: dict[str, Any]) -> str:
    parts = [payload.get("text", ""), payload.get("summary", ""), payload.get("headline", "")]
    for key in ("platform_copy", "content", "market_content"):
        value = payload.get(key)
        if isinstance(value, dict):
            parts.append(json.dumps(value, ensure_ascii=False))
        elif isinstance(value, str):
            parts.append(value)
    return "\n".join(str(part) for part in parts if part)


def schema_completeness(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, Any]:
    required = {"case_id", "edition", "input", "reference"}
    missing = sorted(required - set(case))
    reference_required = {"required_facts", "required_sources", "expected_theme", "allowed_tickers", "forbidden_claims", "expected_result"}
    missing.extend(f"reference.{field}" for field in sorted(reference_required - set(case.get("reference", {}))))
    if candidate_output is not None:
        missing.extend(f"candidate.{field}" for field in ("text", "delivery_allowed") if field not in candidate_output)
    return _result(1.0 if not missing else 0.0, "pass" if not missing else "fail", "required fields are present" if not missing else "required fields are missing", missing)


def source_grounding(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _payload(case, candidate_output)
    source_ids = set(payload.get("source_ids", [])) | set(payload.get("source_urls", []))
    source_ids |= set(payload.get("sources", [])) if isinstance(payload.get("sources"), list) else set()
    required = case.get("reference", {}).get("required_sources", [])
    missing = [item for item in required if item not in source_ids]
    unsupported = []
    for fact in case.get("reference", {}).get("required_facts", []):
        if isinstance(fact, dict) and fact.get("value") is not None and str(fact["value"]) not in _text(payload):
            unsupported.append(fact)
    evidence = missing + unsupported
    return _result(1.0 if not evidence else 0.0, "pass" if not evidence else "fail", "all facts are grounded" if not evidence else "source grounding missing", evidence)


def ticker_validity(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, Any]:
    allowed = set(case.get("reference", {}).get("allowed_tickers", []))
    payload = _payload(case, candidate_output)
    actual = set(payload.get("tickers", []))
    actual |= {str(item.get("symbol")) for item in payload.get("tickers_data", []) if isinstance(item, dict) and item.get("symbol")}
    invalid = sorted(actual - allowed) if allowed else []
    return _result(1.0 if not invalid else 0.0, "pass" if not invalid else "fail", "tickers are allowed" if not invalid else "ticker is not allowed", invalid)


def temporal_consistency(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, Any]:
    input_data = _payload(case, candidate_output)
    ok = input_data.get("report_date") == input_data.get("data_cutoff_date", input_data.get("report_date"))
    timestamp_dates = [str(value)[:10] for value in input_data.get("data_timestamps", []) if value]
    if timestamp_dates and any(value != input_data.get("report_date") for value in timestamp_dates):
        ok = False
    return _result(1.0 if ok else 0.0, "pass" if ok else "fail", "date and cutoff align" if ok else "date and cutoff differ", [input_data.get("report_date"), input_data.get("data_cutoff_date"), timestamp_dates])


def forbidden_claim_check(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _payload(case, candidate_output)
    text = _text(payload)
    forbidden = case.get("reference", {}).get("forbidden_claims", [])
    found = [item for item in forbidden if item in text]
    unsupported_numbers = []
    known_numbers = {str(item.get("value")) for item in case.get("reference", {}).get("required_facts", []) if isinstance(item, dict) and item.get("value") is not None}
    # Only inspect explicitly structured numeric claims. Dates, run ids and free
    # text are intentionally excluded so metadata cannot become a false claim.
    structured_claims = payload.get("numeric_claims", [])
    if isinstance(structured_claims, list):
        for claim in structured_claims:
            token = str(claim)
            if token.replace("%", "") not in known_numbers:
                unsupported_numbers.append(token)
    evidence = found + [f"unsupported_number:{item}" for item in unsupported_numbers]
    return _result(1.0 if not evidence else 0.0, "pass" if not evidence else "fail", "no forbidden claims" if not evidence else "forbidden or unsupported claim found", evidence)


def delivery_decision_accuracy(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = case.get("reference", {}).get("expected_result")
    actual = _payload(case, candidate_output).get("delivery_allowed")
    ok = (expected == "pass" and actual is True) or (expected == "fail" and actual is False)
    return _result(1.0 if ok else 0.0, "pass" if ok else "fail", "delivery decision matches reference" if ok else "delivery decision mismatch", [expected, actual])


def evaluate_case(case: dict[str, Any], candidate_output: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    return {
        "schema_completeness": schema_completeness(case, candidate_output),
        "source_grounding": source_grounding(case, candidate_output),
        "ticker_validity": ticker_validity(case, candidate_output),
        "temporal_consistency": temporal_consistency(case, candidate_output),
        "forbidden_claim_check": forbidden_claim_check(case, candidate_output),
        "delivery_decision_accuracy": delivery_decision_accuracy(case, candidate_output),
    }
