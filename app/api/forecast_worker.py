"""Forecaster worker: the weekly cohort, the daily B run, labels and the trial ledger
(phase 6 contract section 3, docs/plans/forecaster.md).

Schedule (local container time, XETR sessions):
* weekly cohort ``w<as-of>``: every candidate of ``paper_universe``; packets frozen at the close of the
  week's LAST session (Friday, Thursday before a holiday Friday); forecasts run over the weekend; deadline
  = 08:00 on the next session day (Monday). Local lane first; a busy lane is retried with backoff until the
  deadline, then the unfinished names fall back (see ``forecasts``). Arms R Q A B D E.
* daily cohort ``d<as-of>``: after every other session close, the practice buckets' held names (priority)
  plus the top 30 candidates of the newest weekly cohort (by B p 20d) get arm B for 5d / 20d (role
  ``daily``, graded apart); deadline 08:00 on the next session day, held names alone fall back.
* a cohort is created only while it is still prospective (before its deadline): after that it is recorded
  ``missed`` and never filled in later (forecasting a week that already moved is hindsight).
* labels (``forecasts.update_labels``) at most hourly; the trial ledger every pass (a row only on change).

One job (cohort run) at a time runs as its own task, so the loop keeps ticking, ``runlog`` records progress
and a lane outage of hours does not look like a stalled worker. A restart resumes unfinished cohorts: the
log is the state (rows already written are never redone).

Gate: ``FORECAST_ENABLED=1`` (read at ``start()``).
"""
import asyncio
import datetime
import logging
import os
import time

from app.api import calendars, forecast_packets as packets, forecasts, paper_buckets, paper_ledger, paper_universe, runlog

log = logging.getLogger(__name__)

MIC = "XETR"
POLL_S = int(os.getenv("FORECAST_POLL_S", "300"))
LABEL_EVERY_S = 3600
DEADLINE_AT = datetime.time(8, 0)       # local, on the next session day
DAILY_TOP = 30
MAX_ATTEMPTS = 5                        # consecutive crashed runs before a cohort is marked failed
WEEKLY_ARMS = list(forecasts.ARMS)
DAILY_ARMS = ["B"]

_task: asyncio.Task | None = None
_stats = {"running": False, "enabled": False, "last_run": 0.0, "runs": 0, "errors": 0, "lastError": None}
_job: dict = {"task": None, "cohort": None, "kind": None, "startedAt": None, "progress": {}, "lastResult": None}
_labels = {"at": 0.0, "last": None}


class RunRefused(Exception):
    """A manual run that cannot start; ``code`` is ``disabled`` (503) or ``refused`` (409)."""

    def __init__(self, reason: str, code: str = "refused"):
        super().__init__(reason)
        self.code = code


def _now() -> float:
    """The worker clock (tests monkeypatch it)."""
    return time.time()


def enabled() -> bool:
    return os.getenv("FORECAST_ENABLED", "0") == "1"


# ------------------------------------------------------------------ schedule

def deadline_for(as_of: datetime.date) -> float:
    """08:00 local on the first XETR session after ``as_of`` (Monday for a Friday as-of)."""
    nxt = calendars.next_session(MIC, as_of)
    return datetime.datetime.combine(nxt, DEADLINE_AT).timestamp()


def last_session_of_week(day: datetime.date) -> datetime.date:
    monday = day - datetime.timedelta(days=day.weekday())
    return max(calendars.sessions_between(MIC, monday, monday + datetime.timedelta(days=6)))


def weekly_due(now: float) -> datetime.date | None:
    """The as-of date of a weekly cohort that may exist now: the newest completed session when it is the
    last session of its ISO week (so from Friday's close, through the weekend, to the next deadline)."""
    s = packets.last_completed_session(now)
    return s if s == last_session_of_week(s) else None


def held_ids() -> list[str]:
    """Ids the practice buckets hold according to their own ledger fills (test seam)."""
    rows = paper_ledger.read_orders()
    held: set[str] = set()
    for b in paper_buckets.ids():
        held.update(paper_ledger.ledger_holdings(paper_ledger.bucket_rows(rows, b)))
    return sorted(held)


