"""Paper-trading candidate universe (no network: yahoo is faked, the clock is injected).

Run inside the service image so dependencies resolve:
  podman run --rm -v $PWD:/work -w /work -e HISTORY_DIR=/tmp/t localhost/portfolio-dashboard:latest \
    bash -c "pip install -q pytest && PYTHONPATH=/work python -m pytest tests/unit/test_paper_universe.py -q"
"""
import asyncio
import datetime
import json
import pathlib

import pytest

from app.api import broker, jsonstore, paper_universe as pu, prices

ROWS = [
    {"id": "SAP", "name": "SAP", "isin": "DE0007164600", "yahoo": "SAP.DE", "venue": "XETR",
     "kind": "equity", "sector": "Information Technology", "t212": None},
    {"id": "ASML", "name": "ASML Holding", "isin": "NL0010273215", "yahoo": "ASML.AS", "venue": "XAMS",
     "kind": "equity", "sector": "Information Technology", "t212": None},
    {"id": "ADYEN", "name": "Adyen", "isin": "NL0012969182", "yahoo": "ADYEN.AS", "venue": "XAMS",
     "kind": "equity", "sector": "Financials", "t212": None},
    {"id": "NVDA", "name": "NVIDIA", "isin": "US67066G1040", "yahoo": "NVD.DE", "venue": "XETR",
     "kind": "equity", "sector": "Information Technology", "t212": None},
    {"id": "EUNL", "name": "iShares Core MSCI World", "isin": "IE00B4L5Y983", "yahoo": "EUNL.DE",
     "venue": "XETR", "kind": "etf", "sector": "ETF", "role": "benchmark", "t212": None},
]


def run(coro):
    return asyncio.run(coro)


def ts(y, m, d, h=0, mi=0):
    return int(datetime.datetime(y, m, d, h, mi, tzinfo=datetime.timezone.utc).timestamp())


def inst(ticker, isin, currency="EUR", schedule_id=None):
    return broker.Instrument(ticker=ticker, isin=isin, name=None, currency=currency, type="STOCK",
                             short_name=None, schedule_id=schedule_id)


class FakeYF:
    """Stand-in for prices._yf: answers by the blocking function that is asked."""

    def __init__(self):
        self.calls = []
        self.daily = {}     # (symbol, period) -> rows | None | Exception
        self.bars = {}      # (symbol, interval, period) -> bars | None | Exception

    async def __call__(self, key, fn, *args):
        self.calls.append((fn.__name__, args))
        if fn is prices._yf_daily_blocking:
            val = self.daily.get((args[0], args[1]))
        elif fn is prices._yf_bars_blocking:
            val = self.bars.get((args[0], args[1], args[2]))
        else:
            raise AssertionError(f"unexpected yahoo call {fn.__name__}")
        if isinstance(val, Exception):
            raise val
        return val


@pytest.fixture
def env(tmp_path, monkeypatch):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"asOf": "2026-10-21", "source": "test", "entries": ROWS}))
    clock = {"t": float(ts(2026, 10, 21, 8))}
    yf = FakeYF()
    monkeypatch.setattr(pu, "STORE_FILE", tmp_path / "paper_universe.json")
    monkeypatch.setattr(pu, "SEED_FILE", seed)
    monkeypatch.setattr(pu, "_closes_cache", {})
    monkeypatch.setattr(pu, "_quote_cache", {})
    monkeypatch.setattr(pu, "_intraday_cache", {})
    monkeypatch.setattr(pu, "_load_cache", {"sig": None, "doc": None})
    monkeypatch.setattr(pu, "_now", lambda: clock["t"])
    monkeypatch.setattr(prices, "_yf", yf)
    return type("Env", (), {"store": tmp_path / "paper_universe.json", "seed": seed,
                            "clock": clock, "yf": yf, "tmp": tmp_path})


# ------------------------------------------------------------------ load / fallback

