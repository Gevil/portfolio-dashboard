"""US lead: seed `us` symbols, Finnhub subscription set, live_ws.us_lead, the risk guard and the
intraday pack field. No network, no sleeps: ticks / forex / quotes are injected, `now` is explicit."""
import asyncio
import datetime
import json

import pytest

from app.api import jsonstore, live_ws, paper_pm as pm, paper_risk as pr, paper_universe as pu, prices

# 2026-10-21 is a Wednesday; Berlin is on CEST (UTC+2), New York on EDT (UTC-4).
OVERLAP = datetime.datetime(2026, 10, 21, 14, 0, tzinfo=datetime.timezone.utc).timestamp()      # 16:00 / 10:00
XETR_CLOSED = datetime.datetime(2026, 10, 21, 16, 0, tzinfo=datetime.timezone.utc).timestamp()  # 18:00 / 12:00
US_CLOSED = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc).timestamp()    # 14:00 / 08:00
SATURDAY = datetime.datetime(2026, 10, 24, 14, 0, tzinfo=datetime.timezone.utc).timestamp()


def row(id_, isin, us=None, role="candidate", t212=None, yahoo=None):
    r = {"id": id_, "name": id_, "isin": isin, "yahoo": yahoo or f"{id_}.DE", "venue": "XETR",
         "kind": "equity", "sector": "IT", "role": role, "t212": t212}
    if us is not None:
        r["us"] = us
    return r


# ------------------------------------------------------------------ seed / registry

def test_seed_us_field_matches_the_isin_prefix_for_every_row():
    doc = pu._clean_doc(jsonstore.load(pu.SEED_FILE))
    us_isin = {r["id"] for r in doc["entries"] if r["isin"].startswith("US")}
    with_us = {r["id"] for r in doc["entries"] if r.get("us")}
    assert us_isin and us_isin == with_us            # every US name has one, nothing else does
    by_id = {r["id"]: r["us"] for r in doc["entries"] if r.get("us")}
    assert by_id["NVDA"] == "NVDA" and by_id["BRKB"] == "BRK.B" and by_id["MRKUS"] == "MRK"


def test_clean_row_drops_us_for_a_non_us_isin_or_a_bad_symbol():
    assert pu._clean_row(row("SAP", "DE0007164600", us="SAP"))["isin"] == "DE0007164600"
    assert "us" not in pu._clean_row(row("SAP", "DE0007164600", us="SAP"))
    assert "us" not in pu._clean_row(row("X", "US0000000000", us="not a ticker!"))
    assert "us" not in pu._clean_row(row("X", "US0000000000", us=5))
    assert pu._clean_row(row("BRKB", "US0846707026", us=" brk.b "))["us"] == "BRK.B"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "STORE_FILE", tmp_path / "store.json")
    monkeypatch.setattr(pu, "SEED_FILE", tmp_path / "seed.json")
    monkeypatch.setitem(pu._load_cache, "sig", None)
    monkeypatch.setitem(pu._load_cache, "doc", None)
    monkeypatch.setattr(pu, "_quote_cache", {})
    yield tmp_path


def test_a_store_row_without_us_learns_it_from_the_seed_and_keeps_its_t212(store):
    (store / "seed.json").write_text(json.dumps({"entries": [row("NVDA", "US67066G1040", us="NVDA"),
                                                             row("SAP", "DE0007164600")]}))
    (store / "store.json").write_text(json.dumps({"entries": [row("NVDA", "US67066G1040", t212="NVDA_US_EQ")]}))
    assert pu.us_symbol("nvda") == "NVDA"
    assert pu.t212_ticker("NVDA") == "NVDA_US_EQ"
    assert pu.us_symbol("SAP") is None and pu.us_symbol("nope") is None
    assert pu.us_symbols() == {"NVDA": "NVDA"}


# ------------------------------------------------------------------ subscriptions