def top_candidates(n: int = DAILY_TOP) -> list[str]:
    """The n candidates with the highest local B p(outperform 20d) of the newest weekly cohort."""
    weekly = [c for c in forecasts.read_cohorts().values() if c.get("kind") == "weekly"]
    if not weekly:
        return []
    cid = max(weekly, key=lambda c: c["asOf"])["id"]
    best: dict[str, float] = {}
    for r in forecasts.read_log():
        if (r.get("cohort") == cid and r.get("kind") == "aggregate" and r.get("arm") == "B" and r.get("horizon") == 20
                and r.get("source") == forecasts.LOCAL and r.get("p") is not None):
            best[r["id"]] = r["p"]
    return [i for i, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def daily_ids() -> tuple[list[str], list[str]]:
    """(ids to forecast, priority ids): held names first, then the top candidates; only ids the registry knows."""
    held = [p for p in held_ids() if paper_universe.get(p)]
    ids = list(dict.fromkeys(held + top_candidates()))
    return ids, held


def _new_record(kind: str, as_of: datetime.date, now: float) -> dict:
    if kind == "weekly":
        cands = paper_universe.ids("candidate")
        return {"id": forecasts.weekly_id(as_of), "kind": "weekly", "asOf": as_of.isoformat(), "ids": cands,
                "subset": forecasts.ensure_subset(cands), "arms": WEEKLY_ARMS, "horizons": list(forecasts.HORIZONS),
                "priority": [], "deadline": deadline_for(as_of), "createdAt": now, "status": "running", "attempts": 0}
    ids, held = daily_ids()
    return {"id": forecasts.daily_id(as_of), "kind": "daily", "asOf": as_of.isoformat(), "ids": ids, "arms": DAILY_ARMS,
            "horizons": list(forecasts.HORIZONS), "priority": held, "deadline": deadline_for(as_of), "createdAt": now,
            "status": "running", "attempts": 0}


# ------------------------------------------------------------------ the job

def job_running() -> bool:
    t = _job["task"]
    return t is not None and not t.done()


async def _job_main(rec: dict) -> None:
    try:
        res = await forecasts.run(rec["ids"], arms=tuple(rec["arms"]), horizons=tuple(rec["horizons"]), cohort=rec["id"],
                                  role=forecasts.cohort_role(rec["id"]),
                                  as_of=datetime.date.fromisoformat(rec["asOf"]), deadline=rec["deadline"],
                                  priority=rec.get("priority") or (), progress=_job["progress"])
        rec["attempts"] = 0
        rec["result"] = {k: res[k] for k in ("samples", "pending", "pendingD", "invalidPool", "packetFailed", "complete")}
        rec["result"]["fallback"] = {k: len(v) for k, v in res["fallback"].items()}
        rec["result"]["d"] = {k: len(v) for k, v in res["d"].items()}
        rec["status"] = "done" if res["complete"] else ("partial" if _now() >= rec["deadline"] else "running")
        _job["lastResult"] = {"cohort": rec["id"], "status": rec["status"], "complete": res["complete"]}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["lastError"] = f"{type(e).__name__}: {str(e)[:150]}"
        if rec["attempts"] >= MAX_ATTEMPTS:
            rec["status"] = "failed"
        _stats["errors"] += 1
        _stats["lastError"] = rec["lastError"]
        log.exception("forecast job %s failed", rec["id"])
    finally:
        forecasts.put_cohort(rec)
        _job["task"] = None


def _start_job(rec: dict) -> None:
    _job.update(cohort=rec["id"], kind=rec["kind"], startedAt=_now(), progress={})
    _job["task"] = asyncio.create_task(_job_main(rec))


def _pick(now: float) -> tuple[dict | None, str]:
    """The next job to start (a new or resumed cohort record) and a note, or (None, note)."""
    cohorts = forecasts.read_cohorts()
    running = sorted((c for c in cohorts.values() if c.get("status") == "running"),
                     key=lambda c: (c.get("kind") != "weekly", c["asOf"]))
    if running:
        return running[0], f"resume {running[0]['id']}"
    due = weekly_due(now)
    if due is not None:
        cid = forecasts.weekly_id(due)
        if cid not in cohorts:
            rec = _new_record("weekly", due, now)
            if now >= rec["deadline"]:
                rec.update(status="missed", ids=[], note="the cohort window closed before it started")
                forecasts.put_cohort(rec)
                return None, f"{cid} missed"
            forecasts.put_cohort(rec)
            return rec, f"start {cid}"
    as_of = packets.last_completed_session(now)
    if due == as_of:
        return None, "idle"                       # the weekly cohort covers this session
    cid = forecasts.daily_id(as_of)
    if cid in cohorts:
        return None, "idle"
    rec = _new_record("daily", as_of, now)
    if not rec["ids"]:
        return None, "idle: no daily ids yet"
    if now >= rec["deadline"]:
        rec.update(status="missed", ids=[], note="the daily window closed before it started")
        forecasts.put_cohort(rec)
        return None, f"{cid} missed"
    forecasts.put_cohort(rec)
    return rec, f"start {cid}"


async def request_run(kind: str, now: float | None = None) -> dict:
    """Manual trigger (``POST /api/paper/forecast-run``): start (or resume) the newest cohort of ``kind`` in the
    background and return at once. Raises :class:`RunRefused`: disabled, a job already running, a weekly
    cohort outside its window (it exists only at the last session's close of a week, before its deadline), or
    an already finished cohort / nothing to forecast."""
    now = _now() if now is None else now
    if not enabled():
        raise RunRefused("forecaster disabled (FORECAST_ENABLED=1)", "disabled")
    if job_running():
        raise RunRefused(f"a run is already in progress ({_job['cohort']})")
    cohorts = forecasts.read_cohorts()
    as_of = packets.last_completed_session(now)
    if kind == "cohort":
        due = weekly_due(now)
        if due is None:
            raise RunRefused("weekly cohorts are frozen at the close of the week's last session; none is due")
        cid = forecasts.weekly_id(due)
        rec = cohorts.get(cid)
        if rec is None:
            rec = _new_record("weekly", due, now)
            if now >= rec["deadline"]:
                raise RunRefused(f"{cid}: the cohort window closed (deadline passed); forecasting it now would be hindsight")
        elif rec.get("status") in ("done", "missed", "partial"):
            raise RunRefused(f"{cid} is {rec['status']}")
        else:
            rec["status"], rec["attempts"] = "running", 0
    elif kind == "daily":
        cid = forecasts.daily_id(as_of)
        rec = cohorts.get(cid)
        if rec is None:
            rec = _new_record("daily", as_of, now)
            if not rec["ids"]:
                raise RunRefused("nothing to forecast yet (no held names and no weekly cohort)")
            if now >= rec["deadline"]:
                raise RunRefused(f"{cid}: the daily window closed (deadline passed)")
        elif rec.get("status") in ("done", "missed", "partial"):
            raise RunRefused(f"{cid} is {rec['status']}")
        else:
            rec["status"], rec["attempts"] = "running", 0
    else:
        raise RunRefused("kind must be 'cohort' or 'daily'")
    forecasts.put_cohort(rec)
    _start_job(rec)
    return {"started": True, "kind": kind, "cohort": rec["id"], "asOf": rec["asOf"], "ids": len(rec["ids"]),
            "deadline": rec["deadline"]}


# ------------------------------------------------------------------ pass

async def _tick() -> tuple[bool, str, bool]:
    """One supervisor pass -> (ok, note, acted). Never raises."""
    now = _now()
    ok, acted, notes = True, False, []
    try:
        cands = paper_universe.ids("candidate")
        forecasts.ensure_subset(cands)
        if forecasts.record_config(now, cands)["changed"]:
            notes.append("trial ledger: config changed")
            acted = True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ok = False
        notes.append(f"ledger: {type(e).__name__}")
        log.exception("forecast ledger failed")
    try:
        if job_running():
            p = _job["progress"]
            notes.append(f"{_job['cohort']}: {p.get('phase', '?')} {p.get('done', 0)}/{p.get('total', 0)}"
                         + (f" (lane: {p['laneWaiting']})" if p.get("laneWaiting") else ""))
        else:
            rec, note = _pick(now)
            notes.append(note)
            if rec is not None:
                _start_job(rec)
                acted = True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ok = False
        notes.append(f"schedule: {type(e).__name__}")
        log.exception("forecast scheduling failed")
    if now - _labels["at"] >= LABEL_EVERY_S:
        _labels["at"] = now
        try:
            _labels["last"] = await forecasts.update_labels(now)
            if _labels["last"]["added"]:
                notes.append(f"labels +{_labels['last']['added']}")
                acted = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ok = False
            notes.append(f"labels: {type(e).__name__}")
            log.exception("forecast labels failed")
    return ok, "; ".join(n for n in notes if n), acted


async def _pass() -> bool:
    started = time.time()
    ok, note, acted = await _tick()
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    if not ok:
        _stats["errors"] += 1
    runlog.record("forecast_worker", ok, time.time() - started, note,
                  idle=(ok and not acted and not job_running() and note.startswith("idle")))
    return ok


async def _loop() -> None:
    log.info("forecast_worker loop started (poll %ss)", POLL_S)
    await _pass()
    while True:
        await asyncio.sleep(POLL_S)
        await _pass()


def start() -> None:
    global _task
    _stats["enabled"] = enabled()
    if not _stats["enabled"]:
        log.info("forecast_worker disabled (FORECAST_ENABLED unset)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True


async def stop() -> None:
    global _task
    _stats["running"] = False
    for t in (_task, _job["task"]):
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    _task = None
    _job["task"] = None


async def close() -> None:
    """No HTTP client of its own; main.py may await this at shutdown like the other workers."""
    return None


def status() -> dict:
    cohorts = forecasts.read_cohorts()
    return {**_stats, "enabled": enabled(), "interval_s": POLL_S,
            "job": {"running": job_running(), "cohort": _job["cohort"], "kind": _job["kind"],
                    "startedAt": _job["startedAt"], "progress": dict(_job["progress"]), "last": _job["lastResult"]},
            "cohorts": {c: v.get("status") for c, v in sorted(cohorts.items())[-8:]},
            "labels": _labels["last"]}