def test_seed_is_used_when_no_store(env):
    assert pu.ids() == ["SAP", "ASML", "ADYEN", "NVDA", "EUNL"]
    assert [e["id"] for e in pu.entries()] == pu.ids()
    sap = pu.get("SAP")
    assert sap["role"] == "candidate"          # missing role defaults to candidate
    assert sap["yahoo"] == "SAP.DE" and sap["t212"] is None
    assert pu.get("EUNL")["role"] == "benchmark"


def test_store_wins_per_id_and_seed_rows_missing_from_it_are_merged(env):
    stored = {**ROWS[0], "t212": "SAPd_EQ", "sector": "stored sector"}
    env.store.write_text(json.dumps({"entries": [stored]}))
    # the store's SAP row wins; the seed's other rows follow in seed order
    assert pu.ids() == ["SAP", "ASML", "ADYEN", "NVDA", "EUNL"]
    assert pu.get("SAP")["t212"] == "SAPd_EQ" and pu.get("SAP")["sector"] == "stored sector"
    jsonstore.save(env.store, {"entries": [ROWS[1], ROWS[0]]})
    assert pu.ids() == ["ASML", "SAP", "ADYEN", "NVDA", "EUNL"]    # follows file changes


def test_a_seed_row_added_later_appears_without_touching_the_store(env):
    env.store.write_text(json.dumps({"entries": [ROWS[0]]}))
    assert "NEWCO" not in pu.ids()
    seed = json.loads(env.seed.read_text())
    seed["entries"].append({**ROWS[1], "id": "NEWCO", "yahoo": "NEW.DE"})
    env.seed.write_text(json.dumps(seed))
    assert pu.ids()[-1] == "NEWCO" and pu.get("NEWCO")["yahoo"] == "NEW.DE"


@pytest.mark.parametrize("content", ["{not json", "", '{"entries": []}', '{"entries": "x"}', "[]",
                                     '{"entries": [{"id": "X"}]}'])
def test_unusable_store_falls_back_to_seed(env, content):
    env.store.write_text(content)
    assert pu.ids() == ["SAP", "ASML", "ADYEN", "NVDA", "EUNL"]


def test_missing_store_and_seed_gives_empty_universe(env):
    env.seed.unlink()
    assert pu.entries() == [] and pu.ids() == [] and pu.get("SAP") is None


def test_bad_rows_are_dropped_and_first_duplicate_wins(env):
    good = dict(ROWS[0])
    rows = [
        good,
        {**ROWS[1], "id": "SAP", "yahoo": "OTHER.AS"},           # duplicate id
        {**ROWS[1], "id": "NOVENUE", "venue": "XNYS"},           # venue outside the five
        {**ROWS[1], "id": "NOKIND", "kind": "index"},
        {**ROWS[1], "id": "NOROLE", "role": "holding"},
        {**ROWS[1], "id": "NOSYM", "yahoo": ""},
        "garbage",
        {**ROWS[1], "id": " asml ", "t212": "  "},               # id normalised, blank t212 -> None
    ]
    env.seed.write_text(json.dumps({"entries": []}))        # an empty seed adds nothing
    env.store.write_text(json.dumps({"entries": rows}))
    assert pu.ids() == ["SAP", "ASML"]
    assert pu.get("SAP")["yahoo"] == "SAP.DE"
    assert pu.get("ASML")["t212"] is None


# ------------------------------------------------------------------ lookups

def test_ids_by_role(env):
    assert pu.ids("candidate") == ["SAP", "ASML", "ADYEN", "NVDA"]
    assert pu.ids("benchmark") == ["EUNL"]
    assert pu.ids(None) == pu.ids()
    assert pu.ids("holding") == []


