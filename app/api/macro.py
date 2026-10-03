"""Macro panel: US Treasury yield curve, FRED rates, earnings calendar.

All sources live-verified (P4 endpoint report):
- Treasury daily-treasury-rates CSV (browser UA, MM/DD/YYYY, empty cells).
- FRED series/observations (keyed; values are strings, "." = missing).
- Nasdaq earnings calendar per calendar-day (browser UA mandatory; per-symbol
  filter unsupported -> forward-scan day by day until the watchlist hits).
- CFTC "Traders in Futures" Socrata dataset (6dca-aqww) — S&P 500 COT rows
  for the market-view panel (report week is weekly, Tuesday publication).

Everything is cache-first: the endpoints never block on a cold upstream for
longer than one refresh; background loops refresh the curve daily and the
earnings scan weekly. Needs ``FRED_API_KEY`` for the FRED rows (curve works
without it).
"""
import asyncio
import calendar as _calendar
import csv
import datetime
import io
import logging
import os
import pathlib
import time

import httpx

from app.api import jsonstore, runlog

log = logging.getLogger("macro")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
CURVE_CACHE = DATA_DIR / "macro_curve.json"
COT_CACHE = DATA_DIR / "macro_cot.json"
EARNINGS_CACHE = DATA_DIR / "earnings.json"
# Was missing entirely: refresh_fred() and status() both reference it, so every
# /api/background call died with NameError (the auth middleware's broad try
# dressed it up as a 401). Supplied by env.secrets FRED_API_KEY; FRED rows
# degrade to [] without it.
FRED_KEY = os.getenv("FRED_API_KEY", "")

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{year}/all"
                "?type=daily_treasury_yield_curve"
                "&field_tdr_date_value={year}&page&_format=csv")
NASDAQ_CAL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
EARNINGS_SCAN_DAYS = 45
EARNINGS_REFRESH_S = 7 * 86400
CURVE_REFRESH_S = 86400
COT_REFRESH_S = 86400
COT_LIMIT = 5
# Legacy futures-only COT. The %S%500% filter picks up E-MINI S&P 500 /
# S&P 500 Consolidated (live-verified 2026-09-12, latest week 2026-36).
COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
COT_PARAMS = {
    "$limit": COT_LIMIT,
    "$order": "report_date_as_yyyy_mm_dd DESC",
    "$where": "upper(contract_market_name) like upper('%S%500%')",
}
# CFTC's own column names (``noncomm_postions_spread_all`` is their typo).
COT_FIELDS = {
    "date": "report_date_as_yyyy_mm_dd",
    "market": "contract_market_name",
    "exchange": "market_and_exchange_names",
    "open_interest": "open_interest_all",
    "noncomm_long": "noncomm_positions_long_all",
    "noncomm_short": "noncomm_positions_short_all",
    "noncomm_spread": "noncomm_postions_spread_all",
    "comm_long": "comm_positions_long_all",
    "comm_short": "comm_positions_short_all",
}
COT_TEXT_FIELDS = frozenset({"date", "market", "exchange"})


# ------------------------------------------------------------------ calendar
# Trading-day gate (digest batching, scoreboard horizons). No new pip deps:
# ``exchange-calendars`` would drag a pandas-sized tree in for what is a fixed
# rule table plus one computus. Holidays are DERIVED per year (fixed dates +
# nth-weekday rules + Easter movables) instead of being pasted as a literal
# list — a literal silently treats any year outside its range as a full
# working year, which is exactly the failure mode that would fire a digest on
# Christmas.

CAL_YEARS = range(datetime.date.today().year - 1,
                  datetime.date.today().year + 3)


