"""paper_risk: pure order planning + trading window (contract §2)."""
from datetime import datetime, timezone

import pytest

from app.api import paper_risk as pr

NOW = 1_800_000_000.0
IDS = ("A", "B", "E")
UNIVERSE = {i: {"t212": f"{i}_EQ"} for i in IDS}
KINDS = {"A": "equity", "B": "equity", "E": "etf"}
LIMITS = {"maxWeightPct": {"equity": 40, "etf": 100}, "minCashPct": 2,
          "maxTurnoverPctPerDay": 100, "maxOrdersPerDay": 6, "minTradeEur": 25,
          "rebalanceBandPct": 3, "maxSpreadOrStaleS": 900, "limitSlippageBps": 30,
          "adjustFraction": 1.0}


def quote(price=100.0, age=10, stale=False):
    return {"price": price, "asOf": int(NOW - age) if age is not None else None, "stale": stale}


def run(**over):
    kw = dict(targets={}, cash_pct=None, holdings={}, cash_eur=1000.0,
              quotes={i: quote() for i in IDS}, universe=UNIVERSE, kinds=KINDS,
              limits=dict(LIMITS), budget_eur=1000.0, decimals={}, now=NOW,
              session_ok={i: True for i in IDS}, orders_today=0, turnover_today_eur=0.0)
    lim = over.pop("limits", {})
    kw.update(over)
    kw["limits"] = {**LIMITS, **lim}
    return pr.plan_orders(**kw)


def reasons(res):
    return {s["id"]: s["reason"] for s in res["skipped"]}


def by_id(res):
    return {i.id: i for i in res["intents"]}


# ---- weights -------------------------------------------------------------

def test_clip_per_kind_and_cash_remainder():
    res = run(targets={"A": 60, "E": 30})
    assert res["targetsApplied"] == {"A": 40, "E": 30}  # equity clipped, etf cap 100 not hit


def test_etf_not_clipped_but_min_cash_forces_renormalisation():
    res = run(targets={"E": 100})
    assert res["targetsApplied"]["E"] == pytest.approx(98.0)
    assert sum(res["targetsApplied"].values()) + 2 == pytest.approx(100)


def test_renormalise_scales_proportionally_to_fit_cash():
    res = run(targets={"A": 40, "B": 40, "E": 60}, cash_pct=None)
    t = res["targetsApplied"]
    assert sum(t.values()) == pytest.approx(98.0)
    assert t["A"] / t["E"] == pytest.approx(40 / 60)


def test_explicit_cash_pct_respected_and_floored():
    assert run(targets={"E": 100}, cash_pct=10)["targetsApplied"]["E"] == pytest.approx(90)
    assert run(targets={"E": 100}, cash_pct=0)["targetsApplied"]["E"] == pytest.approx(98)


def test_not_in_universe_skipped():
    res = run(targets={"ZZ": 10, "E": 50})
    assert reasons(res)["ZZ"] == "not_in_universe"
    assert "ZZ" not in res["targetsApplied"]


def test_exempt_skips_max_weight_only_not_min_cash():
    assert run(targets={"A": 60}, exempt_caps=True)["targetsApplied"]["A"] == 60
    assert run(targets={"E": 100}, exempt_caps=True)["targetsApplied"]["E"] == pytest.approx(98)


# ---- skips ---------------------------------------------------------------

def test_band_and_min_trade_skips():
    h = {"E": 5.0}
    # actual 50% of 1000
    assert reasons(run(targets={"E": 52}, holdings=h, cash_eur=500))["E"] == "within_band"
    assert by_id(run(targets={"E": 55}, holdings=h, cash_eur=500))["E"].side == "buy"
    res = run(targets={"E": 3}, limits={"rebalanceBandPct": 0})  # 30 EUR -> ok
    assert "E" in by_id(res)
    res = run(targets={"E": 2}, limits={"rebalanceBandPct": 0})  # 20 EUR < 25
    assert reasons(res)["E"] == "below_min_trade"


def test_exempt_bypasses_band_but_not_min_trade():
    res = run(targets={"E": 3}, exempt_caps=True)  # |3-0| <= band 3 but exempt
    assert "E" in by_id(res)
    res = run(targets={"E": 2}, exempt_caps=True)
    assert reasons(res)["E"] == "below_min_trade"


