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
    ({"taxResidency": "DE"}, "'CZ', 'NL' or null"),
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


# -------------------------------------------------------- paperTrading

def test_seed_paper_trading_block_round_trips_unchanged():
    cfg, _ = apply({"chatModel": "lane"})
    assert cfg["paperTrading"] == SEED["paperTrading"]


def test_good_paper_trading_block_is_normalised():
    cfg, _ = apply({"paperTrading": {
        "mode": " Shadow ", "strategyVersion": " s2 ", "budgetEur": 500,
        "universe": {"asml": {"t212": " ASMLa_EQ "}, "NVDA": {"t212": "NVDd_EQ"}},
        "limits": {"maxWeightPct": {"EQUITY": 40, "etf": 100}, "minCashPct": 2,
                   "maxOrdersPerDay": 6,
                   "killSwitch": {"drawdownPct": 15, "dailyLossPct": 5}},
        "schedule": {"decideAfter": "digest", "venues": ["XETR"]}}})
    pt = cfg["paperTrading"]
    assert pt["mode"] == "shadow" and pt["strategyVersion"] == "s2"
    assert pt["budgetEur"] == 500.0
    assert pt["universe"] == {"ASML": {"t212": "ASMLa_EQ"},
                              "NVDA": {"t212": "NVDd_EQ"}}
    assert pt["limits"]["maxWeightPct"] == {"equity": 40.0, "etf": 100.0}
    assert pt["limits"]["killSwitch"] == {"drawdownPct": 15.0, "dailyLossPct": 5.0}
    assert pt["schedule"] == {"decideAfter": "digest", "venues": ["XETR"]}


def test_paper_trading_null_removes_the_block():
    cfg, _ = apply({"paperTrading": None})
    assert "paperTrading" not in cfg


@pytest.mark.parametrize("mode,text", [
    ("bogus", "mode must be one of"),
    (5, "mode must be a string"),
])
def test_bad_paper_trading_mode_is_rejected(mode, text):
    bad({"paperTrading": {"mode": mode}}, text)


@pytest.mark.parametrize("mode", ["live-approve", "live-auto", "LIVE-AUTO"])
def test_live_paper_trading_modes_are_rejected_in_phase_1(mode):
    bad({"paperTrading": {"mode": mode}}, "not available yet")


def test_paper_trading_universe_must_reference_a_holding():
    bad({"paperTrading": {"universe": {"GSPC": {"t212": "X"}}}},
        "only holdings can be traded")
    bad({"paperTrading": {"universe": {"TSLA": {"t212": "X"}}}},
        "not on the watchlist")


@pytest.mark.parametrize("body,text", [
    ({"universe": {}}, "non-empty"),
    ({"universe": {"ASML": {}}}, r"\.t212 must be a non-empty string"),
    ({"universe": {"ASML": {"t212": "  "}}}, r"\.t212 must be a non-empty string"),
    ({"universe": {"ASML": "ASMLa_EQ"}}, r"must be an object \{t212: ticker\}"),
    ({"strategyVersion": ""}, "strategyVersion must be a non-empty string"),
    ({"strategyVersion": "x" * 33}, "strategyVersion must be <= 32"),
    ({"budgetEur": 5}, r"budgetEur must be in \[10, 100000\]"),
    ({"budgetEur": 200000}, r"budgetEur must be in \[10, 100000\]"),
    ("nope", "paperTrading must be an object"),
])
def test_bad_paper_trading_fields_are_rejected(body, text):
    bad({"paperTrading": body}, text)


@pytest.mark.parametrize("limits,text", [
    ({"bogusKey": 1}, "unknown key"),
    ({"minCashPct": 150}, r"minCashPct must be in \[0, 100\]"),
    ({"minCashPct": -1}, r"minCashPct must be in \[0, 100\]"),
    ({"maxOrdersPerDay": 0}, r"maxOrdersPerDay must be in \[1, 100\]"),
    ({"maxSpreadOrStaleS": 0}, r"maxSpreadOrStaleS must be in"),
    ({"maxWeightPct": {}}, "maxWeightPct must be a non-empty object"),
    ({"maxWeightPct": {"bond": 40}}, "unknown kind"),
    ({"maxWeightPct": {"equity": 0}}, r"in \(0, 100\]"),
    ({"maxWeightPct": {"equity": 150}}, r"in \(0, 100\]"),
    ({"killSwitch": {"drawdownPct": 15, "junk": 1}}, "unknown key"),
    ({"killSwitch": {"drawdownPct": 0}}, r"in \(0, 100\]"),
    ({"killSwitch": {}}, "killSwitch must be a non-empty object"),
    ("nope", "paperTrading.limits must be an object"),
])
def test_bad_paper_trading_limits_are_rejected(limits, text):
    bad({"paperTrading": {"limits": limits}}, text)


