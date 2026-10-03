"""Symbol registry: entry normalization, derived flags, provider mapping and
venue sessions (no network, no config file).

Run inside the service image (see test_prices.py for the command).
"""
import datetime

import pytest

from app.api import registry

UTC = datetime.timezone.utc

ASML = {
    "id": "asml", "kind": "equity", "role": "holding", "quoteCurrency": "EUR",
    "providers": {"yahoo": "ASML.AS", "twelvedata": "ASML", "finnhub": "ASML"},
    "listing": {"symbol": "ASML.AS", "venue": "XAMS", "currency": "EUR"},
    "custom": {"keep": "me"},
}
SXR8 = {
    "id": "SXR8", "kind": "etf",
    "providers": {"yahoo": "SXR8.DE", "twelvedata": None, "finnhub": None},
    "listing": {"symbol": "SXR8.DE", "venue": "XETR", "currency": "EUR"},
}
GSPC = {"id": "GSPC", "kind": "index", "providers": {"yahoo": "^GSPC"}}


@pytest.fixture
def wl(monkeypatch):
    state = {"raw": [dict(ASML), dict(SXR8), dict(GSPC)]}
    monkeypatch.setattr(registry, "_raw_watchlist", lambda: state["raw"])
    monkeypatch.setattr(registry, "_memo_raw", None)
    return state


def epoch(y, m, d, h, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=UTC).timestamp()


# ---------------------------------------------------------------- normalize

def test_equity_defaults_and_unknown_keys_preserved():
    e = registry.normalize_entry(ASML)
    assert e["id"] == "ASML" and e["symbol"] == "ASML" and e["label"] == "ASML"
    assert e["alertable"] is True and e["analyzeable"] is True
    assert e["custom"] == {"keep": "me"}          # superset of the input
    assert e["providers"] == {"yahoo": "ASML.AS", "twelvedata": "ASML", "finnhub": "ASML"}


def test_flags_derive_from_kind_and_can_be_overridden():
    etf = registry.normalize_entry(SXR8)
    assert (etf["alertable"], etf["analyzeable"]) == (True, False)
    idx = registry.normalize_entry(GSPC)
    assert (idx["alertable"], idx["analyzeable"]) == (False, False)
    assert idx["role"] == "benchmark" and idx["quoteCurrency"] == "USD"
    forced = registry.normalize_entry({**SXR8, "analyzeable": True, "alertable": False})
    assert (forced["alertable"], forced["analyzeable"]) == (False, True)


def test_missing_provider_keys_are_unsupported_not_raw_ids():
    idx = registry.normalize_entry(GSPC)
    assert idx["providers"] == {"yahoo": "^GSPC", "twelvedata": None, "finnhub": None}
    # benchmark listing is derived from the yahoo symbol
    assert idx["listing"] == {"symbol": "^GSPC", "venue": "XNYS", "currency": "USD"}


@pytest.mark.parametrize("bad", [
    {},                                                   # no id
    {"id": "X", "kind": "crypto"},
    {"id": "X", "role": "owner"},
    {"id": "X", "quoteCurrency": "GBP", "listing": {"symbol": "X", "venue": "XAMS"}},
    {"id": "X", "providers": {"bing": "X"}, "listing": {"symbol": "X", "venue": "XAMS"}},
    {"id": "X", "providers": ["yahoo"], "listing": {"symbol": "X", "venue": "XAMS"}},
    {"id": "X"},                                          # holding needs a listing
    {"id": "X", "listing": {"symbol": "X", "venue": "NOPE"}},
    {"id": "X", "listing": {"symbol": "X", "venue": "XAMS", "currency": "USD"}},
    {"id": "X", "alertable": "yes", "listing": {"symbol": "X", "venue": "XAMS"}},
    {"id": "^SPX", "listing": {"symbol": "X", "venue": "XAMS"}},
])
def test_invalid_entries_raise_readable_value_error(bad):
    with pytest.raises(ValueError) as exc:
        registry.normalize_entry(bad)
    assert str(exc.value)


def test_validate_entries_rejects_duplicates_with_index():
    with pytest.raises(ValueError, match=r"watchlist\[1\].*duplicate"):
        registry.validate_entries([ASML, {**ASML, "id": "ASML"}])


# ---------------------------------------------------------------- lookups

