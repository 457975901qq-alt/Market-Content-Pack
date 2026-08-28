from __future__ import annotations

import json
from pathlib import Path

import run_state
from observability import RunContext, RunObserver, JsonEventLogger, tokyo_now


def _context(run_id: str = "market_20260805_1830_ab12") -> RunContext:
    now = tokyo_now()
    return RunContext(
        run_id=run_id,
        task_type="market_content",
        target_date="2026-08-05",
        session="evening",
        scheduled_at=now,
        started_at=now,
        prompt_version="evening_premarket_watch_v2",
        renderer_version="svg_renderer_v1",
        model_name="rule_template",
    )


def test_json_event_logger_is_single_line_and_redacts_secrets(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = JsonEventLogger(_context(), path)
    logger.emit(
        message="provider failed api_key=super-secret",
        stage="content_generation",
        event="stage_failed",
        status="failed",
        metadata={"authorization": "Bearer top-secret", "nested": {"password": "pw"}},
    )

    raw = path.read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    assert "super-secret" not in raw
    assert "top-secret" not in raw
    assert "\n" not in json.loads(raw)["message"]
    assert json.loads(raw)["run_id"] == "market_20260805_1830_ab12"


def test_run_observer_writes_success_metrics_and_summary(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs" / "runs" / "market_20260805_1830_ab12"
    state_root = tmp_path / "runtime"
    run_id = "market_20260805_1830_ab12"
    state = run_state.create(run_id, "evening_premarket_watch", state_root, output_root)
    source_root = output_root / "market_sources"
    source_root.mkdir(parents=True)
    (source_root / "source_status.json").write_text(
        json.dumps({"source_count": 12, "sources": {"rss": {"status": "healthy"}}}),
        encoding="utf-8",
    )
    observer = RunObserver(_context(run_id), output_root, output_root / "logs", {"minimum_source_count": 10})
    observer.event(stage="input_selection", event="input_selection_completed", status="success")
    observer.stage_started("content_generation")
    observer.stage_finished("content_generation", "success")
    summary = observer.finalize(state, 0)

    summary_path = output_root / "logs" / "run_summary.json"
    metrics_path = output_root / "logs" / "metrics.json"
    assert summary_path.exists()
    assert metrics_path.exists()
    assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "success"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))["metrics"]
    assert metrics["pipeline_run_total"] == 1
    assert metrics["pipeline_success_total"] == 1
    assert metrics["source_count"] == 12
    assert metrics["stage_duration_seconds"]["content_generation"] >= 0
    assert json.loads((output_root / "logs" / "alerts.json").read_text(encoding="utf-8"))["alerts"] == []


def test_run_observer_failure_keeps_summary_and_emits_p1(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs" / "runs" / "market_20260805_1831_cd34"
    state_root = tmp_path / "runtime"
    run_id = "market_20260805_1831_cd34"
    state = run_state.create(run_id, "evening_premarket_watch", state_root, output_root)
    observer = RunObserver(_context(run_id), output_root, output_root / "logs")
    observer.transition("generate_content", "running")
    observer.transition("generate_content", "failed", {"error_code": "json_parse_failed", "message": "invalid JSON"})
    run_state.mark(state, "generate_content", "failed", state_root, error={"error_code": "json_parse_failed", "message": "invalid JSON"})
    summary = observer.finalize(state, 1)

    assert summary["status"] == "failed"
    assert summary["failed_stage"] == "content_generation"
    assert summary["errors"][0]["error_code"] == "MODEL_INVALID_JSON"
    alerts = json.loads((output_root / "logs" / "alerts.json").read_text(encoding="utf-8"))["alerts"]
    assert alerts[0]["level"] == "P1"
    assert alerts[0]["error_code"] == "MODEL_INVALID_JSON"
    assert summary["delivery_enabled"] is False
    assert summary["delivery_status"] == "skipped"


def test_run_id_accepts_legacy_and_short_id_formats() -> None:
    assert run_state.RUN_ID_RE.fullmatch("market_20260805_1830")
    assert run_state.RUN_ID_RE.fullmatch("market_20260805_1830_ab12")
    assert not run_state.RUN_ID_RE.fullmatch("market_20260805_1830_ABC")
