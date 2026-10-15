"""Shared notification plumbing: ntfy push with a durable outbox + the
per-ticker notification store.

Every alert producer (price alerts, news, filings, digest, approvals, ...)
calls :func:`push`. It returns a :class:`Delivery`:

* ``sent``      - ntfy accepted the message now.
* ``queued``    - ntfy was unreachable, or quiet hours hold the push; the
                  message is in the durable outbox (``data/ntfy_outbox.json``)
                  and the worker loop retries it with exponential backoff
                  (a held one waits for its ``not_before``) for up to
                  ``OUTBOX_MAX_AGE_S``.
* ``filtered``  - below ``NOTIFY_MIN_SEVERITY`` or over its per-source cap;
                  nothing will ever be sent.
* ``duplicate`` - the caller's ``dedupe_key`` was pushed inside its window.
* ``failed``    - ntfy was unreachable AND the outbox write failed: the alert
                  exists nowhere, so the producer must keep its pending state.

A ``Delivery`` is truthy only when the alert was accepted (sent / queued).
Producers decide whether to consume their cooldown / dedupe / seen state with
``delivery.consumed`` (every status except ``failed``): a filtered or duplicate
alert must not be re-raised forever, and a queued one is already guaranteed.

Policy applied before anything leaves the box (see :func:`policy_status`):

* quiet hours (``NOTIFY_QUIET_HOURS``, default ``22-7``, container-local): a
  push is stored in the outbox with ``not_before`` = the window end and
  returns ``queued`` ("quiet hours"). Priority-5 pushes from ``price`` /
  ``policy`` and approval pushes (``actions``) bypass the hold. The outbox
  drain honours ``not_before``.
* per-source caps (``NOTIFY_SOURCE_CAPS``, rolling 24 h, persisted in
  ``data/notify_caps.json``): over the cap nothing is sent and the push
  returns ``filtered`` ("cap"); :func:`alert` still stores the row. Approval
  pushes are never capped.
* sources ``ops_watch`` / ``ops`` go to ``NTFY_TOPIC_OPS`` (default
  ``<NTFY_TOPIC>-ops``) with the priority capped at 3.

The notification store file is bind-mounted to the host and read by the alert
center (``app/api/alerts.py``); rows carry an id, source, ticker, severity and
url so the UI can list, filter and acknowledge them.
"""
import asyncio
import datetime
import hashlib
import logging
import os
import re
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
CAPS_FILE = os.path.join(DATA_DIR, "notify_caps.json")
CAP_WINDOW_S = 24 * 3600
DEFAULT_QUIET_HOURS = "22-7"
DEFAULT_SOURCE_CAPS = ("ops_watch=3,ops=3,triage=6,news=6,analysis=4,price=8,"
                       "insider=6,policy=3,market_light=2")
OPS_SOURCES = ("ops_watch", "ops")
OPS_MAX_PRIORITY = 3
QUIET_BYPASS_SOURCES = ("price", "policy")     # ... at priority 5 only


def _now() -> float:
    """The module clock (tests monkeypatch it)."""
    return time.time()


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

    @property
    def capped(self) -> bool:
        """True when the per-source cap suppressed the push (``alert()``
        still stores the row for the alert center)."""
        return self.status == FILTERED and self.reason == "cap"


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


async def _send(headers: dict, body: str, topic: str | None = None) -> bool:
    """One POST to ntfy (``topic`` overrides NTFY_TOPIC); True only on HTTP
    200. Never raises."""
    global _down_until
    try:
        r = await _client_get().post(
            f"{NTFY_URL}/{topic or NTFY_TOPIC}", content=body.encode(),
            headers=headers, auth=(NTFY_USER, NTFY_PASS))
        if r.status_code == 200:
            _down_until = 0.0
            return True
        log.warning("ntfy push non-200: %s", r.status_code)
    except Exception as e:
        log.warning("ntfy push failed: %s", e)
    _down_until = _now() + DOWN_HOLD_S
    return False


# --- durable outbox -------------------------------------------------------

