"""Paper-trading background worker (plan §5, §6.1; contracts phase2 §3.4-3.6,
phase3 §3).

Worker contract (``skills/workers-and-alerts``): ``start()``/``async stop()``/
``async close()``/``status()``, gated by ``PAPER_TRADING_ENABLED=1`` AND the
demo broker being configured, listed in ``main.BACKGROUND_MODULES``.

Every pass runs ``paper_exec.run_pass`` FIRST (reconcile the non-terminal
order rows, cancel orders whose trading window is over or that a halt
covers, evaluate the kill switch, retry an unsent halt push) - so after a
restart the unresolved rows are reconciled before anything else - then
refreshes the broker's cached status (a cheap ``account()`` call updates its
``lastOkAt``/``lastError``), then the scheduled PM decision, then the daily
snapshot and the daily summary push. Once per LOCAL date, after the XETR
session close for that date (skip non-session days), a bucket snapshot is
persisted (``paper_eval.take_snapshot`` -> ``paper_ledger``); also one at
startup when today's row is missing (so a redeploy right after the close
still gets a row).

Phase 3 schedule (mode ``shadow`` or ``demo`` only, XETR session days only):

* **Decision**: ``paper_pm.decide(trigger="schedule")`` once per local date,
  when the Phase 2 trading window is open AND (today's digest batch has
  finished - ``digest.status()["last_batch"]["ts"]`` is today after 08:45
  local - OR local time is at/after 11:00 as the fallback). A deferral (gate,
  lane down, budget) is ``ok=True`` with a note ``deferred: ...`` and is
  retried at most ``DECIDE_MAX_ATTEMPTS`` times per date, ``DECIDE_RETRY_S``
  apart; a stored decision (valid or not) closes the date. ``decide`` itself
  refuses a second scheduled decision for the same date, so a restart cannot
  repeat it.
* **Summary**: after the XETR close and the day's snapshot, ONE push per
  session day (source ``paper``, priority 3, no model-written URLs). The
  ``notifiedDate`` is stored in ``data/paper_summary.json`` for every
  Delivery status except ``failed`` (retried next pass).

Cadence: ``ACTIVE_PASS_S`` (60 s) while any order row is non-terminal or a
trading window is open on a session day, else ``IDLE_PASS_S`` (300 s).
Phase 5: every enabled bucket (``paper_buckets``) gets its own snapshot at the
close and, in shadow/demo, its own briefing (``paper_ledger.save_brief``, built
from stored data only, no LLM); the daily summary push and the weekly report are
ONE push each covering every active bucket. The scheduled decision here is the
daily bucket's; the intraday (i2) schedule decides at ``intraday_slots`` (first
slot at the window open, then every ``intervalMin``, the last strictly before
the window end; a slot missed by more than half an interval is skipped, never
caught up; ``flatAtClose`` flattens at the last slot with no model call).
Phase 6: with ``universeSource: paper_universe`` the worker maps the registry
to Trading 212 tickers (``broker.instruments()`` + ``paper_universe.map_t212``)
at most once a day.
"""
import asyncio
import datetime
import logging
import os
import time

from app.api import broker as broker_mod
from app.api import (calendars, digest, jsonstore, notify, paper_buckets, paper_eval,
                     paper_exec, paper_ledger, paper_pm, paper_metrics, paper_universe,
                     runlog, textsafe)

log = logging.getLogger("paper_trader")

ACTIVE_PASS_S = 60
IDLE_PASS_S = 300
MIC = "XETR"          # the plan's snapshot time is pinned to the XETR close
MODES_ACTIVE = ("shadow", "demo")
DIGEST_DONE_AFTER = (8, 45)      # local (hour, minute): a digest batch ending after this is "today's"
FALLBACK_AFTER = (11, 0)         # local time at/after which the decision no longer waits for the digest
DECIDE_RETRY_S = 1800
DECIDE_MAX_ATTEMPTS = 3
SUMMARY_FILE = paper_ledger.DATA_DIR / "paper_summary.json"
REPORT_FILE = paper_ledger.DATA_DIR / "paper_reports.json"
MAP_FILE = paper_ledger.DATA_DIR / "paper_universe_map.json"
MAP_RETRY_S = 1800
INTRADAY_GRACE_S = 300

_task: asyncio.Task | None = None
_stats = {"running": False, "enabled": False, "last_run": 0.0, "runs": 0,
          "errors": 0, "lastSnapshotDate": None, "lastDecisionDate": None,
          "lastSummaryDate": None, "lastReportWeek": None, "lastBriefDate": None}
