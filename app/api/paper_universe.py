"""Paper-trading candidate universe ("open universe", docs/plans/forecaster.md).

A registry SEPARATE from the real watchlist: candidates are chosen by an index
rule (Euro Stoxx 50 + AEX + DAX 40 + the largest US companies with an EUR Xetra
listing) and are never added to ``config/config.json``, so the real dashboard,
its alerts and its digest are unchanged.

* ``data/paper_universe.json`` (``jsonstore``) is the live copy; when it is
  missing, empty or corrupt the tracked seed ``ops/paper_universe_seed.json`` is
  used. Both share one shape: ``{"asOf", "source", "entries": [row, ...]}`` with
  rows ``{id, name, isin, yahoo, venue, kind, sector, role, t212}``.
* ``daily_closes`` / ``listing_quote`` fetch by the row's yahoo symbol through
  the ``prices`` yahoo internals (rate-limit backoff, serialised executor), so
  the id never has to be on the real watchlist and ``prices`` state (listing
  history store, quote cache) is never touched. Both never raise.
* ``map_t212`` fills ``t212`` from a broker instrument list (ISIN + EUR); the
  broker itself is never called here.
"""
import asyncio
import copy
import datetime
import logging
import os
import pathlib
import re
import time
from zoneinfo import ZoneInfo

from app.api import jsonstore, prices

log = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
STORE_FILE = DATA_DIR / "paper_universe.json"
SEED_FILE = pathlib.Path(__file__).resolve().parents[2] / "ops" / "paper_universe_seed.json"

KINDS = ("equity", "etf")
ROLES = ("candidate", "benchmark")
# MIC -> exchange-local timezone (the date a bar belongs to, for prevClose)
VENUE_TZ = {
    "XETR": "Europe/Berlin",
    "XAMS": "Europe/Amsterdam",
    "XPAR": "Europe/Paris",
    "XBRU": "Europe/Brussels",
    "XMIL": "Europe/Rome",
}
# Trading 212 appends one lowercase exchange letter to the symbol
# (``SAPd_EQ`` Xetra, ``ASMLa_EQ`` Amsterdam). A HINT only: ranking, never a filter.
T212_SUFFIX_VENUE = {"d": "XETR", "a": "XAMS", "p": "XPAR", "b": "XBRU", "m": "XMIL"}
_T212_SUFFIX_RE = re.compile(r"^.+?([a-z])_EQ$")

CLOSES_TTL_S = prices.DAILY_CLOSES_TTL_S      # 6 h, same as prices.daily_closes
QUOTE_TTL_S = 900                             # 15 min: the PM prices orders from this
QUOTE_STALE_S = prices.STALE_CLOSED_S         # newest bar older than this: stale
MAX_CACHE_ENTRIES = 1000

_closes_cache: dict[tuple[str, str], tuple] = {}   # (id, period) -> (rows, ts)
_adj_cache: dict[tuple[str, str], tuple] = {}      # (id, period) -> (rows, ts)
_quote_cache: dict[str, tuple] = {}                # id -> (quote, ts)
_load_cache: dict = {"sig": None, "doc": None}


def _now() -> float:
    return time.time()


# ------------------------------------------------------------------ loading

def _clean_row(raw) -> dict | None:
    """A validated copy of one row, or None when it cannot be used."""
    if not isinstance(raw, dict):
        return None
    rid = raw.get("id")
    yahoo = raw.get("yahoo")
    if not isinstance(rid, str) or not rid.strip() or not isinstance(yahoo, str) or not yahoo.strip():
        return None
    venue = raw.get("venue")
    kind = raw.get("kind")
    role = raw.get("role") or "candidate"
    if venue not in VENUE_TZ or kind not in KINDS or role not in ROLES:
        return None
    t212 = raw.get("t212")
    if not isinstance(t212, str) or not t212.strip():
        t212 = None
    row = copy.deepcopy(raw)          # unknown keys survive a map_t212 rewrite
    row.update({
        "id": rid.strip().upper(),
        "yahoo": yahoo.strip(),
        "venue": venue,
        "kind": kind,
        "role": role,
        "isin": (raw.get("isin") or "").strip().upper() if isinstance(raw.get("isin"), str) else None,
        "t212": t212.strip() if t212 else None,
    })
    return row


def _clean_doc(raw) -> dict | None:
    """Validated document (duplicate ids: first wins); None when no row survives."""
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return None
    rows, seen = [], set()
    for item in raw["entries"]:
        row = _clean_row(item)
        if row is None:
            log.warning("paper_universe: dropped unusable row %r", (item or {}).get("id")
                        if isinstance(item, dict) else item)
            continue
        if row["id"] in seen:
            log.warning("paper_universe: duplicate id %s ignored", row["id"])
            continue
        seen.add(row["id"])
        rows.append(row)
    if not rows:
        return None
    doc = {k: v for k, v in raw.items() if k != "entries"}
    doc["entries"] = rows
    return doc


