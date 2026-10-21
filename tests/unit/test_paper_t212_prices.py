"""Trading 212 held-position prices: the broker helper, the Practice valuation
overlay (t212 only when newer than Yahoo), the freshness samples + auto-disable,
the sell reference in the risk engine and the intraday pack's livePrice.
No network, injected clocks, files in tmp_path."""
import asyncio
import time

import httpx
import pytest

from app.api import broker, broker_t212, paper_pm, paper_risk
from app.api import paper_eval as pe

KEY, SECRET = "demo-key-xx", "demo-secret-xx"
UNIVERSE = {"ASML": {"t212": "ASMLa_EQ"}, "NVDA": {"t212": "NVDd_EQ"}}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- broker helper

class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now

    async def sleep(self, s):
        self.now += s


def make_broker(rows, status=200):
    clock = Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json=rows)))
    b = broker_t212.T212Broker(KEY, SECRET, env="demo", base_url=broker_t212.DEMO_URL,
                               clock=clock.time, client=client, sleep_fn=clock.sleep)
    return b, clock


def pos_row(ticker, price, currency="EUR"):
    return {"averagePricePaid": 100.0, "currentPrice": price, "quantity": 2.0,
            "instrument": {"ticker": ticker, "currency": currency}}


def test_held_quotes_maps_eur_positions_with_the_read_time(monkeypatch):
    monkeypatch.setattr(broker, "_singletons", {})
    b, clock = make_broker([pos_row("ASMLa_EQ", 650.5), pos_row("AAPL_US_EQ", 190.0, "USD"),
                            pos_row("NVDd_EQ", None), pos_row("ZERO_EQ", 0.0)])
    out = run(b.held_quotes())
    assert out == {"ASMLa_EQ": {"price": 650.5, "asOf": int(clock.now), "source": "t212"}}


def test_held_quotes_asof_is_the_cached_reads_time_when_throttled(monkeypatch):
    monkeypatch.setattr(broker, "_singletons", {})
    b, clock = make_broker([pos_row("ASMLa_EQ", 650.5)])
    first = run(b.held_quotes())
    clock.now += broker_t212.INTERVALS_S["positions"] / 2      # too soon: the cache answers
    second = run(b.held_quotes())
    assert second["ASMLa_EQ"]["asOf"] == first["ASMLa_EQ"]["asOf"]


def test_held_quotes_failed_read_is_empty(monkeypatch):
    monkeypatch.setattr(broker, "_singletons", {})
    b, _ = make_broker({"detail": "x"}, status=500)
    assert run(b.held_quotes()) == {}


# ------------------------------------------------------- valuation overlay

class HeldBroker:
    def __init__(self, held):
        self.held = held

    async def held_quotes(self, fresh=False):
        return self.held


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = {"quotes": {}, "broker": None}
    monkeypatch.setattr(pe, "PRICE_FRESH_FILE", tmp_path / "price_fresh.json")
    monkeypatch.setattr(pe, "_broker", lambda: state["broker"])

    async def quote_universe(universe, ids=None):
        return dict(state["quotes"])

    monkeypatch.setattr(pe, "_quote_universe", quote_universe)
    return state


def yq(price, as_of, prev=100.0):
    return {"price": price, "asOf": as_of, "prevClose": prev, "source": "yahoo", "stale": False}


def test_newer_t212_price_replaces_the_yahoo_quote(env):
    now = int(time.time())
    env["quotes"] = {"ASML": yq(650.0, now - 900, prev=640.0), "NVDA": yq(100.0, now - 900)}
    env["broker"] = HeldBroker({"ASMLa_EQ": {"price": 656.0, "asOf": now - 20, "source": "t212"}})
    out = run(pe.valuation_quotes(UNIVERSE))
    a = out["ASML"]
    assert (a["price"], a["asOf"], a["source"], a["stale"]) == (656.0, now - 20, "t212", False)
    assert a["prevClose"] == 640.0 and a["changePct"] == pytest.approx((656 / 640 - 1) * 100)
    assert out["NVDA"] == env["quotes"]["NVDA"]          # not held: untouched


