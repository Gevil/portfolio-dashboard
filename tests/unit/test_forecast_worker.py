"""Forecast worker (contract phase 6 section 3): the weekly / daily schedule, cohort windows, resume,
the manual trigger and the supervisor pass. ``forecasts.run`` (the engine, tested in test_forecasts.py) and every
clock are faked: no lane, no Claude, no network, no sleeps.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime
from zoneinfo import ZoneInfo

import pytest

from app.api import forecast_worker as W
from app.api import forecasts as F
from app.api import jsonstore, paper_universe

BERLIN = ZoneInfo("Europe/Berlin")
IDS = [f"S{i:02d}" for i in range(1, 7)]


def at(y, m, d, h=0, mi=0):
    """An instant in Berlin (the XETR clock: what the sessions are computed in)."""
    return datetime.datetime(y, m, d, h, mi, tzinfo=BERLIN).timestamp()


def local(y, m, d, h=0, mi=0):
    """The container-local instant the worker's deadlines are built from."""
    return datetime.datetime(y, m, d, h, mi).timestamp()


SAT = at(2026, 10, 24, 12, 0)             # the weekend after the 2026-10-23 close
TUE_EVENING = at(2026, 10, 27, 18, 30)


def summary(complete=True, **over):
    out = {"cohort": "x", "samples": {"asked": 1, "valid": 1, "invalid": 0, "laneErrors": 0}, "pending": [], "pendingD": [],
           "invalidPool": [], "packetFailed": {}, "complete": complete,
           "fallback": {"opus": [], "sonnet": [], "hold": []}, "d": {"done": [], "failed": []}}
    out.update(over)
    return out


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name, fname in (("LOG_FILE", "forecast_log.jsonl"), ("LEDGER_FILE", "trial_ledger.json"),
                        ("SUBSET_FILE", "forecast_subset.json"), ("COHORTS_FILE", "forecast_cohorts.json"),
                        ("LABELS_FILE", "forecast_labels.json")):
        monkeypatch.setattr(F, name, tmp_path / fname)
    store = tmp_path / "universe.json"
    entries = [{"id": i, "name": i, "isin": f"DE00000000{n:02d}", "yahoo": f"{i}.DE", "venue": "XETR", "kind": "equity",
                "sector": "Tech", "role": "candidate", "t212": None} for n, i in enumerate(IDS)]
    entries.append({"id": "EUNL", "name": "World", "isin": "IE00B4L5Y983", "yahoo": "EUNL.DE", "venue": "XETR",
                    "kind": "etf", "sector": "ETF", "role": "benchmark", "t212": None})
    jsonstore.save(store, {"asOf": "2026-10-21", "source": "test", "entries": entries})
    monkeypatch.setattr(paper_universe, "STORE_FILE", store)
    monkeypatch.setattr(paper_universe, "SEED_FILE", tmp_path / "no-seed.json")
    monkeypatch.setenv("FORECAST_ENABLED", "1")
    st = {"now": SAT, "held": [], "runs": [], "result": summary, "gate": None, "labels": [], "records": []}

    async def fake_run(ids, **kw):
        st["runs"].append({"ids": list(ids), **kw})
        kw["progress"].update(phase="done", done=len(ids), total=len(ids))
        if st["gate"] is not None:
            await st["gate"].wait()
        res = st["result"]
        return res() if callable(res) else res

    async def update_labels(now=None):
        st["labels"].append(now)
        return {"added": 0, "pending": 0}

    monkeypatch.setattr(W, "_now", lambda: st["now"])
    monkeypatch.setattr(W, "held_ids", lambda: list(st["held"]))
    monkeypatch.setattr(W.forecasts, "run", fake_run)
    monkeypatch.setattr(W.forecasts, "update_labels", update_labels)
    monkeypatch.setattr(W.runlog, "record", lambda *a, **k: st["records"].append((a, k)))
    monkeypatch.setattr(W, "_task", None)
    W._job.update(task=None, cohort=None, kind=None, startedAt=None, progress={}, lastResult=None)
    W._labels.update(at=0.0, last=None)
    W._stats.update(running=False, enabled=False, last_run=0.0, runs=0, errors=0, lastError=None)
    return st


def weekly_record(status="done", as_of="2026-10-23", ids=IDS):
    return {"id": f"w{as_of}", "kind": "weekly", "asOf": as_of, "ids": ids, "status": status, "arms": list(F.ARMS),
            "horizons": [5, 20], "priority": [], "deadline": local(2026, 10, 26, 8, 0), "attempts": 0}