_sched = {"date": None, "attempts": 0, "lastAttemptAt": 0.0, "done": False}
_isched = {"date": None, "done": set()}


def _broker():
    return broker_mod.get_broker("demo")


def _now() -> float:
    """The worker clock for the schedule (tests monkeypatch it)."""
    return time.time()


def enabled() -> bool:
    return os.getenv("PAPER_TRADING_ENABLED", "0") == "1" and _broker() is not None


def _today_local() -> datetime.date:
    return datetime.datetime.now().date()


def snapshot_due(today: datetime.date, last_snapshot_date: str | None,
                 now: datetime.datetime | None = None, force: bool = False) -> bool:
    """True when today's bucket snapshot should be (re)taken: today is not
    already the last snapshot's date, today is an XETR session, and (unless
    ``force``, used at startup to catch up a missing row without waiting for
    the next scheduled pass) the session has closed."""
    if last_snapshot_date == today.isoformat():
        return False
    bounds = calendars.session_bounds(MIC, today)
    if bounds is None:
        return False
    if force:
        return True
    now = now or datetime.datetime.now(bounds[1].tzinfo)
    return now >= bounds[1]


def _active_buckets() -> list[str]:
    """The enabled buckets whose mode is shadow|demo, in bucket order: the ones
    the close-time summary, weekly report and briefing cover."""
    return [b for b in paper_eval.enabled_buckets()
            if paper_eval._paper_cfg(b)["mode"] in MODES_ACTIVE]


async def _maybe_snapshot(force: bool = False) -> bool:
    """One snapshot per enabled bucket per session day (each bucket has its own
    file and its own 'already taken' check). True when any was taken."""
    today = _today_local()
    took = False
    for bucket in paper_eval.enabled_buckets():
        last = paper_ledger.latest_snapshot(bucket)
        if not snapshot_due(today, (last or {}).get("date"), force=force):
            continue
        row = await paper_eval.take_snapshot(today.isoformat(), bucket)
        if row:
            _stats["lastSnapshotDate"] = row.get("date")
            took = True
    return took


# ------------------------------------------------------------ PM schedule

def digest_ready(now: float) -> bool:
    """Today's digest batch has finished (its ``last_batch.ts`` is today, at or
    after 08:45 local), or the 11:00 local fallback has been reached."""
    local = datetime.datetime.fromtimestamp(now)
    if (local.hour, local.minute) >= FALLBACK_AFTER:
        return True
    last = (digest.status() or {}).get("last_batch")
    try:
        ts = float(last["ts"]) if isinstance(last, dict) and last.get("ts") else None
    except (TypeError, ValueError):
        ts = None
    if ts is None:
        return False
    done = datetime.datetime.fromtimestamp(ts)
    return done.date() == local.date() and (done.hour, done.minute) >= DIGEST_DONE_AFTER


def _describe_decision(row: dict) -> str:
    if row.get("deferred"):
        return f"deferred: {row['deferred']}"
    if not row.get("valid"):
        return "invalid output, no orders"
    if row.get("noChange"):
        return "no change"
    n = len(row.get("rowIds") or [])
    return f"targets, {n} order intent(s)" + (f" ({row['error']})" if row.get("error") else "")


async def _maybe_decide() -> tuple[bool, str | None, bool]:
    """-> (ok, runlog note or None, acted). Never raises."""
    try:
        now = _now()
        if paper_eval._paper_cfg()["mode"] not in MODES_ACTIVE:
            return True, None, False
        today = datetime.datetime.fromtimestamp(now).date().isoformat()
        if _sched["date"] != today:
            _sched.update(date=today, attempts=0, lastAttemptAt=0.0, done=False)
        if _sched["done"] or not paper_exec.window_state(MIC, now)["open"]:
            return True, None, False
        if not digest_ready(now):
            return True, None, False
        if _sched["attempts"] >= DECIDE_MAX_ATTEMPTS or (
                _sched["attempts"] and now - _sched["lastAttemptAt"] < DECIDE_RETRY_S):
            return True, None, False
        _sched["attempts"] += 1
        _sched["lastAttemptAt"] = now
        res = await paper_pm.decide(trigger="schedule", now=now)
    except Exception as e:
        log.exception("paper_trader decision failed")
        return False, f"decision failed: {type(e).__name__}", True
    deferred = res.get("deferred")
    if deferred and deferred != "already decided today":
        return True, f"deferred: {deferred}", True      # a deferral is not a failure
    _sched["done"] = True
    _stats["lastDecisionDate"] = today
    if deferred:                                        # decided earlier today (restart): nothing to do
        return True, None, False
    return True, f"decision {_describe_decision(res)}", True


