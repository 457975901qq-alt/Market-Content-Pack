#!/usr/bin/env python3
"""Read-only deterministic second reviewer for a completed run."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.reviewer_models import ReviewCheck, ReviewDecision, ReviewResult, ReviewerInfo
from run_state import atomic_write_json, sha256


REVIEWER_PROMPT_VERSION = "market_content_reviewer_v1"
MAX_MODEL_REVIEW_ATTEMPTS = 3


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def review_run(run_id: str, output_root: Path, review_root: Path | None = None, qa_path: Path | None = None) -> dict[str, Any]:
    output_root = output_root.resolve()
    review_root = (review_root or Path("runtime/reviews") / run_id).resolve()
    content_path = output_root / "market_content" / "market_content.json"
    qa_path = qa_path or (output_root / "logs" / "qa_report.json")
    source_path = output_root / "market_sources" / "source_status.json"
    content = _load(content_path, {})
    qa = _load(qa_path, {})
    sources = _load(source_path, {})
    content_hash = sha256(content_path) if content_path.exists() else "0" * 64
    checks: list[ReviewCheck] = []
    critical: list[str] = []
    warnings: list[str] = []

    def check(name: str, result: str, evidence: list[Any], expected: Any, actual: Any, artifact: Path | None = None, remediation: str | None = None) -> None:
        nonlocal critical
        checks.append(ReviewCheck(check_name=name, result=result, evidence=evidence, artifact=str(artifact) if artifact else None, expected=expected, actual=actual, remediation_step=remediation))
        if result == "fail":
            critical.append(name)
        elif result == "warning":
            warnings.append(name)

    check("content_exists", "pass" if content_path.exists() and content else "fail", [str(content_path)], True, content_path.exists(), content_path, "generate_content")
    check("text_qa", "pass" if qa.get("status") == "pass" else "fail", [str(qa_path)], "pass", qa.get("status"), qa_path, "validate_content_consistency")
    source_count = sources.get("source_count", 0)
    preview = bool(content.get("preview_data")) or content.get("source_status") == "preview"
    if source_count == 0 and not preview:
        check("source_grounding", "fail", [str(source_path)], ">0", source_count, source_path, "collect_sources")
    elif source_count == 0:
        check("source_grounding", "warning", ["preview_data=true"], ">0 or preview", source_count, source_path, None)
    else:
        check("source_grounding", "pass", [str(source_path)], ">0", source_count, source_path, None)
    if not content.get("date") or not content.get("edition"):
        check("edition_metadata", "fail", [], "date and edition", {"date": content.get("date"), "edition": content.get("edition")}, content_path, "generate_content")
    else:
        check("edition_metadata", "pass", [], "date and edition", {"date": content.get("date"), "edition": content.get("edition")}, content_path, None)
    no_qualified_market_data = (
        not preview
        and not content.get("major_indexes")
        and not content.get("important_stocks")
        and "数据暂缺" in str(content.get("summary", ""))
    )
    if no_qualified_market_data:
        check("market_data_readiness", "fail", ["major_indexes=[]", "important_stocks=[]"], "at least one qualified market data group", "no qualified market data", content_path, "collect_market_quotes")
    else:
        check("market_data_readiness", "pass", [], "qualified market data or preview", "ready", content_path, None)

    decision = ReviewDecision.reject if critical else ReviewDecision.approve
    requested_tool = os.environ.get("REVIEWER_PROVIDER", "deterministic").strip().lower()
    generator_tool = os.environ.get("MARKET_CONTENT_PROVIDER", "").strip().lower()
    reviewer_tool = "deterministic_review_engine"
    reviewer_type = "deterministic"
    independence_warning = False
    if requested_tool in {"ollama", "gemini"}:
        try:
            from model_providers import ProviderError, call_gemini, call_ollama

            reviewer_type = "model"
            reviewer_tool = requested_tool
            independence_warning = requested_tool == generator_tool
            prompt_payload = {
                "prompt_version": REVIEWER_PROMPT_VERSION,
                "task": (
                    "Review only the supplied structured evidence. Return exactly one compact JSON object, "
                    "with lowercase decision approve, reject, or needs_review; numeric confidence; "
                    "and arrays critical_findings and warnings. No markdown, no prose outside JSON. "
                    "Use at most 3 findings and 3 warnings."
                ),
                "evidence": {
                    "content": content,
                    "qa": {"status": qa.get("status"), "page_count": len(qa.get("pages", [])) if isinstance(qa.get("pages"), list) else None},
                    "sources": {"source_count": sources.get("source_count"), "filtered_count": sources.get("filtered_count")},
                },
            }
            prompt = json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))
            model_result = None
            last_error: Exception | None = None
            for attempt in range(1, MAX_MODEL_REVIEW_ATTEMPTS + 1):
                try:
                    raw = call_gemini(prompt) if requested_tool == "gemini" else call_ollama(prompt)
                    candidate = json.loads(raw)
                    if not isinstance(candidate, dict):
                        raise ValueError("reviewer_payload_not_object")
                    normalized_decision = str(candidate.get("decision", "")).strip().lower()
                    if normalized_decision not in {"approve", "reject", "needs_review"}:
                        raise ValueError("reviewer_decision_invalid")
                    candidate["decision"] = normalized_decision
                    model_result = candidate
                    break
                except (ProviderError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt < MAX_MODEL_REVIEW_ATTEMPTS:
                        prompt = json.dumps({
                            "prompt_version": REVIEWER_PROMPT_VERSION,
                            "task": "Return only compact valid JSON. No markdown or commentary. Lowercase decision: approve, reject, or needs_review.",
                            "evidence": prompt_payload["evidence"],
                        }, ensure_ascii=False, separators=(",", ":"))
                        time.sleep(0.2)
            if model_result is None:
                raise ValueError(f"reviewer_model_parse_failed:{type(last_error).__name__ if last_error else 'unknown'}")
            decision = ReviewDecision(model_result["decision"])
            critical = [str(item) for item in model_result.get("critical_findings", [])]
            warnings = [str(item) for item in model_result.get("warnings", [])]
        except (ProviderError, OSError, ValueError, TypeError, json.JSONDecodeError):
            # A reviewer outage is never an implicit approval.
            reviewer_type = "deterministic"
            reviewer_tool = "deterministic_review_engine"
            decision = ReviewDecision.needs_review
            critical = ["reviewer_unavailable"]
    result = ReviewResult(
        run_id=run_id,
        content_hash=content_hash,
        reviewer=ReviewerInfo(type=reviewer_type, tool=reviewer_tool, version=REVIEWER_PROMPT_VERSION),
        decision=decision,
        confidence=0.98 if not critical else 0.95,
        critical_findings=critical,
        warnings=warnings,
        checks=checks,
        reviewed_at=datetime.now(timezone.utc),
    ).model_dump(mode="json")
    result["independence_warning"] = independence_warning
    atomic_write_json(review_root / "review_result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--review-root")
    parser.add_argument("--qa-path")
    args = parser.parse_args(argv)
    result = review_run(
        args.run_id,
        Path(args.output_root),
        Path(args.review_root) if args.review_root else None,
        Path(args.qa_path) if args.qa_path else None,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["decision"] == "approve" else 1


if __name__ == "__main__":
    raise SystemExit(main())
