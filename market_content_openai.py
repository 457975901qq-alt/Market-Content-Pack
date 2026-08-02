#!/usr/bin/env python3
"""Generate and validate daily market content as strict JSON."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from edition_profiles import EditionContext, resolve_edition_context
from model_providers import ProviderError, call_gemini, call_ollama


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("MARKET_CONTENT_OUTPUT_DIR", str(ROOT / "outputs" / "market_content"))).expanduser().resolve()
OUTPUT_JSON = OUTPUT_DIR / "market_content.json"
PLATFORM_COPY_MD = OUTPUT_DIR / "douyin.md"
ERROR_LOG = Path(os.environ.get("MARKET_CONTENT_ERROR_LOG", str(ROOT / "logs" / "market_content_errors.log"))).expanduser().resolve()
TOKYO = ZoneInfo("Asia/Tokyo")

OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
INVESTMENT_DISCLAIMER = "仅作信息整理与情景分析，不构成个性化投资建议。"
_INVESTMENT_STANCES = ["偏积极观察", "中性观察", "偏谨慎", "数据不足"]
_INVESTMENT_ACTIONS = ["观察", "等待验证", "控制风险", "数据不足"]
_MARKET_REGIMES = ["risk_on", "risk_off", "mixed", "insufficient_data"]


MARKET_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "date",
        "timezone",
        "edition",
        "prompt_version",
        "data_cutoff",
        "scheduled_local_time",
        "source_window_start",
        "source_window_end",
        "market_session",
        "edition_fields",
        "summary",
        "key_points",
        "major_indexes",
        "important_stocks",
        "macro_events",
        "earnings",
        "risk_factors",
        "analysis_text",
        "ai_investment_view",
        "douyin",
    ],
    "properties": {
        "date": {"type": "string"},
        "timezone": {"type": "string", "enum": ["Asia/Tokyo"]},
        "edition": {"type": "string", "enum": ["morning_close_review", "evening_premarket_watch"]},
        "prompt_version": {"type": "string", "minLength": 1},
        "data_cutoff": {"type": "string", "format": "date-time"},
        "scheduled_local_time": {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$"},
        "source_window_start": {"type": "string", "format": "date-time"},
        "source_window_end": {"type": "string", "format": "date-time"},
        "market_session": {"type": "string", "enum": ["close_review", "premarket_watch"]},
        "edition_fields": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "major_indexes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "ticker", "change_percent", "reason"],
                "properties": {
                    "name": {"type": "string"},
                    "ticker": {"type": "string"},
                    "change_percent": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "important_stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "ticker", "change_percent", "reason"],
                "properties": {
                    "name": {"type": "string"},
                    "ticker": {"type": "string"},
                    "change_percent": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "macro_events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event", "date", "importance", "impact"],
                "properties": {
                    "event": {"type": "string"},
                    "date": {"type": "string"},
                    "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                    "impact": {"type": "string"},
                },
            },
        },
        "earnings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["company", "ticker", "date", "importance"],
                "properties": {
                    "company": {"type": "string"},
                    "ticker": {"type": "string"},
                    "date": {"type": "string"},
                    "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "risk_factors": {"type": "array", "items": {"type": "string"}},
        "analysis_text": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "subtitle", "sections"],
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["heading", "content"],
                        "properties": {
                            "heading": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
            },
        },
        "ai_investment_view": {
            "type": "object",
            "additionalProperties": False,
            "required": ["market_environment", "stance", "action", "thesis", "evidence", "risks", "invalidation_conditions", "suggestions", "disclaimer"],
            "properties": {
                "market_environment": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["regime", "summary", "confidence", "signals"],
                    "properties": {
                        "regime": {"type": "string", "enum": _MARKET_REGIMES},
                        "summary": {"type": "string", "minLength": 1},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "signals": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    },
                },
                "stance": {"type": "string", "enum": _INVESTMENT_STANCES},
                "action": {"type": "string", "enum": _INVESTMENT_ACTIONS},
                "thesis": {"type": "string", "minLength": 1},
                "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "risks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "invalidation_conditions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "suggestions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
                "disclaimer": {"type": "string", "const": INVESTMENT_DISCLAIMER},
            },
        },
        "douyin": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "cover_title", "caption", "hashtags"],
            "properties": {
                "title": {"type": "string"},
                "cover_title": {"type": "string"},
                "caption": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


class MarketContentError(Exception):
    def __init__(self, error_type: str, message: str, raw_response: str = "", failure_position: str = ""):
        super().__init__(message)
        self.error_type = error_type
        self.raw_response = raw_response
        self.failure_position = failure_position


def today_tokyo(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(TOKYO)).astimezone(TOKYO).date().isoformat()


def log_market_error(error_type: str, raw_response: str, failure_position: str, exc: BaseException | None = None) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "error_type": error_type,
        "failure_position": failure_position,
        "raw_response": raw_response,
    }
    with ERROR_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if exc is not None:
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            fh.write("\n")


def clear_outputs_on_failure() -> None:
    for path in [OUTPUT_JSON, PLATFORM_COPY_MD]:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log_market_error("cleanup_failed", str(path), "clear_outputs_on_failure", exc)


def _edition_context(edition: str | None = None, started_at: dt.datetime | None = None) -> EditionContext:
    selected = edition or os.environ.get("MARKET_EDITION", "").strip()
    if not selected:
        raise MarketContentError("edition_missing", "An explicit edition is required.", "", "edition")
    try:
        return resolve_edition_context(selected, started_at=started_at)
    except ValueError as exc:
        raise MarketContentError("edition_invalid", str(exc), "", "edition") from exc


def build_prompt(market_context: str, context: EditionContext | None = None) -> str:
    context = context or _edition_context()
    execution_context = {
        "edition": context.edition,
        "market_session": context.market_session,
        "data_cutoff": context.scheduled_cutoff.isoformat(),
        "source_window_start": context.source_window_start.isoformat(),
        "source_window_end": context.source_window_end.isoformat(),
        "scheduled_local_time": context.scheduled_local_time,
        "version_fields": list(context.version_fields),
        "focus": context.focus,
    }
    output_contract = {
        "date": context.scheduled_cutoff.astimezone(TOKYO).date().isoformat(),
        "timezone": "Asia/Tokyo",
        "edition": context.edition,
        "prompt_version": context.prompt_version,
        "data_cutoff": context.scheduled_cutoff.isoformat(),
        "scheduled_local_time": context.scheduled_local_time,
        "source_window_start": context.source_window_start.isoformat(),
        "source_window_end": context.source_window_end.isoformat(),
        "market_session": context.market_session,
        "edition_fields": {field: "string" for field in context.version_fields},
        "summary": "string",
        "key_points": ["string"],
        "major_indexes": [{"name": "string", "ticker": "SPX|NDX|DJI", "change_percent": "string", "reason": "string"}],
        "important_stocks": [{"name": "string", "ticker": "string", "change_percent": "string", "reason": "string"}],
        "macro_events": [],
        "earnings": [],
        "risk_factors": ["string"],
        "analysis_text": {"title": "string", "subtitle": "string", "sections": [{"heading": "string", "content": "string"}]},
        "ai_investment_view": {
            "market_environment": {"regime": "risk_on|risk_off|mixed|insufficient_data", "summary": "当前市场环境", "confidence": 0.0, "signals": ["已验证信号"]},
            "stance": "偏积极观察|中性观察|偏谨慎|数据不足",
            "action": "观察|等待验证|控制风险|数据不足",
            "thesis": "只基于已验证数据的非个性化情景判断",
            "evidence": ["source_id 或结构化行情证据"],
            "risks": ["主要风险"],
            "invalidation_conditions": ["失效或需要重新验证的条件"],
            "suggestions": ["最多三条非个性化观察或风险管理建议"],
            "disclaimer": INVESTMENT_DISCLAIMER,
        },
        "douyin": {"title": "string", "cover_title": "string", "caption": "string", "hashtags": ["string"]},
    }
    return f"""
{context.prompt_text}

