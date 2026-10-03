"""Listing-series price pipeline (no network: yahoo is faked, time is frozen).

Run inside the service image so dependencies (yfinance etc.) resolve:
  podman run --rm -v $PWD:/work -w /work -e HISTORY_DIR=/tmp/t localhost/portfolio-dashboard:latest \
    bash -c "pip install -q pytest && PYTHONPATH=/work python -m pytest tests/unit -v"
"""
import asyncio
import copy
import datetime
import json
import math
import threading
import types

import pytest

from app.api import live_ws, prices, registry

D = 86400
NOW = 1_752_494_400  # Mon 2025-07-14 12:00:00 UTC = 14:00 CEST, inside the Euronext session
ASML_OPEN = 1_752_476_400   # Mon 2025-07-14 07:00 UTC = 09:00 CEST

ENTRIES = [
    {"id": "ASML", "kind": "equity", "role": "holding", "quoteCurrency": "EUR",
     "providers": {"yahoo": "ASML.AS", "twelvedata": "ASML", "finnhub": "ASML"},
     "listing": {"symbol": "ASML.AS", "venue": "XAMS", "currency": "EUR"}},
    {"id": "SXR8", "kind": "etf", "role": "holding", "quoteCurrency": "EUR",
     "providers": {"yahoo": "SXR8.DE", "twelvedata": None, "finnhub": None},
     "listing": {"symbol": "SXR8.DE", "venue": "XETR", "currency": "EUR"}},
    {"id": "GSPC", "kind": "index", "role": "benchmark", "quoteCurrency": "USD",
     "providers": {"yahoo": "^GSPC", "twelvedata": None, "finnhub": None},
     "listing": {"symbol": "^GSPC", "venue": "XNYS", "currency": "USD"}},
    {"id": "XYZ", "kind": "equity", "role": "holding", "quoteCurrency": "EUR",
     "providers": {"yahoo": None, "twelvedata": None, "finnhub": None},
     "listing": {"symbol": "XYZ.DE", "venue": "XETR", "currency": "EUR"}},
    {"id": "NOLIST", "kind": "index", "role": "benchmark", "providers": {}},
]


def pt(t, c=100.0, **kw):
    return {"t": int(t), "c": float(c), **kw}


def ts(y, m, d, h=0, mi=0):
    return int(datetime.datetime(y, m, d, h, mi, tzinfo=datetime.timezone.utc).timestamp())


def run(coro):
    return asyncio.run(coro)


