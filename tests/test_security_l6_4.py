from __future__ import annotations

import json

import pytest

from security import (
    AuditLogger,
    MemorySecretProvider,
    SecurityError,
    assert_safe_persistence,
    build_subprocess_env,
    redact_sensitive,
    security_gate,
    validate_url,
)
from security.core import permission_audit, security_preflight
from tools.security_drills import run_drills


def test_secret_value_is_opaque_and_purpose_bound() -> None:
    provider = MemorySecretProvider({"GEMINI_API_KEY": "test-secret-value"})
    value = provider.get("GEMINI_API_KEY", consumer="content_generator", purpose="generate_market_content", run_id="run-1")
    assert value is not None
    assert "test-secret-value" not in repr(value)
    assert value.reveal("generate_market_content") == "test-secret-value"
    with pytest.raises(SecurityError):
        value.reveal("wrong-purpose")
    with pytest.raises(SecurityError):
        value.to_json()


def test_unknown_secret_is_not_registered() -> None:
    provider = MemorySecretProvider({})
    with pytest.raises(SecurityError, match="SECRET_NOT_REGISTERED"):
        provider.get("UNKNOWN_SECRET", consumer="content_generator", purpose="generate_market_content", run_id="run-1")


def test_consumer_and_purpose_are_denied() -> None:
    provider = MemorySecretProvider({"GEMINI_API_KEY": "test-secret-value"})
    with pytest.raises(SecurityError, match="SECRET_CONSUMER_DENIED"):
        provider.get("GEMINI_API_KEY", consumer="delivery_adapter", purpose="external_delivery", run_id="run-1")


def test_redaction_handles_structures_urls_and_raw_known_values() -> None:
    secret = "sk-test-secret-value-123456"
    result = redact_sensitive({"api_key": secret, "url": f"https://example.test/?token={secret}", "nested": [{"password": secret}]}, secret_values=[secret])
    encoded = json.dumps(result, ensure_ascii=False)
    assert secret not in encoded
    assert "[REDACTED]" in encoded


def test_persistence_guard_blocks_secret_fields() -> None:
    with pytest.raises(SecurityError, match="SECRET_PERSISTENCE_BLOCKED"):
        assert_safe_persistence({"authorization": "Bearer test-secret-value"}, path="metrics.json")


def test_subprocess_environment_is_allowlist_only() -> None:
    env = build_subprocess_env(base_env={"PATH": "/bin", "SAFE": "yes", "OPENAI_API_KEY": "secret"}, allowed_keys=["PATH", "SAFE"])
    assert env == {"PATH": "/bin", "SAFE": "yes"}


def test_url_policy_blocks_ssrf_and_allows_registered_host() -> None:
    assert validate_url("https://api.openai.com/v1/responses")
    with pytest.raises(SecurityError, match="SSRF_BLOCKED"):
        validate_url("http://127.0.0.1:8080/internal", allowed_hosts=["127.0.0.1"])


def test_url_policy_allows_explicit_local_ollama_exception() -> None:
    assert validate_url("http://127.0.0.1:11434/api/tags", allowed_hosts=[], allow_localhost=True)


def test_audit_log_hash_chain_detects_tampering(tmp_path) -> None:
    path = tmp_path / "security_audit.jsonl"
    audit = AuditLogger(path)
    audit.append("unit.test", actor="tester", outcome="passed", details={"safe": True}, reason="test")
    assert audit.verify()["status"] == "passed"
    text = path.read_text(encoding="utf-8").replace('"outcome":"passed"', '"outcome":"tampered"')
    path.write_text(text, encoding="utf-8")
    assert audit.verify()["status"] == "blocked"


def test_development_permission_warning_does_not_block_gate(tmp_path) -> None:
    for name in ("runtime", "state", "logs"):
        path = tmp_path / name
        path.mkdir()
        path.chmod(0o755)
    report = security_preflight(root=tmp_path, mode="development", provider_name="rule_template", delivery_enabled=False)
    assert report["status"] == "warning"
    assert security_gate(report)["status"] == "passed"
    production = security_preflight(root=tmp_path, mode="production", provider_name="rule_template", delivery_enabled=False)
    assert production["status"] == "blocked"


def test_seven_offline_drills_pass() -> None:
    report = run_drills()
    assert report["total"] == 7
    assert report["passed"] == 7
    assert report["status"] == "passed"
