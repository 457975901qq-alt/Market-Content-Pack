#!/usr/bin/env python3
"""Fail-safe Phoenix/OpenTelemetry tracing with a local audit fallback."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
