"""Render the final notification from already validated market artifacts.

The renderer is intentionally a presentation-only boundary.  It does not
collect data, call a model, recalculate QA, or change delivery state.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


MISSING = "数据未提供"
STATUS_MISSING = "状态未提供"
INDEX_ORDER = ("VOO", "QQQM")
LEGACY_INDEX_ORDER = ("NDX", "SPX", "DJI")
INDEX_NAMES = {
    "VOO": "VOO（标普500 ETF）",
    "QQQM": "QQQM（纳斯达克100 ETF）",
    "NDX": "纳斯达克100",
    "SPX": "标普500",
    "DJI": "道琼斯",
}
TOKYO = ZoneInfo("Asia/Tokyo")


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip() or fallback


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _edition_title(edition: str) -> str:
    if edition == "morning_close_review":
        return "每日市场早盘报告"
    if edition == "evening_premarket_watch":
        return "每日市场晚间报告"
    return "每日市场报告"


def _session_label(content: dict[str, Any], manifest: dict[str, Any]) -> str:
    edition = _text(content.get("edition") or manifest.get("edition"))
    if edition == "morning_close_review":
        return "早盘"
    if edition == "evening_premarket_watch":
        return "晚间"
    return _text(content.get("session_name") or manifest.get("market_session"), "时段未提供")


def _report_date(content: dict[str, Any], manifest: dict[str, Any]) -> str:
    value = content.get("date") or manifest.get("report_date") or manifest.get("data_cutoff")
    return _text(value, MISSING)[:10] if value else MISSING


def _data_cutoff(content: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Format only the stored cutoff; never substitute the runtime clock."""
    raw = content.get("data_cutoff") or manifest.get("data_cutoff")
    if not raw:
        return "截止时间未提供"
    value = _text(raw)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return f"{value} JST"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TOKYO)
    return parsed.strftime("%Y-%m-%d %H:%M JST")