def _easter(year: int) -> datetime.date:
    """Easter Sunday — anonymous Gregorian (Meeus/Jones/Butcher) computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - computus naming
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def _observed(day: datetime.date) -> datetime.date:
    """US-style observance: a fixed holiday on Saturday moves to Friday, on
    Sunday to Monday (what NYSE publishes; Euronext simply stays closed)."""
    if day.weekday() == 5:
        return day - datetime.timedelta(days=1)
    if day.weekday() == 6:
        return day + datetime.timedelta(days=1)
    return day


def _weekday_n(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """n-th (or n==-1: last) ``weekday`` (Mon=0) of ``month``."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    if n > 0:
        return first + datetime.timedelta(days=offset + 7 * (n - 1))
    last = datetime.date(year, month,
                         _calendar.monthrange(year, month)[1])
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def _year_holidays(market: str, year: int) -> set:
    """Exchange closures for one year (weekend-only closures dropped: they are
    not trading days anyway and would only bloat the set)."""
    e = _easter(year)
    if market == "XAMS":
        # Euronext Amsterdam: Good Friday, Easter Monday, Ascension, Whit
        # Monday + King's Day (Apr 27), Liberation Day (May 5), 1st + 2nd
        # Christmas day. No observance shifts (a Sunday holiday just stays one).
        days = {datetime.date(year, 1, 1), datetime.date(year, 4, 27),
                datetime.date(year, 5, 5), datetime.date(year, 12, 25),
                datetime.date(year, 12, 26),
                e - datetime.timedelta(days=2), e + datetime.timedelta(days=1),
                e + datetime.timedelta(days=39),
                e + datetime.timedelta(days=50)}
    else:
        days = {_observed(datetime.date(year, 1, 1)),
                _observed(datetime.date(year, 6, 19)),
                _observed(datetime.date(year, 7, 4)),
                _observed(datetime.date(year, 12, 25)),
                _weekday_n(year, 1, 0, 3),      # Martin Luther King Jr. Day
                _weekday_n(year, 2, 0, 3),      # Presidents Day
                _weekday_n(year, 5, 0, -1),     # Memorial Day
                _weekday_n(year, 9, 0, 1),      # Labor Day
                _weekday_n(year, 11, 3, 4),     # Thanksgiving
                e - datetime.timedelta(days=2)}  # Good Friday
    return {d for d in days if d.weekday() < 5}


HOLIDAYS: dict = {m: set().union(*(_year_holidays(m, y) for y in CAL_YEARS))
                  for m in ("XNYS", "XAMS")}


def holidays(market: str, year: int) -> set:
    """Closure set for one market/year, computed on demand so any horizon walk
    reaching outside ``CAL_YEARS`` still gets real holidays."""
    return _year_holidays(market if market in ("XNYS", "XAMS") else "XNYS",
                          year)


def is_trading_day(market: str, day: datetime.date) -> bool:
    """Mon-Fri and not an exchange closure."""
    return day.weekday() < 5 and day not in holidays(market, day.year)


def market_for_ticker(ticker: str) -> str:
    """XAMS for Euronext-listed symbols (the same membership the Euronext
    quote provider uses), XNYS otherwise — including indices."""
    from app.api import live_ws
    return "XAMS" if live_ws.is_eu_symbol(ticker) else "XNYS"


def next_trading_day(market: str, day: datetime.date) -> datetime.date:
    """First trading day strictly after ``day``."""
    nxt = day + datetime.timedelta(days=1)
    while not is_trading_day(market, nxt):
        nxt += datetime.timedelta(days=1)
    return nxt


def advance_trading_days(market: str, day: datetime.date, n: int) -> datetime.date:
    """The trading day ``n`` sessions after ``day`` (n<=0 returns ``day``)."""
    out = day
    for _ in range(max(0, n)):
        out = next_trading_day(market, out)
    return out


def trading_days_between(market: str, start: datetime.date,
                         end: datetime.date) -> list:
    """Trading days in [start, end] (empty when inverted)."""
    out, cur = [], start
    while cur <= end:
        if is_trading_day(market, cur):
            out.append(cur)
        cur += datetime.timedelta(days=1)
    return out

_client: httpx.AsyncClient | None = None
_tasks: list[asyncio.Task] = []
_stats = {"curve_last": 0.0, "earnings_last": 0.0, "cot_last": 0.0,
          "errors": 0}


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": BROWSER_UA}, timeout=30,
            follow_redirects=True)
    return _client


def _num(x: str | None) -> float | None:
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