@pytest.mark.parametrize("t_as_of_delta", [0, 50])        # equal, or older than yahoo's asOf
def test_t212_price_not_newer_keeps_yahoo(env, t_as_of_delta):
    env["quotes"] = {"ASML": yq(650.0, 5_000)}
    env["broker"] = HeldBroker({"ASMLa_EQ": {"price": 700.0, "asOf": 5_000 - t_as_of_delta,
                                             "source": "t212"}})
    assert run(pe.valuation_quotes(UNIVERSE))["ASML"] == env["quotes"]["ASML"]
    assert pe.price_fresh_status()["samples"] == 1       # sampled all the same


def test_t212_price_fills_in_for_a_missing_yahoo_quote(env):
    now = int(time.time())
    env["quotes"] = {"ASML": None}
    env["broker"] = HeldBroker({"ASMLa_EQ": {"price": 656.0, "asOf": now - 5, "source": "t212"}})
    q = run(pe.valuation_quotes(UNIVERSE))["ASML"]
    assert (q["price"], q["source"]) == (656.0, "t212") and q["prevClose"] is None


def test_overlay_is_inert_without_a_held_quotes_broker_or_when_disabled(env):
    env["quotes"] = {"ASML": yq(650.0, 1_000)}
    env["broker"] = object()                              # e.g. another broker: no held_quotes
    assert run(pe.valuation_quotes(UNIVERSE)) == env["quotes"]
    env["broker"] = HeldBroker({"ASMLa_EQ": {"price": 1.0, "asOf": 9_999_999_999, "source": "t212"}})
    pe.jsonstore.save(pe.PRICE_FRESH_FILE, {"disabled": True, "samples": []})
    assert run(pe.valuation_quotes(UNIVERSE)) == env["quotes"]


# ----------------------------------------------------------- freshness check

def rec(i, older, now=10_000, t_price=100.0, y_price=100.0, pid=None):
    t_as_of, y_as_of = (1_000, 1_500) if older else (2_000, 1_500)
    pe.record_price_sample(pid or f"X{i}", {"price": t_price, "asOf": t_as_of},
                           {"price": y_price, "asOf": y_as_of}, now)


def test_samples_are_capped_at_500_newest_kept(env):
    for i in range(520):
        rec(i, older=False)
    s = pe._fresh_doc()["samples"]
    assert len(s) == 500 and s[-1]["id"] == "X519" and s[0]["id"] == "X20"


def test_a_repeated_sample_is_not_stored_twice(env):
    rec(0, older=False, pid="A")
    rec(1, older=False, pid="A")
    assert pe.price_fresh_status()["samples"] == 1


def test_disables_at_80_percent_older_in_the_last_50(env):
    for i in range(10):
        rec(i, older=False)
    for i in range(10, 49):
        rec(i, older=True)
    assert pe.t212_price_enabled()                         # 49 samples: not enough yet
    rec(49, older=True)                                    # 40 of 50 = 80 %
    assert not pe.t212_price_enabled()
    st = pe.price_fresh_status()
    assert st["enabled"] is False and st["reason"] and st["disabledAt"] == 10_000
    assert run(pe.held_prices(UNIVERSE)) == {}             # the source is off


def test_stays_enabled_below_80_percent(env):
    for i in range(11):
        rec(i, older=False)
    for i in range(11, 50):
        rec(i, older=True)                                 # 39 of 50 = 78 %
    assert pe.t212_price_enabled()


def test_old_stale_samples_outside_the_last_50_do_not_disable(env):
    for i in range(100):
        rec(i, older=i < 30)          # 30 stale samples, then 70 good ones: no window of 50 reaches 80 %
    assert pe.t212_price_enabled()


def test_status_reports_median_gap_and_age(env):
    for i, (p, at_) in enumerate([(101.0, 10_010), (103.0, 10_020), (102.0, 10_030)]):
        rec(i, older=False, now=at_, t_price=p)            # t212AsOf 2000 -> ages 8010, 8020, 8030
    st = pe.price_fresh_status()
    assert st["medianGapPct"] == pytest.approx(2.0)
    assert st["medianAgeS"] == 8_020                       # median of 8010, 8020, 8030
    assert st["samples"] == 3 and st["enabled"] is True


