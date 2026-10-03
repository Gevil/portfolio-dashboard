"""Finnhub provider (free plan).

`get_quote` returns a typed result dict
{"kind": "ok"|"no_data"|"rate_limited"|"unknown_symbol", "data": ...}
so callers can tell an unknown symbol (worth caching) from a rate limit
(never cache; the backoff already handles it).
"""
import os
import time
import asyncio
import logging
import httpx

log = logging.getLogger(__name__)

API_KEY = (os.getenv("FINNHUB_API_KEY") or "").strip()
BASE_URL = "https://finnhub.io/api/v1"

_max_backoff = 600
_initial_delay = 30
_backoff: dict[str, float] = {}
_consecutive_429: dict[str, int] = {}

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


def _client_for_loop() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=8, follow_redirects=True)
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
            log.debug("Finnhub client close failed: %s", e)


def _enabled() -> bool:
    return bool(API_KEY)


def _can_fetch() -> bool:
    return time.time() >= _backoff.get("finnhub", 0.0)


def _set_backoff(next_allowed: float):
    _backoff["finnhub"] = next_allowed


async def _get(path: str, params: dict | None = None) -> tuple[str, httpx.Response | None]:
    """Perform a GET. Returns (kind, response); response is only set for kind == OK."""
    if not _enabled():
        return NO_DATA, None
    if not _can_fetch():
        return RATE_LIMITED, None

    params = dict(params or {})
    params["token"] = API_KEY

    try:
        client = _client_for_loop()
        r = await client.get(f"{BASE_URL}{path}", params=params)
        if r.status_code == 429:
            consec = _consecutive_429.get("finnhub", 0) + 1
            _consecutive_429["finnhub"] = consec
            delay = min(
                _initial_delay * (2 ** min(consec - 1, 4)),
                _max_backoff,
            )
            _set_backoff(time.time() + delay)
            log.warning(
                "Finnhub rate limited (consecutive=%d), next attempt in %.1fs",
                consec,
                delay,
            )
            return RATE_LIMITED, None
        if r.status_code in (403, 404):
            # 403 = endpoint not on the free plan, 404 = no such symbol.
            log.warning(
                "Finnhub %d for %s (symbol=%s)", r.status_code, path, params.get("symbol"),
            )
            return UNKNOWN_SYMBOL, None
        if r.status_code not in (200, 201):
            log.warning("Finnhub non-2xx: %d for %s", r.status_code, path)
            return NO_DATA, None
        # On success, reset consecutive 429
        _consecutive_429["finnhub"] = 0
        return OK, r
    except Exception as e:
        log.warning("Finnhub request error on %s: %s", path, e)
        return NO_DATA, None


def _json(r: httpx.Response) -> dict | None:
    try:
        data = r.json()
    except Exception as e:
        log.warning("Finnhub bad JSON: %s", e)
        return None
    return data if isinstance(data, dict) else None


async def get_profile(ticker: str) -> dict | None:
    """
    Fetch profile: sector, industry.
    """
    kind, r = await _get("/stock/profile2", {"symbol": ticker})
    if kind != OK or r is None:
        return None

    data = _json(r)
    if data is None:
        return None

    sector = (data.get("finnhubIndustry") or data.get("sector") or "").strip() or None
    industry = (data.get("finnhubIndustry") or data.get("industry") or "").strip() or None

    return {
        "sector": sector,
        "industry": industry,
    }


async def get_candles(ticker: str, resolution: str = "D", limit: int = 90) -> list[dict] | None:
    """
    Fetch historical candles via Finnhub /stock/candle.

    PREMIUM ONLY: /stock/candle is not part of the free plan - it answers 403
    there, so this returns None on a free-tier key. Kept for keys that have
    the paid data plan; call sites must treat None as "no data".

    Returns list of {"t": timestamp, "c": close} or None.
    """
    if not _enabled():
        return None

    now = int(time.time())
    to = now
    from_ts = now - (limit * 86400)

    kind, r = await _get(
        "/stock/candle",
        {
            "symbol": ticker,
            "resolution": resolution,
            "from": from_ts,
            "to": to,
        },
    )
    if kind != OK or r is None:
        return None

    data = _json(r)
    if data is None:
        return None

    if data.get("s") != "ok":
        log.warning("Finnhub candles for %s: status=%s", ticker, data.get("s"))
        return None

    ts_list = data.get("t") or []
    c_list = data.get("c") or []

    if not ts_list or not c_list:
        return None

    points = []
    for t, c in zip(ts_list, c_list):
        if c is None:
            continue
        points.append({
            "t": int(t),
            "c": float(c),
        })

    return points or None


async def get_quote(ticker: str) -> dict:
    """
    Typed result; on kind == "ok", data is:
      {"price": float, "previousClose": float}
    """
    kind, r = await _get("/quote", {"symbol": ticker})
    if kind != OK or r is None:
        return _result(kind)

    data = _json(r)
    if data is None:
        return _result(NO_DATA)

    c = data.get("c")  # current price
    pc = data.get("pc")  # previous close

    if not c and not pc:
        # Finnhub answers 200 with an all-zero payload for symbols it does
        # not carry (non-US listings on the free plan).
        log.warning("Finnhub quote for %s is all zeros - treating as unknown symbol", ticker)
        return _result(UNKNOWN_SYMBOL)

    if not c or not pc:
        log.warning("Finnhub quote for %s incomplete (c=%s, pc=%s)", ticker, c, pc)
        return _result(NO_DATA)

    return _result(OK, {
        "price": float(c),
        "previousClose": float(pc),
    })
