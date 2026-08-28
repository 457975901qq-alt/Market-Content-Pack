from pathlib import Path
from math import cos, sin, radians
from PIL import Image, ImageDraw, ImageFont

OUT = Path("/Users/ara/Documents/新闻搜索/outputs/20260708_market_pack")
OUT.mkdir(parents=True, exist_ok=True)

W, H = 1080, 1540
BG = "#f6f8fb"
CARD = "#ffffff"
INK = "#172033"
MUTED = "#667085"
BLUE = "#2367b2"
NAVY = "#102a55"
GREEN = "#1f9d55"
RED = "#d83b35"
ORANGE = "#f59e0b"
YELLOW = "#facc15"
LINE = "#d8e1ea"
PALE_BLUE = "#e8f1fb"
PALE_RED = "#fff0f0"
PALE_GREEN = "#ecfdf3"

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
    "title": font(46, True),
    "h1": font(30, True),
    "h2": font(23, True),
    "body": font(19),
    "body_b": font(19, True),
    "small": font(16),
    "tiny": font(13),
    "num": font(25, True),
}


def tb(d, s, f):
    return d.textbbox((0, 0), s, font=f)


def draw_text(d, xy, s, fill=INK, f=None, anchor=None):
    d.text(xy, s, fill=fill, font=f or F["body"], anchor=anchor)


def wrap(d, s, f, max_w, max_lines=2):
    out, line = [], ""
    for ch in s:
        trial = line + ch
        if tb(d, trial, f)[2] <= max_w:
            line = trial
        else:
            if line:
                out.append(line)
            line = ch
            if len(out) == max_lines:
                break
    if line and len(out) < max_lines:
        out.append(line)
    if len(out) == max_lines and "".join(out) != s:
        last = out[-1]
        while tb(d, last + "...", f)[2] > max_w and last:
            last = last[:-1]
        out[-1] = last + "..."
    return out


def card(d, x, y, w, h, title, icon=None):
    d.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=CARD, outline=LINE, width=1)
    if icon:
        icon_circle(d, x + 30, y + 31, icon)
        draw_text(d, (x + 58, y + 20), title, NAVY, F["h2"])
    else:
        draw_text(d, (x + 20, y + 20), title, NAVY, F["h2"])


def icon_circle(d, cx, cy, kind):
    d.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=PALE_BLUE, outline="#b7cce5")
    if kind == "chip":
        d.rounded_rectangle((cx - 9, cy - 9, cx + 9, cy + 9), radius=3, outline=BLUE, width=2)
        draw_text(d, (cx, cy - 8), "AI", BLUE, F["tiny"], "ma")
    elif kind == "bank":
        d.polygon([(cx - 12, cy - 2), (cx, cy - 12), (cx + 12, cy - 2)], fill=BLUE)
        for i in range(3):
            d.rectangle((cx - 10 + i * 9, cy, cx - 6 + i * 9, cy + 11), fill=BLUE)
    elif kind == "calendar":
        d.rectangle((cx - 11, cy - 10, cx + 11, cy + 12), outline=BLUE, width=2)
        d.rectangle((cx - 11, cy - 10, cx + 11, cy - 4), fill=BLUE)
    elif kind == "money":
        draw_text(d, (cx, cy - 10), "$", GREEN, F["body_b"], "ma")
    elif kind == "post":
        d.rounded_rectangle((cx - 10, cy - 11, cx + 10, cy + 11), radius=3, outline=BLUE, width=2)
        d.line((cx - 6, cy - 2, cx + 6, cy - 2), fill=BLUE, width=2)
        d.line((cx - 6, cy + 5, cx + 4, cy + 5), fill=BLUE, width=2)
    else:
        d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=BLUE)


def pill(d, x, y, s, fill, fg="white", pad=12):
    w = tb(d, s, F["small"])[2] + pad * 2
    d.rounded_rectangle((x, y, x + w, y + 28), radius=14, fill=fill)
    draw_text(d, (x + w / 2, y + 5), s, fg, F["small"], "ma")
    return w


def bar(d, x, y, w, pct, color, label, value):
    draw_text(d, (x, y - 2), label, INK, F["small"])
    draw_text(d, (x + w, y - 2), value, color, F["small"], "ra")
    d.rounded_rectangle((x, y + 25, x + w, y + 39), radius=7, fill="#e8edf3")
    if pct >= 0:
        d.rounded_rectangle((x, y + 25, x + int(w * min(pct, 1)), y + 39), radius=7, fill=color)
    else:
        fw = int(w * min(abs(pct), 1))
        d.rounded_rectangle((x + w - fw, y + 25, x + w, y + 39), radius=7, fill=color)


