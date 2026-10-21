"""Paper-trading ledger: season/snapshot/order/halt stores + ledger-based bucket
accounting (contract §3.1-3.2).

Positions are plain dicts (Protocol duck-typing: no broker_t212/httpx import
needed to test this module) with the ``broker.Position`` field names.

Run inside the service image (see test_prices.py for the command).
"""
import pathlib

import pytest

from app.api import paper_ledger as pl

UNIVERSE = {"ASML": {"t212": "ASMLa_EQ"}, "NVDA": {"t212": "NVDd_EQ"},
           "SXR8": {"t212": "SXR8d_EQ"}}


def pos(ticker, qty, avg, current=None):
    return {"ticker": ticker, "quantity": qty, "average_price": avg,
            "current_price": current, "ppl": None, "currency": "EUR"}


def quote(price, prev=None, source="yahoo", stale=False, as_of=1_000):
    return {"price": price, "prevClose": prev, "currency": "EUR",
            "asOf": as_of, "source": source, "stale": stale}


def order(pid="ASML", side="buy", qty=1.0, limit=100.0, state="filled", filled=None,
          value=None, avg=None, ref=100.0, intent_id=None):
    """An order-intent row with only the fields the ledger reads."""
    return {"intentId": intent_id or f"2026-10-19-s1-manual-{pid}-{side}-1", "id": pid,
            "ticker": UNIVERSE[pid]["t212"], "side": side, "quantity": qty,
            "limitPrice": limit, "refPrice": ref, "state": state,
            "filledQuantity": qty if (filled is None and state == "filled") else (filled or 0.0),
            "filledValueEur": value, "avgFillPrice": avg}


def bucket(positions, orders, budget=1000.0, prices=None, decimals=None):
    return pl.bucket_from_positions(
        positions=positions, account=None, universe=UNIVERSE, budget_eur=budget,
        prices_by_id=prices or {}, orders=orders, decimals_by_id=decimals)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path: pathlib.Path):
    monkeypatch.setattr(pl, "SEASON_FILE", tmp_path / "paper_season.json")
    monkeypatch.setattr(pl, "SNAPSHOTS_FILE", tmp_path / "paper_snapshots.json")
    monkeypatch.setattr(pl, "ORDERS_FILE", tmp_path / "paper_orders.json")
    monkeypatch.setattr(pl, "HALT_FILE", tmp_path / "paper_halt.json")
    monkeypatch.setattr(pl, "BRIEFS_FILE", tmp_path / "paper_briefs.json")


# ------------------------------------------------- ledger cash (contract §3.2)

def test_cash_is_budget_minus_buys_plus_sells_minus_reserved_open_buys():
    orders = [
        order("ASML", "buy", 1.0, value=300.0),                      # filled buy 300
        order("NVDA", "buy", 2.0, value=200.0),                      # filled buy 200
        order("NVDA", "sell", 1.0, value=120.0),                     # filled sell 120
        order("SXR8", "buy", 4.0, limit=50.0, state="partially_filled",
              filled=1.0, value=50.0),                               # 1 filled, 3 open x 50 reserved
    ]
    cash = pl.ledger_cash(orders, 1000.0)
    assert cash["filledBuyEur"] == 550.0 and cash["filledSellEur"] == 120.0
    assert cash["reservedEur"] == 150.0
    assert cash["cashEur"] == 1000.0 - 550.0 + 120.0 - 150.0


def test_reservation_covers_pending_submitted_and_unknown_buys_but_never_sells():
    orders = [order("ASML", "buy", 2.0, limit=100.0, state=s) for s in ("pending", "submitted", "unknown")]
    orders.append(order("NVDA", "sell", 5.0, limit=90.0, state="submitted"))
    cash = pl.ledger_cash(orders, 1000.0)
    assert cash["reservedEur"] == 600.0       # 3 buys x 2 x 100; the open sell reserves nothing
    assert cash["cashEur"] == 400.0


@pytest.mark.parametrize("state", ["rejected", "not_sent", "shadow"])
def test_rows_that_never_reached_the_market_have_no_effect_on_cash(state):
    assert pl.ledger_cash([order("ASML", "buy", 5.0, state=state)], 1000.0) == {
        "cashEur": 1000.0, "reservedEur": 0.0, "filledBuyEur": 0.0, "filledSellEur": 0.0}