def test_reenable_clears_flag_and_samples(env):
    pe.jsonstore.save(pe.PRICE_FRESH_FILE, {"disabled": True, "samples": [{"id": "A"}]})
    pe.reenable_t212_price()
    assert pe.t212_price_enabled() and pe.price_fresh_status()["samples"] == 0


def test_status_payload_carries_the_freshness_block(env):
    assert pe.price_fresh_status() == {"enabled": True, "disabledAt": None, "reason": None,
                                       "samples": 0, "medianGapPct": None, "medianAgeS": None}


# --------------------------------------------------------- risk: sell reference

NOW = 1_800_000_000
LIMITS = {"maxWeightPct": {"equity": 40, "etf": 100}, "minCashPct": 2,
          "maxTurnoverPctPerDay": 100, "maxOrdersPerDay": 6, "minTradeEur": 25,
          "rebalanceBandPct": 3, "maxSpreadOrStaleS": 900, "limitSlippageBps": 30,
          "adjustFraction": 1.0}
R_UNIVERSE = {i: {"t212": f"{i}_EQ"} for i in ("A", "E")}


def plan(**over):
    kw = dict(targets={}, cash_pct=None, holdings={}, cash_eur=1000.0,
              quotes={i: {"price": 100.0, "asOf": NOW - 10, "stale": False} for i in R_UNIVERSE},
              universe=R_UNIVERSE, kinds={"A": "equity", "E": "equity"}, limits=LIMITS,
              budget_eur=5000.0, decimals={}, now=NOW, session_ok={"A": True, "E": True},
              orders_today=0, turnover_today_eur=0.0)
    kw.update(over)
    return paper_risk.plan_orders(**kw)


def intent(res, id_):
    return next(i for i in res["intents"] if i.id == id_)


def test_sell_of_a_held_name_uses_a_fresh_t212_price():
    res = plan(targets={"A": 0}, holdings={"A": 8.0}, cash_eur=200.0,
               held_prices={"A": {"price": 98.5, "asOf": NOW - 30, "source": "t212"}})
    i = intent(res, "A")
    assert (i.side, i.ref_price, i.ref_price_as_of) == ("sell", 98.5, NOW - 30)
    assert i.limit_price == pytest.approx(98.5 * 0.997, abs=0.01)


def test_sell_boundary_120s_is_fresh_121s_falls_back_to_the_quote():
    held = {"A": {"price": 98.5, "asOf": NOW - 120, "source": "t212"}}
    assert intent(plan(targets={"A": 0}, holdings={"A": 8.0}, cash_eur=200.0,
                       held_prices=held), "A").ref_price == 98.5
    held["A"]["asOf"] = NOW - 121
    i = intent(plan(targets={"A": 0}, holdings={"A": 8.0}, cash_eur=200.0, held_prices=held), "A")
    assert (i.ref_price, i.ref_price_as_of) == (100.0, NOW - 10)


def test_buys_never_use_the_t212_price():
    res = plan(targets={"E": 50}, holdings={"A": 0.0}, cash_eur=1000.0,
               held_prices={"E": {"price": 90.0, "asOf": NOW - 5, "source": "t212"}})
    i = intent(res, "E")
    assert (i.side, i.ref_price, i.ref_price_as_of) == ("buy", 100.0, NOW - 10)


def test_no_held_prices_is_the_old_behaviour():
    a = plan(targets={"A": 0}, holdings={"A": 8.0}, cash_eur=200.0)
    b = plan(targets={"A": 0}, holdings={"A": 8.0}, cash_eur=200.0, held_prices={})
    assert intent(a, "A") == intent(b, "A") and intent(a, "A").ref_price == 100.0


# ------------------------------------------------------- pm pack: livePrice

def test_live_price_section_gap_vs_the_delayed_quote():
    t = {"price": 102.0, "asOf": NOW - 5, "source": "t212"}
    out = paper_pm._live_price_section(t, {"price": 100.0})
    assert out["price"] == 102.0 and out["source"] == "t212" and out["gapPctVsDelayed"] == 2.0
    assert out["asOf"] != paper_pm.MISSING
    assert paper_pm._live_price_section(t, None)["gapPctVsDelayed"] == paper_pm.MISSING
