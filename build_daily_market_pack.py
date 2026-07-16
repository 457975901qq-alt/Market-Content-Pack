#!/usr/bin/env python3
"""Build and validate the daily market content and 9-page image pack."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ERROR_LOG = ROOT / "logs" / "error.log"


def append_error(message: str) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")


def run_step(cmd: list[str], required: bool = True) -> int:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        append_error(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
        if required:
            return proc.returncode
    return 0


def main() -> int:
    # 1. Generate and validate the only content source.
    status = run_step([sys.executable, "market_content_openai.py"], required=True)
    if status != 0:
        return status

    # 2. GitHub data enriches page 09. Failure is recorded; renderer uses a
    # conservative missing-data state and never invents project metrics.
    run_step([sys.executable, "github_ai_projects.py"], required=False)

    # 3. Render the unified 1080x1920, 9-page pack.
    status = run_step([sys.executable, "render_market_pack_unified.py"], required=True)
    if status != 0:
        return status

    # 4. Block publication unless every page passes structural QA.
    return run_step([sys.executable, "validate_market_image_pack.py"], required=True)


if __name__ == "__main__":
    raise SystemExit(main())