@pytest.mark.parametrize("q,reason", [
    (None, "no_quote"),
    ({"price": None, "asOf": NOW, "stale": False}, "no_quote"),
    (quote(stale=True), "stale_quote"),
    (quote(age=901), "quote_too_old"),
    (quote(age=None), "quote_age_unknown"),
])
def test_bad_quotes_skip(q, reason):
    quotes = {i: quote() for i in IDS}
    if q is None:
        del quotes["E"]
    else:
        quotes["E"] = q
    for exempt in (False, True):  # staleness is never exempt
        res = run(targets={"E": 50}, quotes=quotes, exempt_caps=exempt)
        assert reasons(res)["E"] == reason and not res["intents"]


def test_quote_age_boundary_inclusive():
    assert "E" in by_id(run(targets={"E": 50}, quotes={"E": quote(age=900)}))


def test_session_closed_skips_even_when_exempt():
    for sess in ({"E": False}, {}):
        res = run(targets={"E": 50}, session_ok=sess, exempt_caps=True)
        assert reasons(res)["E"] == "session_closed" and not res["intents"]


# ---- sizing & ordering ---------------------------------------------------

def test_intent_fields():
    i = by_id(run(targets={"E": 50}))["E"]
    assert (i.ticker, i.side, i.quantity, i.ref_price) == ("E_EQ", "buy", 5.0, 100.0)
    assert i.ref_price_as_of == int(NOW - 10)
    assert i.value_eur == pytest.approx(500.0)
    assert i.reason == "rebalance 0.0%->50.0%"


def test_sells_before_buys_and_sell_proceeds_fund_buys():
    res = run(targets={"A": 20, "E": 50}, holdings={"A": 8.0}, cash_eur=200.0,
              limits={"maxTurnoverPctPerDay": 200})
    assert [(i.id, i.side) for i in res["intents"]] == [("A", "sell"), ("E", "buy")]
    assert res["intents"][0].quantity == pytest.approx(6.0)
    assert res["intents"][1].quantity == pytest.approx(5.0)  # 200 cash alone would give 1


def test_buys_limited_by_cash():
    res = run(targets={"E": 50}, holdings={"A": 7.0}, cash_eur=300.0, budget_eur=5000)
    i = by_id(res)["E"]
    assert i.quantity == pytest.approx(2.99)  # 300 / limit 100.30
    assert i.quantity * i.limit_price <= 300.0
    assert "buy_reduced:E" in res["warnings"]


def test_no_cash_no_buy():
    res = run(targets={"E": 50}, holdings={"A": 10.0}, cash_eur=0.0, budget_eur=5000)
    assert reasons(res)["E"] == "insufficient_cash" and not res["intents"]


def test_never_oversell():
    res = run(targets={"A": 0}, holdings={"A": 1.5}, decimals={"A": 1})
    assert by_id(res)["A"].quantity == 1.5
    res = run(targets={"A": 0}, holdings={"A": 2.567}, decimals={"A": 2})
    assert by_id(res)["A"].quantity == 2.56  # rounded down, never above held


def test_sell_not_larger_than_value_gap():
    res = run(targets={"A": 30}, holdings={"A": 4.0}, cash_eur=600.0)  # 40% -> 30%: 100 EUR
    assert by_id(res)["A"].quantity == pytest.approx(1.0)


def test_quantity_rounds_down_and_zero_dropped():
    res = run(targets={"E": 59}, decimals={"E": 0})
    assert by_id(res)["E"].quantity == 5.0
    res = run(targets={"E": 30}, quotes={"E": quote(price=1000.0)}, decimals={"E": 0})
    assert reasons(res)["E"] == "zero_quantity" and not res["intents"]
    res = run(targets={"A": 0}, holdings={"A": 0.5}, decimals={"A": 0}, cash_eur=950)
    assert reasons(res)["A"] == "zero_quantity"


@pytest.mark.parametrize("price,buy,sell", [
    (100.0, 100.30, 99.70),         # exact, no float-noise round-up
    (10.003, 10.04, 9.97),          # buy up, sell down
])
def test_limit_price_rounding(price, buy, sell):
    q = {i: quote(price=price) for i in IDS}
    b = by_id(run(targets={"E": 50}, quotes=q, cash_eur=10000, budget_eur=10000))["E"]
    assert b.limit_price == buy
    s = by_id(run(targets={"A": 0}, quotes=q, holdings={"A": 100.0}, cash_eur=0, budget_eur=10000))["A"]
    assert s.limit_price == sell


