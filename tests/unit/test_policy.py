"""policy: band math, exact water-filling, equity-only single-stock breaches, the
month-end trend filter, and the daily watcher (edge-triggered pushes, state
consumed only for a non-failed Delivery, weekly reminder). No network, no
sleeps: the portfolio snapshot, closes, clock, sessions and notify are faked.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import calendar
import datetime

import pytest

from app.api import notify, policy

TODAY = datetime.date(2026, 10, 15)
T0 = 1_792_000_000.0
DAY = 86400.0


def run(coro):
    return asyncio.run(coro)


def row(ident, value, kind="equity"):
    return {"id": ident, "kind": kind, "valueEur": value}


@pytest.fixture
def pol(monkeypatch, tmp_path):
    h = {"cfg": {}, "rows": [], "closes": {}, "holdings": ["ASML", "SXR8"],
         "bench": "GSPC", "today": TODAY, "now": T0, "session": True,
         "delivery": notify.Delivery(notify.SENT), "alerts": [], "closes_calls": []}

    async def snapshot():
        return {"positions": [dict(r) for r in h["rows"]]}

    async def daily_closes(ident, period="2y"):
        h["closes_calls"].append(ident)
        return list(h["closes"].get(ident, []))

    async def alert(ticker, source, title, body, **kw):
        h["alerts"].append({"ticker": ticker, "source": source, "title": title,
                            "body": body, **kw})
        d = h["delivery"]
        return d(title) if callable(d) else d

    monkeypatch.setattr(policy, "STATE_FILE", tmp_path / "policy_state.json")
    monkeypatch.setattr(policy.config_store, "read", lambda: h["cfg"])
    monkeypatch.setattr(policy.portfolio, "snapshot", snapshot)
    monkeypatch.setattr(policy.prices, "daily_closes", daily_closes)
    monkeypatch.setattr(policy.registry, "holdings", lambda: list(h["holdings"]))
    monkeypatch.setattr(policy.registry, "benchmark_id", lambda: h["bench"])
    monkeypatch.setattr(policy.notify, "alert", alert)
    monkeypatch.setattr(policy, "_today", lambda: h["today"])
    monkeypatch.setattr(policy, "_now", lambda: h["now"])
    monkeypatch.setattr(policy, "_session_today", lambda day: h["session"])
    return h


def titles(h):
    return [a["title"] for a in h["alerts"]]


# ------------------------------------------------------------------ band math

def test_band_half_width_is_the_smaller_of_absolute_and_relative():
    assert policy.band_for(10) == (7.5, 12.5)        # 10 x 25% = 2.5 < 5
    assert policy.band_for(50) == (45.0, 55.0)       # 50 x 25% = 12.5 > 5
    assert policy.band_for(10, {"absPct": 2, "relPct": 50}) == (8.0, 12.0)
    assert policy.band_for(2) == (1.5, 2.5)
    assert policy.band_for(1, {"absPct": 5, "relPct": 200}) == (0.0, 3.0)   # floored at 0


def test_status_on_the_edge_is_ok():
    assert policy.band_status(12.5, 7.5, 12.5) == "ok"
    assert policy.band_status(12.51, 7.5, 12.5) == "over"
    assert policy.band_status(7.49, 7.5, 12.5) == "under"


def test_payload_drift_rows_use_priced_weights_and_the_band(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 10, "NVDA": 40, "SXR8": 50}}}
    pol["rows"] = [row("ASML", 1250.0), row("NVDA", 3000.0), row("SXR8", 5750.0, "etf")]
    out = run(policy.policy_payload())
    assert out["configured"] is True
    assert out["current"] == {"SXR8": 57.5, "NVDA": 30.0, "ASML": 12.5}
    assert out["drift"] == [
        {"id": "ASML", "targetPct": 10, "currentPct": 12.5, "bandLoPct": 7.5,
         "bandHiPct": 12.5, "status": "ok"},                       # exactly on the edge
        {"id": "NVDA", "targetPct": 40, "currentPct": 30.0, "bandLoPct": 35.0,
         "bandHiPct": 45.0, "status": "under"},
        {"id": "SXR8", "targetPct": 50, "currentPct": 57.5, "bandLoPct": 45.0,
         "bandHiPct": 55.0, "status": "over"}]
    assert out["targets"] == {"ASML": 10, "NVDA": 40, "SXR8": 50} and out["asOf"]


def test_without_investment_policy_the_payload_is_unconfigured_but_has_weights(pol):
    pol["rows"] = [row("ASML", 3000.0), row("SXR8", 1000.0, "etf")]
    out = run(policy.policy_payload())
    assert out["configured"] is False and out["targets"] == {}
    assert out["current"] == {"ASML": 75.0, "SXR8": 25.0}
    assert out["drift"] == [] and out["trend"] == []
    assert out["maxSingleStock"] == {"limitPct": None, "breaches": []}
    assert out["contribution"] == {"amountEur": 0.0, "allocation": {}}
    assert pol["closes_calls"] == []


def test_an_unpriced_holding_is_left_out_of_weights_and_drift(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 50, "SXR8": 50}}}
    pol["rows"] = [row("ASML", 1000.0), row("SXR8", None, "etf")]
    out = run(policy.policy_payload())
    assert out["current"] == {"ASML": 100.0}
    assert [d["id"] for d in out["drift"]] == ["ASML"]
    assert "SXR8" not in out["contribution"]["allocation"]
    assert any(w["code"] == "unpriced" and w["ids"] == ["SXR8"] for w in out["warnings"])


def test_a_failing_snapshot_degrades_instead_of_raising(pol, monkeypatch):
    async def boom():
        raise RuntimeError("provider down")
    monkeypatch.setattr(policy.portfolio, "snapshot", boom)
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 100}}}
    out = run(policy.policy_payload())
    assert out["current"] == {} and out["drift"] == []
    assert out["warnings"][0]["code"] == "portfolio_unavailable"


# ------------------------------------------------------------ single stock

def test_max_single_stock_counts_equities_only_never_etfs(pol):
    pol["cfg"] = {"investmentPolicy": {"maxSingleStockPct": 35}}
    pol["rows"] = [row("ASML", 4000.0), row("NVDA", 2000.0),
                   row("SXR8", 4000.0, "etf")]                     # the ETF is 40% too
    out = run(policy.policy_payload())
    assert out["maxSingleStock"] == {"limitPct": 35, "breaches": ["ASML"]}


def test_a_weight_exactly_at_the_limit_is_not_a_breach(pol):
    pol["cfg"] = {"investmentPolicy": {"maxSingleStockPct": 35}}
    pol["rows"] = [row("ASML", 3500.0), row("SXR8", 6500.0, "etf")]
    assert run(policy.policy_payload())["maxSingleStock"]["breaches"] == []


# ------------------------------------------------------------ contribution

def test_water_fill_known_answers():
    assert policy.water_fill([600, 400], [.5, .5], 100) == pytest.approx([0, 100])
    assert policy.water_fill([600, 400], [.5, .5], 250) == pytest.approx([25, 225])
    assert policy.water_fill([100, 200, 700], [.2, .3, .5], 100) == pytest.approx([60, 40, 0])
    assert policy.water_fill([0, 0], [.7, .3], 100) == pytest.approx([70, 30])


def test_water_fill_zero_contribution_allocates_nothing():
    assert policy.water_fill([600, 400], [.5, .5], 0) == [0.0, 0.0]
    assert policy.water_fill([600, 400], [.5, .5], -5) == [0.0, 0.0]
    assert policy.water_fill([600, 400], [0, 0], 100) == [0.0, 0.0]


def test_water_fill_never_sells_when_every_holding_is_overweight():
    # targets cover only 60% of the plan, so both holdings sit above L x t:
    # nothing is sold (no negative amount) and the money still goes somewhere
    a = policy.water_fill([600, 400], [.3, .3], 100)
    assert a == pytest.approx([0, 100]) and min(a) >= 0
    assert sum(a) == pytest.approx(100)


def test_to_cents_sums_to_the_amount_exactly():
    third = policy.to_cents([100 / 3] * 3, 100)
    assert round(sum(third) * 100) == 10000 and sorted(third) == [33.33, 33.33, 33.34]
    assert round(sum(policy.to_cents([.01 / 3] * 3, .01)) * 100) == 1
    assert policy.to_cents([0.0, 0.0], 0) == [0.0, 0.0]
    assert policy.to_cents([0, 100.0], 100) == [0.0, 100.0]


def test_payload_contribution_goes_to_the_most_underweight_target(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 50, "SXR8": 50},
                                       "monthlyContributionEur": 300}}
    pol["rows"] = [row("ASML", 5000.0), row("SXR8", 4000.0, "etf")]
    out = run(policy.policy_payload())
    assert out["contribution"] == {"amountEur": 300.0,
                                   "allocation": {"ASML": 0.0, "SXR8": 300.0}}


def test_payload_contribution_splits_when_the_gap_is_wider_than_the_amount(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 50, "SXR8": 50},
                                       "monthlyContributionEur": 250}}
    pol["rows"] = [row("ASML", 600.0), row("SXR8", 400.0, "etf")]
    alloc = run(policy.policy_payload())["contribution"]["allocation"]
    assert alloc == {"ASML": 25.0, "SXR8": 225.0}


def test_payload_every_holding_over_its_band_still_sums_to_the_contribution(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 30, "NVDA": 30},
                                       "monthlyContributionEur": 300}}
    pol["rows"] = [row("ASML", 4500.0), row("NVDA", 4500.0), row("SXR8", 1000.0, "etf")]
    out = run(policy.policy_payload())
    assert [d["status"] for d in out["drift"]] == ["over", "over"]
    assert out["contribution"]["allocation"] == {"ASML": 150.0, "NVDA": 150.0}
    assert any(w["code"] == "no_target" and w["ids"] == ["SXR8"] for w in out["warnings"])


def test_payload_zero_contribution_and_an_unheld_target(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 50, "NVDA": 50}}}
    pol["rows"] = [row("ASML", 1000.0)]
    out = run(policy.policy_payload())
    assert out["contribution"] == {"amountEur": 0.0,
                                   "allocation": {"ASML": 0.0, "NVDA": 0.0}}
    assert out["drift"][1]["currentPct"] == 0.0 and out["drift"][1]["status"] == "under"
    pol["cfg"]["investmentPolicy"]["monthlyContributionEur"] = 100
    assert run(policy.policy_payload())["contribution"]["allocation"] == {
        "ASML": 0.0, "NVDA": 100.0}


def test_payload_contribution_cents_add_up(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"A": 1, "B": 1, "C": 1},
                                       "monthlyContributionEur": 100}}
    pol["rows"] = [row("A", 1.0), row("B", 1.0), row("C", 1.0)]
    alloc = run(policy.policy_payload())["contribution"]["allocation"]
    assert round(sum(alloc.values()) * 100) == 10000


# ------------------------------------------------------------------ trend

def month_end_rows(values, last=(2026, 9)):
    """A decoy mid-month bar plus the month-end bar per month, oldest first;
    ``values[-1]`` is the ``last`` month's month-end close."""
    y, m = last
    months = []
    for _ in values:
        months.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    rows = []
    for (y, m), v in zip(reversed(months), values):
        rows.append((f"{y}-{m:02d}-15", 9999.0))
        rows.append((f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}", float(v)))
    return rows


FLIP = [100] * 11 + [120]            # below at the Aug month end, above at Sep's
RISING = list(range(100, 112))       # above at both


def test_trend_sma_is_the_mean_of_the_last_ten_completed_month_ends():
    closes = month_end_rows(FLIP) + [("2026-10-02", 1.0)]    # running month is ignored
    assert policy.trend_row("ASML", closes, TODAY) == {
        "id": "ASML", "monthEnd": "2026-09-30", "close": 120.0, "sma10m": 102.0,
        "state": "above", "changed": True}


def test_trend_unchanged_state_and_the_equal_close_is_below():
    up = policy.trend_row("SXR8", month_end_rows(RISING), TODAY)
    assert (up["state"], up["changed"], up["sma10m"]) == ("above", False, 106.5)
    flat = policy.trend_row("SXR8", month_end_rows([100] * 10), TODAY)
    assert (flat["state"], flat["changed"], flat["sma10m"]) == ("below", False, 100.0)
    down = policy.trend_row("SXR8", month_end_rows([100] * 10 + [120, 100]), TODAY)
    assert (down["state"], down["changed"]) == ("below", True)


def test_trend_needs_ten_completed_months_and_a_current_series():
    assert policy.trend_row("X", month_end_rows([100] * 9), TODAY) is None
    stale = month_end_rows([100] * 12, last=(2026, 8))        # no September bars
    assert policy.trend_row("X", stale, TODAY) is None
    assert policy.trend_row("X", [], TODAY) is None


def test_the_running_month_is_never_completed_until_it_ends():
    rows = month_end_rows([100] * 9, last=(2026, 8)) + [("2026-09-30", 7.0)]
    assert policy.month_ends(rows, datetime.date(2026, 9, 30))[-1] == ("2026-08-31", 100.0)
    assert policy.month_ends(rows, datetime.date(2026, 10, 1))[-1] == ("2026-09-30", 7.0)


def test_payload_trend_covers_holdings_plus_the_benchmark_and_skips_short_series(pol):
    pol["cfg"] = {"investmentPolicy": {"trendFilter": True}}
    pol["rows"] = [row("ASML", 5000.0), row("SXR8", 5000.0, "etf")]
    pol["closes"] = {"ASML": month_end_rows(FLIP), "SXR8": month_end_rows(RISING),
                     "GSPC": month_end_rows([100] * 4)}
    out = run(policy.policy_payload())
    assert [t["id"] for t in out["trend"]] == ["ASML", "SXR8"]
    assert pol["closes_calls"] == ["ASML", "SXR8", "GSPC"]
    assert any(w["code"] == "trend_unavailable" and w["ids"] == ["GSPC"]
               for w in out["warnings"])


def test_trend_is_off_unless_the_filter_is_enabled(pol):
    pol["cfg"] = {"investmentPolicy": {"targets": {"ASML": 100}}}
    pol["rows"] = [row("ASML", 1000.0)]
    assert run(policy.policy_payload())["trend"] == [] and pol["closes_calls"] == []


# ------------------------------------------------------------------ worker

BAND_CFG = {"investmentPolicy": {"targets": {"ASML": 50, "SXR8": 50},
                                 "monthlyContributionEur": 200}}


def band_rows(asml, sxr8):
    return [row("ASML", asml), row("SXR8", sxr8, "etf")]


def test_each_band_edge_pushes_once_then_stays_quiet(pol):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)      # 60 / 40: out of band
    res = run(policy.run_once())
    assert res["pushed"] == 2 and res["ok"] is True
    assert titles(pol) == ["Policy: ASML over its band", "Policy: SXR8 under its band"]
    first = pol["alerts"][0]
    assert (first["ticker"], first["source"], first["priority"]) == ("POLICY", "policy", 3)
    assert "60.0% vs target 50% (band 45.0-55.0%)" in first["body"]
    assert "Next contribution: EUR 200.00 -> SXR8" in pol["alerts"][1]["body"]
    assert policy._load_state()["band"] == {"ASML": "over", "SXR8": "under"}

    assert run(policy.run_once())["pushed"] == 0 and len(pol["alerts"]) == 2   # no repeat

    pol["rows"] = band_rows(5200.0, 4800.0)                                   # both back in band
    assert run(policy.run_once())["pushed"] == 2
    assert titles(pol)[2:] == ["Policy: ASML back within its band",
                               "Policy: SXR8 back within its band"]
    assert policy._load_state()["band"] == {}
    assert run(policy.run_once())["pushed"] == 0


