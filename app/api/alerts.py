"""Alert center: the persisted notification store as a list with an
acknowledgement state.

Every alert producer writes a row with ``notify.store`` (id, ticker, source,
title, body, priority, severity, url). This module flattens those per-ticker
lists newest-first and tracks what the user has already seen. Reading the
local store instead of replaying the ntfy stream means the list works while
ntfy is down, survives ntfy's cache expiry, and carries structured fields.

Acknowledgement is a watermark plus an explicit id set, persisted in
``data/alerts_ack.json``:

* ``ack_all`` moves the watermark to the newest stored row (everything stored
  so far is read; later rows are unread again);
* ``ack(ids)`` marks single rows read without moving the watermark.
"""
import logging
import os
import time

from app.api import jsonstore, notify

log = logging.getLogger(__name__)

ACK_FILE = os.path.join(os.getenv("HISTORY_DIR", "/app/data"),
                        "alerts_ack.json")
ACK_IDS_CAP = 2000
MAX_LIMIT = 200


def _rows() -> list[dict]:
    """Every stored notification, normalised, newest first."""
    store = jsonstore.load(notify.NOTIF_STORE, {})
    if not isinstance(store, dict):
        return []
    out: list[dict] = []
    for ticker, rows in store.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row.get("ts") or 0)
                prio = int(row.get("priority") or 3)
            except (TypeError, ValueError):
                continue
            out.append({
                "id": row.get("id") or notify.legacy_id(ticker, row),
                "time": ts,
                "title": str(row.get("title") or ""),
                "message": str(row.get("body") or ""),
                "priority": prio,
                "severity": notify.alert_severity(prio, row.get("severity")),
                "source": str(row.get("source") or ""),
                "ticker": str(row.get("ticker") or ticker),
                "url": str(row.get("url") or ""),
            })
    out.sort(key=lambda r: r["time"], reverse=True)
    return out


def _load_ack() -> tuple[float, set[str]]:
    data = jsonstore.load(ACK_FILE, {})
    if not isinstance(data, dict):
        return 0.0, set()
    try:
        mark = float(data.get("watermark") or 0)
    except (TypeError, ValueError):
        mark = 0.0
    ids = data.get("ids")
    return mark, {str(i) for i in ids} if isinstance(ids, list) else set()


def _is_acked(row: dict, mark: float, ids: set[str]) -> bool:
    return row["time"] <= mark or row["id"] in ids


def snapshot(limit: int = 50) -> dict:
    """``{"items": [... newest first, each with "acked"], "unread": int}``.
    ``unread`` counts the whole store, not just the returned page."""
    limit = max(1, min(MAX_LIMIT, int(limit)))
    mark, ids = _load_ack()
    rows = _rows()
    for r in rows:
        r["acked"] = _is_acked(r, mark, ids)
    return {"items": rows[:limit],
            "unread": sum(1 for r in rows if not r["acked"])}


async def fetch_alerts(limit: int = 50) -> list[dict]:
    """The newest alerts as a bare list (the /api/stream SSE feed pushes each
    new id to the UI as an ``alert`` event)."""
    return snapshot(limit)["items"]


def ack(ids: list[str] | None = None, ack_all: bool = False) -> dict:
    """Mark alerts read. Returns the fresh ``{"unread": int}``; raises
    ``ValueError`` when neither a non-empty id list nor ``ack_all`` is given."""
    if not ack_all and not ids:
        raise ValueError("ids or all required")
    mark, seen = _load_ack()
    if ack_all:
        rows = _rows()
        newest = rows[0]["time"] if rows else time.time()
        mark = max(mark, newest)
        seen = set()
    else:
        seen.update(str(i) for i in ids or [])
    kept = sorted(seen)[-ACK_IDS_CAP:]
    if not jsonstore.save(ACK_FILE, {"watermark": mark, "ids": kept}):
        raise OSError("alert ack state could not be saved")
    return {"unread": snapshot(1)["unread"]}
