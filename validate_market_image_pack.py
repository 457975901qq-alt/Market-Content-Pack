#!/usr/bin/env python3
"""Validate the directly rendered 8-page market image pack."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
PACK_DIR = ROOT / "outputs" / "market_image_pack"
MANIFEST_PATH = PACK_DIR / "manifest.json"
ERROR_LOG = ROOT / "logs" / "market_image_pack_errors.log"
EXPECTED_SIZE = (1080, 1440)
EXPECTED_NAMES = [
    "01_cover.png",
    "02_overview.png",
    "03_macro_central_banks.png",
    "04_commodities_geopolitics.png",
    "05_ai_semiconductors.png",
    "06_big_tech_assets.png",
    "07_calendar_opex_github.png",
    "08_flows_summary.png",
]


def fail(code: str, detail: str) -> int:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"error_type": code, "detail": detail}, ensure_ascii=False) + "\n")
    print(f"FAIL [{code}] {detail}", file=sys.stderr)
    return 1


def main() -> int:
    if not MANIFEST_PATH.exists():
        return fail("manifest_missing", str(MANIFEST_PATH))
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fail("manifest_invalid", str(exc))

    if manifest.get("width") != EXPECTED_SIZE[0] or manifest.get("height") != EXPECTED_SIZE[1]:
        return fail("manifest_size_mismatch", f"expected {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}")

    pages = manifest.get("pages")
    if not isinstance(pages, list) or len(pages) != 8:
        size_text = len(pages) if isinstance(pages, list) else "invalid"
        return fail("page_count_mismatch", f"expected 8 pages, got {size_text}")

    names = [Path(item).name for item in pages]
    if names != EXPECTED_NAMES:
        return fail("page_order_mismatch", f"expected {EXPECTED_NAMES}, got {names}")

    for name in EXPECTED_NAMES:
        page_path = PACK_DIR / name
        if not page_path.exists():
            return fail("page_missing", name)
        try:
            with Image.open(page_path) as image:
                if image.format != "PNG":
                    return fail("format_mismatch", f"{name}: {image.format}")
                if image.size != EXPECTED_SIZE:
                    return fail("size_mismatch", f"{name}: {image.size}")
                if image.mode not in {"RGB", "RGBA"}:
                    return fail("mode_mismatch", f"{name}: {image.mode}")
        except OSError as exc:
            return fail("image_open_failed", f"{name}: {exc}")
        if page_path.stat().st_size < 20_000:
            return fail("suspiciously_small_image", f"{name}: {page_path.stat().st_size} bytes")

    print("PASS: 8-page market image pack validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
