from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from reviewer_agent import review_run


def test_reviewer_approves_valid_preview_run(tmp_path: Path) -> None:
    (tmp_path / "market_content").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "market_sources").mkdir()
    (tmp_path / "market_content" / "market_content.json").write_text(json.dumps({"date": "2026-07-19", "edition": "morning_close_review", "preview_data": True}), encoding="utf-8")
    (tmp_path / "market_content" / "douyin.md").write_text("preview", encoding="utf-8")
    (tmp_path / "logs" / "qa_report.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    (tmp_path / "market_sources" / "source_status.json").write_text(json.dumps({"source_count": 0}), encoding="utf-8")
    result = review_run("market_20260719_1200", tmp_path, tmp_path / "review")
    assert result["decision"] == "approve"
    assert (tmp_path / "review" / "review_result.json").exists()


def test_reviewer_accepts_explicit_shadow_qa_path(tmp_path: Path) -> None:
    (tmp_path / "market_content").mkdir()
    (tmp_path / "market_sources").mkdir()
    qa_root = tmp_path / "shadow_logs"
    qa_root.mkdir()
    (tmp_path / "market_content" / "market_content.json").write_text(json.dumps({"date": "2026-07-19", "edition": "morning_close_review", "preview_data": True}), encoding="utf-8")
    (tmp_path / "market_content" / "douyin.md").write_text("preview", encoding="utf-8")
    (qa_root / "qa_report.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    (tmp_path / "market_sources" / "source_status.json").write_text(json.dumps({"source_count": 0}), encoding="utf-8")
    result = review_run("market_20260719_1201", tmp_path, tmp_path / "review", qa_root / "qa_report.json")
    assert result["decision"] == "approve"


def test_model_reviewer_retries_invalid_json_and_normalizes_decision(tmp_path: Path) -> None:
    (tmp_path / "market_content").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "market_sources").mkdir()
    (tmp_path / "market_content" / "market_content.json").write_text(json.dumps({"date": "2026-07-19", "edition": "morning_close_review", "preview_data": True}), encoding="utf-8")
    (tmp_path / "logs" / "qa_report.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    (tmp_path / "market_sources" / "source_status.json").write_text(json.dumps({"source_count": 0}), encoding="utf-8")
    responses = ["{\"decision\":", json.dumps({"decision": "APPROVE", "confidence": 0.9, "critical_findings": [], "warnings": []})]
    with patch.dict(os.environ, {"REVIEWER_PROVIDER": "gemini"}, clear=False), patch("model_providers.call_gemini", side_effect=responses):
        result = review_run("market_20260719_1202", tmp_path, tmp_path / "review")
    assert result["decision"] == "approve"
    assert result["reviewer"]["tool"] == "gemini"
