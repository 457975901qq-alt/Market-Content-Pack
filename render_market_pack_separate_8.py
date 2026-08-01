#!/usr/bin/env python3
"""Render the daily market pack directly into 8 independent social images.

This renderer does not create a combined contact sheet. Each page is drawn and
saved independently as a 1080x1440 PNG so the publishing flow can upload pages
one by one.

All visible text and numbers must come from market_content.json or from the
optional GitHub projects JSON. When a field is missing, the page shows a
conservative placeholder instead of inventing data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
CONTENT_PATH = ROOT / "outputs" / "market_content" / "market_content.json"
GITHUB_PROJECTS_PATH = ROOT / "outputs" / "github_ai_projects" / "ai_open_source_projects.json"
OUTPUT_DIR = ROOT / "outputs" / "market_image_pack"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

WIDTH = 1080
HEIGHT = 1440
SAFE_X = 64
SAFE_Y = 54
HEADER_H = 228
CARD_GAP = 22
CARD_RADIUS = 24

BG = "#F7F2E8"
CARD = "#FFFDF9"
INK = "#111111"
MUTED = "#666666"
LINE = "#E7E1D8"
ORANGE = "#F36A13"
GREEN = "#2E8B45"
RED = "#D63C32"
BLUE = "#2F68B7"
WARNING = "#D28B1E"
NEUTRAL = "#7A7A7A"
NAVY = "#18263E"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
BOLD_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


def first_existing(candidates: list[str]) -> str | None:
    for path in candidates:
        if Path(path).exists():
            return path
    return None


REGULAR_FONT = first_existing(FONT_CANDIDATES)
BOLD_FONT = first_existing(BOLD_FONT_CANDIDATES)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = BOLD_FONT if bold else REGULAR_FONT
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


F = {
    "brand": font(27, True),
    "page": font(28, True),
    "title": font(56, True),
    "subtitle": font(28, True),
    "card_title": font(27, True),
    "body": font(22),
    "body_b": font(22, True),
    "small": font(18),
    "tiny": font(15),
    "metric": font(40, True),
    "hero": font(84, True),
}


PAGE_DEFS = [
    (1, "cover", "封面", "当天最重要的市场主线"),
    (2, "overview", "市场总览", "指数、主线与Top 3催化剂"),
    (3, "macro_central_banks", "宏观与央行", "利率、美元、央行与宏观事件"),
    (4, "commodities_geopolitics", "大宗商品与地缘政治", "能源、黄金、铜与风险传导"),
    (5, "ai_semiconductors", "AI与半导体", "算力、芯片、供应链与板块线索"),
    (6, "big_tech_assets", "大科技与重点资产", "核心股票、指数ETF与影响判断"),
    (7, "calendar_opex_github", "事件日历 / OPEX / GitHub", "时间点、期权结构与开源项目"),
    (8, "flows_summary", "资金流与本日总结", "资金方向、风险点与后续关注"),
]


def load_content() -> dict[str, Any]:
    if not CONTENT_PATH.exists():
        raise FileNotFoundError(f"missing market content JSON: {CONTENT_PATH}")
    data = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    required = ["date", "timezone", "summary", "key_points", "image_text"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"required field missing or empty: {', '.join(missing)}")
    if data.get("timezone") != "Asia/Tokyo":
        raise ValueError("timezone must be Asia/Tokyo")
    return data


def load_github_projects() -> list[dict[str, Any]]:
    if not GITHUB_PROJECTS_PATH.exists():
        return []
    try:
        data = json.loads(GITHUB_PROJECTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    selected = data.get("selected")
    return selected if isinstance(selected, list) else []


def safe_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def text_width(draw: ImageDraw.ImageDraw, value: str, fnt: ImageFont.ImageFont) -> int:
    return int(draw.textbbox((0, 0), value, font=fnt)[2])


def wrap(draw: ImageDraw.ImageDraw, value: str, fnt: ImageFont.ImageFont, width: int, limit: int = 3) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if text_width(draw, candidate, fnt) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = ch
            if len(lines) >= limit:
                break
    if current and len(lines) < limit:
        lines.append(current)
    if len(lines) == limit and "".join(lines) != text:
        while lines[-1] and text_width(draw, lines[-1] + "…", fnt) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    value: str,
    x: int,
    y: int,
    fnt: ImageFont.ImageFont,
    width: int,
    *,
    fill: str = INK,
    limit: int = 3,
    gap: int = 6,
) -> int:
    lines = wrap(draw, value, fnt, width, limit)
    line_height = getattr(fnt, "size", 20) + gap
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill)
        y += line_height
    return y


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    return image, ImageDraw.Draw(image)


def rounded_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], *, fill: str = CARD, outline: str = LINE) -> None:
    draw.rounded_rectangle(box, radius=CARD_RADIUS, fill=fill, outline=outline, width=2)


def draw_header(draw: ImageDraw.ImageDraw, page_no: int, title: str, conclusion: str, data: dict[str, Any]) -> None:
    draw.ellipse((SAFE_X, SAFE_Y + 8, SAFE_X + 24, SAFE_Y + 32), outline=ORANGE, width=6)
    draw.text((SAFE_X + 38, SAFE_Y), "每日市场图片包", font=F["brand"], fill=INK)
    draw.text((WIDTH - SAFE_X, SAFE_Y), f"{page_no:02d}", font=F["page"], fill=INK, anchor="ra")
    draw.text((SAFE_X, 118), title, font=F["title"], fill=INK)
    draw_wrapped(draw, conclusion, SAFE_X, 182, F["subtitle"], WIDTH - SAFE_X * 2 - 120, fill=INK, limit=2)
    draw.line((SAFE_X, 214, SAFE_X + 140, 214), fill=ORANGE, width=8)
    footer = f"{data.get('date', '')}｜{data.get('timezone', '')}｜{data.get('market_session', '')}"
    draw.text((WIDTH - SAFE_X, 214), footer, font=F["tiny"], fill=MUTED, anchor="ra")


def draw_hero_cover(draw: ImageDraw.ImageDraw, title: str, subtitle: str) -> None:
    draw_wrapped(draw, title, SAFE_X, 310, F["hero"], WIDTH - SAFE_X * 2, limit=2, gap=4)
    draw.rectangle((SAFE_X, 535, SAFE_X + 10, 650), fill=ORANGE)
    draw_wrapped(draw, subtitle, SAFE_X + 35, 540, F["subtitle"], WIDTH - SAFE_X * 2 - 35, fill=MUTED, limit=3, gap=8)
    draw.rounded_rectangle((110, 760, 620, 1230), radius=52, fill="#1A1A1A", outline=ORANGE, width=7)
    draw.rounded_rectangle((160, 810, 570, 1180), radius=40, fill="#111111", outline="#FF9A3D", width=5)
    draw.text((365, 980), "AI", font=font(148, True), fill=ORANGE, anchor="mm")
    bars = [180, 260, 340, 430]
    for idx, bar_h in enumerate(bars):
        x = 650 + idx * 72
        draw.rounded_rectangle((x, 1230 - bar_h, x + 44, 1230), radius=10, fill=ORANGE)
    pts = [(530, 980), (635, 880), (720, 960), (810, 820), (920, 690)]
    draw.line(pts, fill=ORANGE, width=15, joint="curve")
    draw.polygon([(920, 690), (875, 718), (918, 744)], fill=ORANGE)


def color_for_change(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("-"):
        return RED
    if raw.startswith("+"):
        return GREEN
    return NEUTRAL


def metric_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, value: str, *, change: str = "", note: str = "") -> None:
    rounded_card(draw, box)
    x1, y1, x2, y2 = box
    draw.text((x1 + 22, y1 + 18), title, font=F["card_title"], fill=INK)
    draw.text((x1 + 22, y1 + 70), value or "待核验", font=F["metric"], fill=INK)
    if change:
        draw.text((x2 - 22, y1 + 88), change, font=F["body_b"], fill=color_for_change(change), anchor="ra")
    if note:
        draw_wrapped(draw, note, x1 + 22, y1 + 136, F["small"], x2 - x1 - 44, fill=MUTED, limit=3)


def list_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, items: list[str], *, accent: str = ORANGE, empty_text: str = "暂无重要变化") -> None:
    rounded_card(draw, box)
    x1, y1, x2, y2 = box
    draw.text((x1 + 22, y1 + 18), title, font=F["card_title"], fill=INK)
    values = items[:]
    if not values:
        values = [empty_text]
    y = y1 + 76
    available_h = y2 - y - 14
    item_count = min(len(values), 6)
    step = max(58, available_h // max(1, item_count))
    for idx, item in enumerate(values[:item_count]):
        cy = y + idx * step
        draw.ellipse((x1 + 22, cy + 8, x1 + 36, cy + 22), fill=accent)
        draw_wrapped(draw, str(item), x1 + 50, cy, F["body"], x2 - x1 - 72, fill=INK, limit=2, gap=4)


def render_cover(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    image_text = data.get("image_text") or {}
    title = str(image_text.get("title") or data.get("summary") or "今日市场")
    subtitle = str(image_text.get("subtitle") or data.get("summary") or "")
    draw_header(draw, 1, "封面", "当天最重要的市场主线", data)
    draw_hero_cover(draw, title, subtitle)
    draw.text((SAFE_X, HEIGHT - 78), f"摘要：{data.get('summary', '')}", font=F["small"], fill=MUTED)
    image.save(destination)


def render_overview(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 2, "市场总览", data.get("summary", "指数、主线与Top 3催化剂"), data)
    indexes = safe_list(data, "major_indexes")
    rounded_card(draw, (SAFE_X, 260, WIDTH - SAFE_X, 740))
    draw.text((SAFE_X + 22, 280), "主要指数表现", font=F["card_title"], fill=INK)
    y = 342
    if indexes:
        for row in indexes[:6]:
            name = str(row.get("name") or row.get("ticker") or "指数")
            ticker = str(row.get("ticker") or "")
            change = str(row.get("change_percent") or "待核验")
            draw.text((SAFE_X + 24, y), name, font=F["body_b"], fill=INK)
            draw.text((SAFE_X + 280, y), ticker, font=F["small"], fill=MUTED)
            draw.text((WIDTH - SAFE_X - 24, y), change, font=F["body_b"], fill=color_for_change(change), anchor="ra")
            draw.line((SAFE_X + 22, y + 46, WIDTH - SAFE_X - 22, y + 46), fill=LINE, width=2)
            y += 66
    else:
        draw.text((SAFE_X + 24, 350), "暂无可靠指数数据", font=F["body"], fill=NEUTRAL)
    metric_card(draw, (SAFE_X, 762, 498, 1024), "市场主线", "Top 3", note="使用已核验 key_points，不自动补造")
    metric_card(draw, (518, 762, WIDTH - SAFE_X, 1024), "风险提示", "必看", note="使用已核验 risk_factors，不自动补造")
    list_card(draw, (SAFE_X, 1046, WIDTH - SAFE_X, HEIGHT - 60), "今日Top 3催化剂", [str(x) for x in safe_list(data, "key_points")[:3]], accent=ORANGE, empty_text="暂无已核验催化剂")
    image.save(destination)


def render_macro(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 3, "宏观与央行", "利率、美元、央行与宏观事件", data)
    macro_events = safe_list(data, "macro_events")
    events = [f"{x.get('date', '')}｜{x.get('event', '')}｜{x.get('importance', '')}" for x in macro_events]
    list_card(draw, (SAFE_X, 260, 498, 760), "宏观事件", events[:6], accent=BLUE, empty_text="暂无宏观事件")
    list_card(draw, (518, 260, WIDTH - SAFE_X, 760), "全球央行", [
        "Fed：仅展示已核验政策与定价",
        "BOJ：仅展示已核验利率与措辞变化",
        "ECB / BOE / BOC：无更新时标记暂无重要变化",
    ], accent=ORANGE)
    metric_card(draw, (SAFE_X, 782, 498, 1038), "美元指数 DXY", "待核验", note="不得与 USD/JPY 混写")
    metric_card(draw, (518, 782, WIDTH - SAFE_X, 1038), "USD/JPY", "待核验", note="汇率口径独立展示")
    list_card(draw, (SAFE_X, 1060, WIDTH - SAFE_X, HEIGHT - 60), "宏观关注点", [str(data.get("summary", ""))] + [str(x) for x in safe_list(data, "risk_factors")[:2]], accent=WARNING)
    image.save(destination)


def render_commodities(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 4, "大宗商品与地缘政治", "能源、黄金、铜与风险传导", data)
    metric_card(draw, (SAFE_X, 260, 498, 485), "WTI 原油", "待核验", note="缺少价格、单位或方向时不展示具体数值")
    metric_card(draw, (518, 260, WIDTH - SAFE_X, 485), "Brent 原油", "待核验", note="与 WTI 分开展示")
    metric_card(draw, (SAFE_X, 507, 498, 732), "现货黄金 XAU/USD", "待核验", note="默认口径：XAU/USD，单位 USD/oz")
    metric_card(draw, (518, 507, WIDTH - SAFE_X, 732), "铜 / 天然气", "待核验", note="未完成双重核验时不展示数值")
    list_card(draw, (SAFE_X, 754, WIDTH - SAFE_X, HEIGHT - 60), "地缘政治与政策风险", [str(x) for x in safe_list(data, "risk_factors")[:6]], accent=WARNING, empty_text="暂无已核验地缘风险")
    image.save(destination)


def render_ai_semis(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 5, "AI与半导体", "算力、芯片、供应链与板块线索", data)
    stocks = safe_list(data, "important_stocks")
    selected = []
    for item in stocks:
        name = str(item.get("ticker") or item.get("name") or "")
        upper = name.upper()
        if any(key in upper for key in ["NVDA", "AMD", "TSM", "MU", "ASML", "SMH", "SOXX"]):
            selected.append(f"{name}｜{item.get('change_percent', '待核验')}｜{item.get('reason', '暂无说明')}")
    list_card(draw, (SAFE_X, 260, 498, 760), "核心线索", [str(x) for x in safe_list(data, "key_points")[:5]], accent=ORANGE)
    list_card(draw, (518, 260, WIDTH - SAFE_X, 760), "板块与个股", selected[:5], accent=GREEN, empty_text="暂无已核验半导体个股")
    metric_card(draw, (SAFE_X, 782, 498, 1038), "半导体 ETF / 指数", "待核验", note="仅在输入 JSON 提供时展示")
    metric_card(draw, (518, 782, WIDTH - SAFE_X, 1038), "供应链状态", "持续跟踪", note="没有已核验数据时只展示定性说明")
    list_card(draw, (SAFE_X, 1060, WIDTH - SAFE_X, HEIGHT - 60), "风险与验证条件", [str(x) for x in safe_list(data, "risk_factors")[:4]], accent=WARNING)
    image.save(destination)


def render_big_tech_assets(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 6, "大科技与重点资产", "核心股票、指数ETF与影响判断", data)
    stocks = safe_list(data, "important_stocks")
    boxes = []
    start_y = 260
    card_w = 290
    card_h = 310
    for row in range(2):
        for col in range(3):
            x1 = SAFE_X + col * (card_w + CARD_GAP)
            y1 = start_y + row * (card_h + CARD_GAP)
            boxes.append((x1, y1, x1 + card_w, y1 + card_h))
    for idx, box in enumerate(boxes):
        item = stocks[idx] if idx < len(stocks) else {}
        title = str(item.get("ticker") or item.get("name") or "暂无标的")
        change = str(item.get("change_percent") or "待核验")
        reason = str(item.get("reason") or "暂无已核验说明")
        metric_card(draw, box, title, change, note=reason)
    impacts = []
    for row in safe_list(data, "major_indexes")[:5]:
        impacts.append(f"{row.get('name') or row.get('ticker')}｜{row.get('change_percent', '待核验')}")
    list_card(draw, (SAFE_X, 924, WIDTH - SAFE_X, HEIGHT - 60), "重点资产影响", impacts, accent=BLUE, empty_text="暂无已核验重点资产数据")
    image.save(destination)


def render_calendar_opex_github(data: dict[str, Any], github_projects: list[dict[str, Any]], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 7, "事件日历 / OPEX / GitHub", "时间点、期权结构与开源项目", data)
    macro_events = [f"{x.get('date', '')}｜{x.get('event', '')}｜{x.get('importance', '')}" for x in safe_list(data, "macro_events")]
    list_card(draw, (SAFE_X, 260, 498, 720), "事件日历", macro_events[:6], accent=BLUE, empty_text="暂无重要事件")
    list_card(draw, (518, 260, WIDTH - SAFE_X, 720), "OPEX 与衍生品", [
        "仅在输入提供日期时展示具体到期日",
        "缺少 Gamma 数据时不展示具体点位",
        "用结构说明代替未核验数值",
    ], accent=WARNING)
    project_lines = []
    for repo in github_projects[:4]:
        name = str(repo.get("full_name") or repo.get("name") or "unknown")
        desc = str(repo.get("description") or "暂无说明")
        project_lines.append(f"{name}｜{desc}")
    list_card(draw, (SAFE_X, 742, WIDTH - SAFE_X, HEIGHT - 60), "GitHub 热门 AI 项目", project_lines, accent=ORANGE, empty_text="暂无 GitHub 项目数据")
    image.save(destination)


def render_flows_summary(data: dict[str, Any], destination: Path) -> None:
    image, draw = canvas()
    draw_header(draw, 8, "资金流与本日总结", "资金方向、风险点与后续关注", data)
    list_card(draw, (SAFE_X, 260, WIDTH - SAFE_X, 620), "ETF 资金流", [
        "只有在输入 JSON 提供经核验资金流时才展示具体净额",
        "价格涨跌不能替代申购赎回",
        "缺失数据时保留“待核验/暂无可靠数据”",
    ], accent=GREEN)
    list_card(draw, (SAFE_X, 642, 498, 1038), "本日总结", [str(data.get("summary", ""))] + [str(x) for x in safe_list(data, "key_points")[:2]], accent=ORANGE)
    list_card(draw, (518, 642, WIDTH - SAFE_X, 1038), "主要风险", [str(x) for x in safe_list(data, "risk_factors")[:4]], accent=RED, empty_text="暂无已核验风险")
    list_card(draw, (SAFE_X, 1060, WIDTH - SAFE_X, HEIGHT - 60), "下一步关注", [f"{x.get('date', '')}｜{x.get('event', '')}" for x in safe_list(data, "macro_events")[:5]], accent=BLUE, empty_text="暂无后续关注事件")
    image.save(destination)


def main() -> int:
    data = load_content()
    github_projects = load_github_projects()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    paths = [OUTPUT_DIR / f"{page_no:02d}_{slug}.png" for page_no, slug, _, _ in PAGE_DEFS]
    render_cover(data, paths[0])
    render_overview(data, paths[1])
    render_macro(data, paths[2])
    render_commodities(data, paths[3])
    render_ai_semis(data, paths[4])
    render_big_tech_assets(data, paths[5])
    render_calendar_opex_github(data, github_projects, paths[6])
    render_flows_summary(data, paths[7])

    manifest = {
        "date": data.get("date"),
        "timezone": data.get("timezone"),
        "market_session": data.get("market_session"),
        "width": WIDTH,
        "height": HEIGHT,
        "page_count": 8,
        "pages": [str(path.relative_to(ROOT)) for path in paths],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
