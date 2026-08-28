from __future__ import annotations

import datetime as dt

from edition_profiles import resolve_edition_context
from market_quotes import CORE_SYMBOLS, _freshness, collect_quotes, market_quote_cache_key


TEST_DATA_TIMESTAMP = (resolve_edition_context("morning_close_review").scheduled_cutoff - dt.timedelta(hours=1)).isoformat()


def test_production_core_symbols_are_voo_and_qqqm() -> None:
    assert CORE_SYMBOLS == ("VOO", "QQQM")


def test_default_production_collection_requires_only_etf_proxies() -> None:
    def primary(symbol: str) -> dict:
        payload = _primary(symbol)
        payload["asset_type"] = "etf"
        payload["display_name"] = symbol
        return payload

    result = collect_quotes(
        "morning_close_review",
        fetch_primary=primary,
        fetch_secondary=_secondary,
        require_crosscheck=False,
    )
    assert result["required_symbols"] == ["QQQM", "VOO"]
    assert {item["symbol"] for item in result["quotes"] if item["symbol"] in CORE_SYMBOLS} == set(CORE_SYMBOLS)


def _primary(symbol: str) -> dict:
    return {
        "provider": "primary",
        "source_url": f"https://primary.test/{symbol}",
        "current_price": 101.0,
        "previous_close": 100.0,
        "price_series": [99.0, 100.0, 101.0],
        "data_timestamp": TEST_DATA_TIMESTAMP,
        "currency": "USD",
        "unit": "price",
    }


def _secondary(symbol: str) -> dict:
    return {
        "provider": "secondary",
        "source_url": f"https://secondary.test/{symbol}",
        "current_price": 101.2,
        "previous_close": 100.0,
        "price_series": [100.0, 101.2],
        "data_timestamp": TEST_DATA_TIMESTAMP,
        "currency": "USD",
        "unit": "price",
    }


def test_structured_quotes_require_core_symbols_and_cross_check() -> None:
    result = collect_quotes(
        "morning_close_review",
        symbols=["SPX", "NDX", "DJI"],
        require_crosscheck=True,
        fetch_primary=_primary,
        fetch_secondary=_secondary,
    )
    assert result["status"] == "success"
    assert {item["symbol"] for item in result["quotes"]} == {"SPX", "NDX", "DJI"}
    assert all(item["cross_check"]["performed"] for item in result["quotes"])
    assert all(item["direction"] == "up" for item in result["quotes"])


def test_missing_secondary_source_blocks_required_cross_check() -> None:
    def missing_secondary(symbol: str) -> dict:
        raise TimeoutError("secondary unavailable")

    result = collect_quotes(
        "evening_premarket_watch",
        symbols=["SPX", "NDX", "DJI"],
        require_crosscheck=True,
        fetch_primary=_primary,
        fetch_secondary=missing_secondary,
    )
    assert result["status"] == "failed"
    assert any(item["error_type"] == "source_cross_check_missing" for item in result["errors"])


def test_unresolved_price_conflict_blocks_market_data() -> None:
    def conflicting(symbol: str) -> dict:
        value = _secondary(symbol)
        value["current_price"] = 130.0
        return value

    result = collect_quotes(
        "morning_close_review",
        symbols=["SPX", "NDX", "DJI"],
        require_crosscheck=True,
        fetch_primary=_primary,
        fetch_secondary=conflicting,
    )
    assert result["status"] == "failed"
    assert set(result["unresolved_conflicts"]) == {"SPX", "NDX", "DJI"}