def test_get_is_case_insensitive_and_returns_copies(env):
    row = pu.get(" sap ")
    assert row["id"] == "SAP"
    row["yahoo"] = "HACKED"
    pu.entries()[0]["yahoo"] = "HACKED"
    assert pu.get("SAP")["yahoo"] == "SAP.DE"
    assert pu.get("NOPE") is None and pu.get("") is None and pu.get(None) is None


def test_t212_ticker(env):
    assert pu.t212_ticker("SAP") is None
    assert pu.t212_ticker("NOPE") is None
    env.store.write_text(json.dumps({"entries": [{**ROWS[0], "t212": "SAPd_EQ"}]}))
    assert pu.t212_ticker("sap") == "SAPd_EQ"


# ------------------------------------------------------------------ daily closes

def test_daily_closes_fetches_by_yahoo_symbol_of_a_non_watchlist_id(env):
    rows = [("2026-10-19", 100.0), ("2026-10-20", 101.5)]
    env.yf.daily[("NVD.DE", "1y")] = rows
    before = (dict(prices._daily_closes_cache), dict(prices.history_store))
    assert run(pu.daily_closes("nvda", "1y")) == rows
    assert env.yf.calls == [("_yf_daily_blocking", ("NVD.DE", "1y"))]
    # prices' own caches / stores are untouched
    assert (dict(prices._daily_closes_cache), dict(prices.history_store)) == before


def test_daily_closes_is_cached_for_six_hours_per_id_and_period(env):
    env.yf.daily[("SAP.DE", "2y")] = [("2026-10-20", 200.0)]
    env.yf.daily[("SAP.DE", "5y")] = [("2026-10-20", 200.0), ("2021-10-20", 150.0)]
    assert run(pu.daily_closes("SAP", "2y")) == [("2026-10-20", 200.0)]
    env.clock["t"] += 6 * 3600 - 1
    run(pu.daily_closes("SAP", "2y"))
    assert len(env.yf.calls) == 1
    run(pu.daily_closes("SAP", "5y"))          # another period is its own entry
    assert len(env.yf.calls) == 2
    env.clock["t"] += 2
    run(pu.daily_closes("SAP", "2y"))          # 6 h elapsed: refetch
    assert len(env.yf.calls) == 3


def test_daily_closes_cached_value_is_a_copy(env):
    env.yf.daily[("SAP.DE", "2y")] = [("2026-10-20", 200.0)]
    first = run(pu.daily_closes("SAP", "2y"))
    first.append(("x", 1.0))
    assert run(pu.daily_closes("SAP", "2y")) == [("2026-10-20", 200.0)]


def test_daily_closes_failure_serves_last_cached_rows_else_empty(env):
    sym = ("SAP.DE", "2y")
    env.yf.daily[sym] = None                   # yahoo failed / backing off
    assert run(pu.daily_closes("SAP", "2y")) == []
    env.yf.daily[sym] = [("2026-10-20", 200.0)]
    assert run(pu.daily_closes("SAP", "2y")) == [("2026-10-20", 200.0)]
    env.clock["t"] += 7 * 3600
    env.yf.daily[sym] = []                     # expired entry + empty answer
    assert run(pu.daily_closes("SAP", "2y")) == [("2026-10-20", 200.0)]


def test_daily_closes_never_raises(env):
    env.yf.daily[("SAP.DE", "2y")] = RuntimeError("boom")
    assert run(pu.daily_closes("SAP", "2y")) == []
    n = len(env.yf.calls)
    # unknown id, unsupported period and garbage ids ask yahoo for nothing
    assert run(pu.daily_closes("NOPE", "2y")) == []
    assert run(pu.daily_closes("SAP", "3d")) == []
    assert run(pu.daily_closes(None, "2y")) == []
    assert len(env.yf.calls) == n


# ------------------------------------------------------------------ quote

BAR_T = ts(2026, 10, 20, 15, 0)          # Tue 17:00 CEST, inside the Xetra session