def intraday_slots(opens_at: float | None, closes_at: float | None,
                   interval_min: int) -> list[float]:
    """Epoch seconds of the decision slots of one session: the first at the
    trading-window open, then every ``interval_min``, all strictly before the
    window end. [] on a non-session day."""
    if opens_at is None or closes_at is None or interval_min <= 0:
        return []
    out, t = [], float(opens_at)
    while t < closes_at:
        out.append(t)
        t += interval_min * 60
    return out


def next_slot(cfg: dict, now: float | None = None) -> float | None:
    """The intraday bucket's next slot today (epoch s) or None (not active, no
    session, or no slot left)."""
    ts = _now() if now is None else now
    if not cfg.get("enabled") or cfg.get("mode") not in MODES_ACTIVE:
        return None
    w = paper_exec.window_state(MIC, ts)
    return next((s for s in intraday_slots(w["opensAt"], w["closesAt"], cfg.get("intervalMin") or 60)
                 if s > ts), None)


async def _maybe_decide_intraday() -> tuple[bool, str | None, bool]:
    """-> (ok, runlog note or None, acted). Never raises. One decision per slot:
    a slot is marked done BEFORE the call (a deferral or a failure is not retried
    - the next slot is the next chance, nothing is caught up)."""
    label = "?"
    try:
        now = _now()
        cfg = paper_eval._paper_cfg(paper_buckets.INTRADAY)
        if not cfg.get("enabled") or cfg["mode"] not in MODES_ACTIVE:
            return True, None, False
        w = paper_exec.window_state(MIC, now)
        if not w["open"]:
            return True, None, False
        slots = intraday_slots(w["opensAt"], w["closesAt"], cfg["intervalMin"])
        due = [s for s in slots if s <= now]
        if not due:
            return True, None, False
        slot = due[-1]
        last = slot == slots[-1]
        if not last and now - slot > max(INTRADAY_GRACE_S, cfg["intervalMin"] * 30):
            return True, None, False             # missed: never catch up
        today = datetime.datetime.fromtimestamp(now).date().isoformat()
        label = datetime.datetime.fromtimestamp(slot).strftime("%H:%M")
        if _isched["date"] != today:
            _isched.update(date=today, done=set())
        if label in _isched["done"]:
            return True, None, False
        _isched["done"].add(label)
        if any(r.get("date") == today and r.get("slot") == label
               for r in paper_pm.read_decisions(paper_buckets.INTRADAY)):
            return True, None, False             # decided before a restart
        res = await paper_pm.decide(trigger="schedule", now=now, bucket=paper_buckets.INTRADAY,
                                    slot=label, flat=bool(cfg.get("flatAtClose") and last))
    except Exception as e:
        log.exception("paper_trader intraday decision failed")
        return False, f"intraday {label} failed: {type(e).__name__}", True
    if res.get("deferred") and not res.get("decisionId"):
        return True, f"intraday {label} deferred: {res['deferred']}", True
    return True, f"intraday {label} {_describe_decision(res)}", True


async def _maybe_map_t212(brk) -> tuple[bool, str | None, bool]:
    """Open universe only: map the registry to Trading 212 tickers at most once
    a day (a failed attempt is retried after ``MAP_RETRY_S``). -> (ok, note or
    None, acted). Never raises."""
    try:
        if paper_eval._paper_cfg().get("universeSource") != paper_buckets.PAPER_UNIVERSE:
            return True, None, False
        now = _now()
        today = datetime.datetime.fromtimestamp(now).date().isoformat()
        state = jsonstore.load(MAP_FILE, {})
        state = state if isinstance(state, dict) else {}
        if state.get("date") == today or now - float(state.get("attemptAt") or 0) < MAP_RETRY_S:
            return True, None, False
        instruments = await brk.instruments()
        if broker_mod.is_error(instruments):
            jsonstore.save(MAP_FILE, {**state, "attemptAt": now, "error": instruments["error"]})
            return True, f"t212 mapping: {instruments['error']}", True
        res = await paper_universe.map_t212(instruments)
        jsonstore.save(MAP_FILE, {"date": today if res["saved"] else None, "attemptAt": now,
                                  "mapped": len(res["mapped"]), "unmapped": len(res["unmapped"]),
                                  "saved": res["saved"]})
        return True, f"t212 mapping {len(res['mapped'])} mapped, {len(res['unmapped'])} not", True
    except Exception as e:
        log.exception("paper_trader t212 mapping failed")
        return False, f"t212 mapping failed: {type(e).__name__}", True


