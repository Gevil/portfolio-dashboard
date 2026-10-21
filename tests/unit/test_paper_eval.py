"""Practice-book payloads, season creation and baselines (plan §4, §8.1).

The demo broker is a plain fake object implementing the §1 Protocol
(account/positions/status) - no httpx/broker_t212 import needed here.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime

import pytest

from app.api import broker as broker_mod
from app.api import paper_eval as pe

UNIVERSE = {"ASML": {"t212": "ASMLa_EQ"}, "NVDA": {"t212": "NVDd_EQ"},
           "SXR8": {"t212": "SXR8d_EQ"}}
REGISTRY = {"ASML": {"id": "ASML", "label": "ASML", "kind": "equity"},
           "NVDA": {"id": "NVDA", "label": "NVDA", "kind": "equity"},
           "SXR8": {"id": "SXR8", "label": "S&P 500", "kind": "etf"}}


class FakeBroker:
    name, env = "t212", "demo"

    def __init__(self, account=None, positions=None, error=None):
        self._account = account
        self._positions = positions if positions is not None else []
        self._error = error
        self.calls = 0

    async def account(self):
        self.calls += 1
        if self._error:
            return broker_mod.err(self._error)
        return self._account

    async def positions(self):
        if self._error:
            return broker_mod.err(self._error)
        return self._positions

    async def instruments(self):
        return []

    async def instrument(self, ticker):
        return None

    async def open_orders(self):
        return []

    async def order_history(self, limit=50):
        return []

    def quantity_decimals(self, ticker):
        return 2

    def status(self):
        st = {"name": "t212", "env": "demo", "configured": True,
              "reachable": not self._error, "rateLimitedUntil": None,
              "lastOkAt": 123.0 if not self._error else 0.0,
              "lastError": None if not self._error else broker_mod.err(self._error),
              "calls": self.calls, "errors": 0}
        st.update(getattr(self, "status_override", {}))
        return st

    async def close(self):
        pass


def acct(total=5000.0, cash=3700.0, invested=1300.0):
    return broker_mod.Account(currency="EUR", total=total, free_cash=cash,
                              invested=invested, fetched_at=1.0)


def position(ticker, qty, avg, current=None):
    return broker_mod.Position(ticker=ticker, quantity=qty, average_price=avg,
                               current_price=current, ppl=None, currency="EUR")


def quote(price, prev=None, source="yahoo", stale=False, as_of=1_000):
    return {"price": price, "prevClose": prev, "currency": "EUR",
           "asOf": as_of, "source": source, "stale": stale}


def fill_row(pid, side, qty, value, state="filled", limit=100.0, filled=None):
    """An order-ledger row (what ``paper_exec`` writes) with the fields accounting reads."""
    return {"intentId": f"2026-10-18-s1-manual-{pid}-{side}-1", "id": pid,
            "ticker": UNIVERSE[pid]["t212"], "side": side, "quantity": qty,
            "limitPrice": limit, "refPrice": limit, "state": state,
            "filledQuantity": qty if filled is None else filled,
            "filledValueEur": value, "avgFillPrice": None}


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = {"broker": FakeBroker(account=acct(), positions=[]),
             "cfg": {"mode": "off", "strategyVersion": "s1", "budgetEur": 1000.0,
                     "universe": dict(UNIVERSE), "limits": {}},
             "quotes": {}, "bars": {}, "real_positions": []}
    monkeypatch.setattr(pe.paper_ledger, "SEASON_FILE", tmp_path / "season.json")
    monkeypatch.setattr(pe.paper_ledger, "SNAPSHOTS_FILE", tmp_path / "snapshots.json")
    monkeypatch.setattr(pe.paper_ledger, "ORDERS_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(pe.paper_ledger, "HALT_FILE", tmp_path / "halt.json")
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "1")
    monkeypatch.setattr(pe, "_broker", lambda: state["broker"])
    monkeypatch.setattr(pe.paper_buckets.config_store, "read", lambda: {"paperTrading": state["cfg"]})
    monkeypatch.setattr(pe.registry, "get", lambda pid: REGISTRY.get(str(pid).upper()))

    async def listing_quote(pid):
        return state["quotes"].get(pid)

    async def listing_bars(pid, rng):
        return state["bars"].get(pid, [])

    async def real_snapshot():
        return {"positions": state["real_positions"]}

    monkeypatch.setattr(pe.prices, "listing_quote", listing_quote)
    monkeypatch.setattr(pe.prices, "listing_bars", listing_bars)
    monkeypatch.setattr(pe.portfolio_mod, "snapshot", real_snapshot)
    return state


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- enabled

def test_enabled_requires_both_the_env_flag_and_a_configured_broker(env, monkeypatch):
    assert pe.enabled() is True
    monkeypatch.delenv("PAPER_TRADING_ENABLED", raising=False)
    assert pe.enabled() is False
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "1")
    monkeypatch.setattr(pe, "_broker", lambda: None)
    assert pe.enabled() is False


def test_portfolio_snapshot_raises_disabled(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING_ENABLED", raising=False)
    monkeypatch.setattr(pe, "_broker", lambda: None)
    with pytest.raises(pe.PaperDisabled):
        run(pe.portfolio_snapshot())


# ------------------------------------------------------- GET /api/portfolio

def test_portfolio_snapshot_shape_and_bucket_math(env):
    env["broker"] = FakeBroker(account=acct(total=5000.0, cash=3700.0),
                               positions=[position("ASMLa_EQ", 2.0, 300.0)])
    env["quotes"] = {"ASML": quote(650.0, 640.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 600.0)])   # the ledger explains the position
    s = run(pe.portfolio_snapshot())
    assert s["book"] == "practice"
    assert s["capabilities"] == pe.CAPABILITIES
    assert s["banner"]["account"] == "t212-demo" and s["banner"]["mode"] == "off"
    assert s["benchmark"] is None
    assert "stale" not in s
    assert s["warnings"] == []
    b = s["bucket"]
    assert b["budgetEur"] == 1000.0
    assert b["cashEur"] == 400.0          # ledger: 1000 - 600 filled buy
    assert b["reservedEur"] == 0.0
    assert b["equityEur"] == pytest.approx(1700.0)   # 2*650 + 400
    assert b["accountTotalEur"] == 5000.0 and b["accountCashEur"] == 3700.0
    assert b["seasonStarted"] is False and b["inceptionDate"] is None
    assert b["strategyVersion"] == "s1"
    asml = next(r for r in s["positions"] if r["id"] == "ASML")
    assert asml["ticker"] == "ASMLa_EQ" and asml["shares"] == 2.0
    assert asml["valueEur"] == 1300.0 and asml["investedEur"] == 600.0
    assert asml["pnlEur"] == 700.0
    assert asml["weightPct"] == pytest.approx(1300 / 1700 * 100, abs=0.01)
    assert s["totals"]["valueEur"] == pytest.approx(1700.0)
    assert s["totals"]["investedEur"] == 600.0


def test_a_position_the_ledger_does_not_explain_is_not_the_buckets(env):
    """Holdings come from the bucket's own fills only: a position nobody traded
    through the dashboard is not held by the bucket, does not reduce its cash and
    is an untracked_position warning."""
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 2.0, 300.0)])
    env["quotes"] = {"ASML": quote(650.0)}
    s = run(pe.portfolio_snapshot())
    b = s["bucket"]
    assert b["cashEur"] == 1000.0 and b["reservedEur"] == 0.0
    assert b["equityEur"] == pytest.approx(1000.0) and s["positions"] == []
    w = next(w for w in s["warnings"] if w["code"] == "untracked_position")
    assert w["ids"] == ["ASML"]


def test_portfolio_snapshot_warns_ledger_mismatch(env):
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 1.0, 300.0)])
    env["quotes"] = {"ASML": quote(300.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 600.0)])   # ledger says 2, broker holds 1
    s = run(pe.portfolio_snapshot())
    w = next(w for w in s["warnings"] if w["code"] == "ledger_mismatch")
    assert w["ids"] == ["ASML"]
    assert not any(w["code"] == "untracked_position" for w in s["warnings"])


def test_open_buys_reserve_cash_and_do_not_lower_equity(env):
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 1.0, 300.0)])
    env["quotes"] = {"ASML": quote(300.0)}
    pe.paper_ledger.save_orders([
        fill_row("ASML", "buy", 1.0, 300.0),
        fill_row("NVDA", "buy", 2.0, None, state="submitted", limit=50.0, filled=0.0)])
    b = run(pe.portfolio_snapshot())["bucket"]
    assert b["reservedEur"] == 100.0                     # 2 x 50 limit
    assert b["cashEur"] == 1000.0 - 300.0 - 100.0
    assert b["equityEur"] == pytest.approx(1000.0)       # 300 position + 600 free + 100 reserved


def test_unreachable_broker_still_reports_the_ledgers_reserved_cash(env):
    pe.paper_ledger.save_snapshot_row({
        "date": "2026-10-18", "equityEur": 1000.0, "cashEur": 900.0, "positions": [],
        "accountTotalEur": 5000.0, "accountCashEur": 3700.0, "takenAt": 1.0})
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 3.0, None, state="submitted",
                                          limit=40.0, filled=0.0)])
    env["broker"] = FakeBroker(error="timeout")
    s = run(pe.portfolio_snapshot())
    assert s["stale"] is True and s["bucket"]["reservedEur"] == 120.0


def test_portfolio_snapshot_unmapped_position_warning(env):
    env["broker"] = FakeBroker(account=acct(),
                               positions=[position("ASMLa_EQ", 1.0, 100.0),
                                         position("TSLA_EQ", 5.0, 50.0)])
    env["quotes"] = {"ASML": quote(110.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 1.0, 100.0)])
    s = run(pe.portfolio_snapshot())
    assert [r["id"] for r in s["positions"]] == ["ASML"]
    w = next(w for w in s["warnings"] if w["code"] == "unmapped_broker_position")
    assert w["ids"] == ["TSLA_EQ"]


def test_portfolio_snapshot_falls_back_to_last_snapshot_when_broker_unreachable(env):
    pe.paper_ledger.save_snapshot_row({
        "date": "2026-10-18", "equityEur": 1234.0, "cashEur": 400.0,
        "positions": [{"id": "ASML", "ticker": "ASMLa_EQ", "quantity": 2.0,
                       "priceEur": 650.0, "valueEur": 1300.0}],
        "accountTotalEur": 5000.0, "accountCashEur": 3700.0, "takenAt": 1.0})
    env["broker"] = FakeBroker(error="timeout")
    s = run(pe.portfolio_snapshot())
    assert s["stale"] is True
    assert s["bucket"]["equityEur"] == 1234.0
    assert any(w["code"] == "broker_unreachable" for w in s["warnings"])
    row = s["positions"][0]
    assert row["id"] == "ASML" and row["valueEur"] == 1300.0 and row["stale"] is True


def test_portfolio_snapshot_raises_unavailable_without_any_snapshot(env):
    env["broker"] = FakeBroker(error="timeout")
    with pytest.raises(pe.PaperUnavailable):
        run(pe.portfolio_snapshot())


# -------------------------------------------------------------- snapshots

def test_take_snapshot_row_shape(env):
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 2.0, 300.0)])
    env["quotes"] = {"ASML": quote(650.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 600.0)])
    row = run(pe.take_snapshot("2026-10-19"))
    assert row == {
        "date": "2026-10-19", "bucket": "daily", "equityEur": 1700.0, "cashEur": 400.0,
        "positions": [{"id": "ASML", "ticker": "ASMLa_EQ", "quantity": 2.0,
                       "priceEur": 650.0, "valueEur": 1300.0}],
        "accountTotalEur": 5000.0, "accountCashEur": 3700.0,
        "takenAt": row["takenAt"], "warnings": [],
    }
    assert pe.paper_ledger.latest_snapshot()["date"] == "2026-10-19"


def test_snapshot_equity_includes_cash_reserved_for_open_buys(env):
    env["broker"] = FakeBroker(account=acct(), positions=[])
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 4.0, None, state="submitted",
                                          limit=100.0, filled=0.0)])
    row = run(pe.take_snapshot("2026-10-19"))
    assert row["cashEur"] == 600.0 and row["equityEur"] == 1000.0   # an open order is not a loss


def test_take_snapshot_returns_none_when_broker_unreachable(env):
    env["broker"] = FakeBroker(error="auth")
    assert run(pe.take_snapshot()) is None


# ------------------------------------------------------------------ season

def test_create_season_derives_renormalised_weights_from_the_real_snapshot(env):
    env["real_positions"] = [
        {"id": "ASML", "weightPct": 30.0}, {"id": "NVDA", "weightPct": 20.0},
        {"id": "SXR8", "weightPct": 40.0}, {"id": "GSPC", "weightPct": 10.0}]
    row = run(pe.create_season())
    # GSPC is not in the universe: the remaining 90% renormalises to 100.
    assert row["baselineWeights"] == {
        "ASML": pytest.approx(round(30 / 90 * 100, 4)),
        "NVDA": pytest.approx(round(20 / 90 * 100, 4)),
        "SXR8": pytest.approx(round(40 / 90 * 100, 4))}
    assert row["inceptionDate"] == datetime.date.today().isoformat()
    assert row["strategyVersion"] == "s1" and row["book"] == "practice"
    assert pe.paper_ledger.read_season() == row


def test_create_season_409_when_one_already_exists(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    run(pe.create_season())
    with pytest.raises(pe.PaperConflict):
        run(pe.create_season())


def test_create_season_409_reset_but_bucket_still_holds_positions(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    run(pe.create_season())
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 1.0, 100.0)])   # the bucket's own fills hold ASML
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 1.0, 100.0)])
    with pytest.raises(pe.PaperConflict, match="still holds positions"):
        run(pe.create_season(reset=True))


def test_create_season_reset_succeeds_once_the_bucket_is_empty(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    first = run(pe.create_season())
    env["broker"] = FakeBroker(account=acct(), positions=[])
    second = run(pe.create_season(reset=True))
    assert second["inceptionDate"] == first["inceptionDate"]
    assert pe.paper_ledger.read_season() == second


def test_create_season_bad_request_without_universe_overlap(env):
    env["real_positions"] = [{"id": "GSPC", "weightPct": 100.0}]
    with pytest.raises(pe.PaperBadRequest, match="none of the paperTrading.universe"):
        run(pe.create_season())


def test_create_season_disabled(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING_ENABLED", raising=False)
    with pytest.raises(pe.PaperDisabled):
        run(pe.create_season())


# -------------------------------------------------------------- status

def test_status_reports_disabled_reason(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING_ENABLED", raising=False)
    monkeypatch.setattr(pe, "_broker", lambda: None)
    monkeypatch.setattr(pe.paper_buckets.config_store, "read", lambda: {})
    monkeypatch.setattr(pe.paper_ledger, "read_season", lambda bucket="daily": None)
    out = pe.status()
    assert out["enabled"] is False and "reason" in out
    assert out["season"] == {"started": False, "inceptionDate": None, "baselineWeights": {}, "previous": []}
    assert out["universe"] == {}


def test_status_reports_season_and_broker_state(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    run(pe.create_season())
    out = pe.status()
    assert out["enabled"] is True
    assert out["broker"]["configured"] is True
    assert out["season"]["started"] is True
    assert out["season"]["baselineWeights"] == {"ASML": pytest.approx(100.0)}
    assert out["universe"] == {"ASML": {"t212": "ASMLa_EQ", "kind": "equity"},
                               "NVDA": {"t212": "NVDd_EQ", "kind": "equity"},
                               "SXR8": {"t212": "SXR8d_EQ", "kind": "etf"}}


def test_status_broker_block_uses_the_brokers_own_reachability(env):
    brk = env["broker"]
    # recovered: the broker answers again, an old error is no longer shown
    brk.status_override = {"reachable": True, "lastError": broker_mod.err("timeout")}
    b = pe.status()["broker"]
    assert b["reachable"] is True and b["lastError"] is None
    # rate limited: still reachable, the 429 is shown with its pin
    brk.status_override = {"reachable": True, "rateLimitedUntil": 999.0,
                           "lastError": broker_mod.err("rate_limited", 429)}
    b = pe.status()["broker"]
    assert b["reachable"] is True and b["rateLimitedUntil"] == 999.0
    assert b["lastError"]["error"] == "rate_limited"
    # down: unreachable with the error
    brk.status_override = {"reachable": False, "lastError": broker_mod.err("auth", 401)}
    b = pe.status()["broker"]
    assert b["reachable"] is False and b["lastError"]["error"] == "auth"


# ----------------------------------------------------------------- baselines

def closes(prices_by_date):
    return [(d, p) for d, p in sorted(prices_by_date.items())]


def test_buy_hold_index_is_the_frozen_weight_average_return():
    weights = {"A": 60.0, "B": 40.0}
    axis = ["2026-01-01", "2026-01-02", "2026-01-03"]
    closes_by_id = {
        "A": closes({"2026-01-01": 100.0, "2026-01-02": 110.0, "2026-01-03": 121.0}),
        "B": closes({"2026-01-01": 50.0, "2026-01-02": 45.0, "2026-01-03": 50.0}),
    }
    idx = pe.baseline_index(closes_by_id, weights, axis, None)
    pct = [round((idx[d] - 1.0) * 100.0, 6) for d in axis]
    assert pct == [0.0, 2.0, 12.6]


def test_trend_baseline_moves_an_out_of_signal_id_to_cash_until_next_month_end(monkeypatch):
    weights = {"A": 100.0}
    axis = ["2026-01-05", "2026-01-30", "2026-02-02", "2026-02-27"]
    closes_by_id = {"A": closes({"2026-01-05": 100.0, "2026-01-30": 110.0,
                                 "2026-02-02": 110.0, "2026-02-27": 90.0})}
    signals = {"2026-01-05": "BUY", "2026-01-30": "BUY", "2026-02-27": "SELL"}

    def fake_signals(full_closes, day):
        return {"trend_10m": signals[day]}

    monkeypatch.setattr(pe.scoreboard, "baseline_signals", fake_signals)
    idx = pe.baseline_index(closes_by_id, weights, axis, "trend_10m")
    pct = {d: round((idx[d] - 1.0) * 100.0, 6) for d in axis}
    assert pct["2026-01-05"] == 0.0
    assert pct["2026-01-30"] == 10.0                     # 100 -> 110
    assert pct["2026-02-02"] == 10.0                     # unchanged, still in signal
    # Feb bears its own -18.18% move (still "in" until the Feb-27 re-evaluation
    # itself), THEN the id would sit flat in cash going forward.
    assert pct["2026-02-27"] == pytest.approx(-10.0, abs=0.01)   # 110 -> 90


def test_signal_baseline_matches_buy_hold_when_never_out_of_signal(monkeypatch):
    weights = {"A": 100.0}
    axis = ["2026-01-05", "2026-01-30"]
    closes_by_id = {"A": closes({"2026-01-05": 100.0, "2026-01-30": 120.0})}
    monkeypatch.setattr(pe.scoreboard, "baseline_signals",
                        lambda full_closes, day: {"momentum_12_1": "BUY"})
    idx_signal = pe.baseline_index(closes_by_id, weights, axis, "momentum_12_1")
    idx_hold = pe.baseline_index(closes_by_id, weights, axis, None)
    assert idx_signal == idx_hold


def test_month_end_dates_picks_the_last_date_per_calendar_month():
    dates = ["2026-01-05", "2026-01-20", "2026-01-30", "2026-02-02", "2026-02-27"]
    assert pe._month_end_dates(dates) == {"2026-01-30", "2026-02-27"}


# ================================================== Phase 4: cost model, metrics, grading

def test_snapshot_rows_carry_the_ledger_warning_codes_the_watchdog_reads(env):
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 1.0, 300.0)])
    env["quotes"] = {"ASML": quote(300.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 600.0)])     # ledger says 2, broker holds 1
    assert run(pe.take_snapshot("2026-10-19"))["warnings"] == ["ledger_mismatch"]


def test_cost_model_defaults_and_partial_overrides(env):
    assert pe._paper_cfg()["costModel"] == {"spreadBps": 10.0, "commissionEur": 0.0, "fxBps": 0.0}
    env["cfg"]["costModel"] = {"spreadBps": 4, "commissionEur": 1.5}
    assert pe._paper_cfg()["costModel"] == {"spreadBps": 4.0, "commissionEur": 1.5, "fxBps": 0.0}
    env["cfg"]["costModel"] = {"spreadBps": -3, "fxBps": "x"}              # garbage never reaches the maths
    assert pe._paper_cfg()["costModel"] == {"spreadBps": 10.0, "commissionEur": 0.0, "fxBps": 0.0}


class FakeMetrics:
    """Stands in for ``paper_metrics``: records what the wiring hands over."""

    def __init__(self, segments):
        self.segments, self.calls, self.graded = segments, [], []

    def version_segments(self, decisions, orders, inception):
        self.seen = (decisions, inception)
        return list(self.segments)

    def bucket_metrics(self, equity, buy_hold, fills, cost, flows=None):
        self.calls.append({"equity": equity, "buy_hold": buy_hold, "fills": fills, "cost": cost})
        return {"verdict": "inconclusive", "n": len(self.calls)}

    def grade_decisions(self, decisions, closes, horizons=(5, 20), baseline=None):
        self.graded.append((decisions, closes, baseline))
        return {"horizons": {"5": {}, "20": {}}, "rows": [], "warnings": []}


@pytest.fixture
def evaluated(env, monkeypatch):
    pe.paper_ledger.write_season({"book": "practice", "strategyVersion": "s1",
                                  "inceptionDate": "2026-10-16", "baselineWeights": {"ASML": 100.0}})
    for d, eq in (("2026-10-16", 1000.0), ("2026-10-19", 1010.0), ("2026-10-20", 1020.0)):
        pe.paper_ledger.save_snapshot_row({"date": d, "equityEur": eq, "cashEur": 0.0})
    pe.paper_ledger.save_orders([
        {"intentId": "a", "strategyVersion": "s1", "state": "filled", "filledQuantity": 1.0},
        {"intentId": "b", "strategyVersion": "s2", "state": "filled", "filledQuantity": 2.0},
        {"intentId": "c", "strategyVersion": "s2", "state": "shadow", "filledQuantity": 0}])
    env["cfg"].update(strategyVersion="s2", costModel={"spreadBps": 20})
    fm = FakeMetrics([{"strategyVersion": "s1", "from": "2026-10-16", "to": "2026-10-19"},
                      {"strategyVersion": "s2", "from": "2026-10-19", "to": None}])

    async def daily_closes(pid, period):
        return [("2026-10-16", 100.0), ("2026-10-19", 101.0), ("2026-10-20", 102.0)]

    monkeypatch.setattr(pe, "_metrics_mod", lambda: fm)
    monkeypatch.setattr(pe, "_decisions", lambda bucket="daily": [{"decisionId": "d1"}])
    monkeypatch.setattr(pe.prices, "daily_closes", daily_closes)
    fm.env = env
    return fm


def test_practice_performance_carries_metrics_for_the_current_version_and_all_versions(evaluated):
    out = run(pe.performance_summary("1M"))
    assert out["twrPct"] is not None                                         # the Phase 1 keys remain
    v1, v2 = out["versions"]
    assert (v1["strategyVersion"], v1["from"], v1["to"]) == ("s1", "2026-10-16", "2026-10-19")
    assert (v2["strategyVersion"], v2["from"], v2["to"]) == ("s2", "2026-10-19", None)
    assert out["metrics"] == v2["metrics"] == {"verdict": "inconclusive", "n": 2}
    c1, c2 = evaluated.calls
    assert c1["equity"] == {"2026-10-16": 1000.0, "2026-10-19": 1010.0}      # the slice includes its end date
    assert c2["equity"] == {"2026-10-19": 1010.0, "2026-10-20": 1020.0}
    assert [f["intentId"] for f in c1["fills"]] == ["a"] and [f["intentId"] for f in c2["fills"]] == ["b"]
    assert c2["cost"] == {"spreadBps": 20.0, "commissionEur": 0.0, "fxBps": 0.0}
    assert c2["buy_hold"] == pytest.approx({"2026-10-19": 1.01, "2026-10-20": 1.02})   # unrounded factors
    assert evaluated.seen == ([{"decisionId": "d1"}], "2026-10-16")


def test_performance_metrics_ignore_the_chart_range(evaluated):
    run(pe.performance_summary("1M"))
    first = evaluated.calls[:]
    evaluated.calls.clear()
    run(pe.performance_summary("ALL"))
    assert evaluated.calls == first


def test_a_current_version_without_rows_is_measured_from_inception(evaluated, env):
    env["cfg"]["strategyVersion"] = "s9"
    out = run(pe.performance_summary("ALL"))
    assert [v["strategyVersion"] for v in out["versions"]] == ["s1", "s2", "s9"]
    assert out["metrics"] == out["versions"][2]["metrics"]
    assert evaluated.calls[2]["equity"].keys() == {"2026-10-16", "2026-10-19", "2026-10-20"}


def test_performance_without_a_season_has_no_metrics(env):
    out = run(pe.performance_summary("1M"))
    assert out["metrics"] is None and out["versions"] == []


def test_a_metrics_failure_keeps_the_rest_of_the_payload(evaluated, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad")
    monkeypatch.setattr(evaluated, "bucket_metrics", boom)
    out = run(pe.performance_summary("ALL"))
    assert out["metrics"] is None and out["versions"] == []
    assert "metrics_unavailable" in [w["code"] for w in out["warnings"]]
    assert out["perfBenchmarkPct"] is not None


def test_grading_passes_the_frozen_baseline_and_universe_closes(evaluated):
    out = run(pe.grading())
    assert out["horizons"] == {"5": {}, "20": {}}
    (decisions, closes, baseline), = evaluated.graded
    assert decisions == [{"decisionId": "d1"}] and baseline == {"ASML": 100.0}
    assert set(closes) == {"ASML", "NVDA", "SXR8"} and closes["ASML"][0] == ("2026-10-16", 100.0)


def test_grading_is_disabled_without_paper_trading(env, monkeypatch):
    monkeypatch.setattr(pe, "_broker", lambda: None)
    with pytest.raises(pe.PaperDisabled):
        run(pe.grading())


# ============================================== Phase 5: buckets, compare

def with_intraday(env, **over):
    env["cfg"]["buckets"] = {"intraday": {"enabled": True, "mode": "shadow", "budgetEur": 600,
                                          **over}}


def i_fill(pid, side, qty, value, state="filled"):
    row = fill_row(pid, side, qty, value, state=state)
    row["bucket"] = "intraday"
    row["intentId"] = f"2026-10-18-intraday-i1-agent-{pid}-{side}-1"
    return row


def test_a_season_needs_the_enabled_budgets_to_fit_in_the_demo_account(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    with_intraday(env)                                              # 1000 + 600
    env["broker"] = FakeBroker(account=acct(total=1500.0), positions=[])
    with pytest.raises(pe.PaperConflict, match=r"budgets \(EUR 1,600\.00\) exceed .*\(EUR 1,500\.00\)"):
        run(pe.create_season())
    assert pe.paper_ledger.read_season() is None
    with pytest.raises(pe.PaperConflict, match="exceed"):
        run(pe.create_season(bucket="intraday"))                    # either bucket starts the same check
    env["broker"] = FakeBroker(account=acct(total=1600.0), positions=[])
    assert run(pe.create_season())["baselineWeights"] == {"ASML": 100.0}          # exactly enough


def test_a_disabled_bucket_budget_does_not_count_and_the_bucket_cannot_start(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    env["cfg"]["buckets"] = {"intraday": {"enabled": False, "budgetEur": 90000}}
    env["broker"] = FakeBroker(account=acct(total=1000.0), positions=[])
    assert run(pe.create_season())["bucket"] == "daily"
    with pytest.raises(pe.PaperConflict, match="not enabled"):
        run(pe.create_season(bucket="intraday"))


def test_an_unknown_account_total_makes_the_season_unavailable(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    env["broker"] = FakeBroker(account=acct(total=None), positions=[])
    with pytest.raises(pe.PaperUnavailable):
        run(pe.create_season())
    env["broker"] = FakeBroker(error="timeout")
    with pytest.raises(pe.PaperUnavailable):
        run(pe.create_season())


def test_each_bucket_has_its_own_season_file_row_and_strategy_version(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 60.0}, {"id": "NVDA", "weightPct": 40.0}]
    with_intraday(env, strategyVersion="i7")
    daily = run(pe.create_season())
    intra = run(pe.create_season(bucket="intraday"))
    assert (daily["bucket"], daily["strategyVersion"]) == ("daily", "s1")
    assert (intra["bucket"], intra["strategyVersion"]) == ("intraday", "i7")
    assert pe.paper_ledger.read_season("intraday") == intra and pe.paper_ledger.read_season() == daily
    with pytest.raises(pe.PaperConflict, match="already exists"):
        run(pe.create_season(bucket="intraday"))
    with pytest.raises(ValueError, match="bucket"):
        run(pe.create_season(bucket="weekly"))


def test_resetting_one_bucket_season_needs_that_buckets_positions_gone(env):
    env["real_positions"] = [{"id": "ASML", "weightPct": 100.0}]
    with_intraday(env)
    run(pe.create_season())
    run(pe.create_season(bucket="intraday"))
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 1.0, 100.0)])         # DAILY holds ASML
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 1.0, 100.0)])
    assert run(pe.create_season(reset=True, bucket="intraday"))["bucket"] == "intraday"
    with pytest.raises(pe.PaperConflict, match="still holds positions"):
        run(pe.create_season(reset=True))


def test_the_status_block_is_the_requested_buckets_and_lists_every_bucket(env):
    with_intraday(env, strategyVersion="i3")
    pe.paper_ledger.write_season({"book": "practice", "bucket": "intraday", "inceptionDate": "2026-10-19",
                                  "baselineWeights": {"ASML": 100.0}}, "intraday")
    pe.paper_ledger.write_halt({"halted": True, "reason": "x", "at": 1.0, "by": "owner"}, "intraday")
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 1.0, None, state="submitted", filled=0.0),
                                 i_fill("NVDA", "buy", 1.0, None, state="submitted"),
                                 i_fill("SXR8", "buy", 1.0, None, state="pending")])
    out = pe.status()
    assert out["bucketId"] == "daily" and out["mode"] == "off" and out["banner"]["text"].endswith("agent: off")
    assert set(out["buckets"]) == {"daily", "intraday"}
    d, i = out["buckets"]["daily"], out["buckets"]["intraday"]
    assert (d["enabled"], d["mode"], d["halted"], d["strategyVersion"], d["openIntents"]) == (
        True, "off", False, "s1", 1)
    assert d["season"]["started"] is False and d["nextSlot"] is None
    assert (i["enabled"], i["mode"], i["halted"], i["budgetEur"], i["strategyVersion"],
            i["openIntents"]) == (True, "shadow", True, 600.0, "i3", 2)
    assert i["season"] == {"started": True, "inceptionDate": "2026-10-19",
                           "baselineWeights": {"ASML": 100.0}, "previous": []}
    assert {"lastDate", "lastValid", "nextEligible", "reason"} <= set(i["decision"])
    assert i["decision"]["reason"] == "halted" and i["nextSlot"] is None
    top = pe.status("intraday")
    assert top["bucketId"] == "intraday" and top["mode"] == "shadow"
    assert top["bucket"]["budgetEur"] == 600.0 and top["season"]["started"] is True
    assert "intraday agent: shadow" in top["banner"]["text"] and top["banner"]["bucket"] == "intraday"
    assert set(top["buckets"]) == {"daily", "intraday"}


def test_a_disabled_intraday_bucket_reports_off_whatever_mode_is_written(env):
    env["cfg"]["buckets"] = {"intraday": {"enabled": False, "mode": "demo"}}
    i = pe.status()["buckets"]["intraday"]
    assert i["enabled"] is False and i["mode"] == "off"


def test_the_practice_payload_is_the_requested_buckets_own_ledger(env):
    with_intraday(env)
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 3.0, 100.0)])
    env["quotes"] = {"ASML": quote(110.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 200.0), i_fill("ASML", "buy", 1.0, 100.0)])
    d = run(pe.portfolio_snapshot())
    i = run(pe.portfolio_snapshot("intraday"))
    assert d["bucket"]["id"] == "daily" and i["bucket"]["id"] == "intraday"
    assert [(p["id"], p["shares"]) for p in d["positions"]] == [("ASML", 2.0)]
    assert [(p["id"], p["shares"]) for p in i["positions"]] == [("ASML", 1.0)]
    assert d["bucket"]["cashEur"] == 800.0 and d["bucket"]["budgetEur"] == 1000.0
    assert i["bucket"]["cashEur"] == 500.0 and i["bucket"]["budgetEur"] == 600.0     # budget 600 - 100
    assert d["warnings"] == [] and i["warnings"] == []                # broker 3 == 2 + 1: no mismatch
    assert i["banner"]["mode"] == "shadow" and "intraday agent" in i["banner"]["text"]
    assert d["banner"]["text"].endswith("agent: off")


def test_the_stale_fallback_reads_the_requested_buckets_snapshot(env):
    with_intraday(env)
    pe.paper_ledger.save_snapshot_row({"date": "2026-10-18", "equityEur": 111.0, "cashEur": 1.0,
                                       "positions": [], "takenAt": 1.0}, "intraday")
    env["broker"] = FakeBroker(error="timeout")
    assert run(pe.portfolio_snapshot("intraday"))["bucket"]["equityEur"] == 111.0
    with pytest.raises(pe.PaperUnavailable):
        run(pe.portfolio_snapshot())                                  # the daily bucket has none


def test_snapshots_are_taken_and_stored_per_bucket(env):
    with_intraday(env)
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 3.0, 100.0)])
    env["quotes"] = {"ASML": quote(110.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 200.0), i_fill("ASML", "buy", 1.0, 100.0)])
    d = run(pe.take_snapshot("2026-10-19"))
    i = run(pe.take_snapshot("2026-10-19", "intraday"))
    assert (d["bucket"], d["equityEur"]) == ("daily", 800.0 + 220.0)
    assert (i["bucket"], i["equityEur"]) == ("intraday", 500.0 + 110.0)
    assert [r["equityEur"] for r in pe.paper_ledger.read_snapshots()] == [1020.0]
    assert [r["equityEur"] for r in pe.paper_ledger.read_snapshots("intraday")] == [610.0]
    assert d["warnings"] == [] == i["warnings"]


def test_a_mismatch_against_the_sum_of_both_buckets_is_in_every_snapshot(env):
    with_intraday(env)
    env["broker"] = FakeBroker(account=acct(), positions=[position("ASMLa_EQ", 2.0, 100.0)])
    env["quotes"] = {"ASML": quote(100.0)}
    pe.paper_ledger.save_orders([fill_row("ASML", "buy", 2.0, 200.0), i_fill("ASML", "buy", 1.0, 100.0)])
    assert run(pe.take_snapshot("2026-10-19"))["warnings"] == ["ledger_mismatch"]
    assert run(pe.take_snapshot("2026-10-19", "intraday"))["warnings"] == ["ledger_mismatch"]


def test_history_and_performance_follow_the_bucket(env, monkeypatch):
    async def daily_closes(pid, period):
        return [("2026-10-16", 100.0), ("2026-10-19", 101.0)]
    monkeypatch.setattr(pe.prices, "daily_closes", daily_closes)
    for b, inc, eq in (("daily", "2026-10-16", 1000.0), ("intraday", "2026-10-19", 500.0)):
        pe.paper_ledger.write_season({"book": "practice", "inceptionDate": inc,
                                      "baselineWeights": {"ASML": 100.0}}, b)
        pe.paper_ledger.save_snapshot_row({"date": inc, "equityEur": eq, "cashEur": 0.0}, b)
    assert run(pe.history("ALL"))["points"][0]["valueEur"] == 1000.0
    h = run(pe.history("ALL", "intraday"))
    assert h["bucket"] == "intraday" and h["points"][0]["valueEur"] == 500.0
    assert run(pe.performance_summary("ALL", "intraday"))["startValueEur"] == 500.0
    assert run(pe.history("ALL", "intraday"))["warnings"][0]["code"] != "season_not_started"
    pe.paper_ledger.SEASON_FILE.with_name(pe.paper_ledger.SEASON_FILE.stem + ".intraday.json").unlink()
    assert run(pe.history("ALL", "intraday"))["warnings"][0]["code"] == "season_not_started"


def test_metrics_use_only_the_buckets_orders_decisions_and_cost_model(evaluated, env):
    pe.paper_ledger.write_season({"book": "practice", "inceptionDate": "2026-10-16",
                                  "baselineWeights": {"ASML": 100.0}}, "intraday")
    for d, eq in (("2026-10-16", 500.0), ("2026-10-19", 505.0)):
        pe.paper_ledger.save_snapshot_row({"date": d, "equityEur": eq, "cashEur": 0.0}, "intraday")
    rows = pe.paper_ledger.read_orders()
    pe.paper_ledger.save_orders(rows + [
        {"intentId": "i", "bucket": "intraday", "strategyVersion": "i1", "state": "filled",
         "filledQuantity": 4.0}])
    env["cfg"]["buckets"] = {"intraday": {"enabled": True, "strategyVersion": "i1"}}
    evaluated.calls.clear()
    out = run(pe.performance_metrics("intraday"))
    assert [v["strategyVersion"] for v in out["versions"]] == ["s1", "s2", "i1"]
    call = evaluated.calls[-1]
    assert call["equity"] == {"2026-10-16": 500.0, "2026-10-19": 505.0}
    assert [f["intentId"] for f in call["fills"]] == ["i"]
    assert evaluated.seen[1] == "2026-10-16"


def test_grading_uses_one_and_five_sessions_for_intraday_and_five_and_twenty_for_daily(evaluated, monkeypatch):
    seen = []

    def grade(decisions, closes, horizons=(5, 20), baseline=None):
        seen.append((horizons, baseline))
        return {"horizons": {}, "rows": [], "warnings": []}
    monkeypatch.setattr(evaluated, "grade_decisions", grade)
    pe.paper_ledger.write_season({"book": "practice", "inceptionDate": "2026-10-16",
                                  "baselineWeights": {"NVDA": 100.0}}, "intraday")
    run(pe.grading())
    run(pe.grading("intraday"))
    assert seen == [((5, 20), {"ASML": 100.0}), ((1, 5), {"NVDA": 100.0})]


FILL_TS = {d: datetime.datetime.fromisoformat(d + "T12:00:00+00:00").timestamp()
           for d in ("2026-10-13", "2026-10-15")}


@pytest.fixture
def two_seasons(env, monkeypatch):
    """Daily since 2026-10-12, intraday since 2026-10-14; ASML closes +1 per day."""
    with_intraday(env)
    dates = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]

    async def daily_closes(pid, period):
        return [(d, 100.0 + i) for i, d in enumerate(dates)]
    monkeypatch.setattr(pe.prices, "daily_closes", daily_closes)
    for b, inc in (("daily", "2026-10-12"), ("intraday", "2026-10-14")):
        pe.paper_ledger.write_season({"book": "practice", "inceptionDate": inc,
                                      "baselineWeights": {"ASML": 100.0}}, b)
    for d, eq in zip(dates, (1000.0, 1010.0, 1020.0, 1030.0, 1040.0)):
        pe.paper_ledger.save_snapshot_row({"date": d, "equityEur": eq, "cashEur": 0.0})
    for d, eq in zip(dates[2:], (600.0, 603.0, 606.0)):
        pe.paper_ledger.save_snapshot_row({"date": d, "equityEur": eq, "cashEur": 0.0}, "intraday")
    pe.paper_ledger.save_orders([
        {**fill_row("ASML", "buy", 2.0, 200.0), "intentId": "2026-10-15-daily-s1-agent-ASML-buy-1",
         "strategyVersion": "s1", "updatedAt": FILL_TS["2026-10-15"]},
        {**i_fill("ASML", "buy", 1.0, 100.0), "intentId": "2026-10-15-intraday-i1-agent-ASML-buy-1",
         "strategyVersion": "i1", "updatedAt": FILL_TS["2026-10-15"]},
        {**i_fill("ASML", "sell", 1.0, 100.0), "intentId": "2026-10-13-intraday-i1-agent-ASML-sell-1",
         "strategyVersion": "i1", "updatedAt": FILL_TS["2026-10-13"]}])    # before the window: not counted
    monkeypatch.setattr(pe, "_decisions", lambda bucket="daily": (
        [{"valid": True, "noChange": True}, {"valid": True, "noChange": False},
         {"valid": False}, {"deferred": "budget", "valid": False}] if bucket == "daily"
        else [{"valid": True, "noChange": True}]))
    return env


def test_compare_measures_both_buckets_and_buy_and_hold_on_the_same_window(two_seasons):
    out = run(pe.compare())
    assert set(out) >= {"asOf", "buckets", "buyHold", "decisions", "turnoverPct", "costDragPct"}
    assert out["from"] == "2026-10-14"                          # the later of the two inceptions
    d, i = out["buckets"]["daily"], out["buckets"]["intraday"]
    assert d["sessions"] == i["sessions"] == 2
    # the basket (ASML 102 -> 104) is +1.96 % over the common window for both buckets
    assert d["buyHoldPct"] == pytest.approx(104 / 102 * 100 - 100, abs=1e-3)
    assert i["buyHoldPct"] == pytest.approx(104 / 102 * 100 - 100, abs=1e-3)
    assert d["returnPct"] == pytest.approx(1040 / 1020 * 100 - 100, abs=1e-3)
    assert i["returnPct"] == pytest.approx(606 / 600 * 100 - 100, abs=1e-3)
    assert d["excessPct"] == pytest.approx(0.0, abs=1e-3)
    assert i["excessPct"] == pytest.approx(1.0 - (104 / 102 * 100 - 100), abs=1e-3)
    assert d["verdict"] == i["verdict"] == "insufficient_sample"
    assert out["buyHold"]["sessions"] == 2
    assert out["buyHold"]["returnPct"] == pytest.approx(104 / 102 * 100 - 100, abs=1e-3)
    assert out["buyHold"]["sessions"] == 2 and "maxDrawdownPct" in out["buyHold"]
    assert out["decisions"] == {"daily": {"valid": 1, "noChange": 1, "invalid": 1, "deferred": 1},
                                "intraday": {"valid": 0, "noChange": 1, "invalid": 0, "deferred": 0}}
    assert out["turnoverPct"]["daily"] == pytest.approx(200.0 / 1030.0 * 100, abs=1e-2)
    assert out["turnoverPct"]["intraday"] == pytest.approx(100.0 / 603.0 * 100, abs=1e-2)
    assert out["costDragPct"]["daily"] == pytest.approx(200 * 0.0005 / 1020.0 * 100, abs=1e-3)
    assert out["costDragPct"]["intraday"] == pytest.approx(100 * 0.0005 / 600.0 * 100, abs=1e-3)


def test_compare_with_one_bucket_started_has_none_for_the_other(env, monkeypatch):
    async def daily_closes(pid, period):
        return [("2026-10-16", 100.0), ("2026-10-19", 102.0)]
    monkeypatch.setattr(pe.prices, "daily_closes", daily_closes)
    pe.paper_ledger.write_season({"book": "practice", "inceptionDate": "2026-10-16",
                                  "baselineWeights": {"ASML": 100.0}})
    pe.paper_ledger.save_snapshot_row({"date": "2026-10-16", "equityEur": 1000.0, "cashEur": 0.0})
    pe.paper_ledger.save_snapshot_row({"date": "2026-10-19", "equityEur": 1010.0, "cashEur": 0.0})
    out = run(pe.compare())
    assert out["buckets"]["intraday"] is None and out["buckets"]["daily"]["returnPct"] == pytest.approx(1.0)
    assert out["turnoverPct"]["intraday"] is None and out["costDragPct"]["intraday"] is None
    assert out["decisions"]["intraday"] == {"valid": 0, "noChange": 0, "invalid": 0, "deferred": 0}


def test_compare_without_any_season_is_empty_but_well_formed(env):
    out = run(pe.compare())
    assert out["buckets"] == {"daily": None, "intraday": None} and out["buyHold"] is None
    assert out["from"] is None and out["warnings"] == []


def test_compare_survives_a_failing_bucket(two_seasons, monkeypatch):
    real = pe.paper_ledger.read_snapshots

    def flaky(bucket="daily"):
        if bucket == "intraday":
            raise RuntimeError("bad file")
        return real(bucket)
    monkeypatch.setattr(pe.paper_ledger, "read_snapshots", flaky)
    out = run(pe.compare())
    assert out["buckets"]["intraday"] is None and out["buckets"]["daily"] is not None
    assert [(w["code"], w["bucket"]) for w in out["warnings"]] == [("metrics_unavailable", "intraday")]


def test_compare_is_disabled_without_paper_trading(env, monkeypatch):
    monkeypatch.setattr(pe, "_broker", lambda: None)
    with pytest.raises(pe.PaperDisabled):
        run(pe.compare())


def test_the_enabled_bucket_helpers_follow_the_config_seam(env):
    assert pe.enabled_buckets() == ["daily"] and pe.budget_total() == 1000.0
    with_intraday(env)
    assert pe.enabled_buckets() == ["daily", "intraday"] and pe.budget_total() == 1600.0


# ------------------------------------------------------------------ sector + previous seasons

def _registry_rows(monkeypatch, rows):
    monkeypatch.setattr(pe.paper_universe, "get",
                        lambda pid: rows.get(str(pid).upper()) if isinstance(pid, str) else None)


def test_practice_rows_carry_the_registry_sector_and_name_label(env, monkeypatch):
    _registry_rows(monkeypatch, {"N01": {"id": "N01", "name": "N01 AG", "kind": "equity",
                                         "sector": "Industrials"}})
    monkeypatch.setattr(pe.registry, "get", lambda pid: REGISTRY.get(str(pid).upper()))
    rows = pe._format_rows(
        [{"id": "N01", "valueEur": 100.0, "investedEur": 90.0, "priceEur": 10.0, "prevCloseEur": 9.0,
          "quantity": 10.0, "priceAsOf": 1, "priceSource": "yahoo", "stale": False, "ticker": "N01d_EQ"},
         {"id": "ASML", "valueEur": 50.0, "investedEur": 40.0, "priceEur": 5.0, "prevCloseEur": 4.0,
          "quantity": 10.0, "priceAsOf": 1, "priceSource": "yahoo", "stale": False, "ticker": "ASMLa_EQ"}],
        150.0, {})
    by_id = {r["id"]: r for r in rows}
    assert (by_id["N01"]["sector"], by_id["N01"]["label"]) == ("Industrials", "N01 AG")
    assert by_id["ASML"]["sector"] is None and by_id["ASML"]["label"] == "ASML"      # not a registry entry

    snap = {"takenAt": 1.0, "positions": [{"id": "N01", "quantity": 1.0, "priceEur": 10.0, "valueEur": 10.0},
                                          {"id": "ASML", "quantity": 1.0, "priceEur": 5.0, "valueEur": 5.0}]}
    from_snap = {r["id"]: r for r in pe._rows_from_snapshot(snap, 15.0)}
    assert (from_snap["N01"]["sector"], from_snap["N01"]["label"]) == ("Industrials", "N01 AG")
    assert from_snap["ASML"]["sector"] is None


def test_the_season_block_lists_previous_seasons_with_their_end():
    season = {"strategyVersion": "s2", "inceptionDate": "2026-10-20", "startedAt": 300.0,
              "baselineWeights": {"EUNL": 100.0},
              "previous": [{"strategyVersion": "s1", "inceptionDate": "2026-10-01", "startedAt": 100.0,
                            "baselineWeights": {"ASML": 100.0}},
                           {"strategyVersion": "s1b", "inceptionDate": "2026-10-10", "startedAt": 200.0}]}
    block = pe._season_block(season)
    assert block["previous"] == [
        {"strategyVersion": "s1", "inceptionDate": "2026-10-01", "endedAt": 200.0},
        {"strategyVersion": "s1b", "inceptionDate": "2026-10-10", "endedAt": 300.0}]
    assert pe._season_block({"inceptionDate": "2026-10-20", "startedAt": 1.0})["previous"] == []
    assert pe._season_block(None)["previous"] == []
