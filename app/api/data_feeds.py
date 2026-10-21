"""Data-feeds worker: drives the free accuracy feeds of docs/research/data-sources.md (section 4 items 2, 4, 6, 7).

One supervisor task (poll every 60 s) runs four independent steps, each on its own cadence and each isolated from the
others (a failing source costs only that source's data):

* **regime** (``regime_feed``): ECB + FRED CSV observations, hourly.
* **regulators** (``regulators``): the AFM short-position export and the AMF BDIF notices, daily from 08:00 Berlin.
* **consensus** (``consensus_log``): the analyst snapshot of every registry equity, daily on an XETR session day from
  30 minutes after the close. The ids already done today persist in ``state.json`` so a restart resumes the day.
* **identity audit** (OpenFIGI): monthly, every registry ISIN against its Yahoo symbol and venue. Mismatches are only
  REPORTED: ``ops_watch`` raises one grouped ``identity-mismatch`` finding for ``IDENTITY_FINDING_S`` after the audit
  (14 days, so an unfixed mismatch re-alerts with the next monthly audit); nothing is fixed automatically.

Every source has its own spacing (``regime_feed.SPACING_S``), health row and exponential backoff (a rate-limit
answer backs it off at once; other errors after 3 in a row; 120 s doubling to 1 h). ``GET /api/background`` shows them.

State: ``data/data_feeds/state.json`` (cadence markers) and ``data/data_feeds/identity.json`` (the last audit). The
observations themselves live in ``data/regime``, ``data/consensus`` and the universe news store.

Gate: ``DATA_FEEDS_ENABLED`` (default: on when ``FORECAST_ENABLED=1``), read at ``start()``. Respect the sources'
robots/ToS exactly as the report says: no Stooq, Euronext, BaFin or Boerse Frankfurt, polite spacing, descriptive UA.
"""
import asyncio
import datetime
import json
import logging
import os
import pathlib
import re
import time
from zoneinfo import ZoneInfo

from app.api import calendars, consensus_log, jsonstore, paper_universe, regime_feed, regulators, runlog, textsafe
from app.api.regime_feed import FeedError

log = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
FEEDS_DIR = DATA_DIR / "data_feeds"
STATE_FILE = FEEDS_DIR / "state.json"
IDENTITY_FILE = FEEDS_DIR / "identity.json"
_BERLIN = ZoneInfo("Europe/Berlin")
MIC = "XETR"

POLL_S = 60
PASS_BUDGET_S = 240                    # no new step starts after this many seconds of one pass
WATCH_INTERVAL_S = POLL_S + PASS_BUDGET_S + 300     # ops_watch calls a worker stalled after 2 x this
REGIME_EVERY_S = 3600
REGULATORS_HOUR = 8                    # Berlin hour from which the daily registers are polled
CONSENSUS_AFTER_CLOSE_S = 1800
IDENTITY_NEEDS_S = 90                  # an audit (13 OpenFIGI calls) starts only with this much pass budget left
IDENTITY_FINDING_S = 14 * 86400

SOURCES = ("ecb", "fred", "afm", "bdif", "consensus", "openfigi")
BACKOFF_BASE_S = 120.0
BACKOFF_MAX_S = 3600.0
FAILS_BEFORE_BACKOFF = 3

FIGI_URL = "https://api.openfigi.com/v3/mapping"
FIGI_BATCH = 10                        # keyless limit: 10 jobs per request, 25 requests per minute
EXCH_CODE = {"XETR": "GY", "XAMS": "NA", "XPAR": "FP", "XBRU": "BB", "XMIL": "IM"}
SUFFIX_VENUE = {"DE": "XETR", "AS": "XAMS", "PA": "XPAR", "BR": "XBRU", "MI": "XMIL"}

_task: asyncio.Task | None = None
_stats = {"running": False, "enabled": False, "last_run": 0.0, "runs": 0, "errors": 0, "lastError": None,
          "lastPass": None}
_health: dict[str, dict] = {}
_state: dict | None = None


def enabled() -> bool:
    v = os.getenv("DATA_FEEDS_ENABLED", "").strip()
    if not v:
        v = os.getenv("FORECAST_ENABLED", "0").strip()
    return v == "1"


