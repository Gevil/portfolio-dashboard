"""SEC EDGAR insider/8-K watcher (Form 4 + results-release signal).

Live-verified endpoint set (see the P4 endpoint report):
- ``data.sec.gov/submissions/CIK<cik:010d>.json`` - column-oriented
  ``filings.recent`` is the Form 4 / 8-K index (date column ``filingDate``;
  ``fileDate`` fallback; ``items`` containing ``2.02`` = results release).
- Raw Form 4 XML at ``/Archives/edgar/data/<cik>/<acc_nodash>/<doc>`` with the
  ``xslF345X05/`` render-prefix stripped; every scalar is wrapped in a
  ``<value>`` element.
- SEC policy: declared ``User-Agent`` (app name + admin contact, never a
  browser string), ``Accept-Encoding: gzip, deflate``, <= 10 req/s.

Scope: US registrants only. ASML is a foreign private issuer (6-K/20-F, no
Form 4 - insider deals go to the Dutch AFM), so the insider feed is gated per
issuer by the presence of ``4`` filings. Enabled with ``EDGAR_ENABLED=1``.

Alerts: one push per FILING (its notable trades grouped): Form 4 open-market
sale (code S) or purchase (code P) above a dollar threshold -> ntfy priority
5; 8-K with item 2.02 -> informational. A filing is marked seen only after
its alert was accepted by ``notify.push`` (sent or queued in the durable
outbox) - a SEC hiccup or a failed push leaves it unseen and the next pass
retries. The seen file is a ``{key: filing_date}`` map; a missing or corrupt
file means "unknown" and is re-seeded SILENTLY (never a flood of old
filings). Recent parsed Form 4s are kept in ``data/insider_recent.json`` for
the UI (``/api/insider``).
"""
import asyncio
import logging
import os
import pathlib
import time
import xml.etree.ElementTree as ET

import httpx

from app.api import jsonstore, notify, prices, runlog
from app.api.textsafe import clean_headline

log = logging.getLogger("edgar")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
SEEN_FILE = DATA_DIR / "edgar_seen.json"
RECENT_FILE = DATA_DIR / "insider_recent.json"
TICKER_CACHE = DATA_DIR / "edgar_tickers.json"
SEC_UA = os.getenv("SEC_USER_AGENT",
                   "portfolio-dashboard (set SEC_USER_AGENT with a contact email)")
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}
POLL_INTERVAL_S = int(os.getenv("EDGAR_POLL_INTERVAL", "1800"))
LOOKBACK_DAYS = 3
SEEN_KEEP_DAYS = LOOKBACK_DAYS + 4
# Open-market trades at or above this notional fire an alert.
ALERT_NOTIONAL_USD = float(os.getenv("EDGAR_ALERT_NOTIONAL_USD", "1000000"))
REQ_DELAY_S = 0.15
MAX_ALERT_TRADE_LINES = 8
# Fallback map if company_tickers.json is unreachable.
BUILTIN_CIK = {"NVDA": 1045810}

_client: httpx.AsyncClient | None = None
_task: asyncio.Task | None = None
_last_req = 0.0
_stats = {"last_run": 0.0, "alerts": 0, "errors": 0, "running": False}

CODE_LABEL = {"P": "open-market purchase", "S": "open-market sale",
              "A": "grant/award", "F": "tax withholding",
              "M": "option exercise", "G": "gift",
              "D": "disposition to issuer"}


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=SEC_HEADERS, timeout=20.0,
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


def _load_seen() -> dict[str, str] | None:
    """``{filing key: filing date}``; None = unknown (missing or corrupt
    file) - the caller must reseed silently instead of alerting."""
    data = jsonstore.load(SEEN_FILE, None)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):                  # legacy format: keys only
        today = time.strftime("%Y-%m-%d", time.gmtime())
        return {str(k): today for k in data}
    return None


def _save_seen(seen: dict[str, str]) -> bool:
    cutoff = time.strftime("%Y-%m-%d",
                           time.gmtime(time.time() - SEEN_KEEP_DAYS * 86400))
    kept = {k: d for k, d in seen.items() if d >= cutoff}
    return jsonstore.save(SEEN_FILE, kept)