def test_a_cancelled_partial_fill_counts_its_fill_and_releases_the_rest():
    orders = [order("ASML", "buy", 4.0, limit=100.0, state="cancelled", filled=1.5, value=150.0)]
    cash = pl.ledger_cash(orders, 1000.0)
    assert cash["reservedEur"] == 0.0
    assert cash["cashEur"] == 850.0


def test_fill_value_falls_back_to_avg_price_then_ref_price():
    assert pl.fill_value(order(qty=2.0, value=210.0, avg=99.0)) == 210.0
    assert pl.fill_value(order(qty=2.0, value=None, avg=99.0)) == 198.0
    assert pl.fill_value(order(qty=2.0, value=None, avg=None, ref=101.0)) == 202.0
    assert pl.fill_value(order(qty=2.0, state="submitted")) == 0.0


def test_free_cash_never_goes_negative():
    assert pl.ledger_cash([order("ASML", "buy", 10.0, value=1500.0)], 1000.0)["cashEur"] == 0.0


# --------------------------------------------------------- bucket accounting

def test_bucket_equity_is_priced_positions_plus_free_and_reserved_cash():
    orders = [order("ASML", "buy", 1.0, value=300.0),
              order("SXR8", "buy", 4.0, limit=50.0, state="partially_filled", filled=1.0, value=50.0)]
    positions = [pos("ASMLa_EQ", 1.0, 300.0), pos("SXR8d_EQ", 1.0, 50.0)]
    out = bucket(positions, orders, prices={"ASML": quote(330.0, 320.0), "SXR8": quote(52.0)})
    assert out["reservedEur"] == 150.0
    assert out["cashEur"] == 1000.0 - 350.0 - 150.0
    # the reserved cash is still the bucket's money: an open order must not look like a loss
    assert out["equityEur"] == pytest.approx(330.0 + 52.0 + 500.0 + 150.0)
    assert out["bucketCostEur"] == 350.0
    assert out["warnings"] == []
    asml = next(r for r in out["positions"] if r["id"] == "ASML")
    assert asml["valueEur"] == 330.0 and asml["investedEur"] == 300.0
    assert asml["priceSource"] == "yahoo" and asml["stale"] is False


def test_account_totals_are_passed_through_and_none_without_an_account():
    out = pl.bucket_from_positions(positions=[], account={"total": 5000.0, "free_cash": 3700.0},
                                   universe=UNIVERSE, budget_eur=1000.0, prices_by_id={}, orders=[])
    assert out["accountTotalEur"] == 5000.0 and out["accountCashEur"] == 3700.0
    assert out["cashEur"] == 1000.0
    assert bucket([], [])["accountTotalEur"] is None


def test_unmapped_broker_position_is_excluded_and_warned():
    orders = [order("ASML", "buy", 1.0, value=100.0)]
    out = bucket([pos("ASMLa_EQ", 1.0, 100.0), pos("TSLA_EQ", 5.0, 200.0)], orders,
                 prices={"ASML": quote(110.0)})
    assert [r["id"] for r in out["positions"]] == ["ASML"]
    assert out["bucketCostEur"] == 100.0        # the unmapped position never counts
    w = next(w for w in out["warnings"] if w["code"] == "unmapped_broker_position")
    assert w["ids"] == ["TSLA_EQ"]


def test_missing_listing_quote_falls_back_to_broker_current_price():
    orders = [order("ASML", "buy", 2.0, value=1200.0)]
    out = bucket([pos("ASMLa_EQ", 2.0, 600.0, current=700.0)], orders, budget=2000.0,
                 prices={"ASML": None})
    row = out["positions"][0]
    assert row["priceEur"] == 700.0 and row["priceSource"] == "t212"
    assert row["valueEur"] == 1400.0


def test_no_usable_price_leaves_the_position_unvalued():
    orders = [order("ASML", "buy", 2.0, value=1200.0)]
    out = bucket([pos("ASMLa_EQ", 2.0, 600.0, current=None)], orders, budget=2000.0,
                 prices={"ASML": None})
    assert out["positions"][0]["valueEur"] is None
    assert out["cashEur"] == 800.0 and out["equityEur"] == 800.0   # 2000 - 1200, nothing priced


# ------------------------------------------------------------------ warnings

