from __future__ import annotations

import healthcheck


def test_phoenix_healthcheck_converts_otlp_endpoint_to_ui(monkeypatch) -> None:
    observed = {}

    monkeypatch.setattr(healthcheck, "setting", lambda name, default="": "http://127.0.0.1:4317" if name == "PHOENIX_COLLECTOR_ENDPOINT" else "")

    def fake_request(url: str, *args, **kwargs):
        observed["url"] = url
        return {"status": "healthy", "http_status": 200}

    monkeypatch.setattr(healthcheck, "_request", fake_request)
    result = healthcheck.check_phoenix()

    assert result["status"] == "healthy"
    assert observed["url"] == "http://127.0.0.1:6006"


def test_massive_healthcheck_scopes_to_declared_ndx_capability(monkeypatch) -> None:
    class FakeSecret:
        def reveal(self, purpose: str) -> str:
            return "redacted-test-secret"

    monkeypatch.setattr(healthcheck, "get_secret", lambda *args, **kwargs: FakeSecret())
    seen = []

    def fake_request(url: str, *args, **kwargs):
        seen.append(url)
        return {"status": "healthy", "http_status": 200}

    monkeypatch.setattr(healthcheck, "_request", fake_request)
    result = healthcheck.check_massive()

    assert result["status"] == "healthy"
    assert len(seen) == 1
    assert "I%3ANDX" in seen[0]
    assert "SPX" in result["unsupported_capabilities"]
    assert "DJI" in result["unsupported_capabilities"]
