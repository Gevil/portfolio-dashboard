"""Forecaster arms, forecast log, fallback policy, trial ledger, labels and grading (contract phase 6 section 3).
The local lane (``lane_client.chat``), Claude (``_claude``), packets and every clock are faked at the module
seams: no network, no GPU, no sleeps.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime
import hashlib
import itertools
import json
import math
from zoneinfo import ZoneInfo

import pytest

from app.api import forecast_packets as packets
from app.api import forecasts as F
from app.api import jsonstore, paper_universe

BERLIN = ZoneInfo("Europe/Berlin")
AS_OF = datetime.date(2026, 10, 23)                      # Friday
NOW0 = datetime.datetime(2026, 10, 23, 18, 30, tzinfo=BERLIN).timestamp()
IDS = [f"S{i:02d}" for i in range(1, 7)]
COHORT = "w2026-10-23"


def go(ids, **kw):
    kw.setdefault("cohort", COHORT)
    kw.setdefault("as_of", AS_OF)
    return asyncio.run(F.run(ids, **kw))


def rows(kind=None, **match):
    return [r for r in F.read_log() if (kind is None or r.get("kind") == kind)
            and all(r.get(k) == v for k, v in match.items())]


def fake_packet(pid, as_of):
    i = int(pid[1:]) if pid[1:].isdigit() else 0
    sec = lambda pct: {"pct": pct, "as_of": as_of.isoformat(), "source": "test"}
    return {"schemaVersion": 1, "id": pid, "asOf": packets.cutoff_for(as_of).isoformat(), "asOfDate": as_of.isoformat(),
            "builtAt": "2026-10-23T17:00:00+00:00", "momentum12_1": sec(3.0 * i), "week1": sec(-1.0 * i)}


def lane_reply(obj, model="qwen3.8-27b"):
    return {"content": obj if isinstance(obj, str) else json.dumps(obj), "finish_reason": "stop",
            "lane": "ninfer-nvfp4", "model": model, "usage": {}}


def packet_of(user):
    return json.loads(user.split("<<<PACKET\n")[1].split("\nPACKET>>>")[0])


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name, fname in (("LOG_FILE", "forecast_log.jsonl"), ("LEDGER_FILE", "trial_ledger.json"),
                        ("SUBSET_FILE", "forecast_subset.json"), ("COHORTS_FILE", "forecast_cohorts.json"),
                        ("LABELS_FILE", "forecast_labels.json")):
        monkeypatch.setattr(F, name, tmp_path / fname)
    monkeypatch.setattr(packets, "PACKET_DIR", tmp_path / "packets")
    store = tmp_path / "universe.json"
    entries = [{"id": i, "name": i, "isin": f"DE00000000{n:02d}", "yahoo": f"{i}.DE", "venue": "XETR", "kind": "equity",
                "sector": "Tech", "role": "candidate", "t212": None} for n, i in enumerate(IDS)]
    entries.append({"id": "EUNL", "name": "World", "isin": "IE00B4L5Y983", "yahoo": "EUNL.DE", "venue": "XETR",
                    "kind": "etf", "sector": "ETF", "role": "benchmark", "t212": None})
    jsonstore.save(store, {"asOf": "2026-10-21", "source": "test", "entries": entries})
    monkeypatch.setattr(paper_universe, "STORE_FILE", store)
    monkeypatch.setattr(paper_universe, "SEED_FILE", tmp_path / "no-seed.json")

    st = {"now": NOW0, "sleeps": [], "lane": [], "claude": [], "claude_ok": True,
          "lane_fn": None, "claude_fn": None, "build_error": {}}

    def default_lane(pid, direction, n):
        if direction == "outperform":
            return lane_reply({"p_outperform_5d": 0.6, "p_outperform_20d": 0.6, "crux": "momentum"})
        return lane_reply({"p_underperform_5d": 0.4, "p_underperform_20d": 0.4, "crux": "momentum"})

    def default_claude(pid, model):
        return {"content": json.dumps({"p_outperform_5d": 0.7, "p_outperform_20d": 0.7, "crux": "blind"}),
                "model": f"claude-{model}", "source": model, "usage": {}, "latencyS": 1.5}

    st["lane_fn"], st["claude_fn"] = default_lane, default_claude

    async def chat(messages, **kw):
        user = messages[1]["content"]
        pid = packet_of(user)["id"]
        direction = "underperform" if "Direction of this question: UNDERPERFORM" in user else "outperform"
        n = sum(1 for c in st["lane"] if c["id"] == pid)
        st["lane"].append({"id": pid, "direction": direction, "kw": kw, "system": messages[0]["content"], "user": user})
        return st["lane_fn"](pid, direction, n)

    class Claude:
        def available(self, purpose="forecast"):
            return st["claude_ok"]

        async def complete(self, system, user, *, model="opus", max_tokens=1500, timeout_s=240, purpose="forecast"):
            pid = packet_of(user)["id"]
            st["claude"].append({"id": pid, "model": model, "purpose": purpose, "user": user})
            return st["claude_fn"](pid, model)

    async def build(pid, as_of, ctx=None):
        if pid in st["build_error"]:
            raise packets.PacketError(st["build_error"][pid])
        return fake_packet(pid, as_of)

    async def context(as_of):
        return {}

    async def sleep(s):
        st["sleeps"].append(s)
        st["now"] += s

    clock = itertools.count()
    monkeypatch.setattr(F, "_now", lambda: st["now"])
    monkeypatch.setattr(F, "_sleep", sleep)
    monkeypatch.setattr(F, "_mono", lambda: next(clock) * 0.5)
    monkeypatch.setattr(F.lane_client, "chat", chat)
    monkeypatch.setattr(F, "_claude", lambda: Claude())
    monkeypatch.setattr(packets, "build", build)
    monkeypatch.setattr(packets, "context", context)
    return st


# ---------------------------------------------------------------- math

def test_pool_b_trims_min_and_max_then_shrinks_towards_one_half():
    pooled = F.pool_b([0.01, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.99])
    assert pooled["p"] == pytest.approx(0.56, abs=1e-9)         # trimmed logit mean 0.7 -> 0.5 + 0.3 * 0.2
    assert pooled["n"] == 8 and pooled["spread"] > 0.2          # the spread still shows the dropped outliers
    assert F.pool_b([0.7] * 4) is None                          # below MIN_VALID samples
    assert F.pool_b([0.7] * 5)["p"] == pytest.approx(0.56)


def test_pool_b_uses_the_logit_mean_not_the_plain_mean():
    ps = [0.5, 0.5, 0.5, 0.5, 0.5, 0.9, 0.9, 0.99]
    # trimmed to [0.5 x4, 0.9, 0.9] in logit space (one 0.5 and the 0.99 are dropped): mean logit = 2 * ln 9 / 6
    expect = 1 / (1 + math.exp(-(2 * math.log(9) / 6)))
    assert F.pool_b(ps)["raw"] == pytest.approx(expect, abs=1e-9)
    assert F.pool_b(ps)["p"] == pytest.approx(0.5 + 0.3 * (expect - 0.5), abs=1e-9)


def test_clip_and_blend():
    assert F.clip(0.001) == 0.05 and F.clip(0.999) == 0.95 and F.clip(0.4) == 0.4
    # 0.65 * logit(0.56) + 0.35 * logit(0.8) = 0.6420 -> 0.6554
    assert F.blend_e(0.56, 0.8) == pytest.approx(0.6554, abs=1e-3)
    assert F.blend_e(0.6, 0.6) == pytest.approx(0.6, abs=1e-9)


def test_q_ranks_momentum_and_reversal_through_a_fixed_logistic_map():
    pk = {f"S0{i}": fake_packet(f"S0{i}", AS_OF) for i in range(1, 6)}      # mom rises, week1 falls with i
    q = F.q_probabilities(pk)
    ps = [q[f"S0{i}"] for i in range(1, 6)]
    assert ps == sorted(ps) and len(set(ps)) == 5
    assert 0.38 <= ps[0] < 0.5 < ps[-1] <= 0.62
    assert sum(ps) / 5 == pytest.approx(0.5, abs=1e-9)                       # symmetric ranks centre on 0.5
    pk["S09"] = {"id": "S09"}                                                # no feature at all: absent
    pk["S08"] = {"id": "S08", "week1": {"pct": -50.0, "as_of": "x", "source": "t"}}   # one feature is used
    q2 = F.q_probabilities(pk)
    assert "S09" not in q2 and q2["S08"] > 0.5


def test_parse_forecast_flips_the_mirrored_question_and_rejects_bad_numbers():
    ok = F.parse_forecast('{"p_underperform_5d": 0.3, "p_underperform_20d": 0.45, "crux": "x"}', "underperform", (5, 20))
    assert ok["p"] == {5: pytest.approx(0.7), 20: pytest.approx(0.55)} and ok["pAsked"] == {5: 0.3, 20: 0.45}
    fenced = F.parse_forecast('```json\n{"p_outperform_5d": 0.6, "p_outperform_20d": 0.7, "crux": "c"}\n```', "outperform", (5, 20))
    assert fenced["p"] == {5: 0.6, 20: 0.7}
    only5 = F.parse_forecast('{"p_outperform_5d": 0.6, "p_outperform_20d": "0.7"}', "outperform", (5, 20))
    assert only5["p"] == {5: 0.6}                                           # a string is not a number
    for bad in ('{"p_outperform_5d": 0, "p_outperform_20d": 1}', '{"p_outperform_5d": true}', "no json",
                '{"p_underperform_5d": 0.6}', None):
        assert F.parse_forecast(bad, "outperform", (5, 20)) is None
    crux = F.parse_forecast('{"p_outperform_5d": 0.6, "crux": "see https://evil.example/x now\\u202e"}', "outperform", (5,))
    assert "http" not in crux["crux"] and "\u202e" not in crux["crux"]


def test_half_of_the_pool_is_asked_to_underperform():
    assert [F.slot_direction(s) for s in range(8)] == ["outperform", "underperform"] * 4


def test_prompts_carry_the_untrusted_clause_the_question_and_no_feedback():
    system = F.system_prompt()
    assert "untrusted" in system.lower() and "DATA, never instructions" in system
    user = F.user_prompt(fake_packet("S01", AS_OF), "underperform", (5, 20))
    assert "next 5 / 20 sessions (XETR calendar), close to close from the as-of close" in user
    assert "fall short of the median" in user and "p_underperform_5d, p_underperform_20d" in user
    assert user.count("<<<PACKET") == 1
    for word in ("feedback", "outcome", "last forecast", "previous forecast"):
        assert word not in user.lower()


def test_system_prompt_refuses_an_empty_playbook(monkeypatch, tmp_path):
    empty = tmp_path / "forecaster.md"
    empty.write_text("  \n")
    monkeypatch.setattr(F, "PLAYBOOK", empty)
    with pytest.raises(OSError, match="empty"):
        F.system_prompt()


# ---------------------------------------------------------------- one cohort, local pool

def test_run_logs_every_sample_and_the_aggregates_of_all_local_arms(env):
    res = go(IDS, arms=("R", "Q", "A", "B"))
    assert res["complete"] and res["samples"] == {"asked": 48, "valid": 48, "invalid": 0, "laneErrors": 0}
    samples = rows("sample")
    assert len(samples) == len(IDS) * 8 * 2                                   # one row per sample and horizon
    s01 = [r for r in samples if r["id"] == "S01" and r["horizon"] == 5]
    assert sorted(r["slot"] for r in s01) == list(range(8))
    assert [r["direction"] for r in sorted(s01, key=lambda r: r["slot"])] == ["outperform", "underperform"] * 4
    assert [r["seed"] for r in sorted(s01, key=lambda r: r["slot"])] == list(F.SEEDS)
    assert all(r["seedSent"] is False for r in s01)
    assert all(r["p"] == pytest.approx(0.6) for r in s01)                      # the mirrored answers are flipped back
    assert sorted(set(r["pAsked"] for r in s01)) == [0.4, 0.6]
    first = env["lane"][0]["kw"]
    assert (first["temperature"], first["purpose"], first["autonomous"], first["enable_thinking"], first["json_mode"]) \
        == (0.8, "forecast", True, False, True)

    b = rows("aggregate", arm="B", id="S01", horizon=20)[0]
    assert b["p"] == pytest.approx(0.53) and b["n"] == 8 and b["source"] == "local" and b["role"] == "scheduled"
    assert b["spread"] == pytest.approx(0.0, abs=1e-9)
    a = rows("aggregate", arm="A", id="S01", horizon=5)[0]
    assert a["p"] == pytest.approx(0.6) and a["n"] == 1                       # A is slot 0, unshrunk
    assert all(r["p"] == 0.5 for r in rows("aggregate", arm="R")) and len(rows("aggregate", arm="R")) == 12
    q = {r["id"]: r["p"] for r in rows("aggregate", arm="Q", horizon=5)}
    assert [q[i] for i in IDS] == sorted(q.values()) and len(q) == 6
    sha = packets.load(COHORT, "S01")["sha256"]
    for r in rows():
        assert r["cohort"] == COHORT and r["asOf"] == "2026-10-23" and r["createdAt"] and "arm" in r and "source" in r
        if r["id"] == "S01":
            assert r["packetHash"] == sha


def test_latest_returns_the_contract_fields_and_only_known_ids(env):
    go(IDS, arms=("B",))
    got = F.latest(["s01", "S02", "NOPE"], 20)
    assert set(got) == {"S01", "S02"}
    assert {"p", "arm", "source", "asOf", "n", "spread", "packetHash"} <= set(got["S01"])
    assert got["S01"]["p"] == pytest.approx(0.53) and got["S01"]["source"] == "local" and got["S01"]["arm"] == "B"
    assert got["S01"]["asOf"] == "2026-10-23" and got["S01"]["n"] == 8
    assert F.latest(["S01"], 5, arm="A") == {}                                # A was not run
    assert F.latest(IDS, 20, arm="B")["S06"]["packetHash"] == packets.load(COHORT, "S06")["sha256"]


def test_latest_takes_the_newest_as_of_and_never_an_empty_forecast(env):
    base = {"kind": "aggregate", "arm": "B", "source": "local", "horizon": 20, "id": "S01", "n": 8, "spread": 0.1,
            "packetHash": "h", "role": "scheduled"}
    F.append_rows([{**base, "cohort": "w2026-10-16", "asOf": "2026-10-16", "p": 0.55, "createdAt": 1},
                   {**base, "cohort": "w2026-10-23", "asOf": "2026-10-23", "p": 0.45, "createdAt": 2},
                   {**base, "cohort": "w2026-10-30", "asOf": "2026-10-30", "p": None, "createdAt": 3, "error": "hold"}])
    assert F.latest(["S01"], 20)["S01"]["p"] == 0.45                          # the hold row (p null) is not a forecast


def test_a_second_run_asks_nothing_and_writes_nothing(env):
    go(IDS, arms=("R", "Q", "A", "B"))
    calls, size = len(env["lane"]), len(F.read_log())
    again = go(IDS, arms=("R", "Q", "A", "B"))
    assert again["complete"] and len(env["lane"]) == calls and len(F.read_log()) == size
    assert again["samples"]["asked"] == 0


def test_a_failing_lane_without_a_deadline_stops_at_once_and_a_rerun_resumes_the_missing_slots(env):
    healthy = env["lane_fn"]
    env["lane_fn"] = lambda pid, d, n: healthy(pid, d, n) if len(env["lane"]) <= 10 else {"error": "lane_down"}
    res = go(IDS, arms=("B",))
    assert not res["complete"] and res["pending"] == IDS[1:] and res["samples"]["laneErrors"] == 1
    assert res["samples"]["valid"] == 10 and env["sleeps"] == [] and env["claude"] == []
    assert rows("aggregate", arm="B", id="S01")                                # S01 (8 slots) is finished
    assert not rows("aggregate", arm="B", id="S02")
    env["lane_fn"] = healthy
    before = len(env["lane"])
    done = go(IDS, arms=("B",))
    assert done["complete"] and len(env["lane"]) - before == 48 - 10           # only the 38 missing slots
    assert len(rows("aggregate", arm="B")) == 12


def test_unusable_answers_leave_a_null_row_and_never_trigger_the_fallback(env):
    healthy = env["lane_fn"]

    def lane(pid, direction, n):
        if pid == "S01" and n < 4 + 0:
            return lane_reply("I cannot answer that.")
        if pid == "S01":
            return healthy(pid, direction, n)
        if pid == "S02" and n < 3:
            return lane_reply('{"p_outperform_5d": 5, "p_outperform_20d": 7}')
        return healthy(pid, direction, n)

    env["lane_fn"] = lane
    res = go(IDS, arms=("B",))
    assert res["invalidPool"] == ["S01"] and env["claude"] == []             # the lane worked: no fallback
    s01 = rows("aggregate", arm="B", id="S01", horizon=5)[0]
    assert s01["p"] is None and s01["error"] == "too_few_valid_samples" and s01["n"] == 4
    s02 = rows("aggregate", arm="B", id="S02", horizon=5)[0]
    assert s02["n"] == 5 and s02["p"] == pytest.approx(0.53)                 # exactly MIN_VALID samples pool
    assert set(F.latest(["S01", "S02"], 5)) == {"S02"}
    bad = rows("sample", id="S01", slot=0, horizon=5)[0]
    assert bad["p"] is None and bad["error"] == "unparseable"


def test_a_packet_that_cannot_be_built_is_reported_and_the_rest_still_runs(env):
    env["build_error"]["S02"] = "no close on/before 2026-10-23 for S02"
    res = go(IDS, arms=("B",))
    assert list(res["packetFailed"]) == ["S02"] and not res["complete"]
    assert not rows(id="S02") and len(rows("aggregate", arm="B")) == 10


def test_unknown_ids_and_bad_arms_or_horizons(env):
    res = go(["S01", "ZZZ"], arms=("R",))
    assert res["unknown"] == ["ZZZ"] and len(rows("aggregate", arm="R")) == 2
    with pytest.raises(ValueError):
        go(["S01"], arms=("X",))
    with pytest.raises(ValueError):
        go(["S01"], arms=("B",), horizons=(1,))


def test_forecast_ids_is_the_contract_entry_point(env):
    res = asyncio.run(F.forecast_ids(["S01", "S02"]))
    assert res["cohort"] == f"adhoc-{AS_OF.isoformat()}" and res["role"] == "adhoc"   # as_of = last completed session
    assert F.latest(["S01", "S02"], 5)["S02"]["p"] == pytest.approx(0.53)
    assert F.latest(["S01"], 20, arm="B")["S01"]["n"] == 8


def test_horizons_can_be_narrowed(env):
    asyncio.run(F.forecast_ids(["S01"], arms=("B",), horizons=(20,)))
    assert {r["horizon"] for r in rows("aggregate")} == {20}


# ---------------------------------------------------------------- fallback policy

def test_busy_lane_is_retried_with_growing_waits_then_the_whole_row_falls_back_to_opus(env):
    env["lane_fn"] = lambda pid, d, n: {"error": "lane_down"}
    deadline = NOW0 + 3600
    res = go(IDS, arms=("B",), deadline=deadline, priority=("S03",))
    assert env["sleeps"] == [30, 60, 120, 300, 600, 600, 600, 600, 600, 90]  # backoff, capped, ends at the deadline
    assert sum(env["sleeps"]) == 3600 and len(env["lane"]) == 11
    assert [c["id"] for c in env["claude"]] == ["S03", "S01", "S02", "S04", "S05", "S06"]   # priority first
    assert {c["model"] for c in env["claude"]} == {"opus"} and {c["purpose"] for c in env["claude"]} == {"forecast"}
    assert res["complete"] and res["fallback"]["opus"] == ["S03", "S01", "S02", "S04", "S05", "S06"]
    b = rows("aggregate", arm="B", id="S01", horizon=20)[0]
    assert (b["source"], b["role"], b["p"], b["n"], b["model"]) == ("opus", "fallback", 0.7, 1, "claude-opus")
    assert b["spread"] is None
    got = F.latest(["S01"], 20)["S01"]
    assert got["source"] == "opus" and got["role"] == "fallback"            # the source tag travels with the number
    assert not [r for r in rows("sample") if r["source"] == "local"]          # no local sample was ever answered
    call = rows("sample", id="S01", horizon=5)[0]
    assert call["arm"] == "B" and call["source"] == "opus" and call["slot"] is None and call["role"] == "fallback"


def test_a_name_with_a_pool_in_hand_at_the_deadline_is_replaced_whole_not_topped_up(env):
    healthy = env["lane_fn"]
    env["lane_fn"] = lambda pid, d, n: healthy(pid, d, n) if (pid == "S01" and n < 3) else {"error": "timeout"}
    go(["S01"], arms=("B",), deadline=NOW0 + 100)
    b = rows("aggregate", arm="B", id="S01", horizon=5)
    assert len(b) == 1 and b[0]["source"] == "opus" and b[0]["role"] == "fallback"
    assert len(rows("sample", id="S01", source="local", horizon=5)) == 3       # the 3 answers stay logged, unused


def test_opus_out_of_quota_falls_to_sonnet_and_is_not_asked_again(env):
    env["lane_fn"] = lambda pid, d, n: {"error": "lane_down"}
    env["claude_fn"] = lambda pid, model: ({"error": "limit", "detail": "window"} if model == "opus" else
                                           {"content": json.dumps({"p_outperform_5d": 0.62, "p_outperform_20d": 0.58, "crux": "c"}),
                                            "model": "claude-sonnet", "source": "sonnet", "usage": {}, "latencyS": 1.0})
    res = go(IDS[:3], arms=("B",), deadline=NOW0 + 60)
    assert [(c["id"], c["model"]) for c in env["claude"]] == [("S01", "opus"), ("S01", "sonnet"), ("S02", "sonnet"), ("S03", "sonnet")]
    assert res["fallback"]["sonnet"] == ["S01", "S02", "S03"] and res["fallback"]["opus"] == []
    b5, b20 = (rows("aggregate", arm="B", id="S02", horizon=h)[0] for h in (5, 20))
    assert (b5["source"], b5["p"], b20["p"]) == ("sonnet", 0.62, 0.58)
    assert F.latest(["S02"], 5)["S02"]["source"] == "sonnet"


def test_no_claude_means_a_hold_row_and_the_pm_sees_nothing(env):
    env["lane_fn"] = lambda pid, d, n: {"error": "lane_down"}
    env["claude_ok"] = False
    res = go(IDS[:2], arms=("B",), deadline=NOW0 + 60)
    assert env["claude"] == [] and res["fallback"]["hold"] == ["S01", "S02"] and res["complete"]
    hold = rows("aggregate", arm="B", id="S01", horizon=5)[0]
    assert (hold["p"], hold["source"], hold["role"], hold["error"]) == (None, "none", "fallback", "hold_default")
    assert F.latest(["S01", "S02"], 5) == {}
    again = go(IDS[:2], arms=("B",), deadline=NOW0 + 60)                       # never filled in later
    assert again["complete"] and len(rows("aggregate", arm="B")) == 4 and env["claude"] == []


def test_a_dead_claude_is_asked_once_not_per_name(env):
    env["lane_fn"] = lambda pid, d, n: {"error": "lane_down"}
    env["claude_fn"] = lambda pid, model: {"error": "disabled", "detail": "CLAUDE_ENABLED"}
    res = go(IDS[:3], arms=("B",), deadline=NOW0 + 60)
    assert len(env["claude"]) == 1 and res["fallback"]["hold"] == ["S01", "S02", "S03"]


def test_fallback_is_only_for_b_never_for_a_and_never_without_a_deadline(env):
    env["lane_fn"] = lambda pid, d, n: {"error": "lane_down"}
    go(IDS[:2], arms=("A",), deadline=NOW0 + 60)
    assert env["claude"] == [] and not rows("aggregate", arm="B")


# ---------------------------------------------------------------- arms D and E

def expected_subset(n):
    return sorted(IDS, key=lambda i: (hashlib.sha256(f"{F.SUBSET_SALT}:{i}".encode()).hexdigest(), i))[:n]


def test_d_runs_blind_opus_on_the_fixed_hash_subset_and_e_blends_where_both_exist(env, monkeypatch):
    monkeypatch.setattr(F, "SUBSET_SIZE", 3)
    res = go(IDS, arms=("B", "D", "E"))
    sub = expected_subset(3)
    assert F.stored_subset() == sub and json.loads(F.SUBSET_FILE.read_text())["salt"] == F.SUBSET_SALT
    assert sorted(c["id"] for c in env["claude"]) == sorted(sub) and {c["model"] for c in env["claude"]} == {"opus"}
    assert sorted(res["d"]["done"]) == sorted(sub)
    d = rows("aggregate", arm="D", id=sub[0], horizon=5)[0]
    assert (d["p"], d["source"], d["role"], d["n"]) == (0.7, "opus", "scheduled", 1)
    e = {r["id"]: r["p"] for r in rows("aggregate", arm="E", horizon=20)}
    assert set(e) == set(sub) and all(p == pytest.approx(0.5925, abs=2e-3) for p in e.values())   # blend(0.53, 0.7)
    assert not rows("aggregate", arm="D", id=[i for i in IDS if i not in sub][0])
    assert not [r for r in rows("aggregate", arm="B") if r["source"] != "local"]
    # the stored subset never reshuffles when the universe changes
    assert F.ensure_subset(["Z1", "Z2", "Z3", "Z4"]) == sub


def test_the_subset_is_the_first_30_by_hash_of_the_id(env):
    cands = [f"N{i:03d}" for i in range(100)]
    sub = F.ensure_subset(cands)
    want = sorted(cands, key=lambda i: (hashlib.sha256(f"{F.SUBSET_SALT}:{i}".encode()).hexdigest(), i))[:30]
    assert sub == want and len(sub) == 30
    assert F.ensure_subset(list(reversed(cands))) == want                      # order of the input is irrelevant


def test_one_opus_call_serves_arm_d_and_the_fallback(env, monkeypatch):
    monkeypatch.setattr(F, "SUBSET_SIZE", 3)
    env["lane_fn"] = lambda pid, d, n: {"error": "lane_down"}
    res = go(IDS, arms=("B", "D"), deadline=NOW0 + 60, priority=("S06",))
    sub = expected_subset(3)
    assert len(env["claude"]) == 6 and env["claude"][0]["id"] == "S06"          # one call per name, held name first
    for pid in IDS:
        b = rows("aggregate", arm="B", id=pid, horizon=5)[0]
        assert (b["source"], b["role"]) == ("opus", "fallback")
        assert (b.get("reused") == "D") == (pid in sub)
        assert bool(rows("aggregate", arm="D", id=pid, horizon=5)) == (pid in sub)
        assert rows("sample", id=pid, horizon=5)[0]["arm"] == ("D" if pid in sub else "B")
    assert sorted(res["d"]["done"]) == sorted(sub)
    assert not rows("aggregate", arm="E")


def test_d_waits_for_opus_before_the_deadline_and_is_final_after_it(env, monkeypatch):
    monkeypatch.setattr(F, "SUBSET_SIZE", 3)
    env["claude_fn"] = lambda pid, model: {"error": "limit", "detail": "window"}
    res = go(IDS, arms=("B", "D"), deadline=NOW0 + 86400)
    sub = expected_subset(3)
    assert not res["complete"] and sorted(res["pendingD"]) == sorted(sub)
    assert not rows("aggregate", arm="D") and {c["model"] for c in env["claude"]} == {"opus"}   # no Sonnet for D
    late = go(IDS, arms=("B", "D"), deadline=NOW0 - 1)
    assert late["complete"] and sorted(late["d"]["failed"]) == sorted(sub)
    dead = rows("aggregate", arm="D", id=sub[0], horizon=5)[0]
    assert dead["p"] is None and dead["error"] == "opus_unavailable"
    assert not rows("aggregate", arm="E")                                      # E needs B and D


# ---------------------------------------------------------------- log

def test_the_log_skips_corrupt_lines_and_never_glues_rows_after_a_torn_write(env):
    assert F.append_rows([{"kind": "aggregate", "id": "S01", "n": 1}])
    with open(F.LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "aggregate", "id": "S02", "n"')                      # a crash mid-line, no newline
    assert F.append_rows([{"kind": "aggregate", "id": "S03", "n": 3}])
    assert [r["id"] for r in F.read_log()] == ["S01", "S03"]


def test_an_unserialisable_row_writes_nothing(env):
    assert F.append_rows([{"kind": "aggregate", "id": "S01", "p": float("nan")}]) is False
    assert not F.LOG_FILE.exists() or F.LOG_FILE.read_text() == ""
    assert F.read_log() == []


def test_the_log_cache_follows_the_file(env):
    F.append_rows([{"kind": "aggregate", "arm": "B", "id": "S01", "horizon": 20, "p": 0.4, "asOf": "2026-10-16", "createdAt": 1}])
    assert F.latest(["S01"], 20)["S01"]["p"] == 0.4
    F.append_rows([{"kind": "aggregate", "arm": "B", "id": "S01", "horizon": 20, "p": 0.6, "asOf": "2026-10-23", "createdAt": 2}])
    assert F.latest(["S01"], 20)["S01"]["p"] == 0.6


def test_a_run_aborts_when_the_log_cannot_be_written(env, monkeypatch):
    monkeypatch.setattr(F, "append_rows", lambda rows: False)
    with pytest.raises(F.ForecastLogError):
        go(["S01"], arms=("R",))


# ---------------------------------------------------------------- trial ledger

def test_trial_ledger_records_a_hash_per_day_and_a_row_per_change(env, monkeypatch, tmp_path):
    t1 = datetime.datetime(2026, 10, 21, 9, 0).timestamp()
    first = F.record_config(t1, IDS)
    assert first["changed"] and len(F.ledger()["rows"]) == 1 and F.ledger()["rows"][0]["changedKeys"] == ["initial"]
    assert not F.record_config(t1 + 3600, IDS)["changed"]
    assert not F.record_config(t1 + 86400, IDS)["changed"]
    doc = F.ledger()
    assert len(doc["rows"]) == 1 and doc["daily"] == {"2026-10-21": first["hash"], "2026-10-22": first["hash"]}
    cfg = doc["rows"][0]["config"]
    assert cfg["nSamples"] == 8 and cfg["kappa"] == 0.3 and cfg["blend"] == {"B": 0.65, "D": 0.35}
    assert cfg["arms"] == list(F.ARMS) and set(cfg["prompts"]) == {"forecaster", "untrustedClause", "question"}
    assert cfg["universeHash"] == F.universe_hash(IDS) and cfg["seedSent"] is False

    monkeypatch.setattr(F, "KAPPA", 0.4)
    second = F.record_config(t1 + 2 * 86400, IDS)
    assert second["changed"] and second["hash"] != first["hash"]
    assert F.ledger()["rows"][-1]["changedKeys"] == ["kappa"]
    monkeypatch.setattr(F, "KAPPA", 0.3)
    other = tmp_path / "forecaster.md"
    other.write_text("a different prompt")
    monkeypatch.setattr(F, "PLAYBOOK", other)
    assert F.record_config(t1 + 3 * 86400, IDS)["changed"]
    assert F.ledger()["rows"][-1]["changedKeys"] == ["kappa", "prompts"]       # back to 0.3 AND a new prompt: both differ from row 2
    assert F.record_config(t1 + 4 * 86400, IDS + ["S99"])["changed"]
    assert F.ledger()["rows"][-1]["changedKeys"] == ["universeHash"]


# ---------------------------------------------------------------- labels

def closes_for(rets, as_of=AS_OF, h=5, last=None):
    """{id: closes} with close 100 on the as-of and 100 * (1 + r/100) on the target session."""
    from app.api import calendars
    target = calendars.add_sessions("XETR", as_of, h)
    out = {}
    for pid, r in rets.items():
        out[pid] = [((as_of - datetime.timedelta(days=3)).isoformat(), 99.0), (as_of.isoformat(), 100.0),
                    (target.isoformat(), 100.0 * (1 + r / 100.0))]
    return out


def test_label_is_one_when_the_return_beats_the_universe_median(env):
    rets = {f"U{i:02d}": float(i) for i in range(12)}                         # 0..11: median 5.5
    lab = F.compute_label(AS_OF, 5, list(rets), closes_for(rets))
    assert lab["target"] == "2026-10-30" and lab["n"] == 12 and lab["median"] == 5.5
    assert [p for p, y in lab["y"].items() if y == 1] == [f"U{i:02d}" for i in range(6, 12)]
    assert lab["ex"]["U11"] == 5.5 and lab["ex"]["U00"] == -5.5


def test_a_name_exactly_at_the_median_is_left_out(env):
    rets = {f"U{i:02d}": float(i) for i in range(11)}                         # odd count: U05 IS the median
    lab = F.compute_label(AS_OF, 5, list(rets), closes_for(rets))
    assert "U05" not in lab["y"] and len(lab["y"]) == 10 and lab["median"] == 5.0


def test_label_waits_for_enough_names_with_a_close_on_the_target(env):
    rets = {f"U{i:02d}": float(i) for i in range(12)}
    closes = closes_for(rets)
    for pid in ("U00", "U01", "U02"):                                          # their series end before the target
        closes[pid] = closes[pid][:2]
    assert F.compute_label(AS_OF, 5, list(rets), closes) is None
    closes = closes_for(rets)
    closes["U00"] = [("2026-10-01", 99.0), ("2026-10-02", 100.0)] + closes["U00"][2:]   # stale entry price, 3 weeks old
    assert F.compute_label(AS_OF, 5, list(rets), closes)["n"] == 11


def test_update_labels_waits_for_the_target_session_and_never_recomputes(env, monkeypatch):
    rets = {f"U{i:02d}": float(i) for i in range(12)}
    F.put_cohort({"id": COHORT, "kind": "weekly", "asOf": "2026-10-23", "ids": list(rets), "status": "done"})
    series = {"v": closes_for(rets, h=5)}

    async def closes_of(pid):
        return series["v"][pid]

    monkeypatch.setattr(F, "_closes_of", closes_of)
    early = datetime.datetime(2026, 10, 29, 18, 30, tzinfo=BERLIN).timestamp()      # target 10-30 not complete
    assert asyncio.run(F.update_labels(early)) == {"added": 0, "pending": 2}
    assert F.read_labels() == {}
    on_time = datetime.datetime(2026, 10, 30, 18, 30, tzinfo=BERLIN).timestamp()
    assert asyncio.run(F.update_labels(on_time)) == {"added": 1, "pending": 1}       # 5d labelled, 20d not yet
    assert set(F.read_labels()["2026-10-23"]) == {"5"}
    series["v"] = closes_for({k: -v for k, v in rets.items()}, h=5)                  # the data changes: labels must not
    assert asyncio.run(F.update_labels(on_time))["added"] == 0
    assert F.read_labels()["2026-10-23"]["5"]["y"]["U11"] == 1


def test_label_universe_is_pinned_by_the_weekly_cohort_record(env):
    F.put_cohort({"id": "w2026-10-16", "kind": "weekly", "asOf": "2026-10-16", "ids": ["A", "B"], "status": "done"})
    F.put_cohort({"id": "w2026-10-23", "kind": "weekly", "asOf": "2026-10-23", "ids": ["A", "B", "C"], "status": "done"})
    F.put_cohort({"id": "d2026-10-22", "kind": "daily", "asOf": "2026-10-22", "ids": ["Z"], "status": "done"})
    assert F.label_universe("2026-10-22") == ["A", "B"] and F.label_universe("2026-10-23") == ["A", "B", "C"]
    assert F.label_universe("2026-10-01") == IDS                                 # before any cohort: today's candidates


# ---------------------------------------------------------------- grading

def test_calibration_slope_is_one_for_calibrated_and_half_for_overconfident_forecasts(env):
    ps = [0.7] * 100 + [0.3] * 100
    calibrated = [1] * 70 + [0] * 30 + [1] * 30 + [0] * 70
    assert F.calibration_slope(ps, calibrated) == pytest.approx(1.0, abs=0.01)
    over = [1] * 60 + [0] * 40 + [1] * 40 + [0] * 60                              # true rates 0.6 / 0.4
    assert F.calibration_slope(ps, over) == pytest.approx(math.log(1.5) / math.log(7 / 3), abs=0.01)
    assert F.calibration_slope([0.5] * 100, [1, 0] * 50) is None                  # constant forecast
    assert F.calibration_slope(ps[:20], calibrated[:20]) is None                  # too few pairs
    assert F.calibration_slope([0.6] * 40, [1] * 40) is None                      # one label class


def test_holm_step_down():
    assert F.holm([0.01, 0.04, 0.03, 0.5]) == [0.04, 0.09, 0.09, 0.5]
    assert F.holm([None, 0.01, 0.2, 0.3]) == [None, 0.04, 0.6, 0.6]


def test_comparison_status_follows_the_readout_schedule():
    assert F.comparison_status(0, 0) == "pending"
    assert F.comparison_status(29, 6) == "insufficient" and F.comparison_status(90, 3) == "insufficient"
    assert F.comparison_status(150, 5) == "sanity" and F.comparison_status(240, 8) == "interim"
    assert F.comparison_status(360, 12) == "verdict"


def pair(cohort, i, arm, source, p, y, ex=None, h=5, asof=None):
    return {"cohort": cohort, "id": i, "arm": arm, "source": source, "horizon": h, "p": p, "y": y,
            "ex": ex if ex is not None else (1.0 if y else -1.0), "asOf": asof or cohort[1:]}


def test_cell_metrics_for_a_perfect_and_a_constant_forecaster():
    n = 10
    perfect = [pair("w2026-10-23", f"S{i}", "B", "local", 0.9 if i >= 5 else 0.1, 1 if i >= 5 else 0, ex=float(i - 4.5)) for i in range(n)]
    c = F._cell(perfect)
    assert c["n"] == 10 and c["brier"] == pytest.approx(0.01) and c["brierR"] == 0.25
    assert c["bss"] == pytest.approx(0.96) and c["icCohorts"] == 1 and 0.8 < c["ic"] < 1.0     # two tied p values: rank IC < 1
    ranked = F._cell([pair("w2026-10-23", f"S{i}", "B", "local", 0.5 + 0.04 * i, i % 2, ex=float(i)) for i in range(n)])
    assert ranked["ic"] == pytest.approx(1.0)
    assert F._cell([pair(f"w2026-10-{20 + i}", f"S{i}", "B", "local", 0.5 + 0.04 * i, i % 2, ex=float(i)) for i in range(4)])["ic"] is None   # < 5 names per cohort
    assert c["hitRate"]["hits"] == 10 and c["hitRate"]["n"] == 10
    assert c["hitRate"]["wilson95"] == F.scoreboard.wilson(10, 10) and c["insufficient"] is True
    flat = F._cell([pair("w2026-10-23", f"S{i}", "R", "code", 0.5, i % 2) for i in range(n)])
    assert flat["bss"] == 0.0 and flat["ic"] is None and flat["hitRate"]["n"] == 0 and flat["calibrationSlope"] is None


def write_experiment(tmp_path, cohorts=12, *, b_vs_a="better", with_mixed=False, labelled=True):
    """Hand-written log + labels: 30 subset names (+ 1 outside it) in `cohorts` weekly cohorts. B is right 70 %
    of the time (p .6/.4), A / Q / D / E say 0.5, or B = A when b_vs_a == 'same'."""
    sub = [f"T{i:02d}" for i in range(30)]
    jsonstore.save(F.SUBSET_FILE, {"salt": "t", "size": 30, "ids": sub})
    start = datetime.date(2026, 1, 2)
    log_rows, labels = [], {}
    for k in range(cohorts + (1 if with_mixed else 0)):
        asof = (start + datetime.timedelta(days=7 * k)).isoformat()
        cohort = f"w{asof}"
        mixed = with_mixed and k == cohorts
        ys = {}
        for i, pid in enumerate(sub + ["X01"]):
            y = 1 if (k * 7 + i) % 3 else 0
            ys[pid] = y
            right = (i % 10) not in (0, 1, 2)
            p_b = (0.6 if y else 0.4) if right else (0.4 if y else 0.6)
            src = "opus" if mixed and i < 5 else "local"
            role = "fallback" if src != "local" else "scheduled"
            base = {"kind": "aggregate", "cohort": cohort, "asOf": asof, "id": pid, "horizon": 5, "n": 8, "createdAt": k}
            log_rows += [{**base, "arm": "B", "source": src, "role": role, "p": p_b},
                         {**base, "arm": "A", "source": "local", "role": "scheduled", "p": p_b if b_vs_a == "same" else 0.5},
                         {**base, "arm": "Q", "source": "code", "role": "scheduled", "p": 0.5},
                         {**base, "arm": "D", "source": "opus", "role": "scheduled", "p": 0.5},
                         {**base, "arm": "E", "source": "blend", "role": "scheduled", "p": 0.5}]
        labels[asof] = {"5": {"target": "x", "n": 31, "median": 0.0, "y": ys, "ex": {p: (1.0 if y else -1.0) for p, y in ys.items()}}}
        F.put_cohort({"id": cohort, "kind": "weekly", "asOf": asof, "ids": sub, "status": "done"})
    F.append_rows(log_rows)
    if labelled:
        jsonstore.save(F.LABELS_FILE, {"labels": labels})


def test_twelve_cohorts_give_a_verdict_with_week_block_ci_and_holm(env, tmp_path):
    write_experiment(tmp_path, 12)
    out = F.arms_payload(NOW0)
    comps = {c["id"]: c for c in out["comparisons"] if c["horizon"] == 5}
    ba = comps["B-A"]
    assert ba["status"] == "verdict" and ba["n"] == 360 and ba["cohorts"] == 12      # X01 (not in the subset) is excluded
    assert ba["meanDeltaBrier"] == pytest.approx(0.7 * (0.16 - 0.25) + 0.3 * (0.36 - 0.25), abs=2e-3)
    lo, hi = ba["ci95"]
    assert lo <= ba["meanDeltaBrier"] <= hi and hi < 0                              # CI excludes 0: B beats A
    assert ba["significant"] is True and ba["favours"] == "B" and ba["pHolm"] >= ba["pApprox"]
    for cid in ("B-Q", "D-B", "E-B"):
        assert comps[cid]["significant"] is True
    assert comps["D-B"]["meanDeltaBrier"] > 0 and comps["D-B"]["favours"] == "B"     # D (constant) is worse than B
    assert comps["B-A"]["primary"] is True
    secondary = [c for c in out["comparisons"] if c["horizon"] == 20]
    assert len(secondary) == 4 and all(c["status"] == "pending" and not c["primary"] for c in secondary)


def test_identical_arms_are_not_significant_and_few_cohorts_are_only_a_sanity_look(env, tmp_path):
    write_experiment(tmp_path, 12, b_vs_a="same")
    ba = next(c for c in F.arms_payload(NOW0)["comparisons"] if c["id"] == "B-A" and c["horizon"] == 5)
    assert ba["meanDeltaBrier"] == 0.0 and ba["significant"] is False and ba["favours"] is None


@pytest.mark.parametrize("cohorts,status", [(3, "insufficient"), (5, "sanity"), (9, "interim")])
def test_early_looks_report_no_verdict(env, tmp_path, cohorts, status):
    write_experiment(tmp_path, cohorts)
    ba = next(c for c in F.arms_payload(NOW0)["comparisons"] if c["id"] == "B-A" and c["horizon"] == 5)
    assert ba["status"] == status and ba["significant"] is None and ba["favours"] is None
    assert (ba["ci95"] is not None) == (ba["n"] >= 30)


def test_no_labels_means_pending_everywhere(env, tmp_path):
    write_experiment(tmp_path, 4, labelled=False)
    out = F.arms_payload(NOW0)
    assert out["cells"] == [] and all(c["status"] == "pending" and c["n"] == 0 for c in out["comparisons"])
    assert out["experiment"]["ungradedRows"] > 0 and out["experiment"]["maturedCohorts5d"] == []


def test_cells_are_keyed_by_arm_source_and_horizon_and_mixed_cohorts_are_reported_apart(env, tmp_path):
    write_experiment(tmp_path, 12, with_mixed=True)
    out = F.arms_payload(NOW0)
    keys = {(c["arm"], c["source"], c["horizon"]) for c in out["cells"]}
    assert keys == {("Q", "code", 5), ("A", "local", 5), ("B", "local", 5), ("D", "opus", 5), ("E", "blend", 5)}
    assert next(c for c in out["cells"] if c["arm"] == "B")["n"] == 12 * 31          # the mixed cohort is not in there
    mixed = {(c["arm"], c["source"]) for c in out["mixedCells"]}
    assert ("B", "opus") in mixed and ("B", "local") in mixed                       # sources stay separate even there
    exp = out["experiment"]
    last = f"w{(datetime.date(2026, 1, 2) + datetime.timedelta(days=7 * 12)).isoformat()}"
    assert exp["mixedCohorts"] == [last] and exp["composition"][last]["fallbackShare"] == round(5 / 31, 3)
    assert exp["composition"][last]["mixed"] is True and exp["fallbackCohortShare"] == round(1 / 13, 3)
    assert exp["underpowered"] is False
    ba = next(c for c in out["comparisons"] if c["id"] == "B-A" and c["horizon"] == 5)
    assert ba["cohorts"] == 12                                                       # comparisons use pure cohorts only


def test_daily_forecasts_are_graded_apart_from_the_experiment(env, tmp_path):
    write_experiment(tmp_path, 4)
    base = {"kind": "aggregate", "cohort": "d2026-01-06", "asOf": "2026-01-02", "id": "T00", "horizon": 5, "arm": "B",
            "source": "local", "role": "daily", "p": 0.9, "n": 8, "createdAt": 99}
    F.append_rows([base])
    out = F.arms_payload(NOW0)
    assert [(c["arm"], c["n"]) for c in out["dailyCells"]] == [("B", 1)]
    assert next(c for c in out["cells"] if c["arm"] == "B")["n"] == 4 * 31


def test_readout_schedule_and_experiment_status(env, tmp_path):
    F.put_cohort({"id": "w2026-10-16", "kind": "weekly", "asOf": "2026-10-16", "ids": IDS, "status": "done"})
    F.put_cohort({"id": "w2026-10-23", "kind": "weekly", "asOf": "2026-10-23", "ids": IDS, "status": "running"})
    out = F.arms_payload(NOW0)
    assert out["arms"] == list(F.ARMS) and out["horizons"] == [5, 20]
    r = out["readout"]
    assert r["weeks"] == [4, 8, 12] and r["currentWeek"] == 2 and r["next"] == {"week": 4, "kind": "sanity"}
    assert r["kinds"] == {"4": "sanity", "8": "interim", "12": "verdict"} and r["frozenForWeeks"] == 12
    assert out["experiment"]["cohortStatus"] == {"w2026-10-16": "done", "w2026-10-23": "running"}
    assert F.readout(12)["next"] is None
