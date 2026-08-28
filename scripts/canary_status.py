#!/usr/bin/env python3
"""Read-only Shadow Canary stability status command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canary_controls import evaluate_canary_stability


def build_status(root: Path) -> dict:
    result = evaluate_canary_stability(root)
    return {
        "window_size": result["window_size"],
        "eligible_runs": result["eligible_runs"],
        "remaining_runs": max(0, int(result["window_size"]) - int(result["eligible_runs"])),
        "completed": result["completed"],
        "qa_pass": result["qa_pass"],
        "reviewer_pass": result["reviewer_pass"],
        "final_gate_pass": result["final_gate_pass"],
        "checks": result["checks"],
        "canary_stability_pass": result["canary_stability_pass"],
        "blocking_reasons": result["blocking_reasons"],
        "run_ids": result["run_ids"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读查看 Shadow Canary Stability Window")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    status = build_status(args.root.resolve())
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print("Canary Stability Status")
        print("=======================")
        for key in ("window_size", "eligible_runs", "remaining_runs", "completed", "qa_pass", "reviewer_pass", "final_gate_pass"):
            print(f"{key}: {status[key]}")
        for key, value in status["checks"].items():
            if key not in {"completed", "qa_pass", "reviewer_pass", "final_gate_pass"}:
                print(f"{key}: {value}")
        print(f"canary_stability_pass: {status['canary_stability_pass']}")
        print("blocking_reasons:")
        for reason in status["blocking_reasons"]:
            print(f"- {reason}")
    return 0 if status["canary_stability_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
