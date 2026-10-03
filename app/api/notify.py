"""Shared notification plumbing: ntfy push with a durable outbox + the
per-ticker notification store.

Every alert producer (price alerts, news, filings, digest, approvals, ...)
calls :func:`push`. It returns a :class:`Delivery`:

* ``sent``      - ntfy accepted the message now.
* ``queued``    - ntfy was unreachable; the message is in the durable outbox
                  (``data/ntfy_outbox.json``) and the worker loop retries it
                  with exponential backoff for up to ``OUTBOX_MAX_AGE_S``.
* ``filtered``  - below ``NOTIFY_MIN_SEVERITY``; nothing will ever be sent.
* ``duplicate`` - the caller's ``dedupe_key`` was pushed inside its window.
* ``failed``    - ntfy was unreachable AND the outbox write failed: the alert
                  exists nowhere, so the producer must keep its pending state.

A ``Delivery`` is truthy only when the alert was accepted (sent / queued).
Producers decide whether to consume their cooldown / dedupe / seen state with
``delivery.consumed`` (every status except ``failed``): a filtered or duplicate
alert must not be re-raised forever, and a queued one is already guaranteed.

The notification store file is bind-mounted to the host and read by the alert
center (``app/api/alerts.py``); rows carry an id, source, ticker, severity and
url so the UI can list, filter and acknowledge them.
"""
import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass

import httpx

from app.api import jsonstore, runlog

log = logging.getLogger("notify")

NTFY_URL = os.getenv("NTFY_URL", "http://ntfy:80")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "stocks")
NTFY_USER = os.getenv("NTFY_USER", "stockbot")
NTFY_PASS = os.getenv("NTFY_PASS", "changeme")

DATA_DIR = os.getenv("HISTORY_DIR", "/app/data")
NOTIF_STORE = os.path.join(DATA_DIR, "notifications.json")
NOTIF_CAP = 200
OUTBOX_FILE = os.path.join(DATA_DIR, "ntfy_outbox.json")
OUTBOX_CAP = 200
OUTBOX_MAX_AGE_S = int(os.getenv("NTFY_OUTBOX_MAX_AGE_S", str(24 * 3600)))
OUTBOX_POLL_S = int(os.getenv("NTFY_OUTBOX_POLL_S", "15"))
BACKOFF_BASE_S = 30
BACKOFF_MAX_S = 1800
SEND_TIMEOUT_S = 10
DOWN_HOLD_S = 15          # after a failed send, queue directly for this long

# Producer-facing severity ladder. Every alert producer states an intent
# ("this matters") and the mapping below turns it into an ntfy priority, so a
# producer never has to know ntfy's 1-5 scale — and NOTIFY_MIN_SEVERITY can
# silence a whole class of noise without touching the producers.
SEVERITIES = ("info", "warning", "error", "critical")
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}
SEVERITY_PRIORITY = {"info": 1, "warning": 3, "error": 5, "critical": 5}
DEFAULT_SEVERITY = "warning"
# Alert-center severity (what the UI shows) per producer severity.
ALERT_SEVERITY = {"info": "info", "warning": "warn", "error": "urgent",
                  "critical": "urgent"}
# ntfy action fields in documented order ("action" is emitted first).
ACTION_FIELDS = ("label", "url", "method", "body", "clear-cache", "intent")

SENT, QUEUED, FILTERED, DUPLICATE, FAILED = (
    "sent", "queued", "filtered", "duplicate", "failed")


@dataclass(frozen=True)
class Delivery:
    """Outcome of one :func:`push` (see module docstring)."""
    status: str
    reason: str = ""

    def __bool__(self) -> bool:
        return self.status in (SENT, QUEUED)

    @property
    def consumed(self) -> bool:
        """True when the producer may consume its dedupe/cooldown state."""
        return self.status != FAILED


_stats = {"sent": 0, "queued": 0, "filtered": 0, "duplicate": 0, "failed": 0,
          "retried_ok": 0, "dropped": 0, "last_drain": 0.0, "runs": 0,
          "errors": 0}
_task: asyncio.Task | None = None
_client: httpx.AsyncClient | None = None
_down_until = 0.0
_dedupe: dict[str, float] = {}


def min_severity() -> str:
    """Quietest severity that still gets pushed (NOTIFY_MIN_SEVERITY)."""
    sev = os.getenv("NOTIFY_MIN_SEVERITY", DEFAULT_SEVERITY).strip().lower()
    if sev in SEVERITY_RANK:
        return sev
    log.warning("bad NOTIFY_MIN_SEVERITY %r — using %r", sev, DEFAULT_SEVERITY)
    return DEFAULT_SEVERITY


def _escape_action(value: str) -> str:
    """',' separates an action's fields and ';' separates actions — inside a
    value both must be backslash-escaped (as must the backslash itself)."""
    return (value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;"))


def encode_action(action: dict) -> str:
    """One ntfy action string: ``action=http,label=Buy,url=...,method=POST``."""
    parts = ["action=" + _escape_action(str(action.get("action") or "http"))]
    for field in ACTION_FIELDS:
        value = action.get(field)
        if value is None or value == "":
            continue
        parts.append(f"{field}=" + _escape_action(str(value)))
    return ",".join(parts)


def _ascii(value: str) -> str:
    """HTTP header values are latin-1/ascii only; never let a headline with an
    emoji raise out of push()."""
    return str(value).encode("ascii", "replace").decode()


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=SEND_TIMEOUT_S)
    return _client


