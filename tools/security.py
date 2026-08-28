#!/usr/bin/env python3
"""L6-4 security audit, preflight, gate, and offline-drill CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# When invoked as ``python tools/security.py``, Python puts ``tools`` before
# the project root and would otherwise import this CLI as the ``security``
# package.  Pin the real project root first.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security import AuditLogger, SecurityError, assert_safe_persistence, authorize, scan_secrets, security_preflight
from security.core import dependency_audit, launchagent_audit, permission_audit, rotation_preview, secret_inventory, security_gate, switch_keychain_account


REPORT_ROOT = ROOT / "reports" / "security"


def _write_json(path: Path, payload: Any) -> None:
    assert_safe_persistence(payload, path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _audit(args: argparse.Namespace) -> dict[str, Any]:
    preflight = security_preflight(root=ROOT, mode=args.mode, provider_name=args.provider, delivery_enabled=args.delivery_enabled)
    gate = security_gate(preflight, release_gate_status=args.release_gate)
    report = {
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "preflight": preflight,
        "gate": gate,
        "inventory": secret_inventory(root=ROOT, mode=args.mode),
        "permissions": permission_audit(ROOT),
        "launchagent": launchagent_audit(ROOT),
        "dependency_audit": dependency_audit(ROOT),
        "sensitive_values_logged": False,
    }
    _write_json(REPORT_ROOT / "security_audit.json", report)
    AuditLogger().append("security.audit", actor=args.actor or os.environ.get("USER", "unknown"), outcome=gate["status"], details={"mode": args.mode, "gate": gate["status"]}, reason=args.reason or "offline security audit")
    return report


def _authorize_dangerous(args: argparse.Namespace, capability: str) -> None:
    decision = authorize(actor=args.actor, role=args.role, capability=capability, reason=args.reason, approve=args.approve)
    AuditLogger().append("authorization." + capability, actor=args.actor, outcome="allowed" if decision["allowed"] else "denied", details={"code": decision["code"], "role": args.role}, reason=args.reason)
    if not decision["allowed"]:
        raise SecurityError(decision["code"], "authorization denied")


def _drills() -> dict[str, Any]:
    from tools.security_drills import run_drills
    return run_drills()


def _rotation_preview(args: argparse.Namespace) -> dict[str, Any]:
    return rotation_preview(name=args.name, new_account=args.account)


def _rotation_switch(args: argparse.Namespace) -> dict[str, Any]:
    _authorize_dangerous(args, "secrets.rotate")
    return switch_keychain_account(name=args.name, new_account=args.account)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mode", choices=["development", "test", "production"], default="development")
    common.add_argument("--provider", default="rule_template")
    common.add_argument("--release-gate", default="passed")
    common.add_argument("--delivery-enabled", action="store_true")
    common.add_argument("--actor", default=os.environ.get("USER", ""))
    common.add_argument("--role", default="operator")
    common.add_argument("--reason", default="")
    audit = sub.add_parser("audit", parents=[common])
    audit.set_defaults(handler=lambda args: _audit(args))
    preflight = sub.add_parser("preflight", parents=[common])
    preflight.set_defaults(handler=lambda args: security_preflight(root=ROOT, mode=args.mode, provider_name=args.provider, delivery_enabled=args.delivery_enabled))
    gate = sub.add_parser("gate", parents=[common])
    gate.set_defaults(mode="production")
    gate.set_defaults(handler=lambda args: security_gate(security_preflight(root=ROOT, mode=args.mode, provider_name=args.provider, delivery_enabled=args.delivery_enabled), release_gate_status=args.release_gate))
    scan = sub.add_parser("scan-secrets")
    scan.add_argument("--include-history", action="store_true")
    scan.set_defaults(handler=lambda args: scan_secrets(ROOT, include_history=args.include_history))
    perms = sub.add_parser("permissions")
    perms.set_defaults(handler=lambda args: permission_audit(ROOT))
    launch = sub.add_parser("launchagent")
    launch.set_defaults(handler=lambda args: launchagent_audit(ROOT))
    deps = sub.add_parser("dependencies")
    deps.set_defaults(handler=lambda args: dependency_audit(ROOT))
    inv = sub.add_parser("secrets-inventory", parents=[common])
    inv.set_defaults(handler=lambda args: secret_inventory(root=ROOT, mode=args.mode))
    rotate = sub.add_parser("secrets-rotate-preview")
    rotate.add_argument("--name", required=True)
    rotate.add_argument("--account", required=True)
    rotate.set_defaults(handler=_rotation_preview)
    switch = sub.add_parser("secrets-switch", parents=[common])
    switch.add_argument("--name", required=True)
    switch.add_argument("--account", required=True)
    switch.add_argument("--approve", action="store_true")
    switch.set_defaults(handler=_rotation_switch)
    drills = sub.add_parser("drill")
    drills.set_defaults(handler=lambda args: _drills())
    verify = sub.add_parser("verify-audit")
    verify.set_defaults(handler=lambda args: AuditLogger().verify())
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except SecurityError as exc:
        result = {"status": "blocked", "code": exc.code, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") not in {"blocked", "failed"} and result.get("gate", {}).get("status", "passed") != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
