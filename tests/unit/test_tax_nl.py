"""tax_nl: box 3 forfait formula on the spec examples, tegenbewijs min() rule, Jan-1
valuation from lots, dividend credit, warnings, taxNL config validation, CZ payload
unchanged, NL digest tag None. No network: config, clock, closes, dividends and the
portfolio snapshot are faked.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import copy
import datetime
import json
import pathlib

import pytest

from app.api import config_edit, digest, evidence, tax, tax_nl

TODAY = datetime.date(2026, 10, 15)
SEED = json.loads((pathlib.Path(__file__).resolve().parents[2]
                   / "config" / "config.example.json").read_text())


def run(coro):
    return asyncio.run(coro)


def lot(day, shares, price, side="buy"):
    return {"date": day, "shares": shares, "priceEur": price, "feesEur": 0.0, "side": side}


# ------------------------------------------------------------ forfait formula

P25, P26 = tax_nl.PARAMS[2025], tax_nl.PARAMS[2026]


def test_forfait_spec_example_2026_single():
    f = tax_nl.forfait(20_000, 80_000, 0, P26, partner=False)
    assert f["base"] == 100_000 and f["returnEur"] == pytest.approx(5_056)
    assert f["effectiveReturnPct"] == pytest.approx(5.056)
    assert f["taxEur"] == pytest.approx(740, abs=0.5)


def test_forfait_spec_example_2025_overige_only():
    f = tax_nl.forfait(0, 100_000, 0, P25, partner=False)
    assert f["taxEur"] == pytest.approx(896, abs=0.5)        # 36% x 42,316 x 5.88%


def test_forfait_partner_doubles_heffingvrij_and_threshold():
    single = tax_nl.forfait(20_000, 80_000, 0, P26, partner=False)
    partners = tax_nl.forfait(20_000, 80_000, 0, P26, partner=True)
    assert partners["taxFree"] == 2 * single["taxFree"] == 118_714
    assert partners["taxEur"] == 0.0                         # 100,000 < 118,714
    big = tax_nl.forfait(0, 200_000, 0, P26, partner=True)
    assert big["taxEur"] == pytest.approx(0.36 * (200_000 - 118_714) * 0.06, abs=0.01)
    # 7,600 threshold for partners: 7,000 debt is below it
    assert tax_nl.forfait(0, 200_000, 7_000, P26, True)["debtEur"] == 0.0
    assert tax_nl.forfait(0, 200_000, 8_000, P26, True)["debtEur"] == pytest.approx(400)


def test_forfait_debt_only_counts_above_the_threshold():
    below = tax_nl.forfait(0, 100_000, 3_000, P26, False)
    none = tax_nl.forfait(0, 100_000, 0, P26, False)
    assert below["taxEur"] == none["taxEur"] and below["base"] == 100_000
    above = tax_nl.forfait(0, 100_000, 10_000, P26, False)   # 6,200 counts
    assert above["base"] == pytest.approx(93_800)
    assert above["returnEur"] == pytest.approx(6_000 - 6_200 * 0.027)
    assert above["taxEur"] == pytest.approx(
        0.36 * (93_800 - 59_357) * above["returnEur"] / 93_800)


def test_forfait_nonpositive_base_is_zero_tax():
    f = tax_nl.forfait(0, 1_000, 20_000, P26, False)         # 16,200 debt counts > 1,000
    assert f["base"] < 0 and f["taxEur"] == 0.0 and f["effectiveReturnPct"] == 0.0
    assert tax_nl.forfait(0, 0, 0, P26, False)["taxEur"] == 0.0


# --------------------------------------------------------------- tegenbewijs

def test_tegenbewijs_spec_example_loss_gives_zero_no_heffingvrij():
    t = tax_nl.tegenbewijs(100_000, 105_000, 10_000, 0, 0, 0.36)
    assert t["rawReturnEur"] == -5_000 and t["actualReturnEur"] == 0 and t["taxEur"] == 0
    gain = tax_nl.tegenbewijs(100_000, 130_000, 10_000, 0, 0, 0.36)
    assert gain["actualReturnEur"] == 20_000 and gain["taxEur"] == pytest.approx(7_200)
    # no heffingvrij: a small gain is taxed in full
    small = tax_nl.tegenbewijs(10_000, 10_500, 0, 0, 0, 0.36)
    assert small["taxEur"] == pytest.approx(180)
    # withdrawals add back, dividends add
    assert tax_nl.tegenbewijs(100, 90, 0, 20, 5, 0.36)["actualReturnEur"] == 15


# ------------------------------------------------------------------ payload

class World:
    def __init__(self):
        self.config = {"taxResidency": "NL", "portfolio": {}}
        self.closes = {}
        self.divs = {}
        self.div_fail = set()
        self.values = {}
        self.entries = {}


@pytest.fixture
def w(monkeypatch):
    h = World()

    async def closes(ident, period):
        return h.closes.get(ident, [])

    async def divs(ident):
        if ident in h.div_fail:
            raise RuntimeError("down")
        return h.divs.get(ident, [])

    async def snapshot():
        return {"positions": [{"id": k, "valueEur": v} for k, v in h.values.items()]}

    monkeypatch.setattr(tax_nl.config_store, "read", lambda: h.config)
    monkeypatch.setattr(tax, "_today", lambda: TODAY)
    monkeypatch.setattr(tax_nl, "_closes", closes)
    monkeypatch.setattr(tax_nl, "_dividends", divs)
    monkeypatch.setattr(tax_nl.portfolio, "snapshot", snapshot)
    monkeypatch.setattr(tax_nl.registry, "get", lambda pid: h.entries.get(pid))
    return h


ASML_ENTRY = {"id": "ASML", "kind": "equity", "isin": "NL0010273215",
              "listing": {"symbol": "ASML.AS"}}
NVDA_ENTRY = {"id": "NVDA", "kind": "equity", "listing": {"symbol": "NVD.DE"}}


def asml(h, lots, shares, closes, value=None):
    h.config["portfolio"]["ASML"] = {"shares": shares, "investedAmount": 1.0, "lots": lots}
    h.closes["ASML"] = closes
    h.entries["ASML"] = ASML_ENTRY
    h.values["ASML"] = value


def year(out, y):
    return next(r for r in out["years"] if r["year"] == y)


def codes(out):
    return {x["code"]: x for x in out["warnings"]}


CLOSES = [("2024-12-31", 1000.0), ("2025-12-31", 950.0), ("2026-10-14", 1000.0)]
LOTS = [lot("2024-06-03", 100, 900.0), lot("2025-07-01", 10, 1100.0)]


def test_jan1_value_comes_from_lots_and_the_prior_year_end_close(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    out = run(tax.tax_payload())
    y25, y26 = year(out, 2025), year(out, 2026)
    assert y25["peildatum"] == "2025-01-01" and y25["portfolioJan1Eur"] == 100_000
    # all 110 shares (incl. the 2025-07-01 purchase) are held on 1 Jan 2026
    assert y26["portfolioJan1Eur"] == pytest.approx(110 * 950)
    assert y25["forfait"]["taxEur"] == pytest.approx(
        tax_nl.forfait(0, 100_000, 0, P25, False)["taxEur"], abs=0.01)


def test_tegenbewijs_beats_forfait_on_a_loss_and_forfait_wins_on_a_gain(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    out = run(tax.tax_payload())
    y25 = year(out, 2025)
    teg = y25["tegenbewijs"]
    assert teg["vStart"] == 100_000 and teg["vEnd"] == pytest.approx(110 * 950)
    assert teg["deposits"] == pytest.approx(11_000) and teg["withdrawals"] == 0
    assert teg["rawReturnEur"] == pytest.approx(104_500 - 100_000 - 11_000)
    assert teg["actualReturnEur"] == 0 and teg["taxEur"] == 0
    assert y25["applies"] == "tegenbewijs" and y25["taxEur"] == 0
    # 2026 YTD: actual 5,500 -> 1,980 > forfait ~975 -> forfait applies, labelled estimate
    y26 = year(out, 2026)
    assert y26["tegenbewijs"]["vEnd"] == 110_000 and y26["tegenbewijs"]["estimate"] is True
    assert y26["tegenbewijs"]["taxEur"] == pytest.approx(0.36 * 5_500)
    assert y26["applies"] == "forfait" and y26["estimate"] is True
    assert y26["taxEur"] == y26["forfait"]["taxEur"]


def test_sales_are_withdrawals_and_buy_fees_are_not_deposits(w):
    lots = [lot("2024-06-03", 100, 900.0), lot("2025-03-03", 20, 1000.0, "sell")]
    lots[0]["feesEur"] = 50.0
    asml(w, lots, 80, CLOSES, value=80_000.0)
    teg = year(run(tax.tax_payload()), 2025)["tegenbewijs"]
    assert teg["withdrawals"] == pytest.approx(20_000) and teg["deposits"] == 0
    assert teg["vEnd"] == pytest.approx(80 * 950)


def test_holding_bought_mid_year_has_zero_value_that_year(w):
    asml(w, [lot("2025-07-01", 10, 1100.0), lot("2026-01-01", 5, 1000.0)], 15,
         CLOSES, value=15_000.0)
    out = run(tax.tax_payload())
    y25, y26 = year(out, 2025), year(out, 2026)
    assert y25["portfolioJan1Eur"] == 0 and y25["forfait"]["taxEur"] == 0
    assert y26["portfolioJan1Eur"] == pytest.approx(10 * 950)   # the 1 Jan 2026 buy is not held yet
    assert y26["tegenbewijs"]["deposits"] == pytest.approx(5_000)


def test_dividends_use_shares_held_before_the_ex_date_and_credit_dutch_tax(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    w.divs["ASML"] = [(datetime.date(2025, 4, 25), 2.0),
                      (datetime.date(2025, 7, 1), 1.0),     # bought that day: 100 shares
                      (datetime.date(2024, 4, 25), 9.0)]    # other year
    y25 = year(run(tax.tax_payload()), 2025)
    assert y25["tegenbewijs"]["dividends"] == pytest.approx(100 * 2.0 + 100 * 1.0)
    assert y25["dividendTaxCreditEur"] == pytest.approx(45.0)    # 15% of gross, estimate
    assert y25["dividendTaxCreditEstimate"] is True and y25["estimate"] is True
    assert y25["netEur"] == pytest.approx(y25["taxEur"] - 45.0)


def test_real_dividend_tax_withheld_replaces_the_estimate(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    w.divs["ASML"] = [(datetime.date(2025, 4, 25), 2.0)]
    w.config["taxNL"] = {"dividendTaxWithheldEur": {"2025": 31.5}}
    y25 = year(run(tax.tax_payload()), 2025)
    assert y25["dividendTaxCreditEur"] == 31.5 and y25["dividendTaxCreditEstimate"] is False


def test_only_dutch_stocks_get_a_dividend_credit(w):
    w.config["portfolio"]["NVDA"] = {"shares": 10, "investedAmount": 1.0,
                                     "lots": [lot("2024-06-03", 10, 100.0)]}
    w.closes["NVDA"] = [("2024-12-31", 100.0), ("2025-12-31", 120.0)]
    w.entries["NVDA"] = NVDA_ENTRY
    w.values["NVDA"] = 1_300.0
    w.divs["NVDA"] = [(datetime.date(2025, 6, 1), 1.0)]
    y25 = year(run(tax.tax_payload()), 2025)
    assert y25["tegenbewijs"]["dividends"] == 10 and y25["dividendTaxCreditEur"] == 0


def test_missing_other_assets_warns_and_labels_investments_only(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    out = run(tax.tax_payload())
    assert codes(out)["other_assets_missing"]["ids"] == ["2025", "2026", "projection"]
    assert year(out, 2025)["scope"] == "investments_only" and year(out, 2025)["otherAssets"] is None


def test_other_assets_feed_the_forfait_and_flag_partial_tegenbewijs(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    w.config["taxNL"] = {"fiscalPartner": False, "otherAssets": {
        "2025": {"bankEur": 20_000, "otherEur": 0, "debtsEur": 0},
        "2026": {"bankEur": 20_000, "otherEur": 0, "debtsEur": 0}}}
    out = run(tax.tax_payload())
    assert "other_assets_missing" not in codes(out)
    y25 = year(out, 2025)
    assert y25["scope"] == "with_other_assets" and y25["forfait"]["base"] == 120_000
    assert y25["forfait"]["taxEur"] == pytest.approx(
        tax_nl.forfait(20_000, 100_000, 0, P25, False)["taxEur"], abs=0.01)
    assert y25["tegenbewijs"]["partial"] is True


def test_positions_without_lots_use_current_shares_for_the_current_year_only(w):
    w.config["portfolio"]["SXR8"] = {"shares": 3.0, "investedAmount": None}
    w.closes["SXR8"] = [("2025-12-31", 600.0)]
    w.values["SXR8"] = 1_900.0
    w.entries["SXR8"] = {"id": "SXR8", "kind": "etf", "listing": {"symbol": "SXR8.DE"}}
    out = run(tax.tax_payload())
    assert [r["year"] for r in out["years"]] == [2026]
    assert year(out, 2026)["portfolioJan1Eur"] == pytest.approx(1_800)
    assert codes(out)["history_basis_current_shares"]["ids"] == ["SXR8"]


def test_past_year_without_lots_is_left_out_and_flagged(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    w.config["portfolio"]["SXR8"] = {"shares": 3.0, "investedAmount": None}
    w.closes["SXR8"] = [("2025-12-31", 600.0)]
    w.values["SXR8"] = 1_900.0
    out = run(tax.tax_payload())
    assert codes(out)["lots_missing"]["ids"] == ["SXR8"]
    assert year(out, 2025)["complete"] is False and year(out, 2026)["complete"] is True


def test_dividend_source_failure_is_a_warning_not_an_error(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    w.div_fail.add("ASML")
    assert codes(run(tax.tax_payload()))["dividends_unavailable"]["ids"] == ["ASML"]


def test_projection_has_law_and_proposal_scenarios_labelled_estimate(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    proj = run(tax.tax_payload())["projection"]
    assert proj["year"] == 2027 and proj["estimate"] is True and proj["valueEur"] == 110_000
    by = {s["label"]: s for s in proj["scenarios"]}
    assert set(by) == {"law", "proposal"} and all(s["estimate"] for s in by.values())
    assert by["law"]["taxEur"] == pytest.approx(0.36 * (110_000 - 59_357) * 0.0637, abs=1)
    assert by["proposal"]["taxEur"] == pytest.approx(0.36 * (110_000 - 30_846) * 0.0787, abs=1)
    assert by["proposal"]["taxFreeEur"] == 30_846 and by["proposal"]["status"] == "proposal"


def test_params_table_and_statuses_in_payload(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    params = run(tax.tax_payload())["params"]
    assert params["2025"]["status"] == "final" and params["2025"]["pOther"] == 0.0588
    assert params["2026"]["status"] == "provisional" and params["2026"]["taxFree"] == 59_357
    assert params["2027"]["pOther"] == 0.0637 and params["2027"]["proposal"]["pOther"] == 0.0787
    assert params["2027"]["proposal"]["taxFree"] == 30_846
    assert all(p["sources"] and all(u.startswith("https://") for u in p["sources"])
               for p in params.values())


def test_checklist_lists_the_aangifte_figures(w):
    asml(w, LOTS, 110, CLOSES, value=110_000.0)
    out = run(tax.tax_payload())
    labels = " | ".join(i["label"] for i in out["checklist"])
    assert all(i["year"] == 2025 for i in out["checklist"])
    for needle in ("Banktegoeden", "ASML on 1 Jan 2025", "Debts", "dividendbelasting",
                   "Dividends received", "31 Dec", "Deposits", "Withdrawals",
                   "Interest received", "Interest paid"):
        assert needle in labels
    assert out["disclaimer"] and "not tax advice" in out["disclaimer"]


def test_nl_without_positions_gives_an_empty_but_valid_payload(w):
    out = run(tax.tax_payload())
    assert out["residency"] == "NL" and out["years"] == [] and out["checklist"] == []


# ------------------------------------------------- CZ unchanged / NL elsewhere

def test_cz_payload_shape_is_unchanged_and_never_touches_tax_nl(monkeypatch):
    cfg = {"taxResidency": "CZ", "portfolio": {}}
    monkeypatch.setattr(tax.config_store, "read", lambda: cfg)
    monkeypatch.setattr(tax, "_today", lambda: TODAY)

    async def boom():
        raise AssertionError("tax_nl must not run for CZ")
    monkeypatch.setattr(tax_nl, "payload", boom)
    out = run(tax.tax_payload())
    assert set(out) == {"residency", "lots", "ytd", "warnings", "notes", "asOf"}
    assert out["residency"] == "CZ" and out["notes"] == list(tax.NOTES)


def test_nl_sell_status_has_no_taxable_flag_and_the_status_text(monkeypatch):
    cfg = {"taxResidency": "NL", "portfolio": {"ASML": {
        "shares": 1.0, "lots": [lot("2026-05-01", 1.0, 100.0)]}}}
    monkeypatch.setattr(tax.config_store, "read", lambda: cfg)
    monkeypatch.setattr(tax, "_today", lambda: TODAY)
    st = tax.sell_tax_status("ASML")
    assert st["taxable"] is None and st["residency"] == "NL"
    assert st["statusText"] == "NL box 3: sales are not taxed individually; assessed on 1 Jan value"
    cz = {**cfg, "taxResidency": "CZ"}
    monkeypatch.setattr(tax.config_store, "read", lambda: cz)
    assert "statusText" not in tax.sell_tax_status("ASML")


def test_policy_context_carries_the_nl_status_text(monkeypatch):
    cfg = {"taxResidency": "NL", "portfolio": {"ASML": {"shares": 1.0}}}
    monkeypatch.setattr(evidence.config_store, "read", lambda: cfg)
    monkeypatch.setattr(tax.config_store, "read", lambda: cfg)
    ctx = run(evidence._policy_context("ASML"))
    assert ctx["taxResidency"] == "NL" and ctx["tax"]["taxable"] is None
    assert ctx["tax"]["statusText"] == tax.NL_SELL_STATUS


def test_digest_tax_tag_is_none_for_nl_even_with_young_lots(monkeypatch):
    cfg = {"taxResidency": "NL", "portfolio": {"ASML": {
        "shares": 1.0, "lots": [lot("2026-05-01", 1.0, 100.0)]}}}
    monkeypatch.setattr(digest.config_store, "read", lambda: cfg)
    monkeypatch.setattr(tax.config_store, "read", lambda: cfg)
    sell = {"rating": "SELL", "action": "sell", "scale_tier": "sell", "score": 10}
    assert run(digest._tax_tag("ASML", sell, {"state": "confirmed", "prev": {}})) is None


# ------------------------------------------------------------ config validation

def apply(body):
    cfg = copy.deepcopy(SEED)
    config_edit.apply_update(cfg, body)
    return cfg


def bad(body, text):
    with pytest.raises(config_edit.ConfigInvalid, match=text):
        apply(body)


def test_nl_residency_and_tax_nl_are_accepted_and_normalised():
    cfg = apply({"taxResidency": "nl", "taxNL": {
        "fiscalPartner": True,
        "otherAssets": {2025: {"bankEur": 1000, "otherEur": 0, "debtsEur": 5}},
        "dividendTaxWithheldEur": {"2025": 12.5}}})
    assert cfg["taxResidency"] == "NL"
    assert cfg["taxNL"] == {
        "fiscalPartner": True,
        "otherAssets": {"2025": {"bankEur": 1000.0, "otherEur": 0.0, "debtsEur": 5.0}},
        "dividendTaxWithheldEur": {"2025": 12.5}}


def test_tax_nl_null_removes_the_key_and_absent_leaves_it():
    cfg = apply({"taxNL": {"fiscalPartner": False}})
    assert cfg["taxNL"] == {"fiscalPartner": False}
    assert "taxNL" not in apply({"taxNL": None})
    kept = copy.deepcopy(cfg)
    config_edit.apply_update(kept, {"taxResidency": "NL"})
    assert kept["taxNL"] == {"fiscalPartner": False}


@pytest.mark.parametrize("body,text", [
    ({"taxNL": []}, "taxNL must be an object"),
    ({"taxNL": {"partner": True}}, "unknown key.*partner"),
    ({"taxNL": {"fiscalPartner": "yes"}}, "fiscalPartner must be true or false"),
    ({"taxNL": {"otherAssets": {"2025": {"savings": 1}}}}, "unknown key.*savings"),
    ({"taxNL": {"otherAssets": {"2025": {"bankEur": -1}}}}, "bankEur must be >= 0"),
    ({"taxNL": {"otherAssets": {"2025": {"bankEur": "x"}}}}, "must be a number"),
    ({"taxNL": {"otherAssets": {"2025": {"bankEur": True}}}}, "must be a number"),
    ({"taxNL": {"otherAssets": {"2019": {"bankEur": 1}}}}, "year must be 2020..2100"),
    ({"taxNL": {"otherAssets": {"2101": {"bankEur": 1}}}}, "year must be 2020..2100"),
    ({"taxNL": {"otherAssets": {"abc": {"bankEur": 1}}}}, "year must be"),
    ({"taxNL": {"otherAssets": {"2025": 5}}}, "must be an object"),
    ({"taxNL": {"dividendTaxWithheldEur": {"2025": -3}}}, "must be >= 0"),
    ({"taxNL": {"dividendTaxWithheldEur": {"1999": 3}}}, "year must be"),
    ({"taxNL": {"dividendTaxWithheldEur": [1]}}, "must be an object keyed by year"),
])
def test_tax_nl_config_is_validated(body, text):
    bad(body, text)
