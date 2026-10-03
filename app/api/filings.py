"""EDGAR daily-index + full-text watch feed (Tier-S evidence).

Two producers, one event ring (``data/filings_events.json``, newest first,
cap 200) that step 6's triage worker consumes:

1. Daily pass over the SEC's *form-type* index
   ``/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx``,
   filtered to watchlist CIKs. Live-verified 2026-09-12 layout (the plan's
   fixed-width offsets for form/company/CIK are exact; the date and path
   columns sit further right than the plan's 86/98 — on 2026-09-11 every row
   has its date at 91 and its path at 103 — so the tail is matched by regex
   instead of hardcoded offsets)::

       Form Type   Company Name                    CIK        Date Filed  File Name
       144         ACME CORP                    1045810       20260911    edgar/data/...

   ``form`` = ``L[:12]``, ``company`` = ``L[12:74]``, then ``CIK``,
   ``YYYYMMDD`` and the ``edgar/...`` path. Forms of interest default to
   ``144, 8-K, SC 13D, SC 13G, 13F-HR`` (Form 4 / 6-K stay on edgar.py's
   per-ticker submissions path). Pass at 23:40 local — EDGAR closes 17:15 ET
   (= 23:15 CET), so the file exists by then; a 404 on a fresh date is
   retried the next night, an old date is marked done.

2. Weekly EDGAR full-text watch (``efts.sec.gov/LATEST/search-index``) over
   the ``filingWatch`` config block (default ``{"keywords": ["HBM4"],
   "forms": "8-K"}``). Keyword hits are NOT filtered to the watchlist — they
   are cross-thread evidence — and carry ``ticker: ""`` when the filer is not
   a watchlist CIK.

SEC access follows edgar.py: declared ``User-Agent`` (app name + admin
contact, never a browser string), ``Accept-Encoding: gzip, deflate``,
``REQ_DELAY_S`` spacing. Note www.sec.gov answers 403 to contacts on
non-routable domains (``@localhost``/``@localdomain``), which is why the CIK
lookup keeps a local mirror of ``company_tickers.json`` next to edgar's map.
"""
import asyncio
import datetime

import logging
import os
import pathlib
import re
import time
import urllib.parse

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api import config_store, edgar, jsonstore, runlog

log = logging.getLogger("filings")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))

EVENTS_FILE = DATA_DIR / "filings_events.json"
STATE_FILE = DATA_DIR / "filings_seen.json"
TICKER_MAP_FILE = DATA_DIR / "sec_ticker_map.json"
TICKER_MAP_TTL_S = 86400

SEC_UA = os.getenv("SEC_USER_AGENT",
                   "portfolio-dashboard (set SEC_USER_AGENT with a contact email)")
SEC_HEADERS = {"User-Agent": SEC_UA,
               "Accept-Encoding": "gzip, deflate"}
REQ_DELAY_S = 0.15                      # same pacing discipline as edgar.py
IDX_URL = ("https://www.sec.gov/Archives/edgar/daily-index/"
           "{year}/QTR{qtr}/form.{compact}.idx")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/"

FTS_DEFAULTS = {"keywords": ["HBM4"], "forms": "8-K"}
DEFAULTS = {
    "hour": 23,
    "minute": 40,
    "forms": ["144", "8-K", "SC 13D", "SC 13G", "13F-HR"],
    "lookback_days": 5,
    "max_days_per_pass": 3,
    "ring": 200,
    "seen_ring": 120,
    "fts_interval_days": 7,
    "fts_window_days": 7,
    "fts_max_hits": 20,
}

# Fixed-width head (form 12, company 62, CIK 12) + free-spaced tail.
_TAIL_RE = re.compile(r"^(?P<cik>\d{1,10})\s+(?P<filed>\d{8})\s+(?P<path>\S+)")

_client: httpx.AsyncClient | None = None
_task: asyncio.Task | None = None
_last_req = 0.0
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "running": False,
          "next_run": 0.0, "events": 0, "last_fts": 0.0, "fts_hits": 0}


