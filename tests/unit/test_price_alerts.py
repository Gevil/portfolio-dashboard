"""price_alerts: EWMA sigma, the sigma-scaled session rule with its escalation
ladder and re-arm, the opening-window gap rule, the pct fallback mode, state
saved only for accepted pushes; plus the rules v3 schema (v2 read as v3, POST
validation, sigma in load_public)."""
import asyncio
import datetime
import math
import re

import pytest

from app.api import jsonstore, notify, price_alerts, prices, registry
from app.api import rules as rules_mod

NOW = 1_800_000_000.0
SESSION = datetime.date(2026, 10, 14)
SESSION_ISO = SESSION.isoformat()
UTC = datetime.timezone.utc

PCT_RULE = {"kind": "pct-move", "mode": "pct", "thresholdPct": 3.0,
            "hotPct": 5.0, "zAlert": 2.0, "zHot": 3.0, "rearmZ": 1.0,
            "minPct": 1.0, "gapWindowMin": 30, "direction": "both",
            "cooldownMin": 120}
SIGMA_RULE = {**PCT_RULE, "mode": "sigma"}


def closes_with_sigma(sigma_pct, n=60):
    """n daily closes whose log returns all have magnitude sigma_pct: the EWMA
    of a constant |r| is exactly |r|, whatever lambda is. A wild bar for the
    current session is appended (it must be excluded)."""
    r = sigma_pct / 100.0
    c, rows = 100.0, []
    for i in range(n):
        day = SESSION - datetime.timedelta(days=n - i)
        rows.append((day.isoformat(), c))
        c *= math.exp(r if i % 2 == 0 else -r)
    rows.append((SESSION_ISO, 5000.0))
    return rows


@pytest.fixture
def pa(tmp_path, monkeypatch):
    monkeypatch.setattr(price_alerts, "STATE_FILE", tmp_path / "state.json")
    price_alerts._suspect.clear()
    h = {"quote": {"price": 100.0, "prevClose": 100.0, "currency": "EUR",
                   "stale": False, "marketOpen": True},
         "open": (100.0, NOW - 3600), "rule": dict(SIGMA_RULE), "pushes": [],
         "delivery": notify.Delivery(notify.SENT),
         "closes": closes_with_sigma(1.0), "history_calls": [],
         "open_ts": NOW - 7200}          # outside the 30 min gap window

    async def listing_quote(ident):
        return h["quote"]

    async def session_open_price(ident):
        return h["open"]

    async def daily_closes(ticker, period="2y"):
        h["history_calls"].append((ticker, period))
        return h["closes"]

    async def alert(ticker, source, title, body, **kw):
        h["pushes"].append({"ticker": ticker, "source": source,
                            "title": title, "body": body, **kw})
        return h["delivery"]

    def session_bounds(mic, day):
        opened = datetime.datetime.fromtimestamp(h["open_ts"], tz=UTC)
        return opened, opened + datetime.timedelta(hours=8)

    monkeypatch.setattr(prices, "listing_quote", listing_quote)
    monkeypatch.setattr(prices, "session_open_price", session_open_price)
    monkeypatch.setattr(prices, "daily_closes", daily_closes)
    monkeypatch.setattr(price_alerts.calendars, "mic_for", lambda ident: "XAMS")
    monkeypatch.setattr(price_alerts.calendars, "quote_session",
                        lambda mic, now=None: SESSION)
    monkeypatch.setattr(price_alerts.calendars, "session_bounds",
                        session_bounds)
    monkeypatch.setattr(registry, "session_window",
                        lambda ident, now=None: (int(NOW - 3600), int(NOW + 3600)))
    monkeypatch.setattr(price_alerts.rules_mod, "pct_move",
                        lambda sym: h["rule"])
    monkeypatch.setattr(price_alerts.notify, "alert", alert)
    return h


def check(state, now=NOW):
    return asyncio.run(price_alerts.check_ticker("ASML", state, now))


# ------------------------------------------------------------------- sigma

