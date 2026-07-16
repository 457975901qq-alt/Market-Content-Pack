#!/usr/bin/env python3
"""Validate the generated market image pack before publication."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

from market_pack_design import HEIGHT, WIDTH

ROOT = Path(__file__).resolve().parent
PACK_DIR = ROOT / "outputs" / "market_image_pack"
MANIFEST = PACK_DIR / "manifest.json"
ERROR_LOG = ROOT / "logs" / "market_image_pack_errors.log"
EXPECTED_NAMES = [
    "01_cover.png",
    "02_overview.png",
    "03_macro_central_banks.png",
    "04_commodities_geopolitics.png",
    "05_ai_semiconductors.png",
    "06_big_tech_assets.png",
    "07_calendar_opex.png",
    "08_etf_flows_structure.png",
    "09_github_weekly.png",
]


def fail(code: str, detail: str) -> int:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"error_type": code, "detail": detail}, ensure_ascii=False) + "\n")
    print(f"FAIL [{code}] {detail}", file=sys.stderr)
    return 1


def main() -> int:
    if not MANIFEST.exists():
        return fail("manifest_missing", str(MANIFEST))
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fail("manifest_invalid", str(exc))
    if manifest.get("width") != WIDTH or manifest.get("height") != HEIGHT:
        return fail("manifest_size_mismatch", f"expected {WIDTH}x{HEIGHT}")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or len(pages) != 9:
        return fail("page_count_mismatch", f"expected 9 pages, got {len(pages) if isinstance(pages, list) else 'invalid'}")
    actual_names = [Path(item).name for item in pages]
    if actual_names != EXPECTED_NAMES:
        return fail("page_order_mismatch", f"expected {EXPECTED_NAMES}, got {actual_names}")
    for name in EXPECTED_NAMES:
        path = PACK_DIR / name
        if not path.exists():
            return fail("page_missing", name)
        try:
            with Image.open(path) as image:
                if image.format != "PNG":
                    return fail("format_mismatch", f"{name}: {image.format}")
                if image.size != (WIDTH, HEIGHT):
                    return fail("size_mismatch", f"{name}: {image.size}")
                if image.mode not in {"RGB", "RGBA"}:
                    return fail("mode_mismatch", f"{name}: {image.mode}")
        except OSError as exc:
            return fail("image_open_failed", f"{name}: {exc}")
        if path.stat().st_size < 25_000:
            return fail("suspiciously_small_image", f"{name}: {path.stat().st_size} bytes")
    print("PASS: 9-page market image pack validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
