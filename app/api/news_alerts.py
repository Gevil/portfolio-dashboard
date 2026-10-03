"""Negative-impact news alerts (absorbs the host-side news_monitor timer).

Every ``NEWS_INTERVAL_S`` (default 900, aligned to the old ``*:0/15`` timer)
this reads the per-ticker headline cache that ``topnews`` already maintains
(Finnhub company-news, else GDELT, else RSS - fetched once, paced and backed
off there; this module no longer fetches the same providers a second time).
GDELT is queried directly ONLY when a ticker's cache is missing or stale.

Headlines are keyword-matched and pushed as ONE grouped ntfy alert per ticker +
a notification-store entry. Quiet hours hold alerts in ``news_pending.json``
until the window ends; hot (priority-5) alerts bypass the per-ticker cooldown.

Gates that keep the noise out:
* relevance - an item must mention its ticker (config ``aliases``) in the
  headline or the summary before any keyword class may fire on it;
* classes - only a SHORT high-precision list (``hot_keywords``) may push at
  priority 5. Broad words (sanction, tariff, probe, lawsuit, ...) are
  ``broad_keywords``: they and the warm class go to the AI triage gate, and
  when triage is off they push at priority 4 / 3 - never 5;
* dedupe - one title-hash store per ticker (``newsdedupe``): the same story
  syndicated under many URLs alerts once. An item is marked seen only when it
  was delivered (or accepted into the durable outbox / quiet-hours queue) AND
  listed in the stored notification - never because it was merely fetched.

Every headline is sanitised (``textsafe.clean_headline``) before it is stored,
pushed or handed to triage.

Config: optional ``newsMonitor`` block in the dashboard config (read through
``config_store``) overrides DEFAULTS (keywords, lookback, quiet hours,
cooldown). Enabled only when ``NEWS_ALERTS_ENABLED=1`` (set at the cutover that
disables the host timer). Quiet hours use container-local time: run the
container with ``TZ=Europe/Prague`` to keep the host timer's semantics.
"""
import asyncio
import logging
import os
import pathlib
import time

import httpx

from app.api import (config_store, jsonstore, newsdedupe, notify, prices,
                     runlog, topnews, triage)
from app.api.textsafe import clean_headline, clean_text

log = logging.getLogger("news_alerts")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
LAST_ALERT_FILE = DATA_DIR / "news_last_alert.json"
PENDING_FILE = DATA_DIR / "news_pending.json"

NEWS_INTERVAL_S = int(os.getenv("NEWS_ALERTS_INTERVAL", "900"))
# A topnews cache entry older than this is not trusted (its refresh task is
# failing) and GDELT is queried directly instead.
CACHE_MAX_AGE_S = 3 * topnews.INTERVAL_S
MAX_LISTED = 25            # items one stored notification lists
MAX_PUSH_LINES = 15        # titles one overnight digest push lists

# Priority-3 headlines are not pushed on their own: the matcher cannot tell a
# story about the ticker from one that merely mentions it, so they are offered
# to the AI triage gate instead. With triage disabled the old behaviour
# returns - turning the model off must not silence news alerts.
TRIAGE_ON = os.getenv("TRIAGE_ENABLED", "1") == "1"

DEFAULTS = {
    "lookback_min": 90,
    "max_headlines_per_alert": 3,
    "quiet_hours": [0, 7],
    "alert_cooldown_min": 60,
    "dedupe_days": 7,
    # Ticker-relevance gate: an item must mention the ticker (one of these
    # aliases) in the headline or the summary before any keyword class may
    # fire on it. The top-level "aliases" block of config.json overrides this.
    "aliases": {
        "ASML": ["asml", "euv"],
        "NVDA": ["nvidia", "nvda"],
    },
    # priority-5: short, high-precision phrases only
    "hot_keywords": [
        "downgraded", "guidance cut", "earnings miss", "misses estimates",
        "export ban", "bankruptcy", "bankrupt",
    ],
    # priority-4 cap: broad words that produced false priority-5 pushes
    "broad_keywords": [
        "downgrade", "export control", "recall", "lawsuit", "lawsuits",
        "insider sell", "insider selling", "probe", "sanction", "sanctions",
        "crackdown", "antitrust", "inquiry", "default", "fraud", "tariff",
        "tariffs",
    ],
    # priority-3: material events worth a glance
    "warm_keywords": [
        "earnings", "guidance", "revenue", "order backlog", "bookings",
        "EUV", "lithography", "foundry", "capex", "TSMC", "Canon", "Nikon",
        "DRAM", "export", "China", "ECB", "rate cut", "rate hike", "defense",
        "merger", "acquisition", "buyback", "dividend", "upgrade",
        "price target",
    ],
}

