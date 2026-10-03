"""USD->EUR rates (key-free) for the data layer and the /api/forex endpoint.

Latest rate chain: frankfurter.dev (ECB reference, follows redirects) ->
yahoo ``EURUSD=X`` -> last cached value (also persisted to ``forex.json`` so a
restart keeps it). Every result carries its provenance:

    {"rate": float | None, "source": str | None, "asOf": epoch_s | None,
     "stale": bool}

``stale`` is True once the rate is older than ``STALE_AFTER`` (24 h). A stale
rate is only good for display: persisted conversions must check ``stale``.
``rate`` is None only when no valid rate was ever obtained - a made-up constant
is never presented as a market rate.

``daily_rates(days)`` gives one EUR-per-USD rate per ECB reference day so a
series is converted bar by bar with the rate valid for that bar's day instead
of re-converting a whole window at today's rate.
"""
import asyncio
import datetime
import logging
import os
import time

import httpx

from app.api import jsonstore

log = logging.getLogger(__name__)

TTL = 900                     # latest rate cache
STALE_AFTER = 24 * 3600       # older than this: stale, display-only
RETRY_AFTER_FAILURE = 60      # don't hammer providers while they are all down
DAILY_TTL = 6 * 3600          # per-day series refresh
DAILY_MAX_DAYS = 1500

FRANKFURTER_URL = "https://api.frankfurter.dev"
YAHOO_PAIR = "EURUSD=X"       # USD per 1 EUR; we invert it

_state: dict = {"rate": None, "source": None, "asOf": None}
_loaded = False
_next_retry = 0.0
_lock = asyncio.Lock()

_daily: dict[str, float] = {}
_daily_fetched_at = 0.0
_daily_span = 0
_daily_lock = asyncio.Lock()

# One AsyncClient for the module; rebuilt when its event loop is gone.
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _client_for_loop() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=6, follow_redirects=True)
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
            log.debug("forex client close failed: %s", e)


def _cache_file() -> str:
    return os.path.join(os.getenv("HISTORY_DIR", "/app/data"), "forex.json")


def _sane(rate) -> float | None:
    """A USD->EUR rate outside this band is a broken payload, not a market move."""
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return None
    return rate if 0.5 <= rate <= 2.0 else None


