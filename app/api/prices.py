"""Price data layer: ONE series per watchlist entry - its listing (see registry).

* Holdings are valued and charted from the held EUR listing (yahoo, e.g.
  ``ASML.AS``); the benchmark from yahoo ``^GSPC``. The stored history, the
  quote, /api/prices, /api/history, the SSE stream and every alert read that
  same series, so the 1D chart can no longer interleave two sources.
* The US live feeds (Twelve Data / Finnhub WebSocket) only populate
  ``live_ws.live_prices`` and appear on price items as a separately labelled
  ``usLive`` reference. They never write into the listing series.
* A provider mapping of None (registry) means "unsupported": the call is
  skipped, no request is sent and no negative-cache entry is written.
* History is retained by age (<=24 h 1-min, <=7 d 5-min, <=365 d daily) and
  persisted at most once per ``HISTORY_WRITE_INTERVAL`` from an executor.
"""
import asyncio
import datetime
import json
import logging
import math
import os
import time
from zoneinfo import ZoneInfo

import yfinance as yf

from app.api import forex, jsonstore, registry
from app.api.providers import finnhub, twelvedata

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ config

RANGES = ("1D", "1W", "1M", "3M", "6M", "1Y")

# Per-range history cache TTLs (seconds); doubled while the venue is closed.
HISTORY_CACHE_TTL_BY_RANGE = {
    "1D": 120,
    "1W": 900,
    "1M": 6 * 3600,
    "3M": 6 * 3600,
    "6M": 6 * 3600,
    "1Y": 6 * 3600,
}
HISTORY_CACHE_TTL_DEFAULT = 900
MAX_CACHE_ENTRIES = 200

HISTORY_DIR = os.getenv("HISTORY_DIR", "/app/data")
HISTORY_FILE = os.path.join(HISTORY_DIR, "listing_history.json")
VALUATION_FILE = os.path.join(HISTORY_DIR, "valuation.json")
HISTORY_FORMAT_VERSION = 2
HISTORY_MAX_DAYS = 365
HISTORY_WRITE_INTERVAL = 120   # seconds between whole-file writes (<= 60-300)
VALUATION_WRITE_INTERVAL = 60
VALUATION_YF_TTL = 86400

# Age retention: (max age, bucket seconds). Clipping is by age first.
RETENTION_1MIN_S = 24 * 3600
RETENTION_5MIN_S = 7 * 86400

# Quote polling / staleness
QUOTE_TTL_OPEN = 45            # cached quote reused this long while the venue is open
QUOTE_TTL_CLOSED = 900
STALE_OPEN_S = 30 * 60         # open venue but newest bar older than this: stale
STALE_CLOSED_S = 4 * 86400     # closed venue: stale after a long weekend + holiday
US_LIVE_MAX_AGE = 900
YF_TIMEOUT = 40
SEED_COOLDOWN = 600

RATE_LIMIT_INITIAL_DELAY = 60
MAX_BACKOFF = 600

# Source tags carried by persisted points (optional "src" key). A live poll
# beats a seeded/backfilled bar on a timestamp collision.
SRC_YF = "yf"
SRC_SEED = "seed"
DEFAULT_SRC = SRC_YF
SRC_PRIORITY = {SRC_YF: 10, SRC_SEED: 0}

# /api/seed-history intervals -> (yahoo interval, max lookback days, ~bars/day)
SEED_INTERVALS = {
    "1min": ("1m", 7, 510),
    "5min": ("5m", 60, 102),
    "15min": ("15m", 60, 34),
    "30min": ("30m", 60, 17),
    "1h": ("1h", 730, 9),
    "1day": ("1d", 3650, 1),
}
MAX_SEED_OUTPUTSIZE = 5000

# ------------------------------------------------------------------ state

history_store: dict[str, list[dict]] = {}     # id -> [{t, c, src?, v?}] in listing currency
_history_meta: dict[str, dict] = {}           # id -> {symbol, currency} the series was built from
_store_version: dict[str, int] = {}
_load_failed = False
_history_cache: dict[str, tuple] = {}
_history_inflight: dict[str, asyncio.Task] = {}

_valuation_cache: dict[str, dict] = {}
_valuation_inflight: set[str] = set()
_valuation_tasks: set = set()

_quotes: dict[str, dict] = {}                 # id -> latest listing quote
_quote_inflight: dict[str, asyncio.Task] = {}
_prev_close_cache: dict[str, tuple] = {}      # id -> (value|None, session_day, ts)
_seed_last: dict[str, float] = {}
_seed_inflight: dict[str, asyncio.Task] = {}

_backoff: dict[str, float] = {}
_consecutive_429: dict[str, int] = {}
_fetch_lock = asyncio.Lock()

# Negative-result cache: {(key, symbol): ts}. Keys: "yahoo" (quote/history),
# "yahoo:valuation", "twelvedata:fundamentals", "finnhub:profile". Shorter
# than the old hour: a transient empty answer must not blank a symbol for long.
_negative_cache: dict[tuple[str, str], float] = {}
_NEGATIVE_TTL = 600


# ------------------------------------------------------------------ helpers

