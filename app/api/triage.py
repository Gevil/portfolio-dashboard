"""AI triage: one batched lane call decides what a headline is actually worth.

The keyword matcher cannot tell "Nvidia says H20 export licence approved" from a
podcast about tariffs that mentions Nvidia once — and the historical
false-positives this replaces (tariffs podcast, Protolabs, Pre-IPO fraud, a Meta
lawsuit under NVDA, a SpaceX insider note under NVDA) are exactly that shape.
So keyword-warm news items are no longer pushed on their own: ``news_alerts``
offers them here, and ONE lane call per pass classifies the whole batch.

Only ``severity >= warning`` AND ``relevance >= 0.7`` reaches the phone.
Everything is logged to ``data/triage_log.json`` (ring) so the gate can be
audited — including the items the model correctly suppressed. An
``action_hint == "deep_dive"`` does not start an analysis: it files an approval
(see ``approvals``), unless the day's autonomous lane budget is already spent.

With the lane down the module does nothing at all: candidates stay queued (bounded)
and the keyword gate alone never fires, which is the point — the suppression must
not depend on the model being up.
"""
import asyncio
import hashlib
import datetime as dt
import json
import logging
import os
import pathlib
import time

from app.api import (approvals, jsonstore, lane_client, newsdedupe, notify,
                     prices, runlog, shortvolume)
from app.api.textsafe import clean_headline, clean_text

log = logging.getLogger("triage")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
CANDIDATES_FILE = DATA_DIR / "triage_candidates.json"
LOG_FILE = DATA_DIR / "triage_log.json"

INTERVAL_S = int(os.getenv("TRIAGE_INTERVAL_S", "600"))
MAX_CANDIDATES_QUEUED = 60
BATCH_MAX = int(os.getenv("TRIAGE_BATCH_MAX", "10"))
LOG_RING = 500
SEEN_TTL_S = 3 * 86400
CANDIDATE_TTL_S = int(os.getenv("TRIAGE_CANDIDATE_TTL_S", str(6 * 3600)))
MIN_RELEVANCE = float(os.getenv("TRIAGE_MIN_RELEVANCE", "0.7"))
PUSH_SEVERITIES = frozenset({"warning", "error", "critical"})
SEVERITY_PRIORITY = {"info": 1, "warning": 3, "error": 5, "critical": 5}
SHORT_SIGMA = float(os.getenv("TRIAGE_SHORT_SIGMA", "2.0"))
PLAYBOOK = pathlib.Path(__file__).resolve().parent.parent / "playbooks" / "triage.md"

SEVERITIES = ("info", "warning", "error", "critical")

_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "pushed": 0,
          "suppressed": 0, "queued": 0, "proposals": 0, "lane_down": 0,
          "expired": 0}
_lock = asyncio.Lock()


# --------------------------------------------------------------------- store

def _load(path: pathlib.Path, default):
    return jsonstore.load(path, default)


def _save(path: pathlib.Path, data) -> bool:
    return jsonstore.save(path, data)


def _item_key(kind: str, natural: str) -> str:
    """Dedupe key in the shared ``newsdedupe`` store. News candidates pass
    their ``newsdedupe.news_key`` as ``natural``, so a story seen by the
    keyword path and by triage is one story; other kinds hash their id."""
    if kind == "news":
        return natural
    return f"{kind}:{hashlib.sha1(natural.encode()).hexdigest()[:12]}"


def _key_of(cand: dict) -> str:
    return str(cand.get("key") or str(cand.get("id", "")).partition(":")[2])


def _mark_seen(cands: list[dict]) -> None:
    """Decided candidates are never offered again (SEEN_TTL_S)."""
    for c in cands:
        newsdedupe.mark(c["ticker"], [_key_of(c)])


def offer(ticker: str, kind: str, natural: str, payload: dict) -> bool:
    """Producer API (``news_alerts`` for keyword-warm items, this module's own
    feed watchers for the rest). Returns False when already queued/seen."""
    sym = (ticker or "").upper()
    key = _item_key(kind, natural)
    cid = f"{sym}:{key}"
    if newsdedupe.seen(sym, key, SEEN_TTL_S):
        return False
    queue = _load(CANDIDATES_FILE, [])
    if not isinstance(queue, list):
        queue = []
    if any(c.get("id") == cid for c in queue if isinstance(c, dict)):
        return False
    row = {"id": cid, "key": key, "ticker": sym, "kind": kind,
           "ts": time.time(), **payload}
    row["id"] = cid
    # Candidate text comes from third-party feeds: clean it once, at the door,
    # so neither the model prompt, the log nor a push ever sees raw input.
    row["headline"] = clean_headline(payload.get("headline"), 220)
    row["source"] = clean_text(payload.get("source"), 60)
    row["detail"] = clean_text(payload.get("detail"), 220)
    queue.append(row)
    _save(CANDIDATES_FILE, queue[-MAX_CANDIDATES_QUEUED:])
    _stats["queued"] = len(queue)
    return True


