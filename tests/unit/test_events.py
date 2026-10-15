"""``events.build``: merge, sort, clamp, isolate failing sources."""
import asyncio
import datetime

import httpx
import pytest

from app.api import events

TODAY = datetime.date(2026, 10, 15)


def d(n):
    return TODAY + datetime.timedelta(days=n)


class Fakes:
    def __init__(self, monkeypatch):
        self.closure_calls, self.yf_calls, self.cpi_calls = [], [], 0
        self.earnings = {"ASML": {"symbol": "ASML", "date": d(9).isoformat(),
                                  "time": "time-after-hours"}}
        self.closures = {"XAMS": [d(40)], "XETR": [d(40), d(5)], "XNYS": []}
        self.calendar = {"ASML.AS": {"ex": d(3), "pay": d(20)},
                         "SAP.DE": {"ex": None, "pay": None}}
        self.cpi = [d(12).isoformat(), d(-30).isoformat()]
        monkeypatch.setattr(events, "_today", lambda: TODAY)
        monkeypatch.setattr(events, "_div_cache", {})
        monkeypatch.setattr(events, "_cpi_cache", (0.0, None))
        monkeypatch.setattr(events.macro, "earnings", self.macro_earnings)
        monkeypatch.setattr(events.calendars, "closures", self.closures_for)
        monkeypatch.setattr(events.registry, "holdings", lambda: ["ASML", "SAP"])
        monkeypatch.setattr(events.registry, "listing_symbol",
                            lambda i: {"ASML": "ASML.AS", "SAP": "SAP.DE"}[i])
        monkeypatch.setattr(events, "_yf_calendar_blocking", self.yf_calendar)
        monkeypatch.setattr(events, "_fetch_cpi_dates", self.fetch_cpi)

    async def macro_earnings(self):
        if isinstance(self.earnings, Exception):
            raise self.earnings
        return self.earnings

    def closures_for(self, mic, start, end):
        self.closure_calls.append((mic, start, end))
        return [x for x in self.closures[mic] if start <= x <= end]

    def yf_calendar(self, sym):
        self.yf_calls.append(sym)
        res = self.calendar[sym]
        if isinstance(res, Exception):
            raise res
        return res

    async def fetch_cpi(self, today):
        self.cpi_calls += 1
        if isinstance(self.cpi, Exception):
            raise self.cpi
        return [x for x in self.cpi if x >= today.isoformat()]


@pytest.fixture
def f(monkeypatch):
    return Fakes(monkeypatch)


def build(days=60):
    return asyncio.run(events.build(days))


def kinds(res):
    return [(e["date"], e["kind"]) for e in res["events"]]


def test_events_from_every_source_are_merged_and_sorted_by_date(f):
    res = build(60)
    assert res["warnings"] == []
    dates = [e["date"] for e in res["events"]]
    assert dates == sorted(dates)
    assert {e["kind"] for e in res["events"]} == {
        "earnings", "ex_dividend", "dividend_pay", "market_closed", "fomc", "ecb",
        "cpi_us"}
    # the payload shape of contract C9
    assert set(res) == {"asOf", "events", "warnings"}
    for e in res["events"]:
        assert set(e) == {"date", "time", "kind", "ticker", "title", "source", "url"}
    earn = next(e for e in res["events"] if e["kind"] == "earnings")
    assert earn["ticker"] == "ASML" and earn["time"] == "after-hours"
    assert earn["date"] == d(9).isoformat()
    ex = next(e for e in res["events"] if e["kind"] == "ex_dividend")
    assert (ex["ticker"], ex["date"]) == ("ASML", d(3).isoformat())
    # the past CPI date from FRED is outside the window and not listed
    assert [e["date"] for e in res["events"] if e["kind"] == "cpi_us"] == [d(12).isoformat()]


def test_same_day_events_sort_deterministically(f):
    f.closures = {"XAMS": [d(5)], "XETR": [d(5)], "XNYS": [d(5)]}
    res = build(10)
    day5 = [e["title"] for e in res["events"] if e["date"] == d(5).isoformat()]
    assert day5 == sorted(day5)


def test_days_is_clamped_to_1_365():
    assert events.clamp_days(0) == 1 and events.clamp_days(-5) == 1
    assert events.clamp_days(9999) == 365 and events.clamp_days("30") == 30
    assert events.clamp_days("junk") == 60 and events.clamp_days(None) == 60


