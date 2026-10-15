"""PUT /api/config validation + merge rules (pure; real registry schema).

Run inside the service image (see test_prices.py for the command).
"""
import copy
import datetime
import json
import pathlib

import pytest

from app.api import config_edit

SEED = json.loads((pathlib.Path(__file__).resolve().parents[2]
                   / "config" / "config.example.json").read_text())


def apply(body, cfg=None):
    cfg = copy.deepcopy(cfg or SEED)
    added = config_edit.apply_update(cfg, body)
    return cfg, added


def bad(body, text):
    with pytest.raises(config_edit.ConfigInvalid, match=text):
        apply(body)


def test_seed_config_is_valid_under_the_registry_schema():
    cfg, added = apply({"watchlist": SEED["watchlist"],
                        "portfolio": SEED["portfolio"]})
    assert added == []
    assert [e["id"] for e in cfg["watchlist"]] == ["ASML", "NVDA", "SXR8", "GSPC"]
    assert cfg["portfolio"]["SXR8"] == SEED["portfolio"]["SXR8"] == {"shares": 3.0, "investedAmount": None}


@pytest.mark.parametrize("pos,text", [
    ({"shares": 0, "investedAmount": 1}, "shares must be > 0"),
    ({"shares": -1}, "shares must be > 0"),
    ({"shares": "abc"}, "shares must be a number"),
    ({"shares": True}, "shares must be a number"),
    ({"shares": float("nan")}, "finite"),
    ({"investedAmount": 5}, "shares must be a number"),
    ({"shares": 1, "investedAmount": -0.01}, "investedAmount must be >= 0"),
    ({"shares": 1, "investedAmount": "x"}, "investedAmount must be a number"),
    ("nope", "must be an object"),
])
def test_bad_positions_are_rejected(pos, text):
    bad({"portfolio": {"ASML": pos}}, text)


def test_position_must_be_a_watchlist_holding():
    bad({"portfolio": {"TSLA": {"shares": 1}}}, "not on the watchlist")
    bad({"portfolio": {"GSPC": {"shares": 1}}}, "only holdings")


def test_position_normalised_and_legacy_fields_dropped():
    cfg, _ = apply({"portfolio": {"asml": {
        "shares": "2.5", "investedAmount": None, "currency": "EUR",
        "investedPrice": 1745.6, "priceCurrency": "USD"}}})
    assert cfg["portfolio"] == {"ASML": {"shares": 2.5, "investedAmount": None}}


@pytest.mark.parametrize("wl,text", [
    ("ASML,NVDA", "must be a list"),
    ([], "cannot be empty"),
    ([42], "must be an object"),
    ([SEED["watchlist"][0], SEED["watchlist"][0]], "duplicate id"),
    ([{"id": "ASML", "kind": "bond"}], "kind must be one of"),
    ([{"id": "X", "providers": {"bloomberg": "X"}}], "unknown provider"),
])
def test_bad_watchlists_are_rejected(wl, text):
    bad({"watchlist": wl}, text)


def test_sparse_watchlist_entry_keeps_stored_providers_and_flags():
    wl = [{"id": "asml", "label": "ASML NV"}, {"id": "NVDA"}, {"id": "SXR8"},
          {"id": "GSPC"}]
    cfg, added = apply({"watchlist": wl})
    asml = cfg["watchlist"][0]
    assert asml["label"] == "ASML NV"
    assert asml["providers"]["yahoo"] == "ASML.AS"
    assert asml["listing"]["venue"] == "XAMS"
    assert cfg["watchlist"][2]["analyzeable"] is False
    assert added == []


def test_new_ticker_is_reported_for_history_seeding():
    wl = copy.deepcopy(SEED["watchlist"]) + [
        {"id": "SAP", "listing": {"symbol": "SAP.DE", "venue": "XETR",
                                  "currency": "EUR"},
         "providers": {"yahoo": "SAP.DE"}}]
    cfg, added = apply({"watchlist": wl})
    assert added == ["SAP"]
    assert cfg["watchlist"][-1]["id"] == "SAP"


def test_removing_a_held_ticker_requires_dropping_the_position():
    wl = [e for e in SEED["watchlist"] if e["id"] != "NVDA"]
    bad({"watchlist": wl}, "NVDA: not on the watchlist")
    cfg, _ = apply({"watchlist": wl, "portfolio": {
        k: v for k, v in SEED["portfolio"].items() if k != "NVDA"}})
    assert "NVDA" not in cfg["portfolio"]


