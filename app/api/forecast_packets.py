"""Frozen, point-in-time forecast packets (docs/plans/forecaster.md, phase 6 contract section 3).

One packet per (cohort, id): everything a forecaster may know about a name at
the cohort's as-of close, <= ``PACKET_MAX_CHARS`` (about 3k tokens). Every value
carries ``as_of`` + ``source`` or is the literal ``MISSING``; a source that fails
or only knows something newer than the as-of degrades that one field alone.

Point in time: closes and volume bars are truncated to the as-of date; headlines come from the
``universe_news`` store and need ``publishedAt`` AND ``ingestedAt`` at or before the as-of close (an item
first stored after it never enters, however old its story); the earnings date is the newest yfinance-calendar
observation fetched at or before the cutoff; the digest row and the FRED observations are cut at the same
instant. :func:`leaks` walks a packet and reports any ``as_of`` / ``publishedAt`` after the cutoff;
:func:`freeze` refuses to store one. No URL ever enters a packet (the store's reader has no URL field).

Schema 2: prices (``price.close``) stay RAW closes; returns, week1, 12-1 momentum, sigma and the 52w range come from
the ADJUSTED closes (``paper_universe.adj_closes``) and say so in ``source`` (the raw series is the labelled
fallback). ``dataStatus`` compares the last close with the last session the name's venue had completed at the
cutoff (``expected_session``, venue calendar): older is ``stale_data`` and ``forecasts.run`` leaves the name out
instead of forecasting on old data. ``regime`` also carries ``regime_feed.pit`` (ECB, FRED) and ``regulatorEvents``
holds ``regulators.pit_events`` as type + date tags only; both are the literal ``MISSING`` when the reader has nothing.

Frozen: ``data/forecast_packets/<cohort>/<id>.json.gz`` holds ``{"sha256",
"packet"}``. A stored packet is never rebuilt; :func:`freeze` returns the stored one
and :func:`load` raises :class:`PacketError` on a hash mismatch instead of healing it.
"""
import asyncio
import datetime
import gzip
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import time
from zoneinfo import ZoneInfo

from app.api import calendars, digest, jsonstore, macro, paper_universe, price_alerts, prices, scoreboard, textsafe, universe_news

log = logging.getLogger(__name__)

MISSING = "MISSING"
NA_ETF = "NOT_APPLICABLE (ETF, no earnings)"
MIC = "XETR"
SCHEMA_VERSION = 2                  # 2: returns/momentum/sigma/range from ADJUSTED closes, dataStatus, regime ECB+FRED, regulatorEvents
DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
PACKET_DIR = DATA_DIR / "forecast_packets"
BENCH_ID = "EUNL"                    # the index leg of the regime (paper_universe benchmark)
MAX_HEADLINES = 6
HEADLINE_CHARS = 200
NEWS_WINDOW_S = universe_news.NEWS_WINDOW_S      # 7 days
PACKET_MAX_CHARS = 9000              # ~3k tokens at a conservative 3 chars per token
CLOSE_GRACE_S = 20 * 60              # daily bars are complete this long after the session close
VOLUME_WINDOW = 20
VIX_SERIES = "VIXCLS"
VIX_STRESS, VIX_CALM = 25.0, 18.0
CLOSES_SOURCE = "paper_universe.daily_closes (EUR listing, raw close)"
ADJ_SOURCE = "paper_universe.adj_closes (EUR listing, split and dividend adjusted close)"
STALE_STATUS = "stale_data"
REG_DAYS = 30                        # regulator events shown: the last 30 days up to the as-of
REG_MAX_EVENTS = 5
ECB_KEYS = ("mro", "dfr", "dfrLastChange", "estr", "aaa10y")
FRED_KEYS = ("vix", "us10y")
_TZ = ZoneInfo("Europe/Berlin")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ONE_DAY = datetime.timedelta(days=1)
TIME_KEYS = ("as_of", "publishedAt")


class PacketError(Exception):
    """A packet that cannot be built, frozen or trusted (leak, corruption, size)."""


def _now() -> float:
    return time.time()


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _r(value, nd: int = 2):
    v = _num(value)
    return MISSING if v is None else round(v, nd)


def _iso_utc(epoch) -> str | None:
    v = _num(epoch)
    if v is None or v <= 0:
        return None
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()


# ------------------------------------------------------------------ sessions