@pytest.fixture
def feeds(monkeypatch):
    live_ws.live_prices.clear()
    live_ws._parked.clear()
    live_ws._wire_owner.clear()
    monkeypatch.setattr(live_ws, "_desired", lambda p: {"ASML": "ASML"} if p == "finnhub" else {"ASML": "ASML"})
    monkeypatch.setattr(live_ws, "_universe_source", lambda: "paper_universe")
    rows = [row("AAA", "US1", us="AAA"), row("BBB", "US2", us="BBB", t212="BBB_US_EQ"),
            row("SAP", "DE1"), row("EUNL", "IE1", role="benchmark")]
    monkeypatch.setattr(pu, "entries", lambda: rows)
    yield rows
    live_ws.live_prices.clear()
    live_ws._wire_owner.clear()


def test_open_universe_adds_the_us_symbols_after_the_watchlist_mapped_names_first(feeds):
    got = live_ws._desired_finnhub()
    assert list(got) == ["ASML", "BBB", "AAA"]
    assert got == {"ASML": "ASML", "BBB": "BBB", "AAA": "AAA"}


def test_watchlist_source_subscribes_no_universe_symbol(feeds, monkeypatch):
    monkeypatch.setattr(live_ws, "_universe_source", lambda: "watchlist")
    assert live_ws._desired_finnhub() == {"ASML": "ASML"}


def test_subscriptions_are_capped_at_50_with_the_watchlist_first(feeds, monkeypatch):
    monkeypatch.setattr(live_ws, "_desired", lambda p: {f"W{i}": f"W{i}" for i in range(10)})
    monkeypatch.setattr(pu, "entries", lambda: [row(f"U{i}", "US1", us=f"U{i}") for i in range(60)])
    got = live_ws._desired_finnhub()
    assert len(got) == 50
    assert list(got)[:10] == [f"W{i}" for i in range(10)]      # watchlist untouched
    assert list(got)[10:12] == ["U0", "U1"]                    # then the universe in order
    monkeypatch.setattr(live_ws, "_desired", lambda p: {f"W{i}": f"W{i}" for i in range(70)})
    assert list(live_ws._desired_finnhub()) == [f"W{i}" for i in range(50)]   # even an over-long watchlist


def test_a_symbol_shared_with_the_watchlist_is_not_subscribed_twice(feeds, monkeypatch):
    monkeypatch.setattr(live_ws, "_desired", lambda p: {"AAA": "WL_AAA"})
    assert live_ws._desired_finnhub()["AAA"] == "WL_AAA"       # watchlist id wins
    assert len(live_ws._desired_finnhub()) == 2


def test_sync_starts_finnhub_with_the_merged_set_and_keeps_universe_ticks(feeds, monkeypatch):
    started = []

    async def fake_loop(sub_map):
        started.append(dict(sub_map))
        await asyncio.sleep(3600)

    monkeypatch.setattr(live_ws, "FINNHUB_API_KEY", "k")
    monkeypatch.setattr(live_ws, "TWELVE_DATA_API_KEY", "")
    monkeypatch.setattr(live_ws, "_finnhub_ws_loop", fake_loop)
    monkeypatch.setattr(live_ws, "_feed_tasks", {})
    live_ws.live_prices["AAA"] = {"price": 5.0, "ts": 1, "source": "finnhub", "currency": "USD"}
    live_ws.live_prices["GONE"] = {"price": 5.0, "ts": 1, "source": "finnhub", "currency": "USD"}

    async def go():
        await live_ws._sync_feeds_once()
        await asyncio.sleep(0)
        for _, t in live_ws._feed_tasks.values():
            t.cancel()
    asyncio.run(go())
    assert started == [{"ASML": "ASML", "BBB": "BBB", "AAA": "AAA"}]
    assert "AAA" in live_ws.live_prices and "GONE" not in live_ws.live_prices


# ------------------------------------------------------------------ us_lead

@pytest.fixture
def lead_env(store, monkeypatch):
    (store / "seed.json").write_text(json.dumps({"entries": [row("NVDA", "US67066G1040", us="NVDA"),
                                                             row("SAP", "DE0007164600")]}))
    live_ws.live_prices.clear()
    live_ws._wire_owner.clear()
    st = {"fx": {"rate": 0.9, "stale": False}}
    monkeypatch.setattr(live_ws, "_fx_rate", lambda: st["fx"])
    live_ws.live_prices["NVDA"] = {"price": 110.0, "ts": int(OVERLAP) - 30, "source": "finnhub", "currency": "USD"}
    pu._quote_cache["NVDA"] = ({"id": "NVDA", "price": 97.5, "asOf": int(OVERLAP) - 600, "currency": "EUR"},
                               OVERLAP)
    monkeypatch.setattr(pu, "_now", lambda: OVERLAP)
    yield st
    live_ws.live_prices.clear()


