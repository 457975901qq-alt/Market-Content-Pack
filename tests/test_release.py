from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest

from release import (
    ReleaseError,
    ReleaseLock,
    VersionRouter,
    canary_gate,
    checkpoint_compatibility,
    deployment_integrity,
    migration_dry_run,
    prepare_release,
    promote_release,
    release_history,
    release_status,
    rollback_release,
    run_offline_release_drill,
    semver_key,
    verify_package,
)
from scheduler import Scheduler, load_scheduler_config


ROOT = Path(__file__).parents[1]


def make_candidate(tmp_path: Path, version: str = "1.2.3") -> dict:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "edition_profiles.json").write_text('{"editions": {}}\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("print('safe')\n", encoding="utf-8")
    return prepare_release(tmp_path, version, allow_dirty=True, execute_checks=False)


def test_semver_requires_release_version_shape() -> None:
    assert semver_key("1.2.3") == (1, 2, 3)
    with pytest.raises(ReleaseError) as error:
        semver_key("1.2")
    assert error.value.code == "RELEASE_VERSION_INVALID"


def test_prepare_builds_immutable_manifest_and_package_without_env_or_tests(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=never-package-this\n", encoding="utf-8")
    result = make_candidate(tmp_path)
    release_dir = tmp_path / "releases" / "1.2.3"
    assert result["status"] == "candidate"
    assert (release_dir / "artifact_manifest.json").exists()
    assert (release_dir / "release_manifest.json").exists()
    package = Path(result["package"]["path"])
    assert verify_package(package)["status"] == "passed"
    with tarfile.open(package, "r:gz") as archive:
        names = archive.getnames()
    assert ".env" not in names
    assert all(not name.startswith("tests/") for name in names)


def test_canary_gate_pauses_on_insufficient_data_and_quality_regression() -> None:
    assert canary_gate([]).status == "insufficient_data"
    result = canary_gate([{"schema_passed": False}, {"schema_passed": True}])
    assert result.status == "paused"
    assert "SCHEMA_FAILURE" in result.blockers


def test_router_routes_only_declared_jobs_to_candidate() -> None:
    router = VersionRouter("1.0.0", "1.1.0")
    assert router.route("morning_content", stage="canary-1")["selected_version"] == "1.1.0"
    assert router.route("evening_content", stage="canary-1")["selected_version"] == "1.0.0"
    assert router.route("evening_content", stage="active")["selected_version"] == "1.1.0"


def test_checkpoint_and_migration_policy_are_fail_closed() -> None:
    assert checkpoint_compatibility(producer={"schema_version": "v1"}, current={"schema_version": "v2"})["status"] == "blocked"
    assert checkpoint_compatibility(producer={"prompt_version": "old"}, current={"prompt_version": "new"})["decision"] == "requires_restart"
    assert migration_dry_run(ROOT)["destructive_executed"] is False


def test_promote_and_rollback_are_approval_gated_and_preserve_outputs(tmp_path: Path) -> None:
    candidate = make_candidate(tmp_path, "1.2.3")
    preview = promote_release(tmp_path, "1.2.3", stage="active", actor=os.environ.get("USER", ""), role="maintainer", reason="test preview", canary_results=[{}, {}])
    assert preview["status"] == "preview"
    promoted = promote_release(tmp_path, "1.2.3", stage="active", actor=os.environ.get("USER", ""), role="maintainer", reason="offline test approval", approve=True, canary_results=[{}, {}])
    assert promoted["status"] == "promoted"
    marker = tmp_path / "outputs" / "runs" / "keep.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep\n", encoding="utf-8")
    target = make_candidate(tmp_path, "1.2.4")
    rollback_preview = rollback_release(tmp_path, "1.2.3", actor=os.environ.get("USER", ""), role="maintainer", reason="test rollback")
    assert rollback_preview["status"] == "preview"
    rolled = rollback_release(tmp_path, "1.2.3", actor=os.environ.get("USER", ""), role="maintainer", reason="offline rollback approval", approve=True)
    assert rolled["status"] == "rolled_back"
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_release_lock_blocks_second_operation(tmp_path: Path) -> None:
    first = ReleaseLock(tmp_path, "promote", "1.0.0")
    with first:
        with pytest.raises(ReleaseError) as error:
            with ReleaseLock(tmp_path, "rollback", "0.9.0"):
                pass
        assert error.value.code == "RELEASE_LOCKED"


def test_deployment_reports_drift_and_status_history(tmp_path: Path) -> None:
    make_candidate(tmp_path)
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments" / "release_state.json").write_text(json.dumps({"active_version": "1.2.3"}), encoding="utf-8")
    assert deployment_integrity(tmp_path)["status"] == "passed"
    (tmp_path / "app.py").write_text("tampered\n", encoding="utf-8")
    assert deployment_integrity(tmp_path)["status"] == "blocked"
    assert release_status(tmp_path)["integrity"]["status"] == "blocked"
    assert release_history(tmp_path)["releases"][0]["version"] == "1.2.3"


def test_scheduler_persists_release_route_and_blocks_version_drift(tmp_path: Path) -> None:
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments" / "release_state.json").write_text(json.dumps({"active_version": "1.0.0", "candidate_version": "1.1.0", "canary_stage": "canary-1", "release_id": "r1"}), encoding="utf-8")
    scheduler = Scheduler(tmp_path, tmp_path / "runtime" / "state_index.sqlite3", load_scheduler_config(ROOT))
    item = scheduler.enqueue_today()
    morning = next(job for job in item if job["job_type"] == "morning_content")
    evening = next(job for job in item if job["job_type"] == "evening_content")
    assert morning["requested_version"] == "1.1.0"
    assert morning["resolved_version"] == "1.1.0"
    assert evening["resolved_version"] == "1.0.0"


def test_six_offline_release_drills_pass(tmp_path: Path) -> None:
    report = run_offline_release_drill(tmp_path)
    assert report["offline"] is True
    assert report["external_delivery"] is False
    assert len(report["scenarios"]) == 6
    assert report["passed"] is True