def cutoff_for(as_of: datetime.date) -> datetime.datetime:
    """The as-of instant: the XETR close of ``as_of`` (early closes included)."""
    bounds = calendars.session_bounds(MIC, as_of)
    if bounds is not None:
        return bounds[1]
    return datetime.datetime(as_of.year, as_of.month, as_of.day, 17, 30, tzinfo=_TZ)


def last_completed_session(now: float) -> datetime.date:
    """The newest XETR session whose daily bar is complete at ``now`` (close plus
    ``CLOSE_GRACE_S``); never today's still-forming bar."""
    local = datetime.datetime.fromtimestamp(now, _TZ)
    today = local.date()
    bounds = calendars.session_bounds(MIC, today)
    if bounds is not None and local >= bounds[1] + datetime.timedelta(seconds=CLOSE_GRACE_S):
        return today
    return calendars.session_on_or_before(MIC, today - _ONE_DAY)


def expected_session(venue: str, as_of: datetime.date, cutoff_epoch: float) -> datetime.date:
    """The newest session of ``venue`` (its own calendar: holidays, early closes) that had closed at the
    cohort cutoff. A name whose last close is older than this is stale. An EU venue that is shut on the
    as-of expects its previous session; a venue that closes after the cutoff (XNYS) expects the one before."""
    day = calendars.session_on_or_before(venue, as_of)
    for _ in range(5):
        bounds = calendars.session_bounds(venue, day)
        if bounds is None or bounds[1].timestamp() <= cutoff_epoch:
            break
        day = calendars.previous_session(venue, day)
    return day


# ------------------------------------------------------------------ pure features

def clean_closes(rows, as_of_iso: str) -> list[tuple[str, float]]:
    """[(iso date, close)] oldest first, finite and positive, dated on/before the as-of."""
    out: dict[str, float] = {}
    for item in rows or []:
        try:
            d, c = item
        except (TypeError, ValueError):
            continue
        d, c = str(d)[:10], _num(c)
        if c is None or c <= 0 or d > as_of_iso:
            continue
        out[d] = c
    return sorted(out.items())


def _adjusted_used(closes, adj, as_of_iso: str) -> list[tuple[str, float]] | None:
    """The point-in-time adjusted series when it ends on the same date as the raw ``closes``, else None
    (the raw series is then used and the sections say so)."""
    if not adj or not closes:
        return None
    cleaned = clean_closes(adj, as_of_iso)
    return cleaned if cleaned and cleaned[-1][0] == closes[-1][0] else None


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0


def market_features(closes: list[tuple[str, float]], as_of_iso: str, adj=None, expected_iso: str | None = None) -> dict:
    """price / returns / week1 / momentum12_1 / sigma / range52w from point-in-time closes.

    ``closes`` are the RAW closes: they give the price section (what the market quotes). ``adj`` is the
    adjusted-close series of the same rows; when it ends on the same date as the raw series, returns, week1,
    momentum, sigma and the 52w range are computed from it (a split or a dividend is not a move), otherwise
    from the raw closes and the sections say so in their ``source``. ``expected_iso`` is the last session the
    venue had completed at the cutoff (default: the as-of date); an older last close is ``stale``."""
    names = ("price", "returns", "week1", "momentum12_1", "sigmaDaily", "range52w")
    if not closes:
        return {k: MISSING for k in names}
    last_d, last = closes[-1]
    used = _adjusted_used(closes, adj, as_of_iso)
    series, source = (used, ADJ_SOURCE) if used else (closes, CLOSES_SOURCE)
    vals = [c for _, c in series]
    n = len(vals)
    a_last = vals[-1]

    def sect(src=source, **kw):
        return {**kw, "as_of": last_d, "source": src}

    rets = {}
    for label, k in (("1d", 1), ("5d", 5), ("20d", 20), ("60d", 60)):
        rets[label] = sect(pct=round(_pct(a_last, vals[-1 - k]), 2)) if n > k else MISSING
    week: dict | str = MISSING
    target = (datetime.date.fromisoformat(last_d) - datetime.timedelta(days=7)).isoformat()
    older = [c for d, c in series if d <= target]
    if older:
        week = sect(pct=round(_pct(a_last, older[-1]), 2), basis="close on/before 7 calendar days earlier")
    i = n - 1
    mom: dict | str = MISSING
    if i >= scoreboard.MOM_LONG:
        mom = sect(pct=round(_pct(vals[i - scoreboard.MOM_SKIP], vals[i - scoreboard.MOM_LONG]), 2),
                   basis=f"close[t-{scoreboard.MOM_SKIP}] / close[t-{scoreboard.MOM_LONG}] - 1")
    sigma = price_alerts.ewma_sigma(vals[-(scoreboard.MOM_LONG + 1):])
    window = vals[-scoreboard.MOM_LONG:]
    rng: dict | str = MISSING
    if len(window) >= 20:
        hi, lo = max(window), min(window)
        rng = sect(pctBelowHigh=round(_pct(a_last, hi), 2), pctAboveLow=round(_pct(a_last, lo), 2),
                   windowSessions=len(window))
    stale = last_d < (expected_iso or as_of_iso)
    return {
        "price": sect(CLOSES_SOURCE, close=round(last, 4), currency="EUR", stale=stale),
        "returns": rets,
        "week1": week,
        "momentum12_1": mom,
        "sigmaDaily": MISSING if sigma is None else sect(pct=round(sigma * 100.0, 3),
                                                         basis="EWMA lambda 0.94 of daily log returns"),
        "range52w": rng,
    }


