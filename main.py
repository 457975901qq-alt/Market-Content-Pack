"""Unified application entry point.

The business pipeline remains implemented in ``build_daily_market_pack``;
this module only provides the stable project-level command.
"""

from __future__ import annotations

from build_daily_market_pack import main as build_main


if __name__ == "__main__":
    raise SystemExit(build_main())