def test_untracked_position_when_no_ledger_fill_explains_it():
    out = bucket([pos("ASMLa_EQ", 2.0, 300.0)], [], prices={"ASML": quote(300.0)})
    w = next(w for w in out["warnings"] if w["code"] == "untracked_position")
    assert w["ids"] == ["ASML"]
    assert not any(w["code"] == "ledger_mismatch" for w in out["warnings"])
    # cash comes from the ledger, so the unexplained position does not reduce it
    assert out["cashEur"] == 1000.0


def test_shadow_and_unfilled_rows_do_not_explain_a_position():
    rows = [order("ASML", "buy", 2.0, state="shadow"), order("ASML", "buy", 2.0, state="rejected")]
    out = bucket([pos("ASMLa_EQ", 2.0, 300.0)], rows, prices={"ASML": quote(300.0)})
    assert [w["code"] for w in out["warnings"]] == ["untracked_position"]


def test_ledger_mismatch_when_broker_quantity_differs_by_more_than_the_step():
    rows = [order("ASML", "buy", 5.0, value=500.0)]
    out = bucket([pos("ASMLa_EQ", 4.0, 100.0)], rows, prices={"ASML": quote(100.0)})
    w = next(w for w in out["warnings"] if w["code"] == "ledger_mismatch")
    assert w["ids"] == ["ASML"]
    assert not any(w["code"] == "untracked_position" for w in out["warnings"])


def test_a_sold_out_position_the_broker_still_holds_is_a_mismatch():
    rows = [order("ASML", "buy", 5.0, value=500.0), order("ASML", "sell", 5.0, value=520.0)]
    out = bucket([pos("ASMLa_EQ", 5.0, 100.0)], rows, prices={"ASML": quote(100.0)})
    assert [w["code"] for w in out["warnings"]] == ["ledger_mismatch"]


def test_ledger_missing_but_broker_flat_is_a_mismatch():
    rows = [order("ASML", "buy", 5.0, value=500.0)]
    out = bucket([], rows)
    assert [w["code"] for w in out["warnings"]] == ["ledger_mismatch"]


@pytest.mark.parametrize("broker_qty,mismatch", [(4.99, False), (5.01, False), (5.0, False),
                                                 (4.98, True), (5.02, True)])
def test_mismatch_tolerance_is_one_instrument_step(broker_qty, mismatch):
    rows = [order("ASML", "buy", 5.0, value=500.0)]
    out = bucket([pos("ASMLa_EQ", broker_qty, 100.0)], rows, prices={"ASML": quote(100.0)},
                 decimals={"ASML": 2})
    assert any(w["code"] == "ledger_mismatch" for w in out["warnings"]) is mismatch


def test_mismatch_step_follows_the_decimals_of_the_instrument():
    rows = [order("ASML", "buy", 5.0, value=500.0)]
    out = bucket([pos("ASMLa_EQ", 4.5, 100.0)], rows, prices={"ASML": quote(100.0)},
                 decimals={"ASML": 0})        # whole shares only: step 1, but 0.5 < 1
    assert out["warnings"] == []
    out = bucket([pos("ASMLa_EQ", 3.0, 100.0)], rows, prices={"ASML": quote(100.0)},
                 decimals={"ASML": 0})
    assert [w["code"] for w in out["warnings"]] == ["ledger_mismatch"]


def test_a_position_that_matches_the_ledger_raises_no_warning():
    rows = [order("ASML", "buy", 5.0, value=500.0), order("ASML", "sell", 2.0, value=210.0)]
    out = bucket([pos("ASMLa_EQ", 3.0, 100.0)], rows, prices={"ASML": quote(100.0)})
    assert out["warnings"] == []


# ------------------------------------------------------- in-flight bookkeeping

def test_open_exposure_nets_remaining_open_buys_and_sells_per_id():
    rows = [order("ASML", "buy", 5.0, state="submitted"),
            order("ASML", "buy", 4.0, state="partially_filled", filled=1.0),   # 3 remaining
            order("NVDA", "sell", 2.0, state="unknown"),
            order("NVDA", "buy", 9.0, state="filled")]                          # terminal: ignored
    assert pl.open_exposure(rows) == {"ASML": 8.0, "NVDA": -2.0}