def test_a_direct_over_to_under_move_is_an_edge(pol):
    pol["cfg"] = BAND_CFG
    pol["rows"] = band_rows(6000.0, 4000.0)
    run(policy.run_once())
    pol["rows"] = band_rows(4000.0, 6000.0)
    run(policy.run_once())
    assert titles(pol)[2:] == ["Policy: ASML moved from over to under band",
                               "Policy: SXR8 moved from under to over band"]
    assert policy._load_state()["band"] == {"ASML": "under", "SXR8": "over"}


def test_a_failed_delivery_leaves_the_state_and_is_retried(pol):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    pol["delivery"] = notify.Delivery(notify.FAILED)
    res = run(policy.run_once())
    assert (res["pushed"], res["failed"], res["ok"]) == (0, 2, False)
    assert policy._load_state()["band"] == {} and policy._load_state()["lastPass"] is None

    pol["delivery"] = notify.Delivery(notify.SENT)
    assert run(policy.run_once())["pushed"] == 2                  # both edges retried
    assert policy._load_state()["band"] == {"ASML": "over", "SXR8": "under"}


def test_only_the_failed_edge_is_retried(pol):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    pol["delivery"] = lambda title: (notify.Delivery(notify.FAILED) if "ASML" in title
                                     else notify.Delivery(notify.SENT))
    run(policy.run_once())
    assert policy._load_state()["band"] == {"SXR8": "under"}
    pol["delivery"] = notify.Delivery(notify.SENT)
    before = len(pol["alerts"])
    assert run(policy.run_once())["pushed"] == 1
    assert titles(pol)[before:] == ["Policy: ASML over its band"]