def test_ewma_sigma_known_values():
    # returns +1%, -2%: var0 = 1e-4; var1 = .94 * 1e-4 + .06 * 4e-4 = 1.18e-4
    closes = [100.0, 100.0 * math.exp(0.01), 100.0 * math.exp(-0.01)]
    assert price_alerts.ewma_sigma(closes, min_returns=2) == \
        pytest.approx(math.sqrt(1.18e-4), rel=1e-9)
    # returns +2%, 0, -1%: var = .94 * (.94 * 4e-4) + .06 * 1e-4 = 3.5944e-4
    closes = [100.0, 100.0 * math.exp(0.02), 100.0 * math.exp(0.02),
              100.0 * math.exp(0.01)]
    assert price_alerts.ewma_sigma(closes, min_returns=3) == \
        pytest.approx(math.sqrt(3.5944e-4), rel=1e-9)


def test_ewma_sigma_of_constant_magnitude_returns_and_degenerate_inputs():
    rows = closes_with_sigma(2.5)
    sigma = price_alerts.ewma_sigma([c for _, c in rows[:-1]])
    assert sigma == pytest.approx(0.025, rel=1e-9)
    assert price_alerts.ewma_sigma([100.0] * 40) is None          # flat
    assert price_alerts.ewma_sigma([100.0, 101.0, 100.0]) is None  # too short


def test_sigma_for_excludes_the_current_session_bar(pa):
    sigma, as_of = asyncio.run(price_alerts.sigma_for("ASML", NOW))
    assert sigma == pytest.approx(1.0, rel=1e-9)        # the 5000.0 bar is out
    assert as_of == (SESSION - datetime.timedelta(days=1)).isoformat()
    assert pa["history_calls"] == [("ASML", "1y")]


# ------------------------------------------------------ sigma session move

def test_session_move_alerts_at_z_with_body_naming_z_and_sigma(pa):
    pa["quote"].update(price=102.5, prevClose=102.5)    # +2.5% = z 2.5
    state = {}
    assert check(state) == "alerted"
    p = pa["pushes"][0]
    assert p["ticker"] == "ASML" and p["source"] == "price"
    assert p["priority"] == 3 and p["severity"] == "warning"
    assert "ASML +2.5% vs session open 100.00 (z +2.5, \u03c3 1.0%)" in p["body"]
    assert state["ASML#z"] == {"session": SESSION_ISO, "dir": 1, "level": 2,
                               "hot": False, "ts": NOW}
    assert "ASML#move" not in state


def test_below_z_alert_or_below_min_pct_stays_quiet(pa):
    pa["quote"].update(price=101.5, prevClose=101.5)    # z 1.5 < zAlert 2
    assert check({}) == "quiet" and pa["pushes"] == []
    pa["closes"] = closes_with_sigma(0.3)               # tiny sigma: z 3, 0.9%
    pa["quote"].update(price=100.9, prevClose=100.9)
    assert check({}) == "quiet" and pa["pushes"] == []  # below minPct 1.0
    pa["quote"].update(price=101.1, prevClose=101.1)    # 1.1% >= minPct
    assert check({}) == "alerted" and pa["pushes"][0]["priority"] == 5


def test_sigma_mode_respects_direction(pa):
    pa["rule"]["direction"] = "down"
    pa["quote"].update(price=102.5, prevClose=102.5)
    assert check({}) == "quiet"
    pa["quote"].update(price=97.5, prevClose=97.5)
    assert check({}) == "alerted"


def test_ladder_realerts_only_at_a_higher_level_or_on_reaching_hot(pa):
    state = {}
    steps = [(102.4, "alerted", 3),     # z 2.4: level 2
             (102.8, "quiet", None),    # still level 2
             (103.2, "alerted", 5),     # level 3 and hot
             (103.5, "quiet", None),    # level 3, hot already
             (104.1, "alerted", 5)]     # level 4
    for i, (price, want, prio) in enumerate(steps):
        pa["quote"].update(price=price, prevClose=price)
        assert check(state, NOW + 60 * i) == want, price
        if prio:
            assert pa["pushes"][-1]["priority"] == prio
    assert len(pa["pushes"]) == 3
    assert state["ASML#z"]["level"] == 4 and state["ASML#z"]["hot"] is True


