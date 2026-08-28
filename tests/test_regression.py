from __future__ import annotations

import json
import shutil
from pathlib import Path

from quality_store import QualityStore
from tools.regression import build_release_gate, build_trend, load_cases, score_case, update_baseline


ROOT = Path(__file__).parents[1]


def test_fixed_regression_dataset_has_normal_goldens_and_failure_cases() -> None:
    cases = load_cases(ROOT)
    assert len(cases) >= 20
    normal = [case for case in cases if case["kind"] == "normal"]
    failure = [case for case in cases if case["kind"] == "failure"]
    assert len(normal) >= 5
    assert len(failure) >= 10
    for case in normal:
        assert (ROOT / "tests" / "regression" / case["baseline_file"]).exists()


def test_normal_case_matches_golden_and_failure_case_is_fail_closed() -> None:
    cases = load_cases(ROOT)
    normal = next(case for case in cases if case["case_id"] == "evening_normal_001")
    failure = next(case for case in cases if case["case_id"] == "date_mismatch_001")
    assert score_case(normal, ROOT)["status"] == "passed"
    failed_result = score_case(failure, ROOT)
    assert failed_result["status"] == "passed"
    assert failed_result["regressions"] == []


def test_hard_fact_change_blocks_release_gate() -> None:
    case = next(case for case in load_cases(ROOT) if case["case_id"] == "evening_normal_001")
    candidate = dict(case["candidate"])
    candidate["target_date"] = "2026-08-04"
    result = score_case(case, ROOT, candidate)
    assert result["status"] == "failed"
    assert any(item["severity"] == "CRITICAL" for item in result["regressions"])
    gate = build_release_gate([result], root=ROOT)
    assert gate["status"] == "blocked"


def test_quality_store_creates_l6_2_tables(tmp_path: Path) -> None:
    store = QualityStore(tmp_path / "quality.sqlite3")
    with store._connect() as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    assert {"regression_runs", "regression_case_results", "quality_daily_summary"} <= tables


def test_baseline_is_read_only_without_explicit_approval(tmp_path: Path) -> None:
    regression_root = tmp_path / "tests" / "regression"
    regression_root.mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "regression" / "cases.json", regression_root / "cases.json")
    golden_root = regression_root / "golden"
    golden_root.mkdir()
    source = ROOT / "tests" / "regression" / "golden" / "evening_normal_001.json"
    target = golden_root / source.name
    shutil.copy(source, target)
    before = target.read_bytes()
    preview = update_baseline(tmp_path, "evening_normal_001", approve=False)
    assert preview["approved"] is False
    assert "updated_path" not in preview
    assert target.read_bytes() == before
    assert (tmp_path / "outputs" / "quality" / "baseline_preview_evening_normal_001.json").exists()


def test_trend_marks_missing_comparison_as_insufficient(tmp_path: Path) -> None:
    run_log = tmp_path / "outputs" / "runs" / "market_20260805_1000" / "logs"
    run_log.mkdir(parents=True)
    (run_log / "run_summary.json").write_text(
        json.dumps({
            "run_id": "market_20260805_1000",
            "started_at": "2026-08-05T10:00:00+09:00",
            "finished_at": "2026-08-05T10:00:10+09:00",
            "status": "success",
            "content_review_passed": True,
            "image_qa_passed": None,
            "text_image_match": None,
            "retry_count": 0,
            "fallback_count": 0,
        }),
        encoding="utf-8",
    )
    trend = build_trend(tmp_path, 7)
    assert trend["sample_count"] == 1
    assert trend["status"] == "insufficient_data"
    assert trend["metric_details"]["run_success_rate"]["denominator"] == 1
