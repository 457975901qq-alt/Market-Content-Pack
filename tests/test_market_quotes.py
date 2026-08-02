from __future__ import annotations

import datetime as dt

from edition_profiles import resolve_edition_context
from market_quotes import _freshness, collect_quotes


TEST_DATA_TIMESTAMP = (resolve_edition_context("morning_close_review").scheduled_cutoff - dt.timedelta(hours=1)).isoformat()


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