def _outbox_load() -> list[dict]:
    rows = jsonstore.load(OUTBOX_FILE, [])
    return [r for r in rows if isinstance(r, dict) and r.get("id")] \
        if isinstance(rows, list) else []


def _enqueue(headers: dict, body: str, topic: str | None = None,
             not_before: float = 0.0) -> bool:
    """Append to the outbox (synchronous: no await between load and save, so
    it cannot interleave with the drain loop). ``not_before`` holds the entry
    (quiet hours) until that epoch; ``topic`` overrides NTFY_TOPIC. False when
    the write failed."""
    now = _now()
    rows = _outbox_load()
    entry = {"id": uuid.uuid4().hex[:12], "created": now, "attempts": 0,
             "next_try": not_before or now + BACKOFF_BASE_S,
             "headers": headers, "body": body}
    if topic:
        entry["topic"] = topic
    if not_before:
        entry["not_before"] = not_before
    rows.append(entry)
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
    (tests / manual flush) but never ``not_before``: a push held by quiet
    hours stays held until its window ends. Returns
    ``{"sent", "pending", "dropped"}``."""
    now = _now()
    sent = dropped = 0
    for entry in _outbox_load():
        if now - float(entry.get("created") or now) > OUTBOX_MAX_AGE_S:
            _outbox_remove(entry["id"])
            dropped += 1
            _stats["dropped"] += 1
            log.warning("ntfy outbox: dropped expired alert %r",
                        (entry.get("headers") or {}).get("Title"))
            continue
        if float(entry.get("not_before") or 0) > now:
            continue                  # held by quiet hours
        if not force and float(entry.get("next_try") or 0) > now:
            continue
        if await _send(entry.get("headers") or {}, entry.get("body") or "",
                       entry.get("topic")):
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
            r["next_try"] = _now() + min(
                BACKOFF_BASE_S * 2 ** r["attempts"], BACKOFF_MAX_S)
    jsonstore.save(OUTBOX_FILE, rows)


# --- policy: quiet hours, caps, ops routing --------------------------------

def ops_topic() -> str:
    """ntfy topic of the ops sources (NTFY_TOPIC_OPS, default <topic>-ops)."""
    return os.getenv("NTFY_TOPIC_OPS", "").strip() or f"{NTFY_TOPIC}-ops"


def effective_priority(source: str, priority: int) -> int:
    """The priority that reaches ntfy: ops sources never exceed 3."""
    return min(priority, OPS_MAX_PRIORITY) if source in OPS_SOURCES \
        else priority


def quiet_window() -> tuple[int, int] | None:
    """(start_hour, end_hour) of NOTIFY_QUIET_HOURS (``"22-7"``; a window
    that wraps midnight is fine), or None when disabled (empty / ``off`` /
    start == end). A malformed value falls back to the default."""
    raw = os.getenv("NOTIFY_QUIET_HOURS", DEFAULT_QUIET_HOURS).strip().lower()
    if raw in ("", "off", "none"):
        return None
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", raw)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 23:
        log.warning("bad NOTIFY_QUIET_HOURS %r - using %r", raw,
                    DEFAULT_QUIET_HOURS)
        m = re.fullmatch(r"(\d+)-(\d+)", DEFAULT_QUIET_HOURS)
    start, end = int(m.group(1)), int(m.group(2))
    return None if start == end else (start, end)


def quiet_until(now: float | None = None) -> float | None:
    """Epoch at which the current quiet window ends, or None outside it."""
    win = quiet_window()
    if win is None:
        return None
    start, end = win
    dt = datetime.datetime.fromtimestamp(_now() if now is None else now)
    inside = (start <= dt.hour < end) if start < end \
        else (dt.hour >= start or dt.hour < end)
    if not inside:
        return None
    stop = dt.replace(hour=end, minute=0, second=0, microsecond=0)
    if stop <= dt:
        stop += datetime.timedelta(days=1)
    return stop.timestamp()


def source_caps() -> dict[str, int]:
    """``NOTIFY_SOURCE_CAPS`` as {source: max pushes per rolling 24 h}."""
    caps: dict[str, int] = {}
    for part in os.getenv("NOTIFY_SOURCE_CAPS", DEFAULT_SOURCE_CAPS).split(","):
        part = part.strip()
        if not part:
            continue
        name, _, num = part.partition("=")
        try:
            caps[name.strip()] = int(num)
        except ValueError:
            log.warning("bad NOTIFY_SOURCE_CAPS entry %r - ignored", part)
    return caps


def _cap_log(now: float) -> dict[str, list[float]]:
    """{source: push timestamps inside the rolling window} from CAPS_FILE."""
    raw = jsonstore.load(CAPS_FILE, {})
    out: dict[str, list[float]] = {}
    if not isinstance(raw, dict):
        return out
    for src, stamps in raw.items():
        keep = []
        for t in stamps if isinstance(stamps, list) else []:
            try:
                t = float(t)
            except (TypeError, ValueError):
                continue
            if now - t < CAP_WINDOW_S:
                keep.append(t)
        if keep:
            out[str(src)] = keep
    return out


def _cap_reserve(source: str, now: float) -> bool:
    """Count one push for ``source``; False (nothing recorded) when it is
    already at its cap. Reserved before the send so concurrent pushes cannot
    both slip under the cap; ``_cap_release`` undoes it on ``failed``."""
    stamps = _cap_log(now)
    rows = stamps.setdefault(source, [])
    cap = source_caps().get(source)
    if cap is not None and len(rows) >= cap:
        return False
    rows.append(now)
    if not jsonstore.save(CAPS_FILE, stamps):
        log.warning("notify caps state write failed")
    return True


def _cap_release(source: str, now: float) -> None:
    stamps = _cap_log(now)
    rows = stamps.get(source) or []
    if now in rows:
        rows.remove(now)
        jsonstore.save(CAPS_FILE, stamps)


def policy_status() -> dict:
    """Quiet hours, caps, per-source 24 h counts, held outbox entries."""
    now = _now()
    win = quiet_window()
    until = quiet_until(now)
    return {"quiet_hours": {"window": f"{win[0]}-{win[1]}" if win else "off",
                            "active": until is not None, "until": until},
            "caps": source_caps(),
            "counts_24h": {s: len(v) for s, v in _cap_log(now).items()},
            "held": sum(1 for r in _outbox_load()
                        if float(r.get("not_before") or 0) > now),
            "ops_topic": ops_topic()}


# --- push -----------------------------------------------------------------

async def push(title: str, body: str, severity: str = DEFAULT_SEVERITY, *,
               priority: int | None = None, tags: str | None = None,
               actions: list[dict] | None = None,
               dedupe_key: str | None = None,
               dedupe_s: int = 3600, source: str = "",
               click: str | None = None) -> Delivery:
    """Push via ntfy (see the module docstring for the ``Delivery`` contract
    and the quiet-hours / cap / ops-routing policy).

    ``severity`` (info|warning|error|critical) is the producer's statement of
    importance; it maps to an ntfy priority unless the caller passes an
    explicit ``priority``. ``source`` names the producer (caps, ops routing,
    the quiet-hours bypass). ``actions`` renders the X-Actions header - ntfy
    turns each into a button on the notification (the approval flow; such a
    push is never capped or held). ``dedupe_key`` suppresses a repeat of the
    same key within ``dedupe_s``. X-Click makes the phone notification open
    the dashboard when tapped: ``click`` (``alert()`` passes the row deep
    link) or the LAN URL via NTFY_CLICK_URL.
    """
    sev = str(severity or DEFAULT_SEVERITY).strip().lower()
    if sev not in SEVERITY_RANK:
        log.warning("unknown severity %r - treated as %r", severity,
                    DEFAULT_SEVERITY)
        sev = DEFAULT_SEVERITY
    floor = min_severity()
    if SEVERITY_RANK[sev] < SEVERITY_RANK[floor]:
        log.info("push filtered (%s < %s): %s", sev, floor, title)
        _stats["filtered"] += 1
        return Delivery(FILTERED, f"{sev} < {floor}")
    now = _now()
    if dedupe_key:
        for k in [k for k, ts in _dedupe.items() if now - ts > 86400]:
            del _dedupe[k]
        if now - _dedupe.get(dedupe_key, 0.0) < dedupe_s:
            _stats["duplicate"] += 1
            return Delivery(DUPLICATE, dedupe_key)
    encoded = [encode_action(a) for a in actions or () if isinstance(a, dict)]
    prio = effective_priority(
        source, int(priority) if priority is not None
        else SEVERITY_PRIORITY[sev])
    reserved = bool(source) and not encoded
    if reserved and not _cap_reserve(source, now):
        log.info("push over the %s cap: %s", source, title)
        _stats["filtered"] += 1
        return Delivery(FILTERED, "cap")
    hold_until = None
    if not encoded and not (prio >= 5 and source in QUIET_BYPASS_SOURCES):
        hold_until = quiet_until(now)
    headers = {
        "Title": _ascii(title),
        "Priority": str(prio),
        "Tags": _ascii(tags or ("chart_with_upwards_trend" if prio >= 4
                                else "newspaper")),
    }
    click = click or os.getenv("NTFY_CLICK_URL", "")
    if click:
        headers["X-Click"] = _ascii(click)
    if encoded:
        headers["X-Actions"] = _ascii(";".join(encoded))
    topic = ops_topic() if source in OPS_SOURCES else None
    if dedupe_key:
        _dedupe[dedupe_key] = now
    if hold_until:
        if _enqueue(headers, body, topic, not_before=hold_until):
            _stats["queued"] += 1
            return Delivery(QUEUED, "quiet hours")
    elif now >= _down_until and await _send(headers, body, topic):
        _stats["sent"] += 1
        return Delivery(SENT)
    elif _enqueue(headers, body, topic):
        _stats["queued"] += 1
        return Delivery(QUEUED, "ntfy unreachable")
    if reserved:
        _cap_release(source, now)
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


def new_row_id() -> str:
    return uuid.uuid4().hex[:12]


def store(ticker: str, source: str, title: str, body: str = "",
          priority: int = 3, url: str = "",
          severity: str | None = None, row_id: str | None = None) -> str:
    """Append to the per-ticker notification store (bind-mounted to host).
    ``row_id`` pins the id (``alert()`` allocates it before the push so the
    X-Click deep link can carry it). Returns the row id ('' when the write
    failed)."""
    st = jsonstore.load(NOTIF_STORE, {})
    if not isinstance(st, dict):
        st = {}
    lst = st.setdefault(ticker, [])
    row_id = row_id or new_row_id()
    lst.insert(0, {"id": row_id, "ts": _now(), "ticker": ticker,
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
                dedupe_key: str | None = None, dedupe_s: int = 3600,
                actions: list[dict] | None = None,
                store_body: str | None = None) -> Delivery:
    """Push + record in the notification store: the one sequence every alert
    producer needs. The store row id is allocated FIRST so the push carries
    ``X-Click = <DASHBOARD_PUBLIC_URL or NTFY_CLICK_URL>#alerts/<id>`` and the
    tap lands on exactly that row. The row is stored when the push was
    accepted or suppressed by the source cap (the alert center still lists
    it). ``store_body`` stores a longer body than the push text; ``actions``
    are the approval buttons. Never raises; the caller consumes its
    cooldown / dedupe / seen state iff ``delivery.consumed``."""
    row_id = new_row_id()
    base = (os.getenv("DASHBOARD_PUBLIC_URL", "").strip()
            or os.getenv("NTFY_CLICK_URL", "").strip())
    prio = effective_priority(source, priority)
    try:
        delivery = await push(title, body, severity, priority=prio,
                              tags=tags, actions=actions,
                              dedupe_key=dedupe_key, dedupe_s=dedupe_s,
                              source=source,
                              click=f"{base}#alerts/{row_id}" if base else None)
    except Exception as e:
        log.warning("push crashed for %s (%s): %s", ticker, source, e)
        return Delivery(FAILED, f"push raised: {e}")
    if delivery or delivery.capped:
        store(ticker, source, title, body if store_body is None else store_body,
              priority=prio, url=url, severity=severity, row_id=row_id)
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
            "min_severity": min_severity(), "policy": policy_status()}
