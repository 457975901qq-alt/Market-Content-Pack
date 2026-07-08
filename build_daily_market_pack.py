#!/usr/bin/env python3
"""Build the daily market pack with optional GitHub AI project data refresh."""

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
    market_content_status = run_step([sys.executable, "market_content_openai.py"], required=True)
    if market_content_status != 0:
        return market_content_status

    # GitHub data improves the AI open-source section, but rendering should still
    # complete with fallback projects if the token is missing or the API fails.
    run_step([sys.executable, "github_ai_projects.py"], required=False)
    return run_step([sys.executable, "render_market_pack_calm_20260708.py"], required=True)


if __name__ == "__main__":
    raise SystemExit(main())