def _load_disk() -> None:
    """Restore the last cached rate once per process."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    data = jsonstore.load(_cache_file(), None)
    if not isinstance(data, dict):
        return
    rate, as_of = _sane(data.get("rate")), data.get("asOf")
    if rate is None or not isinstance(as_of, (int, float)) or isinstance(as_of, bool):
        return
    _state.update(rate=rate, source=str(data.get("source") or "cache"), asOf=float(as_of))


def _result() -> dict:
    rate, as_of = _state["rate"], _state["asOf"]
    if rate is None or as_of is None:
        return {"rate": None, "source": None, "asOf": None, "stale": True}
    return {
        "rate": rate,
        "source": _state["source"],
        "asOf": int(as_of),
        "stale": (time.time() - as_of) > STALE_AFTER,
    }


# ------------------------------------------------------------------ providers

async def _frankfurter_latest(client: httpx.AsyncClient) -> float | None:
    r = await client.get(f"{FRANKFURTER_URL}/v1/latest",
                         params={"base": "USD", "symbols": "EUR"})
    if r.status_code != 200:
        log.warning("frankfurter non-2xx: %d", r.status_code)
        return None
    payload = r.json()
    if not isinstance(payload, dict):
        return None
    return _sane((payload.get("rates") or {}).get("EUR"))


def _yahoo_eurusd_blocking(period: str) -> list[tuple[str, float]]:
    """[(iso_day, USD per EUR)] from yahoo daily bars (executor only)."""
    import math
    import yfinance as yf
    hist = yf.Ticker(YAHOO_PAIR).history(period=period, interval="1d")
    if hist is None or hist.empty or "Close" not in hist.columns:
        return []
    out = []
    for ts, row in hist.iterrows():
        try:
            c = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if math.isfinite(c) and c > 0:
            out.append((ts.date().isoformat(), c))
    return out


async def _yahoo_latest(_client: httpx.AsyncClient) -> float | None:
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _yahoo_eurusd_blocking, "5d")
    if not rows:
        return None
    return _sane(1.0 / rows[-1][1])


_PROVIDERS = (
    ("frankfurter", _frankfurter_latest),
    ("yahoo", _yahoo_latest),
)


async def _fetch_latest() -> tuple[float, str] | None:
    client = _client_for_loop()
    for name, fetch in _PROVIDERS:
        try:
            rate = await fetch(client)
        except Exception as e:
            log.warning("forex provider %s failed: %s", name, e)
            continue
        if rate is not None:
            return rate, name
        log.warning("forex provider %s returned no usable rate", name)
    return None


async def get_rate_usd_eur() -> dict:
    """Latest USD->EUR (1 USD = rate EUR) with provenance; cached 15 min."""
    global _next_retry
    _load_disk()
    now = time.time()
    if _state["rate"] is not None and (now - _state["asOf"]) < TTL:
        return _result()
    if now < _next_retry:
        return _result()

    async with _lock:
        now = time.time()
        if _state["rate"] is not None and (now - _state["asOf"]) < TTL:
            return _result()
        if now < _next_retry:
            return _result()
        got = await _fetch_latest()
        if got is not None:
            rate, source = got
            _state.update(rate=rate, source=source, asOf=now)
            _next_retry = 0.0
            await asyncio.to_thread(
                jsonstore.save, _cache_file(),
                {"rate": rate, "source": source, "asOf": now})
            return _result()
        _next_retry = now + RETRY_AFTER_FAILURE

    res = _result()
    if res["rate"] is None:
        log.error("forex: no USD->EUR rate available from any provider")
    else:
        log.warning("forex refresh failed; keeping cached %.4f from %s (age %.0fs, stale=%s)",
                    res["rate"], res["source"], time.time() - res["asOf"], res["stale"])
    return res


def cached_rate() -> dict:
    """Last known rate without a fetch (rate None if never obtained)."""
    _load_disk()
    return _result()


# ------------------------------------------------------------------ daily

async def _frankfurter_daily(client: httpx.AsyncClient, start: str, end: str) -> dict[str, float]:
    r = await client.get(f"{FRANKFURTER_URL}/v1/{start}..{end}",
                         params={"base": "USD", "symbols": "EUR"})
    if r.status_code != 200:
        log.warning("frankfurter range non-2xx: %d", r.status_code)
        return {}
    payload = r.json()
    rates = payload.get("rates") if isinstance(payload, dict) else None
    out: dict[str, float] = {}
    for day, row in (rates or {}).items():
        rate = _sane((row or {}).get("EUR")) if isinstance(row, dict) else None
        if rate is not None:
            out[str(day)] = rate
    return out


async def _yahoo_daily(days: int) -> dict[str, float]:
    period = "1y" if days <= 365 else "5y"
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _yahoo_eurusd_blocking, period)
    out: dict[str, float] = {}
    for day, usd_per_eur in rows:
        rate = _sane(1.0 / usd_per_eur)
        if rate is not None:
            out[day] = rate
    return out


async def daily_rates(days: int) -> dict[str, float]:
    """{iso_date: EUR per 1 USD} for the last ``days`` days (reference days only).

    Weekends/holidays are absent - use ``rate_on`` to carry the last rate
    forward. Never raises: {} when nothing could be fetched or cached.
    """
    global _daily_fetched_at, _daily_span
    try:
        days = max(1, min(int(days), DAILY_MAX_DAYS))
    except (TypeError, ValueError):
        days = 30
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = (today - datetime.timedelta(days=days)).isoformat()

    def _window() -> dict[str, float]:
        return {d: r for d, r in _daily.items() if d >= start}

    now = time.time()
    if _daily and days <= _daily_span and now - _daily_fetched_at < DAILY_TTL:
        return _window()
    async with _daily_lock:
        now = time.time()
        if _daily and days <= _daily_span and now - _daily_fetched_at < DAILY_TTL:
            return _window()
        fetched: dict[str, float] = {}
        try:
            fetched = await _frankfurter_daily(_client_for_loop(), start, today.isoformat())
        except Exception as e:
            log.warning("forex daily (frankfurter) failed: %s", e)
        if not fetched:
            try:
                fetched = await _yahoo_daily(days)
            except Exception as e:
                log.warning("forex daily (yahoo) failed: %s", e)
        if fetched:
            _daily.update(fetched)
            _daily_fetched_at = now
            _daily_span = max(days, _daily_span)
    return _window()


def rate_on(rates: dict[str, float], iso_date: str, max_lookback: int = 7) -> float | None:
    """Rate valid on ``iso_date``: that day's, else the latest earlier reference
    day within ``max_lookback`` days (weekend / holiday carry-forward)."""
    try:
        day = datetime.date.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return None
    for back in range(max_lookback + 1):
        rate = rates.get((day - datetime.timedelta(days=back)).isoformat())
        if rate is not None:
            return rate
    return None