def _signature(path: pathlib.Path):
    try:
        st = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path), st.st_mtime_ns, st.st_size)


def _load_doc() -> dict:
    """The live document: the store's rows, plus every seed row whose id the
    store lacks (store wins per existing id, so a mapped ``t212`` survives);
    the seed alone when the store is missing/unusable. Re-read when either
    file changed; callers get the cached object and must not mutate it."""
    sig = (_signature(STORE_FILE), _signature(SEED_FILE))
    if _load_cache["sig"] == sig and _load_cache["doc"] is not None:
        return _load_cache["doc"]
    doc = _clean_doc(jsonstore.load(STORE_FILE))
    seed = _clean_doc(jsonstore.load(SEED_FILE))
    if doc is None:
        doc = seed
        if doc is None:
            log.warning("paper_universe: neither %s nor %s is usable", STORE_FILE.name, SEED_FILE.name)
            doc = {"entries": []}
    elif seed is not None:
        have = {r["id"] for r in doc["entries"]}
        extra = [r for r in seed["entries"] if r["id"] not in have]
        if extra:
            doc = {**doc, "entries": [*doc["entries"], *extra]}
    _load_cache["sig"] = sig
    _load_cache["doc"] = doc
    return doc


def entries() -> list[dict]:
    """Every row (candidates and benchmarks) in file order; copies."""
    return copy.deepcopy(_load_doc()["entries"])


def get(pid: str) -> dict | None:
    """The row of `pid` (case-insensitive) or None; a copy."""
    key = (pid or "").strip().upper() if isinstance(pid, str) else ""
    for row in _load_doc()["entries"]:
        if row["id"] == key:
            return copy.deepcopy(row)
    return None


def ids(role: str | None = None) -> list[str]:
    """Ids in file order, optionally only `role` ('candidate' | 'benchmark').
    An unknown role matches nothing."""
    return [r["id"] for r in _load_doc()["entries"] if role is None or r["role"] == role]


def t212_ticker(pid: str) -> str | None:
    """The mapped Trading 212 ticker, None when unmapped or unknown."""
    row = get(pid)
    return row["t212"] if row else None


# ------------------------------------------------------------------ prices

def _symbol(pid: str) -> str | None:
    row = get(pid)
    return row["yahoo"] if row else None


async def daily_closes(pid: str, period: str = "2y") -> list[tuple[str, float]]:
    """[(exchange-local ISO date, raw unadjusted close)] oldest first, fetched by
    the entry's yahoo symbol (the id need not be watched). Cached 6 h per
    (id, period); a failed fetch returns the last cached value or []. Never
    raises; unknown ids and periods -> []."""
    key = ((pid or "").strip().upper() if isinstance(pid, str) else "", period)
    if period not in prices.DAILY_CLOSES_PERIODS:
        return []
    sym = _symbol(key[0])
    if not sym:
        return []
    cached = _closes_cache.get(key)
    if cached and _now() - cached[1] < CLOSES_TTL_S:
        return list(cached[0])
    try:
        rows = await prices._yf(sym, prices._yf_daily_blocking, sym, period)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("paper_universe daily_closes %s failed: %s", key[0], e)
        rows = None
    if rows:
        _closes_cache[key] = (list(rows), _now())
        prices._prune_cache(_closes_cache, MAX_CACHE_ENTRIES)
        return list(rows)
    return list(cached[0]) if cached else []


async def adj_closes(pid: str, period: str = "2y") -> list[tuple[str, float]]:
    """[(exchange-local ISO date, Adj Close)] oldest first: Yahoo's split- and dividend-adjusted close of
    the same daily rows `daily_closes` returns raw. For returns, momentum and ranges ONLY (price and
    valuation stay raw). Ratios between two dates are total-return ratios up to the latest of them, so a
    point-in-time cut of this series is leak-free; the level is not comparable with the raw close. Rows
    whose adjusted value is missing are dropped (never filled from the raw close). Cached 6 h per
    (id, period); a failed fetch returns the last cached value or []. Never raises; unknown ids and
    periods -> []."""
    key = ((pid or "").strip().upper() if isinstance(pid, str) else "", period)
    if period not in prices.DAILY_CLOSES_PERIODS:
        return []
    sym = _symbol(key[0])
    if not sym:
        return []
    cached = _adj_cache.get(key)
    if cached and _now() - cached[1] < CLOSES_TTL_S:
        return list(cached[0])
    try:
        raw = await prices._yf(sym, prices._yf_daily_adj_blocking, sym, period)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("paper_universe adj_closes %s failed: %s", key[0], e)
        raw = None
    rows = [(d, a) for d, _c, a in raw or [] if a is not None]
    if rows:
        _adj_cache[key] = (list(rows), _now())
        prices._prune_cache(_adj_cache, MAX_CACHE_ENTRIES)
        return list(rows)
    return list(cached[0]) if cached else []


