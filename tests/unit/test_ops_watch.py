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


# ------------------------------------------------------------ paper findings

@pytest.fixture
def paper(ow, monkeypatch):
    from app.api import paper_eval, paper_ledger, paper_pm, paper_trader
    ow["findings"] = []
    ow.update(enabled=True, halt={"halted": False, "at": None, "by": None},
              snaps=[], decisions=[], intraday=False,
              i_halt={"halted": False, "at": None, "by": None}, i_snaps=[], i_decisions=[])
    monkeypatch.setattr(paper_trader, "enabled", lambda: ow["enabled"])
    monkeypatch.setattr(paper_eval, "_paper_cfg",
                        lambda bucket="daily": {"enabled": bucket == "daily" or ow["intraday"]})
    monkeypatch.setattr(paper_ledger, "read_halt",
                        lambda bucket="daily": dict(ow["halt"] if bucket == "daily" else ow["i_halt"]))
    monkeypatch.setattr(paper_ledger, "read_snapshots",
                        lambda bucket="daily": list(ow["snaps"] if bucket == "daily" else ow["i_snaps"]))
    monkeypatch.setattr(paper_pm, "read_decisions",
                        lambda bucket="daily": list(ow["decisions"] if bucket == "daily"
                                                    else ow["i_decisions"]))
    return ow


def keys(now=T0):
    return [f["key"] for f in ops_watch.paper_findings(now)]


def test_halted_fires_only_after_four_hours(paper):
    paper["halt"] = {"halted": True, "at": T0 - 3 * HOUR, "by": "kill_switch"}
    assert keys() == []
    paper["halt"]["at"] = T0 - 4 * HOUR - 1
    assert keys() == ["paper-halted"]
    paper["halt"]["halted"] = False                         # resumed
    assert keys() == []


@pytest.mark.parametrize("a,b,fires", [
    (["ledger_mismatch"], ["ledger_mismatch"], True),
    (["untracked_position"], ["ledger_mismatch"], True),
    ([], ["ledger_mismatch"], False),                       # a single snapshot is not "consecutive"
    (["ledger_mismatch"], [], False),                       # it cleared
    (["something_else"], ["something_else"], False),
])
def test_ledger_mismatch_needs_two_consecutive_snapshots(paper, a, b, fires):
    paper["snaps"] = [{"date": "2026-10-15", "warnings": ["ledger_mismatch"]},   # older: ignored
                      {"date": "2026-10-16", "warnings": a},
                      {"date": "2026-10-19", "warnings": b}]
    assert (keys() == ["paper-ledger-mismatch"]) is fires


def test_ledger_mismatch_with_one_snapshot_only_does_not_fire(paper):
    paper["snaps"] = [{"date": "2026-10-19", "warnings": ["ledger_mismatch"]}]
    assert keys() == []


def test_invalid_decisions_need_three_consecutive_scheduled_ones(paper):
    bad = lambda d: {"date": d, "trigger": "schedule", "valid": False}
    paper["decisions"] = [bad("2026-10-15"), bad("2026-10-16")]
    assert keys() == []
    paper["decisions"].append({"date": "2026-10-17", "trigger": "manual", "valid": True})   # ignored
    paper["decisions"].append({"date": "2026-10-17", "trigger": "schedule", "deferred": "budget"})
    assert keys() == []
    paper["decisions"].append(bad("2026-10-19"))
    assert keys() == ["paper-decisions-invalid"]
    paper["decisions"].append({"date": "2026-10-20", "trigger": "schedule", "valid": True})
    assert keys() == []                                     # the newest three are no longer all invalid


def test_no_paper_findings_when_the_worker_is_off(paper):
    paper["enabled"] = False
    paper["halt"] = {"halted": True, "at": T0 - 9 * HOUR, "by": "owner"}
    assert keys() == []


def test_paper_worker_is_watched_for_staleness_only_when_enabled(paper):
    from app.api import paper_trader
    assert ("paper_trader", paper_trader.IDLE_PASS_S) in ops_watch._watched_workers()
    paper["enabled"] = False
    assert "paper_trader" not in [n for n, _ in ops_watch._watched_workers()]


