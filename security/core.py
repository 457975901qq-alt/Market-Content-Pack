"""Dependency-free security primitives used by L6-4.

This module deliberately has no imports from business modules.  It is safe to
use from persistence, provider, scheduler, and CLI code without creating an
import cycle.  Plaintext secrets are never included in returned reports or
exception messages.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets as _random
import shutil
import socket
import stat
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "security" / "secret_registry.json"
NETWORK_POLICY_PATH = ROOT / "config" / "security" / "network_policy.json"
AUDIT_PATH = ROOT / "logs" / "security_audit.jsonl"

SECRET_KEY_WORDS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token", "private_key")
SECRET_ASSIGNMENT_RE = re.compile(r"(?i)(api[_-]?key|authorization|bearer|cookie|password|secret|token)\s*[:=]\s*([^\s,;\"']+)")
SECRET_LITERAL_ASSIGNMENT_RE = re.compile(r"(?i)(api[_-]?key|authorization|bearer|cookie|password|secret|token)\s*[:=]\s*['\"]([^'\"]{16,})['\"]")
URL_SECRET_RE = re.compile(r"(?i)([?&](?:api[_-]?key|access_token|token|password|secret)=)([^&#\s]+)")
HIGH_CONFIDENCE_RE = re.compile(r"(?:sk-[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]+ PRIVATE KEY-----)")


class SecurityError(RuntimeError):
    """A fail-closed security error whose text is safe to display."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}:{message}")


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mask(value: str) -> str:
    if not value:
        return ""
    return "****" + value[-4:] if len(value) >= 4 else "****"


class SecretValue:
    """Opaque secret wrapper.

    The only plaintext escape hatch is ``reveal(purpose)`` after the provider
    has already checked the registry.  Stringification, repr, and JSON-like
    conversion are always masked or blocked.
    """

    __slots__ = ("_value", "name", "source", "_purposes")

    def __init__(self, value: str, *, name: str, source: str, purposes: Sequence[str] = ()) -> None:
        if not isinstance(value, str) or not value:
            raise SecurityError("SECRET_EMPTY", "secret value is empty")
        self._value = value
        self.name = name
        self.source = source
        self._purposes = frozenset(purposes)

    def reveal(self, purpose: str) -> str:
        if purpose not in self._purposes:
            raise SecurityError("SECRET_PURPOSE_DENIED", "secret purpose is not authorized")
        return self._value

    def masked(self) -> str:
        return _mask(self._value)

    def fingerprint(self) -> str:
        return _fingerprint(self._value)

    def __str__(self) -> str:
        return self.masked()

    def __repr__(self) -> str:
        return f"SecretValue(name={self.name!r}, source={self.source!r}, value='[REDACTED]')"

    def to_json(self) -> None:
        raise SecurityError("SECRET_SERIALIZATION_BLOCKED", "SecretValue cannot be serialized")

    def __json__(self) -> None:
        return self.to_json()


class SecretProvider(Protocol):
    def get(self, name: str, *, consumer: str, purpose: str, run_id: str) -> SecretValue | None:
        ...


def load_secret_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecurityError("SECRET_REGISTRY_UNREADABLE", type(exc).__name__) from exc
    if not isinstance(data, dict) or not isinstance(data.get("secrets"), list):
        raise SecurityError("SECRET_REGISTRY_INVALID", "registry must contain secrets list")
    return data