def test_unknown_keys_ignored_and_other_blocks_preserved():
    cfg, added = apply({"displayCurrency": "USD", "bogus": 1,
                        "chatModel": " qwen "})
    assert "displayCurrency" not in cfg and "bogus" not in cfg
    assert cfg["chatModel"] == "qwen"
    assert cfg["aliases"] == SEED["aliases"]
    assert cfg["alertRules"] == SEED["alertRules"]
    assert cfg["portfolio"] == SEED["portfolio"] and added == []


def test_chat_model_must_be_a_string_and_blank_is_ignored():
    bad({"chatModel": 5}, "chatModel must be a string")
    cfg, _ = apply({"chatModel": "  "})
    assert cfg["chatModel"] == "lane"


# ------------------------------------------------------------------- lots

TODAY = datetime.date(2026, 10, 14)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(config_edit, "_today", lambda: TODAY)


def lot(date, shares, price, fees=0.0, side="buy"):
    return {"date": date, "shares": shares, "priceEur": price,
            "feesEur": fees, "side": side}


def position(lots, cfg=None):
    out, _ = apply({"portfolio": {"ASML": {"lots": lots}}}, cfg)
    return out["portfolio"]["ASML"]


def cfg_with_lots():
    """SEED plus two ASML lots: 1 x 900 + 1 fee, 1.5 x 1000 -> 2.5 / 2401."""
    cfg = copy.deepcopy(SEED)
    cfg["portfolio"]["ASML"] = position([lot("2024-03-14", 1.0, 900.0, fees=1.0),
                                         lot("2025-02-10", 1.5, 1000.0)])
    return cfg


def test_lots_derive_shares_and_cost_with_fees_and_keep_the_lots():
    asml = cfg_with_lots()["portfolio"]["ASML"]
    assert asml["shares"] == 2.5 and asml["investedAmount"] == 2401.0
    assert [x["date"] for x in asml["lots"]] == ["2024-03-14", "2025-02-10"]


def test_buy_fees_are_part_of_the_cost_basis():
    pos = position([lot("2024-01-01", 2.0, 100.0, fees=2.0)])
    assert pos["shares"] == 2.0 and pos["investedAmount"] == 202.0


def test_fifo_sell_consumes_the_oldest_lot_first():
    pos = position([lot("2024-01-01", 1.0, 100.0, fees=1.0),
                    lot("2024-02-01", 1.0, 200.0),
                    lot("2024-03-01", 1.0, 300.0, fees=2.0, side="sell")])
    assert pos["shares"] == 1.0
    assert pos["investedAmount"] == 200.0          # the 101-cost lot is gone


def test_fifo_partial_sell_keeps_the_proportional_cost_of_the_lot():
    pos = position([lot("2024-01-01", 2.0, 100.0, fees=2.0),    # 101 / share
                    lot("2024-02-01", 1.0, 130.0),
                    lot("2024-03-01", 1.5, 500.0, side="sell")])
    assert pos["shares"] == 1.5
    assert pos["investedAmount"] == 180.5          # 0.5 x 101 + 1 x 130


def test_lots_are_stored_sorted_and_same_day_buy_precedes_sell():
    pos = position([lot("2024-03-01", 1.0, 300.0, side="sell"),
                    lot("2024-03-01", 2.0, 250.0),
                    lot("2024-01-01", 1.0, 100.0)])
    assert [(x["date"], x["side"]) for x in pos["lots"]] == [
        ("2024-01-01", "buy"), ("2024-03-01", "buy"), ("2024-03-01", "sell")]
    assert pos["shares"] == 2.0 and pos["investedAmount"] == 500.0


def test_lot_dated_today_is_allowed():
    assert position([lot("2026-10-14", 1.0, 10.0)])["shares"] == 1.0