# ----------------------------------------------------------------- summary

def _decision_line(date: str, bucket: str = paper_buckets.DAILY) -> str:
    rows = [r for r in paper_pm.read_decisions(bucket) if r.get("date") == date]
    if bucket != paper_buckets.DAILY:          # many decisions a day: counts, not targets
        c = paper_metrics.decision_counts(rows)
        return (f"Decisions: {c['valid']} valid, {c['noChange']} no change, "
                f"{c['invalid']} invalid, {c['deferred']} deferred")
    decided = [r for r in rows if not r.get("deferred")]
    row = (decided or rows or [None])[-1]
    if row is None:
        return "Decision: none today"
    if row.get("deferred") or not row.get("valid") or row.get("noChange"):
        return "Decision: " + _describe_decision(row)
    out = row.get("output") or {}
    parts = [f"{pid} {float(v):g}%" for pid, v in (out.get("targets") or {}).items()]
    cash = out.get("cash_pct")
    if cash is not None:
        parts.append(f"cash {float(cash):g}%")
    return "Decision: targets " + ", ".join(parts)


def _orders_line(date: str, bucket: str = paper_buckets.DAILY) -> str:
    rows = [r for r in paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
            if str(r.get("intentId") or "").startswith(date + "-")]
    if not rows:
        return "Orders: none"
    shadow = sum(1 for r in rows if r.get("state") == "shadow")
    placed = sum(1 for r in rows if r.get("state") in
                 ("pending", "submitted", "unknown", "partially_filled", "filled", "cancelled"))
    filled = sum(1 for r in rows if (r.get("filledQuantity") or 0) > 0)
    if shadow and not placed:
        return f"Orders: {shadow} shadow (not sent)"
    return f"Orders: {placed} placed, {filled} filled" + (f", {shadow} shadow" if shadow else "")


async def _summary_snapshot(date: str, close_ts: float,
                            bucket: str = paper_buckets.DAILY) -> dict | None:
    """Today's snapshot of the bucket, taken at or after the close (a startup
    snapshot taken before it is retaken so the summary reports the closing equity)."""
    snap = paper_ledger.snapshot_on(date, bucket)
    if snap is None or (snap.get("takenAt") or 0) < close_ts:
        snap = await paper_eval.take_snapshot(date, bucket)
    return snap


async def _figures(cfg: dict, snap: dict, date: str, bucket: str) -> dict:
    """Equity, day change and the since-inception comparison with buy & hold
    for one bucket (None where unknown). Pure numbers: nothing model-written."""
    equity = float(snap.get("equityEur") or 0.0)
    prior = [r for r in paper_ledger.read_snapshots(bucket)
             if r.get("date") and r["date"] < date and (r.get("equityEur") or 0) > 0]
    prev = max(prior, key=lambda r: r["date"]) if prior else None
    day_delta = day_pct = None
    if prev:
        day_delta = equity - float(prev["equityEur"])
        day_pct = day_delta / float(prev["equityEur"]) * 100.0
    budget = float(cfg["budgetEur"])
    bucket_pct = (equity / budget - 1.0) * 100.0 if budget > 0 and equity > 0 else None
    buy_hold = None
    season = paper_ledger.read_season(bucket)
    if season is not None:
        try:
            quotes = await paper_eval._quote_universe(cfg["universe"])
            buy_hold = await paper_exec._buy_hold_pct(season, quotes)
        except Exception as e:
            log.warning("paper_trader figures: buy-hold failed: %s", type(e).__name__)
    return {"equityEur": equity, "dayDeltaEur": day_delta, "dayPct": day_pct,
            "sinceInceptionPct": bucket_pct, "buyHoldPct": buy_hold,
            "excessPts": (bucket_pct - buy_hold
                          if bucket_pct is not None and buy_hold is not None else None)}


def _halt_line(bucket: str) -> str:
    halt = paper_ledger.read_halt(bucket)
    return ("HALTED: " + (textsafe.clean_text(halt.get("reason"), 100) or "halted")
            if halt.get("halted") else "Halt: none")