def cfg() -> dict:
    merged = dict(DEFAULTS)
    block = config_store.read().get("filings") or {}
    if isinstance(block, dict):
        merged.update(block)
    return merged


def fts_cfg() -> dict:
    """``filingWatch`` config block over the seeded default (plan step 3)."""
    merged = dict(FTS_DEFAULTS)
    block = config_store.read().get("filingWatch") or {}
    if isinstance(block, dict):
        merged.update(block)
    if isinstance(merged.get("keywords"), str):
        merged["keywords"] = [merged["keywords"]]
    return merged


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=SEC_HEADERS, timeout=45,
                                    follow_redirects=True)
    return _client


async def _sec_get(url: str) -> httpx.Response:
    """Rate-paced SEC GET (well under the 10 req/s policy)."""
    global _last_req
    wait = REQ_DELAY_S - (time.monotonic() - _last_req)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_req = time.monotonic()
    return await _client_get().get(url)


async def _watchlist_ciks() -> dict[str, int]:
    """Watchlist symbol -> CIK.

    edgar's accessor is imported, not copied. Its declared contact
    (``@localhost``) is rejected by www.sec.gov, so its map can degrade to
    the builtin entry; the local mirror below covers the gap and is written
    with this module's own (routable) contact.
    """
    from app.main import get_watchlist
    symbols = [s.upper() for s in get_watchlist()]
    out: dict[str, int] = {}
    try:
        out.update({s.upper(): int(c)
                    for s, c in (await edgar.ticker_ciks(symbols)).items()})
    except Exception as e:
        log.warning("edgar ticker map unavailable: %s", e)
    if any(not out.get(s) for s in symbols):
        mirror = jsonstore.load(TICKER_MAP_FILE, {})
        if not isinstance(mirror, dict) or \
                mirror.get("fetched", 0) <= time.time() - TICKER_MAP_TTL_S:
            try:
                r = await _sec_get(TICKER_MAP_URL)
                if r.is_success:
                    mirror = {"fetched": time.time(),
                              "map": {str(v.get("ticker", "")).upper():
                                      int(v.get("cik_str", 0))
                                      for v in r.json().values()
                                      if isinstance(v, dict)}}
                    jsonstore.save(TICKER_MAP_FILE, mirror)
                else:
                    log.warning("company_tickers.json HTTP %s — declare a "
                                "routable SEC_USER_AGENT contact domain",
                                r.status_code)
            except (httpx.HTTPError, ValueError, TypeError) as e:
                log.warning("company_tickers.json failed: %s", e)
        mapping = mirror.get("map", {}) if isinstance(mirror, dict) else {}
        for sym in symbols:
            if not out.get(sym) and mapping.get(sym):
                out[sym] = int(mapping[sym])
    missing = [s for s in symbols if not out.get(s)]
    if missing:
        log.debug("no CIK for %s (index/provider symbols have none)",
                  ",".join(missing))
    # edgar's map always carries its builtin entry; keep only the watchlist.
    return {s: c for s, c in out.items() if c and s in set(symbols)}


def _idx_url(day: datetime.date) -> str:
    return IDX_URL.format(year=day.year, qtr=(day.month - 1) // 3 + 1,
                          compact=day.isoformat().replace("-", ""))


def _iso(compact: str) -> str:
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


def parse_index(text: str, forms: set[str],
                cik_to_sym: dict[int, str]) -> list[dict]:
    """Watchlist filing events from one ``form.*.idx`` body."""
    events = []
    for line in text.splitlines():
        if len(line) < 75:
            continue
        form = line[:12].strip()
        if form not in forms:
            continue
        m = _TAIL_RE.match(line[74:].strip())
        if not m:
            continue
        cik = int(m["cik"])
        sym = cik_to_sym.get(cik)
        if not sym:
            continue
        events.append({"form": form,
                       "company": line[12:74].strip(),
                       "cik": cik,
                       "filed": _iso(m["filed"]),
                       "url": ARCHIVE + m["path"].lstrip("/"),
                       "ticker": sym,
                       "source": "daily-index"})
    return events