_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "alerts": 0, "errors": 0, "running": False,
          "to_triage": 0}


def cfg() -> dict:
    merged = dict(DEFAULTS)
    try:
        raw = config_store.read()
        # Ticker aliases are a top-level config block (they describe the
        # watchlist, not the monitor); newsMonitor overrides everything else.
        if isinstance(raw.get("aliases"), dict):
            merged["aliases"] = raw["aliases"]
        block = raw.get("newsMonitor") or {}
        if isinstance(block, dict):
            merged.update(block)
    except Exception as e:
        log.warning("newsMonitor config read failed: %s", e)
    # Lower-case both sides once: matching is case-insensitive everywhere.
    merged["aliases"] = {
        str(k).upper(): [str(a).strip().lower() for a in (v or [])
                         if str(a).strip()]
        for k, v in (merged.get("aliases") or {}).items()}
    return merged


def _text(item: dict) -> str:
    """Headline + summary lower-cased: the two fields every rule matches."""
    return f"{item.get('headline') or ''} {item.get('summary') or ''}".lower()


def alias_hit(item: dict, ticker: str, c: dict) -> bool:
    """True when the item mentions ``ticker`` through one of its aliases.
    Relevance gate, not a priority rule. Without it a keyword class fires on
    stories about other companies that merely share the keyword (a tariffs
    podcast, a rival's lawsuit reported under NVDA), which is the whole
    false-positive class this module produced. A ticker with no configured
    ``aliases`` falls back to its own symbol, so adding a watchlist entry never
    silently disables its alerts."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return False
    aliases = (c.get("aliases") or {}).get(sym) or [sym.lower()]
    text = _text(item)
    return any(a in text for a in aliases if a)


def match(item: dict, c: dict, ticker: str) -> int:
    """0 = no alert, 3 = warm, 4 = broad hot word (capped), 5 = hot. An item
    that does not mention the ticker is 0 whatever it says: the keyword
    classes decide how loud an alert is, never whether it is about this
    ticker."""
    if not alias_hit(item, ticker, c):
        return 0
    t = (item.get("headline") or "").lower()
    if any(k.lower() in t for k in c["hot_keywords"]):
        return 5
    if any(k.lower() in t for k in c.get("broad_keywords") or []):
        return 4
    if any(k.lower() in t for k in c["warm_keywords"]):
        return 3
    return 0


def in_quiet_hours(c: dict, hour: int | None = None) -> bool:
    h = time.localtime().tm_hour if hour is None else hour
    try:
        lo, hi = c["quiet_hours"]
        return lo <= h < hi
    except (TypeError, ValueError):
        return False


def _safe_url(url) -> str:
    url = clean_text(url, 500, strip_urls=False) if isinstance(url, str) else ""
    return url if url.startswith(("http://", "https://")) else ""


def _clean_item(it: dict) -> dict:
    """Untrusted provider item -> sanitised item (the only shape that is ever
    stored, pushed or passed on)."""
    return {
        "headline": clean_headline(it.get("headline")),
        "summary": clean_text(it.get("summary"), 300),
        "url": _safe_url(it.get("url")),
        "source": clean_text(it.get("source"), 40),
        "published_at": it.get("published_at") or 0,
        "direct": bool(it.get("direct")),
    }


async def _ticker_items(client: httpx.AsyncClient, ticker: str,
                        cutoff: float) -> list[dict]:
    """Fresh headlines for one ticker: the topnews cache, else GDELT."""
    entry = topnews.get_top_news(ticker) or {}
    raw = entry.get("items") or []
    age = time.time() - float(entry.get("ts") or 0)
    if not raw or age > CACHE_MAX_AGE_S:
        raw = await topnews.fetch_gdelt(client, ticker) or []
    out = [_clean_item(it) for it in raw if isinstance(it, dict)]
    return [it for it in out if it["headline"]
            and (it["published_at"] or 0) >= cutoff]


def _line(it: dict) -> str:
    return f"* {it['source']}: {it['headline']}" if it.get("source") \
        else f"* {it['headline']}"


def _body(items: list[dict], shown: int) -> str:
    """Titles (with links) of the first ``shown`` items, then a count of the
    rest."""
    lines = []
    for it in items[:shown]:
        lines.append(_line(it) + (f"\n  {it['url']}" if it.get("url") else ""))
    if len(items) > shown:
        lines.append(f"...+{len(items) - shown} more")
    return "\n".join(lines)


def _pending_rows(raw) -> list[dict]:
    """Normalise a ticker's pending list (current dict rows, or the legacy
    ``[title, body, prio]`` triples written by older builds)."""
    rows = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict):
            rows.append(r)
        elif isinstance(r, list) and len(r) == 3:
            rows.append({"prio": r[2], "items": [], "legacy_body": str(r[1])})
    return rows


async def process_ticker(client: httpx.AsyncClient, ticker: str, c: dict,
                         pending: dict) -> int:
    """One ticker pass; returns the number of alerts accepted."""
    cutoff = time.time() - c["lookback_min"] * 60
    items = await _ticker_items(client, ticker, cutoff)
    ttl_s = c["dedupe_days"] * 86400
    cands: list[tuple[int, dict, str]] = []     # (prio, item, dedupe key)
    in_pass: set[str] = set()
    for it in items:
        prio = match(it, c, ticker) or (3 if it.get("direct") else 0)
        key = newsdedupe.news_key(it["headline"])
        if not prio or not key or key in in_pass:
            continue
        if newsdedupe.seen(ticker, key, ttl_s):
            continue
        in_pass.add(key)
        cands.append((prio, it, key))
    if not cands:
        return 0

    # Warm / broad items go to triage. offer() dedupes by title key against its
    # queue and the shared seen store, and marks an item seen only once the
    # model has decided it, so re-offering a queued story each pass is free.
    if TRIAGE_ON:
        offered = 0
        for prio, it, key in cands:
            if prio < 5 and triage.offer(
                    ticker, "news", key,
                    {"headline": it["headline"], "source": it["source"],
                     "url": it["url"],
                     "detail": it["summary"] or it["headline"]}):
                offered += 1
        if offered:
            log.info("%s: %d headline(s) handed to triage", ticker, offered)
            _stats["to_triage"] += offered
        cands = [x for x in cands if x[0] >= 5]
        if not cands:
            return 0

    top_prio = max(p for p, _, _ in cands)
    last = jsonstore.load(LAST_ALERT_FILE, {}).get(ticker, 0)
    if top_prio < 5 and time.time() - last < c["alert_cooldown_min"] * 60:
        log.info("%s: alert held back by cooldown (%d item(s) stay unseen)",
                 ticker, len(cands))
        return 0

    cands.sort(key=lambda x: -x[0])
    listed = cands[:MAX_LISTED]
    shown_items = [it for _, it, _ in listed]
    keys = [k for _, _, k in listed]
    n = len(shown_items)
    title = f"{ticker}: {n} relevant headline(s)"
    push_body = _body(shown_items, c["max_headlines_per_alert"])
    push_body += f"\n(last {c['lookback_min']} min)"
    full_body = _body(shown_items, n)

    if in_quiet_hours(c):
        log.info("%s: %d alert(s) held for quiet hours", ticker, n)
        pending.setdefault(ticker, []).append(
            {"prio": top_prio, "items": shown_items, "keys": keys})
        if jsonstore.save(PENDING_FILE, pending):
            newsdedupe.mark(ticker, keys)      # durably queued: not re-added
        else:
            pending[ticker].pop()
            log.error("%s: pending write failed - items stay unseen", ticker)
        return 0

    delivery = await notify.push(
        title, push_body, severity="error" if top_prio >= 5 else "warning",
        priority=top_prio, tags="newspaper" if top_prio == 3 else "warning")
    if not delivery.consumed:
        # Neither sent nor queued: keep the items unseen so the next pass
        # re-notifies once ntfy / the outbox is writable again.
        log.warning("%s: delivery failed (%s), alert not consumed", ticker,
                    delivery.reason)
        return 0
    if delivery:
        notify.store(ticker, "news", title, full_body, priority=top_prio,
                     url=shown_items[0].get("url", ""))
    newsdedupe.mark(ticker, keys)
    st = jsonstore.load(LAST_ALERT_FILE, {})
    st[ticker] = time.time()
    jsonstore.save(LAST_ALERT_FILE, st)
    return 1 if delivery else 0


async def flush_pending(pending: dict) -> int:
    """Deliver what quiet hours held: ONE grouped push per ticker listing every
    held title. A ticker leaves ``pending`` only when its push was accepted
    (sent / queued / deliberately filtered); a failed one stays for the next
    pass, and the file is rewritten after every ticker so a crash mid-flush
    loses nothing. Returns the number of tickers flushed."""
    flushed = 0
    for ticker, raw in list(pending.items()):
        rows = _pending_rows(raw)
        items, seen_keys, legacy = [], set(), []
        for r in rows:
            for it in r.get("items") or []:
                key = newsdedupe.news_key(it.get("headline")) or it.get("headline")
                if key not in seen_keys:
                    seen_keys.add(key)
                    items.append(it)
            if r.get("legacy_body"):
                legacy.append(r["legacy_body"])
        if not items and not legacy:
            pending.pop(ticker, None)
            continue
        prio = max(int(r.get("prio") or 3) for r in rows)
        title = f"{ticker} overnight news ({len(items) or len(legacy)})"
        if items:
            lines = [_line(it) for it in items[:MAX_PUSH_LINES]]
            if len(items) > MAX_PUSH_LINES:
                lines.append(f"...+{len(items) - MAX_PUSH_LINES} more "
                             "in the dashboard alert center")
            push_body = "\n".join(lines)
            full_body = _body(items, len(items))
        else:
            push_body = full_body = "\n".join(legacy)[:3500]
        delivery = await notify.push(
            title, push_body, severity="error" if prio >= 5 else "warning",
            priority=prio)
        if not delivery.consumed:
            log.warning("%s: overnight flush not delivered (%s), kept pending",
                        ticker, delivery.reason)
            continue
        if delivery:
            notify.store(ticker, "news", title, full_body, priority=prio,
                         url=(items[0].get("url", "") if items else ""))
        pending.pop(ticker, None)
        if not jsonstore.save(PENDING_FILE, pending):
            log.error("pending write failed after flushing %s", ticker)
        flushed += 1
    return flushed


async def run_once() -> None:
    c = cfg()
    from app.main import get_watchlist
    # Index tickers (config "alertable": false) are on the watchlist for the
    # market view, not to be alerted on.
    tickers = [t for t in get_watchlist() if prices.is_alertable(t)]
    raw_pending = jsonstore.load(PENDING_FILE, {})
    pending = raw_pending if isinstance(raw_pending, dict) else {}
    if not in_quiet_hours(c) and pending:
        await flush_pending(pending)
    async with httpx.AsyncClient(timeout=20) as client:
        for t in tickers:
            try:
                n = await process_ticker(client, t, c, pending)
                _stats["alerts"] += n
            except (httpx.HTTPError, OSError, ValueError) as e:
                _stats["errors"] += 1
                log.warning("news monitor %s failed: %s", t, e)
    log.info("news monitor done: %s", ", ".join(tickers))


async def _loop() -> None:
    log.info("news_alerts loop started (every %ds)", NEWS_INTERVAL_S)
    while True:
        started = time.time()
        _stats["last_run"] = started
        alerts0, errors0 = _stats["alerts"], _stats["errors"]
        ok = False
        note = ""
        try:
            await run_once()
            alerts = _stats["alerts"] - alerts0
            errors = _stats["errors"] - errors0
            ok = errors == 0
            note = f"{alerts} alert(s), {errors} per-ticker error(s)"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("news_alerts pass failed")
        runlog.record("news_alerts", ok, time.time() - started, note)
        await asyncio.sleep(NEWS_INTERVAL_S)


def start() -> None:
    global _task
    if os.getenv("NEWS_ALERTS_ENABLED", "0") != "1":
        log.info("news_alerts disabled (set NEWS_ALERTS_ENABLED=1)")
        return
    if _task is None or _task.done():
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


def status() -> dict:
    return {**_stats, "enabled": os.getenv("NEWS_ALERTS_ENABLED", "0") == "1",
            "interval_s": NEWS_INTERVAL_S}
