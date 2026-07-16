#!/usr/bin/env python3
"""Render a unified 9-page daily market image pack.

All text and numbers are taken from outputs/market_content/market_content.json.
Missing data is shown conservatively instead of being invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from market_pack_design import (
    BOLD_FONT,
    CARD_GAP,
    CARD_RADIUS,
    COLORS,
    HEADER_HEIGHT,
    HEIGHT,
    REGULAR_FONT,
    SAFE_BOTTOM,
    SAFE_TOP,
    SAFE_X,
    WIDTH,
)

ROOT = Path(__file__).resolve().parent
CONTENT_PATH = ROOT / "outputs" / "market_content" / "market_content.json"
OUTPUT_ROOT = ROOT / "outputs" / "market_image_pack"
MANIFEST_PATH = OUTPUT_ROOT / "manifest.json"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = BOLD_FONT if bold else REGULAR_FONT
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


F = {
    "brand": font(29, True),
    "page": font(34, True),
    "title": font(60, True),
    "conclusion": font(29, True),
    "card_title": font(29, True),
    "body": font(23),
    "body_b": font(23, True),
    "small": font(19),
    "tiny": font(16),
    "metric": font(43, True),
    "hero": font(88, True),
}


def load_content() -> dict[str, Any]:
    if not CONTENT_PATH.exists():
        raise FileNotFoundError(f"missing market content: {CONTENT_PATH}")
    data = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    for key in ["date", "timezone", "market_session", "summary", "key_points", "image_text"]:
        if not data.get(key):
            raise ValueError(f"required field missing or empty: {key}")
    if data["timezone"] != "Asia/Tokyo":
        raise ValueError("timezone must be Asia/Tokyo")
    return data


def text_width(draw: ImageDraw.ImageDraw, value: str, fnt: ImageFont.ImageFont) -> int:
    return int(draw.textbbox((0, 0), value, font=fnt)[2])


def wrap(draw: ImageDraw.ImageDraw, value: str, fnt: ImageFont.ImageFont, width: int, limit: int = 3) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in value.strip():
        if text_width(draw, current + char, fnt) <= width:
            current += char
        else:
            if current:
                lines.append(current)
            current = char
            if len(lines) >= limit:
                break
    if current and len(lines) < limit:
        lines.append(current)
    if len(lines) == limit and "".join(lines) != value.strip():
        while lines[-1] and text_width(draw, lines[-1] + "…", fnt) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def draw_wrapped(draw: ImageDraw.ImageDraw, value: str, x: int, y: int, fnt: ImageFont.ImageFont,
                 width: int, fill: str | None = None, limit: int = 3, gap: int = 8) -> int:
    fill = fill or COLORS["ink"]
    for line in wrap(draw, value, fnt, width, limit):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += getattr(fnt, "size", 20) + gap
    return y


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["background"])
    return image, ImageDraw.Draw(image)


def rounded_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str | None = None,
                 outline: str | None = None, radius: int = CARD_RADIUS, width: int = 2) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill or COLORS["card"],
                           outline=outline or COLORS["line"], width=width)


def header(draw: ImageDraw.ImageDraw, number: int, title: str, conclusion: str) -> None:
    draw.ellipse((SAFE_X, SAFE_TOP + 8, SAFE_X + 24, SAFE_TOP + 32), outline=COLORS["orange"], width=6)
    draw.text((SAFE_X + 38, SAFE_TOP), "每日市场内容包", font=F["brand"], fill=COLORS["ink"])
    draw.text((WIDTH - SAFE_X, SAFE_TOP - 3), f"{number:02d}", font=F["page"], fill=COLORS["ink"], anchor="ra")
    draw.text((SAFE_X, 132), title, font=F["title"], fill=COLORS["ink"])
    draw_wrapped(draw, conclusion, SAFE_X, 222, F["conclusion"], WIDTH - SAFE_X * 2 - 150,
                 fill=COLORS["ink"], limit=2, gap=5)
    draw.line((SAFE_X, 292, SAFE_X + 135, 292), fill=COLORS["orange"], width=8)


def title_from_content(data: dict[str, Any]) -> tuple[str, str]:
    image_text = data.get("image_text") or {}
    title = str(image_text.get("title") or data.get("summary") or "今日市场")
    subtitle = str(image_text.get("subtitle") or data.get("summary") or "")
    return title, subtitle


def safe_items(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def color_for_change(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("-"):
        return COLORS["red"]
    if raw.startswith("+"):
        return COLORS["green"]
    return COLORS["neutral"]


def line_chart(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: str, seed: int = 1) -> None:
    x1, y1, x2, y2 = box
    points = []
    span = max(1, x2 - x1)
    for index in range(14):
        x = x1 + index * span / 13
        wave = ((index * 7 + seed * 5) % 11) - 5
        trend = (index - 6) * (1 if color == COLORS["green"] else -1)
        y = (y1 + y2) / 2 - wave * 5 - trend * 2
        points.append((x, max(y1 + 5, min(y2 - 5, y))))
    draw.line(points, fill=color, width=4)


def metric_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, value: str,
                change: str = "", note: str = "", chart: bool = False, seed: int = 1) -> None:
    rounded_card(draw, box)
    x1, y1, x2, y2 = box
    draw.text((x1 + 25, y1 + 22), title, font=F["card_title"], fill=COLORS["ink"])
    draw.text((x1 + 25, y1 + 76), value or "暂无可靠数据", font=F["metric"], fill=COLORS["ink"])
    if change:
        draw.text((x2 - 24, y1 + 92), change, font=F["body_b"], fill=color_for_change(change), anchor="ra")
    if note:
        draw_wrapped(draw, note, x1 + 25, y1 + 140, F["small"], x2 - x1 - 50,
                     fill=COLORS["muted"], limit=2)
    if chart:
        line_chart(draw, (x1 + 24, y2 - 105, x2 - 24, y2 - 24), color_for_change(change), seed)


def list_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str,
              items: list[str], accent: str | None = None) -> None:
    rounded_card(draw, box)
    x1, y1, x2, y2 = box
    draw.text((x1 + 25, y1 + 22), title, font=F["card_title"], fill=COLORS["ink"])
    y = y1 + 82
    if not items:
        items = ["暂无重要变化"]
    max_items = max(1, min(len(items), 6))
    item_height = max(68, (y2 - y - 22) // max_items)
    for index, item in enumerate(items[:max_items]):
        cy = y + index * item_height
        draw.ellipse((x1 + 26, cy + 9, x1 + 39, cy + 22), fill=accent or COLORS["orange"])
        draw_wrapped(draw, str(item), x1 + 55, cy, F["body"], x2 - x1 - 82, limit=2, gap=4)


def render_cover(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas()
    draw.rounded_rectangle((22, 22, WIDTH - 22, HEIGHT - 22), radius=32, outline=COLORS["orange"], width=2)
    draw.ellipse((SAFE_X, SAFE_TOP + 8, SAFE_X + 24, SAFE_TOP + 32), outline=COLORS["orange"], width=6)
    draw.text((SAFE_X + 38, SAFE_TOP), "每日市场内容包", font=F["brand"], fill=COLORS["ink"])
    draw.text((WIDTH - SAFE_X, SAFE_TOP - 3), "01", font=F["page"], fill=COLORS["ink"], anchor="ra")
    title, subtitle = title_from_content(data)
    draw_wrapped(draw, title, SAFE_X, 250, F["hero"], WIDTH - SAFE_X * 2, limit=2, gap=4)
    draw.rectangle((SAFE_X, 560, SAFE_X + 9, 685), fill=COLORS["orange"])
    draw_wrapped(draw, subtitle, SAFE_X + 35, 565, F["conclusion"], WIDTH - SAFE_X * 2 - 35,
                 fill=COLORS["muted"], limit=3, gap=8)
    # Programmatic hero visual: chip, bars and arrow. No market numbers are invented.
    draw.rounded_rectangle((120, 1030, 690, 1580), radius=55, fill="#242424", outline=COLORS["orange"], width=8)
    draw.rounded_rectangle((175, 1085, 635, 1525), radius=42, fill="#111111", outline="#FF9A3D", width=5)
    draw.text((405, 1245), "AI", font=font(150, True), fill=COLORS["orange"], anchor="mm")
    for idx, height in enumerate([180, 260, 340, 455]):
        x = 690 + idx * 75
        draw.rounded_rectangle((x, 1580 - height, x + 46, 1580), radius=12, fill=COLORS["orange"])
    arrow_points = [(555, 1290), (650, 1170), (730, 1250), (820, 1075), (930, 820)]
    draw.line(arrow_points, fill=COLORS["orange"], width=16, joint="curve")
    draw.polygon([(930, 820), (882, 850), (928, 875)], fill=COLORS["orange"])
    draw.text((SAFE_X, HEIGHT - 118), f"数据时段：{data.get('market_session', '')}｜{data.get('date', '')}",
              font=F["small"], fill=COLORS["muted"])
    image.save(out)


def render_overview(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 2, "市场总览", data.get("summary", "市场结构一图看懂"))
    indexes = safe_items(data, "major_indexes")
    rounded_card(draw, (SAFE_X, 340, WIDTH - SAFE_X, 1010))
    draw.text((SAFE_X + 26, 365), "主要指数表现", font=F["card_title"], fill=COLORS["ink"])
    y = 435
    for row in indexes[:7]:
        name = str(row.get("name") or row.get("ticker") or "指数")
        ticker = str(row.get("ticker") or "")
        change = str(row.get("change_percent") or "待核验")
        draw.text((SAFE_X + 28, y), name, font=F["body_b"], fill=COLORS["ink"])
        draw.text((SAFE_X + 365, y), ticker, font=F["body"], fill=COLORS["muted"])
        draw.text((WIDTH - SAFE_X - 28, y), change, font=F["body_b"], fill=color_for_change(change), anchor="ra")
        draw.line((SAFE_X + 24, y + 52, WIDTH - SAFE_X - 24, y + 52), fill=COLORS["line"], width=2)
        y += 76
    if not indexes:
        draw.text((SAFE_X + 28, 450), "暂无可靠指数数据", font=F["body"], fill=COLORS["neutral"])
    metric_card(draw, (SAFE_X, 1040, 515, 1375), "市场情绪", "待核验", note="输入JSON未提供情绪指数时不展示具体数值")
    metric_card(draw, (545, 1040, WIDTH - SAFE_X, 1375), "美债收益率", "待核验", note="仅在输入JSON提供收益率和方向时展示")
    list_card(draw, (SAFE_X, 1405, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "今日Top 3市场催化剂",
              [str(x) for x in safe_items(data, "key_points")[:3]])
    image.save(out)


def render_macro(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 3, "宏观数据与全球央行", "通胀、利率、就业与央行定价")
    events = safe_items(data, "macro_events")
    macro = [f"{x.get('date','')}｜{x.get('event','')}｜{x.get('impact','')}" for x in events]
    list_card(draw, (SAFE_X, 340, 515, 1045), "美国宏观数据", macro[:5], COLORS["blue"])
    list_card(draw, (545, 340, WIDTH - SAFE_X, 1045), "全球央行", [
        "Fed：依据已核验讲话与利率定价",
        "ECB：无更新时标记暂无重要变化",
        "BOJ：仅展示已核验政策变化",
        "BOE：仅展示已核验政策变化",
        "BOC：仅展示已核验政策变化",
    ], COLORS["orange"])
    metric_card(draw, (SAFE_X, 1075, 515, 1460), "美元指数 DXY", "待核验", note="不得与USD/JPY混用")
    metric_card(draw, (545, 1075, WIDTH - SAFE_X, 1460), "USD/JPY", "待核验", note="独立汇率展示")
    list_card(draw, (SAFE_X, 1490, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "宏观判断",
              [str(data.get("summary", ""))] + [str(x) for x in safe_items(data, "risk_factors")[:2]])
    image.save(out)


def render_commodities(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 4, "大宗商品与地缘政治", "能源、贵金属、工业金属与风险传导")
    names = ["WTI原油", "布伦特原油", "现货黄金 XAU/USD", "铜", "天然气", "其他大宗商品"]
    positions = [(SAFE_X, 340, 515, 665), (545, 340, WIDTH - SAFE_X, 665),
                 (SAFE_X, 695, 515, 1020), (545, 695, WIDTH - SAFE_X, 1020),
                 (SAFE_X, 1050, 515, 1375), (545, 1050, WIDTH - SAFE_X, 1375)]
    for idx, (name, box) in enumerate(zip(names, positions), 1):
        metric_card(draw, box, name, "待核验", note="输入缺少价格、单位或方向时不展示具体数值", chart=True, seed=idx)
    list_card(draw, (SAFE_X, 1405, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "地缘政治焦点",
              [str(x) for x in safe_items(data, "risk_factors")[:4]], COLORS["warning"])
    image.save(out)


def render_semis(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 5, "AI与半导体", "算力、芯片、供应链与资金方向")
    stocks = safe_items(data, "important_stocks")
    ai_rows = [f"{x.get('ticker') or x.get('name')}｜{x.get('change_percent','')}｜{x.get('reason','')}" for x in stocks
               if any(k in str(x).upper() for k in ["NVDA", "AMD", "ASML", "TSM", "MU", "SMH", "SOXX"])]
    list_card(draw, (SAFE_X, 340, 515, 880), "AI基础设施需求", [str(x) for x in safe_items(data, "key_points")[:4]], COLORS["orange"])
    metric_card(draw, (545, 340, WIDTH - SAFE_X, 880), "半导体板块", "待核验", note="仅使用输入中的指数或ETF数据", chart=True)
    list_card(draw, (SAFE_X, 910, 515, 1450), "产业链重点", ai_rows[:5], COLORS["green"])
    list_card(draw, (545, 910, WIDTH - SAFE_X, 1450), "关键公司动态", ai_rows[5:10] or [str(x) for x in safe_items(data, "key_points")[:3]])
    list_card(draw, (SAFE_X, 1480, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "资金与验证条件",
              [str(x) for x in safe_items(data, "risk_factors")[:4]], COLORS["green"])
    image.save(out)


def render_big_tech(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 6, "大型科技与重点资产", "大科技、指数ETF与重点标的")
    stocks = safe_items(data, "important_stocks")
    boxes = []
    start_y = 340
    card_w = 286
    for row in range(2):
        for col in range(3):
            x1 = SAFE_X + col * (card_w + CARD_GAP)
            y1 = start_y + row * 425
            boxes.append((x1, y1, x1 + card_w, y1 + 395))
    for idx, box in enumerate(boxes):
        item = stocks[idx] if idx < len(stocks) else {}
        title = str(item.get("ticker") or item.get("name") or "暂无标的")
        value = str(item.get("change_percent") or "待核验")
        metric_card(draw, box, title, value, note=str(item.get("reason") or "暂无重要变化"), chart=True, seed=idx + 1)
    list_card(draw, (SAFE_X, 1215, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "重要ETF与资产影响",
              [f"{x.get('name') or x.get('ticker')}｜{x.get('change_percent','')}｜{x.get('reason','')}" for x in safe_items(data, "major_indexes")[:6]])
    image.save(out)


def render_calendar(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 7, "事件日历与OPEX", "宏观数据、财报和衍生品时间点")
    events = [f"{x.get('date','')}｜{x.get('event','')}｜{x.get('impact','')}" for x in safe_items(data, "macro_events")]
    earnings = [f"{x.get('date','')}｜{x.get('ticker') or x.get('company')}｜{x.get('importance','')}" for x in safe_items(data, "earnings")]
    list_card(draw, (SAFE_X, 340, 515, 1120), "今日重要事件", events[:7], COLORS["orange"])
    list_card(draw, (545, 340, WIDTH - SAFE_X, 1120), "财报日历", earnings[:7], COLORS["blue"])
    list_card(draw, (SAFE_X, 1150, 515, HEIGHT - SAFE_BOTTOM), "期权到期与OPEX", [
        "仅在输入提供到期日时展示具体日期",
        "缺少Gamma数据时不展示磁吸位",
        "关注QQQ、SPY、IWM等高流动性标的",
    ], COLORS["warning"])
    list_card(draw, (545, 1150, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "今日最值得关注的3件事",
              [str(x) for x in safe_items(data, "key_points")[:3]], COLORS["orange"])
    image.save(out)


def render_flows(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 8, "ETF资金流与市场结构", "资金方向、流动性与国债事件")
    list_card(draw, (SAFE_X, 340, WIDTH - SAFE_X, 1020), "主要ETF资金流", [
        "输入JSON未提供经核验申购赎回数据时，不展示具体净流量",
        "价格涨跌不能替代ETF份额变化",
        "主题ETF、债券ETF和商品ETF分开呈现",
    ], COLORS["green"])
    metric_card(draw, (SAFE_X, 1050, 515, 1415), "市场结构", "待核验", note="上涨/下跌/持平比例需来自真实数据")
    metric_card(draw, (545, 1050, WIDTH - SAFE_X, 1415), "流动性", "待核验", note="融资余额、美元流动性和结算信息分开核验")
    list_card(draw, (SAFE_X, 1445, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "国债拍卖与结算",
              [f"{x.get('date','')}｜{x.get('event','')}｜{x.get('impact','')}" for x in safe_items(data, "macro_events") if "债" in str(x)][:5])
    image.save(out)


def render_github(data: dict[str, Any], out: Path) -> None:
    image, draw = canvas(); header(draw, 9, "GitHub热门AI项目", "开源项目、本周总结与后续验证")
    projects_path = ROOT / "outputs" / "github_ai_projects" / "ai_open_source_projects.json"
    projects: list[dict[str, Any]] = []
    if projects_path.exists():
        raw = json.loads(projects_path.read_text(encoding="utf-8"))
        projects = raw.get("selected") or []
    rounded_card(draw, (SAFE_X, 340, WIDTH - SAFE_X, 1250))
    draw.rectangle((SAFE_X + 18, 360, WIDTH - SAFE_X - 18, 435), fill="#1E1E1E")
    headings = [(SAFE_X + 45, "项目名称"), (430, "用途"), (735, "热度与意义")]
    for x, label in headings:
        draw.text((x, 382), label, font=F["body_b"], fill="white")
    y = 465
    if not projects:
        projects = [{"full_name": "暂无可靠项目数据", "description": "先运行 github_ai_projects.py", "stargazers_count": 0}]
    for repo in projects[:5]:
        name = str(repo.get("full_name") or repo.get("name") or "unknown")
        desc = str(repo.get("description") or "暂无说明")
        stars = str(repo.get("stargazers_count") or "待核验")
        draw_wrapped(draw, name, SAFE_X + 45, y, F["body_b"], 300, limit=2)
        draw_wrapped(draw, desc, 430, y, F["small"], 280, limit=3)
        draw_wrapped(draw, f"Stars：{stars}\n更新与讨论热度需综合判断", 735, y, F["small"], 250, limit=3)
        draw.line((SAFE_X + 25, y + 125, WIDTH - SAFE_X - 25, y + 125), fill=COLORS["line"], width=2)
        y += 145
    list_card(draw, (SAFE_X, 1280, 515, HEIGHT - SAFE_BOTTOM), "本周滚动总结",
              [str(data.get("summary", ""))] + [str(x) for x in safe_items(data, "key_points")[:2]], COLORS["orange"])
    list_card(draw, (545, 1280, WIDTH - SAFE_X, HEIGHT - SAFE_BOTTOM), "下周重点关注",
              [f"{x.get('date','')}｜{x.get('event','')}" for x in safe_items(data, "macro_events")[:4]], COLORS["blue"])
    image.save(out)


def main() -> int:
    data = load_content()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    outputs = [OUTPUT_ROOT / f"{i:02d}_{name}.png" for i, name in enumerate([
        "cover", "overview", "macro_central_banks", "commodities_geopolitics", "ai_semiconductors",
        "big_tech_assets", "calendar_opex", "etf_flows_structure", "github_weekly"], 1)]
    render_cover(data, outputs[0]); render_overview(data, outputs[1]); render_macro(data, outputs[2])
    render_commodities(data, outputs[3]); render_semis(data, outputs[4]); render_big_tech(data, outputs[5])
    render_calendar(data, outputs[6]); render_flows(data, outputs[7]); render_github(data, outputs[8])
    MANIFEST_PATH.write_text(json.dumps({
        "date": data["date"], "timezone": data["timezone"], "market_session": data["market_session"],
        "width": WIDTH, "height": HEIGHT, "pages": [str(path.relative_to(ROOT)) for path in outputs]
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