# -------------------------------------------------- open universe (phase 6)

def test_universe_source_is_normalised_and_does_not_need_the_watchlist():
    cfg, _ = apply({"paperTrading": {"universeSource": " Paper_Universe ", "mode": "shadow",
                                     "limits": {"maxPositions": 10, "maxSectorPct": 40,
                                                "minPositionEur": 50,
                                                "maxWeightPct": {"equity": 20, "etf": 40}}}})
    pt = cfg["paperTrading"]
    assert pt["universeSource"] == "paper_universe" and "universe" not in pt   # nothing watchlist-bound required
    assert pt["limits"] == {"maxPositions": 10.0, "maxSectorPct": 40.0, "minPositionEur": 50.0,
                            "maxWeightPct": {"equity": 20.0, "etf": 40.0}}
    cfg, _ = apply({"paperTrading": {"universeSource": "watchlist"}})
    assert cfg["paperTrading"]["universeSource"] == "watchlist"


@pytest.mark.parametrize("value", ["", "yahoo", None, 3, ["paper_universe"]])
def test_a_bad_universe_source_is_rejected(value):
    bad({"paperTrading": {"universeSource": value}}, "universeSource must be one of")


@pytest.mark.parametrize("limits,text", [
    ({"maxPositions": 0}, r"maxPositions must be in \[1, 100\]"),
    ({"maxPositions": 101}, r"maxPositions must be in \[1, 100\]"),
    ({"maxSectorPct": 0}, r"maxSectorPct must be in \[1, 100\]"),
    ({"maxSectorPct": 150}, r"maxSectorPct must be in \[1, 100\]"),
    ({"minPositionEur": -1}, r"minPositionEur must be in \[0, 100000\]"),
])
def test_open_universe_limits_are_range_checked(limits, text):
    bad({"paperTrading": {"limits": limits}}, text)


def test_good_cost_model_is_normalised_and_partial_keys_are_kept():
    cfg, _ = apply({"paperTrading": {"costModel": {"spreadBps": 5, "commissionEur": "1.5"}}})
    assert cfg["paperTrading"]["costModel"] == {"spreadBps": 5.0, "commissionEur": 1.5}
    cfg, _ = apply({"paperTrading": {"costModel": {"spreadBps": 500, "commissionEur": 50, "fxBps": 500}}})
    assert cfg["paperTrading"]["costModel"] == {"spreadBps": 500.0, "commissionEur": 50.0, "fxBps": 500.0}
    cfg, _ = apply({"paperTrading": {"costModel": {"spreadBps": 0, "commissionEur": 0, "fxBps": 0}}})
    assert cfg["paperTrading"]["costModel"]["spreadBps"] == 0.0


@pytest.mark.parametrize("cost,text", [
    ({"spreadBps": -1}, r"spreadBps must be in \[0, 500\]"),
    ({"spreadBps": 501}, r"spreadBps must be in \[0, 500\]"),
    ({"commissionEur": -0.01}, r"commissionEur must be in \[0, 50\]"),
    ({"commissionEur": 50.5}, r"commissionEur must be in \[0, 50\]"),
    ({"fxBps": 501}, r"fxBps must be in \[0, 500\]"),
    ({"fxBps": -5}, r"fxBps must be in \[0, 500\]"),
    ({"slippageBps": 1}, "unknown key"),
    ({"spreadBps": True}, "must be a number"),
    ({"spreadBps": "wide"}, "must be a number"),
    ([10, 0, 0], "costModel must be an object"),
])
def test_bad_cost_model_is_rejected(cost, text):
    bad({"paperTrading": {"costModel": cost}}, text)


# ------------------------------------------------- paperTrading.buckets (phase 5)

GOOD_INTRADAY = {"enabled": True, "mode": "off", "strategyVersion": "i1", "budgetEur": 1000,
                 "intervalMin": 60, "flatAtClose": False,
                 "limits": {"maxOrdersPerDay": 12, "killSwitch": {"dailyLossPct": 3}}}


