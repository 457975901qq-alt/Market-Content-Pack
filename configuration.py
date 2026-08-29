"""Runtime policy and non-secret configuration diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "config" / "runtime_policy.json"


def load_env_file(path: Path = ROOT / ".env") -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values


def load_runtime_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"runtime_policy_unreadable:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("runtime_policy_must_be_object")
    return payload


def configuration_report(
    *,
    provider: str | None = None,
    environ: Mapping[str, str] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = {**load_env_file(), **dict(environ or os.environ)}
    policy = policy or load_runtime_policy()
    selected = (provider or env.get("MARKET_CONTENT_PROVIDER") or "auto").strip().lower()
    required_by_provider = policy.get("required_environment_by_provider") or {}
    required = list(required_by_provider.get(selected, []))
    if selected == "auto":
        required = []
    missing = [name for name in required if not str(env.get(name, "")).strip()]
    return {
        "service": "configuration",
        "status": "healthy" if not missing else "unavailable",
        "provider": selected,
        "required_environment": required,
        "missing_environment": missing,
        "step_timeout_seconds": int(policy.get("step_timeout_seconds", 600)),
        "source_fallback_enabled": bool(policy.get("source_fallback_enabled", True)),
        "sensitive_values_logged": False,
    }


__all__ = ["configuration_report", "load_env_file", "load_runtime_policy"]
