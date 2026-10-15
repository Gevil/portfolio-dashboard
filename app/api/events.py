"""Calendar of upcoming market events: ``GET /api/events?days=60``.

``build(days)`` -> ``{asOf, events: [{date, time|null, kind, ticker|null, title,
source, url|null}], warnings: []}`` sorted by date. ``days`` is clamped to
1..365; the window is today .. today+days (inclusive, container-local date).

Sources (each isolated: a failing source adds a ``warnings`` entry and never
fails the endpoint):

* ``earnings``: ``macro.earnings()`` (Nasdaq calendar cache, watchlist symbols);
* ``ex_dividend`` / ``dividend_pay``: yfinance ``Ticker(listing symbol).calendar``
  for each holding, in the executor, cached 24 h in memory (a failure is cached
  for one hour so a dead provider is not hammered);
* ``market_closed``: ``calendars.closures`` for XAMS, XETR and XNYS (weekday
  holidays; early closes are not listed);
* ``fomc`` / ``ecb``: the static tables below, transcribed from the official
  pages (each carries its URL and the date it was retrieved);
* ``cpi_us``: FRED ``release/dates`` for release 10 (Consumer Price Index),
  key from env ``FRED_API_KEY`` read at call time and never logged or put in a
  warning; cached 24 h; omitted with a warning on any failure.
"""
import asyncio
import datetime
import logging
import os
import time

import httpx

from app.api import calendars, macro, registry

log = logging.getLogger("events")

MIN_DAYS, MAX_DAYS = 1, 365
CACHE_TTL_S = 24 * 3600
FAIL_TTL_S = 3600
YF_TIMEOUT_S = 20.0
FRED_TIMEOUT_S = 15.0
FRED_URL = "https://api.stlouisfed.org/fred/release/dates"
FRED_RELEASE_ID = 10
CPI_URL = "https://fred.stlouisfed.org/release?rid=10"
CLOSURE_MICS = (("XAMS", "Euronext Amsterdam"), ("XETR", "Xetra"),
                ("XNYS", "NYSE"))

# FOMC 2026-2027 scheduled meetings (first day, decision day), copied from the
# official calendar; each date is tentative until confirmed at the meeting
# before it (the page says so).
FOMC_SOURCE_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_RETRIEVED = "2026-10-15"
FOMC_MEETINGS = (
    ("2026-01-27", "2026-01-28"), ("2026-03-17", "2026-03-18"),
    ("2026-04-28", "2026-04-29"), ("2026-06-16", "2026-06-17"),
    ("2026-07-28", "2026-07-29"), ("2026-09-15", "2026-09-16"),
    ("2026-10-27", "2026-10-28"), ("2026-12-08", "2026-12-09"),
    ("2027-01-26", "2027-01-27"), ("2027-03-16", "2027-03-17"),
    ("2027-04-27", "2027-04-28"), ("2027-06-08", "2027-06-09"),
    ("2027-07-27", "2027-07-28"), ("2027-09-14", "2027-09-15"),
    ("2027-10-26", "2027-10-27"), ("2027-12-07", "2027-12-08"),
)

# ECB Governing Council monetary-policy meetings (day 1, day 2 = decision and
# press conference) as listed on the official calendar page on the retrieval
# date. The page lists only meetings from 2026-10-28 on for 2026; earlier 2026
# meetings are past and deliberately not transcribed from memory.
ECB_SOURCE_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
ECB_RETRIEVED = "2026-10-15"
ECB_MEETINGS = (
    ("2026-10-28", "2026-10-29"), ("2026-12-16", "2026-12-17"),
    ("2027-02-03", "2027-02-04"), ("2027-03-17", "2027-03-18"),
    ("2027-04-28", "2027-04-29"), ("2027-06-09", "2027-06-10"),
    ("2027-07-21", "2027-07-22"), ("2027-09-08", "2027-09-09"),
    ("2027-10-27", "2027-10-28"), ("2027-12-15", "2027-12-16"),
)

# key -> (monotonic-free wall time, value); value None = the fetch failed.
_div_cache: dict[str, tuple[float, dict | None]] = {}
_cpi_cache: tuple[float, list[str] | None] = (0.0, None)


def _today() -> datetime.date:
    return datetime.date.today()


def _as_date(value) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _fresh(stamp: float, value) -> bool:
    ttl = CACHE_TTL_S if value is not None else FAIL_TTL_S
    return time.time() - stamp < ttl


def _event(day: datetime.date, kind: str, title: str, source: str, *,
           ticker: str | None = None, when: str | None = None,
           url: str | None = None) -> dict:
    return {"date": day.isoformat(), "time": when, "kind": kind,
            "ticker": ticker, "title": title, "source": source, "url": url}


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

_EARNINGS_TIME = {"time-pre-market": "pre-market", "time-after-hours": "after-hours"}


async def _earnings_events(start, end) -> tuple[list[dict], list[str]]:
    data = await macro.earnings()
    out = []
    for key, row in (data or {}).items():
        day = _as_date(row.get("date")) if isinstance(row, dict) else None
        if day is None:
            continue
        out.append(_event(day, "earnings", f"{key} earnings", "nasdaq",
                          ticker=str(key).upper(),
                          when=_EARNINGS_TIME.get(str(row.get("time") or ""))))
    return out, []


def _yf_calendar_blocking(sym: str) -> dict:
    """Executor only: ``{"ex": date|None, "pay": date|None}``."""
    import yfinance as yf
    cal = yf.Ticker(sym).calendar
    if not isinstance(cal, dict):
        return {"ex": None, "pay": None}
    return {"ex": _as_date(cal.get("Ex-Dividend Date")),
            "pay": _as_date(cal.get("Dividend Date"))}


