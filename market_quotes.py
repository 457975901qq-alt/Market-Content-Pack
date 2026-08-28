"""Real structured market-quote collection with source cross-checking.

The collector never invents values. A quote is usable only when its primary
source has a complete numeric payload and, by default, a second source agrees.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import inspect
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from edition_profiles import resolve_edition_context
from security import get_secret, validate_url


ROOT = Path(__file__).resolve().parent
_CANARY_FAULTS_INJECTED: set[str] = set()
POLICY_PATH = Path(os.environ.get("MARKET_DATA_POLICY_PATH", str(ROOT / "config" / "market_data_policy.json"))).expanduser()
# Production market universe.  These are tradable ETF instruments, not index
# values: VOO is used as the S&P 500 exposure proxy and QQQM as the Nasdaq-100
# exposure proxy.  Legacy index symbols remain in SYMBOL_MAP for explicit
# historical tests and compatibility, but are no longer selected by default.
CORE_SYMBOLS = ("VOO", "QQQM")
DEFAULT_STOCKS = ("NVDA", "MSFT", "AAPL")
AS_OF_ERROR_CODES = {
    "invalid_as_of",
    "timezone_missing",
    "provider_history_unavailable",
    "no_market_data_at_or_before_cutoff",
    "market_data_stale",
    "market_data_after_cutoff",
    "crosscheck_temporal_mismatch",
}


class MassiveProviderError(RuntimeError):
    """Safe, structured errors emitted by the NDX historical adapter."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None, provider_status: str | None = None, retryable: bool = False) -> None:
        self.code = code
        self.http_status = http_status
        self.provider_status = provider_status
        self.retryable = retryable
        super().__init__(message)