async def _summary_body(cfg: dict, snap: dict, date: str,
                        bucket: str = paper_buckets.DAILY) -> str:
    f = await _figures(cfg, snap, date, bucket)
    line1 = f"Equity \u20ac{f['equityEur']:,.2f}"
    if f["dayPct"] is not None:
        line1 += f", day {f['dayPct']:+.2f}% ({f['dayDeltaEur']:+,.2f})"
    else:
        line1 += ", day change n/a"
    if f["sinceInceptionPct"] is None:
        line2 = "Since inception: n/a"
    elif f["buyHoldPct"] is None:
        line2 = f"Since inception: bucket {f['sinceInceptionPct']:+.2f}%, buy&hold n/a"
    else:
        line2 = (f"Since inception: bucket {f['sinceInceptionPct']:+.2f}% vs buy&hold "
                 f"{f['buyHoldPct']:+.2f}% ({f['excessPts']:+.2f} pts)")
    lines = [line1, line2, _decision_line(date, bucket), _orders_line(date, bucket),
             _halt_line(bucket)]
    return "\n".join(textsafe.clean_text(ln, 140) for ln in lines)


def _combined(parts: dict) -> str:
    """One push body out of the per-bucket bodies ({bucket: body}): the bare
    body for the daily bucket alone (the pre-phase-5 text), else one titled
    block per bucket."""
    if list(parts) == [paper_buckets.DAILY]:
        return parts[paper_buckets.DAILY]
    return "\n".join(f"[{b}]\n{body}" for b, body in parts.items())


def _push_title(prefix: str, buckets: list[str], tail: str) -> str:
    if buckets == [paper_buckets.DAILY]:
        return f"{prefix} bucket {tail}" if prefix == "Practice" else f"{prefix} {tail}"
    if len(buckets) == 1:
        return f"{prefix} {buckets[0]} bucket {tail}" if prefix == "Practice" else f"{prefix} {buckets[0]} {tail}"
    return f"{prefix} buckets {tail}" if prefix == "Practice" else f"{prefix} {tail}"


async def _maybe_summary() -> tuple[bool, str | None, bool]:
    """ONE summary push per session day covering every active bucket, after the
    close and the snapshots. -> (ok, note or None, acted). Never raises."""
    try:
        now = _now()
        buckets = _active_buckets()
        if not buckets:
            return True, None, False
        day = datetime.datetime.fromtimestamp(now).date()
        bounds = calendars.session_bounds(MIC, day)
        if bounds is None or now < bounds[1].timestamp():
            return True, None, False
        date = day.isoformat()
        state = jsonstore.load(SUMMARY_FILE, {})
        if isinstance(state, dict) and state.get("notifiedDate") == date:
            return True, None, False
        parts = {}
        for b in buckets:
            snap = await _summary_snapshot(date, bounds[1].timestamp(), b)
            if snap is None:
                return True, None, False                # broker unreachable: next pass
            parts[b] = await _summary_body(paper_eval._paper_cfg(b), snap, date, b)
        d = await notify.alert("PAPER", "paper", _push_title("Practice", buckets, date),
                               _combined(parts), priority=3,
                               dedupe_key=f"paper_summary:{date}", dedupe_s=86400)
    except Exception as e:
        log.exception("paper_trader summary failed")
        return False, f"summary failed: {type(e).__name__}", False
    if not d.consumed:                                  # failed: retried next pass
        return False, "summary push failed", False
    jsonstore.save(SUMMARY_FILE, {"notifiedDate": date, "status": d.status, "at": now})
    _stats["lastSummaryDate"] = date
    return True, f"summary {d.status}", True


# ------------------------------------------------------------ weekly report

def iso_week(day: datetime.date) -> str:
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def is_last_session_of_week(day: datetime.date) -> bool:
    """True when ``day`` is an XETR session and no later XETR session exists
    in its ISO week (Good Friday weeks end on Thursday)."""
    if calendars.session_bounds(MIC, day) is None:
        return False
    d = day + datetime.timedelta(days=1)
    while d.isocalendar()[1] == day.isocalendar()[1]:
        if calendars.session_bounds(MIC, d) is not None:
            return False
        d += datetime.timedelta(days=1)
    return True


def _at_or_before(series: dict, date: str) -> float | None:
    keys = [k for k in series if k <= date]
    return series[max(keys)] if keys else None


def _pp(v) -> str:
    return "n/a" if v is None else f"{float(v):+.1f}"


