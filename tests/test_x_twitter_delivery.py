from __future__ import annotations

from delivery_gate import XTwitterDeliveryAdapter, build_delivery_adapter


def test_x_twitter_adapter_is_unconfigured_without_publish_token(monkeypatch) -> None:
    monkeypatch.setattr("delivery_gate.get_secret", lambda *args, **kwargs: None)
    adapter = XTwitterDeliveryAdapter()
    assert adapter.health()["status"] == "unconfigured"
    assert adapter.health()["media_upload"] is False


def test_x_twitter_adapter_requires_all_oauth1_credentials(monkeypatch) -> None:
    def fake_secret(name, **kwargs):
        if name == "X_PUBLISH_ACCESS_TOKEN_SECRET":
            return None

        class FakeSecret:
            def reveal(self, purpose: str) -> str:
                return f"value-for-{name}"

        return FakeSecret()

    monkeypatch.setattr("delivery_gate.get_secret", fake_secret)
    health = XTwitterDeliveryAdapter().health()
    assert health["status"] == "unconfigured"
    assert health["auth_scheme"] == "oauth1_user_context"
    assert health["missing_credentials"] == ["X_PUBLISH_ACCESS_TOKEN_SECRET"]


def test_delivery_policy_selects_x_twitter_adapter(monkeypatch) -> None:
    monkeypatch.setattr("delivery_gate.get_secret", lambda *args, **kwargs: None)
    adapter = build_delivery_adapter({"adapter": "x_twitter"})
    assert isinstance(adapter, XTwitterDeliveryAdapter)


def test_x_twitter_adapter_rejects_long_text_before_network(monkeypatch) -> None:
    class FakeSecret:
        def reveal(self, purpose: str) -> str:
            return "test-user-context-token"

    monkeypatch.setattr("delivery_gate.get_secret", lambda *args, **kwargs: FakeSecret())
    adapter = XTwitterDeliveryAdapter()
    try:
        adapter.publish({"text": "x" * 281}, "test-idempotency-key")
    except RuntimeError as exc:
        assert str(exc) == "x_twitter_post_text_too_long"
    else:
        raise AssertionError("long X post must be rejected before network")


def test_x_twitter_oauth1_signature_is_deterministic(monkeypatch) -> None:
    values = {
        "X_CONSUMER_KEY": "consumer-key",
        "X_CONSUMER_SECRET": "consumer-secret",
        "X_PUBLISH_ACCESS_TOKEN": "access-token",
        "X_PUBLISH_ACCESS_TOKEN_SECRET": "access-token-secret",
    }

    class FakeSecret:
        def __init__(self, value: str) -> None:
            self.value = value

        def reveal(self, purpose: str) -> str:
            return self.value

    monkeypatch.setattr("delivery_gate.get_secret", lambda name, **kwargs: FakeSecret(values[name]))
    adapter = XTwitterDeliveryAdapter()
    first = adapter._oauth1_authorization_header("POST", "https://api.x.com/2/tweets", nonce="fixed", timestamp="1700000000")
    second = adapter._oauth1_authorization_header("POST", "https://api.x.com/2/tweets", nonce="fixed", timestamp="1700000000")
    assert first == second
    assert first.startswith("OAuth ")
    assert "oauth_signature=" in first
    assert "consumer-secret" not in first
    assert "access-token-secret" not in first


def test_x_twitter_adapter_rejects_attachments_before_network(monkeypatch) -> None:
    class FakeSecret:
        def reveal(self, purpose: str) -> str:
            return "test-user-context-token"

    monkeypatch.setattr("delivery_gate.get_secret", lambda *args, **kwargs: FakeSecret())
    adapter = XTwitterDeliveryAdapter()
    try:
        adapter.publish({"text": "text-only post", "attachments": ["/tmp/report.png"]}, "test-idempotency-key")
    except RuntimeError as exc:
        assert str(exc) == "x_twitter_media_not_supported"
    else:
        raise AssertionError("X adapter must reject attachments before network")