# ------------------------------------------------------------------ per-source health and backoff

def _h(src: str) -> dict:
    return _health.setdefault(src, {"ok": 0, "fail": 0, "items": 0, "consecutiveFails": 0, "lastOkAt": None,
                                    "lastFailAt": None, "lastError": None, "backoffUntil": 0.0})


def source_ready(src: str, now: float) -> bool:
    return now >= _h(src)["backoffUntil"]


def note_ok(src: str, new_items: int, now: float) -> None:
    h = _h(src)
    h.update(ok=h["ok"] + 1, items=h["items"] + new_items, consecutiveFails=0, lastOkAt=now, lastError=None)


def note_fail(src: str, kind: str, detail: str, now: float) -> None:
    """Count a failure and back the source off: at once for a rate limit, after FAILS_BEFORE_BACKOFF in a row
    otherwise; the pause doubles per further failure up to BACKOFF_MAX_S."""
    h = _h(src)
    h["fail"] += 1
    h["consecutiveFails"] += 1
    h["lastFailAt"] = now
    h["lastError"] = textsafe.clean_text(f"{kind}: {detail}" if detail else kind, 160)
    n = h["consecutiveFails"] if kind == "rate_limited" else h["consecutiveFails"] - FAILS_BEFORE_BACKOFF + 1
    if n >= 1:
        h["backoffUntil"] = now + min(BACKOFF_BASE_S * 2 ** (n - 1), BACKOFF_MAX_S)


def health() -> dict:
    return {s: dict(_h(s)) for s in SOURCES}


async def _guard(src: str, call, now: float) -> tuple[bool, bool, dict | None]:
    """Run one source call under its backoff and health row -> ``(attempted, ok, result)``. ``call`` returns the
    ``{"new", "failed", ...}`` dict of a refresh function or raises ``FeedError``; it is never allowed to raise out."""
    if not source_ready(src, now):
        return False, False, None
    try:
        res = await call()
    except asyncio.CancelledError:
        raise
    except FeedError as e:
        note_fail(src, e.kind, e.detail, now)
        return True, False, None
    except Exception as e:
        log.exception("data_feeds %s failed", src)
        note_fail(src, "error", type(e).__name__, now)
        return True, False, None
    failed = res.get("failed") if isinstance(res, dict) else None
    if failed:
        first = str(next(iter(failed.values())))
        kind = "rate_limited" if any(str(v).startswith("rate_limited") for v in failed.values()) \
            else first.split(":", 1)[0]
        note_fail(src, kind, f"{len(failed)} failed: {first}", now)
        return True, False, res
    note_ok(src, int((res or {}).get("new") or 0), now)
    return True, True, res


# ------------------------------------------------------------------ state

def _load_state() -> dict:
    global _state
    if _state is None:
        doc = jsonstore.load(STATE_FILE, {})
        doc = doc if isinstance(doc, dict) else {}
        reg = doc.get("regulators") if isinstance(doc.get("regulators"), dict) else {}
        con = doc.get("consensus") if isinstance(doc.get("consensus"), dict) else {}
        rg = doc.get("regime") if isinstance(doc.get("regime"), dict) else {}
        _state = {"regime": {"last": float(rg.get("last") or 0.0)},
                  "regulators": {"afm": str(reg.get("afm") or ""), "bdif": str(reg.get("bdif") or "")},
                  "consensus": {"date": str(con.get("date") or ""),
                                "done": [x for x in con.get("done") or [] if isinstance(x, str)]}}
    return _state


def _save_state() -> None:
    if _state is not None and not jsonstore.save(STATE_FILE, _state):
        log.warning("data_feeds: state not saved")


def reset_state() -> None:
    """Forget every in-memory cache (tests; a restarted worker starts from the files)."""
    global _state, _task
    _state, _task = None, None
    _us_quotes_last["at"] = 0.0
    _health.clear()
    _stats.update(running=False, enabled=False, last_run=0.0, runs=0, errors=0, lastError=None, lastPass=None)


# ------------------------------------------------------------------ cadence (pure)

