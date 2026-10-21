"""Forecast HTTP routes (contract phase 6 section 3). The route coroutines are awaited directly (no TestClient:
that would start every background worker), the log / cohort files live in ``tmp_path`` and the worker's manual
trigger is faked.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import gzip
import json

import pytest

from app.api import forecast_packets as P
from app.api import forecast_routes as R
from app.api import forecast_worker as W
from app.api import forecasts as F
from app.api import paper_ledger


def run(coro):
    return asyncio.run(coro)


def body(resp):
    return json.loads(resp.body)


class Req:
    def __init__(self, payload=None, broken=False):
        self.payload, self.broken = payload, broken

    async def json(self):
        if self.broken:
            raise ValueError("not json")
        return self.payload


def get(**kw):
    args = {"book": "practice", "cohort": "", "id": "", "limit": 500, "samples": False, "bucket": ""}
    args.update(kw)
    return run(R.get_forecasts(**args))


def agg(cohort, as_of, pid, arm="B", p=0.55, horizon=20, **extra):
    return {"kind": "aggregate", "cohort": cohort, "asOf": as_of, "id": pid, "arm": arm, "source": "local", "role": "scheduled",
            "horizon": horizon, "p": p, "n": 8, "createdAt": 1, **extra}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name, fname in (("LOG_FILE", "forecast_log.jsonl"), ("LEDGER_FILE", "trial_ledger.json"),
                        ("SUBSET_FILE", "forecast_subset.json"), ("COHORTS_FILE", "forecast_cohorts.json"),
                        ("LABELS_FILE", "forecast_labels.json")):
        monkeypatch.setattr(F, name, tmp_path / fname)
    monkeypatch.setattr(W, "status", lambda: {"running": True, "enabled": True, "interval_s": 300, "job": {"running": False}})
    return {}


@pytest.fixture
def logged(env):
    F.put_cohort({"id": "w2026-10-16", "kind": "weekly", "asOf": "2026-10-16", "ids": ["S01", "S02"], "status": "done",
                  "deadline": 1.0, "arms": ["B"]})
    F.put_cohort({"id": "w2026-10-23", "kind": "weekly", "asOf": "2026-10-23", "ids": ["S01", "S02"], "status": "running"})
    F.append_rows([agg("w2026-10-16", "2026-10-16", "S01", p=0.41), agg("w2026-10-16", "2026-10-16", "S02", p=0.42),
                   agg("w2026-10-23", "2026-10-23", "S01", p=0.51), agg("w2026-10-23", "2026-10-23", "S02", p=0.52),
                   agg("w2026-10-23", "2026-10-23", "S01", arm="A", p=0.6, horizon=5),
                   {"kind": "sample", "cohort": "w2026-10-23", "asOf": "2026-10-23", "id": "S01", "arm": "B", "slot": 0,
                    "horizon": 5, "p": 0.6, "source": "local", "role": "scheduled", "createdAt": 1}])
    F.append_rows([agg("d2026-10-27", "2026-10-27", "S01", p=0.49, role="daily")])


def test_every_route_needs_book_practice(env):
    for bad in ("", "real", "live", None):
        r = get(book=bad)
        assert r.status_code == 400 and body(r) == {"error": "book must be 'practice'"}
        r = run(R.get_arms(book=bad))
        assert r.status_code == 400
        r = run(R.post_forecast_run(Req({"book": bad, "kind": "cohort"})))
        assert r.status_code == 400


def test_the_default_listing_is_the_newest_cohorts_aggregates_newest_first(logged):
    out = body(get())
    assert out["cohort"] == "d2026-10-27" and [i["id"] for i in out["items"]] == ["S01"]       # the daily cohort is the newest
    assert [c["id"] for c in out["cohorts"]] == ["d2026-10-27", "w2026-10-23", "w2026-10-16"]
    assert out["arms"] == list(F.ARMS) and out["book"] == "practice"
    weekly = next(c for c in out["cohorts"] if c["id"] == "w2026-10-16")
    assert (weekly["kind"], weekly["status"], weekly["ids"], weekly["deadline"]) == ("weekly", "done", 2, 1.0)
    assert next(c for c in out["cohorts"] if c["id"] == "d2026-10-27")["kind"] == "daily"       # no record: derived from the id


def test_filter_by_cohort_and_id_and_samples_on_request(logged):
    out = body(get(cohort="w2026-10-23"))
    assert [(i["id"], i["arm"], i["horizon"]) for i in out["items"]] == [("S01", "A", 5), ("S02", "B", 20), ("S01", "B", 20)]
    assert all(i["kind"] == "aggregate" for i in out["items"])                  # newest first, samples hidden
    with_samples = body(get(cohort="w2026-10-23", id="s01", samples=True))
    assert with_samples["id"] == "S01" and {i["kind"] for i in with_samples["items"]} == {"aggregate", "sample"}
    across = body(get(id="S01"))
    assert {i["cohort"] for i in across["items"]} == {"w2026-10-16", "w2026-10-23", "d2026-10-27"} and across["cohort"] is None
    assert {i["source"] for i in across["items"]} == {"local"} and all("role" in i for i in across["items"])


def test_unknown_cohort_is_404_and_limit_is_clamped(logged):
    r = get(cohort="w1999-01-01")
    assert r.status_code == 404 and body(r) == {"error": "unknown cohort"}
    assert len(body(get(cohort="w2026-10-23", limit=1))["items"]) == 1
    assert len(body(get(cohort="w2026-10-23", limit=0))["items"]) == 1
    assert len(body(get(cohort="w2026-10-23", limit=-5))["items"]) == 1


def test_an_empty_log_is_an_empty_listing(env):
    out = body(get())
    assert out["items"] == [] and out["cohorts"] == [] and out["cohort"] is None


def test_arms_payload_carries_grading_comparisons_readout_and_the_worker(env):
    out = body(run(R.get_arms(book="practice")))
    assert out["arms"] == list(F.ARMS) and out["cells"] == [] and out["worker"]["interval_s"] == 300
    assert len(out["comparisons"]) == 8 and {c["status"] for c in out["comparisons"]} == {"pending"}
    assert [c["id"] for c in out["comparisons"] if c["horizon"] == 5] == ["B-A", "B-Q", "D-B", "E-B"]
    assert out["readout"]["weeks"] == [4, 8, 12] and "experiment" in out


def test_post_validates_the_body_before_anything_starts(env, monkeypatch):
    started = []

    async def request_run(kind, now=None):
        started.append(kind)
        return {"started": True, "kind": kind, "cohort": "w2026-10-23", "asOf": "2026-10-23", "ids": 6, "deadline": 1.0}

    monkeypatch.setattr(W, "request_run", request_run)
    assert run(R.post_forecast_run(Req(broken=True))).status_code == 400
    assert run(R.post_forecast_run(Req(["cohort"]))).status_code == 400
    assert run(R.post_forecast_run(Req({"kind": "cohort"}))).status_code == 400
    r = run(R.post_forecast_run(Req({"book": "practice", "kind": "weekly"})))
    assert r.status_code == 400 and "kind" in body(r)["error"]
    assert started == []
    ok = run(R.post_forecast_run(Req({"book": "practice", "kind": "daily"})))
    assert ok.status_code == 202 and body(ok)["started"] is True and started == ["daily"]


def test_post_maps_refusals(env, monkeypatch):
    async def disabled(kind, now=None):
        raise W.RunRefused("forecaster disabled (FORECAST_ENABLED=1)", "disabled")

    async def busy(kind, now=None):
        raise W.RunRefused("a run is already in progress (w2026-10-23)")

    monkeypatch.setattr(W, "request_run", disabled)
    r = run(R.post_forecast_run(Req({"book": "practice", "kind": "cohort"})))
    assert r.status_code == 503 and body(r)["error"] == "forecast_disabled"
    monkeypatch.setattr(W, "request_run", busy)
    r = run(R.post_forecast_run(Req({"book": "practice", "kind": "cohort"})))
    assert r.status_code == 409 and body(r) == {"error": "a run is already in progress (w2026-10-23)"}


def test_the_router_exposes_exactly_the_contract_routes():
    paths = {(tuple(sorted(r.methods)), r.path) for r in R.router.routes}
    assert paths == {(("GET",), "/api/paper/forecasts"), (("GET",), "/api/paper/forecasts/packet"),
                     (("GET",), "/api/paper/arms"), (("POST",), "/api/paper/forecast-run")}


# ------------------------------------------------------------------ name / sector / held

def _registry(monkeypatch, rows):
    monkeypatch.setattr(R.paper_universe, "get",
                        lambda pid: dict(rows[pid]) if isinstance(pid, str) and pid in rows else None)


def _fills(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(paper_ledger, "ORDERS_FILE", tmp_path / "orders.json")
    paper_ledger.save_orders(rows)


def _fill(pid, side, qty, bucket=None):
    row = {"id": pid, "ticker": f"{pid}d_EQ", "side": side, "quantity": qty, "filledQuantity": qty,
           "filledValueEur": qty * 10.0, "state": "filled"}
    if bucket:
        row["bucket"] = bucket
    return row


def test_rows_carry_name_sector_and_whether_the_bucket_holds_the_name(logged, monkeypatch, tmp_path):
    _registry(monkeypatch, {"S01": {"id": "S01", "name": "Sample One", "sector": "Industrials"}})
    _fills(monkeypatch, tmp_path, [_fill("S01", "buy", 2.0), _fill("S02", "buy", 1.0),
                                   _fill("S02", "sell", 1.0),                    # sold out: not held
                                   _fill("S02", "buy", 3.0, bucket="intraday")])
    items = body(get(cohort="w2026-10-23"))["items"]
    s01 = [i for i in items if i["id"] == "S01"]
    s02 = [i for i in items if i["id"] == "S02"]
    assert s01 and all((i["name"], i["sector"], i["held"]) == ("Sample One", "Industrials", True) for i in s01)
    assert s02 and all((i["name"], i["sector"], i["held"]) == (None, None, False) for i in s02)
    assert all(i["p"] is not None for i in items)                                # the log row is untouched
    intraday = body(get(cohort="w2026-10-23", bucket="intraday"))["items"]
    assert {i["id"]: i["held"] for i in intraday} == {"S01": False, "S02": True}


def test_an_unknown_bucket_is_a_400_and_a_broken_ledger_holds_nothing(logged, monkeypatch, tmp_path):
    r = get(bucket="weekly")
    assert r.status_code == 400

    def boom():
        raise RuntimeError("disk")

    monkeypatch.setattr(paper_ledger, "read_orders", boom)
    items = body(get(cohort="w2026-10-23"))["items"]
    assert items and all(i["held"] is False for i in items)


# ------------------------------------------------------------------ frozen packet

@pytest.fixture
def packets(monkeypatch, tmp_path):
    root = tmp_path / "data" / "forecast_packets"
    monkeypatch.setattr(P, "PACKET_DIR", root)
    packet = {"schemaVersion": 1, "id": "S01", "asOf": "2026-10-23", "price": {"as_of": "x", "v": 1.5}}
    doc = {"sha256": P.packet_hash(packet), "packet": packet}
    (root / "w2026-10-23").mkdir(parents=True)
    (root / "w2026-10-23" / "S01.json.gz").write_bytes(gzip.compress(json.dumps(doc).encode()))
    (tmp_path / "data" / "secret.json.gz").write_bytes(gzip.compress(json.dumps({"sha256": "x", "packet": {"leak": 1}}).encode()))
    return {"root": root, "packet": packet}


def packet(**kw):
    args = {"book": "practice", "cohort": "w2026-10-23", "id": "S01"}
    args.update(kw)
    return run(R.get_forecast_packet(**args))


def test_the_packet_route_returns_the_frozen_packet(packets):
    r = packet(id="s01")                                   # ids are upper-cased like the list route
    out = body(r)
    assert r.status_code == 200 and out["packet"] == packets["packet"] and out["id"] == "S01"
    assert out["sha256"] == P.packet_hash(packets["packet"]) and out["cohort"] == "w2026-10-23"


def test_an_absent_packet_is_404(packets):
    assert packet(id="S99").status_code == 404
    assert packet(cohort="w1999-01-01").status_code == 404


@pytest.mark.parametrize("cohort,pid", [
    ("..", "secret"), ("../..", "S01"), ("w2026-10-23", "../secret"), ("w2026-10-23/..", "secret"),
    ("w2026-10-23", "S01/../../secret"), ("w2026-10-23\\..", "S01"), ("", "S01"), ("w2026-10-23", ""),
    (".", "S01"), ("w2026-10-23", ".."), ("w2026-10-23", "S 01"), ("w2026-10-23", "S01\n"), ("%2e%2e", "S01"),
    ("/etc", "passwd"), ("w2026-10-23", "S01\x00"), ("é", "S01")])
def test_the_packet_route_rejects_unsafe_names(packets, cohort, pid):
    r = packet(cohort=cohort, id=pid)
    assert r.status_code == 400 and body(r) == {"error": "invalid cohort or id"}


def test_the_packet_route_needs_book_practice_and_reports_a_tampered_file(packets):
    assert packet(book="real").status_code == 400
    path = packets["root"] / "w2026-10-23" / "S01.json.gz"
    path.write_bytes(gzip.compress(json.dumps({"sha256": "0" * 64, "packet": packets["packet"]}).encode()))
    r = packet()
    assert r.status_code == 500 and body(r) == {"error": "packet unreadable"}