def test_hot_reached_within_the_same_integer_level_escalates(pa):
    pa["rule"].update(zAlert=2.0, zHot=2.5)
    state = {}
    pa["quote"].update(price=102.2, prevClose=102.2)    # z 2.2: level 2
    assert check(state) == "alerted"
    pa["quote"].update(price=102.6, prevClose=102.6)    # z 2.6: still level 2
    assert check(state, NOW + 60) == "alerted"          # ... but now hot
    assert pa["pushes"][-1]["priority"] == 5


def test_rearm_below_rearmz_persists_and_respects_cooldown_same_direction(pa):
    state = {}
    pa["quote"].update(price=102.4, prevClose=102.4)
    assert check(state, NOW) == "alerted"

    pa["quote"].update(price=101.5, prevClose=101.5)    # z 1.5: not re-armed
    assert check(state, NOW + 60) == "quiet"
    assert state["ASML#z"]["level"] == 2
    pa["quote"].update(price=102.4, prevClose=102.4)
    assert check(state, NOW + 120) == "quiet"           # armed at level 2

    pa["quote"].update(price=100.5, prevClose=100.5)    # z 0.5 < rearmZ 1
    assert check(state, NOW + 180) == "quiet"
    assert price_alerts._load_state()["ASML#z"]["level"] == 0   # persisted

    pa["quote"].update(price=102.4, prevClose=102.4)
    assert check(state, NOW + 240) == "quiet"           # cooldown 120 min
    assert len(pa["pushes"]) == 1
    assert check(state, NOW + 121 * 60) == "alerted"


def test_opposite_direction_after_rearm_alerts_without_cooldown(pa):
    state = {}
    pa["quote"].update(price=102.4, prevClose=102.4)
    assert check(state, NOW) == "alerted"
    pa["quote"].update(price=100.5, prevClose=100.5)
    check(state, NOW + 60)                              # re-armed
    pa["quote"].update(price=97.6, prevClose=97.6)      # z -2.4
    assert check(state, NOW + 120) == "alerted"
    assert state["ASML#z"]["dir"] == -1


def test_next_session_rearms_a_hot_ladder(pa):
    state = {"ASML#z": {"session": "2026-10-13", "dir": 1, "level": 3,
                        "hot": True, "ts": NOW - 60}}
    pa["quote"].update(price=102.2, prevClose=102.2)    # level 2 < 3 yesterday
    assert check(state) == "alerted"
    assert state["ASML#z"]["session"] == SESSION_ISO
    assert state["ASML#z"]["level"] == 2


def test_failed_delivery_keeps_the_ladder_and_retries_next_pass(pa):
    pa["quote"].update(price=102.5, prevClose=102.5)
    pa["delivery"] = notify.Delivery(notify.FAILED)
    state = {}
    assert check(state) == "skipped" and state == {}
    assert not price_alerts.STATE_FILE.exists()

    pa["delivery"] = notify.Delivery(notify.SENT)
    assert check(state, NOW + 60) == "alerted" and "ASML#z" in state
    assert price_alerts.STATE_FILE.exists()


@pytest.mark.parametrize("status", [notify.QUEUED, notify.FILTERED,
                                    notify.DUPLICATE])
def test_every_non_failed_delivery_consumes_the_ladder(pa, status):
    pa["quote"].update(price=102.5, prevClose=102.5)
    pa["delivery"] = notify.Delivery(status)
    state = {}
    assert check(state) == "alerted" and state["ASML#z"]["level"] == 2


# ---------------------------------------------------------------- gap rule

def gap_setup(pa, price=95.0, prev=100.0, age_s=600):
    pa["quote"].update(price=price, prevClose=prev)
    pa["open"] = (price, NOW - 3600)       # opened at the gapped level
    pa["open_ts"] = NOW - age_s


