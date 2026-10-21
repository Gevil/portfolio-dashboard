import datetime
from zoneinfo import ZoneInfo

import pytest

from app.api import calendars, paper_metrics as pm
from app.api import scoreboard

D0 = datetime.date(2026, 1, 2)
COST0 = {"spreadBps": 0, "commissionEur": 0, "fxBps": 0}


def _dates(n):
    return [(D0 + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _series(rets, start=100.0):
    out, v = [start], start
    for r in rets:
        v *= 1.0 + r
        out.append(v)
    return out


def _build(eq_rets, bh_rets):
    ds = _dates(len(eq_rets) + 1)
    return dict(zip(ds, _series(eq_rets))), dict(zip(ds, _series(bh_rets, 1.0)))


def _epoch(y, m, d, hh=12):
    return datetime.datetime(y, m, d, hh, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


# ---- bucket_metrics ---------------------------------------------------------

def test_hand_computed_metrics_and_costs():
    # equity returns +10%, -10%, +10%; buy&hold returns +5%, 0, +5%
    equity = {"2026-01-02": 100.0, "2026-01-05": 110.0, "2026-01-06": 99.0, "2026-01-07": 108.9}
    bh = {"2026-01-02": 1.0, "2026-01-05": 1.05, "2026-01-06": 1.05, "2026-01-07": 1.1025}
    fills = [{"date": "2026-01-02", "filledValueEur": 50.0},
             {"updatedAt": _epoch(2026, 1, 5), "filledValueEur": -50.0},
             {"date": "2026-02-01", "filledValueEur": 999.0}]  # after the window: ignored
    cost = {"spreadBps": 10, "commissionEur": 1.0, "fxBps": 5}
    m = pm.bucket_metrics(equity, bh, fills, cost)
    assert m["sessions"] == 3
    assert m["returnPct"] == pytest.approx(8.9, abs=1e-3)       # 1.1*0.9*1.1
    assert m["buyHoldPct"] == pytest.approx(10.25, abs=1e-3)
    assert m["excessPct"] == pytest.approx(-1.35, abs=1e-3)
    assert m["maxDrawdownPct"] == pytest.approx(10.0, abs=1e-3)  # 110 -> 99
    # sample std 0.11547 * sqrt(252) * 100
    assert m["volatilityPct"] == pytest.approx(183.30, abs=0.01)
    # excess +5%, -10%, +5%: sample std 0.086603 * sqrt(252) * 100
    assert m["trackingErrorPct"] == pytest.approx(137.48, abs=0.01)
    # per fill 50 * (10/2 + 5)/1e4 + 1 = 1.05; two fills; start equity 100
    assert m["costDragPct"] == pytest.approx(2.1, abs=1e-3)
    assert m["netReturnPct"] == pytest.approx(6.8, abs=1e-3)
    assert m["netExcessPct"] == pytest.approx(-3.45, abs=1e-3)
    assert m["turnoverPct"] == pytest.approx(100 / 104.475 * 100, abs=1e-3)
    assert m["informationRatio"] is None
    assert m["excessCI95"] is None and m["verdict"] == "insufficient_sample"


def test_costs_are_zero_without_fills_and_half_spread_only():
    eq, bh = _build([0.01] * 3, [0.0] * 3)
    assert pm.bucket_metrics(eq, bh, [], {})["costDragPct"] == 0.0
    start = next(iter(eq.values()))
    fills = [{"date": _dates(1)[0], "filledValueEur": 10000.0}]
    m = pm.bucket_metrics(eq, bh, fills, {"spreadBps": 20})
    assert m["costDragPct"] == pytest.approx(10000 * 10 / 1e4 / start * 100, abs=1e-3)  # 1.0 EUR... of 100


def test_default_cost_applies_when_keys_missing():
    assert pm.fill_cost(1000.0, {}) == pytest.approx(0.5)       # 10 bps spread -> 5 bps/side
    assert pm.fill_cost(1000.0, {"spreadBps": 0, "commissionEur": 2, "fxBps": 10}) == pytest.approx(3.0)


def test_information_ratio_needs_20_sessions():
    eq_rets = [0.01, 0.0] * 10
    eq, bh = _build(eq_rets[:19], [0.0] * 19)
    assert pm.bucket_metrics(eq, bh, [], COST0)["informationRatio"] is None
    eq, bh = _build(eq_rets, [0.0] * 20)
    m = pm.bucket_metrics(eq, bh, [], COST0)
    assert m["sessions"] == 20
    # mean 0.005, sample std sqrt(20 * 0.005^2 / 19): IR = 0.005/std*sqrt(252)
    assert m["informationRatio"] == pytest.approx(15.47, rel=1e-3)


def test_verdict_ahead_behind_inconclusive():
    ahead = pm.bucket_metrics(*_build([0.01, 0.0] * 10, [0.0] * 20), [], COST0)
    assert ahead["verdict"] == "ahead" and ahead["excessCI95"][0] > 0
    behind = pm.bucket_metrics(*_build([-0.01, 0.0] * 10, [0.0] * 20), [], COST0)
    assert behind["verdict"] == "behind" and behind["excessCI95"][1] < 0
    mixed = pm.bucket_metrics(*_build([0.01, -0.01] * 10, [0.0] * 20), [], COST0)
    lo, hi = mixed["excessCI95"]
    assert lo < 0 < hi and mixed["verdict"] == "inconclusive"


def test_verdict_insufficient_below_20_sessions_even_if_ahead():
    m = pm.bucket_metrics(*_build([0.01] * 19, [0.0] * 19), [], COST0)
    assert m["sessions"] == 19 and m["verdict"] == "insufficient_sample" and m["excessCI95"] is None


def test_too_few_dates_returns_empty_metrics():
    m = pm.bucket_metrics({"2026-01-02": 100.0}, {"2026-01-02": 1.0}, [], COST0)
    assert m["sessions"] == 0 and m["returnPct"] is None and m["verdict"] == "insufficient_sample"


def test_only_common_dates_are_used():
    eq, bh = _build([0.01] * 3, [0.0] * 3)
    eq["2025-12-01"] = 50.0  # no buy&hold value: ignored
    assert pm.bucket_metrics(eq, bh, [], COST0)["sessions"] == 3


def test_flows_are_removed_from_returns():
    eq = {"2026-01-02": 100.0, "2026-01-05": 160.0}
    bh = {"2026-01-02": 1.0, "2026-01-05": 1.0}
    m = pm.bucket_metrics(eq, bh, [], COST0, flows={"2026-01-05": 50.0})
    assert m["returnPct"] == pytest.approx(10.0, abs=1e-3)


# ---- block_bootstrap_ci ------------------------------------------------------

def test_ci_hand_computed_constant_series():
    # every resample sums 20 x 1% = 20 pp
    assert pm.block_bootstrap_ci([0.01] * 20) == pytest.approx((20.0, 20.0))


def test_ci_deterministic_with_seed_and_none_below_20():
    xs = [0.01, -0.02, 0.005, 0.0, 0.015, 0.03, -0.01, 0.002, 0.007, -0.004,
          0.012, -0.015, 0.0, 0.02, -0.006, 0.001, 0.009, -0.003, 0.011, -0.008,
          0.004, 0.016, -0.012, 0.0, 0.006]
    a = pm.block_bootstrap_ci(xs, seed=7)
    assert a == pm.block_bootstrap_ci(xs, seed=7)
    assert a != pm.block_bootstrap_ci(xs, seed=8)
    assert a[0] <= a[1]
    assert pm.block_bootstrap_ci(xs[:19]) is None


# ---- version_segments --------------------------------------------------------

def test_version_segments():
    decisions = [{"strategyVersion": "v1", "date": "2026-03-10"},
                 {"strategyVersion": "v1", "date": "2026-03-03"},
                 {"strategyVersion": "v2", "date": "2026-03-17"}]
    orders = [{"strategyVersion": "v1", "createdAt": _epoch(2026, 3, 4)},
              {"strategyVersion": "v2", "createdAt": _epoch(2026, 3, 16)}]
    assert pm.version_segments(decisions, orders, "2026-03-02") == [
        {"strategyVersion": "v1", "from": "2026-03-02", "to": "2026-03-16"},
        {"strategyVersion": "v2", "from": "2026-03-16", "to": None}]


def test_version_segments_empty_and_single():
    assert pm.version_segments([], [], "2026-03-02") == []
    assert pm.version_segments([{"strategyVersion": "v1", "date": "2026-03-05"}], [], "2026-03-02") == [
        {"strategyVersion": "v1", "from": "2026-03-02", "to": None}]


# ---- grade_decisions ---------------------------------------------------------

BASE3 = {"A": 40.0, "B": 30.0, "C": 30.0}


def _dec(date, targets=None, **kw):
    row = {"decisionId": f"{date}-v1-1", "date": date, "createdAt": 1.0, "strategyVersion": "v1",
           "valid": True, "deferred": None, "noChange": targets is None,
           "output": {"targets": targets or {}}}
    row.update(kw)
    return row


def test_outcome_uses_active_weights_and_pending_rows():
    closes = {"A": [("2026-03-02", 100.0), ("2026-03-09", 105.0)],
              "B": [("2026-03-02", 100.0), ("2026-03-09", 100.0)],
              "C": [("2026-03-02", 100.0), ("2026-03-09", 98.0)]}
    res = pm.grade_decisions([_dec("2026-03-02", {"A": 50.0, "B": 30.0, "C": 20.0})], closes,
                             baseline=BASE3)
    row = res["rows"][0]
    assert row["active"] == {"A": 10.0, "B": 0.0, "C": -10.0}
    h5 = row["horizons"]["5"]
    # 10% x +5%  +  (-10%) x (-2%) = 0.7 pp
    assert h5["status"] == "graded" and h5["outcomePct"] == pytest.approx(0.7) and h5["hit"] is True
    assert row["horizons"]["20"] == {"status": "pending", "maturesOn": "2026-03-30"}
    cell5, cell20 = res["horizons"]["5"], res["horizons"]["20"]
    assert (cell5["n"], cell5["hits"], cell5["pending"]) == (1, 1, 0)
    assert cell5["meanOutcomePct"] == pytest.approx(0.7)
    assert (cell20["n"], cell20["pending"], cell20["hitRatePct"]) == (0, 1, None)
    assert cell20["wilson95"] is None


def test_ic_sign_follows_active_weight_vs_return():
    closes = {"A": [("2026-03-02", 100.0), ("2026-03-09", 103.0)],
              "B": [("2026-03-02", 100.0), ("2026-03-09", 101.0)],
              "C": [("2026-03-02", 100.0), ("2026-03-09", 98.0)]}
    good = pm.grade_decisions([_dec("2026-03-02", {"A": 50.0, "B": 30.0, "C": 20.0})], closes,
                              horizons=(5,), baseline=BASE3)
    bad = pm.grade_decisions([_dec("2026-03-02", {"A": 30.0, "B": 30.0, "C": 40.0})], closes,
                             horizons=(5,), baseline=BASE3)
    assert good["horizons"]["5"]["ic"] == pytest.approx(1.0)
    assert bad["horizons"]["5"]["ic"] == pytest.approx(-1.0)
    assert bad["horizons"]["5"]["hits"] == 0


def test_held_rows_use_weights_in_effect_and_skip_invalid():
    closes = {"A": [("2026-03-02", 100.0), ("2026-03-03", 100.0), ("2026-03-09", 105.0), ("2026-03-10", 110.0)],
              "B": [("2026-03-02", 100.0), ("2026-03-03", 100.0), ("2026-03-09", 100.0), ("2026-03-10", 100.0)],
              "C": [("2026-03-02", 100.0), ("2026-03-03", 100.0), ("2026-03-09", 98.0), ("2026-03-10", 95.0)]}
    decisions = [
        _dec("2026-03-01", None),                                        # held before any targets: flat
        _dec("2026-03-02", {"A": 50.0, "B": 30.0, "C": 20.0}),
        _dec("2026-03-03", None),                                        # held at the same weights
        _dec("2026-03-04", {"A": 0.0, "B": 0.0, "C": 100.0}, valid=False),
        _dec("2026-03-05", {"A": 0.0, "B": 0.0, "C": 100.0}, deferred="lane: down"),
    ]
    res = pm.grade_decisions(decisions, closes, horizons=(5,), baseline=BASE3)
    kinds = [(r["date"], r["kind"], r["horizons"]["5"].get("status")) for r in res["rows"]]
    assert kinds == [("2026-03-01", "held", "flat"), ("2026-03-02", "targets", "graded"),
                     ("2026-03-03", "held", "graded")]
    # held row: T = 2026-03-03, exit 2026-03-10 -> 10% x 10 + (-10%) x (-5) = 1.5
    assert res["rows"][2]["horizons"]["5"]["outcomePct"] == pytest.approx(1.5)
    assert res["horizons"]["5"]["n"] == 2  # the flat row is not counted


def test_missing_closes_make_row_unpriced_with_warning():
    closes = {"A": [("2026-03-02", 100.0), ("2026-03-09", 105.0)]}
    res = pm.grade_decisions([_dec("2026-03-02", {"A": 50.0, "B": 30.0, "C": 20.0})], closes,
                             horizons=(5,), baseline=BASE3)
    assert res["rows"][0]["horizons"]["5"]["status"] == "unpriced"
    assert res["horizons"]["5"]["n"] == 0 and res["horizons"]["5"]["unpriced"] == 1
    assert any("no closes for C" in w for w in res["warnings"])


def _flip_flop(n):
    """n decisions on consecutive sessions alternating A up / A down; A rises 1 %/session, B flat."""
    sessions = calendars.sessions_between("XETR", datetime.date(2026, 3, 2), datetime.date(2026, 7, 31))
    a = [(d.isoformat(), 100.0 * 1.01 ** i) for i, d in enumerate(sessions)]
    b = [(d.isoformat(), 100.0) for d in sessions]
    decisions = []
    for i in range(n):
        tg = {"A": 60.0, "B": 40.0} if i % 2 == 0 else {"A": 40.0, "B": 60.0}
        decisions.append(_dec(sessions[i].isoformat(), tg))
    return decisions, {"A": a, "B": b}


def test_wilson_interval_and_insufficient_flag_below_30():
    decisions, closes = _flip_flop(10)
    res = pm.grade_decisions(decisions, closes, horizons=(5,), baseline={"A": 50.0, "B": 50.0})
    cell = res["horizons"]["5"]
    assert (cell["n"], cell["hits"], cell["hitRatePct"]) == (10, 5, 50.0)
    # Wilson 95% for 5/10 (percent)
    assert cell["wilson95"] == pytest.approx([23.7, 76.3], abs=0.05)  # scoreboard rounds to 0.1
    assert cell["wilson95"] == [round(x, 4) for x in scoreboard.wilson(5, 10)]
    assert cell["insufficient_sample"] is True and cell["minSamples"] == 30
    assert cell["meanOutcomePct"] == pytest.approx(0.0, abs=0.2)  # up/down alternation cancels roughly


def test_insufficient_flag_clears_at_30_graded():
    decisions, closes = _flip_flop(30)
    cell = pm.grade_decisions(decisions, closes, horizons=(5,),
                              baseline={"A": 50.0, "B": 50.0})["horizons"]["5"]
    assert cell["n"] == 30 and cell["insufficient_sample"] is False
    one_short = pm.grade_decisions(decisions[:29], closes, horizons=(5,),
                                   baseline={"A": 50.0, "B": 50.0})["horizons"]["5"]
    assert one_short["insufficient_sample"] is True


# ---- phase 5 §7: sharpe, sortino, beta, decision counts ---------------------

def _ratios(eq_rets, bh_rets):
    eq, bh = _build(eq_rets, bh_rets)
    return pm.bucket_metrics(eq, bh, [], COST0)


def test_sharpe_sortino_and_beta_match_a_direct_calculation():
    import statistics
    eq_rets = [0.02, -0.01] * 10                    # 20 sessions
    bh_rets = [0.01, -0.005] * 10                   # the bucket moves exactly twice as much
    m = _ratios(eq_rets, bh_rets)
    assert m["sessions"] == 20
    root = 252 ** 0.5
    mean, std = statistics.mean(eq_rets), statistics.stdev(eq_rets)
    downside = (sum(min(r, 0.0) ** 2 for r in eq_rets) / len(eq_rets)) ** 0.5
    assert m["sharpe"] == pytest.approx(mean / std * root, abs=1e-3)
    assert m["sortino"] == pytest.approx(mean / downside * root, abs=1e-3)
    assert m["betaToBuyHold"] == pytest.approx(2.0, abs=1e-4)
    assert m["sharpe"] == pytest.approx(5.158, abs=0.01) and m["sortino"] == pytest.approx(11.225, abs=0.01)


def test_beta_is_covariance_over_variance_of_the_daily_returns():
    import statistics
    eq_rets = [0.011, -0.004, 0.007, 0.002, -0.009] * 4
    bh_rets = [0.006, -0.002, 0.009, -0.001, -0.004] * 4
    m = _ratios(eq_rets, bh_rets)
    ma, mb = statistics.mean(eq_rets), statistics.mean(bh_rets)
    cov = sum((a - ma) * (b - mb) for a, b in zip(eq_rets, bh_rets)) / (len(eq_rets) - 1)
    assert m["betaToBuyHold"] == pytest.approx(cov / statistics.variance(bh_rets), abs=1e-3)


@pytest.mark.parametrize("n,defined", [(1, False), (5, False), (19, False), (20, True)])
def test_the_ratios_need_twenty_sessions(n, defined):
    m = _ratios(([0.01, -0.005] * 10)[:n], ([0.004, -0.002] * 10)[:n])
    assert m["sessions"] == n
    got = (m["sharpe"], m["sortino"], m["betaToBuyHold"])
    assert (None not in got) is defined and (defined or got == (None, None, None))


def test_a_bucket_with_no_down_day_has_no_sortino_and_a_flat_baseline_has_no_beta():
    m = _ratios([0.01, 0.02] * 10, [0.0] * 20)
    assert m["sortino"] is None and m["betaToBuyHold"] is None
    assert m["sharpe"] is not None and m["sharpe"] > 0


def test_a_constant_return_has_no_sharpe_instead_of_float_noise():
    m = _ratios([0.01] * 20, [0.005, -0.005] * 10)
    assert m["sharpe"] is None and m["sortino"] is None


def test_the_empty_result_carries_the_new_keys():
    m = pm.bucket_metrics({}, {}, [], COST0)
    assert {"sharpe", "sortino", "betaToBuyHold"} <= set(m)
    assert m["sharpe"] is m["sortino"] is m["betaToBuyHold"] is None


def test_a_losing_bucket_has_a_negative_sharpe_and_sortino():
    m = _ratios([-0.01, 0.002] * 10, [0.001] * 20)
    assert m["sharpe"] < 0 and m["sortino"] < 0


def test_decision_counts_split_valid_no_change_invalid_and_deferred():
    rows = [{"valid": True, "noChange": False}, {"valid": True, "noChange": True},
            {"valid": True, "noChange": True}, {"valid": False},
            {"valid": False, "deferred": "budget"}, {"deferred": "lane: lane_down"}, {}]
    assert pm.decision_counts(rows) == {"valid": 1, "noChange": 2, "invalid": 2, "deferred": 2}
    assert pm.decision_counts([]) == {"valid": 0, "noChange": 0, "invalid": 0, "deferred": 0}
