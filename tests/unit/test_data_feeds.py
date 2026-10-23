"""Data feeds (docs/research/data-sources.md section 4 items 2, 4, 6, 7): CSV parsing (ECB, FRED, AFM cp1252), BDIF
parsing, the point-in-time readers (``ingestedAt`` filter), per-source backoff and health, the worker cadence, the
consensus revision maths and the OpenFIGI identity finding. Every network seam is faked (``regime_feed._request``,
``prices._yf``, the clocks and the sleeper): no network, no sleeps, no lane.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import main
from app.api import consensus_log, data_feeds, forex, jsonstore, ops_watch, paper_universe, regime_feed, regulators
from app.api import universe_news as un
from app.api.regime_feed import FeedError

BERLIN = ZoneInfo("Europe/Berlin")
UTC = datetime.timezone.utc


def berlin(y, m, d, h=0, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=BERLIN).timestamp()


NOW = berlin(2026, 10, 21, 10, 0)             # Wednesday, XETR session day, before the close
AFTER_CLOSE = berlin(2026, 10, 21, 18, 1)     # close 17:30 + 30 min


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat()


def run(coro):
    return asyncio.run(coro)


ROWS = [
    {"id": "SAP", "name": "SAP", "isin": "DE0007164600", "yahoo": "SAP.DE", "venue": "XETR", "kind": "equity",
     "sector": "IT", "role": "candidate", "t212": None},
    {"id": "ASML", "name": "ASML Holding", "isin": "NL0010273215", "yahoo": "ASML.AS", "venue": "XAMS", "kind": "equity",
     "sector": "IT", "role": "candidate", "t212": None},
    {"id": "NVDA", "name": "NVIDIA", "isin": "US67066G1040", "yahoo": "NVD.DE", "venue": "XETR", "kind": "equity",
     "sector": "IT", "role": "candidate", "t212": None},
    {"id": "TTE", "name": "TotalEnergies", "isin": "FR0000120271", "yahoo": "TTE.PA", "venue": "XPAR", "kind": "equity",
     "sector": "Energy", "role": "candidate", "t212": None},
    {"id": "EUNL", "name": "World ETF", "isin": "IE00B4L5Y983", "yahoo": "EUNL.DE", "venue": "XETR", "kind": "etf",
     "sector": "ETF", "role": "benchmark", "t212": None},
]
FIGI = {   # ISIN -> OpenFIGI listings on the registry exchange
    "DE0007164600": [{"ticker": "SAP", "name": "SAP SE", "exchCode": "GY"}],
    "NL0010273215": [{"ticker": "ASML", "name": "ASML HOLDING NV", "exchCode": "NA"}],
    "US67066G1040": [{"ticker": "NVD", "name": "NVIDIA CORP", "exchCode": "GY"}],
    "FR0000120271": [{"ticker": "TTE", "name": "TOTALENERGIES SE", "exchCode": "FP"}],
    "IE00B4L5Y983": [{"ticker": "EUNL", "name": "ISHARES CORE MSCI WORLD", "exchCode": "GY"}],
}


# ------------------------------------------------------------------ fixtures and fakes

@pytest.fixture
def env(monkeypatch, tmp_path):
    for m in (regime_feed, regulators, data_feeds):
        m.reset_state()
    for k in ("DATA_FEEDS_ENABLED", "FORECAST_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    store = tmp_path / "paper_universe.json"
    jsonstore.save(store, {"asOf": "2026-10-21", "source": "test", "entries": ROWS})
    monkeypatch.setattr(paper_universe, "STORE_FILE", store)
    monkeypatch.setattr(paper_universe, "SEED_FILE", tmp_path / "no-seed.json")
    monkeypatch.setattr(un, "NEWS_DIR", tmp_path / "news")
    monkeypatch.setattr(regime_feed, "REGIME_DIR", tmp_path / "regime")
    monkeypatch.setattr(consensus_log, "CONSENSUS_DIR", tmp_path / "consensus")
    monkeypatch.setattr(data_feeds, "STATE_FILE", tmp_path / "feeds" / "state.json")
    monkeypatch.setattr(data_feeds, "IDENTITY_FILE", tmp_path / "feeds" / "identity.json")
    w = SimpleNamespace(now=NOW, mono=1000.0, calls=[], sleeps=[], routes=[], yf={}, yf_calls=[], records=[])

    async def fake_request(method, url, params, headers, json_body):
        w.calls.append({"method": method, "url": url, "params": params, "headers": headers, "json": json_body})
        for needle, handler in w.routes:
            if needle in url:
                return handler(method, url, params, json_body)
        return 404, b""

    async def fake_sleep(seconds):
        w.sleeps.append(seconds)
        w.mono += seconds

    async def fake_yf(key, fn, sym):
        w.yf_calls.append(sym)
        got = w.yf.get(sym)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(regime_feed, "_now", lambda: w.now)
    monkeypatch.setattr(regime_feed, "_mono", lambda: w.mono)
    monkeypatch.setattr(regime_feed, "_sleep", fake_sleep)
    monkeypatch.setattr(regime_feed, "_request", fake_request)
    monkeypatch.setattr(consensus_log.prices, "_yf", fake_yf)
    monkeypatch.setattr(data_feeds.runlog, "record", lambda *a, **k: w.records.append((a, k)))
    yield w
    for m in (regime_feed, regulators, data_feeds):
        m.reset_state()


def serve(w, needle, status=200, body=b""):
    w.routes.insert(0, (needle, lambda *a: (status, body)))


def ecb_csv(obs):
    return ("KEY,FREQ,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n" + "".join(f"X,B,{d},{v},A\n" for d, v in obs)).encode()


ECB_DATA = {"mro": [("2026-09-10", 2.4), ("2026-09-16", 2.65), ("2026-10-20", 2.65)],
            "dfr": [("2026-09-10", 2.25), ("2026-09-16", 2.5), ("2026-10-20", 2.5)],
            "estr": [("2026-10-20", 2.44)], "aaa10y": [("2026-10-20", 3.44)], "eurusd": [("2026-10-20", 1.1215)]}


def serve_ecb(w, **over):
    data = {**ECB_DATA, **over}
    for key, (flow, series) in regime_feed.ECB_SERIES.items():
        serve(w, f"{flow}/{series}", 200, ecb_csv(data[key]))


def fred_csv(series, obs, head="observation_date"):
    return (f"{head},{series}\n" + "".join(f"{d},{v}\n" for d, v in obs)).encode()


FRED_DATA = {"VIXCLS": [("2026-10-16", 17.9), ("2026-10-19", 18.44)], "DGS10": [("2026-10-19", 5.15)]}


def serve_fred(w, **over):
    data = {**FRED_DATA, **over}
    w.routes.insert(0, ("fredgraph.csv", lambda m, u, p, j: (200, fred_csv(p["id"], data[p["id"]]))))


AFM_HEAD = "Positie houder;Naam uitgevende instelling;ISIN;Netto shortpositie;Positiedatum\n"
AFM_CSV = (AFM_HEAD + "Citadel Advisors LLC;ASML Holding;NL0010273215;0,52;19-10-2026\n"
           "Some Fund;Other NV;NL0000000001;1,10;18-10-2026\n"
           "Broken;ASML Holding;NL0010273215;n/a;19-10-2026\n")


def serve_afm(w, text=AFM_CSV):
    serve(w, "afm.nl/export.aspx", 200, text.encode("utf-8-sig"))


BDIF_ITEM = {"numero": "2026DD1", "datePublication": "2026-10-20T08:00:00", "emetteur": "TOTALENERGIES SE",
             "urlDocument": "/pdf/1.pdf"}


def serve_bdif(w, dd=(BDIF_ITEM,), vad=()):
    def handler(m, u, p, j):
        return 200, json.dumps({"total": 1, "result": list(dd if p["TypesInformation"] == "DD" else vad)}).encode()
    w.routes.insert(0, ("bdif.amf-france.org", handler))


def serve_figi(w, data=None):
    data = data or FIGI

    def handler(m, u, p, jobs):
        return 200, json.dumps([{"data": data[j["idValue"]]} if data.get(j["idValue"]) else
                                {"warning": "No identifier found."} for j in jobs]).encode()
    w.routes.insert(0, ("openfigi.com", handler))


def serve_all(w):
    serve_ecb(w)
    serve_fred(w)
    serve_afm(w)
    serve_bdif(w)
    serve_figi(w)


def info(target=100.0, n=10, rec="buy", rec_mean=2.0, hi=120.0, lo=80.0):
    return {"targetMeanPrice": target, "targetHighPrice": hi, "targetLowPrice": lo, "recommendationKey": rec,
            "recommendationMean": rec_mean, "numberOfAnalystOpinions": n}


# ------------------------------------------------------------------ CSV parsing: ECB, FRED, AFM

def test_ecb_csv_parses_daily_rows_oldest_first_and_skips_unusable_ones():
    raw = (b"\xef\xbb\xbfKEY,FREQ,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
           b"A,B,2026-10-20,2.44,A\nA,B,2026-10-19,2.43,A\nA,B,2026-10-18,,A\nA,B,2026-10,2.0,A\nA,B,2026-10-17,nan,A\n")
    assert regime_feed.parse_ecb_csv(raw) == [("2026-10-19", 2.43), ("2026-10-20", 2.44)]


def test_ecb_csv_without_the_expected_columns_is_a_parse_failure():
    with pytest.raises(FeedError) as e:
        regime_feed.parse_ecb_csv("<html>maintenance</html>")
    assert e.value.kind == "parse"


@pytest.mark.parametrize("head", ["observation_date", "DATE"])
def test_fred_csv_skips_missing_observations_and_accepts_both_date_headers(head):
    raw = fred_csv("VIXCLS", [("2026-10-15", "."), ("2026-10-16", 17.9), ("2026-10-17", ""), ("2026-10-19", 18.44)], head)
    assert regime_feed.parse_fred_csv(raw, "VIXCLS") == [("2026-10-16", 17.9), ("2026-10-19", 18.44)]


def test_fred_csv_without_a_date_column_is_a_parse_failure():
    with pytest.raises(FeedError):
        regime_feed.parse_fred_csv("<html>blocked</html>\nx,y\n", "VIXCLS")


def test_last_change_is_the_date_the_value_last_moved():
    obs = [("2026-09-10", 2.25), ("2026-09-15", 2.25), ("2026-09-16", 2.5), ("2026-10-20", 2.5)]
    assert regime_feed.last_change(obs) == "2026-09-16"
    assert regime_feed.last_change([("2026-09-10", 2.25), ("2026-09-11", 2.25)]) is None


def test_afm_export_in_cp1252_is_decoded_not_mangled():
    text = AFM_HEAD + "Société Générale;Philips;NL0000009538;0,75;12/10/2026\n"
    [row] = regulators.parse_afm_csv(text.encode("cp1252"))
    assert row == {"holder": "Société Générale", "issuer": "Philips", "isin": "NL0000009538", "netShortPct": 0.75,
                   "date": "2026-10-12"}


def test_afm_export_utf8_bom_comma_decimals_and_bad_rows():
    rows = regulators.parse_afm_csv(AFM_CSV.encode("utf-8-sig"))
    assert [(r["isin"], r["netShortPct"], r["date"]) for r in rows] == [("NL0010273215", 0.52, "2026-10-19"),
                                                                        ("NL0000000001", 1.1, "2026-10-18")]


def test_afm_header_without_the_expected_words_falls_back_to_the_documented_order_at_five_columns():
    text = "a;b;c;d;e\nHolder X;Issuer Y;NL0010273215;0,6;2026-10-19\n"
    [row] = regulators.parse_afm_csv(text)
    assert (row["holder"], row["isin"], row["netShortPct"]) == ("Holder X", "NL0010273215", 0.6)
    with pytest.raises(FeedError):
        regulators.parse_afm_csv("a;b;c\nx;y;z\n")                     # not five columns, no keywords: no guessing


def test_afm_export_that_shifted_layout_is_a_parse_failure_not_an_empty_success():
    with pytest.raises(FeedError):
        regulators.parse_afm_csv(AFM_HEAD + "x;y;not-an-isin;nope;nodate\n")


# ------------------------------------------------------------------ BDIF parsing

def test_bdif_items_are_read_tolerantly_and_foreign_or_undated_items_are_dropped():
    payload = {"total": 4, "result": [
        BDIF_ITEM,
        {"numero": "2", "datePublication": "2026-10-19", "societes": [{"raisonSociale": "Totalenergies SE"}]},
        {"numero": "3", "datePublication": "2026-10-18", "emetteur": "Some Other Corp"},
        {"numero": "4", "emetteur": "TOTALENERGIES SE"},
    ]}
    got = regulators.parse_bdif(payload, "TotalEnergies")
    assert [(g["docId"], g["date"]) for g in got] == [("2026DD1", "2026-10-20"), ("2", "2026-10-19")]
    assert got[0]["url"] == "https://bdif.amf-france.org/pdf/1.pdf" and got[1]["url"] == ""
    assert regulators.parse_bdif({"unexpected": "shape"}, "TotalEnergies") == []


def test_company_name_matching_ignores_legal_forms_accents_and_truncation():
    assert regime_feed.names_match("L'Oréal", "L OREAL SA")
    assert regime_feed.names_match("LVMH Moet Hennessy Louis Vuitton", "LVMH MOET HENNESSY LOUIS VUI")
    assert regime_feed.names_match("TotalEnergies", "TOTALENERGIES SE")
    assert not regime_feed.names_match("TotalEnergies", "Exxon Mobil Corp")
    assert regime_feed.names_match("", "Anything")                    # nothing to compare is never an alarm


# ------------------------------------------------------------------ regime feed: store and PIT reader

def test_regime_refresh_stores_every_series_and_pit_reads_them_back(env):
    serve_ecb(env)
    serve_fred(env)
    assert run(regime_feed.refresh_ecb()) == {"new": 6, "failed": {}}
    assert run(regime_feed.refresh_fred()) == {"new": 2, "failed": {}}
    got = regime_feed.pit(iso(NOW + 3600))
    assert got["ecb"]["mro"] == {"value": 2.65, "date": "2026-10-20", "source": "ECB Data Portal FM/B.U2.EUR.4F.KR.MRR_FR.LEV"}
    assert got["ecb"]["dfr"]["value"] == 2.5
    assert got["ecb"]["dfrLastChange"]["value"] == "2026-09-16" and got["ecb"]["dfrLastChange"]["date"] == "2026-10-20"
    assert (got["ecb"]["estr"]["value"], got["ecb"]["aaa10y"]["value"], got["ecb"]["eurusd"]["value"]) == (2.44, 3.44, 1.1215)
    assert (got["fred"]["vix"]["value"], got["fred"]["vix"]["date"], got["fred"]["us10y"]["value"]) == (18.44, "2026-10-19", 5.15)


def test_regime_pit_never_returns_a_value_ingested_after_the_as_of(env):
    serve_ecb(env)
    run(regime_feed.refresh_ecb(NOW))
    serve_ecb(env, mro=[("2026-10-22", 2.9)], dfr=[("2026-09-16", 2.5), ("2026-10-22", 2.75)])
    run(regime_feed.refresh_ecb(NOW + 2 * 86400))
    assert regime_feed.pit(iso(NOW - 1))["ecb"]["mro"] == regime_feed.MISSING
    assert regime_feed.pit(iso(NOW + 86400))["ecb"]["mro"]["value"] == 2.65         # the 22nd's value did not exist yet
    assert regime_feed.pit(iso(NOW + 2 * 86400))["ecb"]["mro"]["value"] == 2.9
    assert regime_feed.pit(iso(NOW + 86400))["ecb"]["dfrLastChange"]["value"] == "2026-09-16"
    assert regime_feed.pit(iso(NOW + 2 * 86400))["ecb"]["dfrLastChange"]["value"] == "2026-10-22"
    assert regime_feed.pit(iso(NOW + 86400))["fred"]["vix"] == regime_feed.MISSING


def test_regime_store_appends_only_on_change_but_at_least_once_per_month(env):
    serve_ecb(env)
    run(regime_feed.refresh_ecb(NOW))
    assert run(regime_feed.refresh_ecb(NOW + 3600))["new"] == 0                      # nothing changed, same month
    nxt_month = berlin(2026, 11, 3, 10)
    assert run(regime_feed.refresh_ecb(nxt_month))["new"] == 6                       # a month row keeps lookups short
    assert regime_feed.pit(iso(nxt_month + 1))["ecb"]["estr"]["value"] == 2.44


def test_regime_pit_accepts_a_bare_date_as_the_end_of_that_day_and_rejects_garbage(env):
    serve_ecb(env)
    run(regime_feed.refresh_ecb(NOW))                                               # 2026-10-21 08:00 UTC
    assert regime_feed.pit("2026-10-21")["ecb"]["mro"]["value"] == 2.65
    assert regime_feed.pit("2026-10-20")["ecb"]["mro"] == regime_feed.MISSING
    with pytest.raises(ValueError):
        regime_feed.pit("yesterday")


def test_ecb_rate_series_ask_for_history_and_the_others_for_the_latest_few(env):
    serve_ecb(env)
    run(regime_feed.refresh_ecb(NOW))
    by_url = {c["url"].split("/service/data/")[1]: c["params"] for c in env.calls}
    assert "startPeriod" in by_url["FM/B.U2.EUR.4F.KR.DFR.LEV"] and "startPeriod" in by_url["FM/B.U2.EUR.4F.KR.MRR_FR.LEV"]
    assert by_url["EST/B.EU000A2X2A25.WT"]["lastNObservations"] == regime_feed.RECENT_OBS
    assert all(p["format"] == "csvdata" for p in by_url.values())


def test_fred_requests_override_the_ua_with_the_library_one(env):
    # FRED tarpits descriptive and browser UAs (15 s read timeouts, probed live from the pod on 2026-10-21) and
    # answers the plain library UA in ~50 ms: every FRED call overrides the client's descriptive UA.
    serve_fred(env)
    run(regime_feed.refresh_fred(NOW))
    assert env.calls and all(c["headers"]["User-Agent"].startswith("python-httpx/") for c in env.calls)
    assert "Mozilla" not in regime_feed.SOURCE_UA["fred"]
    assert regime_feed._client_get().headers["User-Agent"] == regime_feed.USER_AGENT   # other sources keep it
    run(regime_feed.close())
    assert {c["params"]["id"] for c in env.calls} == {"VIXCLS", "DGS10"}


def test_a_rate_limit_answer_stops_the_ecb_pass_and_is_reported_per_series(env):
    serve_ecb(env)
    serve(env, "FM/B.U2.EUR.4F.KR.MRR_FR.LEV", 429)
    res = run(regime_feed.refresh_ecb(NOW))
    assert res["new"] == 0 and list(res["failed"]) == ["mro"] and res["failed"]["mro"].startswith("rate_limited")
    assert len(env.calls) == 1                                                      # the rest would be refused too


def test_one_broken_series_does_not_cost_the_others(env):
    serve_ecb(env)
    serve(env, "EST/B.EU000A2X2A25.WT", 500)
    res = run(regime_feed.refresh_ecb(NOW))
    assert res["new"] == 5 and list(res["failed"]) == ["estr"]
    assert regime_feed.pit(iso(NOW + 1))["ecb"]["estr"] == regime_feed.MISSING
    assert regime_feed.pit(iso(NOW + 1))["ecb"]["aaa10y"]["value"] == 3.44


def test_requests_to_one_source_are_spaced(env):
    serve_ecb(env)
    run(regime_feed.refresh_ecb(NOW))
    assert env.sleeps == [regime_feed.SPACING_S["ecb"]] * 4                          # 5 requests, 4 gaps, no real sleep


# ------------------------------------------------------------------ regulators: ingestion and PIT reader

def store_rows():
    return un.read_jsonl(un.month_path(NOW))


def test_afm_rows_are_matched_by_isin_typed_and_have_no_title(env):
    serve_afm(env)
    res = run(regulators.refresh_afm(NOW))
    assert res["new"] == 1 and res["parsed"] == 2 and res["matched"] == 1 and res["complete"]
    [row] = store_rows()
    assert row == {"id": "ASML", "kind": "short_position", "source": "regulator", "regulator": "AFM",
                   "eventDate": "2026-10-19", "ingestedAt": un.iso_ceil(NOW), "eventHash": row["eventHash"],
                   "holder": "Citadel Advisors LLC", "netShortPct": 0.52}
    assert "title" not in row
    assert un.recent("ASML", 10, NOW) == []                                          # never a headline


def test_afm_positions_are_deduped_across_days_and_across_a_lost_state_file(env, tmp_path):
    serve_afm(env)
    run(regulators.refresh_afm(NOW))
    assert run(regulators.refresh_afm(NOW + 86400))["new"] == 0
    (tmp_path / "news" / "regulators_state.json").unlink()
    regulators.reset_state()
    assert run(regulators.refresh_afm(NOW + 2 * 86400))["new"] == 1                  # a duplicate row ...
    assert len(store_rows()) == 2
    [ev] = regulators.pit_events("ASML", iso(NOW + 3 * 86400), 10)                   # ... that readers collapse
    assert ev["ingested_at"] == NOW


def test_regulator_pit_filters_on_ingestion_time_and_the_event_window(env):
    serve_afm(env)
    run(regulators.refresh_afm(NOW))
    assert regulators.pit_events("ASML", iso(NOW - 1), 30) == []                     # ingested after this as-of
    [ev] = regulators.pit_events("ASML", iso(NOW + 3600), 5)
    assert ev == {"kind": "short_position", "regulator": "AFM", "event_date": "2026-10-19", "ingested_at": NOW,
                  "net_short_pct": 0.52}
    assert regulators.pit_events("ASML", iso(NOW + 3600), 2)                         # 19th is inside "2 days before the 21st"
    assert regulators.pit_events("ASML", iso(NOW + 3600), 1) == []
    assert regulators.pit_events("SAP", iso(NOW + 3600), 30) == []
    assert regulators.pit_events("ASML", "2026-10-19", 30) == []                      # a bare date ends that day: not yet ingested


def test_bdif_rows_keep_the_url_in_the_store_but_not_in_the_reader(env):
    serve_bdif(env, vad=({"numero": "V1", "datePublication": "2026-10-20", "emetteur": "TotalEnergies SE"},))
    res = run(regulators.refresh_bdif(NOW))
    assert res == {"new": 2, "failed": {}, "complete": True}
    by_kind = {r["kind"]: r for r in store_rows()}
    assert by_kind["directors_dealing"]["url"] == "https://bdif.amf-france.org/pdf/1.pdf"
    assert by_kind["directors_dealing"]["regulator"] == "AMF" and by_kind["short_position"]["docId"] == "V1"
    events = regulators.pit_events("TTE", iso(NOW + 1), 10)
    assert {e["kind"] for e in events} == {"directors_dealing", "short_position"}
    assert all("url" not in e and "holder" not in e for e in events)
    assert {c["params"]["TypesInformation"] for c in env.calls} == {"DD", "VAD"}
    assert {c["params"]["RechercheTexte"] for c in env.calls} == {"TotalEnergies"}   # only the French name is searched


def test_bdif_window_starts_30_days_back_then_at_the_last_complete_pass_minus_an_overlap(env):
    serve_bdif(env)
    run(regulators.refresh_bdif(NOW))
    assert env.calls[0]["params"]["DateDebut"] == "2026-09-21"
    env.calls.clear()
    run(regulators.refresh_bdif(NOW + 86400))
    assert env.calls[0]["params"]["DateDebut"] == "2026-10-18"                       # last complete pass (the 21st) - 3 days


def test_bdif_rate_limit_ends_the_pass_incomplete_and_keeps_the_window(env):
    serve(env, "bdif.amf-france.org", 429)
    res = run(regulators.refresh_bdif(NOW))
    assert res["complete"] is False and list(res["failed"]) == ["TTE/DD"] and len(env.calls) == 1
    assert regulators._load_state()["bdifLastOk"] is None


# ------------------------------------------------------------------ consensus: snapshots, PIT reader, revision maths

def put_snap(pid, at, target, rec_mean=2.0, n=10, key="buy"):
    row = {"id": pid, "targetMean": target, "targetHigh": None, "targetLow": None, "recKey": key, "recMean": rec_mean,
           "nAnalysts": n, "fetchedAt": un.iso_ceil(at)}
    assert un.append_jsonl(un.month_path(at, consensus_log.CONSENSUS_DIR), [row])


DAY = 86400.0
T0 = berlin(2026, 10, 21, 18, 30)


def test_snapshot_row_maps_yahoo_fields_and_skips_names_without_analyst_data():
    row = consensus_log.snapshot_row("SAP", info(target=210.5, n=24, rec="strong_buy", rec_mean=1.6), T0)
    assert row == {"id": "SAP", "targetMean": 210.5, "targetHigh": 120.0, "targetLow": 80.0, "recKey": "strong_buy",
                   "recMean": 1.6, "nAnalysts": 24, "fetchedAt": un.iso_ceil(T0)}
    assert consensus_log.snapshot_row("SAP", {"longName": "SAP SE", "recommendationKey": "none"}, T0) is None
    assert consensus_log.snapshot_row("SAP", {}, T0) is None
    bad = consensus_log.snapshot_row("SAP", {"targetMeanPrice": -3, "numberOfAnalystOpinions": 7}, T0)
    assert bad["targetMean"] is None and bad["nAnalysts"] == 7


def test_revisions_math_over_seven_and_thirty_days(env):
    put_snap("SAP", T0 - 30 * DAY, 100.0, rec_mean=3.0)
    put_snap("SAP", T0 - 7 * DAY, 110.0, rec_mean=2.8)
    put_snap("SAP", T0, 121.0, rec_mean=2.5)
    assert consensus_log.revisions("SAP", iso(T0 + 3600)) == {
        "targetMeanChg7dPct": 10.0, "targetMeanChg30dPct": 21.0, "recMeanChg30d": -0.5}


def test_revisions_use_only_what_was_known_at_the_as_of(env):
    put_snap("SAP", T0 - 30 * DAY, 100.0)
    put_snap("SAP", T0 - 7 * DAY, 110.0)
    put_snap("SAP", T0, 121.0)
    got = consensus_log.revisions("SAP", iso(T0 - 1))                                # the newest row is not known yet
    assert got == {"targetMeanChg7dPct": None, "targetMeanChg30dPct": None, "recMeanChg30d": None}
    assert consensus_log.pit("SAP", iso(T0 - 1))["targetMean"] == 110.0
    assert consensus_log.pit("SAP", iso(T0))["targetMean"] == 121.0
    assert consensus_log.pit("SAP", iso(T0 - 31 * DAY)) is None
    assert consensus_log.revisions("SAP", iso(T0 - 31 * DAY)) is None


def test_revisions_reference_must_be_close_to_the_window_and_the_consensus_thick_enough(env):
    put_snap("SAP", T0 - 20 * DAY, 100.0)
    put_snap("SAP", T0, 110.0)
    assert consensus_log.revisions("SAP", iso(T0 + 1)) == {
        "targetMeanChg7dPct": None, "targetMeanChg30dPct": None, "recMeanChg30d": None}   # 20 d old is no 7 d / 30 d reference
    put_snap("ASML", T0 - 7 * DAY, 100.0, n=2)                                        # thin consensus: noise
    put_snap("ASML", T0, 110.0, n=12)
    assert consensus_log.revisions("ASML", iso(T0 + 1))["targetMeanChg7dPct"] is None
    put_snap("NVDA", T0 - 7 * DAY, 100.0, n=9)
    put_snap("NVDA", T0, 90.0, n=9)
    assert consensus_log.revisions("NVDA", iso(T0 + 1))["targetMeanChg7dPct"] == -10.0


def test_revisions_of_a_stale_newest_row_are_none_per_field(env):
    put_snap("SAP", T0 - 7 * DAY, 100.0)
    put_snap("SAP", T0, 110.0)
    got = consensus_log.revisions("SAP", iso(T0 + 6 * DAY))
    assert got == {"targetMeanChg7dPct": None, "targetMeanChg30dPct": None, "recMeanChg30d": None}


def test_run_snapshot_writes_rows_skips_uncovered_names_and_stops_after_three_failures_in_a_row(env):
    rows = [r for r in ROWS if r["kind"] == "equity"]
    env.yf = {"SAP.DE": info(target=200.0), "ASML.AS": {"longName": "no analysts"}, "NVD.DE": None, "TTE.PA": None}
    res = run(consensus_log.run_snapshot(rows, T0, regime_feed.mono() + 600))
    assert res["new"] == 1 and res["done"] == ["SAP", "ASML"] and set(res["failed"]) == {"NVDA", "TTE"}
    assert env.sleeps == [1.0, 1.0, 1.0]                                              # one spacing gap per later request
    env.yf = {r["yahoo"]: None for r in ROWS}
    env.yf_calls.clear()
    run(consensus_log.run_snapshot(rows + rows, T0, regime_feed.mono() + 600))
    assert len(env.yf_calls) == consensus_log.FAILS_STOP                              # a limited Yahoo is not hammered


def test_run_snapshot_honours_the_deadline(env):
    rows = [r for r in ROWS if r["kind"] == "equity"]
    env.yf = {r["yahoo"]: info() for r in rows}
    res = run(consensus_log.run_snapshot(rows, T0, regime_feed.mono() - 1))
    assert res == {"new": 0, "done": [], "failed": {}}


# ------------------------------------------------------------------ backoff and per-source health

def test_rate_limit_backs_a_source_off_at_once_and_other_errors_after_three_in_a_row(env):
    data_feeds.note_fail("ecb", "rate_limited", "HTTP 429", 1000.0)
    assert not data_feeds.source_ready("ecb", 1119.0) and data_feeds.source_ready("ecb", 1120.0)
    for i in range(2):
        data_feeds.note_fail("fred", "http", "HTTP 500", 1000.0 + i)
    assert data_feeds.source_ready("fred", 1002.0)                                    # two plain failures: still polite-retry
    data_feeds.note_fail("fred", "http", "HTTP 500", 1002.0)
    assert not data_feeds.source_ready("fred", 1100.0) and data_feeds.source_ready("fred", 1122.0)
    data_feeds.note_fail("fred", "http", "HTTP 500", 1122.0)                          # the pause doubles ...
    assert not data_feeds.source_ready("fred", 1122.0 + 239) and data_feeds.source_ready("fred", 1122.0 + 240)
    for _ in range(20):                                                              # ... up to an hour
        data_feeds.note_fail("fred", "http", "HTTP 500", 5000.0)
    assert data_feeds._h("fred")["backoffUntil"] == 5000.0 + data_feeds.BACKOFF_MAX_S


def test_a_success_clears_the_streak_and_counts_the_items(env):
    data_feeds.note_fail("afm", "http", "HTTP 500", 1.0)
    data_feeds.note_ok("afm", 3, 2.0)
    h = data_feeds.health()["afm"]
    assert (h["ok"], h["fail"], h["items"], h["consecutiveFails"], h["lastError"]) == (1, 1, 3, 0, None)


def test_guard_skips_a_source_in_backoff_and_files_failures_on_its_health_row(env):
    calls = []

    async def failing():
        calls.append(1)
        return {"new": 0, "failed": {"mro": "rate_limited: HTTP 429"}}

    assert run(data_feeds._guard("ecb", failing, 100.0)) == (True, False, {"new": 0, "failed": {"mro": "rate_limited: HTTP 429"}})
    assert run(data_feeds._guard("ecb", failing, 101.0)) == (False, False, None)      # backing off: not even called
    assert len(calls) == 1
    h = data_feeds.health()["ecb"]
    assert h["fail"] == 1 and "rate_limited" in h["lastError"] and h["backoffUntil"] == 220.0


def test_guard_turns_an_exception_into_a_failure_instead_of_raising(env):
    async def boom():
        raise RuntimeError("bad")

    assert run(data_feeds._guard("fred", boom, 5.0)) == (True, False, None)
    assert data_feeds.health()["fred"]["lastError"] == "error: RuntimeError"


# ------------------------------------------------------------------ worker cadence

def test_regime_is_due_hourly():
    assert data_feeds.regime_due(NOW, 0.0)
    assert not data_feeds.regime_due(NOW, NOW - 3599) and data_feeds.regime_due(NOW, NOW - 3600)


def test_registers_are_due_once_per_berlin_day_from_eight_oclock():
    assert not data_feeds.regulators_due(berlin(2026, 10, 21, 7, 59), "2026-10-20")
    assert data_feeds.regulators_due(berlin(2026, 10, 21, 8, 0), "2026-10-20")
    assert not data_feeds.regulators_due(berlin(2026, 10, 21, 23, 0), "2026-10-21")


def test_consensus_runs_on_session_days_from_half_an_hour_after_the_close():
    assert data_feeds.consensus_target(berlin(2026, 10, 21, 17, 59)) is None
    assert data_feeds.consensus_target(berlin(2026, 10, 21, 18, 0)) == "2026-10-21"
    assert data_feeds.consensus_target(berlin(2026, 10, 24, 20, 0)) is None            # Saturday
    assert data_feeds.consensus_target(berlin(2026, 12, 25, 20, 0)) is None            # Xetra holiday


def test_identity_audit_is_due_once_per_berlin_month():
    assert data_feeds.identity_due(NOW, None)
    assert not data_feeds.identity_due(NOW, {"month": "2026-10"})
    assert data_feeds.identity_due(berlin(2026, 11, 1, 3), {"month": "2026-10"})


def test_a_full_day_of_passes(env):
    serve_all(env)
    env.yf = {r["yahoo"]: info(target=100.0 + i) for i, r in enumerate(r for r in ROWS if r["kind"] == "equity")}
    ok, note, acted = run(data_feeds._tick())                                         # 10:00: all but the consensus
    assert ok and acted and "regime 2/2 ok" in note and "regulators 2/2 ok" in note and "identity 1/1 ok" in note
    assert "consensus" not in note
    h = data_feeds.health()
    assert [h[s]["ok"] for s in ("ecb", "fred", "afm", "bdif", "openfigi")] == [1] * 5 and h["consensus"]["ok"] == 0
    assert regime_feed.pit(iso(env.now + 1))["ecb"]["mro"]["value"] == 2.65
    assert regulators.pit_events("ASML", iso(env.now + 1), 5)[0]["net_short_pct"] == 0.52
    assert jsonstore.load(data_feeds.IDENTITY_FILE)["mismatches"] == []

    env.now += 60                                                                    # next poll: nothing is due
    ok, note, acted = run(data_feeds._tick())
    assert (ok, note, acted) == (True, "idle", False)

    env.now = AFTER_CLOSE                                                            # after the close: regime again + consensus
    ok, note, acted = run(data_feeds._tick())
    assert ok and "consensus 1/1 ok, 4 new" in note and "regime 2/2 ok" in note and "regulators" not in note
    assert consensus_log.pit("SAP", iso(env.now + 1))["targetMean"] == 100.0
    assert consensus_log.pit("EUNL", iso(env.now + 1)) is None                        # ETFs are not snapshotted
    assert env.yf_calls == ["SAP.DE", "ASML.AS", "NVD.DE", "TTE.PA"]
    env.yf_calls.clear()
    env.now += 60
    assert run(data_feeds._tick())[1] == "idle" and env.yf_calls == []                # the day is done
    assert data_feeds.status()["consensus"] == {"date": "2026-10-21", "done": 4}


def test_a_restart_resumes_the_consensus_day_with_the_names_still_missing(env):
    serve_all(env)
    env.now = AFTER_CLOSE
    env.yf = {"SAP.DE": info(), "ASML.AS": info(), "NVD.DE": info(), "TTE.PA": None}
    run(data_feeds._tick())
    assert data_feeds.status()["consensus"]["done"] == 3
    data_feeds.reset_state()                                                         # process restart: state comes from the file
    env.yf = {"TTE.PA": info()}
    env.yf_calls.clear()
    env.now += 60
    run(data_feeds._tick())
    assert env.yf_calls == ["TTE.PA"] and data_feeds.status()["consensus"]["done"] == 4


def test_one_failing_source_does_not_stop_the_others_and_is_not_a_failed_pass(env):
    serve_all(env)
    serve(env, "data-api.ecb.europa.eu", 500)
    ok, note, _ = run(data_feeds._tick())
    assert ok and "regime 1/2 ok" in note
    assert regime_feed.pit(iso(env.now + 1))["fred"]["vix"]["value"] == 18.44
    h = data_feeds.health()
    assert h["ecb"]["fail"] == 1 and h["ecb"]["lastError"].startswith("http") and h["fred"]["ok"] == 1


def test_every_source_failing_is_a_failed_pass_and_the_next_pass_respects_the_backoff(env):
    for host in ("data-api.ecb.europa.eu", "fredgraph.csv", "afm.nl", "bdif.amf-france.org", "openfigi.com"):
        serve(env, host, 429)
    ok, note, _ = run(data_feeds._tick())
    assert not ok and "every source failed" in note
    n = len(env.calls)
    env.now += 60
    assert run(data_feeds._tick())[1] == "idle" and len(env.calls) == n               # all five are backing off


def test_a_crashing_step_is_contained_and_reported(env, monkeypatch):
    serve_all(env)

    async def boom(now, deadline, st):
        raise RuntimeError("step bug")

    monkeypatch.setattr(data_feeds, "STEPS", (("regime", boom),) + data_feeds.STEPS[1:])
    ok, note, _ = run(data_feeds._tick())
    assert not ok and "regime: RuntimeError" in note and "regulators 2/2 ok" in note


def test_the_worker_records_each_pass_in_runlog_and_reports_status(env):
    serve_all(env)
    assert run(data_feeds._pass()) is True
    (args, kw), = env.records
    assert args[0] == "data_feeds" and args[1] is True and kw == {"idle": False}
    st = data_feeds.status()
    assert st["interval_s"] == data_feeds.WATCH_INTERVAL_S and st["runs"] == 1 and st["lastPass"]["ok"]
    assert st["identityAudit"]["mismatches"] == 0 and set(st["sources"]) == set(data_feeds.SOURCES)


# ------------------------------------------------------------------ identity audit (OpenFIGI) and the ops_watch finding

def row_of(pid):
    return next(r for r in ROWS if r["id"] == pid)


def test_matching_listing_has_no_mismatch():
    assert data_feeds.compare_identity(row_of("SAP"), FIGI["DE0007164600"]) == []
    assert data_feeds.compare_identity(row_of("EUNL"), FIGI["IE00B4L5Y983"]) == []


def test_each_kind_of_identity_mismatch_is_reported():
    sap = row_of("SAP")
    assert [m["kind"] for m in data_feeds.compare_identity(sap, [{"ticker": "SAPX", "name": "SAP SE"}])] == ["ticker"]
    assert [m["kind"] for m in data_feeds.compare_identity(sap, [{"ticker": "SAP", "name": "Totally Different Inc"}])] == ["name"]
    assert [m["kind"] for m in data_feeds.compare_identity(sap, [])] == ["listing"]
    wrong_venue = {**sap, "venue": "XAMS"}                                            # a .DE symbol filed under Amsterdam
    kinds = [m["kind"] for m in data_feeds.compare_identity(wrong_venue, [{"ticker": "SAP", "name": "SAP SE"}])]
    assert kinds == ["venue"]
    assert data_feeds.compare_identity(sap, [{"ticker": "S.A-P", "name": "SAP SE"}]) == []   # punctuation is not a change


@pytest.mark.parametrize("registry,figi", [
    ("BMW", "BAYERISCHE MOTOREN WERKE AG"), ("Munich Re", "MUENCHENER RUECKVER AG-REG"),
    ("Inditex", "INDUSTRIA DE DISENO TEXTIL"), ("AMD", "ADVANCED MICRO DEVICES"),
])
def test_identity_names_match_acronyms_exonyms_and_transliteration(registry, figi):
    row = {"id": "X", "name": registry, "yahoo": "X.DE", "venue": "XETR", "isin": "DE0000000000"}
    assert data_feeds.compare_identity(row, [{"ticker": "X", "name": figi, "exchCode": "GY"}]) == []


def test_identity_names_still_fail_for_a_genuinely_different_company():
    for registry, figi in (("BMW", "DAIMLER TRUCK HOLDING AG"), ("Munich Re", "ALLIANZ SE"), ("AMD", "INTEL CORP")):
        row = {"id": "X", "name": registry, "yahoo": "X.DE", "venue": "XETR", "isin": "DE0000000000"}
        kinds = [m["kind"] for m in data_feeds.compare_identity(row, [{"ticker": "X", "name": figi}])]
        assert kinds == ["name"], registry


def test_xetr_listing_on_another_german_code_passes_but_a_wrong_isin_still_fails(env):
    rows = [{"id": i, "name": n, "isin": isin, "yahoo": f"{i}.DE", "venue": "XETR", "kind": "equity"}
            for i, n, isin in (("FRE", "Fresenius", "DE0005785604"), ("QIA", "Qiagen", "NL0012169213"),
                               ("EUNL", "iShares Core MSCI World", "IE00B4L5Y983"),
                               ("SXR8", "iShares Core S&P 500", "IE00B5BMR087"), ("BAD", "Wrong Co", "XS0000000000"))]
    figi = {"DE0005785604": ("FRE", "FRESENIUS SE & CO KGAA", "GR"), "NL0012169213": ("QIA", "QIAGEN NV", "GF"),
            "IE00B4L5Y983": ("EUNL", "ISHARES CORE MSCI WORLD", "GS"),
            "IE00B5BMR087": ("SXR8", "ISHARES CORE S&P 500", "GD"), "XS0000000000": ("BAD", "WRONG CO", "US")}

    def handler(m, u, p, jobs):
        out = []
        for j in jobs:
            t, name, code = figi[j["idValue"]]
            out.append({"warning": "No identifier found."} if "exchCode" in j else
                       {"data": [{"ticker": t, "name": name, "exchCode": code},
                                 {"ticker": t, "name": name, "exchCode": "UN"}]})
        return 200, json.dumps(out).encode()
    env.routes.insert(0, ("openfigi.com", handler))
    run(data_feeds.run_identity(NOW, rows))
    doc = jsonstore.load(data_feeds.IDENTITY_FILE)
    assert [(m["id"], m["kind"]) for m in doc["mismatches"]] == [("BAD", "listing")]


def test_audit_batches_ten_isins_per_request_with_the_exchange_code_and_stores_the_result(env):
    many = [{**ROWS[0], "id": f"S{i}", "isin": f"DE{i:010d}", "yahoo": f"S{i}.DE"} for i in range(12)]
    data = {m["isin"]: [{"ticker": m["id"], "name": "SAP SE"}] for m in many}
    serve_figi(env, data)
    res = run(data_feeds.run_identity(NOW, many))
    assert res["checked"] == 12 and res["new"] == 0
    assert [len(c["json"]) for c in env.calls] == [10, 2] and all(c["method"] == "POST" for c in env.calls)
    assert env.calls[0]["json"][0] == {"idType": "ID_ISIN", "idValue": many[0]["isin"], "exchCode": "GY"}
    assert env.sleeps == [regime_feed.SPACING_S["openfigi"]]                           # keyless limit: 25 requests / minute
    doc = jsonstore.load(data_feeds.IDENTITY_FILE)
    assert doc["month"] == "2026-10" and doc["checked"] == 12 and doc["mismatches"] == []


def test_audit_that_gets_a_malformed_answer_fails_whole_and_stores_nothing(env):
    serve(env, "openfigi.com", 200, b'[{"data": []}]')                                # 1 answer for 5 requests
    with pytest.raises(FeedError):
        run(data_feeds.run_identity(NOW, ROWS))
    assert jsonstore.load(data_feeds.IDENTITY_FILE, None) is None


def test_openfigi_mismatch_becomes_one_grouped_ops_watch_finding(env, monkeypatch):
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "1")
    data = {**FIGI, "US67066G1040": [{"ticker": "NVDA", "name": "NVIDIA CORP", "exchCode": "GY"}],   # ticker changed
            "FR0000120271": []}                                                                         # no listing any more
    serve_figi(env, data)
    run(data_feeds.run_identity(NOW, paper_universe.entries()))
    bad = data_feeds.identity_mismatches(NOW + 3600)
    assert sorted((m["id"], m["kind"]) for m in bad) == [("NVDA", "ticker"), ("TTE", "listing")]
    [f] = ops_watch.identity_findings(NOW + 3600)
    assert f["key"] == "identity-mismatch" and "2 mismatch" in f["title"]
    assert "NVDA ticker" in f["body"] and "TTE listing" in f["body"] and "nothing is changed automatically" in f["body"]


def test_identity_finding_lists_six_and_counts_the_rest(env, monkeypatch):
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "1")
    jsonstore.save(data_feeds.IDENTITY_FILE, {"at": NOW, "month": "2026-10", "checked": 9, "errors": 0, "mismatches": [
        {"id": f"X{i}", "kind": "ticker", "detail": "d"} for i in range(9)]})
    [f] = ops_watch.identity_findings(NOW)
    assert f["title"].startswith("Registry identity: 9") and "X5 ticker" in f["body"] and "X6" not in f["body"] and "+3 more" in f["body"]


def test_identity_finding_clears_with_a_clean_audit_expires_after_two_weeks_and_needs_the_worker(env, monkeypatch):
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "1")
    jsonstore.save(data_feeds.IDENTITY_FILE, {"at": NOW, "month": "2026-10", "checked": 1, "errors": 0,
                                              "mismatches": [{"id": "A", "kind": "name", "detail": "d"}]})
    assert ops_watch.identity_findings(NOW + data_feeds.IDENTITY_FINDING_S - 1)
    assert ops_watch.identity_findings(NOW + data_feeds.IDENTITY_FINDING_S + 1) == []
    jsonstore.save(data_feeds.IDENTITY_FILE, {"at": NOW, "month": "2026-10", "checked": 1, "errors": 0, "mismatches": []})
    assert ops_watch.identity_findings(NOW) == []
    jsonstore.save(data_feeds.IDENTITY_FILE, {"at": NOW, "month": "2026-10", "checked": 1, "errors": 0,
                                              "mismatches": [{"id": "A", "kind": "name", "detail": "d"}]})
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "0")
    assert ops_watch.identity_findings(NOW) == []


def test_identity_finding_alerts_dedupes_and_resolves_in_run_pass(env, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "1")
    sent, stored, clock = [], [], {"now": NOW}

    async def collect():
        return [], {"source": "bridge", "lane": None}

    async def alert(ticker, source, title, body, **kw):
        sent.append((source, title))
        return ops_watch.notify.Delivery(ops_watch.notify.SENT)

    monkeypatch.setattr(ops_watch, "STATE_FILE", tmp_path / "ops_state.json")
    monkeypatch.setattr(ops_watch, "collect", collect)
    monkeypatch.setattr(ops_watch, "stale_worker_findings", lambda now=None: [])
    monkeypatch.setattr(ops_watch, "paper_findings", lambda now=None: [])
    monkeypatch.setattr(ops_watch, "claude_findings", lambda now=None: [])
    monkeypatch.setattr(ops_watch.notify, "alert", alert)
    monkeypatch.setattr(ops_watch.notify, "store", lambda *a, **k: stored.append(a) or "row")
    monkeypatch.setattr(ops_watch.runlog, "record", lambda *a, **k: None)
    monkeypatch.setattr(ops_watch, "time", SimpleNamespace(time=lambda: clock["now"]))
    jsonstore.save(data_feeds.IDENTITY_FILE, {"at": NOW, "month": "2026-10", "checked": 1, "errors": 0,
                                              "mismatches": [{"id": "A", "kind": "name", "detail": "d"}]})
    assert run(ops_watch.run_pass())["fired"] == ["identity-mismatch"]
    clock["now"] += 3600
    assert run(ops_watch.run_pass())["fired"] == []                                    # deduped for 24 h
    jsonstore.save(data_feeds.IDENTITY_FILE, {"at": clock["now"], "month": "2026-10", "checked": 1, "errors": 0,
                                              "mismatches": []})
    clock["now"] += 3600
    assert run(ops_watch.run_pass())["resolved"] == ["identity-mismatch"]
    assert len(sent) == 1


# ------------------------------------------------------------------ wiring that has bitten before

def test_the_worker_is_listed_in_the_supervisor_and_watched_for_staleness(monkeypatch):
    assert data_feeds in main.BACKGROUND_MODULES
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "1")
    assert ("data_feeds", data_feeds.WATCH_INTERVAL_S) in ops_watch._watched_workers()
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "0")
    assert "data_feeds" not in [n for n, _ in ops_watch._watched_workers()]


def test_gate_follows_the_forecaster_unless_set_explicitly(monkeypatch):
    for k in ("DATA_FEEDS_ENABLED", "FORECAST_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert not data_feeds.enabled()
    monkeypatch.setenv("FORECAST_ENABLED", "1")
    assert data_feeds.enabled()
    monkeypatch.setenv("DATA_FEEDS_ENABLED", "0")
    assert not data_feeds.enabled()


# ------------------------------------------------------------------ forex: the ECB reference rate as a provider

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ecb_answer(day, usd_per_eur):
    return httpx.Response(200, content=ecb_csv([(day, usd_per_eur)]))


def test_ecb_provider_inverts_the_reference_rate():
    today = datetime.datetime.now(UTC).date()

    async def go():
        async with _client(lambda r: _ecb_answer((today - datetime.timedelta(days=1)).isoformat(), 1.25)) as c:
            return await forex._ecb_latest(c)

    assert run(go()) == pytest.approx(0.8)


def test_ecb_provider_refuses_a_dead_feed_and_a_non_200():
    old = (datetime.datetime.now(UTC).date() - datetime.timedelta(days=forex.ECB_MAX_AGE_DAYS + 1)).isoformat()

    async def go(handler):
        async with _client(handler) as c:
            return await forex._ecb_latest(c)

    assert run(go(lambda r: _ecb_answer(old, 1.25))) is None
    assert run(go(lambda r: httpx.Response(503))) is None


def test_chain_goes_frankfurter_then_ecb_then_yahoo(monkeypatch):
    today = datetime.datetime.now(UTC).date().isoformat()
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.frankfurter.dev":
            return httpx.Response(500)
        return _ecb_answer(today, 1.25)

    def yahoo(period):
        raise AssertionError("yahoo must not be reached while the ECB answers")

    async def go():
        async with _client(handler) as c:
            monkeypatch.setattr(forex, "_client_for_loop", lambda: c)
            monkeypatch.setattr(forex, "_yahoo_eurusd_blocking", yahoo)
            return await forex._fetch_latest()

    rate, source = run(go())
    assert source == "ecb" and rate == pytest.approx(0.8)
    assert seen == ["api.frankfurter.dev", "data-api.ecb.europa.eu"]


def test_us_lead_names_get_their_xetra_quote_refreshed_only_during_the_overlap(env, monkeypatch):
    # us_lead needs a Xetra quote <= 30 min old; nothing else quotes names the buckets do not hold.
    import app.api.live_ws as lw
    asked = []

    async def quote(pid):
        asked.append(pid)
        return {"price": 1.0}
    monkeypatch.setattr(paper_universe, "us_symbols", lambda: {"MSFT": "MSFT", "AAPL": "AAPL"})
    monkeypatch.setattr(paper_universe, "listing_quote", quote)
    open_ = {"v": False}
    monkeypatch.setattr(lw, "_session_open", lambda mic, now: open_["v"])
    assert run(data_feeds._step_us_quotes(NOW, NOW + 60, {})) is None and asked == []      # markets not both open
    open_["v"] = True
    assert run(data_feeds._step_us_quotes(NOW, NOW + 60, {})) == (2, 2, 0)
    assert sorted(asked) == ["AAPL", "MSFT"]
    assert run(data_feeds._step_us_quotes(NOW + 60, NOW + 120, {})) is None                 # spaced 5 minutes
    assert run(data_feeds._step_us_quotes(NOW + data_feeds.US_QUOTES_EVERY_S, NOW + 999, {})) == (2, 2, 0)


def test_a_listing_missing_in_openfigi_passes_when_trading_212_mapped_the_isin():
    row = {"id": "INPST", "isin": "LU2290522684", "yahoo": "INPST.AS", "venue": "XAMS", "name": "InPost"}
    assert [m["kind"] for m in data_feeds.compare_identity(row, [])] == ["listing"]
    assert data_feeds.compare_identity({**row, "t212": "INPSTa_EQ"}, []) == []