def test_gap_alerts_inside_the_window_once_per_session(pa):
    gap_setup(pa)
    state = {}
    assert check(state) == "alerted"
    p = pa["pushes"][0]
    assert "-5.0% gap vs prev close" in p["title"]
    assert "ASML -5.0% vs prev close 100.00 (z -5.0, \u03c3 1.0%)" in p["body"]
    assert p["priority"] == 5 and p["severity"] == "error"
    assert state == {"ASML#gap_session": SESSION_ISO}

    pa["quote"].update(price=90.0)                      # worse, same session
    pa["open"] = (90.0, NOW - 3600)
    assert check(state, NOW + 60) == "quiet" and len(pa["pushes"]) == 1


@pytest.mark.parametrize("age_s,fires", [
    (0, True), (600, True), (1800, True),       # [open, open + 30 min]
    (1801, False), (7200, False),               # window closed
    (-60, False),                               # not open yet
])
def test_gap_window_is_gapwindowmin_after_the_open(pa, age_s, fires):
    gap_setup(pa, age_s=age_s)
    assert check({}) == ("alerted" if fires else "quiet")
    assert len(pa["pushes"]) == (1 if fires else 0)


def test_gap_window_follows_the_rule_gapwindowmin(pa):
    pa["rule"]["gapWindowMin"] = 10
    gap_setup(pa, age_s=11 * 60)
    assert check({}) == "quiet"
    gap_setup(pa, age_s=10 * 60)
    assert check({}) == "alerted"


def test_gap_is_skipped_when_prev_close_is_unknown(pa):
    gap_setup(pa, price=95.0, prev=None)
    assert check({}) == "quiet" and pa["pushes"] == []
    pa["quote"].pop("prevClose")
    assert check({}) == "quiet"


def test_failed_gap_delivery_does_not_use_up_the_session(pa):
    gap_setup(pa)
    pa["delivery"] = notify.Delivery(notify.FAILED)
    state = {}
    assert check(state) == "skipped" and state == {}
    pa["delivery"] = notify.Delivery(notify.SENT)
    assert check(state, NOW + 60) == "alerted"
    assert state["ASML#gap_session"] == SESSION_ISO


def test_gap_beyond_the_sanity_band_needs_a_second_reading(pa):
    gap_setup(pa, price=70.0)                           # -30%: bad print?
    state = {}
    assert check(state, NOW) == "quiet" and pa["pushes"] == []
    assert check(state, NOW + 60) == "alerted"          # confirmed


def test_gap_and_session_move_in_one_pass_go_out_as_one_push(pa):
    pa["quote"].update(price=106.0, prevClose=100.0)    # +6% gap, z 6
    pa["open"] = (103.0, NOW - 3600)                    # +2.9% session, z 2.9
    pa["open_ts"] = NOW - 600
    state = {}
    assert check(state) == "alerted"
    assert len(pa["pushes"]) == 1
    assert set(state) == {"ASML#z", "ASML#gap_session"}
    assert "gap vs prev close" in pa["pushes"][0]["title"]
    assert "in session" in pa["pushes"][0]["title"]


# ------------------------------------------------- pct mode and the fallback

def test_pct_mode_uses_fixed_thresholds_and_never_loads_history(pa):
    pa["rule"] = dict(PCT_RULE)
    pa["quote"].update(price=103.5, prevClose=103.5)    # +3.5% in session
    state = {}
    assert check(state) == "alerted"
    assert "in session" in pa["pushes"][0]["title"]
    assert "Threshold 3.0%." in pa["pushes"][0]["body"]
    assert set(state) == {"ASML#move"}
    assert pa["history_calls"] == []


def test_pct_mode_gap_uses_hot_threshold_inside_the_window(pa):
    pa["rule"] = dict(PCT_RULE)
    gap_setup(pa, price=94.0)                           # -6% >= hotPct
    assert check({}) == "alerted"
    p = pa["pushes"][0]
    assert "-6.0% gap vs prev close" in p["title"]
    assert p["priority"] == 5 and p["severity"] == "error"