@pytest.mark.parametrize("status", [notify.SENT, notify.QUEUED, notify.FILTERED,
                                    notify.DUPLICATE])
def test_every_non_failed_status_consumes_the_edge(pol, status):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    pol["delivery"] = notify.Delivery(status)
    assert run(policy.run_once())["pushed"] == 2
    assert run(policy.run_once())["pushed"] == 0
    assert policy._load_state()["band"] == {"ASML": "over", "SXR8": "under"}


def test_single_stock_breach_entered_and_left(pol):
    pol["cfg"] = {"investmentPolicy": {"maxSingleStockPct": 35}}
    pol["rows"] = [row("ASML", 4000.0), row("SXR8", 6000.0, "etf")]   # the ETF at 60% is ignored
    run(policy.run_once())
    assert titles(pol) == ["Policy: ASML above the single-stock limit"]
    assert "40.0% of the portfolio, above the 35% single-stock limit" in pol["alerts"][0]["body"]
    assert policy._load_state()["single"] == ["ASML"]
    assert run(policy.run_once())["pushed"] == 0

    pol["rows"] = [row("ASML", 3000.0), row("SXR8", 7000.0, "etf")]
    run(policy.run_once())
    assert titles(pol)[1:] == ["Policy: ASML back under the single-stock limit"]
    assert policy._load_state()["single"] == []


