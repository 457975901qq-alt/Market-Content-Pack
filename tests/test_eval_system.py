from __future__ import annotations

from evals.evaluators.deterministic import evaluate_case
from evals.evaluators.llm_judges import JudgeConfig, backoff_seconds, dynamic_batch_size, judge_batch, parse_judge_response
from evals.experiments.run_market_experiment import run_full
from evals.datasets.loader import load_dataset
from pathlib import Path
import json
import evals.phoenix_adapter as phoenix_adapter


def test_deterministic_evaluator_blocks_missing_source_and_unknown_ticker() -> None:
    case = {
        "case_id": "case_1",
        "edition": "morning_close_review",
        "input": {"source_ids": [], "tickers": ["FAKE"], "report_date": "2026-07-19", "data_cutoff_date": "2026-07-18", "delivery_allowed": True},
        "reference": {"required_sources": ["source_1"], "allowed_tickers": ["SPX"], "expected_result": "fail"},
    }
    result = evaluate_case(case)
    assert result["source_grounding"]["label"] == "fail"
    assert result["ticker_validity"]["label"] == "fail"
    assert result["temporal_consistency"]["label"] == "fail"
    assert result["delivery_decision_accuracy"]["label"] == "fail"


def test_golden_dataset_has_required_categories() -> None:
    dataset = Path(__file__).parents[1] / "evals" / "datasets" / "market_content_v1"
    cases = load_dataset(dataset)
    assert len(cases) == 30
    from collections import Counter
    assert Counter(case["metadata"]["category"] for case in cases) == {
        "success": 10,
        "ollama_output_anomaly": 5,
        "gemini_fallback": 5,
        "market_data_missing": 5,
        "image_qa_renderer_failure": 5,
    }


def test_judge_response_contains_both_metrics_in_one_call() -> None:
    raw = json.dumps({"results": [{"case_id": "c1", "candidate": "ollama", "factual_faithfulness": {"score": 1.0}, "content_usability": {"score": 0.8}}]})
    rows = judge_batch([{"case_id": "c1", "candidate": "ollama"}], JudgeConfig("ollama", "mock"), call=lambda _: raw)
    assert rows["request_count"] == 1
    assert set(rows["results"][0]) >= {"factual_faithfulness", "content_usability"}


def test_invalid_or_missing_case_id_is_rejected() -> None:
    raw = json.dumps({"results": [{"case_id": "other", "factual_faithfulness": {"score": 1}, "content_usability": {"score": 1}}]})
    parsed = judge_batch([{"case_id": "c1", "candidate": "mock"}], JudgeConfig("ollama", "mock", max_retries=0), call=lambda _: raw)
    assert parsed["status"] == "judge_error"


def test_quota_policy_and_dynamic_batch_size() -> None:
    assert backoff_seconds("rpm", 0) == 10
    assert backoff_seconds("rpm", 2) == 40
    assert backoff_seconds("rpd", 0) == 0
    assert dynamic_batch_size([{"text": "x"} for _ in range(10)], 10) == 10
    assert dynamic_batch_size([{"text": "x" * 100000} for _ in range(10)], 10) < 10


def test_cache_and_checkpoint_resume_keep_offline_run_idempotent(tmp_path: Path) -> None:
    dataset = Path(__file__).parents[1] / "evals" / "datasets" / "market_content_v1"
    report_path = tmp_path / "report.json"
    first = run_full(dataset, ["local_template"], 2, "none", True, False, None, False, report_path, False)
    second = run_full(dataset, ["local_template"], 2, "none", True, False, None, True, report_path, False)
    assert first["delivered"] is False
    assert second["delivered"] is False
    assert report_path.exists()


def test_phi_or_provider_failure_does_not_change_delivery() -> None:
    raw = json.dumps({"results": []})
    result = judge_batch([{"case_id": "c1", "candidate": "mock"}], JudgeConfig("gemini", "mock", max_retries=0), call=lambda _: raw)
    assert result["status"] == "judge_error"


def test_phoenix_adapter_is_skipped_without_explicit_enable(monkeypatch) -> None:
    monkeypatch.delenv("PHOENIX_EVAL_ENABLED", raising=False)
    result = phoenix_adapter.create_dataset_and_experiment("market_content_v1", [], {})
    assert result["status"] == "skipped"


def test_phoenix_adapter_registers_dataset_and_experiment_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PHOENIX_EVAL_ENABLED", "true")
    calls = []

    def fake_request(method, path, payload=None, timeout=5.0):
        calls.append((method, path, payload))
        if path.startswith("/v1/datasets?"):
            return {"data": [], "next_cursor": None}
        if path == "/v1/datasets/upload?sync=true":
            return {"data": {"dataset_id": "ds_1", "version_id": "ver_1", "num_created_examples": 1}}
        if path == "/v1/datasets/ds_1":
            return {"data": {"id": "ds_1", "name": "market_content_v1", "version_id": "ver_1"}}
        if path.startswith("/v1/datasets/ds_1/experiments?"):
            return {"data": [], "next_cursor": None}
        if path == "/v1/datasets/ds_1/experiments":
            return {"data": {"id": "exp_1", "name": "market_content_offline"}}
        raise AssertionError(path)

    monkeypatch.setattr(phoenix_adapter, "_request", fake_request)
    result = phoenix_adapter.create_dataset_and_experiment(
        "market_content_v1",
        [{"case_id": "c1", "input": {"text": "fixture"}, "reference": {}, "metadata": {}}],
        {"experiment_name": "market_content_offline", "dataset_version": "market_content_v1", "candidates": ["local_template"]},
    )
    assert result["status"] == "created"
    assert result["dataset_id"] == "ds_1"
    assert result["experiment_id"] == "exp_1"
    upload = next(payload for method, path, payload in calls if path == "/v1/datasets/upload?sync=true")
    assert upload["example_ids"] == ["c1"]
    assert "api_key" not in json.dumps(upload).lower()


def test_phoenix_adapter_failure_is_non_blocking(monkeypatch) -> None:
    monkeypatch.setenv("PHOENIX_EVAL_ENABLED", "true")
    monkeypatch.setattr(phoenix_adapter, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    result = phoenix_adapter.create_dataset_and_experiment("market_content_v1", [], {})
    assert result["status"] == "unavailable"
    assert result["reason"] == "OSError"