运行上下文（由程序提供，不得改写）：
{json.dumps(execution_context, ensure_ascii=False, indent=2)}

市场资料：
{market_context}

最终输出协议（必须严格遵守）：
- 只输出一个 JSON 对象，不要 Markdown、解释文字或其他 schema。
- 必须包含下面示例中的全部顶层 key；不要把 summary 改成对象，不要输出 assets、analysis 等未定义 key。
- `major_indexes` 至少逐项覆盖输入行情中的 SPX、NDX、DJI；`important_stocks` 只使用输入行情中的代码。
- `date` 必须逐字等于 `{context.scheduled_cutoff.astimezone(TOKYO).date().isoformat()}`，不要使用当前系统日期。
- 所有数组和字符串字段即使没有合格事实也必须按 schema 返回；不要用空字符串替代必填结论。
- `ai_investment_view` 只能是非个性化的情景分析，不得输出买入、卖出、加仓、减仓、做多、做空、目标价或止损价。
- `ai_investment_view.market_environment` 必须先判断当前环境为 risk_on、risk_off、mixed 或 insufficient_data，并给出 0 到 1 的置信度与可追溯信号。
- `ai_investment_view.suggestions` 最多三条，只能是观察、等待验证、风险管理或分散化框架，不得针对个人账户给出仓位、金额或具体交易指令。
- `ai_investment_view.evidence` 只能引用输入中的 source_id、source_url 或结构化行情字段；数据不足时 stance/action 必须为“数据不足”。
- `ai_investment_view.disclaimer` 必须逐字等于“{INVESTMENT_DISCLAIMER}”。
{json.dumps(output_contract, ensure_ascii=False, indent=2)}
""".strip()


def call_openai(market_context: str, context: EditionContext | None = None) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise MarketContentError("api_key_missing", "OPENAI_API_KEY is not set.", "", "call_openai")

    body = {
        "model": DEFAULT_MODEL,
        "input": [
            {
                "role": "system",
                "content": "You return only valid JSON matching the requested schema.",
            },
            {
                "role": "user",
                "content": build_prompt(market_context, context),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "daily_market_content",
                "schema": MARKET_CONTENT_SCHEMA,
                "strict": True,
            }
        },
    }
    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raw = ""
        if isinstance(exc, urllib.error.HTTPError):
            raw = exc.read().decode("utf-8", errors="replace")
        raise MarketContentError("api_request_failed", str(exc), raw, "call_openai") from exc

    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text

    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)


def rule_template_response(context: EditionContext, market_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a conservative fallback from validated market data only.

    With no market data this preserves the old status-only response. When a
    validated quote artifact is available, retaining its required symbols
    lets downstream provenance checks continue without inventing facts.
    """
    fields = {field: "数据暂缺，等待合格来源" for field in context.version_fields}
    quotes = [item for item in (market_data or {}).get("quotes", []) if isinstance(item, dict)]
    index_symbols = {"SPX", "NDX", "DJI"}
    major_indexes = []
    important_stocks = []
    for quote in quotes:
        symbol = str(quote.get("symbol") or "")
        change = quote.get("change_pct")
        if symbol in index_symbols:
            major_indexes.append({
                "name": quote.get("display_name") or symbol,
                "ticker": symbol,
                "change_percent": f"{float(change):.2f}%" if isinstance(change, (int, float)) else "数据暂缺",
                "reason": "保留已验证行情；模型内容生成不可用。",
            })
        elif quote.get("asset_type") == "stock":
            important_stocks.append({
                "name": quote.get("display_name") or symbol,
                "ticker": symbol,
                "change_percent": f"{float(change):.2f}%" if isinstance(change, (int, float)) else "数据暂缺",
                "reason": "保留已验证行情；模型内容生成不可用。",
            })
    investment_view = _investment_view_from_validated_quotes(quotes)
    return {
        "date": context.scheduled_cutoff.astimezone(TOKYO).date().isoformat(),
        "timezone": "Asia/Tokyo",
        "edition": context.edition,
        "prompt_version": context.prompt_version,
        "data_cutoff": context.scheduled_cutoff.isoformat(),
        "scheduled_local_time": context.scheduled_local_time,
        "source_window_start": context.source_window_start.isoformat(),
        "source_window_end": context.source_window_end.isoformat(),
        "market_session": context.market_session,
        "edition_fields": fields,
        "summary": "数据暂缺：暂无足够经过校验的市场数据，公开发布已阻止。",
        "key_points": ["请等待合格行情和新闻来源接入。"],
        "major_indexes": major_indexes,
        "important_stocks": important_stocks,
        "macro_events": [],
        "earnings": [],
        "risk_factors": ["数据来源不足，不能形成确定性结论。"],
        "analysis_text": {
            "title": "市场数据暂缺",
            "subtitle": "仅保留状态，不生成未经来源支持的数字",
            "sections": [{"heading": "数据状态", "content": "等待合格来源后再生成内容。"}],
        },
        "ai_investment_view": investment_view,
        "douyin": {
            "title": "每日市场内容包",
            "cover_title": "数据暂缺",
            "caption": "当前没有足够经过校验的来源，暂不发布。",
            "hashtags": [],
        },
    }


