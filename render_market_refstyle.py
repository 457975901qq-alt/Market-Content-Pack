from pathlib import Path
from math import cos, sin, radians

from PIL import Image, ImageDraw, ImageFont


OUT = Path("/tmp/market_pack_2026-07-08_refstyle_v2")
OUT.mkdir(parents=True, exist_ok=True)

W, H = 1080, 1560
BG = "#fbfcfd"
INK = "#172033"
MUTED = "#5e6a7d"
BLUE = "#2266b3"
BLUE_DARK = "#132b57"
GREEN = "#249354"
RED = "#db3a34"
ORANGE = "#f5a623"
YELLOW = "#f7c744"
LINE = "#d9e0e8"
CARD = "#ffffff"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def font(size, bold=False):
    path = FONT_CANDIDATES[0]
    if bold:
        path = "/System/Library/Fonts/STHeiti Medium.ttc"
    for candidate in [path] + FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


F = {
    "title": font(48, True),
    "h1": font(30, True),
    "h2": font(22, True),
    "body": font(18),
    "body_b": font(18, True),
    "small": font(15),
    "tiny": font(13),
}


def text(draw, xy, s, fill=INK, f=None, anchor=None):
    draw.text(xy, s, fill=fill, font=f or F["body"], anchor=anchor)


def bbox(draw, s, f):
    return draw.textbbox((0, 0), s, font=f)


def wrap(draw, s, f, max_w, max_lines=2):
    lines, line = [], ""
    for ch in s:
        trial = line + ch
        if bbox(draw, trial, f)[2] <= max_w:
            line = trial
        else:
            if line:
                lines.append(line)
            line = ch
            if len(lines) == max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and "".join(lines) != s:
        last = lines[-1]
        while bbox(draw, last + "...", f)[2] > max_w and last:
            last = last[:-1]
        lines[-1] = last + "..."
    return lines


def card(draw, x, y, w, h, title, icon=""):
    draw.rounded_rectangle((x, y, x + w, y + h), radius=10, fill=CARD, outline=LINE, width=1)
    if icon:
        header_icon(draw, x + 22, y + 21, icon)
        text(draw, (x + 66, y + 18), title, BLUE_DARK, F["h2"])
    else:
        text(draw, (x + 22, y + 18), title, BLUE_DARK, F["h2"])


def header_icon(draw, x, y, kind, color=BLUE):
    """Compact icons for card headers; large illustrations use icon_box()."""
    if kind == "trophy":
        draw.rectangle((x + 10, y + 17, x + 22, y + 27), fill=color)
        draw.polygon([(x + 7, y + 4), (x + 25, y + 4), (x + 22, y + 18), (x + 10, y + 18)], fill=color)
        draw.arc((x + 1, y + 3, x + 13, y + 18), 90, 270, fill=color, width=2)
        draw.arc((x + 19, y + 3, x + 31, y + 18), -90, 90, fill=color, width=2)
        draw.rectangle((x + 6, y + 27, x + 26, y + 31), fill=color)
    elif kind == "rocket":
        draw.polygon([(x + 15, y + 1), (x + 29, y + 31), (x + 15, y + 24), (x + 1, y + 31)], fill=color)
        draw.polygon([(x + 8, y + 24), (x + 2, y + 35), (x + 16, y + 28)], fill=ORANGE)
        draw.polygon([(x + 22, y + 24), (x + 30, y + 35), (x + 15, y + 28)], fill=ORANGE)
    elif kind == "fire":
        draw.polygon([(x + 17, y + 1), (x + 29, y + 19), (x + 21, y + 35), (x + 5, y + 25)], fill=ORANGE)
        draw.polygon([(x + 17, y + 12), (x + 23, y + 24), (x + 17, y + 35), (x + 10, y + 24)], fill=RED)
    elif kind == "globe":
        draw.ellipse((x + 1, y + 2, x + 31, y + 32), outline=GREEN, width=2)
        draw.line((x + 1, y + 17, x + 31, y + 17), fill=GREEN, width=2)
        draw.arc((x + 8, y + 2, x + 24, y + 32), 80, 280, fill=GREEN, width=2)
    elif kind == "bank":
        draw.polygon([(x + 1, y + 13), (x + 16, y + 1), (x + 31, y + 13)], fill="#476a9a")
        for i in range(4):
            draw.rectangle((x + 5 + i * 7, y + 15, x + 9 + i * 7, y + 32), fill="#476a9a")
        draw.rectangle((x + 2, y + 33, x + 30, y + 36), fill="#476a9a")
    elif kind == "money":
        draw.rounded_rectangle((x + 1, y + 4, x + 31, y + 21), radius=4, fill=GREEN)
        text(draw, (x + 16, y + 5), "$", "white", F["small"], "ma")
        draw.ellipse((x + 4, y + 21, x + 24, y + 41), fill=YELLOW)
        draw.ellipse((x + 17, y + 16, x + 35, y + 34), fill="#d89e28")
    elif kind == "calendar":
        draw.rectangle((x + 1, y + 4, x + 31, y + 35), outline="#4e78a8", width=2)
        draw.rectangle((x + 1, y + 4, x + 31, y + 13), fill="#4e78a8")
        for i in range(3):
            for j in range(2):
                draw.rectangle((x + 7 + i * 8, y + 18 + j * 8, x + 11 + i * 8, y + 22 + j * 8), fill="#9bb6d5")
    elif kind == "doc":
        draw.rounded_rectangle((x + 6, y + 1, x + 30, y + 35), radius=4, outline="#4e78a8", width=2)
        draw.line((x + 11, y + 13, x + 25, y + 13), fill="#4e78a8", width=2)
        draw.line((x + 11, y + 22, x + 24, y + 22), fill="#4e78a8", width=2)
    else:
        draw.ellipse((x + 2, y + 2, x + 32, y + 32), fill=color)


