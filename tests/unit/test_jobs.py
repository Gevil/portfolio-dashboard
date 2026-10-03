"""jobs: mode-strength dedupe, cancel, restart semantics, resume hand-out, and
the serialized runner (tmp job table; the module state is reset per test)."""
import asyncio

import pytest

from app.api import jobs


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "analysis_jobs.json")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_queue", asyncio.Queue())
    monkeypatch.setattr(jobs, "_runner", None)
    monkeypatch.setattr(jobs, "_consumer", None)
    monkeypatch.setattr(jobs, "_workers", set())
    yield


def test_same_or_weaker_request_dedupes_onto_the_in_flight_job():
    first = jobs.enqueue("asml", "standard", "user")
    assert first["deduped"] is False and first["mode"] == "standard"
    same = jobs.enqueue("ASML", "standard", "user")
    weaker = jobs.enqueue("ASML", "quick", "digest")
    assert same["deduped"] and same["job_id"] == first["job_id"]
    assert weaker["deduped"] and weaker["job_id"] == first["job_id"]
    assert weaker["mode"] == "standard"          # the mode that will actually run
    assert len(jobs.list_jobs()) == 1


def test_stronger_request_is_not_swallowed_by_a_weaker_job():
    quick = jobs.enqueue("ASML", "quick", "digest")
    deep = jobs.enqueue("ASML", "deep", "user")
    assert deep["deduped"] is False and deep["mode"] == "deep"
    assert deep["behind"] == quick["job_id"] and deep["job_id"] != quick["job_id"]
    assert "queued behind quick" in jobs.get(deep["job_id"])["message"]
    # a later standard request is covered by the deep job, not the quick one
    std = jobs.enqueue("ASML", "standard", "user")
    assert std["deduped"] and std["job_id"] == deep["job_id"] and std["mode"] == "deep"


def test_other_tickers_are_independent_and_input_is_validated():
    a = jobs.enqueue("ASML", "quick", "user")
    b = jobs.enqueue("NVDA", "quick", "user")
    assert a["job_id"] != b["job_id"]
    with pytest.raises(ValueError):
        jobs.enqueue("ASML", "bogus", "user")
    with pytest.raises(ValueError):
        jobs.enqueue("  ", "quick", "user")


def test_running_job_covers_before_a_queued_one():
    queued = jobs.enqueue("ASML", "quick", "user")
    jobs._jobs[queued["job_id"]]["status"] = "running"
    assert jobs.in_flight("ASML")["id"] == queued["job_id"]
    assert jobs.in_flight("ASML", "standard") is None


def test_only_queued_jobs_can_be_cancelled():
    j = jobs.enqueue("ASML", "quick", "user")["job_id"]
    assert jobs.cancel(j) is True
    assert jobs.get(j)["status"] == "error" and jobs.get(j)["message"] == "cancelled by request"
    assert jobs.cancel(j) is False                       # no longer queued
    assert jobs.cancel("nope") is False
    # a cancelled job frees the ticker for a new request
    assert jobs.enqueue("ASML", "quick", "user")["deduped"] is False


def test_restart_settles_orphans_and_digest_jobs_are_handed_out_once():
    d = jobs.enqueue("ASML", "quick", "digest")["job_id"]
    u = jobs.enqueue("NVDA", "quick", "user")["job_id"]
    jobs._jobs[d]["status"] = "running"
    jobs.persist()

    async def boot():
        jobs._jobs.clear()
        jobs.start()                        # reload from disk like a new process
        await jobs.stop()
    asyncio.run(boot())
    # start() marked them as a restart; stop() then had nothing in flight
    assert jobs.get(d)["message"] == jobs.REASON_RESTART
    assert jobs.get(u)["status"] == "error"

    picked = jobs.take_interrupted("digest", 3600)
    assert [p["ticker"] for p in picked] == ["ASML"]           # not the user job
    assert jobs.take_interrupted("digest", 3600) == []         # handed out ONCE
    assert jobs.get(d)["resumed"] is True


def test_take_interrupted_ignores_old_and_genuinely_failed_jobs():
    old = jobs.enqueue("ASML", "quick", "digest")["job_id"]
    jobs._finish(jobs._jobs[old], "error", jobs.REASON_RESTART)
    jobs._jobs[old]["created_at"] = 1.0                  # long ago
    failed = jobs.enqueue("NVDA", "quick", "digest")["job_id"]
    jobs._finish(jobs._jobs[failed], "error", "lane_down")
    assert jobs.take_interrupted("digest", 3600) == []


def test_runner_outcomes_are_recorded_and_serialized():
    running = {"now": 0, "peak": 0}
    order = []

    async def runner(job):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.01)
        order.append(job["ticker"])
        if job["ticker"] == "BAD":
            job["status"] = "error"
            job["message"] = "budget: spent"
        if job["ticker"] == "BOOM":
            raise RuntimeError("kaput")
        running["now"] -= 1

    async def go():
        jobs.set_runner(runner)
        jobs.start()
        ids = {t: jobs.enqueue(t, "quick", "user")["job_id"]
               for t in ("OK", "BAD", "BOOM")}
        for _ in range(200):
            if all(jobs.get(i)["status"] in ("done", "error") for i in ids.values()):
                break
            await asyncio.sleep(0.01)
        out = {t: jobs.get(i) for t, i in ids.items()}
        await jobs.stop()
        return out

    out = asyncio.run(go())
    assert running["peak"] == 1 and order == ["OK", "BAD", "BOOM"]
    assert out["OK"]["status"] == "done"
    assert out["BAD"]["status"] == "error" and out["BAD"]["message"] == "budget: spent"
    assert out["BOOM"]["status"] == "error" and "kaput" in out["BOOM"]["message"]


def test_job_without_a_runner_fails_loudly():
    async def go():
        jobs.start()
        jid = jobs.enqueue("ASML", "quick", "user")["job_id"]
        for _ in range(100):
            if jobs.get(jid)["status"] != "queued":
                break
            await asyncio.sleep(0.01)
        res = jobs.get(jid)
        await jobs.stop()
        return res
    res = asyncio.run(go())
    assert res["status"] == "error" and "no analysis runner" in res["message"]
