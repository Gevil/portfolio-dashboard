"""Paper-trading worker: gate, snapshot scheduling, pass accounting, cadence
(plan §5, §6.1; contract §3.4-3.6; worker contract in skills/workers-and-alerts).

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime

import pytest

from app.api import broker as broker_mod
from app.api import notify
from app.api import paper_trader as pt

TZ = datetime.timezone(datetime.timedelta(hours=2))   # Europe/Berlin summer, for shape only


def bounds(day, open_h=9, close_h=17, close_m=30):
    return (datetime.datetime(day.year, day.month, day.day, open_h, 0, tzinfo=TZ),
           datetime.datetime(day.year, day.month, day.day, close_h, close_m, tzinfo=TZ))


IDLE_RESULT = {"active": 0, "cancelled": 0, "errors": [], "halted": False}


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = {"broker": None, "session": True, "snapshots": [], "last_row": None,
             "exec": dict(IDLE_RESULT), "order": [], "records": []}

    def session_bounds(mic, day):
        return bounds(day) if state["session"] else None

    async def take_snapshot(date=None, bucket="daily"):
        state["order"].append("snapshot")
        row = {"date": date or "2026-10-19", "equityEur": 1000.0}
        state["snapshots"].append(row)
        state["last_row"] = row
        return row

    async def run_pass():
        state["order"].append("exec")
        return dict(state["exec"])

    def record(*a, **k):
        state["records"].append((a, k))

    monkeypatch.setattr(pt.calendars, "session_bounds", session_bounds)
    monkeypatch.setattr(pt.paper_eval, "take_snapshot", take_snapshot)
    monkeypatch.setattr(pt.paper_exec, "run_pass", run_pass)
    monkeypatch.setattr(pt.paper_ledger, "latest_snapshot", lambda bucket="daily": state["last_row"])
    monkeypatch.setattr(pt.paper_ledger, "ORDERS_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(pt.paper_ledger, "HALT_FILE", tmp_path / "halt.json")
    monkeypatch.setattr(pt.paper_ledger, "BRIEFS_FILE", tmp_path / "briefs.json")
    monkeypatch.setattr(pt, "_broker", lambda: state["broker"])
    monkeypatch.setattr(pt, "_today_local", lambda: datetime.date(2026, 10, 19))
    monkeypatch.setattr(pt.runlog, "record", record)
    return state


def run(coro):
    return asyncio.run(coro)


class AccountBroker:
    def __init__(self, order=None, result=None):
        self.order = order
        self.result = result if result is not None else {"total": 1.0}

    async def account(self):
        if self.order is not None:
            self.order.append("account")
        return self.result


# --------------------------------------------------------------------- gate

def test_enabled_requires_env_and_broker(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING_ENABLED", raising=False)
    monkeypatch.setattr(pt, "_broker", lambda: object())
    assert pt.enabled() is False
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "1")
    assert pt.enabled() is True
    monkeypatch.setattr(pt, "_broker", lambda: None)
    assert pt.enabled() is False


def test_start_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING_ENABLED", raising=False)
    pt.start()
    assert pt.status()["running"] is False


# --------------------------------------------------------------- snapshot_due

TODAY = datetime.date(2026, 10, 19)


def test_snapshot_due_false_when_already_taken_today():
    assert pt.snapshot_due(TODAY, "2026-10-19") is False


def test_snapshot_due_false_on_a_non_session_day(monkeypatch):
    monkeypatch.setattr(pt.calendars, "session_bounds", lambda mic, day: None)
    assert pt.snapshot_due(TODAY, None) is False
    assert pt.snapshot_due(TODAY, None, force=True) is False


def test_snapshot_due_false_before_the_session_close(monkeypatch):
    monkeypatch.setattr(pt.calendars, "session_bounds", lambda mic, day: bounds(day))
    before_close = datetime.datetime(2026, 10, 19, 12, 0, tzinfo=TZ)
    assert pt.snapshot_due(TODAY, None, now=before_close) is False


def test_snapshot_due_true_after_the_session_close(monkeypatch):
    monkeypatch.setattr(pt.calendars, "session_bounds", lambda mic, day: bounds(day))
    after_close = datetime.datetime(2026, 10, 19, 18, 0, tzinfo=TZ)
    assert pt.snapshot_due(TODAY, None, now=after_close) is True


def test_snapshot_due_force_bypasses_only_the_close_gate(monkeypatch):
    monkeypatch.setattr(pt.calendars, "session_bounds", lambda mic, day: bounds(day))
    before_close = datetime.datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
    assert pt.snapshot_due(TODAY, None, now=before_close, force=True) is True
    # still honours "already taken today" even when forced
    assert pt.snapshot_due(TODAY, "2026-10-19", now=before_close, force=True) is False


# -------------------------------------------------------------------- passes

def test_pass_is_idle_when_no_snapshot_is_due(env):
    env["session"] = False
    ok = run(pt._pass())
    assert ok is True
    assert env["snapshots"] == []
    assert pt._stats["runs"] >= 1


def test_startup_pass_takes_a_snapshot_when_todays_row_is_missing(env):
    ok = run(pt._pass(force_snapshot=True))
    assert ok is True
    assert len(env["snapshots"]) == 1


def test_startup_pass_does_not_duplicate_an_existing_row(env):
    env["last_row"] = {"date": "2026-10-19", "equityEur": 1.0}
    run(pt._pass(force_snapshot=True))
    assert env["snapshots"] == []


def test_pass_failure_is_caught_and_recorded(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(pt.paper_eval, "take_snapshot", boom)
    errors_before = pt._stats["errors"]
    ok = run(pt._pass(force_snapshot=True))
    assert ok is False
    assert pt._stats["errors"] == errors_before + 1


def test_pass_refreshes_broker_status_via_account_call(env):
    calls = {"n": 0}

    class Broker:
        async def account(self):
            calls["n"] += 1
            return {"total": 1.0}

    env["broker"] = Broker()
    env["session"] = False   # isolate: only interested in the account() call
    run(pt._pass())
    assert calls["n"] == 1


def test_pass_reconciles_before_the_broker_refresh_and_the_snapshot(env):
    """§3.4: unresolved rows are reconciled before anything else (startup too)."""
    env["broker"] = AccountBroker(order=env["order"])
    run(pt._pass(force_snapshot=True))
    assert env["order"] == ["exec", "account", "snapshot"]


def test_pass_without_a_broker_runs_no_exec_and_no_account_call(env):
    env["session"] = False
    assert run(pt._pass()) is True
    assert env["order"] == []


def test_exec_errors_make_the_pass_a_failed_pass(env):
    env["broker"] = AccountBroker()
    env["exec"] = {**IDLE_RESULT, "active": 1, "errors": ["timeout", "http"]}
    env["session"] = False
    errors_before = pt._stats["errors"]
    assert run(pt._pass()) is False
    assert pt._stats["errors"] == errors_before + 1
    (args, kwargs), = env["records"]
    assert args[:2] == ("paper_trader", False) and args[3] == "exec: timeout,http"
    assert kwargs == {"idle": False}


@pytest.mark.parametrize("result,idle", [
    ({}, True),
    ({"active": 2}, False),
    ({"cancelled": 1}, False),
    ({"halted": True}, False),
])
def test_a_pass_is_recorded_idle_only_when_nothing_happened(env, result, idle):
    env["broker"] = AccountBroker()
    env["exec"] = {**IDLE_RESULT, **result}
    env["session"] = False
    assert run(pt._pass()) is True
    (args, kwargs), = env["records"]
    assert kwargs == {"idle": idle}
    assert args[3] == ("idle" if idle else f"{result.get('active', 0)} open intent(s)")


def test_a_snapshot_pass_is_never_recorded_idle(env):
    env["broker"] = AccountBroker()
    run(pt._pass(force_snapshot=True))
    (args, kwargs), = env["records"]
    assert kwargs == {"idle": False} and args[3].startswith("snapshot ")


def test_a_broker_status_error_is_noted_but_is_not_a_failed_pass(env):
    env["broker"] = AccountBroker(result=broker_mod.err("timeout"))
    env["session"] = False
    assert run(pt._pass()) is True
    (args, _), = env["records"]
    assert args[3] == "broker status: timeout"


# ------------------------------------------------------------------- cadence

def test_next_delay_is_60_s_while_busy_and_300_s_otherwise(monkeypatch):
    monkeypatch.setattr(pt.paper_exec, "needs_fast_pass", lambda now=None: True)
    assert pt.next_delay() == 60
    monkeypatch.setattr(pt.paper_exec, "needs_fast_pass", lambda now=None: False)
    assert pt.next_delay() == 300


def test_a_failing_cadence_check_errs_towards_the_short_interval(monkeypatch):
    def boom(now=None):
        raise RuntimeError("calendar broke")
    monkeypatch.setattr(pt.paper_exec, "needs_fast_pass", boom)
    assert pt.next_delay() == 60


class StopLoop(Exception):
    pass


def test_the_loop_runs_a_forced_startup_pass_then_sleeps_by_the_current_cadence(env, monkeypatch):
    passes, delays = [], []
    cadence = iter([60, 300, 60])

    async def fake_pass(force_snapshot=False):
        passes.append(force_snapshot)
        return True

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) == 3:
            raise StopLoop()

    monkeypatch.setattr(pt, "_pass", fake_pass)
    monkeypatch.setattr(pt, "next_delay", lambda: next(cadence))
    monkeypatch.setattr(pt.asyncio, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        run(pt._loop())
    assert passes == [True, False, False]          # startup pass is forced, later ones are not
    assert delays == [60, 300, 60]


# -------------------------------------------------------------------- status

def test_status_reports_interval_cadence_and_the_exec_fields(env, monkeypatch):
    monkeypatch.setattr(pt.paper_exec, "worker_state",
                        lambda: {"lastReconcileAt": 123.0, "consecutiveErrors": 2, "lastError": "http"})
    pt.paper_ledger.save_orders([{"state": "submitted"}, {"state": "unknown"}, {"state": "filled"}])
    pt.paper_ledger.write_halt({"halted": True, "reason": "x", "at": 1.0, "by": "owner",
                                "resumedAt": None, "resumedBy": None})
    s = pt.status()
    assert s["openIntents"] == 2 and s["halted"] is True
    assert s["lastReconcileAt"] == 123.0 and s["consecutiveErrors"] == 2
    # ops_watch flags a worker whose last success is older than 2 x interval_s: the LONGEST gap
    assert s["interval_s"] == 300 and s["activeIntervalS"] == 60
    assert {"running", "enabled", "runs", "errors", "lastSnapshotDate"} <= set(s)


# =================================================== Phase 3: PM schedule

def local(hour, minute=0):
    """Epoch of 2026-10-19 (a session day) at a LOCAL wall-clock time."""
    return datetime.datetime(2026, 10, 19, hour, minute).timestamp()


@pytest.fixture
def sched(env, monkeypatch, tmp_path):
    s = {"now": local(10), "mode": "shadow", "window": True, "digest": None, "calls": [],
         "result": {"decisionId": "2026-10-19-s1-1", "valid": True, "noChange": True}}
    monkeypatch.setattr(pt, "_sched", {"date": None, "attempts": 0, "lastAttemptAt": 0.0, "done": False})
    monkeypatch.setattr(pt, "_now", lambda: s["now"])
    monkeypatch.setattr(pt.paper_eval, "_paper_cfg", lambda bucket="daily": {
        "bucket": bucket, "enabled": bucket == "daily",
        "mode": s["mode"] if bucket == "daily" else "off", "strategyVersion": "s1",
        "budgetEur": 1000.0, "universe": {"ASML": {"t212": "ASMLa_EQ"}}, "limits": {}})
    monkeypatch.setattr(pt.paper_exec, "window_state",
                        lambda mic, now: {"open": s["window"], "opensAt": 1, "closesAt": 2})
    monkeypatch.setattr(pt.digest, "status", lambda: {"last_batch": s["digest"]})
    monkeypatch.setattr(pt, "SUMMARY_FILE", tmp_path / "paper_summary.json")

    async def decide(*, trigger, now=None):
        s["calls"].append((trigger, now))
        if isinstance(s["result"], Exception):
            raise s["result"]
        return s["result"]

    monkeypatch.setattr(pt.paper_pm, "decide", decide)
    return s


def test_the_decision_waits_for_todays_digest_batch_until_the_11_o_clock_fallback(sched):
    assert run(pt._maybe_decide()) == (True, None, False) and sched["calls"] == []       # no batch yet, 10:00
    sched["digest"] = {"ts": local(8, 30)}                                                # finished BEFORE 08:45
    assert run(pt._maybe_decide())[2] is False and sched["calls"] == []
    sched["digest"] = {"ts": local(8, 30) - 86400}                                        # yesterday's batch
    run(pt._maybe_decide())
    assert sched["calls"] == []
    sched["now"] = local(11, 0)                                                           # the fallback
    ok, note, acted = run(pt._maybe_decide())
    assert (ok, acted) == (True, True) and note == "decision no change"
    assert [c[0] for c in sched["calls"]] == ["schedule"]


def test_the_decision_fires_after_the_digest_batch_finished_after_08_45(sched):
    sched["digest"] = {"ts": local(9, 20)}
    ok, note, acted = run(pt._maybe_decide())
    assert (ok, note, acted) == (True, "decision no change", True)
    assert sched["calls"] == [("schedule", sched["now"])]


def test_the_decision_fires_once_per_local_date(sched):
    sched["digest"] = {"ts": local(9, 20)}
    run(pt._maybe_decide())
    sched["now"] = local(10, 30)
    assert run(pt._maybe_decide()) == (True, None, False)
    sched["now"] = local(15, 0)
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 1
    next_day = datetime.datetime(2026, 10, 20, 10, 0).timestamp()
    sched["now"], sched["digest"] = next_day, {"ts": next_day - 3000}
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 2                  # a new date re-arms the schedule


@pytest.mark.parametrize("mode", ["off", "live", ""])
def test_the_decision_only_runs_in_shadow_or_demo(sched, mode):
    sched["mode"], sched["digest"] = mode, {"ts": local(9, 20)}
    assert run(pt._maybe_decide()) == (True, None, False) and sched["calls"] == []


@pytest.mark.parametrize("mode", ["shadow", "demo"])
def test_shadow_and_demo_both_decide(sched, mode):
    sched["mode"], sched["digest"] = mode, {"ts": local(9, 20)}
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 1


def test_the_decision_needs_the_trading_window_to_be_open(sched):
    sched["window"], sched["digest"] = False, {"ts": local(9, 20)}
    assert run(pt._maybe_decide()) == (True, None, False) and sched["calls"] == []
    sched["window"] = True
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 1


def test_a_deferral_is_ok_with_a_note_and_is_retried_later_but_not_every_pass(sched):
    sched["digest"] = {"ts": local(9, 20)}
    sched["result"] = {"deferred": "budget"}
    assert run(pt._maybe_decide()) == (True, "deferred: budget", True)
    sched["now"] = local(10, 5)                                       # too soon to ask again
    assert run(pt._maybe_decide()) == (True, None, False) and len(sched["calls"]) == 1
    sched["now"] = local(10, 40)                                      # DECIDE_RETRY_S later
    assert run(pt._maybe_decide())[1] == "deferred: budget" and len(sched["calls"]) == 2
    sched["now"] = local(11, 20)
    run(pt._maybe_decide())
    sched["now"] = local(12, 0)
    assert run(pt._maybe_decide()) == (True, None, False)             # DECIDE_MAX_ATTEMPTS reached
    assert len(sched["calls"]) == pt.DECIDE_MAX_ATTEMPTS == 3
    assert sched["calls"][-1][0] == "schedule"


def test_a_lane_deferral_stored_as_a_row_is_retried_like_any_deferral(sched):
    sched["digest"] = {"ts": local(9, 20)}
    sched["result"] = {"decisionId": "d", "deferred": "lane: lane_down", "valid": False}
    assert run(pt._maybe_decide()) == (True, "deferred: lane: lane_down", True)
    sched["now"] = local(10, 40)
    sched["result"] = {"decisionId": "d2", "valid": True, "noChange": False, "rowIds": ["a", "b"]}
    assert run(pt._maybe_decide())[1] == "decision targets, 2 order intent(s)"
    sched["now"] = local(13, 0)
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 2                                   # decided: the date is closed


def test_an_invalid_decision_closes_the_date_without_failing_the_pass(sched):
    sched["digest"] = {"ts": local(9, 20)}
    sched["result"] = {"decisionId": "d", "valid": False, "error": "x", "deferred": None}
    assert run(pt._maybe_decide()) == (True, "decision invalid output, no orders", True)
    sched["now"] = local(11, 30)
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 1


def test_decided_earlier_today_after_a_restart_closes_the_date_silently(sched):
    sched["digest"] = {"ts": local(9, 20)}
    sched["result"] = {"deferred": "already decided today"}
    assert run(pt._maybe_decide()) == (True, None, False)
    sched["now"] = local(10, 40)
    run(pt._maybe_decide())
    assert len(sched["calls"]) == 1


def test_a_crashing_decision_is_a_failed_pass_but_never_raises(sched):
    sched["digest"] = {"ts": local(9, 20)}
    sched["result"] = RuntimeError("bug")
    assert run(pt._maybe_decide()) == (False, "decision failed: RuntimeError", True)


def test_a_pass_runs_the_decision_between_the_broker_refresh_and_the_snapshot(sched, env, monkeypatch):
    sched["digest"] = {"ts": local(9, 20)}
    env["broker"] = AccountBroker(order=env["order"])
    env["session"] = False
    orig = pt.paper_pm.decide

    async def decide(*, trigger, now=None):
        env["order"].append("decide")
        return await orig(trigger=trigger, now=now)
    monkeypatch.setattr(pt.paper_pm, "decide", decide)
    assert run(pt._pass()) is True
    assert env["order"] == ["exec", "account", "decide"]
    (args, kwargs), = env["records"]
    assert args[1] is True and args[3] == "decision no change" and kwargs == {"idle": False}


def test_a_deferred_decision_is_recorded_ok_with_a_deferred_note(sched, env):
    sched["digest"] = {"ts": local(9, 20)}
    sched["result"] = {"deferred": "mode off"}
    env["broker"], env["session"] = AccountBroker(), False
    assert run(pt._pass()) is True
    (args, _), = env["records"]
    assert args[1] is True and args[3] == "deferred: mode off"


def test_a_crashing_decision_makes_the_pass_fail(sched, env):
    sched["digest"], sched["result"] = {"ts": local(9, 20)}, RuntimeError("bug")
    env["broker"], env["session"] = AccountBroker(), False
    errors_before = pt._stats["errors"]
    assert run(pt._pass()) is False
    assert pt._stats["errors"] == errors_before + 1


def test_no_decision_without_the_broker(sched, env):
    sched["digest"] = {"ts": local(9, 20)}
    env["session"] = False
    assert run(pt._pass()) is True and sched["calls"] == []


# ======================================================= Phase 3: summary

def local_bounds(day):
    return (datetime.datetime(day.year, day.month, day.day, 9, 0).astimezone(),
            datetime.datetime(day.year, day.month, day.day, 17, 30).astimezone())


@pytest.fixture
def summ(sched, env, monkeypatch):
    sched["now"] = local(18, 0)
    s = sched
    s.update(alerts=[], delivery=notify.Delivery("sent"), taken=[], decisions=[], buy_hold=0.8,
             snap={"date": "2026-10-19", "equityEur": 1012.5, "takenAt": local(17, 50)},
             snapshots=[{"date": "2026-10-16", "equityEur": 1000.0}])
    monkeypatch.setattr(pt.calendars, "session_bounds",
                        lambda mic, day: local_bounds(day) if env["session"] else None)

    async def alert(*a, **k):
        s["alerts"].append((a, k))
        return s["delivery"]

    async def take_snapshot(date=None, bucket="daily"):
        s["taken"].append(date)
        return s["retaken"] if "retaken" in s else s["snap"]

    async def quotes(universe):
        return {}

    async def buy_hold(season, q):
        return s["buy_hold"]

    monkeypatch.setattr(pt.notify, "alert", alert)
    monkeypatch.setattr(pt.paper_eval, "take_snapshot", take_snapshot)
    monkeypatch.setattr(pt.paper_eval, "_quote_universe", quotes)
    monkeypatch.setattr(pt.paper_exec, "_buy_hold_pct", buy_hold)
    monkeypatch.setattr(pt.paper_ledger, "snapshot_on", lambda d, bucket="daily": s["snap"])
    monkeypatch.setattr(pt.paper_ledger, "read_snapshots",
                        lambda bucket="daily": [*s["snapshots"], s["snap"]])
    monkeypatch.setattr(pt.paper_ledger, "read_season",
                        lambda bucket="daily": {"baselineWeights": {"ASML": 100.0}})
    monkeypatch.setattr(pt.paper_pm, "read_decisions", lambda bucket="daily": list(s["decisions"]))
    env["session"] = True
    return s


def test_one_summary_is_pushed_after_the_close_with_the_documented_shape(summ):
    summ["decisions"] = [{"date": "2026-10-19", "valid": True, "noChange": True}]
    pt.paper_ledger.save_orders([
        {"intentId": "2026-10-19-s1-agent-ASML-buy-1", "state": "filled", "filledQuantity": 0.1},
        {"intentId": "2026-10-19-s1-agent-NVDA-sell-1", "state": "submitted", "filledQuantity": 0},
        {"intentId": "2026-10-16-s1-agent-ASML-buy-1", "state": "filled", "filledQuantity": 1}])
    assert run(pt._maybe_summary()) == (True, "summary sent", True)
    (args, kwargs), = summ["alerts"]
    assert args[:3] == ("PAPER", "paper", "Practice bucket 2026-10-19") and kwargs["priority"] == 3
    lines = args[3].split("\n")
    assert len(lines) <= 6
    assert lines[0] == "Equity \u20ac1,012.50, day +1.25% (+12.50)"
    assert lines[1] == "Since inception: bucket +1.25% vs buy&hold +0.80% (+0.45 pts)"
    assert lines[2] == "Decision: no change"
    assert lines[3] == "Orders: 2 placed, 1 filled"
    assert lines[4] == "Halt: none"
    assert "http" not in args[3]


def test_the_summary_is_pushed_once_per_session_day_and_remembered(summ):
    run(pt._maybe_summary())
    assert run(pt._maybe_summary()) == (True, None, False)
    assert len(summ["alerts"]) == 1
    assert pt.jsonstore.load(pt.SUMMARY_FILE)["notifiedDate"] == "2026-10-19"
    assert pt._stats["lastSummaryDate"] == "2026-10-19"
    summ["now"] = datetime.datetime(2026, 10, 20, 18, 0).timestamp()      # next session day
    summ["snap"] = {"date": "2026-10-20", "equityEur": 1020.0, "takenAt": summ["now"] - 60}
    run(pt._maybe_summary())
    assert len(summ["alerts"]) == 2


@pytest.mark.parametrize("status", ["sent", "queued", "filtered", "duplicate"])
def test_every_status_but_failed_consumes_the_summary(summ, status):
    summ["delivery"] = notify.Delivery(status)
    assert run(pt._maybe_summary()) == (True, f"summary {status}", True)
    assert pt.jsonstore.load(pt.SUMMARY_FILE)["notifiedDate"] == "2026-10-19"
    run(pt._maybe_summary())
    assert len(summ["alerts"]) == 1


def test_a_failed_push_is_not_consumed_and_is_retried_next_pass(summ):
    summ["delivery"] = notify.Delivery("failed", "outbox write failed")
    assert run(pt._maybe_summary()) == (False, "summary push failed", False)
    assert pt.jsonstore.load(pt.SUMMARY_FILE, {}) == {}
    summ["delivery"] = notify.Delivery("sent")
    assert run(pt._maybe_summary()) == (True, "summary sent", True)
    assert len(summ["alerts"]) == 2


def test_no_summary_before_the_close_on_a_non_session_day_or_with_the_mode_off(summ, env):
    summ["now"] = local(10, 0)
    assert run(pt._maybe_summary()) == (True, None, False)
    summ["now"] = local(18, 0)
    env["session"] = False
    assert run(pt._maybe_summary()) == (True, None, False)
    env["session"], summ["mode"] = True, "off"
    assert run(pt._maybe_summary()) == (True, None, False)
    assert summ["alerts"] == []


def test_a_snapshot_taken_before_the_close_is_retaken_for_the_summary(summ):
    summ["snap"] = {"date": "2026-10-19", "equityEur": 990.0, "takenAt": local(9, 5)}     # startup row
    summ["retaken"] = {"date": "2026-10-19", "equityEur": 1005.0, "takenAt": local(17, 55)}
    run(pt._maybe_summary())
    assert summ["taken"] == ["2026-10-19"]
    assert summ["alerts"][0][0][3].startswith("Equity \u20ac1,005.00")


def test_a_missing_snapshot_waits_for_the_next_pass_without_pushing(summ):
    summ["snap"] = None
    summ["retaken"] = None                                                  # broker unreachable
    assert run(pt._maybe_summary()) == (True, None, False)
    assert summ["alerts"] == [] and pt.jsonstore.load(pt.SUMMARY_FILE, {}) == {}


@pytest.mark.parametrize("decisions,expected", [
    ([], "Decision: none today"),
    ([{"date": "2026-10-19", "valid": False, "error": "x"}], "Decision: invalid output, no orders"),
    ([{"date": "2026-10-19", "deferred": "lane: lane_down", "valid": False}], "Decision: deferred: lane: lane_down"),
    ([{"date": "2026-10-19", "valid": True, "noChange": False,
       "output": {"targets": {"ASML": 30, "NVDA": 15.5, "SXR8": 52.5}, "cash_pct": 2,
                  "rationale": "buy now at https://evil.example"}}],
     "Decision: targets ASML 30%, NVDA 15.5%, SXR8 52.5%, cash 2%"),
    ([{"date": "2026-10-18", "valid": True, "noChange": True}], "Decision: none today"),
    ([{"date": "2026-10-19", "deferred": "budget", "valid": False},
      {"date": "2026-10-19", "valid": True, "noChange": True}], "Decision: no change"),
])
def test_the_decision_line_states_what_was_decided_today_and_never_model_prose(summ, decisions, expected):
    summ["decisions"] = decisions
    run(pt._maybe_summary())
    body = summ["alerts"][0][0][3]
    assert body.split("\n")[2] == expected
    assert "evil" not in body and "rationale" not in body


def test_the_orders_line_for_shadow_rows_and_for_no_orders(summ):
    assert pt._orders_line("2026-10-19") == "Orders: none"
    pt.paper_ledger.save_orders([{"intentId": "2026-10-19-s1-agent-ASML-buy-1", "state": "shadow"},
                                 {"intentId": "2026-10-19-s1-agent-NVDA-buy-1", "state": "shadow"}])
    assert pt._orders_line("2026-10-19") == "Orders: 2 shadow (not sent)"


def test_the_halt_state_and_missing_comparisons_are_stated_plainly(summ):
    summ["buy_hold"] = None
    summ["snapshots"] = []
    pt.paper_ledger.write_halt({"halted": True, "reason": "drawdown 16% see https://evil.example",
                                "at": 1.0, "by": "kill_switch", "resumedAt": None, "resumedBy": None})
    run(pt._maybe_summary())
    lines = summ["alerts"][0][0][3].split("\n")
    assert lines[0] == "Equity \u20ac1,012.50, day change n/a"
    assert lines[1] == "Since inception: bucket +1.25%, buy&hold n/a"
    assert lines[4].startswith("HALTED: drawdown 16%") and "evil" not in lines[4]


def test_a_summary_crash_fails_the_pass_without_raising(summ, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("push bug")
    monkeypatch.setattr(pt.notify, "alert", boom)
    assert run(pt._maybe_summary()) == (False, "summary failed: RuntimeError", False)


def test_the_pass_pushes_the_summary_after_the_snapshot_and_records_it(summ, env):
    env["broker"] = AccountBroker(order=env["order"])
    env["last_row"] = {"date": "2026-10-16", "equityEur": 1000.0}
    summ["window"] = False                          # no decision: isolate snapshot + summary
    assert run(pt._pass(force_snapshot=True)) is True
    assert env["order"] == ["exec", "account"] and len(summ["taken"]) == 1     # the forced snapshot
    assert len(summ["alerts"]) == 1
    (args, kwargs), = env["records"]
    assert args[3] == "snapshot 2026-10-19; briefing daily; summary sent" and kwargs == {"idle": False}


# ============================================ Phase 4: weekly report

FRI = datetime.date(2026, 10, 23)         # last XETR session of ISO week 2026-W43


def at(day, hour, minute=0):
    return datetime.datetime(day.year, day.month, day.day, hour, minute).timestamp()


@pytest.fixture
def weekly(summ, monkeypatch, tmp_path):
    """A Friday after the close; Mon-Fri are sessions unless listed in ``holidays``."""
    holidays = set()

    def session_bounds(mic, day):
        return local_bounds(day) if day.weekday() < 5 and day not in holidays else None

    async def factors(season):
        return {"2026-10-16": 1.0, "2026-10-23": 1.02}

    async def perf_metrics(bucket="daily"):
        return {"metrics": summ["metrics"], "versions": [], "warnings": []}

    summ.update(now=at(FRI, 18), holidays=holidays,
                snap={"date": "2026-10-23", "equityEur": 1030.0, "takenAt": at(FRI, 17, 50)},
                metrics={"excessPct": 1.234, "excessCI95": [-0.5, 2.6], "verdict": "inconclusive",
                         "sessions": 5, "costDragPct": 0.12})
    monkeypatch.setattr(pt.calendars, "session_bounds", session_bounds)
    monkeypatch.setattr(pt, "REPORT_FILE", tmp_path / "paper_reports.json")
    monkeypatch.setattr(pt.paper_eval, "buy_hold_factors", factors)
    monkeypatch.setattr(pt.paper_eval, "performance_metrics", perf_metrics)
    return summ


def test_the_weekly_report_is_pushed_once_on_the_last_session_with_the_documented_shape(weekly):
    weekly["decisions"] = [
        {"date": "2026-10-16", "valid": True},                       # previous week: not counted
        {"date": "2026-10-19", "valid": True, "noChange": False},
        {"date": "2026-10-20", "valid": True, "noChange": True},
        {"date": "2026-10-21", "valid": False},
        {"date": "2026-10-22", "deferred": "budget", "valid": False}]
    pt.paper_ledger.save_orders([
        {"intentId": "2026-10-16-s1-agent-ASML-buy-1", "filledQuantity": 1},
        {"intentId": "2026-10-21-s1-agent-ASML-buy-1", "filledQuantity": 0.5},
        {"intentId": "2026-10-22-s1-agent-ASML-sell-1", "state": "shadow", "filledQuantity": 0}])
    assert run(pt._maybe_weekly_report()) == (True, "weekly report sent", True)
    (args, kwargs), = weekly["alerts"]
    assert args[:3] == ("PAPER", "paper", "Practice week 2026-W43") and kwargs["priority"] == 3
    lines = args[3].split("\n")
    assert len(lines) <= 8 and "http" not in args[3]
    assert lines[0] == "Week: bucket +3.00% vs buy&hold +2.00% (+1.00 pts)"
    assert lines[1] == "Since inception: excess +1.2 pp CI -0.5 .. +2.6 pp, inconclusive (5 sessions)"
    assert lines[2] == "Decisions: 1 valid, 1 no change, 1 invalid, 1 deferred"
    assert lines[3] == "Orders filled: 1"
    assert lines[4] == "Cost drag 0.12% of equity"
    assert lines[5] == "Halt: none"
    assert pt.jsonstore.load(pt.REPORT_FILE)["notifiedWeek"] == "2026-W43"
    assert run(pt._maybe_weekly_report()) == (True, None, False)       # same week: nothing more
    assert len(weekly["alerts"]) == 1


def test_the_report_states_the_halt_and_missing_metrics_plainly(weekly, monkeypatch):
    weekly["metrics"] = None
    pt.paper_ledger.write_halt({"halted": True, "reason": "drawdown 16%", "at": 1.0, "by": "kill_switch",
                                "resumedAt": None, "resumedBy": None})
    run(pt._maybe_weekly_report())
    lines = weekly["alerts"][0][0][3].split("\n")
    assert lines[1] == "Since inception: metrics n/a" and lines[4] == "Cost drag: n/a"
    assert lines[5] == "HALTED: drawdown 16%"


def test_no_report_before_the_close_or_on_an_earlier_session_of_the_week(weekly):
    weekly["now"] = at(FRI, 12)
    assert run(pt._maybe_weekly_report()) == (True, None, False)
    weekly["now"] = at(datetime.date(2026, 10, 22), 18)                 # Thursday, Friday is a session
    assert run(pt._maybe_weekly_report()) == (True, None, False)
    assert weekly["alerts"] == [] and weekly["taken"] == []


def test_a_holiday_friday_moves_the_report_to_thursday(weekly):
    weekly["holidays"].add(FRI)
    weekly["now"] = at(datetime.date(2026, 10, 22), 18)
    weekly["snap"] = {"date": "2026-10-22", "equityEur": 1010.0, "takenAt": weekly["now"] - 600}
    assert run(pt._maybe_weekly_report())[1] == "weekly report sent"
    assert weekly["alerts"][0][0][2] == "Practice week 2026-W43"
    assert pt.is_last_session_of_week(datetime.date(2026, 10, 21)) is False
    assert pt.is_last_session_of_week(FRI) is False                      # not a session at all


@pytest.mark.parametrize("mode,fires", [("off", False), ("live", False), ("shadow", True), ("demo", True)])
def test_the_report_only_runs_in_shadow_or_demo(weekly, mode, fires):
    weekly["mode"] = mode
    run(pt._maybe_weekly_report())
    assert len(weekly["alerts"]) == (1 if fires else 0)


def test_the_next_iso_week_gets_its_own_report(weekly):
    run(pt._maybe_weekly_report())
    nxt = datetime.date(2026, 10, 30)
    weekly["now"] = at(nxt, 18)
    weekly["snap"] = {"date": "2026-10-30", "equityEur": 1040.0, "takenAt": at(nxt, 17, 50)}
    assert run(pt._maybe_weekly_report()) == (True, "weekly report sent", True)
    assert [a[0][2] for a in weekly["alerts"]] == ["Practice week 2026-W43", "Practice week 2026-W44"]


@pytest.mark.parametrize("status", ["sent", "queued", "filtered", "duplicate"])
def test_every_delivery_status_but_failed_consumes_the_week(weekly, status):
    weekly["delivery"] = notify.Delivery(status)
    assert run(pt._maybe_weekly_report()) == (True, f"weekly report {status}", True)
    assert run(pt._maybe_weekly_report()) == (True, None, False)
    assert len(weekly["alerts"]) == 1


def test_a_failed_report_push_is_retried_next_pass(weekly):
    weekly["delivery"] = notify.Delivery("failed")
    assert run(pt._maybe_weekly_report()) == (False, "weekly report push failed", False)
    assert pt.jsonstore.load(pt.REPORT_FILE, {}) == {}
    weekly["delivery"] = notify.Delivery("sent")
    assert run(pt._maybe_weekly_report()) == (True, "weekly report sent", True)
    assert len(weekly["alerts"]) == 2


def test_the_report_waits_for_a_snapshot_taken_at_or_after_the_close(weekly):
    weekly["snap"] = None                                              # broker unreachable
    assert run(pt._maybe_weekly_report()) == (True, None, False)
    assert weekly["alerts"] == [] and pt.jsonstore.load(pt.REPORT_FILE, {}) == {}


# ================================= Phase 5: two buckets, one push, briefings

def two_buckets(s, monkeypatch, daily_mode="shadow", intraday_mode="shadow", intraday_enabled=True):
    """Per-bucket stand-ins on top of the ``summ`` fixture: a daily bucket (budget 1000) and an
    intraday bucket (budget 600), each with its own snapshots, season and decisions."""
    s.update(
        taken2=[],
        snaps={"daily": {"date": "2026-10-19", "equityEur": 1012.5, "takenAt": local(17, 50)},
               "intraday": {"date": "2026-10-19", "equityEur": 606.0, "takenAt": local(17, 50)}},
        prior={"daily": [{"date": "2026-10-16", "equityEur": 1000.0}],
               "intraday": [{"date": "2026-10-16", "equityEur": 600.0}]},
        decs={"daily": [{"date": "2026-10-19", "valid": True, "noChange": True}],
              "intraday": [
                  {"date": "2026-10-19", "decisionId": "i-1", "slot": "10:00", "valid": True,
                   "noChange": False, "trigger": "schedule", "createdAt": 1.0,
                   "output": {"targets": {"ASML": 50}, "cash_pct": 50, "confidence": 0.6,
                              "rationale": "Opening range broke up, buy ASML."}},
                  {"date": "2026-10-19", "decisionId": "i-2", "slot": "11:00", "valid": True,
                   "noChange": False, "trigger": "schedule", "createdAt": 2.0,
                   "output": {"targets": {"ASML": 40}, "cash_pct": 60, "confidence": 0.5,
                              "rationale": "Faded, trim."}},
                  {"date": "2026-10-19", "decisionId": "i-3", "slot": "12:00", "valid": True,
                   "noChange": True, "trigger": "schedule", "createdAt": 3.0, "output": {"no_change": True}},
                  {"date": "2026-10-19", "decisionId": "i-4", "slot": "13:00", "deferred": "budget",
                   "valid": False, "trigger": "schedule", "createdAt": 4.0},
                  {"date": "2026-10-16", "valid": True, "noChange": True}]})

    def cfg(bucket="daily"):
        if bucket == "daily":
            return {"bucket": "daily", "enabled": True, "mode": daily_mode, "strategyVersion": "s1",
                    "budgetEur": 1000.0, "universe": {"ASML": {"t212": "ASMLa_EQ"}}, "limits": {}}
        return {"bucket": "intraday", "enabled": intraday_enabled,
                "mode": intraday_mode if intraday_enabled else "off", "strategyVersion": "i1",
                "budgetEur": 600.0, "universe": {"ASML": {"t212": "ASMLa_EQ"}}, "limits": {}}

    async def take_snapshot(date=None, bucket="daily"):
        s["taken2"].append((date, bucket))
        return s["snaps"].get(bucket)

    monkeypatch.setattr(pt.paper_eval, "_paper_cfg", cfg)
    monkeypatch.setattr(pt.paper_eval, "take_snapshot", take_snapshot)
    monkeypatch.setattr(pt.paper_ledger, "snapshot_on", lambda d, bucket="daily": s["snaps"].get(bucket))
    monkeypatch.setattr(pt.paper_ledger, "read_snapshots", lambda bucket="daily": [
        *s["prior"][bucket], *([s["snaps"][bucket]] if s["snaps"].get(bucket) else [])])
    monkeypatch.setattr(pt.paper_pm, "read_decisions", lambda bucket="daily": list(s["decs"][bucket]))


@pytest.fixture
def two(summ, monkeypatch):
    two_buckets(summ, monkeypatch)
    pt.paper_ledger.save_orders([
        {"intentId": "2026-10-19-intraday-i1-agent-ASML-buy-1", "bucket": "intraday", "id": "ASML",
         "side": "buy", "state": "filled", "filledQuantity": 1.0, "filledValueEur": 100.0,
         "quantity": 1.0, "limitPrice": 100.0, "refPrice": 100.0}])
    return summ


def test_one_combined_summary_covers_both_buckets(two):
    assert run(pt._maybe_summary()) == (True, "summary sent", True)
    (args, kwargs), = two["alerts"]
    assert args[:3] == ("PAPER", "paper", "Practice buckets 2026-10-19") and kwargs["priority"] == 3
    assert args[3].split("\n") == [
        "[daily]",
        "Equity \u20ac1,012.50, day +1.25% (+12.50)",
        "Since inception: bucket +1.25% vs buy&hold +0.80% (+0.45 pts)",
        "Decision: no change", "Orders: none", "Halt: none",
        "[intraday]",
        "Equity \u20ac606.00, day +1.00% (+6.00)",
        "Since inception: bucket +1.00% vs buy&hold +0.80% (+0.20 pts)",
        "Decisions: 2 valid, 1 no change, 0 invalid, 1 deferred", "Orders: 1 placed, 1 filled",
        "Halt: none"]
    assert "Opening range" not in args[3] and "http" not in args[3]       # no model prose, no URLs
    assert pt.jsonstore.load(pt.SUMMARY_FILE)["notifiedDate"] == "2026-10-19"
    assert run(pt._maybe_summary()) == (True, None, False) and len(two["alerts"]) == 1   # ONE push per day


def test_the_combined_summary_shows_each_buckets_own_halt(two):
    pt.paper_ledger.write_halt({"halted": True, "reason": "daily loss 3%", "at": 1.0, "by": "kill_switch"},
                               "intraday")
    run(pt._maybe_summary())
    lines = two["alerts"][0][0][3].split("\n")
    assert lines[5] == "Halt: none" and lines[11] == "HALTED: daily loss 3%"


def test_the_combined_summary_waits_until_every_bucket_has_its_snapshot(two):
    two["snaps"]["intraday"] = None                          # broker unreachable for the retake
    assert run(pt._maybe_summary()) == (True, None, False)
    assert two["alerts"] == [] and pt.jsonstore.load(pt.SUMMARY_FILE, {}) == {}
    two["snaps"]["intraday"] = {"date": "2026-10-19", "equityEur": 606.0, "takenAt": local(17, 50)}
    assert run(pt._maybe_summary()) == (True, "summary sent", True)


def test_an_intraday_snapshot_taken_before_the_close_is_retaken_for_the_summary(two):
    two["snaps"]["intraday"] = {"date": "2026-10-19", "equityEur": 1.0, "takenAt": local(9, 0)}
    two["retaken"] = None
    run(pt._maybe_summary())
    assert ("2026-10-19", "intraday") in two["taken2"] and ("2026-10-19", "daily") not in two["taken2"]


def test_a_bucket_that_is_off_or_disabled_is_not_in_the_push(summ, monkeypatch):
    two_buckets(summ, monkeypatch, intraday_enabled=False)
    run(pt._maybe_summary())
    (args, _), = summ["alerts"]
    assert args[2] == "Practice bucket 2026-10-19" and "[daily]" not in args[3]    # the pre-phase-5 text
    summ["alerts"].clear()
    pt.jsonstore.save(pt.SUMMARY_FILE, {})
    two_buckets(summ, monkeypatch, daily_mode="off")
    run(pt._maybe_summary())
    (args, _), = summ["alerts"]
    assert args[2] == "Practice intraday bucket 2026-10-19" and args[3].startswith("[intraday]")
    summ["alerts"].clear()
    pt.jsonstore.save(pt.SUMMARY_FILE, {})
    two_buckets(summ, monkeypatch, daily_mode="off", intraday_mode="off")
    assert run(pt._maybe_summary()) == (True, None, False) and summ["alerts"] == []


def test_the_weekly_report_is_one_push_with_a_block_per_bucket(weekly, monkeypatch):
    two_buckets(weekly, monkeypatch)
    weekly["snaps"] = {"daily": {"date": "2026-10-23", "equityEur": 1030.0, "takenAt": at(FRI, 17, 50)},
                       "intraday": {"date": "2026-10-23", "equityEur": 612.0, "takenAt": at(FRI, 17, 50)}}
    weekly["decs"]["intraday"] = [{"date": "2026-10-22", "valid": True, "noChange": True},
                                  {"date": "2026-10-22", "valid": False}]
    pt.paper_ledger.save_orders([
        {"intentId": "2026-10-22-intraday-i1-agent-ASML-buy-1", "bucket": "intraday",
         "state": "filled", "filledQuantity": 1.0},
        {"intentId": "2026-10-22-daily-s1-agent-ASML-buy-1", "state": "filled", "filledQuantity": 1.0},
        {"intentId": "2026-10-23-daily-s1-agent-ASML-sell-1", "state": "filled", "filledQuantity": 1.0}])
    assert run(pt._maybe_weekly_report()) == (True, "weekly report sent", True)
    (args, _), = weekly["alerts"]
    assert args[2] == "Practice week 2026-W43"
    lines = args[3].split("\n")
    assert lines[0] == "[daily]" and lines[7] == "[intraday]" and len(lines) == 14
    assert lines[1].startswith("Week: bucket +3.00% vs buy&hold +2.00%")
    assert lines[8] == "Week: bucket +2.00% vs buy&hold +2.00% (+0.00 pts)"
    assert lines[10] == "Decisions: 0 valid, 1 no change, 1 invalid, 0 deferred"
    assert lines[4] == "Orders filled: 2" and lines[11] == "Orders filled: 1"      # each bucket's own rows
    assert pt.jsonstore.load(pt.REPORT_FILE)["notifiedWeek"] == "2026-W43"
    assert run(pt._maybe_weekly_report()) == (True, None, False)


def test_snapshots_are_taken_per_bucket_and_each_has_its_own_already_taken_check(env, monkeypatch):
    last = {"daily": {"date": "2026-10-19"}, "intraday": None}
    taken = []

    async def take_snapshot(date=None, bucket="daily"):
        taken.append((date, bucket))
        return {"date": date, "bucket": bucket}
    monkeypatch.setattr(pt.paper_eval, "enabled_buckets", lambda: ["daily", "intraday"])
    monkeypatch.setattr(pt.paper_eval, "take_snapshot", take_snapshot)
    monkeypatch.setattr(pt.paper_ledger, "latest_snapshot", lambda bucket="daily": last[bucket])
    assert run(pt._maybe_snapshot(force=True)) is True
    assert taken == [("2026-10-19", "intraday")]                    # daily already has today's row
    last["intraday"] = {"date": "2026-10-19"}
    assert run(pt._maybe_snapshot(force=True)) is False and len(taken) == 1
    last.update(daily=None, intraday=None)
    run(pt._maybe_snapshot(force=True))
    assert taken[1:] == [("2026-10-19", "daily"), ("2026-10-19", "intraday")]
    monkeypatch.setattr(pt.paper_eval, "enabled_buckets", lambda: ["daily"])        # intraday disabled
    taken.clear()
    run(pt._maybe_snapshot(force=True))
    assert taken == [("2026-10-19", "daily")]


# ---- briefings (phase 5 §7)

def test_a_briefing_per_bucket_is_stored_after_the_close_from_stored_data(two):
    assert run(pt._maybe_briefs()) == (True, "briefing daily, intraday", True)
    d, = pt.paper_ledger.read_briefs("daily")
    i, = pt.paper_ledger.read_briefs("intraday")
    assert (d["bucket"], d["date"], d["strategyVersion"], d["mode"]) == ("daily", "2026-10-19", "s1", "shadow")
    assert (i["bucket"], i["strategyVersion"]) == ("intraday", "i1")
    assert pt.paper_ledger.BRIEFS_FILE.with_name("briefs.intraday.json").exists()
    assert i["text"] == (
        "intraday 2026-10-19: Equity \u20ac606.00 (+1.00% on the day); since inception +1.00% vs buy & hold "
        "+0.80% (+0.20 pts). 2 decision(s) with targets, 1 no change, 0 invalid, 1 deferred. "
        "1 order(s): 1 filled (EUR 100.00). Halt: none.")
    assert d["text"].startswith("daily 2026-10-19: Equity \u20ac1,012.50 (+1.25% on the day)")
    assert d["text"].endswith("0 decision(s) with targets, 1 no change, 0 invalid, 0 deferred. no orders. Halt: none.")
    assert i["equity"]["equityEur"] == 606.0 and i["equity"]["buyHoldPct"] == 0.8
    assert i["equity"]["sinceInceptionPct"] == 1.0 and i["equity"]["excessPts"] == pytest.approx(0.2)
    assert [x["slot"] for x in i["decisions"]] == ["10:00", "11:00", "12:00", "13:00"]   # today's rows only
    first = i["decisions"][0]
    assert first["targets"] == {"ASML": 50} and first["cashPct"] == 50 and first["confidence"] == 0.6
    assert first["rationale"] == "Opening range broke up, buy ASML."          # the model text lives here ...
    assert "Opening range" not in i["text"]                                    # ... never in the note itself
    assert i["decisions"][3]["deferred"] == "budget" and i["decisions"][2]["noChange"] is True
    assert i["orders"]["count"] == 1 and i["orders"]["filled"] == 1 and i["orders"]["filledValueEur"] == 100.0
    assert i["orders"]["items"][0]["intentId"] == "2026-10-19-intraday-i1-agent-ASML-buy-1"
    assert d["orders"] == {"count": 0, "shadow": 0, "filled": 0, "filledValueEur": 0.0, "items": []}
    assert pt._stats["lastBriefDate"] == "2026-10-19"


def test_a_briefing_is_built_once_per_bucket_and_day_and_never_calls_the_lane(two, monkeypatch):
    async def no_lane(*a, **k):
        raise AssertionError("a briefing must not call the lane")
    monkeypatch.setattr(pt.paper_pm, "decide", no_lane)
    run(pt._maybe_briefs())
    created = pt.paper_ledger.read_briefs("intraday")[0]["createdAt"]
    two["now"] = local(18, 30)
    assert run(pt._maybe_briefs()) == (True, None, False)
    assert pt.paper_ledger.read_briefs("intraday")[0]["createdAt"] == created
    assert len(pt.paper_ledger.read_briefs("daily")) == 1


def test_a_briefing_waits_for_the_close_and_for_the_buckets_snapshot(two):
    two["now"] = local(16, 0)
    assert run(pt._maybe_briefs()) == (True, None, False)
    assert pt.paper_ledger.read_briefs("daily") == [] and pt.paper_ledger.read_briefs("intraday") == []
    two["now"] = local(18, 0)
    two["snaps"]["intraday"] = None
    assert run(pt._maybe_briefs()) == (True, "briefing daily", True)         # daily is not held up by intraday
    assert pt.paper_ledger.read_briefs("intraday") == []
    two["snaps"]["intraday"] = {"date": "2026-10-19", "equityEur": 606.0, "takenAt": local(17, 50)}
    assert run(pt._maybe_briefs()) == (True, "briefing intraday", True)
    assert len(pt.paper_ledger.read_briefs("daily")) == 1


def test_no_briefing_on_a_non_session_day_or_for_a_bucket_that_is_off(two, env, monkeypatch):
    env["session"] = False
    assert run(pt._maybe_briefs()) == (True, None, False)
    env["session"] = True
    two_buckets(two, monkeypatch, daily_mode="off", intraday_enabled=False)
    assert run(pt._maybe_briefs()) == (True, None, False)
    assert pt.paper_ledger.read_briefs("daily") == [] and pt.paper_ledger.read_briefs("intraday") == []


def test_a_briefing_marks_a_halted_bucket_and_shadow_rows(two):
    pt.paper_ledger.write_halt({"halted": True, "reason": "stop https://evil.example", "at": 1.0,
                                "by": "owner"}, "intraday")
    pt.paper_ledger.save_orders([
        {"intentId": "2026-10-19-intraday-i1-agent-ASML-buy-1", "bucket": "intraday", "state": "shadow",
         "filledQuantity": 0.0}])
    run(pt._maybe_briefs())
    i, = pt.paper_ledger.read_briefs("intraday")
    assert i["orders"]["count"] == 1 and i["orders"]["shadow"] == 1 and i["orders"]["filled"] == 0
    assert "1 order(s): 0 filled (EUR 0.00), 1 shadow" in i["text"]
    assert "HALTED: stop" in i["text"] and "http" not in i["text"]


def test_a_briefing_crash_fails_the_pass_without_raising(two, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(pt.paper_ledger, "save_brief", boom)
    assert run(pt._maybe_briefs()) == (False, "briefing failed: RuntimeError", False)


def test_the_pass_builds_the_briefings_before_the_summary_and_records_them(two, env):
    env["broker"] = AccountBroker(order=env["order"])
    env["last_row"] = {"date": "2026-10-16", "equityEur": 1000.0}
    two["window"] = False
    assert run(pt._pass(force_snapshot=True)) is True
    (args, kwargs), = env["records"]
    assert "briefing daily, intraday; summary sent" in args[3] and kwargs == {"idle": False}
    assert len(two["alerts"]) == 1


def test_the_worker_status_names_the_halted_buckets(env, monkeypatch):
    monkeypatch.setattr(pt.paper_exec, "worker_state", lambda: {})
    assert pt.status()["halted"] is False and pt.status()["haltedBuckets"] == []
    pt.paper_ledger.write_halt({"halted": True, "reason": "x", "at": 1.0, "by": "owner"}, "intraday")
    s = pt.status()
    assert s["halted"] is True and s["haltedBuckets"] == ["intraday"]
