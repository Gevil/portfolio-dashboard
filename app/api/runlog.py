"""Worker-run ring log: the "silently skipped forever" killer.

Every background worker pass records one row — worker, ok, duration, note — so
``/api/worker-runs`` can answer *which* worker stopped doing anything, and when.
Before this, a worker that skipped its work (lane down, market closed, empty
feed, provider outage) was indistinguishable from a healthy one: ``_stats``
counters only ever went up.

Each worker owns its own ring of ``RUN_CAP`` rows (newest-first) in
``data/worker_runs.json`` (``{worker: [row, ...]}``): one chatty worker can no
longer push every other worker's history out of a shared ring. A legacy flat
list file is regrouped on first read.

Idle passes (``idle=True``: nothing changed, nothing to do) are not written as
rows - except one heartbeat row per ``HEARTBEAT_S`` - but they DO refresh the
in-memory liveness stamp behind :func:`last_success`, which is what the ops
watchdog uses to flag a worker whose passes stopped.

``record()`` is synchronous and NEVER raises: observability must not be able to
break the worker that calls it. All workers share one event loop and
``record()`` contains no ``await``, so the read-modify-write can never
interleave with another pass inside this process.
"""
import logging
import os
import pathlib
import time

from app.api import jsonstore

log = logging.getLogger("runlog")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
RUNS_FILE = DATA_DIR / "worker_runs.json"
RUN_CAP = 60              # rows kept PER WORKER
HEARTBEAT_S = 3600        # idle workers still leave one row per hour
NOTE_CHARS = 200

_STARTED = time.time()
_last_ok: dict[str, float] = {}      # worker -> ts of newest successful pass
_last_row: dict[str, float] = {}     # worker -> ts of newest written row


def _load() -> dict[str, list]:
    data = jsonstore.load(RUNS_FILE, {})
    if isinstance(data, list):                     # legacy single ring
        rings: dict[str, list] = {}
        for row in data:
            if isinstance(row, dict) and row.get("worker"):
                rings.setdefault(str(row["worker"]), []).append(row)
        return {w: rows[:RUN_CAP] for w, rows in rings.items()}
    if isinstance(data, dict):
        return {str(w): rows for w, rows in data.items()
                if isinstance(rows, list)}
    return {}


def record(worker: str, ok: bool, duration_s: float, note: str = "", *,
           idle: bool = False) -> None:
    """Append one pass result to the worker's ring. Never raises.

    ``idle=True`` marks a successful pass that found nothing to do: it only
    refreshes liveness and writes a row at most once per ``HEARTBEAT_S``."""
    try:
        worker = str(worker)
        now = time.time()
        if ok:
            _last_ok[worker] = now
        if idle and ok and now - _last_row.get(worker, 0.0) < HEARTBEAT_S:
            return
        rings = _load()
        rows = rings.setdefault(worker, [])
        rows.insert(0, {
            "ts": round(now, 3),
            "worker": worker,
            "ok": bool(ok),
            "duration_s": round(float(duration_s), 3),
            "note": str(note or "")[:NOTE_CHARS],
        })
        del rows[RUN_CAP:]
        if jsonstore.save(RUNS_FILE, rings):
            _last_row[worker] = now
    except Exception as e:
        log.warning("worker run log write failed: %s", e)


def recent(limit: int = 100, worker: str | None = None) -> list[dict]:
    """Rings merged newest-first (served verbatim by ``/api/worker-runs``);
    ``worker`` narrows it to one ring."""
    rings = _load()
    if worker:
        rings = {worker: rings.get(worker, [])}
    rows = [r for ring in rings.values() for r in ring
            if isinstance(r, dict)]
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows[:limit] if limit > 0 else rows


def last(worker: str) -> dict | None:
    """Newest recorded row for one worker (None when it never wrote one)."""
    ring = _load().get(str(worker)) or []
    return ring[0] if ring else None


def last_success(worker: str) -> float | None:
    """Epoch of the newest successful pass in THIS process (idle passes
    included); None when the worker has not completed one since boot."""
    return _last_ok.get(str(worker))


def uptime_s() -> float:
    return time.time() - _STARTED
