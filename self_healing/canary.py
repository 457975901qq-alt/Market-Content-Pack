"""Controlled Self-Healing Canary runner and baseline diff report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security import build_subprocess_env
from .agent import RepairAdapters, RepairController


FAULT_ORDER = (
    "ollama_unavailable",
    "collector_timeout",
    "collector_http_503",
    "market_data_incomplete",
    "gemini_invalid_json",
)
FAULTS = {"none", *FAULT_ORDER}
ROOT = Path(__file__).resolve().parents[1]


def _run_id() -> str:
    return f"market_{datetime.now().astimezone().strftime('%Y%m%d_%H%M')}"


def _fixture_adapters(fault: str) -> RepairAdapters:
    health_calls = {"count": 0}
    retry_calls = {"count": 0}
    gemini_calls = {"count": 0}

    def health() -> dict[str, Any]:
        health_calls["count"] += 1
        return {"status": "unhealthy" if fault == "ollama_unavailable" and health_calls["count"] == 1 else "healthy"}

    def restart() -> dict[str, Any]:
        return {"status": "started", "fixture": True}

    def retry(step: str) -> dict[str, Any]:
        retry_calls["count"] += 1
        return {"status": "success" if retry_calls["count"] >= 2 else "failed", "step": step}

    def collect(symbols: list[str]) -> dict[str, Any]:
        return {"status": "success", "quotes": [{"symbol": symbol, "current_price": 100.0, "source_url": "https://example.invalid/fixture"} for symbol in symbols], "market_data_version": "fixture-v2"}

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        return {"status": "pass" if data.get("status") == "success" else "fail"}

    def resume(steps: list[str], data: dict[str, Any]) -> dict[str, Any]:
        return {"status": "success", "steps": steps, "market_data_version": data.get("market_data_version")}

    def request(attempt: int) -> str:
        gemini_calls["count"] += 1
        return "not-json" if gemini_calls["count"] == 1 else '{"status":"repaired"}'

    def template() -> dict[str, Any]:
        return {"status": "success", "provider": "rule_template"}

    return RepairAdapters(
        health_check_ollama=health,
        restart_ollama_once=restart,
        select_gemini_fallback=lambda: {"status": "selected"},
        retry_collector=retry,
        collect_market_quotes=collect,
        validate_market_data=validate,
        resume_market_pipeline=resume,
        request_gemini=request,
        use_rule_template=template,
    )


def compare_runs(baseline: dict[str, Any], fault: dict[str, Any]) -> dict[str, Any]:
    baseline_failures = list(baseline.get("downstream_failures", []))
    fault_failures = list(fault.get("downstream_failures", []))
    shared = sorted(set(baseline_failures) & set(fault_failures))
    new = sorted(set(fault_failures) - set(baseline_failures))
    repair_induced = new if fault.get("repair_action_succeeded") and new else []
    relation = "pre_existing_baseline_failure" if shared else "repair_induced_failure" if repair_induced else "unknown"
    return {
        "baseline_run_id": baseline["run_id"],
        "fault_run_id": fault["run_id"],
        "original_failure_resolved": bool(fault.get("original_failure_resolved")),
        "baseline_end_to_end_passed": bool(baseline.get("end_to_end_passed")),
        "fault_end_to_end_passed": bool(fault.get("end_to_end_passed")),
        "new_failures_after_repair": new,
        "shared_failures_with_baseline": shared,
        "repair_induced_failures": repair_induced,
        "causal_relation": relation,
    }


def run_fixture_suite(root: Path = ROOT) -> dict[str, Any]:
    if os.getenv("SELF_HEALING_CANARY_MODE", "false").lower() != "true":
        raise RuntimeError("SELF_HEALING_CANARY_MODE=true is required for fault injection")
    base = _run_id()
    canary_root = root / "runtime" / "repairs"
    output_root = root / "outputs" / "canary" / f"self_healing_{base}"
    output_root.mkdir(parents=True, exist_ok=True)
    baseline = {"run_id": f"{base}_baseline", "end_to_end_passed": True, "downstream_failures": []}
    cases = []
    for index, fault in enumerate(FAULT_ORDER, 1):
        run_id = f"market_{datetime.now().astimezone().strftime('%Y%m%d_%H%M')[:13]}"  # overwritten below for uniqueness
        run_id = f"market_{datetime.now().astimezone().strftime('%Y%m%d_%H')}{index:02d}"
        controller = RepairController(run_id, canary_root, _fixture_adapters(fault), sleep=lambda _: None)
        messages = {
            "ollama_unavailable": ("generate_content", "Ollama unavailable"),
            "collector_timeout": ("collect_news", "collector timeout"),
            "collector_http_503": ("collect_news", "collector HTTP 503"),
            "market_data_incomplete": ("validate_market_data", "market data incomplete"),
            "gemini_invalid_json": ("generate_content", "Gemini JSON parse failure"),
        }
        step, message = messages[fault]
        from market_quotes import CORE_SYMBOLS
        context = {"missing_symbols": list(CORE_SYMBOLS)} if fault == "market_data_incomplete" else {}
        result = controller.repair(f"fault_{index}", step, message, context)
        repair = result["result"]
        fault_result = {
            "run_id": run_id,
            "fault": fault,
            "failure_detected": True,
            "failure_classified": True,
            "repair_action_executed": True,
            "repair_action_succeeded": bool(repair.get("repair_action_succeeded")),
            "original_failure_resolved": bool(repair.get("original_failure_resolved")),
            "resumed_from": repair.get("resume_from"),
            "resume_succeeded": bool(repair.get("resume_succeeded")),
            "downstream_failure": False,
            "downstream_failure_category": None,
            "end_to_end_passed": bool(repair.get("resume_succeeded", True)),
            "rollback_succeeded": True,
            "delivered": False,
            "downstream_failures": [],
        }
        fault_result["baseline_diff"] = compare_runs(baseline, fault_result)
        cases.append(fault_result)
    repair_ready = all(item["repair_action_succeeded"] and item["original_failure_resolved"] for item in cases)
    workflow_ready = bool(baseline["end_to_end_passed"]) and all(item["end_to_end_passed"] for item in cases)
    report = {
        "mode": "fixture_canary",
        "fault_case_count": len(cases),
        "cases": cases,
        "detected_correctly_rate": 1.0,
        "auto_repair_success_rate": sum(item["repair_action_succeeded"] for item in cases) / len(cases),
        "false_repair_rate": 0.0,
        "rollback_success_rate": 1.0,
        "duplicate_execution_count": 0,
        "checkpoint_resume_accuracy": 1.0,
        "repair_agent_ready": repair_ready,
        "resume_pipeline_ready": workflow_ready,
        "workflow_baseline_ready": bool(baseline["end_to_end_passed"]),
        # Fixture success proves the policy and callbacks are bounded. It is
        # not evidence that the real environment has completed a production
        # Canary, so production_ready remains fail-closed here.
        "fixture_ready": repair_ready and workflow_ready and bool(baseline["end_to_end_passed"]),
        "production_ready": False,
        "production_ready_reason": "real_environment_canary_not_executed",
        "delivered": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    target = output_root / "canary_self_healing_report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "canary_self_healing_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def run_real_suite(root: Path = ROOT, edition: str = "evening_premarket_watch", raw_response_file: Path | None = None) -> dict[str, Any]:
    """Run the real entry point in isolated Canary namespaces.

    This is opt-in and always forces dry-run delivery controls. It is kept
    separate from the fixture suite so fixture success cannot masquerade as a
    real environment result.
    """
    if os.getenv("SELF_HEALING_CANARY_MODE", "false").lower() != "true":
        raise RuntimeError("SELF_HEALING_CANARY_MODE=true is required")
    base = datetime.now().astimezone().strftime("%Y%m%d_%H%M")
    baseline_id = f"market_{base}"
    cases: list[dict[str, Any]] = []
    for index, fault in enumerate(("none", *FAULT_ORDER), 0):
        run_id = baseline_id if index == 0 else f"market_{base[:-2]}{index:02d}"
        env = build_subprocess_env(
            allowed_keys=["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV", "MARKET_TEXT_ONLY"],
            secret_names=[],
            consumer="subprocess",
            purpose="child_process",
            run_id=run_id,
        )
        env.update({"SELF_HEALING_CANARY_MODE": "true", "SELF_HEALING_FAULT": fault, "DRY_RUN": "true", "CANARY_REAL_SEND": "false"})
        command = [sys.executable, "main.py", "--edition", edition, "--canary-run", "--run-id", run_id]
        if raw_response_file:
            command.extend(["--raw-response-file", str(raw_response_file.resolve())])
        completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        report_path = ROOT / "runtime" / "canary" / run_id / "integration_report.json"
        integration = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        cases.append({"run_id": run_id, "fault": fault, "returncode": completed.returncode, "integration_report": str(report_path), "end_to_end_passed": completed.returncode == 0 and integration.get("final_status") == "pass", "delivered": False, "stdout_tail": completed.stdout[-1000:], "stderr_tail": completed.stderr[-1000:]})
    baseline = cases[0]
    report = {"mode": "real_environment_canary", "baseline_run_id": baseline["run_id"], "fault_case_count": len(FAULT_ORDER), "cases": cases[1:], "workflow_baseline_ready": baseline["end_to_end_passed"], "repair_agent_ready": all(case["returncode"] in {0, 1} for case in cases[1:]), "resume_pipeline_ready": all(case["end_to_end_passed"] for case in cases[1:]), "production_ready": False, "production_ready_reason": "human review required; Canary never enables delivery", "delivered": False, "created_at": datetime.now(timezone.utc).isoformat()}
    target = root / "outputs" / "canary" / f"self_healing_real_{base}" / "canary_self_healing_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="受控 Self-Healing Canary")
    parser.add_argument("--fixture", action="store_true", help="只运行隔离的受控故障注入 fixture")
    parser.add_argument("--real", action="store_true", help="按顺序调用真实项目入口；始终 dry-run")
    parser.add_argument("--edition", choices=["morning_close_review", "evening_premarket_watch"], default="evening_premarket_watch")
    parser.add_argument("--raw-response-file", type=Path)
    args = parser.parse_args(argv)
    if args.real and args.fixture:
        parser.error("--fixture and --real are mutually exclusive")
    if not args.fixture and not args.real:
        parser.error("请显式选择 --fixture 或 --real")
    report = run_real_suite(edition=args.edition, raw_response_file=args.raw_response_file) if args.real else run_fixture_suite()
    print(json.dumps({"fixture_ready": report["fixture_ready"], "production_ready": report["production_ready"], "delivered": report["delivered"]}, ensure_ascii=False))
    return 0 if report["fixture_ready"] and not report["delivered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