def test_future_or_old_market_timestamp_is_rejected() -> None:
    import datetime as dt

    cutoff = dt.datetime(2026, 7, 20, 17, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    future = _freshness("2026-07-21T00:00:00+00:00", cutoff, 120)
    old = _freshness("2026-07-10T00:00:00+00:00", cutoff, 120)
    valid = _freshness("2026-07-17T13:30:00+00:00", cutoff, 120)
    assert future["stale"] is True
    assert old["stale"] is True
    assert valid["stale"] is False


def test_evening_1730_cutoff_accepts_previous_us_close_at_1732_jst() -> None:
    started_at = dt.datetime(2026, 8, 7, 17, 32, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    context = resolve_edition_context("evening_premarket_watch", started_at)

    assert context.scheduled_local_time == "17:30"
    assert context.scheduled_cutoff == started_at.replace(hour=17, minute=30, second=0, microsecond=0)

    previous_us_close = _freshness("2026-08-06T13:30:00+00:00", context.scheduled_cutoff, 120)
    assert previous_us_close["stale"] is False
    assert previous_us_close["reason"] is None


def _historical_primary(symbol: str, as_of=None) -> dict:
    return {
        "provider": "primary",
        "source_url": f"https://primary.test/{symbol}",
        "current_price": 999.0,
        "previous_close": 98.0,
        "data_timestamp": "2026-08-20T22:30:00+09:00",
        "observations": [
            {"timestamp": "2026-08-20T16:55:00+09:00", "value": 100.0},
            {"timestamp": "2026-08-20T17:10:00+09:00", "value": 101.0},
            {"timestamp": "2026-08-20T17:25:00+09:00", "value": 102.0},
            {"timestamp": "2026-08-20T17:45:00+09:00", "value": 103.0},
            {"timestamp": "2026-08-20T22:30:00+09:00", "value": 104.0},
        ],
        "price_series": [100.0, 101.0, 102.0, 103.0, 104.0],
        "currency": "USD",
        "unit": "price",
    }


def _historical_secondary(symbol: str, as_of=None) -> dict:
    payload = _historical_primary(symbol, as_of)
    payload["provider"] = "secondary"
    payload["source_url"] = f"https://secondary.test/{symbol}"
    return payload


def test_latest_quote_without_as_of_preserves_latest_mode() -> None:
    result = collect_quotes("morning_close_review", symbols=["SPX"], require_crosscheck=False, fetch_primary=_historical_primary, fetch_secondary=_historical_secondary)
    quote = result["quotes"][0]
    assert quote["current_price"] == 999.0
    assert quote["selection_mode"] == "latest"
    assert quote["requested_as_of"] is None


def test_as_of_selects_last_real_observation_before_cutoff() -> None:
    cutoff = "2026-08-20T17:30:00+09:00"
    result = collect_quotes("evening_premarket_watch", symbols=["SPX"], required_symbols=["SPX"], require_crosscheck=True, as_of=cutoff, fetch_primary=_historical_primary, fetch_secondary=_historical_secondary)
    quote = result["quotes"][0]
    assert result["status"] == "success"
    assert quote["current_price"] == 102.0
    assert quote["data_timestamp"] == "2026-08-20T08:25:00+00:00"
    assert quote["requested_as_of"] == "2026-08-20T08:30:00+00:00"
    assert quote["selection_mode"] == "as_of"
    assert quote["sources"]["primary"]["requested_as_of"] == quote["sources"]["secondary"]["requested_as_of"]


def test_as_of_never_selects_post_cutoff_observation() -> None:
    result = collect_quotes("evening_premarket_watch", symbols=["SPX"], required_symbols=["SPX"], require_crosscheck=False, as_of="2026-08-20T17:30:00+09:00", fetch_primary=_historical_primary, fetch_secondary=_historical_secondary)
    assert all(item["data_timestamp"] <= "2026-08-20T08:30:00+00:00" for item in result["quotes"])


def test_crosscheck_rejects_different_selected_observation_timestamps() -> None:
    def shifted_secondary(symbol: str, as_of=None) -> dict:
        payload = _historical_secondary(symbol, as_of)
        payload["observations"] = [
            {"timestamp": "2026-08-20T17:20:00+09:00", "value": 102.0},
        ]
        return payload

    result = collect_quotes(
        "evening_premarket_watch",
        symbols=["SPX"],
        required_symbols=["SPX"],
        require_crosscheck=True,
        as_of="2026-08-20T17:30:00+09:00",
        fetch_primary=_historical_primary,
        fetch_secondary=shifted_secondary,
    )
    assert result["status"] == "failed"
    assert result["quotes"][0]["cross_check"]["temporal_mismatch"] is True
    assert any(item["error_type"] == "crosscheck_temporal_mismatch" for item in result["errors"])


def test_as_of_no_data_before_cutoff_is_explicit_failure() -> None:
    def future_only(symbol: str, as_of=None) -> dict:
        payload = _historical_primary(symbol, as_of)
        payload["observations"] = [{"timestamp": "2026-08-20T18:00:00+09:00", "value": 105.0}]
        return payload

    result = collect_quotes("evening_premarket_watch", symbols=["SPX"], require_crosscheck=False, as_of="2026-08-20T17:30:00+09:00", fetch_primary=future_only, fetch_secondary=future_only)
    assert result["status"] == "failed"
    assert any(item["error_type"] == "market_data_after_cutoff" for item in result["errors"])


def test_as_of_without_any_observation_returns_no_data_failure() -> None:
    def empty_history(symbol: str, as_of=None) -> dict:
        payload = _historical_primary(symbol, as_of)
        payload["observations"] = []
        payload["data_timestamp"] = None
        return payload

    result = collect_quotes("evening_premarket_watch", symbols=["SPX"], required_symbols=["SPX"], require_crosscheck=False, as_of="2026-08-20T17:30:00+09:00", fetch_primary=empty_history, fetch_secondary=empty_history)
    assert result["status"] == "failed"
    assert any(item["error_type"] == "no_market_data_at_or_before_cutoff" for item in result["errors"])


def test_as_of_requires_timezone_aware_timestamp() -> None:
    result = collect_quotes("evening_premarket_watch", symbols=["SPX"], require_crosscheck=False, as_of="2026-08-20T17:30:00", fetch_primary=_historical_primary, fetch_secondary=_historical_secondary)
    assert result["status"] == "failed"
    assert result["errors"][0]["error_type"] == "timezone_missing"


def test_market_quote_cache_keys_isolate_latest_and_as_of_snapshots() -> None:
    latest = market_quote_cache_key("SPX", provider="primary")
    as_of = market_quote_cache_key("SPX", provider="primary", as_of="2026-08-20T17:30:00+09:00")
    other_as_of = market_quote_cache_key("SPX", provider="primary", as_of="2026-08-20T18:30:00+09:00")
    assert latest != as_of
    assert as_of != other_as_of
    assert as_of == market_quote_cache_key("SPX", provider="primary", as_of="2026-08-20T08:30:00Z")


def test_market_data_version_changes_for_different_as_of_snapshots() -> None:
    first = collect_quotes("evening_premarket_watch", symbols=["SPX"], required_symbols=["SPX"], require_crosscheck=False, as_of="2026-08-20T17:30:00+09:00", fetch_primary=_historical_primary, fetch_secondary=_historical_secondary)
    second = collect_quotes("evening_premarket_watch", symbols=["SPX"], required_symbols=["SPX"], require_crosscheck=False, as_of="2026-08-20T18:30:00+09:00", fetch_primary=_historical_primary, fetch_secondary=_historical_secondary)
    assert first["market_data_version"] != second["market_data_version"]


def test_massive_index_adapter_selects_last_bar_before_cutoff_without_persisting_key(monkeypatch) -> None:
    import market_quotes

    class Secret:
        def reveal(self, purpose: str) -> str:
            assert purpose == "collect_quotes"
            return "test-massive-key"

    monkeypatch.setattr(market_quotes, "get_secret", lambda *args, **kwargs: Secret())
    captured: dict[str, object] = {}

    def fake_request(url: str, timeout: float = 15, headers=None) -> dict:
        captured["url"] = url
        captured["headers"] = headers
        return {
            "status": "OK",
            "ticker": "I:SPX",
            "results": [
                {"t": 1787213400000, "c": 100.0},  # 17:10 JST
                {"t": 1787214300000, "c": 101.0},  # 17:25 JST
                {"t": 1787215500000, "c": 102.0},  # 17:45 JST, must be ignored
            ],
        }

    monkeypatch.setattr(market_quotes, "_request_json", fake_request)
    result = market_quotes._fetch_massive("SPX", as_of="2026-08-20T17:30:00+09:00")

    assert result["provider"] == "massive_indices"
    assert result["instrument"] == "I:SPX"
    assert result["current_price"] == 101.0
    assert result["selection_mode"] == "as_of"
    assert "apiKey=" not in str(captured["url"])
    assert captured["headers"] == {"Authorization": "Bearer test-massive-key"}


def _massive_daily_rows(count: int = 126, *, start: dt.date = dt.date(2026, 2, 23), duplicate: bool = False, null_close: bool = False, invalid_ohlc: bool = False) -> list[dict]:
    rows: list[dict] = []
    current = start
    while len(rows) < count:
        if current.weekday() < 5:
            value = float(29000 + len(rows))
            row = {
                "t": int(dt.datetime.combine(current, dt.time(0), tzinfo=dt.timezone.utc).timestamp() * 1000),
                "o": value - 5,
                "h": value + 10,
                "l": value - 10,
                "c": None if null_close and len(rows) == count - 1 else value,
                "v": 1000,
            }
            if invalid_ohlc and len(rows) == count - 1:
                row["h"] = value - 20
                row["l"] = value - 10
            rows.append(row)
            if duplicate and len(rows) == count - 1:
                rows.append(dict(row))
        current += dt.timedelta(days=1)
    return rows


class _FakeMassiveResponse:
    status = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        import json
        return json.dumps(self.payload).encode()


def _patch_massive_response(monkeypatch, payload: dict):
    import market_quotes

    class Secret:
        def reveal(self, purpose: str) -> str:
            assert purpose == "collect_quotes"
            return "test-massive-key"

    monkeypatch.setattr(market_quotes, "get_secret", lambda *args, **kwargs: Secret())
    monkeypatch.setattr(market_quotes.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(market_quotes.urllib.request, "urlopen", lambda *args, **kwargs: _FakeMassiveResponse(payload))


def test_massive_primary_success_does_not_call_fallback(monkeypatch) -> None:
    import market_quotes

    called = []
    monkeypatch.setattr(market_quotes, "_massive_ndx_historical_daily", lambda *args, **kwargs: called.append(True))
    result = collect_quotes("evening_premarket_watch", symbols=["NDX"], required_symbols=["NDX"], require_crosscheck=True, as_of="2026-08-20T17:30:00+09:00", fetch_primary=_historical_primary)
    assert result["status"] == "success"
    assert called == []
    assert result["quotes"][0]["provider"] == "primary"


def test_massive_fallback_after_primary_timeout_passes(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "DELAYED", "results": _massive_daily_rows()})

    def timeout(_symbol: str, as_of=None):
        raise TimeoutError("primary timeout")

    result = collect_quotes("evening_premarket_watch", symbols=["NDX"], required_symbols=["NDX"], require_crosscheck=True, as_of="2026-08-20T17:30:00+09:00", fetch_primary=timeout)
    quote = result["quotes"][0]
    assert result["status"] == "success"
    assert quote["provider"] == "massive"
    assert quote["provider_role"] == "historical_secondary"
    assert quote["fallback_used"] is True
    assert quote["massive_status"] == "DELAYED"
    assert quote["bar_count"] >= 90
    assert any(event["event"] == "NDX_HISTORICAL_FALLBACK_MASSIVE_OK" for event in result["provider_events"])


def test_massive_fallback_after_primary_no_data_passes(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "OK", "results": _massive_daily_rows()})

    def no_data(_symbol: str, as_of=None):
        raise ValueError("yahoo_empty_result")

    result = collect_quotes("evening_premarket_watch", symbols=["NDX"], required_symbols=["NDX"], as_of="2026-08-20T17:30:00+09:00", fetch_primary=no_data)
    assert result["status"] == "success"
    assert result["quotes"][0]["provider"] == "massive"


def test_massive_89_bars_is_rejected(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "OK", "results": _massive_daily_rows(89, start=dt.date(2026, 4, 14))})
    try:
        market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    except market_quotes.MassiveProviderError as exc:
        assert exc.code == "INSUFFICIENT_HISTORY"
    else:
        raise AssertionError("expected insufficient history")


