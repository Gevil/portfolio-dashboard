"""Twelve Data provider (free plan).

Two things worth knowing before editing:

- Request pacing is GLOBAL (free plan: 8 credits/min). The spacing is held
  under `_pace_lock`, so concurrent callers queue instead of all reading the
  same `_last_req_ts`, sleeping the same amount and firing together.
- `get_quote` / `get_time_series` return a typed result dict
  {"kind": "ok"|"no_data"|"rate_limited"|"unknown_symbol", "data": ...}
  so callers can tell "this symbol does not exist here" (cache it) from
  "we are rate limited right now" (do NOT cache, back off).
"""
import os
import time
import asyncio
import datetime
import logging
import httpx

log = logging.getLogger(__name__)

API_KEY = (os.getenv("TWELVE_DATA_API_KEY") or "").strip()
BASE_URL = "https://api.twelvedata.com"

_max_backoff = 600
_initial_delay = 30
_backoff: float = 0.0
_consecutive_429: int = 0
_last_req_ts: float = 0.0
_min_interval: float = 10.0
# Guards the (since_last -> sleep -> _last_req_ts) sequence as one unit.
_pace_lock = asyncio.Lock()

_quote_cache: dict[str, dict] = {}
_quote_ttl = 60  # 60 seconds for live price
_ts_cache: dict[str, dict] = {}
_ts_ttl = 900  # 15 minutes for time-series data
# Bound both caches: the keys are caller-supplied symbols, so an unbounded
# dict is an unbounded memory leak.
_MAX_CACHE_ENTRIES = 200

# Typed result kinds
OK = "ok"
NO_DATA = "no_data"
RATE_LIMITED = "rate_limited"
UNKNOWN_SYMBOL = "unknown_symbol"

# One AsyncClient for the module (connection reuse instead of a new pool per
# request). Rebuilt if the loop it was bound to is gone.
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _result(kind: str, data=None) -> dict:
    """Typed provider result. `data` is only meaningful for kind == OK."""
    return {"kind": kind, "data": data}


def _prune(cache: dict, limit: int = _MAX_CACHE_ENTRIES):
    """Drop oldest insertions until the cache fits `limit`."""
    while len(cache) > limit:
        cache.pop(next(iter(cache)), None)


def _cache_put(cache: dict, key: str, data):
    cache.pop(key, None)  # re-insert so insertion order tracks recency
    cache[key] = {"ts": time.time(), "data": data}
    _prune(cache)


def _client_for_loop() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=10)
        _client_loop = loop
    return _client


async def aclose() -> None:
    """Close the shared client. Safe to call repeatedly / without a client."""
    global _client, _client_loop
    client, _client, _client_loop = _client, None, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception as e:
            log.debug("Twelve Data client close failed: %s", e)


def _enabled() -> bool:
    return bool(API_KEY)


def _can_fetch() -> bool:
    return time.time() >= _backoff


def _set_backoff(next_allowed: float):
    global _backoff
    _backoff = next_allowed


async def _pace():
    """Serialize the global request spacing across concurrent callers."""
    global _last_req_ts
    async with _pace_lock:
        since_last = time.time() - _last_req_ts
        if since_last < _min_interval:
            await asyncio.sleep(_min_interval - since_last)
        _last_req_ts = time.time()


async def _get(path: str, params: dict | None = None) -> tuple[str, httpx.Response | None]:
    """
    Perform a paced GET. Returns (kind, response); response is only set for
    kind == OK.
    """
    global _consecutive_429

    if not _enabled():
        return NO_DATA, None
    if not _can_fetch():
        return RATE_LIMITED, None

    params = dict(params or {})
    params["apikey"] = API_KEY

    await _pace()

    try:
        client = _client_for_loop()
        r = await client.get(f"{BASE_URL}{path}", params=params)

        if r.status_code == 429:
            _consecutive_429 += 1
            delay = min(
                _initial_delay * (2 ** min(_consecutive_429 - 1, 5)),
                _max_backoff,
            )
            _set_backoff(time.time() + delay)
            log.warning(
                "Twelve Data rate limited (consecutive=%d), next attempt in %.1fs",
                _consecutive_429,
                delay,
            )
            return RATE_LIMITED, None

        if r.status_code == 404:
            # Twelve Data answers 404 for symbols outside the plan/universe.
            log.warning("Twelve Data unknown symbol for %s: %s", path, params.get("symbol"))
            return UNKNOWN_SYMBOL, None

        if r.status_code not in (200, 201):
            log.warning("Twelve Data non-2xx: %d for %s", r.status_code, path)
            return NO_DATA, None

        # On success, reset consecutive 429
        _consecutive_429 = 0
        return OK, r
    except Exception as e:
        log.warning("Twelve Data request error on %s: %s", path, e)
        return NO_DATA, None


def _json(r: httpx.Response) -> dict | None:
    try:
        data = r.json()
    except Exception as e:
        log.warning("Twelve Data bad JSON: %s", e)
        return None
    return data if isinstance(data, dict) else None