def test_deterministic_order():
    kw = dict(targets={"B": 10, "A": 10, "E": 10}, holdings={"A": 8.0, "B": 8.0, "E": 8.0},
              cash_eur=0.0, budget_eur=5000)
    kw["targets"] = {"E": 40, "B": 10, "A": 10}
    res = run(**kw)
    assert [(i.id, i.side) for i in res["intents"]] == [("A", "sell"), ("B", "sell"), ("E", "buy")]
    assert run(**kw) == res
    rev = run(**{**kw, "targets": dict(reversed(list(kw["targets"].items())))})
    assert rev["intents"] == res["intents"]


# ---- caps ----------------------------------------------------------------

CAP2 = {"A": 25, "B": 25}


def test_turnover_cap_and_carry_over():
    lim = {"maxTurnoverPctPerDay": 30}  # 300 EUR on 1000
    res = run(targets=CAP2, limits=lim)
    assert [i.id for i in res["intents"]] == ["A"] and reasons(res)["B"] == "turnover_cap"
    res = run(targets=CAP2, limits=lim, turnover_today_eur=100.0)  # 200 left < 250
    assert not res["intents"] and set(reasons(res).values()) == {"turnover_cap"}
    res = run(targets=CAP2, limits=lim, turnover_today_eur=500.0)  # already over
    assert not res["intents"]


def test_sells_consume_turnover_before_buys():
    res = run(targets={"A": 0, "B": 25}, holdings={"A": 3.0}, cash_eur=700.0,
              limits={"maxTurnoverPctPerDay": 40})  # 400: sell 300 then buy 250 > 100 left
    assert [i.side for i in res["intents"]] == ["sell"] and reasons(res)["B"] == "turnover_cap"


def test_order_count_cap_and_carry_over():
    res = run(targets=CAP2, limits={"maxOrdersPerDay": 2}, orders_today=1)
    assert [i.id for i in res["intents"]] == ["A"] and reasons(res)["B"] == "order_cap"
    res = run(targets=CAP2, orders_today=6)
    assert not res["intents"] and set(reasons(res).values()) == {"order_cap"}


def test_exempt_bypasses_turnover_and_order_caps():
    res = run(targets=CAP2, limits={"maxTurnoverPctPerDay": 1, "maxOrdersPerDay": 1},
              orders_today=5, turnover_today_eur=999.0, exempt_caps=True)
    assert [i.id for i in res["intents"]] == ["A", "B"]


def test_budget_cap_applies_even_when_exempt():
    # positions 400 + cash 1000, budget 500 -> only 100 EUR of room
    for exempt in (False, True):
        res = run(targets={"E": 50}, holdings={"A": 4.0}, cash_eur=1000.0, budget_eur=500.0,
                  exempt_caps=exempt)
        assert by_id(res)["E"].quantity == pytest.approx(1.0)
    res = run(targets={"E": 50}, holdings={"A": 4.0}, cash_eur=1000.0, budget_eur=400.0)
    assert reasons(res)["E"] == "budget_cap"


def test_sells_free_budget_room():
    res = run(targets={"A": 0, "E": 50}, holdings={"A": 4.0}, cash_eur=1000.0, budget_eur=400.0)
    assert by_id(res)["E"].quantity == pytest.approx(4.0)  # 400 room after selling 400


def test_cash_not_exempt():
    res = run(targets={"E": 98}, cash_eur=300.0, exempt_caps=True, budget_eur=5000)
    i = by_id(res)["E"]
    assert i.quantity * i.limit_price <= 300.0


def test_seed_style_plan_passes_default_caps_when_exempt():
    lim = {"maxTurnoverPctPerDay": 30, "maxOrdersPerDay": 1}
    res = run(targets={"A": 40, "E": 58}, limits=lim, exempt_caps=True)
    assert len(res["intents"]) == 2
    assert len(run(targets={"A": 40, "E": 58}, limits=lim)["intents"]) < 2


def test_no_equity():
    res = run(targets={"E": 50}, cash_eur=0.0)
    assert not res["intents"] and reasons(res)["E"] == "no_equity"


# ---- trading window ------------------------------------------------------

def _dt(h, m, s=0):
    return datetime(2026, 10, 19, h, m, s, tzinfo=timezone.utc)