async def _report_body(snap: dict, day: datetime.date,
                       bucket: str = paper_buckets.DAILY) -> str:
    date = day.isoformat()
    monday = (day - datetime.timedelta(days=day.weekday())).isoformat()
    equity = float(snap.get("equityEur") or 0.0)
    season = paper_ledger.read_season(bucket) or {}
    snaps = paper_ledger.read_snapshots(bucket)
    prior = [r for r in snaps if r.get("date") and r["date"] < monday
             and (r.get("equityEur") or 0) > 0]
    base = max(prior, key=lambda r: r["date"]) if prior else None
    factors: dict = {}
    try:
        factors = await paper_eval.buy_hold_factors(season) if season else {}
    except Exception as e:
        log.warning("paper_trader report: buy-hold failed: %s", type(e).__name__)
    if base is None:
        line1 = "Week: no prior snapshot, n/a"
    else:
        wk = (equity / float(base["equityEur"]) - 1.0) * 100.0
        f0, f1 = _at_or_before(factors, base["date"]), _at_or_before(factors, date)
        if f0 and f1:
            bh = (f1 / f0 - 1.0) * 100.0
            line1 = f"Week: bucket {wk:+.2f}% vs buy&hold {bh:+.2f}% ({wk - bh:+.2f} pts)"
        else:
            line1 = f"Week: bucket {wk:+.2f}%, buy&hold n/a"
    metrics = (await paper_eval.performance_metrics(bucket))["metrics"]
    if not metrics:
        line2, line5 = "Since inception: metrics n/a", "Cost drag: n/a"
    else:
        ci = metrics.get("excessCI95")
        ci_txt = f" CI {_pp(ci[0])} .. {_pp(ci[1])} pp" if ci else ""
        line2 = (f"Since inception: excess {_pp(metrics.get('excessPct'))} pp{ci_txt}, "
                 f"{metrics.get('verdict')} ({metrics.get('sessions')} sessions)")
        drag = metrics.get("costDragPct")
        line5 = "Cost drag: n/a" if drag is None else f"Cost drag {float(drag):.2f}% of equity"
    rows = [r for r in paper_pm.read_decisions(bucket)
            if monday <= str(r.get("date") or "") <= date]
    c = paper_metrics.decision_counts(rows)
    line3 = (f"Decisions: {c['valid']} valid, {c['noChange']} no change, "
             f"{c['invalid']} invalid, {c['deferred']} deferred")
    filled = sum(1 for r in paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
                 if monday <= str(r.get("intentId") or "")[:10] <= date
                 and (r.get("filledQuantity") or 0) > 0)
    line4 = f"Orders filled: {filled}"
    lines = [line1, line2, line3, line4, line5, _halt_line(bucket)]
    return "\n".join(textsafe.clean_text(ln, 140) for ln in lines)


async def _maybe_weekly_report() -> tuple[bool, str | None, bool]:
    """ONE push on the last XETR session of each ISO week covering every active
    bucket, after the close and the day's snapshots. -> (ok, note or None,
    acted). Never raises."""
    try:
        now = _now()
        buckets = _active_buckets()
        if not buckets:
            return True, None, False
        day = datetime.datetime.fromtimestamp(now).date()
        if not is_last_session_of_week(day):
            return True, None, False
        bounds = calendars.session_bounds(MIC, day)
        if now < bounds[1].timestamp():
            return True, None, False
        week = iso_week(day)
        state = jsonstore.load(REPORT_FILE, {})
        if isinstance(state, dict) and state.get("notifiedWeek") == week:
            return True, None, False
        parts = {}
        for b in buckets:
            snap = await _summary_snapshot(day.isoformat(), bounds[1].timestamp(), b)
            if snap is None:
                return True, None, False                # broker unreachable: next pass
            parts[b] = await _report_body(snap, day, b)
        d = await notify.alert("PAPER", "paper", _push_title("Practice week", buckets, week),
                               _combined(parts), priority=3,
                               dedupe_key=f"paper_report:{week}", dedupe_s=7 * 86400)
    except Exception as e:
        log.exception("paper_trader weekly report failed")
        return False, f"weekly report failed: {type(e).__name__}", False
    if not d.consumed:                                  # failed: retried next pass
        return False, "weekly report push failed", False
    jsonstore.save(REPORT_FILE, {"notifiedWeek": week, "status": d.status, "at": now})
    _stats["lastReportWeek"] = week
    return True, f"weekly report {d.status}", True


# ---------------------------------------------------------------- briefings