def test_day_usage_counts_only_orders_that_reached_the_market_today():
    rows = [
        order("ASML", "buy", 2.0, value=200.0, intent_id="2026-10-19-s1-manual-ASML-buy-1"),
        order("NVDA", "buy", 3.0, ref=50.0, state="submitted", intent_id="2026-10-19-s1-manual-NVDA-buy-1"),
        order("SXR8", "buy", 1.0, state="rejected", intent_id="2026-10-19-s1-manual-SXR8-buy-1"),
        order("ASML", "sell", 1.0, state="shadow", intent_id="2026-10-19-s1-manual-ASML-sell-1"),
        order("ASML", "buy", 9.0, value=900.0, intent_id="2026-10-18-s1-manual-ASML-buy-1"),   # yesterday
    ]
    count, turnover = pl.day_usage(rows, "2026-10-19")
    assert count == 2
    assert turnover == 200.0 + 3.0 * 50.0     # fill value + committed qty x ref price while open


def test_next_intent_id_sequences_per_day_prefix():
    rows = []
    first = pl.next_intent_id(rows, "2026-10-19", "s1", "manual", "ASML", "buy")
    assert first == "2026-10-19-daily-s1-manual-ASML-buy-1"
    rows.append({"intentId": first})
    assert pl.next_intent_id(rows, "2026-10-19", "s1", "manual", "ASML", "buy") == \
        "2026-10-19-daily-s1-manual-ASML-buy-2"
    # other side / source / day / id restart at 1
    assert pl.next_intent_id(rows, "2026-10-19", "s1", "manual", "ASML", "sell").endswith("-1")
    assert pl.next_intent_id(rows, "2026-10-19", "s1", "seed", "ASML", "buy").endswith("-1")
    assert pl.next_intent_id(rows, "2026-10-20", "s1", "manual", "ASML", "buy").endswith("-1")


def test_the_bucket_is_part_of_the_intent_id_so_two_buckets_never_collide():
    daily = pl.next_intent_id([], "2026-10-19", "s1", "agent", "ASML", "buy", "daily")
    rows = [{"intentId": daily}]
    intraday = pl.next_intent_id(rows, "2026-10-19", "s1", "agent", "ASML", "buy", "intraday")
    assert intraday == "2026-10-19-intraday-s1-agent-ASML-buy-1" and intraday != daily
    rows.append({"intentId": intraday})
    assert pl.next_intent_id(rows, "2026-10-19", "s1", "agent", "ASML", "buy", "intraday").endswith("-buy-2")
    assert pl.next_intent_id(rows, "2026-10-19", "s1", "agent", "ASML", "buy", "daily").endswith("-buy-2")


def test_open_sell_proceeds_is_remaining_quantity_times_limit_of_open_sells_only():
    rows = [order("NVDA", "sell", 5.0, limit=90.0, state="submitted"),
            order("NVDA", "sell", 4.0, limit=80.0, state="partially_filled", filled=1.0),   # 3 left
            order("NVDA", "sell", 2.0, limit=70.0, state="filled"),                            # terminal
            order("ASML", "buy", 9.0, limit=100.0, state="submitted")]                        # a buy
    assert pl.open_sell_proceeds(rows) == 5.0 * 90.0 + 3.0 * 80.0


# -------------------------------------------------------------------- stores

def test_orders_store_round_trips_and_ignores_garbage():
    assert pl.read_orders() == []
    rows = [order("ASML", "buy", 1.0, state="submitted")]
    assert pl.save_orders(rows) is True
    assert pl.read_orders() == rows
    pl.ORDERS_FILE.write_text('{"not": "a list"}')
    assert pl.read_orders() == []


@pytest.mark.parametrize("state,filled,prunable", [
    ("shadow", 0.0, True), ("rejected", 0.0, True), ("not_sent", 0.0, True),
    ("cancelled", 0.0, True),
    ("cancelled", 0.5, False),                       # a partial fill that was cancelled: cash depends on it
    ("filled", 2.0, False), ("partially_filled", 1.0, False),
    ("submitted", 0.0, False), ("pending", 0.0, False), ("unknown", 0.0, False),
])
def test_only_terminal_rows_without_fills_are_prunable(state, filled, prunable):
    assert pl.is_prunable(order(state=state, filled=filled, qty=2.0)) is prunable