def berlin(now: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(now, _BERLIN)


def regime_due(now: float, last: float) -> bool:
    return now - last >= REGIME_EVERY_S


def regulators_due(now: float, last_day: str) -> bool:
    local = berlin(now)
    return local.hour >= REGULATORS_HOUR and local.date().isoformat() != last_day


def consensus_target(now: float) -> str | None:
    """The Berlin date to snapshot now: today, once it is an XETR session day and 30 min after its close."""
    local = berlin(now)
    day = local.date()
    if not calendars.is_session(MIC, day):
        return None
    bounds = calendars.session_bounds(MIC, day)
    if bounds is None:
        return None
    return day.isoformat() if local >= bounds[1] + datetime.timedelta(seconds=CONSENSUS_AFTER_CLOSE_S) else None


def identity_due(now: float, doc) -> bool:
    month = berlin(now).strftime("%Y-%m")
    return not (isinstance(doc, dict) and doc.get("month") == month)


# ------------------------------------------------------------------ identity audit (OpenFIGI)

def _norm_ticker(t) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(t or "").upper())


def compare_identity(row: dict, listings: list[dict]) -> list[dict]:
    """Mismatches between a registry row and the OpenFIGI listings of its ISIN on the registry's exchange:
    ``[{"kind": venue | listing | ticker | name, "detail"}]``, empty when everything agrees. ``venue`` = the Yahoo
    suffix names another exchange than the registry venue; ``listing`` = OpenFIGI knows no listing there; ``ticker`` =
    the Yahoo base symbol is none of the tickers listed there; ``name`` = no listed name plausibly matches the
    registry name."""
    out: list[dict] = []
    yahoo = str(row.get("yahoo") or "")
    base, _, suffix = yahoo.rpartition(".")
    base = base or yahoo
    implied = SUFFIX_VENUE.get(suffix.upper()) if "." in yahoo else None
    if implied and implied != row.get("venue"):
        out.append({"kind": "venue", "detail": f"yahoo {yahoo} is a {implied} symbol, registry venue {row.get('venue')}"})
    if not listings:
        out.append({"kind": "listing", "detail": f"OpenFIGI has no {EXCH_CODE.get(row.get('venue'), '?')} listing "
                                                 f"for {row.get('isin')}"})
        return out
    tickers = {_norm_ticker(l.get("ticker")) for l in listings} - {""}
    if tickers and _norm_ticker(base) not in tickers:
        out.append({"kind": "ticker", "detail": f"yahoo {yahoo} vs OpenFIGI {', '.join(sorted(tickers))[:60]}"})
    names = [l.get("name") for l in listings if l.get("name")]
    if names and not any(regime_feed.names_match(row.get("name"), n) for n in names):
        out.append({"kind": "name", "detail": f"registry '{textsafe.clean_text(row.get('name'), 40)}' vs OpenFIGI "
                                              f"'{textsafe.clean_text(names[0], 40)}'"})
    return out


def _figi_listing(d: dict) -> dict:
    return {"ticker": textsafe.clean_text(d.get("ticker"), 24), "name": textsafe.clean_text(d.get("name"), 80),
            "exchCode": textsafe.clean_text(d.get("exchCode"), 8)}


async def run_identity(now: float, rows: list[dict]) -> dict:
    """Audit ``rows`` (registry rows) against OpenFIGI and store the result in ``identity.json``. All or nothing: a
    failed call raises ``FeedError`` and the whole audit is retried (after the source's backoff)."""
    todo = [r for r in rows if isinstance(r.get("isin"), str) and r["isin"] and r.get("venue") in EXCH_CODE]
    mismatches: list[dict] = []
    checked = errors = 0
    for i in range(0, len(todo), FIGI_BATCH):
        batch = todo[i:i + FIGI_BATCH]
        body = [{"idType": "ID_ISIN", "idValue": r["isin"], "exchCode": EXCH_CODE[r["venue"]]} for r in batch]
        raw = await regime_feed.fetch_bytes("openfigi", FIGI_URL, method="POST", json_body=body,
                                            headers={"Content-Type": "application/json"})
        try:
            answer = json.loads(regime_feed.decode_text(raw))
        except ValueError:
            raise FeedError("parse", "OpenFIGI answer is not JSON")
        if not isinstance(answer, list) or len(answer) != len(batch):
            raise FeedError("parse", "OpenFIGI answer does not match the request")
        for r, item in zip(batch, answer):
            if not isinstance(item, dict) or "error" in item:
                errors += 1
                continue
            checked += 1
            listings = [_figi_listing(d) for d in item.get("data") or [] if isinstance(d, dict)]
            for m in compare_identity(r, listings):
                mismatches.append({"id": r["id"], "isin": r["isin"], "yahoo": r["yahoo"], "venue": r["venue"], **m})
    doc = {"at": now, "month": berlin(now).strftime("%Y-%m"), "checked": checked, "errors": errors,
           "mismatches": mismatches}
    if not jsonstore.save(IDENTITY_FILE, doc):
        raise FeedError("write", "identity.json not writable")
    return {"new": len(mismatches), "failed": {}, "checked": checked, "errors": errors}