SYMBOL_MAP = {
    "VOO": {"yahoo": "VOO", "google": ("VOO", "NYSEARCA:VOO", "Vanguard S&P 500 ETF"), "stooq": "voo.us", "asset_type": "etf", "display_name": "VOO（标普500 ETF）"},
    "QQQM": {"yahoo": "QQQM", "google": ("QQQM", "NASDAQ:QQQM", "Invesco NASDAQ 100 ETF"), "stooq": "qqqm.us", "asset_type": "etf", "display_name": "QQQM（纳斯达克100 ETF）"},
    "SPX": {"yahoo": "^GSPC", "google": (".INX", "INDEXSP", "S\\u0026P 500"), "stooq": "^spx", "massive": "I:SPX", "asset_type": "index", "display_name": "标普500"},
    "NDX": {"yahoo": "^NDX", "google": ("NDX", "INDEXNASDAQ", "Nasdaq-100"), "stooq": "^ndq", "massive": "I:NDX", "asset_type": "index", "display_name": "纳斯达克100"},
    "DJI": {"yahoo": "^DJI", "google": (".DJI", "INDEXDJX", "Dow Jones Industrial Average"), "stooq": "^dji", "massive": "I:DJI", "asset_type": "index", "display_name": "道琼斯"},
    "GOLD": {"yahoo": "GC=F", "google": ("GCW00", "COMEX", "Gold"), "stooq": "xauusd", "asset_type": "commodity", "display_name": "黄金"},
    "DXY": {"yahoo": "DX-Y.NYB", "google": ("DXY", "INDEXTVC", "U.S. Dollar Index"), "stooq": "dxy", "asset_type": "fx", "display_name": "美元指数"},
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _policy() -> dict[str, Any]:
    try:
        data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _configured_secondary_provider() -> str:
    return os.environ.get("MARKET_SECONDARY_PROVIDER", str(_policy().get("secondary_provider", "google_finance"))).strip().lower()


def _normalize_as_of(value: dt.datetime | str | None) -> dt.datetime | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_as_of") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


def _timestamp(value: str | int | float | dt.datetime) -> dt.datetime:
    try:
        if isinstance(value, dt.datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = dt.datetime.fromtimestamp(value, dt.timezone.utc)
        else:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_market_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


def _market_error_code(exc: Exception) -> str:
    if isinstance(exc, MassiveProviderError):
        return exc.code
    message = str(exc).strip()
    if message in AS_OF_ERROR_CODES:
        return message
    return type(exc).__name__


def _invoke_fetcher(fetcher: Any, symbol: str, as_of: dt.datetime | None) -> dict[str, Any]:
    """Call legacy one-argument test/providers and new cutoff-aware providers."""
    try:
        parameters = inspect.signature(fetcher).parameters
        accepts_as_of = "as_of" in parameters or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    except (TypeError, ValueError):
        accepts_as_of = False
    if as_of is not None and accepts_as_of:
        return fetcher(symbol, as_of=as_of)
    return fetcher(symbol)


def _select_as_of_payload(payload: dict[str, Any], as_of: dt.datetime | None) -> dict[str, Any]:
    if as_of is None:
        return {**payload, "selection_mode": "latest", "requested_as_of": None, "selected_as_of": payload.get("data_timestamp")}
    cutoff = _normalize_as_of(as_of)
    assert cutoff is not None
    raw_observations = payload.get("observations")
    observations: list[tuple[dt.datetime, float]] = []
    if isinstance(raw_observations, list):
        for row in raw_observations:
            try:
                if isinstance(row, dict):
                    timestamp = _timestamp(row.get("timestamp") or row.get("data_timestamp"))
                    value = float(row.get("value", row.get("close")))
                else:
                    timestamp = _timestamp(row[0])
                    value = float(row[1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            observations.append((timestamp, value))
    if observations:
        eligible = [item for item in observations if item[0] <= cutoff]
        if not eligible:
            raise ValueError("market_data_after_cutoff")
        selected_timestamp, selected_value = max(eligible, key=lambda item: item[0])
        ordered = sorted(eligible, key=lambda item: item[0])
        previous = ordered[-2][1] if len(ordered) > 1 else payload.get("previous_close")
        if previous is None or float(previous) <= 0:
            raise ValueError("previous_close_missing")
        return {
            **payload,
            "current_price": selected_value,
            "previous_close": float(previous),
            "price_series": [value for _, value in ordered],
            "data_timestamp": selected_timestamp.isoformat(),
            "provider_timestamp": selected_timestamp.isoformat(),
            "requested_as_of": cutoff.isoformat(),
            "selected_as_of": selected_timestamp.isoformat(),
            "selection_mode": "as_of",
        }
    timestamp_value = payload.get("data_timestamp")
    if not timestamp_value:
        raise ValueError("no_market_data_at_or_before_cutoff")
    parsed = _timestamp(timestamp_value)
    if parsed > cutoff:
        raise ValueError("market_data_after_cutoff")
    return {
        **payload,
        "data_timestamp": parsed.isoformat(),
        "provider_timestamp": parsed.isoformat(),
        "requested_as_of": cutoff.isoformat(),
        "selected_as_of": parsed.isoformat(),
        "selection_mode": "as_of",
    }


def market_quote_cache_key(symbol: str, *, provider: str, as_of: dt.datetime | str | None = None) -> str:
    normalized = _normalize_as_of(as_of)
    payload = {
        "symbol": symbol,
        "provider": provider,
        "selection_mode": "as_of" if normalized is not None else "latest",
        "requested_as_of": normalized.isoformat() if normalized else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _request_json(url: str, timeout: float = 15, headers: dict[str, str] | None = None) -> dict[str, Any]:
    validate_url(url, consumer="market_data", purpose="collect_quotes")
    request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("market_source_payload_not_object")
    return payload


def _request_text(url: str, timeout: float = 15) -> str:
    validate_url(url, consumer="market_data", purpose="collect_quotes")
    request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_yahoo(symbol: str, timeout: float = 15, as_of: dt.datetime | None = None) -> dict[str, Any]:
    mapped = SYMBOL_MAP.get(symbol, {"yahoo": symbol, "asset_type": "stock", "display_name": symbol})
    encoded = urllib.parse.quote(str(mapped["yahoo"]), safe="")
    interval = "1h" if as_of is not None else "1d"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval={interval}"
    payload = _request_json(url, timeout)
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("yahoo_empty_result")
    data = result[0]
    timestamps = data.get("timestamp") or []
    closes = ((data.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    points = [(int(ts), float(close)) for ts, close in zip(timestamps, closes) if ts is not None and close is not None]
    if not points:
        raise ValueError("yahoo_price_series_missing")
    meta = data.get("meta") or {}
    current = points[-1][1]
    previous = meta.get("previousClose")
    if previous is None and len(points) > 1:
        previous = points[-2][1]
    if previous is None or float(previous) <= 0:
        raise ValueError("yahoo_previous_close_missing")
    return _select_as_of_payload({
        "provider": "yahoo_chart",
        "source_url": url,
        "current_price": current,
        "previous_close": float(previous),
        "price_series": [value for _, value in points],
        "data_timestamp": dt.datetime.fromtimestamp(points[-1][0], dt.timezone.utc).isoformat(),
        "observations": [{"timestamp": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(), "value": value} for timestamp, value in points],
        "currency": meta.get("currency") or "USD",
        "unit": "price",
    }, as_of)


def _fetch_stooq(symbol: str, timeout: float = 15, as_of: dt.datetime | None = None) -> dict[str, Any]:
    mapped = SYMBOL_MAP.get(symbol, {"stooq": f"{symbol.lower()}.us"})
    stooq_symbol = str(mapped.get("stooq") or f"{symbol.lower()}.us")
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(stooq_symbol, safe='^')}&i=d"
    validate_url(url, consumer="market_data", purpose="collect_quotes")
    request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    points: list[tuple[dt.datetime, float]] = []
    for row in rows[-5:]:
        try:
            timestamp = dt.datetime.strptime(str(row["Date"]), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append((timestamp, close))
    if not points:
        raise ValueError("stooq_price_series_missing")
    previous = points[-2][1] if len(points) > 1 else None
    if previous is None or previous <= 0:
        raise ValueError("stooq_previous_close_missing")
    return _select_as_of_payload({
        "provider": "stooq_csv",
        "source_url": url,
        "current_price": points[-1][1],
        "previous_close": previous,
        "price_series": [value for _, value in points],
        "data_timestamp": points[-1][0].isoformat(),
        "observations": [{"timestamp": timestamp.isoformat(), "value": value} for timestamp, value in points],
        "currency": "USD",
        "unit": "price",
    }, as_of)


def _fetch_google_finance(symbol: str, timeout: float = 15, as_of: dt.datetime | None = None) -> dict[str, Any]:
    """Read the quote payload embedded in the public Google Finance page.

    The parser is anchored to the requested symbol and exchange tuple. It
    does not scan arbitrary numbers from the page, which prevents related
    instruments or page chrome from becoming market data.
    """
    mapped = SYMBOL_MAP.get(symbol, {})
    google_symbol, exchange, display_name = mapped.get("google", (symbol, "NASDAQ", None))
    url = f"https://www.google.com/finance/quote/{urllib.parse.quote(str(google_symbol), safe='.') }:{urllib.parse.quote(str(exchange), safe='')}?hl=en"
    html = _request_text(url, timeout)
    escaped_symbol = re.escape(str(google_symbol))
    escaped_exchange = re.escape(str(exchange))
    name_fragment = re.escape(str(display_name)) if display_name else r'[^"]+'
    pattern = rf'"{escaped_symbol}","{escaped_exchange}"\],"{name_fragment}",\d+,(?:null|"USD"),\[([^\]]+)\]'
    match = re.search(pattern, html)
    if not match:
        raise ValueError("google_quote_payload_missing")
    values = [part.strip() for part in match.group(1).split(",")]
    if len(values) < 3:
        raise ValueError("google_quote_values_incomplete")
    try:
        current = float(values[0])
        change = float(values[2])
    except (TypeError, ValueError) as exc:
        raise ValueError("google_quote_values_invalid") from exc
    tail = html[match.end():match.end() + 1200]
    previous_match = re.search(r",null,([0-9]+(?:\.[0-9]+)?)", tail)
    timestamp_match = re.search(r"\[([0-9]{10})\]", tail)
    if current <= 0 or previous_match is None or timestamp_match is None:
        raise ValueError("google_quote_metadata_missing")
    previous = float(previous_match.group(1))
    timestamp = dt.datetime.fromtimestamp(int(timestamp_match.group(1)), dt.timezone.utc).isoformat()
    if as_of is not None:
        raise ValueError("provider_history_unavailable")
    return {
        "provider": "google_finance",
        "source_url": url,
        "current_price": current,
        "previous_close": previous,
        "change_pct": change,
        "price_series": [],
        "data_timestamp": timestamp,
        "currency": "USD",
        "unit": "price",
    }


def _massive_interval(value: str) -> tuple[int, str]:
    normalized = value.strip().lower()
    supported = {"1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"), "30m": (30, "minute"), "1h": (1, "hour"), "1d": (1, "day")}
    if normalized not in supported:
        raise ValueError("massive_interval_unsupported")
    return supported[normalized]


def _fetch_massive(symbol: str, timeout: float = 15, as_of: dt.datetime | None = None) -> dict[str, Any]:
    """Fetch real index bars from Massive without persisting the API key."""
    mapped = SYMBOL_MAP.get(symbol, {})
    massive_symbol = str(mapped.get("massive") or "")
    if not massive_symbol:
        raise ValueError("massive_symbol_mapping_missing")
    secret = get_secret(
        "MASSIVE_API_KEY",
        consumer="market_data",
        purpose="collect_quotes",
        run_id=os.environ.get("MARKET_RUN_ID", "unspecified"),
    )
    if secret is None:
        raise ValueError("massive_api_key_missing")
    multiplier, timespan = _massive_interval(os.environ.get("MASSIVE_MARKET_INTERVAL", "5m"))
    cutoff = _normalize_as_of(as_of) or dt.datetime.now(dt.timezone.utc)
    window_start = cutoff - dt.timedelta(days=7)
    base_url = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
    encoded = urllib.parse.quote(massive_symbol, safe="")
    url = f"{base_url}/v2/aggs/ticker/{encoded}/range/{multiplier}/{timespan}/{int(window_start.timestamp() * 1000)}/{int(cutoff.timestamp() * 1000)}?sort=asc&limit=50000"
    headers = {"Authorization": f"Bearer {secret.reveal('collect_quotes')}"}
    payload = _request_json(url, timeout, headers=headers)
    raw_results = payload.get("results") or []
    observations: list[dict[str, Any]] = []
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = dt.datetime.fromtimestamp(float(row["t"]) / 1000, dt.timezone.utc).isoformat()
            close = float(row["c"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        observations.append({"timestamp": timestamp, "value": close})
    if not observations:
        raise ValueError("massive_price_series_missing")
    return _select_as_of_payload({
        "provider": "massive_indices",
        "source_url": url.split("?", 1)[0],
        "instrument": massive_symbol,
        "asset_type": "index",
        "current_price": observations[-1]["value"],
        "previous_close": observations[-2]["value"] if len(observations) > 1 else None,
        "price_series": [item["value"] for item in observations],
        "data_timestamp": observations[-1]["timestamp"],
        "observations": observations,
        "currency": "USD",
        "unit": "index_value",
    }, as_of)


def _massive_error_from_body(body: dict[str, Any], *, http_status: int, fallback_code: str, fallback_message: str, retryable: bool) -> MassiveProviderError:
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    code = str((error or {}).get("code") or (error or {}).get("error_code") or fallback_code)
    message = str((error or {}).get("message") or (error or {}).get("error_message") or fallback_message)
    lowered = f"{code} {message}".lower()
    if http_status == 401:
        code = "MASSIVE_AUTH_ERROR"
    elif http_status == 403:
        code = "MASSIVE_NOT_ENTITLED" if any(token in lowered for token in ("plan", "entitl", "permission", "subscription", "not authorized")) else "MASSIVE_FORBIDDEN"
    elif http_status == 429:
        code = "MASSIVE_RATE_LIMITED"
    elif http_status >= 500:
        code = "MASSIVE_PROVIDER_ERROR"
    return MassiveProviderError(code, message, http_status=http_status, provider_status=str(body.get("status") or "") or None, retryable=retryable)


def _massive_ndx_historical_daily(as_of: dt.datetime, timeout: float = 15) -> dict[str, Any]:
    """Fetch and validate Massive's deliberately narrow NDX daily capability."""
    cutoff = _normalize_as_of(as_of)
    if cutoff is None:
        raise ValueError("massive_ndx_historical_requires_as_of")
    secret = get_secret(
        "MASSIVE_API_KEY",
        consumer="market_data",
        purpose="collect_quotes",
        run_id=os.environ.get("MARKET_RUN_ID", "unspecified"),
    )
    if secret is None:
        raise MassiveProviderError("MASSIVE_API_KEY_MISSING", "Massive credential is not configured")

    end_date = cutoff.date()
    start_date = end_date - dt.timedelta(days=180)
    base_url = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
    encoded = urllib.parse.quote("I:NDX", safe="")
    endpoint = f"{base_url}/v2/aggs/ticker/{encoded}/range/1/day/{start_date.isoformat()}/{end_date.isoformat()}"
    url = endpoint + "?" + urllib.parse.urlencode({"sort": "asc", "limit": "500"})
    headers = {
        "Authorization": f"Bearer {secret.reveal('collect_quotes')}",
        "Accept": "application/json",
    }
    attempts = max(1, min(int(os.environ.get("MASSIVE_MAX_RETRIES", "3")), 3))
    payload: dict[str, Any] | None = None
    response_status: int | None = None
    for attempt in range(attempts):
        try:
            validate_url(url, consumer="market_data", purpose="collect_quotes")
            request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0", **headers}, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_status = int(response.status)
                raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise MassiveProviderError("MASSIVE_SCHEMA_ERROR", "Massive response is not an object", http_status=response_status)
            payload = parsed
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            retryable = exc.code == 429 or exc.code >= 500
            error = _massive_error_from_body(body, http_status=int(exc.code), fallback_code=f"HTTP_{exc.code}", fallback_message=exc.reason or "Massive HTTP error", retryable=retryable)
            if retryable and attempt + 1 < attempts:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = max(0.0, min(float(retry_after), 30.0)) if retry_after else min(2.0 ** attempt, 10.0)
                except (TypeError, ValueError):
                    delay = min(2.0 ** attempt, 10.0)
                time.sleep(delay)
                continue
            raise error from exc
        except urllib.error.URLError as exc:
            if attempt + 1 < attempts:
                time.sleep(min(2.0 ** attempt, 10.0))
                continue
            raise MassiveProviderError("MASSIVE_NETWORK_ERROR", "Massive network request failed", retryable=True) from exc
        except TimeoutError as exc:
            if attempt + 1 < attempts:
                time.sleep(min(2.0 ** attempt, 10.0))
                continue
            raise MassiveProviderError("MASSIVE_TIMEOUT", "Massive request timed out", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise MassiveProviderError("MASSIVE_SCHEMA_ERROR", "Massive response is not valid JSON", http_status=response_status) from exc

    if payload is None:
        raise MassiveProviderError("MASSIVE_PROVIDER_ERROR", "Massive returned no response")
    provider_status = str(payload.get("status") or "UNKNOWN")
    if provider_status.upper() in {"ERROR", "FAILED"}:
        raise _massive_error_from_body(payload, http_status=response_status or 200, fallback_code="MASSIVE_API_ERROR", fallback_message="Massive returned an error", retryable=False)

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise MassiveProviderError("MASSIVE_NO_DATA", "Massive results are missing", http_status=response_status, provider_status=provider_status)
    bars_by_date: dict[str, dict[str, Any]] = {}
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = _timestamp(float(row["t"]) / 1000)
            close = float(row["c"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if close <= 0:
            continue
        bar: dict[str, Any] = {
            "symbol": "I:NDX",
            "index": "NDX",
            "provider": "massive",
            "date": timestamp.date().isoformat(),
            "timestamp": timestamp.isoformat(),
            "close": close,
            "provider_status": provider_status,
            "retrieved_at": _now(),
        }
        for raw_key, key in (("o", "open"), ("h", "high"), ("l", "low"), ("v", "volume")):
            if row.get(raw_key) is not None:
                try:
                    bar[key] = float(row[raw_key])
                except (TypeError, ValueError) as exc:
                    raise MassiveProviderError("MASSIVE_SCHEMA_ERROR", f"invalid {key} in Massive bar", http_status=response_status, provider_status=provider_status) from exc
        if all(key in bar for key in ("open", "high", "low", "close")):
            if bar["high"] < bar["low"] or bar["high"] < bar["open"] or bar["high"] < bar["close"] or bar["low"] > bar["open"] or bar["low"] > bar["close"]:
                raise MassiveProviderError("MASSIVE_INVALID_OHLC", "Massive OHLC relationship is invalid", http_status=response_status, provider_status=provider_status)
        bars_by_date[bar["date"]] = bar

    bars = [bars_by_date[key] for key in sorted(bars_by_date)]
    if not bars:
        raise MassiveProviderError("MASSIVE_NO_DATA", "Massive returned no valid NDX daily bars", http_status=response_status, provider_status=provider_status)
    latest_date = dt.date.fromisoformat(bars[-1]["date"])
    if (end_date - latest_date).days > 7:
        raise MassiveProviderError("STALE_DATA", "Massive latest NDX bar is stale", http_status=response_status, provider_status=provider_status)
    if len(bars) >= 2 and max((dt.date.fromisoformat(right["date"]) - dt.date.fromisoformat(left["date"])).days for left, right in zip(bars, bars[1:])) > 10:
        raise MassiveProviderError("MASSIVE_HISTORY_GAP", "Massive NDX history contains an abnormal gap", http_status=response_status, provider_status=provider_status)
    if len({bar["close"] for bar in bars}) == 1:
        raise MassiveProviderError("MASSIVE_INVALID_DATA", "Massive NDX close values are identical", http_status=response_status, provider_status=provider_status)
    if len(bars) < 90:
        raise MassiveProviderError("INSUFFICIENT_HISTORY", "Massive NDX history has fewer than 90 trading days", http_status=response_status, provider_status=provider_status)

    observations = [{"timestamp": bar["timestamp"], "value": bar["close"]} for bar in bars]
    selected = _select_as_of_payload({
        "provider": "massive",
        "provider_role": "historical_secondary",
        "index": "NDX",
        "symbol": "I:NDX",
        "instrument": "I:NDX",
        "asset_type": "index",
        "source_url": endpoint,
        "provider_status": provider_status,
        "retrieved_at": _now(),
        "bars": bars,
        "bar_count": len(bars),
        "oldest_date": bars[0]["date"],
        "latest_date": bars[-1]["date"],
        "current_price": bars[-1]["close"],
        "previous_close": bars[-2]["close"],
        "price_series": [bar["close"] for bar in bars],
        "data_timestamp": bars[-1]["timestamp"],
        "observations": observations,
        "currency": "USD",
        "unit": "index_value",
    }, cutoff)
    selected["provider_status"] = provider_status
    selected["bar_count"] = len(bars)
    selected["oldest_date"] = bars[0]["date"]
    selected["latest_date"] = bars[-1]["date"]
    selected["validated"] = True
    return selected


def _fetch_secondary_market_source(symbol: str, timeout: float = 15, as_of: dt.datetime | None = None) -> dict[str, Any]:
    provider = _configured_secondary_provider()
    if provider == "massive":
        if symbol != "NDX" or as_of is None:
            raise ValueError("massive_capability_not_supported")
        return _massive_ndx_historical_daily(as_of, timeout)
    if provider == "google_finance":
        return _fetch_google_finance(symbol, timeout, as_of)
    if provider == "stooq_csv":
        return _fetch_stooq(symbol, timeout, as_of)
    raise ValueError(f"unsupported_secondary_provider:{provider}")


def _record_source(source_status: dict[str, Any], provider: str, success: bool) -> None:
    item = source_status.setdefault(provider, {"status": "unknown", "count": 0})
    if success:
        item["count"] = int(item.get("count", 0)) + 1
        item["status"] = "healthy"
    elif item.get("status") != "healthy":
        item["status"] = "failed"


def _provider_error_record(symbol: str, source: str, exc: Exception) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": os.environ.get("MARKET_RUN_ID", "unspecified"),
        "symbol": symbol,
        "source": source,
        "error_type": _market_error_code(exc),
        "message": str(exc)[:300],
    }
    if isinstance(exc, MassiveProviderError):
        record.update({
            "http_status": exc.http_status,
            "provider_status": exc.provider_status,
            "retryable": exc.retryable,
        })
    return record


def _massive_fallback_allowed(exc: Exception) -> bool:
    if isinstance(exc, MassiveProviderError):
        return bool(exc.retryable) or exc.code in {"MASSIVE_RATE_LIMITED", "MASSIVE_PROVIDER_ERROR"}
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    message = str(exc).lower()
    return any(token in message for token in ("empty_result", "price_series_missing", "no_data", "stale", "provider_unavailable", "temporary"))


def _freshness(timestamp: str | None, cutoff: dt.datetime, max_staleness_hours: float) -> dict[str, Any]:
    if not timestamp:
        return {"stale": True, "reason": "data_timestamp_missing"}
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return {"stale": True, "reason": "data_timestamp_invalid", "data_timestamp": timestamp}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    allowed_lag_hours = max_staleness_hours
    cutoff_utc = cutoff.astimezone(dt.timezone.utc)
    future = parsed > cutoff_utc
    too_old = (cutoff_utc - parsed).total_seconds() > allowed_lag_hours * 3600
    return {
        "edition_cutoff": cutoff.isoformat(),
        "data_timestamp": timestamp,
        "max_staleness_hours": allowed_lag_hours,
        "stale": future or too_old,
        "reason": "future_timestamp" if future else "staleness_window_exceeded" if too_old else None,
    }


def _quote(symbol: str, primary: dict[str, Any], secondary: dict[str, Any] | None, threshold: float, runtime_timestamp: str) -> dict[str, Any]:
    current = float(primary["current_price"])
    previous = float(primary["previous_close"])
    change = (current - previous) / previous * 100
    direction = "up" if change > 0 else "down" if change < 0 else "flat"
    conflict = False
    difference = None
    temporal_mismatch = False
    secondary_timestamp = None
    if secondary is not None:
        secondary_current = float(secondary["current_price"])
        difference = abs(current - secondary_current) / max(abs(current), 1e-12)
        conflict = difference > threshold
        primary_timestamp = primary.get("selected_as_of") or primary.get("data_timestamp")
        secondary_timestamp = secondary.get("selected_as_of") or secondary.get("data_timestamp")
        temporal_mismatch = bool(primary_timestamp and secondary_timestamp and primary_timestamp != secondary_timestamp)
    return {
        "symbol": symbol,
        "display_name": SYMBOL_MAP.get(symbol, {}).get("display_name", symbol),
        "asset_type": SYMBOL_MAP.get(symbol, {}).get("asset_type", "stock"),
        "current_price": current,
        "previous_close": previous,
        "change_pct": round(change, 6),
        "direction": direction,
        "currency": primary.get("currency") or "USD",
        "unit": primary.get("unit") or "price",
        "price_series": primary.get("price_series") or [],
        "data_timestamp": primary.get("data_timestamp"),
        "quote_timestamp": primary.get("data_timestamp"),
        "requested_as_of": primary.get("requested_as_of"),
        "selected_as_of": primary.get("selected_as_of"),
        "selection_mode": primary.get("selection_mode", "latest"),
        "provider": primary.get("provider"),
        "source": primary.get("source_url"),
        "runtime_timestamp": runtime_timestamp,
        "source_url": primary.get("source_url"),
        "source_id": hashlib.sha256(str(primary.get("source_url")).encode()).hexdigest()[:16],
        "sources": {
            "primary": primary,
            "secondary": secondary,
        },
        "cross_check": {
            "performed": secondary is not None,
            "difference_ratio": difference,
            "threshold": threshold,
            "conflict": conflict,
            "temporal_mismatch": temporal_mismatch,
            "primary_selected_as_of": primary.get("selected_as_of") or primary.get("data_timestamp"),
            "secondary_selected_as_of": secondary_timestamp,
        },
    }


def collect_quotes(
    edition: str,
    *,
    symbols: list[str] | None = None,
    require_crosscheck: bool | None = None,
    fetch_primary: Any = _fetch_yahoo,
    fetch_secondary: Any = _fetch_secondary_market_source,
    as_of: dt.datetime | str | None = None,
    required_symbols: list[str] | None = None,
) -> dict[str, Any]:
    context = resolve_edition_context(edition)
    try:
        requested_as_of = _normalize_as_of(as_of)
    except ValueError as exc:
        return {
            "edition": edition,
            "timezone": "Asia/Tokyo",
            "market_session": context.market_session,
            "data_cutoff": context.scheduled_cutoff.isoformat(),
            "requested_as_of": None,
            "selection_mode": "as_of" if as_of is not None else "latest",
            "collected_at": _now(),
            "status": "failed",
            "source_status": {},
            "quotes": [],
            "required_symbols": sorted(set(required_symbols or (symbols or [*CORE_SYMBOLS, *DEFAULT_STOCKS]))),
            "missing_required_symbols": [],
            "unresolved_conflicts": [],
            "errors": [{"error_type": _market_error_code(exc), "message": str(exc)}],
            "require_crosscheck": False,
            "conflict_threshold": None,
            "max_staleness_hours": None,
            "policy_path": str(POLICY_PATH),
        }
    effective_cutoff = requested_as_of or context.scheduled_cutoff.astimezone(dt.timezone.utc)
    selected = list(dict.fromkeys(symbols or [*CORE_SYMBOLS, *DEFAULT_STOCKS]))
    if os.environ.get("SELF_HEALING_CANARY_MODE", "false").lower() == "true" and os.environ.get("SELF_HEALING_FAULT", "none").strip() == "market_data_incomplete" and "market_data_incomplete" not in _CANARY_FAULTS_INJECTED:
        _CANARY_FAULTS_INJECTED.add("market_data_incomplete")
        selected = [symbol for symbol in selected if symbol not in set(CORE_SYMBOLS)]
    policy = _policy()
    # An explicit symbol list is an explicit request (including legacy test
    # fixtures).  Only the no-argument production path uses policy defaults.
    required = set(required_symbols or (symbols if symbols is not None else policy.get("required_symbols") or CORE_SYMBOLS))
    threshold = float(os.environ.get("MARKET_SOURCE_CONFLICT_THRESHOLD", policy.get("conflict_threshold", 0.02)))
    crosscheck = _env_bool("MARKET_REQUIRE_CROSSCHECK", bool(policy.get("require_crosscheck", True))) if require_crosscheck is None else require_crosscheck
    max_staleness_hours = float(os.environ.get("MARKET_MAX_STALENESS_HOURS", policy.get("max_staleness_hours", 120)))
    quotes: list[dict[str, Any]] = []
    source_status: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    provider_events: list[dict[str, Any]] = []
    configured_secondary = _configured_secondary_provider()
    massive_fallback_mode = configured_secondary == "massive" and fetch_secondary is _fetch_secondary_market_source

    def fetch_pair(symbol: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        primary: dict[str, Any] | None = None
        secondary: dict[str, Any] | None = None
        pair_errors: list[dict[str, Any]] = []
        pair_events: list[dict[str, Any]] = []
        try:
            primary = _select_as_of_payload(_invoke_fetcher(fetch_primary, symbol, requested_as_of), requested_as_of)
        except Exception as exc:
            primary_error = _provider_error_record(symbol, "yahoo_chart", exc)
            pair_errors.append(primary_error)
            if symbol == "NDX" and massive_fallback_mode and requested_as_of is not None:
                pair_events.append({"event": "NDX_HISTORICAL_PRIMARY_FAILED", "provider": "yahoo_chart", "index": "NDX", "symbol": "I:NDX", "status": "failed", "failure_reason": primary_error.get("error_type"), "timestamp": _now()})
                if _massive_fallback_allowed(exc):
                    pair_events.append({"event": "NDX_HISTORICAL_FALLBACK_MASSIVE_START", "provider": "massive", "index": "NDX", "symbol": "I:NDX", "role": "historical_secondary", "status": "started", "timestamp": _now()})
                    try:
                        secondary = _massive_ndx_historical_daily(requested_as_of)
                        pair_events.append({"event": "NDX_HISTORICAL_FALLBACK_MASSIVE_OK", "provider": "massive", "index": "NDX", "symbol": "I:NDX", "role": "historical_secondary", "status": "success", "bar_count": secondary.get("bar_count"), "latest_date": secondary.get("latest_date"), "massive_status": secondary.get("provider_status"), "timestamp": _now()})
                    except Exception as fallback_exc:
                        pair_errors.append(_provider_error_record(symbol, "massive", fallback_exc))
                        pair_events.append({"event": "NDX_HISTORICAL_FALLBACK_MASSIVE_REJECTED", "provider": "massive", "index": "NDX", "symbol": "I:NDX", "role": "historical_secondary", "status": "failed", "failure_reason": _market_error_code(fallback_exc), "timestamp": _now()})
                else:
                    pair_events.append({"event": "NDX_HISTORICAL_UNAVAILABLE", "provider": "massive", "index": "NDX", "symbol": "I:NDX", "status": "blocked", "failure_reason": "primary_error_not_fallback_eligible", "timestamp": _now()})
            elif not massive_fallback_mode:
                try:
                    secondary = _select_as_of_payload(_invoke_fetcher(fetch_secondary, symbol, requested_as_of), requested_as_of)
                except Exception as secondary_exc:
                    pair_errors.append(_provider_error_record(symbol, configured_secondary, secondary_exc))
        else:
            # Massive is a narrow historical fallback, not a general-purpose
            # cross-check. A healthy primary must never call it.
            if symbol == "NDX" and massive_fallback_mode and requested_as_of is not None:
                pair_events.append({"event": "NDX_HISTORICAL_PRIMARY_OK", "provider": "yahoo_chart", "index": "NDX", "symbol": "I:NDX", "role": "primary", "status": "success", "timestamp": _now()})
            if not massive_fallback_mode:
                try:
                    secondary = _select_as_of_payload(_invoke_fetcher(fetch_secondary, symbol, requested_as_of), requested_as_of)
                except Exception as secondary_exc:
                    pair_errors.append(_provider_error_record(symbol, configured_secondary, secondary_exc))
        if symbol == "NDX" and massive_fallback_mode and primary is None and secondary is None:
            pair_events.append({"event": "NDX_HISTORICAL_UNAVAILABLE", "provider": "massive", "index": "NDX", "symbol": "I:NDX", "role": "historical_secondary", "status": "failed", "failure_reason": next((error.get("error_type") for error in pair_errors if error.get("source") == "massive"), "fallback_unavailable"), "timestamp": _now()})
        return symbol, primary, secondary, pair_errors, pair_events

    workers = max(1, min(int(os.environ.get("MARKET_SOURCE_MAX_WORKERS", policy.get("max_workers", 4))), len(selected)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-quote") as pool:
        fetched = list(pool.map(fetch_pair, selected))
    for symbol, primary, secondary, pair_errors, pair_events in fetched:
        errors.extend(pair_errors)
        provider_events.extend(pair_events)
        if primary is not None:
            _record_source(source_status, str(primary.get("provider", "primary")), True)
        else:
            _record_source(source_status, "yahoo_chart", False)
        if secondary is not None:
            _record_source(source_status, str(secondary.get("provider", "secondary")), True)
        elif not massive_fallback_mode:
            _record_source(source_status, _configured_secondary_provider(), False)
        if primary is None and secondary is None:
            continue
        final_payload = primary or secondary
        assert final_payload is not None
        item = _quote(symbol, final_payload, secondary if primary is not None else None, threshold, _now())
        if primary is None and secondary is not None:
            primary_error = next((error for error in pair_errors if error.get("source") == "yahoo_chart"), {})
            item.update({
                "provider": "massive",
                "provider_role": "historical_secondary",
                "fallback_used": True,
                "primary_provider": "yahoo_chart",
                "primary_failure_reason": primary_error.get("error_type"),
                "massive_status": secondary.get("provider_status"),
                "bar_count": secondary.get("bar_count"),
                "oldest_date": secondary.get("oldest_date"),
                "latest_date": secondary.get("latest_date"),
                "validated": secondary.get("validated", False),
            })
            item["sources"] = {"primary": None, "secondary": secondary}
            item["cross_check"] = {
                "performed": False,
                "difference_ratio": None,
                "threshold": threshold,
                "conflict": False,
                "temporal_mismatch": False,
                "primary_selected_as_of": None,
                "secondary_selected_as_of": secondary.get("selected_as_of") or secondary.get("data_timestamp"),
            }
        item["freshness"] = _freshness(item["data_timestamp"], effective_cutoff, max_staleness_hours)
        if item["freshness"]["stale"]:
            errors.append({"symbol": symbol, "error_type": "stale_market_data", "message": item["freshness"].get("reason", "stale")})
        if item["cross_check"]["conflict"]:
            errors.append({"symbol": symbol, "error_type": "source_conflict", "message": "primary and secondary prices exceed configured difference threshold"})
        if crosscheck and item["cross_check"]["temporal_mismatch"]:
            errors.append({"symbol": symbol, "error_type": "crosscheck_temporal_mismatch", "message": "primary and secondary selected observations have different timestamps"})
        if crosscheck and secondary is None and not massive_fallback_mode:
            errors.append({"symbol": symbol, "error_type": "source_cross_check_missing", "message": "secondary source unavailable"})
        quotes.append(item)

    quote_map = {item["symbol"]: item for item in quotes}
    missing_required = sorted(required - set(quote_map))
    unresolved_conflicts = [item["symbol"] for item in quotes if item["cross_check"]["conflict"]]
    status = "success" if not missing_required and not unresolved_conflicts and not any(
        error.get("symbol") in required and error.get("error_type") in {"source_cross_check_missing", "source_conflict", "crosscheck_temporal_mismatch", "stale_market_data"}
        for error in errors
    ) else "failed"
    run_id = os.environ.get("MARKET_RUN_ID", "unspecified")
    for event in provider_events:
        event.setdefault("run_id", run_id)
    normalized = {
        "edition": edition,
        "timezone": "Asia/Tokyo",
        "market_session": context.market_session,
        "data_cutoff": effective_cutoff.isoformat(),
        "requested_as_of": requested_as_of.isoformat() if requested_as_of else None,
        "selection_mode": "as_of" if requested_as_of else "latest",
        "collected_at": _now(),
        "status": status,
        "source_status": source_status,
        "quotes": quotes,
        "required_symbols": sorted(required),
        "missing_required_symbols": missing_required,
        "unresolved_conflicts": unresolved_conflicts,
        "errors": errors,
        "provider_events": provider_events,
        "require_crosscheck": crosscheck,
        "conflict_threshold": threshold,
        "max_staleness_hours": max_staleness_hours,
        "policy_path": str(POLICY_PATH),
    }
    normalized["market_data_version"] = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return normalized


def write_artifact(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect source-backed structured market quotes.")
    parser.add_argument("--edition", choices=["morning_close_review", "evening_premarket_watch"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbols", default=os.environ.get("MARKET_STOCK_SYMBOLS", ",".join(DEFAULT_STOCKS)))
    args = parser.parse_args(argv)
    symbols = list(dict.fromkeys([*CORE_SYMBOLS, *(item.strip().upper() for item in args.symbols.split(",") if item.strip())]))
    try:
        # Historical fallback decisions must use the edition cutoff rather
        # than an unconstrained "latest" query.
        payload = collect_quotes(
            args.edition,
            symbols=symbols,
            as_of=resolve_edition_context(args.edition).scheduled_cutoff,
        )
        write_artifact(payload, args.output)
    except Exception as exc:
        payload = {"edition": args.edition, "status": "failed", "errors": [{"error_type": type(exc).__name__, "message": str(exc)[:300]}], "quotes": []}
        write_artifact(payload, args.output)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "status": payload["status"], "quote_count": len(payload["quotes"]), "market_data_version": payload["market_data_version"]}, ensure_ascii=False))
    return 0 if payload["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
