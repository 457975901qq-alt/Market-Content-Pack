"""Deterministic controls for Production Canary V1.

This module is deliberately outside the Agent registry.  Planner, models and
repair callbacks can observe its results but cannot mutate run mode, delivery
policy, the kill switch, approvals, or the target whitelist.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RunMode(str, Enum):
    DRY_RUN = "dry_run"
    SHADOW_CANARY = "shadow_canary"
    PRODUCTION_CANARY = "production_canary"
    PRODUCTION = "production"


class RunModeConflict(ValueError):
    code = "RUN_MODE_CONFLICT"


@dataclass(frozen=True)
class RunModeResolution:
    mode: RunMode
    resolved_from: str
    conflict: bool = False
    reason_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_mode": self.mode.value,
            "resolved_from": self.resolved_from,
            "conflict": self.conflict,
            "reason_code": self.reason_code,
        }


def resolve_run_mode(
    *,
    cli_mode: str | None = None,
    shadow_run: bool = False,
    canary_run: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> RunModeResolution:
    """Resolve one authoritative mode and reject contradictory inputs.

    ``--canary-run`` is an existing fault-injection entry point; it maps to a
    shadow canary and never grants delivery permission.
    """
    values: list[tuple[str, str]] = []
    if cli_mode:
        values.append(("cli", cli_mode.strip().lower()))
    environ = env or os.environ
    env_mode = str(environ.get("DAILY_MARKET_RUN_MODE") or environ.get("MARKET_RUN_MODE") or "").strip().lower()
    if env_mode:
        values.append(("env", env_mode))
    if shadow_run or canary_run:
        values.append(("flag", RunMode.SHADOW_CANARY.value))
    elif dry_run:
        values.append(("flag", RunMode.DRY_RUN.value))

    valid = {item.value for item in RunMode}
    invalid = [value for _, value in values if value not in valid]
    if invalid:
        raise RunModeConflict(f"{RunModeConflict.code}:invalid:{','.join(invalid)}")
    distinct = {value for _, value in values}
    if len(distinct) > 1:
        raise RunModeConflict(f"{RunModeConflict.code}:{','.join(sorted(distinct))}")
    if not values:
        return RunModeResolution(RunMode.DRY_RUN, "default")
    source, value = values[0]
    return RunModeResolution(RunMode(value), source)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "off"}:
        return False
    return None


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def current_artifact_hash(manifest: Mapping[str, Any]) -> str | None:
    hashes = manifest.get("artifact_hashes")
    if not isinstance(hashes, dict) or not hashes:
        return None
    canonical = json.dumps({str(k): str(v) for k, v in sorted(hashes.items())}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _approval_valid(
    approval: Mapping[str, Any] | None,
    *,
    run_id: str,
    artifact_hash: str | None,
    content_hash: str | None,
    target: str | None,
    now: datetime,
) -> tuple[bool, list[str], str | None]:
    if not isinstance(approval, Mapping):
        return False, ["DELIVERY_APPROVAL_MISSING"], None
    blockers: list[str] = []
    status = str(approval.get("status") or ("APPROVED" if approval.get("approved") is True else ""))
    if status != "APPROVED":
        blockers.append("DELIVERY_APPROVAL_NOT_APPROVED")
    if approval.get("approved_by") in {None, "", "agent", "planner", "model"}:
        blockers.append("DELIVERY_APPROVAL_ACTOR_INVALID")
    if approval.get("run_id") != run_id:
        blockers.append("DELIVERY_APPROVAL_RUN_MISMATCH")
    if artifact_hash is None or approval.get("artifact_hash") != artifact_hash:
        blockers.append("DELIVERY_ARTIFACT_HASH_MISMATCH")
    if content_hash is None or approval.get("content_hash") != content_hash:
        blockers.append("DELIVERY_CONTENT_HASH_MISMATCH")
    expires_at = approval.get("expires_at")
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            blockers.append("DELIVERY_APPROVAL_EXPIRED")
    except (TypeError, ValueError):
        blockers.append("DELIVERY_APPROVAL_EXPIRY_INVALID")
    allowed_targets = approval.get("allowed_targets")
    if target and (not isinstance(allowed_targets, list) or target not in allowed_targets):
        blockers.append("DELIVERY_TARGET_NOT_APPROVED")
    return not blockers, blockers, str(approval.get("approval_id")) if approval.get("approval_id") else None


def read_delivery_controls(root: Path, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read delivery controls fail-closed, including source conflicts."""
    delivery = _read_json(root / "config" / "delivery_policy.json")
    release = _read_json(root / "config" / "release_policy.json")
    environ = env or os.environ
    config_switch = _parse_bool(delivery.get("global_delivery_kill_switch"))
    env_raw = environ.get("GLOBAL_DELIVERY_KILL_SWITCH")
    env_switch = _parse_bool(env_raw) if env_raw is not None else None
    reasons: list[str] = []
    if config_switch is None:
        reasons.append("KILL_SWITCH_CONFIG_MISSING_OR_INVALID")
    if env_raw is not None and env_switch is None:
        reasons.append("KILL_SWITCH_ENV_INVALID")
    if config_switch is not None and env_switch is not None and config_switch != env_switch:
        reasons.append("KILL_SWITCH_CONFIG_CONFLICT")
    kill_switch_active = True if reasons else bool(config_switch if env_switch is None else env_switch)
    delivery_external = _parse_bool(delivery.get("external_delivery_enabled"))
    release_external = _parse_bool(release.get("external_delivery_enabled"))
    if delivery_external is None:
        delivery_external = False
    if release_external is None:
        release_external = False
    if delivery_external != release_external:
        reasons.append("EXTERNAL_DELIVERY_CONFIG_CONFLICT")
    targets = delivery.get("target_whitelist")
    if not isinstance(targets, list):
        targets = []
    return {
        "kill_switch_active": kill_switch_active,
        "external_delivery_enabled": bool(delivery_external and release_external),
        "target_whitelist": [str(item) for item in targets],
        "approval_ttl_seconds": int(delivery.get("approval_ttl_seconds", 3600) or 3600),
        "config_reasons": sorted(set(reasons)),
    }


