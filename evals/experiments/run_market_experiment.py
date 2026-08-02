from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.datasets.loader import dataset_version, load_dataset
from evals.evaluators.deterministic import evaluate_case
from evals.evaluators.llm_judges import JudgeConfig, deterministic_judge_rows, dynamic_batch_size, judge_batch
from evals.phoenix_adapter import create_dataset_and_experiment

ROOT = Path(__file__).resolve().parents[2]
HARD_METRICS = ("schema_completeness", "source_grounding", "ticker_validity", "temporal_consistency", "forbidden_claim_check")
CANDIDATES = ("current_ollama", "new_ollama_prompt", "gemini", "local_template")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _resolve_dataset(value: str) -> Path:
    candidate = Path(value)
    if candidate.exists():
        return candidate
    packaged = ROOT / "evals" / "datasets" / value
    if packaged.exists():
        return packaged
    raise FileNotFoundError(f"Evaluation dataset not found: {value}")


def _candidate_output(case: dict[str, Any], candidate: str) -> dict[str, Any]:
    payload = dict(case.get("input", {}))
    category = case.get("metadata", {}).get("category")
    payload["candidate"] = candidate
    payload["text"] = str(payload.get("text", ""))
    if category in {"ollama_output_anomaly", "gemini_fallback", "market_data_missing", "image_qa_renderer_failure"}:
        payload["delivery_allowed"] = False
    if candidate == "local_template":
        payload["text"] = f"{case.get('reference', {}).get('expected_theme', 'market_observation')}：数据暂缺，仅作信息整理。"
    return payload


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _cache_key(dataset_version_value: str, case_id: str, candidate: str, output_hash: str, provider: str, model: str, prompt_version: str) -> str:
    raw = "|".join((dataset_version_value, case_id, candidate, output_hash, provider, model, prompt_version))
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"completed_cases": [], "completed_candidates": [], "requests": {"total": 0, "success": 0, "429": 0}, "pending_retry": [], "cache_hits": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"completed_cases": [], "completed_candidates": [], "requests": {"total": 0, "success": 0, "429": 0}, "pending_retry": [], "cache_hits": 0, "checkpoint_recovered": False}


def _average(rows: list[dict[str, Any]], metric: str) -> float | None:
    scores = [float(row[metric]["score"]) for row in rows if isinstance(row.get(metric), dict) and isinstance(row[metric].get("score"), (int, float))]
    return round(sum(scores) / len(scores), 4) if scores else None


def select_review_cases(rows: list[dict[str, Any]], max_cases: int = 20) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        fact = float(row.get("factual_faithfulness", {}).get("score", 1.0))
        usability = float(row.get("content_usability", {}).get("score", 1.0))
        if 0.80 <= fact <= 0.95 or 0.65 <= usability <= 0.85 or row.get("unstable_local_judge") or row.get("deterministic_fact_issue"):
            selected[str(row["case_id"] + "::" + row.get("candidate", ""))] = row
    return list(selected.values())[:max_cases]