def _events() -> list[dict]:
    events = jsonstore.load(EVENTS_FILE, [])
    return events if isinstance(events, list) else []


def _append_events(events: list[dict], ring: int) -> int:
    """Prepend new (URL-deduped) events, keep the ring, -> count added."""
    current = _events()
    known = {e.get("url") for e in current}
    fresh = []
    for e in events:
        if e.get("url") in known or e in fresh:
            continue
        known.add(e.get("url"))
        fresh.append(e)
    if fresh:
        merged = fresh + current
        merged.sort(key=lambda e: e.get("filed", ""), reverse=True)
        if not jsonstore.save(EVENTS_FILE, merged[:ring]):
            log.warning("filings events not persisted")
    return len(fresh)


def _load_state() -> dict:
    state = jsonstore.load(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


async def ingest_day(day: datetime.date, forms: set[str],
                     cik_to_sym: dict[int, str]) -> tuple[int, bool]:
    """-> (new events, date settled). Settled means: do not probe again."""
    url = _idx_url(day)
    try:
        r = await _sec_get(url)
    except httpx.HTTPError as e:
        log.warning("daily index %s failed: %s", day, e)
        return 0, False
    if r.status_code == 404:
        # A fresh date may still be disseminating; an old one never arrives
        # (weekend/holiday) and is settled.
        stale = day <= datetime.date.today() - datetime.timedelta(days=2)
        log.debug("daily index %s: 404", day)
        return 0, stale
    if not r.is_success:
        log.warning("daily index %s HTTP %s", day, r.status_code)
        return 0, False
    events = parse_index(r.text, forms, cik_to_sym)
    added = _append_events(events, int(cfg()["ring"]))
    log.info("filings: %s -> %d watchlist filing(s), %d new",
             day, len(events), added)
    return added, True


async def run_daily() -> int:
    """Ingest the newest unprocessed weekday(s)."""
    conf = cfg()
    ciks = await _watchlist_ciks()
    if not ciks:
        log.warning("filings: no watchlist CIK resolvable, pass skipped")
        return 0
    cik_to_sym = {cik: sym for sym, cik in ciks.items()}
    forms = {str(f).strip() for f in conf["forms"]}
    state = _load_state()
    seen = set(state.get("dates") or [])
    day = datetime.date.today() - datetime.timedelta(days=1)
    added = processed = 0
    for _ in range(max(1, int(conf["lookback_days"]))):
        if processed >= max(1, int(conf["max_days_per_pass"])):
            break
        if day.weekday() < 5 and day.isoformat() not in seen:
            processed += 1
            n, settled = await ingest_day(day, forms, cik_to_sym)
            added += n
            if settled:
                seen.add(day.isoformat())
        day -= datetime.timedelta(days=1)
    state["dates"] = sorted(seen)[-int(cfg()["seen_ring"]):]
    if not jsonstore.save(STATE_FILE, state):
        log.warning("filings state not persisted (dates); pass will repeat")
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    _stats["events"] = len(_events())
    return added


async def run_fts() -> int:
    """One full-text sweep over the ``filingWatch`` keywords."""
    conf, watch = cfg(), fts_cfg()
    keywords = [str(k).strip() for k in (watch.get("keywords") or []) if str(k).strip()]
    if not keywords:
        return 0
    forms = str(watch.get("forms") or "")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=max(1, int(conf["fts_window_days"])))
    ciks = await _watchlist_ciks()
    cik_to_sym = {str(cik): sym for sym, cik in ciks.items()}
    state = _load_state()
    known = set(state.get("fts_seen") or [])
    events: list[dict] = []
    for kw in keywords:
        params = {"q": f'"{kw}"', "forms": forms,
                  "startdt": start.isoformat(), "enddt": end.isoformat()}
        url = f"{FTS_URL}?{urllib.parse.urlencode(params)}"
        try:
            r = await _sec_get(url)
            if not r.is_success:
                log.warning("edgar fts %r HTTP %s", kw, r.status_code)
                continue
            hits = (r.json().get("hits") or {}).get("hits") or []
        except (httpx.HTTPError, ValueError) as e:
            log.warning("edgar fts %r failed: %s", kw, e)
            continue
        new = 0
        for hit in hits:
            src = hit.get("_source") or {}
            adsh = src.get("adsh") or ""
            if not adsh or adsh in known:
                continue
            known.add(adsh)
            new += 1
            cik = (src.get("ciks") or [""])[0]
            names = src.get("display_names") or [""]
            events.append({
                "form": src.get("form") or (src.get("root_forms") or [""])[0],
                "company": str(names[0]).split("  (")[0].strip(),
                "cik": int(cik) if str(cik).isdigit() else cik,
                "filed": src.get("file_date") or "",
                "url": _fts_url(cik, adsh),
                "ticker": cik_to_sym.get(str(cik), ""),
                "source": "fts",
                "keyword": kw,
            })
            if new >= int(conf["fts_max_hits"]):
                break
        log.info("filings fts: %r %s..%s -> %d new hit(s)", kw, start, end, new)
    added = _append_events(events, int(cfg()["ring"]))
    state["fts_seen"] = sorted(known)[-500:]
    state["last_fts"] = time.time()
    if not jsonstore.save(STATE_FILE, state):
        log.warning("filings state not persisted (fts_seen)")
    _stats["last_fts"] = state["last_fts"]
    _stats["fts_hits"] += added
    return added