async def _send(headers: dict, body: str) -> bool:
    """One POST to ntfy; True only on HTTP 200. Never raises."""
    global _down_until
    try:
        r = await _client_get().post(
            f"{NTFY_URL}/{NTFY_TOPIC}", content=body.encode(),
            headers=headers, auth=(NTFY_USER, NTFY_PASS))
        if r.status_code == 200:
            _down_until = 0.0
            return True
        log.warning("ntfy push non-200: %s", r.status_code)
    except Exception as e:
        log.warning("ntfy push failed: %s", e)
    _down_until = time.time() + DOWN_HOLD_S
    return False


# --- durable outbox -------------------------------------------------------

def _outbox_load() -> list[dict]:
    rows = jsonstore.load(OUTBOX_FILE, [])
    return [r for r in rows if isinstance(r, dict) and r.get("id")] \
        if isinstance(rows, list) else []


def _enqueue(headers: dict, body: str) -> bool:
    """Append to the outbox (synchronous: no await between load and save, so
    it cannot interleave with the drain loop). False when the write failed."""
    now = time.time()
    rows = _outbox_load()
    rows.append({"id": uuid.uuid4().hex[:12], "created": now,
                 "attempts": 0, "next_try": now + BACKOFF_BASE_S,
                 "headers": headers, "body": body})
    if len(rows) > OUTBOX_CAP:
        _stats["dropped"] += len(rows) - OUTBOX_CAP
        log.warning("ntfy outbox over cap: dropping %d oldest",
                    len(rows) - OUTBOX_CAP)
        rows = rows[-OUTBOX_CAP:]
    return jsonstore.save(OUTBOX_FILE, rows)


def outbox_size() -> int:
    return len(_outbox_load())


async def drain_once(force: bool = False) -> dict:
    """Retry every due outbox entry once. ``force`` ignores the backoff clock
    (tests / manual flush). Returns ``{"sent", "pending", "dropped"}``."""
    now = time.time()
    sent = dropped = 0
    for entry in _outbox_load():
        if now - float(entry.get("created") or now) > OUTBOX_MAX_AGE_S:
            _outbox_remove(entry["id"])
            dropped += 1
            _stats["dropped"] += 1
            log.warning("ntfy outbox: dropped expired alert %r",
                        (entry.get("headers") or {}).get("Title"))
            continue
        if not force and float(entry.get("next_try") or 0) > now:
            continue
        if await _send(entry.get("headers") or {}, entry.get("body") or ""):
            _outbox_remove(entry["id"])
            sent += 1
            _stats["retried_ok"] += 1
        else:
            _outbox_bump(entry["id"])
            break          # ntfy is down: stop hammering, keep order
    return {"sent": sent, "pending": outbox_size(), "dropped": dropped}


def _outbox_remove(entry_id: str) -> None:
    rows = [r for r in _outbox_load() if r["id"] != entry_id]
    jsonstore.save(OUTBOX_FILE, rows)


def _outbox_bump(entry_id: str) -> None:
    rows = _outbox_load()
    for r in rows:
        if r["id"] == entry_id:
            r["attempts"] = int(r.get("attempts") or 0) + 1
            r["next_try"] = time.time() + min(
                BACKOFF_BASE_S * 2 ** r["attempts"], BACKOFF_MAX_S)
    jsonstore.save(OUTBOX_FILE, rows)


# --- push -----------------------------------------------------------------

