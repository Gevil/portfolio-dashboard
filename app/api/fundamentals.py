"""XBRL annual fundamentals per watchlist issuer (Tier-S evidence).

Source: ``data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json`` (live-verified
2026-09-12: NVDA 4.1 MB / 627 us-gaap concepts, ASML 1.8 MB / 623). Raw payloads
are multi-megabyte, so the per-CIK cache
(``data/companyfacts/{cik}.json``, TTL 7 d) stores only the concepts extracted
below — the whole feed then costs one download per issuer per week.

Concept sets are unions with alternates, because the plan's single names do not
cover the actual tagging: NVDA reports operating cash flow as
``NetCashProvidedByUsedInOperatingActivities`` (no ``OperatingCashFlow`` at
all), ASML has no ``Revenues`` (only
``RevenueFromContractWithCustomerExcludingAssessedTax``). ``capex`` is added on
top of the plan's list — ``fcfNi`` is free cash flow / net income and FCF needs
it; it cannot be derived from the plan's six concepts alone.

Rows are keyed by *period end*, not by the ``fy``/``fp`` fields: those describe
the filing that reported a fact, so one FY 10-K carries three comparative years
all stamped ``fy=<filing year>, fp=FY`` (verified on NVDA's FY2026 10-K). The
fiscal-year label of a row is the year of its period end (NVDA's fiscal 2026
ended 2026-01-25, ASML's fiscal 2025 ended 2025-12-31).

Currency is surfaced because foreign issuers report in their own currency in
the us-gaap namespace: ASML's entries are EUR-only, so labelling those numbers
USD would be a ~15% error. USD is preferred when present.

A weekly warm pass keeps the cache hot for the analysis evidence pack (which
reads caches only, never the network).
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

from app.api import config_store, edgar, jsonstore, runlog

log = logging.getLogger("fundamentals")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))

FACTS_DIR = DATA_DIR / "companyfacts"
TICKER_MAP_FILE = DATA_DIR / "sec_ticker_map.json"
TICKER_MAP_TTL_S = 86400

SEC_UA = os.getenv("SEC_USER_AGENT",
                   "portfolio-dashboard (set SEC_USER_AGENT with a contact email)")
SEC_HEADERS = {"User-Agent": SEC_UA,
               "Accept-Encoding": "gzip, deflate"}
REQ_DELAY_S = 0.15                      # same pacing discipline as edgar.py
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# Preferred concept first: the first one that reports a period wins.
CONCEPTS = {
    "revenue": ("Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet"),
    "ni": ("NetIncomeLoss", "ProfitLoss"),
    "ocf": ("OperatingCashFlow",
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "ltDebt": ("LongTermDebtNoncurrent", "LongTermDebt",
               "LongTermDebtAndCapitalLeaseObligations"),
    "assets": ("Assets",),
    "equity": ("StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableTo"
               "NoncontrollingInterest"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"),
}
# Balance-sheet concepts carry no ``start``.
INSTANT = frozenset({"ltDebt", "assets", "equity"})
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})

DEFAULTS = {
    "ttl_days": 7,
    "years": 8,
    "warm_interval_days": 7,
    "warm_start_delay_s": 90,
}

_client: httpx.AsyncClient | None = None
_task: asyncio.Task | None = None
_last_req = 0.0
_warming = False
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "running": False,
          "warmed": 0, "next_run": 0.0}


def cfg() -> dict:
    merged = dict(DEFAULTS)
    block = config_store.read().get("fundamentals") or {}
    if isinstance(block, dict):
        merged.update(block)
    return merged


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=SEC_HEADERS, timeout=90,
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
    """Watchlist symbol -> CIK (edgar's accessor + local mirror; see
    filings.py for why the mirror exists)."""
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
    return {s: c for s, c in out.items() if c and s in set(symbols)}


def _cache_path(cik: int) -> pathlib.Path:
    return FACTS_DIR / f"{cik}.json"


def _trim(raw: dict, cik: int) -> dict:
    """Keep only the concepts we extract (raw payloads are MBs)."""
    us_gaap = (raw.get("facts") or {}).get("us-gaap") or {}
    keep = {}
    for names in CONCEPTS.values():
        for name in names:
            if name in us_gaap:
                keep[name] = us_gaap[name]
    return {"cik": cik,
            "name": raw.get("entityName") or "",
            "fetched": time.time(),
            "units": keep}


def _date(text) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _annual_entry(entry: dict, instant: bool) -> bool:
    """Annual-filing row: FY period (instant, or ~12-month duration)."""
    if entry.get("form") not in ANNUAL_FORMS:
        return False
    end = _date(entry.get("end"))
    if end is None:
        return False
    start = _date(entry.get("start"))
    if instant:
        return start is None
    if start is None:
        return False
    return 300 <= (end - start).days <= 400


def _currency(us_gaap: dict) -> str | None:
    """Reporting currency: USD when tagged, else the dominant unit."""
    counts: dict[str, int] = {}
    for names in CONCEPTS.values():
        for name in names:
            for unit, entries in ((us_gaap.get(name) or {}).get("units")
                                  or {}).items():
                counts[unit] = counts.get(unit, 0) + len(entries)
    if not counts:
        return None
    if counts.get("USD"):
        return "USD"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _field_values(us_gaap: dict, names: tuple[str, ...], unit: str,
                  instant: bool) -> dict[str, float]:
    """period-end -> value, latest filing wins, first concept that reports."""
    out: dict[str, float] = {}
    filed: dict[str, str] = {}
    for name in names:
        entries = ((us_gaap.get(name) or {}).get("units") or {}).get(unit) or []
        for entry in entries:
            if not _annual_entry(entry, instant):
                continue
            end = str(entry["end"])
            val = entry.get("val")
            if end in out and filed.get(end, "") >= str(entry.get("filed", "")):
                continue
            if not isinstance(val, (int, float)):
                continue
            out[end] = float(val)
            filed[end] = str(entry.get("filed", ""))
    return out


def _near(mapping: dict[str, float], end: str,
          tol_days: int = 10) -> float | None:
    """Value for ``end``, or the closest period end within ``tol_days``
    (balance-sheet dates can sit a day or two off the income-statement end)."""
    if end in mapping:
        return mapping[end]
    target = _date(end)
    if target is None or not mapping:
        return None
    best_key, best_gap = None, tol_days + 1
    for key in mapping:
        other = _date(key)
        if other is None:
            continue
        gap = abs((other - target).days)
        if gap < best_gap:
            best_key, best_gap = key, gap
    return mapping.get(best_key) if best_key else None


def extract(us_gaap: dict, years: int) -> dict:
    """Annual rows + ROE / FCF-NI series from a trimmed companyfacts body."""
    unit = _currency(us_gaap)
    empty = {"currency": unit, "annual": [], "roe": [], "fcfNi": []}
    if not unit:
        return empty
    vals = {field: _field_values(us_gaap, names, unit, field in INSTANT)
            for field, names in CONCEPTS.items()}
    periods = sorted(set(vals["revenue"]) | set(vals["ni"]))[-max(1, years):]
    rows, roe, fcf_ni = [], [], []
    for end in periods:
        row = {
            "fy": int(end[:4]),
            "revenue": vals["revenue"].get(end),
            "ni": vals["ni"].get(end),
            "ocf": vals["ocf"].get(end),
            "ltDebt": _near(vals["ltDebt"], end),
            "assets": _near(vals["assets"], end),
            "equity": _near(vals["equity"], end),
        }
        rows.append(row)
        equity, ni = row["equity"], row["ni"]
        if isinstance(ni, (int, float)) and isinstance(equity, (int, float)) \
                and equity > 0:
            roe.append({"fy": row["fy"], "value": round(ni / equity, 4)})
        ocf, capex = row["ocf"], vals["capex"].get(end)
        if isinstance(ocf, (int, float)) and isinstance(capex, (int, float)):
            fcf = ocf - abs(capex)
            if isinstance(ni, (int, float)) and ni:
                fcf_ni.append({"fy": row["fy"],
                               "value": round(fcf / ni, 3)})
    return {"currency": unit, "annual": rows, "roe": roe, "fcfNi": fcf_ni}


def _payload(symbol: str, cik: int | None, cache: dict | None,
             note: str = "") -> dict:
    body = {"symbol": symbol.upper(), "cik": cik, "currency": None,
            "annual": [], "roe": [], "fcfNi": [], "fetched": None}
    if isinstance(cache, dict):
        part = extract(cache.get("units") or {}, int(cfg()["years"]))
        body.update(part)
        body["fetched"] = datetime.datetime.fromtimestamp(
            cache.get("fetched", 0)).isoformat(timespec="seconds")
    if note:
        body["note"] = note
    return body


def cached(symbol: str) -> dict | None:
    """Cache-only read (the analysis evidence pack never hits the network)."""
    ciks = _cached_ciks()
    cik = ciks.get(symbol.upper())
    if not cik:
        return None
    cache = jsonstore.load(_cache_path(cik), None)
    if not isinstance(cache, dict):
        return None
    return _payload(symbol, cik, cache)


_CIK_CACHE: dict[str, int] = {}
_CIK_CACHE_AT = 0.0


def _cached_ciks() -> dict[str, int]:
    """Symbol -> CIK without any I/O wait (cache-only accessors).

    Uses the last async resolution; before the first one it falls back to the
    on-disk ticker mirror plus edgar's builtin entry — both plain file reads.
    """
    if _CIK_CACHE:
        return dict(_CIK_CACHE)
    out = dict(getattr(edgar, "BUILTIN_CIK", {}))
    mirror = jsonstore.load(TICKER_MAP_FILE, {})
    if isinstance(mirror, dict):
        for sym, cik in (mirror.get("map") or {}).items():
            if cik:
                out.setdefault(str(sym).upper(), int(cik))
    return out


async def _ensure(cik: int, ttl_s: float) -> dict | None:
    """Trimmed companyfacts for one CIK, refreshed when the cache is stale."""
    path = _cache_path(cik)
    cache = jsonstore.load(path, None)
    if isinstance(cache, dict) and cache.get("fetched", 0) > time.time() - ttl_s:
        return cache
    try:
        r = await _sec_get(FACTS_URL.format(cik=cik))
        if not r.is_success:
            log.warning("companyfacts CIK%010d HTTP %s", cik, r.status_code)
            return cache if isinstance(cache, dict) else None
        trimmed = _trim(r.json(), cik)
        if not jsonstore.save(path, trimmed):
            log.warning("companyfacts CIK%010d cache not persisted", cik)
        log.info("companyfacts CIK%010d refreshed (%d concepts cached)",
                 cik, len(trimmed.get("units", {})))
        return trimmed
    except (httpx.HTTPError, ValueError) as e:
        log.warning("companyfacts CIK%010d failed: %s", cik, e)
        return cache if isinstance(cache, dict) else None


async def fundamentals(symbol: str) -> dict:
    """``{symbol, cik, currency, annual, roe, fcfNi, fetched}`` (fetch-through)."""
    global _CIK_CACHE, _CIK_CACHE_AT
    sym = symbol.upper()
    if not _CIK_CACHE or _CIK_CACHE_AT <= time.time() - 86400:
        _CIK_CACHE = await _watchlist_ciks()
        _CIK_CACHE_AT = time.time()
    cik = _CIK_CACHE.get(sym)
    if not cik:
        return _payload(sym, None, None, "no SEC CIK (index or foreign symbol)")
    cache = await _ensure(cik, float(cfg()["ttl_days"]) * 86400)
    if not isinstance(cache, dict):
        return _payload(sym, cik, None, "companyfacts unavailable")
    return _payload(sym, cik, cache)


async def warm() -> int:
    """Refresh every watchlist issuer's cache (evidence-pack readiness)."""
    global _CIK_CACHE, _CIK_CACHE_AT
    ciks = await _watchlist_ciks()
    _CIK_CACHE, _CIK_CACHE_AT = ciks, time.time()
    ttl_s = float(cfg()["ttl_days"]) * 86400
    done = 0
    for sym, cik in sorted(ciks.items()):
        if await _ensure(cik, ttl_s):
            done += 1
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    _stats["warmed"] = done
    log.info("fundamentals: %d/%d watchlist issuer(s) cached", done, len(ciks))
    return done


