"""Performance maths: lots -> shares/flows, TWR, XIRR, series loading
(pure functions, no network).

Run inside the service image (see test_prices.py for the command).
"""
import datetime

import pytest

from app.api import performance as perf

AXIS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def lot(date, shares, price, fees=0.0, side="buy"):
    return {"date": date, "shares": shares, "priceEur": price,
            "feesEur": fees, "side": side}


def ts(y, m, d, h=12):
    return int(datetime.datetime(y, m, d, h,
                                 tzinfo=datetime.timezone.utc).timestamp())


def twr_of(lots, closes, axis=AXIS):
    """Cumulative TWR series of one holding with ``closes`` per axis day."""
    held = perf.shares_by_day(lots, axis)
    values = {d: held[d] * c for d, c in zip(axis, closes)}
    return perf.twr_series(values, perf.flows_by_day(lots, axis), axis)


# ------------------------------------------------------ shares and flows

def test_shares_are_trade_date_and_a_non_trading_day_lot_counts_next_day():
    axis = ["2026-09-01", "2026-09-04"]
    lots = [lot("2026-09-01", 2.0, 10.0), lot("2026-09-02", 1.0, 10.0),
            lot("2026-09-04", 1.5, 10.0, side="sell")]
    assert perf.shares_by_day(lots, axis) == {"2026-09-01": 2.0,
                                              "2026-09-04": 1.5}


def test_flow_signs_and_booking_day():
    axis = ["2026-09-01", "2026-09-04", "2026-09-07"]
    lots = [lot("2026-09-01", 1.0, 100.0),               # opening, no flow
            lot("2026-09-02", 2.0, 50.0, fees=3.0),      # -> 09-04, +103
            lot("2026-09-07", 1.0, 100.0, fees=1.0, side="sell"),  # -99
            lot("2026-09-08", 1.0, 100.0)]               # after the window
    assert perf.flows_by_day(lots, axis) == {"2026-09-04": 103.0,
                                             "2026-09-07": -99.0}


def test_net_invested_counts_all_history_before_the_window():
    lots = [lot("2026-08-01", 1.0, 100.0, fees=1.0),
            lot("2026-09-03", 1.0, 120.0, side="sell")]
    assert perf.net_invested_by_day(lots, AXIS) == {
        "2026-09-01": 101.0, "2026-09-02": 101.0,
        "2026-09-03": -19.0, "2026-09-04": -19.0}


def test_clean_lots_skips_malformed_rows_and_sorts_buys_first():
    pos = {"lots": [{"date": "bad", "shares": 1, "priceEur": 1, "side": "buy"},
                    "x",
                    {"date": "2026-01-02", "shares": 1, "priceEur": 5,
                     "side": "sell"},
                    {"date": "2026-01-02", "shares": 2, "priceEur": 4,
                     "side": "buy"},
                    {"date": "2026-01-01", "shares": 0, "priceEur": 4,
                     "side": "buy"}]}
    assert perf.clean_lots(pos) == [
        lot("2026-01-02", 2.0, 4.0), lot("2026-01-02", 1.0, 5.0, side="sell")]
    assert perf.clean_lots({"shares": 1}) == []
    assert perf.clean_lots(None) == []


# -------------------------------------------------------------------- TWR

def test_twr_is_unchanged_by_a_mid_period_deposit():
    closes = [100.0, 110.0, 110.0, 121.0]
    base = [lot("2026-09-01", 1.0, 100.0)]
    deposit = base + [lot("2026-09-03", 1.0, 110.0)]
    assert [round(x, 6) for _, x in twr_of(base, closes)] == [
        0.0, 0.1, 0.1, 0.21]
    assert [round(x, 6) for _, x in twr_of(deposit, closes)] == [
        0.0, 0.1, 0.1, 0.21]


def test_flat_price_with_a_deposit_gives_zero_twr():
    closes = [100.0] * 4
    lots = [lot("2026-09-01", 1.0, 100.0), lot("2026-09-03", 2.0, 100.0)]
    assert [x for _, x in twr_of(lots, closes)] == [0.0, 0.0, 0.0, 0.0]