def _investment_view_from_validated_quotes(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a conservative, non-personalized view from validated quotes only."""
    indexes = [
        quote for quote in quotes
        if quote.get("symbol") in {"SPX", "NDX", "DJI"}
        and isinstance(quote.get("change_pct"), (int, float))
        and quote.get("source_url")
        and not (quote.get("freshness") or {}).get("stale", False)
        and not (quote.get("cross_check") or {}).get("conflict", False)
    ]
    if len(indexes) < 3:
        return {
            "market_environment": {
                "regime": "insufficient_data",
                "summary": "关键指数数据未完整通过验证。",
                "confidence": 0.0,
                "signals": ["validated_index_set_incomplete"],
            },
            "stance": "数据不足",
            "action": "数据不足",
            "thesis": "关键指数尚未全部通过来源、时效和交叉核对，暂不形成方向性判断。",
            "evidence": ["validated_index_set_incomplete"],
            "risks": ["缺少完整指数或来源存在冲突。"],
            "invalidation_conditions": ["SPX、NDX、DJI 完成有效交叉核对后重新评估。"],
            "suggestions": ["等待完整行情和来源交叉验证后再评估。"],
            "disclaimer": INVESTMENT_DISCLAIMER,
        }
    average_change = sum(float(item["change_pct"]) for item in indexes) / len(indexes)
    stock_changes = [item for item in quotes if item.get("asset_type") == "stock" and isinstance(item.get("change_pct"), (int, float))]
    stock_mixed = any(float(item["change_pct"]) < 0 for item in stock_changes) and any(float(item["change_pct"]) > 0 for item in stock_changes)
    if average_change > 0.25:
        stance, action = "偏积极观察", "等待验证"
        thesis = "主要指数上一交易时段整体收高，但该判断只适用于情景观察，不代表应立即采取交易动作。"
        regime = "mixed" if stock_mixed else "risk_on"
        suggestions = ["关注强势方向是否获得下一时段确认。", "避免将单日指数上涨外推为长期趋势。"]
    elif average_change < -0.25:
        stance, action = "偏谨慎", "控制风险"
        thesis = "主要指数上一交易时段整体走弱，优先观察风险是否扩散，不形成方向性交易指令。"
        regime = "risk_off"
        suggestions = ["优先观察风险是否扩散。", "等待企稳和来源一致性改善后再评估。"]
    else:
        stance, action = "中性观察", "等待验证"
        thesis = "主要指数表现分化或变化有限，等待更多来源与下一时段确认。"
        regime = "mixed"
        suggestions = ["区分指数、行业和个股表现。", "等待下一时段方向确认。"]
    evidence = [
        f"{item['symbol']} change_pct={float(item['change_pct']):.2f}% source_id={item.get('source_id', 'unknown')}"
        for item in indexes
    ]
    risks = ["上一交易时段数据不能替代下一时段实时行情。", "个股与行业可能与指数方向背离。"]
    if stock_mixed:
        risks.append("已验证个股存在方向分化，主题判断不应外推到全部标的。")
    return {
        "market_environment": {
            "regime": regime,
            "summary": "主要指数与已验证个股共同反映的当前环境，仅用于情景观察。",
            "confidence": 0.65 if stock_mixed else 0.75,
            "signals": evidence,
        },
        "stance": stance,
        "action": action,
        "thesis": thesis,
        "evidence": evidence,
        "risks": risks,
        "invalidation_conditions": ["指数出现新的来源冲突或时效失效。", "下一交易时段走势与当前情景明显背离。"],
        "suggestions": suggestions,
        "disclaimer": INVESTMENT_DISCLAIMER,
    }


def generate_with_provider(market_context: str, context: EditionContext, provider: str) -> str:
    prompt = build_prompt(market_context, context)
    try:
        if provider == "auto":
            errors: list[str] = []
            for fallback in ("ollama", "gemini", "rule_template"):
                try:
                    raw = generate_with_provider(market_context, context, fallback)
                    candidate = parse_json_response(raw)
                    _normalize_run_metadata(candidate, context)
                    validate_market_content(candidate, raw, context)
                    return raw
                except MarketContentError as exc:
                    errors.append(f"{fallback}:{exc.error_type}")
            raise MarketContentError("provider_chain_exhausted", ";".join(errors), "", "provider:auto")
        if provider == "ollama":
            return call_ollama(prompt)
        if provider == "gemini":
            return call_gemini(prompt)
        if provider == "rule_template":
            return json.dumps(rule_template_response(context), ensure_ascii=False)
        if provider == "openai":
            return call_openai(market_context, context)
    except ProviderError as exc:
        raise MarketContentError(exc.error_type, str(exc), exc.raw_response, f"provider:{provider}") from exc
    raise MarketContentError("provider_invalid", f"Unsupported content provider: {provider}", "", "provider")


def parse_json_response(raw_response: str) -> dict[str, Any]:
    if raw_response is None:
        raise MarketContentError("empty_response", "OpenAI returned no response.", "", "parse_json_response")
    if not raw_response.strip():
        raise MarketContentError("empty_response", "OpenAI returned an empty string.", raw_response, "parse_json_response")
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise MarketContentError("json_parse_failed", str(exc), raw_response, "parse_json_response") from exc
    if not isinstance(parsed, dict):
        raise MarketContentError("invalid_json_type", "Top-level JSON must be an object.", raw_response, "parse_json_response")
    return parsed


def require_non_empty_string(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MarketContentError("empty_required_field", f"{key} is missing or empty.", json.dumps(data, ensure_ascii=False), key)


def require_non_empty_list(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise MarketContentError("empty_required_field", f"{key} is missing, empty, or not a list.", json.dumps(data, ensure_ascii=False), key)


def _safe_investment_view() -> dict[str, Any]:
    """Compatibility value for legacy JSON without the new analysis field."""
    return {
        "market_environment": {
            "regime": "insufficient_data",
            "summary": "旧版内容未包含可验证的市场环境字段。",
            "confidence": 0.0,
            "signals": ["legacy_content_compatibility"],
        },
        "stance": "数据不足",
        "action": "数据不足",
        "thesis": "旧版内容未包含投资情景字段，继续保持保守状态。",
        "evidence": ["legacy_content_compatibility"],
        "risks": ["缺少结构化投资分析字段，不能形成方向性结论。"],
        "invalidation_conditions": ["重新生成包含完整证据、风险和失效条件的内容。"],
        "suggestions": ["等待完整市场环境分析后再评估。"],
        "disclaimer": INVESTMENT_DISCLAIMER,
    }


def _validate_investment_view(data: dict[str, Any], raw_response: str) -> None:
    view = data.get("ai_investment_view")
    if view is None:
        data["ai_investment_view"] = _safe_investment_view()
        view = data["ai_investment_view"]
    if not isinstance(view, dict):
        raise MarketContentError("investment_view_invalid", "ai_investment_view must be an object.", raw_response, "ai_investment_view")
    environment = view.get("market_environment")
    if not isinstance(environment, dict):
        raise MarketContentError("investment_view_invalid", "market_environment must be an object.", raw_response, "ai_investment_view.market_environment")
    for key in ("regime", "summary"):
        if not isinstance(environment.get(key), str) or not environment[key].strip():
            raise MarketContentError("investment_view_invalid", f"market_environment.{key} is missing or empty.", raw_response, f"ai_investment_view.market_environment.{key}")
    if environment["regime"] not in _MARKET_REGIMES:
        raise MarketContentError("investment_view_invalid", "market_environment.regime is not allowed.", raw_response, "ai_investment_view.market_environment.regime")
    confidence = environment.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise MarketContentError("investment_view_invalid", "market_environment.confidence must be between 0 and 1.", raw_response, "ai_investment_view.market_environment.confidence")
    signals = environment.get("signals")
    if not isinstance(signals, list) or not signals or any(not isinstance(item, str) or not item.strip() for item in signals):
        raise MarketContentError("investment_view_invalid", "market_environment.signals must be a non-empty string list.", raw_response, "ai_investment_view.market_environment.signals")
    for key in ("stance", "action", "thesis", "disclaimer"):
        if not isinstance(view.get(key), str) or not view[key].strip():
            raise MarketContentError("investment_view_invalid", f"ai_investment_view.{key} is missing or empty.", raw_response, f"ai_investment_view.{key}")
    if view["stance"] not in _INVESTMENT_STANCES or view["action"] not in _INVESTMENT_ACTIONS:
        raise MarketContentError("investment_view_invalid", "ai_investment_view stance/action is not allowed.", raw_response, "ai_investment_view")
    if view["disclaimer"] != INVESTMENT_DISCLAIMER:
        raise MarketContentError("investment_view_invalid", "ai_investment_view disclaimer mismatch.", raw_response, "ai_investment_view.disclaimer")
    for key in ("evidence", "risks", "invalidation_conditions", "suggestions"):
        value = view.get(key)
        if not isinstance(value, list) or not value or (key == "suggestions" and len(value) > 3) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise MarketContentError("investment_view_invalid", f"ai_investment_view.{key} must be a non-empty string list.", raw_response, f"ai_investment_view.{key}")
    if view["stance"] == "数据不足" and environment["regime"] != "insufficient_data":
        raise MarketContentError("investment_view_invalid", "data-insufficient stance requires insufficient_data regime.", raw_response, "ai_investment_view.market_environment.regime")
    forbidden = ("买入", "卖出", "加仓", "减仓", "做多", "做空", "目标价", "止损价")
    if any(token in json.dumps(view, ensure_ascii=False) for token in forbidden):
        raise MarketContentError("investment_view_unsafe", "ai_investment_view contains a direct trading instruction.", raw_response, "ai_investment_view")


def _check_or_fill(data: dict[str, Any], key: str, expected: str, raw_response: str) -> None:
    existing = data.get(key)
    if existing not in (None, "", expected):
        raise MarketContentError("edition_metadata_mismatch", f"{key} must equal {expected}.", raw_response, key)
    data[key] = expected


def _attach_edition_metadata(data: dict[str, Any], context: EditionContext, raw_response: str) -> None:
    expected = context.as_json()
    for key in [
        "edition", "prompt_version", "data_cutoff", "scheduled_local_time",
        "source_window_start", "source_window_end", "market_session",
    ]:
        # The edition is a controlled CLI/runtime value, not a model fact.
        # Normalize harmless provider typos while keeping all time and session
        # metadata strict so stale or cross-edition data still fails closed.
        if key == "edition":
            data[key] = str(expected[key])
            continue
        _check_or_fill(data, key, str(expected[key]), raw_response)


def validate_market_content(
    data: dict[str, Any],
    raw_response: str = "",
    context: EditionContext | None = None,
) -> None:
    context = context or _edition_context()
    _attach_edition_metadata(data, context, raw_response)
    require_non_empty_string(data, "date")
    require_non_empty_string(data, "timezone")
    require_non_empty_string(data, "summary")
    require_non_empty_list(data, "key_points")
    edition_fields = data.get("edition_fields")
    if not isinstance(edition_fields, dict):
        raise MarketContentError("edition_fields_missing", "edition_fields must be an object.", raw_response, "edition_fields")
    expected_fields = set(context.version_fields)
    actual_fields = {str(key) for key, value in edition_fields.items() if isinstance(value, str) and value.strip()}
    missing_fields = expected_fields - actual_fields
    if missing_fields:
        raise MarketContentError(
            "edition_fields_missing",
            f"Missing fields for {context.edition}: {sorted(missing_fields)}.",
            raw_response,
            "edition_fields",
        )
    foreign_fields = actual_fields - expected_fields
    if foreign_fields:
        raise MarketContentError(
            "edition_fields_mismatch",
            f"Fields from another edition are not allowed: {sorted(foreign_fields)}.",
            raw_response,
            "edition_fields",
        )

    analysis_text = data.get("analysis_text")
    if not isinstance(analysis_text, dict):
        raise MarketContentError("empty_required_field", "analysis_text is missing or not an object.", raw_response, "analysis_text")
    require_non_empty_string(analysis_text, "title")
    require_non_empty_list(analysis_text, "sections")
    _validate_investment_view(data, raw_response)

    if data["timezone"] != "Asia/Tokyo":
        raise MarketContentError("timezone_mismatch", "timezone must be Asia/Tokyo.", raw_response, "timezone")
    expected_date = context.scheduled_cutoff.astimezone(TOKYO).date().isoformat()
    if data["date"] != expected_date:
        raise MarketContentError(
            "date_mismatch",
            f"date must equal current Asia/Tokyo date {expected_date}.",
            raw_response,
            "date",
        )


def _normalize_run_metadata(parsed: dict[str, Any], context: EditionContext) -> None:
    _attach_edition_metadata(parsed, context, json.dumps(parsed, ensure_ascii=False))
    # The report date is execution metadata derived from the edition cutoff,
    # not a market fact that the model is allowed to invent.  Providers may
    # omit it even when the rest of the structured response is valid, so fill
    # only this deterministic field before schema validation.
    expected_date = context.scheduled_cutoff.astimezone(TOKYO).date().isoformat()
    if parsed.get("date") in (None, ""):
        parsed["date"] = expected_date
    if parsed.get("timezone") in (None, ""):
        parsed["timezone"] = "Asia/Tokyo"


def write_outputs(data: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    douyin = data.get("douyin") or {}
    hashtags = " ".join(douyin.get("hashtags") or [])
    PLATFORM_COPY_MD.write_text(
        "\n".join(
            [
                "# 抖音文案",
                "",
                f"标题：{douyin.get('title', '')}",
                f"封面标题：{douyin.get('cover_title', '')}",
                "",
                douyin.get("caption", ""),
                "",
                "AI投资观察（非个性化）",
                f"判断：{(data.get('ai_investment_view') or {}).get('stance', '')}",
                f"框架：{(data.get('ai_investment_view') or {}).get('action', '')}",
                f"市场环境：{((data.get('ai_investment_view') or {}).get('market_environment') or {}).get('regime', '')}",
                f"结论：{(data.get('ai_investment_view') or {}).get('thesis', '')}",
                f"建议框架：{'；'.join((data.get('ai_investment_view') or {}).get('suggestions', []))}",
                f"风险：{'；'.join((data.get('ai_investment_view') or {}).get('risks', []))}",
                (data.get('ai_investment_view') or {}).get('disclaimer', INVESTMENT_DISCLAIMER),
                "",
                hashtags,
                "",
            ]
        ),
        encoding="utf-8",
    )


def read_context(path: Path | None, market_data_path: Path | None = None) -> str:
    context = "今日市场资料由上游搜索流程提供；请按已知事实生成保守摘要。"
    if path is not None:
        # Keep the source artifact unchanged, but give local models a bounded
        # evidence view.  The previous implementation placed every article
        # body into a single prompt, which made Ollama truncate structured
        # fields before validation. URLs, timestamps and short evidence are
        # retained so grounding still has deterministic source anchors.
        try:
            raw_materials = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw_materials = None
        if isinstance(raw_materials, list):
            compact = []
            for item in raw_materials[:40]:
                if not isinstance(item, dict):
                    continue
                compact.append({
                    "source_id": item.get("source_id"),
                    "source_type": item.get("source_type"),
                    "source_url": item.get("source_url"),
                    "title": str(item.get("title") or "")[:180],
                    "published_at": item.get("published_at"),
                    "body": str(item.get("body") or "")[:260],
                    "topic": item.get("topic"),
                })
            context += "\n\n来源证据（压缩视图，原始文件保持不变）：\n" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        else:
            context += "\n\n市场资料：\n" + path.read_text(encoding="utf-8")[:12000]
    if market_data_path is not None:
        try:
            market_data = json.loads(market_data_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            market_data = {}
        if isinstance(market_data, dict):
            compact_quotes = []
            for quote in market_data.get("quotes", []):
                if not isinstance(quote, dict):
                    continue
                compact_quotes.append({key: quote.get(key) for key in (
                    "symbol", "display_name", "asset_type", "current_price", "previous_close",
                    "change_pct", "direction", "currency", "unit", "price_series",
                    "data_timestamp", "source_url", "source_id", "sources", "cross_check", "freshness",
                )})
            compact_market = {key: market_data.get(key) for key in (
                "edition", "timezone", "market_session", "data_cutoff", "status", "required_symbols",
                "market_data_version", "unresolved_conflicts", "errors",
            )}
            compact_market["quotes"] = compact_quotes
            context += "\n\n经过来源校验的结构化行情 JSON（只允许使用其中的数字和代码）：\n"
            context += json.dumps(compact_market, ensure_ascii=False, separators=(",", ":"))
        else:
            context += "\n\n经过来源校验的结构化行情 JSON（只允许使用其中的数字和代码）：\n"
            context += market_data_path.read_text(encoding="utf-8")[:12000]
    return context


def run(raw_response: str, edition: str | None = None, started_at: dt.datetime | None = None, market_data_path: Path | None = None) -> dict[str, Any]:
    context = _edition_context(edition, started_at)
    parsed = parse_json_response(raw_response)
    _normalize_run_metadata(parsed, context)
    validate_market_content(parsed, raw_response, context)
    if market_data_path is not None:
        market_data = json.loads(market_data_path.read_text(encoding="utf-8"))
        if market_data.get("status") != "success":
            raise MarketContentError("market_data_not_validated", "Structured market data is not validated.", json.dumps(market_data, ensure_ascii=False), "market_data")
        required_symbols = {str(item) for item in market_data.get("required_symbols") or ("SPX", "NDX", "DJI")}
        quote_symbols = {str(item.get("symbol")) for item in market_data.get("quotes", []) if isinstance(item, dict)}
        content_symbols = {str(item.get("ticker")) for item in parsed.get("major_indexes", []) if isinstance(item, dict)}
        missing_symbols = sorted((required_symbols & quote_symbols) - content_symbols)
        if missing_symbols:
            raise MarketContentError("market_data_not_propagated", f"content is missing validated symbols: {missing_symbols}", json.dumps(parsed, ensure_ascii=False), "market_data")
        parsed["market_data_version"] = market_data.get("market_data_version")
        parsed["market_data_hash"] = hashlib.sha256(market_data_path.read_bytes()).hexdigest()
    write_outputs(parsed)
    return parsed


def main() -> int:
    env_path = ROOT / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    parser = argparse.ArgumentParser(description="Generate strict JSON market content with a selected provider.")
    parser.add_argument("--edition", choices=["morning_close_review", "evening_premarket_watch"], default=os.environ.get("MARKET_EDITION"))
    parser.add_argument("--provider", choices=["openai", "ollama", "gemini", "rule_template", "auto"], default=os.environ.get("MARKET_CONTENT_PROVIDER", "openai"))
    parser.add_argument("--market-context-file", type=Path)
    parser.add_argument("--market-data-file", type=Path)
    parser.add_argument("--raw-response-file", type=Path, help="Validate a saved raw OpenAI response instead of calling the API.")
    args = parser.parse_args()

    raw_response = ""
    try:
        if args.raw_response_file:
            raw_response = args.raw_response_file.read_text(encoding="utf-8")
            run(raw_response, edition=args.edition, market_data_path=args.market_data_file)
        else:
            raw_response = generate_with_provider(
                read_context(args.market_context_file, args.market_data_file), _edition_context(args.edition), args.provider
            )
            run(raw_response, edition=args.edition, market_data_path=args.market_data_file)
    except MarketContentError as exc:
        log_market_error(exc.error_type, exc.raw_response or raw_response, exc.failure_position, exc)
        clear_outputs_on_failure()
        print(f"market content generation stopped: {exc.error_type}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - fail closed and log unexpected issues.
        log_market_error("unexpected_error", raw_response, "market_content_openai.main", exc)
        clear_outputs_on_failure()
        print(f"market content generation stopped: unexpected_error: {exc}", file=sys.stderr)
        return 1

    print(OUTPUT_JSON)
    print(PLATFORM_COPY_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
