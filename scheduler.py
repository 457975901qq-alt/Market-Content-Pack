"""Durable local scheduling, leasing and recovery for the market pipeline.

The scheduler is deliberately separate from business rules.  It owns the
SQLite task queue, while ``build_daily_market_pack.py`` remains the pipeline
executor and ``run_state.py`` remains the JSON/checkpoint source of truth.
All timestamps are Asia/Tokyo and no method in this module enables delivery.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable
from zoneinfo import ZoneInfo

from security import assert_safe_persistence

from edition_profiles import resolve_edition_context
from run_state import artifact_valid


ROOT = Path(__file__).resolve().parent
TOKYO = ZoneInfo("Asia/Tokyo")
PIPELINE_VERSION = "6.3.0"
SCHEMA_VERSION = "validation_models_v1"
JOB_TYPES = ("morning_content", "morning_images", "evening_content", "evening_images")
TERMINAL_STATUSES = {"succeeded", "blocked", "cancelled", "skipped", "dead_letter"}
CLAIMABLE_STATUSES = {"pending", "retry_wait", "recovering"}
STAGES = ("input_selection", "source_collection", "normalization", "content_generation", "schema_validation", "cross_validation", "reviewer_validation", "image_rendering", "image_qa", "delivery")
NON_RETRYABLE_DEFAULTS = {
    "INPUT_DATE_MISMATCH", "INPUT_SESSION_MISMATCH", "CROSS_VALIDATION_CONFLICT", "TEXT_IMAGE_MISMATCH",
    "INVALID_SCHEMA_VERSION", "GOLD_INSTRUMENT_MISMATCH", "SECRET_EXPOSURE_DETECTED", "P0_NOT_BLOCKED",
    "CROSS_DATE_CATCHUP_BLOCKED", "DEPENDENCY_WINDOW_EXPIRED", "DEPENDENCY_FAILED",
}


def now_local() -> datetime:
    return datetime.now(TOKYO)


def iso(value: datetime | None = None) -> str:
    return (value or now_local()).astimezone(TOKYO).isoformat()


def parse_time(value: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in value.split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid_local_time:{value}")
    return hour, minute


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=TOKYO) if parsed.tzinfo is None else parsed).astimezone(TOKYO)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    assert_safe_persistence(payload, path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_run_id(scheduled_at: datetime) -> str:
    return f"market_{scheduled_at.strftime('%Y%m%d_%H%M')}_{uuid.uuid4().hex[:6]}"


def load_scheduler_config(root: Path = ROOT) -> dict[str, Any]:
    path = root / "config" / "scheduler_policy.json"
    payload = _read_json(path, {})
    if not isinstance(payload, dict) or payload.get("timezone") != "Asia/Tokyo":
        raise RuntimeError("scheduler_policy_missing_or_timezone_invalid")
    for job_type in JOB_TYPES:
        if job_type not in payload.get("jobs", {}):
            raise RuntimeError(f"scheduler_job_policy_missing:{job_type}")
    return payload


def logical_job_key(job_type: str, target_date: str, session: str) -> str:
    if job_type not in JOB_TYPES:
        raise ValueError(f"unsupported_job_type:{job_type}")
    return f"{job_type}:{target_date}:{session}"


def scheduled_at_for(job_type: str, target_date: str, config: dict[str, Any]) -> datetime:
    hour, minute = parse_time(str(config["jobs"][job_type]["scheduled_local_time"]))
    return datetime.fromisoformat(target_date).replace(hour=hour, minute=minute, tzinfo=TOKYO)


def _session_for(job_type: str, config: dict[str, Any]) -> str:
    return str(config["jobs"][job_type]["session"])


def _edition_for(job_type: str, config: dict[str, Any]) -> str:
    return str(config["jobs"][job_type]["edition"])


def _safe_status_transition(old: str, new: str) -> None:
    allowed = {
        "pending": {"claimed", "waiting_dependency", "skipped", "blocked", "cancelled"},
        "waiting_dependency": {"pending", "blocked", "skipped", "cancelled"},
        "claimed": {"running", "recovering", "cancelled"},
        "running": {"succeeded", "retry_wait", "recovering", "failed", "blocked", "dead_letter"},
        "retry_wait": {"claimed", "blocked", "skipped", "cancelled"},
        "recovering": {"claimed", "retry_wait", "blocked", "cancelled"},
        "failed": {"retry_wait", "dead_letter", "blocked", "cancelled"},
        "succeeded": set(), "blocked": set(), "cancelled": set(), "skipped": set(), "dead_letter": set(),
    }
    if new not in allowed.get(old, set()):
        raise ValueError(f"invalid_job_status_transition:{old}:{new}")


@dataclass(frozen=True)
class CheckpointCompatibilityResult:
    decision: str
    compatible: bool
    resume_from_stage: str
    reason_codes: tuple[str, ...]
    invalidated_stages: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "compatible": self.compatible,
            "resume_from_stage": self.resume_from_stage,
            "reason_codes": list(self.reason_codes),
            "invalidated_stages": list(self.invalidated_stages),
        }


def _checkpoint_checksum(checkpoint: dict[str, Any]) -> str:
    payload = {key: value for key, value in checkpoint.items() if key != "checksum"}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def evaluate_checkpoint(checkpoint: dict[str, Any], expected: dict[str, Any]) -> CheckpointCompatibilityResult:
    """Validate identity/version/artifacts and choose the smallest safe resume point."""
    if not isinstance(checkpoint, dict):
        return CheckpointCompatibilityResult("restart", False, "input_selection", ("CHECKPOINT_CORRUPT",), STAGES)
    reasons: list[str] = []
    if checkpoint.get("checksum") and checkpoint.get("checksum") != _checkpoint_checksum(checkpoint):
        reasons.append("CHECKPOINT_CHECKSUM_MISMATCH")
    required = ("run_id", "target_date", "session", "last_completed_stage")
    if any(not checkpoint.get(field) for field in required):
        reasons.append("CHECKPOINT_CORRUPT")
    for field, reason in (("run_id", "RUN_ID_MISMATCH"), ("target_date", "INPUT_DATE_MISMATCH"), ("session", "INPUT_SESSION_MISMATCH"), ("input_content_id", "INPUT_CONTENT_ID_MISMATCH"), ("input_hash", "INPUT_HASH_CHANGED"), ("schema_version", "SCHEMA_VERSION_INCOMPATIBLE")):
        if field in expected and checkpoint.get(field) != expected.get(field):
            reasons.append(reason)
    if expected.get("pipeline_version") and checkpoint.get("pipeline_version"):
        expected_parts = str(expected["pipeline_version"]).split(".")
        actual_parts = str(checkpoint["pipeline_version"]).split(".")
        if expected_parts[:2] != actual_parts[:2]:
            reasons.append("PIPELINE_VERSION_INCOMPATIBLE")
    if expected.get("release_version") and checkpoint.get("release_version") != expected.get("release_version"):
        reasons.append("RELEASE_VERSION_INCOMPATIBLE")
    if checkpoint.get("p0_error"):
        return CheckpointCompatibilityResult("blocked", False, "delivery", ("P0_CHECKPOINT_BLOCKED",), ("delivery",))
    if checkpoint.get("artifacts_valid") is False:
        reasons.append("OUTPUT_ARTIFACT_INVALID")
    if reasons:
        return CheckpointCompatibilityResult("restart", False, "input_selection", tuple(dict.fromkeys(reasons)), STAGES)
    last = str(checkpoint.get("last_completed_stage"))
    if last not in STAGES:
        return CheckpointCompatibilityResult("restart", False, "input_selection", ("CHECKPOINT_CORRUPT",), STAGES)
    if expected.get("prompt_version") and checkpoint.get("prompt_version") != expected.get("prompt_version"):
        return CheckpointCompatibilityResult("restart", False, "content_generation", ("PROMPT_VERSION_CHANGED",), STAGES[3:])
    if expected.get("renderer_version") and checkpoint.get("renderer_version") != expected.get("renderer_version"):
        return CheckpointCompatibilityResult("resume", True, "image_rendering", ("RENDERER_VERSION_CHANGED",), STAGES[7:])
    resume_index = min(len(STAGES) - 1, STAGES.index(last) + 1)
    return CheckpointCompatibilityResult("resume", True, STAGES[resume_index], (), STAGES[resume_index:])


class SchedulerStore:
    """Durable queue tables in the existing runtime SQLite database."""

    def __init__(self, path: Path, config: dict[str, Any] | None = None) -> None:
        self.path = path
        self.config = config or {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                logical_job_key TEXT NOT NULL UNIQUE,
                job_type TEXT NOT NULL,
                target_date TEXT NOT NULL,
                session TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                input_dependency TEXT,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                run_id TEXT NOT NULL,
                checkpoint_id TEXT,
                locked_by TEXT,
                lock_expires_at TEXT,
                heartbeat_at TEXT,
                current_stage TEXT,
                next_retry_at TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                last_error_code TEXT,
                last_error_message TEXT,
                pipeline_version TEXT,
                schema_version TEXT,
                prompt_version TEXT,
                renderer_version TEXT,
                requested_version TEXT,
                resolved_version TEXT,
                release_id TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                rerun_of TEXT,
                rerun_reason TEXT,
                requested_by TEXT,
                requested_at TEXT
            );
            CREATE TABLE IF NOT EXISTS job_executions (
                execution_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                trigger_type TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT,
                finished_at TEXT,
                status TEXT NOT NULL,
                resumed_from_stage TEXT,
                last_completed_stage TEXT,
                failed_stage TEXT,
                error_code TEXT,
                error_message TEXT,
                summary_path TEXT,
                FOREIGN KEY(job_id) REFERENCES scheduled_jobs(job_id)
            );
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                execution_id TEXT,
                run_id TEXT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                stage TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS scheduler_metrics (
                metric_name TEXT PRIMARY KEY,
                metric_value INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due ON scheduled_jobs(status, scheduled_at, next_retry_at, priority);
            CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_lock ON scheduled_jobs(status, lock_expires_at, heartbeat_at);
            CREATE INDEX IF NOT EXISTS idx_job_executions_job ON job_executions(job_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, timestamp);
            """)
            existing_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(scheduled_jobs)").fetchall()}
            for name in ("requested_version", "resolved_version", "release_id"):
                if name not in existing_columns:
                    db.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {name} TEXT")

    @contextmanager
    def transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _metric(self, db: sqlite3.Connection, name: str, amount: int = 1) -> None:
        db.execute("INSERT INTO scheduler_metrics(metric_name, metric_value, updated_at) VALUES(?,?,?) ON CONFLICT(metric_name) DO UPDATE SET metric_value=metric_value+excluded.metric_value, updated_at=excluded.updated_at", (name, amount, iso()))

    def _event(self, db: sqlite3.Connection, job_id: str, execution_id: str | None, run_id: str | None, event_type: str, old: str | None, new: str | None, stage: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        db.execute("INSERT INTO job_events(job_id, execution_id, run_id, timestamp, event_type, from_status, to_status, stage, metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, execution_id, run_id, iso(), event_type, old, new, stage, _json(metadata or {})))

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            item["payload"] = {}
        return item

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._row(db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone())

    def get_by_key(self, key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._row(db.execute("SELECT * FROM scheduled_jobs WHERE logical_job_key=?", (key,)).fetchone())

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if status:
                rows = db.execute("SELECT * FROM scheduled_jobs WHERE status=? ORDER BY scheduled_at, priority DESC", (status,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM scheduled_jobs ORDER BY scheduled_at, priority DESC").fetchall()
        return [self._row(row) for row in rows if row is not None]

    def enqueue(self, job_type: str, target_date: str, session: str, scheduled_at: datetime, trigger_type: str = "scheduled", *, run_id: str | None = None, payload: dict[str, Any] | None = None, requested_by: str | None = None, rerun_of: str | None = None, rerun_reason: str | None = None, requested_version: str | None = None, resolved_version: str | None = None, release_id: str | None = None) -> tuple[dict[str, Any], bool]:
        key = logical_job_key(job_type, target_date, session)
        policy = self.config.get("jobs", {}).get(job_type, {})
        dependency_type = policy.get("depends_on")
        dependency_key = logical_job_key(dependency_type, target_date, session) if dependency_type else None
        job_id = f"job_{target_date.replace('-', '')}_{job_type}"
        created = iso()
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM scheduled_jobs WHERE logical_job_key=?", (key,)).fetchone()
            if existing is not None:
                self._metric(db, "duplicate_job_prevented_total")
                return self._row(existing), False
            status = "waiting_dependency" if dependency_key else "pending"
            db.execute("""INSERT INTO scheduled_jobs(job_id, logical_job_key, job_type, target_date, session, scheduled_at, trigger_type, input_dependency, status, priority, attempt, max_attempts, run_id, created_at, pipeline_version, schema_version, prompt_version, renderer_version, requested_version, resolved_version, release_id, payload_json, rerun_of, rerun_reason, requested_by, requested_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (job_id, key, job_type, target_date, session, iso(scheduled_at), trigger_type, dependency_key, status, int(policy.get("priority", 0)), 0, int(self.config.get("retry", {}).get("max_task_attempts", 3)), run_id or _new_run_id(scheduled_at), created, PIPELINE_VERSION, SCHEMA_VERSION, None, None, requested_version, resolved_version, release_id, _json(payload or {}), rerun_of, rerun_reason, requested_by, created if requested_by else None))
            self._event(db, job_id, None, run_id or db.execute("SELECT run_id FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()[0], "JOB_ENQUEUED", None, status, metadata={"trigger_type": trigger_type})
            return self._row(db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()), True

    def update_payload(self, job_id: str, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("UPDATE scheduled_jobs SET payload_json=? WHERE job_id=?", (_json(payload), job_id))

    def _dependency_state(self, db: sqlite3.Connection, dependency_key: str | None) -> tuple[str | None, dict[str, Any] | None]:
        if not dependency_key:
            return None, None
        row = db.execute("SELECT * FROM scheduled_jobs WHERE logical_job_key=?", (dependency_key,)).fetchone()
        if row is None:
            return "missing", None
        return str(row["status"]), self._row(row)

    def _misfire(self, row: sqlite3.Row, current: datetime) -> tuple[str, str] | None:
        target = datetime.fromisoformat(str(row["target_date"])).date()
        if target < current.date() and not bool(self.config.get("misfire", {}).get("cross_date_catchup_enabled", False)):
            return "blocked", "CROSS_DATE_CATCHUP_BLOCKED"
        policy = self.config.get("jobs", {}).get(row["job_type"], {})
        scheduled = parse_dt(row["scheduled_at"]) or current
        grace = int(policy.get("grace_seconds", 0))
        if target == current.date() and current > scheduled + timedelta(seconds=grace):
            return "skipped", "SCHEDULE_MISFIRE_EXPIRED"
        return None

    def _promote_dependencies(self, db: sqlite3.Connection, current: datetime) -> None:
        rows = db.execute("SELECT * FROM scheduled_jobs WHERE status='waiting_dependency'").fetchall()
        for row in rows:
            dependency_status, _ = self._dependency_state(db, row["input_dependency"])
            if dependency_status == "succeeded":
                _safe_status_transition("waiting_dependency", "pending")
                db.execute("UPDATE scheduled_jobs SET status='pending' WHERE job_id=?", (row["job_id"],))
                self._event(db, row["job_id"], None, row["run_id"], "DEPENDENCY_SATISFIED", "waiting_dependency", "pending")
            elif dependency_status in {"failed", "blocked", "dead_letter", "cancelled", "skipped"}:
                _safe_status_transition("waiting_dependency", "blocked")
                db.execute("UPDATE scheduled_jobs SET status='blocked', finished_at=?, last_error_code=?, last_error_message=? WHERE job_id=?", (iso(current), "DEPENDENCY_FAILED", f"dependency_status:{dependency_status}", row["job_id"]))
                self._event(db, row["job_id"], None, row["run_id"], "DEPENDENCY_FAILED", "waiting_dependency", "blocked", metadata={"dependency_status": dependency_status})
            else:
                self._metric(db, "dependency_wait_total")

    def claim(self, worker_id: str, current: datetime | None = None) -> dict[str, Any] | None:
        current = (current or now_local()).astimezone(TOKYO)
        with self.transaction() as db:
            self._promote_dependencies(db, current)
            rows = db.execute("SELECT * FROM scheduled_jobs WHERE status IN ('pending','retry_wait','recovering') AND scheduled_at<=? AND (next_retry_at IS NULL OR next_retry_at<=?) AND (lock_expires_at IS NULL OR lock_expires_at<=?) ORDER BY priority DESC, scheduled_at, id", (iso(current), iso(current), iso(current))).fetchall()
            for row in rows:
                misfire = self._misfire(row, current)
                if misfire:
                    new_status, error_code = misfire
                    old = str(row["status"])
                    _safe_status_transition(old, new_status)
                    db.execute("UPDATE scheduled_jobs SET status=?, finished_at=?, last_error_code=?, last_error_message=? WHERE job_id=?", (new_status, iso(current), error_code, error_code, row["job_id"]))
                    self._event(db, row["job_id"], None, row["run_id"], "MISFIRE", old, new_status, metadata={"error_code": error_code})
                    self._metric(db, "scheduled_job_misfire_total")
                    continue
                dependency_status, _ = self._dependency_state(db, row["input_dependency"])
                if dependency_status and dependency_status != "succeeded":
                    continue
                old = str(row["status"])
                if old not in CLAIMABLE_STATUSES:
                    continue
                execution_id = f"exec_{uuid.uuid4().hex}"
                lease = int(self.config.get("lease_seconds", 120))
                expires = current + timedelta(seconds=lease)
                updated = db.execute("UPDATE scheduled_jobs SET status='claimed', locked_by=?, lock_expires_at=?, heartbeat_at=?, attempt=attempt+1, started_at=COALESCE(started_at,?), current_stage=? WHERE job_id=? AND status=? AND (lock_expires_at IS NULL OR lock_expires_at<=?)", (worker_id, iso(expires), iso(current), iso(current), "input_selection", row["job_id"], old, iso(current))).rowcount
                if updated != 1:
                    self._metric(db, "job_claim_conflict_total")
                    continue
                attempt = int(row["attempt"]) + 1
                db.execute("INSERT INTO job_executions(execution_id, job_id, run_id, attempt, trigger_type, worker_id, started_at, heartbeat_at, status, resumed_from_stage) VALUES(?,?,?,?,?,?,?,?,?,?)", (execution_id, row["job_id"], row["run_id"], attempt, row["trigger_type"], worker_id, iso(current), iso(current), "claimed", None))
                self._event(db, row["job_id"], execution_id, row["run_id"], "JOB_CLAIMED", old, "claimed", "input_selection", metadata={"worker_id": worker_id, "attempt": attempt})
                self._metric(db, "scheduled_job_total")
                return self._row(db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (row["job_id"],)).fetchone()) | {"execution_id": execution_id, "attempt": attempt}
        return None

    def mark_running(self, job: dict[str, Any], worker_id: str, stage: str | None = None) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT status FROM scheduled_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if row is None:
                raise KeyError(job["job_id"])
            _safe_status_transition(str(row["status"]), "running")
            db.execute("UPDATE scheduled_jobs SET status='running', current_stage=?, heartbeat_at=? WHERE job_id=? AND locked_by=?", (stage or "input_selection", iso(), job["job_id"], worker_id))
            db.execute("UPDATE job_executions SET status='running', heartbeat_at=?, resumed_from_stage=? WHERE execution_id=? AND worker_id=?", (iso(), stage or "input_selection", job["execution_id"], worker_id))
            self._event(db, job["job_id"], job["execution_id"], job["run_id"], "JOB_STARTED", "claimed", "running", stage or "input_selection")

    def heartbeat(self, job_id: str, execution_id: str, worker_id: str, current_stage: str | None = None, current: datetime | None = None) -> bool:
        current = (current or now_local()).astimezone(TOKYO)
        lease = int(self.config.get("lease_seconds", 120))
        with self._connect() as db:
            updated = db.execute("UPDATE scheduled_jobs SET heartbeat_at=?, lock_expires_at=?, current_stage=? WHERE job_id=? AND locked_by=? AND status IN ('claimed','running') AND lock_expires_at>?", (iso(current), iso(current + timedelta(seconds=lease)), current_stage, job_id, worker_id, iso(current))).rowcount
            if updated:
                db.execute("UPDATE job_executions SET heartbeat_at=? WHERE execution_id=? AND worker_id=?", (iso(current), execution_id, worker_id))
            return updated == 1

    def complete(self, job: dict[str, Any], worker_id: str, status: str, *, error_code: str | None = None, error_message: str | None = None, failed_stage: str | None = None, summary_path: str | None = None, payload: dict[str, Any] | None = None, retryable: bool = True, current: datetime | None = None) -> str:
        current = (current or now_local()).astimezone(TOKYO)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id=? AND locked_by=?", (job["job_id"], worker_id)).fetchone()
            if row is None:
                raise RuntimeError(f"job_lease_not_owned:{job['job_id']}")
            old = str(row["status"])
            target = status
            next_retry = None
            if status == "succeeded":
                target = "succeeded"
            elif status == "interrupted":
                target = "recovering"
            elif status in {"blocked", "cancelled", "skipped"}:
                target = status
            else:
                code = str(error_code or "TASK_FAILED")
                non_retryable = code in set(self.config.get("non_retryable_error_codes", [])) | NON_RETRYABLE_DEFAULTS or not retryable
                if non_retryable:
                    target = "blocked"
                elif int(row["attempt"]) >= int(row["max_attempts"]):
                    target = "dead_letter"
                else:
                    target = "retry_wait"
                    retry = self.config.get("retry", {})
                    delay = min(float(retry.get("maximum_delay_seconds", 900)), float(retry.get("initial_delay_seconds", 60)) * (float(retry.get("multiplier", 2)) ** max(0, int(row["attempt"]) - 1)))
                    next_retry = current + timedelta(seconds=delay)
            _safe_status_transition(old, target)
            final_payload = payload if payload is not None else json.loads(row["payload_json"] or "{}")
            if not isinstance(final_payload, dict):
                final_payload = {}
            db.execute("UPDATE scheduled_jobs SET status=?, locked_by=NULL, lock_expires_at=NULL, heartbeat_at=?, finished_at=?, next_retry_at=?, last_error_code=?, last_error_message=?, current_stage=?, checkpoint_id=COALESCE(?, checkpoint_id), payload_json=? WHERE job_id=?", (target, iso(current), iso(current) if target in TERMINAL_STATUSES else None, iso(next_retry) if next_retry else None, error_code, (error_message or "")[:1000] if error_message else None, failed_stage, final_payload.get("checkpoint_id"), _json(final_payload), row["job_id"]))
            execution_status = "interrupted" if status == "interrupted" else target
            db.execute("UPDATE job_executions SET status=?, heartbeat_at=?, finished_at=?, last_completed_stage=?, failed_stage=?, error_code=?, error_message=?, summary_path=? WHERE execution_id=?", (execution_status, iso(current), iso(current), final_payload.get("last_completed_stage") if isinstance(final_payload, dict) else None, failed_stage, error_code, (error_message or "")[:1000] if error_message else None, summary_path, job["execution_id"]))
            event = "JOB_SUCCEEDED" if target == "succeeded" else "JOB_INTERRUPTED" if target == "recovering" else "JOB_RETRY_WAIT" if target == "retry_wait" else "JOB_DEAD_LETTER" if target == "dead_letter" else "JOB_BLOCKED"
            self._event(db, row["job_id"], job["execution_id"], row["run_id"], event, old, target, failed_stage, {"error_code": error_code, "next_retry_at": iso(next_retry) if next_retry else None})
            if target == "succeeded":
                self._metric(db, "scheduled_job_success_total")
            elif target in {"blocked", "dead_letter"}:
                self._metric(db, "scheduled_job_failure_total")
            if target == "retry_wait":
                self._metric(db, "task_retry_total")
            if target == "dead_letter":
                self._metric(db, "dead_letter_total")
            if target == "recovering":
                self._metric(db, "checkpoint_resume_total")
            return target

    def recover_stale(self, current: datetime | None = None) -> dict[str, Any]:
        current = (current or now_local()).astimezone(TOKYO)
        report = {"scanned_jobs": 0, "stale_jobs": [], "resumed_jobs": [], "retried_jobs": [], "blocked_jobs": [], "skipped_jobs": [], "duplicate_jobs_prevented": 0, "errors": []}
        threshold = int(self.config.get("stale_worker_threshold_seconds", 150))
        with self.transaction() as db:
            self._promote_dependencies(db, current)
            rows = db.execute("SELECT * FROM scheduled_jobs WHERE status IN ('claimed','running','recovering','retry_wait','waiting_dependency','pending')").fetchall()
            report["scanned_jobs"] = len(rows)
            for row in rows:
                job_id = row["job_id"]
                if row["status"] in {"claimed", "running"}:
                    heartbeat = parse_dt(row["heartbeat_at"])
                    expires = parse_dt(row["lock_expires_at"])
                    if (expires and expires <= current) or (heartbeat and heartbeat <= current - timedelta(seconds=threshold)):
                        execution = db.execute("SELECT execution_id FROM job_executions WHERE job_id=? AND status IN ('claimed','running') ORDER BY started_at DESC LIMIT 1", (job_id,)).fetchone()
                        if execution:
                            db.execute("UPDATE job_executions SET status='interrupted', finished_at=?, error_code=?, error_message=? WHERE execution_id=?", (iso(current), "STALE_JOB_DETECTED", "lease_or_heartbeat_expired", execution[0]))
                            execution_id = execution[0]
                        else:
                            execution_id = None
                        db.execute("UPDATE scheduled_jobs SET status='recovering', locked_by=NULL, lock_expires_at=NULL, heartbeat_at=?, last_error_code=?, last_error_message=? WHERE job_id=?", (iso(current), "STALE_JOB_DETECTED", "lease_or_heartbeat_expired", job_id))
                        self._event(db, job_id, execution_id, row["run_id"], "RECOVERY_STARTED", row["status"], "recovering", row["current_stage"], {"reason": "lease_or_heartbeat_expired"})
                        self._metric(db, "stale_job_detected_total")
                        report["stale_jobs"].append(job_id)
                elif row["status"] == "recovering":
                    report["resumed_jobs"].append(job_id)
                elif row["status"] in {"pending", "retry_wait"}:
                    misfire = self._misfire(row, current)
                    if misfire:
                        new_status, error_code = misfire
                        _safe_status_transition(str(row["status"]), new_status)
                        db.execute("UPDATE scheduled_jobs SET status=?, finished_at=?, last_error_code=?, last_error_message=? WHERE job_id=?", (new_status, iso(current), error_code, error_code, job_id))
                        self._event(db, job_id, None, row["run_id"], "MISFIRE", row["status"], new_status, metadata={"error_code": error_code})
                        self._metric(db, "scheduled_job_misfire_total")
                        report["blocked_jobs" if new_status == "blocked" else "skipped_jobs"].append(job_id)
                    elif row["status"] == "retry_wait" and (not row["next_retry_at"] or parse_dt(row["next_retry_at"]) <= current):
                        report["retried_jobs"].append(job_id)
                elif row["status"] == "retry_wait" and (not row["next_retry_at"] or parse_dt(row["next_retry_at"]) <= current):
                    report["retried_jobs"].append(job_id)
            self._write_metric_snapshot(db)
        return report

    def _write_metric_snapshot(self, db: sqlite3.Connection) -> None:
        return None

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (ValueError, TypeError, json.JSONDecodeError):
                item["metadata"] = {}
            output.append(item)
        return output

    def executions(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM job_executions WHERE job_id=? ORDER BY started_at", (job_id,)).fetchall()]

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS count FROM scheduled_jobs GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def metrics(self) -> dict[str, int]:
        with self._connect() as db:
            return {str(row["metric_name"]): int(row["metric_value"]) for row in db.execute("SELECT metric_name, metric_value FROM scheduler_metrics").fetchall()}

    def cancel(self, job_id: str, reason: str = "manual_cancel") -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            _safe_status_transition(str(row["status"]), "cancelled")
            db.execute("UPDATE scheduled_jobs SET status='cancelled', finished_at=?, last_error_code=?, last_error_message=? WHERE job_id=?", (iso(), "JOB_CANCELLED", reason, job_id))
            self._event(db, job_id, None, row["run_id"], "JOB_CANCELLED", row["status"], "cancelled", metadata={"reason": reason})
        return self.get(job_id) or {}

    def repair_locks(self, current: datetime | None = None) -> dict[str, Any]:
        current = (current or now_local()).astimezone(TOKYO)
        repaired: list[str] = []
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM scheduled_jobs WHERE status IN ('claimed','running') AND lock_expires_at<=?", (iso(current),)).fetchall()
            for row in rows:
                db.execute("UPDATE scheduled_jobs SET status='recovering', locked_by=NULL, lock_expires_at=NULL, last_error_code='STALE_JOB_DETECTED' WHERE job_id=?", (row["job_id"],))
                self._event(db, row["job_id"], None, row["run_id"], "LOCK_REPAIRED", row["status"], "recovering")
                repaired.append(row["job_id"])
        return {"repaired_jobs": repaired, "active_locks_preserved": True}

    def rerun(self, job_id: str, reason: str, requested_by: str = "manual", force: bool = False) -> dict[str, Any]:
        if not force:
            raise ValueError("force_required_for_rerun")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            old = str(row["status"])
            if old not in TERMINAL_STATUSES:
                raise ValueError(f"rerun_requires_terminal_job:{old}")
            new_run_id = _new_run_id(parse_dt(row["scheduled_at"]) or now_local())
            db.execute("UPDATE scheduled_jobs SET status='pending', run_id=?, trigger_type='manual', attempt=0, next_retry_at=NULL, locked_by=NULL, lock_expires_at=NULL, heartbeat_at=NULL, started_at=NULL, finished_at=NULL, last_error_code=NULL, last_error_message=NULL, rerun_of=?, rerun_reason=?, requested_by=?, requested_at=? WHERE job_id=?", (new_run_id, row["run_id"], reason, requested_by, iso(), job_id))
            self._event(db, job_id, None, new_run_id, "FORCE_RERUN_REQUESTED", old, "pending", metadata={"rerun_of": row["run_id"], "reason": reason, "requested_by": requested_by})
        return self.get(job_id) or {}

    def retry_dead_letter(self, job_id: str, reason: str, requested_by: str = "manual") -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "dead_letter":
                raise ValueError("dead_letter_retry_requires_dead_letter_status")
            target = datetime.fromisoformat(str(row["target_date"])).date()
            if target != now_local().date() and not bool(self.config.get("misfire", {}).get("cross_date_catchup_enabled", False)):
                db.execute("UPDATE scheduled_jobs SET status='blocked', finished_at=?, last_error_code=?, last_error_message=? WHERE job_id=?", (iso(), "CROSS_DATE_CATCHUP_BLOCKED", reason, job_id))
                self._event(db, job_id, None, row["run_id"], "DEAD_LETTER_RETRY_BLOCKED", "dead_letter", "blocked", metadata={"reason": reason, "requested_by": requested_by})
            else:
                db.execute("UPDATE scheduled_jobs SET status='pending', next_retry_at=NULL, locked_by=NULL, lock_expires_at=NULL, requested_by=?, requested_at=?, last_error_code=NULL, last_error_message=NULL WHERE job_id=?", (requested_by, iso(), job_id))
                self._event(db, job_id, None, row["run_id"], "DEAD_LETTER_RETRY_REQUESTED", "dead_letter", "pending", metadata={"reason": reason, "requested_by": requested_by})
        return self.get(job_id) or {}


def validate_dependency(job: dict[str, Any], dependency: dict[str, Any] | None, root: Path = ROOT) -> dict[str, Any]:
    if dependency is None or dependency.get("status") != "succeeded":
        return {"valid": False, "error_code": "DEPENDENCY_FAILED", "reason": "dependency_not_succeeded"}
    if job.get("target_date") != dependency.get("target_date") or job.get("session") != dependency.get("session"):
        return {"valid": False, "error_code": "INPUT_SESSION_MISMATCH", "reason": "dependency_date_or_session_mismatch"}
    payload = dependency.get("payload") or {}
    content_path = Path(str(payload.get("content_path") or ""))
    if not content_path.is_absolute():
        content_path = root / content_path
    if not content_path.exists():
        return {"valid": False, "error_code": "INPUT_PACKAGE_MISSING", "reason": "dependency_content_missing"}
    content = _read_json(content_path, {})
    if not isinstance(content, dict):
        return {"valid": False, "error_code": "SCHEMA_VALIDATION_FAILED", "reason": "dependency_content_invalid_json"}
    expected_edition = "morning_close_review" if job["session"] == "morning" else "evening_premarket_watch"
    if content.get("edition") != expected_edition:
        return {"valid": False, "error_code": "INPUT_SESSION_MISMATCH", "reason": "dependency_edition_mismatch"}
    expected_time = "06:30" if job["session"] == "morning" else "17:30"
    if content.get("scheduled_local_time") != expected_time:
        return {"valid": False, "error_code": "INPUT_SESSION_MISMATCH", "reason": "dependency_scheduled_time_mismatch"}
    content_date = str(content.get("date") or content.get("target_date") or "")
    if content_date != job["target_date"]:
        return {"valid": False, "error_code": "INPUT_DATE_MISMATCH", "reason": "dependency_date_mismatch"}
    expected_hash = payload.get("content_hash")
    actual_hash = _sha256(content_path)
    if expected_hash and expected_hash != actual_hash:
        return {"valid": False, "error_code": "INPUT_HASH_CHANGED", "reason": "dependency_content_hash_changed"}
    return {"valid": True, "content_path": str(content_path.resolve()), "content_hash": actual_hash, "input_content_id": payload.get("input_content_id") or content_path.name}


class Scheduler:
    def __init__(self, root: Path = ROOT, db_path: Path | None = None, config: dict[str, Any] | None = None) -> None:
        self.root = root
        self.config = config or load_scheduler_config(root)
        self.store = SchedulerStore(db_path or root / "runtime" / "state_index.sqlite3", self.config)

    def _release_for_job(self, job_type: str) -> dict[str, Any]:
        state = _read_json(self.root / "deployments" / "release_state.json", {})
        active = state.get("active_version")
        candidate = state.get("candidate_version")
        if not active and not candidate:
            return {}
        try:
            from release import VersionRouter

            routed = VersionRouter(str(active or candidate), str(candidate) if candidate else None).route(job_type, stage=str(state.get("canary_stage") or "active"))
        except (ImportError, TypeError, ValueError):
            routed = {"selected_version": active or candidate}
        return {"requested_version": candidate or active, "resolved_version": routed.get("selected_version"), "release_id": state.get("release_id")}

    def enqueue_today(self, current: datetime | None = None) -> list[dict[str, Any]]:
        current = (current or now_local()).astimezone(TOKYO)
        result = []
        for job_type in JOB_TYPES:
            session = _session_for(job_type, self.config)
            scheduled = scheduled_at_for(job_type, current.date().isoformat(), self.config)
            item, _ = self.store.enqueue(job_type, current.date().isoformat(), session, scheduled, "scheduled", payload={"timezone": "Asia/Tokyo", "scheduled_local_time": self.config["jobs"][job_type]["scheduled_local_time"]}, **self._release_for_job(job_type))
            result.append(item)
        return result

    def trigger(self, job_type: str, current: datetime | None = None) -> dict[str, Any]:
        current = (current or now_local()).astimezone(TOKYO)
        if job_type not in JOB_TYPES:
            raise ValueError(f"unsupported_job_type:{job_type}")
        if job_type.endswith("_images"):
            if current.hour < 6 or (current.hour == 6 and current.minute < 30):
                raise ValueError("MANUAL_TRIGGER_WINDOW_BLOCKED")
            expected_session = "morning" if current.hour < 18 or (current.hour == 18 and current.minute < 30) else "evening"
            if _session_for(job_type, self.config) != expected_session:
                raise ValueError("INPUT_SESSION_MISMATCH")
        scheduled = scheduled_at_for(job_type, current.date().isoformat(), self.config)
        item, _ = self.store.enqueue(job_type, current.date().isoformat(), _session_for(job_type, self.config), scheduled, "manual", payload={"manual_trigger_at": iso(current), "timezone": "Asia/Tokyo"}, requested_by="cli", **self._release_for_job(job_type))
        return item

    def recovery_scan(self, current: datetime | None = None) -> dict[str, Any]:
        report = self.store.recover_stale(current)
        report["generated_at"] = iso(current)
        output = self.root / "outputs" / "scheduler"
        out = output / f"recovery_scan_{now_local().strftime('%Y%m%d_%H%M%S')}.json"
        _write_json(out, report)
        missed = {"generated_at": report["generated_at"], "missed_jobs": report.get("skipped_jobs", []) + report.get("blocked_jobs", []), "skipped_jobs": report.get("skipped_jobs", []), "blocked_jobs": report.get("blocked_jobs", [])}
        missed_path = output / f"missed_jobs_{now_local().strftime('%Y%m%d_%H%M%S')}.json"
        _write_json(missed_path, missed)
        return {**report, "report_path": str(out.resolve()), "missed_report_path": str(missed_path.resolve())}

    def status_report(self, current: datetime | None = None) -> dict[str, Any]:
        current = (current or now_local()).astimezone(TOKYO)
        jobs = self.store.list_jobs()
        counts = self.store.counts()
        worker_status = _read_json(self.root / "outputs" / "scheduler" / "worker_status.json", {})
        active_locks = [{key: item.get(key) for key in ("job_id", "run_id", "status", "locked_by", "lock_expires_at", "heartbeat_at", "current_stage")} for item in jobs if item.get("locked_by")]
        future = [item for item in jobs if item.get("status") in CLAIMABLE_STATUSES and (parse_dt(item.get("scheduled_at")) or current) >= current]
        next_job = min(future, key=lambda item: item["scheduled_at"]) if future else None
        report = {"generated_at": iso(current), "timezone": "Asia/Tokyo", "scheduler_active": True, "worker_count": 1 if worker_status.get("status") in {"started", "scanning", "running"} else 0, "worker_status": worker_status, "counts": {key: counts.get(key, 0) for key in ("pending", "waiting_dependency", "claimed", "running", "retry_wait", "recovering", "failed", "dead_letter", "blocked", "succeeded", "cancelled", "skipped")}, "recent_success": [item for item in jobs if item.get("status") == "succeeded"][-5:], "recent_failure": [item for item in jobs if item.get("status") in {"failed", "blocked", "dead_letter"}][-5:], "active_locks": active_locks, "next_scheduled_job": next_job, "metrics": self.store.metrics(), "today_missed_jobs": [item["job_id"] for item in jobs if item.get("target_date") == current.date().isoformat() and item.get("status") in {"skipped", "blocked"} and item.get("last_error_code", "").startswith("SCHEDULE")], "duplicate_jobs_prevented": self.store.metrics().get("duplicate_job_prevented_total", 0)}
        output = self.root / "outputs" / "scheduler"
        _write_json(output / "scheduler_status.json", report)
        lines = ["# Scheduler Status", "", f"- Timezone: `{report['timezone']}`", f"- Generated: `{report['generated_at']}`", f"- Active: `{report['scheduler_active']}`", "", "## Counts", "", "| Status | Count |", "|---|---:|"]
        lines.extend(f"| {key} | {value} |" for key, value in report["counts"].items())
        lines.extend(["", "## Metrics", "", "| Metric | Value |", "|---|---:|"])
        lines.extend(f"| {key} | {value} |" for key, value in report["metrics"].items())
        (output / "scheduler_status.md").parent.mkdir(parents=True, exist_ok=True)
        (output / "scheduler_status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report


Executor = Callable[[dict[str, Any]], dict[str, Any]]


class Worker:
    def __init__(self, scheduler: Scheduler, worker_id: str | None = None, executor: Executor | None = None) -> None:
        self.scheduler = scheduler
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.executor = executor or self.execute_job
        self.stop_event = Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    def _write_worker_status(self, status: str, current_job: str | None = None) -> None:
        _write_json(self.scheduler.root / "outputs" / "scheduler" / "worker_status.json", {"worker_id": self.worker_id, "status": status, "current_job_id": current_job, "heartbeat_at": iso(), "shutdown_requested": self.stop_event.is_set(), "delivery_enabled": False})

    def execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        expected_release = self.scheduler._release_for_job(job["job_type"]).get("resolved_version")
        if expected_release and job.get("resolved_version") != expected_release:
            return {"status": "blocked", "error_code": "WORKER_VERSION_MISMATCH", "error_message": "job resolved version no longer matches deployment routing", "retryable": False}
        if job["job_type"].endswith("_content"):
            edition = _edition_for(job["job_type"], self.scheduler.config)
            command = [sys.executable, str(self.scheduler.root / "build_daily_market_pack.py"), "--edition", edition, "--provider", "rule_template", "--shadow-run", "--run-id", job["run_id"]]
            completed = subprocess.run(command, cwd=self.scheduler.root, capture_output=True, text=True, timeout=int(self.scheduler.config.get("pipeline_timeout_seconds", 600)), check=False)
            if completed.returncode != 0:
                return {"status": "failed", "error_code": "PIPELINE_FAILED", "error_message": (completed.stderr or completed.stdout)[-1000:], "retryable": True}
            output_root = self.scheduler.root / "outputs" / "shadow" / job["run_id"]
            content_path = output_root / "market_content" / "market_content.json"
            payload = dict(job.get("payload") or {})
            if content_path.exists():
                payload.update({"output_root": str(output_root.resolve()), "content_path": str(content_path.resolve()), "content_hash": _sha256(content_path), "input_content_id": content_path.name})
            return {"status": "succeeded", "payload": payload, "summary_path": str((output_root / "logs" / "run_summary.json").resolve())}
        dependency = self.scheduler.store.get_by_key(job.get("input_dependency")) if job.get("input_dependency") else None
        validation = validate_dependency(job, dependency, self.scheduler.root)
        if not validation.get("valid"):
            return {"status": "failed", "error_code": validation.get("error_code"), "error_message": validation.get("reason"), "retryable": False}
        content_path = Path(validation["content_path"])
        output_root = self.scheduler.root / "outputs" / "shadow" / job["run_id"]
        image_path = output_root / "images" / "market_content.svg"
        log_root = output_root / "logs"
        try:
            from image_renderer import render_image_pack, validate_image_pack

            render_image_pack(content_path, image_path.parent, job["run_id"])
            qa = validate_image_pack(image_path, content_path, job["run_id"])
            _write_json(log_root / "image_qa.json", qa)
            if qa.get("status") != "pass":
                self._write_job_summary(job, output_root, "failed", "IMAGE_QA_FAILED")
                return {"status": "failed", "error_code": "IMAGE_QA_FAILED", "error_message": "image QA gate failed", "retryable": False, "summary_path": str((log_root / "run_summary.json").resolve())}
            self._write_job_summary(job, output_root, "success", None)
            return {"status": "succeeded", "payload": {**(job.get("payload") or {}), "output_root": str(output_root.resolve()), "image_path": str(image_path.resolve()), "input_content_id": validation["input_content_id"], "input_content_hash": validation["content_hash"]}, "summary_path": str((log_root / "run_summary.json").resolve())}
        except Exception as exc:
            self._write_job_summary(job, output_root, "failed", "RENDERER_NOT_REGISTERED")
            return {"status": "failed", "error_code": "RENDERER_NOT_REGISTERED", "error_message": str(exc), "retryable": False, "summary_path": str((log_root / "run_summary.json").resolve())}

    def _write_job_summary(self, job: dict[str, Any], output_root: Path, status: str, error_code: str | None) -> None:
        _write_json(output_root / "logs" / "run_summary.json", {"run_id": job["run_id"], "job_id": job["job_id"], "target_date": job["target_date"], "session": job["session"], "pipeline_version": PIPELINE_VERSION, "release_id": job.get("release_id"), "requested_version": job.get("requested_version"), "resolved_version": job.get("resolved_version"), "started_at": job.get("started_at"), "finished_at": iso(), "status": status, "failed_stage": job.get("current_stage") if status != "success" else None, "error_code": error_code, "delivery_enabled": False, "delivery_status": "skipped"})

    def run_once(self, current: datetime | None = None) -> dict[str, Any] | None:
        if self.stop_event.is_set():
            return None
        self._write_worker_status("scanning")
        self.scheduler.recovery_scan(current)
        job = self.scheduler.store.claim(self.worker_id, current)
        if not job:
            self._write_worker_status("idle")
            return None
        self._write_worker_status("running", job["job_id"])
        self.scheduler.store.mark_running(job, self.worker_id, job.get("current_stage") or "input_selection")
        expected_release = self.scheduler._release_for_job(job["job_type"]).get("resolved_version")
        if expected_release and job.get("resolved_version") != expected_release:
            target = self.scheduler.store.complete(job, self.worker_id, "blocked", error_code="WORKER_VERSION_MISMATCH", error_message="job resolved version no longer matches deployment routing", failed_stage="input_selection", retryable=False, current=current)
            return {"job_id": job["job_id"], "execution_id": job["execution_id"], "run_id": job["run_id"], "status": target, "error_code": "WORKER_VERSION_MISMATCH"}
        heartbeat_stop = Event()
        heartbeat_interval = max(1.0, float(self.scheduler.config.get("heartbeat_interval_seconds", 30)))

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(heartbeat_interval):
                self.scheduler.store.heartbeat(job["job_id"], job["execution_id"], self.worker_id, job.get("current_stage") or "running")

        heartbeat_thread = Thread(target=heartbeat_loop, name=f"heartbeat-{job['job_id']}", daemon=True)
        heartbeat_thread.start()
        try:
            result = self.executor(job)
            status = str(result.get("status", "failed"))
            target = self.scheduler.store.complete(job, self.worker_id, status, error_code=result.get("error_code"), error_message=result.get("error_message"), failed_stage=result.get("failed_stage") or job.get("current_stage"), summary_path=result.get("summary_path"), payload=result.get("payload"), retryable=bool(result.get("retryable", True)), current=current)
            return {"job_id": job["job_id"], "execution_id": job["execution_id"], "run_id": job["run_id"], "status": target, "result": result}
        except KeyboardInterrupt:
            self._write_job_summary(job, self.scheduler.root / "outputs" / "shadow" / job["run_id"], "interrupted", "WORKER_INTERRUPTED")
            target = self.scheduler.store.complete(job, self.worker_id, "interrupted", error_code="WORKER_INTERRUPTED", error_message="worker shutdown requested", failed_stage=job.get("current_stage"), retryable=True, current=current)
            return {"job_id": job["job_id"], "execution_id": job["execution_id"], "run_id": job["run_id"], "status": target}
        except Exception as exc:
            target = self.scheduler.store.complete(job, self.worker_id, "failed", error_code=type(exc).__name__.upper(), error_message=str(exc), failed_stage=job.get("current_stage"), retryable=True, current=current)
            return {"job_id": job["job_id"], "execution_id": job["execution_id"], "run_id": job["run_id"], "status": target, "error": str(exc)}
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            self._write_worker_status("idle" if not self.stop_event.is_set() else "shutting_down")

    def run_forever(self, interval_seconds: float | None = None) -> None:
        interval = float(interval_seconds or self.scheduler.config.get("poll_interval_seconds", 15))
        self._write_worker_status("started")
        try:
            while not self.stop_event.is_set():
                self.run_once()
                self.stop_event.wait(interval)
        finally:
            self._write_worker_status("shutting_down")

    def run_scheduler_forever(self, interval_seconds: float | None = None) -> None:
        """Combined launchd-friendly loop: enqueue today's four jobs, then work."""
        interval = float(interval_seconds or self.scheduler.config.get("poll_interval_seconds", 15))
        self._write_worker_status("started")
        try:
            while not self.stop_event.is_set():
                self.scheduler.enqueue_today()
                self.run_once()
                self.stop_event.wait(interval)
        finally:
            self._write_worker_status("shutting_down")


def write_dead_letter_report(scheduler: Scheduler) -> dict[str, Any]:
    jobs = scheduler.store.list_jobs("dead_letter")
    report = {"generated_at": iso(), "count": len(jobs), "jobs": jobs}
    _write_json(scheduler.root / "outputs" / "scheduler" / "dead_letter_report.json", report)
    return report


def verify_job_checkpoint(scheduler: Scheduler, job_id: str) -> dict[str, Any]:
    job = scheduler.store.get(job_id)
    if not job:
        raise KeyError(job_id)
    payload = job.get("payload") or {}
    checkpoint = payload.get("checkpoint")
    if checkpoint is None:
        run_id = job.get("run_id")
        candidates = [scheduler.root / "runtime" / "state" / f"{run_id}.json", scheduler.root / "runtime" / "shadow" / run_id / "state" / f"{run_id}.json"]
        for candidate in candidates:
            loaded = _read_json(candidate)
            if isinstance(loaded, dict):
                checkpoint = {"run_id": loaded.get("run_id"), "target_date": loaded.get("date"), "session": job.get("session"), "last_completed_stage": loaded.get("current_step") or "input_selection", "artifacts_valid": True}
                break
    if not isinstance(checkpoint, dict):
        result = CheckpointCompatibilityResult("restart", False, "input_selection", ("CHECKPOINT_MISSING",), STAGES)
    else:
        expected = {key: payload[key] for key in ("run_id", "target_date", "session", "input_content_id", "input_hash", "schema_version", "prompt_version", "renderer_version", "release_version") if key in payload}
        result = evaluate_checkpoint(checkpoint, expected)
    output = {"job_id": job_id, "run_id": job.get("run_id"), **result.as_dict(), "verified_at": iso()}
    _write_json(scheduler.root / "outputs" / "scheduler" / f"checkpoint_verify_{job_id}.json", output)
    return output


def run_offline_drill(root: Path = ROOT) -> dict[str, Any]:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="market_scheduler_drill_") as temp:
        drill_root = Path(temp)
        config = load_scheduler_config(root)
        scheduler = Scheduler(drill_root, drill_root / "runtime" / "state_index.sqlite3", config)
        base = datetime(2026, 8, 6, 6, 30, tzinfo=TOKYO)
        jobs = scheduler.enqueue_today(base)
        image = scheduler.store.get_by_key("morning_images:2026-08-06:morning")
        waiting_before = image and image["status"] == "waiting_dependency"
        content = scheduler.store.get_by_key("morning_content:2026-08-06:morning")
        content_done = False
        if content:
            content_done = True
            with scheduler.store.transaction() as db:
                db.execute("UPDATE scheduled_jobs SET status='succeeded', payload_json=? WHERE job_id=?", (_json({"content_path": str(drill_root / "content.json")}), content["job_id"]))
        after = scheduler.store.claim("drill_worker", base + timedelta(minutes=10))
        dependency_ready = after is not None and after["job_type"] == "morning_images"
        conflict = scheduler.store.claim("drill_worker_2", base + timedelta(minutes=10)) is None
        crashed = scheduler.store.get_by_key("evening_content:2026-08-06:evening")
        crash_recovered = False
        if crashed:
            claimed = scheduler.store.claim("crash_worker", base + timedelta(hours=12))
            if claimed:
                scheduler.store.mark_running(claimed, "crash_worker", "content_generation")
                scheduler.store.recover_stale(base + timedelta(hours=13))
                crash_recovered = scheduler.store.get(crashed["job_id"])["status"] == "recovering"
        report = {"generated_at": iso(), "offline": True, "external_delivery": False, "scenarios": {"normal_dependency_run": waiting_before and content_done and dependency_ready, "mid_run_crash_recovery": crash_recovered, "duplicate_claim_competition": conflict, "wrong_session_input": evaluate_checkpoint({"run_id": "r", "target_date": "2026-08-06", "session": "morning", "last_completed_stage": "image_rendering"}, {"run_id": "r", "target_date": "2026-08-06", "session": "evening"}).decision == "restart", "cross_date_recovery": evaluate_checkpoint({"run_id": "r", "target_date": "2026-08-05", "session": "morning", "last_completed_stage": "content_generation"}, {"run_id": "r", "target_date": "2026-08-06", "session": "morning"}).decision == "restart"}}
    report["passed"] = all(report["scenarios"].values())
    output = root / "outputs" / "scheduler"
    _write_json(output / "drill_report.json", report)
    lines = ["# Scheduler Offline Drill", "", f"- Passed: **{report['passed']}**", f"- External delivery: `{report['external_delivery']}`", "", "| Scenario | Result |", "|---|---|"]
    lines.extend(f"| {key} | {value} |" for key, value in report["scenarios"].items())
    (output / "drill_report.md").parent.mkdir(parents=True, exist_ok=True)
    (output / "drill_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


__all__ = ["CheckpointCompatibilityResult", "JOB_TYPES", "ROOT", "Scheduler", "SchedulerStore", "Worker", "evaluate_checkpoint", "logical_job_key", "run_offline_drill", "validate_dependency", "verify_job_checkpoint"]