def test_a_paper_finding_alerts_dedupes_realerts_and_resolves_like_any_other(paper):
    paper["halt"] = {"halted": True, "at": T0 - 5 * HOUR, "by": "owner"}
    assert run_pass(paper, 0)["fired"] == ["paper-halted"]
    assert run_pass(paper, 6 * HOUR)["fired"] == []              # deduped
    assert len(paper["alerts"]) == 1
    assert (paper["alerts"][0]["source"], paper["alerts"][0]["priority"]) == ("ops_watch", 3)
    assert run_pass(paper, 24 * HOUR + 1)["fired"] == ["paper-halted"]    # 24 h re-alert
    paper["halt"]["halted"] = False
    res = run_pass(paper, 25 * HOUR)
    assert res["resolved"] == ["paper-halted"] and res["findings"] == []
    assert [s["title"] for s in paper["stored"]] == ["Resolved: Paper trading halted"]
    paper["halt"]["halted"] = True
    assert run_pass(paper, 26 * HOUR)["fired"] == ["paper-halted"]        # returns -> alerts at once


def test_the_intraday_bucket_gets_its_own_findings_only_while_enabled(paper):
    paper["i_halt"] = {"halted": True, "at": T0 - 5 * HOUR, "by": "kill_switch"}
    paper["i_snaps"] = [{"date": "2026-10-16", "warnings": ["ledger_mismatch"]},
                        {"date": "2026-10-19", "warnings": ["untracked_position"]}]
    bad = lambda d: {"date": d, "trigger": "schedule", "valid": False}
    paper["i_decisions"] = [bad("2026-10-15"), bad("2026-10-16"), bad("2026-10-19")]
    assert keys() == []                                         # the bucket is not enabled
    paper["intraday"] = True
    assert keys() == ["paper-halted:intraday", "paper-ledger-mismatch:intraday",
                      "paper-decisions-invalid:intraday"]
    titles = [f["title"] for f in ops_watch.paper_findings(T0)]
    assert titles == ["Paper trading halted (intraday)", "Paper ledger mismatch (intraday)",
                      "Paper decisions invalid (intraday)"]


def test_a_halted_daily_bucket_does_not_raise_the_intraday_finding_and_vice_versa(paper):
    paper["intraday"] = True
    paper["halt"] = {"halted": True, "at": T0 - 5 * HOUR, "by": "owner"}
    assert keys() == ["paper-halted"]
    paper["halt"] = {"halted": False, "at": None, "by": None}
    paper["i_halt"] = {"halted": True, "at": T0 - 5 * HOUR, "by": "owner"}
    assert keys() == ["paper-halted:intraday"]


def test_a_failing_paper_read_does_not_break_the_lane_watch(paper, monkeypatch):
    from app.api import paper_pm

    def boom(bucket="daily"):
        raise RuntimeError("bad file")
    monkeypatch.setattr(paper_pm, "read_decisions", boom)
    paper["findings"] = [dict(FINDING)]
    assert run_pass(paper, 0)["fired"] == ["degraded:ninfer"]


# --- Claude adapter findings -------------------------------------------------------------------

SECRET = "sk-ant-oat01-SECRETBYTES"


@pytest.fixture
def claude(ow, monkeypatch):
    import datetime
    from app.api import claude_client
    ow["findings"] = []
    today = datetime.datetime.fromtimestamp(T0).date()
    c = {"enabled": True, "status": {"tokenPresent": True, "tokenFormatOk": True,
                                     "tokenFirstSeen": (today - datetime.timedelta(days=10)).isoformat(),
                                     "lastError": None},
         "today": today}
    monkeypatch.setattr(claude_client, "enabled", lambda: c["enabled"])
    monkeypatch.setattr(claude_client, "status", lambda: dict(c["status"]))
    return {**ow, "c": c}


def ckeys():
    return [f["key"] for f in ops_watch.claude_findings(T0)]


def test_claude_healthy_or_disabled_has_no_findings(claude):
    assert ckeys() == []
    claude["c"]["enabled"] = False
    claude["c"]["status"]["tokenFormatOk"] = False
    assert ckeys() == []