def test_single_stock_state_is_not_consumed_by_a_failed_push(pol):
    pol["cfg"] = {"investmentPolicy": {"maxSingleStockPct": 35}}
    pol["rows"] = [row("ASML", 4000.0), row("SXR8", 6000.0, "etf")]
    pol["delivery"] = notify.Delivery(notify.FAILED)
    run(policy.run_once())
    assert policy._load_state()["single"] == []
    pol["delivery"] = notify.Delivery(notify.SENT)
    assert run(policy.run_once())["pushed"] == 1


def test_a_trend_flip_pushes_once_per_month_end(pol):
    pol["cfg"] = {"investmentPolicy": {"trendFilter": True}}
    pol["rows"] = band_rows(5000.0, 5000.0)
    pol["closes"] = {"ASML": month_end_rows(FLIP), "SXR8": month_end_rows(RISING)}
    run(policy.run_once())
    assert titles(pol) == ["Policy: ASML trend flipped above"]      # SXR8 did not change
    assert "month-end close 120 is above its 10-month average 102" in pol["alerts"][0]["body"]
    assert policy._load_state()["trendAlerted"] == {"ASML": "2026-09-30"}
    pol["today"] = datetime.date(2026, 10, 20)                       # same month end, later pass
    assert run(policy.run_once())["pushed"] == 0


