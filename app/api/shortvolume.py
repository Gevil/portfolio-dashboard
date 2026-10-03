"""FINRA Reg SHO daily short-volume feed (Tier-S evidence).

FINRA publishes two pipe-delimited files per trading day under
``cdn.finra.org/equity/regsho/daily/`` (both live-verified 2026-09-12):

- ``CNMSshvolYYYYMMDD.txt`` — consolidated short volume; the ``Market``
  column lists every venue that printed the symbol (``B,Q,N``).
- ``FNSQshvolYYYYMMDD.txt`` — FINRA/Nasdaq TRF (off-exchange) only.

FNSQ is a strict *subset* of CNMS (2026-09-11 AAPL: FNSQ total 19.35M vs
CNMS 20.53M), so the two must NOT be summed — CNMS is the primary source and
FNSQ is only consulted for symbols CNMS has no row for.

Both files start with the header ``Date|Symbol|ShortVolume|ShortExemptVolume|
TotalVolume|Market`` and carry fractional share volumes (weighted averages).
``ratio`` is ``short / total`` — FINRA's own published column definition;
short-exempt volume is reported separately and is not added.

Only watchlist symbols are kept, newest ``max_series`` trading days each.
Rows whose total volume is under ``min_total`` are dropped as noise: ASML's
US-listed ADR prints ~0.3M shares/day and is therefore absent by design.

Daily pass at 22:30 local (``TZ=Europe/Prague``). FINRA stamps the day's file
around 21:18 UTC = 23:18 CET, so a pass ingests the newest *published* file;
the walk-back over ``lookback_weekdays`` weekdays covers weekends, holidays
and publication lag.
``warm_if_cold()`` lets the first API request run the
same pass instead of waiting for the night.
"""
import asyncio
import datetime

import logging
import os
import pathlib
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api import config_store, jsonstore, runlog

log = logging.getLogger("shortvolume")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))

SERIES_FILE = DATA_DIR / "shortvolume.json"

FINRA_URL = ("https://cdn.finra.org/equity/regsho/daily/"
             "{prefix}shvol{compact}.txt")
# Declared contact (same identity discipline as edgar.py): CDN rejects nothing
# today, but an anonymous browser string is a bad citizen on a rate-limited feed.
FINRA_UA = os.getenv("FINRA_USER_AGENT",
                     "portfolio-dashboard (set FINRA_USER_AGENT with a contact email)")
REQ_DELAY_S = 0.15          # same pacing discipline as edgar.py
MAX_SEEN_DATES = 90

DEFAULTS = {
    "hour": 22,
    "minute": 30,
    "min_total": 1_000_000,
    "max_series": 20,
    "lookback_weekdays": 7,
    "first_run_lookback_weekdays": 30,
    "max_files_per_pass": 8,
}

_task: asyncio.Task | None = None
_client: httpx.AsyncClient | None = None
_last_req = 0.0
_warming = False
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "running": False,
          "next_run": 0.0, "dates": 0, "low_total_skips": 0}


def cfg() -> dict:
    merged = dict(DEFAULTS)
    block = config_store.read().get("shortVolume") or {}
    if isinstance(block, dict):
        merged.update(block)
    return merged


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": FINRA_UA,
                     "Accept-Encoding": "gzip, deflate"},
            timeout=60, follow_redirects=True)
    return _client


async def _pace() -> None:
    """Space requests (files are ~1 MB; stay a polite neighbour)."""
    global _last_req
    wait = REQ_DELAY_S - (time.monotonic() - _last_req)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_req = time.monotonic()


def _prev_weekday(day: datetime.date) -> datetime.date:
    """Last weekday strictly before ``day`` (today's file is never stamped)."""
    d = day - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


async def _file_rows(prefix: str, compact: str,
                     wanted: set[str]) -> dict[str, dict] | None:
    """Watchlist rows from one FINRA file; ``None`` when not published/error.

    Streams and stops as soon as every wanted symbol has been seen.
    """
    await _pace()
    url = FINRA_URL.format(prefix=prefix, compact=compact)
    out: dict[str, dict] = {}
    try:
        async with _client_get().stream("GET", url) as r:
            if r.status_code == 404:
                return None
            if not r.is_success:
                await r.aread()
                log.warning("finra %s%s HTTP %s", prefix, compact,
                            r.status_code)
                return None
            async for line in r.aiter_lines():
                fields = line.split("|")
                if len(fields) < 5 or fields[0] == "Date":
                    continue
                sym = fields[1].strip().upper()
                if sym not in wanted:
                    continue
                short, total = _num(fields[2]), _num(fields[4])
                if short is None or total is None or total <= 0:
                    continue
                out[sym] = {"date": _iso(compact), "short": short,
                            "total": total}
                if len(out) >= len(wanted):
                    break
        return out
    except httpx.HTTPError as e:
        log.warning("finra %s%s failed: %s", prefix, compact, e)
        return None


def _iso(compact: str) -> str:
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


async def _day_rows(day: datetime.date,
                    wanted: set[str]) -> dict[str, dict] | None:
    """Consolidated short/total per watchlist symbol for one trading day."""
    compact = day.isoformat().replace("-", "")
    rows = await _file_rows("CNMS", compact, wanted)
    if rows is None:
        return None
    missing = wanted - set(rows)
    if missing:
        extra = await _file_rows("FNSQ", compact, missing)
        if extra:
            rows.update(extra)
    return rows