def volume_section(bars, as_of_iso: str, venue: str) -> dict | str:
    """Last session's volume vs the mean of the 20 sessions before it."""
    by_day: dict[str, float] = {}
    for b in bars or []:
        t, v = _num((b or {}).get("t")), _num((b or {}).get("v"))
        if t is None or v is None or v < 0:
            continue
        try:
            day = paper_universe._bar_date(int(t), venue)
        except Exception:
            continue
        if day <= as_of_iso:
            by_day[day] = v
    days = sorted(by_day)
    if len(days) < VOLUME_WINDOW + 1:
        return MISSING
    prior = [by_day[d] for d in days[-VOLUME_WINDOW - 1:-1]]
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return MISSING
    return {"ratio": round(by_day[days[-1]] / avg, 2), "lastVolume": int(by_day[days[-1]]),
            "avg20": int(avg), "as_of": days[-1], "source": "yahoo daily bars (prices._yf_bars_blocking)"}


def regime_label(vix: float | None, above_ma200: bool | None) -> str | None:
    """By code: stress (VIX >= 25), weak (index below its MA200), calm (VIX known and < 18, index
    above), neutral otherwise (an unknown VIX is never "calm"); None when neither input is known."""
    if vix is None and above_ma200 is None:
        return None
    if vix is not None and vix >= VIX_STRESS:
        return "stress"
    if above_ma200 is False:
        return "weak"
    if above_ma200 and vix is not None and vix < VIX_CALM:
        return "calm"
    return "neutral"