class FakeYF:
    """Stand-in for prices._yf: answers by the blocking function that is asked."""

    def __init__(self):
        self.calls = []
        self.bars = {}      # (symbol, interval) -> list | None
        self.daily = {}     # symbol -> [(iso_date, close)] | None
        self.info = {}      # symbol -> dict | None

    async def __call__(self, key, fn, *args):
        self.calls.append((fn.__name__, args))
        if fn is prices._yf_bars_blocking:
            sym, interval = args[0], args[1]
            return self.bars.get((sym, interval))
        if fn is prices._yf_daily_blocking:
            return self.daily.get(args[0])
        if fn is prices._yf_info_blocking:
            return self.info.get(args[0])
        raise AssertionError(f"unexpected yahoo call {fn.__name__}")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Every test gets empty stores, tmp files, a frozen clock and our registry
    (the old module-level state leaked between tests)."""
    clock = types.SimpleNamespace(time=lambda: NOW)
    monkeypatch.setattr(prices, "time", clock)
    monkeypatch.setattr(live_ws, "time", clock)
    monkeypatch.setattr(prices, "HISTORY_FILE", str(tmp_path / "listing_history.json"))
    monkeypatch.setattr(prices, "VALUATION_FILE", str(tmp_path / "valuation.json"))
    monkeypatch.setattr(registry, "_raw_watchlist", lambda: ENTRIES)
    monkeypatch.setattr(registry, "_memo_raw", None)
    prices._reset_state()
    live_ws.live_prices.clear()
    yield
    prices._reset_state()
    live_ws.live_prices.clear()


@pytest.fixture
def fake_yf(monkeypatch):
    fake = FakeYF()
    monkeypatch.setattr(prices, "_yf", fake)
    return fake


def minute_bars(start, n, base=1840.0):
    return [{"t": start + i * 60, "c": base + math.sin(i / 20) * 3} for i in range(n)]


# ---------------------------------------------------------------- timestamps

def test_normalize_ts_seconds_passthrough():
    assert prices.normalize_ts(1_750_000_000) == 1_750_000_000


def test_normalize_ts_millis_to_seconds():
    assert prices.normalize_ts(1_750_000_000_000) == 1_750_000_000


def test_is_valid_ts_has_no_2030_upper_cap():
    assert prices._is_valid_ts(1577836800)
    assert not prices._is_valid_ts(1577836799)
    assert prices._is_valid_ts(1893456001)        # 2030-01-01 + 1s: used to be dropped
    assert prices._is_valid_ts(ts(2031, 6, 1))
    assert not prices._is_valid_ts("garbage") and not prices._is_valid_ts(None)


# ---------------------------------------------------------------- retention by age

def test_cutoffs_empty():
    assert prices._apply_granularity_cutoffs([], NOW) == []


def test_cutoffs_keep_one_minute_resolution_for_24h():
    pts = [pt(NOW - 3 * 3600 + i * 60) for i in range(180)]
    assert len(prices._apply_granularity_cutoffs(pts, NOW)) == 180


def test_cutoffs_collapse_to_5min_between_24h_and_7d():
    base = (NOW - 2 * D) // 300 * 300
    pts = [pt(base + 10, 1), pt(base + 70, 2), pt(base + 250, 3), pt(base + 300, 4)]
    out = prices._apply_granularity_cutoffs(pts, NOW)
    assert [p["c"] for p in out] == [3.0, 4.0]           # last point of each 5-min bucket


def test_cutoffs_collapse_to_daily_after_7d_and_drop_after_365d():
    day = (NOW - 20 * D) // D * D
    pts = [pt(day + 100, 1), pt(day + 5000, 2), pt(day + D + 5, 3),
           pt(NOW - 400 * D, 9), pt(NOW - 60, 5)]
    out = prices._apply_granularity_cutoffs(pts, NOW)
    assert [p["c"] for p in out] == [2.0, 3.0, 5.0]


def test_cutoffs_clip_by_age_not_by_gap():
    # The old gap classifier treated an isolated recent point as "daily" and
    # could drop it; age decides first now.
    pts = [pt(NOW - 10 * D, 1), pt(NOW - 3600, 2)]
    out = prices._apply_granularity_cutoffs(pts, NOW)
    assert [p["c"] for p in out] == [1.0, 2.0]


# ---------------------------------------------------------------- buckets / sparkline

def test_bucket_empty_and_single():
    assert prices._bucket_last_close([], 3600) == []
    assert prices._bucket_last_close([pt(10, 5.0)], 3600) == [pt(10, 5.0)]


def test_bucket_hourly_last_close_per_hour():
    pts = [pt(ts(2026, 7, 27, 9, 5), 100), pt(ts(2026, 7, 27, 9, 50), 102),
           pt(ts(2026, 7, 27, 10, 10), 103), pt(ts(2026, 7, 27, 10, 40), 104)]
    assert [p["c"] for p in prices._bucket_last_close(pts, 3600)] == [102.0, 104.0]


def test_bucket_daily_survives_a_weekend_gap():
    pts = [pt(ts(2026, 7, 24, 15, 30), 100.0), pt(ts(2026, 7, 27, 15, 30), 101.0)]
    assert [p["c"] for p in prices._bucket_last_close(pts, 86400)] == [100.0, 101.0]


def test_sparkline_prefers_last_24h_and_is_bounded():
    prices.history_store["ASML"] = [pt(NOW - 3600 + i * 30, 100 + i) for i in range(100)]
    spark = prices.sparkline_for("ASML", 30)
    assert len(spark) == 30 and spark[0] == 100.0
    assert prices.sparkline_for("NOPE") == []


# ---------------------------------------------------------------- persist

def test_persist_drops_nan_inf_negative_and_future_points():
    n = prices._persist_points("ASML", [
        pt(NOW - 60, 100.0), pt(NOW - 120, float("nan")), pt(NOW - 180, float("inf")),
        pt(NOW - 240, -5.0), pt(NOW - 300, 0.0), pt(NOW + 3 * D, 101.0),
        {"t": "x", "c": 1.0}, {"c": 1.0}, pt(100, 5.0),
    ])
    assert n == 1
    assert prices.history_store["ASML"] == [pt(NOW - 60, 100.0)]


def test_persist_ignores_unwatched_and_listingless_symbols():
    assert prices._persist_points("AAPL", [pt(NOW - 60)]) == 0
    assert prices._persist_points("NOLIST", [pt(NOW - 60)]) == 0
    assert prices.history_store == {}


def test_persist_collision_priority_live_beats_seed_but_not_the_reverse():
    t = NOW - 600
    prices._persist_points("ASML", [pt(t, 100.0)], src=prices.SRC_YF)
    prices._persist_points("ASML", [pt(t, 90.0)], src=prices.SRC_SEED)
    assert prices.history_store["ASML"][0]["c"] == 100.0
    prices._persist_points("ASML", [pt(t, 101.0)], src=prices.SRC_YF)   # same source: newer wins
    assert prices.history_store["ASML"][0]["c"] == 101.0
    prices._persist_points("ASML", [pt(t - 60, 50.0)], src=prices.SRC_SEED)
    prices._persist_points("ASML", [pt(t - 60, 55.0)], src=prices.SRC_YF)
    assert prices.history_store["ASML"][0] == pt(t - 60, 55.0)


def test_persist_keeps_a_known_volume_when_the_update_has_none():
    t = NOW - 600
    prices._persist_points("ASML", [pt(t, 100.0, v=1200)])
    prices._persist_points("ASML", [pt(t, 100.5)])
    assert prices.history_store["ASML"][0]["v"] == 1200


def test_persist_bumps_store_version_only_on_change():
    prices._persist_points("ASML", [pt(NOW - 60, 100.0)])
    v = prices._store_version["ASML"]
    prices._persist_points("ASML", [pt(NOW - 60, 100.0)])    # identical
    assert prices._store_version["ASML"] == v


def test_listing_change_discards_the_old_series():
    prices._persist_points("ASML", [pt(NOW - 60, 1800.0)])
    assert prices.history_store["ASML"]
    prices._history_meta["ASML"] = {"symbol": "ASML", "currency": "USD"}   # built from NASDAQ USD
    prices._persist_points("ASML", [pt(NOW - 30, 1801.0)])
    assert prices.history_store["ASML"] == [pt(NOW - 30, 1801.0)]


# ---------------------------------------------------------------- alignment of seeded bars

def test_align_moves_coarse_bars_to_their_close_slot_and_drops_future_ones():
    out = prices._align_bars("ASML", "5m", [pt(NOW - 3600, 1), pt(NOW - 100, 2)])
    assert out == [pt(NOW - 3600 + 240, 1)]              # the open 5-min bar would be in the future
    assert prices._align_bars("ASML", "1m", [pt(NOW - 60, 1)]) == [pt(NOW - 60, 1)]


def test_align_daily_bars_to_session_close_in_venue_time():
    fri_midnight_ams = ts(2025, 7, 10, 22, 0)             # Fri 2025-07-11 00:00 CEST
    mon_midnight_ams = ts(2025, 7, 13, 22, 0)             # Mon 2025-07-14 00:00 CEST (open day)
    out = prices._align_bars("ASML", "1d", [pt(fri_midnight_ams, 1), pt(mon_midnight_ams, 2)])
    assert out == [pt(ts(2025, 7, 11, 15, 30), 1)]       # 17:30 CEST; today's open bar dropped


# ---------------------------------------------------------------- single-source series regression

def test_us_live_ticks_never_enter_the_listing_series():
    """B3 regression: ASML 1D mixed src=ws (USD feed) and untagged (EUR) points
    ~1.3% apart in the same minute -> spikes. The listing series is yahoo only;
    US ticks are a separate labelled reference."""
    bars = minute_bars(ASML_OPEN, 300)
    prices._persist_points("ASML", bars, src=prices.SRC_YF)
    before = copy.deepcopy(prices.history_store["ASML"])

    # US NASDAQ ticks ~8% away (different listing + currency), inside the same minutes
    for i in range(0, 300, 7):
        live_ws._apply_tick("ASML", 2000.0 + i * 0.1, ASML_OPEN + i * 60, "twelvedata", max_stale=None)
    live_ws._apply_tick("ASML", 2010.0, NOW - 10, "finnhub")

    assert prices.history_store["ASML"] == before         # not one point was added or changed
    series = prices._series_for_range("ASML", "1D", NOW)
    assert [p["t"] for p in series] == sorted({p["t"] for p in series})   # unique, ascending
    steps = [abs(b["c"] - a["c"]) / a["c"] for a, b in zip(series, series[1:])]
    assert max(steps) < 0.005                             # no cross-source jump
    assert all(p.get("src") is None for p in series)      # no 'ws' tag anywhere
    assert live_ws.live_prices["ASML"]["currency"] == "USD"


def test_us_live_view_is_labelled_and_only_for_streamable_symbols():
    live_ws.live_prices["ASML"] = {"price": 2010.0, "ts": NOW - 30, "source": "twelvedata", "currency": "USD"}
    live_ws.live_prices["GSPC"] = {"price": 6000.0, "ts": NOW - 30, "source": "twelvedata", "currency": "USD"}
    view = prices.us_live_view("ASML")
    assert view == {"price": 2010.0, "currency": "USD", "asOf": NOW - 30, "source": "twelvedata"}
    assert prices.us_live_view("GSPC") is None            # no streaming mapping in the registry
    assert prices.us_live_view("SXR8") is None
    live_ws.live_prices["ASML"]["ts"] = NOW - 4000        # too old to show
    assert prices.us_live_view("ASML") is None
    live_ws.live_prices["ASML"] = {"price": float("nan"), "ts": NOW, "source": "x"}
    assert prices.us_live_view("ASML") is None


# ---------------------------------------------------------------- listing quote

def test_listing_quote_from_yahoo_persists_bars_and_computes_change(fake_yf):
    bars = minute_bars(ASML_OPEN, 298)                    # newest bar = NOW - 120
    fake_yf.bars[("ASML.AS", "1m")] = bars
    fake_yf.daily["ASML.AS"] = [("2025-07-11", 1790.0), ("2025-07-14", 1805.0)]
    q = run(prices.listing_quote("ASML"))
    last = bars[-1]
    assert q["id"] == "ASML" and q["price"] == last["c"] and q["asOf"] == last["t"]
    assert q["prevClose"] == 1790.0                       # last close BEFORE the quote's session
    assert q["change"] == pytest.approx(last["c"] - 1790.0)
    assert q["changePct"] == pytest.approx((last["c"] - 1790.0) / 1790.0 * 100)
    assert (q["currency"], q["source"], q["venue"]) == ("EUR", "yahoo", "XAMS")
    assert q["marketOpen"] is True and q["stale"] is False
    assert len(prices.history_store["ASML"]) == 298       # the series came from the same poll
    n_calls = len(fake_yf.calls)
    assert run(prices.listing_quote("ASML"))["price"] == last["c"]
    assert len(fake_yf.calls) == n_calls                  # TTL cache, no second request


def test_listing_quote_flags_a_stale_open_session(fake_yf):
    fake_yf.bars[("ASML.AS", "1m")] = minute_bars(NOW - 3 * 3600, 10)
    fake_yf.daily["ASML.AS"] = [("2025-07-11", 1790.0)]
    assert run(prices.listing_quote("ASML"))["stale"] is True


def test_listing_quote_unknown_or_listingless_asks_yahoo_nothing(fake_yf):
    assert run(prices.listing_quote("NOPE")) is None
    assert run(prices.listing_quote("NOLIST")) is None
    assert fake_yf.calls == [] and prices._negative_cache == {}


def test_listing_quote_without_data_sets_a_negative_entry_and_skips_next_time(fake_yf):
    fake_yf.bars[("XYZ.DE", "1m")] = []
    fake_yf.bars[("XYZ.DE", "15m")] = []
    assert run(prices.listing_quote("XYZ")) is None
    assert ("yahoo", "XYZ.DE") in prices._negative_cache
    n = len(fake_yf.calls)
    assert run(prices.listing_quote("XYZ")) is None
    assert len(fake_yf.calls) == n


def test_price_item_carries_listing_currency_and_labelled_us_reference(monkeypatch):
    live_ws.live_prices["ASML"] = {"price": 2010.0, "ts": NOW - 5, "source": "finnhub", "currency": "USD"}

    async def quote(t):
        return {"id": t, "price": 1850.0, "prevClose": 1800.0, "change": 50.0, "changePct": 2.78,
                "currency": "EUR", "asOf": NOW - 60, "source": "yahoo", "stale": False,
                "venue": "XAMS", "marketOpen": True}
    monkeypatch.setattr(prices, "listing_quote", quote)
    item = run(prices._price_item("ASML"))
    assert item["price"] == 1850.0 and item["priceCurrency"] == "EUR"
    assert item["previousClose"] == 1800.0 and item["change24h"] == 2.78
    assert item["usLive"]["currency"] == "USD" and item["usLive"]["price"] == 2010.0
    assert item["price"] != item["usLive"]["price"]       # never merged
    assert "status" not in item


def test_price_item_unsupported_and_error_states(monkeypatch):
    assert run(prices._price_item("NOLIST"))["status"] == "unsupported"

    async def none(_t):
        return None
    monkeypatch.setattr(prices, "listing_quote", none)
    item = run(prices._price_item("ASML"))
    assert item["status"] == "error" and item["price"] is None


def test_fetch_prices_returns_every_symbol_in_order(monkeypatch):
    async def quote(t):
        return None
    monkeypatch.setattr(prices, "listing_quote", quote)
    out = run(prices.fetch_prices(["GSPC", "ASML", "NOLIST"]))
    assert [i["ticker"] for i in out] == ["GSPC", "ASML", "NOLIST"]


# ---------------------------------------------------------------- valuation / negative-cache keys

def test_valuation_skips_index_and_etf_without_request_or_negative_entry(fake_yf):
    assert run(prices._fetch_valuation_from_yf("GSPC")) is None
    assert run(prices._fetch_valuation_from_yf("SXR8")) is None
    assert fake_yf.calls == [] and prices._negative_cache == {}


def test_valuation_unsupported_yahoo_mapping_is_skipped_silently(fake_yf):
    assert run(prices._fetch_valuation_from_yf("XYZ")) is None
    assert fake_yf.calls == [] and prices._negative_cache == {}


def test_valuation_negative_cache_has_its_own_key(fake_yf):
    fake_yf.info["ASML.AS"] = {}
    assert run(prices._fetch_valuation_from_yf("ASML")) is None
    assert ("yahoo:valuation", "ASML.AS") in prices._negative_cache
    assert ("yahoo", "ASML.AS") not in prices._negative_cache     # quotes/history untouched
    n = len(fake_yf.calls)
    run(prices._fetch_valuation_from_yf("ASML"))
    assert len(fake_yf.calls) == n                                # now skipped


def test_valuation_does_not_block_the_quote_path(fake_yf):
    prices._note_negative("yahoo:valuation", "ASML.AS")
    assert not prices._negative_skip("yahoo", "ASML.AS")
    fake_yf.info["ASML.AS"] = {"trailingPE": 35.0, "sector": "Technology", "dividendYield": 0.9}
    prices._negative_cache.clear()
    vals = run(prices._fetch_valuation_from_yf("ASML"))
    assert vals["pe"] == 35.0 and vals["sector"] == "Technology"
    assert ("yahoo:valuation", "ASML.AS") not in prices._negative_cache


def test_provider_fundamentals_and_profile_skip_unmapped_ids(monkeypatch):
    async def boom(_sym):
        raise AssertionError("request sent for an unsupported mapping")
    monkeypatch.setattr(prices.twelvedata, "get_fundamentals", boom)
    monkeypatch.setattr(prices.finnhub, "get_profile", boom)
    monkeypatch.setattr(prices.twelvedata, "_enabled", lambda: True)
    monkeypatch.setattr(prices.finnhub, "_enabled", lambda: True)
    for pid in ("GSPC", "SXR8", "XYZ"):
        assert run(prices._provider_fundamentals(pid)) is None
        assert run(prices._provider_profile(pid)) is None
    assert prices._negative_cache == {}


def test_refresh_valuation_only_touches_equities(fake_yf):
    fake_yf.info["ASML.AS"] = {"trailingPE": 20.0, "sector": "Technology"}

    async def go():
        await prices.refresh_valuation()
    run(go())
    assert set(prices._valuation_cache) == {"ASML"}
    assert prices._valuation_cache["ASML"]["pe"] == 20.0
    assert all(c[1][0] != "SXR8.DE" for c in fake_yf.calls)


# ---------------------------------------------------------------- store IO

def test_history_write_is_throttled_executor_based_and_roundtrips(monkeypatch):
    threads = []
    real_save = prices.jsonstore.save

    def spy(path, data, **kw):
        threads.append(threading.get_ident())
        return real_save(path, data, **kw)
    monkeypatch.setattr(prices.jsonstore, "save", spy)

    async def go():
        prices._history_writer.last = NOW                  # a write just happened
        prices._persist_points("ASML", [pt(NOW - 60, 1800.0)])
        handle = prices._history_writer._handle
        assert handle is not None                          # deferred, not written inline
        prices._persist_points("ASML", [pt(NOW, 1801.0)])
        assert prices._history_writer._handle is handle    # coalesced: still ONE timer
        assert threads == []
        await prices.flush_stores()
        return threading.get_ident()
    main_thread = run(go())
    assert len(threads) == 1 and threads[0] != main_thread  # off the event loop

    data = json.loads(open(prices.HISTORY_FILE).read())
    assert data["version"] == 2
    rec = data["series"]["ASML"]
    assert rec["symbol"] == "ASML.AS" and rec["currency"] == "EUR"
    assert [p["c"] for p in rec["points"]] == [1800.0, 1801.0]

    saved = copy.deepcopy(prices.history_store)
    prices._reset_state()
    prices._load_history_store()
    assert prices.history_store == saved


def test_history_write_interval_is_in_the_throttle_window():
    assert 60 <= prices.HISTORY_WRITE_INTERVAL <= 300


def test_corrupt_history_file_is_quarantined_and_writes_disabled(tmp_path):
    with open(prices.HISTORY_FILE, "w") as f:
        f.write("{not json")
    prices._load_history_store()
    assert prices._load_failed is True and prices.history_store == {}
    assert any(p.name.startswith("listing_history.json.bad-") for p in tmp_path.iterdir())
    prices._schedule_history_write()
    assert prices._history_writer.dirty is False           # would have clobbered the only copy


def test_load_drops_nan_and_keeps_post_2030_points(tmp_path):
    far = ts(2031, 3, 1)
    with open(prices.HISTORY_FILE, "w") as f:
        json.dump({"version": 2, "series": {"ASML": {"symbol": "ASML.AS", "currency": "EUR", "points": [
            {"t": far, "c": 5.0}, {"t": NOW, "c": None}, {"t": NOW - 60, "c": 7.0}]}}}, f)
    prices._load_history_store()
    assert [p["c"] for p in prices.history_store["ASML"]] == [7.0, 5.0]


def test_cleanup_applies_retention_and_drops_unwatched_series():
    prices.history_store["ASML"] = [pt(NOW - 400 * D), pt(NOW - 60)]
    prices.history_store["OLD"] = [pt(NOW - 60)]
    prices._cleanup_history_store()
    assert prices.history_store == {"ASML": [pt(NOW - 60)]}


def test_assess_history_depth_by_age_bucket():
    assert all(prices._assess_history_depth("ASML", NOW).values())
    pts = [pt(NOW - 8 * D - i * D) for i in range(30)]                 # 30 daily bars
    pts += [pt(NOW - 2 * D + i * 300) for i in range(24)]              # 24 five-minute bars
    pts += [pt(ASML_OPEN + i * 60) for i in range(16)]                 # 16 in today's session
    prices.history_store["ASML"] = pts
    assert prices._assess_history_depth("ASML", NOW) == {
        "needs_daily": False, "needs_hourly": False, "needs_intraday": False}


# ---------------------------------------------------------------- history views / ranges

def test_ranges_include_6m_and_invalid_range_is_rejected():
    assert "6M" in prices.RANGES and "6M" in prices.HISTORY_CACHE_TTL_BY_RANGE
    with pytest.raises(ValueError):
        run(prices.fetch_price_history("ASML", "5Y"))
    assert run(prices.fetch_price_history("NOLIST", "1D")) is None
    assert run(prices.listing_bars("ASML", "bogus")) == []


def test_1d_range_is_the_latest_started_session_only():
    fri = ts(2025, 7, 11, 10, 0)
    prices.history_store["ASML"] = [pt(fri, 1), pt(ASML_OPEN + 60, 2), pt(ASML_OPEN + 120, 3)]
    assert [p["c"] for p in prices._series_for_range("ASML", "1D", NOW)] == [2.0, 3.0]
    # Sunday: the latest started session is Friday's
    sunday = ts(2025, 7, 13, 12, 0)
    assert [p["c"] for p in prices._series_for_range("ASML", "1D", sunday)] == [1.0]


def test_long_ranges_are_one_bar_per_day_and_week_is_hourly():
    prices.history_store["ASML"] = (
        [pt(NOW - 10 * D + 15 * 3600 + 60 * i, 100 + i) for i in range(3)]
        + [pt(NOW - 3600 + 60 * i, 200 + i) for i in range(30)])
    month = prices._series_for_range("ASML", "1M", NOW)
    assert len(month) == 2 and month[0]["c"] == 102.0
    week = prices._series_for_range("ASML", "1W", NOW)
    assert [p["c"] for p in week] == [229.0]


def test_listing_bars_converts_each_bar_with_its_own_days_rate(monkeypatch):
    sat = ts(2025, 7, 12, 12)
    bars = [pt(ts(2025, 7, 10, 20), 100.0), pt(sat, 100.0), pt(ts(2025, 7, 14, 20), 100.0)]

    async def history(t, rng):
        return {"ticker": t, "range": rng, "currency": "USD", "source": "yahoo", "data": bars}

    asked = []

    async def daily(days):
        asked.append(days)
        return {"2025-07-10": 0.85, "2025-07-14": 0.90}
    monkeypatch.setattr(prices, "fetch_price_history", history)
    monkeypatch.setattr(prices.forex, "daily_rates", daily)
    out = run(prices.listing_bars("GSPC", "1W", "EUR"))
    assert [round(b["c"], 4) for b in out] == [85.0, 85.0, 90.0]   # Sat carries Fri's rate
    assert [b["t"] for b in out] == [b["t"] for b in bars]
    assert asked == [asked[0]] and asked[0] >= 8                   # ONE rate fetch for the window
    own = run(prices.listing_bars("GSPC", "1W"))
    assert [b["c"] for b in own] == [100.0, 100.0, 100.0]          # native currency untouched
    assert run(prices.listing_bars("GSPC", "1W", "GBP")) == []     # unsupported target


def test_listing_bars_drops_bars_without_a_rate(monkeypatch):
    async def history(t, rng):
        return {"currency": "USD", "data": [pt(ts(2025, 7, 14, 20), 100.0)]}

    async def daily(days):
        return {}
    monkeypatch.setattr(prices, "fetch_price_history", history)
    monkeypatch.setattr(prices.forex, "daily_rates", daily)
    assert run(prices.listing_bars("GSPC", "1W", "EUR")) == []


# ---------------------------------------------------------------- seed / refresh

def test_seed_validates_before_asking_yahoo(fake_yf):
    for args in (("ASML", "2min", 10), ("ASML", "1day", 0), ("ASML", "1day", 10 ** 6),
                 ("AAPL", "1day", 10), ("NOLIST", "1day", 10)):
        with pytest.raises(ValueError):
            run(prices.seed_listing_history(*args))
    assert fake_yf.calls == []


def test_seed_merges_only_valid_bars_and_never_replaces_the_store(fake_yf):
    prices._persist_points("ASML", [pt(NOW - 500 * 60, 1700.0)])
    raw = [pt(NOW - 5 * 3600 + i * 60, 1800.0 + i) for i in range(10)]
    raw.insert(3, pt(NOW - 5 * 3600 + 3 * 60 + 30, float("nan")))
    raw.insert(4, pt(NOW - 5 * 3600 + 3 * 60 + 40, float("inf")))
    fake_yf.bars[("ASML.AS", "1m")] = raw
    stored = run(prices.seed_listing_history("ASML", "1min", 100))
    assert stored == 10
    series = prices.history_store["ASML"]
    assert len(series) == 11 and series[0]["c"] == 1700.0           # old point survived
    assert all(math.isfinite(p["c"]) for p in series)
    # seeded 1-min bars are tagged as seed
    assert all(p.get("src") == "seed" for p in series[1:])


def test_seed_without_yahoo_data_raises_and_leaves_the_store(fake_yf):
    prices._persist_points("ASML", [pt(NOW - 60, 1800.0)])
    fake_yf.bars[("ASML.AS", "1d")] = None
    with pytest.raises(ValueError):
        run(prices.seed_listing_history("ASML", "1day", 30))
    assert prices.history_store["ASML"] == [pt(NOW - 60, 1800.0)]


def test_refresh_history_failure_keeps_the_stored_series(fake_yf):
    prices._persist_points("ASML", [pt(NOW - 60, 1800.0)])
    fake_yf.bars[("ASML.AS", "1d")] = None                          # yahoo down
    assert run(prices.refresh_history("ASML")) is False
    assert prices.history_store["ASML"] == [pt(NOW - 60, 1800.0)]


def test_refresh_history_success_swaps_the_whole_series(fake_yf):
    prices._persist_points("ASML", [pt(NOW - 60, 1800.0)])
    day = ts(2025, 7, 9, 22, 0)                                     # Thu 00:00 CEST on Jul 10
    fake_yf.bars[("ASML.AS", "1d")] = [pt(day, 1750.0)]
    fake_yf.bars[("ASML.AS", "5m")] = []
    fake_yf.bars[("ASML.AS", "1m")] = []
    assert run(prices.refresh_history("ASML")) is True
    series = prices.history_store["ASML"]
    assert [p["c"] for p in series] == [1750.0]                     # old point replaced
    assert prices.history_store["ASML"][0]["src"] == "seed"


def test_ensure_history_backfills_a_thin_series_once_per_cooldown(fake_yf):
    day = ts(2025, 7, 9, 22, 0)
    fake_yf.bars[("ASML.AS", "1d")] = [pt(day - i * D, 1700.0 + i) for i in range(40)]
    fake_yf.bars[("ASML.AS", "5m")] = [pt(NOW - 3 * D + i * 300, 1800.0) for i in range(30)]
    fake_yf.bars[("ASML.AS", "1m")] = [pt(ASML_OPEN + i * 60, 1840.0) for i in range(20)]
    assert run(prices.ensure_history("ASML")) is True
    n = len(fake_yf.calls)
    assert n == 3
    run(prices.ensure_history("ASML"))                              # healthy now
    assert len(fake_yf.calls) == n
    assert run(prices.ensure_history("NOLIST")) is False