def gauge(d, cx, cy, r, value, label):
    for a0, a1, c in [(180, 225, RED), (225, 270, ORANGE), (270, 315, YELLOW), (315, 360, GREEN)]:
        d.arc((cx - r, cy - r, cx + r, cy + r), a0, a1, fill=c, width=22)
    a = radians(180 + value * 180)
    d.line((cx, cy, cx + cos(a) * (r - 14), cy + sin(a) * (r - 14)), fill=INK, width=7)
    d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=INK)
    draw_text(d, (cx, cy + 24), label, ORANGE, F["body_b"], "ma")


def header(d, title, subtitle, page):
    draw_text(d, (34, 30), title, "#06121f", F["title"])
    draw_text(d, (36, 86), subtitle, MUTED, F["body"])
    pill(d, 805, 34, "2026-07-08 周三", NAVY)
    draw_text(d, (1044, 88), page, MUTED, F["small"], "ra")


def bullets(d, x, y, items, width, gap=46):
    yy = y
    for color, head, body in items:
        d.ellipse((x, yy + 7, x + 12, yy + 19), fill=color)
        draw_text(d, (x + 22, yy), head, color, F["body_b"])
        for i, line in enumerate(wrap(d, body, F["small"], width - 22, 2)):
            draw_text(d, (x + 22, yy + 23 + i * 19), line, INK, F["small"])
        yy += gap