def test_massive_delayed_status_with_90_bars_passes(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "DELAYED", "results": _massive_daily_rows(90, start=dt.date(2026, 4, 14))})
    result = market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    assert result["validated"] is True
    assert result["provider_status"] == "DELAYED"


def test_massive_stale_latest_date_is_rejected(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "OK", "results": _massive_daily_rows(126, start=dt.date(2025, 12, 1))})
    try:
        market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    except market_quotes.MassiveProviderError as exc:
        assert exc.code == "STALE_DATA"
    else:
        raise AssertionError("expected stale data")


def test_massive_403_is_not_entitled_and_not_retried(monkeypatch) -> None:
    import market_quotes
    import urllib.error

    _patch_massive_response(monkeypatch, {})
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://api.massive.com", 403, "Forbidden", {}, __import__("io").BytesIO(b'{"status":"ERROR","error":{"code":"PLAN_RESTRICTED","message":"plan restricted"}}'))

    monkeypatch.setattr(market_quotes.urllib.request, "urlopen", forbidden)
    try:
        market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    except market_quotes.MassiveProviderError as exc:
        assert exc.code == "MASSIVE_NOT_ENTITLED"
        assert exc.http_status == 403
    else:
        raise AssertionError("expected entitlement error")
    assert len(calls) == 1


