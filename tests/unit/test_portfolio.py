"""Portfolio valuation + history (no network: quotes/bars/FX are faked).

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime

import pytest

from app.api import portfolio


def ts(y, m, d, h=12):
    return int(datetime.datetime(y, m, d, h,
                                 tzinfo=datetime.timezone.utc).timestamp())


ENTRIES = [
    {"id": "ASML", "label": "ASML", "kind": "equity", "role": "holding",
     "quoteCurrency": "EUR", "listing": {"currency": "EUR"}},
    {"id": "SXR8", "label": "S&P 500 (iShares CSPX)", "kind": "etf",
     "role": "holding", "quoteCurrency": "EUR", "listing": {"currency": "EUR"}},
    {"id": "GSPC", "label": "GSPC", "kind": "index", "role": "benchmark",
     "quoteCurrency": "USD", "listing": None},
]
PORTFOLIO = {"ASML": {"shares": 2.0, "investedAmount": 150.0},
             "SXR8": {"shares": 10.0, "investedAmount": None}}


def q(price, prev, **kw):
    return {"price": price, "prevClose": prev, "currency": "EUR",
            "asOf": 1_000, "source": "yahoo", "stale": False, **kw}


@pytest.fixture
def env(monkeypatch):
    state = {"quotes": {"ASML": q(110.0, 100.0), "SXR8": q(50.0, 49.0),
                        "GSPC": {**q(5000.0, 4950.0), "currency": "USD",
                                 "changePct": 1.01}},
             "bars": {}, "rates": {}, "portfolio": dict(PORTFOLIO),
             "entries": ENTRIES, "config": {}}

    async def listing_quote(pid):
        return state["quotes"].get(pid)

    async def listing_bars(pid, rng):
        return state["bars"].get(pid, [])

    async def rate():
        return {"rate": 0.9, "source": "ecb", "asOf": 1_000, "stale": False}

    async def daily_rates(days):
        return state["rates"]

    def rate_on(rates, day, max_lookback=7):
        older = [d for d in rates if d <= day]
        return rates[max(older)] if older else None

    monkeypatch.setattr(portfolio.prices, "listing_quote", listing_quote)
    monkeypatch.setattr(portfolio.prices, "listing_bars", listing_bars)
    monkeypatch.setattr(portfolio.forex, "get_rate_usd_eur", rate)
    monkeypatch.setattr(portfolio.forex, "daily_rates", daily_rates)
    monkeypatch.setattr(portfolio.forex, "rate_on", rate_on)
    monkeypatch.setattr(portfolio.registry, "entries",
                        lambda: [dict(e) for e in state["entries"]])
    monkeypatch.setattr(portfolio.registry, "benchmark_id", lambda: "GSPC")
    monkeypatch.setattr(portfolio.config_store, "read",
                        lambda: {"portfolio": state["portfolio"],
                                 **state["config"]})
    monkeypatch.setitem(portfolio._cache, "snap", None)
    # Older tests use fixed bar dates (2025-01 .. 2027-01) that only fit the
    # trim window of a clock this early; time-sensitive tests pin their own.
    monkeypatch.setattr(portfolio.performance, "today",
                        lambda: datetime.date(2026, 1, 10))
    return state


def snap():
    return asyncio.run(portfolio.snapshot())


def row(s, pid):
    return next(r for r in s["positions"] if r["id"] == pid)


def test_cost_missing_is_valued_but_excluded_from_pnl(env):
    s = snap()
    t = s["totals"]
    assert t["valueEur"] == 720.0                   # 220 + 500
    assert t["investedEur"] == 150.0                # ASML only
    assert t["pnlEur"] == 70.0
    assert t["pnlPct"] == pytest.approx(46.67)
    assert t["costMissing"] == ["SXR8"]
    sxr8 = row(s, "SXR8")
    assert sxr8["valueEur"] == 500.0
    assert sxr8["investedEur"] is None and sxr8["pnlEur"] is None
    assert sxr8["pnlPct"] is None
    w = next(w for w in s["warnings"] if w["code"] == "cost_missing")
    assert w["ids"] == ["SXR8"]


def test_day_pnl_uses_prev_close_of_the_same_listing(env):
    s = snap()
    assert row(s, "ASML")["dayPnlEur"] == 20.0      # 2 * (110 - 100)
    assert row(s, "SXR8")["dayPnlEur"] == 10.0
    assert s["totals"]["dayPnlEur"] == 30.0
    assert s["totals"]["dayPnlPct"] == pytest.approx(30 / 690 * 100, abs=0.01)


def test_weights_sum_to_100_and_follow_value(env):
    s = snap()
    assert row(s, "ASML")["weightPct"] == pytest.approx(30.56)
    assert sum(r["weightPct"] for r in s["positions"]) == pytest.approx(
        100.0, abs=0.02)


def test_stale_quote_warns_and_flags_the_row(env):
    env["quotes"]["SXR8"] = q(50.0, 49.0, stale=True)
    s = snap()
    assert row(s, "SXR8")["stale"] is True
    assert row(s, "ASML")["stale"] is False
    w = next(w for w in s["warnings"] if w["code"] == "stale_price")
    assert w["ids"] == ["SXR8"]
    assert row(s, "SXR8")["priceSource"] == "yahoo"
    assert row(s, "SXR8")["priceAsOf"] == 1_000


def test_missing_quote_is_excluded_not_guessed(env):
    env["quotes"]["ASML"] = None
    s = snap()
    assert row(s, "ASML")["valueEur"] is None
    assert s["totals"]["valueEur"] == 500.0
    assert s["totals"]["investedEur"] is None       # no priced cost basis
    assert row(s, "SXR8")["weightPct"] == 100.0
    assert [w["ids"] for w in s["warnings"] if w["code"] == "no_price"] \
        == [["ASML"]]


def test_usd_listed_holding_needs_a_real_fx_rate(env, monkeypatch):
    env["entries"] = [dict(ENTRIES[0], quoteCurrency="USD",
                           listing={"currency": "USD"}), ENTRIES[2]]
    env["portfolio"] = {"ASML": PORTFOLIO["ASML"]}
    env["quotes"]["ASML"] = q(110.0, 100.0, currency="USD")
    assert row(snap(), "ASML")["valueEur"] == pytest.approx(198.0)  # 2*110*.9

    async def no_rate():
        return {"rate": None, "source": None, "asOf": None, "stale": True}
    monkeypatch.setattr(portfolio.forex, "get_rate_usd_eur", no_rate)
    portfolio._cache["snap"] = None
    s = snap()
    assert row(s, "ASML")["valueEur"] is None
    assert any(w["code"] == "fx_missing" for w in s["warnings"])


def test_benchmark_block_and_mdd(env):
    env["bars"]["ASML"] = [{"t": i, "c": c} for i, c in
                           enumerate([100.0, 120.0, 90.0, 110.0])]
    s = snap()
    assert s["benchmark"]["id"] == "GSPC"
    assert s["benchmark"]["currency"] == "USD"
    assert s["benchmark"]["dayPct"] == 1.01
    assert row(s, "ASML")["mddPct"] == -25.0
    assert row(s, "SXR8")["mddPct"] is None          # no bars


def test_max_drawdown_edge_cases():
    assert portfolio._max_drawdown_pct([]) is None
    assert portfolio._max_drawdown_pct([5.0]) is None
    assert portfolio._max_drawdown_pct([1.0, 2.0, 3.0]) == 0.0


def test_position_for_reads_the_snapshot_cache(env):
    assert portfolio.position_for("ASML") is None    # cold cache
    snap()
    assert portfolio.position_for("asml")["valueEur"] == 220.0
    assert portfolio.position_for("GSPC") is None
    portfolio._cache["ts"] -= portfolio.CACHE_TTL_S + 1
    assert portfolio.position_for("ASML") is None    # expired


def test_snapshot_recomputes_when_positions_change(env):
    assert snap()["totals"]["valueEur"] == 720.0
    env["portfolio"] = {"ASML": {"shares": 1.0, "investedAmount": 150.0}}
    assert snap()["totals"]["valueEur"] == 110.0


# ---------------------------------------------------------------- history

def _days(closes, start_day=1, month=9):
    return [{"t": ts(2026, month, start_day + i), "c": c}
            for i, c in enumerate(closes)]


def hist(rng="3M"):
    return asyncio.run(portfolio.history(rng))


def test_history_values_benchmark_in_eur_and_rebases_both(env):
    env["portfolio"] = {"ASML": {"shares": 2.0, "investedAmount": 15.0}}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0])
    env["bars"]["GSPC"] = _days([100.0, 110.0, 121.0])
    env["rates"] = {"2026-09-01": 0.5}              # carried forward
    h = hist()
    assert [p["valueEur"] for p in h["points"]] == [20.0, 22.0, 24.0]
    assert {p["investedEur"] for p in h["points"]} == {15.0}
    assert [p["c"] for p in h["benchmark"]["points"]] == [50.0, 55.0, 60.5]
    assert h["benchmark"]["currency"] == "EUR"
    assert [p["pct"] for p in h["indexed"]["portfolio"]] == [0.0, 10.0, 20.0]
    assert [p["pct"] for p in h["indexed"]["benchmark"]] == [0.0, 10.0, 21.0]


def test_history_carries_forward_across_holidays_and_common_start(env):
    env["portfolio"] = {"ASML": {"shares": 1.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([10.0, 12.0, 14.0], start_day=2)   # starts d2
    # benchmark starts a day earlier and skips d3 (US holiday)
    env["bars"]["GSPC"] = [{"t": ts(2026, 9, 1), "c": 90.0},
                           {"t": ts(2026, 9, 2), "c": 100.0},
                           {"t": ts(2026, 9, 4), "c": 120.0}]
    env["rates"] = {"2026-08-31": 1.0}
    h = hist()
    assert all(p["investedEur"] is None for p in h["points"])      # no cost
    ind = h["indexed"]
    assert [p["pct"] for p in ind["portfolio"]] == [0.0, 20.0, 40.0]
    # d3 carries the benchmark's d2 value (100) forward
    assert [p["pct"] for p in ind["benchmark"]] == [0.0, 0.0, 20.0]


def test_history_without_rates_has_no_benchmark_and_warns(env):
    env["bars"]["ASML"] = _days([10.0, 11.0])
    env["bars"]["GSPC"] = _days([100.0, 110.0])
    env["rates"] = {}
    h = hist()
    assert h["benchmark"]["points"] == []
    assert h["indexed"] == {"portfolio": [], "benchmark": [],
                            "perfBenchmark": []}
    assert any(w["code"] == "fx_missing" for w in h["warnings"])


def test_history_leaves_out_holding_without_bars(env):
    env["bars"]["ASML"] = _days([10.0, 11.0])
    env["rates"] = {"2026-08-31": 1.0}
    h = hist("1M")
    w = next(w for w in h["warnings"] if w["code"] == "history_missing"
             and w["ids"] == ["SXR8"])
    assert w
    assert [p["valueEur"] for p in h["points"]] == [20.0, 22.0]


def test_history_rejects_unknown_range(env):
    with pytest.raises(ValueError):
        hist("5Y")


# ------------------------------------------- lots basis / perf benchmark

def L(date, shares, price, side="buy", fees=0.0):
    return {"date": date, "shares": shares, "priceEur": price,
            "feesEur": fees, "side": side}


def pos(*lots, invested=None):
    held = sum(x["shares"] if x["side"] == "buy" else -x["shares"]
               for x in lots)
    return {"shares": held, "investedAmount": invested, "lots": list(lots)}


def codes(h):
    return {w["code"] for w in h["warnings"]}


def test_history_uses_held_shares_when_every_holding_has_lots(env):
    env["portfolio"] = {"ASML": pos(L("2026-09-01", 1.0, 10.0),
                                    L("2026-09-03", 1.0, 12.0),
                                    invested=22.0)}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0, 13.0])
    env["bars"]["GSPC"] = _days([100.0, 105.0, 110.0, 115.0])
    env["rates"] = {"2026-08-31": 0.5}
    h = hist()
    assert h["basis"] == "lots"
    assert "history_basis_current_shares" not in codes(h)
    # 1 share until the 2nd lot on 09-03, then 2 (today's count would be 2)
    assert [p["valueEur"] for p in h["points"]] == [10.0, 11.0, 24.0, 26.0]
    assert {p["investedEur"] for p in h["points"]} == {22.0}
    assert [p["eur"] for p in h["invested"]] == [10.0, 10.0, 22.0, 22.0]
    # 1.1 x (24 - 12) / 11 x 26 / 24 = 1.3: the deposit is not performance
    assert [p["pct"] for p in h["twr"]] == [0.0, 10.0, 20.0, 30.0]
    assert [p["t"] for p in h["twr"]] == [p["t"] for p in h["points"]]


def test_history_without_lots_values_todays_shares_and_says_so(env):
    env["portfolio"] = {"ASML": {"shares": 2.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0, 13.0])
    h = hist()
    assert h["basis"] == "current_shares"
    w = next(w for w in h["warnings"]
             if w["code"] == "history_basis_current_shares")
    assert w["ids"] == ["ASML"]
    assert [p["valueEur"] for p in h["points"]] == [20.0, 22.0, 24.0, 26.0]
    assert "invested" not in h and "twr" not in h


def test_one_holding_without_lots_switches_the_whole_history_back(env):
    env["portfolio"] = {"ASML": pos(L("2026-09-02", 1.0, 11.0)),
                        "SXR8": {"shares": 2.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([10.0, 11.0])
    env["bars"]["SXR8"] = _days([5.0, 6.0])
    h = hist()
    assert h["basis"] == "current_shares"
    w = next(w for w in h["warnings"]
             if w["code"] == "history_basis_current_shares")
    assert w["ids"] == ["SXR8"]
    # ASML counted from day 1 although its only lot is dated day 2
    assert [p["valueEur"] for p in h["points"]] == [20.0, 23.0]
    assert "invested" not in h and "twr" not in h


def test_lots_before_the_window_count_and_a_later_first_buy_starts_the_axis(env):
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0])
    env["portfolio"] = {"ASML": pos(L("2026-01-05", 1.0, 9.0))}
    h = hist()
    assert [p["valueEur"] for p in h["points"]] == [10.0, 11.0, 12.0]
    assert h["twr"][-1]["pct"] == 20.0
    env["portfolio"] = {"ASML": pos(L("2026-09-02", 1.0, 11.0))}
    h = hist()
    assert [p["valueEur"] for p in h["points"]] == [11.0, 12.0]
    assert [p["pct"] for p in h["twr"]] == [0.0, 9.09]


def test_history_perf_benchmark_defaults_to_the_role_benchmark(env):
    env["portfolio"] = {"ASML": {"shares": 2.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0])
    env["bars"]["GSPC"] = _days([100.0, 110.0, 121.0])
    env["rates"] = {"2026-08-31": 0.5}
    h = hist()
    pb = h["perfBenchmark"]
    assert set(pb) == {"id", "label", "kind", "points"}
    assert (pb["id"], pb["kind"]) == ("GSPC", "price")
    assert pb["points"] == h["benchmark"]["points"]
    assert h["indexed"]["perfBenchmark"] == h["indexed"]["benchmark"]


def test_history_perf_benchmark_follows_the_config_as_an_eur_series(env):
    env["config"] = {"performanceBenchmark": "sxr8"}
    env["portfolio"] = {"ASML": {"shares": 2.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0])
    env["bars"]["SXR8"] = _days([50.0, 55.0, 60.0])
    env["bars"]["GSPC"] = _days([100.0, 110.0, 121.0])
    env["rates"] = {"2026-08-31": 0.5}
    h = hist()
    pb = h["perfBenchmark"]
    assert (pb["id"], pb["kind"]) == ("SXR8", "total_return")
    assert pb["label"] == "S&P 500 (iShares CSPX)"
    assert [p["c"] for p in pb["points"]] == [50.0, 55.0, 60.0]   # no FX
    assert [p["pct"] for p in h["indexed"]["perfBenchmark"]] == [0.0, 10.0, 20.0]
    assert h["benchmark"]["id"] == "GSPC"
    assert [p["c"] for p in h["benchmark"]["points"]] == [50.0, 55.0, 60.5]


def test_unknown_performance_benchmark_falls_back_with_a_warning(env):
    env["config"] = {"performanceBenchmark": "NOPE"}
    env["bars"]["ASML"] = _days([10.0, 11.0])
    h = hist()
    assert h["perfBenchmark"]["id"] == "GSPC"
    w = next(w for w in h["warnings"] if w["code"] == "perf_benchmark_unknown")
    assert w["ids"] == ["NOPE"]


def test_all_range_reads_daily_closes_sized_by_the_oldest_lot(env, monkeypatch):
    calls = {"closes": [], "rates": [], "bars": []}

    async def daily_closes(ticker, period="2y"):
        calls["closes"].append((ticker, period))
        return {"ASML": [("2026-09-01", 10.0), ("2026-09-02", 12.0)],
                "GSPC": [("2026-09-01", 100.0), ("2026-09-02", 120.0)]
                }.get(ticker, [])

    async def bars(pid, rng):
        calls["bars"].append(pid)
        return []

    async def daily_rates(days):
        calls["rates"].append(days)
        return env["rates"]

    monkeypatch.setattr(portfolio.prices, "daily_closes", daily_closes,
                        raising=False)
    monkeypatch.setattr(portfolio.prices, "listing_bars", bars)
    monkeypatch.setattr(portfolio.forex, "daily_rates", daily_rates)
    monkeypatch.setattr(portfolio.performance, "today",
                        lambda: datetime.date(2026, 10, 14))
    env["portfolio"] = {"ASML": pos(L("2024-03-14", 1.0, 100.0),
                                    invested=100.0)}
    env["rates"] = {"2026-08-31": 0.5}
    h = hist("all")
    assert h["range"] == "ALL" and h["basis"] == "lots"
    assert [p["valueEur"] for p in h["points"]] == [10.0, 12.0]
    assert [p["c"] for p in h["benchmark"]["points"]] == [50.0, 60.0]
    assert calls["bars"] == []
    assert ("ASML", "5y") in calls["closes"]       # 944 days old + buffer
    assert calls["rates"] == [954]


def test_no_lots_all_is_clamped_to_5y_and_every_series_is_trimmed(env, monkeypatch):
    today = datetime.date(2026, 10, 14)
    monkeypatch.setattr(portfolio.performance, "today", lambda: today)
    periods = []
    grid = [today - datetime.timedelta(days=7 * i) for i in range(900)][::-1]

    async def daily_closes(ticker, period="2y"):
        periods.append(period)
        if ticker == "ASML":          # 'max'-like: ~17 years of history
            return [(d.isoformat(), 10.0 + i * 0.01) for i, d in enumerate(grid)]
        if ticker == "GSPC":          # USD benchmark, longer than its FX rates
            return [(d.isoformat(), 100.0 + i * 0.1) for i, d in enumerate(grid)]
        return []

    monkeypatch.setattr(portfolio.prices, "daily_closes", daily_closes,
                        raising=False)
    fx_start = today - datetime.timedelta(days=4 * 365)
    env["rates"] = {d.isoformat(): 0.9 for d in grid if d >= fx_start}
    env["portfolio"] = {"ASML": {"shares": 2.0, "investedAmount": None}}
    m = asyncio.run(portfolio.model("ALL"))
    floor = (today - datetime.timedelta(days=5 * 365 + 10)).isoformat()
    assert m["basis"] != "lots"
    assert set(periods) == {"5y"}
    assert min(m["series"]["ASML"]) >= floor       # 17y of history trimmed
    assert min(m["axis"]) >= floor
    assert min(m["bench"]["days"]) >= floor        # FX-limited, ~4y
    h = hist("all")
    ip, ib = h["indexed"]["portfolio"], h["indexed"]["benchmark"]
    assert ip and ip[0]["t"] == ib[0]["t"]
    assert ip[0]["pct"] == 0.0 and ib[0]["pct"] == 0.0


def test_every_view_rejects_an_unknown_range(env):
    for fn in (portfolio.performance_summary, portfolio.attribution,
               portfolio.model):
        with pytest.raises(ValueError, match="1M, 3M, 6M, 1Y, ALL"):
            asyncio.run(fn("5Y"))


# ------------------------------------------------------------ performance

def perf_env(env):
    """ASML: 1 share on 2025-01-01 and a 2nd on 2026-01-01 (both at 1000),
    worth 2 x 1155 on 2027-01-01; benchmark +10 %."""
    env["portfolio"] = {"ASML": pos(L("2025-01-01", 1.0, 1000.0),
                                    L("2026-01-01", 1.0, 1000.0),
                                    invested=2000.0)}
    env["bars"]["ASML"] = [{"t": ts(2025, 1, 1), "c": 1000.0},
                           {"t": ts(2026, 1, 1), "c": 1000.0},
                           {"t": ts(2027, 1, 1), "c": 1155.0}]
    env["bars"]["GSPC"] = [{"t": ts(2025, 1, 1), "c": 100.0},
                           {"t": ts(2026, 1, 1), "c": 100.0},
                           {"t": ts(2027, 1, 1), "c": 110.0}]
    env["rates"] = {"2024-12-31": 1.0}


PERF_KEYS = {"range", "basis", "twrPct", "xirrPct", "perfBenchmarkId",
             "perfBenchmarkPct", "excessPct", "startValueEur", "endValueEur",
             "netFlowsEur", "asOf", "warnings"}


def test_performance_separates_time_weighted_from_money_weighted(env):
    perf_env(env)
    p = asyncio.run(portfolio.performance_summary("1Y"))
    assert set(p) == PERF_KEYS
    assert p["basis"] == "lots"
    assert p["twrPct"] == 15.5        # 0 % in year 1, then +15.5 % on 2000
    assert p["xirrPct"] == 10.0       # -1000, -1000, +2310 at 1y steps
    assert (p["startValueEur"], p["endValueEur"]) == (1000.0, 2310.0)
    assert p["netFlowsEur"] == 1000.0     # the 2026 lot; 2025 is the opening
    assert p["perfBenchmarkId"] == "GSPC" and p["perfBenchmarkPct"] == 10.0
    assert p["excessPct"] == 5.5
    assert p["asOf"] == "2027-01-01"
    assert "xirr_needs_dated_lots" not in codes(p)


def test_performance_all_range_uses_the_dated_lots(env, monkeypatch):
    perf_env(env)

    async def daily_closes(ticker, period="2y"):
        return [(portfolio.performance.day_of(b["t"]), b["c"])
                for b in env["bars"].get(ticker, [])]

    monkeypatch.setattr(portfolio.prices, "daily_closes", daily_closes,
                        raising=False)
    monkeypatch.setattr(portfolio.performance, "today",
                        lambda: datetime.date(2027, 1, 2))
    p = asyncio.run(portfolio.performance_summary("ALL"))
    assert p["range"] == "ALL"
    assert (p["twrPct"], p["xirrPct"]) == (15.5, 10.0)


def test_performance_without_lots_is_the_value_change_and_has_no_xirr(env):
    env["portfolio"] = {"ASML": {"shares": 2.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0])
    env["bars"]["GSPC"] = _days([100.0, 105.0, 110.0])
    env["rates"] = {"2026-08-31": 0.5}
    p = asyncio.run(portfolio.performance_summary("3M"))
    assert p["basis"] == "current_shares"
    assert p["twrPct"] == 20.0 and p["xirrPct"] is None
    assert p["netFlowsEur"] is None
    assert (p["startValueEur"], p["endValueEur"]) == (20.0, 24.0)
    assert (p["perfBenchmarkPct"], p["excessPct"]) == (10.0, 10.0)
    assert {"xirr_needs_dated_lots", "history_basis_current_shares"} <= codes(p)


def test_performance_without_any_series_keeps_the_keys_and_reports_nulls(env):
    p = asyncio.run(portfolio.performance_summary("1M"))
    assert set(p) == PERF_KEYS
    assert p["twrPct"] is None and p["xirrPct"] is None
    assert p["asOf"] is None and p["startValueEur"] is None
    assert "history_missing" in codes(p)


# ------------------------------------------------------------ attribution

def attribution(rng="3M"):
    return asyncio.run(portfolio.attribution(rng))


def test_attribution_splits_the_eur_return_into_local_fx_and_cross(env):
    env["config"] = {"exposureCurrency": {"ASML": "usd"}}
    env["portfolio"] = {"ASML": {"shares": 1.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([100.0, 110.0, 121.0])
    env["rates"] = {"2026-09-01": 0.50, "2026-09-02": 0.55, "2026-09-03": 0.60}
    out = attribution()
    assert set(out) == {"range", "positions", "fx", "warnings"}
    assert out["fx"] == {"pair": "USD/EUR", "changePct": 20.0}
    (a,) = out["positions"]
    assert set(a) == {"id", "exposureCurrency", "eurReturnPct",
                      "localReturnPct", "fxReturnPct", "crossPct"}
    assert a["exposureCurrency"] == "USD"
    assert a["eurReturnPct"] == 21.0 and a["fxReturnPct"] == 20.0
    assert a["localReturnPct"] == pytest.approx(100 * (1.21 / 1.20 - 1), abs=0.01)
    # the identity, up to the 2 dp rounding of the payload
    assert (1 + a["localReturnPct"] / 100) * (1 + a["fxReturnPct"] / 100) \
        == pytest.approx(1 + a["eurReturnPct"] / 100, abs=1e-4)
    assert a["eurReturnPct"] == pytest.approx(
        a["localReturnPct"] + a["fxReturnPct"] + a["crossPct"], abs=0.011)


def test_attribution_eur_exposure_has_no_fx_and_unknown_currencies_warn(env):
    env["config"] = {"exposureCurrency": {"SXR8": "GBP"}}
    env["portfolio"] = {"ASML": {"shares": 1.0, "investedAmount": None},
                        "SXR8": {"shares": 1.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([100.0, 110.0])
    env["bars"]["SXR8"] = _days([10.0, 11.0])
    out = attribution()
    asml, sxr8 = out["positions"]
    assert asml["exposureCurrency"] == "EUR"
    assert (asml["eurReturnPct"], asml["localReturnPct"],
            asml["fxReturnPct"], asml["crossPct"]) == (10.0, 10.0, 0.0, 0.0)
    assert sxr8["eurReturnPct"] == 10.0
    assert sxr8["localReturnPct"] is None and sxr8["fxReturnPct"] is None
    w = next(w for w in out["warnings"]
             if w["code"] == "fx_unsupported_currency")
    assert w["ids"] == ["SXR8"]
    assert out["fx"]["changePct"] is None            # no rates at all


def test_attribution_without_rates_reports_usd_positions_as_unsplit(env):
    env["config"] = {"exposureCurrency": {"ASML": "USD"}}
    env["portfolio"] = {"ASML": {"shares": 1.0, "investedAmount": None}}
    env["bars"]["ASML"] = _days([100.0, 110.0])
    out = attribution()
    (a,) = out["positions"]
    assert a["eurReturnPct"] == 10.0 and a["localReturnPct"] is None
    assert any(w["code"] == "fx_missing" and w["ids"] == ["ASML"]
               for w in out["warnings"])


def test_attribution_of_a_position_with_lots_starts_at_its_first_lot(env):
    env["portfolio"] = {"ASML": pos(L("2026-09-01", 1.0, 10.0)),
                        "SXR8": pos(L("2026-09-02", 1.0, 50.0))}
    env["bars"]["ASML"] = _days([10.0, 11.0, 12.0])
    env["bars"]["SXR8"] = _days([40.0, 50.0, 55.0])
    by_id = {p["id"]: p for p in attribution()["positions"]}
    assert by_id["ASML"]["eurReturnPct"] == 20.0
    assert by_id["SXR8"]["eurReturnPct"] == 10.0     # 50 -> 55, not 40 -> 55