def seed_weekly_forecasts():
    F.put_cohort(weekly_record())
    rows = [{"kind": "aggregate", "cohort": "w2026-10-23", "asOf": "2026-10-23", "arm": "B", "horizon": 20, "source": "local",
             "role": "scheduled", "id": i, "p": p, "createdAt": 1} for i, p in zip(IDS, [0.51, 0.58, 0.55, 0.49, 0.6, 0.53])]
    rows += [{**rows[3], "source": "opus", "role": "fallback", "p": 0.9},          # a fallback row never ranks a name
             {**rows[0], "horizon": 5, "p": 0.99}]                                   # nor does another horizon
    F.append_rows(rows)


# ---------------------------------------------------------------- schedule

@pytest.mark.parametrize("when,due", [
    (at(2026, 10, 23, 14, 0), None),                    # Friday before the close: last completed is Thursday
    (at(2026, 10, 23, 17, 40), None),                   # inside the 20 min bar grace
    (at(2026, 10, 23, 18, 0), "2026-10-23"),
    (at(2026, 10, 24, 12, 0), "2026-10-23"),            # the weekend
    (at(2026, 10, 26, 7, 0), "2026-10-23"),             # Monday before the deadline
    (at(2026, 10, 28, 18, 30), None),                   # a Wednesday close is not the last of its week
    (at(2027, 3, 25, 18, 30), "2027-03-25"),            # Thursday: Good Friday is a holiday, the week ends there
])
def test_weekly_cohort_is_due_from_the_weeks_last_close(env, when, due):
    got = W.weekly_due(when)
    assert (got.isoformat() if got else None) == due


def test_deadline_is_eight_local_on_the_next_session_day():
    assert W.deadline_for(datetime.date(2026, 10, 23)) == local(2026, 10, 26, 8, 0)        # Friday -> Monday
    assert W.deadline_for(datetime.date(2026, 10, 27)) == local(2026, 10, 28, 8, 0)
    assert W.deadline_for(datetime.date(2027, 3, 25)) == local(2027, 3, 30, 8, 0)          # Good Friday + Easter Monday


def test_a_weekly_cohort_covers_every_candidate_and_freezes_its_config(env):
    rec, note = W._pick(SAT)
    assert note == "start w2026-10-23" and rec["id"] == "w2026-10-23" and rec["kind"] == "weekly"
    assert rec["ids"] == IDS and "EUNL" not in rec["ids"]                  # candidates only, benchmark excluded
    assert rec["arms"] == list(F.ARMS) and rec["horizons"] == [5, 20] and rec["priority"] == []
    assert rec["deadline"] == local(2026, 10, 26, 8, 0) and rec["status"] == "running"
    assert F.read_cohorts()["w2026-10-23"] == rec
    assert rec["subset"] == F.stored_subset() and len(rec["subset"]) == 6      # fewer candidates than SUBSET_SIZE
    again, note = W._pick(SAT)                                                  # a running cohort is resumed first
    assert again["id"] == rec["id"] and note == "resume w2026-10-23"


def test_a_cohort_window_that_closed_is_recorded_missed_and_never_filled_in(env):
    env["now"] = at(2026, 10, 26, 8, 30)                                       # Monday, after the 08:00 deadline
    rec, note = W._pick(env["now"])
    assert rec is None and note == "w2026-10-23 missed"
    saved = F.read_cohorts()["w2026-10-23"]
    assert saved["status"] == "missed" and saved["ids"] == []
    assert W._pick(env["now"]) == (None, "idle")                               # and it is not retried
    assert env["runs"] == []


def test_the_daily_cohort_is_held_names_first_then_the_top_candidates_of_the_last_weekly_cohort(env):
    seed_weekly_forecasts()
    env["held"] = ["S02", "ZZZ"]                                               # an id the registry does not know is dropped
    assert W.top_candidates(3) == ["S05", "S02", "S03"]
    rec, note = W._pick(TUE_EVENING)
    assert note == "start d2026-10-27" and rec["kind"] == "daily"
    assert rec["ids"] == ["S02", "S05", "S03", "S06", "S01", "S04"] and rec["priority"] == ["S02"]
    assert rec["arms"] == ["B"] and rec["horizons"] == [5, 20] and rec["deadline"] == local(2026, 10, 28, 8, 0)
    assert W._pick(TUE_EVENING)[1] == "resume d2026-10-27"


def test_no_daily_cohort_on_the_weekly_day_or_with_nothing_to_forecast(env):
    seed_weekly_forecasts()
    env["held"] = ["S02"]
    assert W._pick(SAT) == (None, "idle")                                      # the weekly cohort covers Friday
    F.put_cohort({**weekly_record(), "status": "done"})
    env["held"] = []
    F.COHORTS_FILE.unlink()                                                    # no weekly cohort, no held name
    assert W._pick(TUE_EVENING) == (None, "idle: no daily ids yet") and F.read_cohorts() == {}


