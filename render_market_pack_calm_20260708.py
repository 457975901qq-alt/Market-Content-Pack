#!/usr/bin/env python3
"""Render market content images from image_text only.

The image renderer intentionally reads only:
image_text.title, image_text.subtitle, and image_text.sections.
Platform copy such as douyin/X/WeChat text must stay in separate text files.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs" / "market_content"
MARKET_CONTENT_JSON = OUT / "market_content.json"
TOKYO = ZoneInfo("Asia/Tokyo")

W, H = 1080, 1920
BG = "#ffffff"
INK = "#17212f"
MUTED = "#667085"
SUBTLE = "#eef2f6"
LINE = "#d8dee8"
NAVY = "#13233a"
BLUE = "#2563eb"
GREEN = "#139667"
RED = "#cf3f3f"
AMBER = "#d98c1f"
PANEL = "#fbfcfe"
PROHIBITED_COPY_PATTERNS = [
    r"抖音[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"小红书[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"公众号[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"X\s*文案[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"Twitter\s*文案[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"平台文案[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"发布文案[^，。；;.!！?？]*[，。；;.!！?？]?",
    r"douyin|cover_title|caption|hashtags|wechat\.md|x\.md",
]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    preferred = "/System/Library/Fonts/STHeiti Medium.ttc" if bold else FONT_CANDIDATES[0]
    for path in [preferred, *FONT_CANDIDATES]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


F = {
    "hero": font(48, True),
    "title": font(32, True),
    "heading": font(25, True),
    "body": font(23),
    "small": font(18),
    "tiny": font(15),
    "num": font(34, True),
}


def now_tokyo() -> dt.datetime:
    return dt.datetime.now(TOKYO)


def measure(draw: ImageDraw.ImageDraw, text: str, font_obj: ImageFont.ImageFont) -> int:
    return draw.textbbox((0, 0), text, font=font_obj)[2]


def wrap(draw: ImageDraw.ImageDraw, value: str, font_obj: ImageFont.ImageFont, max_w: int, max_lines: int) -> list[str]:
    words = list(value.strip())
    lines: list[str] = []
    cur = ""
    for ch in words:
        candidate = cur + ch
        if measure(draw, candidate, font_obj) <= max_w:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
        cur = ch
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and "".join(lines) != value.strip():
        last = lines[-1]
        while last and measure(draw, last + "...", font_obj) > max_w:
            last = last[:-1]
        lines[-1] = last + "..."
    return lines


def split_content(value: str, limit: int = 86) -> list[str]:
    normalized = re.sub(r"\s+", " ", value.strip())
    if len(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    buf = ""
    for part in re.split(r"([。；;.!！?？])", normalized):
        if not part:
            continue
        if len(buf) + len(part) <= limit:
            buf += part
            continue
        if buf:
            chunks.append(buf.strip())
        buf = part
        while len(buf) > limit:
            chunks.append(buf[:limit].strip())
            buf = buf[limit:]
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def sanitize_image_copy(value: str) -> str:
    cleaned = value.strip()
    for pattern in PROHIBITED_COPY_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(" ，。；;")


def load_image_text() -> dict[str, Any]:
    if not MARKET_CONTENT_JSON.exists():
        raise FileNotFoundError(f"market content JSON not found: {MARKET_CONTENT_JSON}")
    data = json.loads(MARKET_CONTENT_JSON.read_text(encoding="utf-8"))
    image_text = data.get("image_text")
    if not isinstance(image_text, dict):
        raise ValueError("image_text is missing or not an object")
    title = image_text.get("title")
    sections = image_text.get("sections")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("image_text.title is missing or empty")
    if not isinstance(sections, list) or not sections:
        raise ValueError("image_text.sections is missing or empty")
    sanitized_title = sanitize_image_copy(title)
    if not sanitized_title:
        raise ValueError("image_text.title becomes empty after platform-copy filtering")
    return {
        "title": sanitized_title,
        "subtitle": sanitize_image_copy(str(image_text.get("subtitle", ""))),
        "sections": image_text.get("sections", []),
    }


def build_cards(image_text: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for section in image_text.get("sections", []):
        if not isinstance(section, dict):
            continue
        heading = sanitize_image_copy(str(section.get("heading", "")))
        content = sanitize_image_copy(str(section.get("content", "")))
        if not heading or not content:
            continue
        chunks = split_content(content)
        for index, chunk in enumerate(chunks):
            suffix = "" if index == 0 else f" {index + 1}"
            cards.append({"heading": f"{heading}{suffix}", "content": chunk})
    if not cards:
        raise ValueError("image_text.sections has no renderable heading/content pairs")
    return cards


def card_height(content: str) -> int:
    length = len(content)
    if length > 70:
        return 330
    if length > 45:
        return 292
    return 254


def paginate(cards: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    pages: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    used = 0
    max_used = 1420
    for card in cards:
        h = card_height(card["content"]) + 24
        if current and used + h > max_used:
            pages.append(current)
            current = []
            used = 0
        current.append(card)
        used += h
    if current:
        pages.append(current)
    return pages


def clear_old_images() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for path in OUT.glob("market_pack_*.png"):
        path.unlink()


def draw_header(draw: ImageDraw.ImageDraw, image_text: dict[str, Any], page_no: int, total_pages: int) -> None:
    title = str(image_text.get("title", "")).strip()
    subtitle = str(image_text.get("subtitle", "")).strip()

    draw.rounded_rectangle((42, 42, 1038, 238), radius=34, fill=NAVY)
    draw.text((80, 78), title[:24], fill="white", font=F["hero"])
    for i, line in enumerate(wrap(draw, subtitle, F["small"], 710, 2)):
        draw.text((82, 151 + i * 28), line, fill="#d6e0ef", font=F["small"])
    draw.rounded_rectangle((830, 82, 986, 132), radius=25, fill="white")
    draw.text((908, 95), f"{page_no:02d}/{total_pages:02d}", fill=NAVY, font=F["small"], anchor="ma")

    draw.rounded_rectangle((80, 258, 1000, 268), radius=5, fill=SUBTLE)
    draw.rounded_rectangle((80, 258, 80 + int(920 * page_no / total_pages), 268), radius=5, fill=BLUE)


def draw_visual_strip(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, index: int) -> None:
    palette = [BLUE, GREEN, AMBER, RED]
    color = palette[index % len(palette)]
    draw.rounded_rectangle((x, y, x + w, y + 48), radius=18, fill="#f3f6fb")
    bar_x = x + 22
    for i, height in enumerate([14, 26, 20, 32, 18]):
        bx = bar_x + i * 46
        draw.rounded_rectangle((bx, y + 32 - height, bx + 24, y + 32), radius=5, fill=color if i <= index % 5 else LINE)
    for i in range(3):
        cx = x + w - 126 + i * 42
        draw.ellipse((cx, y + 14, cx + 20, y + 34), fill=palette[(index + i) % len(palette)])


def draw_card(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, card: dict[str, str], index: int) -> None:
    palette = [BLUE, GREEN, AMBER, RED]
    color = palette[index % len(palette)]
    draw.rounded_rectangle((x, y, x + w, y + h), radius=28, fill=PANEL, outline=LINE, width=2)
    draw.rounded_rectangle((x, y, x + 14, y + h), radius=7, fill=color)

    draw.ellipse((x + 38, y + 34, x + 90, y + 86), fill=color)
    draw.text((x + 64, y + 46), f"{index + 1}", fill="white", font=F["small"], anchor="ma")

    heading_lines = wrap(draw, card["heading"], F["heading"], w - 150, 1)
    draw.text((x + 112, y + 34), heading_lines[0], fill=INK, font=F["heading"])

    body_y = y + 105
    for line in wrap(draw, card["content"], F["body"], w - 96, 4):
        draw.text((x + 48, body_y), line, fill=INK, font=F["body"])
        body_y += 34

    draw_visual_strip(draw, x + 48, y + h - 72, w - 96, index)


def draw_footer(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle((80, 1852, 1000, 1858), radius=3, fill=SUBTLE)


def render_page(
    image_text: dict[str, Any],
    cards: list[dict[str, str]],
    page_no: int,
    total_pages: int,
    start_index: int,
) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw_header(draw, image_text, page_no, total_pages)

    y = 318
    for index, card in enumerate(cards):
        h = card_height(card["content"])
        draw_card(draw, 54, y, 972, h, card, start_index + index)
        y += h + 24

    draw_footer(draw)
    return img


def main() -> int:
    image_text = load_image_text()
    cards = build_cards(image_text)
    pages = paginate(cards)
    stamp = now_tokyo().strftime("%Y%m%d_%H%M")

    clear_old_images()
    output_paths: list[Path] = []
    start_index = 0
    for i, page_cards in enumerate(pages, start=1):
        image = render_page(image_text, page_cards, i, len(pages), start_index)
        path = OUT / f"market_pack_{stamp}_{i:02d}.png"
        image.save(path, quality=95)
        output_paths.append(path)
        start_index += len(page_cards)

    for path in output_paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