def test_pct_mode_cooldown_blocks_repeat(pa):
    pa["rule"] = dict(PCT_RULE)
    pa["quote"].update(price=103.5, prevClose=103.5)
    state = {"ASML#move": NOW - 60}                     # fired a minute ago
    assert check(state) == "quiet" and pa["pushes"] == []
    assert check(state, now=NOW + 121 * 60) == "alerted"


def test_sigma_mode_without_history_falls_back_to_pct_thresholds(pa):
    pa["closes"] = []
    pa["quote"].update(price=103.5, prevClose=103.5)
    state = {}
    assert check(state) == "alerted"
    assert set(state) == {"ASML#move"}
    assert "Threshold 3.0%." in pa["pushes"][0]["body"]


# ----------------------------------------------------------- shared guards

def test_stale_closed_or_silenced_tickers_are_skipped(pa):
    pa["quote"].update(price=90.0, stale=True)
    assert check({}) == "skipped"
    pa["quote"].update(stale=False, marketOpen=False)
    assert check({}) == "skipped"
    pa["quote"].update(marketOpen=True)
    pa["rule"] = None                                   # pct-move rule removed
    assert check({}) == "skipped" and pa["pushes"] == []


def test_late_first_bar_is_not_a_session_anchor(pa):
    pa["open"] = (100.0, NOW - 60)                      # feed joined mid-session
    pa["quote"].update(price=104.0, prevClose=104.0)
    assert check({}) == "quiet"


def test_move_beyond_sanity_band_needs_a_second_reading(pa):
    pa["quote"].update(price=70.0, prevClose=70.0)      # -30% vs the open
    state = {}
    assert check(state, NOW) == "quiet" and pa["pushes"] == []
    assert check(state, NOW + 60) == "alerted"


def test_load_state_keeps_legacy_floats_and_drops_malformed_entries(pa):
    jsonstore.save(price_alerts.STATE_FILE, {
        "A#move": 5.0, "A#gap": 7, "A#z": {"session": "x", "level": 1},
        "A#gap_session": "2026-10-14", "B#z": "bad", "B#gap_session": 3,
        "C#move": "nope"})
    assert price_alerts._load_state() == {
        "A#move": 5.0, "A#gap": 7.0, "A#z": {"session": "x", "level": 1},
        "A#gap_session": "2026-10-14"}


# ------------------------------------------------------------------ passes

def test_pass_isolates_a_failing_ticker(pa, monkeypatch):
    monkeypatch.setattr(registry, "entries", lambda: [
        {"id": "BAD", "alertable": True}, {"id": "ASML", "alertable": True},
        {"id": "^GSPC", "alertable": False}])
    monkeypatch.setattr(rules_mod, "_sigma", {})
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


def test_pass_publishes_sigma_for_every_alertable_id(pa, monkeypatch):
    monkeypatch.setattr(registry, "entries", lambda: [
        {"id": "ASML", "alertable": True}, {"id": "BAD", "alertable": True},
        {"id": "^GSPC", "alertable": False}])
    monkeypatch.setattr(rules_mod, "_sigma", {})
    monkeypatch.setattr(rules_mod.config_store, "read", lambda: {})
    real = prices.daily_closes

    async def daily_closes(ticker, period="2y"):
        if ticker == "BAD":
            raise RuntimeError("history exploded")
        return await real(ticker, period)

    async def quiet(sym, state, now=None):
        return "quiet"

    monkeypatch.setattr(prices, "daily_closes", daily_closes)
    monkeypatch.setattr(price_alerts, "check_ticker", quiet)
    out = asyncio.run(price_alerts.check_price_alerts())
    assert out["errors"] == 0
    sigma = rules_mod.load_public()["sigma"]
    assert list(sigma) == ["ASML"]
    assert sigma["ASML"]["sigmaPct"] == pytest.approx(1.0, abs=1e-3)
    assert sigma["ASML"]["asOf"] == "2026-10-13"


# --------------------------------------------------------------- rules v3