def _qa_status(manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    return _text(delivery_status.get("qa_status", manifest.get("qa_status")), "unknown").lower()


def _qa_label(status: str) -> str:
    return {"pass": "QA 已通过", "success": "QA 已通过", "fail": "QA 失败", "failed": "QA 失败"}.get(status, "QA 状态待确认")


def _qa_tone(status: str) -> str:
    return "good" if status in {"pass", "success"} else "bad" if status in {"fail", "failed"} else "muted"


def _source_count(manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    source_status = manifest.get("source_status") if isinstance(manifest.get("source_status"), dict) else {}
    value = delivery_status.get("source_count", source_status.get("source_count"))
    if value is None and isinstance(source_status.get("sources"), dict):
        value = len(source_status["sources"])
    return _text(value, MISSING)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _external_publish_enabled(manifest: dict[str, Any], delivery_status: dict[str, Any]) -> bool:
    if "external_publish_enabled" in delivery_status:
        return _as_bool(delivery_status["external_publish_enabled"])
    if "external_publish_enabled" in manifest:
        return _as_bool(manifest["external_publish_enabled"])
    return manifest.get("external_publish") not in {None, "removed", "disabled", "off"}


def _publish_feature_status(manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    return "已开启" if _external_publish_enabled(manifest, delivery_status) else "已关闭"


def _delivered_label(manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    return "已交付" if bool(delivery_status.get("delivered", manifest.get("delivered", False))) else "未执行"


def _short_conclusion(summary: Any) -> str:
    """Extract a short title from the existing conclusion; never invent facts."""
    value = _text(summary, MISSING)
    if value == MISSING:
        return value
    first_clause = re.split(r"[，。；：!?！？]", value, maxsplit=1)[0].strip()
    return first_clause[:24] if first_clause else MISSING


def _conclusion_title(content: dict[str, Any]) -> str:
    return _short_conclusion(content.get("summary"))


def _short_text(value: Any, limit: int = 52) -> str:
    text = _text(value, MISSING)
    if text == MISSING:
        return text
    sentence = re.split(r"[。！？!?]", text, maxsplit=1)[0].strip()
    if len(sentence) <= limit:
        return sentence
    return sentence[: max(1, limit - 1)] + "…"


def _index_status(index: dict[str, Any]) -> str:
    """Use only an explicit status; missing status must remain visible."""
    return _text(index.get("status"), STATUS_MISSING)


def _asset_section_title(indexes: list[dict[str, Any]]) -> str:
    symbols = {_text(item.get("ticker") or item.get("symbol")).upper() for item in indexes}
    return "核心资产表现" if symbols & set(INDEX_ORDER) else "指数表现"


def _index_key(index: dict[str, Any]) -> str | None:
    raw = _text(index.get("ticker") or index.get("index_code") or index.get("symbol")).upper()
    if raw in INDEX_NAMES:
        return raw
    name = _text(index.get("name") or index.get("display_name"))
    for key, label in INDEX_NAMES.items():
        if label in name:
            return key
    return None


def _ordered_indexes(indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    used: set[int] = set()
    # New production artifacts contain the two ETF proxies.  Keep the old
    # order only for historical reports and fixtures that explicitly contain
    # index symbols.
    order = INDEX_ORDER if any(_index_key(item) in set(INDEX_ORDER) for item in indexes) else LEGACY_INDEX_ORDER
    for key in order:
        for position, item in enumerate(indexes):
            if position not in used and _index_key(item) == key:
                ordered.append(item)
                used.add(position)
                break
    ordered.extend(item for position, item in enumerate(indexes) if position not in used)
    return ordered


def _numeric_change(value: Any) -> float | None:
    raw = _text(value).replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
    return float(match.group(0)) if match else None


def _direction_sign(value: Any) -> str | None:
    direction = _text(value).lower()
    if direction in {"up", "上涨", "上升", "positive", "gain", "inflow", "流入"}:
        return "up"
    if direction in {"down", "下跌", "下降", "negative", "loss", "outflow", "流出"}:
        return "down"
    if direction in {"neutral", "中性", "持平", "flat"}:
        return "neutral"
    return None


def _semantic_conflicts(indexes: list[dict[str, Any]]) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    for item in indexes:
        value = _numeric_change(item.get("change_percent"))
        direction = _direction_sign(item.get("direction"))
        if value is None or direction is None:
            continue
        actual = "up" if value > 0 else "down" if value < 0 else "neutral"
        if actual != direction:
            key = _index_key(item) or _text(item.get("name"), "unknown_index")
            conflicts.append({
                "field": f"major_indexes[{key}].direction",
                "direction": _text(item.get("direction")),
                "change_percent": _text(item.get("change_percent")),
                "expected_sign": actual,
            })
    return conflicts


def _direction_label(index: dict[str, Any]) -> str:
    value = _numeric_change(index.get("change_percent"))
    sign = _direction_sign(index.get("direction"))
    if sign is None and value is not None:
        sign = "up" if value > 0 else "down" if value < 0 else "neutral"
    if value is None and sign is None:
        return "— 方向未提供"
    return {"up": "↑ 上涨", "down": "↓ 下跌", "neutral": "— 中性"}.get(sign or "neutral", "— 方向未提供")


def _index_tone(index: dict[str, Any]) -> str:
    direction = _text(index.get("direction")).lower()
    if direction in {"up", "上涨", "上升", "positive", "gain"}:
        return "up"
    if direction in {"down", "下跌", "下降", "negative", "loss"}:
        return "down"
    if direction in {"neutral", "中性", "持平", "flat"}:
        return "neutral"
    raw = _text(index.get("change_percent"))
    if raw.startswith("+"):
        return "up"
    if raw.startswith("-"):
        return "down"
    try:
        numeric = float(raw.replace("%", ""))
    except ValueError:
        return "neutral"
    return "up" if numeric > 0 else "down" if numeric < 0 else "neutral"


def _technical_files(manifest: dict[str, Any], delivery_status: dict[str, Any]) -> list[tuple[str, str]]:
    files = delivery_status.get("files") if isinstance(delivery_status.get("files"), dict) else {}
    content_path = files.get("content") or delivery_status.get("content_path")
    manifest_path = files.get("manifest") or delivery_status.get("manifest_path")
    output_root = delivery_status.get("output_root") or manifest.get("output_root")
    return [
        ("完整简报", _text(content_path, "market_content.json")),
        ("运行报告", _text(manifest_path, "run_manifest.json")),
        ("运行目录", _text(output_root, "未记录")),
    ]


def _daily_sections(content: dict[str, Any]) -> list[dict[str, Any]]:
    sections = content.get("daily_sections")
    return [item for item in sections if isinstance(item, dict)] if isinstance(sections, list) else []


DAILY_MODULE_DEFINITIONS = (
    ("top_catalysts", "今日Top 3市场催化剂"),
    ("ai_semiconductors", "AI与半导体"),
    ("mega_tech", "大科技"),
    ("us_macro", "美国宏观"),
    ("global_central_banks", "全球央行"),
    ("geopolitics_policy", "地缘政治与政策"),
    ("index_rebalances", "指数调整"),
    ("etf_flows", "ETF调仓与资金流"),
    ("opex_derivatives", "OPEX与衍生品"),
    ("treasuries_liquidity", "美债与流动性"),
    ("oil_commodities", "原油与大宗商品"),
    ("ipo_financing", "IPO与融资"),
    ("breaking_news", "突发新闻"),
    ("github_ai_projects", "GitHub热门AI项目"),
    ("asset_impact", "对重点资产的影响"),
)


def _daily_module_entries(content: dict[str, Any]) -> list[tuple[int, str, dict[str, Any]]]:
    """Return all fixed modules without merging or inventing missing content."""
    by_id = {
        _text(item.get("section_id")): item
        for item in _daily_sections(content)
        if _text(item.get("section_id"))
    }
    entries: list[tuple[int, str, dict[str, Any]]] = []
    for number, (section_id, title) in enumerate(DAILY_MODULE_DEFINITIONS, start=1):
        item = by_id.get(section_id)
        if item is None:
            item = {
                "section_id": section_id,
                "title": title,
                "status": "unavailable",
                "content": "数据暂缺",
                "evidence": [],
            }
        entries.append((number, title, item))
    return entries


def _daily_section_status(value: Any) -> str:
    return {"available": "已覆盖", "partial": "部分覆盖", "unavailable": "数据暂缺"}.get(_text(value), "状态未提供")


def _daily_section_status_class(value: Any) -> str:
    return {"available": "available", "partial": "partial", "unavailable": "unavailable"}.get(_text(value), "unknown")


def _html_daily_sections(content: dict[str, Any]) -> str:
    entries = _daily_module_entries(content)
    nav = "".join(
        f'<a class="module-index__item" href="#daily-module-{number:02d}"><span>{number:02d}</span>{_html_escape(title)}</a>'
        for number, title, _ in entries
    )
    cards: list[str] = []
    for number, title, item in entries:
        status = _daily_section_status(item.get("status"))
        status_class = _daily_section_status_class(item.get("status"))
        evidence = [value for value in _list(item.get("evidence")) if _text(value)]
        evidence_html = ""
        if evidence:
            evidence_html = '<div class="daily-section__evidence"><span>来源证据</span>' + "".join(
                f'<div>{_html_escape(value)}</div>' for value in evidence[:3]
            ) + "</div>"
        cards.append(
            f'<article id="daily-module-{number:02d}" class="panel daily-section-card daily-section-card--{status_class}">'
            f'<div class="daily-section__top"><span class="daily-section__number">{number:02d}</span>'
            f'<span class="daily-section__status daily-section__status--{status_class}">{_html_escape(status)}</span></div>'
            f'<h3>{_html_escape(title)}</h3><p>{_html_escape(item.get("content"), MISSING)}</p>{evidence_html}</article>'
        )
    return (
        '<section class="daily-modules-section">'
        '<div class="daily-modules-header"><div><div class="eyebrow">完整内容</div>'
        '<h2 class="section-title">15个市场模块</h2><p class="section-subtitle">按固定顺序独立阅读，状态和来源证据随模块展示。</p></div>'
        '<div class="module-count">15 MODULES</div></div>'
        f'<nav class="module-index" aria-label="市场模块导航">{nav}</nav>'
        f'<div class="daily-modules">{"".join(cards)}</div>'
        '</section>'
    )


def _runtime_rows(content: dict[str, Any], manifest: dict[str, Any], delivery_status: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("内容 QA", _qa_label(_qa_status(manifest, delivery_status))),
        ("来源数量", _source_count(manifest, delivery_status)),
        ("外部发布功能", _publish_feature_status(manifest, delivery_status)),
        ("交付结果", _delivered_label(manifest, delivery_status)),
        ("报告日期", _report_date(content, manifest)),
        ("运行 ID", _text(delivery_status.get("run_id") or manifest.get("run_id"), MISSING)),
    ]


def _theme(theme: str) -> dict[str, str]:
    if theme not in {"light", "dark"}:
        raise ValueError("theme must be light or dark")
    if theme == "dark":
        return {"surface": "#111827", "card": "#1f2937", "border": "#374151", "text": "#f9fafb", "muted": "#9ca3af", "good": "#86efac", "bad": "#fca5a5", "accent": "#fbbf24", "up": "#fb6b4b", "down": "#86efac", "neutral": "#93c5fd"}
    return {"surface": "#f8fafc", "card": "#ffffff", "border": "#dbe3ec", "text": "#111827", "muted": "#64748b", "good": "#15803d", "bad": "#b91c1c", "accent": "#c2410c", "up": "#c2410c", "down": "#15803d", "neutral": "#1e3a5f"}


def _style(colors: dict[str, str], extra: str = "") -> str:
    return f"color:{colors['text']};{extra}"


def _card(title: str, body: str, colors: dict[str, str], tone: str = "muted") -> str:
    tone_color = colors.get(tone, colors["muted"])
    return f'<div style="background:{colors["card"]};border:1px solid {colors["border"]};border-radius:10px;padding:14px 16px;min-width:0;flex:1;color:{colors["text"]}"><div style="font-size:14px;color:{colors["muted"]};margin-bottom:7px">{title}</div><div style="font-size:26px;font-weight:750;color:{tone_color}">{body}</div></div>'


def _index_cards(indexes: list[dict[str, Any]], colors: dict[str, str]) -> str:
    cards: list[str] = []
    for item in indexes[:3]:
        name = _text(item.get("name") or item.get("display_name") or item.get("ticker"), MISSING)
        change = _text(item.get("change_percent"), MISSING)
        status = _index_status(item)
        direction = _direction_label(item)
        value_color = colors[_index_tone(item)]
        cards.append(
            f'<div style="background:{colors["card"]};border:1px solid {colors["border"]};border-radius:10px;padding:14px 16px;flex:1;min-width:150px;color:{colors["text"]}">'
            f'<div style="font-size:14px;color:{colors["muted"]}">{name}</div>'
            f'<div style="font-size:32px;line-height:1.1;font-weight:800;color:{value_color}">{change}</div>'
            f'<div style="font-size:14px;color:{value_color}">{direction}</div>'
            f'<div style="font-size:12px;color:{colors["muted"]}">{status}</div></div>'
        )
    while len(cards) < 3:
        cards.append(_card(MISSING, MISSING, colors))
    return '<div style="display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 18px">' + "".join(cards) + "</div>"


def _signal_cards(signals: list[dict[str, Any]], risks: list[str], colors: dict[str, str]) -> str:
    driver_items = [_short_text(item.get("heading"), 30) for item in signals[:3] if isinstance(item, dict)]
    if not driver_items:
        driver_items = [MISSING]
    risk_items = [_short_text(item, 48) for item in risks[:3]] or [MISSING]
    driver_body = "".join(f"<div style='margin:6px 0'>• {item}</div>" for item in driver_items)
    risk_body = "".join(f"<div style='margin:6px 0'>• {item}</div>" for item in risk_items)
    return (
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 18px">'
        f'<div style="background:{colors["card"]};border:1px solid {colors["border"]};border-radius:10px;padding:14px 16px;flex:1;min-width:260px;color:{colors["text"]}"><div style="font-weight:750;color:{colors["up"]};margin-bottom:8px">↑ 市场驱动</div>{driver_body}</div>'
        f'<div style="background:{colors["card"]};border:1px solid {colors["border"]};border-radius:10px;padding:14px 16px;flex:1;min-width:260px;color:{colors["text"]}"><div style="font-weight:750;color:{colors["down"]};margin-bottom:8px">↓ 主要风险</div>{risk_body}</div>'
        '</div>'
    )


def _ai_card(view: dict[str, Any], colors: dict[str, str]) -> str:
    stance = _text(view.get("stance"), MISSING)
    thesis = _text(view.get("thesis"), MISSING)
    action = _text(view.get("action"), MISSING)
    disclaimer = _text(view.get("disclaimer"), "仅作信息整理与情景分析，不构成个性化投资建议。")
    return (
        f'<div style="background:{colors["card"]};border:1px solid {colors["border"]};border-radius:10px;padding:16px;color:{colors["text"]};margin:8px 0 16px">'
        f'<div style="font-weight:750;margin-bottom:10px">AI 投资观察 <span style="display:inline-block;border:1px solid {colors["accent"]};border-radius:999px;padding:3px 9px;color:{colors["accent"]};font-size:14px;margin-left:6px">{stance}</span></div>'
        f'<div style="margin:7px 0"><span style="color:{colors["muted"]}">观点：</span>{thesis}</div>'
        f'<div style="margin:7px 0"><span style="color:{colors["muted"]}">观察框架：</span>{action}</div>'
        f'<div style="font-size:12px;color:{colors["muted"]};margin-top:12px">{disclaimer}</div></div>'
    )


def _technical_details(content: dict[str, Any], manifest: dict[str, Any], delivery_status: dict[str, Any], colors: dict[str, str]) -> str:
    rows = "".join(
        f'<div style="display:flex;gap:16px;padding:4px 0"><span style="width:120px;color:{colors["muted"]}">{name}</span><span>{value}</span></div>'
        for name, value in _runtime_rows(content, manifest, delivery_status)
    )
    files = _technical_files(manifest, delivery_status)
    paths = "".join(
        f'<div style="padding:4px 0"><span style="color:{colors["muted"]}">{label}：</span><strong><code>{Path(path).name if path else MISSING}</code></strong><br><code style="font-size:11px;overflow-wrap:anywhere;color:{colors["muted"]}">{path}</code></div>'
        for label, path in files
    )
    return (
        f'<details style="margin-top:12px;color:{colors["text"]}"><summary style="cursor:pointer;font-weight:650">运行与技术信息 ▾</summary>'
        f'<div style="border-top:1px solid {colors["border"]};margin-top:10px;padding-top:10px;font-size:14px">{rows}'
        f'<div style="border-top:1px solid {colors["border"]};margin-top:10px;padding-top:10px"><div style="color:{colors["muted"]};margin-bottom:5px">相关文件</div>{paths}</div></div></details>'
    )


def _render_error_report(
    content: dict[str, Any],
    manifest: dict[str, Any],
    delivery_status: dict[str, Any],
    theme: str,
    semantic_conflicts: list[dict[str, str]] | None = None,
) -> str:
    status = _qa_status(manifest, delivery_status)
    failed_step = _text(delivery_status.get("failed_step") or manifest.get("failed_step"), MISSING)
    reason = _text(delivery_status.get("error_reason") or delivery_status.get("error_message"), MISSING)
    missing = delivery_status.get("missing_fields") or manifest.get("missing_fields") or []
    log_path = _text(delivery_status.get("log_path"), MISSING)
    colors = _theme(theme)
    lines = [
        f"# {_edition_title(_text(content.get('edition') or manifest.get('edition')))}",
        f'<div style="background:{colors["surface"]};border:1px solid {colors["bad"]};border-radius:10px;padding:14px;color:{colors["text"]}"><strong>生成失败</strong><br><span style="color:{colors["muted"]}">QA 状态：{_qa_label(status)}</span></div>',
        "",
        "**失败阶段**：" + failed_step,
        "",
        "**错误原因**：" + reason,
        "",
        "**缺失或冲突字段**：" + (", ".join(map(str, missing)) if missing else MISSING),
    ]
    if semantic_conflicts:
        lines.extend([
            "",
            "## 资产语义冲突",
            *[
                f"- 字段：`{item['field']}`；方向值：`{item['direction']}`；涨跌幅：`{item['change_percent']}`；应为：`{item['expected_sign']}`"
                for item in semantic_conflicts
            ],
        ])
    lines.extend([
        "",
        "<details><summary>运行与技术信息 ▾</summary>",
        "",
        "- 运行目录：`" + _text(delivery_status.get("output_root") or manifest.get("output_root"), MISSING) + "`",
        "- 日志文件：`" + log_path + "`",
        "</details>",
        "",
    ])
    return "\n".join(lines)


def _render_plain_report(content: dict[str, Any], manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    edition = _text(content.get("edition") or manifest.get("edition"))
    indexes = [item for item in _list(content.get("major_indexes")) if isinstance(item, dict)]
    signals = [item for item in _list(content.get("analysis_text", {}).get("sections") if isinstance(content.get("analysis_text"), dict) else []) if isinstance(item, dict)]
    risks = [_text(item) for item in _list(content.get("risk_factors")) if _text(item)]
    view = content.get("ai_investment_view") if isinstance(content.get("ai_investment_view"), dict) else {}
    lines = [
        f"# {_edition_title(edition)}",
        f"{_report_date(content, manifest)} · {_session_label(content, manifest)}",
        f"[{_qa_label(_qa_status(manifest, delivery_status))}] [{_source_count(manifest, delivery_status)} 个来源] [未发布]",
        "",
        _conclusion_title(content),
        _text(content.get("summary"), MISSING),
        "",
        f"## {_asset_section_title(indexes)}",
    ]
    for item in _ordered_indexes(indexes)[:3]:
        lines.append(f"- {_text(item.get('name') or item.get('display_name'), MISSING)}：{_text(item.get('change_percent'), MISSING)}（{_direction_label(item)}；{_index_status(item)}）")
    if not indexes:
        lines.append(f"- {MISSING}")
    driver_lines = [f"- {_short_text(item.get('heading'), 48)}" for item in signals[:3]] or [f"- {MISSING}"]
    risk_lines = [f"- {_short_text(item, 60)}" for item in risks[:3]] or [f"- {MISSING}"]
    lines.extend(["", "## 市场驱动", *driver_lines, "", "## 主要风险", *risk_lines, "", "## AI 投资观察"])
    if view:
        lines.extend([f"- 观点：{_text(view.get('stance'), MISSING)}", f"- 结论：{_text(view.get('thesis'), MISSING)}", f"- 观察框架：{_text(view.get('action'), MISSING)}"])
    else:
        lines.append(f"- {MISSING}")
    lines.extend(["", "## 15个市场模块"])
    for number, title, item in _daily_module_entries(content):
        lines.extend([
            "",
            f"### {number:02d}｜{_text(title, MISSING)}",
            f"**状态：** {_daily_section_status(item.get('status'))}",
            _text(item.get("content"), MISSING),
        ])
        evidence = [value for value in _list(item.get("evidence")) if _text(value)]
        if evidence:
            lines.append("**来源证据：** " + "；".join(_text(value) for value in evidence[:3]))
    lines.extend(["", "<details><summary>运行与技术信息 ▾</summary>", ""])
    lines.extend(f"- {name}：{value}" for name, value in _runtime_rows(content, manifest, delivery_status))
    lines.append("</details>")
    return "\n".join(lines) + "\n"


def render_delivery_report(
    content: dict[str, Any],
    manifest: dict[str, Any],
    delivery_status: dict[str, Any] | None = None,
    *,
    theme: str = "light",
    rich_text: bool = True,
) -> str:
    """Render a QA-gated notification without changing any business data."""
    delivery_status = dict(delivery_status or {})
    qa_status = _qa_status(manifest, delivery_status)
    indexes = [item for item in _list(content.get("major_indexes")) if isinstance(item, dict)]
    semantic_conflicts = _semantic_conflicts(indexes)
    if qa_status not in {"pass", "success"}:
        return _render_error_report(content, manifest, delivery_status, theme)
    if semantic_conflicts:
        return _render_error_report(
            content,
            manifest,
            {**delivery_status, "failed_step": "render_semantic_validation", "error_reason": "指数数值与涨跌方向冲突"},
            theme,
            semantic_conflicts,
        )
    if not rich_text:
        return _render_plain_report(content, manifest, delivery_status)

    colors = _theme(theme)
    edition = _text(content.get("edition") or manifest.get("edition"))
    signals = [item for item in _list(content.get("analysis_text", {}).get("sections") if isinstance(content.get("analysis_text"), dict) else []) if isinstance(item, dict)]
    risks = [_text(item) for item in _list(content.get("risk_factors")) if _text(item)]
    view = content.get("ai_investment_view") if isinstance(content.get("ai_investment_view"), dict) else {}
    status = _qa_label(qa_status)
    source_count = _source_count(manifest, delivery_status)
    delivered = _delivered_label(manifest, delivery_status)
    return "\n".join([
        f'<section data-theme="{theme}" style="color:{colors["text"]};background:{colors["surface"]};border:1px solid {colors["border"]};border-radius:12px;padding:18px;max-width:960px">',
        f'<div style="font-size:24px;font-weight:800;line-height:1.2">{_edition_title(edition)}</div>',
        f'<div style="font-size:14px;color:{colors["muted"]};margin-top:5px">{_report_date(content, manifest)} · {_session_label(content, manifest)} · 数据截止：{_data_cutoff(content, manifest)}</div>',
        f'<div style="margin:12px 0 18px"><span style="border:1px solid {colors[_qa_tone(qa_status)]};border-radius:999px;padding:4px 9px;color:{colors[_qa_tone(qa_status)]};font-size:13px">{status}</span> <span style="border:1px solid {colors["border"]};border-radius:999px;padding:4px 9px;color:{colors["muted"]};font-size:13px">{source_count} 个来源</span> <span style="border:1px solid {colors["border"]};border-radius:999px;padding:4px 9px;color:{colors["muted"]};font-size:13px">未发布</span></div>',
        f'<div style="border-left:4px solid {colors["accent"]};padding:3px 0 3px 14px;margin-bottom:18px"><div style="font-size:22px;font-weight:800">{_conclusion_title(content)}</div><div style="margin-top:6px;line-height:1.6">{_text(content.get("summary"), MISSING)}</div></div>',
        f'<div style="font-size:17px;font-weight:750;margin:8px 0">{escape(_asset_section_title(indexes))}</div>',
        _index_cards(_ordered_indexes(indexes), colors),
        _signal_cards(signals, risks, colors),
        _ai_card(view, colors),
        _technical_details(content, manifest, delivery_status, colors),
        '</section>',
        "",
    ])


HTML_CSS = """
:root {
  color-scheme: light dark;
  --report-bg: #f8fafc;
  --report-card: #ffffff;
  --report-border: #dbe3ec;
  --report-text: #111827;
  --report-muted: #64748b;
  --report-good: #15803d;
  --report-bad: #b91c1c;
  --report-accent: #b45309;
  --report-up: #c2410c;
  --report-down: #15803d;
  --report-neutral: #1e3a5f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --report-bg: #111827;
    --report-card: #1f2937;
    --report-border: #374151;
    --report-text: #f9fafb;
    --report-muted: #9ca3af;
    --report-good: #86efac;
    --report-bad: #fca5a5;
    --report-accent: #fbbf24;
    --report-up: #fb6b4b;
    --report-down: #86efac;
    --report-neutral: #93c5fd;
  }
}
[data-theme="light"] {
  --report-bg: #f8fafc;
  --report-card: #ffffff;
  --report-border: #dbe3ec;
  --report-text: #111827;
  --report-muted: #64748b;
  --report-good: #15803d;
  --report-bad: #b91c1c;
  --report-accent: #b45309;
  --report-up: #c2410c;
  --report-down: #15803d;
  --report-neutral: #1e3a5f;
}
[data-theme="dark"] {
  --report-bg: #111827;
  --report-card: #1f2937;
  --report-border: #374151;
  --report-text: #f9fafb;
  --report-muted: #9ca3af;
  --report-good: #86efac;
  --report-bad: #fca5a5;
  --report-accent: #fbbf24;
  --report-up: #fb6b4b;
  --report-down: #86efac;
  --report-neutral: #93c5fd;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 28px 18px;
  background: var(--report-bg);
  color: var(--report-text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", sans-serif;
  line-height: 1.55;
}
.market-report { max-width: 1000px; margin: 0 auto; }
.report-header { margin-bottom: 22px; }
.report-title { margin: 0; font-size: clamp(24px, 4vw, 32px); letter-spacing: .01em; }
.report-meta { color: var(--report-muted); font-size: 14px; margin-top: 4px; }
.status-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.status-badge { display: inline-flex; align-items: center; min-height: 28px; padding: 3px 10px; border: 1px solid var(--report-border); border-radius: 999px; color: var(--report-muted); font-size: 13px; }
.status-badge--good { color: var(--report-good); border-color: var(--report-good); }
.status-badge--bad { color: var(--report-bad); border-color: var(--report-bad); }
.panel { background: var(--report-card); border: 1px solid var(--report-border); border-radius: 12px; }
.conclusion { border-left: 4px solid var(--report-accent); padding: 18px 20px; margin-bottom: 22px; }
.conclusion-title { margin: 0; font-size: clamp(22px, 3.4vw, 30px); line-height: 1.25; }
.conclusion-text { margin: 8px 0 0; }
.section-title { margin: 0 0 10px; font-size: 18px; }
.index-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 22px; }
.index-card { padding: 16px; min-height: 132px; display: flex; flex-direction: column; justify-content: space-between; }
.index-name { color: var(--report-muted); font-size: 14px; }
.index-change { font-size: clamp(30px, 5vw, 48px); font-weight: 800; letter-spacing: -.02em; line-height: 1.1; }
.index-change--up { color: var(--report-up); }
.index-change--down { color: var(--report-down); }
.index-change--neutral { color: var(--report-neutral); }
.index-status { color: var(--report-muted); font-size: 14px; }
.index-status--up { color: var(--report-up); }
.index-status--down { color: var(--report-down); }
.index-status--neutral { color: var(--report-neutral); }
.signal-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 22px; }
.signal-panel { padding: 16px; }
.signal-heading { margin: 0 0 10px; font-size: 16px; }
.signal-heading--good { color: var(--report-up); }
.signal-heading--risk { color: var(--report-down); }
.signal-item { display: grid; grid-template-columns: 12px minmax(0, 1fr); gap: 8px; margin: 8px 0; }
.signal-marker { color: var(--report-accent); }
.signal-item__heading { font-weight: 700; }
.signal-item__text { color: var(--report-muted); font-size: 14px; }
.ai-panel { padding: 18px; margin-bottom: 22px; }
.ai-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 0 0 12px; font-size: 18px; }
.stance { border: 1px solid var(--report-accent); border-radius: 999px; padding: 3px 10px; color: var(--report-accent); font-size: 13px; font-weight: 600; }
.ai-row { margin: 8px 0; }
.label { color: var(--report-muted); }
.disclaimer { color: var(--report-muted); font-size: 12px; margin-top: 14px; }
.daily-modules-section { border-top: 1px solid var(--report-border); padding-top: 22px; margin-bottom: 24px; }
.daily-modules-header { display: flex; justify-content: space-between; align-items: end; gap: 18px; margin-bottom: 14px; }
.eyebrow { color: var(--report-accent); font-size: 12px; font-weight: 750; letter-spacing: .12em; text-transform: uppercase; }
.section-subtitle { color: var(--report-muted); margin: -4px 0 0; font-size: 14px; }
.module-count { color: var(--report-muted); font: 700 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .08em; white-space: nowrap; }
.module-index { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px; padding: 10px; margin-bottom: 14px; background: var(--report-card); border: 1px solid var(--report-border); border-radius: 10px; }
.module-index__item { display: flex; align-items: center; gap: 7px; min-width: 0; padding: 7px 8px; color: var(--report-muted); text-decoration: none; font-size: 12px; line-height: 1.25; }
.module-index__item:hover { color: var(--report-text); background: var(--report-bg); border-radius: 6px; }
.module-index__item span { color: var(--report-accent); font: 750 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; }
.daily-modules { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.daily-section-card { min-height: 156px; padding: 16px 18px; scroll-margin-top: 18px; }
.daily-section-card:first-child { grid-column: 1 / -1; }
.daily-section-card--available { border-top: 3px solid var(--report-good); }
.daily-section-card--partial { border-top: 3px solid var(--report-accent); }
.daily-section-card--unavailable, .daily-section-card--unknown { border-top: 3px solid var(--report-border); }
.daily-section__top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.daily-section__number { color: var(--report-accent); font: 800 16px/1 ui-monospace, SFMono-Regular, Menlo, monospace; }
.daily-section__status { padding: 3px 8px; border: 1px solid var(--report-border); border-radius: 999px; color: var(--report-muted); font-size: 11px; white-space: nowrap; }
.daily-section__status--available { color: var(--report-good); border-color: var(--report-good); }
.daily-section__status--partial { color: var(--report-accent); border-color: var(--report-accent); }
.daily-section-card h3 { margin: 14px 0 7px; font-size: 17px; line-height: 1.25; }
.daily-section-card p { margin: 0; color: var(--report-text); white-space: pre-wrap; overflow-wrap: anywhere; }
.daily-section__evidence { margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--report-border); color: var(--report-muted); font-size: 12px; overflow-wrap: anywhere; }
.daily-section__evidence span { display: block; margin-bottom: 4px; color: var(--report-accent); font-weight: 700; }
.technical { border-top: 1px solid var(--report-border); padding-top: 14px; }
.technical summary { cursor: pointer; color: var(--report-text); font-weight: 700; }
.technical-body { border-top: 1px solid var(--report-border); margin-top: 10px; padding-top: 12px; }
.kv-list { display: grid; grid-template-columns: minmax(150px, 0.3fr) minmax(0, 1fr); gap: 7px 18px; font-size: 14px; }
.kv-key { color: var(--report-muted); }
.file-list { border-top: 1px solid var(--report-border); margin-top: 14px; padding-top: 12px; }
.file-item { padding: 6px 0; }
.file-item a { color: var(--report-text); text-decoration: none; border-bottom: 1px dotted var(--report-muted); }
.file-path { display: block; color: var(--report-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
.error-panel { border-left: 4px solid var(--report-bad); padding: 18px 20px; }
.error-title { color: var(--report-bad); font-size: 22px; font-weight: 800; }
@media (max-width: 720px) {
  body { padding: 18px 12px; }
  .index-grid, .signal-grid { grid-template-columns: 1fr; }
  .index-card { min-height: 112px; }
  .daily-modules-header { align-items: start; }
  .module-index { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .daily-modules { grid-template-columns: 1fr; }
  .daily-section-card:first-child { grid-column: auto; }
  .kv-list { grid-template-columns: 1fr; gap: 2px; }
  .kv-key { margin-top: 6px; }
}
"""


def _html_escape(value: Any, fallback: str = MISSING) -> str:
    return escape(_text(value, fallback), quote=True)


def _html_status_badge(label: str, tone: str = "muted") -> str:
    modifier = f" status-badge--{tone}" if tone in {"good", "bad"} else ""
    return f'<span class="status-badge{modifier}">{_html_escape(label)}</span>'


def _html_index_cards(indexes: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    color_class = {"up": "up", "down": "down", "neutral": "neutral"}
    for item in _ordered_indexes(indexes)[:3]:
        tone = color_class[_index_tone(item)]
        name = item.get("name") or item.get("display_name") or item.get("ticker")
        status = _index_status(item)
        direction = _direction_label(item)
        cards.append(
            f'<article class="panel index-card"><div class="index-name">{_html_escape(name)}</div>'
            f'<div class="index-change index-change--{tone}">{_html_escape(item.get("change_percent"))}</div>'
            f'<div class="index-status index-status--{tone}">{_html_escape(direction)}</div>'
            f'<div class="index-status">{_html_escape(status)}</div></article>'
        )
    if not cards:
        return '<div class="index-grid"><article class="panel index-card"><div class="index-name">核心资产表现</div><div class="index-change index-change--neutral">数据未提供</div><div class="index-status">数据未提供</div></article></div>'
    return '<div class="index-grid">' + "".join(cards) + "</div>"


def _html_signal_panel(title: str, signals: list[dict[str, Any]], risks: list[str], risk: bool = False) -> str:
    items: list[str] = []
    if risk:
        items = [f'<div class="signal-item"><span class="signal-marker">!</span><div>{_html_escape(item)}</div></div>' for item in risks[:3]]
    else:
        for item in signals[:3]:
            heading = _html_escape(item.get("heading"))
            body = _html_escape(_short_text(item.get("content"), 110))
            items.append(f'<div class="signal-item"><span class="signal-marker">•</span><div><div class="signal-item__heading">{heading}</div><div class="signal-item__text">{body}</div></div></div>')
    if not items:
        items = [f'<div class="signal-item"><span class="signal-marker">•</span><div>{MISSING}</div></div>']
    heading_class = "signal-heading--risk" if risk else "signal-heading--good"
    return f'<section class="panel signal-panel"><h2 class="signal-heading {heading_class}">{_html_escape(title)}</h2>{"".join(items)}</section>'


def _html_technical_details(content: dict[str, Any], manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    rows = "".join(f'<div class="kv-key">{_html_escape(name)}</div><div>{_html_escape(value)}</div>' for name, value in _runtime_rows(content, manifest, delivery_status))
    files = "".join(
        f'<div class="file-item"><a href="{_html_escape(_relative_artifact_href(path, manifest, delivery_status))}"><strong>{_html_escape(Path(path).name if path else MISSING)}</strong></a><span class="file-path">{_html_escape(path)}</span></div>'
        for _, path in _technical_files(manifest, delivery_status)
    )
    return f'<details class="technical"><summary>运行与技术信息</summary><div class="technical-body"><div class="kv-list">{rows}</div><div class="file-list"><div class="label">相关文件</div>{files}</div></div></details>'


def _relative_artifact_href(path: str, manifest: dict[str, Any], delivery_status: dict[str, Any]) -> str:
    """Return a safe relative href from the delivery directory."""
    output_root = _text(delivery_status.get("output_root") or manifest.get("output_root"))
    if not output_root or not path or path == MISSING:
        return "#"
    try:
        base = Path(output_root).resolve() / "delivery"
        target = Path(path).resolve()
        return Path(os.path.relpath(target, base)).as_posix()
    except (OSError, ValueError):
        return "#"


def render_delivery_report_html(
    content: dict[str, Any],
    manifest: dict[str, Any],
    delivery_status: dict[str, Any] | None = None,
    *,
    theme: str = "auto",
) -> str:
    """Render a standalone, dependency-free HTML delivery report."""
    if theme not in {"auto", "light", "dark"}:
        raise ValueError("theme must be auto, light, or dark")
    delivery_status = dict(delivery_status or {})
    qa_status = _qa_status(manifest, delivery_status)
    theme_attr = "" if theme == "auto" else f' data-theme="{theme}"'
    title = _html_escape(_edition_title(_text(content.get("edition") or manifest.get("edition"))))
    meta = f'{_html_escape(_report_date(content, manifest))} · {_html_escape(_session_label(content, manifest))} · 数据截止：{_html_escape(_data_cutoff(content, manifest))}'
    source_count = _html_escape(_source_count(manifest, delivery_status))
    indexes = [item for item in _list(content.get("major_indexes")) if isinstance(item, dict)]
    semantic_conflicts = _semantic_conflicts(indexes)

    if qa_status not in {"pass", "success"} or semantic_conflicts:
        failure_status = "fail" if semantic_conflicts else qa_status
        failed_step_value = "render_semantic_validation" if semantic_conflicts else (delivery_status.get("failed_step") or manifest.get("failed_step"))
        reason_value = "指数数值与涨跌方向冲突" if semantic_conflicts else (delivery_status.get("error_reason") or delivery_status.get("error_message"))
        failed_step = _html_escape(failed_step_value)
        reason = _html_escape(reason_value)
        missing = delivery_status.get("missing_fields") or manifest.get("missing_fields") or []
        conflict_rows = "".join(
            f'<li><code>{_html_escape(item["field"])}</code>：方向值 <code>{_html_escape(item["direction"])}</code>，涨跌幅 <code>{_html_escape(item["change_percent"])}</code>，应为 <code>{_html_escape(item["expected_sign"])}</code></li>'
            for item in semantic_conflicts
        )
        conflict_block = f'<h2 class="section-title">资产语义冲突</h2><ul>{conflict_rows}</ul>' if semantic_conflicts else ""
        body = f'<section class="market-report"><header class="report-header"><h1 class="report-title">{title}</h1><div class="report-meta">{meta}</div><div class="status-bar">{_html_status_badge("QA 失败", "bad")}</div></header><section class="panel error-panel"><div class="error-title">生成失败</div><p><span class="label">失败阶段：</span>{failed_step}</p><p><span class="label">错误原因：</span>{reason}</p><p><span class="label">缺失或冲突字段：</span>{_html_escape(", ".join(map(str, missing)) if missing else MISSING)}</p>{conflict_block}</section>{_html_technical_details(content, manifest, delivery_status)}</section>'
    else:
        signals = [item for item in _list(content.get("analysis_text", {}).get("sections") if isinstance(content.get("analysis_text"), dict) else []) if isinstance(item, dict)]
        risks = [_text(item) for item in _list(content.get("risk_factors")) if _text(item)]
        view = content.get("ai_investment_view") if isinstance(content.get("ai_investment_view"), dict) else {}
        qa_tone = _qa_tone(qa_status)
        stance = _html_escape(view.get("stance"))
        body = (
            f'<section class="market-report"><header class="report-header"><h1 class="report-title">{title}</h1><div class="report-meta">{meta}</div><div class="status-bar">'
            f'{_html_status_badge(_qa_label(qa_status), qa_tone)}{_html_status_badge(f"{source_count} 个来源")}{_html_status_badge("已交付" if _delivered_label(manifest, delivery_status) == "已交付" else "未发布")}</div></header>'
            f'<section class="panel conclusion"><h2 class="conclusion-title">{_html_escape(_conclusion_title(content))}</h2><p class="conclusion-text">{_html_escape(content.get("summary"))}</p></section>'
            f'<section><h2 class="section-title">{escape(_asset_section_title(indexes))}</h2>{_html_index_cards(indexes)}</section>'
            f'<section class="signal-grid">{_html_signal_panel("↑ 市场驱动", signals, risks)}{_html_signal_panel("↓ 主要风险", signals, risks, risk=True)}</section>'
            f'<section class="panel ai-panel"><h2 class="ai-heading">AI 投资观察 <span class="stance">{stance}</span></h2><p class="ai-row"><span class="label">观点：</span>{_html_escape(view.get("thesis"))}</p><p class="ai-row"><span class="label">观察框架：</span>{_html_escape(view.get("action"))}</p><p class="disclaimer">{_html_escape(view.get("disclaimer"), "仅作信息整理与情景分析，不构成个性化投资建议。")}</p></section>'
            f'{_html_daily_sections(content)}'
            f'{_html_technical_details(content, manifest, delivery_status)}</section>'
        )
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark"><link rel="icon" href="data:,"><title>{title}</title><style>{HTML_CSS}</style></head><body{theme_attr}>{body}</body></html>\n'


__all__ = ["render_delivery_report", "render_delivery_report_html"]
