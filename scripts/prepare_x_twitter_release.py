#!/usr/bin/env python3
"""Prepare, but never authorize or send, one X/Twitter release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_preparation import prepare_x_twitter_release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 X/Twitter 发布前检查与审批材料，不发送")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target", required=True, help="已批准的目标账号标识；不会自动加入白名单")
    args = parser.parse_args(argv)
    report = prepare_x_twitter_release(args.root.resolve(), args.run_id, args.target)
    print(json.dumps({
        "status": report["status"],
        "run_id": report["run_id"],
        "adapter": report["adapter"],
        "text_length": report["text_length"],
        "blockers": report["blockers"],
        "approval_digest": report["approval_digest"],
        "external_request_made": report["external_request_made"],
        "config_mutated": report["config_mutated"],
        "delivered": report["delivered"],
        "report_path": report["report_path"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "READY_FOR_HUMAN_APPROVAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
