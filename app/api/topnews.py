"""Top-news cache for the dashboard.

Per-ticker most-relevant news, refreshed by a periodic task:
- Primary: Finnhub company-news (3-day window, per symbol).
- Fallback: GDELT DOC API (key-free, last 24h, newest first) - requests are
  spaced >=2s apart, retried once after 30s on HTTP 429, then the endpoint is
  left alone until a persisted backoff marker expires.

Results are cached in data/topnews.json (atomic replace, plus an in-memory
copy so a corrupt or missing file degrades to memory instead of empty).
Each ticker is fetched in isolation: a failing provider keeps that ticker's
last-known-good entry and never aborts the rest of the batch.
"""
import os
import re
import json
import time
import asyncio
import logging
import datetime

import httpx

from app.api.textsafe import clean_text

log = logging.getLogger(__name__)

DATA_DIR = os.getenv("HISTORY_DIR", "/app/data")
CACHE_FILE = os.path.join(DATA_DIR, "topnews.json")
GDELT_BACKOFF_FILE = os.path.join(DATA_DIR, "gdelt_backoff.json")

FINNHUB_KEY = (os.getenv("FINNHUB_API_KEY") or "").strip()
FINNHUB_BASE = "https://finnhub.io/api/v1"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"

ITEMS_PER_TICKER = 10
INTERVAL_S = int(os.getenv("TOPNEWS_INTERVAL_S", "900"))

# GDELT doc-API etiquette (it answers HTTP 429 when pushed too hard).
GDELT_MIN_SPACING_S = 2.0
GDELT_RETRY_AFTER_S = 30.0
GDELT_BACKOFF_S = 900.0

# Last-known-good cache held in process, so a corrupt/missing file on disk
# still serves the previous payload.
_mem_cache: dict = {}
_gdelt_last_call = 0.0   # time.monotonic() of the last GDELT request
_gdelt_until_ts = 0.0    # time.time() until which GDELT stays skipped


def _load_disk() -> dict:
    global _mem_cache
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _mem_cache = data
            return data
        log.warning("topnews cache is %s, not an object - using memory",
                    type(data).__name__)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("topnews cache read failed (%s) - using memory", e)
    return dict(_mem_cache)


def _save_disk(cache: dict) -> None:
    """Publish the cache atomically (tmp + os.replace) and keep it in memory."""
    global _mem_cache
    _mem_cache = cache
    tmp = CACHE_FILE + ".tmp"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        log.warning("topnews cache write failed: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _gdelt_backoff_until() -> float:
    """Epoch seconds until which GDELT is backed off (memory, else disk)."""
    global _gdelt_until_ts
    if _gdelt_until_ts > time.time():
        return _gdelt_until_ts
    try:
        with open(GDELT_BACKOFF_FILE) as f:
            until = float((json.load(f) or {}).get("until_ts") or 0)
    except Exception:
        until = 0.0
    _gdelt_until_ts = max(_gdelt_until_ts, until)
    return _gdelt_until_ts


def _set_gdelt_backoff(seconds: float) -> None:
    """Mark GDELT as off-limits in memory and on disk for `seconds`."""
    global _gdelt_until_ts
    _gdelt_until_ts = time.time() + seconds
    tmp = GDELT_BACKOFF_FILE + ".tmp"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump({"until_ts": _gdelt_until_ts}, f)
        os.replace(tmp, GDELT_BACKOFF_FILE)
    except Exception as e:
        log.warning("topnews gdelt backoff write failed: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
    log.warning("topnews gdelt: backing off for %.0fs", seconds)


def _trunc(s: str, n: int = 160) -> str:
    """Provider text -> one-line, control/bidi/URL-free, length-capped text.
    The cache feeds the UI, news alerts and LLM evidence packs, so untrusted
    feed text is sanitised once, here, at the door."""
    return clean_text(s, max_len=n)


def _utc_dt(epoch: float) -> datetime.datetime:
    """Epoch seconds -> tz-aware UTC datetime (this module never goes naive)."""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)


def _fin_published_at(it: dict) -> int:
    """Finnhub /company-news publishes epoch seconds in its "datetime" field."""
    raw = it.get("datetime")
    if raw is None:
        return 0
    try:
        return int(_utc_dt(float(raw)).timestamp())
    except (TypeError, ValueError, OSError, OverflowError):
        log.warning("topnews finnhub: bad datetime %r", raw)
        return 0


def _norm_fin_items(raw: list) -> list[dict]:
    items = []
    for it in raw:
        items.append({
            "headline": _trunc(it.get("headline")),
            "summary": _trunc(it.get("summary") or it.get("headline")),
            "url": it.get("url") or "",
            "source": it.get("source") or "finnhub",
            "published_at": _fin_published_at(it),
        })
    items.sort(key=lambda x: x["published_at"], reverse=True)
    return items[:ITEMS_PER_TICKER]


_SEENDATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})")


def _gdelt_published_at(seen: str) -> int:
    """GDELT seendate ("20260912T093000Z") -> epoch seconds, parsed as UTC."""
    m = _SEENDATE_RE.match(seen or "")
    if not m:
        return 0
    dt = datetime.datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)),
        tzinfo=datetime.timezone.utc,
    )
    return int(dt.timestamp())