def test_window_boundaries():
    o, c = _dt(9, 0), _dt(17, 30)
    assert pr.OPEN_DELAY_MIN == 30 and pr.CUTOFF_BEFORE_CLOSE_MIN == 30
    assert not pr.trading_window(o, c, _dt(9, 29, 59))
    assert pr.trading_window(o, c, _dt(9, 30))
    assert pr.trading_window(o, c, _dt(16, 59, 59))
    assert not pr.trading_window(o, c, _dt(17, 0))
    assert not pr.trading_window(o, c, _dt(8, 0))
    assert not pr.trading_window(o, c, _dt(18, 0))


def test_window_early_close():
    o, c = _dt(9, 0), _dt(13, 0)
    assert pr.trading_window(o, c, _dt(12, 29, 59))
    assert not pr.trading_window(o, c, _dt(12, 30))


# ---- open-universe limits (phase 6): maxPositions / maxSectorPct / minPositionEur ----

NAMES = ("A", "B", "C", "D", "E1", "F")
OPEN_UNIVERSE = {"A": {"t212": "A_EQ", "sector": "Tech"}, "B": {"t212": "B_EQ", "sector": "tech"},
                 "C": {"t212": "C_EQ", "sector": "Banks"}, "D": {"t212": "D_EQ", "sector": "Banks"},
                 "E1": {"t212": "E1_EQ", "sector": None}, "F": {"t212": "F_EQ"}}
OPEN_KINDS = {k: "equity" for k in NAMES}


def run_open(**over):
    lim = {"maxPositions": 3, "maxSectorPct": 40, "minPositionEur": 50,
           "maxWeightPct": {"equity": 40, "etf": 100}}
    lim.update(over.pop("limits", {}))
    base = dict(universe=OPEN_UNIVERSE, kinds=OPEN_KINDS,
                quotes={i: quote() for i in NAMES}, session_ok={i: True for i in NAMES},
                limits=lim)
    base.update(over)
    return run(**base)


def test_without_the_open_keys_nothing_changes():
    res = run(targets={"A": 35, "B": 35, "E": 28})       # no sector/min/maxPositions in LIMITS
    assert res["targetsApplied"] == {"A": 35, "B": 35, "E": 28}


def test_max_positions_drops_the_lowest_weighted_new_names():
    res = run_open(targets={"A": 30, "B": 10, "C": 25, "D": 15, "E1": 20}, limits={"maxSectorPct": 100})
    applied = res["targetsApplied"]
    assert {k for k, v in applied.items() if v > 0} == {"A", "C", "E1"}
    assert reasons(res)["B"] == "max_positions" and reasons(res)["D"] == "max_positions"


def test_max_positions_never_drops_a_held_name_and_counts_untouched_holdings():
    # F is held but not in the targets (no instruction, no trade): it uses one slot
    res = run_open(targets={"A": 30, "B": 25, "C": 20, "D": 15}, holdings={"F": 1.0},
                   cash_eur=900.0, limits={"maxSectorPct": 100})
    assert {k for k, v in res["targetsApplied"].items() if v > 0} == {"A", "B"}      # 1 held + 2 new = 3
    held_low = run_open(targets={"A": 30, "B": 25, "C": 20, "D": 6}, holdings={"D": 0.6},
                        cash_eur=940.0, limits={"maxSectorPct": 100, "minPositionEur": 0})
    assert held_low["targetsApplied"]["D"] == 6           # a held name keeps its target ...
    assert reasons(held_low).get("C") == "max_positions"  # ... a new, lower-weighted one goes


def test_sector_cap_scales_one_sector_down_proportionally():
    res = run_open(targets={"A": 30, "B": 30, "C": 20}, limits={"maxPositions": 10})   # Tech 60 > 40
    t = res["targetsApplied"]
    assert t["A"] + t["B"] == pytest.approx(40) and t["A"] == pytest.approx(t["B"])
    assert t["C"] == pytest.approx(20)                    # other sectors untouched
    assert "sector_capped:tech" in res["warnings"]


def test_names_without_a_sector_are_not_capped():
    res = run_open(targets={"E1": 35, "F": 35}, limits={"maxPositions": 10, "maxSectorPct": 10})
    assert res["targetsApplied"] == {"E1": 35, "F": 35}


