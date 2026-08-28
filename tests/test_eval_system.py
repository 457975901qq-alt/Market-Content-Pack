from __future__ import annotations

from evals.evaluators.deterministic import evaluate_case
from evals.evaluators.llm_judges import JudgeConfig, backoff_seconds, dynamic_batch_size, judge_batch, parse_judge_response
from evals.experiments.run_market_experiment import run_full
from evals.datasets.loader import load_dataset
from pathlib import Path
import json
import evals.phoenix_adapter as phoenix_adapter
from build_daily_market_pack import _build_offline_evaluation_case
from production_preflight import evaluate_preflight
from image_renderer import render_image_pack, validate_image_pack
from delivery_gate import EmailDeliveryAdapter, WebhookDeliveryAdapter, authorize_delivery
from production_canary import validate_canary
from deliver_run import deliver_run
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


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


def test_pipeline_offline_case_contains_complete_reference_schema() -> None:
    case = _build_offline_evaluation_case(
        "market_acceptance_1",
        "evening_premarket_watch",
        [{"source_url": "https://example.invalid/source-1"}],
        {"date": "2026-07-19", "summary": "market summary", "analysis_text": "market observation"},
    )

    assert set(case) == {"case_id", "edition", "input", "reference"}
    assert set(case["reference"]) == {
        "required_facts",
        "required_sources",
        "expected_theme",
        "allowed_tickers",
        "forbidden_claims",
        "expected_result",
    }
    assert case["reference"]["expected_theme"] == "market observation"
    assert evaluate_case(case)["schema_completeness"]["score"] == 1.0


def test_pipeline_offline_case_accepts_structured_analysis_title() -> None:
    case = _build_offline_evaluation_case(
        "market_acceptance_2",
        "evening_premarket_watch",
        [],
        {"date": "2026-07-19", "analysis_text": {"title": "market observation"}},
    )

    assert case["reference"]["expected_theme"] == "market observation"


def test_production_preflight_fails_closed_when_delivery_capabilities_are_missing(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "config" / "evaluation_policy.json").write_text(
        json.dumps({"allow_delivery": False, "allow_production_update": False}),
        encoding="utf-8",
    )
    (tmp_path / "config" / "tool_routing_policy.json").write_text(json.dumps({"tools": {}}), encoding="utf-8")
    (tmp_path / "reports" / "canary_self_healing_report.json").write_text(
        json.dumps({"production_ready": False}),
        encoding="utf-8",
    )

    report = evaluate_preflight(tmp_path)

    assert report["ready"] is False
    assert {item["code"] for item in report["blockers"]} >= {
        "delivery_policy_enabled",
        "production_update_policy_enabled",
        "publish_adapter_available",
        "image_pipeline_available",
        "production_canary_ready",
    }