def test_listing_quote_by_yahoo_symbol_with_prev_close_from_the_previous_local_date(env):
    env.yf.bars[("NVD.DE", "1m", "1d")] = [{"t": BAR_T - 60, "c": 103.0}, {"t": BAR_T, "c": 104.0}]
    env.yf.daily[("NVD.DE", "5d")] = [("2026-10-16", 98.0), ("2026-10-19", 100.0), ("2026-10-20", 103.5)]
    env.clock["t"] = BAR_T + 120
    q = run(pu.listing_quote("NVDA"))
    assert q["id"] == "NVDA" and q["symbol"] == "NVD.DE" and q["venue"] == "XETR"
    assert q["price"] == 104.0 and q["currency"] == "EUR" and q["asOf"] == BAR_T and q["source"] == "yahoo"
    assert q["prevClose"] == 100.0                 # today's own daily row is never the previous close
    assert q["change"] == pytest.approx(4.0) and q["changePct"] == pytest.approx(4.0)
    assert q["stale"] is False


def test_listing_quote_without_a_previous_row_has_no_change(env):
    env.yf.bars[("NVD.DE", "1m", "1d")] = [{"t": BAR_T, "c": 104.0}]
    env.yf.daily[("NVD.DE", "5d")] = [("2026-10-20", 103.5)]
    env.clock["t"] = BAR_T + 60
    q = run(pu.listing_quote("NVDA"))
    assert q["price"] == 104.0
    assert q["prevClose"] is None and q["change"] is None and q["changePct"] is None


def test_listing_quote_uses_the_venue_calendar_date_for_prev_close(env):
    # 23:30 UTC on the 20th is 01:30 CEST on the 21st: that bar belongs to the 21st locally
    late = ts(2026, 10, 20, 23, 30)
    env.yf.bars[("ASML.AS", "1m", "1d")] = [{"t": late, "c": 700.0}]
    env.yf.daily[("ASML.AS", "5d")] = [("2026-10-20", 690.0), ("2026-10-21", 695.0)]
    env.clock["t"] = late + 60
    q = run(pu.listing_quote("ASML"))
    assert q["prevClose"] == 690.0                 # bar belongs to the 21st locally


def test_listing_quote_falls_back_to_5d_15m_bars(env):
    env.yf.bars[("SAP.DE", "1m", "1d")] = []
    env.yf.bars[("SAP.DE", "15m", "5d")] = [{"t": BAR_T, "c": 201.0}]
    env.yf.daily[("SAP.DE", "5d")] = [("2026-10-19", 200.0)]
    env.clock["t"] = BAR_T + 60
    q = run(pu.listing_quote("SAP"))
    assert q["price"] == 201.0 and q["prevClose"] == 200.0
    assert [c[1][1:] for c in env.yf.calls if c[0] == "_yf_bars_blocking"] == [("1m", "1d"), ("15m", "5d")]


def test_listing_quote_is_cached_and_served_stale_when_the_refresh_fails(env):
    env.yf.bars[("SAP.DE", "1m", "1d")] = [{"t": BAR_T, "c": 201.0}]
    env.yf.daily[("SAP.DE", "5d")] = [("2026-10-19", 200.0)]
    env.clock["t"] = BAR_T + 60
    run(pu.listing_quote("SAP"))
    n = len(env.yf.calls)
    env.clock["t"] += pu.QUOTE_TTL_S - 5
    assert run(pu.listing_quote("SAP"))["price"] == 201.0
    assert len(env.yf.calls) == n                  # inside the TTL: no fetch
    env.clock["t"] += 10
    env.yf.bars[("SAP.DE", "1m", "1d")] = None     # yahoo failing
    q = run(pu.listing_quote("SAP"))
    assert len(env.yf.calls) > n
    assert q["price"] == 201.0 and q["stale"] is True


def test_listing_quote_marks_an_old_bar_stale(env):
    env.yf.bars[("SAP.DE", "1m", "1d")] = [{"t": BAR_T, "c": 201.0}]
    env.clock["t"] = BAR_T + pu.QUOTE_STALE_S + 1
    assert run(pu.listing_quote("SAP"))["stale"] is True