def icon_box(draw, x, y, kind, color=BLUE):
    if kind == "trophy":
        draw.rectangle((x + 9, y + 20, x + 25, y + 34), fill=color)
        draw.polygon([(x + 6, y + 7), (x + 28, y + 7), (x + 24, y + 22), (x + 10, y + 22)], fill=color)
        draw.arc((x, y + 5, x + 13, y + 22), 90, 270, fill=color, width=3)
        draw.arc((x + 21, y + 5, x + 34, y + 22), -90, 90, fill=color, width=3)
        draw.rectangle((x + 5, y + 34, x + 29, y + 39), fill=color)
    elif kind == "rocket":
        draw.polygon([(x + 16, y), (x + 33, y + 34), (x + 16, y + 26), (x, y + 34)], fill=color)
        draw.polygon([(x + 9, y + 28), (x, y + 42), (x + 17, y + 34)], fill=ORANGE)
        draw.polygon([(x + 24, y + 28), (x + 34, y + 42), (x + 16, y + 34)], fill=ORANGE)
    elif kind == "fire":
        draw.polygon([(x + 18, y), (x + 32, y + 22), (x + 22, y + 42), (x + 6, y + 30)], fill=ORANGE)
        draw.polygon([(x + 18, y + 14), (x + 25, y + 29), (x + 17, y + 42), (x + 10, y + 28)], fill=RED)
    elif kind == "globe":
        draw.ellipse((x, y, x + 34, y + 34), outline=GREEN, width=2)
        draw.line((x, y + 17, x + 34, y + 17), fill=GREEN, width=2)
        draw.arc((x + 7, y, x + 27, y + 34), 80, 280, fill=GREEN, width=2)
    elif kind == "bank":
        draw.polygon([(x, y + 14), (x + 18, y), (x + 36, y + 14)], fill="#476a9a")
        for i in range(4):
            draw.rectangle((x + 4 + i * 8, y + 16, x + 9 + i * 8, y + 38), fill="#476a9a")
        draw.rectangle((x + 1, y + 39, x + 35, y + 43), fill="#476a9a")
    elif kind == "money":
        draw.rounded_rectangle((x, y + 4, x + 36, y + 24), radius=4, fill=GREEN)
        text(draw, (x + 18, y + 5), "$", "white", F["body_b"], "ma")
        draw.ellipse((x + 6, y + 24, x + 32, y + 50), fill=YELLOW)
        draw.ellipse((x + 20, y + 17, x + 43, y + 40), fill="#d89e28")
    elif kind == "calendar":
        draw.rectangle((x, y + 5, x + 36, y + 42), outline="#4e78a8", width=2)
        draw.rectangle((x, y + 5, x + 36, y + 15), fill="#4e78a8")
        for i in range(3):
            for j in range(2):
                draw.rectangle((x + 7 + i * 10, y + 21 + j * 9, x + 12 + i * 10, y + 26 + j * 9), fill="#9bb6d5")
    elif kind == "doc":
        draw.rounded_rectangle((x + 6, y, x + 32, y + 40), radius=4, outline="#4e78a8", width=2)
        draw.line((x + 11, y + 13, x + 27, y + 13), fill="#4e78a8", width=2)
        draw.line((x + 11, y + 22, x + 25, y + 22), fill="#4e78a8", width=2)
    elif kind == "chip":
        draw.rounded_rectangle((x + 4, y + 4, x + 34, y + 34), radius=5, fill="#e5f3f7", outline="#217287", width=3)
        text(draw, (x + 19, y + 10), "AI", "#217287", F["body"], "ma")


