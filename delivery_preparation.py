"""Read-only preparation for an explicitly approved X/Twitter release.

This module never changes delivery policy, the kill switch, approvals, or
``delivered``. It produces evidence for a human approval step only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from canary_controls import current_artifact_hash, delivery_preflight
from delivery_gate import build_delivery_adapter
from run_state import atomic_write_json, sha256


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_path(root: Path, run_id: str) -> Path:
    candidates = (
        root / "outputs" / "runs" / run_id / "logs" / "run_manifest.json",
        root / "outputs" / "runs" / run_id / "run_manifest.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"production_manifest_not_found:{run_id}")


def approval_digest(*, run_id: str, target: str, content_hash: str, artifact_hash: str, adapter: str = "x_twitter") -> str:
    payload = {
        "adapter": adapter,
        "artifact_hash": artifact_hash,
        "content_hash": content_hash,
        "run_id": run_id,
        "target": target,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_x_twitter_release(root: Path, run_id: str, target: str | None = None) -> dict[str, Any]:
    """Build non-authorizing release evidence for one completed text run."""
    root = root.resolve()
    manifest_path = _manifest_path(root, run_id)
    manifest = _read_json(manifest_path)
    output_root = Path(str(manifest.get("output_root") or "")).resolve()
    content_path = output_root / "market_content" / "market_content.json"
    content = _read_json(content_path)
    policy = _read_json(root / "config" / "delivery_policy.json")
    adapter = build_delivery_adapter(policy)
    adapter_health = adapter.health()
    content_hash = str(manifest.get("content_hash") or (sha256(content_path) if content_path.is_file() else ""))
    artifact_hash = current_artifact_hash(manifest)
    mode = str(manifest.get("mode") or manifest.get("output_mode") or "text").lower()
    text = str(content.get("summary") or content.get("headline") or "").strip()
    blockers: list[str] = []

    if "shadow" in output_root.parts or "canary" in output_root.parts:
        blockers.append("SHADOW_OR_CANARY_RUN_NOT_ELIGIBLE")
    if mode != "text":
        blockers.append("X_TWITTER_TEXT_ONLY_ADAPTER")
    if not content_path.is_file() or not content:
        blockers.append("CONTENT_ARTIFACT_MISSING")
    if not content_hash or not artifact_hash:
        blockers.append("ARTIFACT_INTEGRITY_MISSING")
    if not text:
        blockers.append("X_TEXT_MISSING")
    if len(text) > 280:
        blockers.append("X_TEXT_OVER_280_CHARACTERS")
    if adapter_health.get("status") != "ready":
        blockers.append("X_ADAPTER_NOT_READY")

    preflight = delivery_preflight(
        root,
        run_id=run_id,
        run_mode=str(manifest.get("run_mode") or "production"),
        manifest=manifest,
        approval=None,
        target=target,
    )
    blockers.extend(preflight.blockers)
    blockers = list(dict.fromkeys(blockers))
    digest = approval_digest(
        run_id=run_id,
        target=target,
        content_hash=content_hash,
        artifact_hash=artifact_hash,
    ) if target and content_hash and artifact_hash else None
    report = {
        "mode": "prepare_only",
        "run_id": run_id,
        "adapter": "x_twitter",
        "target": target,
        "manifest_path": str(manifest_path.resolve()),
        "content_path": str(content_path.resolve()),
        "content_hash": content_hash or None,
        "artifact_hash": artifact_hash,
        "text_length": len(text),
        "text_ready": bool(text) and len(text) <= 280,
        "adapter_health": adapter_health,
        "delivery_preflight": preflight.as_dict(),
        "approval_digest": digest,
        "approval_material": {
            "run_id": run_id,
            "target": target,
            "adapter": "x_twitter",
            "content_hash": content_hash or None,
            "artifact_hash": artifact_hash,
            "approval_digest": digest,
        } if digest else None,
        "blockers": blockers,
        "status": "READY_FOR_HUMAN_APPROVAL" if not blockers else "BLOCKED",
        "external_request_made": False,
        "config_mutated": False,
        "delivered": False,
    }
    report_path = root / "runtime" / "delivery_preflight" / f"{run_id}.json"
    atomic_write_json(report_path, report)
    report["report_path"] = str(report_path.resolve())
    return report


__all__ = ["approval_digest", "prepare_x_twitter_release"]