def test_lookup_helpers(wl):
    assert registry.ids() == ["ASML", "SXR8", "GSPC"]
    assert registry.holdings() == ["ASML", "SXR8"]
    assert registry.benchmark_id() == "GSPC"
    assert registry.is_benchmark("gspc") and not registry.is_benchmark("ASML")
    assert registry.kind("SXR8") == "etf" and registry.role("ASML") == "holding"
    assert registry.is_alertable("ASML") and not registry.is_alertable("GSPC")
    assert registry.is_analyzable("ASML") and not registry.is_analyzable("SXR8")
    assert registry.listing_symbol("ASML") == "ASML.AS"
    assert registry.listing_currency("GSPC") == "USD"
    assert registry.is_eu("ASML") and not registry.is_eu("GSPC")


def test_unknown_id_is_nothing(wl):
    assert registry.get("NOPE") is None
    assert not registry.is_watched("NOPE")
    assert not registry.is_alertable("NOPE") and not registry.is_analyzable("NOPE")
    assert registry.provider_symbol("NOPE", "yahoo") is None
    assert registry.listing("NOPE") is None and not registry.market_open("NOPE")


def test_provider_symbol_never_falls_back_to_the_raw_id(wl):
    assert registry.provider_symbol("ASML", "yahoo") == "ASML.AS"
    assert registry.provider_symbol("ASML", "twelvedata") == "ASML"
    assert registry.provider_symbol("GSPC", "twelvedata") is None
    assert registry.provider_symbol("GSPC", "finnhub") is None
    assert registry.provider_symbol("SXR8", "twelvedata") is None
    assert registry.provider_symbol("ASML", "bing") is None


def test_entries_are_copies_and_follow_config_changes(wl):
    registry.entries()[0]["label"] = "mutated"
    assert registry.get("ASML")["label"] == "ASML"
    wl["raw"] = [dict(GSPC)]                      # config edit, no restart
    assert registry.ids() == ["GSPC"]
    assert not registry.is_watched("ASML")


def test_bad_entry_is_skipped_not_fatal(wl):
    wl["raw"] = [{"id": "BAD", "kind": "nonsense"}, dict(GSPC)]
    assert registry.ids() == ["GSPC"]


# ---------------------------------------------------------------- sessions

def test_market_open_follows_the_listing_venue_and_dst(wl):
    # Mon 2026-07-13: Amsterdam is CEST (UTC+2): 09:00 local = 07:00 UTC
    assert registry.market_open("ASML", epoch(2026, 7, 13, 7, 0))
    assert not registry.market_open("ASML", epoch(2026, 7, 13, 6, 59))
    assert registry.market_open("ASML", epoch(2026, 7, 13, 15, 30))   # 17:30 local
    assert not registry.market_open("ASML", epoch(2026, 7, 13, 15, 31))
    # winter (CET, UTC+1): 09:00 local = 08:00 UTC
    assert registry.market_open("ASML", epoch(2026, 1, 12, 8, 0))
    assert not registry.market_open("ASML", epoch(2026, 1, 12, 7, 59))
    # the same instant is a different session in New York: 10:00 UTC is 06:00 EDT
    assert not registry.market_open("GSPC", epoch(2026, 7, 13, 10, 0))
    assert registry.market_open("GSPC", epoch(2026, 7, 13, 13, 30))   # 09:30 EDT
    assert registry.market_open("ASML", epoch(2026, 7, 13, 13, 30))   # EU still open


def test_weekend_is_closed(wl):
    assert not registry.market_open("ASML", epoch(2026, 7, 11, 10, 0))   # Saturday
    assert not registry.market_open("GSPC", epoch(2026, 7, 12, 15, 0))   # Sunday


def test_session_window_latest_started_session(wl):
    mon_open = int(epoch(2026, 7, 13, 7, 0))
    mon_close = int(epoch(2026, 7, 13, 15, 30))
    fri_open = int(epoch(2026, 7, 10, 7, 0))
    fri_close = int(epoch(2026, 7, 10, 15, 30))
    assert registry.session_window("ASML", epoch(2026, 7, 13, 10, 0)) == (mon_open, mon_close)
    assert registry.session_window("ASML", epoch(2026, 7, 13, 20, 0)) == (mon_open, mon_close)
    # before the open and on the weekend the previous weekday's session is the latest
    assert registry.session_window("ASML", epoch(2026, 7, 13, 6, 0)) == (fri_open, fri_close)
    assert registry.session_window("ASML", epoch(2026, 7, 11, 12, 0)) == (fri_open, fri_close)
    assert registry.session_window("NOPE", epoch(2026, 7, 13, 12, 0)) is None
