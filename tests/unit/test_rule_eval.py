"""rule_eval: state pruning, absolute rule on the listing quote, no benchmark."""
import asyncio
import time

import pytest

from app.api import notify, prices, rule_eval

DAY = 86400


# ------------------------------------------------------------------- prune

def test_prune_keeps_live_entries_whatever_their_age():
    old = time.time() - 90 * DAY
    state = {
        "ASML#0#absolute": {"fired": True, "fired_at": "2026-01-01",
                            "touch": old},                    # live one-shot
        "NVDA#0#ema_cross": {"last_bar_t": 5, "touch": old},  # live baseline
        "GONE#0#absolute": {"fired": True, "touch": old},     # rule deleted
        "NEW#0#absolute": {"side": "above", "touch": time.time()},
    }
    live = {"ASML#0#absolute", "NVDA#0#ema_cross"}
    kept = rule_eval._prune(state, live)
    assert set(kept) == {"ASML#0#absolute", "NVDA#0#ema_cross",
                         "NEW#0#absolute"}
    assert kept["ASML#0#absolute"]["fired"] is True


def test_evaluate_rule_refreshes_touch_of_a_fired_one_shot(monkeypatch):
    monkeypatch.setattr(prices, "is_alertable", lambda ident: True)
    key = rule_eval._state_key("ASML", 0, "absolute")
    state = {key: {"fired": True, "fired_at": "2026-01-01", "touch": 1.0}}
    rule = {"kind": "absolute", "condition": "ABOVE", "targetPrice": 100,
            "oneShot": True}
    env = asyncio.run(rule_eval._evaluate_rule("ASML", 0, rule, state, {}))
    assert env["status"] == "skipped_closed"
    assert state[key]["fired"] is True
    assert state[key]["touch"] > time.time() - 5


def test_benchmark_symbols_are_never_evaluated(monkeypatch):
    monkeypatch.setattr(prices, "is_alertable", lambda ident: False)
    state = {}
    rule = {"kind": "absolute", "condition": "ABOVE", "targetPrice": 100}
    env = asyncio.run(rule_eval._evaluate_rule("GSPC", 0, rule, state, {}))
    assert env["status"] == "skipped_closed" and state == {}


# ---------------------------------------------------------------- absolute

@pytest.fixture
def quote(monkeypatch):
    holder = {"q": None}

    async def listing_quote(ident):
        return holder["q"]

    monkeypatch.setattr(prices, "listing_quote", listing_quote)
    return holder


RULE = {"kind": "absolute", "condition": "ABOVE", "targetPrice": 100.0}


def test_absolute_baselines_then_triggers_on_the_listing_quote(quote):
    st = {}
    quote["q"] = {"price": 95.0, "asOf": 1_700_000_000, "stale": False}
    env = asyncio.run(rule_eval._eval_absolute("ASML", RULE, st, {}))
    assert env["status"] == "not_triggered" and st["side"] == "below"

    quote["q"] = {"price": 101.0, "asOf": 1_700_000_060, "stale": False}
    env = asyncio.run(rule_eval._eval_absolute("ASML", RULE, st, {}))
    assert env["status"] == "triggered"
    assert env["data_timestamp"]                   # taken from asOf


def test_absolute_degrades_on_stale_or_missing_quote(quote):
    st = {"side": "below", "level": 100.0}
    quote["q"] = {"price": 101.0, "asOf": 1, "stale": True}
    env = asyncio.run(rule_eval._eval_absolute("ASML", RULE, st, {}))
    assert env["status"] == "degraded" and st["side"] == "below"
    quote["q"] = None
    env = asyncio.run(rule_eval._eval_absolute("ASML", RULE, st, {}))
    assert env["status"] == "degraded"


# ---------------------------------------------------------------- delivery

def test_failed_delivery_rolls_state_back_but_filtered_is_consumed(monkeypatch):
    sent = []

    async def alert(ticker, source, title, body, **kw):
        sent.append(title)
        return outcome["d"]

    outcome = {"d": notify.Delivery(notify.FAILED)}
    monkeypatch.setattr(rule_eval.notify, "alert", alert)

    async def evaluator(ticker, rule, st, ctx):
        st["side"] = "above"
        return rule_eval._env("triggered", kind="absolute", observed=101,
                              threshold="ABOVE 100", data_timestamp="t",
                              detail={"price": 101.0, "target": 100.0,
                                      "condition": "ABOVE"})

    monkeypatch.setitem(rule_eval._EVALUATORS, "absolute", evaluator)
    rule = {"kind": "absolute", "condition": "ABOVE", "targetPrice": 100.0,
            "cooldownMin": 60}

    st = {"side": "below"}
    env = asyncio.run(rule_eval._guard_and_eval("ASML", 0, "absolute", rule,
                                                st, {}))
    assert st == {"side": "below"} and "rolled back" in env["reason"]

    outcome["d"] = notify.Delivery(notify.FILTERED)
    st = {"side": "below"}
    asyncio.run(rule_eval._guard_and_eval("ASML", 0, "absolute", rule, st, {}))
    assert st["side"] == "above" and st["last_alert_ts"] > 0