def identity_mismatches(now: float | None = None) -> list[dict]:
    """The mismatches of the last audit while it is younger than IDENTITY_FINDING_S (what ``ops_watch`` reports);
    ``[]`` when the worker is off, no audit ran or it found nothing."""
    if not enabled():
        return []
    now = time.time() if now is None else now
    doc = jsonstore.load(IDENTITY_FILE, None)
    if not isinstance(doc, dict) or not isinstance(doc.get("at"), (int, float)) or now - doc["at"] > IDENTITY_FINDING_S:
        return []
    return [m for m in doc.get("mismatches") or [] if isinstance(m, dict) and m.get("id")]


# ------------------------------------------------------------------ steps: each -> (attempted, ok, new) or None

async def _step_regime(now: float, deadline: float, st: dict):
    if not regime_due(now, st["regime"]["last"]):
        return None
    att = ok = new = 0
    for src, fn in (("ecb", regime_feed.refresh_ecb), ("fred", regime_feed.refresh_fred)):
        a, o, res = await _guard(src, lambda fn=fn: fn(now), now)
        att += a
        ok += o
        new += int((res or {}).get("new") or 0)
    if att:
        st["regime"]["last"] = now              # retry in an hour (the backoff covers repeated failures)
        return att, ok, new
    return None


async def _step_regulators(now: float, deadline: float, st: dict):
    reg = st["regulators"]
    today = berlin(now).date().isoformat()
    att = ok = new = 0
    if regulators_due(now, reg["afm"]):
        a, o, res = await _guard("afm", lambda: regulators.refresh_afm(now), now)
        att += a
        ok += o
        new += int((res or {}).get("new") or 0)
        if res and res.get("complete"):
            reg["afm"] = today
    if regulators_due(now, reg["bdif"]):
        a, o, res = await _guard("bdif", lambda: regulators.refresh_bdif(now, deadline), now)
        att += a
        ok += o
        new += int((res or {}).get("new") or 0)
        if res and res.get("complete"):
            reg["bdif"] = today
    return (att, ok, new) if att else None


async def _step_consensus(now: float, deadline: float, st: dict):
    target = consensus_target(now)
    if target is None:
        return None
    con = st["consensus"]
    if con["date"] != target:
        con["date"], con["done"] = target, []
    done = set(con["done"])
    pending = [r for r in paper_universe.entries() if r["kind"] == "equity" and r["id"] not in done]
    if not pending:
        return None
    a, o, res = await _guard("consensus", lambda: consensus_log.run_snapshot(pending, now, deadline), now)
    if res:
        con["done"] = sorted(done | set(res.get("done") or []))
    return (int(a), int(o), int((res or {}).get("new") or 0)) if a else None


async def _step_identity(now: float, deadline: float, st: dict):
    if not identity_due(now, jsonstore.load(IDENTITY_FILE, None)):
        return None
    if regime_feed.mono() + IDENTITY_NEEDS_S > deadline:
        return None                              # not enough budget left this pass: next poll
    rows = paper_universe.entries()
    a, o, res = await _guard("openfigi", lambda: run_identity(now, rows), now)
    return (int(a), int(o), int((res or {}).get("new") or 0)) if a else None


US_QUOTES_EVERY_S = 300            # Xetra quote refresh for the US-lead names during the NYSE/XETR overlap
_us_quotes_last = {"at": 0.0}