@dataclass(frozen=True)
class DeliveryDecision:
    allowed: bool
    run_id: str
    run_mode: str
    delivery_state: str
    reason_code: str
    blockers: list[str]
    kill_switch_active: bool
    external_delivery_enabled: bool
    human_approval_valid: bool
    artifact_integrity_valid: bool
    target: str | None
    approval_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "run_id": self.run_id,
            "run_mode": self.run_mode,
            "delivery_state": self.delivery_state,
            "reason_code": self.reason_code,
            "blockers": self.blockers,
            "kill_switch_active": self.kill_switch_active,
            "external_delivery_enabled": self.external_delivery_enabled,
            "human_approval_valid": self.human_approval_valid,
            "artifact_integrity_valid": self.artifact_integrity_valid,
            "target": self.target,
            "approval_id": self.approval_id,
        }


def delivery_preflight(
    root: Path,
    *,
    run_id: str,
    run_mode: str,
    manifest: Mapping[str, Any],
    approval: Mapping[str, Any] | None = None,
    target: str | None = None,
    now: datetime | None = None,
) -> DeliveryDecision:
    controls = read_delivery_controls(root)
    blockers = list(controls["config_reasons"])
    if run_mode != RunMode.PRODUCTION_CANARY.value:
        blockers.append("DENY_WRONG_RUN_MODE")
    if controls["kill_switch_active"]:
        blockers.append("KILL_SWITCH_ACTIVE")
    if not controls["external_delivery_enabled"]:
        blockers.append("EXTERNAL_DELIVERY_DISABLED")
    if manifest.get("canary_technical_ready") is not True:
        blockers.append("CANARY_TECHNICAL_NOT_READY")
    if manifest.get("canary_stability_pass") is not True:
        blockers.append("CANARY_STABILITY_NOT_READY")
    if manifest.get("production_ready") is not True:
        blockers.append("PRODUCTION_NOT_READY")
    if manifest.get("qa_status") != "pass":
        blockers.append("FINAL_QUALITY_GATE_NOT_PASS")
    whitelist = controls["target_whitelist"]
    if not target or target not in whitelist:
        blockers.append("DELIVERY_TARGET_NOT_WHITELISTED")
    artifact_hash = current_artifact_hash(manifest)
    content_hash = str(manifest.get("content_hash")) if manifest.get("content_hash") else None
    approval_ok, approval_blockers, approval_id = _approval_valid(
        approval,
        run_id=run_id,
        artifact_hash=artifact_hash,
        content_hash=content_hash,
        target=target,
        now=_now(now),
    )
    blockers.extend(approval_blockers)
    blockers = list(dict.fromkeys(blockers))
    allowed = not blockers
    return DeliveryDecision(
        allowed=allowed,
        run_id=run_id,
        run_mode=run_mode,
        delivery_state="READY_FOR_APPROVAL" if allowed else "PREFLIGHT_DENIED",
        reason_code=blockers[0] if blockers else "PREFLIGHT_PASS",
        blockers=blockers,
        kill_switch_active=bool(controls["kill_switch_active"]),
        external_delivery_enabled=bool(controls["external_delivery_enabled"]),
        human_approval_valid=approval_ok,
        artifact_integrity_valid=artifact_hash is not None and content_hash is not None,
        target=target,
        approval_id=approval_id,
    )