def test_twr_weights_every_day_equally_whatever_the_capital():
    # 1 share, then a 2nd bought at 100 on day 2, price doubles on day 3:
    # the return is 100 % although most of the capital arrived late.
    axis = AXIS[:3]
    lots = [lot("2026-09-01", 1.0, 100.0), lot("2026-09-02", 1.0, 100.0)]
    assert twr_of(lots, [100.0, 100.0, 200.0], axis)[-1][1] == pytest.approx(1.0)


def test_twr_books_a_sale_as_a_negative_flow_and_a_fee_as_drag():
    lots = [lot("2026-09-01", 2.0, 100.0),
            lot("2026-09-03", 1.0, 100.0, fees=1.0, side="sell")]
    assert perf.flows_by_day(lots, AXIS) == {"2026-09-03": -99.0}
    series = twr_of(lots, [100.0] * 4)
    # day 3: (100 - (-99)) / 200 - 1 = -0.5 %: the fee, nothing else
    assert series[2][1] == pytest.approx(-0.005)
    assert series[3][1] == pytest.approx(-0.005)


def test_daily_returns_skip_days_after_a_zero_value():
    axis = AXIS
    values = {"2026-09-01": 0.0, "2026-09-02": 100.0, "2026-09-03": 110.0,
              "2026-09-04": 99.0}
    assert perf.daily_returns(values, {"2026-09-02": 100.0}, axis) == [
        ("2026-09-03", pytest.approx(0.1)), ("2026-09-04", pytest.approx(-0.1))]
    assert perf.twr_series(values, {"2026-09-02": 100.0}, axis)[-1][1] == \
        pytest.approx(1.1 * 0.9 - 1.0)


def test_twr_of_an_empty_axis_is_empty():
    assert perf.twr_series({}, {}, []) == []


def test_a_lot_priced_at_the_order_total_is_skipped_not_compounded():
    # Real-world entry error: priceEur holds the order total (1000) for 5 shares
    # trading at 100, so the day's flow (5000) dwarfs the value it added (500).
    # Compounding that day gave a TWR far below -100 %; it must be left out and
    # reported instead.
    closes = [100.0, 100.0, 100.0, 110.0]
    lots = [lot("2026-09-01", 1.0, 100.0), lot("2026-09-03", 5.0, 1000.0)]
    held = perf.shares_by_day(lots, AXIS)
    values = {d: held[d] * c for d, c in zip(AXIS, closes)}
    flows = perf.flows_by_day(lots, AXIS)
    series = perf.twr_series(values, flows, AXIS)
    assert all(x > -1.0 for _, x in series)
    assert series[-1][1] == pytest.approx(0.10)          # only the real price move
    assert perf.inconsistent_flow_days(values, flows, AXIS) == ["2026-09-03"]


# ------------------------------------------------------------------- XIRR

D0 = datetime.date(2025, 1, 1)


def test_xirr_one_year_ten_percent():
    r = perf.xirr([(D0, -1000.0), (datetime.date(2026, 1, 1), 1100.0)])
    assert r * 100.0 == pytest.approx(10.0, abs=1e-8)
    assert perf.xirr([("2025-01-01", -1000), ("2026-01-01", 1100)]) == \
        pytest.approx(0.10, abs=1e-9)


def test_xirr_negative_return():
    assert perf.xirr([("2025-01-01", -1000), ("2026-01-01", 900)]) == \
        pytest.approx(-0.10, abs=1e-9)


def test_xirr_multiple_flows_closed_form():
    # -1000 now, -1000 after 1y, +2310 after 2y: 2310 x^2 - 1000 x - 1000 = 0
    # has the root x = 1 / 1.1.
    flows = [("2025-01-01", -1000.0), ("2026-01-01", -1000.0),
             ("2027-01-01", 2310.0)]
    assert perf.xirr(flows) == pytest.approx(0.10, abs=1e-9)
    # flow order does not matter
    assert perf.xirr(flows[::-1]) == pytest.approx(0.10, abs=1e-9)


def test_xirr_with_an_interim_inflow_is_a_bond_at_five_percent():
    flows = [("2025-01-01", -1000.0), ("2026-01-01", 50.0),
             ("2027-01-01", 1050.0)]
    assert perf.xirr(flows) == pytest.approx(0.05, abs=1e-9)


