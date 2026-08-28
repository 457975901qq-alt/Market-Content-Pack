#!/usr/bin/env python3
"""Offline-safe L6-5 release lifecycle CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release import (  # noqa: E402
    ReleaseError,
    VersionRouter,
    deployment_integrity,
    prepare_release,
    promote_release,
    release_history,
    release_status,
    run_offline_release_drill,
    verify_package,
)


def _json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _read(path: str | None, default: object) -> object:
    if not path:
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L6-5 release, canary and rollback controls")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--allow-dirty", action="store_true")
    prepare.add_argument("--skip-checks", action="store_true")
    prepare.add_argument("--security-mode", choices=["development", "production"], default="development")
    prepare.add_argument("--provider", default="rule_template")

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--version", required=True)

    verify = sub.add_parser("verify-package")
    verify.add_argument("path")

    integrity = sub.add_parser("integrity")
    integrity.add_argument("--version")

    promote = sub.add_parser("promote")
    promote.add_argument("--version", required=True)
    promote.add_argument("--stage", choices=["shadow", "canary-1", "canary-2", "canary-3", "active"], required=True)
    promote.add_argument("--actor", default=os.environ.get("USER", ""))
    promote.add_argument("--role", default="maintainer")
    promote.add_argument("--reason", default="")
    promote.add_argument("--approve", action="store_true")
    promote.add_argument("--canary-json")

    rollback = sub.add_parser("rollback")
    rollback.add_argument("--to-version", required=True)
    rollback.add_argument("--actor", default=os.environ.get("USER", ""))
    rollback.add_argument("--role", default="maintainer")
    rollback.add_argument("--reason", default="")
    rollback.add_argument("--approve", action="store_true")

    route = sub.add_parser("route")
    route.add_argument("--stage", required=True)
    route.add_argument("--job-type", required=True)
    route.add_argument("--active-version", required=True)
    route.add_argument("--candidate-version")

    sub.add_parser("status")
    sub.add_parser("history")
    sub.add_parser("drill")

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            _json(prepare_release(ROOT, args.version, allow_dirty=args.allow_dirty, execute_checks=not args.skip_checks, security_mode=args.security_mode, security_provider=args.provider))
        elif args.command == "preflight":
            path = ROOT / "releases" / args.version / "release_preflight.json"
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "blocked", "code": "RELEASE_PREFLIGHT_MISSING", "version": args.version}
            _json(payload)
            return 0 if payload.get("status") in {"passed", "warning"} else 2
        elif args.command == "verify-package":
            _json(verify_package(Path(args.path).expanduser().resolve()))
        elif args.command == "integrity":
            _json(deployment_integrity(ROOT, args.version))
        elif args.command == "promote":
            results = _read(args.canary_json, [])
            _json(promote_release(ROOT, args.version, stage=args.stage, actor=args.actor, role=args.role, reason=args.reason, approve=args.approve, canary_results=results if isinstance(results, list) else []))
        elif args.command == "rollback":
            from release import rollback_release
            _json(rollback_release(ROOT, args.to_version, actor=args.actor, role=args.role, reason=args.reason, approve=args.approve))
        elif args.command == "route":
            _json(VersionRouter(args.active_version, args.candidate_version).route(args.job_type, stage=args.stage))
        elif args.command == "status":
            _json(release_status(ROOT))
        elif args.command == "history":
            _json(release_history(ROOT))
        elif args.command == "drill":
            result = run_offline_release_drill(ROOT)
            _json(result)
            return 0 if result.get("passed") else 2
    except (ReleaseError, OSError, ValueError, json.JSONDecodeError) as exc:
        _json({"status": "blocked", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