async def ticker_ciks(symbols: list[str]) -> dict[str, int]:
    """ticker -> CIK via daily-cached company_tickers.json (+ builtin)."""
    out = dict(BUILTIN_CIK)
    cache = jsonstore.load(TICKER_CACHE, {})
    if not isinstance(cache, dict):
        cache = {}
    fresh = cache.get("fetched", 0) > time.time() - 86400
    if not fresh:
        try:
            r = await _sec_get("https://www.sec.gov/files/company_tickers.json")
            if r.is_success:
                rows = r.json()
                cache = {"fetched": time.time(),
                         "map": {str(v.get("ticker", "")).upper(): v["cik_str"]
                                 for v in rows.values() if isinstance(v, dict)
                                 and "cik_str" in v}}
                jsonstore.save(TICKER_CACHE, cache)
            else:
                log.warning("company_tickers.json HTTP %s", r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("ticker map fetch failed: %s", e)
    for s in symbols:
        cik = (cache.get("map") or {}).get(s.upper())
        if cik:
            out[s.upper()] = int(cik)
    return out


def _recent_records(sub: dict, forms: tuple[str, ...]) -> list[dict]:
    """Zip the column-oriented filings.recent into records, filtered."""
    rec = (sub.get("filings") or {}).get("recent") or {}
    n = len(rec.get("form", []))
    out: list[dict] = []
    for i in range(n):
        form = rec["form"][i]
        if form not in forms:
            continue
        out.append({
            "form": form,
            "date": (rec.get("filingDate") or rec.get("fileDate")
                     or [""] * n)[i],
            "acc": rec["accessionNumber"][i],
            "items": (rec.get("items") or [""] * n)[i],
            "doc": (rec.get("primaryDocument") or [""] * n)[i],
        })
    return out


def _val(elem: ET.Element, path: str) -> str | None:
    """First <value> text under a child path (Form 4 wraps scalars)."""
    node = elem.find(path)
    if node is None:
        return None
    v = node.find("value")
    text = v.text if v is not None else node.text
    return (text or "").strip() or None


def _num(text: str | None) -> float:
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def parse_form4(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    own = "reportingOwner/reportingOwnerRelationship"
    out: dict = {
        "insider": clean_headline(
            _val(root, "reportingOwner/reportingOwnerId/rptOwnerName") or "",
            80),
        "officer": clean_headline(_val(root, f"{own}/officerTitle") or "", 80),
        "isOfficer": (_val(root, f"{own}/isOfficer") == "1"),
        "isDirector": (_val(root, f"{own}/isDirector") == "1"),
        "issuer": clean_headline(_val(root, "issuer/issuerName") or "", 80),
        "symbol": clean_headline(
            _val(root, "issuer/issuerTradingSymbol") or "", 16),
        "period": _val(root, "periodOfReport") or "",
        "trades": [],
    }
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        shares = (_val(tx, "transactionAmounts/transactionShares/value")
                  or _val(tx, "transactionAmounts/transactionShares"))
        price = (_val(tx, "transactionAmounts/transactionPricePerShare/value")
                 or _val(tx, "transactionAmounts/transactionPricePerShare"))
        notional = _num(shares) * _num(price)
        out["trades"].append({
            "date": (_val(tx, "transactionDate/value")
                     or _val(tx, "transactionDate") or ""),
            "code": _val(tx, "transactionCoding/transactionCode") or "",
            "security": clean_headline(
                _val(tx, "securityTitle/value")
                or _val(tx, "securityTitle") or "", 60),
            "shares": _num(shares),
            "price": _num(price),
            "notional": round(notional, 2),
            "acqDisp": _val(
                tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "ownedAfter": _val(
                tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/"
                    "value"),
            "direct": _val(
                tx, "ownershipNature/directOrIndirectOwnership/value"),
        })
    return out


def _store_recent(parsed: dict) -> None:
    """Keep the parsed filing for the UI; a retried filing replaces itself."""
    recent = jsonstore.load(RECENT_FILE, [])
    if not isinstance(recent, list):
        recent = []
    recent = [e for e in recent
              if not (isinstance(e, dict) and e.get("acc") == parsed["acc"])]
    recent.insert(0, parsed)
    jsonstore.save(RECENT_FILE, recent[:50])


def _form4_message(cik: int, ticker: str, parsed: dict, rec: dict,
                   notable: list[dict]) -> tuple[str, str]:
    who = parsed["insider"] or "insider"
    role = f" ({parsed['officer']})" if parsed["officer"] else ""
    labels = {CODE_LABEL[t["code"]] for t in notable}
    kind = labels.pop() if len(labels) == 1 else f"{len(notable)} trades"
    title = f"{ticker} insider {kind}"
    lines = [f"{who}{role}"]
    for t in notable[:MAX_ALERT_TRADE_LINES]:
        lines.append(f"{CODE_LABEL[t['code']]}: {t['shares']:,.0f} sh @ "
                     f"${t['price']:,.2f} = ${t['notional']:,.0f} "
                     f"({t['date']})")
    if len(notable) > MAX_ALERT_TRADE_LINES:
        lines.append(f"... and {len(notable) - MAX_ALERT_TRADE_LINES} more")
    lines.append(f"Filed {rec['date']}")
    lines.append("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                 f"&CIK={cik:010d}&type=4")
    return title, "\n".join(lines)


async def _deliver(ticker: str, title: str, body: str, priority: int,
                   tags: str) -> bool:
    """Push + store one filing alert. False only when the alert exists
    nowhere (ntfy down AND outbox write failed) - the filing stays unseen."""
    delivery = await notify.alert(
        ticker, "insider", title, body, priority=priority, tags=tags,
        severity="error" if priority >= 5 else "warning")
    if delivery:
        _stats["alerts"] += 1
    return delivery.consumed


async def process_form4(cik: int, rec: dict, ticker: str) -> bool:
    """True when the filing is fully handled (alert accepted, or nothing
    notable, or permanently unparseable); False = retry next pass."""
    doc = rec["doc"].split("/", 1)[-1] if rec["doc"].startswith("xsl") \
        else rec["doc"]
    if not doc:
        return True
    url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
           f"{rec['acc'].replace('-', '')}/{doc}")
    r = await _sec_get(url)
    if not r.is_success:
        log.warning("Form 4 doc fetch %s: %s", url, r.status_code)
        return r.status_code in (404, 410)      # gone for good vs transient
    try:
        parsed = parse_form4(r.text)
    except ET.ParseError as e:
        log.warning("Form 4 XML parse failed for %s: %s", rec["acc"], e)
        return True
    parsed.update({"acc": rec["acc"], "filed": rec["date"], "ticker": ticker})
    _store_recent(parsed)
    notable = [t for t in parsed["trades"]
               if t["code"] in ("P", "S") and t["notional"] >= ALERT_NOTIONAL_USD]
    if not notable:
        return True
    title, body = _form4_message(cik, ticker, parsed, rec, notable)
    return await _deliver(ticker, title, body, 5, "briefcase")


async def process_8k(cik: int, rec: dict, ticker: str) -> bool:
    if "2.02" not in rec.get("items", ""):
        return True
    title = f"{ticker} 8-K: results release (item 2.02)"
    body = (f"Filed {rec['date']}, items {rec['items']}\n"
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik:010d}&type=8-K")
    return await _deliver(ticker, title, body, 3, "newspaper")


async def _submissions(cik: int, sym: str) -> dict | None:
    """The company's submissions JSON; None on any transport/HTTP/parse
    failure (logged, counted)."""
    try:
        r = await _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if not r.is_success:
            log.warning("submissions %s: HTTP %s", sym, r.status_code)
            _stats["errors"] += 1
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError) as e:
        _stats["errors"] += 1
        log.warning("submissions fetch %s failed: %s", sym, e)
        return None


def _watch_symbols() -> list[str]:
    from app.main import get_watchlist
    # Index tickers (config "alertable": false) have no insider/8-K signal.
    return [s for s in get_watchlist() if prices.is_alertable(s)]


async def run_once() -> None:
    seen = _load_seen()
    if seen is None:                     # never alert from an unknown baseline
        await _seed_seen()
        return
    symbols = _watch_symbols()
    ciks = await ticker_ciks(symbols)
    cutoff = time.strftime("%Y-%m-%d",
                           time.gmtime(time.time() - LOOKBACK_DAYS * 86400))
    for sym in symbols:
        cik = ciks.get(sym.upper())
        if not cik:
            continue
        sub = await _submissions(cik, sym)
        if sub is None:
            continue
        for rec in _recent_records(sub, ("4", "8-K")):
            if rec["date"] < cutoff:
                continue
            key = f"{sym}:{rec['acc']}"
            if key in seen:
                continue
            try:
                if rec["form"] == "4":
                    done = await process_form4(cik, rec, sym)
                else:
                    done = await process_8k(cik, rec, sym)
            except (httpx.HTTPError, ET.ParseError, ValueError) as e:
                _stats["errors"] += 1
                log.warning("processing %s %s failed: %s", sym, rec["acc"], e)
                continue
            if done:
                seen[key] = rec["date"]
                if not _save_seen(seen):
                    log.error("edgar seen-set write failed - %s may re-alert",
                              key)
            else:
                _stats["errors"] += 1


async def _seed_seen() -> None:
    """Populate the seen-set without firing alerts (first run, or after the
    seen file was lost/corrupt). Writes only if >= 1 fetch succeeded: a total
    outage must leave the state 'unknown' so the next pass seeds again rather
    than starting from an empty baseline that would flood alerts."""
    symbols = _watch_symbols()
    ciks = await ticker_ciks(symbols)
    cutoff = time.strftime("%Y-%m-%d",
                           time.gmtime(time.time() - LOOKBACK_DAYS * 86400))
    seen: dict[str, str] = {}
    fetched = 0
    for sym, cik in ciks.items():
        if sym not in {s.upper() for s in symbols}:
            continue
        sub = await _submissions(cik, sym)
        if sub is None:
            continue
        fetched += 1
        for rec in _recent_records(sub, ("4", "8-K")):
            if rec["date"] >= cutoff:
                seen[f"{sym}:{rec['acc']}"] = rec["date"]
    if not fetched:
        log.warning("edgar seed: no submissions fetch succeeded - will retry")
        return
    if _save_seen(seen):
        log.info("edgar seen-set seeded: %d filings", len(seen))


async def _loop() -> None:
    log.info("edgar loop started (every %ds, lookback %dd)",
             POLL_INTERVAL_S, LOOKBACK_DAYS)
    while True:
        started = time.time()
        _stats["last_run"] = started
        alerts0, errors0 = _stats["alerts"], _stats["errors"]
        ok = False
        note = ""
        try:
            seeding = _load_seen() is None
            await run_once()
            note = ("seen-set seeded (silent pass)" if seeding else
                    f"{_stats['alerts'] - alerts0} filing alert(s)")
            ok = _stats["errors"] == errors0
            if not ok:
                note += f", {_stats['errors'] - errors0} error(s)"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("edgar pass failed")
        runlog.record("edgar", ok, time.time() - started, note)
        await asyncio.sleep(POLL_INTERVAL_S)


def start() -> None:
    global _task
    if os.getenv("EDGAR_ENABLED", "0") != "1":
        log.info("edgar disabled (set EDGAR_ENABLED=1)")
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
    await close()


async def close() -> None:
    if _client and not _client.is_closed:
        await _client.aclose()


def recent(symbol: str | None = None) -> list[dict]:
    entries = jsonstore.load(RECENT_FILE, [])
    if not isinstance(entries, list):
        return []
    if symbol:
        sym = symbol.upper()
        entries = [e for e in entries
                   if e.get("ticker", "").upper() == sym
                   or e.get("symbol", "").upper() == sym]
    return entries[:30]


def status() -> dict:
    return {**_stats, "enabled": os.getenv("EDGAR_ENABLED", "0") == "1"}