@pytest.mark.parametrize("flows", [
    [("2025-01-01", -1000.0)],
    [("2025-01-01", -1000.0), ("2026-01-01", -5.0)],
    [("2025-01-01", 1000.0), ("2026-01-01", 5.0)],
    [("2025-01-01", 0.0), ("2026-01-01", 0.0)],
    # total loss within a day: the root lies far outside the bracket
    [("2025-01-01", -1000.0), ("2025-01-02", 1.0)],
])
def test_xirr_without_a_root_in_the_bracket_is_none(flows):
    assert perf.xirr(flows) is None


def test_xirr_for_window_uses_the_opening_value_and_inner_lots():
    lots = {"A": [lot("2026-01-01", 1.0, 1000.0)]}
    r = perf.xirr_for_window(lots, "2025-01-01", "2027-01-01", 1000.0, 2310.0)
    assert r == pytest.approx(0.10, abs=1e-9)


def test_xirr_for_window_does_not_double_count_lots_in_the_opening_value():
    lots = {"A": [lot("2025-01-01", 1.0, 1000.0)]}     # on the start day
    r = perf.xirr_for_window(lots, "2025-01-01", "2026-01-01", 1000.0, 1100.0)
    assert r == pytest.approx(0.10, abs=1e-9)


def test_xirr_for_all_history_uses_dated_lots_not_an_opening_value():
    lots = {"A": [lot("2025-01-01", 1.0, 1000.0)],
            "B": [lot("2026-01-01", 1.0, 1000.0)]}
    r = perf.xirr_for_window(lots, "2025-01-01", "2027-01-01", 12345.0, 2310.0,
                             all_history=True)
    assert r == pytest.approx(0.10, abs=1e-9)


def test_xirr_for_window_counts_sales_as_inflows():
    lots = {"A": [lot("2026-01-01", 1.0, 1100.0, side="sell")]}
    r = perf.xirr_for_window(lots, "2025-01-01", "2026-01-01", 1000.0, 0.0)
    assert r == pytest.approx(0.10, abs=1e-9)


# ----------------------------------------------------------- series input

def test_to_days_from_bars_keeps_the_last_close_of_a_day_and_drops_junk():
    bars = [{"t": ts(2026, 9, 2, 16), "c": 12.0}, {"t": ts(2026, 9, 2, 9), "c": 11.0},
            {"t": ts(2026, 9, 3), "c": 0}, {"t": ts(2026, 9, 4), "c": None},
            {"t": ts(2026, 9, 1), "c": 10.0}, {"c": 5.0}]
    assert perf.to_days(bars) == {"2026-09-01": 10.0, "2026-09-02": 12.0}


def test_to_days_from_daily_close_rows():
    rows = [("2026-09-02", 10.0), ("2026-09-01", 5.0), ("bad", 1.0),
            ("2026-09-03", -1.0), ["2026-09-04", 7.0]]
    assert perf.to_days(rows) == {"2026-09-01": 5.0, "2026-09-02": 10.0,
                                  "2026-09-04": 7.0}


def test_to_days_converts_usd_with_the_carried_rate_and_drops_unrated_days():
    entry = {"id": "GSPC", "quoteCurrency": "USD", "listing": None}
    rows = [("2026-08-20", 100.0), ("2026-09-02", 100.0)]
    out = perf.to_days(rows, perf.factor_for(entry, {"2026-09-01": 0.5}))
    assert out == {"2026-09-02": 50.0}                 # 08-20: older than 7 d


def test_other_currencies_have_no_factor_and_are_dropped():
    entry = {"id": "X", "listing": {"currency": "GBP"}}
    assert perf.to_days([("2026-09-01", 5.0)], perf.factor_for(entry, {})) == {}
    assert perf.entry_currency({"id": "Y"}) == "EUR"


def test_carry_leaves_days_before_the_first_value_absent():
    assert perf.carry({"2026-09-02": 5.0}, AXIS) == {
        "2026-09-02": 5.0, "2026-09-03": 5.0, "2026-09-04": 5.0}


@pytest.mark.parametrize("days,period", [
    (100, "6mo"), (183, "6mo"), (184, "1y"), (400, "2y"), (1200, "5y"),
    (3000, "max")])
def test_period_for_picks_the_smallest_covering_period(days, period):
    assert perf.period_for(days) == period


def test_first_on_or_after():
    assert perf.first_on_or_after(AXIS, "2026-09-02") == "2026-09-02"
    assert perf.first_on_or_after(AXIS, "2026-08-01") == "2026-09-01"
    assert perf.first_on_or_after(AXIS, "2026-09-05") is None
