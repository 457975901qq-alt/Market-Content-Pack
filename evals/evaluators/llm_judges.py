from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from model_providers import ProviderError, call_gemini, call_ollama

JUDGE_PROMPT_VERSION = "market_content_judge_v1"
METRICS = ("factual_faithfulness", "content_usability")


@dataclass(frozen=True)
class JudgeConfig:
    provider: str
    model: str
    prompt_version: str = JUDGE_PROMPT_VERSION
    temperature: float = 0.0
    max_retries: int = 2


def build_prompt(rows: list[dict[str, Any]], prompt_version: str = JUDGE_PROMPT_VERSION) -> str:
    return json.dumps({"prompt_version": prompt_version, "task": "Return strict JSON with results for every case_id and both metrics.", "rubric": {"factual_faithfulness": "1.0 fully grounded, 0.8 minor wording expansion, 0.5 unverifiable inference, 0.0 fabricated or wrong", "content_usability": "1.0 publishable, 0.8 minor edits, 0.5 substantial rewrite, 0.0 unusable"}, "cases": rows}, ensure_ascii=False)


def _strict_results(payload: Any, expected_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("judge response must contain results array")
    rows = [row for row in payload["results"] if isinstance(row, dict)]
    returned_ids = {str(row.get("case_id")) for row in rows}
    missing = expected_ids - returned_ids
    if missing:
        raise ValueError(f"missing case_id: {sorted(missing)}")
    for row in rows:
        for metric in METRICS:
            value = row.get(metric)
            if not isinstance(value, dict) or not isinstance(value.get("score"), (int, float)):
                raise ValueError(f"invalid metric payload for {row.get('case_id')}:{metric}")
            value["score"] = max(0.0, min(1.0, float(value["score"])))
            value.setdefault("label", "pass" if value["score"] >= 0.75 else "warning")
            value.setdefault("explanation", "")
            value.setdefault("evidence", [])
    return rows


def parse_judge_response(text: str, expected_ids: set[str]) -> list[dict[str, Any]]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return _strict_results(json.loads(cleaned), expected_ids)


def deterministic_judge_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        score = float(row.get("deterministic_score", 1.0))
        result.append({"case_id": row["case_id"], "candidate": row.get("candidate", ""), "factual_faithfulness": {"score": score, "label": "pass" if score >= 0.9 else "fail", "explanation": "deterministic offline judge", "evidence": row.get("evidence", [])}, "content_usability": {"score": score, "label": "pass" if score >= 0.75 else "fail", "explanation": "deterministic offline judge", "evidence": row.get("evidence", [])}})
    return result


def judge_batch(rows: list[dict[str, Any]], config: JudgeConfig, call: Callable[[str], str] | None = None) -> dict[str, Any]:
    if not rows:
        return {"results": [], "request_count": 0, "retry_count": 0, "errors": []}
    expected_ids = {str(row["case_id"]) for row in rows}
    if call is None:
        call = lambda prompt: call_ollama(prompt, config.model) if config.provider == "ollama" else call_gemini(prompt, config.model)
    errors: list[str] = []
    quota_status: str | None = None
    retry_count = 0
    for attempt in range(config.max_retries + 1):
        try:
            raw = call(build_prompt(rows, config.prompt_version))
            return {"results": parse_judge_response(raw, expected_ids), "request_count": 1, "retry_count": retry_count, "errors": errors}
        except (ProviderError, ValueError, json.JSONDecodeError) as exc:
            detail = str(exc)
            if isinstance(exc, ProviderError):
                detail += " " + exc.raw_response[:500]
            errors.append(type(exc).__name__ + ": " + detail[:800])
            quota_type = classify_quota_error(detail)
            if quota_type == "rpd":
                quota_status = "pending_quota_reset"
                break
            if quota_type == "tpm":
                quota_status = "reduce_batch_size"
                break
            if quota_type == "rpm" and attempt < config.max_retries:
                quota_status = "rpm_backoff"
                delay = backoff_seconds(quota_type, attempt)
                if os.environ.get("EVAL_ENABLE_BACKOFF", "false").lower() == "true":
                    time.sleep(delay)
            if attempt >= config.max_retries:
                break
            retry_count += 1
    return {"results": [], "request_count": 1 + retry_count, "retry_count": retry_count, "errors": errors, "status": quota_status or "judge_error", "recommended_batch_size": max(1, len(rows) // 2) if quota_status == "reduce_batch_size" else None}


def classify_quota_error(error: str) -> str | None:
    text = error.lower()
    if "resource_exhausted" not in text and "429" not in text and "quota" not in text:
        return None
    for quota_type in ("rpm", "rpd", "tpm"):
        if quota_type in text:
            return quota_type
    return "unknown"


def backoff_seconds(quota_type: str | None, attempt: int) -> int:
    if quota_type == "rpm":
        return (10, 20, 40)[min(attempt, 2)]
    return 0


def dynamic_batch_size(rows: list[dict[str, Any]], configured: int = 10, token_budget: int = 12000) -> int:
    """Keep requests bounded when a few cases contain unusually large text."""
    configured = max(1, min(configured, 10))
    if not rows:
        return configured
    estimated_chars = sum(len(json.dumps(row, ensure_ascii=False)) for row in rows)
    estimated_tokens = max(1, estimated_chars // 4)
    return max(1, min(configured, token_budget // max(1, estimated_tokens // len(rows))))