def test_a_failed_trend_push_is_retried_next_pass(pol):
    pol["cfg"] = {"investmentPolicy": {"trendFilter": True}}
    pol["rows"] = band_rows(5000.0, 5000.0)
    pol["closes"] = {"ASML": month_end_rows(FLIP)}
    pol["delivery"] = notify.Delivery(notify.FAILED)
    run(policy.run_once())
    assert policy._load_state()["trendAlerted"] == {}
    pol["delivery"] = notify.Delivery(notify.SENT)
    assert run(policy.run_once())["pushed"] == 1


def reminder_setup(pol, last_reminder):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    assert policy.jsonstore.save(policy.STATE_FILE, {
        "version": 1, "band": {"ASML": "over", "SXR8": "under"}, "single": [],
        "trendAlerted": {}, "lastReminder": last_reminder, "lastPass": None})


def test_weekly_reminder_while_out_of_band(pol):
    reminder_setup(pol, T0)
    pol["now"] = T0 + 6 * DAY
    assert run(policy.run_once())["pushed"] == 0                     # day 6: not yet
    pol["now"] = T0 + 7 * DAY
    assert run(policy.run_once())["pushed"] == 1
    sent = pol["alerts"][0]
    assert sent["title"] == "Policy reminder: 2 out of band" and sent["priority"] == 3
    assert "ASML over: 60.0% vs target 50%" in sent["body"]
    assert policy._load_state()["lastReminder"] == T0 + 7 * DAY      # the clock restarted
    pol["now"] = T0 + 13 * DAY
    assert run(policy.run_once())["pushed"] == 0
    pol["now"] = T0 + 14 * DAY
    assert run(policy.run_once())["pushed"] == 1


