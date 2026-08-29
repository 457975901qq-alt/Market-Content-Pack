"""Offline regression evaluation, quality trends and release gating.

This module never calls a provider, changes a production artifact, or enables
delivery.  Regression candidates are deterministic fixtures; real run history
is read from existing run summaries/manifests and persisted to a separate
quality SQLite database.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import statistics
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from quality_store import QualityStore
from security import AuditLogger, SecurityError, authorize


ROOT = Path(__file__).resolve().parents[1]
TOKYO = ZoneInfo("Asia/Tokyo")
CASES_PATH = ROOT / "tests" / "regression" / "cases.json"
QUALITY_ROOT = ROOT / "outputs" / "quality"
PIPELINE_VERSION = "6.2.0"
BASELINE_VERSION = "1.0.0"
MIN_TREND_SAMPLE = 5


def _quality_root(root: Path) -> Path:
    return root / "outputs" / "quality"

HARD_FACT_FIELDS = (
    "target_date", "session", "data_cutoff", "required_symbols", "facts",
    "gold_instrument", "gold_unit", "gold_value", "gold_disclosure",
)
STRUCTURE_FIELDS = ("structure",)
GATE_FIELDS = ("schema_passed", "reviewer_passed", "p0_blocked", "delivery_attempted")


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_cases(root: Path = ROOT) -> list[dict[str, Any]]:
    payload = _read(root / "tests" / "regression" / "cases.json", {})
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise RuntimeError("regression_cases_missing")
    return [item for item in cases if isinstance(item, dict) and item.get("case_id")]


def _case_by_id(cases: Iterable[dict[str, Any]], case_id: str) -> dict[str, Any]:
    for case in cases:
        if case.get("case_id") == case_id:
            return case
    raise ValueError(f"regression_case_not_found:{case_id}")


def load_baseline(case: dict[str, Any], root: Path = ROOT) -> dict[str, Any] | None:
    filename = case.get("baseline_file")
    if not filename:
        return None
    path = root / "tests" / "regression" / filename
    baseline = _read(path)
    if not isinstance(baseline, dict) or baseline.get("case_id") != case.get("case_id"):
        raise RuntimeError(f"invalid_baseline:{path}")
    return baseline


def _git_info(root: Path) -> tuple[str | None, str | None]:
    def run(args: list[str]) -> str | None:
        try:
            result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=5, check=False)
            return result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return run(["git", "rev-parse", "HEAD"]), run(["git", "branch", "--show-current"])


def _add_regression(regressions: list[dict[str, Any]], severity: str, code: str, path: str, expected: Any, actual: Any, message: str) -> None:
    regressions.append({"severity": severity, "code": code, "path": path, "expected": expected, "actual": actual, "message": message})


def _fact_match_rate(expected: dict[str, Any], candidate: dict[str, Any]) -> float | None:
    facts = expected.get("facts")
    if not isinstance(facts, list):
        return None
    if not facts:
        return 1.0
    actual = candidate.get("facts") if isinstance(candidate.get("facts"), list) else []
    return round(sum(1 for item in facts if item in actual) / len(facts), 4)


def _compare_normal(case: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, float | None]:
    expected = baseline.get("expected") or {}
    regressions: list[dict[str, Any]] = []
    fact_rate = _fact_match_rate(expected, candidate)
    for field in HARD_FACT_FIELDS:
        if field not in expected:
            continue
        if candidate.get(field) != expected.get(field):
            severity = "CRITICAL"
            if field in {"gold_instrument", "gold_unit", "gold_value", "gold_disclosure"}:
                code = "GOLD_INSTRUMENT_ERROR"
            elif field == "target_date":
                code = "INPUT_DATE_MISMATCH"
            elif field == "session":
                code = "INPUT_SESSION_MISMATCH"
            else:
                code = "HARD_FACT_CHANGED"
            _add_regression(regressions, severity, code, field, expected.get(field), candidate.get(field), "hard fact changed")
    for field in GATE_FIELDS:
        if field not in expected:
            continue
        if candidate.get(field) != expected.get(field):
            severity = "CRITICAL" if field in {"p0_blocked", "delivery_attempted"} else "HIGH"
            code = "QUALITY_GATE_CHANGED"
            _add_regression(regressions, severity, code, field, expected.get(field), candidate.get(field), "quality gate changed")
    if candidate.get("status") != expected.get("status"):
        _add_regression(regressions, "HIGH", "NORMAL_CASE_FAILED", "status", expected.get("status"), candidate.get("status"), "normal case no longer passes")
    if candidate.get("structure") != expected.get("structure"):
        _add_regression(regressions, "MEDIUM", "STRUCTURE_CHANGED", "structure", expected.get("structure"), candidate.get("structure"), "output structure changed")
    if isinstance(expected.get("source_count"), (int, float)) and float(candidate.get("source_count", 0)) < float(expected["source_count"]):
        _add_regression(regressions, "MEDIUM", "SOURCE_COVERAGE_DOWN", "source_count", expected.get("source_count"), candidate.get("source_count"), "source count decreased")
    expected_duration = expected.get("pipeline_duration_seconds")
    actual_duration = candidate.get("pipeline_duration_seconds")
    if isinstance(expected_duration, (int, float)) and isinstance(actual_duration, (int, float)) and actual_duration > expected_duration * 1.30:
        _add_regression(regressions, "LOW", "DURATION_INCREASED", "pipeline_duration_seconds", expected_duration, actual_duration, "pipeline duration increased over 30 percent")
    if candidate.get("retry_count", 0) > expected.get("retry_count", 0):
        _add_regression(regressions, "MEDIUM", "RETRY_INCREASED", "retry_count", expected.get("retry_count", 0), candidate.get("retry_count"), "retry count increased")
    if candidate.get("fallback_count", 0) > expected.get("fallback_count", 0):
        _add_regression(regressions, "MEDIUM", "FALLBACK_INCREASED", "fallback_count", expected.get("fallback_count", 0), candidate.get("fallback_count"), "fallback use increased")
    if candidate.get("text") != expected.get("text"):
        _add_regression(regressions, "LOW", "TEXT_CHANGED", "text", expected.get("text"), candidate.get("text"), "non-hard text changed")
    hard_gate = not any(item["severity"] == "CRITICAL" for item in regressions)
    return regressions, hard_gate, fact_rate


def _compare_failure(case: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, float | None]:
    expected_code = case.get("expected_error_code")
    regressions: list[dict[str, Any]] = []
    if candidate.get("status") != "failed":
        _add_regression(regressions, "CRITICAL", "FAILURE_NOT_BLOCKED", "status", "failed", candidate.get("status"), "known failure did not fail closed")
    if candidate.get("error_code") != expected_code:
        _add_regression(regressions, "CRITICAL", "ERROR_CODE_CHANGED", "error_code", expected_code, candidate.get("error_code"), "failure classification changed")
    if candidate.get("delivery_attempted") is not False:
        _add_regression(regressions, "CRITICAL", "P0_DELIVERY_NOT_BLOCKED", "delivery_attempted", False, candidate.get("delivery_attempted"), "failure reached delivery")
    if case.get("category") in {"input", "cross_validation", "gold_instrument"} and candidate.get("p0_blocked") is not True:
        _add_regression(regressions, "CRITICAL", "P0_NOT_BLOCKED", "p0_blocked", True, candidate.get("p0_blocked"), "P0 safety boundary was not enforced")
    return regressions, not regressions, None


def score_case(case: dict[str, Any], root: Path = ROOT, candidate_override: dict[str, Any] | None = None) -> dict[str, Any]:
    candidate = copy.deepcopy(candidate_override or case.get("candidate") or {})
    started = datetime.now(TOKYO)
    baseline = load_baseline(case, root)
    if case.get("kind") == "normal":
        if baseline is None:
            raise RuntimeError(f"normal_case_baseline_missing:{case.get('case_id')}")
        regressions, hard_gate, fact_rate = _compare_normal(case, baseline, candidate)
        expected = baseline.get("expected") or {}
    else:
        regressions, hard_gate, fact_rate = _compare_failure(case, candidate)
        expected = {"status": "failed", "error_code": case.get("expected_error_code")}
    schema_passed = candidate.get("schema_passed")
    reviewer_passed = candidate.get("reviewer_passed")
    if case.get("kind") == "normal":
        structure_score = 1.0 if candidate.get("structure") == expected.get("structure") and schema_passed is True else 0.0
        review_values = [value for value in (reviewer_passed,) if isinstance(value, bool)]
        review_score = sum(review_values) / len(review_values) if review_values else 0.0
        source_score = min(1.0, float(candidate.get("source_count", 0)) / max(1.0, float(expected.get("source_count", 1))))
        expected_duration = float(expected.get("pipeline_duration_seconds") or 1)
        actual_duration = float(candidate.get("pipeline_duration_seconds") or expected_duration)
        performance_score = min(1.0, expected_duration / actual_duration) if actual_duration > 0 else 0.0
        dimensions = {
            "fact_accuracy": round(40 * (fact_rate if fact_rate is not None else 0.0), 2),
            "schema_structure": round(20 * structure_score, 2),
            "review_and_qa": round(20 * review_score, 2),
            "source_coverage": round(10 * source_score, 2),
            "performance": round(10 * performance_score, 2),
        }
    else:
        dimensions = {"fact_accuracy": 40.0 if hard_gate else 0.0, "schema_structure": 20.0 if hard_gate else 0.0, "review_and_qa": 20.0 if hard_gate else 0.0, "source_coverage": 10.0 if hard_gate else 0.0, "performance": 10.0 if hard_gate else 0.0}
    score = round(sum(dimensions.values()), 2)
    status = "passed" if hard_gate and score >= 90 else "failed"
    finished = datetime.now(TOKYO)
    return {
        "case_id": case["case_id"], "kind": case.get("kind"), "category": case.get("category"),
        "status": status, "score": score, "hard_gate_passed": hard_gate,
        "dimensions": dimensions, "regressions": regressions,
        "fact_match_rate": fact_rate, "schema_passed": schema_passed,
        "reviewer_passed": reviewer_passed, "retry_count": candidate.get("retry_count", 0),
        "fallback_count": candidate.get("fallback_count", 0), "duration_seconds": candidate.get("pipeline_duration_seconds"),
        "error_code": candidate.get("error_code"), "evaluated_at": finished.isoformat(),
        "evaluation_duration_seconds": round((finished - started).total_seconds(), 6),
    }


def _policy(root: Path = ROOT) -> dict[str, Any]:
    payload = _read(root / "config" / "evaluation_policy.json", {})
    return payload if isinstance(payload, dict) else {}


def build_release_gate(case_results: list[dict[str, Any]], baseline_version: str = BASELINE_VERSION, candidate_version: str = PIPELINE_VERSION, root: Path = ROOT) -> dict[str, Any]:
    policy = _policy(root).get("quality_gate") or {}
    critical = sum(sum(1 for item in result.get("regressions", []) if item.get("severity") == "CRITICAL") for result in case_results)
    high = sum(sum(1 for item in result.get("regressions", []) if item.get("severity") == "HIGH") for result in case_results)
    p0 = sum(1 for result in case_results for item in result.get("regressions", []) if item.get("code") in {"HARD_FACT_CHANGED", "INPUT_DATE_MISMATCH", "INPUT_SESSION_MISMATCH", "P0_DELIVERY_NOT_BLOCKED", "P0_NOT_BLOCKED", "GOLD_INSTRUMENT_ERROR"})
    gold_errors = sum(1 for result in case_results for item in result.get("regressions", []) if item.get("code") == "GOLD_INSTRUMENT_ERROR")
    wrong_session = sum(1 for result in case_results for item in result.get("regressions", []) if item.get("code") == "INPUT_SESSION_MISMATCH")
    stale_input = sum(1 for result in case_results for item in result.get("regressions", []) if item.get("code") in {"INPUT_DATE_MISMATCH", "STALE_INPUT"})
    passed = sum(result.get("status") == "passed" for result in case_results)
    scores = [float(result["score"]) for result in case_results if isinstance(result.get("score"), (int, float))]
    def ratio(field: str) -> tuple[float | None, int]:
        values = [result[field] for result in case_results if isinstance(result.get(field), bool)]
        return (round(sum(values) / len(values), 4), len(values)) if values else (None, 0)
    fact_values = [result["fact_match_rate"] for result in case_results if isinstance(result.get("fact_match_rate"), (int, float))]
    fact_rate = (round(sum(fact_values) / len(fact_values), 4), len(fact_values)) if fact_values else (None, 0)
    schema_rate, schema_n = ratio("schema_passed")
    reviewer_rate, reviewer_n = ratio("reviewer_passed")
    reasons: list[dict[str, Any]] = []
    def require(rule: str, expected: Any, actual: Any, condition: bool) -> None:
        if condition:
            reasons.append({"rule": rule, "expected": expected, "actual": actual})
    require("minimum_total_score", policy.get("minimum_total_score", 90), round(statistics.mean(scores), 2) if scores else None, not scores or statistics.mean(scores) < float(policy.get("minimum_total_score", 90)))
    require("maximum_critical_regressions", policy.get("maximum_critical_regressions", 0), critical, critical > int(policy.get("maximum_critical_regressions", 0)))
    require("maximum_high_regressions", policy.get("maximum_high_regressions", 0), high, high > int(policy.get("maximum_high_regressions", 0)))
    require("maximum_p0_errors", policy.get("maximum_p0_errors", 0), p0, p0 > int(policy.get("maximum_p0_errors", 0)))
    require("maximum_gold_instrument_errors", policy.get("maximum_gold_instrument_errors", 0), gold_errors, gold_errors > int(policy.get("maximum_gold_instrument_errors", 0)))
    require("maximum_wrong_session_errors", policy.get("maximum_wrong_session_errors", 0), wrong_session, wrong_session > int(policy.get("maximum_wrong_session_errors", 0)))
    require("maximum_stale_input_errors", policy.get("maximum_stale_input_errors", 0), stale_input, stale_input > int(policy.get("maximum_stale_input_errors", 0)))
    require("minimum_fact_match_rate", policy.get("minimum_fact_match_rate", 1.0), fact_rate[0], fact_rate[0] is not None and fact_rate[0] < float(policy.get("minimum_fact_match_rate", 1.0)))
    require("minimum_schema_pass_rate", policy.get("minimum_schema_pass_rate", 1.0), schema_rate, schema_rate is not None and schema_rate < float(policy.get("minimum_schema_pass_rate", 1.0)))
    require("minimum_reviewer_pass_rate", policy.get("minimum_reviewer_pass_rate", 0.95), reviewer_rate, reviewer_rate is not None and reviewer_rate < float(policy.get("minimum_reviewer_pass_rate", 0.95)))
    insufficient = len(case_results) < MIN_TREND_SAMPLE or any(count and count < MIN_TREND_SAMPLE for count in (fact_rate[1], schema_n, reviewer_n) if count)
    status = "blocked" if reasons else ("warning" if insufficient else "passed")
    return {
        "status": status, "candidate_version": candidate_version, "baseline_version": baseline_version,
        "total_cases": len(case_results), "passed_cases": passed, "failed_cases": len(case_results) - passed,
        "score": round(statistics.mean(scores), 2) if scores else None, "hard_gate_passed": not any(not result.get("hard_gate_passed") for result in case_results),
        "sample_size": {"total": len(case_results), "fact_match": fact_rate[1], "schema": schema_n, "reviewer": reviewer_n},
        "insufficient_sample_size": insufficient, "critical_regressions": critical, "high_regressions": high,
        "p0_errors": p0, "gold_instrument_errors": gold_errors, "wrong_session_errors": wrong_session,
        "stale_input_errors": stale_input, "reasons": reasons,
        "generated_at": datetime.now(TOKYO).isoformat(),
    }


def _render_regression_md(report: dict[str, Any]) -> str:
    gate = report.get("release_gate") or {}
    lines = ["# Regression Report", "", f"- Candidate：`{report.get('candidate_version')}`", f"- Baseline：`{report.get('baseline_version')}`", f"- Cases：{report.get('total_cases', 0)}", f"- Passed：{report.get('passed_cases', 0)}", f"- Failed：{report.get('failed_cases', 0)}", f"- Release gate：**{gate.get('status', 'unknown')}**", "", "## Case Results", "", "| Case | Status | Score | Regressions |", "|---|---:|---:|---:|"]
    for result in report.get("case_results", []):
        lines.append(f"| {result['case_id']} | {result['status']} | {result.get('score')} | {len(result.get('regressions', []))} |")
    if gate.get("reasons"):
        lines.extend(["", "## Gate Reasons", ""])
        lines.extend(f"- `{item['rule']}`：expected `{item['expected']}`, actual `{item['actual']}`" for item in gate["reasons"])
    return "\n".join(lines) + "\n"


def run_regression(root: Path = ROOT, case_id: str | None = None, baseline_version: str = BASELINE_VERSION, candidate_version: str = PIPELINE_VERSION) -> dict[str, Any]:
    cases = load_cases(root)
    selected = [_case_by_id(cases, case_id)] if case_id else cases
    started = datetime.now(TOKYO)
    results = [score_case(case, root) for case in selected]
    finished = datetime.now(TOKYO)
    gate = build_release_gate(results, baseline_version, candidate_version, root)
    git_commit, branch = _git_info(root)
    regression_run_id = f"regression_{started.strftime('%Y%m%d_%H%M')}_{uuid.uuid4().hex[:6]}"
    report = {
        "regression_run_id": regression_run_id, "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "pipeline_version": candidate_version, "baseline_version": baseline_version, "git_commit": git_commit, "branch": branch,
        "total_cases": len(results), "passed_cases": sum(item["status"] == "passed" for item in results),
        "failed_cases": sum(item["status"] != "passed" for item in results), "case_results": results, "release_gate": gate,
    }
    quality_root = _quality_root(root)
    quality_root.mkdir(parents=True, exist_ok=True)
    _write(quality_root / "latest_regression_report.json", report)
    _write(quality_root / "regression_report.json", report)
    _write(quality_root / "regression_summary.json", {
        "regression_run_id": regression_run_id,
        "pipeline_version": candidate_version,
        "baseline_version": baseline_version,
        "total_cases": report["total_cases"],
        "passed_cases": report["passed_cases"],
        "failed_cases": report["failed_cases"],
        "score": gate.get("score"),
        "release_gate_status": gate.get("status"),
        "generated_at": finished.isoformat(),
    })
    (quality_root / "latest_regression_report.md").write_text(_render_regression_md(report), encoding="utf-8")
    (quality_root / "regression_report.md").write_text(_render_regression_md(report), encoding="utf-8")
    _write(quality_root / "release_gate.json", gate)
    store = QualityStore(root / "runtime" / "quality.sqlite3")
    store.record_run({**report, "score": gate.get("score"), "hard_gate_passed": gate.get("hard_gate_passed"), "release_gate_status": gate.get("status")})
    for result in results:
        store.record_case(regression_run_id, result)
    return report


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=TOKYO) if parsed.tzinfo is None else parsed).astimezone(TOKYO)
    except ValueError:
        return None


def _state_reviewer_passed(manifest: dict[str, Any], root: Path) -> bool | None:
    state_path = manifest.get("state_path")
    if not state_path:
        return None
    state = _read(Path(state_path), {})
    if not isinstance(state, dict):
        return None
    step = (state.get("steps") or {}).get("reviewer_gate")
    return step.get("status") == "success" if isinstance(step, dict) else None


def _load_run_metrics(record: dict[str, Any], root: Path) -> dict[str, Any]:
    metrics_path = record.get("metrics_path")
    if not metrics_path:
        return {}
    path = Path(str(metrics_path))
    if not path.is_absolute():
        path = root / path
    payload = _read(path, {})
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    return metrics if isinstance(metrics, dict) else {}


def _merge_run_metrics(record: dict[str, Any], root: Path) -> dict[str, Any]:
    metrics = _load_run_metrics(record, root)
    if not metrics:
        return record
    for target, source in (
        ("retry_count", "retry_total"),
        ("timeout_count", "timeout_total"),
        ("fallback_count", "fallback_total"),
        ("source_count", "source_count"),
        ("source_failure_count", "source_failure_count"),
    ):
        if source in metrics:
            record[target] = metrics[source]
    if "pipeline_duration_seconds" in metrics and "duration_seconds" not in record:
        record["duration_seconds"] = metrics["pipeline_duration_seconds"]
    record["checkpoint_resumed"] = bool(record.get("checkpoint_resumed") or metrics.get("checkpoint_resume_total", 0))
    record["stage_duration_seconds"] = metrics.get("stage_duration_seconds") if isinstance(metrics.get("stage_duration_seconds"), dict) else {}
    record["metric_counters"] = {key: value for key, value in metrics.items() if key.endswith("_total") and isinstance(value, (int, float))}
    return record


def load_history(root: Path = ROOT) -> list[dict[str, Any]]:
    paths = list((root / "outputs").glob("**/logs/run_summary.json")) + list((root / "outputs").glob("**/logs/run_manifest.json")) + list((root / "logs").glob("**/run_manifest.json"))
    by_run: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = _read(path, {})
        if not isinstance(payload, dict) or not payload.get("run_id"):
            continue
        run_id = str(payload["run_id"])
        if path.name == "run_summary.json":
            record = dict(payload)
            record["_source"] = str(path)
            by_run[run_id] = record
        elif run_id not in by_run:
            manifest = payload
            started = manifest.get("started_at")
            reviewer = _state_reviewer_passed(manifest, root)
            record = {
                "run_id": run_id, "started_at": started, "finished_at": manifest.get("finished_at"),
                "status": "success" if manifest.get("qa_status") == "pass" else "failed",
                "schema_passed": manifest.get("qa_status") == "pass", "reviewer_passed": reviewer,
                "retry_count": 0, "fallback_count": int(bool(manifest.get("fallback_used"))),
                "timeout_count": 0, "source_count": (manifest.get("source_status") or {}).get("source_count"),
                "source_failure_count": None, "delivery_enabled": False, "delivery_status": "skipped",
                "alerts": [], "errors": [], "_source": str(path),
            }
            by_run[run_id] = record
    output: list[dict[str, Any]] = []
    for record in by_run.values():
        started = _parse_date(record.get("started_at"))
        if started is None:
            continue
        finished = _parse_date(record.get("finished_at"))
        duration = record.get("duration_seconds")
        if duration is None and finished:
            duration = max(0.0, (finished - started).total_seconds())
        alerts = record.get("alerts") if isinstance(record.get("alerts"), list) else []
        errors = record.get("errors") if isinstance(record.get("errors"), list) else []
        if "schema_passed" not in record:
            record["schema_passed"] = record.get("status") == "success"
        if "reviewer_passed" not in record and "content_review_passed" in record:
            record["reviewer_passed"] = record.get("content_review_passed")
        p0 = sum(1 for item in alerts if item.get("level") == "P0") + sum(1 for item in errors if str(item.get("error_code", "")).startswith(("INPUT_", "CROSS_")))
        p1 = sum(1 for item in alerts if item.get("level") == "P1")
        if "quality_score" not in record:
            record["quality_score"] = 100.0 if record.get("status") == "success" else 0.0
            record["quality_score_source"] = "derived_from_run_status"
        record = _merge_run_metrics(record, root)
        record.update({"date": started.date().isoformat(), "duration_seconds": float(duration or 0), "p0_count": p0, "p1_count": p1, "started_local": started.isoformat()})
        output.append(record)
    return sorted(output, key=lambda item: (item.get("date", ""), item.get("started_local", "")))


def _rate(values: list[bool | None]) -> float | None:
    valid = [item for item in values if isinstance(item, bool)]
    return round(sum(valid) / len(valid), 4) if valid else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return round(ordered[index], 3)


def _metric_detail(records: list[dict[str, Any]], name: str, values: list[bool | None], window_days: int | None = None) -> dict[str, Any]:
    valid = [item for item in values if isinstance(item, bool)]
    detail = {
        "numerator": sum(valid),
        "denominator": len(valid),
        "sample_count": len(records),
        "value": round(sum(valid) / len(valid), 4) if valid else None,
    }
    if window_days is not None:
        detail["time_window_days"] = window_days
    return detail


def _period_metric_details(records: list[dict[str, Any]], window_days: int | None = None) -> dict[str, Any]:
    bool_metrics = {
        "run_success_rate": [item.get("status") == "success" for item in records],
        "run_failure_rate": [item.get("status") == "failed" for item in records],
        "schema_pass_rate": [item.get("schema_passed") for item in records],
        "reviewer_pass_rate": [item.get("reviewer_passed") for item in records],
        "retry_rate": [int(item.get("retry_count", 0)) > 0 for item in records],
        "fallback_rate": [int(item.get("fallback_count", 0)) > 0 for item in records],
        "timeout_rate": [int(item.get("timeout_count", 0)) > 0 for item in records],
        "checkpoint_resume_rate": [bool(item.get("checkpoint_resumed")) for item in records],
        "delivery_blocked_on_p0_rate": [int(item.get("p0_count", 0)) > 0 and item.get("delivery_status") != "sent" for item in records],
        "delivery_success_rate": [item.get("delivery_status") in {"sent", "success", "delivered"} for item in records if item.get("delivery_attempted") or item.get("delivery_enabled")],
        "delivery_skipped_rate": [item.get("delivery_status") == "skipped" for item in records],
    }
    return {name: _metric_detail(records, name, values, window_days) for name, values in bool_metrics.items()}


def _period_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(item.get("duration_seconds", 0)) for item in records]
    stage_durations = [float(value) for item in records for value in (item.get("stage_duration_seconds") or {}).values() if isinstance(value, (int, float))]
    source_total = sum(float(item.get("source_count") or 0) + float(item.get("source_failure_count") or 0) for item in records)
    source_covered = sum(float(item.get("source_count") or 0) for item in records)
    metric_counters = Counter()
    for item in records:
        metric_counters.update({key: int(value) for key, value in (item.get("metric_counters") or {}).items() if isinstance(value, (int, float))})
    slow_limit = max((float((item.get("metrics_thresholds") or {}).get("pipeline_timeout_seconds", 600)) for item in records), default=600.0)
    return {
        "sample_count": len(records), "run_success_rate": _rate([item.get("status") == "success" for item in records]),
        "run_failure_rate": _rate([item.get("status") == "failed" for item in records]),
        "success_rate": _rate([item.get("status") == "success" for item in records]),
        "schema_pass_rate": _rate([item.get("schema_passed") for item in records]),
        "reviewer_pass_rate": _rate([item.get("reviewer_passed") for item in records]),
        "fact_match_rate": _rate([item.get("fact_match_rate") == 1.0 if isinstance(item.get("fact_match_rate"), (int, float)) else None for item in records]),
        "required_field_coverage": _rate([item.get("schema_passed") for item in records]),
        "source_coverage_rate": round(source_covered / source_total, 4) if source_total else None,
        "p0_count": sum(int(item.get("p0_count", 0)) for item in records), "p1_count": sum(int(item.get("p1_count", 0)) for item in records),
        "p0_failure_rate": _rate([int(item.get("p0_count", 0)) > 0 for item in records]),
        "p1_failure_rate": _rate([int(item.get("p1_count", 0)) > 0 for item in records]),
        "retry_rate": _rate([int(item.get("retry_count", 0)) > 0 for item in records]),
        "fallback_rate": _rate([int(item.get("fallback_count", 0)) > 0 for item in records]),
        "timeout_rate": _rate([int(item.get("timeout_count", 0)) > 0 for item in records]),
        "checkpoint_resume_rate": _rate([bool(item.get("checkpoint_resumed")) for item in records]),
        "average_duration": round(statistics.mean(durations), 3) if durations else None, "average_pipeline_duration": round(statistics.mean(durations), 3) if durations else None,
        "p50_pipeline_duration": round(statistics.median(durations), 3) if durations else None, "p95_duration": _p95(durations), "p95_pipeline_duration": _p95(durations),
        "average_stage_duration": round(statistics.mean(stage_durations), 3) if stage_durations else None, "p95_stage_duration": _p95(stage_durations),
        "slow_run_count": sum(1 for value in durations if value > slow_limit),
        "average_quality_score": round(statistics.mean([float(item.get("quality_score", 0)) for item in records]), 3) if records else None,
        "hallucination_count": metric_counters.get("hallucination_total", 0), "missing_fact_count": metric_counters.get("missing_fact_total", 0),
        "changed_fact_count": metric_counters.get("changed_fact_total", 0), "stale_input_count": metric_counters.get("stale_input_total", 0),
        "wrong_session_count": metric_counters.get("wrong_session_total", 0), "gold_instrument_error_count": metric_counters.get("gold_instrument_error_total", 0),
        "delivery_blocked_on_p0_count": sum(1 for item in records if int(item.get("p0_count", 0)) > 0 and item.get("delivery_status") != "sent"),
        "invalid_output_delivery_count": sum(1 for item in records if item.get("delivery_attempted") and int(item.get("p0_count", 0)) > 0),
        "delivery_success_rate": _rate([item.get("delivery_status") in {"sent", "success", "delivered"} for item in records if item.get("delivery_attempted") or item.get("delivery_enabled")]),
        "delivery_skipped_rate": _rate([item.get("delivery_status") == "skipped" for item in records]),
        "average_retry_count": round(statistics.mean([int(item.get("retry_count", 0)) for item in records]), 3) if records else None,
        "common_errors": Counter(str(item.get("error_code")) for item in records if item.get("error_code")).most_common(5),
    }


def _trend_status(current: float | None, previous: float | None, *, higher_is_better: bool = True) -> str:
    if current is None or previous is None:
        return "insufficient_data"
    delta = current - previous
    if abs(delta) < 0.02:
        return "stable"
    improved = delta > 0 if higher_is_better else delta < 0
    return "improvement" if improved else "degradation"


def build_trend(root: Path = ROOT, days: int = 7) -> dict[str, Any]:
    if days not in {7, 30}:
        raise ValueError("trend_days_must_be_7_or_30")
    records = load_history(root)
    end_date = datetime.now(TOKYO).date()
    start_date = end_date - timedelta(days=days - 1)
    # Replay and isolated test roots often contain a historical window rather
    # than today's runs. Anchor those roots to their newest record so trend
    # validation remains deterministic without changing live-window behavior.
    if records and not any(start_date.isoformat() <= item["date"] <= end_date.isoformat() for item in records):
        end_date = max(datetime.fromisoformat(item["date"]).date() for item in records)
        start_date = end_date - timedelta(days=days - 1)
    previous_start = start_date - timedelta(days=days)
    previous_end = start_date - timedelta(days=1)
    current = [item for item in records if start_date.isoformat() <= item["date"] <= end_date.isoformat()]
    previous = [item for item in records if previous_start.isoformat() <= item["date"] <= previous_end.isoformat()]
    metrics = _period_metrics(current)
    previous_metrics = _period_metrics(previous)
    trend_fields = ("run_success_rate", "run_failure_rate", "schema_pass_rate", "reviewer_pass_rate", "average_quality_score", "average_duration", "p95_duration", "retry_rate", "fallback_rate")
    trend = {field: _trend_status(metrics.get(field), previous_metrics.get(field), higher_is_better=field not in {"run_failure_rate", "average_duration", "p95_duration", "retry_rate", "fallback_rate"}) for field in trend_fields}
    insufficient = len(current) < MIN_TREND_SAMPLE
    comparison_available = any(value in {"improvement", "stable", "degradation"} for value in trend.values())
    result = {
        "window_days": days, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "sample_count": len(current), "insufficient_data": insufficient or not comparison_available, "status": "insufficient_data" if insufficient or not comparison_available else ("degradation" if "degradation" in trend.values() else "stable"),
        "metrics": metrics, "metric_details": _period_metric_details(current, days),
        "previous_period": {"start_date": previous_start.isoformat(), "end_date": previous_end.isoformat(), "metrics": previous_metrics, "metric_details": _period_metric_details(previous, days)},
        "trend": trend, "records": [{"run_id": item.get("run_id"), "date": item.get("date"), "status": item.get("status"), "duration_seconds": item.get("duration_seconds")} for item in current],
        "generated_at": datetime.now(TOKYO).isoformat(),
    }
    quality_root = _quality_root(root)
    quality_root.mkdir(parents=True, exist_ok=True)
    _write(quality_root / f"quality_trend_{days}d.json", result)
    lines = [f"# Quality Trend {days}d", "", f"- Window：{result['start_date']} 至 {result['end_date']}", f"- Samples：{result['sample_count']}", f"- Status：**{result['status']}**", "", "| Metric | Current | Previous | Trend |", "|---|---:|---:|---|"]
    for field in ("run_success_rate", "run_failure_rate", "schema_pass_rate", "reviewer_pass_rate", "average_quality_score", "average_duration", "p95_duration", "retry_rate", "fallback_rate"):
        lines.append(f"| {field} | {metrics.get(field)} | {previous_metrics.get(field)} | {trend.get(field)} |")
    (quality_root / f"quality_trend_{days}d.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = QualityStore(root / "runtime" / "quality.sqlite3")
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in current:
        by_date[item["date"]].append(item)
    for date, items in by_date.items():
        store.upsert_daily_summary({"date": date, "total_runs": len(items), **_period_metrics(items), "samples": items})
    return result


def update_baseline(root: Path, case_id: str, approve: bool, baseline_version: str = BASELINE_VERSION) -> dict[str, Any]:
    case = _case_by_id(load_cases(root), case_id)
    if case.get("kind") != "normal":
        raise ValueError("only_normal_cases_can_update_baseline")
    old = load_baseline(case, root)
    candidate = copy.deepcopy(case.get("candidate") or {})
    preview = {"case_id": case_id, "baseline_version": baseline_version, "approved": bool(approve), "diff": []}
    if old:
        for field in HARD_FACT_FIELDS + GATE_FIELDS + STRUCTURE_FIELDS:
            if field in (old.get("expected") or {}) and candidate.get(field) != old["expected"].get(field):
                preview["diff"].append({"field": field, "before": old["expected"].get(field), "after": candidate.get(field)})
    preview_path = _quality_root(root) / f"baseline_preview_{case_id}.json"
    _write(preview_path, preview)
    if approve:
        filename = case.get("baseline_file")
        if not filename:
            raise RuntimeError("baseline_file_missing")
        baseline = {"case_id": case_id, "baseline_version": baseline_version, "created_at": datetime.now(TOKYO).isoformat(), "approved_by": "explicit_cli_approval", "pipeline_version": PIPELINE_VERSION, "schema_version": "validation_models_v1", "prompt_version": candidate.get("prompt_version", "unknown"), "renderer_version": candidate.get("renderer_version", "unknown"), "expected": candidate}
        _write(root / "tests" / "regression" / filename, baseline)
        preview["updated_path"] = str((root / "tests" / "regression" / filename).resolve())
        _write(preview_path, preview)
    return preview


def _load_gate(root: Path = ROOT) -> dict[str, Any]:
    return _read(_quality_root(root) / "release_gate.json", {"status": "insufficient_data", "reasons": [{"rule": "latest_regression_missing"}]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线回归评估、质量趋势和发布门禁")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--case")
    run.add_argument("--baseline-version", default=BASELINE_VERSION)
    run.add_argument("--candidate-version", default=PIPELINE_VERSION)
    trend = sub.add_parser("trend")
    trend.add_argument("--days", type=int, choices=[7, 30], required=True)
    gate = sub.add_parser("gate")
    update = sub.add_parser("update-baseline")
    update.add_argument("--case", required=True)
    update.add_argument("--approve", action="store_true")
    update.add_argument("--actor", default=os.environ.get("USER", ""))
    update.add_argument("--role", default="maintainer")
    update.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        report = run_regression(case_id=args.case, baseline_version=args.baseline_version, candidate_version=args.candidate_version)
        print(json.dumps({"regression_run_id": report["regression_run_id"], "status": report["release_gate"]["status"], "total_cases": report["total_cases"], "failed_cases": report["failed_cases"]}, ensure_ascii=False))
        return 0 if report["release_gate"]["status"] != "blocked" else 1
    if args.command == "trend":
        report = build_trend(days=args.days)
        print(json.dumps({"days": args.days, "status": report["status"], "sample_count": report["sample_count"]}, ensure_ascii=False))
        return 0
    if args.command == "gate":
        gate_report = _load_gate()
        print(json.dumps(gate_report, ensure_ascii=False))
        return 0 if gate_report.get("status") in {"passed", "warning"} else 1
    decision = authorize(actor=args.actor, role=args.role, capability="regression.update_baseline", reason=args.reason, approve=args.approve)
    AuditLogger().append("regression.update_baseline", actor=args.actor, outcome="allowed" if decision["allowed"] else "denied", details={"case": args.case, "code": decision["code"]}, reason=args.reason)
    if not decision["allowed"]:
        raise SecurityError(decision["code"], "authorization denied")
    preview = update_baseline(ROOT, args.case, args.approve)
    print(json.dumps(preview, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