async def refresh_curve() -> dict | None:
    year = datetime.date.today().year
    try:
        r = await _client_get().get(TREASURY_URL.format(year=year))
        if not r.is_success:
            log.warning("treasury HTTP %s", r.status_code)
            return None
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if not rows:
            return None
        latest = rows[0]  # descending by date
        curve = {"date": latest.get("Date", ""),
                 "fetched": time.time(),
                 "yields": {k: _num(v) for k, v in latest.items() if k != "Date"}}
        y2, y10 = curve["yields"].get("2 Yr"), curve["yields"].get("10 Yr")
        curve["spread2s10s"] = (round(y10 - y2, 2)
                                if y2 is not None and y10 is not None else None)
        if not jsonstore.save(CURVE_CACHE, curve):
            log.warning("macro curve cache not persisted")
        _stats["curve_last"] = time.time()
        return curve
    except (httpx.HTTPError, csv.Error) as e:
        _stats["errors"] += 1
        log.warning("treasury refresh failed: %s", e)
        return None


async def refresh_fred(series: str, limit: int = 5) -> list[dict]:
    if not FRED_KEY:
        return []
    try:
        r = await _client_get().get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series, "api_key": FRED_KEY,
                    "file_type": "json", "sort_order": "desc",
                    "limit": limit})
        if not r.is_success:
            log.warning("fred %s HTTP %s", series, r.status_code)
            return []
        return [{"date": o["date"], "value": _num(o["value"])}
                for o in r.json().get("observations", [])
                if o.get("value") != "."]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        _stats["errors"] += 1
        log.warning("fred %s failed: %s", series, e)
        return []


def _int(x: str | None) -> int | None:
    v = _num(x)
    return None if v is None else int(round(v))


def _cot_row(raw: dict) -> dict:
    """One CFTC row -> the compact shape the market panel renders."""
    row: dict = {}
    for key, src in COT_FIELDS.items():
        val = raw.get(src)
        if key == "date":
            row[key] = str(val or "")[:10]      # "2026-09-08T00:00:00.000"
        elif key in COT_TEXT_FIELDS:
            row[key] = str(val or "").strip()
        else:
            row[key] = _int(val)
    long_, short_ = row["noncomm_long"], row["noncomm_short"]
    row["net_noncomm"] = (long_ - short_
                          if long_ is not None and short_ is not None else None)
    return row


async def refresh_cot() -> list[dict]:
    """CFTC Traders-in-Futures rows for the S&P 500 complex, newest first."""
    try:
        r = await _client_get().get(COT_URL, params=COT_PARAMS)
        if not r.is_success:
            log.warning("cftc cot HTTP %s", r.status_code)
            return []
        rows = [_cot_row(raw) for raw in r.json()[:COT_LIMIT]]
        if not jsonstore.save(COT_CACHE, {"fetched": time.time(), "rows": rows}):
            log.warning("macro cot cache not persisted")
        _stats["cot_last"] = time.time()
        return rows
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        _stats["errors"] += 1
        log.warning("cftc cot failed: %s", e)
        return []


async def _scan_earnings_date(day: datetime.date, symbols: set[str]) -> list[dict]:
    try:
        r = await _client_get().get(NASDAQ_CAL.format(date=day.isoformat()),
                                    timeout=15)
        if not r.is_success:
            return []
        rows = (r.json().get("data") or {}).get("rows") or []
        return [{"symbol": row.get("symbol", "").upper(),
                 "date": day.isoformat(),
                 "time": row.get("time", ""),
                 "epsForecast": row.get("epsForecast", ""),
                 "fiscalQuarterEnding": row.get("fiscalQuarterEnding", "")}
                for row in rows
                if row.get("symbol", "").upper() in symbols]
    except (httpx.HTTPError, ValueError) as e:
        log.debug("nasdaq %s: %s", day, e)
        return []


_scanning = False


async def refresh_earnings() -> dict:
    """Forward-scan the Nasdaq calendar for the watchlist's next dates."""
    global _scanning
    if _scanning:
        return jsonstore.load(EARNINGS_CACHE, {})
    _scanning = True
    try:
        return await _refresh_earnings()
    finally:
        _scanning = False


