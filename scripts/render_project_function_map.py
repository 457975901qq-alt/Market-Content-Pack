from __future__ import annotations

from pathlib import Path
from textwrap import wrap

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "project_function_map.png"
WIDTH, HEIGHT = 2200, 1500


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size, index=0)
            except OSError:
                continue
    return ImageFont.load_default()


TITLE = font(46, True)
SUBTITLE = font(25)
SECTION = font(27, True)
BOX_TITLE = font(24, True)
BODY = font(20)
SMALL = font(17)
TINY = font(15)


BG = "#f6f3ed"
INK = "#17191c"
MUTED = "#5f6872"
LINE = "#aeb6bf"
ACTIVE = "#173f5f"
ACTIVE_FILL = "#e6eff5"
FALLBACK = "#8b4b1f"
FALLBACK_FILL = "#fff0df"
DISABLED = "#757575"
DISABLED_FILL = "#ececec"
GREEN = "#2d6a4f"
GREEN_FILL = "#e8f3ed"
ORANGE = "#c85a22"


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def draw_wrapped(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fnt: ImageFont.FreeTypeFont, fill: str, width: int, line_gap: int = 5) -> int:
    x, y = xy
    chars = max(8, int(width / max(1, text_size(draw, "汉", fnt)[0])))
    lines = []
    for paragraph in text.split("\n"):
        lines.extend(wrap(paragraph, width=chars, break_long_words=False, replace_whitespace=False) or [""])
    line_h = text_size(draw, "国", fnt)[1] + line_gap
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill)
        y += line_h
    return y