def _bar_date(ts: int, venue: str) -> str:
    return datetime.datetime.fromtimestamp(ts, ZoneInfo(VENUE_TZ[venue])).date().isoformat()


async def _prev_close(sym: str, venue: str, last_ts: int) -> float | None:
    """Close of the newest daily row dated BEFORE the last bar's local date."""
    rows = await prices._yf(sym, prices._yf_daily_blocking, sym, "5d")
    if not rows:
        return None
    today = _bar_date(last_ts, venue)
    older = [c for d, c in rows if d < today]
    return older[-1] if older else None


async def _fetch_quote(pid: str, row: dict) -> dict | None:
    sym, venue = row["yahoo"], row["venue"]
    bars = None
    for period, interval in (("1d", "1m"), ("5d", "15m")):
        bars = await prices._yf(sym, prices._yf_bars_blocking, sym, interval, period)
        if bars:
            break
        if bars is None:
            return None            # failure / backoff: keep what we have
    if not bars:
        return None
    last = bars[-1]
    prev = await _prev_close(sym, venue, int(last["t"]))
    price = float(last["c"])
    change = price - prev if prev else None
    return {
        "id": pid,
        "symbol": sym,
        "price": price,
        "prevClose": prev,
        "change": change,
        "changePct": change / prev * 100 if change is not None and prev else None,
        "currency": "EUR",
        "asOf": int(last["t"]),
        "source": "yahoo",
        "venue": venue,
    }


async def listing_quote(pid: str) -> dict | None:
    """Latest quote of the entry's EUR listing, fetched by its yahoo symbol:
    {id, symbol, price, prevClose, change, changePct, currency, asOf, source,
    venue, stale}. Cached 15 min per id; when a refresh fails the last quote is
    served with stale=True. None for unknown ids or when yahoo never answered.
    Never raises, never touches prices' listing store."""
    key = (pid or "").strip().upper() if isinstance(pid, str) else ""
    row = get(key)
    if row is None:
        return None
    now = _now()
    cached = _quote_cache.get(key)
    if cached and now - cached[1] < QUOTE_TTL_S:
        return {**cached[0], "stale": now - cached[0]["asOf"] > QUOTE_STALE_S}
    try:
        quote = await _fetch_quote(key, row)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("paper_universe listing_quote %s failed: %s", key, e)
        quote = None
    if quote is None:
        if cached:
            return {**cached[0], "stale": True}
        return None
    _quote_cache[key] = (quote, _now())
    prices._prune_cache(_quote_cache, MAX_CACHE_ENTRIES)
    return {**quote, "stale": _now() - quote["asOf"] > QUOTE_STALE_S}


INTRADAY_INTERVALS = ("1m", "2m", "5m", "15m")
INTRADAY_TTL_S = 300                          # 5 min
_intraday_cache: dict[tuple[str, str], tuple] = {}     # (id, interval) -> (bars, ts)


async def intraday_bars(pid: str, interval: str = "5m") -> list[dict]:
    """[{t, c, v?}] (epoch s, raw EUR-listing close) of TODAY's session only, oldest first: the bars of the
    entry's yahoo symbol whose exchange-local date is today's. Cached 5 min per (id, interval). A failed
    fetch serves the last cached bars (when they are still today's), else []. Unknown ids / intervals and
    a day without bars -> []. Never raises."""
    try:
        key = ((pid or "").strip().upper() if isinstance(pid, str) else "", interval)
        row = get(key[0])
        if row is None or interval not in INTRADAY_INTERVALS:
            return []
        now = _now()
        today = datetime.datetime.fromtimestamp(now, ZoneInfo(VENUE_TZ[row["venue"]])).date().isoformat()
        cached = _intraday_cache.get(key)
        if cached and now - cached[1] < INTRADAY_TTL_S:
            return [dict(b) for b in cached[0]]
        try:
            bars = await prices._yf(row["yahoo"], prices._yf_bars_blocking, row["yahoo"], interval, "1d")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("paper_universe intraday_bars %s failed: %s", key[0], e)
            bars = None
        if bars is None:                # failure / backoff: keep what we have
            if cached:
                return [b for b in (dict(x) for x in cached[0]) if _bar_date(int(b["t"]), row["venue"]) == today]
            return []
        todays = [dict(b) for b in bars if isinstance(b, dict) and isinstance(b.get("t"), (int, float))
                  and _bar_date(int(b["t"]), row["venue"]) == today]
        _intraday_cache[key] = (todays, now)
        prices._prune_cache(_intraday_cache, MAX_CACHE_ENTRIES)
        return [dict(b) for b in todays]
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("paper_universe intraday_bars failed: %s", type(e).__name__)
        return []