def test_a_daily_window_that_closed_is_missed(env):
    env["held"] = ["S02"]
    env["now"] = at(2026, 10, 28, 9, 0)                                        # Wednesday morning: Tuesday's deadline is past
    rec, note = W._pick(env["now"])
    assert rec is None and note == "d2026-10-27 missed" and F.read_cohorts()["d2026-10-27"]["status"] == "missed"


def test_unfinished_cohorts_resume_weekly_first(env):
    F.put_cohort({**weekly_record("running"), "id": "w2026-10-16", "asOf": "2026-10-16"})
    F.put_cohort({"id": "d2026-10-22", "kind": "daily", "asOf": "2026-10-22", "ids": ["S01"], "status": "running"})
    F.put_cohort(weekly_record("done"))
    assert W._pick(TUE_EVENING)[0]["id"] == "w2026-10-16"


def test_held_names_come_from_the_practice_buckets_own_fills(monkeypatch):
    rows = [{"id": "S01", "side": "buy", "filledQuantity": 2.0, "state": "filled", "filledValueEur": 20.0, "bucket": "daily"},
            {"id": "S02", "side": "buy", "filledQuantity": 1.0, "state": "filled", "filledValueEur": 10.0, "bucket": "intraday"},
            {"id": "S03", "side": "buy", "filledQuantity": 1.0, "state": "shadow", "filledValueEur": 10.0, "bucket": "daily"},
            {"id": "S04", "side": "buy", "filledQuantity": 1.0, "state": "filled", "filledValueEur": 10.0, "bucket": "daily"},
            {"id": "S04", "side": "sell", "filledQuantity": 1.0, "state": "filled", "filledValueEur": 10.0, "bucket": "daily"}]
    monkeypatch.setattr(W.paper_ledger, "read_orders", lambda: rows)
    assert W.held_ids() == ["S01", "S02"]                                      # shadow rows and closed positions do not count


# ---------------------------------------------------------------- the job

def test_the_job_runs_the_cohort_with_its_frozen_record_and_marks_it_done(env):
    rec = weekly_record("running")
    rec["priority"] = ["S02"]
    asyncio.run(W._job_main(rec))
    call = env["runs"][0]
    assert call["ids"] == IDS and call["arms"] == tuple(F.ARMS) and call["horizons"] == (5, 20)
    assert call["cohort"] == "w2026-10-23" and call["role"] == "scheduled" and call["as_of"] == datetime.date(2026, 10, 23)
    assert call["deadline"] == rec["deadline"] and call["priority"] == ["S02"]
    saved = F.read_cohorts()["w2026-10-23"]
    assert saved["status"] == "done" and saved["result"]["complete"] is True and saved["attempts"] == 0
    assert saved["result"]["fallback"] == {"opus": 0, "sonnet": 0, "hold": 0}
    assert W._job["task"] is None and W._job["lastResult"] == {"cohort": "w2026-10-23", "status": "done", "complete": True}


def test_an_incomplete_run_stays_running_until_its_deadline_then_is_partial(env):
    env["result"] = lambda: summary(False, pending=["S03"])
    env["now"] = at(2026, 10, 24, 12, 0)
    asyncio.run(W._job_main(weekly_record("running")))
    assert F.read_cohorts()["w2026-10-23"]["status"] == "running"             # the next pass resumes it
    env["now"] = at(2026, 10, 26, 8, 1)
    asyncio.run(W._job_main(weekly_record("running")))
    assert F.read_cohorts()["w2026-10-23"]["status"] == "partial"             # past the deadline: final


def test_a_crashing_run_is_retried_and_given_up_after_five_attempts(env):
    def boom():
        raise RuntimeError("log not writable")

    env["result"] = boom
    rec = weekly_record("running")
    for n in range(1, W.MAX_ATTEMPTS):
        asyncio.run(W._job_main(rec))
        assert rec["status"] == "running" and rec["attempts"] == n
    asyncio.run(W._job_main(rec))
    saved = F.read_cohorts()["w2026-10-23"]
    assert saved["status"] == "failed" and "log not writable" in saved["lastError"]
    assert W._stats["errors"] == W.MAX_ATTEMPTS and "RuntimeError" in W._stats["lastError"]
    assert W._pick(SAT) == (None, "idle")                                       # a failed cohort is not resumed


# ---------------------------------------------------------------- manual trigger

def refused(coro_fn):
    with pytest.raises(W.RunRefused) as e:
        asyncio.run(coro_fn())
    return e.value


def test_manual_run_needs_the_gate_and_a_known_kind(env, monkeypatch):
    monkeypatch.delenv("FORECAST_ENABLED")
    e = refused(lambda: W.request_run("cohort", SAT))
    assert e.code == "disabled"
    monkeypatch.setenv("FORECAST_ENABLED", "1")
    assert refused(lambda: W.request_run("weekly", SAT)).code == "refused"