def test_us_lead_converts_the_us_price_and_reports_the_gap(lead_env):
    lead = live_ws.us_lead("NVDA", OVERLAP)
    assert lead == {"usPrice": 110.0, "eurusd": round(1 / 0.9, 5), "impliedEur": 99.0,
                    "xetraDelayedEur": 97.5, "gapPct": 1.538, "ageS": 30}


def test_us_lead_is_none_unless_both_sessions_are_open(lead_env):
    for t in (US_CLOSED, XETR_CLOSED, SATURDAY):
        assert live_ws.us_lead("NVDA", t) is None


@pytest.mark.parametrize("mutate", ["no_tick", "fx_stale", "fx_missing", "no_quote", "stale_quote"])
def test_us_lead_is_none_when_an_input_is_missing_or_stale(lead_env, mutate):
    if mutate == "no_tick":
        live_ws.live_prices.clear()
    elif mutate == "fx_stale":
        lead_env["fx"] = {"rate": 0.9, "stale": True}
    elif mutate == "fx_missing":
        lead_env["fx"] = {"rate": None, "stale": True}
    elif mutate == "no_quote":
        pu._quote_cache.clear()
    else:
        pu._quote_cache["NVDA"][0]["asOf"] = int(OVERLAP) - 10 * 86400
    assert live_ws.us_lead("NVDA", OVERLAP) is None


def test_us_lead_none_for_a_non_us_name_and_it_writes_nothing(lead_env):
    assert live_ws.us_lead("SAP", OVERLAP) is None and live_ws.us_lead("NOPE", OVERLAP) is None
    ticks, quotes = json.dumps(live_ws.live_prices, sort_keys=True), json.dumps(pu._quote_cache, sort_keys=True)
    live_ws.us_lead("NVDA", OVERLAP)
    assert json.dumps(live_ws.live_prices, sort_keys=True) == ticks
    assert json.dumps(pu._quote_cache, sort_keys=True) == quotes


def test_us_lead_finds_a_tick_stored_under_the_watchlist_id_of_a_shared_symbol(lead_env):
    live_ws.live_prices["WL_NVDA"] = live_ws.live_prices.pop("NVDA")
    assert live_ws.us_lead("NVDA", OVERLAP) is None
    live_ws._wire_owner["NVDA"] = "WL_NVDA"
    assert live_ws.us_lead("NVDA", OVERLAP)["usPrice"] == 110.0


def test_us_lead_is_none_when_the_xetra_quote_is_older_than_the_open_window(lead_env):
    pu._quote_cache["NVDA"][0]["asOf"] = int(OVERLAP) - prices.STALE_OPEN_S - 1
    assert live_ws.us_lead("NVDA", OVERLAP) is None
    pu._quote_cache["NVDA"][0]["asOf"] = int(OVERLAP) - prices.STALE_OPEN_S
    assert live_ws.us_lead("NVDA", OVERLAP) is not None


# ------------------------------------------------------------------ risk guard

LIM = {"maxWeightPct": {"equity": 40, "etf": 100}, "minCashPct": 2, "maxTurnoverPctPerDay": 100,
       "maxOrdersPerDay": 6, "minTradeEur": 25, "rebalanceBandPct": 3, "maxSpreadOrStaleS": 900,
       "limitSlippageBps": 30, "adjustFraction": 1.0}
NOW = 1_800_000_000.0


def plan(targets, holdings=None, **kw):
    ids = {*targets, *(holdings or {})}
    return pr.plan_orders(
        targets=targets, cash_pct=None, holdings=holdings or {}, cash_eur=1000.0,
        quotes={i: {"price": 100.0, "asOf": int(NOW - 10), "stale": False} for i in ids},
        universe={i: {"t212": f"{i}_EQ"} for i in ids}, kinds={i: "equity" for i in ids},
        limits=dict(LIM), budget_eur=1000.0, decimals={}, now=NOW,
        session_ok={i: True for i in ids}, orders_today=0, turnover_today_eur=0.0, **kw)