def _finite(value) -> float | None:
    """float(value) when finite, else None (NaN/inf/garbage never leave this module)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _volume_of(row) -> int | None:
    """Non-negative integer volume from a provider row or an incoming point."""
    get = getattr(row, "get", None)
    if get is None:
        return None
    for key in ("v", "volume", "Volume"):
        raw = get(key)
        if raw is None:
            continue
        v = _finite(raw)
        if v is None or v < 0:
            return None
        return int(v)
    return None


def _prune_cache(cache: dict, limit: int = MAX_CACHE_ENTRIES):
    """Drop oldest insertions until `cache` fits `limit`."""
    while len(cache) > limit:
        cache.pop(next(iter(cache)), None)


def normalize_ts(ts) -> int:
    """Normalize a provider timestamp to unix seconds (values > 1e12 are ms)."""
    ts = int(ts)
    return ts if ts < 1_000_000_000_000 else ts // 1000


def _is_valid_ts(ts) -> bool:
    """Plausible timestamp: unix seconds at or after 2020-01-01. There is
    deliberately no upper constant (a hard 2030 cap silently dropped every
    later point); future points are rejected against the clock on persist."""
    try:
        return int(ts) >= 1577836800
    except (TypeError, ValueError):
        return False


def _src_priority(src: str | None) -> int:
    return SRC_PRIORITY.get(src or DEFAULT_SRC, SRC_PRIORITY[DEFAULT_SRC])


# Thin delegates: the registry is the single source of truth.

def get_watchlist() -> list[str]:
    return registry.ids()


def is_watched(symbol: str) -> bool:
    return registry.is_watched(symbol)


def is_alertable(symbol: str) -> bool:
    """False for benchmark/index entries and entries flagged alertable=false."""
    return registry.is_alertable(symbol)


def is_analyzable(symbol: str) -> bool:
    """False for index entries (and ETFs): there is no issuer to analyse."""
    return registry.is_analyzable(symbol)


def quote_provider(ticker: str) -> str | None:
    """Provider that produced the cached listing quote (always yahoo)."""
    return (_cache_get(ticker) or {}).get("quoteProvider")


# ------------------------------------------------------------------ throttled writer

class _ThrottledWriter:
    """Whole-file JSON writes: at most one per interval, always from an
    executor (never on the event loop), flushed on shutdown."""

    def __init__(self, path_fn, snapshot_fn, interval_fn, blocked_fn=lambda: False):
        self.path_fn = path_fn
        self.snapshot_fn = snapshot_fn
        self.interval_fn = interval_fn
        self.blocked_fn = blocked_fn
        self.dirty = False
        self.last = 0.0
        self._handle: asyncio.TimerHandle | None = None
        self._task: asyncio.Future | None = None

    def schedule(self):
        if self.blocked_fn():
            return
        self.dirty = True
        if self._handle is not None or (self._task is not None and not self._task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): stays dirty until flush()
        delay = max(0.0, self.last + self.interval_fn() - time.time())
        self._handle = loop.call_later(delay, self._fire)

    def _fire(self):
        self._handle = None
        self._task = asyncio.ensure_future(self._write())

    async def _write(self):
        if not self.dirty or self.blocked_fn():
            return
        snap = self.snapshot_fn()          # built on the loop thread
        self.dirty = False
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, jsonstore.save, self.path_fn(), snap)
        self.last = time.time()
        if not ok:
            log.error("write of %s failed; retrying", self.path_fn())
            self.dirty = True
            self._handle = loop.call_later(max(30, self.interval_fn()), self._fire)

    async def flush(self):
        """Write now if dirty (shutdown / tests)."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._task is not None and not self._task.done():
            try:
                await asyncio.shield(self._task)
            except Exception:
                pass
        if self.dirty and not self.blocked_fn():
            snap = self.snapshot_fn()
            self.dirty = False
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, jsonstore.save, self.path_fn(), snap)
            self.last = time.time()
            if not ok:
                self.dirty = True


def _history_snapshot() -> dict:
    return {
        "version": HISTORY_FORMAT_VERSION,
        "series": {
            t: {**(_history_meta.get(t) or {}), "points": list(pts)}
            for t, pts in history_store.items() if pts
        },
    }


_history_writer = _ThrottledWriter(
    lambda: HISTORY_FILE, _history_snapshot, lambda: HISTORY_WRITE_INTERVAL,
    blocked_fn=lambda: _load_failed)
_valuation_writer = _ThrottledWriter(
    lambda: VALUATION_FILE, lambda: dict(_valuation_cache),
    lambda: VALUATION_WRITE_INTERVAL)


def _schedule_history_write():
    if _load_failed:
        log.error("Refusing to schedule a history write: %s failed to load", HISTORY_FILE)
        return
    _history_writer.schedule()


def _schedule_valuation_write():
    _valuation_writer.schedule()


async def flush_stores():
    """Write history + valuation now (app shutdown)."""
    await _history_writer.flush()
    await _valuation_writer.flush()


# ------------------------------------------------------------------ history store

def _clean_points(points) -> list[dict]:
    out = []
    for p in points or []:
        if not isinstance(p, dict) or "t" not in p or "c" not in p:
            continue
        c = _finite(p["c"])
        try:
            t = normalize_ts(p["t"])
        except (TypeError, ValueError, OverflowError):
            continue
        if c is None or c <= 0 or not _is_valid_ts(t):
            continue
        point = {"t": t, "c": c}
        src = p.get("src")
        if src:
            point["src"] = str(src)
        v = _volume_of(p)
        if v is not None:
            point["v"] = v
        out.append(point)
    return out


def _load_history_store():
    global history_store, _history_meta, _load_failed
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("series"), dict):
            raise ValueError("history store has no 'series' object")
        store, meta = {}, {}
        for ticker, rec in data["series"].items():
            if not isinstance(rec, dict):
                continue
            pts = sorted(_clean_points(rec.get("points")), key=lambda p: p["t"])
            if pts:
                store[ticker] = pts
                meta[ticker] = {"symbol": rec.get("symbol"), "currency": rec.get("currency")}
        history_store, _history_meta = store, meta
    except Exception as e:
        # Quarantine the file so the next write cannot flush an empty store
        # over the only copy.
        history_store, _history_meta = {}, {}
        _load_failed = True
        quarantine = f"{HISTORY_FILE}.bad-{int(time.time())}"
        try:
            os.replace(HISTORY_FILE, quarantine)
        except OSError as move_err:
            log.error("Failed to load history store from %s (%s); could not quarantine it: %s",
                      HISTORY_FILE, e, move_err)
        else:
            log.error("Failed to load history store from %s: %s - quarantined as %s. "
                      "History writes are DISABLED until a good file is restored.",
                      HISTORY_FILE, e, quarantine)


def _load_valuation_cache():
    global _valuation_cache
    data = jsonstore.load(VALUATION_FILE, None)
    if isinstance(data, dict):
        _valuation_cache = data


_load_history_store()
_load_valuation_cache()