def test_listing_quote_never_raises(env):
    assert run(pu.listing_quote("SAP")) is None            # nothing cached, yahoo has no data
    env.yf.bars[("SAP.DE", "1m", "1d")] = RuntimeError("boom")
    assert run(pu.listing_quote("SAP")) is None
    n = len(env.yf.calls)
    assert run(pu.listing_quote("NOPE")) is None and run(pu.listing_quote(None)) is None
    assert len(env.yf.calls) == n


def test_listing_quote_survives_a_failing_prev_close(env):
    env.yf.bars[("SAP.DE", "1m", "1d")] = [{"t": BAR_T, "c": 201.0}]
    env.yf.daily[("SAP.DE", "5d")] = None
    env.clock["t"] = BAR_T + 60
    q = run(pu.listing_quote("SAP"))
    assert q["price"] == 201.0 and q["prevClose"] is None


# ------------------------------------------------------------------ intraday_bars

def _bar(day, h, m, c):
    return {"t": ts(2026, 10, day, h, m), "c": c}


def test_intraday_bars_keep_only_todays_session_by_the_venue_date(env):
    env.yf.bars[("SAP.DE", "5m", "1d")] = [
        _bar(20, 15, 25, 99.0),                    # yesterday's last bar
        _bar(21, 7, 0, 100.0), _bar(21, 7, 5, 101.0)]
    out = run(pu.intraday_bars("sap"))
    assert [b["c"] for b in out] == [100.0, 101.0]
    assert env.yf.calls == [("_yf_bars_blocking", ("SAP.DE", "5m", "1d"))]
    out.append("mutated")
    assert len(run(pu.intraday_bars("SAP"))) == 2          # callers get copies


def test_intraday_bars_are_cached_for_five_minutes(env):
    env.yf.bars[("SAP.DE", "5m", "1d")] = [_bar(21, 7, 0, 100.0)]
    run(pu.intraday_bars("SAP"))
    env.clock["t"] += pu.INTRADAY_TTL_S - 1
    run(pu.intraday_bars("SAP"))
    assert len(env.yf.calls) == 1
    env.yf.bars[("SAP.DE", "5m", "1d")] = [_bar(21, 7, 0, 100.0), _bar(21, 7, 5, 102.0)]
    env.clock["t"] += 2
    assert len(run(pu.intraday_bars("SAP"))) == 2 and len(env.yf.calls) == 2


def test_intraday_bars_failure_serves_the_cache_and_never_raises(env):
    assert run(pu.intraday_bars("SAP")) == []              # yahoo has no data (None), nothing cached
    env.yf.bars[("SAP.DE", "5m", "1d")] = [_bar(21, 7, 0, 100.0)]
    assert len(run(pu.intraday_bars("SAP"))) == 1
    env.clock["t"] += pu.INTRADAY_TTL_S + 1
    env.yf.bars[("SAP.DE", "5m", "1d")] = RuntimeError("boom")
    assert [b["c"] for b in run(pu.intraday_bars("SAP"))] == [100.0]
    env.yf.bars[("SAP.DE", "5m", "1d")] = None             # backoff
    assert len(run(pu.intraday_bars("SAP"))) == 1


def test_intraday_bars_a_stale_cache_from_yesterday_is_not_served_as_today(env):
    env.yf.bars[("SAP.DE", "5m", "1d")] = [_bar(21, 7, 0, 100.0)]
    run(pu.intraday_bars("SAP"))
    env.clock["t"] += 86400                                 # next day, yahoo down
    env.yf.bars[("SAP.DE", "5m", "1d")] = None
    assert run(pu.intraday_bars("SAP")) == []


