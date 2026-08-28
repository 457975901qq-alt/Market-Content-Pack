from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateIndex:
    """Small local index; the JSON state files remain the source of truth."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    edition TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    state_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS step_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_json TEXT,
                    artifact_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canary_runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    input_valid INTEGER NOT NULL DEFAULT 1,
                    completed INTEGER NOT NULL DEFAULT 0,
                    qa_pass INTEGER NOT NULL DEFAULT 0,
                    reviewer_pass INTEGER NOT NULL DEFAULT 0,
                    final_gate_pass INTEGER NOT NULL DEFAULT 0,
                    data_quality_pass INTEGER NOT NULL DEFAULT 0,
                    schema_error_count INTEGER NOT NULL DEFAULT 0,
                    critical_error_count INTEGER NOT NULL DEFAULT 0,
                    unauthorized_tool_call_count INTEGER NOT NULL DEFAULT 0,
                    unintended_delivery_count INTEGER NOT NULL DEFAULT 0,
                    stale_data_escape_count INTEGER NOT NULL DEFAULT 0,
                    renderer_critical_error_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_step_events_run ON step_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_audit_events_run ON audit_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_canary_runs_timestamp ON canary_runs(timestamp);
            """)

    def upsert_run(self, state: dict[str, Any], state_path: Path) -> None:
        step_values = [item.get("status") for item in state.get("steps", {}).values() if isinstance(item, dict)]
        workflow_complete = bool(step_values) and all(value in {"success", "skipped"} for value in step_values)
        status = "completed" if workflow_complete else ("failed" if state.get("failed_step") else "running")
        with self._connect() as db:
            db.execute("""
                INSERT INTO runs(run_id, edition, status, current_step, delivered, state_path, updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    edition=excluded.edition, status=excluded.status,
                    current_step=excluded.current_step, delivered=excluded.delivered,
                    state_path=excluded.state_path, updated_at=excluded.updated_at
            """, (state["run_id"], state["edition"], status, state.get("current_step"), int(bool(state.get("delivered"))), str(state_path.resolve()), state.get("updated_at") or _now()))

    def record_step(self, run_id: str, step: str, status: str, error: dict[str, Any] | None, artifact_count: int) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO step_events(run_id, step, status, error_json, artifact_count, created_at) VALUES(?,?,?,?,?,?)", (run_id, step, status, json.dumps(error, ensure_ascii=False) if error else None, artifact_count, _now()))

    def audit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO audit_events(run_id, event_type, payload_json, created_at) VALUES(?,?,?,?)", (run_id, event_type, json.dumps(payload, ensure_ascii=False), _now()))

    def record_canary_run(self, payload: dict[str, Any]) -> None:
        columns = (
            "run_id", "timestamp", "run_mode", "input_valid", "completed", "qa_pass",
            "reviewer_pass", "final_gate_pass", "data_quality_pass", "schema_error_count",
            "critical_error_count", "unauthorized_tool_call_count", "unintended_delivery_count",
            "stale_data_escape_count", "renderer_critical_error_count",
        )
        values = tuple(payload.get(column) for column in columns)
        with self._connect() as db:
            db.execute(
                f"INSERT INTO canary_runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                + ",".join(f"{column}=excluded.{column}" for column in columns if column != "run_id"),
                values,
            )

    def canary_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM canary_runs ORDER BY timestamp DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
            # PRAGMA table_info returns (cid, name, type, ...); use the
            # column name so stability evaluation sees the recorded values.
            names = [item[1] for item in db.execute("PRAGMA table_info(canary_runs)").fetchall()]
        return [dict(zip(names, row)) for row in reversed(rows)]

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT run_id, step, status, error_json, artifact_count, created_at FROM step_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [{"run_id": row[0], "step": row[1], "status": row[2], "error": json.loads(row[3]) if row[3] else None, "artifact_count": row[4], "created_at": row[5]} for row in rows]


def index_for_state_root(root: Path) -> StateIndex:
    return StateIndex(root / "state_index.sqlite3")
