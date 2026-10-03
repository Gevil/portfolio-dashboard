"""US live reference feeds: subscription set from the registry, reconnect
policy, tick gating and session helpers (no sockets).

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import time as real_time
import types

import pytest

from app.api import live_ws, registry

ENTRIES = [
    {"id": "ASML", "kind": "equity", "providers": {"yahoo": "ASML.AS", "twelvedata": "ASML", "finnhub": "ASML"},
     "listing": {"symbol": "ASML.AS", "venue": "XAMS", "currency": "EUR"}},
    {"id": "NVDA", "kind": "equity", "providers": {"yahoo": "NVD.DE", "twelvedata": "NVDA", "finnhub": None},
     "listing": {"symbol": "NVD.DE", "venue": "XETR", "currency": "EUR"}},
    {"id": "SXR8", "kind": "etf", "providers": {"yahoo": "SXR8.DE", "twelvedata": None, "finnhub": None},
     "listing": {"symbol": "SXR8.DE", "venue": "XETR", "currency": "EUR"}},
    {"id": "GSPC", "kind": "index", "providers": {"yahoo": "^GSPC", "twelvedata": None, "finnhub": None}},
]


@pytest.fixture(autouse=True)
def reg(monkeypatch):
    state = {"raw": [dict(e) for e in ENTRIES]}
    monkeypatch.setattr(registry, "_raw_watchlist", lambda: state["raw"])
    monkeypatch.setattr(registry, "_memo_raw", None)
    live_ws.live_prices.clear()
    yield state
    live_ws.live_prices.clear()


# ---------------------------------------------------------------- subscription set

def test_only_symbols_with_a_provider_mapping_are_subscribed():
    assert live_ws._desired("twelvedata") == {"ASML": "ASML", "NVDA": "NVDA"}
    assert live_ws._desired("finnhub") == {"ASML": "ASML"}     # unmapped -> not subscribed
    # no ticker (GSPC, SXR8) is ever subscribed under its raw id
    assert "GSPC" not in live_ws._desired("twelvedata")
    assert "SXR8" not in live_ws._desired("finnhub")


def test_watchlist_change_changes_the_desired_set_without_restart(reg):
    reg["raw"].append({"id": "AMD", "kind": "equity",
                       "providers": {"yahoo": "AMD.DE", "twelvedata": "AMD", "finnhub": "AMD"},
                       "listing": {"symbol": "AMD.DE", "venue": "XETR", "currency": "EUR"}})
    assert live_ws._desired("twelvedata") == {"ASML": "ASML", "NVDA": "NVDA", "AMD": "AMD"}
    reg["raw"][:] = [reg["raw"][0]]
    assert live_ws._desired("twelvedata") == {"ASML": "ASML"}


def test_feed_sync_starts_restarts_and_stops_feeds(monkeypatch, reg):
    started = []

    async def fake_loop(sub_map):
        started.append(dict(sub_map))
        await asyncio.sleep(3600)

    monkeypatch.setattr(live_ws, "TWELVE_DATA_API_KEY", "k")
    monkeypatch.setattr(live_ws, "FINNHUB_API_KEY", "")
    monkeypatch.setattr(live_ws, "_twelvedata_ws_loop", fake_loop)
    monkeypatch.setattr(live_ws, "_feed_tasks", {})

    async def go():
        await live_ws._sync_feeds_once()
        await asyncio.sleep(0)
        await live_ws._sync_feeds_once()                # unchanged -> no restart
        await asyncio.sleep(0)
        reg["raw"][:] = [reg["raw"][0]]                 # NVDA removed
        await live_ws._sync_feeds_once()
        await asyncio.sleep(0)
        reg["raw"][:] = [dict(ENTRIES[2])]              # nothing streamable left
        await live_ws._sync_feeds_once()
        return dict(live_ws._feed_tasks)
    left = asyncio.run(go())
    assert started == [{"ASML": "ASML", "NVDA": "NVDA"}, {"ASML": "ASML"}]
    assert left == {}


def test_feed_sync_drops_ticks_of_removed_symbols(monkeypatch, reg):
    live_ws.live_prices["NVDA"] = {"price": 1.0, "ts": 1, "source": "x"}
    monkeypatch.setattr(live_ws, "TWELVE_DATA_API_KEY", "")
    monkeypatch.setattr(live_ws, "FINNHUB_API_KEY", "")
    reg["raw"][:] = [reg["raw"][0]]
    asyncio.run(live_ws._sync_feeds_once())
    assert "NVDA" not in live_ws.live_prices


# ---------------------------------------------------------------- reconnect policy

def test_never_connected_only_backs_off():
    sleep, backoff, ff = live_ws._after_session(None, 1000, 4, 2)
    assert (sleep, backoff, ff) == (4, 8, 2)
    assert live_ws._after_session(None, 1000, 60, 0)[1] == live_ws.WS_BACKOFF_MAX


def test_short_session_does_not_reset_the_backoff():
    # connected for 5 s, then dropped: the old code reset to 1 s on the first event
    sleep, backoff, ff = live_ws._after_session(995, 1000, 8, 0)
    assert sleep == 8 and backoff == 16 and ff == 1


def test_healthy_session_resets_backoff_and_fast_fail_count():
    sleep, backoff, ff = live_ws._after_session(900, 1000, 32, 2)
    assert (sleep, backoff, ff) == (live_ws.WS_BACKOFF_START, live_ws.WS_BACKOFF_START, 0)
    # exactly at the threshold counts as healthy
    assert live_ws._after_session(1000 - live_ws.HEALTHY_SESSION_S, 1000, 32, 2)[2] == 0


def test_repeated_fast_failures_park_the_feed():
    ff, backoff = 0, 1
    sleeps = []
    for _ in range(live_ws.WS_FAST_FAIL_LIMIT):
        sleep, backoff, ff = live_ws._after_session(999, 1000, backoff, ff)
        sleeps.append(sleep)
    assert sleeps[-1] == live_ws.WS_FAST_FAIL_PARK and ff == 0
    assert sleeps[0] < live_ws.WS_FAST_FAIL_PARK


# ---------------------------------------------------------------- ticks

def test_tick_gating(monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(live_ws, "time", types.SimpleNamespace(time=lambda: now))
    live_ws._apply_tick("ASML", 2000.0, now - 10, "twelvedata")
    assert live_ws.live_prices["ASML"] == {"price": 2000.0, "ts": now - 10,
                                           "source": "twelvedata", "currency": "USD"}
    live_ws._apply_tick("ASML", 1.0, now - 500, "finnhub")             # stale replay: ignored
    live_ws._apply_tick("ASML", 1.0, now - 20, "finnhub")              # older than the held tick
    live_ws._apply_tick("ASML", float("nan"), now, "finnhub")
    live_ws._apply_tick("ASML", -3.0, now, "finnhub")
    live_ws._apply_tick("ASML", "x", now, "finnhub")
    assert live_ws.live_prices["ASML"]["price"] == 2000.0
    live_ws._apply_tick("ASML", 2001.0, now - 5, "finnhub")
    assert live_ws.live_prices["ASML"]["price"] == 2001.0


def test_parse_epoch_ts_variants():
    assert live_ws._parse_epoch_ts(1_750_000_000) == 1_750_000_000
    assert live_ws._parse_epoch_ts(1_750_000_000_000) == 1_750_000_000
    assert live_ws._parse_epoch_ts("1750000000") == 1_750_000_000
    assert live_ws._parse_epoch_ts("2026-09-12T13:45:00Z") == 1_789_220_700
    for bad in (None, "", "junk", True, 0, -5, float("nan")):
        assert live_ws._parse_epoch_ts(bad) is None


# ---------------------------------------------------------------- sessions come from the registry

def test_session_helpers_follow_the_listing_venue():
    assert live_ws.is_eu_symbol("ASML") and live_ws.is_eu_symbol("NVDA")
    assert not live_ws.is_eu_symbol("GSPC") and not live_ws.is_eu_symbol("NOPE")
    for name in ("EU_SYMBOLS", "EU_SUFFIXES", "FINNHUB_SYMBOLS", "euronext_prices",
                 "bars_1min", "bars_5min", "REST_POLL_SOURCE"):
        assert not hasattr(live_ws, name), name
    assert isinstance(live_ws.market_open("ASML"), bool)
    assert real_time.time() > 0