router = APIRouter()


@router.get("/api/fundamentals/{symbol}")
async def get_fundamentals(symbol: str) -> JSONResponse:
    """Annual XBRL rows + ROE / FCF-NI series for one issuer."""
    return JSONResponse(await fundamentals(symbol))


async def _loop() -> None:
    conf = cfg()
    await asyncio.sleep(max(0.0, float(conf["warm_start_delay_s"])))
    while True:
        started = time.time()
        try:
            done = await warm()
            runlog.record("fundamentals", True, time.time() - started,
                          f"{done} issuer(s) cached")
        except Exception as e:
            _stats["errors"] += 1
            log.exception("fundamentals warm pass failed")
            runlog.record("fundamentals", False, time.time() - started,
                          str(e)[:200])
        delay = max(1, int(conf["warm_interval_days"])) * 86400
        _stats["next_run"] = time.time() + delay
        await asyncio.sleep(delay)


def start() -> None:
    global _task
    if os.getenv("FUNDAMENTALS_ENABLED", "1") != "1":
        log.info("fundamentals disabled (set FUNDAMENTALS_ENABLED=1)")
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
    return {**_stats, "enabled": os.getenv("FUNDAMENTALS_ENABLED", "1") == "1",
            "ciks": len(_CIK_CACHE)}