def _judge_rows(rows: list[dict[str, Any]], config: JudgeConfig, cache_dir: Path, dataset_version_value: str, checkpoint: dict[str, Any], mock: bool = False, checkpoint_path: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    completed = {str(key) for key in checkpoint.get("completed_candidates", [])}
    results: list[dict[str, Any]] = []
    stats = {"request_count": 0, "success_count": 0, "cache_hits": 0, "judge_errors": 0, "quota_429": 0, "pending_quota_review": 0}
    uncached: list[dict[str, Any]] = []
    for row in rows:
        key_id = f"{row['case_id']}::{row.get('candidate', '')}"
        output_hash = _hash_payload(row.get("candidate_output", {}))
        key = _cache_key(dataset_version_value, str(row["case_id"]), str(row.get("candidate", "")), output_hash, config.provider, config.model, config.prompt_version)
        path = cache_dir / f"{key}.json"
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                cached["cache_hit"] = True
                results.append(cached)
                stats["cache_hits"] += 1
                completed.add(key_id)
                continue
            except (OSError, json.JSONDecodeError):
                pass
        uncached.append({"case_id": row["case_id"], "candidate": row.get("candidate", ""), "deterministic_score": row.get("deterministic_score", 1.0), "evidence": row.get("evidence", [])})
    configured_batch = max(1, min(int(os.environ.get("EVAL_JUDGE_BATCH_SIZE", "10")), 10))
    start = 0
    while start < len(uncached):
        batch_size = dynamic_batch_size(uncached[start:], configured=configured_batch)
        batch = uncached[start:start + batch_size]
        if mock:
            judged = {"results": deterministic_judge_rows(batch), "request_count": 1, "retry_count": 0, "errors": []}
        else:
            judged = judge_batch(batch, config)
        stats["request_count"] += judged.get("request_count", 0)
        if judged.get("status") in {"pending_quota_reset", "rpm_backoff", "reduce_batch_size"}:
            stats["quota_429"] += 1
        if judged.get("status") == "pending_quota_reset":
            stats["pending_quota_review"] += len(batch)
            checkpoint.setdefault("pending_retry", []).extend([f"{item['case_id']}::{item.get('candidate', '')}" for item in batch])
            checkpoint["pending_retry"] = sorted(set(checkpoint["pending_retry"]))
            checkpoint["requests"]["429"] = checkpoint["requests"].get("429", 0) + 1
            checkpoint["updated_at"] = _now()
            if checkpoint_path is not None:
                _write_json(checkpoint_path, checkpoint)
            break
        if judged.get("status") == "reduce_batch_size" and batch_size > 1:
            configured_batch = max(1, int(judged.get("recommended_batch_size") or batch_size // 2))
            checkpoint["requests"]["429"] = checkpoint["requests"].get("429", 0) + 1
            checkpoint["updated_at"] = _now()
            if checkpoint_path is not None:
                _write_json(checkpoint_path, checkpoint)
            continue
        if judged.get("status") == "judge_error":
            stats["judge_errors"] += len(batch)
        for item in judged.get("results", []):
            item["judge_provider"] = config.provider
            item["judge_model"] = config.model
            item["judge_prompt_version"] = config.prompt_version
            item["cache_hit"] = False
            match = next((source for source in batch if source["case_id"] == item.get("case_id") and source.get("candidate") == item.get("candidate")), None)
            if match:
                key = _cache_key(dataset_version_value, str(match["case_id"]), str(match.get("candidate", "")), _hash_payload(next((r.get("candidate_output", {}) for r in rows if r["case_id"] == match["case_id"] and r.get("candidate") == match.get("candidate")), {})), config.provider, config.model, config.prompt_version)
                _write_json(cache_dir / f"{key}.json", item)
                completed.add(f"{item['case_id']}::{item.get('candidate', '')}")
            results.append(item)
        checkpoint["completed_candidates"] = sorted(completed)
        checkpoint["requests"]["total"] = checkpoint["requests"].get("total", 0) + judged.get("request_count", 0)
        checkpoint["requests"]["success"] = checkpoint["requests"].get("success", 0) + (1 if judged.get("results") else 0)
        checkpoint["updated_at"] = _now()
        if checkpoint_path is not None:
            _write_json(checkpoint_path, checkpoint)
        start += len(batch)
    stats["pending_quota_review"] += len(checkpoint.get("pending_retry", []))
    return results, stats


def _aggregate_deterministic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for candidate in sorted({str(row["candidate"]) for row in rows}):
        subset = [row for row in rows if row["candidate"] == candidate]
        output[candidate] = {metric: round(sum(float(row["deterministic"][metric]["score"]) for row in subset) / len(subset), 4) if subset else 0.0 for metric in (*HARD_METRICS, "delivery_decision_accuracy")}
        output[candidate]["hard_pass_rate"] = round(sum(all(row["deterministic"][metric]["score"] == 1.0 for metric in HARD_METRICS) for row in subset) / len(subset), 4) if subset else 0.0
    return output


def _aggregate_judge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for candidate in sorted({str(row.get("candidate", "")) for row in rows}):
        subset = [row for row in rows if row.get("candidate") == candidate]
        output[candidate] = {metric: _average(subset, metric) for metric in ("factual_faithfulness", "content_usability")}
    return output


def _threshold_status(deterministic: dict[str, Any], judge_summary: dict[str, Any], candidate: str) -> dict[str, Any]:
    hard = deterministic.get(candidate, {})
    judge = judge_summary.get(candidate, {})
    hard_passed = all(hard.get(metric) == 1.0 for metric in HARD_METRICS)
    factual = judge.get("factual_faithfulness")
    usability = judge.get("content_usability")
    return {"hard_passed": hard_passed, "factual_passed": factual is not None and factual >= 0.90, "usability_passed": usability is not None and usability >= 0.75, "eligible": hard_passed and factual is not None and usability is not None and factual >= 0.90 and usability >= 0.75}


def run(dataset: Path, candidate: str = "local_template", limit: int | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run the offline experiment. The original single-candidate API remains supported."""
    cases = load_dataset(dataset)
    if kwargs.get("case_id"):
        cases = [case for case in cases if case.get("case_id") == kwargs["case_id"]]
    if limit:
        cases = cases[:limit]
    candidates = list(kwargs.get("candidates") or ([candidate] if candidate else ["local_template"]))
    rows: list[dict[str, Any]] = []
    for case in cases:
        for selected in candidates:
            output = _candidate_output(case, selected)
            deterministic = evaluate_case(case, output)
            score = min(deterministic[name]["score"] for name in HARD_METRICS)
            rows.append({"case_id": case["case_id"], "candidate": selected, "candidate_output": output, "deterministic": deterministic, "deterministic_score": score, "evidence": [item for result in deterministic.values() for item in result.get("evidence", [])]})
    return {"experiment_name": "market_content_offline", "candidate": candidate, "dataset": str(dataset), "case_count": len(cases), "rows": rows, "delivered": False, "created_at": _now()}


def run_full(dataset: Path, candidates: list[str], limit: int | None, judge: str, skip_llm: bool, review_only: bool, max_requests: int | None, resume: bool, output: Path, mock_judge: bool = False) -> dict[str, Any]:
    base = run(dataset, candidates=candidates, limit=limit)
    ds_version = dataset_version(dataset, [])
    experiment_id = hashlib.sha256(f"{ds_version}|{'|'.join(candidates)}".encode()).hexdigest()[:16]
    checkpoint_path = ROOT / "evals" / "checkpoints" / f"{experiment_id}.json"
    checkpoint = _load_checkpoint(checkpoint_path) if resume else _load_checkpoint(Path("/dev/null"))
    checkpoint.update({"experiment_id": experiment_id, "dataset_version": ds_version, "judge_provider": judge, "updated_at": _now()})
    cache_dir = ROOT / "evals" / "cache" / "judge"
    deterministic_summary = _aggregate_deterministic(base["rows"])
    ollama_rows: list[dict[str, Any]] = []
    gemini_rows: list[dict[str, Any]] = []
    if not skip_llm and judge in {"ollama", "auto"} and not review_only:
        config = JudgeConfig("ollama", os.environ.get("EVAL_OLLAMA_MODEL", "qwen3.5:9b"))
        ollama_rows, ollama_stats = _judge_rows(base["rows"], config, cache_dir, ds_version, checkpoint, mock=mock_judge, checkpoint_path=checkpoint_path)
    else:
        ollama_stats = {"request_count": 0, "cache_hits": 0, "judge_errors": 0, "quota_429": 0, "pending_quota_review": 0}
    review_queue = select_review_cases(ollama_rows) if ollama_rows else []
    review_provider_enabled = os.environ.get("EVAL_REVIEW_JUDGE", "gemini").strip().lower() == "gemini"
    if not skip_llm and review_provider_enabled and judge in {"ollama", "auto"}:
        review_queue = select_review_cases(ollama_rows)
    elif not skip_llm and judge in {"gemini"}:
        review_queue = base["rows"]
    else:
        review_queue = []
    if not skip_llm and judge in {"gemini", "auto"}:
        review_queue = review_queue or base["rows"][:max_requests or int(os.environ.get("EVAL_GEMINI_MAX_REQUESTS_PER_RUN", "10"))]
        quota = max_requests if max_requests is not None else int(os.environ.get("EVAL_GEMINI_MAX_REQUESTS_PER_RUN", "10"))
        review_queue = review_queue[:quota * int(os.environ.get("EVAL_JUDGE_BATCH_SIZE", "10"))]
        config = JudgeConfig("gemini", os.environ.get("EVAL_GEMINI_MODEL", "gemini-3.5-flash"))
        gemini_rows, gemini_stats = _judge_rows(review_queue, config, cache_dir, ds_version, checkpoint, mock=mock_judge, checkpoint_path=checkpoint_path)
    elif not skip_llm and review_provider_enabled and judge in {"ollama", "auto"}:
        quota = max_requests if max_requests is not None else int(os.environ.get("EVAL_GEMINI_MAX_REQUESTS_PER_RUN", "10"))
        batch_size = int(os.environ.get("EVAL_JUDGE_BATCH_SIZE", "10"))
        review_queue = review_queue[:quota * max(1, min(batch_size, 10))]
        config = JudgeConfig("gemini", os.environ.get("EVAL_GEMINI_MODEL", "gemini-3.5-flash"))
        gemini_rows, gemini_stats = _judge_rows(review_queue, config, cache_dir, ds_version, checkpoint, mock=mock_judge, checkpoint_path=checkpoint_path)
    else:
        gemini_stats = {"request_count": 0, "cache_hits": 0, "judge_errors": 0, "quota_429": 0, "pending_quota_review": 0}
    checkpoint["completed_cases"] = sorted({row["case_id"] for row in base["rows"]})
    checkpoint["cache_hits"] = ollama_stats.get("cache_hits", 0) + gemini_stats.get("cache_hits", 0)
    checkpoint["pending_retry"] = sorted(set(checkpoint.get("pending_retry", [])))
    _write_json(checkpoint_path, checkpoint)
    ollama_summary = _aggregate_judge(ollama_rows)
    gemini_summary = _aggregate_judge(gemini_rows)
    threshold_status = {candidate: _threshold_status(deterministic_summary, gemini_summary.get(candidate) and {candidate: gemini_summary[candidate]} or ollama_summary, candidate) for candidate in candidates}
    gemini_coverage = round(len(gemini_rows) / max(len(base["rows"]), 1), 4)
    report = {"experiment_name": base["experiment_name"], "experiment_id": experiment_id, "dataset": str(dataset), "dataset_version": ds_version, "candidates": candidates, "case_count": len(cases := load_dataset(dataset)[:limit] if limit else load_dataset(dataset)), "deterministic": deterministic_summary, "ollama_judge": {"rows": ollama_rows, "summary": ollama_summary, "request_count": ollama_stats.get("request_count", 0), "cache_hit_count": ollama_stats.get("cache_hits", 0), "judge_error_count": ollama_stats.get("judge_errors", 0), "quota_429_count": ollama_stats.get("quota_429", 0)}, "gemini_review": {"rows": gemini_rows, "summary": gemini_summary, "request_count": gemini_stats.get("request_count", 0), "cache_hit_count": gemini_stats.get("cache_hits", 0), "judge_error_count": gemini_stats.get("judge_errors", 0), "quota_429_count": gemini_stats.get("quota_429", 0), "coverage": gemini_coverage}, "threshold_status": threshold_status, "pending_review_count": max(0, len(base["rows"]) - len(gemini_rows)) + len(checkpoint.get("pending_retry", [])), "legacy_estimated_requests": len(base["rows"]) * 2, "optimized_request_count": ollama_stats.get("request_count", 0) + gemini_stats.get("request_count", 0), "checkpoint": str(checkpoint_path), "ranking_status": "final" if gemini_coverage == 1.0 and gemini_rows and not gemini_stats.get("judge_errors") and not checkpoint.get("pending_retry") else "provisional_ranking", "thresholds": {"hard": 1.0, "factual_faithfulness": 0.90, "content_usability": 0.75}, "phoenix": create_dataset_and_experiment("market_content_v1", cases, base), "delivered": False, "created_at": _now()}
    _write_json(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline quota-aware market content evaluation")
    parser.add_argument("--dataset", default="market_content_v1")
    parser.add_argument("--candidate", default="local_template")
    parser.add_argument("--all-candidates", action="store_true")
    parser.add_argument("--judge", choices=("none", "ollama", "gemini", "auto"), default=os.environ.get("EVAL_PRIMARY_JUDGE", "ollama"))
    parser.add_argument("--review-only", action="store_true")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id")
    parser.add_argument("--skip-llm-judge", action="store_true")
    parser.add_argument("--mock-judge", action="store_true")
    parser.add_argument("--output-report")
    args = parser.parse_args(argv)
    dataset = _resolve_dataset(args.dataset)
    candidates = list(CANDIDATES if args.all_candidates or args.review_only else [args.candidate])
    output = Path(args.output_report) if args.output_report else ROOT / "evals" / "reports" / f"{args.candidate}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report = run_full(dataset, candidates, args.limit, args.judge, args.skip_llm_judge or args.judge == "none", args.review_only, args.max_requests, args.resume, output, args.mock_judge)
    print(json.dumps({"report": str(output), "case_count": report["case_count"], "candidates": candidates, "ranking_status": report["ranking_status"], "delivered": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
