"""Seven deterministic L6-4 offline security drills."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from security.core import AuditLogger, MemorySecretProvider, SecurityError, assert_safe_persistence, build_subprocess_env, redact_sensitive, validate_url


def _result(name: str, passed: bool, detail: str = "") -> dict[str, object]:
    return {"name": name, "status": "passed" if passed else "failed", "detail": detail}


def run_drills() -> dict[str, object]:
    results: list[dict[str, object]] = []
    secret = "sk-DRILL-SECRET-123456789"
    masked = redact_sensitive({"api_key": secret, "message": f"?api_key={secret}"})
    results.append(_result("secret_masking", masked["api_key"] == "[REDACTED]" and "DRILL" not in json.dumps(masked)))
    try:
        provider = MemorySecretProvider({"GEMINI_API_KEY": secret})
        provider.get("GEMINI_API_KEY", consumer="delivery_adapter", purpose="external_delivery", run_id="drill")
        results.append(_result("consumer_purpose_denial", False, "unexpected authorization"))
    except SecurityError:
        results.append(_result("consumer_purpose_denial", True))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".env").write_text("GEMINI_API_KEY=" + secret + "\n", encoding="utf-8")
        (root / ".env").chmod(0o600)
        from security.core import EnvironmentSecretProvider
        blocked = EnvironmentSecretProvider(root=root, mode="production", environ={}).get("GEMINI_API_KEY", consumer="content_generator", purpose="generate_market_content", run_id="drill")
        results.append(_result("production_env_isolation", blocked is None))
    try:
        validate_url("http://169.254.169.254/latest/meta-data", allowed_hosts=["169.254.169.254"])
        results.append(_result("ssrf_block", False))
    except SecurityError:
        results.append(_result("ssrf_block", True))
    env = build_subprocess_env(base_env={"PATH": "/bin", "GEMINI_API_KEY": secret, "SAFE": "1"}, allowed_keys=["PATH", "SAFE"])
    results.append(_result("subprocess_env_minimal", "GEMINI_API_KEY" not in env and env.get("SAFE") == "1"))
    try:
        assert_safe_persistence({"password": secret}, path="drill.json")
        results.append(_result("persistence_guard", False))
    except SecurityError:
        results.append(_result("persistence_guard", True))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        logger = AuditLogger(path)
        logger.append("drill", actor="offline", outcome="passed", reason="drill")
        first = logger.verify()["status"] == "passed"
        path.write_text(path.read_text(encoding="utf-8").replace('"outcome":"passed"', '"outcome":"tampered"'), encoding="utf-8")
        results.append(_result("audit_hash_chain", first and logger.verify()["status"] == "blocked"))
    return {"status": "passed" if all(item["status"] == "passed" for item in results) else "failed", "total": len(results), "passed": sum(item["status"] == "passed" for item in results), "drills": results}