def test_intraday_bars_unknown_id_or_interval_or_no_session_bars_is_empty(env):
    n = len(env.yf.calls)
    assert run(pu.intraday_bars("NOPE")) == [] and run(pu.intraday_bars(None)) == []
    assert run(pu.intraday_bars("SAP", "1h")) == [] and len(env.yf.calls) == n
    env.yf.bars[("SAP.DE", "5m", "1d")] = [_bar(20, 15, 25, 99.0)]       # only yesterday's bars (pre-open)
    assert run(pu.intraday_bars("SAP")) == []


# ------------------------------------------------------------------ map_t212

def test_map_t212_matches_isin_and_eur_only(env):
    res = run(pu.map_t212([
        inst("SAPd_EQ", "DE0007164600"),
        inst("SAP_US_EQ", "DE0007164600", currency="USD"),     # same ISIN, wrong currency
        inst("NVDd_EQ", "US67066G1040"),
        inst("NVDA_US_EQ", "US67066G1040", currency="USD"),
        inst("NOISIN_EQ", None),
        inst("OTHERd_EQ", "DE000UNKNOWN0"),
    ]))
    assert res["mapped"] == {"SAP": "SAPd_EQ", "NVDA": "NVDd_EQ"}
    assert res["unmapped"] == ["ASML", "ADYEN", "EUNL"]
    assert res["saved"] is True


def test_map_t212_never_maps_a_foreign_currency_listing(env):
    res = run(pu.map_t212([inst("SAP_US_EQ", "DE0007164600", currency="USD"),
                           inst("SAPl_EQ", "DE0007164600", currency="GBX")]))
    assert "SAP" in res["unmapped"] and res["mapped"] == {}


def test_map_t212_prefers_the_listing_of_the_entry_venue(env):
    both = [inst("ASMLd_EQ", "NL0010273215"), inst("ASMLa_EQ", "NL0010273215"),
            inst("NVDa_EQ", "US67066G1040"), inst("NVDd_EQ", "US67066G1040")]
    for order in (both, list(reversed(both))):
        res = run(pu.map_t212(order))
        assert res["mapped"]["ASML"] == "ASMLa_EQ"           # XAMS entry -> Amsterdam listing
        assert res["mapped"]["NVDA"] == "NVDd_EQ"            # XETR entry -> Xetra listing


def test_map_t212_venue_match_is_a_preference_not_a_requirement(env):
    res = run(pu.map_t212([inst("ASMLd_EQ", "NL0010273215")]))
    assert res["mapped"] == {"ASML": "ASMLd_EQ"}


def test_map_t212_learns_the_exchange_from_schedule_ids_when_the_suffix_is_no_hint(env):
    # letters x/y mean nothing, but schedule 7 is shared with the unambiguous Amsterdam listing of ADYEN
    instruments = [inst("ADYENa_EQ", "NL0012969182", schedule_id=7),
                   inst("ASMLy_EQ", "NL0010273215", schedule_id=8),
                   inst("ASMLx_EQ", "NL0010273215", schedule_id=7),
                   inst("SAPd_EQ", "DE0007164600", schedule_id=8)]
    for order in (instruments, list(reversed(instruments))):
        assert run(pu.map_t212(order))["mapped"]["ASML"] == "ASMLx_EQ"


def test_map_t212_equal_rank_listings_pick_deterministically(env):
    a, b = inst("ASMLx_EQ", "NL0010273215"), inst("ASMLy_EQ", "NL0010273215")
    assert run(pu.map_t212([b, a]))["mapped"]["ASML"] == "ASMLx_EQ"
    assert run(pu.map_t212([a, b]))["mapped"]["ASML"] == "ASMLx_EQ"


