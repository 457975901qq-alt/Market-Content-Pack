#!/usr/bin/env python3
"""Split a combined market-pack contact sheet into independent social images.

The image generator sometimes returns one 2x4 sheet even when the prompt asks
for eight pages. This post-processor converts that sheet into eight standalone
1080x1440 PNG files, writes a manifest, and optionally removes the source sheet.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "market_pack_pages"
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class ExportedPage:
    page: int
    filename: str
    width: int
    height: int
    crop_box: tuple[int, int, int, int]


def image_files(paths: Iterable[Path]) -> list[Path]:
    return sorted(
        (path for path in paths if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def latest_image(search_root: Path) -> Path:
    candidates = image_files(search_root.rglob("*"))
    if not candidates:
        raise FileNotFoundError(f"No image found under: {search_root}")
    return candidates[0]


def validate_grid(cols: int, rows: int) -> None:
    if cols <= 0 or rows <= 0:
        raise ValueError("Grid columns and rows must both be positive")
    if cols * rows != 8:
        raise ValueError(f"Expected an 8-page grid, got {cols}x{rows}={cols * rows}")


def cell_box(
    *,
    image_width: int,
    image_height: int,
    col: int,
    row: int,
    cols: int,
    rows: int,
    outer_margin: int,
    gutter_x: int,
    gutter_y: int,
) -> tuple[int, int, int, int]:
    usable_width = image_width - outer_margin * 2 - gutter_x * (cols - 1)
    usable_height = image_height - outer_margin * 2 - gutter_y * (rows - 1)
    if usable_width <= 0 or usable_height <= 0:
        raise ValueError("Margins and gutters leave no usable image area")

    cell_width = usable_width / cols
    cell_height = usable_height / rows

    left = round(outer_margin + col * (cell_width + gutter_x))
    top = round(outer_margin + row * (cell_height + gutter_y))
    right = round(left + cell_width)
    bottom = round(top + cell_height)
    return left, top, right, bottom


def fit_to_canvas(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize without distortion, then center-crop to the requested canvas."""
    target_width, target_height = size
    source_width, source_height = image.size
    scale = max(target_width / source_width, target_height / source_height)
    resized = image.resize(
        (round(source_width * scale), round(source_height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def split_sheet(
    source: Path,
    output_dir: Path,
    *,
    cols: int = 2,
    rows: int = 4,
    page_size: tuple[int, int] = (1080, 1440),
    outer_margin: int = 0,
    gutter_x: int = 0,
    gutter_y: int = 0,
    prefix: str | None = None,
    remove_source: bool = False,
) -> list[ExportedPage]:
    validate_grid(cols, rows)
    output_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as opened:
        sheet = opened.convert("RGB")

    if sheet.width < cols * 100 or sheet.height < rows * 100:
        raise ValueError(
            f"Source image is too small for a {cols}x{rows} sheet: {sheet.size}"
        )

    filename_prefix = prefix or source.stem
    exported: list[ExportedPage] = []

    for row in range(rows):
        for col in range(cols):
            page_number = row * cols + col + 1
            crop_box = cell_box(
                image_width=sheet.width,
                image_height=sheet.height,
                col=col,
                row=row,
                cols=cols,
                rows=rows,
                outer_margin=outer_margin,
                gutter_x=gutter_x,
                gutter_y=gutter_y,
            )
            page = fit_to_canvas(sheet.crop(crop_box), page_size)
            filename = f"{filename_prefix}_{page_number:02d}.png"
            destination = output_dir / filename
            page.save(destination, format="PNG", optimize=True)
            exported.append(
                ExportedPage(
                    page=page_number,
                    filename=filename,
                    width=page.width,
                    height=page.height,
                    crop_box=crop_box,
                )
            )

    manifest = {
        "source": str(source),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "grid": {"columns": cols, "rows": rows},
        "page_size": {"width": page_size[0], "height": page_size[1]},
        "page_count": len(exported),
        "pages": [asdict(item) for item in exported],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if remove_source:
        source.unlink()

    return exported


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, help="Combined 2x4 market-pack image")
    parser.add_argument(
        "--search-root",
        type=Path,
        default=ROOT / "outputs",
        help="Find the newest image here when source is omitted",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--cols", type=int, default=2)
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1440)
    parser.add_argument("--outer-margin", type=int, default=0)
    parser.add_argument("--gutter-x", type=int, default=0)
    parser.add_argument("--gutter-y", type=int, default=0)
    parser.add_argument("--prefix")
    parser.add_argument(
        "--remove-source",
        action="store_true",
        default=os.getenv("MARKET_PACK_REMOVE_COMBINED", "0") == "1",
        help="Delete the combined source image after all pages are exported",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source or latest_image(args.search_root)
    exported = split_sheet(
        source.resolve(),
        args.output_dir.resolve(),
        cols=args.cols,
        rows=args.rows,
        page_size=(args.width, args.height),
        outer_margin=args.outer_margin,
        gutter_x=args.gutter_x,
        gutter_y=args.gutter_y,
        prefix=args.prefix,
        remove_source=args.remove_source,
    )
    print(f"Exported {len(exported)} independent pages to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