def _norm_gdelt_items(raw: list) -> list[dict]:
    items = []
    for it in raw:
        items.append({
            "headline": _trunc(it.get("title")),
            "summary": _trunc(it.get("title")),
            "url": it.get("url") or "",
            "source": it.get("domain") or "gdelt",
            "published_at": _gdelt_published_at(it.get("seendate") or ""),
            "domain": it.get("domain") or "",
        })
    # GDELT's own ordering is unreliable even with sort=DateDesc
    items.sort(key=lambda x: x["published_at"], reverse=True)
    return items[:ITEMS_PER_TICKER]


async def _fetch_finnhub(client: httpx.AsyncClient,
                         ticker: str) -> list[dict] | None:
    if not FINNHUB_KEY:
        return None
    # 3-day window: "today only" is often empty for EU tickers at close
    today = time.strftime("%Y-%m-%d")
    three = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 2 * 86400))
    r = await client.get(
        f"{FINNHUB_BASE}/company-news",
        params={"symbol": ticker, "from": three, "to": today,
                "token": FINNHUB_KEY},
        timeout=15,
    )
    if r.status_code != 200:
        log.warning("topnews finnhub %s: HTTP %s", ticker, r.status_code)
        return None
    data = r.json()
    if not isinstance(data, list) or not data:
        return None
    return _norm_fin_items(data)


async def _gdelt_space_out() -> None:
    """Hold >=GDELT_MIN_SPACING_S between consecutive GDELT requests."""
    global _gdelt_last_call
    if _gdelt_last_call:
        wait = GDELT_MIN_SPACING_S - (time.monotonic() - _gdelt_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
    _gdelt_last_call = time.monotonic()


async def _gdelt_request(client: httpx.AsyncClient,
                         ticker: str) -> httpx.Response:
    params = {
        "query": ticker,  # GDELT rejects quoted single-word phrases
        "mode": "ArtList",
        "maxrecords": 20,
        "sort": "DateDesc",  # newest first; VolumeDesc buried fresh articles
        "format": "json",
        "timespan": "1d",  # 15min|1h|1d|7d|1w|1mo|6mo|1y
    }
    await _gdelt_space_out()
    return await client.get(GDELT_DOC, params=params, timeout=20)


async def fetch_gdelt(client: httpx.AsyncClient,
                      ticker: str) -> list[dict] | None:
    until = _gdelt_backoff_until()
    if until > time.time():
        log.warning("topnews gdelt %s: skipped, backoff for another %.0fs",
                    ticker, until - time.time())
        return None
    r = await _gdelt_request(client, ticker)
    if r.status_code == 429:
        log.warning("topnews gdelt %s: HTTP 429, retrying in %.0fs",
                    ticker, GDELT_RETRY_AFTER_S)
        await asyncio.sleep(GDELT_RETRY_AFTER_S)
        r = await _gdelt_request(client, ticker)
        if r.status_code == 429:
            log.warning("topnews gdelt %s: HTTP 429 after retry", ticker)
            _set_gdelt_backoff(GDELT_BACKOFF_S)
            return None
    if r.status_code != 200:
        log.warning("topnews gdelt %s: HTTP %s", ticker, r.status_code)
        return None
    data = r.json()
    arts = data.get("articles") if isinstance(data, dict) else None
    if not arts:
        return None
    return _norm_gdelt_items(arts)


async def fetch_top_news(tickers: list[str]) -> dict:
    """Refresh the per-ticker top-news cache (finnhub, else GDELT).

    Every ticker is fetched inside its own try/except, so a provider failure
    (e.g. GDELT HTTP 429) only keeps that ticker's last-known-good entry.
    The cache is written after each ticker, so partial progress survives a
    crash. Returns {"total": int, "ok": int, "failed": [ticker, ...]}.
    """
    cache = _load_disk()
    tickers = [t.upper() for t in tickers]
    failed: list[str] = []
    async with httpx.AsyncClient() as client:
        for ticker in tickers:
            try:
                items = await _fetch_finnhub(client, ticker)
                source = "finnhub"
                if items is None:
                    items = await fetch_gdelt(client, ticker)
                    source = "gdelt"
                if items is None:
                    # Keyless RSS fallback (Google News; Yahoo RSS is dead).
                    from app.api import rss
                    rs = await rss.fetch_symbol(ticker)
                    if rs:
                        items = rs[:ITEMS_PER_TICKER]
                        source = "rss"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("topnews %s: provider error (%s), keeping cache",
                            ticker, e)
                failed.append(ticker)
                continue
            if items is None:
                # retain last-known-good
                log.warning("topnews %s: fetch failed, keeping cache",
                            ticker)
                failed.append(ticker)
                continue
            cache[ticker] = {
                "ts": time.time(),
                "source": source,
                "items": items,
            }
            _save_disk(cache)  # incremental: keep partial progress on crash
            await asyncio.sleep(1.5)  # be nice to free endpoints
    log.info("topnews refreshed: %s",
             {t: cache.get(t, {}).get("source") for t in tickers})
    return {"total": len(tickers), "ok": len(tickers) - len(failed),
            "failed": failed}


def get_top_news(ticker: str | None = None) -> dict:
    """Read the top-news cache (disk, degrading to the in-memory copy)."""
    cache = _load_disk()
    if ticker:
        return cache.get(ticker.upper(), {})
    return cache