# ------------------------------------------------------------------ Trading 212 mapping

def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _suffix_venue(ticker: str) -> str | None:
    m = _T212_SUFFIX_RE.match(ticker or "")
    return T212_SUFFIX_VENUE.get(m.group(1)) if m else None


def _eur_by_isin(instruments) -> dict[str, list[dict]]:
    """ISIN -> [{ticker, schedule, hint}] of the EUR instruments."""
    out: dict[str, list[dict]] = {}
    for inst in instruments:
        isin, cur, ticker = _field(inst, "isin"), _field(inst, "currency"), _field(inst, "ticker")
        if not (isinstance(isin, str) and isinstance(ticker, str) and ticker.strip()):
            continue
        if not isinstance(cur, str) or cur.strip().upper() != "EUR":
            continue
        sched = _field(inst, "schedule_id")
        ticker = ticker.strip()
        out.setdefault(isin.strip().upper(), []).append(
            {"ticker": ticker, "schedule": sched if isinstance(sched, int) else None,
             "hint": _suffix_venue(ticker)})
    return out


def _schedule_venues(rows: list[dict], by_isin: dict[str, list[dict]]) -> dict[int, str]:
    """schedule_id -> venue, learned from the instruments of the universe whose
    suffix hint already agrees with the entry's venue (majority wins)."""
    votes: dict[int, dict[str, int]] = {}
    for row in rows:
        for cand in by_isin.get(row["isin"] or "", []):
            if cand["schedule"] is not None and cand["hint"] == row["venue"]:
                v = votes.setdefault(cand["schedule"], {})
                v[row["venue"]] = v.get(row["venue"], 0) + 1
    return {s: max(v.items(), key=lambda kv: (kv[1], kv[0]))[0] for s, v in votes.items()}


def _pick(row: dict, cands: list[dict], sched_venue: dict[int, str]) -> str | None:
    if not cands:
        return None

    def rank(c):
        score = 0
        if c["hint"] == row["venue"]:
            score += 2
        if c["schedule"] is not None and sched_venue.get(c["schedule"]) == row["venue"]:
            score += 1
        return (-score, c["ticker"])

    ranked = sorted(cands, key=rank)
    if len(ranked) > 1 and rank(ranked[0])[0] == rank(ranked[1])[0]:
        log.warning("paper_universe: %s has %d equally ranked EUR listings %s; using %s",
                    row["id"], len(ranked), [c["ticker"] for c in ranked], ranked[0]["ticker"])
    return ranked[0]["ticker"]


async def map_t212(instruments) -> dict:
    """Fill ``t212`` of every entry from the broker's instrument list.

    A match is the same ISIN with currency EUR. Several EUR listings: the one
    whose exchange matches the entry's venue wins (the ticker suffix letter,
    ``d`` Xetra / ``a`` Amsterdam ..., and the exchange schedule learned from
    unambiguous matches are hints, not requirements); otherwise the first by
    ticker. Entries without a match get ``t212: None``.

    Returns ``{"mapped": {id: ticker}, "unmapped": [id, ...], "saved": bool}``
    and persists the result to ``data/paper_universe.json``. An unusable list
    (not a list, or empty: a failed broker call) changes nothing: it reports
    the current state with ``saved`` False. Never raises, never calls the broker.
    """
    try:
        doc = _load_doc()
        rows = copy.deepcopy(doc["entries"])
        if not isinstance(instruments, (list, tuple)) or not instruments:
            return {"mapped": {r["id"]: r["t212"] for r in rows if r["t212"]},
                    "unmapped": [r["id"] for r in rows if not r["t212"]], "saved": False}
        by_isin = _eur_by_isin(instruments)
        sched_venue = _schedule_venues(rows, by_isin)
        mapped: dict[str, str] = {}
        unmapped: list[str] = []
        for row in rows:
            ticker = _pick(row, by_isin.get(row["isin"] or "", []), sched_venue)
            row["t212"] = ticker
            if ticker:
                mapped[row["id"]] = ticker
            else:
                unmapped.append(row["id"])
        out_doc = {**{k: v for k, v in doc.items() if k != "entries"}, "entries": rows}
        loop = asyncio.get_running_loop()
        saved = bool(await loop.run_in_executor(None, jsonstore.save, STORE_FILE, out_doc))
        if not saved:
            log.warning("paper_universe: could not persist the t212 mapping")
        return {"mapped": mapped, "unmapped": unmapped, "saved": saved}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("paper_universe map_t212 failed: %s", e)
        return {"mapped": {}, "unmapped": ids(), "saved": False}