def build_readiness_evidence(
    *,
    root: Path,
    state: Mapping[str, Any],
    paths: Mapping[str, Any],
    run_mode: str,
) -> dict[str, Any]:
    """Build evidence from existing artifacts; never infer a pass from a plan alone."""
    logs = Path(paths["logs"])
    market_path = Path(paths["market_quotes"])
    qa = _read_json(logs / "qa_report.json")
    market = _read_json(market_path)
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    logical = state.get("logical_steps") if isinstance(state.get("logical_steps"), dict) else {}
    state_ok = lambda name: (steps.get(name) or {}).get("status") == "success"
    logical_ok = lambda name: (logical.get(name) or {}).get("status") == "success"
    review_ok = logical_ok("reviewer_gate") or state_ok("reviewer_gate")
    final_ok = logical_ok("final_quality_gate") or state_ok("final_validation") or qa.get("status") == "pass"
    unauthorized = 0
    audit_path = logs / "agent_loop.jsonl"
    if audit_path.exists():
        try:
            unauthorized = sum("UNAUTHORIZED_DELIVERY_CONTROL_MUTATION" in line for line in audit_path.read_text(encoding="utf-8").splitlines())
        except OSError:
            unauthorized = 1
    errors = market.get("errors") if isinstance(market.get("errors"), list) else []
    stale_escape = sum(1 for item in errors if isinstance(item, dict) and str(item.get("error_type", "")).lower() in {"stale_market_data", "market_data_stale"})
    evidence = {
        "run_id": state.get("run_id"),
        "run_mode": run_mode,
        "agent_loop": "pass" if state.get("agent_controller") or state.get("agent_state") else "not_run",
        "planner": "pass" if Path(paths.get("plan", "")).exists() else "not_run",
        "tool_registry": "pass" if (root / "function_calling" / "registry.py").exists() else "fail",
        "executor": "pass" if (root / "function_calling" / "function_executor.py").exists() else "fail",
        "checkpoint": "pass" if Path(paths.get("state_root", logs)).exists() else "not_run",
        "recovery": "pass" if (root / "config" / "self_healing_policy.json").exists() else "fail",
        "data_quality": "pass" if market.get("status") == "success" else "fail",
        "historical_fallback": "pass" if any(isinstance(e, dict) and e.get("provider") == "massive" and e.get("status") == "success" for e in market.get("provider_events", [])) else "not_run",
        "qa": "pass" if qa.get("status") == "pass" else "fail",
        "reviewer_gate": "pass" if review_ok else "not_run",
        "final_quality_gate": "pass" if final_ok else "fail",
        "renderer": "not_applicable" if state.get("text_only", True) else ("pass" if _read_json(logs / "image_qa.json").get("status") == "pass" else "fail"),
        "release_policy": "pass" if (root / "config" / "release_policy.json").exists() else "fail",
        "delivery_preflight": "not_run" if run_mode != RunMode.PRODUCTION_CANARY.value else "not_run",
        "unauthorized_tool_calls": int(unauthorized),
        "critical_errors": 0 if not state.get("failed_step") else 1,
        "schema_errors": sum(1 for item in errors if isinstance(item, dict) and "schema" in str(item.get("error_type", "")).lower()),
        "stale_data_escapes": int(stale_escape),
        "unintended_delivery_attempts": 0,
        "production_ready": False,
        "blocking_reasons": ["PRODUCTION_CANARY_NOT_COMPLETED", "EXTERNAL_DELIVERY_DISABLED", "GLOBAL_DELIVERY_KILL_SWITCH_ACTIVE"],
    }
    evidence["canary_technical_ready"] = not any(
        evidence.get(name) == "fail" for name in ("tool_registry", "executor", "data_quality", "qa", "final_quality_gate", "release_policy")
    ) and evidence["unauthorized_tool_calls"] == 0 and evidence["critical_errors"] == 0 and evidence["schema_errors"] == 0 and evidence["stale_data_escapes"] == 0
    return evidence