def pending(limit: int = BATCH_MAX) -> list[dict]:
    queue = _load(CANDIDATES_FILE, [])
    if not isinstance(queue, list):
        return []
    return [c for c in queue if isinstance(c, dict)][:limit]


def _requeue(rest: list[dict]) -> None:
    _save(CANDIDATES_FILE, rest[-MAX_CANDIDATES_QUEUED:])
    _stats["queued"] = len(rest)


def expire_stale() -> int:
    """Candidates older than CANDIDATE_TTL_S are news no more: they are
    removed from the queue, marked seen (never re-offered) and logged as
    auto-suppressed instead of burning a lane turn on yesterday's headline."""
    queue = _load(CANDIDATES_FILE, [])
    if not isinstance(queue, list):
        return 0
    now = time.time()
    fresh = [c for c in queue if isinstance(c, dict)
             and now - float(c.get("ts") or 0) <= CANDIDATE_TTL_S]
    stale = [c for c in queue if isinstance(c, dict)
             and now - float(c.get("ts") or 0) > CANDIDATE_TTL_S]
    if not stale:
        return 0
    _mark_seen(stale)
    _log([{"id": c.get("id"), "ticker": c.get("ticker"), "kind": c.get("kind"),
           "headline": (c.get("headline") or "")[:220],
           "source": c.get("source") or "", "ts": now, "severity": "info",
           "relevance": 0.0, "thesis": "expired unclassified "
           f"(older than {CANDIDATE_TTL_S // 3600} h)",
           "action_hint": "none", "delivered": False, "approval": None,
           "status": "expired"} for c in stale])
    _requeue(fresh)
    _stats["expired"] += len(stale)
    return len(stale)


def _log(rows: list[dict]) -> None:
    log_rows = _load(LOG_FILE, [])
    if not isinstance(log_rows, list):
        log_rows = []
    log_rows = rows + log_rows
    _save(LOG_FILE, log_rows[:LOG_RING])


# ---------------------------------------------------------------- candidates

def collect_feed_candidates() -> list[str]:
    """New short-ratio spikes and fresh filing events become candidates too.

    Reads caches only (FINRA rows, the filings ring) — no provider calls here.
    """
    added: list[str] = []
    try:
        from app.main import get_watchlist
        symbols = [s.upper() for s in get_watchlist() if prices.is_alertable(s)]
    except Exception:
        symbols = []
    for sym in symbols:
        latest = shortvolume.latest(sym) or {}
        ratio = latest.get("ratio")
        if not isinstance(ratio, (int, float)):
            continue
        vals = [r.get("ratio") for r in shortvolume.series(sym)
                if isinstance(r.get("ratio"), (int, float))]
        if len(vals) < 5:              # a sigma from <5 rows is noise
            continue
        mean = sum(vals) / len(vals)
        std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        if std > 0 and ratio >= mean + SHORT_SIGMA * std and offer(
                sym, "short-ratio", str(latest.get("date") or ""), {
                    "headline": (f"Short volume {ratio:.1%} of shares traded, "
                                 f"vs {mean:.1%} mean"),
                    "source": "FINRA Reg SHO",
                    "detail": f"date={latest.get('date')} ratio={ratio:.4f} "
                              f"mean={mean:.4f} sigma={SHORT_SIGMA}"}):
            added.append(sym)
    try:
        from app.api import filings
        for ev in filings.recent(limit=15):
            sym = str(ev.get("symbol") or ev.get("ticker") or "").upper()
            url = ev.get("url") or ""
            if not sym or not url:
                continue
            offer(sym, "filing", url, {
                "headline": f"{ev.get('form')} filed by {sym}",
                "source": "SEC EDGAR",
                "detail": f"filed={ev.get('filed')} desc={ev.get('desc') or ''}"})
    except Exception as e:
        log.debug("filings candidates unavailable: %s", e)
    return added


# ------------------------------------------------------------------- triage

