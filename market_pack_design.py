#!/usr/bin/env python3
"""Shared design tokens and page definitions for the 9-page market image pack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

WIDTH = 1080
HEIGHT = 1920
SAFE_X = 76
SAFE_TOP = 58
SAFE_BOTTOM = 70
HEADER_HEIGHT = 315
CARD_GAP = 22
CARD_RADIUS = 25

COLORS = {
    "background": "#F7F2E8",
    "ink": "#111111",
    "orange": "#F36A13",
    "muted": "#565656",
    "card": "#FFFDF9",
    "line": "#E7E1D8",
    "green": "#2E8B45",
    "red": "#D63C32",
    "neutral": "#767676",
    "warning": "#F28C18",
    "blue": "#2F68B7",
}

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


def first_font(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


REGULAR_FONT = first_font(FONT_CANDIDATES)
BOLD_FONT = first_font(BOLD_FONT_CANDIDATES)


@dataclass(frozen=True)
class PageSpec:
    number: int
    title: str
    conclusion: str
    visual: str


PAGES = [
    PageSpec(1, "封面", "当天最重要的市场主线", "hero"),
    PageSpec(2, "市场总览", "指数、情绪与Top 3催化剂", "overview"),
    PageSpec(3, "宏观数据与全球央行", "通胀、利率、美元与央行定价", "macro"),
    PageSpec(4, "大宗商品与地缘政治", "能源、贵金属与风险传导", "commodities"),
    PageSpec(5, "AI与半导体", "算力、芯片、供应链与资金方向", "semiconductor"),
    PageSpec(6, "大型科技与重点资产", "大科技、指数ETF与重点标的", "big_tech"),
    PageSpec(7, "事件日历与OPEX", "宏观数据、财报和衍生品时间点", "calendar"),
    PageSpec(8, "ETF资金流与市场结构", "资金方向、流动性和国债事件", "flows"),
    PageSpec(9, "GitHub热门AI项目", "开源项目、本周总结与后续验证", "github"),
]

DESIGN_RULES = {
    "canvas": "1080x1920 PNG",
    "safe_area": "四周8%—10%，正文不得进入安全区",
    "header": "数据页统一品牌栏、页码、主标题、一句结论和橙色装饰线",
    "hierarchy": "第一眼结论，第二眼数字与图形，第三眼说明与风险",
    "cards": "每页4—8个模块，圆角、边距和间距统一",
    "charts": "缺少历史序列时只能使用方向/逻辑示意，不得伪装为真实行情",
    "data": "文字、数字、单位、涨跌方向和来源只能来自输入JSON",
    "failure": "缺字段、冲突、无法核验、文本溢出或尺寸错误时整套禁止发布",
}