async def _step_us_quotes(now: float, deadline: float, st: dict):
    """Keep the delayed Xetra quote of every US-lead name current while New York and Xetra are both open,
    so ``live_ws.us_lead`` has something to compare the live US price with (it needs a quote <= 30 min old).
    ``paper_universe.listing_quote`` caches 15 min, so this costs at most ~30 Yahoo quotes per 15 min."""
    from app.api import live_ws
    if now - _us_quotes_last["at"] < US_QUOTES_EVERY_S:
        return None
    if not (live_ws._session_open("XNYS", now) and live_ws._session_open("XETR", now)):
        return None
    ids = list(paper_universe.us_symbols())
    if not ids:
        return None
    _us_quotes_last["at"] = now
    sem = asyncio.Semaphore(4)

    async def one(pid):
        async with sem:
            try:
                return bool(await paper_universe.listing_quote(pid))
            except Exception:
                return False
    got = await asyncio.gather(*(one(pid) for pid in ids))
    return len(ids), sum(got), 0


STEPS = (("regime", _step_regime), ("regulators", _step_regulators), ("consensus", _step_consensus),
         ("identity", _step_identity), ("us_quotes", _step_us_quotes))


async def _tick() -> tuple[bool, str, bool]:
    """One supervisor pass -> (ok, note, acted). Never raises. ``ok`` is False when a step crashed or every source
    attempted in the pass (two or more) failed; one failing source only shows on its health row."""
    now = regime_feed.now()
    started = regime_feed.mono()
    deadline = started + PASS_BUDGET_S
    notes: list[str] = []
    ok, acted = True, False
    attempted = succeeded = 0
    try:
        st = _load_state()
        for name, step in STEPS:
            if regime_feed.mono() > deadline:
                notes.append("budget reached")
                break
            try:
                res = await step(now, deadline, st)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok = False
                notes.append(f"{name}: {type(e).__name__}")
                log.exception("data_feeds step %s failed", name)
                continue
            if res is None:
                continue
            att, good, new = res
            attempted += att
            succeeded += good
            acted = True
            notes.append(f"{name} {good}/{att} ok" + (f", {new} new" if new else ""))
        _save_state()
        if attempted >= 2 and succeeded == 0:
            ok = False
            notes.append("every source failed")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ok = False
        notes.append(f"pass: {type(e).__name__}")
        _stats["lastError"] = f"{type(e).__name__}: {str(e)[:150]}"
        log.exception("data_feeds pass failed")
    if not notes:
        notes.append("idle")
    return ok, "; ".join(notes), acted


async def _pass() -> bool:
    t0 = time.time()
    ok, note, acted = await _tick()
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    _stats["lastPass"] = {"at": _stats["last_run"], "ok": ok, "note": note}
    if not ok:
        _stats["errors"] += 1
    runlog.record("data_feeds", ok, time.time() - t0, note, idle=(ok and not acted))
    return ok


async def _loop() -> None:
    log.info("data_feeds loop started (poll %ss)", POLL_S)
    await _pass()
    while True:
        await asyncio.sleep(POLL_S)
        await _pass()


def start() -> None:
    global _task
    _stats["enabled"] = enabled()
    if not _stats["enabled"]:
        log.info("data_feeds disabled (DATA_FEEDS_ENABLED / FORECAST_ENABLED unset)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True


async def stop() -> None:
    global _task
    _stats["running"] = False
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


async def close() -> None:
    await regime_feed.close()


def status() -> dict:
    st = _load_state()
    doc = jsonstore.load(IDENTITY_FILE, None)
    audit = {"at": doc.get("at"), "month": doc.get("month"), "checked": doc.get("checked"),
             "mismatches": len(doc.get("mismatches") or [])} if isinstance(doc, dict) else None
    return {**_stats, "enabled": enabled(), "interval_s": WATCH_INTERVAL_S, "pollS": POLL_S, "sources": health(),
            "regimeLastAt": st["regime"]["last"] or None, "regulators": dict(st["regulators"]),
            "consensus": {"date": st["consensus"]["date"] or None, "done": len(st["consensus"]["done"])},
            "identityAudit": audit}