def test_window_follows_the_clamped_days(f):
    f.closures = {"XAMS": [d(1), d(2)], "XETR": [], "XNYS": []}
    res = build(0)                                    # clamped to 1 day
    closed = [e["date"] for e in res["events"] if e["kind"] == "market_closed"]
    assert closed == [d(1).isoformat()]
    mic, start, end = f.closure_calls[0]
    assert (start, end) == (TODAY, d(1))
    res = build(100000)                               # clamped to 365 days
    assert f.closure_calls[-1][2] == d(365)
    # FOMC 2026-12-09 is inside 365 days, 2027-12-08 (+419 d) is not
    fomc = [e["date"] for e in res["events"] if e["kind"] == "fomc"]
    assert "2026-12-09" in fomc and "2027-12-08" not in fomc


def test_a_failing_source_becomes_a_warning_and_the_rest_survives(f):
    f.earnings = RuntimeError("nasdaq down")
    f.cpi = RuntimeError("no fred")
    res = build(60)
    assert res["warnings"] == ["earnings: unavailable (RuntimeError)",
                               "cpi_us: unavailable (RuntimeError: no fred)"]
    got = {e["kind"] for e in res["events"]}
    assert "earnings" not in got and "cpi_us" not in got
    assert {"ex_dividend", "market_closed", "fomc", "ecb"} <= got


def test_one_failing_dividend_ticker_warns_but_keeps_the_others(f):
    f.calendar["SAP.DE"] = TimeoutError()
    res = build(60)
    assert res["warnings"] == ["dividends SAP: unavailable (TimeoutError)"]
    assert any(e["kind"] == "ex_dividend" and e["ticker"] == "ASML"
               for e in res["events"])


def test_fred_key_is_never_leaked_into_warnings(f, monkeypatch):
    monkeypatch.setattr(events, "_fetch_cpi_dates", events_fetch_real)
    monkeypatch.setenv("FRED_API_KEY", "s3cr3tkey")

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            raise httpx.ConnectError(f"{url}?api_key={params['api_key']}")
    monkeypatch.setattr(events.httpx, "AsyncClient", Boom)
    res = build(60)
    assert res["warnings"] == ["cpi_us: unavailable (ConnectError)"]
    assert "s3cr3tkey" not in repr(res)


events_fetch_real = events._fetch_cpi_dates


def test_missing_fred_key_is_a_warning(f, monkeypatch):
    monkeypatch.setattr(events, "_fetch_cpi_dates", events_fetch_real)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    res = build(60)
    assert res["warnings"] == ["cpi_us: unavailable (RuntimeError: FRED_API_KEY not set)"]
    assert not [e for e in res["events"] if e["kind"] == "cpi_us"]


def test_closures_cover_amsterdam_xetra_and_nyse(f):
    res = build(60)
    assert [c[0] for c in f.closure_calls] == ["XAMS", "XETR", "XNYS"]
    closed = [(e["date"], e["title"]) for e in res["events"]
              if e["kind"] == "market_closed"]
    assert closed == [(d(5).isoformat(), "Xetra closed (XETR)"),
                      (d(40).isoformat(), "Euronext Amsterdam closed (XAMS)"),
                      (d(40).isoformat(), "Xetra closed (XETR)")]


def test_static_central_bank_dates_inside_the_window(f):
    res = build(30)                                   # 2026-10-15 .. 2026-11-14
    cb = [(e["date"], e["kind"], e["url"], e["source"]) for e in res["events"]
          if e["kind"] in ("fomc", "ecb")]
    assert cb == [
        ("2026-10-28", "fomc", events.FOMC_SOURCE_URL, "federalreserve.gov"),
        ("2026-10-29", "ecb", events.ECB_SOURCE_URL, "ecb.europa.eu")]


def test_static_tables_are_internally_consistent():
    fomc = [datetime.date.fromisoformat(b) for _, b in events.FOMC_MEETINGS]
    ecb = [datetime.date.fromisoformat(b) for _, b in events.ECB_MEETINGS]
    assert all(x.weekday() == 2 for x in fomc)          # FOMC decisions: Wednesday
    assert all(x.weekday() == 3 for x in ecb)           # ECB decisions: Thursday
    assert sum(x.year == 2026 for x in fomc) == 8 and sum(x.year == 2027 for x in fomc) == 8
    for first, last in events.FOMC_MEETINGS + events.ECB_MEETINGS:
        gap = datetime.date.fromisoformat(last) - datetime.date.fromisoformat(first)
        assert gap == datetime.timedelta(days=1)
    assert fomc == sorted(fomc) and ecb == sorted(ecb)
    assert events.FOMC_RETRIEVED and events.ECB_RETRIEVED


def test_dividend_and_cpi_lookups_are_cached(f):
    build(60)
    build(60)
    assert sorted(f.yf_calls) == ["ASML.AS", "SAP.DE"]   # once per ticker, not per call
    assert f.cpi_calls == 1


def test_a_failed_dividend_fetch_is_not_retried_within_the_hour(f):
    f.calendar["SAP.DE"] = TimeoutError()
    build(60)
    build(60)
    assert f.yf_calls.count("SAP.DE") == 1