def _apply_granularity_cutoffs(points: list[dict], now_ts: int) -> list[dict]:
    """Retention by AGE: <=24h keeps one point per minute, <=7d one per 5 min,
    <=365d one per UTC day (the last point of each bucket wins); anything
    older is dropped. Age decides first - sparse data is not 'promoted'."""
    buckets: dict[tuple, dict] = {}
    for p in sorted(points, key=lambda q: q["t"]):
        age = now_ts - p["t"]
        if age > HISTORY_MAX_DAYS * 86400:
            continue
        if age <= RETENTION_1MIN_S:
            key = ("m", p["t"] // 60)
        elif age <= RETENTION_5MIN_S:
            key = ("5", p["t"] // 300)
        else:
            key = ("d", p["t"] // 86400)
        buckets[key] = p
    return sorted(buckets.values(), key=lambda q: q["t"])


def _bump(ticker: str):
    _store_version[ticker] = _store_version.get(ticker, 0) + 1


def _guard_series(ticker: str) -> bool:
    """True when `ticker` has a listing. Drops a stored series that was built
    from a different listing symbol/currency, so a changed listing can never
    mix two series (or currencies) in one history."""
    lst = registry.listing(ticker)
    if not lst:
        return False
    ident = {"symbol": lst["symbol"], "currency": lst["currency"]}
    meta = _history_meta.get(ticker)
    if ticker in history_store and meta != ident:
        log.warning("%s: listing changed (%s -> %s); discarding the stored series",
                    ticker, meta, ident)
        history_store.pop(ticker, None)
        _quotes.pop(ticker, None)
        _bump(ticker)
        _schedule_history_write()
    _history_meta[ticker] = ident
    return True


def _persist_points(ticker: str, points: list[dict], src: str = DEFAULT_SRC) -> int:
    """Merge `points` into the listing series of `ticker`; returns how many
    stored points changed. Non-finite / non-positive prices and implausible
    timestamps are dropped. A colliding timestamp is replaced when the
    incoming source priority is >= the stored one."""
    try:
        if not points:
            return 0
        if not is_watched(ticker):
            log.warning("Not persisting %d points for unwatched symbol %s", len(points), ticker)
            return 0
        if not _guard_series(ticker):
            log.debug("No listing for %s; not persisting", ticker)
            return 0
        now_ts = int(time.time())
        existing = history_store.setdefault(ticker, [])
        merged = {p["t"]: p for p in existing}
        changed = 0
        for raw in points:
            c = _finite(raw.get("c")) if isinstance(raw, dict) else None
            try:
                ts = normalize_ts(raw["t"])
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
            if c is None or c <= 0 or not _is_valid_ts(ts) or ts > now_ts + 86400:
                continue
            p_src = raw.get("src") or src
            point = {"t": ts, "c": c}
            if p_src != DEFAULT_SRC:
                point["src"] = p_src
            v = _volume_of(raw)
            if v is not None:
                point["v"] = v
            cur = merged.get(ts)
            if cur is not None:
                if _src_priority(p_src) < _src_priority(cur.get("src")):
                    continue
                if "v" not in point and "v" in cur:
                    point["v"] = cur["v"]   # an unknown volume must not erase a known one
                if cur.get("c") == point["c"] and cur.get("src") == point.get("src") \
                        and cur.get("v") == point.get("v"):
                    continue
            merged[ts] = point
            changed += 1
        if not changed:
            return 0
        history_store[ticker] = _apply_granularity_cutoffs(
            [merged[t] for t in sorted(merged)], now_ts)
        _bump(ticker)
        _schedule_history_write()
        return changed
    except Exception as e:
        log.warning("Failed to persist points for %s: %s", ticker, e)
        return 0


def _replace_series(ticker: str, points: list[dict]) -> bool:
    """Swap the whole series (used by a successful re-seed only)."""
    if not _guard_series(ticker):
        return False
    clean = _apply_granularity_cutoffs(_clean_points(points), int(time.time()))
    if not clean:
        return False
    history_store[ticker] = clean
    _bump(ticker)
    _schedule_history_write()
    return True


def _cleanup_history_store():
    """Periodic age cutoffs for every series."""
    now_ts = int(time.time())
    for ticker in list(history_store):
        before = len(history_store[ticker])
        history_store[ticker] = _apply_granularity_cutoffs(history_store[ticker], now_ts)
        if len(history_store[ticker]) != before:
            _bump(ticker)
            _schedule_history_write()
        if not history_store[ticker]:
            history_store.pop(ticker, None)
    for ticker in list(history_store):
        if not is_watched(ticker):
            history_store.pop(ticker, None)
            _history_meta.pop(ticker, None)
            _bump(ticker)
            _schedule_history_write()


def _assess_history_depth(ticker: str, now_ts: int | None = None) -> dict:
    """What the stored series still lacks, judged by age bucket:
    daily (>7d old): >=30 points; 5-min tail (24h..7d): >=24; latest session: >=16."""
    now_ts = int(time.time()) if now_ts is None else now_ts
    pts = history_store.get(ticker, [])
    daily = sum(1 for p in pts if now_ts - p["t"] > RETENTION_5MIN_S)
    mid = sum(1 for p in pts if RETENTION_1MIN_S < now_ts - p["t"] <= RETENTION_5MIN_S)
    win = registry.session_window(ticker, now_ts)
    if win:
        intraday = sum(1 for p in pts if win[0] <= p["t"] <= win[1])
    else:
        intraday = sum(1 for p in pts if now_ts - p["t"] <= RETENTION_1MIN_S)
    return {
        "needs_daily": daily < 30,
        "needs_hourly": mid < 24,
        "needs_intraday": intraday < 16,
    }


def _bucket_last_close(points: list[dict], bucket_seconds: int) -> list[dict]:
    """Last point per time bucket (gap tolerant: weekends, holidays, mixed granularity)."""
    if not points:
        return points
    buckets: dict[int, dict] = {}
    for p in sorted(points, key=lambda q: q["t"]):
        buckets[p["t"] // bucket_seconds] = p
    return [buckets[b] for b in sorted(buckets)]


def sparkline_for(ticker: str, n: int = 30) -> list:
    """Sparkline from the persisted listing series: the last 24h of bars when
    there are enough, else the most recent stored bars."""
    pts = history_store.get(ticker) or []
    if len(pts) < 2:
        return []
    now = int(time.time())
    intraday = [p for p in pts if now - p["t"] <= 86400]
    src = intraday if len(intraday) >= 8 else pts
    src = sorted(src, key=lambda p: p["t"])
    if len(src) <= n:
        return [p["c"] for p in src]
    step = len(src) / n
    return [src[int(i * step)]["c"] for i in range(n)]


# ------------------------------------------------------------------ negative cache

def _negative_skip(key: str, symbol: str) -> bool:
    ts = _negative_cache.get((key, symbol))
    if ts is None:
        return False
    if time.time() - ts >= _NEGATIVE_TTL:
        _negative_cache.pop((key, symbol), None)
        return False
    return True


def _note_negative(key: str, symbol: str):
    if _negative_skip(key, symbol):
        return
    _negative_cache[(key, symbol)] = time.time()
    log.info("No data for %s from %s - skipping that provider for %ds",
             symbol, key, _NEGATIVE_TTL)


def _clear_negative(key: str, symbol: str):
    _negative_cache.pop((key, symbol), None)


# ------------------------------------------------------------------ yahoo access

def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "too many requests" in msg or "rate limited" in msg or "429" in msg


def _can_fetch(key: str) -> bool:
    return time.time() >= _backoff.get(key, 0.0)


async def _run_yf(fn, *args):
    """Blocking yfinance call in the default executor, serialized by _fetch_lock
    (yfinance is synchronous: on the loop it would stall every other task)."""
    loop = asyncio.get_running_loop()
    async with _fetch_lock:
        return await asyncio.wait_for(loop.run_in_executor(None, fn, *args), YF_TIMEOUT)


async def _yf(key: str, fn, *args):
    """`fn(*args)` through the executor with rate-limit backoff keyed by `key`.
    Returns the result, or None when skipped/failed (failures are logged)."""
    if not _can_fetch(key):
        return None
    try:
        res = await _run_yf(fn, *args)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if _is_rate_limit_error(e):
            consec = _consecutive_429.get(key, 0) + 1
            _consecutive_429[key] = consec
            delay = min(RATE_LIMIT_INITIAL_DELAY * (2 ** min(consec - 1, 4)), MAX_BACKOFF)
            _backoff[key] = time.time() + delay
            log.warning("yfinance rate limited for %s (consecutive=%d), next attempt in %.1fs",
                        key, consec, delay)
        else:
            log.error("yfinance error for %s: %s", key, e)
        return None
    _consecutive_429[key] = 0
    return res


def _yf_bars_blocking(sym: str, interval: str, period: str | None = None,
                      days: int | None = None) -> list[dict]:
    """Executor only. [{t, c, v?}] raw (unadjusted) closes - they must match the
    live quotes, so dividend adjustment is off. NaN/non-positive rows dropped."""
    tk = yf.Ticker(sym)
    if period:
        hist = tk.history(period=period, interval=interval, auto_adjust=False)
    else:
        start = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=int(days or 1))).strftime("%Y-%m-%d")
        hist = tk.history(start=start, interval=interval, auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist.columns:
        return []
    out = []
    for ts, row in hist.iterrows():
        c = _finite(row.get("Close"))
        if c is None or c <= 0:
            continue
        bar = {"t": int(ts.timestamp()), "c": c}
        v = _volume_of(row)
        if v is not None:
            bar["v"] = v
        out.append(bar)
    return out


def _yf_daily_blocking(sym: str, period: str) -> list[tuple[str, float]]:
    """Executor only. [(exchange-local iso date, close)] of daily bars."""
    tk = yf.Ticker(sym)
    hist = tk.history(period=period, interval="1d", auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist.columns:
        return []
    out = []
    for ts, row in hist.iterrows():
        c = _finite(row.get("Close"))
        if c is not None and c > 0:
            out.append((ts.date().isoformat(), c))
    return out


def _yf_info_blocking(sym: str) -> dict:
    return yf.Ticker(sym).info or {}


_BAR_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def _align_bars(ticker: str, interval: str, bars: list[dict]) -> list[dict]:
    """Stamp seeded/backfilled bars with their CLOSE slot.

    Provider bars are stamped with their start but carry the close, while the
    live 1-min poll stamps (nearly) the price at that minute. Mixing the two
    unaligned makes a coarse bar's close show up minutes early next to 1-min
    bars - a zig-zag in the 1D chart. A coarse bar is moved to its last
    1-minute slot; a daily bar to the venue's session close. Bars that would
    land in the future (today's still-open daily bar) are dropped.
    """
    now = int(time.time())
    out = []
    if interval == "1d":
        v = registry.venue(ticker)
        if not v:
            return []
        tz = ZoneInfo(v["tz"])
        ch, cm = v["close"]
        for b in bars:
            day = datetime.datetime.fromtimestamp(b["t"], tz).date()
            t = int(datetime.datetime(day.year, day.month, day.day, ch, cm, tzinfo=tz).timestamp())
            if t <= now:
                out.append({**b, "t": t})
        return out
    shift = max(0, _BAR_SECONDS.get(interval, 60) - 60)
    for b in bars:
        t = b["t"] + shift
        if t <= now:
            out.append({**b, "t": t})
    return out


# ------------------------------------------------------------------ listing quote

def _venue_day(ticker: str, ts: int) -> str:
    v = registry.venue(ticker)
    tz = ZoneInfo(v["tz"]) if v else datetime.timezone.utc
    return datetime.datetime.fromtimestamp(ts, tz).date().isoformat()


async def _prev_close(ticker: str, sym: str, as_of: int) -> float | None:
    """Close of the session before the one `as_of` belongs to (cached 1h)."""
    day = _venue_day(ticker, as_of)
    ent = _prev_close_cache.get(ticker)
    if ent:
        val, ent_day, ts = ent
        ttl = 3600 if val is not None else 300
        if ent_day == day and time.time() - ts < ttl:
            return val
    rows = await _yf(sym, _yf_daily_blocking, sym, "5d")
    if rows is None:
        return ent[0] if ent and ent[1] == day else None
    earlier = [c for d, c in rows if d < day]
    val = earlier[-1] if earlier else None
    _prev_close_cache[ticker] = (val, day, time.time())
    _prune_cache(_prev_close_cache)
    return val


async def _refresh_listing_quote(ticker: str) -> dict | None:
    lst = registry.listing(ticker)
    if not lst:
        return None            # unsupported: nothing to ask, nothing to cache
    sym = lst["symbol"]
    if _negative_skip("yahoo", sym):
        return _quotes.get(ticker)
    bars = None
    for period, interval in (("1d", "1m"), ("5d", "15m")):
        bars = await _yf(sym, _yf_bars_blocking, sym, interval, period)
        if bars:
            break
        if bars is None:
            return _quotes.get(ticker)   # failure/backoff: keep what we have
    if not bars:
        _note_negative("yahoo", sym)
        return _quotes.get(ticker)
    _clear_negative("yahoo", sym)
    _guard_series(ticker)
    _persist_points(ticker, bars, SRC_YF)
    last = bars[-1]
    prev = await _prev_close(ticker, sym, last["t"])
    q = {
        "price": last["c"],
        "asOf": int(last["t"]),
        "prevClose": prev,
        "currency": lst["currency"],
        "symbol": sym,
        "fetchedAt": time.time(),
    }
    _quotes[ticker] = q
    return q


async def refresh_listing(ticker: str) -> dict | None:
    """Fetch + persist the listing's latest bars (one in-flight fetch per id)."""
    ticker = (ticker or "").strip().upper()
    task = _quote_inflight.get(ticker)
    if task is None or task.done():
        task = asyncio.ensure_future(_refresh_listing_quote(ticker))
        _quote_inflight[ticker] = task
        task.add_done_callback(lambda t, k=ticker:
                               _quote_inflight.pop(k, None) if _quote_inflight.get(k) is t else None)
    return await asyncio.shield(task)


def _quote_view(ticker: str, q: dict, refreshed_ok: bool) -> dict:
    lst = registry.listing(ticker) or {}
    now = time.time()
    is_open = registry.market_open(ticker, now)
    age = now - q["asOf"]
    stale = age > (STALE_OPEN_S if is_open else STALE_CLOSED_S)
    if not refreshed_ok and now - q["fetchedAt"] > 600:
        stale = True
    price, prev = q["price"], q.get("prevClose")
    change = pct = None
    if prev:
        change = price - prev
        pct = change / prev * 100
    return {
        "id": ticker,
        "price": price,
        "prevClose": prev,
        "change": change,
        "changePct": pct,
        "currency": q["currency"],
        "asOf": q["asOf"],
        "source": "yahoo",
        "stale": bool(stale),
        "venue": lst.get("venue"),
        "marketOpen": bool(is_open),
    }


async def listing_quote(ticker: str) -> dict | None:
    """Latest quote of the entry's listing: {id, price, prevClose, change,
    changePct, currency, asOf, source, stale, venue, marketOpen}.
    None when the entry has no listing or yahoo never answered."""
    ticker = (ticker or "").strip().upper()
    if not registry.listing(ticker):
        return None
    q = _quotes.get(ticker)
    ok = True
    ttl = QUOTE_TTL_OPEN if registry.market_open(ticker) else QUOTE_TTL_CLOSED
    if q is None or time.time() - q["fetchedAt"] > ttl:
        try:
            fresh = await refresh_listing(ticker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("listing quote refresh failed for %s: %s", ticker, e)
            fresh = None
        ok = fresh is not None and fresh is _quotes.get(ticker) and \
            time.time() - fresh["fetchedAt"] < 60
        q = _quotes.get(ticker)
    if q is None:
        return None
    return _quote_view(ticker, q, ok)


def _cache_get(ticker: str) -> dict | None:
    """Cache-only read of the listing quote (no provider call)."""
    q = _quotes.get((ticker or "").strip().upper())
    if not q:
        return None
    price, prev = q["price"], q.get("prevClose")
    return {
        "ticker": ticker,
        "price": price,
        "previousClose": prev,
        "change24h": ((price - prev) / prev * 100) if prev else None,
        "currency": q["currency"],
        "asOf": q["asOf"],
        "ts": q["asOf"],
        "quoteProvider": "yahoo",
    }


async def listing_poll_loop():
    """Keep every listing's quote + bars fresh: ~1/min while its venue is open,
    every 15 min otherwise. Re-reads the registry each pass (no restart needed)."""
    while True:
        try:
            for ticker in [e["id"] for e in registry.entries() if e.get("listing")]:
                q = _quotes.get(ticker)
                interval = QUOTE_TTL_OPEN if registry.market_open(ticker) else QUOTE_TTL_CLOSED
                if q is not None and time.time() - q["fetchedAt"] < interval:
                    continue
                try:
                    await refresh_listing(ticker)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("listing poll failed for %s: %s", ticker, e)
                await asyncio.sleep(1)
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("listing poll pass failed")
            await asyncio.sleep(30)


def us_live_view(ticker: str) -> dict | None:
    """Separately labelled US live reference tick (USD, NASDAQ feeds) or None.
    Only symbols the registry maps to a streaming provider have one; it is
    never part of the listing series."""
    if not (registry.provider_symbol(ticker, "twelvedata")
            or registry.provider_symbol(ticker, "finnhub")):
        return None
    from app.api import live_ws
    lp = live_ws.live_prices.get(ticker)
    if not lp:
        return None
    price, ts = _finite(lp.get("price")), lp.get("ts")
    if price is None or not isinstance(ts, (int, float)) or time.time() - ts > US_LIVE_MAX_AGE:
        return None
    return {"price": price, "currency": "USD", "asOf": int(ts),
            "source": lp.get("source")}


# ------------------------------------------------------------------ price items

async def _price_item(ticker: str) -> dict:
    empty = {"ticker": ticker, "price": None, "previousClose": None,
             "change24h": None, "sparkline": []}
    if not registry.listing(ticker):
        return {**empty, "status": "unsupported"}
    try:
        q = await listing_quote(ticker)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("price item failed for %s: %s", ticker, e)
        q = None
    if q is None:
        return {**empty, "sparkline": sparkline_for(ticker), "status": "error"}
    item = {
        "ticker": ticker,
        "price": q["price"],
        "previousClose": q["prevClose"],
        "change24h": q["changePct"],
        "change": q["change"],
        "priceCurrency": q["currency"],
        "asOf": q["asOf"],
        "stale": q["stale"],
        "marketOpen": q["marketOpen"],
        "venue": q["venue"],
        "quoteProvider": "yahoo",
        "sparkline": sparkline_for(ticker),
    }
    if q["stale"]:
        item["status"] = "stale"
    us = us_live_view(ticker)
    if us:
        item["usLive"] = us
    return item


async def fetch_prices(tickers: list[str]) -> list[dict]:
    """One item per ticker, from the listing series (cache-backed)."""
    return list(await asyncio.gather(*(_price_item(str(t).strip().upper()) for t in tickers)))


# ------------------------------------------------------------------ history views

def _history_ttl_for(ticker: str, range_key: str) -> int:
    ttl = HISTORY_CACHE_TTL_BY_RANGE.get(range_key, HISTORY_CACHE_TTL_DEFAULT)
    return ttl if registry.market_open(ticker) else ttl * 2


def _history_cache_get(key: str):
    entry = _history_cache.get(key)
    if not entry:
        return None
    data, ts, version = entry
    ticker, _, range_key = key.partition(":")
    if version != _store_version.get(ticker, 0):
        _history_cache.pop(key, None)
        return None
    if time.time() - ts >= _history_ttl_for(ticker, range_key):
        _history_cache.pop(key, None)
        return None
    return data


def _history_cache_set(key: str, data: dict):
    ticker = key.partition(":")[0]
    _history_cache.pop(key, None)
    _history_cache[key] = (data, time.time(), _store_version.get(ticker, 0))
    _prune_cache(_history_cache)


def _series_for_range(ticker: str, range_key: str, now: int) -> list[dict]:
    pts = sorted(history_store.get(ticker) or [], key=lambda p: p["t"])
    if range_key == "1D":
        win = registry.session_window(ticker, now)
        if not win:
            return []
        return [p for p in pts if win[0] <= p["t"] <= win[1]]
    days = {"1W": 7, "1M": 30, "3M": 90, "6M": 182, "1Y": 365}[range_key]
    pts = [p for p in pts if p["t"] >= now - days * 86400]
    return _bucket_last_close(pts, 3600 if range_key == "1W" else 86400)


async def ensure_history(ticker: str, force: bool = False) -> bool:
    """Backfill a thin series from yahoo (daily 1y, 5-min 7d, 1-min 2d).
    Rate-limited per symbol; one in-flight run per id. True when data exists."""
    ticker = (ticker or "").strip().upper()
    lst = registry.listing(ticker)
    if not lst or not _guard_series(ticker):
        return False
    depth = _assess_history_depth(ticker)
    if not force and not any(depth.values()):
        return True
    if not force and time.time() - _seed_last.get(ticker, 0.0) < SEED_COOLDOWN:
        return bool(history_store.get(ticker))
    task = _seed_inflight.get(ticker)
    if task is None or task.done():
        _seed_last[ticker] = time.time()
        task = asyncio.ensure_future(_backfill(ticker, lst["symbol"], depth, force))
        _seed_inflight[ticker] = task
        task.add_done_callback(lambda t, k=ticker:
                               _seed_inflight.pop(k, None) if _seed_inflight.get(k) is t else None)
    await asyncio.shield(task)
    return bool(history_store.get(ticker))


async def _backfill(ticker: str, sym: str, depth: dict, force: bool):
    if _negative_skip("yahoo", sym):
        return
    plan = []
    if force or depth["needs_daily"]:
        plan.append(("1d", {"period": "1y"}))
    if force or depth["needs_hourly"]:
        plan.append(("5m", {"days": 7}))
    if force or depth["needs_intraday"]:
        plan.append(("1m", {"days": 2}))
    for interval, kw in plan:
        bars = await _yf(sym, _yf_bars_blocking, sym, interval,
                         kw.get("period"), kw.get("days"))
        if bars:
            _persist_points(ticker, _align_bars(ticker, interval, bars), SRC_SEED)
        elif bars == []:
            log.info("backfill %s %s: no bars from yahoo", sym, interval)


async def _build_history(ticker: str, range_key: str) -> dict | None:
    now = int(time.time())
    if range_key == "1D" and registry.market_open(ticker):
        await listing_quote(ticker)       # fresh tail bars before we slice
    points = _series_for_range(ticker, range_key, now)
    if len(points) < (16 if range_key == "1D" else 8):
        if await ensure_history(ticker):
            points = _series_for_range(ticker, range_key, now)
    if not points:
        return None
    return {"ticker": ticker, "range": range_key,
            "currency": registry.listing_currency(ticker),
            "source": "yahoo", "data": points}


async def fetch_price_history(ticker: str, range_key: str = "1W") -> dict | None:
    """Chart series of the listing: {"ticker","range","currency","source","data":[{t,c}]}.
    None for an unknown/unsupported symbol or when no data exists."""
    ticker = (ticker or "").strip().upper()
    range_key = (range_key or "").strip().upper()
    if range_key not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    if not registry.listing(ticker):
        return None
    key = f"{ticker}:{range_key}"
    cached = _history_cache_get(key)
    if cached:
        return cached
    task = _history_inflight.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(_build_history(ticker, range_key))
        _history_inflight[key] = task
        task.add_done_callback(lambda t, k=key:
                               _history_inflight.pop(k, None) if _history_inflight.get(k) is t else None)
    result = await asyncio.shield(task)
    if result:
        _history_cache_set(key, result)
    return result


async def listing_bars(ticker: str, range_key: str, currency: str | None = None) -> list[dict]:
    """[{t, c}] ascending in the listing's own currency (EUR holdings, USD for
    the benchmark); 1M+ ranges are one bar per UTC day. With `currency` the
    bars are converted ONCE per bar with the rate valid for that bar's day
    (forex.daily_rates, last reference day carried over weekends); bars
    without a rate are dropped. Returns [] when unsupported/unavailable."""
    try:
        res = await fetch_price_history(ticker, range_key)
    except ValueError:
        return []
    if not res:
        return []
    bars = [{"t": p["t"], "c": p["c"]} for p in res["data"]]
    target = (currency or "").upper()
    own = (res.get("currency") or "").upper()
    if not target or target == own:
        return bars
    if {target, own} != {"EUR", "USD"}:
        return []
    days = max(1, (int(time.time()) - bars[0]["t"]) // 86400 + 8) if bars else 1
    rates = await forex.daily_rates(days)
    out = []
    for b in bars:
        day = datetime.datetime.fromtimestamp(b["t"], datetime.timezone.utc).date().isoformat()
        rate = forex.rate_on(rates, day)
        if rate is None:
            continue
        out.append({"t": b["t"], "c": b["c"] * rate if target == "EUR" else b["c"] / rate})
    return out


async def session_open_price(ticker: str) -> tuple[float, int] | None:
    """(price, ts) of the first stored listing bar of the latest started session."""
    win = registry.session_window(ticker)
    if not win:
        return None
    for p in sorted(history_store.get(ticker) or [], key=lambda q: q["t"]):
        if win[0] <= p["t"] <= win[1]:
            return p["c"], p["t"]
    return None


async def prewarm_charts():
    """One pass: warm the 1D chart cache for every listing."""
    ids = [e["id"] for e in registry.entries() if e.get("listing")]
    await asyncio.gather(*(fetch_price_history(t, "1D") for t in ids), return_exceptions=True)


# ------------------------------------------------------------------ seed / refresh

async def seed_listing_history(ticker: str, interval: str, outputsize: int) -> int:
    """Merge up to `outputsize` bars of `interval` from yahoo into the listing
    series. Raises ValueError on bad input. Returns the number of bars stored."""
    ticker = (ticker or "").strip().upper()
    if interval not in SEED_INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(SEED_INTERVALS)}")
    if not isinstance(outputsize, int) or not 1 <= outputsize <= MAX_SEED_OUTPUTSIZE:
        raise ValueError(f"outputsize must be between 1 and {MAX_SEED_OUTPUTSIZE}")
    if not is_watched(ticker):
        raise ValueError(f"{ticker} is not on the watchlist")
    lst = registry.listing(ticker)
    if not lst:
        raise ValueError(f"{ticker} has no yahoo listing (unsupported)")
    yf_interval, max_days, per_day = SEED_INTERVALS[interval]
    days = min(max_days, math.ceil(outputsize / per_day * 1.5) + 2)
    sym = lst["symbol"]
    bars = await _yf(sym, _yf_bars_blocking, sym, yf_interval, None, days)
    if not bars:
        raise ValueError(f"no data from yahoo for {sym}")
    bars = _align_bars(ticker, yf_interval, bars)[-outputsize:]
    if not bars:
        raise ValueError(f"no usable bars from yahoo for {sym}")
    _guard_series(ticker)
    return _persist_points(ticker, bars, SRC_SEED)


async def refresh_history(ticker: str) -> bool:
    """Re-seed one series from scratch. The stored series is swapped ONLY when
    the new daily fetch succeeded; a failed refresh leaves it untouched."""
    ticker = (ticker or "").strip().upper()
    lst = registry.listing(ticker)
    if not lst:
        return False
    sym = lst["symbol"]
    daily = await _yf(sym, _yf_bars_blocking, sym, "1d", "1y")
    if not daily:
        log.warning("refresh_history %s: daily fetch failed, keeping the stored series", ticker)
        return False
    pts = [{**b, "src": SRC_SEED} for b in _align_bars(ticker, "1d", daily)]
    for interval, days in (("5m", 7), ("1m", 2)):
        extra = await _yf(sym, _yf_bars_blocking, sym, interval, None, days)
        pts += [{**b, "src": SRC_SEED} for b in _align_bars(ticker, interval, extra or [])]
    _valuation_cache.pop(ticker, None)
    _schedule_valuation_write()
    _quotes.pop(ticker, None)
    _prev_close_cache.pop(ticker, None)
    return _replace_series(ticker, pts)


async def seed_missing_history(tickers: list[str] | None = None):
    """Backfill every thin series (startup + 12-hourly health check)."""
    for ticker in (tickers if tickers is not None else registry.ids()):
        try:
            await ensure_history(ticker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("seed: %s failed: %s", ticker, e)
        await asyncio.sleep(1.5)


# ------------------------------------------------------------------ valuation

_VAL_KEYS = ("pe", "forwardPE", "dividendYield", "roe", "sector")


async def _fetch_valuation_from_yf(symbol: str) -> dict | None:
    """yfinance .info valuation. Only for equities: index/ETF entries have no
    issuer fundamentals and are skipped WITHOUT a request or a negative entry.
    Own negative-cache key ('yahoo:valuation'), never the quote/history one."""
    if registry.kind(symbol) != "equity":
        return None
    ysym = registry.provider_symbol(symbol, "yahoo")
    if not ysym:
        return None
    if _negative_skip("yahoo:valuation", ysym):
        return None
    info = await _yf(f"{ysym}:valuation", _yf_info_blocking, ysym)
    if info is None:
        return None

    def _f(k):
        return _finite(info.get(k))

    vals = {
        "pe": _f("trailingPE"),
        "forwardPE": _f("forwardPE"),
        "dividendYield": _f("dividendYield"),
        "roe": _f("returnOnEquity"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "shortRatio": _f("shortRatio"),
        "analystTarget": _f("targetMeanPrice"),
        "recommendation": info.get("recommendationKey"),
    }
    if not any(vals[k] is not None for k in _VAL_KEYS):
        _note_negative("yahoo:valuation", ysym)
        return None
    _clear_negative("yahoo:valuation", ysym)
    return vals


async def _provider_call_result(key: str, symbol: str, coro, can_fetch):
    """Await `coro`, then clear/set the (key, symbol) negative cache."""
    result = await coro
    if result:
        _clear_negative(key, symbol)
        return result
    if can_fetch():
        _note_negative(key, symbol)
    return None


async def _provider_fundamentals(ticker: str) -> dict | None:
    sym = registry.provider_symbol(ticker, "twelvedata")
    if not sym or not twelvedata._enabled() or _negative_skip("twelvedata:fundamentals", sym):
        return None
    return await _provider_call_result(
        "twelvedata:fundamentals", sym, twelvedata.get_fundamentals(sym), twelvedata._can_fetch)


async def _provider_profile(ticker: str) -> dict | None:
    sym = registry.provider_symbol(ticker, "finnhub")
    if not sym or not finnhub._enabled() or _negative_skip("finnhub:profile", sym):
        return None
    return await _provider_call_result(
        "finnhub:profile", sym, finnhub.get_profile(sym), finnhub._can_fetch)


async def _gather_valuation(symbol: str) -> dict:
    """yahoo first, Twelve Data fundamentals fill gaps, Finnhub profile fills sector."""
    vals: dict = {}
    yf_vals = await _fetch_valuation_from_yf(symbol)
    for k, v in (yf_vals or {}).items():
        if v is not None and vals.get(k) is None:
            vals[k] = v
    try:
        td = await _provider_fundamentals(symbol)
        for k, v in (td or {}).items():
            if v is not None and vals.get(k) is None:
                vals[k] = v
    except Exception as e:
        log.warning("Twelve Data fundamentals failed for %s: %s", symbol, e)
    try:
        p = await _provider_profile(symbol)
        if p:
            if p.get("sector") and not vals.get("sector"):
                vals["sector"] = p["sector"]
            if p.get("industry") and not vals.get("industry"):
                vals["industry"] = p["industry"]
    except Exception as e:
        log.warning("Finnhub profile failed for %s: %s", symbol, e)
    return vals


async def refresh_valuation():
    """Refresh valuation/context for every equity whose cache is missing/stale."""
    for symbol in registry.ids():
        if registry.kind(symbol) != "equity":
            continue
        now = time.time()
        existing = _valuation_cache.get(symbol) or {}
        last = existing.get("lastUpdated")
        stale = last is None or (now - last) > VALUATION_YF_TTL
        empty = all(existing.get(k) is None for k in _VAL_KEYS)
        if existing and not stale and not empty:
            continue
        vals = await _gather_valuation(symbol)
        if vals and any(vals.values()):
            vals["lastUpdated"] = int(now)
            _valuation_cache[symbol] = vals
            _prune_cache(_valuation_cache)
            _schedule_valuation_write()
            log.info("Valuation updated for %s", symbol)
        elif not existing:
            log.info("No valuation data available for %s (first attempt)", symbol)
        await asyncio.sleep(1.0)


def _start_valuation_task(symbol: str):
    """Fire-and-forget one-shot valuation fetch (strong task ref, logged errors)."""
    sym = (symbol or "").strip().upper()
    if not sym or sym in _valuation_inflight or sym in _valuation_cache:
        return None
    _valuation_inflight.add(sym)
    task = asyncio.ensure_future(_ensure_valuation_once(sym))
    _valuation_tasks.add(task)

    def _finished(t):
        _valuation_tasks.discard(t)
        _valuation_inflight.discard(sym)
        if not t.cancelled() and t.exception() is not None:
            log.warning("_ensure_valuation_once failed for %s: %s", sym, t.exception())

    task.add_done_callback(_finished)
    return task


async def _ensure_valuation_once(symbol: str):
    if symbol in _valuation_cache or registry.kind(symbol) != "equity":
        return
    vals = await _gather_valuation(symbol)
    if vals and any(vals.values()):
        vals["lastUpdated"] = int(time.time())
        _valuation_cache[symbol] = vals
        _prune_cache(_valuation_cache)
        _schedule_valuation_write()


def _merge_valuation_for_detail(data: dict) -> dict:
    ticker = (data.get("ticker") or "").upper()
    v = (data.get("valuation") or {}).copy()
    c = (data.get("context") or {}).copy()
    cached = _valuation_cache.get(ticker)
    if not cached:
        data["valuation"], data["context"] = v, c
        return data
    for k in ("pe", "forwardPE", "dividendYield", "roe"):
        if v.get(k) is None and k in cached:
            v[k] = cached[k]
    for k in ("sector", "industry", "shortRatio", "analystTarget", "recommendation"):
        if c.get(k) is None and k in cached:
            c[k] = cached[k]
    if v.get("sector") is None and cached.get("sector"):
        c["sector"] = c.get("sector") or cached["sector"]
    data["valuation"], data["context"] = v, c
    return data


def _build_detail_from_data(data: dict) -> dict:
    v = data.get("valuation") or {}
    c = data.get("context") or {}
    price = data.get("price")
    pe = v.get("pe")
    div_yield = v.get("dividendYield")
    target_mean = c.get("analystTarget")

    signals = []
    if pe is not None:
        if pe > 60:
            signals.append({"label": "P/E very high", "tone": "bearish"})
        elif pe > 30:
            signals.append({"label": "P/E elevated", "tone": "cautious"})
        else:
            signals.append({"label": "P/E reasonable", "tone": "bullish"})
    if div_yield is not None:
        dy = float(div_yield)
        if dy < 0.5:   # fraction from an older cache entry (0.04 == 4%)
            dy *= 100
        if dy > 20:
            log.debug("Ignoring unreliable dividendYield %.2f for %s", dy, data.get("ticker"))
        elif dy > 4.0:
            signals.append({"label": "High dividend yield", "tone": "bullish"})
        elif dy > 2.0:
            signals.append({"label": "Decent dividend", "tone": "neutral"})
    if target_mean and price:
        upside = (target_mean - price) / price * 100
        if upside > 10:
            signals.append({"label": f"Analysts target +{upside:.0f}% upside", "tone": "bullish"})
        elif upside < -10:
            signals.append({"label": f"Analysts target {upside:.0f}% downside", "tone": "bearish"})
    return {
        "ticker": data["ticker"],
        "price": data["price"],
        "priceCurrency": data.get("priceCurrency"),
        "change24h": data["change24h"],
        "sparkline": data.get("sparkline", []),
        "valuation": v,
        "context": c,
        "signals": signals,
    }


async def fetch_ticker_info(ticker: str) -> dict | None:
    """Detail payload: listing quote + cached valuation/context + signals."""
    ticker = (ticker or "").strip().upper()
    q = await listing_quote(ticker)
    if not q:
        return None
    data = {
        "ticker": ticker,
        "price": q["price"],
        "previousClose": q["prevClose"],
        "change24h": q["changePct"],
        "priceCurrency": q["currency"],
        "sparkline": sparkline_for(ticker),
        "valuation": {"pe": None, "forwardPE": None, "dividendYield": None, "roe": None},
        "context": {"sector": None, "industry": None, "shortRatio": None,
                    "analystTarget": None, "recommendation": None},
    }
    data = _merge_valuation_for_detail(data)
    v = data.get("valuation") or {}
    if registry.kind(ticker) == "equity" and all(v.get(k) is None for k in _VAL_KEYS):
        _start_valuation_task(ticker)
    return _build_detail_from_data(data)


# ------------------------------------------------------------------ test support

def _reset_state():
    """Clear every in-memory cache/store (test fixtures; never called in prod)."""
    global history_store, _history_meta, _load_failed
    history_store = {}
    _history_meta = {}
    _load_failed = False
    for d in (_store_version, _history_cache, _quotes, _prev_close_cache, _seed_last,
              _backoff, _consecutive_429, _negative_cache, _valuation_cache):
        d.clear()
    for w in (_history_writer, _valuation_writer):
        if w._handle is not None:
            w._handle.cancel()
        w._handle = None
        w._task = None
        w.dirty = False
        w.last = 0.0
