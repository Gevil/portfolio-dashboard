"""ops_watch: 24 h re-alert, priority-3 warning from source ops_watch, the
"resolved" info row (stored, never pushed), Delivery discipline."""
import asyncio
import types

import pytest

from app.api import notify, ops_watch

T0 = 1_800_000_000.0
HOUR = 3600.0
FINDING = {"key": "degraded:ninfer", "title": "GPU lane ninfer degraded",
           "body": "health check failing"}


@pytest.fixture
def ow(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_watch, "STATE_FILE", tmp_path / "ops_state.json")
    h = {"now": T0, "findings": [dict(FINDING)], "alerts": [], "stored": [],
         "delivery": notify.Delivery(notify.SENT), "store_ok": True}

    async def collect():
        return list(h["findings"]), {"source": "bridge", "lane": None}

    async def alert(ticker, source, title, body, **kw):
        h["alerts"].append({"ticker": ticker, "source": source,
                            "title": title, "body": body, **kw})
        return h["delivery"]

    def store(ticker, source, title, body="", **kw):
        h["stored"].append({"ticker": ticker, "source": source,
                            "title": title, "body": body, **kw})
        return "row1" if h["store_ok"] else ""

    monkeypatch.setattr(ops_watch, "collect", collect)
    monkeypatch.setattr(ops_watch, "stale_worker_findings", lambda now=None: [])
    monkeypatch.setattr(ops_watch.notify, "alert", alert)
    monkeypatch.setattr(ops_watch.notify, "store", store)
    monkeypatch.setattr(ops_watch.runlog, "record", lambda *a, **k: None)
    monkeypatch.setattr(ops_watch, "time",
                        types.SimpleNamespace(time=lambda: h["now"]))
    return h


def run_pass(h, at):
    h["now"] = T0 + at
    return asyncio.run(ops_watch.run_pass())


def test_realert_interval_defaults_to_24_hours():
    assert ops_watch.REALERT_S == 24 * 3600


def test_persistent_finding_realerts_once_a_day(ow):
    assert run_pass(ow, 0)["fired"] == ["degraded:ninfer"]
    for hours in (6, 12, 23.9):                 # the old 6 h cadence is gone
        assert run_pass(ow, hours * HOUR)["fired"] == []
    assert len(ow["alerts"]) == 1
    assert run_pass(ow, 24 * HOUR)["fired"] == ["degraded:ninfer"]
    assert len(ow["alerts"]) == 2


def test_alert_is_a_priority_3_warning_from_the_ops_watch_source(ow):
    run_pass(ow, 0)
    [a] = ow["alerts"]
    assert (a["ticker"], a["source"]) == ("OPS", "ops_watch")
    assert a["priority"] == 3 and a["severity"] == "warning"
    assert a["title"] == FINDING["title"] and a["body"] == FINDING["body"]


def test_cleared_finding_leaves_one_resolved_info_row_and_no_push(ow):
    run_pass(ow, 0)
    ow["findings"] = []
    res = run_pass(ow, HOUR)
    assert res["resolved"] == ["degraded:ninfer"]
    [row] = ow["stored"]
    assert row["source"] == "ops_watch" and row["ticker"] == "OPS"
    assert row["title"] == "Resolved: GPU lane ninfer degraded"
    assert row["priority"] == 1 and row["severity"] == "info"
    assert len(ow["alerts"]) == 1               # resolved rows are not pushed
    assert ops_watch.status()["open_findings"] == []
    run_pass(ow, 2 * HOUR)
    assert len(ow["stored"]) == 1               # nothing more to resolve


def test_a_finding_that_returns_alerts_immediately(ow):
    run_pass(ow, 0)
    ow["findings"] = []
    run_pass(ow, HOUR)
    ow["findings"] = [dict(FINDING)]
    assert run_pass(ow, 2 * HOUR)["fired"] == ["degraded:ninfer"]
    assert len(ow["alerts"]) == 2


def test_failed_delivery_is_retried_next_pass_and_leaves_no_resolved_row(ow):
    ow["delivery"] = notify.Delivery(notify.FAILED)
    assert run_pass(ow, 0)["fired"] == []
    assert run_pass(ow, 300)["fired"] == []     # retried, still failing
    assert len(ow["alerts"]) == 2

    ow["findings"] = []                         # never alerted: nothing to resolve
    assert run_pass(ow, 600)["resolved"] == []
    assert ow["stored"] == []


@pytest.mark.parametrize("status", [notify.QUEUED, notify.FILTERED,
                                    notify.DUPLICATE])
def test_every_non_failed_delivery_starts_the_24h_clock(ow, status):
    ow["delivery"] = notify.Delivery(status)
    run_pass(ow, 0)
    run_pass(ow, HOUR)
    assert len(ow["alerts"]) == 1


def test_unstorable_resolved_row_is_retried(ow):
    run_pass(ow, 0)
    ow["findings"] = []
    ow["store_ok"] = False
    assert run_pass(ow, HOUR)["resolved"] == []
    assert ops_watch.status()["open_findings"] == ["degraded:ninfer"]
    ow["store_ok"] = True
    assert run_pass(ow, 2 * HOUR)["resolved"] == ["degraded:ninfer"]
    assert ops_watch.status()["open_findings"] == []
