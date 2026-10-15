"""tax: FIFO lot matching with partial sells, 3-year unlock dates (29 Feb), YTD
gross proceeds in CZK with a mocked EUR/CZK rate, the 80 % warning, and the
FIFO-next ``sell_tax_status``. No network: config, clock and rates are faked.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime

import pytest

from app.api import tax

TODAY = datetime.date(2026, 10, 15)


def run(coro):
    return asyncio.run(coro)


def lot(day, shares, price, side="buy"):
    return {"date": day, "shares": shares, "priceEur": price, "feesEur": 0.0, "side": side}


@pytest.fixture
def cfg(monkeypatch):
    """Mutable fake config + clock + CZK rates; records the rate lookups."""
    h = {"config": {"taxResidency": "CZ", "portfolio": {}}, "rates": {}, "calls": []}

    async def eur_rate_on(ccy, day):
        h["calls"].append((ccy, day))
        return h["rates"].get(day)

    monkeypatch.setattr(tax.config_store, "read", lambda: h["config"])
    monkeypatch.setattr(tax.forex, "eur_rate_on", eur_rate_on)
    monkeypatch.setattr(tax, "_today", lambda: TODAY)
    return h


def hold(h, ident, lots):
    h["config"]["portfolio"][ident] = {"shares": 1.0, "investedAmount": 1.0, "lots": lots}


# ------------------------------------------------------------ unlock dates

def test_unlock_is_three_years_plus_one_day():
    assert tax.unlock_date(datetime.date(2024, 3, 14)) == datetime.date(2027, 3, 15)
    assert tax.unlock_date(datetime.date(2023, 12, 31)) == datetime.date(2027, 1, 1)


def test_a_29_feb_purchase_unlocks_on_1_march():
    assert tax.unlock_date(datetime.date(2024, 2, 29)) == datetime.date(2027, 3, 1)
    assert tax.unlock_date(datetime.date(2020, 2, 29)) == datetime.date(2023, 3, 1)
    # a 28 Feb purchase whose third anniversary falls in a leap year: 28 Feb + 1 day
    assert tax.unlock_date(datetime.date(2021, 2, 28)) == datetime.date(2024, 2, 29)
    # a leap day whose third anniversary year is a common year
    assert tax.unlock_date(datetime.date(2028, 2, 29)) == datetime.date(2031, 3, 1)


def test_the_unlock_day_itself_is_exempt_the_day_before_is_not(cfg):
    hold(cfg, "ASML", [lot("2023-10-14", 1.0, 100.0), lot("2023-10-15", 1.0, 100.0)])
    rows = {r["date"]: r for r in run(tax.tax_payload())["lots"]}
    assert rows["2023-10-14"]["unlockDate"] == "2026-10-15" and rows["2023-10-14"]["exempt"] is True
    assert rows["2023-10-15"]["unlockDate"] == "2026-10-16" and rows["2023-10-15"]["exempt"] is False


# ------------------------------------------------------------ FIFO + YTD

FIFO_LOTS = [
    lot("2022-03-14", 1.0, 600.0),
    lot("2024-03-14", 2.0, 800.0),
    lot("2025-06-02", 1.0, 900.0),
    lot("2025-12-01", 0.2, 1400.0, "sell"),     # last year: not in the YTD
    lot("2026-02-10", 1.5, 1500.0, "sell"),     # 0.8 left of lot 1 + 0.7 of lot 2
    lot("2026-05-04", 0.3, 1700.0, "sell"),     # lot 2 only
]


def test_fifo_matches_sells_to_the_oldest_lots_across_a_partial_sell(cfg):
    hold(cfg, "ASML", FIFO_LOTS)
    cfg["rates"] = {"2026-02-10": 24.0, "2026-05-04": 25.0}
    out = run(tax.tax_payload())
    age = lambda d: (TODAY - datetime.date.fromisoformat(d)).days     # noqa: E731
    assert out["lots"] == [
        {"id": "ASML", "date": "2022-03-14", "shares": 1.0, "remainingShares": 0.0,
         "ageDays": age("2022-03-14"), "unlockDate": "2025-03-15", "exempt": True},
        {"id": "ASML", "date": "2024-03-14", "shares": 2.0, "remainingShares": 1.0,
         "ageDays": age("2024-03-14"), "unlockDate": "2027-03-15", "exempt": False},
        {"id": "ASML", "date": "2025-06-02", "shares": 1.0, "remainingShares": 1.0,
         "ageDays": age("2025-06-02"), "unlockDate": "2028-06-03", "exempt": False}]


def test_ytd_gross_proceeds_use_the_sale_date_rate_and_skip_other_years(cfg):
    hold(cfg, "ASML", FIFO_LOTS)
    cfg["rates"] = {"2026-02-10": 24.0, "2026-05-04": 25.0}
    out = run(tax.tax_payload())
    assert cfg["calls"] == [("CZK", "2026-02-10"), ("CZK", "2026-05-04")]   # 2025 sale: no lookup
    ytd = out["ytd"]
    # 1.5 x 1500 x 24 + 0.3 x 1700 x 25 = 54000 + 12750, gross: fees are not deducted
    assert ytd["year"] == 2026 and ytd["grossProceedsCzk"] == 66750.0
    assert ytd["thresholdCzk"] == 100000 and ytd["pct"] == 66.75 and ytd["complete"] is True
    # only the 0.7 + 0.3 shares matched to lots younger than 3 years on the sale date
    assert ytd["grossProceedsTaxableLotsCzk"] == 37950.0        # 25200 + 12750
    assert ytd["rate"]["byDate"] == {"2026-02-10": 24.0, "2026-05-04": 25.0}
    assert "ECB" in ytd["rate"]["source"] and "Frankfurter" in ytd["rate"]["source"]
    assert [s["grossCzk"] for s in ytd["sales"]] == [54000.0, 12750.0]
    assert out["warnings"] == [] and out["residency"] == "CZ"


def test_notes_say_the_exempt_sales_question_needs_a_tax_adviser(cfg):
    hold(cfg, "ASML", FIFO_LOTS)
    notes = " ".join(run(tax.tax_payload())["notes"])
    assert "verified with a tax adviser" in notes and "ALL sales" in notes
    assert "FIFO" in notes and "not tax advice" in notes


def test_a_sell_of_only_exempt_lots_counts_in_the_conservative_total(cfg):
    hold(cfg, "ASML", [lot("2020-01-02", 2.0, 100.0), lot("2026-03-02", 2.0, 1600.0, "sell")])
    cfg["rates"] = {"2026-03-02": 25.0}
    ytd = run(tax.tax_payload())["ytd"]
    assert ytd["grossProceedsCzk"] == 80000.0 and ytd["grossProceedsTaxableLotsCzk"] == 0.0


@pytest.mark.parametrize("price,code", [(1599.99, None), (1600.0, "czk_limit_near"),
                                        (1999.0, "czk_limit_near"),
                                        (2000.0, "czk_limit_exceeded")])
def test_warning_starts_at_80_percent_and_escalates_at_the_limit(cfg, price, code):
    hold(cfg, "ASML", [lot("2020-01-02", 2.0, 100.0), lot("2026-03-02", 2.0, price, "sell")])
    cfg["rates"] = {"2026-03-02": 25.0}                    # 2 x price x 25 CZK
    out = run(tax.tax_payload())
    assert [w["code"] for w in out["warnings"]] == ([code] if code else [])
    if code:
        assert "all sales counted" in out["warnings"][0]["message"]


def test_a_missing_rate_is_named_and_the_total_is_flagged_incomplete(cfg):
    hold(cfg, "ASML", [lot("2020-01-02", 2.0, 100.0), lot("2026-03-02", 2.0, 1600.0, "sell")])
    out = run(tax.tax_payload())                            # no rate for 2026-03-02
    assert out["ytd"]["complete"] is False and out["ytd"]["grossProceedsCzk"] == 0.0
    assert out["ytd"]["rate"]["byDate"] == {} and out["ytd"]["sales"] == []
    assert [(w["code"], w["ids"]) for w in out["warnings"]] == [
        ("rate_missing", ["ASML 2026-03-02"])]


def test_a_position_without_dated_lots_is_named_not_guessed(cfg):
    hold(cfg, "ASML", [lot("2024-03-14", 1.0, 800.0)])
    cfg["config"]["portfolio"]["NVDA"] = {"shares": 3.0, "investedAmount": 300.0}
    out = run(tax.tax_payload())
    assert [r["id"] for r in out["lots"]] == ["ASML"]
    assert [(w["code"], w["ids"]) for w in out["warnings"]] == [("lots_missing", ["NVDA"])]


@pytest.mark.parametrize("residency", ["DE", None, ""])
def test_other_residency_gets_an_empty_payload_and_no_rate_lookups(cfg, residency):
    cfg["config"]["taxResidency"] = residency
    hold(cfg, "ASML", FIFO_LOTS)
    out = run(tax.tax_payload())
    assert out["residency"] == (residency or None) and out["lots"] == []
    assert out["ytd"]["grossProceedsCzk"] is None and out["ytd"]["year"] == 2026
    assert out["ytd"]["thresholdCzk"] == 100000
    assert out["warnings"] == [] and out["notes"] == [] and cfg["calls"] == []


def test_residency_is_normalised(cfg):
    cfg["config"]["taxResidency"] = " cz "
    hold(cfg, "ASML", [lot("2024-03-14", 1.0, 800.0)])
    out = run(tax.tax_payload())
    assert out["residency"] == "CZ" and len(out["lots"]) == 1


def test_malformed_lots_are_skipped_not_fatal(cfg):
    hold(cfg, "ASML", [lot("2024-03-14", 1.0, 800.0), {"date": "garbage", "shares": 1},
                       "x", lot("2024-04-01", -1.0, 800.0), lot("2024-05-01", 1.0, 800.0, "hold")])
    assert [r["date"] for r in run(tax.tax_payload())["lots"]] == ["2024-03-14"]


# ------------------------------------------------------------ sell_tax_status

MIXED = [lot("2021-05-10", 1.0, 100.0),      # unlock 2024-05-11: exempt
         lot("2023-10-14", 1.0, 100.0),      # unlock 2026-10-15 == today: exempt
         lot("2023-10-15", 1.0, 100.0),      # unlock 2026-10-16: taxable
         lot("2025-09-01", 2.0, 100.0)]      # unlock 2028-09-02: taxable


def test_whole_position_with_mixed_lot_ages(cfg):
    hold(cfg, "ASML", MIXED)
    assert tax.sell_tax_status("ASML") == {
        "taxable": True, "youngestLotDate": "2025-09-01", "unlockDate": "2028-09-02",
        "exemptShares": 2.0, "taxableShares": 3.0, "residency": "CZ", "lotsKnown": True}


@pytest.mark.parametrize("shares,exempt,young,youngest,unlock,taxable", [
    (1.0, 1.0, 0.0, "2021-05-10", "2024-05-11", False),     # the oldest lot only
    (2.0, 2.0, 0.0, "2023-10-14", "2026-10-15", False),     # the unlock day counts as exempt
    (2.5, 2.0, 0.5, "2023-10-15", "2026-10-16", True),      # half of the first young lot
    (99.0, 2.0, 3.0, "2025-09-01", "2028-09-02", True),     # clamped to the position
    (0, 0.0, 0.0, None, None, False)])
def test_sell_tax_status_looks_at_the_fifo_next_shares(cfg, shares, exempt, young,
                                                        youngest, unlock, taxable):
    hold(cfg, "ASML", MIXED)
    got = tax.sell_tax_status(" asml ", shares)
    assert (got["exemptShares"], got["taxableShares"]) == (exempt, young)
    assert (got["youngestLotDate"], got["unlockDate"], got["taxable"]) == (youngest, unlock, taxable)
    assert got["lotsKnown"] is True


def test_fifo_next_shares_skip_lots_already_sold(cfg):
    hold(cfg, "ASML", [lot("2021-05-10", 1.0, 100.0), lot("2025-09-01", 1.0, 100.0),
                       lot("2026-01-05", 1.0, 150.0, "sell")])
    got = tax.sell_tax_status("ASML")
    assert got["taxable"] is True and got["youngestLotDate"] == "2025-09-01"
    assert (got["exemptShares"], got["taxableShares"]) == (0.0, 1.0)


def test_a_fully_sold_position_has_nothing_taxable(cfg):
    hold(cfg, "ASML", [lot("2025-09-01", 1.0, 100.0), lot("2026-01-05", 1.0, 150.0, "sell")])
    got = tax.sell_tax_status("ASML")
    assert got["lotsKnown"] is True and got["taxable"] is False
    assert (got["exemptShares"], got["taxableShares"], got["youngestLotDate"]) == (0.0, 0.0, None)


def test_no_dated_lots_means_unknown_not_exempt(cfg):
    cfg["config"]["portfolio"]["ASML"] = {"shares": 2.0, "investedAmount": 100.0}
    expected = {"taxable": None, "youngestLotDate": None, "unlockDate": None,
                "exemptShares": 0.0, "taxableShares": 0.0, "residency": "CZ",
                "lotsKnown": False}
    assert tax.sell_tax_status("ASML") == expected
    assert tax.sell_tax_status("NOPE") == expected            # not in the portfolio at all
    cfg["config"]["portfolio"]["ASML"]["lots"] = []
    assert tax.sell_tax_status("ASML") == expected


def test_unusable_share_count_is_unknown(cfg):
    hold(cfg, "ASML", MIXED)
    for bad in ("x", float("nan"), True):
        got = tax.sell_tax_status("ASML", bad)
        assert got["taxable"] is None and got["lotsKnown"] is True
        assert got["exemptShares"] == 0.0 and got["taxableShares"] == 0.0


def test_other_residency_reports_the_lots_but_no_verdict(cfg):
    cfg["config"]["taxResidency"] = "DE"
    hold(cfg, "ASML", MIXED)
    got = tax.sell_tax_status("ASML")
    assert got["taxable"] is None and got["residency"] == "DE" and got["lotsKnown"] is True
    assert got["taxableShares"] == 3.0