def test_map_t212_persists_to_the_store_and_leaves_the_seed_alone(env):
    seed_before = env.seed.read_bytes()
    run(pu.map_t212([inst("SAPd_EQ", "DE0007164600")]))
    assert env.seed.read_bytes() == seed_before
    stored = json.loads(env.store.read_text())
    assert {r["id"]: r["t212"] for r in stored["entries"]}["SAP"] == "SAPd_EQ"
    assert stored["source"] == "test"                        # top-level metadata survives
    assert pu.t212_ticker("SAP") == "SAPd_EQ"
    assert pu.get("EUNL")["role"] == "benchmark"             # roles survive the rewrite


def test_map_t212_remaps_and_clears_entries_the_broker_no_longer_lists(env):
    run(pu.map_t212([inst("SAPd_EQ", "DE0007164600"), inst("NVDd_EQ", "US67066G1040")]))
    res = run(pu.map_t212([inst("SAPd_EQ", "DE0007164600"), inst("ASMLa_EQ", "NL0010273215")]))
    assert res["mapped"] == {"SAP": "SAPd_EQ", "ASML": "ASMLa_EQ"}
    assert pu.t212_ticker("NVDA") is None and pu.t212_ticker("ASML") == "ASMLa_EQ"


@pytest.mark.parametrize("bad", [[], None, {"error": "timeout", "status": None, "detail": ""}, "x"])
def test_map_t212_with_an_unusable_list_changes_nothing(env, bad):
    run(pu.map_t212([inst("SAPd_EQ", "DE0007164600")]))
    stored = env.store.read_bytes()
    res = run(pu.map_t212(bad))
    assert res["mapped"] == {"SAP": "SAPd_EQ"} and "SAP" not in res["unmapped"]
    assert res["saved"] is False
    assert env.store.read_bytes() == stored


def test_map_t212_reports_a_failed_save_and_never_raises(env, monkeypatch):
    monkeypatch.setattr(jsonstore, "save", lambda *a, **k: False)
    res = run(pu.map_t212([inst("SAPd_EQ", "DE0007164600")]))
    assert res["mapped"] == {"SAP": "SAPd_EQ"} and res["saved"] is False


def test_map_t212_skips_items_that_are_not_instruments(env):
    res = run(pu.map_t212([object(), 5, None, {"isin": "DE0007164600", "currency": "EUR", "ticker": "SAPd_EQ"}]))
    assert res["mapped"] == {"SAP": "SAPd_EQ"}


# ------------------------------------------------------------------ the shipped seed

def _luhn_isin(isin):
    digits = "".join(str(int(c, 36)) for c in isin)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def test_shipped_seed_is_a_valid_index_rule_universe():
    path = pathlib.Path(__file__).resolve().parents[2] / "ops" / "paper_universe_seed.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["source"].strip()                                 # membership is cited
    doc = pu._clean_doc(raw)
    assert doc is not None and len(doc["entries"]) == len(raw["entries"])   # no row is dropped
    rows = doc["entries"]
    assert len({r["id"] for r in rows}) == len(rows)
    assert len({r["isin"] for r in rows}) == len(rows)
    suffix_venue = {"DE": "XETR", "AS": "XAMS", "PA": "XPAR", "BR": "XBRU", "MI": "XMIL"}
    for r in rows:
        assert r["id"] == r["id"].upper() and 1 <= len(r["id"]) <= 8, r["id"]
        assert r["t212"] is None, r["id"]
        assert r["name"] and r["sector"], r["id"]
        assert len(r["isin"]) == 12 and _luhn_isin(r["isin"]), (r["id"], r["isin"])
        assert suffix_venue[r["yahoo"].rsplit(".", 1)[-1]] == r["venue"], r["id"]
    bench = {r["id"]: r for r in rows if r["role"] == "benchmark"}
    assert set(bench) == {"EUNL", "SXR8"}
    assert all(b["kind"] == "etf" and b["yahoo"].endswith(".DE") for b in bench.values())
    candidates = [r for r in rows if r["role"] == "candidate"]
    assert len(candidates) >= 100
    assert {"SAP", "ASML", "NVDA"} <= {r["id"] for r in candidates}