def select_headlines(items, cutoff_epoch: float, limit: int = MAX_HEADLINES, window_s: float = NEWS_WINDOW_S) -> list[dict]:
    """Newest-first, at most ``limit``, point in time: published AND ingested at or before the cutoff, published
    within the ``window_s`` before it, round-robin over domains so two or more domains appear whenever the store
    has them. An item without an ingestion time cannot be shown to be point-in-time and is dropped. Text is
    ``textsafe``-cleaned, frame delimiters stripped, framed as ``<<<NEWS ... NEWS>>>``; no URL travels."""
    seen: set[str] = set()
    pool: list[tuple[float, str, str]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        ts, ing = _num(it.get("published_at")), _num(it.get("ingested_at"))
        if ts is None or ing is None or ts <= 0 or ts > cutoff_epoch or ing > cutoff_epoch or ts <= cutoff_epoch - window_s:
            continue
        text = textsafe.clean_headline(it.get("headline"), HEADLINE_CHARS).replace("<<<", "").replace(">>>", "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        pool.append((ts, _domain(it), text))
    pool.sort(key=lambda x: -x[0])
    queues: dict[str, list[tuple[float, str]]] = {}
    for ts, dom, text in pool:
        queues.setdefault(dom, []).append((ts, text))
    out: list[dict] = []
    while queues and len(out) < limit:
        for dom in list(queues):
            if len(out) >= limit:
                break
            ts, text = queues[dom].pop(0)
            out.append({"headline": f"<<<NEWS {text} NEWS>>>", "domain": dom, "publishedAt": _iso_utc(ts), "_ts": ts})
            if not queues[dom]:
                del queues[dom]
    out.sort(key=lambda x: -x["_ts"])
    for o in out:
        del o["_ts"]
    return out


def _domain(item: dict) -> str:
    dom = str(item.get("domain") or "").strip().lower().removeprefix("www.")
    return dom or str(item.get("source") or "").strip().lower() or "unknown"


# ------------------------------------------------------------------ data seams (tests patch these)

async def _closes(pid: str) -> list:
    return await paper_universe.daily_closes(pid, "2y")


async def _volume_bars(row: dict) -> list:
    sym = row["yahoo"]
    return await prices._yf(sym, prices._yf_bars_blocking, sym, "1d", "3mo") or []


async def _fred_vix() -> list:
    return await macro.refresh_fred(VIX_SERIES, limit=10)


def _news_rows(pid: str, cutoff_epoch: float) -> list[dict]:
    """Point-in-time headline rows of ``pid`` (published AND ingested at or before the cutoff, last 7 days)."""
    return universe_news.pit_news(pid, cutoff_epoch, NEWS_WINDOW_S)


def _earnings_obs(pid: str, cutoff_epoch: float) -> dict | None:
    """The newest yfinance-calendar observation fetched at or before the cutoff, None when there is none."""
    return universe_news.next_earnings(pid, cutoff_epoch)


def _earnings_cache() -> dict:
    cache = jsonstore.load(macro.EARNINGS_CACHE, {})
    return cache if isinstance(cache, dict) else {}


def _advice() -> dict:
    return digest.advice_history()


def _regime_feed(as_of_iso: str) -> dict:
    """``regime_feed.pit``: the newest ECB / FRED values ingested at or before the as-of."""
    from app.api import regime_feed
    return regime_feed.pit(as_of_iso)


def _regulator_events(pid: str, as_of_iso: str, days: int) -> list:
    """``regulators.pit_events``: typed, dated regulator filings of ``pid`` up to the as-of."""
    from app.api import regulators
    return regulators.pit_events(pid, as_of_iso, days)


async def _adj_closes(pid: str) -> list:
    return await paper_universe.adj_closes(pid, "2y")


# ------------------------------------------------------------------ cohort context + packet

async def regime(as_of: datetime.date) -> dict | str:
    """Market regime by code at ``as_of``: VIX (FRED, when a key is set) and the benchmark ETF vs
    its 200-session average. One call per cohort; every packet embeds the result."""
    as_of_iso = as_of.isoformat()
    vix = MISSING
    above = None
    index: dict | str = MISSING
    try:
        obs = [o for o in await _fred_vix() if isinstance(o, dict) and _num(o.get("value")) is not None
               and str(o.get("date"))[:10] <= as_of_iso]
        if obs:
            o = max(obs, key=lambda x: str(x["date"]))
            vix = {"level": round(float(o["value"]), 2), "as_of": str(o["date"])[:10], "source": "FRED VIXCLS"}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: vix failed: %s", type(e).__name__)
    try:
        closes = clean_closes(await _closes(BENCH_ID), as_of_iso)
        if len(closes) >= 200:
            vals = [c for _, c in closes]
            sma = sum(vals[-200:]) / 200
            above = vals[-1] > sma
            index = {"id": BENCH_ID, "aboveSma200": above, "pctVsSma200": round(_pct(vals[-1], sma), 2),
                     "as_of": closes[-1][0], "source": f"{CLOSES_SOURCE}, benchmark ETF"}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: index regime failed: %s", type(e).__name__)
    try:
        feed = _feed_sections(_regime_feed(as_of_iso), as_of_iso)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: regime feed failed: %s", type(e).__name__)
        feed = {"ecb": MISSING, "fred": MISSING}
    if not isinstance(vix, dict):        # live FRED call failed or has no key: use the stored point-in-time series
        fv = feed.get("fred", {}).get("vix") if isinstance(feed.get("fred"), dict) else None
        if isinstance(fv, dict) and _num(fv.get("value")) is not None:
            vix = {"level": round(float(fv["value"]), 2), "as_of": str(fv.get("as_of") or fv.get("date"))[:10],
                   "source": "FRED VIXCLS (regime_feed, point-in-time store)"}
    label = regime_label(vix["level"] if isinstance(vix, dict) else None, above)
    stamps = [s["as_of"] for s in (vix, index, feed["ecb"], feed["fred"]) if isinstance(s, dict)]
    if label is None and not stamps:
        return MISSING
    return {"label": label if label is not None else MISSING, "vix": vix, "index": index, **feed, "as_of": max(stamps),
            "source": "code: VIX >= 25 stress; index below MA200 weak; VIX < 18 and above MA200 calm; else neutral"}


def _feed_value(v, as_of_iso: str) -> dict | str:
    """One ``regime_feed.pit`` value ``{value, date, source}`` -> ``{value, as_of, source}``; MISSING when it is
    not a dict, has no usable value or date, or is dated after the as-of."""
    if not isinstance(v, dict):
        return MISSING
    day = str(v.get("date") or "")[:10]
    if len(day) != 10 or day > as_of_iso:
        return MISSING
    val = v.get("value")
    if _num(val) is not None:
        val = round(float(val), 4)
    else:
        val = textsafe.clean_text(val, 40) or None
    src = textsafe.clean_text(v.get("source"), 60)
    if val is None or not src:
        return MISSING
    return {"value": val, "as_of": day, "source": src}


def _feed_sections(feed, as_of_iso: str) -> dict:
    """``regime_feed.pit(as_of)`` -> {"ecb": ..., "fred": ...}; a group with no usable value is MISSING."""
    out: dict = {}
    for group, keys in (("ecb", ECB_KEYS), ("fred", FRED_KEYS)):
        raw = feed.get(group) if isinstance(feed, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        vals = {k: _feed_value(raw.get(k), as_of_iso) for k in keys}
        stamps = [v["as_of"] for v in vals.values() if isinstance(v, dict)]
        out[group] = {**vals, "as_of": max(stamps), "source": f"regime_feed.pit ({group.upper()}), newest value ingested at or before the as-of"} \
            if stamps else MISSING
    return out


def _regulator_section(rows, as_of: datetime.date) -> dict | str:
    """Regulator filings of one name as TAGS: type and date only (no holder, amount or URL), the last
    ``REG_DAYS`` days up to the as-of, newest first, at most ``REG_MAX_EVENTS``; MISSING when none."""
    as_of_iso = as_of.isoformat()
    first = (as_of - datetime.timedelta(days=REG_DAYS)).isoformat()
    found: dict[tuple[str, str], dict] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        kind = textsafe.clean_text(r.get("kind") or r.get("type"), 40)
        day = str(r.get("date") or "")[:10]
        if not kind or len(day) != 10 or not first <= day <= as_of_iso:
            continue
        found[(kind, day)] = {"kind": kind, "date": day}
    events = sorted(found.values(), key=lambda e: (e["date"], e["kind"]), reverse=True)[:REG_MAX_EVENTS]
    if not events:
        return MISSING
    return {"events": events, "windowDays": REG_DAYS, "as_of": events[0]["date"],
            "source": "regulators store (type and date only), at or before the as-of"}


async def context(as_of: datetime.date) -> dict:
    """What every packet of a cohort shares: the regime and the digest advice rows."""
    try:
        advice = _advice()
    except Exception as e:
        log.warning("forecast_packets: advice failed: %s", type(e).__name__)
        advice = {}
    return {"regime": await regime(as_of), "advice": advice if isinstance(advice, dict) else {}}


def _decision_section(pid: str, advice: dict, cutoff_epoch: float) -> dict | str:
    row = advice.get(pid)
    if not isinstance(row, dict) or not row.get("rating"):
        return MISSING
    ts = _num(row.get("ts"))
    if ts is None or ts > cutoff_epoch:
        return MISSING
    return {"rating": row.get("rating"), "action": row.get("action") or MISSING,
            "score": _r(row.get("score"), 1), "confidence": _r(row.get("confidence"), 3),
            "draft": bool(row.get("draft")), "as_of": _iso_utc(ts),
            "source": "digest advice store (decision_v2), the real watchlist only"}


def _earnings_section(pid: str, kind: str, as_of: datetime.date, cutoff_epoch: float) -> dict | str:
    if kind == "etf":
        return NA_ETF
    try:
        obs = _earnings_obs(pid, cutoff_epoch)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: earnings log %s failed: %s", pid, type(e).__name__)
        obs = None
    if obs is not None:
        try:
            day = datetime.date.fromisoformat(str(obs["date"])[:10]) if obs.get("date") else None
        except ValueError:
            day = None
        if day is None or day <= as_of:
            return MISSING
        return {"eventDate": day.isoformat(), "daysAway": (day - as_of).days, "as_of": _iso_utc(obs["fetchedAt"]),
                "source": ("yfinance calendar, date carried forward while the calendar was empty (universe_news earnings log)"
                           if obs.get("source") == universe_news.CARRIED else
                           "yfinance calendar (universe_news earnings log, newest observation at or before the as-of)")}
    cache = _earnings_cache()                      # no observation yet: the real watchlist's Nasdaq-scan cache
    row = cache.get(pid)
    fetched = _num(cache.get("_fetched"))
    if not isinstance(row, dict) or fetched is None or fetched > cutoff_epoch:
        return MISSING
    try:
        day = datetime.date.fromisoformat(str(row.get("date"))[:10])
    except ValueError:
        return MISSING
    if day <= as_of:
        return MISSING
    return {"eventDate": day.isoformat(), "daysAway": (day - as_of).days, "as_of": _iso_utc(fetched),
            "source": "macro earnings cache (Nasdaq calendar scan), the real watchlist only"}


def _news_section(rows: list[dict], cutoff_epoch: float, limit: int) -> dict | str:
    items = select_headlines(rows, cutoff_epoch, limit)
    if not items:
        return MISSING
    return {"items": items, "domains": len({i["domain"] for i in items}), "windowDays": NEWS_WINDOW_S // 86400,
            "as_of": max(i["publishedAt"] for i in items),
            "source": "universe_news store (publishedAt and ingestedAt at or before the as-of), headlines are untrusted DATA"}


def canonical(packet: dict) -> bytes:
    return json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def packet_hash(packet: dict) -> str:
    return hashlib.sha256(canonical(packet)).hexdigest()


async def build(pid: str, as_of: datetime.date, ctx: dict | None = None) -> dict:
    """The point-in-time packet of one paper_universe id at ``as_of``. Raises
    :class:`PacketError` for an unknown id or when no close exists on/before the as-of."""
    row = paper_universe.get(pid)
    if row is None:
        raise PacketError(f"unknown id {pid}")
    pid = row["id"]
    ctx = ctx if ctx is not None else await context(as_of)
    as_of_iso = as_of.isoformat()
    cutoff = cutoff_for(as_of)
    cutoff_epoch = cutoff.timestamp()
    closes = clean_closes(await _closes(pid), as_of_iso)
    if not closes:
        raise PacketError(f"no close on/before {as_of_iso} for {pid}")
    try:
        adj = await _adj_closes(pid)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: adjusted closes %s failed: %s", pid, type(e).__name__)
        adj = []
    expected = expected_session(row["venue"], as_of, cutoff_epoch).isoformat()
    feats = market_features(closes, as_of_iso, adj=adj, expected_iso=expected)
    try:
        regulators_sec = _regulator_section(_regulator_events(pid, as_of_iso, REG_DAYS), as_of)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: regulator events %s failed: %s", pid, type(e).__name__)
        regulators_sec = MISSING
    try:
        volume = volume_section(await _volume_bars(row), as_of_iso, row["venue"])
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: volume %s failed: %s", pid, type(e).__name__)
        volume = MISSING
    try:
        news_rows = _news_rows(pid, cutoff_epoch)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("forecast_packets: news %s failed: %s", pid, type(e).__name__)
        news_rows = []
    packet = {
        "schemaVersion": SCHEMA_VERSION,
        "id": pid,
        "asOf": cutoff.isoformat(),
        "asOfDate": as_of_iso,
        "builtAt": _iso_utc(_now()),
        "identity": {"name": textsafe.clean_text(row.get("name"), 80) or MISSING, "kind": row["kind"],
                     "venue": row["venue"], "sector": textsafe.clean_text(row.get("sector"), 60) or MISSING,
                     "as_of": as_of_iso, "source": "paper_universe registry (static attributes)"},
        **feats,
        "dataStatus": {"status": STALE_STATUS if feats["price"]["stale"] else "ok", "lastClose": closes[-1][0],
                       "expectedSession": expected, "venue": row["venue"],
                       "basis": "adjusted" if _adjusted_used(closes, adj, as_of_iso) else "raw"},
        "volume20d": volume,
        "nextEarnings": _earnings_section(pid, row["kind"], as_of, cutoff_epoch),
        "regime": ctx.get("regime", MISSING),
        "regulatorEvents": regulators_sec,
        "digestDecision": _decision_section(pid, ctx.get("advice") or {}, cutoff_epoch),
        "news": MISSING,
    }
    for limit in range(MAX_HEADLINES, -1, -1):
        packet["news"] = _news_section(news_rows, cutoff_epoch, limit) if limit else MISSING
        if len(canonical(packet)) <= PACKET_MAX_CHARS:
            break
    else:
        raise PacketError(f"packet for {pid} exceeds {PACKET_MAX_CHARS} chars")
    bad = leaks(packet)
    if bad:
        raise PacketError(f"packet for {pid} holds data after its as-of: {bad[:3]}")
    return packet


def is_stale(packet: dict) -> bool:
    """True when the packet's last close is older than the last session its venue had completed at the as-of
    (``dataStatus.status == "stale_data"``): such a name is left out of forecasting, never forecast on old data."""
    ds = packet.get("dataStatus") if isinstance(packet, dict) else None
    return isinstance(ds, dict) and ds.get("status") == STALE_STATUS


# ------------------------------------------------------------------ point-in-time check

def leaks(packet: dict) -> list[str]:
    """Paths of every ``as_of`` / ``publishedAt`` stamped after the packet's cutoff (empty = clean).
    A date-only stamp must not be after ``asOfDate``; a full timestamp must not be after ``asOf``."""
    try:
        cutoff = datetime.datetime.fromisoformat(packet["asOf"]).timestamp()
        cut_day = str(packet["asOfDate"])
    except (KeyError, ValueError, TypeError):
        return ["asOf: missing or unparseable"]
    out: list[str] = []

    def bad(v) -> bool:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v > cutoff
        if isinstance(v, str):
            if len(v) == 10:
                return v > cut_day
            try:
                dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return True
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp() > cutoff
        return True

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                here = f"{path}.{k}" if path else k
                if k in TIME_KEYS:
                    if bad(v):
                        out.append(f"{here}={v}")
                else:
                    walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(packet, "")
    return out


# ------------------------------------------------------------------ frozen store

def path_for(cohort: str, pid: str) -> pathlib.Path:
    if not _ID_RE.match(cohort or "") or not _ID_RE.match(pid or ""):
        raise PacketError(f"unsafe cohort/id: {cohort!r} / {pid!r}")
    return PACKET_DIR / cohort / f"{pid}.json.gz"


def _read_doc(path: pathlib.Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise PacketError(f"unreadable packet {path.name}: {e}") from e
    try:
        doc = json.loads(gzip.decompress(raw).decode("utf-8"))
        packet, stored = doc["packet"], doc["sha256"]
    except (OSError, EOFError, ValueError, KeyError, TypeError) as e:
        raise PacketError(f"corrupt packet {path.name}: {type(e).__name__}") from e
    if not isinstance(packet, dict) or packet_hash(packet) != stored:
        raise PacketError(f"packet {path.name} does not match its sha256")
    return {"sha256": stored, "packet": packet}


def load(cohort: str, pid: str) -> dict | None:
    """``{"sha256", "packet"}`` of the frozen packet, None when it was never frozen; raises
    :class:`PacketError` for a corrupt or tampered file (never rebuilt silently)."""
    return _read_doc(path_for(cohort, pid))


def freeze(cohort: str, pid: str, packet: dict) -> dict:
    """Store ``packet`` unless one is already frozen for (cohort, id): the stored one is returned."""
    path = path_for(cohort, pid)
    existing = _read_doc(path)
    if existing is not None:
        return existing
    bad = leaks(packet)
    if bad:
        raise PacketError(f"refusing to freeze a packet with data after its as-of: {bad[:3]}")
    doc = {"sha256": packet_hash(packet), "packet": packet}
    payload = gzip.compress(json.dumps(doc, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8"), mtime=0)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise PacketError(f"could not store packet {path.name}: {e}") from e
    return doc


async def get_or_build(cohort: str, pid: str, as_of: datetime.date, ctx: dict | None = None) -> dict:
    """The frozen ``{"sha256", "packet"}`` of (cohort, id); built and frozen on first use."""
    existing = load(cohort, pid)
    if existing is not None:
        return existing
    return freeze(cohort, pid, await build(pid, as_of, ctx))


def render(packet: dict) -> str:
    """Prompt text of a packet: one JSON object between ``<<<PACKET`` and ``PACKET>>>``. The build time
    is audit data, never shown to the model."""
    shown = {k: v for k, v in packet.items() if k != "builtAt"}
    body = json.dumps(shown, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"<<<PACKET\n{body}\nPACKET>>>"
