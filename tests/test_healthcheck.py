from __future__ import annotations

import json
import time
from pathlib import Path

from healthcheck import collect_report, record_task_event, write_report


def test_isolated_health_report_does_not_read_global_logs(tmp_path: Path) -> None:
    isolated_logs = tmp_path / "logs" / "shadow" / "market_20260719_1200"
    isolated_reports = tmp_path / "reports" / "shadow" / "market_20260719_1200"
    record_task_event("success", time.monotonic() - 0.1, "morning_close_review", started_epoch=time.time() - 0.1, log_path=isolated_logs / "task_runs.jsonl")
    report = collect_report(task_log=isolated_logs / "task_runs.jsonl", logs_root=isolated_logs)
    assert report["metrics"]["completed_task_count"] == 1
    path = write_report(
        report,
        report_root=isolated_reports,
        task_log=isolated_logs / "task_runs.jsonl",
        logs_root=isolated_logs,
    )
    assert path == isolated_reports / "system_health.md"
    assert json.loads((isolated_reports / "system_health.json").read_text(encoding="utf-8"))["metrics"]["completed_task_count"] == 1
