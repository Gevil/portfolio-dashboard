"""digest: rating precedence, change detection, the delivery-gated state, per
ticker outcomes with one retry, ONE consolidated push, and resume on start.

Everything the batch touches is faked at digest's own seams (``run_one``,
``parse_report``, ``_price_context``, ``lane_gate``, ``notify``); state files
live in tmp_path. No network, no sleeps (retry_delay_s is 0).
"""
import asyncio
import datetime
import json

import pytest

from app.api import digest, notify


# -------------------------------------------------------------------- rating

@pytest.mark.parametrize("text,expected", [
    # the plan's reproduced counter-example: 'would not buy' must not be a BUY
    ("Rating: Hold. I would not buy here; wait for 1400.", "HOLD"),
    # markdown around the label ('*' used to hide the explicit line)
    ("**Rating**: Sell\nAvoid the stock; do not buy the dip.", "SELL"),
    # the explicit Rating line wins over a later keyword
    ("Rating: Hold\nA buy at 1300 would be fine, but today the call is to wait.", "HOLD"),
    ("Final Rating - BUY", "BUY"),
    ("Recommendation: Underweight", "SELL"),
    ("Rating: Overweight", "BUY"),
    ("Rating: Neutral", "HOLD"),
    # no explicit line: SELL/HOLD are checked before BUY, negations are removed
    ("We hold and would sell into strength, not buy.", "SELL"),
    ("We would not buy this.", "OTHER"),
    ("Nothing conclusive.", "OTHER"),
    ("", "OTHER"),
    (None, "OTHER"),
])
def test_norm_rating(text, expected):
    assert digest.norm_rating(text) == expected


def report(**over):
    d = {"final_trade_decision": "**Rating**: Hold\n\nNarrative.",
         "lane": "ninfer-nvfp4", "model": "qwen3.8-27b",
         "price_at_analysis": {"price": 100.0, "currency": "EUR", "as_of": 1790000000},
         "decision_v2": {"decision_type": "buy", "score": 66, "action": "buy",
                         "confidence": "medium", "scale_tier": "buy",
                         "scale_version": "ds-v1",
                         "core_conclusion": {"one_sentence": "Trend intact, see https://x.example/a."},
                         "battle_plan": {"stop_loss": 90, "take_profit": 120},
                         "data_quality": {"grade": "partial", "missing": ["news"]},
                         "evidence_gaps": ["news", "MISSING"]}}
    d.update(over)
    return d


def test_decision_v2_wins_over_contradicting_text():
    p = digest.parse_decision(report(), 240)
    assert p["rating"] == "BUY" and p["rating_source"] == "decision_v2"
    assert p["score"] == 66 and p["action"] == "buy" and p["stop"] == 90.0
    assert p["target"] == 120.0 and p["missing"] == ["news"]
    assert p["gaps"] == ["news"]                       # the bare MISSING marker is not a gap
    assert "http" not in p["excerpt"] and "x.example" not in p["excerpt"]
    assert p["lane"] == "ninfer-nvfp4" and p["report_price"] == 100.0


def test_invalid_decision_type_falls_back_to_text_never_other_silently():
    bad = report(decision_v2={"decision_type": "maybe"})
    p = digest.parse_decision(bad, 240)
    assert p["rating"] == "HOLD" and p["rating_source"] == "text"
    unreadable = report(final_trade_decision="nothing", decision_v2=None)
    assert digest.parse_decision(unreadable, 240)["rating"] == "OTHER"


def test_legacy_report_without_decision_v2():
    p = digest.parse_decision({"final_trade_decision": "Rating: SELL\nExit."}, 240)
    assert p["rating"] == "SELL" and p["score"] is None and p["action"] is None


# ------------------------------------------------------------ change detection

def row(**kw):
    base = {"rating": "HOLD", "action": "hold", "scale_tier": "watch", "score": 50,
            "confidence": "medium", "excerpt": "same"}
    base.update(kw)
    return base


def test_first_advice_and_no_change():
    assert digest.detect_change(None, row())["kind"] == "first"
    assert digest.detect_change(row(), row())["kind"] == "none"


def test_excerpt_text_never_triggers_a_change():
    ch = digest.detect_change(row(excerpt="old words"), row(excerpt="entirely new words"))
    assert ch["kind"] == "none"


def test_rating_action_and_band_changes():
    assert digest.detect_change(row(), row(rating="BUY"))["kind"] == "rating"
    assert digest.detect_change(row(), row(action="reduce"))["kind"] == "action"
    ch = digest.detect_change(row(), row(scale_tier="reduce"))
    assert ch["kind"] == "band" and "band watch" in ch["parts"][0]


def test_score_drift_inside_a_band_is_context_not_a_change():
    ch = digest.detect_change(row(score=50), row(score=55))
    assert ch["kind"] == "none" and any("score 50" in p for p in ch["parts"])