def test_massive_429_retries_three_times(monkeypatch) -> None:
    import market_quotes
    import urllib.error

    _patch_massive_response(monkeypatch, {})
    calls = []

    def limited(*_args, **_kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://api.massive.com", 429, "Too Many Requests", {}, __import__("io").BytesIO(b'{"status":"ERROR","error":{"code":"RATE_LIMITED","message":"rate limited"}}'))

    monkeypatch.setattr(market_quotes.urllib.request, "urlopen", limited)
    try:
        market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    except market_quotes.MassiveProviderError as exc:
        assert exc.code == "MASSIVE_RATE_LIMITED"
        assert exc.retryable is True
    else:
        raise AssertionError("expected rate limit error")
    assert len(calls) == 3


def test_massive_duplicate_dates_are_deduplicated(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "OK", "results": _massive_daily_rows(126, duplicate=True)})
    result = market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    assert result["bar_count"] >= 90
    assert len({bar["date"] for bar in result["bars"]}) == result["bar_count"]


def test_massive_null_close_does_not_pass(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "OK", "results": _massive_daily_rows(90, start=dt.date(2026, 4, 14), null_close=True)})
    try:
        market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    except market_quotes.MassiveProviderError as exc:
        assert exc.code == "INSUFFICIENT_HISTORY"
    else:
        raise AssertionError("expected invalid bar to reduce history")


def test_massive_invalid_high_low_is_rejected(monkeypatch) -> None:
    import market_quotes

    _patch_massive_response(monkeypatch, {"status": "OK", "results": _massive_daily_rows(90, start=dt.date(2026, 4, 14), invalid_ohlc=True)})
    try:
        market_quotes._massive_ndx_historical_daily(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc))
    except market_quotes.MassiveProviderError as exc:
        assert exc.code == "MASSIVE_INVALID_OHLC"
    else:
        raise AssertionError("expected invalid OHLC")


def test_primary_programming_error_does_not_trigger_massive(monkeypatch) -> None:
    import market_quotes

    called = []
    monkeypatch.setattr(market_quotes, "_massive_ndx_historical_daily", lambda *args, **kwargs: called.append(True))

    def programming_error(_symbol: str, as_of=None):
        raise ValueError("programming_error")

    result = collect_quotes("evening_premarket_watch", symbols=["NDX"], required_symbols=["NDX"], as_of="2026-08-20T17:30:00+09:00", fetch_primary=programming_error)
    assert result["status"] == "failed"
    assert called == []
