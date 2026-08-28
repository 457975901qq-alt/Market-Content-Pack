#!/usr/bin/env python3
"""Fail-closed delivery authorization and a pluggable webhook adapter."""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import secrets
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from security import get_secret, validate_url


class DeliveryAdapter(Protocol):
    def health(self) -> dict[str, Any]:
        ...

    def publish(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class DeliveryAuthorization:
    allowed: bool
    checks: dict[str, bool]
    blockers: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "checks": self.checks, "blockers": self.blockers}


def authorize_delivery(
    *,
    policy: dict[str, Any],
    run_id: str,
    artifact_hash: str,
    dry_run: bool,
    adapter_ready: bool,
    approval: dict[str, Any] | None,
    now: datetime | None = None,
) -> DeliveryAuthorization:
    current = now or datetime.now(timezone.utc)
    checks = {
        "delivery_policy_enabled": policy.get("allow_delivery") is True,
        "production_update_policy_enabled": policy.get("allow_production_update") is True,
        "not_dry_run": dry_run is False,
        "adapter_ready": adapter_ready is True,
        "approval_present": isinstance(approval, dict),
        "approval_matches_run": isinstance(approval, dict) and approval.get("run_id") == run_id,
        "approval_matches_artifact": isinstance(approval, dict) and approval.get("artifact_hash") == artifact_hash,
        "approval_granted": isinstance(approval, dict) and approval.get("approved") is True and bool(approval.get("approved_by")),
    }
    expires_at = approval.get("expires_at") if isinstance(approval, dict) else None
    if expires_at:
        try:
            checks["approval_not_expired"] = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) > current
        except ValueError:
            checks["approval_not_expired"] = False
    else:
        checks["approval_not_expired"] = False
    blockers = [name for name, passed in checks.items() if not passed]
    return DeliveryAuthorization(allowed=not blockers, checks=checks, blockers=blockers)


