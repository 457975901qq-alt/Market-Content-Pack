from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scheduler import Scheduler, SchedulerStore, Worker, evaluate_checkpoint, load_scheduler_config, validate_dependency


TOKYO = datetime.now().astimezone().tzinfo
ROOT = Path(__file__).parents[1]


def fixed(value: str) -> datetime:
    return datetime.fromisoformat(value)


def make_scheduler(tmp_path: Path) -> Scheduler:
    return Scheduler(tmp_path, tmp_path / "runtime" / "state_index.sqlite3", load_scheduler_config(ROOT))


def test_enqueue_is_idempotent_and_uses_fixed_tokyo_slots(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    jobs = scheduler.enqueue_today(fixed("2026-08-06T05:00:00+09:00"))
    assert len(jobs) == 4
    assert {item["logical_job_key"] for item in scheduler.store.list_jobs()} == {
        "morning_content:2026-08-06:morning", "morning_images:2026-08-06:morning",
        "evening_content:2026-08-06:evening", "evening_images:2026-08-06:evening",
    }
    assert scheduler.store.get_by_key("evening_content:2026-08-06:evening")["scheduled_at"].startswith("2026-08-06T17:30")
    scheduler.enqueue_today(fixed("2026-08-06T05:01:00+09:00"))
    assert len(scheduler.store.list_jobs()) == 4
    assert scheduler.store.metrics()["duplicate_job_prevented_total"] == 4


def test_dependency_waits_then_promotes(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    image = scheduler.store.get_by_key("morning_images:2026-08-06:morning")
    assert image["status"] == "waiting_dependency"
    with scheduler.store.transaction() as db:
        db.execute("UPDATE scheduled_jobs SET status='succeeded' WHERE logical_job_key='morning_content:2026-08-06:morning'")
    claimed = scheduler.store.claim("worker", fixed("2026-08-06T06:40:00+09:00"))
    assert claimed and claimed["job_type"] == "morning_images"


def test_atomic_claim_and_heartbeat_prevent_second_worker(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    first = scheduler.store.claim("worker-1", fixed("2026-08-06T06:31:00+09:00"))
    second = scheduler.store.claim("worker-2", fixed("2026-08-06T06:31:01+09:00"))
    assert first is not None
    assert second is None
    scheduler.store.mark_running(first, "worker-1", "content_generation")
    assert scheduler.store.heartbeat(first["job_id"], first["execution_id"], "worker-1", "content_generation", fixed("2026-08-06T06:31:10+09:00"))
    assert scheduler.store.get(first["job_id"])["locked_by"] == "worker-1"


def test_expired_lease_becomes_recovering_and_keeps_run_id(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    job = scheduler.store.claim("worker-1", fixed("2026-08-06T06:31:00+09:00"))
    scheduler.store.mark_running(job, "worker-1", "content_generation")
    run_id = job["run_id"]
    report = scheduler.store.recover_stale(fixed("2026-08-06T10:00:00+09:00"))
    assert job["job_id"] in report["stale_jobs"]
    assert scheduler.store.get(job["job_id"])["status"] == "recovering"
    assert scheduler.store.get(job["job_id"])["run_id"] == run_id


def test_misfire_cross_date_is_blocked(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.store.enqueue("morning_content", "2026-08-05", "morning", fixed("2026-08-05T06:30:00+09:00"))
    job = scheduler.store.claim("worker", fixed("2026-08-06T06:30:00+09:00"))
    assert job is None
    blocked = scheduler.store.get_by_key("morning_content:2026-08-05:morning")
    assert blocked["status"] == "blocked"
    assert blocked["last_error_code"] == "CROSS_DATE_CATCHUP_BLOCKED"


def test_checkpoint_decision_rejects_input_and_restarts_renderer_changes() -> None:
    mismatch = evaluate_checkpoint(
        {"run_id": "r", "target_date": "2026-08-05", "session": "morning", "last_completed_stage": "reviewer_validation", "input_hash": "old", "artifacts_valid": True},
        {"run_id": "r", "target_date": "2026-08-06", "session": "morning", "input_hash": "new"},
    )
    assert mismatch.decision == "restart"
    assert "INPUT_DATE_MISMATCH" in mismatch.reason_codes
    renderer = evaluate_checkpoint(
        {"run_id": "r", "target_date": "2026-08-06", "session": "evening", "last_completed_stage": "reviewer_validation", "renderer_version": "old", "artifacts_valid": True},
        {"run_id": "r", "target_date": "2026-08-06", "session": "evening", "renderer_version": "new"},
    )
    assert renderer.decision == "resume"
    assert renderer.resume_from_stage == "image_rendering"


def test_p0_checkpoint_is_blocked_from_delivery() -> None:
    result = evaluate_checkpoint({"run_id": "r", "target_date": "2026-08-06", "session": "morning", "last_completed_stage": "image_qa", "p0_error": True}, {})
    assert result.decision == "blocked"
    assert result.resume_from_stage == "delivery"


def test_worker_retry_then_success_keeps_run_id(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    attempts = []

    def executor(job):
        attempts.append(job["attempt"])
        return {"status": "failed", "error_code": "SOURCE_TIMEOUT", "error_message": "temporary", "retryable": True}

    worker = Worker(scheduler, "worker", executor)
    first = worker.run_once(fixed("2026-08-06T06:31:00+09:00"))
    job = scheduler.store.get_by_key("morning_content:2026-08-06:morning")
    assert first["status"] == "retry_wait"
    assert job["run_id"] == first["run_id"]
    assert job["next_retry_at"] is not None


def test_non_retryable_error_blocks_and_max_attempts_dead_letter(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    blocked = Worker(scheduler, "blocked-worker", lambda job: {"status": "failed", "error_code": "INPUT_DATE_MISMATCH", "retryable": True})
    assert blocked.run_once(fixed("2026-08-06T06:31:00+09:00"))["status"] == "blocked"

    scheduler2 = make_scheduler(tmp_path / "second")
    scheduler2.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    failing = Worker(scheduler2, "failing-worker", lambda job: {"status": "failed", "error_code": "SOURCE_TIMEOUT", "retryable": True})
    current = fixed("2026-08-06T06:31:00+09:00")
    for _ in range(3):
        result = failing.run_once(current)
        job = scheduler2.store.get_by_key("morning_content:2026-08-06:morning")
        current = datetime.fromisoformat(job["next_retry_at"]) + timedelta(seconds=1) if job["next_retry_at"] else current
    assert result["status"] == "dead_letter"


def test_force_rerun_creates_new_run_without_overwriting_execution_history(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    worker = Worker(scheduler, "worker", lambda job: {"status": "succeeded"})
    result = worker.run_once(fixed("2026-08-06T06:31:00+09:00"))
    old_run_id = result["run_id"]
    rerun = scheduler.store.rerun(result["job_id"], "renderer fix", force=True)
    assert rerun["run_id"] != old_run_id
    assert rerun["rerun_of"] == old_run_id
    assert len(scheduler.store.executions(result["job_id"])) == 1


def test_manual_image_trigger_has_time_window_and_session_safety(tmp_path: Path) -> None:
    scheduler = make_scheduler(tmp_path)
    with pytest.raises(ValueError, match="MANUAL_TRIGGER_WINDOW_BLOCKED"):
        scheduler.trigger("morning_images", fixed("2026-08-06T05:59:00+09:00"))
    morning = scheduler.trigger("morning_images", fixed("2026-08-06T10:00:00+09:00"))
    assert morning["session"] == "morning"
    with pytest.raises(ValueError, match="INPUT_SESSION_MISMATCH"):
        scheduler.trigger("morning_images", fixed("2026-08-06T19:00:00+09:00"))


def test_dependency_validation_rejects_wrong_edition_and_accepts_hash(tmp_path: Path) -> None:
    content_path = tmp_path / "content.json"
    content_path.write_text(json.dumps({"edition": "morning_close_review", "date": "2026-08-06", "scheduled_local_time": "06:30"}), encoding="utf-8")
    scheduler = make_scheduler(tmp_path)
    scheduler.enqueue_today(fixed("2026-08-06T06:30:00+09:00"))
    scheduler.enqueue_today(fixed("2026-08-06T06:31:00+09:00"))
    content = scheduler.store.get_by_key("morning_content:2026-08-06:morning")
    image = scheduler.store.get_by_key("morning_images:2026-08-06:morning")
    content["status"] = "succeeded"
    content["payload"] = {"content_path": str(content_path), "content_hash": __import__("hashlib").sha256(content_path.read_bytes()).hexdigest(), "input_content_id": "content.json"}
    assert validate_dependency(image, content, tmp_path)["valid"] is True
    content["payload"]["content_hash"] = "changed"
    assert validate_dependency(image, content, tmp_path)["error_code"] == "INPUT_HASH_CHANGED"