def test_a_good_intraday_bucket_is_normalised():
    cfg, _ = apply({"paperTrading": {"buckets": {"intraday": {
        **GOOD_INTRADAY, "mode": " Shadow ", "strategyVersion": " i2 ", "budgetEur": "500",
        "intervalMin": 30.0, "flatAtClose": True,
        "limits": {"maxWeightPct": {"EQUITY": 40}, "minTradeEur": "25"}}}}})
    b = cfg["paperTrading"]["buckets"]["intraday"]
    assert b == {"enabled": True, "mode": "shadow", "strategyVersion": "i2", "budgetEur": 500.0,
                 "intervalMin": 30, "flatAtClose": True,
                 "limits": {"maxWeightPct": {"equity": 40.0}, "minTradeEur": 25.0}}
    assert isinstance(b["intervalMin"], int)


def test_all_bucket_keys_are_optional_and_an_empty_block_is_valid():
    cfg, _ = apply({"paperTrading": {"buckets": {}}})
    assert cfg["paperTrading"]["buckets"] == {}
    cfg, _ = apply({"paperTrading": {"buckets": {"intraday": {}}}})
    assert cfg["paperTrading"]["buckets"] == {"intraday": {}}


def test_the_daily_keys_survive_next_to_the_buckets():
    cfg, _ = apply({"paperTrading": {"mode": "shadow", "budgetEur": 800,
                                     "buckets": {"intraday": GOOD_INTRADAY}}})
    pt = cfg["paperTrading"]
    assert pt["mode"] == "shadow" and pt["budgetEur"] == 800.0 and "intraday" in pt["buckets"]


@pytest.mark.parametrize("buckets,text", [
    ("nope", "paperTrading.buckets must be an object"),
    ([GOOD_INTRADAY], "paperTrading.buckets must be an object"),
    ({"daily": {"enabled": True}}, r"unknown bucket\(s\) daily"),
    ({"weekly": {}}, r"unknown bucket\(s\) weekly .*only intraday"),
    ({"intraday": GOOD_INTRADAY, "swing": {}}, r"unknown bucket\(s\) swing"),
    ({"intraday": "on"}, "buckets.intraday must be an object"),
    ({"intraday": {"universe": {"ASML": {"t212": "X"}}}}, "unknown key.*universe.*shared"),
    ({"intraday": {"schedule": {}}}, r"unknown key\(s\) schedule"),
    ({"intraday": {"enabled": "yes"}}, "enabled must be true or false"),
    ({"intraday": {"enabled": 1}}, "enabled must be true or false"),
    ({"intraday": {"flatAtClose": "no"}}, "flatAtClose must be true or false"),
    ({"intraday": {"mode": 3}}, "mode must be a string"),
    ({"intraday": {"mode": "paper"}}, "mode must be one of off, shadow, demo"),
    ({"intraday": {"mode": "live-auto"}}, "not available yet"),
    ({"intraday": {"strategyVersion": ""}}, "strategyVersion must be a non-empty string"),
    ({"intraday": {"strategyVersion": "x" * 33}}, "<= 32 characters"),
    ({"intraday": {"budgetEur": 5}}, r"budgetEur must be in \[10, 100000\]"),
    ({"intraday": {"budgetEur": True}}, "budgetEur must be a number"),
    ({"intraday": {"intervalMin": 45}}, "intervalMin must be one of 15, 30, 60, 120"),
    ({"intraday": {"intervalMin": "soon"}}, "intervalMin must be a number"),
    ({"intraday": {"limits": "wide"}}, "paperTrading.limits must be an object"),
    ({"intraday": {"limits": {"maxOrdersPerDay": 0}}}, r"maxOrdersPerDay must be in \[1, 100\]"),
    ({"intraday": {"limits": {"bogus": 1}}}, r"limits: unknown key\(s\) bogus"),
])
def test_bad_bucket_blocks_are_rejected(buckets, text):
    bad({"paperTrading": {"buckets": buckets}}, text)


@pytest.mark.parametrize("interval", [15, 30, 60, 120])
def test_the_allowed_intervals(interval):
    cfg, _ = apply({"paperTrading": {"buckets": {"intraday": {"intervalMin": interval}}}})
    assert cfg["paperTrading"]["buckets"]["intraday"]["intervalMin"] == interval


def test_a_rejected_bucket_edit_leaves_the_config_untouched():
    cfg = copy.deepcopy(SEED)
    with pytest.raises(config_edit.ConfigInvalid):
        config_edit.apply_update(cfg, {"paperTrading": {"buckets": {"intraday": {"intervalMin": 7}}}})
    assert cfg["paperTrading"] == SEED["paperTrading"]