def _merge(have: dict[str, list], rows: dict[str, dict],
           min_total: float, cap: int) -> int:
    """Fold one day's rows into the per-symbol series; -> symbols kept."""
    kept = 0
    for sym, row in rows.items():
        if row["total"] < min_total:
            _stats["low_total_skips"] += 1
            continue
        entry = {"date": row["date"],
                 "ratio": round(row["short"] / row["total"], 6),
                 "total": int(round(row["total"]))}
        series = [r for r in have.get(sym, []) if r.get("date") != row["date"]]
        series.append(entry)
        series.sort(key=lambda r: r.get("date", ""))
        have[sym] = series[-cap:]
        kept += 1
    return kept


async def refresh() -> dict:
    """One ingest pass: walk back over weekdays until the series is topped up."""
    from app.main import get_watchlist
    conf = cfg()
    store = jsonstore.load(SERIES_FILE, {})
    if not isinstance(store, dict):
        store = {}
    symbols = {s.upper() for s in get_watchlist()}
    have: dict[str, list] = {k: list(v) for k, v
                             in (store.get("symbols") or {}).items()
                             if isinstance(v, list)}
    seen: set[str] = set(store.get("seen_dates") or [])
    cap = max(1, int(conf["max_series"]))
    # How many more dated rows the thinnest series still needs (backfill).
    need = max(1, cap - max((len(v) for v in have.values()), default=0))
    window = (int(conf["lookback_weekdays"]) if seen
              else int(conf["first_run_lookback_weekdays"]))
    budget = max(1, int(conf["max_files_per_pass"]))
    min_total = float(conf["min_total"])

    day = _prev_weekday(datetime.date.today())
    steps = requests = hits = 0
    while steps < window and requests < budget and hits < need:
        if day.weekday() < 5 and day.isoformat() not in seen:
            requests += 1
            rows = await _day_rows(day, symbols)
            if rows is None:
                log.debug("shortvolume: %s not published yet", day)
            else:
                seen.add(day.isoformat())
                hits += _merge(have, rows, min_total, cap)
        day -= datetime.timedelta(days=1)
        steps += 1

    store = {"updated": datetime.datetime.now().isoformat(timespec="seconds"),
             "seen_dates": sorted(seen)[-MAX_SEEN_DATES:],
             "symbols": {s: rows[-cap:] for s, rows in have.items()}}
    if not jsonstore.save(SERIES_FILE, store):
        log.warning("shortvolume series not persisted; pass will repeat")
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    _stats["dates"] = len(store["seen_dates"])
    log.info("shortvolume: %d file(s) read, %d dated row(s) ingested, "
             "%d symbols tracked", requests, hits, len(store["symbols"]))
    return store


async def warm_if_cold() -> None:
    """Backfill on the first request so day one is not an empty panel."""
    global _warming
    if _warming:
        return
    if _store().get("symbols"):
        return
    _warming = True
    try:
        await refresh()
    except Exception:
        _stats["errors"] += 1
        log.exception("shortvolume cold start failed")
    finally:
        _warming = False


def _store() -> dict:
    store = jsonstore.load(SERIES_FILE, {})
    return store if isinstance(store, dict) else {}


def series(symbol: str) -> list[dict]:
    """``[{date, ratio, total}]`` oldest-first for one symbol."""
    rows = _store().get("symbols", {}).get(symbol.upper())
    return list(rows) if isinstance(rows, list) else []


def latest(symbol: str) -> dict | None:
    rows = series(symbol)
    return rows[-1] if rows else None


def mean_ratio(symbol: str, window: int = 20) -> float | None:
    """Mean short-volume ratio over the last ``window`` dated rows."""
    rows = series(symbol)[-window:]
    vals = [r["ratio"] for r in rows if isinstance(r.get("ratio"), (int, float))]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 6)


async def payload(symbol: str) -> dict:
    """Endpoint body: ``{symbol, series, latest}`` (cache-only)."""
    await warm_if_cold()
    rows = series(symbol)
    return {"symbol": symbol.upper(), "series": rows,
            "latest": rows[-1] if rows else None}


router = APIRouter()


@router.get("/api/shortvolume/{symbol}")
async def get_shortvolume(symbol: str) -> JSONResponse:
    """FINRA Reg SHO short-volume series: ``{symbol, series, latest}``."""
    return JSONResponse(await payload(symbol))


def _seconds_until_next(hour: int, minute: int) -> float:
    """Seconds until the next local HH:MM (today or tomorrow)."""
    now = datetime.datetime.now()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += datetime.timedelta(days=1)
    _stats["next_run"] = time.time() + (when - now).total_seconds()
    return max(30.0, (when - now).total_seconds())


async def _loop() -> None:
    conf = cfg()
    log.info("shortvolume loop started (daily %02d:%02d local)",
             int(conf["hour"]), int(conf["minute"]))
    while True:
        await asyncio.sleep(_seconds_until_next(int(conf["hour"]),
                                               int(conf["minute"])))
        started = time.time()
        try:
            result = await refresh()
            runlog.record("shortvolume", True, time.time() - started,
                          f"{len(result['symbols'])} symbols, "
                          f"{len(result['seen_dates'])} dates")
        except Exception as e:
            _stats["errors"] += 1
            log.exception("shortvolume pass failed")
            runlog.record("shortvolume", False, time.time() - started,
                          str(e)[:200])


def start() -> None:
    global _task
    if os.getenv("SHORTVOLUME_ENABLED", "1") != "1":
        log.info("shortvolume disabled (set SHORTVOLUME_ENABLED=1)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True


async def stop() -> None:
    global _task
    _stats["running"] = False
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


async def close() -> None:
    if _client and not _client.is_closed:
        await _client.aclose()


def status() -> dict:
    return {**_stats, "enabled": os.getenv("SHORTVOLUME_ENABLED", "1") == "1"}