def label(draw, x, y, s, color=BLUE):
    draw.rounded_rectangle((x, y, x + 44, y + 26), radius=5, fill=color)
    text(draw, (x + 22, y + 6), s, "white", F["tiny"], "ma")


def bar(draw, x, y, w, value, positive=True):
    draw.rounded_rectangle((x, y, x + w, y + 16), radius=6, fill="#e5eaef")
    fill = GREEN if positive else RED
    if positive:
        draw.rounded_rectangle((x, y, x + int(w * value), y + 16), radius=6, fill=fill)
    else:
        fw = int(w * value)
        draw.rounded_rectangle((x + w - fw, y, x + w, y + 16), radius=6, fill=fill)


def mini_line(draw, x, y, w, h, pts, color):
    mn, mx = min(pts), max(pts)
    span = max(mx - mn, 1)
    xy = []
    for i, p in enumerate(pts):
        px = x + int(i * w / (len(pts) - 1))
        py = y + h - int((p - mn) * h / span)
        xy.append((px, py))
    draw.line(xy, fill=color, width=3)
    for p in xy[-2:]:
        draw.ellipse((p[0] - 2, p[1] - 2, p[0] + 2, p[1] + 2), fill=color)


def gauge(draw, cx, cy, r, value, label_text):
    start = 180
    segments = [(180, 225, RED), (225, 270, ORANGE), (270, 315, YELLOW), (315, 360, GREEN)]
    for a0, a1, c in segments:
        draw.arc((cx - r, cy - r, cx + r, cy + r), a0, a1, fill=c, width=28)
    a = radians(start + value * 180)
    end = (cx + int(cos(a) * (r - 20)), cy + int(sin(a) * (r - 20)))
    draw.line((cx, cy, *end), fill=INK, width=8)
    draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=INK)
    text(draw, (cx, cy + 30), label_text, GREEN if value > 0.65 else ORANGE, F["h2"], "ma")


def simple_bull_bear(draw, x, y):
    def p(points, sx, sy, color):
        draw.polygon([(x + int(px * 0.78) + sx, y + int(py * 0.78) + sy) for px, py in points], fill=color)

    def e(box, sx, sy, color):
        x0, y0, x1, y1 = box
        draw.ellipse((x + int(x0 * 0.78) + sx, y + int(y0 * 0.78) + sy, x + int(x1 * 0.78) + sx, y + int(y1 * 0.78) + sy), fill=color)

    p([(10, 60), (65, 25), (120, 60), (90, 90), (35, 90)], 0, 0, "#2866ad")
    e((105, 42, 142, 78), 0, 0, "#2866ad")
    p([(128, 44), (150, 24), (140, 55)], 0, 0, "#2866ad")
    p([(170, 60), (225, 28), (280, 60), (250, 92), (195, 92)], -8, 0, "#a46127")
    e((266, 42, 300, 76), -8, 0, "#a46127")
    p([(290, 45), (315, 28), (304, 58)], -8, 0, "#a46127")


def header(draw, title, sub, right):
    text(draw, (36, 28), title, "#05070b", F["title"])
    text(draw, (38, 92), sub, INK, F["h2"])
    text(draw, (420, 36), "2026年7月8日（周三）", BLUE_DARK, F["h1"])
    text(draw, (423, 76), right, MUTED, F["body"])


