from pathlib import Path
import json
from math import cos, sin, radians
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = Path("/Users/ara/Documents/新闻搜索/outputs/20260708_market_pack_calm")
OUT.mkdir(parents=True, exist_ok=True)
GITHUB_PROJECTS_JSON = Path("/Users/ara/Documents/新闻搜索/outputs/github_ai_projects/ai_open_source_projects.json")
MARKET_CONTENT_JSON = Path("/Users/ara/Documents/新闻搜索/outputs/market_content/market_content.json")

W, H = 1080, 1540
BG = "#f3f0ea"
PANEL = "#fffdf8"
INK = "#17212f"
MUTED = "#687385"
NAVY = "#111f35"
BLUE = "#2f68b7"
GREEN = "#1c8f5a"
RED = "#c7443e"
AMBER = "#d98c1f"
LINE = "#ddd6ca"
SOFT = "#ebe6dc"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def font(size, bold=False):
    preferred = "/System/Library/Fonts/STHeiti Medium.ttc" if bold else FONT_CANDIDATES[0]
    for path in [preferred] + FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


F = {
    "display": font(50, True),
    "title": font(31, True),
    "h": font(23, True),
    "body": font(19),
    "body_b": font(19, True),
    "small": font(15),
    "tiny": font(12),
    "num": font(28, True),
}


def text(d, xy, s, fill=INK, f=None, anchor=None):
    d.text(xy, s, fill=fill, font=f or F["body"], anchor=anchor)


def measure(d, s, f):
    return d.textbbox((0, 0), s, font=f)


def wrap(d, s, f, max_w, max_lines=2):
    lines, cur = [], ""
    for ch in s:
        t = cur + ch
        if measure(d, t, f)[2] <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and "".join(lines) != s:
        last = lines[-1]
        while measure(d, last + "...", f)[2] > max_w and last:
            last = last[:-1]
        lines[-1] = last + "..."
    return lines


def shadow_panel(img, x, y, w, h, r=24, fill=PANEL):
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((x + 2, y + 4, x + w + 2, y + h + 4), radius=r, fill=(36, 38, 45, 22))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    img.alpha_composite(shadow)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=fill, outline=LINE, width=1)
    return d


def section_title(d, x, y, title, kicker=None):
    d.rounded_rectangle((x, y, x + 8, y + 30), radius=4, fill=NAVY)
    text(d, (x + 18, y - 1), title, NAVY, F["h"])
    if kicker:
        text(d, (x + 18, y + 27), kicker, MUTED, F["tiny"])


def chip(d, x, y, s, color=NAVY, fg="white"):
    w = measure(d, s, F["small"])[2] + 24
    d.rounded_rectangle((x, y, x + w, y + 30), radius=15, fill=color)
    text(d, (x + w / 2, y + 5), s, fg, F["small"], "ma")
    return w


def top_header(img, page, label, market_content=None):
    d = ImageDraw.Draw(img)
    date_text = "2026-07-08 周三｜数据截至 7/7 美股收盘"
    if market_content:
        date_text = f"{market_content.get('date', '')}｜{market_content.get('timezone', '')}｜{market_content.get('market_session', '')}"
    d.rounded_rectangle((28, 26, 1052, 144), radius=28, fill=NAVY)
    text(d, (58, 50), "每日市场内容包", "white", F["display"])
    text(d, (60, 110), date_text, "#cbd5e1", F["body"])
    chip(d, 785, 54, label, "#f2efe8", NAVY)
    text(d, (1022, 110), page, "#cbd5e1", F["small"], "ra")


def metric_row(d, x, y, name, value, pct, color, strength):
    text(d, (x, y), name, MUTED, F["small"])
    text(d, (x + 142, y - 5), value, INK, F["body_b"])
    text(d, (x + 292, y - 5), pct, color, F["body_b"], "ra")
    d.rounded_rectangle((x, y + 28, x + 292, y + 42), radius=7, fill=SOFT)
    if color == RED:
        w = int(292 * strength)
        d.rounded_rectangle((x + 292 - w, y + 28, x + 292, y + 42), radius=7, fill=color)
    else:
        d.rounded_rectangle((x, y + 28, x + int(292 * strength), y + 42), radius=7, fill=color)


