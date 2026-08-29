#!/usr/bin/env python3
"""Fail-closed production-readiness preflight for the market content pipeline."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from delivery_gate import build_delivery_adapter


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _check(name: str, passed: bool, detail: str, blockers: list[dict[str, str]], checks: dict[str, dict[str, Any]]) -> None:
    checks[name] = {"status": "pass" if passed else "block", "detail": detail}
    if not passed:
        blockers.append({"code": name, "detail": detail})


def evaluate_preflight(root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Return a non-mutating, fail-closed production readiness report."""
    blockers: list[dict[str, str]] = []
    checks: dict[str, dict[str, Any]] = {}

    evaluation_policy = _read_json(root / "config" / "evaluation_policy.json")
    routing_policy = _read_json(root / "config" / "tool_routing_policy.json")
    delivery_policy = _read_json(root / "config" / "delivery_policy.json")
    tools = routing_policy.get("tools", {}) if isinstance(routing_policy.get("tools"), dict) else {}

    _check(
        "delivery_policy_enabled",
        evaluation_policy.get("allow_delivery") is True,
        "config/evaluation_policy.json must explicitly set allow_delivery=true",
        blockers,
        checks,
    )
    _check(
        "production_update_policy_enabled",
        evaluation_policy.get("allow_production_update") is True,
        "config/evaluation_policy.json must explicitly set allow_production_update=true",
        blockers,
        checks,
    )
    _check(
        "delivery_adapter_policy_enabled",
        delivery_policy.get("enabled") is True,
        "config/delivery_policy.json must explicitly enable a configured adapter",
        blockers,
        checks,
    )

    configured_publish = sorted(
        name
        for name, definition in tools.items()
        if isinstance(definition, dict)
        and definition.get("enabled") is True
        and name != "blocked_delivery_gate"
        and ("publish" in name or "deliver" in name or "send" in name or "delivery" in {str(item) for item in definition.get("supported_tasks", [])})
    )
    _check(
        "publish_adapter_available",
        bool(configured_publish),
        "a production publish adapter must be registered and enabled",
        blockers,
        checks,
    )
    try:
        adapter_health = build_delivery_adapter(delivery_policy).health()
    except (ValueError, TypeError) as exc:
        adapter_health = {"status": "unconfigured", "adapter_error": str(exc)}
    _check(
        "publish_endpoint_configured",
        adapter_health.get("status") == "ready",
        f"selected delivery adapter must be configured: {adapter_health}",
        blockers,
        checks,
    )

    canary_report = _read_json(root / "reports" / "production_canary_report.json")
    if not canary_report:
        canary_report = _read_json(root / "reports" / "canary_self_healing_report.json")
    _check(
        "production_canary_ready",
        canary_report.get("production_ready") is True,
        "a real-environment Canary report with production_ready=true is required",
        blockers,
        checks,
    )

    run_report: dict[str, Any] | None = None
    if run_id:
        state_path = root / "runtime" / "shadow" / run_id / "state" / f"{run_id}.json"
        manifest_path = root / "logs" / "shadow" / run_id / "run_manifest.json"
        state = _read_json(state_path)
        manifest = _read_json(manifest_path)
        run_report = {"run_id": run_id, "state_path": str(state_path), "manifest_path": str(manifest_path)}
        _check(
            "shadow_run_completed",
            bool(state) and not state.get("failed_step") and bool(state.get("completed_steps")),
            f"shadow run {run_id} must complete without failed_step",
            blockers,
            checks,
        )
        _check(
            "shadow_evaluation_passed",
            manifest.get("qa_status") == "pass" and bool(state) and "offline_evaluation" in state.get("completed_steps", []),
            f"shadow run {run_id} must pass QA and offline evaluation",
            blockers,
            checks,
        )
        run_report["state"] = state
        run_report["manifest"] = manifest

    report: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ready": not blockers,
        "checks": checks,
        "blockers": blockers,
    }
    if run_report is not None:
        report["run"] = run_report
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查每日市场内容包是否满足生产放行条件")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--run-id", help="可选：同时检查指定 Shadow 运行")
    args = parser.parse_args(argv)
    report = evaluate_preflight(args.root.resolve(), args.run_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