def draw_dashboard():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "每日市场内容包", "聚焦AI、科技、宏观与全球市场动向", "数据截至：7/7美股收盘")

    margin, gap = 28, 16
    cw = (W - margin * 2 - gap * 2) // 3
    y0, ch = 130, 300
    xs = [margin, margin + cw + gap, margin + (cw + gap) * 2]

    card(d, xs[0], y0, cw, ch, "今日市场总览", "trophy")
    rows = [
        ("US", "标普500", "7,503.85", "-0.4%", RED),
        ("US", "纳斯达克", "25,818.69", "-1.2%", RED),
        ("US", "道琼斯", "52,925.15", "-0.2%", RED),
        ("US", "费半指数", "日内走弱", "承压", RED),
        ("US", "WTI原油", "$68.83", "小涨", GREEN),
    ]
    yy = y0 + 62
    for code, name, val, pct, col in rows:
        label(d, xs[0] + 18, yy - 3, code)
        text(d, (xs[0] + 72, yy), name, INK, F["body"])
        text(d, (xs[0] + 188, yy), val, INK, F["body"], "ma")
        text(d, (xs[0] + cw - 22, yy), pct, col, F["body"], "ra")
        yy += 36
    simple_bull_bear(d, xs[0] + 46, y0 + 200)

    card(d, xs[1], y0, cw, ch, "今日重点板块表现", "rocket")
    sectors = [("能源", 0.78, True, "+0.7%"), ("金融", 0.55, True, "+0.5%"), ("机器人", 0.34, True, "+0.3%"), ("AI软件", 0.18, True, "验证"), ("半导体", 0.62, False, "降温"), ("CPO/光通", 0.74, False, "承压")]
    yy = y0 + 58
    for name, v, pos, pct in sectors:
        text(d, (xs[1] + 20, yy), name, INK, F["body"])
        bar(d, xs[1] + 135, yy + 2, 145, v, pos)
        text(d, (xs[1] + cw - 18, yy), pct, GREEN if pos else RED, F["body"], "ra")
        yy += 36

    card(d, xs[2], y0, cw, ch, "今日Top 3市场催化剂", "fire")
    catalysts = [
        ("1", "AI拥挤交易降温", "Samsung好业绩也被卖"),
        ("2", "本周数据密集", "FOMC纪要、初请、CPI"),
        ("3", "能源风险回归", "霍尔木兹与伊朗油售豁免"),
    ]
    yy = y0 + 60
    for n, t, sub in catalysts:
        d.ellipse((xs[2] + 20, yy - 5, xs[2] + 54, yy + 29), fill=BLUE)
        text(d, (xs[2] + 37, yy), n, "white", F["body_b"], "ma")
        text(d, (xs[2] + 70, yy - 3), t, BLUE_DARK, F["body_b"])
        text(d, (xs[2] + 70, yy + 23), sub, INK, F["small"])
        if n == "1":
            icon_box(d, xs[2] + cw - 60, yy + 2, "chip")
        elif n == "2":
            icon_box(d, xs[2] + cw - 60, yy + 2, "calendar")
        else:
            d.polygon([(xs[2] + cw - 48, yy + 35), (xs[2] + cw - 12, yy + 35), (xs[2] + cw - 30, yy + 12)], fill="#406896")
        yy += 75

    y1 = y0 + ch + 20
    card(d, xs[0], y1, cw, 280, "宏观数据与事件日历", "globe")
    events = [("7/8", "FOMC纪要", "★★★"), ("7/9", "初请失业金", "★★★"), ("7/10", "CPI", "★★★★"), ("本周", "内存/芯片指引", "★★★"), ("持续", "油价/航运", "★★★")]
    yy = y1 + 58
    for day, ev, stars in events:
        d.rounded_rectangle((xs[0] + 20, yy - 6, xs[0] + 78, yy + 22), radius=8, fill="#eef3f8")
        text(d, (xs[0] + 49, yy - 1), day, BLUE_DARK, F["small"], "ma")
        text(d, (xs[0] + 96, yy - 2), ev, INK, F["body"])
        text(d, (xs[0] + cw - 24, yy - 2), stars, RED, F["small"], "ra")
        yy += 39
    icon_box(d, xs[0] + 52, y1 + 220, "calendar")
    mini_line(d, xs[0] + 180, y1 + 230, 90, 40, [1, 2, 2, 3, 5, 6], RED)

    card(d, xs[1], y1, cw, 280, "全球央行动态", "bank")
    banks = [("US", "美联储", "等FOMC纪要"), ("EU", "欧央行", "AI纳入政策框架"), ("JP", "日本央行", "长端利率压力"), ("UK", "英国央行", "通胀与增长平衡"), ("CA", "加拿大央行", "能源与汇率变量")]
    yy = y1 + 58
    for code, name, note in banks:
        label(d, xs[1] + 18, yy - 4, code, {"US": "#2e5ea8", "EU": "#3e5fb1", "JP": "#e5444b", "UK": "#3a5795", "CA": "#d84343"}[code])
        text(d, (xs[1] + 74, yy - 1), name, INK, F["body_b"])
        text(d, (xs[1] + 162, yy - 1), note, MUTED, F["small"])
        yy += 40
    icon_box(d, xs[1] + cw - 82, y1 + 214, "bank")

    card(d, xs[2], y1, cw, 280, "国际重要事件与政策", "globe")
    intl = [("IR", "中东局势", "美撤销伊朗油售豁免"), ("US", "美国监管", "SEC零售欺诈工作组"), ("CN", "中国机器人", "量产预期上修"), ("EU", "欧洲问题", "AI与货币政策讨论"), ("US", "美国科技", "Meta算力需求澄清")]
    yy = y1 + 58
    for code, name, note in intl:
        label(d, xs[2] + 18, yy - 4, code, {"IR": "#238a55", "US": "#2e5ea8", "CN": "#db3a34", "EU": "#3e5fb1"}[code])
        text(d, (xs[2] + 74, yy - 1), name, INK, F["body_b"])
        text(d, (xs[2] + 162, yy - 1), note, MUTED, F["small"])
        yy += 40
    d.rectangle((xs[2] + cw - 64, y1 + 205, xs[2] + cw - 56, y1 + 245), fill="#506987")
    d.polygon([(xs[2] + cw - 82, y1 + 245), (xs[2] + cw - 24, y1 + 245), (xs[2] + cw - 44, y1 + 259)], fill="#3b75ad")

    y2 = y1 + 300
    card(d, xs[0], y2, cw, 250, "大宗商品与加密货币", "doc")
    items = [("原油 WTI", "$68.83", "小幅走高", RED, [1, 2, 1, 3, 4, 5]), ("黄金 XAU", "未接入", "观察避险", ORANGE, [3, 2, 2, 3, 2, 3]), ("比特币 BTC", "未接入", "风险偏好", GREEN, [1, 1, 2, 3, 3, 4])]
    yy = y2 + 60
    for name, val, note, col, pts in items:
        text(d, (xs[0] + 20, yy), name, INK, F["body"])
        text(d, (xs[0] + 144, yy), val, INK, F["small"])
        text(d, (xs[0] + 222, yy), note, col, F["small"])
        mini_line(d, xs[0] + 260, yy - 2, 62, 26, pts, col)
        yy += 58

    card(d, xs[1], y2, cw, 250, "资金流向与市场情绪", "money")
    flow = [("AI基金仍有资金支持", GREEN), ("芯片链从拥挤交易降温", RED), ("ETF资金在大盘内部轮动", ORANGE), ("今天未读取GitHub", MUTED)]
    yy = y2 + 60
    for s, col in flow:
        d.ellipse((xs[1] + 24, yy + 5, xs[1] + 34, yy + 15), fill=col)
        text(d, (xs[1] + 48, yy), s, INK, F["body"])
        yy += 38
    icon_box(d, xs[1] + cw - 90, y2 + 162, "money")

    card(d, xs[2], y2, cw, 250, "市场情绪与机构观点", "")
    gauge(d, xs[2] + cw // 2, y2 + 145, 76, 0.64, "谨慎")
    tags = [("拥挤降温", RED), ("事件密集", ORANGE), ("AI验证", GREEN)]
    tx = xs[2] + 40
    for s, col in tags:
        d.rounded_rectangle((tx, y2 + 205, tx + 82, y2 + 230), radius=12, fill=col)
        text(d, (tx + 41, y2 + 210), s, "white", F["tiny"], "ma")
        tx += 92

    y3 = y2 + 270
    card(d, xs[0], y3, cw, 180, "抖音文案", "doc")
    text(d, (xs[0] + 22, y3 + 62), "封面标题", BLUE_DARK, F["small"])
    text(d, (xs[0] + 22, y3 + 92), "AI没退潮，只是拥挤交易降温", INK, F["body_b"])
    mini_line(d, xs[0] + cw - 96, y3 + 120, 68, 35, [1, 2, 1, 3, 5], BLUE)

    card(d, xs[1], y3, cw, 180, "小红书文案", "doc")
    bullets = ["Samsung好业绩仍被卖", "芯片/内存/CPO回调", "Meta算力需求仍在", "机器人量产成新催化"]
    yy = y3 + 62
    for b in bullets:
        d.rectangle((xs[1] + 24, yy + 4, xs[1] + 32, yy + 12), fill=GREEN)
        text(d, (xs[1] + 42, yy), b, INK, F["small"])
        yy += 26

    card(d, xs[2], y3, cw, 180, "一周总结素材", "calendar")
    grid = [("7/8", "FOMC纪要"), ("7/9", "初请失业金"), ("7/10", "CPI"), ("持续", "油价/航运")]
    yy = y3 + 62
    for day, ev in grid:
        text(d, (xs[2] + 24, yy), day, BLUE_DARK, F["small"])
        text(d, (xs[2] + 82, yy), ev, INK, F["small"])
        yy += 27

    y4 = y3 + 200
    card(d, xs[0], y4, cw, 250, "X 文案", "doc")
    x_lines = ["AI trade not over.", "Crowded chip names", "are de-risking first."]
    yy = y4 + 62
    for line in x_lines:
        text(d, (xs[0] + 22, yy), line, INK, F["small"])
        yy += 24
    mini_line(d, xs[0] + cw - 106, y4 + 176, 76, 36, [1, 1, 2, 2, 4, 5], BLUE)

    card(d, xs[1], y4, cw, 250, "公众号摘要", "doc")
    summary = ["主线：AI进入验证期", "风险：CPI与油价", "机会：订单继续落地"]
    yy = y4 + 62
    for s in summary:
        d.rounded_rectangle((xs[1] + 24, yy + 3, xs[1] + 36, yy + 15), radius=3, fill=BLUE)
        text(d, (xs[1] + 48, yy), s, INK, F["small"])
        yy += 34
    d.rounded_rectangle((xs[1] + 210, y4 + 166, xs[1] + 300, y4 + 210), radius=10, fill="#eef4fa", outline=LINE)
    mini_line(d, xs[1] + 222, y4 + 178, 56, 22, [2, 1, 3, 2, 4], GREEN)

    card(d, xs[2], y4, cw, 250, "来源索引", "doc")
    sources = [("AP", "美股指数"), ("BI", "Samsung/芯片"), ("ECB", "央行AI"), ("X", "Serenity")]
    yy = y4 + 62
    for code, name in sources:
        label(d, xs[2] + 22, yy - 3, code, "#4e78a8")
        text(d, (xs[2] + 82, yy), name, INK, F["small"])
        yy += 36

    text(d, (34, H - 35), "免责声明：本内容仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。", MUTED, F["small"])
    img.save(OUT / "01_reference_dashboard.png", quality=95)


def draw_serenity():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    text(d, (36, 30), "Serenity 今日信息简报", "#05070b", F["title"])
    text(d, (38, 94), "@aleabitoreddit｜2026年7月8日｜今日未读取GitHub", INK, F["h2"])
    text(d, (W - 36, 44), "X 已接入", BLUE_DARK, F["h2"], "ra")
    text(d, (W - 36, 78), "筛选过去24小时市场相关帖", MUTED, F["small"], "ra")

    margin, gap = 28, 22
    cw = (W - margin * 2 - gap) // 2
    y0, h = 130, 270
    cards = [
        ("Serenity 观点 1", "Hesai / NVIDIA 审查", "中国 lidar 合作受美国审查，西方 lidar 与上游激光链或受益。", "chip"),
        ("Serenity 观点 2", "Meta Compute 澄清", "Meta称仍需更多算力，数据中心与云厂商交易继续推进。", "rocket"),
        ("Serenity 观点 3", "机器人 Sputnik Moment", "中国人形机器人量产预期上修，美中量产差距成为政策催化。", "globe"),
        ("Serenity 观点 4", "无差别去杠杆", "NBIS/MRVL/SNDK/AMD/MU等同跌，更像资金去杠杆。", "money"),
    ]
    for i, (ttl, main, sub, ic) in enumerate(cards):
        x = margin + (i % 2) * (cw + gap)
        y = y0 + (i // 2) * (h + 24)
        card(d, x, y, cw, h, ttl)
        icon_box(d, x + 36, y + 94, ic)
        text(d, (x + 134, y + 82), main, INK, F["h1"])
        for j, line in enumerate(wrap(d, sub, F["body"], cw - 170, 3)):
            text(d, (x + 134, y + 126 + j * 28), line, INK, F["body"])

    y1 = y0 + 2 * (h + 24) + 4
    card(d, margin, y1, W - margin * 2, 230, "AI链条轮动图", "")
    chain = [("GPU", "拥挤"), ("HBM/DRAM", "验证"), ("CPO/光通", "错杀?"), ("数据中心", "订单"), ("机器人", "政策催化")]
    x = margin + 42
    yy = y1 + 90
    for idx, (a, b) in enumerate(chain):
        d.rounded_rectangle((x, yy, x + 136, yy + 74), radius=16, fill="#fff8f3", outline="#9b552b", width=2)
        text(d, (x + 68, yy + 17), a, "#5b3725", F["h2"], "ma")
        text(d, (x + 68, yy + 46), b, "#5b3725", F["small"], "ma")
        if idx < len(chain) - 1:
            d.line((x + 144, yy + 37, x + 190, yy + 37), fill="#88491f", width=4)
            d.polygon([(x + 190, yy + 37), (x + 176, yy + 28), (x + 176, yy + 46)], fill="#88491f")
        x += 190

    y2 = y1 + 254
    card(d, margin, y2, W - margin * 2, 270, "可发布短评", "doc")
    text(d, (margin + 24, y2 + 62), "核心判断", BLUE_DARK, F["h2"])
    short = "AI需求没有消失，但资金正在从最拥挤的内存、芯片、光通信交易中降温。后续看内存价格周期，以及大模型算力订单能否继续落地。"
    for i, line in enumerate(wrap(d, short, F["body"], W - margin * 2 - 64, 3)):
        text(d, (margin + 24, y2 + 100 + i * 30), line, INK, F["body"])
    text(d, (margin + 24, y2 + 208), "原帖：2074581698604593367 / 2074568161299771394 / 2074548850996707412 / 2074494514061017508", MUTED, F["tiny"])

    y3 = y2 + 294
    left_w = (W - margin * 2 - gap) // 2
    card(d, margin, y3, left_w, 220, "提及标的热力", "")
    chips = [("NVDA", GREEN), ("META", GREEN), ("OUST", GREEN), ("AEVA", GREEN), ("NBIS", RED), ("MRVL", RED), ("AMD", RED), ("MU", RED)]
    xx, yy = margin + 34, y3 + 64
    for i, (s, col) in enumerate(chips):
        d.rounded_rectangle((xx, yy, xx + 84, yy + 30), radius=14, fill=col)
        text(d, (xx + 42, yy + 7), s, "white", F["tiny"], "ma")
        xx += 96
        if (i + 1) % 4 == 0:
            xx, yy = margin + 34, yy + 48
    mini_line(d, margin + 320, y3 + 158, 120, 34, [5, 3, 4, 2, 2, 1], RED)

    card(d, margin + left_w + gap, y3, left_w, 220, "验证清单", "calendar")
    checks = ["Meta算力订单", "HBM/DRAM价格", "CPO交付节奏", "机器人量产数据"]
    yy = y3 + 62
    for s in checks:
        d.rectangle((margin + left_w + gap + 26, yy + 4, margin + left_w + gap + 36, yy + 14), fill=GREEN)
        text(d, (margin + left_w + gap + 48, yy), s, INK, F["small"])
        yy += 34

    text(d, (34, H - 35), "免责声明：X观点需与公司公告、财报和主流媒体交叉验证；不构成投资建议。", MUTED, F["small"])
    img.save(OUT / "02_reference_serenity.png", quality=95)


if __name__ == "__main__":
    draw_dashboard()
    draw_serenity()
    print(OUT)