def test_pre_v2_state_row_compares_on_rating_only():
    old = {"rating": "HOLD", "excerpt": "x"}                    # no action/band/score
    assert digest.detect_change(old, row())["kind"] == "none"
    assert digest.detect_change(old, row(rating="SELL"))["kind"] == "rating"


# ------------------------------------------------------------------ the batch

def delivered(status="sent"):
    return notify.Delivery(status)


class Harness:
    """Fakes for one run_batch: per-ticker scripted outcomes."""

    def __init__(self, monkeypatch, tmp_path):
        self.pushes, self.stores, self.attempts = [], [], {}
        self.script = {}             # ticker -> list of results (consumed per attempt)
        self.push_status = "sent"
        monkeypatch.setattr(digest, "STATE_FILE", tmp_path / "advice_state.json")
        monkeypatch.setattr(digest, "ADVICE_LOG", tmp_path / "advice_log.json")
        monkeypatch.setattr(digest, "cfg", lambda: {**digest.DEFAULTS, "retry_delay_s": 0})
        monkeypatch.setattr(digest.prices, "is_analyzable", lambda t: True)
        monkeypatch.setattr(digest, "lane_gate", self.gate)
        self.gate_result = (True, "")
        monkeypatch.setattr(digest, "run_one", self.run_one)
        monkeypatch.setattr(digest, "parse_report", self.parse_report)
        monkeypatch.setattr(digest, "_price_context", self.price_context)
        monkeypatch.setattr(digest.notify, "push", self.push)
        monkeypatch.setattr(digest.notify, "store", lambda *a, **k: self.stores.append(a))

    async def gate(self):
        return self.gate_result

    async def run_one(self, ticker, c):
        self.attempts[ticker] = self.attempts.get(ticker, 0) + 1
        step = self.script[ticker].pop(0)
        if step != "ok":                                # anything else = failure reason
            return None, step
        return {"result_path": f"{ticker}@2026-10-03", "lane": "L", "model": "M"}, ""

    def parse_report(self, report_id, n):
        t = report_id.split("@")[0]
        return {"rating": self.ratings.get(t, "BUY"), "rating_source": "decision_v2",
                "excerpt": "e", "action": "buy", "score": 70, "confidence": "high",
                "scale_tier": "buy", "scale_version": "ds-v1", "data_quality": "full",
                "missing": [], "gaps": [], "stop": 90.0, "target": 120.0,
                "lane": None, "model": None}

    ratings: dict = {}

    async def price_context(self, ticker, parsed):
        return ({"price": 100.0, "day_pct": 1.0, "as_of": 1790000000, "stale": False},
                {"price": 100.0, "as_of": 1790000000, "currency": "EUR"}, "market_open")

    async def push(self, title, body, **kw):
        self.pushes.append((title, body, kw))
        return delivered(self.push_status)


@pytest.fixture
def h(monkeypatch, tmp_path):
    out = Harness(monkeypatch, tmp_path)
    out.ratings = {}
    return out


def state(tmp_path):
    p = tmp_path / "advice_state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_clean_batch_sends_one_push_and_advances_state(h, tmp_path):
    h.script = {"ASML": ["ok"], "NVDA": ["ok"]}
    res = asyncio.run(digest.run_batch(["ASML", "NVDA"]))
    assert res["ok"] is True and res["pushed"] == "sent"
    assert len(h.pushes) == 1                              # ONE consolidated push
    assert "ASML" in h.pushes[0][1] and "NVDA" in h.pushes[0][1]
    assert set(state(tmp_path)) == {"ASML", "NVDA"}
    log = json.loads((tmp_path / "advice_log.json").read_text())
    assert [r["ticker"] for r in log] == ["ASML", "NVDA"] and log[0]["rating"] == "BUY"
    assert len(h.stores) == 2


def test_undelivered_push_leaves_state_untouched_so_the_change_is_re_reported(h, tmp_path):
    h.script = {"ASML": ["ok"]}
    h.push_status = "failed"
    res = asyncio.run(digest.run_batch(["ASML"]))
    assert res["ok"] is False and res["pushed"] == "failed"
    assert state(tmp_path) == {} and not (tmp_path / "advice_log.json").exists()
    assert h.stores == []
    # next run: same change is first-advice again, now delivered
    h.push_status, h.script = "queued", {"ASML": ["ok"]}      # queued counts as accepted
    res = asyncio.run(digest.run_batch(["ASML"]))
    assert res["ok"] is True and "ASML" in state(tmp_path)
    assert "first tracked advice" in h.pushes[-1][1] or "first" in h.pushes[-1][0].lower()


@pytest.mark.parametrize("status", ["sent", "queued", "filtered", "duplicate"])
def test_every_non_failed_delivery_consumes_state(h, tmp_path, status):
    h.script = {"ASML": ["ok"]}
    h.push_status = status
    asyncio.run(digest.run_batch(["ASML"]))
    assert "ASML" in state(tmp_path)