@pytest.mark.parametrize("lots,text", [
    ([lot("2026-10-15", 1.0, 10.0)], "in the future"),
    ([lot("14/03/2024", 1.0, 10.0)], r"lots\[0\]\.date must be an ISO date"),
    ([lot("2024-02-30", 1.0, 10.0)], "ISO date"),
    ([lot(None, 1.0, 10.0)], "ISO date"),
    ([lot("2024-01-01", 0, 10.0)], "shares must be > 0"),
    ([lot("2024-01-01", "x", 10.0)], "shares must be a number"),
    ([lot("2024-01-01", 1.0, 0)], "priceEur must be > 0"),
    ([lot("2024-01-01", 1.0, 10.0, fees=-1.0)], "feesEur must be >= 0"),
    ([lot("2024-01-01", 1.0, 10.0, side="hold")], "side must be buy or sell"),
    ([{"date": "2024-01-01", "shares": 1, "priceEur": 1}],
     "side must be buy or sell"),
    ([lot("2024-01-01", 1.0, 10.0), lot("2024-02-01", 2.0, 10.0, side="sell")],
     "sell of 2 shares on 2024-02-01 exceeds the 1 held"),
    ([lot("2024-02-01", 1.0, 10.0), lot("2024-01-01", 1.0, 10.0, side="sell")],
     "exceeds the 0 held"),
    ([lot("2024-01-01", 1.0, 10.0), lot("2024-02-01", 1.0, 10.0, side="sell")],
     "leave no shares"),
    ("2024-01-01", "must be a list of lots"),
    (["buy"], "must be an object"),
])
def test_bad_lots_are_rejected(lots, text):
    bad({"portfolio": {"ASML": {"lots": lots}}}, text)


def test_plain_save_may_repeat_the_lot_derived_values_and_keeps_the_lots():
    cfg = cfg_with_lots()
    stored = cfg["portfolio"]["ASML"]
    for body in ({"shares": 2.5, "investedAmount": 2401.0},
                 {"shares": 2.5 + 1e-12, "investedAmount": 2401.004}):
        out, _ = apply({"portfolio": {"ASML": body}}, cfg)
        assert out["portfolio"]["ASML"] == stored


@pytest.mark.parametrize("body", [
    {"shares": 99, "investedAmount": 2401.0},
    {"shares": 2.5, "investedAmount": 1.0},
    {"shares": 2.5, "investedAmount": 2401.02},
    {"shares": 2.5, "investedAmount": None},
])
def test_plain_save_that_differs_from_the_lots_is_rejected(body):
    with pytest.raises(config_edit.ConfigInvalid,
                       match=r"portfolio\.ASML has dated lots; edit the "
                             r'lots, or send "lots": \[\] to drop them'):
        apply({"portfolio": {"ASML": body}}, cfg_with_lots())


@pytest.mark.parametrize("body,text", [
    ({"shares": "abc", "investedAmount": 1}, "shares must be a number"),
    ({"shares": 0, "investedAmount": 1}, "shares must be > 0"),
    ({"investedAmount": 2401.0}, "shares must be a number"),
    ({"shares": 2.5, "investedAmount": -1}, "investedAmount must be >= 0"),
    ({"shares": 2.5, "investedAmount": "x"}, "investedAmount must be a number"),
])
def test_malformed_plain_save_is_validated_before_the_lots_comparison(body, text):
    with pytest.raises(config_edit.ConfigInvalid, match=text):
        apply({"portfolio": {"ASML": body}}, cfg_with_lots())


def test_a_lots_only_body_derives_and_replaces_the_stored_lots():
    out, _ = apply({"portfolio": {"ASML": {
        "lots": [lot("2024-01-01", 1.0, 50.0)]}}}, cfg_with_lots())
    assert out["portfolio"]["ASML"]["shares"] == 1.0
    assert out["portfolio"]["ASML"]["investedAmount"] == 50.0


def test_empty_lots_clear_them_and_fall_back_to_the_plain_position():
    stored = position([lot("2024-01-01", 2.0, 100.0)])
    cfg, _ = apply({"portfolio": {"ASML": stored}})
    cfg, _ = apply({"portfolio": {"ASML": {"lots": [], "shares": 5,
                                           "investedAmount": None}}}, cfg)
    assert cfg["portfolio"]["ASML"] == {"shares": 5.0, "investedAmount": None}


def test_watchlist_only_save_keeps_positions_with_lots():
    cfg = cfg_with_lots()
    out, _ = apply({"watchlist": SEED["watchlist"]}, cfg)
    assert out["portfolio"]["ASML"] == cfg["portfolio"]["ASML"]


# ------------------------------------------------------- other new keys