async def push(title: str, body: str, severity: str = DEFAULT_SEVERITY, *,
               priority: int | None = None, tags: str | None = None,
               actions: list[dict] | None = None,
               dedupe_key: str | None = None,
               dedupe_s: int = 3600) -> Delivery:
    """Push via ntfy (see the module docstring for the ``Delivery`` contract).

    ``severity`` (info|warning|error|critical) is the producer's statement of
    importance; it maps to an ntfy priority unless the caller passes an
    explicit ``priority``. ``actions`` renders the X-Actions header — ntfy
    turns each into a button on the notification (used by the approval flow).
    ``dedupe_key`` suppresses a repeat of the same key within ``dedupe_s``.
    X-Click makes the phone notification open the dashboard when tapped (LAN
    URL via NTFY_CLICK_URL).
    """
    sev = str(severity or DEFAULT_SEVERITY).strip().lower()
    if sev not in SEVERITY_RANK:
        log.warning("unknown severity %r — treated as %r", severity,
                    DEFAULT_SEVERITY)
        sev = DEFAULT_SEVERITY
    floor = min_severity()
    if SEVERITY_RANK[sev] < SEVERITY_RANK[floor]:
        log.info("push filtered (%s < %s): %s", sev, floor, title)
        _stats["filtered"] += 1
        return Delivery(FILTERED, f"{sev} < {floor}")
    now = time.time()
    if dedupe_key:
        for k in [k for k, ts in _dedupe.items() if now - ts > 86400]:
            del _dedupe[k]
        if now - _dedupe.get(dedupe_key, 0.0) < dedupe_s:
            _stats["duplicate"] += 1
            return Delivery(DUPLICATE, dedupe_key)
    prio = int(priority) if priority is not None else SEVERITY_PRIORITY[sev]
    headers = {
        "Title": _ascii(title),
        "Priority": str(prio),
        "Tags": _ascii(tags or ("chart_with_upwards_trend" if prio >= 4
                                else "newspaper")),
    }
    click = os.getenv("NTFY_CLICK_URL", "")
    if click:
        headers["X-Click"] = _ascii(click)
    if actions:
        encoded = [encode_action(a) for a in actions if isinstance(a, dict)]
        if encoded:
            headers["X-Actions"] = _ascii(";".join(encoded))
    if dedupe_key:
        _dedupe[dedupe_key] = now
    if now >= _down_until and await _send(headers, body):
        _stats["sent"] += 1
        return Delivery(SENT)
    if _enqueue(headers, body):
        _stats["queued"] += 1
        return Delivery(QUEUED, "ntfy unreachable")
    _dedupe.pop(dedupe_key or "", None)
    _stats["failed"] += 1
    log.error("ntfy push failed and outbox write failed: %s", title)
    return Delivery(FAILED, "ntfy unreachable and outbox write failed")


# --- notification store ---------------------------------------------------

def alert_severity(priority: int, severity: str | None = None) -> str:
    """Alert-center severity (info|warn|urgent) for a store row."""
    if severity in ALERT_SEVERITY:
        return ALERT_SEVERITY[severity]
    if severity in ("warn", "urgent", "info"):
        return severity
    return "urgent" if priority >= 5 else "info" if priority <= 2 else "warn"


def store(ticker: str, source: str, title: str, body: str = "",
          priority: int = 3, url: str = "",
          severity: str | None = None) -> str:
    """Append to the per-ticker notification store (bind-mounted to host).
    Returns the new row id ('' when the write failed)."""
    st = jsonstore.load(NOTIF_STORE, {})
    if not isinstance(st, dict):
        st = {}
    lst = st.setdefault(ticker, [])
    row_id = uuid.uuid4().hex[:12]
    lst.insert(0, {"id": row_id, "ts": time.time(), "ticker": ticker,
                   "source": source, "title": title, "body": body,
                   "priority": priority,
                   "severity": alert_severity(priority, severity),
                   "url": url or os.getenv("NTFY_CLICK_URL", "")})
    del lst[NOTIF_CAP:]
    if not jsonstore.save(NOTIF_STORE, st, indent=2):
        log.warning("notification store write failed")
        return ""
    return row_id


def legacy_id(ticker: str, row: dict) -> str:
    """Stable id for a store row written before ids existed."""
    raw = f"{ticker}|{row.get('ts')}|{row.get('title')}"
    return "l" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:11]


async def alert(ticker: str, source: str, title: str, body: str, *,
                priority: int, tags: str | None = None,
                severity: str = DEFAULT_SEVERITY, url: str = "",
                dedupe_key: str | None = None,
                dedupe_s: int = 3600) -> Delivery:
    """Push + record in the notification store when accepted: the one
    sequence every alert producer needs. Never raises; the caller consumes its
    cooldown / dedupe / seen state iff ``delivery.consumed``."""
    try:
        delivery = await push(title, body, severity, priority=priority,
                              tags=tags, dedupe_key=dedupe_key,
                              dedupe_s=dedupe_s)
    except Exception as e:
        log.warning("push crashed for %s (%s): %s", ticker, source, e)
        return Delivery(FAILED, f"push raised: {e}")
    if delivery:
        store(ticker, source, title, body, priority=priority, url=url,
              severity=severity)
    return delivery


# --- background worker (outbox drain) -------------------------------------

async def _loop() -> None:
    log.info("ntfy outbox worker started (every %ss)", OUTBOX_POLL_S)
    while True:
        started = time.time()
        try:
            pending = outbox_size()
            res = await drain_once() if pending else {"sent": 0, "dropped": 0,
                                                      "pending": 0}
            _stats["runs"] += 1
            _stats["last_drain"] = started
            runlog.record("notify_outbox", True, time.time() - started,
                          f"sent {res['sent']}, pending {res['pending']}, "
                          f"dropped {res['dropped']}",
                          idle=not (res["sent"] or res["dropped"]
                                    or res["pending"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["errors"] += 1
            log.exception("ntfy outbox pass failed")
            runlog.record("notify_outbox", False, time.time() - started,
                          "pass raised")
        await asyncio.sleep(OUTBOX_POLL_S)


def start() -> None:
    global _task
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
    await close()


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def status() -> dict:
    return {**_stats, "running": bool(_task and not _task.done()),
            "interval_s": OUTBOX_POLL_S, "outbox": outbox_size(),
            "min_severity": min_severity()}