def test_pruning_drops_the_oldest_prunable_rows_and_nothing_else(monkeypatch):
    monkeypatch.setattr(pl, "MAX_ORDER_ROWS", 5)
    rows = [
        order("ASML", "buy", 1.0, state="submitted", intent_id="open"),
        order("ASML", "buy", 1.0, value=100.0, intent_id="filled-old"),
        order("ASML", "buy", 2.0, state="cancelled", filled=0.5, value=50.0, intent_id="cancelled-with-fill"),
        order("ASML", "buy", 1.0, state="shadow", intent_id="shadow-1"),
        order("ASML", "buy", 1.0, state="rejected", intent_id="rejected-1"),
        order("ASML", "buy", 1.0, state="not_sent", intent_id="not-sent-1"),
        order("ASML", "buy", 1.0, state="cancelled", intent_id="cancelled-nofill"),
        order("ASML", "buy", 1.0, value=100.0, intent_id="filled-new"),
    ]
    assert pl.save_orders(rows) is True
    assert [r["intentId"] for r in pl.read_orders()] == [
        "open", "filled-old", "cancelled-with-fill", "cancelled-nofill", "filled-new"]


def test_ledger_cash_and_holdings_are_unchanged_by_pruning(monkeypatch):
    rows = [
        order("ASML", "buy", 4.0, value=400.0, intent_id="a-fill"),
        order("NVDA", "buy", 3.0, value=300.0, intent_id="b-fill"),
        order("NVDA", "sell", 1.0, value=130.0, intent_id="c-fill"),
        order("ASML", "buy", 4.0, limit=90.0, state="cancelled", filled=1.0, value=88.0,
              intent_id="d-cancel-partial"),
        order("SXR8", "buy", 5.0, limit=50.0, state="submitted", intent_id="e-open-buy"),
        order("NVDA", "sell", 2.0, limit=95.0, state="partially_filled", filled=0.5, value=50.0,
              intent_id="f-open-sell"),
    ]
    junk = [order("ASML", "buy", 1.0, state=s, intent_id=f"junk-{i}-{s}")
            for i in range(4) for s in ("shadow", "rejected", "not_sent", "cancelled")]
    all_rows = junk[:8] + rows + junk[8:]          # old junk before, newer junk after

    def accounting(rs):
        return (pl.ledger_cash(rs, 1000.0), pl.ledger_net_quantity(rs), pl.open_exposure(rs),
                pl.open_sell_proceeds(rs),
                pl.ledger_warnings(rs, {"ASML": 5.0, "NVDA": 1.5}, {"ASML": 2, "NVDA": 2}))
    before = accounting(all_rows)

    monkeypatch.setattr(pl, "MAX_ORDER_ROWS", len(rows))     # force pruning down to the protected rows
    assert pl.save_orders(all_rows) is True
    kept = pl.read_orders()
    assert len(kept) == len(rows) and {r["intentId"][0] for r in kept} == {"a", "b", "c", "d", "e", "f"}
    assert accounting(kept) == before


def test_fill_and_open_rows_are_never_dropped_even_when_they_alone_exceed_the_cap(monkeypatch):
    monkeypatch.setattr(pl, "MAX_ORDER_ROWS", 3)
    rows = [order("ASML", "buy", 1.0, value=100.0, intent_id=f"fill-{i}") for i in range(4)]
    rows.append(order("ASML", "buy", 1.0, state="submitted", intent_id="open"))
    rows.append(order("ASML", "buy", 1.0, state="shadow", intent_id="shadow"))
    assert pl.save_orders(rows) is True
    kept = [r["intentId"] for r in pl.read_orders()]
    assert kept == ["fill-0", "fill-1", "fill-2", "fill-3", "open"]     # the cap yields; only 'shadow' went


def test_nothing_is_pruned_below_the_cap(monkeypatch):
    monkeypatch.setattr(pl, "MAX_ORDER_ROWS", 4)
    rows = [order("ASML", "buy", 1.0, state="shadow", intent_id=f"s{i}") for i in range(4)]
    assert pl.save_orders(rows) is True
    assert len(pl.read_orders()) == 4


def test_halt_defaults_when_absent_and_round_trips():
    assert pl.read_halt() == {"halted": False, "reason": None, "at": None, "by": None,
                              "resumedAt": None, "resumedBy": None}
    row = {"halted": True, "reason": "drawdown", "at": 1.0, "by": "kill_switch",
           "resumedAt": None, "resumedBy": None, "notifiedAt": None}
    assert pl.write_halt(row) is True
    assert pl.read_halt() == row