def _registry_entry(name: str, registry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next((item for item in registry.get("secrets", []) if isinstance(item, dict) and item.get("name") == name), None)


def _authorize_secret(name: str, consumer: str, purpose: str, mode: str, registry: Mapping[str, Any]) -> Mapping[str, Any]:
    entry = _registry_entry(name, registry)
    if not entry:
        raise SecurityError("SECRET_NOT_REGISTERED", "secret is not registered")
    if consumer not in entry.get("allowed_consumers", []) or purpose not in entry.get("allowed_purposes", []):
        raise SecurityError("SECRET_CONSUMER_DENIED", "secret consumer or purpose is not authorized")
    if mode not in entry.get("allowed_modes", ["development", "test", "production"]):
        raise SecurityError("SECRET_MODE_DENIED", "secret is not available in this mode")
    return entry


class MemorySecretProvider:
    """Deterministic provider for tests and offline drills."""

    def __init__(self, values: Mapping[str, str], *, registry: Mapping[str, Any] | None = None, mode: str = "test") -> None:
        self.values = dict(values)
        self.registry = registry or load_secret_registry()
        self.mode = mode

    def get(self, name: str, *, consumer: str, purpose: str, run_id: str) -> SecretValue | None:
        entry = _authorize_secret(name, consumer, purpose, self.mode, self.registry)
        value = self.values.get(name, "")
        if not value:
            return None
        return SecretValue(value, name=name, source="memory", purposes=entry.get("allowed_purposes", []))


class EnvironmentSecretProvider:
    """Reads explicitly registered names from process environment.

    A project ``.env`` is permitted only in development and only when mode is
    0600 or stricter.  Production and test never load it.
    """

    def __init__(self, *, root: Path = ROOT, mode: str = "development", registry: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.root = root
        self.mode = mode
        self.registry = registry or load_secret_registry()
        self.environ = dict(environ or os.environ)
        if mode == "development":
            env_file = root / ".env"
            try:
                if env_file.is_file() and stat.S_IMODE(env_file.stat().st_mode) & 0o077 == 0:
                    for line in env_file.read_text(encoding="utf-8").splitlines():
                        if "=" in line and not line.lstrip().startswith("#"):
                            key, value = line.split("=", 1)
                            self.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            except OSError:
                pass

    def get(self, name: str, *, consumer: str, purpose: str, run_id: str) -> SecretValue | None:
        entry = _authorize_secret(name, consumer, purpose, self.mode, self.registry)
        value = self.environ.get(name, "").strip()
        if not value:
            return None
        return SecretValue(value, name=name, source="environment", purposes=entry.get("allowed_purposes", []))


class KeychainSecretProvider:
    """macOS Keychain adapter; subprocess output is never logged."""

    def __init__(self, *, registry: Mapping[str, Any] | None = None, mode: str = "production", runner: Any = None) -> None:
        self.registry = registry or load_secret_registry()
        self.mode = mode
        self.runner = runner or subprocess.run

    def get(self, name: str, *, consumer: str, purpose: str, run_id: str) -> SecretValue | None:
        entry = _authorize_secret(name, consumer, purpose, self.mode, self.registry)
        keychain = entry.get("keychain") or {}
        if not keychain or shutil.which("security") is None:
            return None
        try:
            result = self.runner(
                ["security", "find-generic-password", "-s", str(keychain.get("service", "market-pipeline")), "-a", str(keychain.get("account", name)), "-w"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        value = str(result.stdout or "").strip()
        return SecretValue(value, name=name, source="macos_keychain", purposes=entry.get("allowed_purposes", [])) if value else None


class CompositeSecretProvider:
    def __init__(self, *, mode: str = "development", root: Path = ROOT, registry: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.mode = mode
        self.registry = registry or load_secret_registry()
        if mode == "test":
            self.providers: list[SecretProvider] = []
        elif mode == "production":
            self.providers = [KeychainSecretProvider(registry=self.registry, mode=mode), EnvironmentSecretProvider(root=root, mode=mode, registry=self.registry, environ=environ)]
        else:
            self.providers = [EnvironmentSecretProvider(root=root, mode=mode, registry=self.registry, environ=environ)]

    def get(self, name: str, *, consumer: str, purpose: str, run_id: str) -> SecretValue | None:
        for provider in self.providers:
            value = provider.get(name, consumer=consumer, purpose=purpose, run_id=run_id)
            if value is not None:
                return value
        return None


def get_secret(name: str, *, consumer: str, purpose: str, run_id: str = "unspecified", provider: SecretProvider | None = None, mode: str | None = None) -> SecretValue | None:
    # An explicitly empty process value is an intentional disable switch. It
    # must not silently fall back to a credential in Keychain (important for
    # tests, incident response, and local provider isolation).
    if name in os.environ and not os.environ[name].strip():
        return None
    selected_mode = mode or os.environ.get("SECURITY_MODE") or _project_security_mode()
    selected = provider or CompositeSecretProvider(mode=selected_mode)
    return selected.get(name, consumer=consumer, purpose=purpose, run_id=run_id)


def _project_security_mode() -> str:
    """Read only the non-secret mode selector from the local environment file.

    Secrets remain in Keychain; this lets scheduled local runs use the same
    Keychain-backed provider without copying credentials into ``.env``.
    """
    env_path = ROOT / ".env"
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "SECURITY_MODE" and value.strip() in {"development", "test", "production"}:
                return value.strip()
    except OSError:
        pass
    return "development"


def redact_sensitive(value: Any, *, key: str | None = None, secret_values: Sequence[str] = (), _depth: int = 0) -> Any:
    """Recursively redact structured values, raw secret strings, and URLs."""
    if _depth > 10:
        return "[REDACTED_DEPTH]"
    if key and any(word in key.lower() for word in SECRET_KEY_WORDS):
        return "[REDACTED]"
    if isinstance(value, SecretValue):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): redact_sensitive(v, key=str(k), secret_values=secret_values, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item, secret_values=secret_values, _depth=_depth + 1) for item in value]
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": redact_sensitive(str(value), secret_values=secret_values)}
    if not isinstance(value, str):
        return value
    text = value
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = URL_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)
    text = HIGH_CONFIDENCE_RE.sub("[REDACTED]", text)
    return text[:4000]


def assert_safe_persistence(payload: Any, *, path: Path | str = "") -> None:
    """Fail closed before sensitive material reaches JSON/log persistence."""
    if isinstance(payload, Mapping):
        for key, item in payload.items():
            if any(word in str(key).lower() for word in SECRET_KEY_WORDS) and item not in (None, "", False, [], {}):
                raise SecurityError("SECRET_PERSISTENCE_BLOCKED", "sensitive field cannot be persisted")
    encoded = json.dumps(redact_sensitive(payload), ensure_ascii=False, sort_keys=True, default=str)
    if "[REDACTED]" in encoded and HIGH_CONFIDENCE_RE.search(json.dumps(payload, ensure_ascii=False, default=str)):
        raise SecurityError("SECRET_PERSISTENCE_BLOCKED", "secret-like value cannot be persisted")


def build_subprocess_env(*, base_env: Mapping[str, str] | None = None, allowed_keys: Sequence[str] = (), secret_names: Sequence[str] = (), provider: SecretProvider | None = None, consumer: str = "subprocess", purpose: str = "child_process", run_id: str = "unspecified") -> dict[str, str]:
    source = dict(base_env or os.environ)
    names = set(allowed_keys)
    result = {name: source[name] for name in names if name in source}
    result.setdefault("PATH", source.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"))
    for name in secret_names:
        secret = get_secret(name, consumer=consumer, purpose=purpose, run_id=run_id, provider=provider)
        if secret is not None:
            result[name] = secret.reveal(purpose)
    return result


def validate_url(url: str, *, consumer: str = "network", purpose: str = "request", allowed_hosts: Sequence[str] | None = None, allow_localhost: bool = False, policy_path: Path = NETWORK_POLICY_PATH) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise SecurityError("URL_POLICY_DENIED", "URL scheme, credentials, or host is not allowed")
    host = parsed.hostname.lower().rstrip(".")
    ip: ipaddress._BaseAddress | None = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved) and not (allow_localhost and ip.is_loopback):
        raise SecurityError("SSRF_BLOCKED", "private or loopback address is not allowed")
    allowed = set(allowed_hosts or [])
    if not allowed:
        try:
            allowed = set(json.loads(policy_path.read_text(encoding="utf-8")).get("allowed_hosts", []))
        except (OSError, json.JSONDecodeError):
            allowed = set()
    local_ok = allow_localhost and host in {"localhost", "127.0.0.1", "::1"}
    if not local_ok and not any(host == item or host.endswith("." + item.lstrip("*.")) for item in allowed):
        raise SecurityError("URL_HOST_DENIED", "host is not on the network allowlist")
    return url


def classify_permission_mode(mode: int) -> str:
    return "secure" if mode & 0o077 == 0 else "insecure"


def permission_audit(root: Path = ROOT, *, mode: str = "development") -> dict[str, Any]:
    targets = [root / ".env", root / ".env.example", root / "runtime", root / "state", root / "logs", root / "config", root / "deploy"]
    findings: list[dict[str, Any]] = []
    for path in targets:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        file_mode = stat.S_IMODE(info.st_mode)
        is_secret = path.name == ".env" or path.name in {"logs", "runtime", "state"}
        if is_secret and file_mode & 0o077:
            findings.append({"path": str(path), "mode": oct(file_mode), "severity": "high" if mode == "production" else "medium", "code": "INSECURE_PERMISSION"})
    return {"status": "passed" if not findings else ("blocked" if mode == "production" else "warning"), "findings": findings}


def launchagent_audit(root: Path = ROOT) -> dict[str, Any]:
    paths = list((root / "deploy").glob("*.plist")) if (root / "deploy").exists() else []
    findings: list[dict[str, Any]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(word in text.lower() for word in ("api_key", "password", "token", "secret", "authorization")):
            findings.append({"path": str(path), "severity": "high", "code": "LAUNCHAGENT_SECRET_REFERENCE"})
        if "EnvironmentVariables" in text and "PATH" not in text:
            findings.append({"path": str(path), "severity": "medium", "code": "LAUNCHAGENT_ENV_UNBOUNDED"})
    return {"status": "passed" if not findings else "blocked", "installed": False, "findings": findings}


def scan_secrets(root: Path = ROOT, *, include_history: bool = False) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    skip_names = {".env", ".env.local", ".env.production", ".env.example"}
    try:
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=False, timeout=8, check=False)
        files = [Path(item.decode()) for item in tracked.stdout.split(b"\0") if item]
    except (OSError, subprocess.SubprocessError):
        files = []
    for relative in files:
        if relative.name in skip_names or relative.suffix in {".png", ".jpg", ".jpeg", ".sqlite3", ".lock"}:
            continue
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            literal = SECRET_LITERAL_ASSIGNMENT_RE.search(line)
            placeholder = any(marker in line.lower() for marker in ("<your-", "replace-with", "your-token", "your-api-key", "example"))
            if HIGH_CONFIDENCE_RE.search(line) or (literal and not placeholder):
                findings.append({"path": str(relative), "line": number, "severity": "critical", "code": "SECRET_EXPOSURE_DETECTED", "fingerprint": _fingerprint(line[:80])})
    history = {"status": "not_run"}
    if include_history:
        history_findings: list[dict[str, Any]] = []
        try:
            history_process = subprocess.run(
                ["git", "-C", str(root), "log", "--all", "--format=%H", "-p", "--", ".", ":!.env", ":!.env.*"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            commit = "unknown"
            for number, line in enumerate(history_process.stdout.splitlines(), 1):
                if re.fullmatch(r"[0-9a-f]{40}", line.strip()):
                    commit = line.strip()[:12]
                    continue
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                literal = SECRET_LITERAL_ASSIGNMENT_RE.search(line)
                placeholder = any(marker in line.lower() for marker in ("<your-", "replace-with", "your-token", "your-api-key", "example"))
                if HIGH_CONFIDENCE_RE.search(line) or (literal and not placeholder):
                    history_findings.append({"commit": commit, "line": number, "severity": "critical", "code": "SECRET_EXPOSURE_IN_HISTORY", "fingerprint": _fingerprint(line[:80])})
            history = {"status": "blocked" if history_findings else "passed", "scanned": True, "findings": history_findings}
        except (OSError, subprocess.SubprocessError):
            history = {"status": "warning", "scanned": False, "reason": "git_history_unavailable"}
    all_findings = findings + list(history.get("findings", []))
    return {"status": "passed" if not all_findings else "blocked", "findings": findings, "history": history, "local_secret_files": [str(p) for p in (root / ".env",) if p.exists()]}


def _canonical(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class AuditLogger:
    """Append-only JSONL hash chain.  Records are redacted and flushed."""

    def __init__(self, path: Path = AUDIT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        else:
            os.chmod(self.path, stat.S_IMODE(self.path.stat().st_mode) & 0o600)

    def _last_hash(self) -> str:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return "GENESIS"
        if not lines:
            return "GENESIS"
        try:
            return str(json.loads(lines[-1]).get("record_hash", "GENESIS"))
        except (json.JSONDecodeError, AttributeError):
            raise SecurityError("AUDIT_CHAIN_INVALID", "audit log tail is invalid")

    def append(self, action: str, *, actor: str, outcome: str, details: Mapping[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, "actor": actor, "outcome": outcome, "reason": reason, "details": redact_sensitive(dict(details or {}))}
        assert_safe_persistence(payload, path=self.path)
        payload["previous_hash"] = self._last_hash()
        payload["record_hash"] = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return payload

    def verify(self) -> dict[str, Any]:
        previous = "GENESIS"
        count = 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return {"status": "blocked", "reason": type(exc).__name__}
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                return {"status": "blocked", "reason": "invalid_json", "record_count": count}
            record_hash = item.pop("record_hash", None)
            if item.get("previous_hash") != previous or hashlib.sha256(_canonical(item).encode("utf-8")).hexdigest() != record_hash:
                return {"status": "blocked", "reason": "hash_chain_mismatch", "record_count": count}
            previous = str(record_hash)
            count += 1
        return {"status": "passed", "record_count": count, "tail_hash": previous}


ROLE_CAPABILITIES = {
    "viewer": {"security.audit", "security.scan"},
    "operator": {"security.audit", "security.scan", "scheduler.run", "scheduler.mutate", "pipeline.run"},
    "maintainer": {"security.audit", "security.scan", "scheduler.run", "scheduler.mutate", "pipeline.run", "regression.update_baseline", "scheduler.repair", "release.promote", "release.rollback"},
    "security_admin": {"security.audit", "security.scan", "regression.update_baseline", "scheduler.repair", "secrets.rotate", "release.promote", "release.rollback"},
    "publisher": set(),
}


def authorize(*, actor: str, role: str, capability: str, reason: str, approve: bool = False, current_user: str | None = None) -> dict[str, Any]:
    if not actor or not reason.strip():
        return {"allowed": False, "code": "AUTH_REASON_REQUIRED"}
    if actor != (current_user or os.environ.get("USER", "")):
        return {"allowed": False, "code": "AUTH_ACTOR_MISMATCH"}
    if role not in ROLE_CAPABILITIES or capability not in ROLE_CAPABILITIES[role]:
        return {"allowed": False, "code": "AUTH_CAPABILITY_DENIED"}
    if capability in {"regression.update_baseline", "scheduler.mutate", "scheduler.repair", "secrets.rotate", "release.promote", "release.rollback"} and not approve:
        return {"allowed": False, "code": "AUTH_APPROVAL_REQUIRED"}
    return {"allowed": True, "code": "AUTHORIZED", "actor": actor, "role": role, "capability": capability}


def dependency_audit(root: Path = ROOT) -> dict[str, Any]:
    lock = root / "uv.lock"
    workflow = root / ".github" / "workflows" / "ci.yml"
    return {"status": "passed" if lock.exists() else "warning", "lockfile_present": lock.exists(), "ci_present": workflow.exists(), "network_install": "uv sync --locked" if workflow.exists() and "--locked" in workflow.read_text(encoding="utf-8", errors="replace") else "unknown"}


def subprocess_audit(root: Path = ROOT) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    unsafe_shell = "shell" + "=True"
    unsafe_env = "os.environ" + ".copy()"
    for path in root.rglob("*.py"):
        if any(part in {".venv", ".git", "__pycache__"} for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if unsafe_shell in line or unsafe_env in line:
                findings.append({"path": str(path.relative_to(root)), "line": number, "severity": "high", "code": "UNSAFE_SUBPROCESS_CONFIGURATION"})
    return {"status": "passed" if not findings else "blocked", "findings": findings}


def secret_inventory(*, root: Path = ROOT, mode: str = "development", provider: SecretProvider | None = None) -> dict[str, Any]:
    registry = load_secret_registry()
    items = []
    for entry in registry["secrets"]:
        name = str(entry["name"])
        try:
            secret = (provider or CompositeSecretProvider(mode=mode, root=root)).get(name, consumer="security_audit", purpose="inventory", run_id="security-audit")
        except SecurityError:
            secret = None
        items.append({"name": name, "present": secret is not None, "source": secret.source if secret else None, "masked": secret.masked() if secret else None, "fingerprint": secret.fingerprint() if secret else None, "allowed_consumers": entry.get("allowed_consumers", [])})
    return {"mode": mode, "items": items, "plaintext_exposed": False}


def rotation_preview(*, name: str, new_account: str, registry_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_secret_registry(registry_path)
    entry = _registry_entry(name, registry)
    if not entry:
        raise SecurityError("SECRET_NOT_REGISTERED", "secret is not registered")
    old_account = (entry.get("keychain") or {}).get("account", name)
    return {"status": "preview", "secret_name": name, "current_account": old_account, "proposed_account": new_account, "changed": old_account != new_account, "plaintext_exposed": False}


def switch_keychain_account(*, name: str, new_account: str, registry_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    if not new_account or any(char in new_account for char in "\n\r/"):
        raise SecurityError("ROTATION_ACCOUNT_INVALID", "keychain account is invalid")
    registry = load_secret_registry(registry_path)
    entry = _registry_entry(name, registry)
    if not entry:
        raise SecurityError("SECRET_NOT_REGISTERED", "secret is not registered")
    entry.setdefault("keychain", {})["account"] = new_account
    assert_safe_persistence(registry, path=registry_path)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{registry_path.name}.", suffix=".tmp", dir=registry_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, registry_path)
        os.chmod(registry_path, 0o600)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return rotation_preview(name=name, new_account=new_account, registry_path=registry_path) | {"status": "switched", "plaintext_exposed": False}


def security_preflight(*, root: Path = ROOT, mode: str = "development", provider_name: str = "rule_template", delivery_enabled: bool = False) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    env_file = root / ".env"
    if mode == "production" and env_file.exists():
        issues.append({"severity": "critical", "code": "PLAINTEXT_ENV_FORBIDDEN"})
    permissions = permission_audit(root, mode=mode)
    issues.extend(permissions["findings"])
    scanner = scan_secrets(root, include_history=True)
    issues.extend(scanner["findings"])
    launchagent = launchagent_audit(root)
    issues.extend(launchagent["findings"])
    processes = subprocess_audit(root)
    issues.extend(processes["findings"])
    if delivery_enabled:
        issues.append({"severity": "critical", "code": "EXTERNAL_DELIVERY_ENABLED"})
    if provider_name not in {"rule_template", "ollama", "gemini", "openai", "auto"}:
        issues.append({"severity": "high", "code": "PROVIDER_UNREGISTERED"})
    required_secret_by_provider = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
    required_secret = required_secret_by_provider.get(provider_name)
    if mode == "production" and required_secret:
        available = get_secret(required_secret, consumer="content_generator", purpose="generate_market_content", run_id="security-preflight", mode=mode)
        if available is None:
            issues.append({"severity": "critical", "code": "REQUIRED_PROVIDER_SECRET_MISSING", "secret_name": required_secret})
    blocked = any(item.get("severity") in {"critical", "high"} for item in issues)
    return {"status": "blocked" if blocked else ("warning" if issues else "passed"), "mode": mode, "provider": provider_name, "issues": issues, "permissions": permissions, "secret_scan": scanner, "launchagent": launchagent, "subprocess_audit": processes, "dependency_audit": dependency_audit(root), "delivery_enabled": delivery_enabled}


def security_gate(report: Mapping[str, Any], *, release_gate_status: str = "passed") -> dict[str, Any]:
    issues = list(report.get("issues", []))
    blockers = [item for item in issues if item.get("severity") in {"critical", "high"}]
    if release_gate_status not in {"passed", "warning", "not_required"}:
        blockers.append({"code": "RELEASE_GATE_BLOCKED", "severity": "high"})
    return {"status": "blocked" if blockers else "passed", "blockers": blockers, "checks": {"security_preflight": not bool(blockers), "release_gate": release_gate_status}}


__all__ = [name for name in globals() if not name.startswith("_")]