def _fts_url(cik: str, adsh: str) -> str:
    """Filing index page for one accession (verified 200 on live accessions)."""
    if not str(cik).isdigit():
        return ARCHIVE + "edgar/data/" + adsh.replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{adsh.replace('-', '')}/{adsh}-index.htm")


def recent(symbol: str | None = None, limit: int = 50) -> list[dict]:
    events = _events()
    if symbol:
        sym = symbol.upper()
        events = [e for e in events if e.get("ticker", "").upper() == sym]
    return events[:limit]


router = APIRouter()


@router.get("/api/filings")
async def get_filings(symbol: str | None = None,
                      limit: int = 50) -> JSONResponse:
    """Recent watchlist filing events (newest first) + worker status."""
    return JSONResponse({"ok": True, "events": recent(symbol, limit),
                         "status": status()})


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
    log.info("filings loop started (daily %02d:%02d local, fts every %dd)",
             int(conf["hour"]), int(conf["minute"]),
             int(conf["fts_interval_days"]))
    while True:
        await asyncio.sleep(_seconds_until_next(int(conf["hour"]),
                                               int(conf["minute"])))
        started = time.time()
        ok = True
        notes = []
        try:
            notes.append(f"{await run_daily()} new")
        except Exception as e:
            ok = False
            notes.append(f"daily failed: {e}")
            _stats["errors"] += 1
            log.exception("filings daily pass failed")
        try:
            due = jsonstore.load(STATE_FILE, {})
            due = float((due.get("last_fts") if isinstance(due, dict) else 0)
                        or 0)
            if due <= time.time() - int(conf["fts_interval_days"]) * 86400:
                notes.append(f"fts {await run_fts()} new")
        except Exception as e:
            ok = False
            notes.append(f"fts failed: {e}")
            _stats["errors"] += 1
            log.exception("filings fts pass failed")
        runlog.record("filings", ok, time.time() - started,
                      "; ".join(notes)[:200])


def start() -> None:
    global _task
    if os.getenv("FILINGS_ENABLED", "1") != "1":
        log.info("filings disabled (set FILINGS_ENABLED=1)")
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
    return {**_stats, "enabled": os.getenv("FILINGS_ENABLED", "1") == "1",
            "watch": fts_cfg().get("keywords")}