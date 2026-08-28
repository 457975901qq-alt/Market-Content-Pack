#!/usr/bin/env python3
"""Local observability primitives with an optional Phoenix trace exporter.

The market pipeline deliberately keeps observability local and fail-safe.  The
JSON event log, metrics, summary and alerts are useful without Prometheus or
an external notification service; tracing remains optional and must never
block content generation.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from security import assert_safe_persistence, redact_sensitive


TOKYO = ZoneInfo("Asia/Tokyo")
PIPELINE_VERSION = "6.1.0"
DEFAULT_THRESHOLDS: dict[str, float | int] = {
    "pipeline_timeout_seconds": 600,
    "model_timeout_seconds": 120,
    "minimum_source_count": 10,
    "max_stage_retries": 3,
    "qa_failure_rate_threshold": 0.20,
    "consecutive_failure_threshold": 2,
}

CORE_STAGES = (
    "input_selection",
    "source_collection",
    "normalization",
    "market_data_collection",
    "market_data_validation",
    "content_generation",
    "schema_validation",
    "cross_validation",
    "reviewer_validation",
    "image_rendering",
    "image_qa",
    "delivery",
)

STEP_TO_STAGE = {
    "health_check": "input_selection",
    "collect_sources": "source_collection",
    "collect_market_quotes": "market_data_collection",
    "collect_market_data": "market_data_collection",
    "crosscheck_market_quote": "market_data_collection",
    "validate_market_data": "market_data_validation",
    "generate_content": "content_generation",
    "final_validation": "schema_validation",
    "build_review_package": "reviewer_validation",
    "reviewer_agent": "reviewer_validation",
    "reviewer_gate": "reviewer_validation",
    "offline_evaluation": "cross_validation",
    # Local archiving is not external delivery.  Keeping it separate prevents
    # a successful local write from being counted as a publish.
    "archive": "archive",
}

LOGICAL_STEP_TO_STAGE = {
    "collect_news": "source_collection",
    "extract_web_content": "normalization",
    "collect_market_data": "market_data_collection",
    "validate_market_data": "market_data_validation",
    "generate_content": "content_generation",
    "validate_content_consistency": "cross_validation",
    "final_quality_gate": "schema_validation",
}

P0_CODES = {
    "INPUT_DATE_MISMATCH",
    "INPUT_SESSION_MISMATCH",
    "CROSS_VALIDATION_CONFLICT",
    "TEXT_IMAGE_MISMATCH",
}
P1_CODES = {
    "MODEL_INVALID_JSON",
    "MODEL_TIMEOUT",
    "REVIEW_REJECTED",
    "RENDERER_NOT_REGISTERED",
    "IMAGE_QA_FAILED",
    "DELIVERY_FAILED",
}

_SECRET_KEY_WORDS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|cookie|password|secret|token)\s*[:=]\s*([^\s,;]+)"
)


def tokyo_now() -> str:
    return datetime.now(TOKYO).isoformat()


def redact(value: Any, key: str | None = None) -> Any:
    return redact_sensitive(value, key=key)


def _legacy_redact(value: Any, key: str | None = None) -> Any:
    """Recursively remove secrets from log/report payloads."""
    if key and any(word in key.lower() for word in _SECRET_KEY_WORDS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(r"\1=[REDACTED]", value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def stable_error_code(error: Any = None, stage: str | None = None) -> str:
    """Map legacy exception/error names to stable observability codes."""
    payload = error if isinstance(error, dict) else {}
    raw = str(payload.get("error_code") or payload.get("error_type") or "").strip().lower()
    message = str(payload.get("message") or payload.get("raw_message") or "").lower()
    failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    failure_type = str(failure.get("failure_category") or "").lower()
    combined = f"{raw} {message} {failure_type}"
    market_codes = {
        "market_data_future": "MARKET_DATA_FUTURE",
        "market_data_stale": "MARKET_DATA_STALE",
        "market_data_conflict": "MARKET_DATA_CONFLICT",
        "market_data_missing": "MARKET_DATA_MISSING",
        "market_data_not_validated": "MARKET_DATA_NOT_VALIDATED",
        "market_provider_unavailable": "MARKET_PROVIDER_UNAVAILABLE",
    }
    if failure_type in market_codes:
        return market_codes[failure_type]
    if "future_timestamp" in combined or "after_cutoff" in combined or "post_cutoff" in combined:
        return "MARKET_DATA_FUTURE"
    if "date_mismatch" in combined or "date mismatch" in combined:
        return "INPUT_DATE_MISMATCH"
    if "session_mismatch" in combined or "wrong_session" in combined or "session mismatch" in combined:
        return "INPUT_SESSION_MISMATCH"
    if "conflict" in combined or "cross_validation" in combined:
        return "CROSS_VALIDATION_CONFLICT"
    if "text_image" in combined or "content_markers" in combined:
        return "TEXT_IMAGE_MISMATCH"
    if "image_qa" in combined:
        return "IMAGE_QA_FAILED"
    if "renderer_not_registered" in combined or "rendering_error" in combined:
        return "RENDERER_NOT_REGISTERED"
    if "review" in combined and ("reject" in combined or "gate" in combined or "failed" in combined):
        return "REVIEW_REJECTED"
    if "schema" in combined or "invalid_json" in combined or "json_parse" in combined:
        return "MODEL_SCHEMA_FAILURE" if "schema" in combined else "MODEL_INVALID_JSON"
    if "timeout" in combined:
        return "MODEL_TIMEOUT" if stage in {"content_generation", "reviewer_validation"} else "SOURCE_TIMEOUT"
    if "source" in combined and ("empty" in combined or "missing" in combined or "fetch" in combined):
        return "SOURCE_EMPTY" if "empty" in combined else "SOURCE_FETCH_FAILED"
    if "checkpoint" in combined and "corrupt" in combined:
        return "CHECKPOINT_CORRUPTED"
    if "delivery" in combined:
        return "DELIVERY_FAILED"
    if raw == "empty_response":
        return "SOURCE_EMPTY" if stage in {"source_collection", "normalization"} else "MODEL_INVALID_JSON"
    if raw == "market_data_missing":
        return "SOURCE_EMPTY"
    if raw == "quality_gate_failed":
        return "IMAGE_QA_FAILED" if stage == "image_qa" else "MODEL_SCHEMA_FAILURE"
    return raw.upper() if raw and raw.replace("_", "").isalnum() else "UNKNOWN_ERROR"


@dataclass
class RunContext:
    run_id: str
    task_type: str
    target_date: str
    session: str
    scheduled_at: str
    started_at: str
    pipeline_version: str = PIPELINE_VERSION
    schema_version: str = "validation_models_v1"
    prompt_version: str = "unknown"
    renderer_version: str = "disabled"
    model_name: str = "unknown"
    delivery_enabled: bool = False
    checkpoint_resumed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class JsonEventLogger:
    """Write one redacted, machine-readable JSON event per line."""

    def __init__(self, context: RunContext, path: Path) -> None:
        self.context = context
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        level: str = "INFO",
        message: str,
        stage: str,
        event: str,
        status: str,
        attempt: int = 1,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": tokyo_now(),
            "level": level,
            "message": message,
            "run_id": self.context.run_id,
            "task_type": self.context.task_type,
            "session": self.context.session,
            "stage": stage,
            "event": event,
            "status": status,
            "attempt": attempt,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "error_message": error_message,
            "pipeline_version": self.context.pipeline_version,
            "schema_version": self.context.schema_version,
            "prompt_version": self.context.prompt_version,
            "renderer_version": self.context.renderer_version,
            "model_name": self.context.model_name,
        }
        if metadata:
            payload.update(metadata)
        safe = redact(payload)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
        return safe


class RunObserver:
    """Collect stage events and produce one local metrics/summary/alert set."""

    def __init__(self, context: RunContext, output_root: Path, log_root: Path, thresholds: dict[str, Any] | None = None) -> None:
        self.context = context
        self.output_root = output_root
        self.log_root = log_root
        self.observability_root = output_root / "logs"
        self.observability_root.mkdir(parents=True, exist_ok=True)
        self.logger = JsonEventLogger(context, self.observability_root / "events.jsonl")
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._started: dict[str, float] = {}
        self._attempts: dict[str, int] = {}

    def stage_started(self, stage: str, metadata: dict[str, Any] | None = None) -> None:
        self._started[stage] = time.monotonic()
        self._attempts[stage] = self._attempts.get(stage, 0) + 1
        self.logger.emit(message="stage started", stage=stage, event="stage_started", status="running", attempt=self._attempts[stage], metadata=metadata)

    def stage_finished(self, stage: str, status: str, error: Any = None, metadata: dict[str, Any] | None = None) -> None:
        started = self._started.pop(stage, None)
        duration_ms = int((time.monotonic() - started) * 1000) if started is not None else None
        payload = error if isinstance(error, dict) else {}
        code = stable_error_code(payload, stage) if status == "failed" else None
        stack = payload.get("traceback") if payload else None
        self.logger.emit(
            level="ERROR" if status == "failed" else "INFO",
            message="stage failed" if status == "failed" else "stage completed",
            stage=stage,
            event="stage_failed" if status == "failed" else "stage_completed",
            status=status,
            attempt=self._attempts.get(stage, 1),
            duration_ms=duration_ms,
            error_code=code,
            error_message=str(payload.get("message") or payload.get("raw_message") or "")[:4000] if payload else None,
            metadata={**(metadata or {}), **({"error_type": payload.get("error_type"), "error_stack": stack} if payload else {})},
        )

    def transition(self, step: str, status: str, error: Any = None, metadata: dict[str, Any] | None = None) -> None:
        stage = STEP_TO_STAGE.get(step, step)
        if status == "running":
            self.stage_started(stage, {"executor_step": step, **(metadata or {})})
        elif status in {"success", "failed", "skipped"}:
            self.stage_finished(stage, status, error, {"executor_step": step, **(metadata or {})})

    @contextmanager
    def monitor_stage(self, stage: str, metadata: dict[str, Any] | None = None):
        self.stage_started(stage, metadata)
        try:
            yield self
        except Exception as exc:
            self.stage_finished(stage, "failed", {"error_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}, metadata)
            raise
        else:
            self.stage_finished(stage, "success", metadata=metadata)

    def event(self, *, stage: str, event: str, status: str, metadata: dict[str, Any] | None = None, error: Any = None) -> None:
        payload = error if isinstance(error, dict) else {}
        self.logger.emit(
            level="ERROR" if status in {"failed", "timeout"} else "INFO",
            message=event,
            stage=stage,
            event=event,
            status=status,
            duration_ms=payload.get("duration_ms"),
            error_code=stable_error_code(payload, stage) if status in {"failed", "timeout"} else None,
            error_message=str(payload.get("message") or "")[:4000] if payload else None,
            metadata={**(metadata or {}), **({"error_type": payload.get("error_type"), "error_stack": payload.get("traceback")} if payload else {})},
        )

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            lines = self.logger.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return events
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    def finalize(self, state: dict[str, Any], result: int) -> dict[str, Any]:
        finished_at = tokyo_now()
        events = self._events()
        for stage in CORE_STAGES:
            if not any(event.get("stage") == stage for event in events):
                self.stage_started(stage, {"reason": "not_run_in_this_mode"})
                self.stage_finished(stage, "skipped", metadata={"reason": "not_run_in_this_mode", "delivered": False} if stage == "delivery" else {"reason": "not_run_in_this_mode"})
        events = self._events()
        stage_durations: dict[str, float] = {}
        for event in events:
            if event.get("event") == "stage_completed" and isinstance(event.get("duration_ms"), (int, float)):
                stage = str(event.get("stage"))
                stage_durations[stage] = round(stage_durations.get(stage, 0.0) + float(event["duration_ms"]) / 1000, 3)
        source_status = self._read_json(self.output_root / "market_sources" / "source_status.json")
        source_count = int(source_status.get("source_count") or 0)
        source_failure_count = sum(1 for item in (source_status.get("sources") or {}).values() if isinstance(item, dict) and str(item.get("status", "")).lower() in {"failed", "unavailable", "error"})
        image_qa = self._read_json(self.log_root / "image_qa.json")
        if not image_qa:
            image_qa = self._read_json(self.output_root / "logs" / "image_qa.json")
        errors: list[dict[str, Any]] = []
        error_codes: list[str] = []
        for step_name, step in (state.get("steps") or {}).items():
            if not isinstance(step, dict) or not step.get("error"):
                continue
            error = step.get("error") if isinstance(step.get("error"), dict) else {"message": str(step.get("error"))}
            code = stable_error_code(error, STEP_TO_STAGE.get(step_name, step_name))
            error_codes.append(code)
            errors.append({"error_code": code, "message": str(error.get("message") or error.get("raw_message") or "")[:4000], "step": step_name, "attempt": step.get("attempt") or 1})
        timeout_count = sum(1 for event in events if "TIMEOUT" in str(event.get("error_code") or "") or event.get("status") == "timeout")
        fallback_count = sum(1 for event in events if event.get("event") == "fallback" or event.get("fallback_used") is True)
        retry_count = int(state.get("retry_count") or 0)
        status = "success" if result == 0 and not state.get("failed_step") else "failed"
        completed_steps = [name for name, item in (state.get("steps") or {}).items() if isinstance(item, dict) and item.get("status") == "success"]
        last_completed = completed_steps[-1] if completed_steps else None
        image_status = image_qa.get("status") if image_qa else None
        text_image_match = None
        if image_qa:
            marker = next((item for item in image_qa.get("checks", []) if item.get("name") == "content_markers"), None)
            text_image_match = marker.get("status") == "pass" if isinstance(marker, dict) else None
        metrics = {
            "pipeline_run_total": 1,
            "pipeline_success_total": 1 if status == "success" else 0,
            "pipeline_failure_total": 1 if status == "failed" else 0,
            "pipeline_duration_seconds": round(max(0.0, (datetime.fromisoformat(finished_at) - datetime.fromisoformat(self.context.started_at)).total_seconds()), 3),
            "stage_duration_seconds": stage_durations,
            "retry_total": retry_count,
            "timeout_total": timeout_count,
            "fallback_total": fallback_count,
            "checkpoint_resume_total": 1 if self.context.checkpoint_resumed else 0,
            "source_count": source_count,
            "source_failure_count": source_failure_count,
            "schema_validation_failure_total": error_codes.count("MODEL_SCHEMA_FAILURE"),
            "cross_validation_conflict_total": error_codes.count("CROSS_VALIDATION_CONFLICT"),
            "review_failure_total": error_codes.count("REVIEW_REJECTED"),
            "image_qa_failure_total": error_codes.count("IMAGE_QA_FAILED"),
            "text_image_mismatch_total": error_codes.count("TEXT_IMAGE_MISMATCH"),
            "stale_input_total": error_codes.count("INPUT_DATE_MISMATCH"),
            "wrong_session_total": error_codes.count("INPUT_SESSION_MISMATCH"),
            "delivery_success_total": sum(1 for event in events if event.get("stage") == "delivery" and event.get("status") == "success"),
        }
        alerts = self._alerts(status, metrics, error_codes, errors)
        metrics_payload = {"run_id": self.context.run_id, "generated_at": finished_at, "metrics": metrics, "thresholds": self.thresholds}
        summary = {
            "run_id": self.context.run_id,
            "pipeline_version": self.context.pipeline_version,
            "task_type": self.context.task_type,
            "target_date": self.context.target_date,
            "session": self.context.session,
            "scheduled_at": self.context.scheduled_at,
            "started_at": self.context.started_at,
            "finished_at": finished_at,
            "duration_seconds": metrics["pipeline_duration_seconds"],
            "status": status,
            "last_completed_stage": STEP_TO_STAGE.get(last_completed, last_completed),
            "failed_stage": STEP_TO_STAGE.get(state.get("failed_step"), state.get("failed_step")),
            "source_count": source_count,
            "source_failure_count": source_failure_count,
            "retry_count": retry_count,
            "timeout_count": timeout_count,
            "fallback_count": fallback_count,
            "checkpoint_resumed": self.context.checkpoint_resumed,
            "content_review_passed": (state.get("steps", {}).get("reviewer_gate", {}).get("status") == "success"),
            "image_qa_passed": image_status == "pass" if image_status is not None else None,
            "text_image_match": text_image_match,
            "delivery_enabled": False,
            "delivery_status": "skipped",
            "errors": errors,
            "alerts": alerts,
            "metrics_path": str((self.observability_root / "metrics.json").resolve()),
            "events_path": str(self.logger.path.resolve()),
        }
        metrics_path = self.observability_root / "metrics.json"
        alerts_path = self.observability_root / "alerts.json"
        summary_path = self.observability_root / "run_summary.json"
        for path, payload in ((metrics_path, metrics_payload), (alerts_path, {"run_id": self.context.run_id, "generated_at": finished_at, "alerts": alerts}), (summary_path, summary)):
            safe_payload = redact(payload)
            assert_safe_persistence(safe_payload, path=path)
            path.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        return summary

    def _alerts(self, status: str, metrics: dict[str, Any], error_codes: list[str], errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for error in errors:
            code = error["error_code"]
            if code in P0_CODES:
                level = "P0"
            elif code in P1_CODES:
                level = "P1"
            else:
                continue
            alerts.append({"level": level, "error_code": code, "stage": error["step"], "message": error["message"], "action": "stop_and_block_delivery" if level == "P0" else "mark_run_failed_and_keep_checkpoint"})
        if metrics["pipeline_duration_seconds"] > float(self.thresholds["pipeline_timeout_seconds"]):
            alerts.append({"level": "P2", "error_code": "PIPELINE_SLOW", "stage": "pipeline", "message": "pipeline duration exceeded configured threshold", "action": "inspect_stage_durations"})
        if metrics["source_count"] < int(self.thresholds["minimum_source_count"]):
            alerts.append({"level": "P2", "error_code": "SOURCE_COUNT_LOW", "stage": "source_collection", "message": "source count is below configured threshold", "action": "inspect_source_health"})
        if metrics["retry_total"] > int(self.thresholds["max_stage_retries"]):
            alerts.append({"level": "P2", "error_code": "RETRY_THRESHOLD_EXCEEDED", "stage": "pipeline", "message": "retry count exceeded configured threshold", "action": "inspect_fallback_chain"})
        return alerts


def _safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    blocked = ("key", "token", "cookie", "secret", "password", "authorization")
    result: dict[str, Any] = {}
    for name, value in attributes.items():
        if any(word in name.lower() for word in blocked):
            continue
        result[name] = value if isinstance(value, (str, int, float, bool)) else json.dumps(value, ensure_ascii=False)[:1000]
    return result


class TraceSession:
    def __init__(self, run_id: str, attributes: dict[str, Any], local_path: Path) -> None:
        self.run_id = run_id
        self.attributes = _safe_attributes(attributes)
        self.local_path = local_path
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self._provider = None
        self._tracer = None
        self._root = None
        self._token = None
        self._otel_context = None
        self._otel_trace = None

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            with self.local_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def start(self) -> None:
        self._write({"trace": "market_content_run", "run_id": self.run_id, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "attributes": self.attributes})
        if os.environ.get("PHOENIX_TRACING_ENABLED", "false").lower() != "true":
            return
        try:
            from opentelemetry import context, trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317"))
            self._provider = TracerProvider(resource=Resource.create({"service.name": "daily-market-content-pack"}))
            self._provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
            trace.set_tracer_provider(self._provider)
            self._tracer = trace.get_tracer("daily-market-content-pack")
            self._root = self._tracer.start_span("market_content_run")
            self._root.set_attributes(self.attributes)
            self._otel_context = context
            self._otel_trace = trace
            self._token = context.attach(trace.set_span_in_context(self._root))
        except Exception as exc:  # tracing must never block content generation
            self._write({"trace": "market_content_run", "run_id": self.run_id, "status": "warning", "error_type": type(exc).__name__, "message": str(exc)[:300]})

    def step(self, name: str, status: str, attributes: dict[str, Any] | None = None) -> None:
        safe = _safe_attributes({"run_id": self.run_id, "status": status, **(attributes or {})})
        self._write({"trace": "market_content_run", "span": name, "status": status, "timestamp": datetime.now(timezone.utc).isoformat(), "attributes": safe})
        if self._tracer is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode
            with self._tracer.start_as_current_span(name) as span:
                span.set_attributes(safe)
                span.set_status(Status(StatusCode.ERROR if status == "failed" else StatusCode.OK))
        except Exception as exc:
            self._write({"trace": "market_content_run", "span": name, "status": "warning", "error_type": type(exc).__name__, "message": str(exc)[:300]})

    def close(self, status: str) -> None:
        self._write({"trace": "market_content_run", "run_id": self.run_id, "status": status, "finished_at": datetime.now(timezone.utc).isoformat()})
        try:
            if self._otel_context is not None and self._token is not None:
                self._otel_context.detach(self._token)
            if self._root is not None:
                from opentelemetry.trace import Status, StatusCode
                self._root.set_status(Status(StatusCode.ERROR if status == "failed" else StatusCode.OK))
                self._root.end()
            if self._provider is not None:
                self._provider.shutdown()
        except Exception:
            pass
