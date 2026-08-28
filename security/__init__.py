"""L6-4 security controls for the daily market-content pipeline."""

from .core import (
    AuditLogger,
    CompositeSecretProvider,
    KeychainSecretProvider,
    MemorySecretProvider,
    SecretProvider,
    SecretValue,
    SecurityError,
    assert_safe_persistence,
    authorize,
    build_subprocess_env,
    classify_permission_mode,
    get_secret,
    load_secret_registry,
    redact_sensitive,
    scan_secrets,
    security_gate,
    security_preflight,
    validate_url,
)

__all__ = [
    "AuditLogger", "CompositeSecretProvider", "KeychainSecretProvider",
    "MemorySecretProvider", "SecretProvider", "SecretValue", "SecurityError",
    "assert_safe_persistence", "authorize", "build_subprocess_env",
    "classify_permission_mode", "get_secret", "load_secret_registry",
    "redact_sensitive", "scan_secrets", "security_gate", "security_preflight",
    "validate_url",
]