def test_new_keys_are_stored_normalised():
    cfg, _ = apply({
        "performanceBenchmark": " sxr8 ",
        "exposureCurrency": {"nvda": "usd"},
        "lookThrough": {"sxr8": {"asOf": "2026-10-14", "source": " Slick ",
                                 "weights": {"nvda": 7.64}}},
        "investmentPolicy": {
            "targets": {"ASML": 30, "nvda": 20, "SXR8": 50.4},
            "maxSingleStockPct": 35, "band": {"absPct": 5, "relPct": 25},
            "monthlyContributionEur": 300, "trendFilter": True, "junk": 1},
        "taxResidency": "cz"})
    assert cfg["performanceBenchmark"] == "SXR8"
    assert cfg["exposureCurrency"] == {"NVDA": "USD"}
    assert cfg["lookThrough"] == {"SXR8": {
        "asOf": "2026-10-14", "source": "Slick", "weights": {"NVDA": 7.64}}}
    assert cfg["investmentPolicy"] == {
        "targets": {"ASML": 30.0, "NVDA": 20.0, "SXR8": 50.4},
        "maxSingleStockPct": 35.0, "band": {"absPct": 5.0, "relPct": 25.0},
        "monthlyContributionEur": 300.0, "trendFilter": True}
    assert cfg["taxResidency"] == "CZ"


def test_null_resets_optional_keys_but_tax_residency_null_is_a_value():
    cfg, _ = apply({"performanceBenchmark": None, "exposureCurrency": None,
                    "lookThrough": None, "taxResidency": None})
    assert not {"performanceBenchmark", "exposureCurrency",
                "lookThrough"} & set(cfg)
    assert cfg["taxResidency"] is None


def test_absent_new_keys_are_left_untouched():
    cfg, _ = apply({"chatModel": "x"})
    for key in ("performanceBenchmark", "exposureCurrency", "lookThrough",
                "taxResidency"):
        assert cfg[key] == SEED[key]


@pytest.mark.parametrize("body,text", [
    ({"performanceBenchmark": "TSLA"}, "TSLA is not on the watchlist"),
    ({"performanceBenchmark": 5}, "must be a watchlist id"),
    ({"exposureCurrency": {"NVDA": "US"}}, "3-letter currency code"),
    ({"exposureCurrency": {"NVDA": "U5D"}}, "3-letter currency code"),
    ({"exposureCurrency": {"TSLA": "USD"}}, "TSLA is not on the watchlist"),
    ({"exposureCurrency": ["USD"]}, "must be an object"),
    ({"lookThrough": {"SXR8": {"weights": {"ASML": 60, "NVDA": 50}}}},
     "sum to 110%"),
    ({"lookThrough": {"SXR8": {"weights": {"SXR8": 5}}}}, "cannot contain"),
    ({"lookThrough": {"SXR8": {"weights": {"NVDA": 0}}}}, r"in \(0, 100\]"),
    ({"lookThrough": {"SXR8": {"weights": {"TSLA": 1}}}}, "not on the watchlist"),
    ({"lookThrough": {"SXR8": {"weights": [1]}}}, "weights must be an object"),
    ({"lookThrough": {"SXR8": {"asOf": "yesterday", "weights": {}}}},
     "asOf must be an ISO date"),
    ({"investmentPolicy": {"targets": {"ASML": 30, "NVDA": 20}}},
     "must sum to 100"),
    ({"investmentPolicy": {"targets": {"ASML": 50, "NVDA": 50.6}}},
     "must sum to 100"),
    ({"investmentPolicy": {"targets": {"GSPC": 100}}}, "only holdings"),
    ({"investmentPolicy": {"targets": {}}}, "non-empty object"),
    ({"investmentPolicy": {"maxSingleStockPct": 0}}, "maxSingleStockPct"),
    ({"investmentPolicy": {"band": {"absPct": -1}}}, "band.absPct must be >= 0"),
    ({"investmentPolicy": {"monthlyContributionEur": -5}},
     "monthlyContributionEur must be >= 0"),
    ({"investmentPolicy": {"trendFilter": "yes"}}, "trendFilter must be true"),
    ({"taxResidency": "DE"}, "'CZ' or null"),
])
def test_bad_new_keys_are_rejected(body, text):
    bad(body, text)


def test_policy_targets_within_half_a_point_are_accepted():
    cfg, _ = apply({"investmentPolicy": {"targets": {"ASML": 50, "NVDA": 50.5}}})
    assert cfg["investmentPolicy"]["targets"] == {"ASML": 50.0, "NVDA": 50.5}


def test_new_keys_are_validated_against_the_watchlist_of_the_same_update():
    wl = [e for e in SEED["watchlist"] if e["id"] != "NVDA"]
    cfg, _ = apply({"watchlist": wl, "portfolio": {"ASML": {"shares": 1}},
                    "performanceBenchmark": "ASML"})
    assert cfg["performanceBenchmark"] == "ASML"
    with pytest.raises(config_edit.ConfigInvalid, match="NVDA is not on"):
        apply({"watchlist": wl, "portfolio": {"ASML": {"shares": 1}},
               "exposureCurrency": {"NVDA": "USD"}})