def test_failed_ticker_is_retried_once_then_reported_in_one_incomplete_push(h, tmp_path):
    h.script = {"ASML": ["ok"], "NVDA": ["lane blip", "lane blip again"]}
    res = asyncio.run(digest.run_batch(["ASML", "NVDA"]))
    assert h.attempts == {"ASML": 1, "NVDA": 2}             # exactly one retry
    assert res["ok"] is False and res["outcomes"]["NVDA"]["status"] == "failed"
    assert res["outcomes"]["NVDA"]["retried"] is True
    assert len(h.pushes) == 1 and "digest incomplete" in h.pushes[0][0]
    assert "NVDA" in h.pushes[0][0] and "lane blip again" in h.pushes[0][1]
    assert "ASML" in h.pushes[0][1]                          # the good ticker is in it too
    assert set(state(tmp_path)) == {"ASML"}                  # only the analysed one advances


def test_retry_that_succeeds_makes_a_clean_batch(h):
    h.script = {"ASML": ["flaky", "ok"]}
    res = asyncio.run(digest.run_batch(["ASML"]))
    assert res["ok"] is True and h.attempts["ASML"] == 2
    assert "incomplete" not in h.pushes[0][0]


def test_spent_budget_is_not_retried(h):
    h.script = {"ASML": ["budget: digest allowance spent"]}
    res = asyncio.run(digest.run_batch(["ASML"]))
    assert h.attempts["ASML"] == 1 and res["ok"] is False


def test_unreadable_rating_is_a_failure_never_an_other_push(h, tmp_path):
    h.ratings = {"ASML": "OTHER"}
    h.script = {"ASML": ["ok", "ok"]}
    res = asyncio.run(digest.run_batch(["ASML"]))
    assert res["outcomes"]["ASML"]["status"] == "failed"
    assert "rating unreadable" in res["outcomes"]["ASML"]["reason"]
    assert "OTHER" not in h.pushes[0][1] and state(tmp_path) == {}


def test_closed_gate_defers_without_pushing_or_spending(h):
    h.gate_result = (False, "no lane is serving")
    h.script = {"ASML": ["ok"]}
    res = asyncio.run(digest.run_batch(["ASML"]))
    assert res["deferred"] is True and h.pushes == [] and h.attempts == {}


def test_change_between_runs_is_reported_and_unchanged_is_quiet(h, tmp_path):
    h.script = {"ASML": ["ok", "ok", "ok"]}
    asyncio.run(digest.run_batch(["ASML"]))
    h.ratings = {"ASML": "SELL"}
    asyncio.run(digest.run_batch(["ASML"]))
    assert "rating BUY \u2192 SELL" in h.pushes[1][1]
    assert h.pushes[1][2]["priority"] == 5
    asyncio.run(digest.run_batch(["ASML"]))
    assert h.pushes[2][2]["priority"] == 3 and "no change" in h.pushes[2][0]


def test_format_digest_title_names_failures_and_never_carries_a_url():
    outcomes = [{"ticker": "ASML", "status": "ok", "rating": "BUY", "change": {"kind": "none", "parts": ["no change"]},
                 "price": {"price": 1.0, "day_pct": 0.5, "as_of": 1790000000}, "excerpt": "fine"},
                {"ticker": "NVDA", "status": "failed", "reason": "lane_down", "retried": True}]
    msg = digest.format_digest(outcomes, datetime.datetime(2026, 10, 3, 8, 45))
    assert "digest incomplete: NVDA (lane_down)" in msg["title"]
    assert "NOT ANALYSED: lane_down (after retry)" in msg["body"]
    assert msg["priority"] == 4 and len(msg["body"]) <= digest.PUSH_BODY_CHARS


# ----------------------------------------------------------------- resume/start

def test_start_re_enqueues_tickers_a_restart_cut_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DIGEST_ENABLED", "1")
    monkeypatch.setattr(digest, "cfg", lambda: {**digest.DEFAULTS, "resume_delay_s": 0})
    monkeypatch.setattr(digest.jobs, "take_interrupted",
                        lambda source, max_age: [{"ticker": "ASML"}, {"ticker": "NVDA"}])
    seen = []

    async def fake_batch(label, tickers):
        seen.append((label, tickers))

    async def idle():
        await asyncio.sleep(3600)
    monkeypatch.setattr(digest, "_run_with_gate_retries", fake_batch)
    monkeypatch.setattr(digest, "_loop", idle)

    async def go():
        digest.start()
        await digest._resume_task
        await digest.stop()
    asyncio.run(go())
    assert seen == [("resumed", ["ASML", "NVDA"])]


def test_start_without_interrupted_jobs_resumes_nothing(monkeypatch):
    monkeypatch.setenv("DIGEST_ENABLED", "1")
    monkeypatch.setattr(digest.jobs, "take_interrupted", lambda s, a: [])

    async def idle():
        await asyncio.sleep(3600)
    monkeypatch.setattr(digest, "_loop", idle)

    async def go():
        digest.start()
        assert digest._resume_task is None
        await digest.stop()
    asyncio.run(go())
