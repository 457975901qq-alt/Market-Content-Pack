#!/usr/bin/env python3
"""Validate a real delivery canary receipt without fabricating production readiness."""

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


def validate_canary(root: Path, run_id: str, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    state = _read_json(root / "runtime" / "shadow" / run_id / "state" / f"{run_id}.json")
    manifest = _read_json(root / "logs" / "shadow" / run_id / "run_manifest.json")
    adapter = build_delivery_adapter(_read_json(root / "config" / "delivery_policy.json"))
    checks = {
        "shadow_completed": bool(state) and not state.get("failed_step"),
        "adapter_configured": adapter.health().get("status") == "ready",
        "receipt_present": isinstance(receipt, dict),
        "receipt_matches_run": isinstance(receipt, dict) and receipt.get("run_id") == run_id,
        "receipt_matches_artifact": isinstance(receipt, dict) and receipt.get("artifact_hash") == manifest.get("content_hash"),
        "receipt_success": isinstance(receipt, dict) and receipt.get("status") == "sent" and bool(receipt.get("idempotency_key")),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "run_id": run_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "production_ready": not blockers,
        "checks": checks,
        "blockers": blockers,
        "receipt": receipt,
        "adapter": adapter.health(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证真实发布 Canary 回执")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = _read_json(args.receipt_file) if args.receipt_file else None
    report = validate_canary(args.root.resolve(), args.run_id, receipt)
    output = args.output or args.root.resolve() / "reports" / "production_canary_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["production_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
