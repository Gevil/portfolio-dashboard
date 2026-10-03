"""lane_client: lane preference, probe cache, pinning, budget, 400 fallback.

Pure: no network, no sleeps. Probes and completions are faked at the module's
own seams (``_probe_once``, ``_post_completion``); the budget file lives in
``tmp_path``.

  podman run --rm -v $PWD:/work -w /work localhost/portfolio-dashboard:latest \
    bash -c "pip install -q pytest && PYTHONPATH=/work python -m pytest tests/unit -v"
"""
import asyncio
import datetime
import json

import httpx
import pytest

from app.api import lane_client as lc


def lane(name, model="m-" + "x", port=8000):
    return {"name": name, "unit": f"{name}.service",
            "health_url": f"http://127.0.0.1:{port}/health", "boot_wait_s": 0,
            "min_free_mib": 0, "model_id": model, "container_base": ""}


A = lane("ninfer-nvfp4", "qwen3.8-27b", 8002)
B = lane("exllama", "Qwen3.8-Flash-Next-exl3", 8003)
V = lane("vllm", "", 8001)                       # no model id: never eligible


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake lanes.conf contents + probe results + isolated budget file."""
    lc.reset_probe_cache()
    monkeypatch.setattr(lc, "BUDGET_FILE", str(tmp_path / "lane_budget.json"))
    for k in ("AUTONOMOUS_TURN_BUDGET", "DIGEST_TURN_RESERVE", "TRIAGE_TURN_CAP",
              "LANE_PREFERENCE"):
        monkeypatch.delenv(k, raising=False)
    state = {"lanes": [V, B, A],               # conf order: exllama BEFORE ninfer
             "probe": {"ninfer-nvfp4": ("ok", "http://a"),
                       "exllama": ("ok", "http://b")},
             "calls": []}
    monkeypatch.setattr(lc, "lanes", lambda: state["lanes"])

    async def fake_probe(l):
        state["calls"].append(l["name"])
        await asyncio.sleep(0)
        return state["probe"].get(l["name"], ("down", ""))

    monkeypatch.setattr(lc, "_probe_once", fake_probe)
    yield state
    lc.reset_probe_cache()


# ---------------------------------------------------------------- preference

def test_order_lanes_preference_then_conf_order():
    conf = [V, B, A, lane("extra", "e", 8004)]
    names = [l["name"] for l in lc.order_lanes(conf, ["ninfer-nvfp4", "exllama"])]
    assert names == ["ninfer-nvfp4", "exllama", "extra"]      # vllm: no model
    # unlisted lanes follow in conf order; an unknown preference is ignored
    names = [l["name"] for l in lc.order_lanes(conf, ["nope", "extra"])]
    assert names == ["extra", "exllama", "ninfer-nvfp4"]


def test_pick_prefers_primary_even_when_conf_lists_fallback_first(env):
    lane_, reason, name, base = asyncio.run(lc._pick_lane())
    assert (reason, name, base) == ("ok", "ninfer-nvfp4", "http://a")


def test_pick_falls_back_when_primary_down(env):
    env["probe"]["ninfer-nvfp4"] = ("down", "")
    lane_, reason, name, base = asyncio.run(lc._pick_lane())
    assert (reason, name) == ("ok", "exllama")
    active = asyncio.run(lc.active_lane())
    assert active["role"] == "fallback" and active["name"] == "exllama"


def test_pick_reasons(env):
    env["probe"] = {"ninfer-nvfp4": ("down", ""), "exllama": ("wrong_model", "")}
    assert asyncio.run(lc._pick_lane())[1:3] == ("model_missing", "exllama")
    lc.reset_probe_cache()
    env["probe"] = {}
    assert asyncio.run(lc._pick_lane())[1] == "lane_down"
    env["lanes"] = [V]
    lc.reset_probe_cache()
    assert asyncio.run(lc._pick_lane())[1] == "no_lanes"


def test_lane_preference_env(env, monkeypatch):
    monkeypatch.setenv("LANE_PREFERENCE", "exllama,ninfer-nvfp4")
    assert asyncio.run(lc._pick_lane())[2] == "exllama"


def test_lane_status_shape(env):
    env["probe"]["ninfer-nvfp4"] = ("down", "")
    st = asyncio.run(lc.lane_status())
    assert st["lane"] == "exllama" and st["serving_model"] is True
    assert st["model"] == "Qwen3.8-Flash-Next-exl3" and st["role"] == "fallback"
    assert st["preference"] == ["ninfer-nvfp4", "exllama"]
    states = {c["name"]: c["state"] for c in st["candidates"]}
    assert states == {"ninfer-nvfp4": "down", "exllama": "ok", "vllm": "no_model"}
    assert {"day", "used", "cap", "left", "purposes"} <= set(st["budget"])
    env["probe"] = {}
    lc.reset_probe_cache()
    st = asyncio.run(lc.lane_status())
    assert st["lane"] is None and st["serving_model"] is False
    assert st["role"] is None and st["base_url"] is None


# --------------------------------------------------------------- probe cache

def test_probe_cache_positive_and_negative(env):
    async def go():
        for _ in range(3):
            await lc._pick_lane()
    asyncio.run(go())
    assert env["calls"] == ["ninfer-nvfp4"]               # cached: one probe
    env["calls"].clear()
    env["probe"]["ninfer-nvfp4"] = ("down", "")
    lc.reset_probe_cache()

    async def go2():
        for _ in range(3):
            await lc._pick_lane()
    asyncio.run(go2())
    # primary probed once (negative cache), fallback once (positive cache)
    assert sorted(env["calls"]) == ["exllama", "ninfer-nvfp4"]


def test_probe_cache_expires(env, monkeypatch):
    monkeypatch.setattr(lc, "PROBE_OK_TTL_S", 0.0)

    async def go():
        await lc._pick_lane()
        await lc._pick_lane()
    asyncio.run(go())
    assert env["calls"] == ["ninfer-nvfp4", "ninfer-nvfp4"]


def test_concurrent_probes_are_single_flight(env):
    async def go():
        return await asyncio.gather(*(lc._pick_lane() for _ in range(8)))
    res = asyncio.run(go())
    assert env["calls"] == ["ninfer-nvfp4"]               # never hit twice at once
    assert all(r[2] == "ninfer-nvfp4" for r in res)


def test_down_lane_gets_the_short_probe_budget(env):
    assert lc._probe_budget(A) == (lc.PROBE_TIMEOUT_S, lc.PROBE_GRACE_S)
    env["probe"]["ninfer-nvfp4"] = ("down", "")
    asyncio.run(lc._pick_lane())
    assert lc._probe_budget(A) == (lc.PROBE_DOWN_TIMEOUT_S,)


# ------------------------------------------------------------------- pinning

def test_pin_sticks_to_one_lane_then_falls_back_once(env):
    async def go():
        with lc.pin() as p:
            first = await lc._pick_lane()
            assert p.lane == "ninfer-nvfp4" and p.model == "qwen3.8-27b"
            # preference flips mid-job: the pin does not move
            env["probe"]["exllama"] = ("ok", "http://b")
            again = await lc._pick_lane()
            # the pinned lane dies: ONE fallback, recorded
            env["probe"]["ninfer-nvfp4"] = ("down", "")
            lc.reset_probe_cache()
            moved = await lc._pick_lane()
            assert p.fell_back and moved[2] == "exllama"
            assert p.fallbacks[0]["from"] == "ninfer-nvfp4"
            assert p.fallbacks[0]["to"] == "exllama"
            # the fallback dies too: no second fallback, an error envelope
            env["probe"]["exllama"] = ("down", "")
            env["probe"]["ninfer-nvfp4"] = ("ok", "http://a")
            lc.reset_probe_cache()
            dead = await lc._pick_lane()
            return first, again, dead, p
    first, again, dead, p = asyncio.run(go())
    assert first[2] == again[2] == "ninfer-nvfp4"
    assert dead[0] is None and dead[1] == "lane_down"
    assert len(p.fallbacks) == 1


def test_pinned_call_retries_once_on_the_fallback(env, monkeypatch):
    async def fake_post(l, base, body, timeout):
        if l["name"] == "ninfer-nvfp4":
            env["probe"]["ninfer-nvfp4"] = ("down", "")
            return {"error": "lane_down"}
        return {"content": "ok", "finish_reason": "stop", "lane": l["name"],
                "model": body["model"], "usage": {}}
    monkeypatch.setattr(lc, "_post_completion", fake_post)

    async def go():
        with lc.pin() as p:
            return await lc.chat([{"role": "user", "content": "x"}]), p
    res, p = asyncio.run(go())
    assert res["content"] == "ok" and res["lane"] == "exllama"
    assert res["model"] == "Qwen3.8-Flash-Next-exl3"
    assert p.fell_back and p.model == "Qwen3.8-Flash-Next-exl3"
    assert res["lane_fallbacks"][0]["to"] == "exllama"


def test_unpinned_lane_down_is_just_an_envelope(env, monkeypatch):
    async def fake_post(l, base, body, timeout):
        return {"error": "lane_down"}
    monkeypatch.setattr(lc, "_post_completion", fake_post)
    res = asyncio.run(lc.chat([{"role": "user", "content": "x"}]))
    assert res == {"error": "lane_down"}


# -------------------------------------------------------------------- budget

def today():
    return datetime.date.today().isoformat()


def test_default_allowances(env):
    st = lc.budget_state()
    assert st["cap"] == 30 and st["left"] == 30
    assert st["digest_reserve"] == 16 and st["triage_cap"] == 10
    assert st["purposes"]["digest"]["left"] == 30
    assert st["purposes"]["triage"]["left"] == 10
    assert st["purposes"]["other"]["allowance"] == 4
    assert lc.budget_state("triage")["left"] == 10
    assert lc.budget_state("approval")["left"] == 4


def test_digest_reserve_is_untouchable_by_others(env):
    for _ in range(10):                                    # triage hits its cap
        assert lc._charge("triage") is not None
    assert lc._charge("triage") is None
    assert lc.can_start("triage", 1) is False
    assert lc.can_start("approval", 4) is True
    assert lc.can_start("approval", 5) is False            # other allowance = 4
    for _ in range(4):
        assert lc._charge("approval") is not None
    assert lc._charge("other") is None
    # 14 used; the digest still has all 16 of its reserve
    st = lc.budget_state("digest")
    assert st["used"] == 14 and st["left"] == 16
    assert lc.can_start("digest", 16) is True
    assert lc.can_start("digest", 17) is False


def test_env_overrides(env, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TURN_BUDGET", "10")
    monkeypatch.setenv("DIGEST_TURN_RESERVE", "6")
    monkeypatch.setenv("TRIAGE_TURN_CAP", "2")
    st = lc.budget_state()
    assert (st["cap"], st["digest_reserve"], st["triage_cap"]) == (10, 6, 2)
    assert st["purposes"]["other"]["allowance"] == 2


def test_old_budget_file_is_tolerated(env, tmp_path):
    (tmp_path / "lane_budget.json").write_text(json.dumps(
        {"day": today(), "used": 12, "cap": 12}))
    st = lc.budget_state()
    assert st["used"] == 12 and st["cap"] == 30 and st["left"] == 18
    (tmp_path / "lane_budget.json").write_text(json.dumps(
        {"day": "2000-01-01", "used": 12, "cap": 12}))
    assert lc.budget_state()["used"] == 0                  # another day: reset
    (tmp_path / "lane_budget.json").write_text("{not json")
    assert lc.budget_state()["used"] == 0                  # corrupt: fresh ledger


def test_reservation_holds_and_releases(env, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TURN_BUDGET", "6")
    monkeypatch.setenv("DIGEST_TURN_RESERVE", "6")
    monkeypatch.setenv("TRIAGE_TURN_CAP", "0")
    with lc.reservation("digest", 4):
        assert lc.budget_state()["held"] == 4
        assert lc.can_start("digest", 3) is False          # only 2 unheld
        assert lc.can_start("digest", 2) is True
        charge = lc._charge("digest")                      # draws from the hold
        assert charge is not None
        st = lc.budget_state()
        assert st["used"] == 1 and st["held"] == 3
        lc._refund(charge)                                 # never reached the model
        st = lc.budget_state()
        assert st["used"] == 0 and st["held"] == 4
        assert lc._charge("digest") is not None
    st = lc.budget_state()
    assert st["used"] == 1 and st["held"] == 0             # unused turns released
    with pytest.raises(lc.BudgetError):
        with lc.reservation("digest", 6):                  # only 5 left
            pass


def test_refund_only_for_turns_that_never_reached_the_model(env, monkeypatch):
    results = iter([{"error": "lane_down"}, {"error": "timeout"},
                    {"error": "http", "status": 503},
                    {"error": "http", "status": 400},
                    {"content": "ok", "finish_reason": "stop"}])

    async def fake_post(l, base, body, timeout):
        return dict(next(results))
    monkeypatch.setattr(lc, "_post_completion", fake_post)

    async def go():
        used = []
        for _ in range(5):
            await lc.chat([{"role": "user", "content": "x"}], autonomous=True,
                          purpose="digest")
            used.append(lc.budget_state()["used"])
        return used
    assert asyncio.run(go()) == [0, 0, 0, 1, 2]


def test_check_and_charge_is_atomic_under_concurrency(env, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TURN_BUDGET", "1")
    monkeypatch.setenv("DIGEST_TURN_RESERVE", "1")
    monkeypatch.setenv("TRIAGE_TURN_CAP", "0")

    async def fake_post(l, base, body, timeout):
        await asyncio.sleep(0)
        return {"content": "ok", "finish_reason": "stop", "lane": l["name"],
                "model": body["model"], "usage": {}}
    monkeypatch.setattr(lc, "_post_completion", fake_post)

    async def go():
        return await asyncio.gather(*(
            lc.chat([{"role": "user", "content": "x"}], autonomous=True,
                    purpose="digest") for _ in range(5)))
    res = asyncio.run(go())
    assert sum(1 for r in res if r.get("content") == "ok") == 1
    assert sum(1 for r in res if r.get("error") == "budget") == 4
    assert lc.budget_state()["used"] == 1


def test_interactive_chat_is_never_charged(env, monkeypatch):
    async def fake_post(l, base, body, timeout):
        return {"content": "hi", "finish_reason": "stop"}
    monkeypatch.setattr(lc, "_post_completion", fake_post)
    asyncio.run(lc.chat([{"role": "user", "content": "x"}]))
    assert lc.budget_state()["used"] == 0


def test_streaming_autonomous_is_refused():
    with pytest.raises(ValueError):
        lc.chat([], stream=True, autonomous=True)


# ------------------------------------------------ optional-field 400 fallback

def _client_factory(monkeypatch, handler):
    real = httpx.AsyncClient

    def make(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(lc.httpx, "AsyncClient", make)


def _ok_response():
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 3}})


def test_400_on_response_format_and_chat_template_kwargs_retries(env, monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(sorted(k for k in ("response_format", "chat_template_kwargs")
                           if k in body))
        if "response_format" in body:
            return httpx.Response(400, text="response_format is not supported")
        if "chat_template_kwargs" in body:
            return httpx.Response(400, text="Extra inputs are not permitted")
        return _ok_response()
    _client_factory(monkeypatch, handler)

    async def call():
        return await lc.chat([{"role": "user", "content": "x"}], json_mode=True,
                             enable_thinking=False)
    res = asyncio.run(call())
    assert res["content"] == "answer"
    assert set(res["dropped_fields"]) == {"response_format", "chat_template_kwargs"}
    assert res["lane"] == "ninfer-nvfp4"
    assert len(seen) == 3
    seen.clear()
    res = asyncio.run(call())                  # remembered: no more 400 round trips
    assert res["content"] == "answer" and seen == [[]]


def test_400_naming_nothing_drops_every_optional_field(env, monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        if "response_format" in body or "chat_template_kwargs" in body:
            return httpx.Response(400, text="bad request")
        return _ok_response()
    _client_factory(monkeypatch, handler)
    res = asyncio.run(lc.chat([{"role": "user", "content": "x"}], json_mode=True,
                              enable_thinking=False))
    assert res["content"] == "answer"


def test_400_without_optional_fields_is_a_plain_http_error(env, monkeypatch):
    _client_factory(monkeypatch, lambda r: httpx.Response(400, text="nope"))
    res = asyncio.run(lc.chat([{"role": "user", "content": "x"}]))
    assert res == {"error": "http", "status": 400}


def test_transport_errors_map_to_envelopes(env, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")
    _client_factory(monkeypatch, refuse)
    assert asyncio.run(lc.chat([{"role": "user", "content": "x"}])) == {
        "error": "lane_down"}


def test_inline_think_block_is_not_the_answer():
    data = {"choices": [{"message": {"content": "<think>hmm</think>\nthe answer"}}]}
    assert lc._content(data) == "the answer"
    data = {"choices": [{"message": {"content": "<think>never closed"}}]}
    assert lc._content(data) == ""
    data = {"choices": [{"message": {"content": None,
                                     "reasoning_content": "secret"}}]}
    assert lc._content(data) == ""