def _playbook() -> str:
    try:
        return PLAYBOOK.read_text(encoding="utf-8")
    except OSError:
        log.warning("triage playbook missing at %s", PLAYBOOK)
        return ("You are a triage classifier for an equity watchlist. Reply with "
                "JSON {\"items\":[{\"id\",\"severity\",\"relevance\",\"thesis\","
                "\"action_hint\"}]} only. severity is info|warning|error|critical, "
                "relevance 0.0-1.0 to the ticker's investment thesis, action_hint "
                "is none|watch|deep_dive. Judge only from the given text.")


def _prompt(batch: list[dict]) -> list[dict]:
    rows = [{"id": c["id"], "ticker": c["ticker"], "kind": c["kind"],
             "headline": (c.get("headline") or "")[:220],
             "source": c.get("source") or "",
             "as_of": dt.datetime.fromtimestamp(c.get("ts") or 0).isoformat(
                 timespec="minutes"),
             "age_min": int((time.time() - (c.get("ts") or 0)) / 60),
             "detail": (c.get("detail") or "")[:220]} for c in batch]
    return [{"role": "system", "content": _playbook()},
            {"role": "user",
             "content": "Classify every item below. One object per id, no extras, "
                        "no prose.\n\n" + json.dumps(rows, ensure_ascii=False)}]


def _parse(text: str) -> list[dict]:
    """Tolerate a fenced or prose-wrapped reply; never raise."""
    body = (text or "").strip()
    if "```" in body:
        for chunk in body.split("```")[1:]:
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:]
            try:
                return _items_of(json.loads(chunk))
            except json.JSONDecodeError:
                continue
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 and end > start:
        try:
            return _items_of(json.loads(body[start:end + 1]))
        except json.JSONDecodeError:
            pass
    return []


def _items_of(data) -> list[dict]:
    if isinstance(data, dict):
        items = data.get("items")
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def _normalise(item: dict) -> dict | None:
    cid = str(item.get("id") or "")
    sev = str(item.get("severity") or "info").lower()
    if not cid or sev not in SEVERITIES:
        return None
    try:
        rel = float(item.get("relevance"))
    except (TypeError, ValueError):
        return None
    return {"id": cid, "severity": sev,
            "relevance": max(0.0, min(1.0, rel)),
            "thesis": clean_text(item.get("thesis"), 300),
            "action_hint": str(item.get("action_hint") or "none").lower()}