def test_claude_auth_fires_and_clears_on_next_success(claude):
    claude["c"]["status"]["lastError"] = {"error": "auth", "detail": "x", "at": T0}
    assert ckeys() == ["claude-auth"]
    claude["c"]["status"]["lastError"] = {"error": "timeout", "detail": "x", "at": T0}
    assert ckeys() == []
    claude["c"]["status"]["lastError"] = None
    assert ckeys() == []


def test_claude_token_format_fires_and_clears(claude):
    claude["c"]["status"]["tokenFormatOk"] = False
    assert ckeys() == ["claude-token-format"]
    claude["c"]["status"]["tokenFormatOk"] = True
    assert ckeys() == []


def test_claude_token_expiring_at_350_days(claude):
    import datetime
    today = claude["c"]["today"]
    st = claude["c"]["status"]
    st["tokenFirstSeen"] = (today - datetime.timedelta(days=349)).isoformat()
    assert ckeys() == []
    st["tokenFirstSeen"] = (today - datetime.timedelta(days=350)).isoformat()
    assert ckeys() == ["claude-token-expiring"]
    st["tokenFirstSeen"] = today.isoformat()                 # rotated token: cleared
    assert ckeys() == []


def test_claude_findings_tell_the_owner_the_fix_and_carry_no_token(claude):
    st = claude["c"]["status"]
    st.update(tokenFormatOk=False, tokenFirstSeen="2020-01-01",
              lastError={"error": "auth", "detail": "bad token ***", "at": T0})
    fs = ops_watch.claude_findings(T0)
    assert [f["key"] for f in fs] == ["claude-auth", "claude-token-format", "claude-token-expiring"]
    for f in fs:
        assert "claude setup-token" in f["body"] and "CLAUDE_CODE_OAUTH_TOKEN" in f["body"]
        assert "restart the pod" in f["body"]
        assert SECRET not in str(f) and "sk-ant-oat0" not in str(f)


def test_claude_finding_alerts_dedupes_and_resolves_in_run_pass(claude):
    claude["c"]["status"]["lastError"] = {"error": "auth", "detail": "x", "at": T0}
    assert run_pass(claude, 0)["fired"] == ["claude-auth"]
    assert run_pass(claude, HOUR)["fired"] == []
    claude["c"]["status"]["lastError"] = None
    assert run_pass(claude, 2 * HOUR)["resolved"] == ["claude-auth"]


# ------------------------------------------------------------ data-quality findings

@pytest.fixture
def earn(ow, monkeypatch):
    from app.api import paper_universe, universe_news
    ids = [f"E{i:02d}" for i in range(20)]
    rows = [{"id": i, "role": "candidate", "kind": "equity"} for i in ids]
    rows += [{"id": "ETF1", "role": "candidate", "kind": "etf"}, {"id": "EUNL", "role": "benchmark", "kind": "etf"},
             {"id": "BENCHEQ", "role": "benchmark", "kind": "equity"}]
    ow["findings"] = []
    ow["ids"] = ids
    ow["obs"] = {i: {"date": "2099-01-01", "fetchedAt": T0 - 100} for i in ids}
    monkeypatch.setattr(universe_news, "enabled", lambda: True)
    monkeypatch.setattr(paper_universe, "entries", lambda: rows)
    monkeypatch.setattr(universe_news, "next_earnings", lambda pid, cutoff: ow["obs"].get(pid))
    return ow


def ekeys():
    return [f["key"] for f in ops_watch.earnings_findings(T0)]


def test_earnings_null_rate_fires_above_25_percent_of_equity_candidates_only(earn):
    for pid in earn["ids"][:5]:                      # 5 of 20 = exactly 25 %: not above
        earn["obs"][pid] = {"date": None, "fetchedAt": T0 - 100}
    assert ekeys() == []
    earn["obs"][earn["ids"][5]] = {"date": None, "fetchedAt": T0 - 100}    # 6 of 20 = 30 %
    (f,) = ops_watch.earnings_findings(T0)
    assert f["key"] == "earnings-null-rate" and "6 of 20" in f["body"] and "30 %" in f["body"]


def test_etfs_and_benchmarks_are_not_in_the_earnings_denominator(earn):
    earn["obs"]["ETF1"] = {"date": None, "fetchedAt": T0 - 100}
    earn["obs"]["BENCHEQ"] = None
    assert ekeys() == []