def test_min_position_zeroes_a_target_worth_less_than_the_minimum():
    res = run_open(targets={"A": 30, "C": 4}, limits={"maxPositions": 10})     # 4 % of 1000 = EUR 40 < 50
    assert "C" not in res["targetsApplied"] and reasons(res)["C"] == "below_min_position"
    assert [i.id for i in res["intents"]] == ["A"]


def test_min_position_sells_out_a_held_name_that_shrinks_below_it():
    res = run_open(targets={"A": 30, "C": 4}, holdings={"C": 1.0}, cash_eur=900.0,
                   limits={"maxPositions": 10})
    assert by_id(res)["C"].side == "sell" and by_id(res)["C"].quantity == 1.0


def test_exempt_caps_skips_the_open_limits_entirely():
    res = run_open(targets={"A": 30, "B": 30, "C": 4, "D": 4}, exempt_caps=True,
                   limits={"maxWeightPct": {"equity": 100, "etf": 100}})
    assert set(res["targetsApplied"]) == {"A", "B", "C", "D"}
    assert all(v > 0 for v in res["targetsApplied"].values())


# ---- partial adjustment (adjustFraction) -----------------------------------

def pa(**kw):
    lim = {"adjustFraction": 0.15, **kw.pop("limits", {})}
    return run(limits=lim, **kw)


def test_partial_adjustment_converges_gradually_over_repeated_calls():
    qty, cash, seen = 0.0, 1000.0, []
    for _ in range(15):
        res = pa(targets={"A": 40}, cash_pct=None, holdings={"A": qty}, cash_eur=cash)
        seen.append(qty * 100 / 10)
        for i in res["intents"]:
            qty += i.quantity
            cash -= i.value_eur
    assert seen[1] == pytest.approx(6.0) and seen[1] < 40
    assert all(b >= a for a, b in zip(seen, seen[1:]))
    assert 37 <= seen[-1] <= 40 and res["targetsApplied"] == {"A": 40}


def test_minimum_step_prevents_paralysis_in_a_small_bucket():
    res = pa(targets={"A": 40}, holdings={"A": 3.6}, cash_eur=640.0)     # diff 4 > band; 15 % of it is EUR 6
    i = by_id(res)["A"]
    assert i.side == "buy" and i.value_eur == pytest.approx(25.0)
    assert res["targetsEffective"]["A"] == pytest.approx(38.5)


def test_a_new_position_starts_at_least_at_min_position():
    res = pa(targets={"A": 10}, limits={"minPositionEur": 50, "adjustFraction": 0.1})
    assert by_id(res)["A"].value_eur >= 50 - 1e-6
    assert res["targetsEffective"]["A"] == pytest.approx(5.0)


def test_the_band_applies_to_the_original_difference():
    res = pa(targets={"A": 40}, holdings={"A": 3.8}, cash_eur=620.0)     # diff 2 <= band 3
    assert reasons(res) == {"A": "within_band"} and res["intents"] == []
    res = pa(targets={"A": 40}, holdings={"A": 3.65}, cash_eur=635.0)    # diff 3.5 > band: trades despite a tiny 15 %
    assert by_id(res)["A"].value_eur == pytest.approx(25.0)


def test_exit_rule_sells_out_when_the_remainder_would_be_dust():
    lim = {"minPositionEur": 50}
    res = pa(targets={"A": 0}, holdings={"A": 1.0}, cash_eur=900.0, limits=lim)      # 10 % -> 7.5 % = EUR 75
    assert by_id(res)["A"].quantity == pytest.approx(0.25)
    res = pa(targets={"A": 0}, holdings={"A": 0.6}, cash_eur=940.0, limits=lim)      # 6 % -> 3.5 % = EUR 35 < 50
    assert by_id(res)["A"].quantity == 0.6 and res["targetsEffective"]["A"] == 0.0


def test_flat_everything_ignores_the_fraction():
    res = pa(targets={"A": 0}, cash_pct=100, holdings={"A": 1.0}, cash_eur=900.0,
             limits={"minPositionEur": 50})
    assert by_id(res)["A"].quantity == 1.0


def test_seed_exempt_caps_ignores_the_fraction():
    res = pa(targets={"A": 40}, exempt_caps=True)
    assert by_id(res)["A"].value_eur == pytest.approx(400.0)
    assert res["targetsEffective"]["A"] == pytest.approx(40.0)