def gauge(d, cx, cy, r, value, label):
    for a0, a1, c in [(180, 235, RED), (235, 300, AMBER), (300, 360, GREEN)]:
        d.arc((cx - r, cy - r, cx + r, cy + r), a0, a1, fill=c, width=24)
    a = radians(180 + 180 * value)
    d.line((cx, cy, cx + cos(a) * (r - 18), cy + sin(a) * (r - 18)), fill=NAVY, width=7)
    d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=NAVY)
    text(d, (cx, cy + 28), label, AMBER, F["body_b"], "ma")


def load_github_projects():
    fallback = [
        {
            "full_name": "bolivestilo/Homekit",
            "stargazers_count": 45,
            "description": "AI agent 控制 Apple Home 设备。",
            "html_url": "https://github.com/bolivestilo/Homekit",
        },
        {
            "full_name": "PeterPanSwift/fox-ai-roundtable",
            "stargazers_count": 36,
            "description": "同一提示并行问 Claude/Codex/Gemini。",
            "html_url": "https://github.com/PeterPanSwift/fox-ai-roundtable",
        },
        {
            "full_name": "nqzai/kakunin-core",
            "stargazers_count": 31,
            "description": "AI agent 身份、风险评分与自动吊销。",
            "html_url": "https://github.com/nqzai/kakunin-core",
        },
    ]
    if not GITHUB_PROJECTS_JSON.exists():
        return fallback
    try:
        data = json.loads(GITHUB_PROJECTS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback
    selected = data.get("selected") or []
    return selected[:3] or fallback


def load_market_content():
    if not MARKET_CONTENT_JSON.exists():
        raise FileNotFoundError(f"market content JSON not found: {MARKET_CONTENT_JSON}")
    data = json.loads(MARKET_CONTENT_JSON.read_text(encoding="utf-8"))
    required = ["date", "timezone", "summary", "key_points", "image_text", "douyin"]
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"market content JSON missing required fields: {', '.join(missing)}")
    return data


def safe_list(data, key, default=None):
    value = data.get(key)
    if isinstance(value, list) and value:
        return value
    return default or []


def pct_color(change):
    text_value = str(change)
    if text_value.strip().startswith("-"):
        return RED
    if text_value and text_value != "0":
        return GREEN
    return MUTED