def dashboard():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "每日市场内容包", "数据截至：7/7 美股收盘；搜索覆盖过去24-48小时", "01 / 主仪表盘")
    margin, gap = 28, 16
    cw = (W - margin * 2 - gap * 2) // 3
    xs = [margin, margin + cw + gap, margin + (cw + gap) * 2]

    card(d, xs[0], 130, cw, 294, "今日市场总览", "post")
    rows = [
        ("S&P 500", "7503.85", "-0.45%", RED, .45),
        ("Nasdaq", "25818.69", "-1.16%", RED, .78),
        ("Dow", "52925.15", "-0.25%", RED, .25),
        ("SOX", "芯片指数", "-4.65%", RED, .95),
        ("WTI", "约72美元", "+5%区间", GREEN, .66),
    ]
    yy = 188
    for name, val, pct, color, p in rows:
        draw_text(d, (xs[0] + 20, yy), name, INK, F["small"])
        draw_text(d, (xs[0] + 145, yy), val, INK, F["small"])
        draw_text(d, (xs[0] + cw - 18, yy), pct, color, F["small"], "ra")
        d.rounded_rectangle((xs[0] + 20, yy + 24, xs[0] + cw - 20, yy + 34), radius=5, fill="#edf1f5")
        d.rounded_rectangle((xs[0] + cw - 20 - int((cw - 40) * p), yy + 24, xs[0] + cw - 20, yy + 34), radius=5, fill=color)
        yy += 44

    card(d, xs[1], 130, cw, 294, "重点板块表现", "chip")
    bar(d, xs[1] + 22, 190, cw - 44, -0.95, RED, "半导体/存储", "弱")
    bar(d, xs[1] + 22, 250, cw - 44, -0.62, RED, "AI硬件链", "承压")
    bar(d, xs[1] + 22, 310, cw - 44, 0.54, GREEN, "金融/医疗", "相对强")
    bar(d, xs[1] + 22, 370, cw - 44, 0.44, GREEN, "能源", "油价驱动")

    card(d, xs[2], 130, cw, 294, "Top 3 催化剂", "money")
    bullets(d, xs[2] + 22, 186, [
        (RED, "AI芯片卖事实", "Samsung强指引未撑住股价，获利盘转向。"),
        (ORANGE, "油价与通胀", "霍尔木兹/伊朗风险推高油价，利率预期再受扰动。"),
        (BLUE, "Fed纪要", "7/8公布纪要，检验鹰派风险。"),
    ], cw - 40, 64)

    y2 = 448
    card(d, xs[0], y2, cw, 285, "宏观日历", "calendar")
    days = [("7/8", "Fed纪要", "★★★"), ("7/10", "SK海力士美股", "★★★"), ("7/15", "BoC决议", "★★"), ("7/23", "ECB", "★★"), ("7/28", "FOMC", "★★★")]
    for i, (day, event, stars) in enumerate(days):
        x = xs[0] + 22 + (i % 2) * 142
        y = y2 + 72 + (i // 2) * 58
        d.rounded_rectangle((x, y, x + 122, y + 44), radius=9, fill="#f3f7fb", outline=LINE)
        draw_text(d, (x + 10, y + 5), day, BLUE, F["body_b"])
        draw_text(d, (x + 58, y + 6), event, INK, F["tiny"])
        draw_text(d, (x + 58, y + 24), stars, ORANGE, F["tiny"])

    card(d, xs[1], y2, cw, 285, "全球央行", "bank")
    bullets(d, xs[1] + 22, y2 + 72, [
        (BLUE, "Fed", "纪要验证加息/通胀分歧，利率敏感资产先波动。"),
        (BLUE, "ECB/BOE", "7月下旬密集议息，汇率与债券波动升温。"),
        (BLUE, "BOJ", "7/30-31会议，日元与日本收益率仍是外溢变量。"),
    ], cw - 40, 58)

    card(d, xs[2], y2, cw, 285, "大宗/加密", "money")
    gauge(d, xs[2] + 94, y2 + 168, 70, .62, "风险中性偏紧")
    draw_text(d, (xs[2] + 188, y2 + 88), "原油", ORANGE, F["body_b"])
    draw_text(d, (xs[2] + 188, y2 + 118), "供应风险溢价上升", INK, F["small"])
    draw_text(d, (xs[2] + 188, y2 + 164), "黄金/BTC", BLUE, F["body_b"])
    draw_text(d, (xs[2] + 188, y2 + 194), "等待Fed纪要信号", INK, F["small"])

    y3 = 757
    card(d, xs[0], y3, cw, 282, "资金流/情绪", "money")
    draw_text(d, (xs[0] + 22, y3 + 72), "芯片ETF：价格下跌 > 实时流量滞后", RED, F["body_b"])
    draw_text(d, (xs[0] + 22, y3 + 112), "高盛PB线索：半导体连续数周被减仓。", INK, F["small"])
    draw_text(d, (xs[0] + 22, y3 + 136), "但AI总敞口仍拥挤，不能视作退潮。", INK, F["small"])
    gauge(d, xs[0] + cw // 2, y3 + 218, 68, .40, "短线谨慎")

    card(d, xs[1], y3, cw, 282, "国际事件", "post")
    bullets(d, xs[1] + 22, y3 + 76, [
        (ORANGE, "中东能源", "油轮/制裁消息抬升原油风险溢价。"),
        (RED, "中国AI芯片", "DeepSeek自研芯片传闻加剧供应链再定价。"),
        (BLUE, "亚洲科技", "韩国/日本芯片链先跌后分化，资金看财报验证。"),
    ], cw - 40, 58)

    card(d, xs[2], y3, cw, 282, "来源索引", "post")
    sources = ["Yahoo Finance / AP", "TradingKey / Reuters线索", "Federal Reserve", "CentralBank.watch", "X: @aleabitoreddit", "GitHub Search API"]
    yy = y3 + 76
    for s in sources:
        pill(d, xs[2] + 22, yy, s, "#eef4ff", NAVY, 10)
        yy += 34

    card(d, 28, 1066, 1024, 398, "市场结论与风险提示", "post")
    pill(d, 56, 1130, "今日结论", RED)
    summary = "AI硬件不是坏消息，而是“预期太满”的回撤：Samsung强指引、DeepSeek芯片、油价上行与Fed纪要叠加，市场从追涨转向验证。"
    for i, line in enumerate(wrap(d, summary, F["body_b"], 930, 3)):
        draw_text(d, (56, 1176 + i * 30), line, INK, F["body_b"])
    pill(d, 56, 1234, "观察重点", BLUE)
    draw_text(d, (56, 1300), "重点跟踪：半导体链去杠杆、资金转向防御，Serenity关注Meta算力、机器人与LiDAR。", INK, F["body_b"])
    draw_text(d, (56, 1368), "提示：不构成投资建议；所有行情和新闻以交易所、公司公告及权威媒体为准。", MUTED, F["small"])
    img.save(OUT / "01_market_dashboard.png", quality=95)


def serenity():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "Serenity 今日信息简报", "@aleabitoreddit 过去24小时重点；twitter-cli 已接入", "02 / X专页")
    card(d, 28, 128, 1024, 482, "4格观点卡", "post")
    views = [
        ("Meta算力", "Meta仍推进大型数据中心与CRVW/Google/ORCL等算力交易；他认为市场被“capex叙事”放大。", "319k views", BLUE),
        ("机器人", "中国人形机器人年产预期10万+，对美国机器人链形成“Sputnik moment”式压力。", "252k views", ORANGE),
        ("芯片回撤", "NBIS/MRVL/INTC/AMD/MU/LITE等同跌，更像保证金/主题平仓而非单家公司基本面。", "973k views", RED),
        ("激光雷达", "Hesai与NVDA合作面临美国审查；西方LiDAR与上游激光供应商可能受益。", "198k views", GREEN),
    ]
    for i, (h, b, m, c) in enumerate(views):
        x = 56 + (i % 2) * 492
        y = 200 + (i // 2) * 176
        d.rounded_rectangle((x, y, x + 450, y + 140), radius=12, fill=PALE_GREEN if c == GREEN else PALE_RED if c == RED else "#f8fafc", outline=LINE)
        pill(d, x + 18, y + 16, h, c)
        draw_text(d, (x + 18, y + 58), wrap(d, b, F["body"], 408, 2)[0], INK, F["body"])
        line2 = wrap(d, b, F["body"], 408, 2)
        if len(line2) > 1:
            draw_text(d, (x + 18, y + 84), line2[1], INK, F["body"])
        draw_text(d, (x + 420, y + 108), m, MUTED, F["tiny"], "ra")

    card(d, 28, 636, 496, 324, "AI/科技链条流程图", "chip")
    nodes = [("Meta", "算力Capex"), ("芯片链", "去杠杆"), ("光通信", "LITE/SIVE"), ("机器人", "TSLA/Agility")]
    x0, y0 = 66, 726
    for i, (a, b) in enumerate(nodes):
        x = x0 + i * 108
        d.rounded_rectangle((x, y0, x + 92, y0 + 82), radius=12, fill="#eef6ff", outline="#b8d3ef")
        draw_text(d, (x + 46, y0 + 15), a, BLUE, F["body_b"], "ma")
        draw_text(d, (x + 46, y0 + 44), b, INK, F["tiny"], "ma")
        if i < len(nodes) - 1:
            d.line((x + 94, y0 + 41, x + 106, y0 + 41), fill=ORANGE, width=4)
            d.polygon([(x + 106, y0 + 41), (x + 98, y0 + 35), (x + 98, y0 + 47)], fill=ORANGE)
    for i, line in enumerate(wrap(d, "链条含义：短线先杀拥挤度，后续看真实订单与财报兑现。", F["body_b"], 420, 2)):
        draw_text(d, (66, 846 + i * 28), line, INK, F["body_b"])

    card(d, 556, 636, 496, 324, "提及标的热力", "money")
    heat = [("$AMD", .82, RED), ("$MU", .75, RED), ("$LITE", .68, GREEN), ("$SIVE", .62, GREEN), ("$META", .70, ORANGE), ("$TSLA", .54, BLUE), ("$OUST", .46, GREEN), ("$AEVA", .44, GREEN)]
    for i, (name, p, c) in enumerate(heat):
        x = 590 + (i % 2) * 205
        y = 712 + (i // 2) * 48
        draw_text(d, (x, y), name, INK, F["body_b"])
        d.rounded_rectangle((x + 76, y + 7, x + 176, y + 21), radius=7, fill="#e8edf3")
        d.rounded_rectangle((x + 76, y + 7, x + 76 + int(100 * p), y + 21), radius=7, fill=c)

    card(d, 28, 986, 496, 392, "验证清单 + 观点提炼", "post")
    pill(d, 58, 1048, "三项验证", BLUE)
    checks = ["财报Capex", "HBM/光模块订单", "强平是否延续"]
    for i, item in enumerate(checks):
        y = 1092 + i * 42
        d.rectangle((58, y, 78, y + 20), outline=BLUE, width=2)
        draw_text(d, (92, y - 3), item, INK, F["body"])
    pill(d, 58, 1228, "观点", RED)
    quote = "核心不是“AI结束”，而是拥挤交易先出清；产业线索仍在，价格需要重新校准。"
    for i, line in enumerate(wrap(d, quote, F["body_b"], 420, 3)):
        draw_text(d, (58, 1272 + i * 30), line, INK, F["body_b"])

    card(d, 556, 986, 496, 392, "原帖链接索引", "post")
    links = [
        "2074568161299771394  Meta算力",
        "2074548850996707412  人形机器人",
        "2074494514061017508  芯片链回撤",
        "2074581698604593367  LiDAR审查",
    ]
    yy = 1060
    for l in links:
        d.rounded_rectangle((586, yy - 8, 1018, yy + 31), radius=9, fill="#f8fafc", outline=LINE)
        draw_text(d, (602, yy), l, INK, F["small"])
        yy += 54
    draw_text(d, (586, 1324), "链接格式：https://x.com/aleabitoreddit/status/{id}", MUTED, F["tiny"])
    img.save(OUT / "02_serenity_x_brief.png", quality=95)


def github_macro():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "AI项目 + 观察清单", "GitHub公开API：筛选2026-07-07至07-08创建/走红项目", "03 / 项目与验证")
    card(d, 28, 126, 1024, 472, "GitHub AI 新项目", "chip")
    projects = [
        ("bolivestilo/Homekit", "45★", "让AI agent控制Apple Home设备，适合智能家居/MCP接口观察。"),
        ("PeterPanSwift/fox-ai-roundtable", "36★", "同一提示并行问Claude/Codex/Gemini本地CLI，适合多模型评审。"),
        ("nqzai/kakunin-core", "31★", "AI agent合规平台：X.509身份、风险评分与自动吊销。"),
        ("wassermanproductions/blockout", "30★", "AI原生影视预演：灰盒场景、机位与角色运动参考。"),
    ]
    for i, (name, stars, desc) in enumerate(projects):
        x = 58 + (i % 2) * 492
        y = 206 + (i // 2) * 150
        d.rounded_rectangle((x, y, x + 450, y + 112), radius=12, fill="#f8fafc", outline=LINE)
        draw_text(d, (x + 16, y + 15), name, BLUE, F["body_b"])
        pill(d, x + 350, y + 12, stars, ORANGE)
        for j, line in enumerate(wrap(d, desc, F["small"], 402, 2)):
            draw_text(d, (x + 16, y + 56 + j * 22), line, INK, F["small"])

    card(d, 28, 626, 496, 340, "本周观察清单", "calendar")
    bullets(d, 58, 704, [
        (BLUE, "Fed纪要", "美东7/8下午公布，重点看通胀和利率路径措辞。"),
        (ORANGE, "芯片财报", "Samsung后，市场等待SK海力士美股交易和后续指引。"),
        (RED, "油价", "中东航运/制裁新闻可能继续扰动通胀交易。"),
    ], 420, 66)

    card(d, 556, 626, 496, 340, "验证清单", "post")
    checks = ["芯片跌幅是否扩散到软件/云", "SMH/SOXX成交是否继续放大", "10Y美债是否守住4.5%附近", "Serenity提及链条是否有公告验证"]
    yy = 704
    for c in checks:
        d.rectangle((586, yy + 2, 606, yy + 22), outline=BLUE, width=2)
        draw_text(d, (622, yy), c, INK, F["body"])
        yy += 52

    card(d, 28, 996, 1024, 390, "来源与项目链接", "post")
    pill(d, 58, 1062, "GitHub", BLUE)
    links_left = [
        "bolivestilo/Homekit",
        "PeterPanSwift/fox-ai-roundtable",
        "nqzai/kakunin-core",
        "wassermanproductions/blockout",
    ]
    for i, item in enumerate(links_left):
        d.rounded_rectangle((58, 1110 + i * 46, 500, 1144 + i * 46), radius=9, fill="#f8fafc", outline=LINE)
        draw_text(d, (74, 1116 + i * 46), item, INK, F["small"])
    pill(d, 560, 1062, "市场来源", RED)
    sources = ["Yahoo Finance / AP", "TradingKey / Reuters线索", "Federal Reserve", "CentralBank.watch"]
    for i, item in enumerate(sources):
        d.rounded_rectangle((560, 1110 + i * 46, 1000, 1144 + i * 46), radius=9, fill="#f8fafc", outline=LINE)
        draw_text(d, (576, 1116 + i * 46), item, INK, F["small"])
    draw_text(d, (58, 1340), "完整链接与平台发布文字见线程正文。", MUTED, F["small"])
    img.save(OUT / "03_github_macro_copy.png", quality=95)


if __name__ == "__main__":
    dashboard()
    serenity()
    github_macro()
    print(OUT)