def _brief_decision(row: dict) -> dict:
    out = row.get("output") or {}
    return {"decisionId": row.get("decisionId"), "slot": row.get("slot"),
            "createdAt": row.get("createdAt"), "trigger": row.get("trigger"),
            "valid": bool(row.get("valid")), "noChange": bool(row.get("noChange")),
            "deferred": row.get("deferred"),
            "error": textsafe.clean_text(row.get("error"), 200) or None,
            "targets": out.get("targets") or {}, "cashPct": out.get("cash_pct"),
            "confidence": out.get("confidence"),
            "rationale": textsafe.clean_text(out.get("rationale"), 600) or None}


def _brief_orders(date: str, bucket: str) -> dict:
    rows = [r for r in paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
            if str(r.get("intentId") or "").startswith(date + "-")]
    return {
        "count": len(rows),
        "shadow": sum(1 for r in rows if r.get("state") == "shadow"),
        "filled": sum(1 for r in rows if (r.get("filledQuantity") or 0) > 0),
        "filledValueEur": round(sum(paper_ledger.fill_value(r) for r in rows), 2),
        "items": [{"intentId": r.get("intentId"), "id": r.get("id"), "side": r.get("side"),
                   "quantity": r.get("quantity"), "limitPrice": r.get("limitPrice"),
                   "state": r.get("state"), "filledQuantity": r.get("filledQuantity"),
                   "avgFillPrice": r.get("avgFillPrice")} for r in rows],
    }


def _brief_text(date: str, bucket: str, figures: dict, decisions: list, orders: dict) -> str:
    """The manager's note: plain sentences from stored numbers only (the model's
    own rationale sits in ``decisions``, never in this text)."""
    c = paper_metrics.decision_counts(decisions)
    eq = f"Equity \u20ac{figures['equityEur']:,.2f}"
    if figures["dayPct"] is not None:
        eq += f" ({figures['dayPct']:+.2f}% on the day)"
    if figures["sinceInceptionPct"] is not None and figures["buyHoldPct"] is not None:
        eq += (f"; since inception {figures['sinceInceptionPct']:+.2f}% vs buy & hold "
               f"{figures['buyHoldPct']:+.2f}% ({figures['excessPts']:+.2f} pts)")
    elif figures["sinceInceptionPct"] is not None:
        eq += f"; since inception {figures['sinceInceptionPct']:+.2f}% (buy & hold n/a)"
    dec = (f"{c['valid']} decision(s) with targets, {c['noChange']} no change, "
           f"{c['invalid']} invalid, {c['deferred']} deferred")
    if orders["count"]:
        od = f"{orders['count']} order(s): {orders['filled']} filled (EUR {orders['filledValueEur']:,.2f})"
        if orders["shadow"]:
            od += f", {orders['shadow']} shadow"
    else:
        od = "no orders"
    return f"{bucket} {date}: {eq}. {dec}. {od}. {_halt_line(bucket)}."


async def _build_brief(cfg: dict, snap: dict, date: str, bucket: str, now: float) -> dict:
    figures = await _figures(cfg, snap, date, bucket)
    decisions = [_brief_decision(r) for r in paper_pm.read_decisions(bucket)
                 if r.get("date") == date]
    orders = _brief_orders(date, bucket)
    return {"bucket": bucket, "date": date, "createdAt": now,
            "strategyVersion": cfg["strategyVersion"], "mode": cfg["mode"],
            "text": textsafe.clean_text(_brief_text(date, bucket, figures, decisions, orders), 800),
            "decisions": decisions, "orders": orders,
            "equity": {k: (None if v is None else round(v, 4)) for k, v in figures.items()}}


async def _maybe_briefs() -> tuple[bool, str | None, bool]:
    """After the XETR close and the bucket's snapshot, ONE briefing per active
    bucket and session day (``paper_briefs[.intraday].json``), built from stored
    data only - no LLM call. -> (ok, note or None, acted). Never raises."""
    built: list[str] = []
    try:
        now = _now()
        buckets = _active_buckets()
        if not buckets:
            return True, None, False
        day = datetime.datetime.fromtimestamp(now).date()
        bounds = calendars.session_bounds(MIC, day)
        if bounds is None or now < bounds[1].timestamp():
            return True, None, False
        date = day.isoformat()
        for b in buckets:
            if any(r.get("date") == date for r in paper_ledger.read_briefs(b)):
                continue
            snap = await _summary_snapshot(date, bounds[1].timestamp(), b)
            if snap is None:
                continue                                # broker unreachable: next pass
            row = await _build_brief(paper_eval._paper_cfg(b), snap, date, b, now)
            if paper_ledger.save_brief(row, b):
                built.append(b)
    except Exception as e:
        log.exception("paper_trader briefing failed")
        return False, f"briefing failed: {type(e).__name__}", False
    if not built:
        return True, None, False
    _stats["lastBriefDate"] = date
    return True, "briefing " + ", ".join(built), True


