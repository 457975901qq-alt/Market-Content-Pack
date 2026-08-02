"""Real structured market-quote collection with source cross-checking.

The collector never invents values. A quote is usable only when its primary
source has a complete numeric payload and, by default, a second source agrees.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from edition_profiles import resolve_edition_context


ROOT = Path(__file__).resolve().parent
_CANARY_FAULTS_INJECTED: set[str] = set()
POLICY_PATH = Path(os.environ.get("MARKET_DATA_POLICY_PATH", str(ROOT / "config" / "market_data_policy.json"))).expanduser()
CORE_SYMBOLS = ("SPX", "NDX", "DJI")
DEFAULT_STOCKS = ("NVDA", "MSFT", "AAPL")
SYMBOL_MAP = {
    "SPX": {"yahoo": "^GSPC", "google": (".INX", "INDEXSP", "S\\u0026P 500"), "stooq": "^spx", "asset_type": "index", "display_name": "标普500"},
    "NDX": {"yahoo": "^NDX", "google": ("NDX", "INDEXNASDAQ", "Nasdaq-100"), "stooq": "^ndq", "asset_type": "index", "display_name": "纳斯达克100"},
    "DJI": {"yahoo": "^DJI", "google": (".DJI", "INDEXDJX", "Dow Jones Industrial Average"), "stooq": "^dji", "asset_type": "index", "display_name": "道琼斯"},
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


def _request_json(url: str, timeout: float = 15) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("market_source_payload_not_object")
    return payload


def _request_text(url: str, timeout: float = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "daily-market-content-pack/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_yahoo(symbol: str, timeout: float = 15) -> dict[str, Any]:
    mapped = SYMBOL_MAP.get(symbol, {"yahoo": symbol, "asset_type": "stock", "display_name": symbol})
    encoded = urllib.parse.quote(str(mapped["yahoo"]), safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
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
    return {
        "provider": "yahoo_chart",
        "source_url": url,
        "current_price": current,
        "previous_close": float(previous),
        "price_series": [value for _, value in points],
        "data_timestamp": dt.datetime.fromtimestamp(points[-1][0], dt.timezone.utc).isoformat(),
        "currency": meta.get("currency") or "USD",
        "unit": "price",
    }


def _fetch_stooq(symbol: str, timeout: float = 15) -> dict[str, Any]:
    mapped = SYMBOL_MAP.get(symbol, {"stooq": f"{symbol.lower()}.us"})
    stooq_symbol = str(mapped.get("stooq") or f"{symbol.lower()}.us")
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(stooq_symbol, safe='^')}&i=d"
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
    return {
        "provider": "stooq_csv",
        "source_url": url,
        "current_price": points[-1][1],
        "previous_close": previous,
        "price_series": [value for _, value in points],
        "data_timestamp": points[-1][0].isoformat(),
        "currency": "USD",
        "unit": "price",
    }


def _fetch_google_finance(symbol: str, timeout: float = 15) -> dict[str, Any]:
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


def _fetch_secondary_market_source(symbol: str, timeout: float = 15) -> dict[str, Any]:
    provider = os.environ.get("MARKET_SECONDARY_PROVIDER", "google_finance").strip().lower()
    if provider == "google_finance":
        return _fetch_google_finance(symbol, timeout)
    if provider == "stooq_csv":
        return _fetch_stooq(symbol, timeout)
    raise ValueError(f"unsupported_secondary_provider:{provider}")


def _record_source(source_status: dict[str, Any], provider: str, success: bool) -> None:
    item = source_status.setdefault(provider, {"status": "unknown", "count": 0})
    if success:
        item["count"] = int(item.get("count", 0)) + 1
        item["status"] = "healthy"
    elif item.get("status") != "healthy":
        item["status"] = "failed"


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


def _quote(symbol: str, primary: dict[str, Any], secondary: dict[str, Any] | None, threshold: float) -> dict[str, Any]:
    current = float(primary["current_price"])
    previous = float(primary["previous_close"])
    change = (current - previous) / previous * 100
    direction = "up" if change > 0 else "down" if change < 0 else "flat"
    conflict = False
    difference = None
    if secondary is not None:
        secondary_current = float(secondary["current_price"])
        difference = abs(current - secondary_current) / max(abs(current), 1e-12)
        conflict = difference > threshold
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
        },
    }


def collect_quotes(
    edition: str,
    *,
    symbols: list[str] | None = None,
    require_crosscheck: bool | None = None,
    fetch_primary: Any = _fetch_yahoo,
    fetch_secondary: Any = _fetch_secondary_market_source,
) -> dict[str, Any]:
    context = resolve_edition_context(edition)
    selected = list(dict.fromkeys(symbols or [*CORE_SYMBOLS, *DEFAULT_STOCKS]))
    if os.environ.get("SELF_HEALING_CANARY_MODE", "false").lower() == "true" and os.environ.get("SELF_HEALING_FAULT", "none").strip() == "market_data_incomplete" and "market_data_incomplete" not in _CANARY_FAULTS_INJECTED:
        _CANARY_FAULTS_INJECTED.add("market_data_incomplete")
        selected = [symbol for symbol in selected if symbol not in {"SPX", "NDX", "DJI"}]
    policy = _policy()
    required = set(policy.get("required_symbols") or CORE_SYMBOLS)
    threshold = float(os.environ.get("MARKET_SOURCE_CONFLICT_THRESHOLD", policy.get("conflict_threshold", 0.02)))
    crosscheck = _env_bool("MARKET_REQUIRE_CROSSCHECK", bool(policy.get("require_crosscheck", True))) if require_crosscheck is None else require_crosscheck
    max_staleness_hours = float(os.environ.get("MARKET_MAX_STALENESS_HOURS", policy.get("max_staleness_hours", 120)))
    quotes: list[dict[str, Any]] = []
    source_status: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    def fetch_pair(symbol: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
        primary: dict[str, Any] | None = None
        secondary: dict[str, Any] | None = None
        pair_errors: list[dict[str, Any]] = []
        try:
            primary = fetch_primary(symbol)
        except Exception as exc:
            pair_errors.append({"symbol": symbol, "source": "yahoo_chart", "error_type": type(exc).__name__, "message": str(exc)[:300]})
        try:
            secondary = fetch_secondary(symbol)
        except Exception as exc:
            pair_errors.append({"symbol": symbol, "source": os.environ.get("MARKET_SECONDARY_PROVIDER", "google_finance"), "error_type": type(exc).__name__, "message": str(exc)[:300]})
        return symbol, primary, secondary, pair_errors

    workers = max(1, min(int(os.environ.get("MARKET_SOURCE_MAX_WORKERS", policy.get("max_workers", 4))), len(selected)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-quote") as pool:
        fetched = list(pool.map(fetch_pair, selected))
    for symbol, primary, secondary, pair_errors in fetched:
        errors.extend(pair_errors)
        if primary is not None:
            _record_source(source_status, str(primary.get("provider", "primary")), True)
        else:
            _record_source(source_status, "yahoo_chart", False)
        if secondary is not None:
            _record_source(source_status, str(secondary.get("provider", "secondary")), True)
        else:
            _record_source(source_status, os.environ.get("MARKET_SECONDARY_PROVIDER", "google_finance"), False)
        if primary is None:
            continue
        item = _quote(symbol, primary, secondary, threshold)
        item["freshness"] = _freshness(item["data_timestamp"], context.scheduled_cutoff, max_staleness_hours)
        if item["freshness"]["stale"]:
            errors.append({"symbol": symbol, "error_type": "stale_market_data", "message": item["freshness"].get("reason", "stale")})
        if item["cross_check"]["conflict"]:
            errors.append({"symbol": symbol, "error_type": "source_conflict", "message": "primary and secondary prices exceed configured difference threshold"})
        if crosscheck and secondary is None:
            errors.append({"symbol": symbol, "error_type": "source_cross_check_missing", "message": "secondary source unavailable"})
        quotes.append(item)

    quote_map = {item["symbol"]: item for item in quotes}
    missing_required = sorted(required - set(quote_map))
    unresolved_conflicts = [item["symbol"] for item in quotes if item["cross_check"]["conflict"]]
    status = "success" if not missing_required and not unresolved_conflicts and not any(
        error.get("symbol") in required and error.get("error_type") in {"source_cross_check_missing", "source_conflict", "stale_market_data"}
        for error in errors
    ) else "failed"
    normalized = {
        "edition": edition,
        "timezone": "Asia/Tokyo",
        "market_session": context.market_session,
        "data_cutoff": context.scheduled_cutoff.isoformat(),
        "collected_at": _now(),
        "status": status,
        "source_status": source_status,
        "quotes": quotes,
        "required_symbols": sorted(required),
        "missing_required_symbols": missing_required,
        "unresolved_conflicts": unresolved_conflicts,
        "errors": errors,
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
        payload = collect_quotes(args.edition, symbols=symbols)
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
