from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from canary_controls import (
    RunMode,
    RunModeConflict,
    current_artifact_hash,
    delivery_preflight,
    evaluate_canary_stability,
    read_delivery_controls,
    resolve_run_mode,
)
from runtime_index import StateIndex


NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


def _write_controls(root: Path, *, kill_switch: object = False, external: object = True, whitelist: list[str] | None = None) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "delivery_policy.json").write_text(json.dumps({
        "enabled": True,
        "global_delivery_kill_switch": kill_switch,
        "external_delivery_enabled": external,
        "target_whitelist": whitelist or ["test"],
        "approval_ttl_seconds": 3600,
    }), encoding="utf-8")
    (root / "config" / "release_policy.json").write_text(json.dumps({
        "external_delivery_enabled": external,
        "canary_stability": {"window_size": 10, "eligible_run_mode": "shadow_canary"},
    }), encoding="utf-8")


def _manifest() -> dict:
    return {
        "run_id": "market_20260823_0100",
        "run_mode": "production_canary",
        "canary_technical_ready": True,
        "canary_stability_pass": True,
        "production_ready": True,
        "qa_status": "pass",
        "content_hash": "content-hash",
        "artifact_hashes": {"market_content.json": "artifact-hash"},
    }


def _approval(manifest: dict, *, run_id: str | None = None, **overrides) -> dict:
    artifact_hash = current_artifact_hash(manifest)
    return {
        "approval_id": "approval-1",
        "run_id": run_id or manifest["run_id"],
        "approved_by": "operator",
        "approved_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "artifact_hash": artifact_hash,
        "content_hash": manifest["content_hash"],
        "allowed_targets": ["test"],
        "status": "APPROVED",
        **overrides,
    }


def _record(root: Path, index: int, **overrides) -> None:
    payload = {
        "run_id": f"market_202608{index:02d}_0100",
        "timestamp": f"2026-08-{index:02d}T01:00:00+00:00",
        "run_mode": "shadow_canary",
        "input_valid": True,
        "completed": True,
        "qa_pass": True,
        "reviewer_pass": True,
        "final_gate_pass": True,
        "data_quality_pass": True,
        "schema_error_count": 0,
        "critical_error_count": 0,
        "unauthorized_tool_call_count": 0,
        "unintended_delivery_count": 0,
        "stale_data_escape_count": 0,
        "renderer_critical_error_count": 0,
        **overrides,
    }
    StateIndex(root / "runtime" / "state_index.sqlite3").record_canary_run(payload)


def test_shadow_canary_is_authoritative_from_flag() -> None:
    result = resolve_run_mode(shadow_run=True)
    assert result.mode is RunMode.SHADOW_CANARY
    assert result.resolved_from == "flag"


def test_dry_run_denies_delivery_mode() -> None:
    assert resolve_run_mode(dry_run=True).mode is RunMode.DRY_RUN


def test_production_mode_is_explicit_but_not_authorized() -> None:
    assert resolve_run_mode(cli_mode="production").mode is RunMode.PRODUCTION


def test_conflicting_cli_and_environment_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("DAILY_MARKET_RUN_MODE", "production")
    with pytest.raises(RunModeConflict):
        resolve_run_mode(cli_mode="shadow_canary", env=None)


def test_kill_switch_true_blocks_controls(tmp_path: Path) -> None:
    _write_controls(tmp_path, kill_switch=True)
    assert read_delivery_controls(tmp_path)["kill_switch_active"] is True


def test_missing_kill_switch_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "delivery_policy.json").write_text(json.dumps({"external_delivery_enabled": True}), encoding="utf-8")
    assert read_delivery_controls(tmp_path)["kill_switch_active"] is True


def test_invalid_kill_switch_fails_closed(tmp_path: Path) -> None:
    _write_controls(tmp_path, kill_switch="sometimes")
    controls = read_delivery_controls(tmp_path)
    assert controls["kill_switch_active"] is True
    assert "KILL_SWITCH_CONFIG_MISSING_OR_INVALID" in controls["config_reasons"]


def test_external_delivery_disabled_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path, external=False)
    decision = delivery_preflight(tmp_path, run_id="market_20260823_0100", run_mode="production_canary", manifest=_manifest(), target="test")
    assert decision.allowed is False
    assert "EXTERNAL_DELIVERY_DISABLED" in decision.blockers


def test_missing_approval_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path)
    decision = delivery_preflight(tmp_path, run_id="market_20260823_0100", run_mode="production_canary", manifest=_manifest(), target="test", now=NOW)
    assert "DELIVERY_APPROVAL_MISSING" in decision.blockers