def test_rows_without_a_bucket_key_are_daily():
    rows = [order("ASML", intent_id="old"), {**order("ASML", intent_id="d"), "bucket": "daily"},
            {**order("ASML", intent_id="i"), "bucket": "intraday"}]
    assert pl.row_bucket(rows[0]) == "daily"
    assert [r["intentId"] for r in pl.bucket_rows(rows, "daily")] == ["old", "d"]
    assert [r["intentId"] for r in pl.bucket_rows(rows, "intraday")] == ["i"]


def test_bucket_holds_positions_follows_the_buckets_own_fills():
    rows = [order("ASML", "buy", 2.0, value=200.0),
            {**order("NVDA", "buy", 1.0, value=100.0), "bucket": "intraday"}]
    assert pl.bucket_holds_positions(rows, "daily") is True
    assert pl.bucket_holds_positions(rows, "intraday") is True
    rows.append({**order("NVDA", "sell", 1.0, value=105.0), "bucket": "intraday"})
    assert pl.bucket_holds_positions(rows, "intraday") is False       # sold out again
    assert pl.bucket_holds_positions([order("ASML", "buy", 1.0, state="submitted")], "daily") is False
    assert pl.bucket_holds_positions([], "daily") is False


def test_ledger_holdings_use_an_average_cost_basis():
    rows = [order("ASML", "buy", 2.0, value=200.0), order("ASML", "buy", 2.0, value=300.0),
            order("ASML", "sell", 1.0, value=140.0)]
    h = pl.ledger_holdings(rows)["ASML"]
    assert h["quantity"] == 3.0 and h["costEur"] == pytest.approx(375.0)     # 500 - 500/4
    shadow = [order("ASML", "buy", 2.0, state="shadow", filled=2.0, value=200.0)]
    assert pl.ledger_holdings(shadow) == {}


def bucket_for(which, positions, orders, **kw):
    return pl.bucket_from_positions(
        positions=positions, account=None, universe=UNIVERSE, budget_eur=1000.0,
        prices_by_id=kw.pop("prices", {}), orders=orders, bucket=which, **kw)


def two_bucket_rows():
    return [order("ASML", "buy", 2.0, value=200.0, intent_id="2026-10-19-daily-s1-agent-ASML-buy-1"),
            {**order("ASML", "buy", 1.0, value=120.0,
                     intent_id="2026-10-19-intraday-i1-agent-ASML-buy-1"), "bucket": "intraday"},
            {**order("NVDA", "buy", 3.0, value=90.0,
                     intent_id="2026-10-19-intraday-i1-agent-NVDA-buy-1"), "bucket": "intraday"}]


def test_each_bucket_holds_and_spends_only_its_own_fills():
    rows = two_bucket_rows()
    broker = [pos("ASMLa_EQ", 3.0, 110.0), pos("NVDd_EQ", 3.0, 30.0)]
    prices = {"ASML": quote(110.0), "NVDA": quote(35.0)}
    d = bucket_for("daily", broker, rows, prices=prices)
    i = bucket_for("intraday", broker, rows, prices=prices)
    assert [(p["id"], p["quantity"]) for p in d["positions"]] == [("ASML", 2.0)]
    assert d["cashEur"] == 800.0 and d["equityEur"] == pytest.approx(800.0 + 220.0)
    assert [(p["id"], p["quantity"]) for p in i["positions"]] == [("ASML", 1.0), ("NVDA", 3.0)]
    assert i["cashEur"] == 1000.0 - 210.0 and i["equityEur"] == pytest.approx(790.0 + 110.0 + 105.0)
    assert d["bucketCostEur"] == 200.0 and i["bucketCostEur"] == 210.0
    assert d["warnings"] == [] and i["warnings"] == []               # broker == sum of both buckets