def test_manual_cohort_run_starts_in_the_background_and_a_finished_cohort_is_refused(env):
    async def main():
        info = await W.request_run("cohort", SAT)
        assert info == {"started": True, "kind": "cohort", "cohort": "w2026-10-23", "asOf": "2026-10-23", "ids": 6,
                        "deadline": local(2026, 10, 26, 8, 0)}
        await W._job["task"]
        return F.read_cohorts()["w2026-10-23"]["status"]

    assert asyncio.run(main()) == "done"
    assert "is done" in str(refused(lambda: W.request_run("cohort", SAT)))


def test_manual_run_refuses_a_second_run_a_wrong_week_day_and_a_closed_window(env):
    async def main():
        env["gate"] = asyncio.Event()
        await W.request_run("cohort", SAT)
        with pytest.raises(W.RunRefused, match="already in progress"):
            await W.request_run("daily", SAT)
        env["gate"].set()
        await W._job["task"]

    asyncio.run(main())
    assert "none is due" in str(refused(lambda: W.request_run("cohort", at(2026, 10, 28, 18, 30))))
    F.COHORTS_FILE.unlink()
    assert "window closed" in str(refused(lambda: W.request_run("cohort", at(2026, 10, 26, 9, 0))))


def test_manual_run_resumes_an_unfinished_cohort(env):
    F.put_cohort({**weekly_record("failed"), "attempts": 5})

    async def main():
        await W.request_run("cohort", SAT)
        await W._job["task"]

    asyncio.run(main())
    saved = F.read_cohorts()["w2026-10-23"]
    assert saved["status"] == "done" and saved["attempts"] == 0 and len(env["runs"]) == 1


def test_manual_daily_run(env):
    env["held"] = ["S03"]

    async def main():
        info = await W.request_run("daily", TUE_EVENING)
        await W._job["task"]
        return info

    info = asyncio.run(main())
    assert info["cohort"] == "d2026-10-27" and info["kind"] == "daily" and info["ids"] == 1
    assert env["runs"][0]["priority"] == ["S03"] and env["runs"][0]["role"] == "daily"
    env["held"] = []
    F.COHORTS_FILE.unlink()
    assert "nothing to forecast" in str(refused(lambda: W.request_run("daily", TUE_EVENING)))


# ---------------------------------------------------------------- supervisor pass

def test_a_pass_records_the_trial_ledger_starts_the_due_cohort_and_labels_hourly(env):
    async def main():
        ok, note, acted = await W._tick()
        assert ok and acted and "trial ledger: config changed" in note and "start w2026-10-23" in note
        await W._job["task"]
        env["now"] += 600
        ok2, note2, acted2 = await W._tick()
        return note2, acted2

    note2, acted2 = asyncio.run(main())
    assert len(F.ledger()["rows"]) == 1 and F.stored_subset()
    assert env["labels"] == [SAT]                                              # not again 10 minutes later
    assert note2 == "idle" and acted2 is False and len(env["runs"]) == 1
    env["now"] += 3600
    asyncio.run(W._tick())
    assert len(env["labels"]) == 2


def test_a_pass_reports_the_running_job_instead_of_starting_another(env):
    async def main():
        env["gate"] = asyncio.Event()
        await W._tick()
        await asyncio.sleep(0)
        _, note, acted = await W._tick()
        env["gate"].set()
        await W._job["task"]
        return note, acted

    note, acted = asyncio.run(main())
    assert "w2026-10-23: done 6/6" in note and acted is False and len(env["runs"]) == 1


def test_a_failing_schedule_is_a_failed_pass_not_a_dead_loop(env, monkeypatch):
    def boom(now):
        raise OSError("disk")

    monkeypatch.setattr(W, "_pick", boom)
    ok, note, acted = asyncio.run(W._tick())
    assert ok is False and "schedule: OSError" in note


def test_the_loop_records_every_pass_and_stop_cancels_it(env, monkeypatch):
    async def tick():
        return True, "idle", False

    monkeypatch.setattr(W, "_tick", tick)

    async def main():
        W.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running = W.status()["running"]
        await W.stop()
        return running

    assert asyncio.run(main()) is True
    assert W.status()["running"] is False and W._stats["runs"] == 1
    (args, kw), = env["records"]
    assert args[0] == "forecast_worker" and args[1] is True and args[3] == "idle" and kw == {"idle": True}


def test_a_disabled_worker_starts_nothing(env, monkeypatch):
    monkeypatch.delenv("FORECAST_ENABLED")

    async def main():
        W.start()
        return W._task

    assert asyncio.run(main()) is None and W.status()["enabled"] is False


def test_status_shape(env):
    F.put_cohort(weekly_record("running"))
    s = W.status()
    assert s["interval_s"] == 300 and s["enabled"] is True and s["job"]["running"] is False
    assert s["cohorts"] == {"w2026-10-23": "running"} and {"runs", "errors", "last_run", "running"} <= set(s)