def reasons(res):
    return {s["id"]: s["reason"] for s in res["skipped"]}


@pytest.mark.parametrize("gap", [1.51, -1.51, 7.0])
def test_a_buy_is_skipped_on_a_fresh_gap_beyond_1_5_pct(gap):
    res = plan({"A": 30}, us_lead={"A": {"gapPct": gap, "ageS": 120}})
    assert reasons(res) == {"A": "us_lead_gap"} and res["intents"] == []


@pytest.mark.parametrize("lead", [
    {"gapPct": 1.5, "ageS": 10},            # the limit itself is allowed
    {"gapPct": -1.5, "ageS": 10},
    {"gapPct": 5.0, "ageS": 121},           # too old to trust
    {"gapPct": None, "ageS": 10},
    {"gapPct": 5.0},                        # no age -> never blocks
    "garbage",
])
def test_a_buy_goes_ahead_when_the_lead_is_small_old_or_malformed(lead):
    res = plan({"A": 30}, us_lead={"A": lead})
    assert [i.side for i in res["intents"]] == ["buy"] and "A" not in reasons(res)


def test_no_lead_for_the_name_or_none_at_all_never_blocks():
    assert plan({"A": 30}, us_lead={"B": {"gapPct": 9, "ageS": 1}})["intents"]
    assert plan({"A": 30}, us_lead=None)["intents"]
    assert plan({"A": 30})["intents"]


def test_the_guard_skips_only_the_buy_of_the_flagged_name_and_never_a_sell():
    lead = {"A": {"gapPct": 3.0, "ageS": 5}, "S": {"gapPct": 3.0, "ageS": 5}}
    res = plan({"A": 30, "B": 30, "S": 0}, holdings={"S": 5.0}, us_lead=lead)
    sides = {i.id: i.side for i in res["intents"]}
    assert sides == {"B": "buy", "S": "sell"}
    assert reasons(res) == {"A": "us_lead_gap"}
    assert res["targetsEffective"]["A"] == 0.0           # nothing was traded towards the target


# ------------------------------------------------------------------ intraday pack

def raw_for(bucket, lead):
    return {"cfg": {"universe": {"NVDA": {"name": "NVIDIA", "kind": "equity", "sector": "IT"},
                                 "SAP": {"name": "SAP", "kind": "equity", "sector": "IT"}},
                    "limits": {}},
            "now": OVERLAP, "today": datetime.date(2026, 10, 21), "bucket_id": bucket,
            "closes": {}, "f20": {}, "f5": {}, "quotes": {}, "last_prices": {}, "bars": {},
            "advice": {}, "us_lead": {"NVDA": lead, "SAP": lead}}


@pytest.fixture
def us_names(monkeypatch):
    monkeypatch.setattr(pu, "us_symbol", lambda pid: "NVDA" if pid == "NVDA" else None)


LEAD = {"usPrice": 110.0, "eurusd": 1.11111, "impliedEur": 99.0, "xetraDelayedEur": 97.5,
        "gapPct": 1.538, "ageS": 30}


def test_intraday_pack_carries_a_labelled_us_lead_for_us_names_only(us_names):
    nv = pm._open_entry(raw_for("intraday", LEAD), "NVDA", 100, True)
    assert nv["usLead"]["gapPct"] == 1.538 and nv["usLead"]["impliedEur"] == 99.0
    assert nv["usLead"]["label"].startswith("reference only")
    assert "usLead" not in pm._open_entry(raw_for("intraday", LEAD), "SAP", 100, True)


def test_intraday_pack_marks_a_missing_lead_and_the_daily_pack_has_none(us_names):
    assert pm._open_entry(raw_for("intraday", None), "NVDA", 100, True)["usLead"] == pm.MISSING
    assert "usLead" not in pm._open_entry(raw_for("daily", LEAD), "NVDA", 100, True)


def test_a_failing_lead_lookup_degrades_to_missing(monkeypatch):
    def boom(pid, now):
        raise RuntimeError("x")
    monkeypatch.setattr(live_ws, "us_lead", boom)
    assert pm._safe_us_lead("NVDA", OVERLAP) is None