@pytest.mark.parametrize("broker,code,ids", [
    ([pos("ASMLa_EQ", 2.0, 100.0), pos("NVDd_EQ", 3.0, 30.0)], "ledger_mismatch", ["ASML"]),
    ([pos("ASMLa_EQ", 3.0, 100.0), pos("NVDd_EQ", 3.0, 30.0), pos("SXR8d_EQ", 1.0, 5.0)],
     "untracked_position", ["SXR8"]),
])
def test_reconciliation_is_broker_wide_and_shown_in_both_buckets(broker, code, ids):
    rows = two_bucket_rows()
    for which in ("daily", "intraday"):
        w = {w["code"]: w for w in bucket_for(which, broker, rows)["warnings"]}
        assert w[code]["ids"] == ids


def test_a_second_buckets_fill_never_makes_the_first_look_mismatched():
    rows = two_bucket_rows()[:1] + [{**order("ASML", "buy", 5.0, value=500.0), "bucket": "intraday"}]
    broker = [pos("ASMLa_EQ", 7.0, 100.0)]
    assert bucket_for("daily", broker, rows)["warnings"] == []


def test_the_per_bucket_stores_are_separate_files_beside_the_daily_ones():
    pl.write_season({"book": "practice", "inceptionDate": "2026-10-19", "baselineWeights": {}})
    pl.write_season({"book": "practice", "bucket": "intraday", "inceptionDate": "2026-10-20",
                     "baselineWeights": {}}, "intraday")
    assert pl.read_season()["inceptionDate"] == "2026-10-19"
    assert pl.read_season("intraday")["inceptionDate"] == "2026-10-20"
    assert pl.SEASON_FILE.with_name("paper_season.intraday.json").exists()
    pl.save_snapshot_row({"date": "2026-10-19", "equityEur": 1.0}, "intraday")
    assert pl.read_snapshots() == [] and len(pl.read_snapshots("intraday")) == 1
    assert pl.latest_snapshot("intraday")["date"] == "2026-10-19" and pl.latest_snapshot() is None
    pl.write_halt({"halted": True, "reason": "x", "at": 1.0, "by": "owner"}, "intraday")
    assert pl.read_halt()["halted"] is False and pl.read_halt("intraday")["halted"] is True
    pl.save_brief({"date": "2026-10-19", "text": "d"})
    pl.save_brief({"date": "2026-10-19", "text": "i1"}, "intraday")
    pl.save_brief({"date": "2026-10-19", "text": "i2"}, "intraday")            # same date replaces
    assert [b["text"] for b in pl.read_briefs()] == ["d"]
    assert [b["text"] for b in pl.read_briefs("intraday")] == ["i2"]
    assert pl.BRIEFS_FILE.with_name("paper_briefs.intraday.json").exists()


# ------------------------------------------------------------------- season

def test_season_store_round_trips():
    assert pl.read_season() is None
    row = {"book": "practice", "strategyVersion": "s1",
          "inceptionDate": "2026-10-19", "startedAt": 1.0,
          "baselineWeights": {"ASML": 60.0, "NVDA": 40.0}}
    assert pl.write_season(row) is True
    assert pl.read_season() == row


# ---------------------------------------------------------------- snapshots

def snap(date, equity=1000.0):
    return {"date": date, "equityEur": equity, "cashEur": 0.0,
           "positions": [], "accountTotalEur": None, "accountCashEur": None,
           "takenAt": 1.0}


def test_snapshots_are_one_row_per_date_newest_replaces_oldest():
    assert pl.read_snapshots() == []
    pl.save_snapshot_row(snap("2026-10-17", 900.0))
    pl.save_snapshot_row(snap("2026-10-18", 950.0))
    pl.save_snapshot_row(snap("2026-10-18", 955.0))   # same date: replaces, not appends
    rows = pl.read_snapshots()
    assert [r["date"] for r in rows] == ["2026-10-17", "2026-10-18"]
    assert pl.snapshot_on("2026-10-18")["equityEur"] == 955.0
    assert pl.latest_snapshot()["date"] == "2026-10-18"


def test_snapshot_rows_are_capped(monkeypatch):
    monkeypatch.setattr(pl, "MAX_SNAPSHOT_ROWS", 3)
    for i in range(1, 6):
        pl.save_snapshot_row(snap(f"2026-10-{10 + i:02d}", float(i)))
    rows = pl.read_snapshots()
    assert len(rows) == 3
    assert [r["date"] for r in rows] == ["2026-10-13", "2026-10-14", "2026-10-15"]
    assert pl.latest_snapshot()["date"] == "2026-10-15"
