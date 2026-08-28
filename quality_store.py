"""SQLite persistence for regression runs and quality trend summaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QualityStore:
    """Versioned quality store; existing state databases are not modified."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS regression_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    regression_run_id TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    baseline_version TEXT NOT NULL,
                    git_commit TEXT,
                    branch TEXT,
                    total_cases INTEGER NOT NULL,
                    passed_cases INTEGER NOT NULL,
                    failed_cases INTEGER NOT NULL,
                    score REAL,
                    hard_gate_passed INTEGER NOT NULL DEFAULT 0,
                    release_gate_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS regression_case_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    regression_run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL,
                    duration_seconds REAL,
                    critical_count INTEGER NOT NULL DEFAULT 0,
                    high_count INTEGER NOT NULL DEFAULT 0,
                    medium_count INTEGER NOT NULL DEFAULT 0,
                    low_count INTEGER NOT NULL DEFAULT 0,
                    fact_match_rate REAL,
                    schema_passed INTEGER,
                    reviewer_passed INTEGER,
                    image_qa_passed INTEGER,
                    text_image_match INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    fallback_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    payload_json TEXT NOT NULL,
                    UNIQUE(regression_run_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS quality_daily_summary (
                    date TEXT PRIMARY KEY,
                    total_runs INTEGER NOT NULL,
                    success_rate REAL,
                    schema_pass_rate REAL,
                    reviewer_pass_rate REAL,
                    image_qa_pass_rate REAL,
                    text_image_match_rate REAL,
                    p0_count INTEGER NOT NULL DEFAULT 0,
                    p1_count INTEGER NOT NULL DEFAULT 0,
                    retry_rate REAL,
                    fallback_rate REAL,
                    average_duration REAL,
                    p95_duration REAL,
                    average_quality_score REAL,
                    sample_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_regression_case_run ON regression_case_results(regression_run_id);
                CREATE INDEX IF NOT EXISTS idx_regression_runs_finished ON regression_runs(finished_at);
                """
            )

    def record_run(self, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO regression_runs
                (regression_run_id, started_at, finished_at, pipeline_version, baseline_version,
                 git_commit, branch, total_cases, passed_cases, failed_cases, score,
                 hard_gate_passed, release_gate_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["regression_run_id"], payload["started_at"], payload["finished_at"],
                    payload["pipeline_version"], payload["baseline_version"], payload.get("git_commit"),
                    payload.get("branch"), payload["total_cases"], payload["passed_cases"],
                    payload["failed_cases"], payload.get("score"), int(bool(payload.get("hard_gate_passed"))),
                    payload["release_gate_status"],
                ),
            )

    def record_case(self, regression_run_id: str, result: dict[str, Any]) -> None:
        dimensions = result.get("dimensions") or {}
        regressions = result.get("regressions") or []
        counts = {level: sum(1 for item in regressions if item.get("severity") == level) for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO regression_case_results
                (regression_run_id, case_id, status, score, duration_seconds, critical_count,
                 high_count, medium_count, low_count, fact_match_rate, schema_passed,
                 reviewer_passed, image_qa_passed, text_image_match, retry_count,
                 fallback_count, error_code, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    regression_run_id, result["case_id"], result["status"], result.get("score"),
                    result.get("duration_seconds"), counts["CRITICAL"], counts["HIGH"], counts["MEDIUM"],
                    counts["LOW"], result.get("fact_match_rate"), _bool_int(result.get("schema_passed")),
                    _bool_int(result.get("reviewer_passed")), _bool_int(result.get("image_qa_passed")),
                    _bool_int(result.get("text_image_match")), int(result.get("retry_count") or 0),
                    int(result.get("fallback_count") or 0), result.get("error_code"),
                    json.dumps(result, ensure_ascii=False),
                ),
            )

    def upsert_daily_summary(self, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO quality_daily_summary
                (date, total_runs, success_rate, schema_pass_rate, reviewer_pass_rate,
                 image_qa_pass_rate, text_image_match_rate, p0_count, p1_count,
                 retry_rate, fallback_rate, average_duration, p95_duration,
                 average_quality_score, sample_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["date"], payload["total_runs"], payload.get("success_rate"),
                    payload.get("schema_pass_rate"), payload.get("reviewer_pass_rate"),
                    payload.get("image_qa_pass_rate"), payload.get("text_image_match_rate"),
                    payload.get("p0_count", 0), payload.get("p1_count", 0), payload.get("retry_rate"),
                    payload.get("fallback_rate"), payload.get("average_duration"),
                    payload.get("p95_duration"), payload.get("average_quality_score"),
                    json.dumps(payload.get("samples", []), ensure_ascii=False),
                ),
            )

    def daily_summaries(self, start_date: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM quality_daily_summary"
        params: tuple[Any, ...] = ()
        if start_date:
            query += " WHERE date >= ?"
            params = (start_date,)
        query += " ORDER BY date"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["samples"] = json.loads(item.pop("sample_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["samples"] = []
            output.append(item)
        return output


def _bool_int(value: Any) -> int | None:
    return None if value is None else int(bool(value))


__all__ = ["QualityStore"]