def next_delay() -> int:
    """Seconds until the next pass (§3.6). A failure to decide errs towards
    the short interval: an open order must not wait five minutes."""
    try:
        return ACTIVE_PASS_S if paper_exec.needs_fast_pass() else IDLE_PASS_S
    except Exception:
        log.exception("paper_trader cadence check failed")
        return ACTIVE_PASS_S


async def _pass(force_snapshot: bool = False) -> bool:
    started = time.time()
    ok = True
    note = ""
    took = False
    worked = False
    acted = False
    active = 0
    extra: list[str] = []
    try:
        brk = _broker()
        if brk is not None:
            res = await paper_exec.run_pass()
            active = res["active"]
            worked = bool(active or res["cancelled"] or res["errors"] or res["halted"])
            if res["errors"]:
                ok = False
                note = "exec: " + ",".join(res["errors"][:3])
            st = await brk.account()
            if broker_mod.is_error(st) and not note:
                note = f"broker status: {st.get('error')}"
            m_ok, m_note, m_acted = await _maybe_map_t212(brk)
            ok, acted = ok and m_ok, acted or m_acted
            if m_note:
                extra.append(m_note)
            d_ok, d_note, d_acted = await _maybe_decide()
            ok, acted = ok and d_ok, acted or d_acted
            if d_note:
                extra.append(d_note)
            i_ok, i_note, i_acted = await _maybe_decide_intraday()
            ok, acted = ok and i_ok, acted or i_acted
            if i_note:
                extra.append(i_note)
        took = await _maybe_snapshot(force=force_snapshot)
        if brk is not None:
            b_ok, b_note, b_acted = await _maybe_briefs()
            ok, acted = ok and b_ok, acted or b_acted
            if b_note:
                extra.append(b_note)
            s_ok, s_note, s_acted = await _maybe_summary()
            ok, acted = ok and s_ok, acted or s_acted
            if s_note:
                extra.append(s_note)
            w_ok, w_note, w_acted = await _maybe_weekly_report()
            ok, acted = ok and w_ok, acted or w_acted
            if w_note:
                extra.append(w_note)
        if not note:
            note = f"snapshot {_stats['lastSnapshotDate']}" if took else (
                f"{active} open intent(s)" if worked else "idle")
        if extra:
            note = "; ".join(extra) if note == "idle" else note + "; " + "; ".join(extra)
    except Exception as e:
        ok = False
        note = str(e)[:200]
        log.exception("paper_trader pass failed")
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    if not ok:
        _stats["errors"] += 1
    runlog.record("paper_trader", ok, time.time() - started, note,
                  idle=(ok and not took and not worked and not acted))
    return ok


async def _loop() -> None:
    log.info("paper_trader loop started (interval %ss active / %ss idle)",
             ACTIVE_PASS_S, IDLE_PASS_S)
    await _pass(force_snapshot=True)   # startup: reconcile first, snapshot if today's row is missing
    while True:
        await asyncio.sleep(next_delay())
        await _pass()


def start() -> None:
    global _task
    _stats["enabled"] = enabled()
    if not _stats["enabled"]:
        log.info("paper_trader disabled (PAPER_TRADING_ENABLED unset, or the "
                 "demo broker is not configured)")
        return
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


async def close() -> None:
    """Close the demo broker's HTTP client if it was ever built (main.py
    awaits this at shutdown)."""
    b = broker_mod._singletons.get("demo")
    if b is not None:
        await b.close()


def status() -> dict:
    """``interval_s`` is the LONGEST gap between passes (what ``ops_watch``
    multiplies by two to decide the worker has stalled). ``halted`` is true when
    any bucket is halted (``haltedBuckets`` names them)."""
    halted = [b for b in paper_buckets.ids() if paper_ledger.read_halt(b)["halted"]]
    return {**_stats, **paper_exec.worker_state(),
            "openIntents": len(paper_ledger.non_terminal(paper_ledger.read_orders())),
            "halted": bool(halted), "haltedBuckets": halted,
            "enabled": enabled(), "interval_s": IDLE_PASS_S,
            "activeIntervalS": ACTIVE_PASS_S}
