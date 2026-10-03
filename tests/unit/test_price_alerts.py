"""price_alerts: session-move + gap-vs-previous-close rules, cooldown keys,
state saved only for accepted pushes."""
import asyncio

import pytest

from app.api import notify, price_alerts, prices, registry

NOW = 1_800_000_000.0
RULE = {"kind": "pct-move", "thresholdPct": 3.0, "hotPct": 5.0,
        "direction": "both", "cooldownMin": 120}


@pytest.fixture
def pa(tmp_path, monkeypatch):
    monkeypatch.setattr(price_alerts, "STATE_FILE", tmp_path / "state.json")
    price_alerts._suspect.clear()
    h = {"quote": {"price": 100.0, "prevClose": 100.0, "currency": "EUR",
                   "stale": False, "marketOpen": True},
         "open": (100.0, NOW - 3600), "rule": dict(RULE), "pushes": [],
         "delivery": notify.Delivery(notify.SENT)}

    async def listing_quote(ident):
        return h["quote"]

    async def session_open_price(ident):
        return h["open"]

    async def alert(ticker, source, title, body, **kw):
        h["pushes"].append({"ticker": ticker, "title": title, "body": body,
                            **kw})
        return h["delivery"]

    monkeypatch.setattr(prices, "listing_quote", listing_quote)
    monkeypatch.setattr(prices, "session_open_price", session_open_price)
    monkeypatch.setattr(registry, "session_window",
                        lambda ident, now=None: (int(NOW - 3600), int(NOW + 3600)))
    monkeypatch.setattr(price_alerts.rules_mod, "pct_move",
                        lambda sym: h["rule"])
    monkeypatch.setattr(price_alerts.notify, "alert", alert)
    return h


def check(state, now=NOW):
    return asyncio.run(price_alerts.check_ticker("ASML", state, now))


def test_gap_alert_fires_when_session_move_is_flat(pa):
    # opened -6% below yesterday's close and stayed there: the session move is
    # ~0 (the anchor hides the gap), the gap alert must still fire
    pa["quote"].update(price=94.0, prevClose=100.0)
    pa["open"] = (94.0, NOW - 3600)
    state = {}
    assert check(state) == "alerted"
    assert len(pa["pushes"]) == 1
    p = pa["pushes"][0]
    assert "-6.0% gap vs prev close" in p["title"]
    assert p["priority"] == 5 and p["severity"] == "error"
    assert set(state) == {"ASML#gap"}


def test_session_move_alert_and_own_cooldown_keys(pa):
    pa["quote"].update(price=103.5, prevClose=103.0)    # +0.5% gap only
    pa["open"] = (100.0, NOW - 3600)                    # +3.5% in session
    state = {}
    assert check(state) == "alerted"
    assert set(state) == {"ASML#move"}
    assert "in session" in pa["pushes"][0]["title"]


def test_both_rules_in_one_pass_go_out_as_one_push(pa):
    pa["quote"].update(price=106.0, prevClose=100.0)    # +6% gap
    pa["open"] = (101.0, NOW - 3600)                    # +4.95% session
    state = {}
    assert check(state) == "alerted"
    assert len(pa["pushes"]) == 1
    assert set(state) == {"ASML#move", "ASML#gap"}


def test_cooldown_blocks_repeat_per_key(pa):
    pa["quote"].update(price=94.0, prevClose=100.0)
    pa["open"] = (94.0, NOW - 3600)
    state = {"ASML#gap": NOW - 60}                      # fired a minute ago
    assert check(state) == "quiet" and pa["pushes"] == []
    assert check(state, now=NOW + 121 * 60) == "alerted"


def test_failed_delivery_does_not_consume_cooldown(pa):
    pa["quote"].update(price=94.0, prevClose=100.0)
    pa["open"] = (94.0, NOW - 3600)
    pa["delivery"] = notify.Delivery(notify.FAILED)
    state = {}
    assert check(state) == "skipped" and state == {}
    assert not price_alerts.STATE_FILE.exists()

    pa["delivery"] = notify.Delivery(notify.SENT)       # retried next pass
    assert check(state) == "alerted" and "ASML#gap" in state
    assert price_alerts.STATE_FILE.exists()


def test_stale_closed_or_silenced_tickers_are_skipped(pa):
    pa["quote"].update(price=90.0, stale=True)
    assert check({}) == "skipped"
    pa["quote"].update(stale=False, marketOpen=False)
    assert check({}) == "skipped"
    pa["quote"].update(marketOpen=True)
    pa["rule"] = None                                   # pct-move rule removed
    assert check({}) == "skipped" and pa["pushes"] == []


def test_direction_and_threshold(pa):
    pa["rule"]["direction"] = "down"
    pa["quote"].update(price=106.0, prevClose=100.0)
    pa["open"] = (106.0, NOW - 3600)
    assert check({}) == "quiet"                         # up move, down-only
    pa["quote"].update(price=102.0, prevClose=100.0)
    pa["open"] = (100.0, NOW - 3600)
    pa["rule"]["direction"] = "both"
    assert check({}) == "quiet"                         # below threshold


def test_move_beyond_sanity_band_needs_a_second_reading(pa):
    pa["quote"].update(price=70.0, prevClose=100.0)     # -30%: bad print?
    pa["open"] = (70.0, NOW - 3600)
    state = {}
    assert check(state, NOW) == "quiet" and pa["pushes"] == []
    assert check(state, NOW + 60) == "alerted"          # confirmed


def test_late_first_bar_is_not_a_session_anchor(pa):
    pa["open"] = (100.0, NOW - 60)                      # feed joined mid-session
    pa["quote"].update(price=104.0, prevClose=104.0)
    assert check({}) == "quiet"


def test_pass_isolates_a_failing_ticker(pa, monkeypatch):
    monkeypatch.setattr(registry, "entries", lambda: [
        {"id": "BAD", "alertable": True}, {"id": "ASML", "alertable": True},
        {"id": "^GSPC", "alertable": False}])
    seen = []

    async def check_ticker(sym, state, now=None):
        seen.append(sym)
        if sym == "BAD":
            raise RuntimeError("provider exploded")
        return "quiet"

    monkeypatch.setattr(price_alerts, "check_ticker", check_ticker)
    out = asyncio.run(price_alerts.check_price_alerts())
    assert seen == ["BAD", "ASML"]
    assert out["errors"] == 1 and out["quiet"] == 1
