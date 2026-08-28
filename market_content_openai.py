#!/usr/bin/env python3
"""Generate and validate daily market content as strict JSON."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs" / "market_content"
OUTPUT_JSON = OUTPUT_DIR / "market_content.json"
DOUYIN_MD = OUTPUT_DIR / "douyin.md"
ERROR_LOG = ROOT / "logs" / "market_content_errors.log"
TOKYO = ZoneInfo("Asia/Tokyo")

OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")


MARKET_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "date",
        "timezone",
        "market_session",
        "summary",
        "key_points",
        "major_indexes",
        "important_stocks",
        "macro_events",
        "earnings",
        "risk_factors",
        "image_text",
        "douyin",
    ],
    "properties": {
        "date": {"type": "string"},
        "timezone": {"type": "string", "enum": ["Asia/Tokyo"]},
        "market_session": {"type": "string", "enum": ["pre_market", "after_close", "intraday"]},
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
        "image_text": {
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


def today_tokyo() -> str:
    return dt.datetime.now(TOKYO).date().isoformat()


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
    for path in [OUTPUT_JSON, DOUYIN_MD]:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log_market_error("cleanup_failed", str(path), "clear_outputs_on_failure", exc)


def build_prompt(market_context: str) -> str:
    return f"""
你是每日市场内容包的数据整理器。请只输出一个严格 JSON 对象，不要输出 Markdown、解释、代码块或额外文字。

硬性要求：
- date 必须是 {today_tokyo()}。
- timezone 必须是 Asia/Tokyo。
- 字段结构必须完全符合指定 JSON schema。
- 如果数据不足，用简短、保守、可验证的表述，不要编造具体涨跌。
- douyin 字段只放文字文案来源；图片内容使用 image_text 字段。

市场资料：
{market_context}
""".strip()


def call_openai(market_context: str) -> str:
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
                "content": build_prompt(market_context),
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


def validate_market_content(data: dict[str, Any], raw_response: str = "") -> None:
    require_non_empty_string(data, "date")
    require_non_empty_string(data, "timezone")
    require_non_empty_string(data, "summary")
    require_non_empty_list(data, "key_points")

    image_text = data.get("image_text")
    if not isinstance(image_text, dict):
        raise MarketContentError("empty_required_field", "image_text is missing or not an object.", raw_response, "image_text")
    require_non_empty_string(image_text, "title")
    require_non_empty_list(image_text, "sections")

    if data["timezone"] != "Asia/Tokyo":
        raise MarketContentError("timezone_mismatch", "timezone must be Asia/Tokyo.", raw_response, "timezone")
    expected_date = today_tokyo()
    if data["date"] != expected_date:
        raise MarketContentError(
            "date_mismatch",
            f"date must equal current Asia/Tokyo date {expected_date}.",
            raw_response,
            "date",
        )


def write_outputs(data: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    douyin = data.get("douyin") or {}
    hashtags = " ".join(douyin.get("hashtags") or [])
    DOUYIN_MD.write_text(
        "\n".join(
            [
                "# 抖音文案",
                "",
                f"标题：{douyin.get('title', '')}",
                f"封面标题：{douyin.get('cover_title', '')}",
                "",
                douyin.get("caption", ""),
                "",
                hashtags,
                "",
            ]
        ),
        encoding="utf-8",
    )


def read_context(path: Path | None) -> str:
    if path is None:
        return "今日市场资料由上游搜索流程提供；请按已知事实生成保守摘要。"
    return path.read_text(encoding="utf-8")


def run(raw_response: str) -> dict[str, Any]:
    parsed = parse_json_response(raw_response)
    validate_market_content(parsed, raw_response)
    write_outputs(parsed)
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate strict JSON market content with OpenAI.")
    parser.add_argument("--market-context-file", type=Path)
    parser.add_argument("--raw-response-file", type=Path, help="Validate a saved raw OpenAI response instead of calling the API.")
    args = parser.parse_args()

    raw_response = ""
    try:
        if args.raw_response_file:
            raw_response = args.raw_response_file.read_text(encoding="utf-8")
        else:
            raw_response = call_openai(read_context(args.market_context_file))
        run(raw_response)
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
    print(DOUYIN_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
