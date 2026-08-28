#!/usr/bin/env python3
"""Probe Massive's recent daily history for the Nasdaq-100 index.

This is intentionally independent from the production market-data adapter. It
does one authenticated request, validates the returned bars, and writes a
redacted report for provider-capability checks.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "outputs" / "tests" / "massive_ndx_90d_test.json"
BASE_URL = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
SYMBOL = "I:NDX"
MIN_BARS = 90
WINDOW_DAYS = 180
MAX_LATEST_AGE_DAYS = 14

VALID_STATUSES = {
    "AVAILABLE_90D",
    "INSUFFICIENT_HISTORY",
    "FORBIDDEN",
    "NOT_ENTITLED",
    "NO_DATA",
    "NOT_FOUND",
    "RATE_LIMITED",
    "AUTH_ERROR",
    "NOT_CONFIGURED",
    "API_ERROR",
    "NETWORK_ERROR",
}


def _secure_api_key() -> tuple[str | None, str | None]:
    """Read the explicit environment variable, then the registered secret store.

    The fallback is for the project's existing Keychain-backed setup; neither
    source is ever included in the returned report or terminal output.
    """

    value = os.environ.get("MASSIVE_API_KEY", "").strip()
    if value:
        return value, "environment"

    try:
        sys.path.insert(0, str(ROOT))
        from security import get_secret  # type: ignore

        secret = get_secret(
            "MASSIVE_API_KEY",
            consumer="market_data",
            purpose="collect_quotes",
            run_id="massive-ndx-90d-test",
            mode=os.environ.get("SECURITY_MODE", "production"),
        )
        if secret is not None:
            return secret.reveal("collect_quotes"), "secure_store"
    except Exception:
        # A missing/unreadable secure store is reported as NOT_CONFIGURED.
        # Do not expose provider or Keychain error text in this capability probe.
        pass
    return None, None


def _json_body(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _api_error(body: dict[str, Any], *, fallback_code: str, fallback_message: str) -> dict[str, Any]:
    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("error_code") or fallback_code
        message = error.get("message") or error.get("error_message") or fallback_message
    else:
        code = body.get("error_code") or body.get("code") or fallback_code
        message = body.get("error_message") or body.get("message") or fallback_message
    return {"code": str(code), "message": str(message)}


def _classify_http(status: int, error_code: str, error_message: str) -> str:
    text = f"{error_code} {error_message}".lower()
    if status == 401:
        return "AUTH_ERROR"
    if status == 403:
        if any(token in text for token in ("plan", "entitl", "permission", "subscription", "not authorized")):
            return "NOT_ENTITLED"
        return "FORBIDDEN"
    if status == 404:
        return "NOT_FOUND"
    if status == 429:
        return "RATE_LIMITED"
    return "API_ERROR"


def _request_history(api_key: str, start: dt.date, end: dt.date) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    encoded_symbol = urllib.parse.quote(SYMBOL, safe="")
    query = urllib.parse.urlencode({"sort": "asc", "limit": "500"})
    url = f"{BASE_URL}/v2/aggs/ticker/{encoded_symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "daily-market-content-ndx-capability-test/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = int(response.status)
            body = _json_body(response.read())
            return status, body, None
    except urllib.error.HTTPError as exc:
        body = _json_body(exc.read())
        api_error = _api_error(body, fallback_code=f"HTTP_{exc.code}", fallback_message=exc.reason or "HTTP error")
        return int(exc.code), body, api_error
    except urllib.error.URLError as exc:
        return 0, {}, {"code": "NETWORK_ERROR", "message": type(exc.reason).__name__ if exc.reason else "network error"}
    except TimeoutError:
        return 0, {}, {"code": "NETWORK_TIMEOUT", "message": "request timed out"}


def _valid_bars(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return [], "results_missing"

    by_date: dict[str, dict[str, Any]] = {}
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        try:
            timestamp_ms = float(row["t"])
            close = float(row["c"])
            timestamp = dt.datetime.fromtimestamp(timestamp_ms / 1000, dt.timezone.utc)
            trading_date = timestamp.date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        item: dict[str, Any] = {
            "date": trading_date,
            "timestamp": timestamp.isoformat(),
            "close": close,
        }
        for source_key, output_key in (("o", "open"), ("h", "high"), ("l", "low"), ("v", "volume")):
            if source_key in row:
                try:
                    item[output_key] = float(row[source_key])
                except (TypeError, ValueError):
                    pass
        by_date[trading_date] = item

    bars = [by_date[key] for key in sorted(by_date)]
    return bars, None if bars else "no_valid_bars"


def _base_result(*, credential_configured: bool, credential_source: str | None, start: dt.date, end: dt.date) -> dict[str, Any]:
    return {
        "provider": "massive",
        "index": "NDX",
        "symbol": SYMBOL,
        "status": "NOT_CONFIGURED" if not credential_configured else "API_ERROR",
        "bars": 0,
        "oldest": None,
        "latest": None,
        "latest_close": None,
        "massive_ndx_recent_historical_secondary": False,
        "error": None,
        "credential_configured": credential_configured,
        "credential_source": credential_source,
        "requested_window": {"from": start.isoformat(), "to": end.isoformat()},
        "interval": "1day",
        "min_required_bars": MIN_BARS,
    }


def run() -> dict[str, Any]:
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=WINDOW_DAYS)
    api_key, credential_source = _secure_api_key()
    result = _base_result(
        credential_configured=api_key is not None,
        credential_source=credential_source,
        start=start,
        end=today,
    )

    if api_key is None:
        result["error"] = {"code": "MASSIVE_API_KEY_MISSING", "message": "MASSIVE_API_KEY is not configured", "ticker": SYMBOL}
        return result

    http_status, payload, request_error = _request_history(api_key, start, today)
    result["http_status"] = http_status
    result["api_status"] = payload.get("status") if payload else None

    if request_error is not None:
        code = str(request_error.get("code", "API_ERROR"))
        message = str(request_error.get("message", "API request failed"))
        result["status"] = "NETWORK_ERROR" if code in {"NETWORK_ERROR", "NETWORK_TIMEOUT"} else _classify_http(http_status, code, message)
        result["error"] = {
            "http_status": http_status or None,
            "api_status": payload.get("status") if payload else None,
            "api_error_code": code,
            "api_error_message": message,
            "ticker": SYMBOL,
        }
        return result

    if str(payload.get("status", "")).upper() in {"ERROR", "FAILED"}:
        api_error = _api_error(payload, fallback_code="API_ERROR", fallback_message="Massive returned an error")
        result["status"] = _classify_http(http_status, api_error["code"], api_error["message"])
        result["error"] = {
            "http_status": http_status,
            "api_status": payload.get("status"),
            "api_error_code": api_error["code"],
            "api_error_message": api_error["message"],
            "ticker": SYMBOL,
        }
        return result

    bars, validation_error = _valid_bars(payload)
    result["bars"] = len(bars)
    if bars:
        result["oldest"] = bars[0]["date"]
        result["latest"] = bars[-1]["date"]
        result["latest_close"] = bars[-1]["close"]
        result["sample_ohlc"] = bars[-1]

    if not bars:
        result["status"] = "NO_DATA"
        result["error"] = {"http_status": http_status, "api_status": payload.get("status"), "api_error_code": validation_error or "NO_DATA", "api_error_message": "No valid date and close observations", "ticker": SYMBOL}
        return result

    if len({bar["close"] for bar in bars}) == 1:
        result["status"] = "NO_DATA"
        result["error"] = {"http_status": http_status, "api_status": payload.get("status"), "api_error_code": "CLOSE_VALUES_IDENTICAL", "api_error_message": "All close values are identical", "ticker": SYMBOL}
        return result

    latest_date = dt.date.fromisoformat(bars[-1]["date"])
    if (today - latest_date).days > MAX_LATEST_AGE_DAYS:
        result["status"] = "API_ERROR"
        result["error"] = {"http_status": http_status, "api_status": payload.get("status"), "api_error_code": "LATEST_DATA_STALE", "api_error_message": f"Latest observation is older than {MAX_LATEST_AGE_DAYS} days", "ticker": SYMBOL}
        return result

    if len(bars) < MIN_BARS:
        result["status"] = "INSUFFICIENT_HISTORY"
        result["error"] = {"http_status": http_status, "api_status": payload.get("status"), "api_error_code": "HISTORY_UNDER_90_TRADING_DAYS", "api_error_message": f"Only {len(bars)} distinct trading days returned", "ticker": SYMBOL}
        return result

    result["status"] = "AVAILABLE_90D"
    result["massive_ndx_recent_historical_secondary"] = True
    return result


def _write_report(result: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = REPORT_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(REPORT_PATH)


def main() -> int:
    result = run()
    _write_report(result)

    print("Provider  Index  Symbol  Status              Bars  Oldest      Latest      Latest Close")
    print(f"Massive   NDX    {SYMBOL:<5}   {result['status']:<18} {result['bars']:>4}  {result['oldest'] or '-':<10}  {result['latest'] or '-':<10}  {result['latest_close'] if result['latest_close'] is not None else '-'}")
    print("\nMassive NDX Historical Assessment\n")
    print(f"NDX status: {result['status']}")
    print(f"bars: {result['bars']}")
    print(f"oldest: {result['oldest'] or '-'}")
    print(f"latest: {result['latest'] or '-'}")
    print(f"latest_close: {result['latest_close'] if result['latest_close'] is not None else '-'}")
    print(f"\nmassive_ndx_recent_historical_secondary = {str(result['massive_ndx_recent_historical_secondary']).lower()}")
    if result.get("error"):
        error = result["error"]
        print("\nAPI error")
        print(f"HTTP status: {error.get('http_status', '-')}")
        print(f"Massive API status: {error.get('api_status', '-')}")
        print(f"API error code: {error.get('api_error_code', '-')}")
        print(f"API error message: {error.get('api_error_message', error.get('message', '-'))}")
        print(f"ticker: {SYMBOL}")
    print(f"\nJSON report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
