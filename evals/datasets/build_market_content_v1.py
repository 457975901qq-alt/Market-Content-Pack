from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def build() -> dict:
    categories = [("success", 10), ("ollama_output_anomaly", 5), ("gemini_fallback", 5), ("market_data_missing", 5), ("image_qa_renderer_failure", 5)]
    project_root = Path(__file__).resolve().parents[3]
    historical_runs = sorted((project_root / "outputs" / "canary").glob("market_*")) + sorted((project_root / "runtime" / "shadow").glob("market_*"))
    cases = []
    index = 1
    for category, count in categories:
        for offset in range(count):
            case_id = f"market_content_v1_{index:02d}"
            date = "2026-07-19"
            fail = category != "success"
            source = f"source_{index:02d}"
            source_path = historical_runs[index - 1] if index <= len(historical_runs) else None
            cases.append({"case_id": case_id, "edition": "evening_premarket_watch", "input": {"report_date": date, "data_cutoff_date": date, "data_timestamps": [date], "source_ids": [source], "source_urls": [f"https://example.invalid/{source}"], "tickers": ["SPX", "NDX", "DJI"], "text": f"预览案例 {case_id}，主题为市场观察，数据源 {source}。" if not fail else f"预览案例 {case_id}。", "delivery_allowed": not fail}, "reference": {"required_facts": [], "required_sources": [source], "expected_theme": "market_observation", "allowed_tickers": ["SPX", "NDX", "DJI"], "forbidden_claims": ["保证盈利", "确定上涨"], "expected_result": "fail" if fail else "pass"}, "metadata": {"category": category, "fixture": True, "source_kind": "historical_artifact_reference" if source_path else "fixture", "source_run_id": source_path.name if source_path else None, "artifact_root": str(source_path.resolve()) if source_path else None, "expected_failure": fail}})
            index += 1
    return {"dataset_version": "market_content_v1", "created_at": datetime.now(timezone.utc).isoformat(), "case_count": len(cases), "cases": cases}


if __name__ == "__main__":
    target = Path(__file__).with_name("market_content_v1") / "dataset.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
