"""PUT /api/config validation + merge rules (pure; real registry schema).

Run inside the service image (see test_prices.py for the command).
"""
import copy
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