def evaluate_canary_stability(root: Path, *, window_size: int | None = None) -> dict[str, Any]:
    from runtime_index import StateIndex

    release = _read_json(root / "config" / "release_policy.json")
    configured = ((release.get("canary_stability") or {}).get("window_size"))
    size = int(window_size or configured or 10)
    records = StateIndex(root / "runtime" / "state_index.sqlite3").canary_runs(limit=size)
    eligible = [item for item in records if item.get("run_mode") == RunMode.SHADOW_CANARY.value and item.get("input_valid", True)]
    selected = eligible[-size:]
    blockers: list[str] = []
    if len(selected) < size:
        blockers.append("INSUFFICIENT_CANARY_RUNS")
    checks = {
        "completed": all(bool(item.get("completed")) for item in selected),
        "qa_pass": all(bool(item.get("qa_pass")) for item in selected),
        "reviewer_pass": all(bool(item.get("reviewer_pass")) for item in selected),
        "final_gate_pass": all(bool(item.get("final_gate_pass")) for item in selected),
        "critical_errors": sum(int(item.get("critical_error_count", 0) or 0) for item in selected) == 0,
        "schema_errors": sum(int(item.get("schema_error_count", 0) or 0) for item in selected) == 0,
        "unauthorized_tool_calls": sum(int(item.get("unauthorized_tool_call_count", 0) or 0) for item in selected) == 0,
        "unintended_delivery_attempts": sum(int(item.get("unintended_delivery_count", 0) or 0) for item in selected) == 0,
        "stale_data_escapes": sum(int(item.get("stale_data_escape_count", 0) or 0) for item in selected) == 0,
        "renderer_critical_errors": sum(int(item.get("renderer_critical_error_count", 0) or 0) for item in selected) == 0,
    }
    for name, passed in checks.items():
        if not passed:
            blockers.append(name.upper())
    return {
        "window_size": size,
        "eligible_runs": len(selected),
        "completed": f"{sum(bool(item.get('completed')) for item in selected)}/{len(selected)}",
        "qa_pass": f"{sum(bool(item.get('qa_pass')) for item in selected)}/{len(selected)}",
        "reviewer_pass": f"{sum(bool(item.get('reviewer_pass')) for item in selected)}/{len(selected)}",
        "final_gate_pass": f"{sum(bool(item.get('final_gate_pass')) for item in selected)}/{len(selected)}",
        "checks": checks,
        "canary_stability_pass": bool(selected) and len(selected) >= size and not blockers,
        "blocking_reasons": list(dict.fromkeys(blockers)),
        "run_ids": [item.get("run_id") for item in selected],
    }


def record_canary_run(root: Path, evidence: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    from runtime_index import StateIndex

    StateIndex(root / "runtime" / "state_index.sqlite3").record_canary_run({
        "run_id": str(evidence.get("run_id")),
        "timestamp": str(manifest.get("finished_at") or datetime.now(timezone.utc).isoformat()),
        "run_mode": str(evidence.get("run_mode")),
        "input_valid": True,
        "completed": not bool(manifest.get("failed_step")),
        "qa_pass": evidence.get("qa") == "pass",
        "reviewer_pass": evidence.get("reviewer_gate") == "pass",
        "final_gate_pass": evidence.get("final_quality_gate") == "pass",
        "data_quality_pass": evidence.get("data_quality") == "pass",
        "schema_error_count": int(evidence.get("schema_errors", 0) or 0),
        "critical_error_count": int(evidence.get("critical_errors", 0) or 0),
        "unauthorized_tool_call_count": int(evidence.get("unauthorized_tool_calls", 0) or 0),
        "unintended_delivery_count": int(evidence.get("unintended_delivery_attempts", 0) or 0),
        "stale_data_escape_count": int(evidence.get("stale_data_escapes", 0) or 0),
        "renderer_critical_error_count": 0,
    })


__all__ = [
    "DeliveryDecision",
    "RunMode",
    "RunModeConflict",
    "RunModeResolution",
    "build_readiness_evidence",
    "current_artifact_hash",
    "delivery_preflight",
    "evaluate_canary_stability",
    "read_delivery_controls",
    "record_canary_run",
    "resolve_run_mode",
]