def test_expired_approval_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path)
    manifest = _manifest()
    approval = _approval(manifest, expires_at=(NOW - timedelta(seconds=1)).isoformat())
    decision = delivery_preflight(tmp_path, run_id=manifest["run_id"], run_mode="production_canary", manifest=manifest, approval=approval, target="test", now=NOW)
    assert "DELIVERY_APPROVAL_EXPIRED" in decision.blockers


def test_approval_run_mismatch_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path)
    manifest = _manifest()
    decision = delivery_preflight(tmp_path, run_id=manifest["run_id"], run_mode="production_canary", manifest=manifest, approval=_approval(manifest, run_id="market_20260823_0200"), target="test", now=NOW)
    assert "DELIVERY_APPROVAL_RUN_MISMATCH" in decision.blockers


def test_artifact_hash_changed_after_approval_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path)
    manifest = _manifest()
    approval = _approval(manifest)
    manifest["artifact_hashes"]["market_quotes.json"] = "changed"
    decision = delivery_preflight(tmp_path, run_id=manifest["run_id"], run_mode="production_canary", manifest=manifest, approval=approval, target="test", now=NOW)
    assert "DELIVERY_ARTIFACT_HASH_MISMATCH" in decision.blockers


def test_content_hash_changed_after_approval_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path)
    manifest = _manifest()
    approval = _approval(manifest)
    manifest["content_hash"] = "changed"
    decision = delivery_preflight(tmp_path, run_id=manifest["run_id"], run_mode="production_canary", manifest=manifest, approval=approval, target="test", now=NOW)
    assert "DELIVERY_CONTENT_HASH_MISMATCH" in decision.blockers


def test_target_not_whitelisted_blocks(tmp_path: Path) -> None:
    _write_controls(tmp_path, whitelist=["other"])
    manifest = _manifest()
    decision = delivery_preflight(tmp_path, run_id=manifest["run_id"], run_mode="production_canary", manifest=manifest, approval=_approval(manifest), target="test", now=NOW)
    assert "DELIVERY_TARGET_NOT_WHITELISTED" in decision.blockers


def test_nine_pass_runs_are_insufficient(tmp_path: Path) -> None:
    for index in range(1, 10):
        _record(tmp_path, index)
    result = evaluate_canary_stability(tmp_path)
    assert result["canary_stability_pass"] is False
    assert "INSUFFICIENT_CANARY_RUNS" in result["blocking_reasons"]


def test_ten_pass_runs_pass_stability(tmp_path: Path) -> None:
    for index in range(1, 11):
        _record(tmp_path, index)
    result = evaluate_canary_stability(tmp_path)
    assert result["canary_stability_pass"] is True
    assert result["eligible_runs"] == 10


def test_one_qa_failure_blocks_stability(tmp_path: Path) -> None:
    for index in range(1, 11):
        _record(tmp_path, index, qa_pass=index != 10)
    result = evaluate_canary_stability(tmp_path)
    assert result["canary_stability_pass"] is False
    assert result["checks"]["qa_pass"] is False


def test_unauthorized_tool_calls_block_stability(tmp_path: Path) -> None:
    for index in range(1, 11):
        _record(tmp_path, index, unauthorized_tool_call_count=1 if index == 10 else 0)
    result = evaluate_canary_stability(tmp_path)
    assert result["canary_stability_pass"] is False
    assert result["checks"]["unauthorized_tool_calls"] is False


def test_unintended_delivery_blocks_stability(tmp_path: Path) -> None:
    for index in range(1, 11):
        _record(tmp_path, index, unintended_delivery_count=1 if index == 10 else 0)
    result = evaluate_canary_stability(tmp_path)
    assert result["canary_stability_pass"] is False
    assert result["checks"]["unintended_delivery_attempts"] is False


def test_resume_does_not_use_old_checkpoint_to_change_mode(monkeypatch) -> None:
    monkeypatch.delenv("DAILY_MARKET_RUN_MODE", raising=False)
    assert resolve_run_mode().mode is RunMode.DRY_RUN


def test_agent_registry_has_no_delivery_control_mutators() -> None:
    from function_calling.registry import build_registry

    names = set(build_registry().keys())
    forbidden = {"set_run_mode", "disable_kill_switch", "enable_external_delivery", "approve_delivery", "modify_release_policy", "modify_whitelist"}
    assert names.isdisjoint(forbidden)


def test_shadow_mode_preflight_is_denied_even_with_approval(tmp_path: Path) -> None:
    _write_controls(tmp_path)
    manifest = _manifest()
    decision = delivery_preflight(tmp_path, run_id=manifest["run_id"], run_mode="shadow_canary", manifest=manifest, approval=_approval(manifest), target="test", now=NOW)
    assert decision.allowed is False
    assert "DENY_WRONG_RUN_MODE" in decision.blockers