def dashboard():
    market_content = load_market_content()
    image_text = market_content.get("image_text") or {}
    sections = safe_list(image_text, "sections")
    key_points = safe_list(market_content, "key_points")
    major_indexes = safe_list(market_content, "major_indexes")
    macro_events = safe_list(market_content, "macro_events")
    risk_factors = safe_list(market_content, "risk_factors")

    img = Image.new("RGBA", (W, H), BG)
    top_header(img, "01 / 市场总览", "MARKET PULSE", market_content)
    d = shadow_panel(img, 28, 174, 500, 390)
    section_title(d, 58, 208, image_text.get("title", "核心结论"), image_text.get("subtitle", ""))
    headline = market_content.get("summary", "")
    for i, line in enumerate(wrap(d, headline, F["title"], 430, 3)):
        text(d, (58, 282 + i * 42), line, INK, F["title"])
    if key_points:
        chip(d, 58, 420, key_points[0][:12], AMBER)
    if len(key_points) > 1:
        chip(d, 190, 420, key_points[1][:12], BLUE)
    if risk_factors:
        chip(d, 342, 420, risk_factors[0][:12], RED)
    focus = "；".join(key_points[:2] + risk_factors[:1])
    for i, line in enumerate(wrap(d, f"重点跟踪：{focus}", F["body"], 420, 2)):
        text(d, (58, 492 + i * 28), line, MUTED, F["body"])

    d = shadow_panel(img, 552, 174, 500, 390)
    section_title(d, 582, 208, "指数与资产", "隔夜主线：科技承压，能源相对强")
    rows = major_indexes[:5]
    for i, row in enumerate(rows):
        change = row.get("change_percent", "")
        metric_row(
            d,
            590,
            276 + i * 55,
            row.get("name") or row.get("ticker") or "Index",
            row.get("ticker") or "",
            change,
            pct_color(change),
            min(0.95, 0.25 + i * 0.14),
        )

    d = shadow_panel(img, 28, 594, 315, 385)
    section_title(d, 58, 628, "板块强弱")
    bars = [("半导体/存储", .95, RED, "弱"), ("AI硬件链", .70, RED, "承压"), ("金融/医疗", .55, GREEN, "强"), ("能源", .50, GREEN, "油价驱动")]
    for i, (name, val, c, lab) in enumerate(bars):
        y = 700 + i * 70
        text(d, (58, y), name, INK, F["body_b"])
        text(d, (310, y), lab, c, F["small"], "ra")
        d.rounded_rectangle((58, y + 32, 310, y + 47), radius=7, fill=SOFT)
        if c == RED:
            w = int(252 * val)
            d.rounded_rectangle((310 - w, y + 32, 310, y + 47), radius=7, fill=c)
        else:
            d.rounded_rectangle((58, y + 32, 58 + int(252 * val), y + 47), radius=7, fill=c)

    d = shadow_panel(img, 367, 594, 315, 385)
    section_title(d, 397, 628, "Top 3 催化剂")
    catalyst_colors = [RED, AMBER, BLUE]
    for i, point in enumerate(key_points[:3]):
        num = f"{i + 1:02d}"
        c = catalyst_colors[i % len(catalyst_colors)]
        title = point[:16]
        note = point[16:34]
        y = 700 + i * 82
        d.ellipse((397, y - 3, 437, y + 37), fill=c)
        text(d, (417, y + 6), num, "white", F["small"], "ma")
        text(d, (452, y - 2), title, INK, F["body_b"])
        text(d, (452, y + 25), note, MUTED, F["small"])

    d = shadow_panel(img, 706, 594, 346, 385)
    section_title(d, 736, 628, "市场情绪")
    gauge(d, 880, 780, 86, .42, "短线谨慎")
    mood = risk_factors[0] if risk_factors else market_content.get("summary", "")
    for i, line in enumerate(wrap(d, mood, F["body"], 270, 3)):
        text(d, (746, 884 + i * 28), line, INK, F["body"])

    d = shadow_panel(img, 28, 1010, 500, 390)
    section_title(d, 58, 1044, "宏观与央行日历")
    events = macro_events[:5]
    for i, event in enumerate(events):
        x = 58 + (i % 2) * 214
        y = 1118 + (i // 2) * 72
        importance = event.get("importance", "low")
        stars = {"high": "★★★", "medium": "★★", "low": "★"}.get(importance, "★")
        d.rounded_rectangle((x, y, x + 180, y + 52), radius=14, fill="#f6f2ea", outline=LINE)
        text(d, (x + 14, y + 12), event.get("date", "")[5:] or "--", BLUE, F["body_b"])
        text(d, (x + 76, y + 9), event.get("event", "")[:9], INK, F["small"])
        text(d, (x + 76, y + 29), stars, AMBER, F["tiny"])

    d = shadow_panel(img, 552, 1010, 500, 390)
    section_title(d, 582, 1044, "来源索引")
    sources = ["Yahoo Finance / AP", "TradingKey / Reuters线索", "Federal Reserve", "CentralBank.watch", "X: @aleabitoreddit", "GitHub Search API"]
    for i, src in enumerate(sources):
        x = 582 + (i % 2) * 220
        y = 1120 + (i // 2) * 58
        d.rounded_rectangle((x, y, x + 190, y + 36), radius=18, fill="#f6f2ea")
        text(d, (x + 14, y + 8), src, INK, F["small"])
    text(d, (582, 1328), "提示：不构成投资建议；行情与新闻以权威来源为准。", MUTED, F["small"])
    img.convert("RGB").save(OUT / "01_market_dashboard_calm.png", quality=95)


def serenity():
    market_content = load_market_content()
    important_stocks = safe_list(market_content, "important_stocks")
    risk_factors = safe_list(market_content, "risk_factors")
    img = Image.new("RGBA", (W, H), BG)
    top_header(img, "02 / Serenity专页", "SERENITY WATCH", market_content)
    d = shadow_panel(img, 28, 174, 1024, 420)
    section_title(d, 58, 208, "4格观点卡", "@aleabitoreddit 过去24小时重点")
    cards = [
        ("Meta算力", "大型数据中心与CRVW/Google/ORCL交易仍推进。", BLUE),
        ("机器人", "中国人形机器人产量预期抬升，美国链条承压追赶。", AMBER),
        ("芯片回撤", "多只硬件股同跌，更像主题平仓与拥挤交易出清。", RED),
        ("LiDAR审查", "Hesai/NVDA合作遇监管关注，西方链条或受益。", GREEN),
    ]
    for i, (title, body, c) in enumerate(cards):
        x = 58 + (i % 2) * 492
        y = 284 + (i // 2) * 126
        d.rounded_rectangle((x, y, x + 450, y + 92), radius=20, fill="#f7f3eb", outline=LINE)
        d.rounded_rectangle((x, y, x + 8, y + 92), radius=4, fill=c)
        text(d, (x + 26, y + 16), title, c, F["body_b"])
        text(d, (x + 26, y + 48), body, INK, F["small"])

    d = shadow_panel(img, 28, 624, 500, 350)
    section_title(d, 58, 658, "科技链条", "从算力到订单兑现")
    nodes = [("Meta", "Capex"), ("芯片", "去杠杆"), ("光通信", "LITE/SIVE"), ("机器人", "TSLA")]
    for i, (a, b) in enumerate(nodes):
        x = 58 + i * 108
        y = 748
        d.rounded_rectangle((x, y, x + 88, y + 78), radius=16, fill="#edf2f7", outline="#cdd8e6")
        text(d, (x + 44, y + 18), a, BLUE, F["body_b"], "ma")
        text(d, (x + 44, y + 48), b, MUTED, F["tiny"], "ma")
        if i < len(nodes) - 1:
            d.line((x + 91, y + 39, x + 105, y + 39), fill=AMBER, width=4)
    text(d, (58, 880), "含义：短线先杀拥挤度，后续看真实订单与财报兑现。", INK, F["body"])

    d = shadow_panel(img, 552, 624, 500, 350)
    section_title(d, 582, 658, "提及标的热度")
    heat = []
    for idx, stock in enumerate(important_stocks[:8]):
        ticker = stock.get("ticker") or stock.get("name") or "N/A"
        color = pct_color(stock.get("change_percent", ""))
        heat.append((ticker, max(0.35, 0.82 - idx * 0.06), color))
    if not heat:
        heat = [("$AMD", .82, RED), ("$MU", .75, RED), ("$LITE", .68, GREEN), ("$SIVE", .62, GREEN)]
    for i, (name, val, c) in enumerate(heat):
        x = 582 + (i % 2) * 210
        y = 724 + (i // 2) * 54
        text(d, (x, y), name, INK, F["body_b"])
        d.rounded_rectangle((x + 78, y + 8, x + 178, y + 23), radius=7, fill=SOFT)
        d.rounded_rectangle((x + 78, y + 8, x + 78 + int(100 * val), y + 23), radius=7, fill=c)

    d = shadow_panel(img, 28, 1004, 500, 380)
    section_title(d, 58, 1038, "验证清单")
    checks = (risk_factors[:3] or ["财报Capex", "HBM/光模块订单", "强平是否延续"])
    for i, item in enumerate(checks):
        y = 1110 + i * 58
        d.rectangle((58, y, 80, y + 22), outline=BLUE, width=2)
        for line in wrap(d, item, F["body_b"], 360, 1):
            text(d, (98, y - 5), line, INK, F["body_b"])
    chip(d, 58, 1302, f"观点：{market_content.get('summary', '')[:18]}", RED)

    d = shadow_panel(img, 552, 1004, 500, 380)
    section_title(d, 582, 1038, "原帖索引")
    ids = ["2074568161299771394  Meta算力", "2074548850996707412  人形机器人", "2074494514061017508  芯片链回撤", "2074581698604593367  LiDAR审查"]
    for i, item in enumerate(ids):
        y = 1108 + i * 58
        d.rounded_rectangle((582, y, 1006, y + 38), radius=19, fill="#f6f2ea")
        text(d, (604, y + 8), item, INK, F["small"])
    text(d, (582, 1332), "链接格式：https://x.com/aleabitoreddit/status/{id}", MUTED, F["tiny"])
    img.convert("RGB").save(OUT / "02_serenity_calm.png", quality=95)


def github_page():
    market_content = load_market_content()
    risk_factors = safe_list(market_content, "risk_factors")
    macro_events = safe_list(market_content, "macro_events")
    img = Image.new("RGBA", (W, H), BG)
    top_header(img, "03 / 项目与验证", "AI PROJECT RADAR", market_content)
    d = shadow_panel(img, 28, 174, 1024, 460)
    section_title(d, 58, 208, "AI开源项目", "GitHub REST API：AI Agent / LLM / MCP / RAG / workflow automation")
    projects = load_github_projects()
    for i, repo in enumerate(projects):
        x = 58 + (i % 2) * 492
        y = 286 + (i // 2) * 130
        name = repo.get("full_name") or repo.get("name") or "unknown"
        star = f"{repo.get('stargazers_count', 0)}★"
        desc = repo.get("description") or ""
        d.rounded_rectangle((x, y, x + 450, y + 92), radius=20, fill="#f7f3eb", outline=LINE)
        for j, line in enumerate(wrap(d, name, F["body_b"], 310, 1)):
            text(d, (x + 22, y + 18 + j * 24), line, BLUE, F["body_b"])
        chip(d, x + 360, y + 16, star, AMBER)
        for j, line in enumerate(wrap(d, desc, F["small"], 400, 2)):
            text(d, (x + 22, y + 55 + j * 20), line, INK, F["small"])

    d = shadow_panel(img, 28, 664, 500, 330)
    section_title(d, 58, 698, "本周观察清单")
    items = macro_events[:3] or [
        {"event": "Fed纪要", "impact": "通胀和利率路径措辞"},
        {"event": "芯片财报", "impact": "SK海力士交易与后续指引"},
        {"event": "油价", "impact": "中东航运/制裁扰动"},
    ]
    for i, item in enumerate(items):
        y = 770 + i * 70
        d.ellipse((58, y + 4, 70, y + 16), fill=[BLUE, AMBER, RED][i])
        text(d, (88, y - 3), item.get("event", "")[:12], INK, F["body_b"])
        text(d, (88, y + 25), item.get("impact", "")[:18], MUTED, F["small"])

    d = shadow_panel(img, 552, 664, 500, 330)
    section_title(d, 582, 698, "验证清单")
    checks = risk_factors[:4] or ["芯片跌幅是否扩散到软件/云", "SMH/SOXX成交是否继续放大", "10Y美债是否守住4.5%附近", "Serenity链条是否有公告验证"]
    for i, c in enumerate(checks):
        y = 760 + i * 50
        d.rectangle((582, y, 602, y + 20), outline=BLUE, width=2)
        text(d, (620, y - 4), c, INK, F["small"])

    d = shadow_panel(img, 28, 1024, 1024, 360)
    section_title(d, 58, 1058, "来源与项目链接")
    left = [(repo.get("full_name") or repo.get("name") or "unknown") for repo in projects]
    right = ["Yahoo Finance / AP", "TradingKey / Reuters线索", "Federal Reserve", "CentralBank.watch"]
    text(d, (58, 1124), "GitHub", BLUE, F["body_b"])
    text(d, (560, 1124), "市场来源", RED, F["body_b"])
    for i, item in enumerate(left):
        y = 1170 + i * 42
        d.rounded_rectangle((58, y, 480, y + 30), radius=15, fill="#f6f2ea")
        for line in wrap(d, item, F["small"], 380, 1):
            text(d, (76, y + 6), line, INK, F["small"])
    for i, item in enumerate(right):
        y = 1170 + i * 42
        d.rounded_rectangle((560, y, 990, y + 30), radius=15, fill="#f6f2ea")
        text(d, (578, y + 6), item, INK, F["small"])
    text(d, (58, 1350), "完整链接与平台发布文字见线程正文。", MUTED, F["small"])
    img.convert("RGB").save(OUT / "03_ai_projects_calm.png", quality=95)


if __name__ == "__main__":
    dashboard()
    serenity()
    github_page()
    print(OUT)