async def _dividend_dates(ident: str) -> dict:
    """Cached ex/pay dates of one holding; raises when the fetch failed."""
    hit = _div_cache.get(ident)
    if hit and _fresh(*hit):
        if hit[1] is None:
            raise RuntimeError("recent failure")
        return hit[1]
    sym = registry.listing_symbol(ident)
    try:
        loop = asyncio.get_running_loop()
        value = await asyncio.wait_for(
            loop.run_in_executor(None, _yf_calendar_blocking, sym), YF_TIMEOUT_S)
    except Exception:
        _div_cache[ident] = (time.time(), None)
        raise
    _div_cache[ident] = (time.time(), value)
    return value


async def _dividend_events(start, end) -> tuple[list[dict], list[str]]:
    ids = [i for i in registry.holdings() if registry.listing_symbol(i)]
    results = await asyncio.gather(*(_dividend_dates(i) for i in ids),
                                   return_exceptions=True)
    out, warnings = [], []
    for ident, res in zip(ids, results):
        if isinstance(res, BaseException):
            warnings.append(f"dividends {ident}: unavailable "
                            f"({type(res).__name__})")
            continue
        if res.get("ex"):
            out.append(_event(res["ex"], "ex_dividend", f"{ident} ex-dividend",
                              "yfinance", ticker=ident))
        if res.get("pay"):
            out.append(_event(res["pay"], "dividend_pay",
                              f"{ident} dividend payment", "yfinance",
                              ticker=ident))
    return out, warnings


async def _closure_events(start, end) -> tuple[list[dict], list[str]]:
    out = []
    for mic, name in CLOSURE_MICS:
        for day in calendars.closures(mic, start, end):
            out.append(_event(day, "market_closed",
                              f"{name} closed ({mic})", "exchange_calendars"))
    return out, []


def static_events(start: datetime.date, end: datetime.date) -> list[dict]:
    """FOMC and ECB decision days inside [start, end] from the static tables."""
    out = []
    for first, decision in FOMC_MEETINGS:
        day = datetime.date.fromisoformat(decision)
        if start <= day <= end:
            out.append(_event(
                day, "fomc",
                f"FOMC rate decision (meeting {first[5:]} to {decision[5:]})",
                "federalreserve.gov", url=FOMC_SOURCE_URL))
    for first, decision in ECB_MEETINGS:
        day = datetime.date.fromisoformat(decision)
        if start <= day <= end:
            out.append(_event(
                day, "ecb",
                f"ECB monetary policy decision (meeting {first[5:]} to "
                f"{decision[5:]})", "ecb.europa.eu", url=ECB_SOURCE_URL))
    return out


async def _static_events(start, end) -> tuple[list[dict], list[str]]:
    return static_events(start, end), []


async def _fetch_cpi_dates(today: datetime.date) -> list[str]:
    """FRED CPI release dates (ISO) from ``today`` on. Raises on any failure;
    exception text is never forwarded (an httpx error carries the URL, which
    carries the key)."""
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    params = {"release_id": FRED_RELEASE_ID, "api_key": key,
              "file_type": "json", "sort_order": "asc",
              "include_release_dates_with_no_data": "true",
              "realtime_start": today.isoformat()}
    async with httpx.AsyncClient(timeout=FRED_TIMEOUT_S) as client:
        r = await client.get(FRED_URL, params=params)
    if not r.is_success:
        raise RuntimeError(f"HTTP {r.status_code}")
    rows = r.json().get("release_dates")
    if not isinstance(rows, list):
        raise RuntimeError("unexpected FRED payload")
    return sorted({d.isoformat() for d in
                   (_as_date(row.get("date")) for row in rows
                    if isinstance(row, dict)) if d})


async def _cpi_events(start, end) -> tuple[list[dict], list[str]]:
    global _cpi_cache
    stamp, dates = _cpi_cache
    if not _fresh(stamp, dates):
        try:
            dates = await _fetch_cpi_dates(start)
        except Exception as e:
            _cpi_cache = (time.time(), None)
            return [], [f"cpi_us: unavailable ({type(e).__name__}"
                        + (f": {e}" if isinstance(e, RuntimeError) else "") + ")"]
        _cpi_cache = (time.time(), dates)
    if dates is None:
        return [], ["cpi_us: unavailable (recent failure)"]
    return [_event(d, "cpi_us", "US CPI release", "fred", url=CPI_URL)
            for d in map(_as_date, dates) if d and start <= d <= end], []


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------

def clamp_days(days) -> int:
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 60
    return max(MIN_DAYS, min(MAX_DAYS, n))


async def _guard(name: str, fn, start, end) -> tuple[list[dict], list[str]]:
    try:
        return await fn(start, end)
    except Exception as e:
        log.warning("events: %s failed: %s", name, type(e).__name__)
        return [], [f"{name}: unavailable ({type(e).__name__})"]


async def build(days=60) -> dict:
    n = clamp_days(days)
    start = _today()
    end = start + datetime.timedelta(days=n)
    sources = (("earnings", _earnings_events), ("dividends", _dividend_events),
               ("closures", _closure_events), ("central_banks", _static_events),
               ("cpi_us", _cpi_events))
    parts = await asyncio.gather(*(_guard(name, fn, start, end)
                                   for name, fn in sources))
    events, warnings = [], []
    for evs, warns in parts:
        events.extend(e for e in evs
                      if start.isoformat() <= e["date"] <= end.isoformat())
        warnings.extend(warns)
    events.sort(key=lambda e: (e["date"], e["time"] or "", e["kind"],
                               e["ticker"] or "", e["title"]))
    return {"asOf": datetime.datetime.now().isoformat(timespec="seconds"),
            "events": events, "warnings": warnings}