def _body_kind(data: dict | None) -> str | None:
    """Twelve Data also reports errors in a 200 body."""
    if data is None:
        return NO_DATA
    if str(data.get("status") or "").lower() == "error":
        code = data.get("code")
        message = data.get("message")
        if code in (404, "404"):
            log.warning("Twelve Data unknown symbol: %s", message)
            return UNKNOWN_SYMBOL
        if code in (429, "429"):
            log.warning("Twelve Data rate limited (body): %s", message)
            return RATE_LIMITED
        log.warning("Twelve Data error body (code=%s): %s", code, message)
        return NO_DATA
    return None


async def get_quote(ticker: str) -> dict:
    """
    Typed result; on kind == "ok", data is:
      {"price": float, "previousClose": float}
    """
    now = time.time()
    cached = _quote_cache.get(ticker)
    if cached and now - cached["ts"] < _quote_ttl:
        return _result(OK, cached["data"])

    kind, r = await _get("/quote", {"symbol": ticker})
    if kind != OK or r is None:
        return _result(kind)

    data = _json(r)
    body_kind = _body_kind(data)
    if body_kind is not None:
        return _result(body_kind)

    # Close only: the session high/low/open are not the live price, and
    # falling back to them showed the day's high as the current quote.
    price = data.get("close")
    if isinstance(price, str):
        price = float(price)
    prev_close = data.get("previous_close")
    if isinstance(prev_close, str):
        prev_close = float(prev_close)

    if not price or not prev_close:
        log.warning("Twelve Data quote for %s has no usable close/previous_close", ticker)
        return _result(NO_DATA)

    result = {
        "price": float(price),
        "previousClose": float(prev_close),
    }

    _cache_put(_quote_cache, ticker, result)

    return _result(OK, result)


async def get_fundamentals(ticker: str) -> dict | None:
    """
    Fetch fundamentals for valuation/context.
    Returns dict with subset:
      pe, forwardPE, dividendYield, roe,
      sector, industry, shortRatio, analystTarget, recommendation
    or None if not available.
    """
    kind, r = await _get("/fundamentals", {"symbol": ticker})
    if kind != OK or r is None:
        return None

    data = _json(r)
    if data is None or _body_kind(data) is not None:
        return None

    def safe_float(v):
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    # valuationMeasures
    val = data.get("valuationMeasures") or {}
    pe = safe_float(val.get("trailingPE"))
    fwd_pe = safe_float(val.get("forwardPE"))

    # financialHighlights
    highlights = data.get("financialHighlights") or {}
    div_yield = safe_float(highlights.get("dividendYield"))
    roe = safe_float(highlights.get("returnOnEquity"))

    # sector/industry
    sector = (data.get("sector") or "").strip() or None
    industry = (data.get("industry") or "").strip() or None

    # shortRatio (sometimes in financialHighlights)
    short_ratio = safe_float(highlights.get("shortRatio"))

    # analystTarget and recommendation from analystRatings
    analyst = data.get("analystRatings") or {}
    analyst_target = None
    recommendation = None
    if isinstance(analyst, dict):
        targets = analyst.get("targetMeanPrice") or {}
        analyst_target = safe_float(targets.get("value"))
        rec = analyst.get("recommendationMean")
        if rec is not None:
            recommendation = str(rec)

    return {
        "pe": pe,
        "forwardPE": fwd_pe,
        "dividendYield": div_yield,
        "roe": roe,
        "sector": sector,
        "industry": industry,
        "shortRatio": short_ratio,
        "analystTarget": analyst_target,
        "recommendation": recommendation,
    }


async def get_time_series(
    ticker: str,
    interval: str = "15min",
    outputsize: int = 100,
) -> dict:
    """
    Typed result; on kind == "ok", data is a list of {"t": unix_ts, "c": close}.
    Aggressively cached in memory and intended to be persisted externally.
    """
    cache_key = f"{ticker}:{interval}:{outputsize}"

    now = time.time()
    cached = _ts_cache.get(cache_key)
    if cached and now - cached["ts"] < _ts_ttl:
        return _result(OK, cached["data"])

    kind, r = await _get(
        "/time_series",
        {
            "symbol": ticker,
            "interval": interval,
            "outputsize": outputsize,
        },
    )
    if kind != OK or r is None:
        return _result(kind)

    data = _json(r)
    body_kind = _body_kind(data)
    if body_kind is not None:
        return _result(body_kind)

    timeseries = data.get("values")
    if not timeseries:
        log.warning("Twelve Data time_series for %s (%s) returned no values", ticker, interval)
        return _result(NO_DATA)

    points = []
    for entry in timeseries:
        dt = entry.get("datetime")
        c = entry.get("close")
        if not dt or c is None:
            continue
        try:
            t = int(datetime.datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp())
            c = float(c)
        except Exception:
            continue
        points.append({"t": t, "c": c})

    if not points:
        log.warning("Twelve Data time_series for %s (%s) had no parsable bars", ticker, interval)
        return _result(NO_DATA)

    _cache_put(_ts_cache, cache_key, points)

    return _result(OK, points)