def test_a_failed_reminder_keeps_the_clock_and_is_retried(pol):
    reminder_setup(pol, T0)
    pol["now"] = T0 + 8 * DAY
    pol["delivery"] = notify.Delivery(notify.FAILED)
    assert run(policy.run_once())["failed"] == 1
    assert policy._load_state()["lastReminder"] == T0
    pol["delivery"] = notify.Delivery(notify.SENT)
    assert run(policy.run_once())["pushed"] == 1


def test_no_reminder_once_everything_is_back_in_band(pol):
    reminder_setup(pol, T0)
    pol["rows"] = band_rows(5100.0, 4900.0)
    pol["now"] = T0 + 30 * DAY
    run(policy.run_once())
    assert titles(pol) == ["Policy: ASML back within its band",
                           "Policy: SXR8 back within its band"]    # edges, no reminder
    assert policy._load_state()["lastReminder"] is None
    pol["now"] = T0 + 60 * DAY
    assert run(policy.run_once())["pushed"] == 0


def test_a_delivered_band_alert_starts_the_reminder_clock_without_a_reminder(pol):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    run(policy.run_once())
    assert len(pol["alerts"]) == 2 and policy._load_state()["lastReminder"] == T0


def test_no_session_means_no_pass_at_all(pol):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    pol["session"] = False
    res = run(policy.run_once())
    assert res["idle"] is True and pol["alerts"] == []
    assert not policy.STATE_FILE.exists()


def test_session_gate_is_xetr_or_xams(monkeypatch):
    seen = []
    monkeypatch.setattr(policy.calendars, "is_session",
                        lambda mic, day: seen.append(mic) or mic == "XAMS")
    assert policy._session_today(TODAY) is True and seen == ["XETR", "XAMS"]
    monkeypatch.setattr(policy.calendars, "is_session", lambda mic, day: False)
    assert policy._session_today(TODAY) is False


def test_unconfigured_or_unpriced_portfolio_is_an_idle_pass(pol):
    pol["rows"] = band_rows(6000.0, 4000.0)
    assert run(policy.run_once())["idle"] is True                    # no investmentPolicy
    pol["cfg"], pol["rows"] = BAND_CFG, []
    assert run(policy.run_once())["idle"] is True                    # nothing priced
    assert pol["alerts"] == []


def test_a_target_removed_from_the_policy_drops_its_state(pol):
    pol["cfg"], pol["rows"] = BAND_CFG, band_rows(6000.0, 4000.0)
    run(policy.run_once())
    pol["cfg"] = {"investmentPolicy": {"targets": {"SXR8": 100}}}
    run(policy.run_once())
    assert "ASML" not in policy._load_state()["band"]


def test_corrupt_state_degrades_to_a_clean_baseline(pol):
    policy.STATE_FILE.write_text("{not json")
    assert policy._load_state() == {"version": 1, "band": {}, "single": [],
                                    "trendAlerted": {}, "lastReminder": None,
                                    "lastPass": None}


def test_status_reports_the_daily_interval_and_the_env_gate(monkeypatch):
    monkeypatch.delenv("POLICY_WATCH_ENABLED", raising=False)
    assert policy.status()["interval_s"] == 86400 and policy.status()["enabled"] is True
    monkeypatch.setenv("POLICY_WATCH_ENABLED", "0")
    assert policy.status()["enabled"] is False
    policy.start()                                  # disabled: no task is created
    assert policy._task is None