def box(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], title: str, body: str, *, fill: str, outline: str, badge: str | None = None) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=18, fill=fill, outline=outline, width=3)
    draw.text((x1 + 18, y1 + 15), title, font=BOX_TITLE, fill=INK)
    draw_wrapped(draw, (x1 + 18, y1 + 53), body, BODY, MUTED, x2 - x1 - 36)
    if badge:
        bw, bh = text_size(draw, badge, TINY)
        bx = x2 - bw - 27
        by = y1 + 16
        draw.rounded_rectangle((bx - 8, by - 3, bx + bw + 8, by + bh + 4), radius=8, fill=outline)
        draw.text((bx, by), badge, font=TINY, fill="white")


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], color: str = LINE, width: int = 4) -> None:
    draw.line((*start, *end), fill=color, width=width)
    x1, y1 = start
    x2, y2 = end
    if abs(x2 - x1) >= abs(y2 - y1):
        direction = 1 if x2 >= x1 else -1
        pts = [(x2, y2), (x2 - direction * 14, y2 - 8), (x2 - direction * 14, y2 + 8)]
    else:
        direction = 1 if y2 >= y1 else -1
        pts = [(x2, y2), (x2 - 8, y2 - direction * 14), (x2 + 8, y2 - direction * 14)]
    draw.polygon(pts, fill=color)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    draw.text((70, 42), "每日市场内容包｜当前功能架构", font=TITLE, fill=INK)
    draw.text((72, 103), "以当前代码与配置为准：文字内容、结构化行情和审计优先；图片与外部发布保持关闭。", font=SUBTITLE, fill=MUTED)
    draw.line((70, 145, WIDTH - 70, 145), fill=ORANGE, width=5)

    # Legend
    legend = [(ACTIVE, "当前主流程"), (FALLBACK, "受控 fallback"), (DISABLED, "当前关闭/阻断")]
    lx = WIDTH - 620
    for color, label in legend:
        draw.rounded_rectangle((lx, 62, lx + 22, 84), radius=6, fill=color)
        draw.text((lx + 32, 60), label, font=TINY, fill=MUTED)
        lx += 190

    # Main pipeline
    draw.text((70, 176), "A 运行主链路", font=SECTION, fill=ACTIVE)
    y = 220
    boxes = [
        ((70, y, 310, y + 125), "入口", "main.py\n早盘 06:30 / 晚盘 17:30\n--dry-run 默认运行", ACTIVE_FILL, ACTIVE, "ACTIVE"),
        ((355, y, 655, y + 125), "运行控制", "edition profile\n状态机 / checkpoint\n运行锁 / SQLite 索引", ACTIVE_FILL, ACTIVE, "ACTIVE"),
        ((700, y, 1010, y + 125), "Planner + Tool Router", "健康检查后排序 Provider\n记录选中与拒绝原因\n限制步骤、调用次数", ACTIVE_FILL, ACTIVE, "ACTIVE"),
        ((1055, y, 1365, y + 125), "Function Calling", "Planner → FunctionCall\nPydantic 参数校验\n固定 Registry 绑定执行", ACTIVE_FILL, ACTIVE, "ACTIVE"),
        ((1410, y, 1740, y + 125), "内容包", "严格 JSON\n15 个独立市场模块\nAI 投资观察（情景分析）", ACTIVE_FILL, ACTIVE, "ACTIVE"),
        ((1785, y, 2130, y + 125), "交付报告", "HTML 可视化报告\nMarkdown 降级报告\n本地归档", GREEN_FILL, GREEN, "LOCAL")
    ]
    for xy, title, body, fill, outline, badge in boxes:
        box(draw, xy, title, body, fill=fill, outline=outline, badge=badge)
    for i in range(len(boxes) - 1):
        arrow(draw, (boxes[i][0][2] + 8, y + 62), (boxes[i + 1][0][0] - 8, y + 62), ACTIVE)

    # Collection layer
    draw.text((70, 405), "B 数据与模型层", font=SECTION, fill=ACTIVE)
    collection = [
        ((70, 450, 390, 645), "多源素材", "source_router\nRSS：当前可用\nGitHub：当前可用\nX / Exa / Jina：按健康状态记录 unavailable", ACTIVE_FILL, ACTIVE, "ROUTED"),
        ((425, 450, 745, 645), "结构化行情", "market_quotes\nYahoo Chart 主源\nGoogle Finance 交叉源\nVOO / QQQM 必需 ETF 资产", ACTIVE_FILL, ACTIVE, "CROSSCHECK"),
        ((780, 450, 1085, 645), "内容 Provider", "Ollama 优先\nGemini / OpenAI 受控候选\nrule_template 保底\n不伪造价格和事件", FALLBACK_FILL, FALLBACK, "FALLBACK"),
        ((1120, 450, 1425, 645), "来源与血缘", "normalized / filtered materials\nmarket_data_version\ncontent_hash\nsource URL / timestamp", ACTIVE_FILL, ACTIVE, "TRACEABLE"),
        ((1460, 450, 1775, 645), "输入契约", "edition / cutoff / session\n固定 Prompt 版本\n过期、未来、冲突数据阻断", ACTIVE_FILL, ACTIVE, "GUARDED"),
        ((1810, 450, 2130, 645), "模型状态", "healthcheck\n实际 Provider / fallback\n模型失败回到规则模板或阻断", FALLBACK_FILL, FALLBACK, "CONTROLLED")
    ]
    for xy, title, body, fill, outline, badge in collection:
        box(draw, xy, title, body, fill=fill, outline=outline, badge=badge)
    # arrows into content package / router
    arrow(draw, (230, 450), (230, 355), ACTIVE)
    arrow(draw, (585, 450), (585, 355), ACTIVE)
    arrow(draw, (930, 450), (930, 355), FALLBACK)
    arrow(draw, (1270, 450), (1270, 355), ACTIVE)
    arrow(draw, (1615, 450), (1615, 355), ACTIVE)
    arrow(draw, (1970, 450), (1970, 355), FALLBACK)

    # Quality and recovery
    draw.text((70, 705), "C 质量、复核与自愈", font=SECTION, fill=ACTIVE)
    quality = [
        ((70, 750, 425, 955), "Quality Gate", "schema completeness\nsource grounding\ntemporal consistency\ncontent / final quality gate", ACTIVE_FILL, ACTIVE, "BLOCKING"),
        ((465, 750, 820, 955), "Reviewer Agent", "独立复核结果\n固定 JSON 结果\napprove / reject / needs_review\n不可自动修改原始 artifact", ACTIVE_FILL, ACTIVE, "2ND PASS"),
        ((860, 750, 1215, 955), "Offline Evaluation", "Golden Dataset\ndeterministic evaluators\nOllama 全量 Judge\nGemini 仅关键样本复核", ACTIVE_FILL, ACTIVE, "OFFLINE"),
        ((1255, 750, 1610, 955), "Gap Repair", "Gap Analyzer\nRepair Planner\n只重跑必要子任务\n重算 hash / 保留旧版本", FALLBACK_FILL, FALLBACK, "BOUNDED"),
        ((1650, 750, 2130, 955), "Self-Healing", "支持：Ollama 不可用、网络失败、行情缺失、Gemini JSON 失败\n最多重试与恢复；高风险进入人工处理", FALLBACK_FILL, FALLBACK, "LOW-RISK")
    ]
    for xy, title, body, fill, outline, badge in quality:
        box(draw, xy, title, body, fill=fill, outline=outline, badge=badge)
    # Route the main content-to-quality connection around the data layer so
    # the diagram reads as a workflow instead of a crossing diagonal.
    draw.line((1575, 345, 1575, 680), fill=ACTIVE, width=4)
    draw.line((1575, 680, 250, 680), fill=ACTIVE, width=4)
    arrow(draw, (250, 680), (250, 750), ACTIVE)
    arrow(draw, (1750, 645), (1610, 750), FALLBACK)
    arrow(draw, (1215, 852), (1255, 852), FALLBACK)
    arrow(draw, (820, 852), (860, 852), ACTIVE)

    # Current disabled capabilities
    draw.text((70, 1015), "D 当前明确关闭或阻断的能力", font=SECTION, fill=DISABLED)
    disabled = [
        ((70, 1060, 550, 1260), "图片生成", "runtime_policy.allow_image_generation = false\n当前日报只生成文字与结构化数据\n不会保存或发送图片", DISABLED_FILL, DISABLED, "OFF"),
        ((590, 1060, 1070, 1260), "外部发布", "delivery_policy.enabled = false\ndeliver / canary_deliver 不进入普通 Function Calling\n不调用正式发布接口", DISABLED_FILL, DISABLED, "BLOCKED"),
        ((1110, 1060, 1590, 1260), "生产变更", "Evaluation 不自动更新 Prompt 或模型\nReviewer 不修改内容\nSelf-Healing 不改业务代码", DISABLED_FILL, DISABLED, "SAFE"),
        ((1630, 1060, 2130, 1260), "可观测与审计", "本地 trace / events / steps\nPhoenix 依赖可选，主流程不依赖它\n报告、状态、决策可追溯", GREEN_FILL, GREEN, "LOCAL")
    ]
    for xy, title, body, fill, outline, badge in disabled:
        box(draw, xy, title, body, fill=fill, outline=outline, badge=badge)

    # Footer
    draw.line((70, 1335, WIDTH - 70, 1335), fill=LINE, width=2)
    footer = "当前结论：已恢复并实际使用的是“真实来源采集 → 结构化行情 → 受控内容生成 → 15 模块校验 → Reviewer → 离线评测 → 本地文字报告”的链路；图片和正式发布不是当前运行结果。"
    draw_wrapped(draw, (70, 1360), footer, SMALL, MUTED, WIDTH - 140)
    draw.text((70, 1438), "生成依据：main.py / build_daily_market_pack.py / source_router.py / market_quotes.py / market_content_openai.py / reviewer_agent.py / evals/ / config/*.json", font=TINY, fill=MUTED)

    image.save(OUT, format="PNG", optimize=True)
    print(OUT)


if __name__ == "__main__":
    main()
