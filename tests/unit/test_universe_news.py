"""Universe news (docs/plans/forecaster.md): sources, per-source health / spacing / backoff, the point-in-time
store, dedupe, cadence and the worker pass. Every network seam is faked (``_get``, ``prices._yf``, the Finnhub call,
the GDELT pacer, the clocks): no network, no sleeps, no lane.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime
import email.utils
import json
from zoneinfo import ZoneInfo

import pytest

from app import main
from app.api import event_cards as ec
from app.api import jsonstore, lane_client, ops_watch, paper_universe, topnews
from app.api import universe_news as un
from app.api.providers import finnhub

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime.datetime(2026, 10, 21, 10, 0).timestamp()          # container-local Wednesday 10:00 (hot window)
UTC = datetime.timezone.utc

ROWS = [
    {"id": "SAP", "name": "SAP", "isin": "DE0007164600", "yahoo": "SAP.DE", "venue": "XETR", "kind": "equity",
     "sector": "Information Technology", "role": "candidate", "t212": None},
    {"id": "ASML", "name": "ASML Holding", "isin": "NL0010273215", "yahoo": "ASML.AS", "venue": "XAMS", "kind": "equity",
     "sector": "Information Technology", "role": "candidate", "t212": None},
    {"id": "NVDA", "name": "NVIDIA", "isin": "US67066G1040", "yahoo": "NVD.DE", "venue": "XETR", "kind": "equity",
     "sector": "Information Technology", "role": "candidate", "t212": None},
    {"id": "TTE", "name": "TotalEnergies", "isin": "FR0000120271", "yahoo": "TTE.PA", "venue": "XPAR", "kind": "equity",
     "sector": "Energy", "role": "candidate", "t212": None},
    {"id": "EUNL", "name": "World ETF", "isin": "IE00B4L5Y983", "yahoo": "EUNL.DE", "venue": "XETR", "kind": "etf",
     "sector": "ETF", "role": "benchmark", "t212": None},
]


def run(coro):
    return asyncio.run(coro)


def row(pid):
    return next(r for r in paper_universe.entries() if r["id"] == pid)


def rows_by_id():
    return {r["id"]: r for r in paper_universe.entries()}


def berlin(y, m, d, h=0, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=BERLIN).timestamp()


def local(y, m, d, h=0, mi=0):
    return datetime.datetime(y, m, d, h, mi).timestamp()


def c(title, hours_before=1.0, domain="reuters.com", url="https://www.reuters.com/a", lang="en"):
    """A source candidate published ``hours_before`` before NOW."""
    return {"title": title, "url": url, "domain": domain, "published": NOW - hours_before * 3600, "lang": lang}


def rss(items):
    body = ""
    for it in items:
        stamp = email.utils.format_datetime(datetime.datetime.fromtimestamp(it["published"], UTC), usegmt=True)
        body += (f"<item><title>{it['title']}</title><link>{it.get('link', 'https://news.google.com/rss/articles/x')}</link>"
                 f"<pubDate>{stamp}</pubDate><source url=\"{it.get('source_url', 'https://www.reuters.com')}\">"
                 f"{it.get('publisher', 'Reuters')}</source></item>")
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


def gdelt(items):
    arts = [{"url": it.get("url", "https://www.ft.com/x"), "title": it["title"], "domain": it.get("domain", "ft.com"),
             "seendate": datetime.datetime.fromtimestamp(it["published"], UTC).strftime("%Y%m%dT%H%M%SZ"),
             "language": it.get("language", "English")} for it in items]
    return json.dumps({"articles": arts})


class World:
    """What the fakes answer, and what was asked."""

    def __init__(self):
        self.yf = {}                       # yahoo symbol -> list | None (a failure) | Exception (a failure)
        self.calendar = {}                 # yahoo symbol -> (date | None,) | Exception
        self.google = lambda params: (200, rss([]))
        self.gdelt = lambda params: (200, gdelt([]))
        self.finnhub = lambda symbol, frm, to: (finnhub.OK, [])
        self.topnews = {}
        self.focus = ([], [])
        self.http, self.yf_calls, self.cal_calls, self.finnhub_calls, self.sleeps, self.records = [], [], [], [], [], []
        self.mono = 0.0

    def google_calls(self):
        return [p for u, p in self.http if u == un.GOOGLE_NEWS]

    def gdelt_calls(self):
        return [p for u, p in self.http if u == topnews.GDELT_DOC]


@pytest.fixture
def env(monkeypatch, tmp_path):
    un.reset_state()
    ec.reset_state()
    for k in ("UNIVERSE_NEWS_ENABLED", "FORECAST_ENABLED", "EVENT_CARDS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("UNIVERSE_NEWS_ENABLED", "1")
    monkeypatch.setenv("EVENT_CARDS_ENABLED", "0")
    monkeypatch.setenv("UNIVERSE_NEWS_YAHOO", "1")
    store = tmp_path / "universe.json"
    jsonstore.save(store, {"asOf": "2026-10-21", "source": "test", "entries": ROWS})
    monkeypatch.setattr(paper_universe, "STORE_FILE", store)
    monkeypatch.setattr(paper_universe, "SEED_FILE", tmp_path / "no-seed.json")
    monkeypatch.setattr(un, "NEWS_DIR", tmp_path / "news")
    monkeypatch.setattr(un, "EARNINGS_FILE", tmp_path / "news" / "earnings.jsonl")
    monkeypatch.setattr(un, "STATE_FILE", tmp_path / "news" / "state.json")
    monkeypatch.setattr(ec, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(ec, "QUEUE_FILE", tmp_path / "cards" / "queue.json")
    monkeypatch.setattr(topnews, "GDELT_BACKOFF_FILE", str(tmp_path / "gdelt_backoff.json"))
    monkeypatch.setattr(topnews, "_gdelt_until_ts", 0.0)
    monkeypatch.setattr(finnhub, "API_KEY", "test-key")
    w = World()
    clock = {"now": NOW}
    w.clock = clock

    async def fake_yf(key, fn, *args):
        if fn is un._yf_news_blocking:
            w.yf_calls.append(args[0])
            v = w.yf.get(args[0], [])
        else:
            w.cal_calls.append(args[0])
            v = w.calendar.get(args[0], (None,))
        return None if isinstance(v, Exception) else v        # prices._yf answers None for any failure

    async def fake_get(url, params=None):
        w.http.append((url, params))
        handler = w.google if url == un.GOOGLE_NEWS else w.gdelt
        res = handler(params)
        if isinstance(res, Exception):
            raise res
        return res

    async def fake_finnhub(symbol, frm, to):
        w.finnhub_calls.append((symbol, frm, to))
        return w.finnhub(symbol, frm, to)

    async def fake_sleep(s):
        w.sleeps.append(s)
        w.mono += s

    async def no_pace():
        return None

    monkeypatch.setattr(un, "_now", lambda: clock["now"])
    monkeypatch.setattr(un, "_mono", lambda: w.mono)
    monkeypatch.setattr(un, "_sleep", fake_sleep)
    monkeypatch.setattr(un, "_get", fake_get)
    monkeypatch.setattr(un, "_gdelt_pace", no_pace)
    monkeypatch.setattr(un, "_finnhub_news", fake_finnhub)
    monkeypatch.setattr(un.prices, "_yf", fake_yf)
    monkeypatch.setattr(un.topnews, "get_top_news", lambda t=None: w.topnews.get(t, {}))
    monkeypatch.setattr(un, "focus_ids", lambda: w.focus)
    monkeypatch.setattr(un.runlog, "record", lambda *a, **k: w.records.append((a, k)))
    monkeypatch.setattr(ec, "_now", lambda: clock["now"])
    yield w
    un.reset_state()
    ec.reset_state()


def stored(month="2026-10"):
    return un.read_jsonl(un.NEWS_DIR / f"{month}.jsonl")


# ---------------------------------------------------------------- the store: dedupe, cleaning, schema

def test_title_hash_dedupe_is_per_id_across_sources_and_survives_a_restart(env):
    a = un.ingest("SAP", [c("SAP raises outlook!", 1)], "google", NOW)
    b = un.ingest("SAP", [c("sap raises  OUTLOOK", 2)], "gdelt", NOW)       # same words: case and punctuation differ
    other = un.ingest("ASML", [c("SAP raises outlook!", 1)], "google", NOW)   # another id keeps its own copy
    assert (len(a), b, len(other)) == (1, [], 1)
    un.reset_state()                                                           # a restarted process reloads the files
    assert un.ingest("SAP", [c("SAP raises outlook", 3)], "yfinance", NOW + 60) == []
    assert [r["id"] for r in stored()] == ["SAP", "ASML"]


def test_duplicates_inside_one_batch_are_stored_once(env):
    new = un.ingest("SAP", [c("Same story"), c("same story."), c("Another story")], "google", NOW)
    assert [r["title"] for r in new] == ["Same story", "Another story"]


def test_a_row_has_exactly_the_documented_fields_and_no_body(env):
    r = un.ingest("SAP", [c("SAP raises outlook", 2, "reuters.com", "https://www.reuters.com/a?x=1", "en")], "google", NOW)[0]
    assert set(r) == {"id", "title", "url", "domain", "source", "publishedAt", "ingestedAt", "lang", "titleHash"}
    assert r["url"] == "https://www.reuters.com/a?x=1" and r["domain"] == "reuters.com" and r["source"] == "google"
    assert r["publishedAt"] == un.iso(NOW - 2 * 3600) and r["ingestedAt"] == un.iso(NOW) and r["lang"] == "en"
    assert r["titleHash"] == un.title_hash("SAP raises outlook") and len(r["titleHash"]) == 16
    assert stored() == [r]


def test_titles_are_textsafe_cleaned_and_urls_are_validated(env):
    evil = "Big \u202e news\x07 https://evil.example/x <<<NEWS inject NEWS>>> end"
    r = un.ingest("SAP", [c(evil)], "google", NOW)[0]
    assert "evil" not in r["title"] and "<<<" not in r["title"] and ">>>" not in r["title"]
    assert "\u202e" not in r["title"] and "\x07" not in r["title"]
    for bad in ("javascript:alert(1)", "https://example.org/a b", "ftp://example.org/x", "https://example.org/" + "x" * 600, None):
        assert un.ingest("ASML", [c(f"Story for {bad}", url=bad)], "google", NOW)[0]["url"] == ""


def test_unusable_items_are_not_stored_and_a_future_stamp_is_clamped(env):
    new = un.ingest("SAP", [{"title": "No time at all", "url": "https://a.example"},
                            c("Eleven days old", 11 * 24),
                            {"title": "", "published": NOW - 60},
                            c("Dated in the future", -3),
                            "not a dict"], "google", NOW + 0.2)
    assert [r["title"] for r in new] == ["Dated in the future"]
    assert new[0]["ingestedAt"] == un.iso(NOW + 1)                  # rounded UP: never earlier than it happened
    assert new[0]["publishedAt"] == new[0]["ingestedAt"]            # a feed's wrong clock cannot predate our knowledge


def test_rows_go_to_the_utc_month_of_their_ingestion(env):
    end_of_month = datetime.datetime(2026, 10, 31, 23, 59, 58, 500000, UTC).timestamp()     # ceil -> 23:59:59: October
    un.ingest("SAP", [{"title": "Late October", "published": end_of_month - 60}], "google", end_of_month)
    un.ingest("SAP", [{"title": "Early November", "published": end_of_month - 60}], "google", end_of_month + 1)
    assert [r["title"] for r in stored("2026-10")] == ["Late October"]
    assert [r["title"] for r in stored("2026-11")] == ["Early November"]


def test_a_failed_write_marks_nothing_seen_so_the_next_pass_retries(env, monkeypatch):
    real = un.append_jsonl
    monkeypatch.setattr(un, "append_jsonl", lambda path, rows: False)
    assert un.ingest("SAP", [c("Retry me")], "google", NOW) == []
    monkeypatch.setattr(un, "append_jsonl", real)
    assert [r["title"] for r in un.ingest("SAP", [c("Retry me")], "google", NOW + 5)] == ["Retry me"]


def test_an_interrupted_line_does_not_glue_two_rows_together(env):
    un.ingest("SAP", [c("First")], "google", NOW)
    path = un.NEWS_DIR / "2026-10.jsonl"
    path.write_text(path.read_text() + '{"id": "SAP", "title": "half a li', encoding="utf-8")
    un.reset_state()
    un.ingest("SAP", [c("Second")], "google", NOW + 1)
    assert [r["title"] for r in stored()] == ["First", "Second"]


# ---------------------------------------------------------------- point in time

def test_pit_news_needs_both_stamps_before_the_cutoff_and_has_no_url(env):
    cutoff = NOW
    un.ingest("SAP", [c("Known", 5)], "google", cutoff - 600)
    un.ingest("SAP", [c("Stored later", 4)], "google", cutoff + 600)                   # ingested after the cutoff
    un.ingest("SAP", [c("Published later", -1)], "google", cutoff - 100)               # clamped to ingestion: known
    un.ingest("SAP", [c("Eight days old", 8 * 24)], "google", cutoff - 600)
    got = un.pit_news("SAP", cutoff)
    assert [g["headline"] for g in got] == ["Published later", "Known"]
    assert all(set(g) == {"headline", "domain", "source", "published_at", "ingested_at"} for g in got)
    assert all(g["published_at"] <= cutoff and g["ingested_at"] <= cutoff for g in got)
    assert {g["headline"] for g in un.pit_news("SAP", cutoff + 3600)} >= {"Stored later"}
    assert un.pit_news("ASML", cutoff) == []


def test_pit_news_reads_across_a_month_boundary(env):
    cutoff = datetime.datetime(2026, 11, 2, 12, 0, tzinfo=UTC).timestamp()
    old = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=UTC).timestamp()
    mid = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()
    late = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=UTC).timestamp()
    for title, at in (("In October", old), ("In November", mid), ("After the cutoff", late)):
        un.ingest("SAP", [{"title": title, "published": at - 60}], "google", at)
    assert [g["headline"] for g in un.pit_news("SAP", cutoff)] == ["In November", "In October"]


def test_recent_lists_newest_ingested_first_with_an_optional_id(env):
    un.ingest("SAP", [c("Sap one")], "google", NOW - 300)
    un.ingest("ASML", [c("Asml one")], "google", NOW - 200)
    un.ingest("SAP", [c("Sap two")], "google", NOW - 100)
    assert [r["title"] for r in un.recent(None, 10, NOW)] == ["Sap two", "Asml one", "Sap one"]
    assert [r["title"] for r in un.recent("SAP", 1, NOW)] == ["Sap two"]
    assert un.recent("SAP", 10, NOW)[0]["url"].startswith("https://")


# ---------------------------------------------------------------- parsers and what each source is asked

def test_yfinance_items_in_both_shapes(env):
    new = {"id": "1", "content": {"title": "SAP lifts outlook", "pubDate": "2026-10-21T07:30:00Z",
                                  "provider": {"displayName": "Reuters"},
                                  "canonicalUrl": {"url": "https://finance.yahoo.com/news/sap-1.html", "lang": "en-US"}}}
    old = {"title": "Old shape headline", "publisher": "Handelsblatt", "link": "https://www.handelsblatt.com/x",
           "providerPublishTime": 1_790_000_000}
    a, b = un._parse_yf_item(new), un._parse_yf_item(old)
    assert (a["title"], a["domain"], un._lang(a["lang"])) == ("SAP lifts outlook", "reuters", "en")   # Yahoo only relays
    assert un._epoch(a["published"]) == datetime.datetime(2026, 10, 21, 7, 30, tzinfo=UTC).timestamp()
    assert (b["domain"], un._epoch(b["published"])) == ("handelsblatt.com", 1_790_000_000.0)
    assert un._parse_yf_item("junk") is None


def test_google_rss_title_suffix_domain_and_time(env):
    xml = rss([{"title": "SAP wins a big deal - Reuters", "published": NOW - 3600}])
    [cand] = un._parse_google(xml, "en")
    assert cand["title"] == "SAP wins a big deal" and cand["domain"] == "reuters.com" and cand["lang"] == "en"
    assert un._epoch(cand["published"]) == NOW - 3600
    with pytest.raises(un.SourceError) as e:
        un._parse_google("this is not xml", "en")
    assert e.value.kind == "parse"


def test_google_is_asked_in_english_and_the_home_language_by_company_name(env):
    for pid in ("SAP", "ASML", "TTE", "NVDA", "EUNL"):
        env.http.clear()
        run(un.fetch_name(row(pid)))
        asked = [(p["hl"], p["gl"], p["ceid"], p["q"]) for p in env.google_calls()]
        expected = {"SAP": [("en-US", "US", "US:en", '"SAP" when:3d'), ("de", "DE", "DE:de", '"SAP" when:3d')],
                    "ASML": [("en-US", "US", "US:en", '"ASML Holding" when:3d'),
                             ("nl", "NL", "NL:nl", '"ASML Holding" when:3d')],
                    "TTE": [("en-US", "US", "US:en", '"TotalEnergies" TTE when:3d'),
                            ("fr", "FR", "FR:fr", '"TotalEnergies" TTE when:3d')],
                    "NVDA": [("en-US", "US", "US:en", '"NVIDIA" NVDA when:3d')],
                    "EUNL": []}[pid]
        assert asked == expected, pid


def test_gdelt_query_is_the_company_name_and_short_names_are_skipped(env):
    run(un.fetch_name(row("ASML")))
    run(un.fetch_name(row("NVDA")))
    run(un.fetch_name(row("SAP")))                                   # "SAP" is below GDELT's 4-character floor
    assert [p["query"] for p in env.gdelt_calls()] == ['"ASML Holding"', "NVIDIA"]
    assert env.gdelt_calls()[0]["sort"] == "DateDesc" and env.gdelt_calls()[0]["format"] == "json"
    assert un.health()["gdelt"]["skipped"] == 1


def test_gdelt_articles_become_candidates_with_language_and_publisher_domain(env):
    env.gdelt = lambda p: (200, gdelt([{"title": "NVIDIA beats estimates", "published": NOW - 600, "domain": "www.ft.com",
                                         "language": "German"}]))
    res = run(un.fetch_name(row("NVDA")))
    r = next(x for x in res["new"] if x["source"] == "gdelt")
    assert (r["domain"], r["lang"], r["title"]) == ("ft.com", "de", "NVIDIA beats estimates")


def test_finnhub_is_only_asked_for_us_names_with_a_key(env):
    run(un.fetch_name(row("SAP")))
    run(un.fetch_name(row("EUNL")))
    assert env.finnhub_calls == []
    run(un.fetch_name(row("NVDA")))
    symbol, frm, to = env.finnhub_calls[0]
    today = datetime.datetime.fromtimestamp(NOW, UTC).date()
    assert (symbol, to, frm) == ("NVDA", today.isoformat(), (today - datetime.timedelta(days=2)).isoformat())
    env.finnhub_calls.clear()
    finnhub.API_KEY = ""
    try:
        run(un.fetch_name(row("NVDA")))
    finally:
        finnhub.API_KEY = "test-key"
    assert env.finnhub_calls == []


def test_finnhub_items_use_the_publisher_not_the_relay_host(env):
    env.finnhub = lambda s, f, t: (finnhub.OK, [{"headline": "NVIDIA ships Blackwell", "url": "https://finnhub.io/api/news?id=1",
                                                 "source": "Yahoo", "datetime": int(NOW - 900)}])
    res = run(un.fetch_name(row("NVDA")))
    r = next(x for x in res["new"] if x["source"] == "finnhub")
    assert r["domain"] == "yahoo" and r["lang"] == "en" and r["publishedAt"] == un.iso(NOW - 900)


def test_the_topnews_cache_feeds_watchlist_ids_and_is_read_from_disk_only(env):
    env.topnews["SAP"] = {"ts": NOW, "source": "finnhub", "items": [
        {"headline": "Cached SAP story", "url": "https://www.handelsblatt.com/x", "source": "Handelsblatt",
         "published_at": int(NOW - 1800)}]}
    res = run(un.fetch_name(row("SAP")))
    r = next(x for x in res["new"] if x["source"] == "topnews")
    assert (r["title"], r["domain"]) == ("Cached SAP story", "handelsblatt.com")
    assert un.health()["topnews"]["ok"] == 1 and un.health()["topnews"]["skipped"] == 0
    run(un.fetch_name(row("ASML")))                                  # no cache entry: skipped, not failed
    assert un.health()["topnews"]["skipped"] == 1 and un.health()["topnews"]["fail"] == 0


# ---------------------------------------------------------------- per-source failure isolation

def test_one_failing_source_costs_only_its_own_items(env):
    env.yf["ASML.AS"] = RuntimeError("yahoo down")
    env.google = lambda p: (500, "")
    env.gdelt = lambda p: (200, gdelt([{"title": "GDELT only story", "published": NOW - 300}]))
    env.topnews["ASML"] = {"items": [{"headline": "Cache only story", "url": "https://x.example/a", "source": "X",
                                      "published_at": int(NOW - 200)}]}
    res = run(un.fetch_name(row("ASML")))
    assert {r["title"] for r in res["new"]} == {"GDELT only story", "Cache only story"}
    h = un.health()
    assert h["yfinance"]["fail"] == 1 and h["google"]["fail"] == 2 and h["gdelt"]["ok"] == 1 and h["topnews"]["ok"] == 1
    assert (res["attempted"], res["ok"]) == (5, 2)                    # yfinance, google en + nl, gdelt, topnews


def test_a_malformed_payload_or_a_raising_source_is_contained(env):
    env.google = lambda p: (200, "<<not xml")
    env.gdelt = lambda p: (200, "Please limit requests to one every 5 seconds.")
    env.yf["ASML.AS"] = [{"title": "Yahoo story", "publisher": "Reuters", "link": "https://www.reuters.com/y",
                          "providerPublishTime": int(NOW - 60)}]
    res = run(un.fetch_name(row("ASML")))
    assert [r["title"] for r in res["new"]] == ["Yahoo story"]
    assert un.health()["google"]["lastError"].startswith("parse") and un.health()["gdelt"]["lastError"].startswith("parse")
    env.google = lambda p: ValueError("boom")                         # an unexpected exception type inside a source
    res = run(un.fetch_name(row("TTE")))
    assert un.health()["google"]["lastError"] in ("network: ValueError",)
    assert res["attempted"] >= 3


def test_yfinance_returning_none_is_a_failure_but_an_empty_list_is_a_success(env):
    env.yf["EUNL.DE"] = None
    run(un.fetch_name(row("EUNL")))
    assert un.health()["yfinance"]["fail"] == 1 and un.health()["yfinance"]["ok"] == 0
    env.yf["EUNL.DE"] = []
    run(un.fetch_name(row("EUNL")))
    assert un.health()["yfinance"]["ok"] == 1 and un.health()["yfinance"]["consecutiveFails"] == 0


# ---------------------------------------------------------------- rate limits, spacing, backoff

def test_a_rate_limit_backs_the_source_off_at_once_and_the_pause_doubles(env):
    env.google = lambda p: (429, "")
    run(un.fetch_name(row("SAP")))
    h = un.health()["google"]
    assert h["fail"] == 1 and h["backoffUntil"] == NOW + un.BACKOFF_BASE_S and h["lastError"].startswith("rate_limited")
    assert len(env.google_calls()) == 1                                # the German call was not even tried
    run(un.fetch_name(row("SAP")))
    assert len(env.google_calls()) == 1                                # still backing off: no request at all
    env.clock["now"] = NOW + un.BACKOFF_BASE_S + 1
    run(un.fetch_name(row("SAP")))
    assert len(env.google_calls()) == 2
    h = un.health()["google"]
    assert h["consecutiveFails"] == 2 and h["backoffUntil"] == NOW + un.BACKOFF_BASE_S + 1 + 2 * un.BACKOFF_BASE_S


def test_other_errors_back_off_only_after_three_in_a_row_and_success_resets(env):
    env.google = lambda p: (500, "")
    run(un.fetch_name(row("NVDA")))
    run(un.fetch_name(row("NVDA")))
    assert un.health()["google"]["backoffUntil"] == 0.0 and un.health()["google"]["fail"] == 2
    run(un.fetch_name(row("NVDA")))
    assert un.health()["google"]["backoffUntil"] == NOW + un.BACKOFF_BASE_S
    env.clock["now"] = NOW + 10 * un.BACKOFF_BASE_S
    env.google = lambda p: (200, rss([]))
    run(un.fetch_name(row("NVDA")))
    assert un.health()["google"]["consecutiveFails"] == 0
    env.google = lambda p: (500, "")
    run(un.fetch_name(row("NVDA")))
    assert un.health()["google"]["backoffUntil"] <= env.clock["now"]  # one failure after a success: no pause


def test_the_backoff_is_capped(env):
    for _ in range(20):
        un.note_fail("gdelt", "rate_limited", "", NOW)
    assert un.health()["gdelt"]["backoffUntil"] == NOW + un.BACKOFF_MAX_S


def test_a_gdelt_429_sets_the_shared_marker_and_other_names_skip_gdelt(env):
    env.gdelt = lambda p: (429, "")
    run(un.fetch_name(row("ASML")))
    assert len(env.gdelt_calls()) == 1 and un.health()["gdelt"]["fail"] == 1
    marker = json.loads(open(topnews.GDELT_BACKOFF_FILE).read())
    assert marker["until_ts"] > un.time.time() + topnews.GDELT_BACKOFF_S - 60       # the file topnews reads too
    env.clock["now"] = NOW + 10_000                                       # our own pause is over, GDELT's marker is not
    run(un.fetch_name(row("NVDA")))
    assert len(env.gdelt_calls()) == 1 and un.health()["gdelt"]["skipped"] == 1


def test_a_marker_already_on_disk_keeps_gdelt_untouched(env):
    topnews._set_gdelt_backoff(900)
    run(un.fetch_name(row("ASML")))
    assert env.gdelt_calls() == [] and un.health()["gdelt"]["fail"] == 0 and un.health()["gdelt"]["skipped"] == 1


def test_requests_to_one_source_keep_their_minimum_spacing(env):
    run(un.fetch_name(row("SAP")))                                    # google en then google de, same instant
    assert env.sleeps == [un.SPACING_S["google"]]
    env.sleeps.clear()
    run(un._pace("finnhub"))
    run(un._pace("finnhub"))                                          # straight after the first: waits the gap
    assert env.sleeps == [un.SPACING_S["finnhub"]]
    env.mono += 10
    run(un._pace("finnhub"))                                          # long after: no wait
    assert env.sleeps == [un.SPACING_S["finnhub"]]


# ---------------------------------------------------------------- cadence

def test_hot_window_edges():
    assert [un.in_hot_window(local(2026, 10, 21, h, m)) for h, m in ((6, 59), (7, 0), (21, 59), (22, 0))] == [False, True, True, False]


def test_held_and_top_names_every_30_minutes_in_the_window_the_rest_every_3_hours():
    last = {"A": NOW - 31 * 60, "B": NOW - 29 * 60, "C": NOW - 31 * 60, "D": NOW - 3.1 * 3600, "E": NOW - 2.9 * 3600}
    assert un.plan(NOW, list("ABCDE"), ["A", "B"], last) == ["A", "D"]
    assert un.plan(NOW, ["Z", "Y", "A"], ["Z"], {}) == ["Z", "A", "Y"]         # never fetched = due; hot first
    night = local(2026, 10, 21, 23, 0)
    assert un.plan(night, ["A", "B"], ["A", "B"], {"A": night - 31 * 60, "B": night - 3.01 * 3600}) == ["B"]


def test_the_most_overdue_name_of_a_group_goes_first():
    last = {"A": NOW - 5 * 3600, "B": NOW - 9 * 3600, "C": NOW - 7 * 3600}
    assert un.plan(NOW, ["A", "B", "C"], [], last) == ["B", "C", "A"]


@pytest.mark.parametrize("when,expected_open", [
    (berlin(2026, 10, 23, 14, 59), None),
    (berlin(2026, 10, 23, 15, 0), berlin(2026, 10, 23, 15, 0)),
    (berlin(2026, 10, 23, 17, 29), berlin(2026, 10, 23, 15, 0)),
    (berlin(2026, 10, 23, 17, 30), None),                                # the packets freeze at the close
    (berlin(2026, 10, 22, 16, 0), None),                                 # Thursday of a normal week
    (berlin(2026, 10, 24, 12, 0), None),                                 # the weekend
    (berlin(2026, 4, 2, 16, 0), berlin(2026, 4, 2, 15, 0)),              # Good Friday: Thursday closes the week
    (berlin(2026, 4, 3, 16, 0), None),
])
def test_the_full_sweep_window_is_the_last_two_and_a_half_hours_of_the_weeks_last_session(when, expected_open):
    assert un.sweep_start(when) == expected_open


def test_inside_the_sweep_every_name_not_fetched_since_it_opened_is_due():
    now = berlin(2026, 10, 23, 16, 0)
    opened = un.sweep_start(now)
    last = {"A": opened + 60, "B": opened - 60, "C": now - 60}
    assert un.plan(now, ["A", "B", "C"], [], last) == ["B"]
    assert un.plan(now, ["A", "B"], [], {"A": opened + 60, "B": opened + 120}) == []


# ---------------------------------------------------------------- the worker pass

def test_a_pass_fetches_hot_names_first_remembers_them_and_then_idles(env):
    env.focus = (["NVDA"], ["SAP"])
    ok, note, acted = run(un._tick())
    assert ok and acted
    assert env.yf_calls == ["NVD.DE", "SAP.DE", "ASML.AS", "EUNL.DE", "TTE.PA"]
    last = jsonstore.load(un.STATE_FILE)["last"]
    assert set(last) == {"SAP", "ASML", "NVDA", "TTE", "EUNL"} and set(last.values()) == {NOW}
    env.yf_calls.clear()
    ok, note, acted = run(un._tick())
    assert (ok, note, acted) == (True, "idle", False) and env.yf_calls == []


def test_a_restart_does_not_sweep_names_that_were_just_fetched(env):
    run(un._tick())
    un.reset_state()
    env.yf_calls.clear()
    run(un._tick())
    assert env.yf_calls == []


def test_the_pass_stops_starting_names_after_its_budget_and_resumes_next_time(env, monkeypatch):
    seen = []

    async def slow(row_):
        seen.append(row_["id"])
        env.mono += 100
        return {"new": [], "attempted": 1, "ok": 1}

    monkeypatch.setattr(un, "fetch_name", slow)
    monkeypatch.setattr(un, "PASS_BUDGET_S", 150)
    ok, note, _ = run(un._tick())
    assert len(seen) == 2 and "budget reached" in note
    run(un._tick())
    assert len(set(seen)) == 4                                          # the next pass continues with the rest


def test_names_whose_sources_are_all_backing_off_are_retried_not_marked_done(env):
    for s in un.SOURCES:
        un._h(s)["backoffUntil"] = NOW + 3600
    ok, note, acted = run(un._tick())
    assert ok and not jsonstore.load(un.STATE_FILE)["last"]
    assert env.yf_calls == [] and env.http == []


def test_every_source_failing_makes_the_pass_fail_visibly(env):
    for r in ROWS:
        env.yf[r["yahoo"]] = RuntimeError("down")
    env.google = lambda p: (500, "")
    env.gdelt = lambda p: (500, "")
    env.finnhub = lambda s, f, t: (finnhub.RATE_LIMITED, None)
    ok, note, _ = run(un._tick())
    assert not ok and "every source failed" in note
    assert run(un._pass()) is True and env.records[-1][0][0] == "universe_news"


def test_one_crashing_name_does_not_stop_the_others(env, monkeypatch):
    real = un.fetch_name

    async def flaky(row_):
        if row_["id"] == "SAP":
            raise RuntimeError("bug")
        return await real(row_)

    monkeypatch.setattr(un, "fetch_name", flaky)
    ok, note, _ = run(un._tick())
    assert not ok and "SAP: RuntimeError" in note
    assert set(jsonstore.load(un.STATE_FILE)["last"]) == {"ASML", "NVDA", "TTE", "EUNL"}


def test_ingestion_is_not_blocked_by_a_hanging_lane_and_only_fresh_focus_items_are_queued(env, monkeypatch):
    monkeypatch.setenv("EVENT_CARDS_ENABLED", "1")
    env.focus = (["SAP"], [])
    env.google = lambda p: (200, rss([{"title": "SAP fresh story", "published": NOW - 3600},
                                      {"title": "SAP three days old", "published": NOW - 3 * 86400}]))
    env.yf["ASML.AS"] = [{"title": "ASML not in focus", "publisher": "Reuters", "link": "https://www.reuters.com/z",
                          "providerPublishTime": int(NOW - 60)}]
    hang = {}

    async def chat(*a, **k):
        hang["called"] = True
        await asyncio.Event().wait()

    monkeypatch.setattr(lane_client, "chat", chat)
    monkeypatch.setattr(lane_client, "can_start", lambda purpose, turns=1: True)

    async def main_():
        out = await un._tick()                                           # returns although the lane never answers
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task = un._cards_task
        assert task is not None and not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return out

    ok, note, acted = run(main_())
    assert ok and hang == {"called": True}
    titles = {r["title"] for r in stored()}
    assert {"SAP fresh story", "SAP three days old", "ASML not in focus"} <= titles       # all ingested
    queued = {i["title"] for i in json.loads(ec.QUEUE_FILE.read_text())["items"]}
    assert queued == {"SAP fresh story"}                                                 # focus + published within 48 h


# ---------------------------------------------------------------- earnings observations

def test_earnings_are_checked_daily_and_each_success_is_a_point_in_time_observation(env):
    env.calendar["SAP.DE"] = ("2026-11-05",)
    env.calendar["NVD.DE"] = RuntimeError("no calendar")
    done = run(un._earnings_step(rows_by_id(), NOW, 1e9))
    assert done == 3 and sorted(env.cal_calls) == ["ASML.AS", "NVD.DE", "SAP.DE", "TTE.PA"]    # the ETF is never asked
    assert un.next_earnings("SAP", NOW + 1) == {"date": "2026-11-05", "fetchedAt": NOW}
    assert un.next_earnings("SAP", NOW - 1) is None                       # nothing was known before it was fetched
    assert un.next_earnings("ASML", NOW + 1) == {"date": None, "fetchedAt": NOW}     # a fetch with no upcoming date
    assert un.next_earnings("NVDA", NOW + 1) is None and un.health()["calendar"]["fail"] == 1
    env.cal_calls.clear()
    run(un._earnings_step(rows_by_id(), NOW + 3600, 1e9))
    assert env.cal_calls == ["NVD.DE"]                                    # only the failed one is due again
    run(un._earnings_step(rows_by_id(), NOW + un.EARNINGS_EVERY_S + 1, 1e9))
    assert len(env.cal_calls) >= 4


def earnings_step_at(env, when):
    env.clock["now"] = when
    return run(un._earnings_step(rows_by_id(), when, 1e9))


def test_an_empty_calendar_carries_the_last_future_date_until_it_passes(env):
    env.calendar["SAP.DE"] = ("2026-10-23",)
    earnings_step_at(env, NOW)
    env.calendar["SAP.DE"] = (None,)                                      # yahoo drops the date
    day2 = berlin(2026, 10, 22, 9)
    earnings_step_at(env, day2)
    assert un.next_earnings("SAP", day2 + 1) == {"date": "2026-10-23", "fetchedAt": day2, "source": "carried"}
    assert un.next_earnings("SAP", NOW + 1) == {"date": "2026-10-23", "fetchedAt": NOW}     # the first sighting is untouched
    day3 = berlin(2026, 10, 23, 9)                                        # the day of the report is not "passed" yet
    earnings_step_at(env, day3)
    assert un.next_earnings("SAP", day3 + 1) == {"date": "2026-10-23", "fetchedAt": day3, "source": "carried"}
    day4 = berlin(2026, 10, 24, 9)                                        # passed: the calendar is honestly empty
    earnings_step_at(env, day4)
    assert un.next_earnings("SAP", day4 + 1) == {"date": None, "fetchedAt": day4}
    assert [r["source"] for r in un.read_jsonl(un.EARNINGS_FILE) if r["id"] == "SAP"] == [
        "yfinance.calendar", "carried", "carried", "yfinance.calendar"]


def test_a_newer_calendar_date_replaces_the_carried_one_and_a_failed_fetch_records_nothing(env):
    env.calendar["SAP.DE"] = ("2026-10-30",)
    earnings_step_at(env, NOW)
    env.calendar["SAP.DE"] = RuntimeError("down")                         # a failure is not an empty calendar
    day2 = berlin(2026, 10, 22, 9)
    earnings_step_at(env, day2)
    assert [r["date"] for r in un.read_jsonl(un.EARNINGS_FILE) if r["id"] == "SAP"] == ["2026-10-30"]
    env.calendar["SAP.DE"] = ("2026-11-04",)                              # the company moved the date
    day3 = berlin(2026, 10, 23, 9)
    earnings_step_at(env, day3)
    assert un.next_earnings("SAP", day3 + 1) == {"date": "2026-11-04", "fetchedAt": day3}


def test_a_name_that_never_had_a_date_is_not_given_one(env):
    earnings_step_at(env, NOW)
    earnings_step_at(env, berlin(2026, 10, 22, 9))
    assert {r["source"] for r in un.read_jsonl(un.EARNINGS_FILE)} == {"yfinance.calendar"}
    assert un.carried_earnings("ASML", "2026-10-22") is None


def test_yf_calendar_parsing_takes_the_earliest_date_on_or_after_today(monkeypatch):
    import datetime as dt
    cals = {"A": {"Earnings Date": [dt.date(2026, 10, 10), dt.date(2026, 11, 7), dt.date(2026, 11, 5)]},
            "B": {}, "C": None, "D": {"Earnings Date": [dt.date(2026, 10, 1)]}, "E": {"Earnings Date": dt.date(2026, 12, 1)},
            "F": {"Earnings Date": [dt.datetime(2026, 11, 3, 9, 0)]}, "G": {"Earnings Date": ["2026-11-09"]}}

    class T:
        def __init__(self, sym):
            self.calendar = cals[sym]

    monkeypatch.setattr(un.yf, "Ticker", T)
    got = {k: un._yf_calendar_blocking(k, "2026-10-21") for k in cals}
    assert got == {"A": ("2026-11-05",), "B": (None,), "C": (None,), "D": (None,), "E": ("2026-12-01",),
                   "F": ("2026-11-03",), "G": ("2026-11-09",)}


# ---------------------------------------------------------------- gate, status, ops watch, route

def test_the_gate_follows_forecast_enabled_unless_set_explicitly(monkeypatch):
    for k in ("UNIVERSE_NEWS_ENABLED", "FORECAST_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert un.enabled() is False
    monkeypatch.setenv("FORECAST_ENABLED", "1")
    assert un.enabled() is True
    monkeypatch.setenv("UNIVERSE_NEWS_ENABLED", "0")
    assert un.enabled() is False
    monkeypatch.setenv("FORECAST_ENABLED", "0")
    monkeypatch.setenv("UNIVERSE_NEWS_ENABLED", "1")
    assert un.enabled() is True


def test_a_disabled_worker_does_not_start_and_status_reports_sources(env, monkeypatch):
    monkeypatch.setenv("UNIVERSE_NEWS_ENABLED", "0")
    un.start()
    assert un._task is None and un.status()["running"] is False
    st = un.status()
    assert set(st["sources"]) == set(un.SOURCES) and st["interval_s"] == un.WATCH_INTERVAL_S
    assert st["interval_s"] >= un.POLL_S + un.PASS_BUDGET_S and st["eventCards"]["queued"] == 0


def test_yahoo_news_is_disabled_by_default_and_reported_as_disabled_not_failing(env, monkeypatch):
    monkeypatch.delenv("UNIVERSE_NEWS_YAHOO")
    assert all(s != "yfinance" for s, _, _ in un.calls_for(row("SAP")))
    run(un.fetch_name(row("SAP")))
    h = un.health()["yfinance"]
    assert h["disabled"] is True and h["fail"] == 0 and h["lastError"] is None
    assert un.status()["sources"]["yfinance"]["disabled"] is True
    monkeypatch.setenv("UNIVERSE_NEWS_YAHOO", "1")
    assert un.calls_for(row("SAP"))[0][0] == "yfinance" and "disabled" not in un.health()["yfinance"]


def test_ops_watch_flags_the_worker_only_while_it_is_enabled(monkeypatch):
    monkeypatch.setenv("UNIVERSE_NEWS_ENABLED", "1")
    assert ("universe_news", un.WATCH_INTERVAL_S) in ops_watch._watched_workers()
    monkeypatch.setenv("UNIVERSE_NEWS_ENABLED", "0")
    assert all(n != "universe_news" for n, _ in ops_watch._watched_workers())


def test_news_route_lists_the_store_and_validates_its_input(env):
    assert un in main.BACKGROUND_MODULES
    un.ingest("SAP", [c("Sap headline")], "google", NOW - 100)
    un.ingest("ASML", [c("Asml headline")], "google", NOW - 50)

    def call(**kw):
        args = {"book": "practice", "id": "", "limit": 50}
        args.update(kw)
        return run(main.paper_news(**args))

    allrows = json.loads(call().body)
    assert [r["title"] for r in allrows["items"]] == ["Asml headline", "Sap headline"]
    assert allrows["items"][0]["url"].startswith("https://") and set(allrows["sources"]) == set(un.SOURCES)
    one = json.loads(call(id="sap", limit=1).body)
    assert one["id"] == "SAP" and [r["title"] for r in one["items"]] == ["Sap headline"]
    assert call(book="real").status_code == 400 and call(book="").status_code == 400
    assert call(id="../etc").status_code == 400 and call(id="NOPE").status_code == 404
    assert len(json.loads(call(limit=0).body)["items"]) == 1                 # limit clamps to 1..200
