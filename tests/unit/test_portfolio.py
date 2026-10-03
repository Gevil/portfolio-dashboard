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
             "entries": ENTRIES}

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
                        lambda: {"portfolio": state["portfolio"]})
    monkeypatch.setitem(portfolio._cache, "snap", None)
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
    assert h["indexed"] == {"portfolio": [], "benchmark": []}
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
