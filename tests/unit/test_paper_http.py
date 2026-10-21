"""``?book=`` gating on /api/portfolio* and the /api/paper/* routes
(plan §4, contract §3.7). Route coroutines are awaited directly (no
TestClient/lifespan: that would start every background worker - this just
exercises the thin dispatch main.py adds around the existing handlers, not
the app lifecycle).

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import json

import pytest

from app import main
from app.api import config_store, paper_eval, paper_ledger

REAL_PAYLOAD = {"asOf": 1, "currency": "EUR", "fx": {"rate": None},
                "totals": {"valueEur": 100.0}, "positions": [],
                "benchmark": None, "warnings": []}


def run(coro):
    return asyncio.run(coro)


def body(response):
    return json.loads(response.body)


class Req:
    """The slice of ``starlette.Request`` the paper routes use."""

    def __init__(self, payload=None, broken=False):
        self.payload, self.broken = payload, broken

    async def json(self):
        if self.broken:
            raise ValueError("not json")
        return self.payload


@pytest.fixture(autouse=True)
def no_real_io(monkeypatch):
    """The ``real`` code path must stay untouched: fake it so these tests
    never hit the network, and so a byte-for-byte comparison is meaningful."""
    async def snapshot():
        return dict(REAL_PAYLOAD)

    async def history(rng):
        return {"range": rng, "points": []}

    async def performance_summary(rng):
        return {"range": rng, "twrPct": None}

    async def attribution(rng):
        return {"range": rng, "positions": []}

    async def risk():
        return {"asOf": 1, "exposures": {}}

    monkeypatch.setattr(main.portfolio_mod, "snapshot", snapshot)
    monkeypatch.setattr(main.portfolio_mod, "history", history)
    monkeypatch.setattr(main.portfolio_mod, "performance_summary", performance_summary)
    monkeypatch.setattr(main.portfolio_mod, "attribution", attribution)
    monkeypatch.setattr(main.risk_mod, "risk", risk)


@pytest.fixture
def paper(monkeypatch, tmp_path):
    """Paper gate open, ledger files in tmp, and every action recording its call."""
    for name in ("SEASON_FILE", "SNAPSHOTS_FILE", "ORDERS_FILE", "HALT_FILE"):
        monkeypatch.setattr(paper_ledger, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(main.paper_eval, "enabled", lambda: True)
    calls = []

    async def rebalance(**kw):
        calls.append(("rebalance", kw))
        return {"plan": {"intents": []}, "rows": []}

    async def seed(**kw):
        calls.append(("seed", kw))
        return {"plan": {"intents": []}, "rows": []}

    async def set_mode(mode, bucket="daily"):
        calls.append(("mode", mode, bucket))
        return mode

    async def halt(reason=None, bucket="daily"):
        calls.append(("halt", reason, bucket))
        return {"halted": True, "reason": reason}

    async def resume(bucket="daily"):
        calls.append(("resume", None, bucket))
        return {"halted": False}

    monkeypatch.setattr(main.paper_exec, "rebalance", rebalance)
    monkeypatch.setattr(main.paper_exec, "seed", seed)
    monkeypatch.setattr(main.paper_exec, "set_mode", set_mode)
    monkeypatch.setattr(main.paper_exec, "halt", halt)
    async def decide(*, trigger, now=None, bucket="daily"):
        calls.append(("decide", trigger, bucket))
        return {"decisionId": "2026-10-19-s1-1", "valid": True, "noChange": True}

    monkeypatch.setattr(main.paper_exec, "resume", resume)
    monkeypatch.setattr(main.paper_pm, "decide", decide)
    monkeypatch.setattr(main.paper_eval, "status",
                        lambda bucket="daily": {"enabled": True, "mode": "demo"})
    monkeypatch.setattr(main.paper_exec, "status_extra",
                        lambda bucket="daily": {"halt": {"halted": False}})
    monkeypatch.setattr(main.paper_trader, "status", lambda: {"running": True})
    monkeypatch.setattr(main.claude_client, "status", lambda: {"enabled": False})
    monkeypatch.setattr(main.forecast_worker, "status", lambda: {"enabled": False, "running": False})
    return calls


# --------------------------------------------------------- real unaffected

def test_real_portfolio_payload_is_byte_identical_when_book_is_real_or_absent():
    r = run(main.get_portfolio(book="real"))
    assert r.status_code == 200
    assert body(r) == REAL_PAYLOAD
    assert "book" not in body(r) and "capabilities" not in body(r)


def test_real_portfolio_never_touches_the_paper_modules(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the real book must not read paper state")
    for mod, names in ((paper_ledger, ("read_orders", "read_halt", "read_season", "ledger_cash")),
                       (main.paper_exec, ("status_extra",)),
                       (main.paper_eval, ("portfolio_snapshot", "status"))):
        for name in names:
            monkeypatch.setattr(mod, name, boom)
    r = run(main.get_portfolio(book="real"))
    assert r.status_code == 200 and body(r) == REAL_PAYLOAD
    assert "reservedEur" not in json.dumps(body(r))


def test_real_history_and_performance_and_risk_and_attribution_unaffected():
    assert body(run(main.get_portfolio_history(range="1M", book="real"))) == \
        {"range": "1M", "points": []}
    assert body(run(main.get_portfolio_performance(range="1M", book="real"))) == \
        {"range": "1M", "twrPct": None}
    assert body(run(main.get_portfolio_risk(book="real"))) == {"asOf": 1, "exposures": {}}
    assert body(run(main.get_portfolio_attribution(range="1M", book="real"))) == \
        {"range": "1M", "positions": []}


# ------------------------------------------------------------- unknown/live

@pytest.mark.parametrize("route,kwargs", [
    (main.get_portfolio, {}),
    (main.get_portfolio_history, {"range": "1M"}),
    (main.get_portfolio_performance, {"range": "1M"}),
    (main.get_portfolio_risk, {}),
    (main.get_portfolio_attribution, {"range": "1M"}),
])
def test_unknown_book_is_always_400(route, kwargs):
    r = run(route(book="sandbox", **kwargs))
    assert r.status_code == 400
    assert body(r) == {"error": "unknown book"}


@pytest.mark.parametrize("route,kwargs", [
    (main.get_portfolio, {}),
    (main.get_portfolio_history, {"range": "1M"}),
    (main.get_portfolio_performance, {"range": "1M"}),
])
def test_live_book_is_400_not_available_in_phase_1(route, kwargs):
    r = run(route(book="live", **kwargs))
    assert r.status_code == 400
    assert body(r) == {"error": "live book not available"}


@pytest.mark.parametrize("book", ["practice", "live"])
def test_risk_and_attribution_reject_any_non_real_book(book, monkeypatch):
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "0")   # practice would be disabled anyway
    r = run(main.get_portfolio_risk(book=book))
    assert r.status_code == 400 and body(r) == {"error": "not available for this book"}
    r = run(main.get_portfolio_attribution(range="1M", book=book))
    assert r.status_code == 400 and body(r) == {"error": "not available for this book"}


# ------------------------------------------------------------ practice gating

def test_practice_portfolio_503_when_disabled(monkeypatch):
    monkeypatch.setattr(paper_eval, "enabled", lambda: False)

    async def raise_disabled(bucket="daily"):
        raise paper_eval.PaperDisabled()
    monkeypatch.setattr(main.paper_eval, "portfolio_snapshot", raise_disabled)
    r = run(main.get_portfolio(book="practice"))
    assert r.status_code == 503 and body(r) == {"error": "paper_disabled"}


def test_practice_portfolio_503_broker_unreachable_without_any_snapshot(monkeypatch):
    async def raise_unavailable(bucket="daily"):
        raise paper_eval.PaperUnavailable()
    monkeypatch.setattr(main.paper_eval, "portfolio_snapshot", raise_unavailable)
    r = run(main.get_portfolio(book="practice"))
    assert r.status_code == 503 and body(r) == {"error": "broker_unreachable"}


def test_practice_portfolio_200_passthrough(monkeypatch):
    async def snap(bucket="daily"):
        return {"book": "practice", "positions": []}
    monkeypatch.setattr(main.paper_eval, "portfolio_snapshot", snap)
    r = run(main.get_portfolio(book="practice"))
    assert r.status_code == 200 and body(r) == {"book": "practice", "positions": []}


# ------------------------------------------------- every route rejects book != practice

ACTION_ROUTES = {
    "orders": lambda b: main.paper_orders(book=b, limit=50),
    "rebalance": lambda b: main.paper_rebalance(Req({"book": b, "targets": {"ASML": 10}, "dryRun": False})),
    "seed": lambda b: main.paper_seed(Req({"book": b, "dryRun": False})),
    "mode": lambda b: main.paper_mode(Req({"book": b, "mode": "demo"})),
    "halt": lambda b: main.paper_halt(Req({"book": b, "reason": "x"})),
    "resume": lambda b: main.paper_resume(Req({"book": b})),
    "season": lambda b: main.paper_start_season(Req({"book": b})),
    "status": lambda b: main.paper_status(book=b),
    "decisions": lambda b: main.paper_decisions(book=b, limit=20),
    "grading": lambda b: main.paper_grading(book=b),
    "decide": lambda b: main.paper_decide(Req({"book": b})),
    "new-season": lambda b: main.paper_new_season(Req({"book": b})),
}


@pytest.mark.parametrize("book", ["real", "live", "bogus", "Practice", " practice"])
@pytest.mark.parametrize("name", sorted(ACTION_ROUTES))
def test_every_paper_route_rejects_a_book_other_than_practice_without_acting(name, book, paper):
    r = run(ACTION_ROUTES[name](book))
    assert r.status_code == 400
    assert "book" in body(r)["error"]
    assert paper == []                          # no action ran, nothing was written


@pytest.mark.parametrize("name", sorted(set(ACTION_ROUTES) - {"season"}))
def test_an_empty_book_is_rejected_too(name, paper):
    # (/season keeps its Phase 1 behaviour: an absent or empty book means practice)
    r = run(ACTION_ROUTES[name](""))
    assert r.status_code == 400 and paper == []


@pytest.mark.parametrize("name", ["rebalance", "seed", "mode", "halt", "resume", "decide"])
def test_a_post_without_a_book_is_rejected(name, paper):
    routes = {"rebalance": main.paper_rebalance, "seed": main.paper_seed, "mode": main.paper_mode,
              "halt": main.paper_halt, "resume": main.paper_resume, "decide": main.paper_decide}
    r = run(routes[name](Req({"targets": {"ASML": 10}, "mode": "demo", "dryRun": False})))
    assert r.status_code == 400 and paper == []


@pytest.mark.parametrize("name", ["rebalance", "seed", "mode", "halt", "resume", "decide"])
@pytest.mark.parametrize("req", [Req(broken=True), Req([1, 2]), Req("x"), Req(None)])
def test_a_post_with_a_missing_or_non_object_body_is_400(name, req, paper):
    routes = {"rebalance": main.paper_rebalance, "seed": main.paper_seed, "mode": main.paper_mode,
              "halt": main.paper_halt, "resume": main.paper_resume, "decide": main.paper_decide}
    r = run(routes[name](req))
    assert r.status_code == 400 and paper == []


# --------------------------------------------------------------- GET status

def test_paper_status_merges_halt_windows_universe_and_the_worker(monkeypatch, paper):
    monkeypatch.setattr(main.paper_eval, "status", lambda bucket="daily": {
        "enabled": True, "mode": "demo",
        "universe": {"ASML": {"t212": "ASMLa_EQ", "kind": "equity"}}})
    monkeypatch.setattr(main.paper_exec, "status_extra", lambda bucket="daily": {
        "halt": {"halted": False}, "openIntents": 2, "lastReconcileAt": 5.0,
        "tradingWindow": {"XETR": {"open": True, "opensAt": 1, "closesAt": 2}}})
    monkeypatch.setattr(main.claude_client, "status", lambda: {"enabled": False})
    monkeypatch.setattr(main.forecast_worker, "status", lambda: {"enabled": False, "running": False})
    r = run(main.paper_status())
    assert r.status_code == 200
    assert body(r) == {
        "enabled": True, "mode": "demo",
        "universe": {"ASML": {"t212": "ASMLa_EQ", "kind": "equity"}},
        "halt": {"halted": False}, "openIntents": 2, "lastReconcileAt": 5.0,
        "tradingWindow": {"XETR": {"open": True, "opensAt": 1, "closesAt": 2}},
        "worker": {"running": True},
        "claude": {"enabled": False}, "forecaster": {"enabled": False, "running": False}}


def test_paper_status_is_always_200_and_book_is_optional(monkeypatch, paper):
    monkeypatch.setattr(main.paper_eval, "status", lambda bucket="daily": {"enabled": False, "reason": "x"})
    for kwargs in ({}, {"book": None}, {"book": "practice"}):
        r = run(main.paper_status(**kwargs))
        assert r.status_code == 200 and body(r)["reason"] == "x"


# --------------------------------------------------------------- GET orders

def test_orders_are_newest_first_with_the_halt_row(paper):
    paper_ledger.save_orders([{"intentId": f"i{n}", "state": "filled"} for n in range(1, 5)])
    paper_ledger.write_halt({"halted": True, "reason": "dd", "at": 1.0, "by": "kill_switch",
                             "resumedAt": None, "resumedBy": None})
    r = run(main.paper_orders(book="practice", limit=3))
    assert r.status_code == 200
    out = body(r)
    assert [x["intentId"] for x in out["items"]] == ["i4", "i3", "i2"]
    assert out["halt"]["halted"] is True and out["halt"]["reason"] == "dd"


def test_orders_limit_is_clamped_to_a_sane_range(paper):
    paper_ledger.save_orders([{"intentId": f"i{n}"} for n in range(1, 4)])
    assert len(body(run(main.paper_orders(book="practice", limit=0)))["items"]) == 1
    assert len(body(run(main.paper_orders(book="practice", limit=-5)))["items"]) == 1
    assert len(body(run(main.paper_orders(book="practice", limit=10 ** 6)))["items"]) == 3


def test_orders_is_503_when_paper_trading_is_disabled(monkeypatch, paper):
    monkeypatch.setattr(main.paper_eval, "enabled", lambda: False)
    r = run(main.paper_orders(book="practice", limit=50))
    assert r.status_code == 503 and body(r) == {"error": "paper_disabled"}


# ----------------------------------------------------- rebalance / seed routes

def test_rebalance_forwards_targets_cash_and_an_explicit_dry_run(paper):
    r = run(main.paper_rebalance(Req({"book": "practice", "targets": {"ASML": 30}, "cashPct": 5,
                                      "dryRun": False})))
    assert r.status_code == 200 and body(r) == {"plan": {"intents": []}, "rows": []}
    assert paper == [("rebalance", {"targets": {"ASML": 30}, "cash_pct": 5, "dry_run": False,
                                    "bucket": "daily"})]


def test_a_missing_dry_run_flag_means_a_dry_run(paper):
    run(main.paper_rebalance(Req({"book": "practice", "targets": {"ASML": 30}})))
    run(main.paper_seed(Req({"book": "practice"})))
    assert [(name, kw["dry_run"]) for name, kw in paper] == [("rebalance", True), ("seed", True)]


@pytest.mark.parametrize("flag", ["false", 0, 1, None, "true"])
@pytest.mark.parametrize("route", ["rebalance", "seed"])
def test_dry_run_must_be_a_boolean(route, flag, paper):
    fn = main.paper_rebalance if route == "rebalance" else main.paper_seed
    r = run(fn(Req({"book": "practice", "targets": {"ASML": 30}, "dryRun": flag})))
    assert r.status_code == 400 and body(r) == {"error": "dryRun must be a boolean"}
    assert paper == []


def test_seed_forwards_the_dry_run_flag(paper):
    run(main.paper_seed(Req({"book": "practice", "dryRun": False})))
    assert paper == [("seed", {"dry_run": False, "bucket": "daily"})]


@pytest.mark.parametrize("exc,status,payload", [
    (paper_eval.PaperConflict("bucket not empty"), 409, {"error": "bucket not empty"}),
    (paper_eval.PaperBadRequest("targets must be a non-empty object"), 400,
     {"error": "targets must be a non-empty object"}),
    (paper_eval.PaperDisabled(), 503, {"error": "paper_disabled"}),
    (paper_eval.PaperUnavailable(), 503, {"error": "broker_unreachable"}),
    (paper_eval.PaperUnavailable("could not persist the order ledger"), 503,
     {"error": "could not persist the order ledger"}),
])
@pytest.mark.parametrize("route", ["rebalance", "seed"])
def test_trade_routes_map_paper_exceptions_to_status_codes(route, exc, status, payload, monkeypatch, paper):
    async def raiser(**kw):
        raise exc
    monkeypatch.setattr(main.paper_exec, route, raiser)
    fn = main.paper_rebalance if route == "rebalance" else main.paper_seed
    r = run(fn(Req({"book": "practice", "targets": {"ASML": 1}, "dryRun": False})))
    assert r.status_code == status and body(r) == payload


def test_an_unexpected_exception_is_not_swallowed(monkeypatch, paper):
    async def raiser(**kw):
        raise RuntimeError("bug")
    monkeypatch.setattr(main.paper_exec, "rebalance", raiser)
    with pytest.raises(RuntimeError):
        run(main.paper_rebalance(Req({"book": "practice", "targets": {"ASML": 1}, "dryRun": True})))


# ---------------------------------------------------------------- mode route

def test_mode_writes_through_paper_exec_and_returns_the_status_payload(paper):
    r = run(main.paper_mode(Req({"book": "practice", "mode": "shadow"})))
    assert r.status_code == 200
    assert paper == [("mode", "shadow", "daily")]
    assert body(r) == {"enabled": True, "mode": "demo", "halt": {"halted": False},
                       "worker": {"running": True}, "claude": {"enabled": False},
                       "forecaster": {"enabled": False, "running": False}}


@pytest.mark.parametrize("exc,status,payload", [
    (paper_eval.PaperBadRequest("mode must be one of off, shadow, demo"), 400,
     {"error": "mode must be one of off, shadow, demo"}),
    (paper_eval.PaperDisabled(), 503, {"error": "paper_disabled"}),
    (config_store.ConfigWriteError("disk full"), 503, {"error": "config could not be written"}),
    (config_store.ConfigUnreadable("corrupt"), 503, {"error": "config could not be written"}),
])
def test_mode_maps_errors(exc, status, payload, monkeypatch, paper):
    async def raiser(mode, bucket="daily"):
        raise exc
    monkeypatch.setattr(main.paper_exec, "set_mode", raiser)
    r = run(main.paper_mode(Req({"book": "practice", "mode": "demo"})))
    assert r.status_code == status and body(r) == payload


# ------------------------------------------------------------ halt / resume

def test_halt_forwards_the_reason_and_returns_the_halt_row(paper):
    r = run(main.paper_halt(Req({"book": "practice", "reason": "news"})))
    assert r.status_code == 200 and body(r) == {"halted": True, "reason": "news"}
    assert paper == [("halt", "news", "daily")]


def test_halt_reason_is_optional_but_must_be_a_string(paper):
    run(main.paper_halt(Req({"book": "practice"})))
    assert paper == [("halt", None, "daily")]
    paper.clear()
    r = run(main.paper_halt(Req({"book": "practice", "reason": 5})))
    assert r.status_code == 400 and paper == []


def test_halt_and_resume_work_even_when_trading_is_gated_off(monkeypatch, paper):
    """A safety stop must never be blocked by the feature gate."""
    monkeypatch.setattr(main.paper_eval, "enabled", lambda: False)
    assert run(main.paper_halt(Req({"book": "practice"}))).status_code == 200
    assert run(main.paper_resume(Req({"book": "practice"}))).status_code == 200
    assert [call[0] for call in paper] == ["halt", "resume"]


def test_resume_returns_the_halt_row(paper):
    r = run(main.paper_resume(Req({"book": "practice"})))
    assert r.status_code == 200 and body(r) == {"halted": False}


def test_a_halt_that_cannot_be_persisted_is_503_with_the_reason(monkeypatch, paper):
    async def raiser(reason=None, bucket="daily"):
        raise paper_eval.PaperUnavailable("could not persist the halt")
    monkeypatch.setattr(main.paper_exec, "halt", raiser)
    r = run(main.paper_halt(Req({"book": "practice"})))
    assert r.status_code == 503 and body(r) == {"error": "could not persist the halt"}


# ------------------------------------------------------------------ season

@pytest.mark.parametrize("exc,status", [
    (paper_eval.PaperConflict("conflict"), 409),
    (paper_eval.PaperBadRequest("bad"), 400),
    (paper_eval.PaperDisabled(), 503),
    (paper_eval.PaperUnavailable(), 503),
])
def test_paper_season_maps_paper_eval_exceptions_to_status_codes(monkeypatch, exc, status):
    async def raiser(**kw):
        raise exc
    monkeypatch.setattr(main.paper_eval, "create_season", raiser)
    r = run(main.paper_start_season(Req({"book": "practice"})))
    assert r.status_code == status


def test_paper_season_200_passthrough(monkeypatch):
    async def ok(**kw):
        return {"inceptionDate": "2026-10-19"}
    monkeypatch.setattr(main.paper_eval, "create_season", ok)
    r = run(main.paper_start_season(Req({"book": "practice", "reset": True})))
    assert r.status_code == 200 and body(r) == {"inceptionDate": "2026-10-19"}


def test_the_forecaster_router_and_worker_are_wired_in():
    paths = set(main.app.openapi()["paths"])
    assert {"/api/paper/forecasts", "/api/paper/arms", "/api/paper/forecast-run"} <= paths
    assert main.forecast_worker in main.BACKGROUND_MODULES


def test_new_season_passes_the_bucket_and_version_through(monkeypatch):
    seen = []

    async def new_season(**kw):
        seen.append(kw)
        return {"strategyVersion": kw["strategy_version"], "inceptionDate": "2026-10-21"}
    monkeypatch.setattr(main.paper_exec, "new_season", new_season)
    r = run(main.paper_new_season(Req({"book": "practice", "bucket": "intraday",
                                       "strategyVersion": "i2"})))
    assert r.status_code == 200 and body(r)["strategyVersion"] == "i2"
    assert seen == [{"book": "practice", "bucket": "intraday", "strategy_version": "i2"}]
    assert run(main.paper_new_season(Req({"book": "practice", "bucket": "weekly"}))).status_code == 400
    assert len(seen) == 1


@pytest.mark.parametrize("exc,status", [
    (paper_eval.PaperConflict("the bucket must be empty"), 409),
    (paper_eval.PaperBadRequest("strategyVersion must be a non-empty string"), 400),
    (paper_eval.PaperDisabled(), 503),
])
def test_new_season_maps_errors_to_status_codes(monkeypatch, exc, status):
    async def raiser(**kw):
        raise exc
    monkeypatch.setattr(main.paper_exec, "new_season", raiser)
    r = run(main.paper_new_season(Req({"book": "practice"})))
    assert r.status_code == status
    if status == 409:
        assert "empty" in body(r)["error"]


# ----------------------------------------------------------- decisions routes

def test_decisions_are_newest_first_with_today_and_the_trading_budget(monkeypatch, paper):
    rows = [{"decisionId": f"d{n}", "date": "2026-10-1%d" % n} for n in range(1, 5)]
    monkeypatch.setattr(main.paper_pm, "read_decisions", lambda bucket="daily": list(rows))
    monkeypatch.setattr(main.paper_pm, "today_row", lambda date=None, bucket="daily": rows[-1])
    seen = []
    monkeypatch.setattr(main.lane_client, "budget_state",
                        lambda purpose=None: seen.append(purpose) or {"purpose": purpose, "left": 3})
    r = run(main.paper_decisions(book="practice", limit=3))
    assert r.status_code == 200
    out = body(r)
    assert [x["decisionId"] for x in out["items"]] == ["d4", "d3", "d2"]
    assert out["today"]["decisionId"] == "d4" and out["budget"] == {"purpose": "trading", "left": 3}
    assert seen == ["trading"] and paper == []


def test_decisions_today_is_null_without_a_row_and_limit_is_clamped(monkeypatch, paper):
    monkeypatch.setattr(main.paper_pm, "read_decisions",
                        lambda bucket="daily": [{"decisionId": "a"}, {"decisionId": "b"}])
    monkeypatch.setattr(main.paper_pm, "today_row", lambda date=None, bucket="daily": None)
    monkeypatch.setattr(main.lane_client, "budget_state", lambda purpose=None: {})
    assert body(run(main.paper_decisions(book="practice", limit=0)))["items"] == [{"decisionId": "b"}]
    out = body(run(main.paper_decisions(book="practice", limit=10 ** 6)))
    assert len(out["items"]) == 2 and out["today"] is None


def test_decisions_is_503_when_paper_trading_is_disabled(monkeypatch, paper):
    monkeypatch.setattr(main.paper_eval, "enabled", lambda: False)
    r = run(main.paper_decisions(book="practice", limit=20))
    assert r.status_code == 503 and body(r) == {"error": "paper_disabled"}


# ------------------------------------------------------------------ grading

def test_grading_passes_the_graded_decisions_through(monkeypatch, paper):
    out = {"horizons": {"5": {"n": 0}, "20": {"n": 0}}, "rows": [], "warnings": []}

    async def grading(bucket="daily"):
        return out
    monkeypatch.setattr(main.paper_eval, "grading", grading)
    r = run(main.paper_grading(book="practice"))
    assert r.status_code == 200 and body(r) == out


def test_grading_is_503_when_paper_trading_is_disabled(monkeypatch, paper):
    async def grading(bucket="daily"):
        raise paper_eval.PaperDisabled()
    monkeypatch.setattr(main.paper_eval, "grading", grading)
    r = run(main.paper_grading(book="practice"))
    assert r.status_code == 503 and body(r) == {"error": "paper_disabled"}


def test_practice_performance_carries_metrics_and_versions_while_real_does_not(monkeypatch, paper):
    async def perf(rng, bucket="daily"):
        return {"range": rng, "book": "practice", "metrics": {"verdict": "inconclusive"},
                "versions": [{"strategyVersion": "s1", "from": "2026-10-01", "to": None,
                              "metrics": {"verdict": "inconclusive"}}]}
    monkeypatch.setattr(main.paper_eval, "performance_summary", perf)
    p = body(run(main.get_portfolio_performance(range="1M", book="practice")))
    assert p["metrics"] == {"verdict": "inconclusive"} and p["versions"][0]["strategyVersion"] == "s1"
    real = body(run(main.get_portfolio_performance(range="1M", book="real")))
    assert real == {"range": "1M", "twrPct": None}


def test_decide_returns_the_stored_row_and_runs_a_manual_decision(paper):
    r = run(main.paper_decide(Req({"book": "practice"})))
    assert r.status_code == 200
    assert body(r) == {"decisionId": "2026-10-19-s1-1", "valid": True, "noChange": True}
    assert paper == [("decide", "manual", "daily")]


@pytest.mark.parametrize("reason", ["mode off", "halted", "no season", "budget", "not a session day"])
def test_decide_is_409_when_a_gate_is_closed(reason, monkeypatch, paper):
    async def gated(*, trigger, now=None, bucket="daily"):
        return {"deferred": reason}
    monkeypatch.setattr(main.paper_pm, "decide", gated)
    r = run(main.paper_decide(Req({"book": "practice"})))
    assert r.status_code == 409 and body(r) == {"error": f"decision not started: {reason}"}


def test_decide_returns_a_deferred_row_from_the_lane_as_200(monkeypatch, paper):
    async def lane_down(*, trigger, now=None, bucket="daily"):
        return {"decisionId": "2026-10-19-s1-1", "deferred": "lane: lane_down", "valid": False}
    monkeypatch.setattr(main.paper_pm, "decide", lane_down)
    r = run(main.paper_decide(Req({"book": "practice"})))
    assert r.status_code == 200 and body(r)["deferred"] == "lane: lane_down"


def test_decide_maps_paper_disabled_to_503(monkeypatch, paper):
    async def disabled(*, trigger, now=None, bucket="daily"):
        raise paper_eval.PaperDisabled()
    monkeypatch.setattr(main.paper_pm, "decide", disabled)
    r = run(main.paper_decide(Req({"book": "practice"})))
    assert r.status_code == 503 and body(r) == {"error": "paper_disabled"}


def test_the_paper_status_payload_gains_the_decision_block(monkeypatch, tmp_path):
    monkeypatch.setattr(paper_ledger, "SEASON_FILE", tmp_path / "season.json")
    monkeypatch.setattr(main.paper_pm, "decision_status", lambda now=None, bucket="daily": {
        "lastDate": "2026-10-16", "lastValid": True, "nextEligible": False, "reason": "mode off"})
    monkeypatch.setattr(paper_eval, "_paper_cfg", lambda bucket="daily": {
        "mode": "off", "strategyVersion": "s1", "budgetEur": 1000.0, "universe": {}, "limits": {}})
    monkeypatch.setattr(paper_eval, "_broker", lambda: None)
    out = paper_eval.status()
    assert out["decision"] == {"lastDate": "2026-10-16", "lastValid": True,
                               "nextEligible": False, "reason": "mode off"}


@pytest.mark.parametrize("method,path,ctype,rejected", [
    ("POST", "/api/paper/mode", "application/json", False),
    ("POST", "/api/paper/mode", "application/json; charset=utf-8", False),
    ("POST", "/api/paper/seed", "text/plain", True),                      # cross-site form (CSRF)
    ("POST", "/api/paper/halt", "application/x-www-form-urlencoded", True),
    ("POST", "/api/paper/decide", None, True),
    ("GET", "/api/paper/status", None, False),
    ("POST", "/api/alerts/ack", None, False),                              # other routes unchanged
])
def test_paper_mutations_require_a_json_content_type(method, path, ctype, rejected):
    assert main._paper_post_rejected(method, path, ctype) is rejected


# ======================================================= Phase 5: buckets

BUCKET_ROUTES = {
    "orders": lambda b: main.paper_orders(book="practice", limit=50, bucket=b),
    "decisions": lambda b: main.paper_decisions(book="practice", limit=20, bucket=b),
    "briefs": lambda b: main.paper_briefs(book="practice", limit=20, bucket=b),
    "grading": lambda b: main.paper_grading(book="practice", bucket=b),
    "compare": lambda b: main.paper_compare(book="practice", bucket=b),
    "status": lambda b: main.paper_status(book="practice", bucket=b),
    "portfolio": lambda b: main.get_portfolio(book="practice", bucket=b),
    "history": lambda b: main.get_portfolio_history(range="1M", book="practice", bucket=b),
    "performance": lambda b: main.get_portfolio_performance(range="1M", book="practice", bucket=b),
    "risk": lambda b: main.get_portfolio_risk(book="real", bucket=b),
    "attribution": lambda b: main.get_portfolio_attribution(range="1M", book="real", bucket=b),
    "rebalance": lambda b: main.paper_rebalance(Req({"book": "practice", "bucket": b,
                                                     "targets": {"ASML": 10}, "dryRun": False})),
    "seed": lambda b: main.paper_seed(Req({"book": "practice", "bucket": b, "dryRun": False})),
    "mode": lambda b: main.paper_mode(Req({"book": "practice", "bucket": b, "mode": "shadow"})),
    "halt": lambda b: main.paper_halt(Req({"book": "practice", "bucket": b})),
    "resume": lambda b: main.paper_resume(Req({"book": "practice", "bucket": b})),
    "season": lambda b: main.paper_start_season(Req({"book": "practice", "bucket": b})),
    "decide": lambda b: main.paper_decide(Req({"book": "practice", "bucket": b})),
}


@pytest.mark.parametrize("bucket", ["weekly", 5, ["daily"]])
@pytest.mark.parametrize("name", sorted(BUCKET_ROUTES))
def test_every_route_rejects_an_unknown_bucket_with_400_and_acts_on_nothing(name, bucket, paper):
    r = run(BUCKET_ROUTES[name](bucket))
    assert r.status_code == 400 and "bucket" in body(r)["error"]
    assert paper == []


def test_the_real_portfolio_ignores_a_valid_bucket_and_stays_byte_identical():
    assert body(run(main.get_portfolio(book="real", bucket="intraday"))) == REAL_PAYLOAD
    assert body(run(main.get_portfolio(book="real"))) == REAL_PAYLOAD


@pytest.mark.parametrize("bucket,expected", [(None, "daily"), ("", "daily"), ("daily", "daily"),
                                             ("intraday", "intraday")])
def test_post_routes_forward_the_validated_bucket_and_default_to_daily(bucket, expected, paper):
    base = {"book": "practice"} if bucket is None else {"book": "practice", "bucket": bucket}
    run(main.paper_rebalance(Req({**base, "targets": {"ASML": 30}, "dryRun": False})))
    run(main.paper_seed(Req({**base, "dryRun": False})))
    run(main.paper_mode(Req({**base, "mode": "shadow"})))
    run(main.paper_halt(Req({**base, "reason": "x"})))
    run(main.paper_resume(Req(base)))
    run(main.paper_decide(Req(base)))
    kinds = [(c[0], c[-1] if not isinstance(c[-1], dict) else c[-1]["bucket"]) for c in paper]
    assert kinds == [(k, expected) for k in ("rebalance", "seed", "mode", "halt", "resume", "decide")]


def test_get_routes_forward_the_bucket(monkeypatch, paper):
    seen = []

    async def snap(bucket="daily"):
        seen.append(("portfolio", bucket))
        return {"book": "practice"}

    async def hist(rng, bucket="daily"):
        seen.append(("history", bucket))
        return {"range": rng}

    async def perf(rng, bucket="daily"):
        seen.append(("performance", bucket))
        return {"range": rng}

    async def grading(bucket="daily"):
        seen.append(("grading", bucket))
        return {"horizons": {}}

    monkeypatch.setattr(main.paper_eval, "portfolio_snapshot", snap)
    monkeypatch.setattr(main.paper_eval, "history", hist)
    monkeypatch.setattr(main.paper_eval, "performance_summary", perf)
    monkeypatch.setattr(main.paper_eval, "grading", grading)
    for fn, kw in ((main.get_portfolio, {}), (main.get_portfolio_history, {"range": "1M"}),
                   (main.get_portfolio_performance, {"range": "1M"})):
        run(fn(book="practice", bucket="intraday", **kw))
        run(fn(book="practice", **kw))
    run(main.paper_grading(book="practice", bucket="intraday"))
    assert seen == [("portfolio", "intraday"), ("portfolio", "daily"),
                    ("history", "intraday"), ("history", "daily"),
                    ("performance", "intraday"), ("performance", "daily"),
                    ("grading", "intraday")]


def test_orders_and_decisions_are_the_buckets_own(monkeypatch, paper):
    paper_ledger.save_orders([
        {"intentId": "d1", "state": "filled"},                                  # no bucket key: daily
        {"intentId": "i1", "bucket": "intraday", "state": "filled"},
        {"intentId": "d2", "bucket": "daily", "state": "filled"}])
    paper_ledger.write_halt({"halted": True, "reason": "dd", "at": 1.0, "by": "owner"}, "intraday")
    daily = body(run(main.paper_orders(book="practice", limit=50)))
    intraday = body(run(main.paper_orders(book="practice", limit=50, bucket="intraday")))
    assert [x["intentId"] for x in daily["items"]] == ["d2", "d1"] and daily["halt"]["halted"] is False
    assert [x["intentId"] for x in intraday["items"]] == ["i1"] and intraday["halt"]["halted"] is True
    seen = []
    monkeypatch.setattr(main.paper_pm, "read_decisions",
                        lambda bucket="daily": [{"decisionId": f"{bucket}-1"}])
    monkeypatch.setattr(main.paper_pm, "today_row", lambda date=None, bucket="daily": None)
    monkeypatch.setattr(main.lane_client, "budget_state",
                        lambda purpose=None: seen.append(purpose) or {})
    out = body(run(main.paper_decisions(book="practice", limit=5, bucket="intraday")))
    assert out["items"] == [{"decisionId": "intraday-1"}] and seen == ["intraday"]


def test_briefs_are_newest_first_per_bucket(monkeypatch, tmp_path, paper):
    monkeypatch.setattr(paper_ledger, "BRIEFS_FILE", tmp_path / "briefs.json")
    for d in ("2026-10-16", "2026-10-19", "2026-10-20"):
        paper_ledger.save_brief({"date": d, "text": f"daily {d}"})
    paper_ledger.save_brief({"date": "2026-10-19", "text": "intraday"}, "intraday")
    out = body(run(main.paper_briefs(book="practice", limit=2)))
    assert [b["date"] for b in out["items"]] == ["2026-10-20", "2026-10-19"] and out["bucket"] == "daily"
    out = body(run(main.paper_briefs(book="practice", limit=20, bucket="intraday")))
    assert [b["text"] for b in out["items"]] == ["intraday"]
    assert run(main.paper_briefs(book="live", limit=5)).status_code == 400


def test_briefs_are_503_when_paper_trading_is_disabled(monkeypatch, paper):
    monkeypatch.setattr(main.paper_eval, "enabled", lambda: False)
    assert run(main.paper_briefs(book="practice", limit=5)).status_code == 503


def test_compare_passes_the_payload_through_and_maps_errors(monkeypatch, paper):
    out = {"asOf": 1, "buckets": {"daily": None, "intraday": None}, "buyHold": None,
           "decisions": {}, "turnoverPct": {}, "costDragPct": {}}

    async def compare():
        return out
    monkeypatch.setattr(main.paper_eval, "compare", compare)
    r = run(main.paper_compare(book="practice"))
    assert r.status_code == 200 and body(r) == out
    assert run(main.paper_compare(book="real")).status_code == 400

    async def disabled():
        raise paper_eval.PaperDisabled()
    monkeypatch.setattr(main.paper_eval, "compare", disabled)
    assert run(main.paper_compare(book="practice")).status_code == 503


def test_the_status_payload_has_the_requested_bucket_block_and_every_bucket(monkeypatch, paper):
    seen = []
    monkeypatch.setattr(main.paper_eval, "status", lambda bucket="daily": seen.append(bucket) or {
        "enabled": True, "bucketId": bucket,
        "buckets": {"daily": {"enabled": True}, "intraday": {"enabled": False}}})
    out = body(run(main.paper_status(bucket="intraday")))
    assert out["bucketId"] == "intraday" and set(out["buckets"]) == {"daily", "intraday"}
    assert body(run(main.paper_status()))["bucketId"] == "daily" and seen == ["intraday", "daily"]