def test_a_passed_date_and_a_never_observed_name_count_as_lacking_but_today_does_not(earn):
    import datetime
    from app.api import universe_news
    today = datetime.datetime.fromtimestamp(T0, universe_news._BERLIN).date().isoformat()
    earn["obs"][earn["ids"][0]] = {"date": today, "fetchedAt": T0 - 100}                  # reports today: present
    for pid in earn["ids"][1:4]:
        earn["obs"][pid] = {"date": "2000-01-01", "fetchedAt": T0 - 100}                  # passed: lacking
    for pid in earn["ids"][4:7]:
        del earn["obs"][pid]                                                              # never observed: lacking
    assert ekeys() == ["earnings-null-rate"]                                              # 6 of 20
    earn["obs"][earn["ids"][4]] = {"date": "2099-01-01", "fetchedAt": T0 - 100}           # back to 5 of 20
    assert ekeys() == []


def test_nothing_is_reported_before_any_name_was_ever_observed_or_with_the_worker_off(earn, monkeypatch):
    from app.api import universe_news
    earn["obs"].clear()
    assert ekeys() == []
    earn["obs"].update({i: {"date": None, "fetchedAt": T0 - 100} for i in earn["ids"]})
    assert ekeys() == ["earnings-null-rate"]
    monkeypatch.setattr(universe_news, "enabled", lambda: False)
    assert ekeys() == []


def test_the_earnings_finding_alerts_dedupes_and_resolves_in_run_pass(earn):
    for pid in earn["ids"][:8]:
        earn["obs"][pid] = {"date": None, "fetchedAt": T0 - 100}
    assert run_pass(earn, 0)["fired"] == ["earnings-null-rate"]
    assert run_pass(earn, HOUR)["fired"] == []
    for pid in earn["ids"][:8]:                                    # the carry-forward / calendar recovered
        earn["obs"][pid] = {"date": "2099-01-01", "fetchedAt": T0 + HOUR}
    assert run_pass(earn, 2 * HOUR)["resolved"] == ["earnings-null-rate"]


@pytest.fixture
def stale(ow, monkeypatch):
    from app.api import forecast_worker, forecasts
    ow["findings"] = []
    ow["cohorts"] = {}
    monkeypatch.setattr(forecast_worker, "enabled", lambda: True)
    monkeypatch.setattr(forecasts, "read_cohorts", lambda: dict(ow["cohorts"]))
    return ow


def cohort(cid, as_of, n, stale_n):
    return {"id": cid, "asOf": as_of, "ids": [f"S{i:02d}" for i in range(n)],
            "result": {"stale": {f"S{i:02d}": "x" for i in range(stale_n)}, "complete": True}}


def test_stale_packet_finding_fires_above_10_percent_of_the_newest_cohort_and_clears_with_the_next(stale):
    stale["cohorts"] = {"w1": cohort("w1", "2026-10-16", 50, 40),     # an old bad cohort is history
                        "w2": cohort("w2", "2026-10-23", 50, 5)}      # exactly 10 %: not above
    assert ops_watch.stale_packet_findings(T0) == []
    stale["cohorts"]["w2"] = cohort("w2", "2026-10-23", 50, 6)
    (f,) = ops_watch.stale_packet_findings(T0)
    assert f["key"] == "forecast-stale-packets" and "w2" in f["body"] and "6 of 50" in f["body"]
    assert run_pass(stale, 0)["fired"] == ["forecast-stale-packets"]
    stale["cohorts"]["d3"] = cohort("d3", "2026-10-26", 50, 0)        # the next cohort is clean
    assert run_pass(stale, HOUR)["resolved"] == ["forecast-stale-packets"]


def test_stale_packet_finding_ignores_cohorts_without_a_result_and_a_disabled_forecaster(stale, monkeypatch):
    from app.api import forecast_worker
    stale["cohorts"] = {"w1": {"id": "w1", "asOf": "2026-10-23", "ids": ["S00"], "status": "running"}}
    assert ops_watch.stale_packet_findings(T0) == []
    stale["cohorts"] = {"w1": cohort("w1", "2026-10-23", 10, 10)}
    monkeypatch.setattr(forecast_worker, "enabled", lambda: False)
    assert ops_watch.stale_packet_findings(T0) == []
