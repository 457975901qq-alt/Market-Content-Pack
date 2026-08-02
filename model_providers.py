#!/usr/bin/env python3
"""Small, dependency-free provider adapters for local or fallback generation.

The adapters deliberately return raw model text. Schema validation remains in
``market_content_openai.py`` so every provider follows the same contract.
"""

from __future__ import annotations

import json
import os
import urllib.error
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
_CANARY_FAULTS_INJECTED: set[str] = set()


def _inject_canary_fault(fault: str) -> None:
    """Inject one bounded failure only inside explicit Self-Healing Canary mode."""
    if os.environ.get("SELF_HEALING_CANARY_MODE", "false").lower() != "true":
        return
    configured = os.environ.get("SELF_HEALING_FAULT", "none").strip()
    if configured != fault or fault in _CANARY_FAULTS_INJECTED:
        return
    _CANARY_FAULTS_INJECTED.add(fault)
    if fault == "ollama_unavailable":
        raise ProviderError("ollama_unavailable", "Ollama unavailable (controlled Canary fault injection)")


def _load_env_file() -> None:
    """Load project-local environment values when the caller did not export them."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.raw_response = raw_response


def _record_model_span(provider: str, model: str, started: float, *, response_length: int = 0, error_type: str = "", fallback_used: bool = False) -> None:
    path = os.environ.get("MARKET_TRACE_PATH", "").strip()
    if not path:
        return
    payload = {
        "trace": "market_content_run",
        "span": f"{provider}_call",
        "status": "error" if error_type else "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attributes": {
            "model_provider": provider,
            "model_name": model,
            "prompt_version": os.environ.get("MARKET_PROMPT_VERSION", "unknown"),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "response_length": response_length,
            "json_parse_result": "unknown",
            "schema_validation_result": "unknown",
            "fallback_used": fallback_used,
            "error_type": error_type,
        },
    }
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _json_request(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Do not include request URLs: Gemini URLs contain the API key.
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise ProviderError("provider_http_error", f"HTTP {exc.code}", detail) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("provider_unavailable", type(exc).__name__, "") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("provider_invalid_json", str(exc), raw[:1000]) from exc
    if not isinstance(payload, dict):
        raise ProviderError("provider_invalid_payload", "Provider response must be an object.", raw[:1000])
    return payload


def _first_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))


def call_ollama(prompt: str, model: str | None = None, timeout: float = 90) -> str:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    selected_model = model or os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
    started = time.monotonic()
    try:
        _inject_canary_fault("ollama_unavailable")
        payload = _json_request(
            f"{base_url}/api/generate",
            {"model": selected_model, "prompt": prompt, "stream": False, "format": "json", "think": False, "options": {"temperature": 0}},
            {}, timeout,
        )
        text = payload.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("provider_empty_response", "Ollama returned no response.", json.dumps(payload)[:1000])
        _record_model_span("ollama", selected_model, started, response_length=len(text))
        return text
    except ProviderError as exc:
        _record_model_span("ollama", selected_model, started, error_type=exc.error_type)
        raise


def call_gemini(prompt: str, model: str | None = None, timeout: float = 90) -> str:
    _load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ProviderError("api_key_missing", "GEMINI_API_KEY is not set.")
    selected_model = model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    # The key is intentionally used only in the request URL and never logged.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent?key={api_key}"
    started = time.monotonic()
    try:
        if os.environ.get("SELF_HEALING_CANARY_MODE", "false").lower() == "true" and os.environ.get("SELF_HEALING_FAULT", "none").strip() == "gemini_invalid_json" and "gemini_invalid_json" not in _CANARY_FAULTS_INJECTED:
            _CANARY_FAULTS_INJECTED.add("gemini_invalid_json")
            _record_model_span("gemini", selected_model, started, response_length=8, error_type="provider_invalid_json")
            return "{\"truncated\":"
        payload = _json_request(
            url,
            {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
            {}, timeout,
        )
        candidates = payload.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content") or {}
            text = _first_text(content.get("parts")) if isinstance(content, dict) else ""
            if text.strip():
                _record_model_span("gemini", selected_model, started, response_length=len(text))
                return text
        raise ProviderError("provider_empty_response", "Gemini returned no text.", json.dumps(payload)[:1000])
    except ProviderError as exc:
        _record_model_span("gemini", selected_model, started, error_type=exc.error_type)
        raise


def health_check(provider: str) -> dict[str, Any]:
    """Return non-secret provider configuration status without a generation call."""
    _load_env_file()
    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
        started = time.monotonic()
        result = {
            "provider": "ollama",
            "configured": False,
            "base_url": base_url,
            "model": model,
            "status": "unhealthy",
        }
        try:
            with urllib.request.urlopen(f"{base_url}/api/tags", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            available = {str(item.get("name")) for item in payload.get("models", []) if isinstance(item, dict)}
            result.update({"configured": response.status == 200 and (not available or model in available), "status": "healthy" if response.status == 200 else "unhealthy", "latency_ms": int((time.monotonic() - started) * 1000), "model_available": model in available if available else None})
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result.update({"status": "unhealthy", "latency_ms": int((time.monotonic() - started) * 1000), "blocking_reason": type(exc).__name__})
        return result
    if provider == "gemini":
        configured = bool(os.environ.get("GEMINI_API_KEY", "").strip())
        return {
            "provider": "gemini",
            "configured": configured,
            "model": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
            # The health check is deliberately configuration-only. A real
            # generation probe is performed by the reviewer itself, so the
            # router can select Gemini without spending an extra request.
            "status": "healthy" if configured else "unhealthy",
        }
    if provider == "rule_template":
        return {"provider": "rule_template", "configured": True, "model": None}
    return {"provider": provider, "configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()), "model": os.environ.get("OPENAI_MODEL", "gpt-5")}