class WebhookDeliveryAdapter:
    """Generic adapter; it never sends unless its publish method is called explicitly."""

    def __init__(self, endpoint: str | None = None, timeout_seconds: float = 20) -> None:
        if endpoint:
            self.endpoint = endpoint.strip()
        else:
            secret = get_secret("DELIVERY_WEBHOOK_URL", consumer="delivery_adapter", purpose="external_delivery", run_id="delivery")
            self.endpoint = secret.reveal("external_delivery") if secret else ""
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        return {"status": "ready" if self.endpoint else "unconfigured", "adapter": "webhook"}

    def publish(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not self.endpoint:
            raise RuntimeError("delivery_webhook_endpoint_missing")
        validate_url(self.endpoint, consumer="delivery_adapter", purpose="external_delivery")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            status = int(response.status)
            if status < 200 or status >= 300:
                raise RuntimeError(f"delivery_webhook_status:{status}")
            return {"status": "sent", "adapter": "webhook", "http_status": status, "idempotency_key": idempotency_key}


class XTwitterDeliveryAdapter:
    """Text-only X publisher using OAuth 1.0a user context.

    The adapter is deliberately not enabled by default. X write endpoints
    require user-context permissions; the source-reading token is never used
    for publishing. Credentials are loaded from the secret registry and
    never included in health output or persisted payloads.
    """

    ENDPOINT = "https://api.x.com/2/tweets"
    CREDENTIALS = {
        "X_CONSUMER_KEY": "x_consumer_key",
        "X_CONSUMER_SECRET": "x_consumer_secret",
        "X_PUBLISH_ACCESS_TOKEN": "x_publish_access_token",
        "X_PUBLISH_ACCESS_TOKEN_SECRET": "x_publish_access_token_secret",
    }

    def __init__(self, endpoint: str | None = None, timeout_seconds: float = 20) -> None:
        self.credentials: dict[str, str] = {}
        for name in self.CREDENTIALS:
            secret = get_secret(name, consumer="delivery_adapter", purpose="external_delivery", run_id="delivery")
            self.credentials[name] = secret.reveal("external_delivery") if secret else ""
        self.endpoint = (endpoint or self.ENDPOINT).strip()
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        missing = [name for name, value in self.credentials.items() if not value]
        return {
            "status": "ready" if not missing else "unconfigured",
            "adapter": "x_twitter",
            "endpoint": self.endpoint,
            "auth_scheme": "oauth1_user_context",
            "user_context_credentials_configured": not missing,
            "missing_credentials": missing,
            "media_upload": False,
        }

    def publish(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        health = self.health()
        if health["status"] != "ready":
            raise RuntimeError(f"x_twitter_oauth1_credentials_missing:{','.join(health['missing_credentials'])}")
        attachments = payload.get("attachments") or []
        if attachments:
            raise RuntimeError("x_twitter_media_not_supported")
        text = str(payload.get("text") or payload.get("summary") or "").strip()
        if not text:
            raise RuntimeError("x_twitter_post_text_missing")
        if len(text) > 280:
            raise RuntimeError("x_twitter_post_text_too_long")
        validate_url(self.endpoint, consumer="delivery_adapter", purpose="external_delivery")
        body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        oauth_header = self._oauth1_authorization_header("POST", self.endpoint)
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": oauth_header,
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        post_id = ((result.get("data") or {}).get("id") if isinstance(result, dict) else None)
        if not post_id:
            raise RuntimeError("x_twitter_response_missing_post_id")
        return {"status": "sent", "adapter": "x_twitter", "post_id": str(post_id), "idempotency_key": idempotency_key}

    def _oauth1_authorization_header(self, method: str, url: str, *, nonce: str | None = None, timestamp: str | None = None) -> str:
        """Build an OAuth 1.0a HMAC-SHA1 header for a JSON request body."""
        oauth = {
            "oauth_consumer_key": self.credentials["X_CONSUMER_KEY"],
            "oauth_nonce": nonce or secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": timestamp or str(int(time.time())),
            "oauth_token": self.credentials["X_PUBLISH_ACCESS_TOKEN"],
            "oauth_version": "1.0",
        }

        def quote(value: str) -> str:
            return urllib.parse.quote(str(value), safe="~-._")

        parsed = urllib.parse.urlsplit(url)
        base_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        signature_pairs = [(key, value) for key, value in query_pairs]
        signature_pairs.extend(oauth.items())
        normalized = "&".join(f"{quote(key)}={quote(value)}" for key, value in sorted(signature_pairs))
        base_string = "&".join((method.upper(), quote(base_url), quote(normalized)))
        signing_key = f"{quote(self.credentials['X_CONSUMER_SECRET'])}&{quote(self.credentials['X_PUBLISH_ACCESS_TOKEN_SECRET'])}"
        signature = base64.b64encode(hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()).decode("ascii")
        oauth["oauth_signature"] = signature
        header = ", ".join(f'{quote(key)}="{quote(value)}"' for key, value in sorted(oauth.items()))
        return f"OAuth {header}"


class EmailDeliveryAdapter:
    """SMTP adapter with explicit configuration and no implicit send."""

    REQUIRED_ENV = (
        "DELIVERY_SMTP_HOST",
        "DELIVERY_SMTP_USERNAME",
        "DELIVERY_SMTP_PASSWORD",
        "DELIVERY_EMAIL_FROM",
        "DELIVERY_EMAIL_TO",
    )

    def __init__(self, env: dict[str, str] | None = None, timeout_seconds: float = 20) -> None:
        values = env or os.environ
        self.host = values.get("DELIVERY_SMTP_HOST", "").strip()
        self.port = int(values.get("DELIVERY_SMTP_PORT", "587") or "587")
        self.username = values.get("DELIVERY_SMTP_USERNAME", "").strip()
        if env is None:
            secret = get_secret("DELIVERY_SMTP_PASSWORD", consumer="delivery_adapter", purpose="external_delivery", run_id="delivery")
            self.password = secret.reveal("external_delivery") if secret else ""
        else:
            # Explicit test/injected adapters remain deterministic; production
            # construction without env routes through SecretProvider above.
            self.password = values.get("DELIVERY_SMTP_PASSWORD", "")
        self.sender = values.get("DELIVERY_EMAIL_FROM", "").strip()
        self.recipients = [item.strip() for item in values.get("DELIVERY_EMAIL_TO", "").split(",") if item.strip()]
        self.use_ssl = values.get("DELIVERY_SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        missing = [name for name in self.REQUIRED_ENV if not self._env_value(name)]
        return {
            "status": "ready" if not missing else "unconfigured",
            "adapter": "smtp_email",
            "host": self.host or None,
            "port": self.port,
            "recipient_count": len(self.recipients),
            "missing": missing,
        }

    def _env_value(self, name: str) -> str:
        return {
            "DELIVERY_SMTP_HOST": self.host,
            "DELIVERY_SMTP_USERNAME": self.username,
            "DELIVERY_SMTP_PASSWORD": self.password,
            "DELIVERY_EMAIL_FROM": self.sender,
            "DELIVERY_EMAIL_TO": ",".join(self.recipients),
        }.get(name, "")

    def publish(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        health = self.health()
        if health["status"] != "ready":
            raise RuntimeError(f"delivery_smtp_configuration_missing:{','.join(health['missing'])}")
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message["Subject"] = str(payload.get("subject") or "每日市场内容包")
        message["X-Idempotency-Key"] = idempotency_key
        message.set_content(str(payload.get("text") or payload.get("summary") or "每日市场内容包"))
        for item in payload.get("attachments", []):
            path = Path(str(item)).resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"delivery_attachment_missing:{path}")
            subtype = "svg+xml" if path.suffix.lower() == ".svg" else "octet-stream"
            message.add_attachment(path.read_bytes(), maintype="image" if subtype == "svg+xml" else "application", subtype=subtype, filename=path.name)
        context = ssl.create_default_context()
        if self.use_ssl:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout_seconds, context=context) as client:
                client.login(self.username, self.password)
                client.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                client.login(self.username, self.password)
                client.send_message(message)
        return {"status": "sent", "adapter": "smtp_email", "idempotency_key": idempotency_key, "recipient_count": len(self.recipients)}


def build_delivery_adapter(policy: dict[str, Any] | None = None) -> DeliveryAdapter:
    selected = str((policy or {}).get("adapter") or "smtp_email").lower()
    if selected in {"x", "twitter", "x_twitter", "twitter_v2"}:
        return XTwitterDeliveryAdapter()
    if selected in {"webhook", "http"}:
        return WebhookDeliveryAdapter()
    if selected in {"smtp", "smtp_email", "email"}:
        return EmailDeliveryAdapter()
    raise ValueError(f"unsupported_delivery_adapter:{selected}")


__all__ = ["DeliveryAdapter", "DeliveryAuthorization", "EmailDeliveryAdapter", "WebhookDeliveryAdapter", "XTwitterDeliveryAdapter", "authorize_delivery", "build_delivery_adapter"]
