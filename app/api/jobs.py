"""In-process analysis job queue (the queue the retired sidecar exposed over HTTP).

Semantics ported from the sidecar's ``api/`` job table, kept honest:

* dedupe is by (ticker, mode STRENGTH), not by ticker alone: a request is
  deduped onto an in-flight job of the same ticker only when that job is at
  least as strong (quick < standard < deep). A user's deep run is never
  swallowed by a queued digest quick: it is queued BEHIND it and the caller is
  told which mode will run (``mode``) and which job it waits for (``behind``);
* cancel is queued-only. A running spine is a chain of lane completions that
  cannot be interrupted mid-call, so claiming otherwise would be a lie: the
  sidecar answered 409 for exactly this reason and we keep the same rule;
* execution is serialized through ``asyncio.Semaphore(1)`` - one pipeline at a
  time, because the lane serves one model and a second concurrent run only
  halves the decode rate of the first;
* the table is durable (``data/analysis_jobs.json``, atomic write via
  ``jsonstore``, ring of ``JOB_CAP``) and a boot pass marks every
  ``queued|running`` row ``error`` with ``interrupted by restart`` - a job whose
  runner died with the process must never look alive to the UI forever. Digest
  jobs interrupted this way (or by a clean shutdown) are handed to the digest
  worker once via ``take_interrupted`` so it can re-run them.

The runner is injected (``set_runner``) so this module never imports the
pipeline: ``app/main.py`` wires ``jobs.set_runner(ta_pipeline.run_job)``.
"""
import asyncio
import logging
import os
import pathlib
import secrets
import time

from app.api import jsonstore

log = logging.getLogger("jobs")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
JOBS_FILE = DATA_DIR / "analysis_jobs.json"
JOB_CAP = 200
MODES = ("quick", "standard", "deep")
# Weaker -> stronger; a stronger run subsumes a weaker one, never the reverse.
MODE_STRENGTH = {mode: i for i, mode in enumerate(MODES)}
STATUSES = ("queued", "running", "done", "error")
IN_FLIGHT = ("queued", "running")
MESSAGE_CHARS = 300
REASON_RESTART = "interrupted by restart"
REASON_SHUTDOWN = "interrupted by shutdown"
INTERRUPTED = (REASON_RESTART, REASON_SHUTDOWN)

_jobs: dict[str, dict] = {}
_queue: asyncio.Queue = asyncio.Queue()
# The whole point of the queue: one pipeline at a time on one GPU lane.
_sem = asyncio.Semaphore(1)
_runner = None
_consumer: asyncio.Task | None = None
_workers: set[asyncio.Task] = set()


def _new_job(ticker: str, mode: str, source: str) -> dict:
    """The job dict - exactly the keys ``/api/analysis/{id}`` serves."""
    return {
        "id": secrets.token_hex(6),
        "ticker": ticker,
        "mode": mode,
        "source": source,
        "status": "queued",
        "message": "queued",
        "decision": None,
        "result_path": None,
        "created_at": time.time(),
        "finished_at": None,
    }


def _load_disk() -> list[dict]:
    rows = jsonstore.load(JOBS_FILE, [])
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def persist() -> None:
    """Write the table atomically (newest-first, capped). Never raises."""
    rows = sorted(_jobs.values(), key=lambda j: j.get("created_at", 0),
                  reverse=True)[:JOB_CAP]
    if not jsonstore.save(JOBS_FILE, rows, indent=2):
        log.warning("job table write failed")


def get(job_id: str) -> dict | None:
    """A copy of one job (callers must not be able to mutate queue state)."""
    job = _jobs.get(job_id)
    return dict(job) if job else None


def list_jobs(limit: int = 50) -> list[dict]:
    """Newest-first job list for ``/api/jobs``."""
    rows = sorted(_jobs.values(), key=lambda j: j.get("created_at", 0),
                  reverse=True)
    return [dict(j) for j in (rows[:limit] if limit > 0 else rows)]


def in_flight(ticker: str, min_mode: str | None = None) -> dict | None:
    """The ticker's queued/running job that will finish first and is at least
    ``min_mode`` strong (any mode when None), if any: a running job beats a
    queued one, then the oldest queued."""
    sym = (ticker or "").upper().strip()
    floor = MODE_STRENGTH.get(min_mode, -1) if min_mode else -1
    best = None
    for job in _jobs.values():
        if job.get("ticker") != sym or job.get("status") not in IN_FLIGHT:
            continue
        if MODE_STRENGTH.get(job.get("mode"), 0) < floor:
            continue
        rank = (job["status"] != "running", job.get("created_at", 0))
        if best is None or rank < best[0]:
            best = (rank, job)
    return best[1] if best else None


def enqueue(ticker: str, mode: str, source: str) -> dict:
    """Queue an analysis run. -> ``{"job_id", "deduped", "mode", ...}``.

    ``mode`` in the answer is the mode that WILL run for the caller: the
    requested one, or - when ``deduped`` - the (equal or stronger) in-flight
    job's mode. A weaker in-flight job does not satisfy a stronger request: the
    new job is queued behind it (``behind`` names it).
    """
    sym = (ticker or "").upper().strip()
    if not sym:
        raise ValueError("ticker required")
    if mode not in MODES:
        raise ValueError(f"unknown analysis mode {mode!r}")
    covering = in_flight(sym, mode)
    if covering is not None:
        log.info("%s: %s request deduped onto in-flight %s job %s (%s)", sym,
                 mode, covering["mode"], covering["id"], covering["status"])
        return {"job_id": covering["id"], "deduped": True,
                "mode": covering["mode"], "status": covering["status"]}
    weaker = in_flight(sym)
    job = _new_job(sym, mode, source or "api")
    if weaker is not None:
        job["message"] = f"queued behind {weaker['mode']} run {weaker['id']}"
    _jobs[job["id"]] = job
    persist()
    _queue.put_nowait(job["id"])
    log.info("%s: job %s queued (mode=%s source=%s%s)", sym, job["id"], mode,
             job["source"],
             f" behind {weaker['id']}" if weaker is not None else "")
    return {"job_id": job["id"], "deduped": False, "mode": mode,
            "status": "queued", "behind": weaker["id"] if weaker else None}