async def _refresh_earnings() -> dict:
    from app.main import get_watchlist
    # Indices never appear on an earnings calendar; excluding them lets the
    # scan stop early once every real holding is found.
    index_like = {"GSPC", "SPX", "^SPX", "^IXIC", "^DJI", "VIX", "^VIX"}
    symbols = {s.upper() for s in get_watchlist()} - index_like
    cache = jsonstore.load(EARNINGS_CACHE, {})
    found: dict[str, dict] = {}
    today = datetime.date.today()
    for offset in range(EARNINGS_SCAN_DAYS):
        day = today + datetime.timedelta(days=offset)
        hits = await _scan_earnings_date(day, symbols)
        for h in hits:
            if h["symbol"] not in found:
                found[h["symbol"]] = h
        if len(found) == len(symbols):
            break
        await asyncio.sleep(1.0)   # ~1 req/s to an undocumented API
    cache.update(found)
    # Drop stale entries whose date has passed (``_fetched`` is metadata).
    cache = {k: v for k, v in cache.items()
             if k == "_fetched"
             or (isinstance(v, dict) and v.get("date", "") >= today.isoformat())}
    cache["_fetched"] = time.time()
    if not jsonstore.save(EARNINGS_CACHE, cache):
        log.warning("macro earnings cache not persisted")
    _stats["earnings_last"] = time.time()
    return cache


async def curve() -> dict:
    cache = jsonstore.load(CURVE_CACHE, {})
    if cache.get("fetched", 0) > time.time() - CURVE_REFRESH_S:
        return cache
    return await refresh_curve() or cache


async def earnings() -> dict:
    cache = jsonstore.load(EARNINGS_CACHE, {})
    if cache.get("_fetched", 0) > time.time() - EARNINGS_REFRESH_S:
        return {k: v for k, v in cache.items() if k != "_fetched"}
    fresh = await refresh_earnings()
    return {k: v for k, v in fresh.items() if k != "_fetched"}


async def cot() -> list[dict]:
    cache = jsonstore.load(COT_CACHE, {})
    if cache.get("fetched", 0) > time.time() - COT_REFRESH_S:
        return cache.get("rows") or []
    return await refresh_cot() or cache.get("rows") or []


async def _curve_loop() -> None:
    while True:
        started = time.time()
        ok = False
        note = ""
        try:
            curve = await refresh_curve()
            ok = bool(curve)
            note = (f"spread2s10s={curve.get('spread2s10s')} "
                    f"date={curve.get('date')}" if curve
                    else "no curve (upstream failed or empty)")
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("macro curve pass failed")
        runlog.record("macro", ok, time.time() - started, "curve: " + note)
        await asyncio.sleep(CURVE_REFRESH_S)


async def _earnings_loop() -> None:
    while True:
        started = time.time()
        ok = False
        note = ""
        try:
            dates = await refresh_earnings()
            ok = True
            n = sum(1 for k in dates if k != "_fetched")
            note = (f"{n} watchlist earnings date(s)" if n
                    else "no watchlist earnings date in the scan window")
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("macro earnings pass failed")
        runlog.record("macro", ok, time.time() - started, "earnings: " + note)
        await asyncio.sleep(EARNINGS_REFRESH_S)


async def _cot_loop() -> None:
    while True:
        started = time.time()
        ok = False
        note = ""
        try:
            rows = await refresh_cot()
            ok = bool(rows)
            note = (f"{len(rows)} COT row(s), latest {rows[0].get('date')}"
                    if rows else "no COT rows (upstream failed or no match)")
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("macro cot pass failed")
        runlog.record("macro", ok, time.time() - started, "cot: " + note)
        await asyncio.sleep(COT_REFRESH_S)


def start() -> None:
    global _tasks
    _tasks = [asyncio.create_task(_curve_loop()),
              asyncio.create_task(_earnings_loop()),
              asyncio.create_task(_cot_loop())]


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    _tasks = []


async def close() -> None:
    if _client and not _client.is_closed:
        await _client.aclose()


def status() -> dict:
    cal = datetime.date.today()
    return {**_stats, "fred_key": bool(FRED_KEY), "running": bool(_tasks),
            "today_trading_day": {m: is_trading_day(m, cal)
                                  for m in ("XNYS", "XAMS")}}