async def run_pass() -> dict:
    async with _lock:
        started = time.time()
        # Stale candidates leave first; feed watchers then offer, and the
        # whole queue is read once: a batch taken before the offers would
        # silently defer fresh candidates a pass.
        expire_stale()
        collect_feed_candidates()
        batch = pending()
        if not batch:
            _stats["last_run"] = started
            _stats["runs"] += 1
            runlog.record("triage", True, time.time() - started,
                          "no candidates", idle=True)
            return {"batch": 0, "pushed": 0, "suppressed": 0}

        budget = lane_client.budget_state("triage")
        if budget["left"] <= 0:
            _stats["last_run"] = started
            _stats["runs"] += 1
            _stats["lane_down"] += 1
            note = f"{len(batch)} candidate(s) held: autonomous lane budget spent"
            runlog.record("triage", True, time.time() - started, note,
                          idle=True)
            return {"batch": len(batch), "pushed": 0, "held": True}

        by_id = {c["id"]: c for c in batch}
        try:
            # enable_thinking=False: this is a classifier on a 1600-token
            # budget, and the lane's hidden reasoning would consume all of it
            # and return content:null (the exact failure the pipeline hit).
            reply = await lane_client.chat(_prompt(batch), max_tokens=1600,
                                           json_mode=True, timeout=240.0,
                                           autonomous=True, purpose="triage",
                                           enable_thinking=False)
        except Exception as e:                    # never expected; lane_client
            code = f"{type(e).__name__}: {str(e)[:120]}"   # returns envelopes
        else:
            if not isinstance(reply, dict):
                code = f"bad reply type {type(reply).__name__}"
            elif reply.get("error"):
                code = str(reply["error"])           # lane_down|timeout|http|
            else:                                    # budget|model_missing
                code = ""
        if code:
            # An error envelope is NOT an empty classification: answering "0
            # classified" here would let a lane outage look like a quiet news
            # day and hide the cause. The queue holds; the next pass retries.
            _stats["errors"] += 1
            _stats["last_run"] = started
            _stats["runs"] += 1
            note = f"{len(batch)} candidate(s) held: lane call failed ({code})"
            runlog.record("triage", False, time.time() - started, note)
            log.warning("triage lane call failed: %s", code)
            return {"batch": len(batch), "error": code[:200]}

        content = str(reply.get("content") or "")
        verdicts = [v for v in (_normalise(i) for i in _parse(content)) if v]
        seen_ids, pushed, suppressed, proposals = set(), 0, 0, 0
        rows = []
        for v in verdicts:
            cand = by_id.get(v["id"])
            if not cand:
                continue
            seen_ids.add(v["id"])
            deliver = (v["severity"] in PUSH_SEVERITIES
                       and v["relevance"] >= MIN_RELEVANCE)
            row = {**v, "ticker": cand["ticker"], "kind": cand["kind"],
                   "headline": (cand.get("headline") or "")[:220],
                   "source": cand.get("source") or "", "ts": time.time(),
                   "delivered": False, "approval": None}
            if deliver:
                actions = []
                if v["action_hint"] == "deep_dive" and budget["left"] > 0:
                    appr = approvals.create(cand["ticker"], v["thesis"],
                                            source_id=v["id"],
                                            raw=row["headline"])
                    actions = approvals.action_urls(appr)
                    row["approval"] = appr["id"]
                    proposals += 1
                body = (f"{row['headline']}\n\n{v['thesis']}\n\n"
                        f"source: {row['source']} · relevance "
                        f"{v['relevance']:.2f} · {row['kind']}")
                title = f"{cand['ticker']}: {row['headline'][:70]}"
                delivery = await notify.push(
                    title, body, severity=v["severity"],
                    priority=SEVERITY_PRIORITY[v["severity"]],
                    tags="warning" if v["severity"] != "info" else None,
                    actions=actions or None)
                row["delivered"] = bool(delivery)
                row["status"] = delivery.status
                if delivery:
                    pushed += 1
                    notify.store(cand["ticker"], "triage", title, body,
                                 priority=SEVERITY_PRIORITY[v["severity"]],
                                 severity=v["severity"])
                elif delivery.consumed:
                    # filtered by NOTIFY_MIN_SEVERITY / duplicate: this item
                    # is decided - re-queueing it would burn a turn per pass.
                    suppressed += 1
                else:
                    # Nowhere to put the alert: keep it queued for a retry.
                    seen_ids.discard(v["id"])
            else:
                suppressed += 1
            rows.append(row)

        _log(rows)
        # Anything the model did not answer for stays queued (bounded), so a
        # truncated reply cannot silently drop a candidate.
        unanswered = [i for i in by_id if i not in seen_ids]
        _mark_seen([by_id[i] for i in seen_ids])
        # Everything decided leaves the queue; candidates beyond this batch
        # (the queue is longer than BATCH_MAX) and unanswered ones stay.
        rest = [c for c in _load(CANDIDATES_FILE, [])
                if isinstance(c, dict) and c.get("id") not in seen_ids]
        _requeue(rest)

        _stats.update(last_run=started, runs=_stats["runs"] + 1, pushed=pushed,
                      suppressed=suppressed, proposals=proposals)
        note = (f"{len(batch)} classified, {pushed} pushed, {suppressed} "
                f"suppressed, {len(unanswered)} unanswered, {proposals} proposal(s)")
        runlog.record("triage", True, time.time() - started, note)
        log.info("triage: %s", note)
        return {"batch": len(batch), "pushed": pushed, "suppressed": suppressed,
                "unanswered": len(unanswered), "proposals": proposals}


async def _loop() -> None:
    log.info("triage loop started (every %ss, batch<=%s, relevance>=%s)",
             INTERVAL_S, BATCH_MAX, MIN_RELEVANCE)
    await asyncio.sleep(45)
    while True:
        try:
            await run_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["errors"] += 1
            log.exception("triage pass failed")
            runlog.record("triage", False, 0.0, "pass raised")
        await asyncio.sleep(INTERVAL_S)


def start() -> None:
    global _task
    if os.getenv("TRIAGE_ENABLED", "1") != "1":
        log.info("triage disabled (set TRIAGE_ENABLED=0)")
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


def status() -> dict:
    return {**_stats, "running": bool(_task),
            "enabled": os.getenv("TRIAGE_ENABLED", "1") == "1",
            "queued": len(_load(CANDIDATES_FILE, []) or []),
            "min_relevance": MIN_RELEVANCE,
            "push_severities": sorted(PUSH_SEVERITIES)}


def recent(limit: int = 50) -> list[dict]:
    rows = _load(LOG_FILE, [])
    return [r for r in rows if isinstance(r, dict)][:limit]