def cancel(job_id: str) -> bool:
    """Cancel a QUEUED job only. Running spines cannot be interrupted."""
    job = _jobs.get(job_id)
    if not job:
        return False
    if job.get("status") != "queued":
        log.info("job %s not cancellable (status=%s)", job_id, job.get("status"))
        return False
    _finish(job, "error", "cancelled by request")
    log.info("job %s cancelled while queued", job_id)
    return True


def set_runner(fn) -> None:
    """Register ``async fn(job: dict) -> None`` (``ta_pipeline.run_job``)."""
    global _runner
    _runner = fn


def _finish(job: dict, status: str, message: str = "") -> None:
    job["status"] = status if status in STATUSES else "error"
    job["message"] = str(message or "")[:MESSAGE_CHARS]
    job["finished_at"] = time.time()
    persist()


async def _execute(job_id: str) -> None:
    """One job's lifecycle, serialized by ``_sem``."""
    async with _sem:
        job = _jobs.get(job_id)
        if not job or job.get("status") != "queued":
            return                      # cancelled (or superseded) while queued
        if _runner is None:
            _finish(job, "error", "no analysis runner registered")
            log.error("job %s failed: no runner wired in app/main.py", job_id)
            return
        job["status"] = "running"
        job["message"] = "running"
        persist()
        started = time.time()
        try:
            await _runner(job)
        except asyncio.CancelledError:
            _finish(job, "error", REASON_SHUTDOWN)
            raise
        except Exception as e:
            log.exception("job %s raised", job_id)
            _finish(job, "error", f"{type(e).__name__}: {e}")
            return
        # The runner may declare failure itself (lane down, budget spent,
        # unparseable PM) by setting status/message instead of raising.
        if job.get("status") == "error":
            job["finished_at"] = job.get("finished_at") or time.time()
            persist()
            log.warning("job %s error: %s (%.0fs)", job_id,
                        str(job.get("message", ""))[:200], time.time() - started)
            return
        _finish(job, "done", job.get("message") or "done")
        log.info("job %s done (%.0fs): %s", job_id, time.time() - started,
                 str(job.get("decision") or "")[:120])


async def _consumer_loop() -> None:
    """Drain the queue; each job runs under the semaphore (serialized)."""
    while True:
        job_id = await _queue.get()
        task = asyncio.create_task(_execute(job_id))
        _workers.add(task)
        task.add_done_callback(_workers.discard)
        _queue.task_done()


def _mark_interrupted(reason: str) -> int:
    """Queued/running rows from a previous process can never finish."""
    n = 0
    for job in _jobs.values():
        if job.get("status") in IN_FLIGHT:
            _finish(job, "error", reason)
            n += 1
    return n


def take_interrupted(source: str, max_age_s: float) -> list[dict]:
    """Jobs of ``source`` that a restart/shutdown cut off within the last
    ``max_age_s`` seconds, newest per ticker, each handed out ONCE (the row is
    flagged ``resumed`` so a second restart does not re-run it again)."""
    cutoff = time.time() - max_age_s
    picked: dict[str, dict] = {}
    for job in sorted(_jobs.values(), key=lambda j: j.get("created_at", 0),
                      reverse=True):
        if (job.get("source") != source or job.get("status") != "error"
                or job.get("message") not in INTERRUPTED
                or job.get("resumed")
                or job.get("created_at", 0) < cutoff):
            continue
        job["resumed"] = True
        picked.setdefault(job.get("ticker", ""), dict(job))
    if picked:
        persist()
    return list(picked.values())


def start() -> None:
    """Worker contract: load the table, settle orphans, start the consumer."""
    global _consumer
    for row in _load_disk():
        _jobs[str(row["id"])] = row
    n = _mark_interrupted(REASON_RESTART)
    if n:
        log.info("settled %d job(s) left queued/running by the restart", n)
    if _consumer is None or _consumer.done():
        _consumer = asyncio.create_task(_consumer_loop())
    log.info("analysis job queue started (%d job(s) in the table, runner=%s)",
             len(_jobs), "wired" if _runner else "MISSING")


async def stop() -> None:
    """Worker contract: stop consuming, drop queued work, settle running rows."""
    global _consumer
    if _consumer and not _consumer.done():
        _consumer.cancel()
        try:
            await _consumer
        except asyncio.CancelledError:
            pass
    _consumer = None
    for task in list(_workers):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    _workers.clear()
    _mark_interrupted(REASON_SHUTDOWN)


def status() -> dict:
    """``/api/background`` row for the queue."""
    counts = {s: 0 for s in STATUSES}
    for job in _jobs.values():
        # A table restored from disk can carry a status this build does not
        # know (an older spelling, a hand-edited file): count it as error
        # rather than crashing /api/background.
        state = job.get("status") if job.get("status") in STATUSES else "error"
        counts[state] += 1
    return {"running": bool(_consumer and not _consumer.done()),
            "runner": _runner is not None,
            "queued": counts["queued"],
            "running_jobs": counts["running"],
            "done": counts["done"],
            "errors": counts["error"],
            "total": len(_jobs)}
