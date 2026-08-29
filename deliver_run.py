#!/usr/bin/env python3
"""Explicit, approval-gated production email delivery for a completed run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from delivery_gate import authorize_delivery, build_delivery_adapter
from canary_controls import current_artifact_hash, delivery_preflight


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _manifest_path(root: Path, run_id: str) -> Path:
    candidates = [
        root / "outputs" / "runs" / run_id / "logs" / "run_manifest.json",
        root / "outputs" / "runs" / run_id / "run_manifest.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"production_manifest_not_found:{run_id}")


def deliver_run(root: Path, run_id: str, approval: dict[str, Any], confirm: bool) -> dict[str, Any]:
    if not confirm:
        return {"status": "blocked", "reason": "confirm_production_send_required", "run_id": run_id}
    manifest_path = _manifest_path(root, run_id)
    manifest = _read_json(manifest_path)
    output_root = Path(str(manifest.get("output_root") or "")).resolve()
    if "shadow" in output_root.parts or "canary" in output_root.parts:
        return {"status": "blocked", "reason": "shadow_or_canary_delivery_forbidden", "run_id": run_id}
    mode = str(manifest.get("mode") or manifest.get("output_mode") or "text").lower()
    content_path = output_root / "market_content" / "market_content.json"
    if not content_path.is_file():
        return {"status": "blocked", "reason": "content_artifact_missing", "run_id": run_id}
    attachments: list[str] = []
    if mode != "text":
        return {"status": "blocked", "reason": f"unsupported_output_mode:{mode}", "run_id": run_id}

    evaluation_policy = _read_json(root / "config" / "evaluation_policy.json")
    delivery_policy = _read_json(root / "config" / "delivery_policy.json")
    if delivery_policy.get("enabled") is not True:
        return {"status": "blocked", "reason": "delivery_adapter_policy_disabled", "run_id": run_id}
    adapter = build_delivery_adapter(delivery_policy)
    artifact_hash = current_artifact_hash(manifest)
    content_hash = str(manifest.get("content_hash") or "")
    if not artifact_hash or not content_hash:
        return {"status": "blocked", "reason": "artifact_integrity_missing", "run_id": run_id}
    target = str(approval.get("target")) if approval.get("target") else None
    decision = delivery_preflight(
        root,
        run_id=run_id,
        run_mode=str(manifest.get("run_mode") or "production"),
        manifest=manifest,
        approval=approval,
        target=target,
    )
    try:
        from runtime_index import StateIndex

        StateIndex(root / "runtime" / "state_index.sqlite3").audit(
            run_id,
            "DELIVERY_PREFLIGHT_PASSED" if decision.allowed else "DELIVERY_PREFLIGHT_DENIED",
            decision.as_dict(),
        )
        if decision.kill_switch_active:
            StateIndex(root / "runtime" / "state_index.sqlite3").audit(run_id, "DELIVERY_KILL_SWITCH_ACTIVE", {"run_id": run_id, "reason_code": "KILL_SWITCH_ACTIVE"})
    except Exception:
        pass
    if not decision.allowed:
        return {"status": "blocked", "run_id": run_id, "delivery_decision": decision.as_dict()}
    authorization = authorize_delivery(
        policy=evaluation_policy,
        run_id=run_id,
        artifact_hash=artifact_hash,
        dry_run=False,
        adapter_ready=adapter.health().get("status") == "ready",
        approval=approval,
    )
    if not authorization.allowed:
        return {"status": "blocked", "run_id": run_id, "authorization": authorization.as_dict()}

    content = _read_json(content_path)
    result = adapter.publish(
        {
            "subject": f"{content.get('date', '')} {manifest.get('edition', 'market')} 市场内容包",
            "text": content.get("summary") or content.get("headline") or "每日市场内容包",
            "attachments": attachments,
        },
        idempotency_key=f"{run_id}:{content_hash}",
    )
    receipt = {**result, "run_id": run_id, "artifact_hash": artifact_hash}
    receipt_path = root / "reports" / "delivery_receipts" / f"{run_id}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "sent", "receipt": receipt, "receipt_path": str(receipt_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发送已批准的生产市场报告邮件")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--approval-file", type=Path, required=True)
    parser.add_argument("--confirm-production-send", action="store_true")
    args = parser.parse_args(argv)
    result = deliver_run(args.root.resolve(), args.run_id, _read_json(args.approval_file), args.confirm_production_send)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "sent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