def test_production_preflight_accepts_completed_shadow_artifact_when_capabilities_are_enabled(tmp_path: Path, monkeypatch) -> None:
    run_id = "market_20260802_1900"
    (tmp_path / "config").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "runtime" / "shadow" / run_id / "state").mkdir(parents=True)
    (tmp_path / "logs" / "shadow" / run_id).mkdir(parents=True)
    (tmp_path / "config" / "evaluation_policy.json").write_text(
        json.dumps({"allow_delivery": True, "allow_production_update": True}),
        encoding="utf-8",
    )
    (tmp_path / "config" / "delivery_policy.json").write_text(
        json.dumps({"enabled": True, "adapter": "webhook", "endpoint_env": "DELIVERY_WEBHOOK_URL"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", "https://example.invalid/publish")
    (tmp_path / "config" / "tool_routing_policy.json").write_text(
        json.dumps({"tools": {"publish": {"enabled": True}, "generate_images": {"enabled": True, "supported_tasks": ["image_generation"]}}}),
        encoding="utf-8",
    )
    (tmp_path / "reports" / "canary_self_healing_report.json").write_text(
        json.dumps({"production_ready": True}),
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "shadow" / run_id / "state" / f"{run_id}.json").write_text(
        json.dumps({"failed_step": None, "completed_steps": ["offline_evaluation"]}),
        encoding="utf-8",
    )
    (tmp_path / "logs" / "shadow" / run_id / "run_manifest.json").write_text(
        json.dumps({"qa_status": "pass", "mode": "image", "image_qa_status": "pass"}),
        encoding="utf-8",
    )

    report = evaluate_preflight(tmp_path, run_id)

    assert report["ready"] is True
    assert report["blockers"] == []


def test_image_renderer_and_qa_produce_a_valid_svg_pack(tmp_path: Path) -> None:
    content_path = tmp_path / "market_content.json"
    content_path.write_text(
        json.dumps(
            {
                "date": "2026-08-02",
                "edition": "evening_premarket_watch",
                "summary": "市场数据暂缺",
                "key_points": ["等待下一交易时段确认"],
                "risk_factors": ["数据不足"],
                "major_indexes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rendered = render_image_pack(content_path, tmp_path / "images", "market_20260802_1901")
    qa = validate_image_pack(Path(rendered["path"]), content_path, "market_20260802_1901")

    assert rendered["status"] == "pass"
    assert qa["status"] == "pass"
    assert {item["name"] for item in qa["checks"]} == {"dimensions", "content_markers"}


def test_delivery_authorization_fails_closed_without_approval() -> None:
    result = authorize_delivery(
        policy={"allow_delivery": True, "allow_production_update": True},
        run_id="market_20260802_1902",
        artifact_hash="abc",
        dry_run=False,
        adapter_ready=True,
        approval=None,
    )

    assert result.allowed is False
    assert "approval_present" in result.blockers
    assert WebhookDeliveryAdapter().health()["status"] == "unconfigured"


def test_delivery_authorization_accepts_matching_unexpired_approval() -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    result = authorize_delivery(
        policy={"allow_delivery": True, "allow_production_update": True},
        run_id="market_20260802_1903",
        artifact_hash="abc",
        dry_run=False,
        adapter_ready=True,
        approval={
            "run_id": "market_20260802_1903",
            "artifact_hash": "abc",
            "approved": True,
            "approved_by": "operator",
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
        },
        now=now,
    )

    assert result.allowed is True
    assert result.blockers == []


def test_email_delivery_adapter_health_and_publish_are_configured_without_real_smtp() -> None:
    env = {
        "DELIVERY_SMTP_HOST": "smtp.example.test",
        "DELIVERY_SMTP_PORT": "587",
        "DELIVERY_SMTP_USERNAME": "sender@example.test",
        "DELIVERY_SMTP_PASSWORD": "secret",
        "DELIVERY_EMAIL_FROM": "sender@example.test",
        "DELIVERY_EMAIL_TO": "receiver@example.test",
    }
    sent = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            sent["args"] = args
            sent["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message):
            sent["message"] = message

    adapter = EmailDeliveryAdapter(env)
    with patch("delivery_gate.smtplib.SMTP", FakeSMTP):
        result = adapter.publish({"subject": "测试", "text": "正文"}, "market_test_1")

    assert adapter.health()["status"] == "ready"
    assert result["adapter"] == "smtp_email"
    assert sent["message"]["X-Idempotency-Key"] == "market_test_1"


def test_production_canary_requires_real_receipt(tmp_path: Path, monkeypatch) -> None:
    run_id = "market_20260802_1904"
    (tmp_path / "config").mkdir()
    (tmp_path / "runtime" / "shadow" / run_id / "state").mkdir(parents=True)
    (tmp_path / "logs" / "shadow" / run_id).mkdir(parents=True)
    (tmp_path / "config" / "delivery_policy.json").write_text(
        json.dumps({"adapter": "webhook"}), encoding="utf-8"
    )
    (tmp_path / "runtime" / "shadow" / run_id / "state" / f"{run_id}.json").write_text(
        json.dumps({"failed_step": None}), encoding="utf-8"
    )
    (tmp_path / "logs" / "shadow" / run_id / "run_manifest.json").write_text(
        json.dumps({"mode": "image", "image_qa_status": "pass", "content_hash": "abc"}), encoding="utf-8"
    )
    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", "https://example.invalid/publish")

    blocked = validate_canary(tmp_path, run_id)
    passed = validate_canary(
        tmp_path,
        run_id,
        {"run_id": run_id, "artifact_hash": "abc", "status": "sent", "idempotency_key": run_id},
    )

    assert blocked["production_ready"] is False
    assert "receipt_present" in blocked["blockers"]
    assert passed["production_ready"] is True


def test_delivery_entrypoint_requires_explicit_confirmation(tmp_path: Path) -> None:
    result = deliver_run(tmp_path, "market_20260802_1905", {}, False)

    assert result == {
        "status": "blocked",
        "reason": "confirm_production_send_required",
        "run_id": "market_20260802_1905",
    }


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