@pytest.fixture
def cfg(monkeypatch):
    box = {}
    monkeypatch.setattr(rules_mod.config_store, "read", lambda: box)
    monkeypatch.setattr(rules_mod.config_store, "update",
                        lambda mutator: mutator(box))
    monkeypatch.setattr(rules_mod, "_sigma", {})
    for var in ("ALERT_THRESHOLD_PCT", "ALERT_HOT_THRESHOLD_PCT",
                "PRICE_ALERT_COOLDOWN_MIN"):
        monkeypatch.delenv(var, raising=False)
    return box


def test_rules_defaults_are_the_v3_contract(cfg):
    pub = rules_mod.load_public()
    assert pub["version"] == 3 and pub["sigma"] == {}
    assert pub["default"] == {
        "mode": "sigma", "zAlert": 2.0, "zHot": 3.0, "rearmZ": 1.0,
        "minPct": 1.0, "gapWindowMin": 30, "cooldownMin": 120,
        "direction": "both", "thresholdPct": 3.0, "hotPct": 5.0}


def test_v2_block_reads_as_v3_sigma_without_being_rewritten(cfg):
    v2 = {"version": 2,
          "default": {"thresholdPct": 4.0, "hotPct": 6.0,
                      "direction": "down", "cooldownMin": 90},
          "perTicker": {"ASML": [{"kind": "pct-move", "thresholdPct": 2.5}]}}
    cfg["alertRules"] = v2
    rule = rules_mod.pct_move("ASML")
    assert rule["mode"] == "sigma"
    assert (rule["zAlert"], rule["zHot"], rule["rearmZ"], rule["minPct"],
            rule["gapWindowMin"]) == (2.0, 3.0, 1.0, 1.0, 30)
    assert (rule["thresholdPct"], rule["hotPct"], rule["direction"],
            rule["cooldownMin"]) == (2.5, 6.0, "down", 90)
    assert rules_mod.load_public()["version"] == 3
    assert cfg["alertRules"] is v2 and v2["version"] == 2    # untouched


@pytest.mark.parametrize("block,fragment", [
    ({"default": {"zAlert": 0.1}}, "zAlert must be between"),
    ({"default": {"gapWindowMin": 0}}, "gapWindowMin must be between"),
    ({"default": {"mode": "fancy"}}, "mode must be one of"),
    ({"default": {"zAlert": 3.0, "zHot": 2.0}}, "zHot must not be below"),
    ({"default": {"rearmZ": 2.0}}, "rearmZ must be below zAlert"),
    ({"perTicker": {"ASML": [{"kind": "pct-move", "zAlert": 4.0}]}},
     "ASML (pct-move): zHot must not be below zAlert"),
])
def test_save_rejects_invalid_v3_blocks(cfg, block, fragment):
    with pytest.raises(rules_mod.RuleError, match=re.escape(fragment)):
        rules_mod.save(block)
    assert cfg == {}                                    # nothing written


def test_save_writes_v3_and_load_public_carries_sigma(cfg):
    rules_mod.set_sigma({"ASML": {"sigmaPct": 2.4, "asOf": "2026-10-13"}})
    pub = rules_mod.save({
        "default": {"mode": "sigma", "zAlert": 2.5, "zHot": 4.0,
                    "gapWindowMin": 15},
        "perTicker": {"asml": [{"kind": "pct-move", "mode": "pct",
                                "thresholdPct": 2.0, "direction": "down"}]},
        "sigma": {"X": {"sigmaPct": 99}}})              # read-only: ignored
    stored = cfg["alertRules"]
    assert stored["version"] == 3 and "sigma" not in stored
    assert stored["default"] == {"zAlert": 2.5, "zHot": 4.0,
                                 "gapWindowMin": 15, "mode": "sigma"}
    assert stored["perTicker"]["ASML"] == [
        {"kind": "pct-move", "mode": "pct", "thresholdPct": 2.0,
         "direction": "down"}]
    assert pub["sigma"] == {"ASML": {"sigmaPct": 2.4, "asOf": "2026-10-13"}}
    assert pub["default"]["zAlert"] == 2.5 and pub["default"]["rearmZ"] == 1.0
