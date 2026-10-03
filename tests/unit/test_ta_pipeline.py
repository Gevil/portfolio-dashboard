"""ta_pipeline: PM JSON extraction, the code-side decision gate, URL scrubbing,
and the report writer -> reports reader round trip (tmp dirs only)."""
import asyncio
import json

import pytest

from app.api import reports, ta_pipeline as tp


# ------------------------------------------------------------- _extract_json

def test_extract_json_bare_fenced_and_embedded():
    assert tp._extract_json('{"score": 55}') == {"score": 55}
    assert tp._extract_json('```json\n{"score": 55}\n```') == {"score": 55}
    assert tp._extract_json('Here you go:\n{"score": 55, "a": 1}\nDone.') == {
        "score": 55, "a": 1}


def test_extract_json_is_string_aware():
    # a } and a ``` inside a string value must not cut the object short
    text = 'prose {"narrative": "closing } brace and ``` fence", "score": 7}'
    assert tp._extract_json(text) == {
        "narrative": "closing } brace and ``` fence", "score": 7}


def test_extract_json_nested_objects_are_not_the_answer():
    text = '{"score": 40, "battle_plan": {"stop_loss": 1, "take_profit": 2}}'
    out = tp._extract_json(text)
    assert set(out) == {"score", "battle_plan"}


def test_extract_json_prefers_the_richest_top_level_object():
    text = ('Example shape: {"score": 0}\n'
            'Answer: {"score": 61, "action": "buy", "confidence": "high"}')
    assert tp._extract_json(text)["score"] == 61
    # an unparseable '{' earlier in the prose does not hide the answer
    assert tp._extract_json('use {braces} then {"score": 3}') == {"score": 3}


@pytest.mark.parametrize("text", ["", None, "no json here", "[1, 2, 3]",
                                  '{"unterminated": ', "{not json}"])
def test_extract_json_none_when_there_is_no_object(text):
    assert tp._extract_json(text) is None


# --------------------------------------------------------- decision gate

def test_band_and_rating_tier_boundaries():
    assert tp._band(80)[0:3] == ("strong_buy", "buy", "buy")
    assert tp._band(60)[1:] == ("buy", "buy")
    assert tp._band(59)[1:] == ("hold", "hold")
    assert tp._band(40)[1:] == ("hold", "hold")
    assert tp._band(39)[1:] == ("reduce", "sell")
    assert tp._band(20)[1:] == ("reduce", "sell")
    assert tp._band(19)[1:] == ("sell", "sell")
    assert [tp._rating_tier(s) for s in (80, 60, 40, 20, 0)] == [
        "Buy", "Overweight", "Hold", "Underweight", "Sell"]


def test_normalize_attribution_sums_to_100():
    out = tp._normalize_attribution({"technical": 30, "news": 30,
                                     "fundamentals": 30, "market_conditions": 30})
    assert sum(out.values()) == 100 and set(out) == set(tp.ATTRIBUTION_KEYS)
    assert tp._normalize_attribution(None) == {k: 25 for k in tp.ATTRIBUTION_KEYS}
    assert all(v >= 0 for v in tp._normalize_attribution(
        {"technical": -5, "news": 10}).values())


def pm_answer(**over):
    d = {"score": 72, "action": "buy", "confidence": "high",
         "core_conclusion": {"one_sentence": "See https://evil.example/x now",
                             "signal_type": "bullish", "time_sensitivity": "days"},
         "narrative": "Buy it. Details at www.evil.example/pay\nSecond line.",
         "evidence_gaps": ["fundamentals"], "guardrail_reason": None}
    d.update(over)
    return d


def test_finalize_scrubs_urls_and_applies_the_scale():
    out = tp._finalize(pm_answer(), {"missing": ["news"]})
    assert out["decision_type"] == "buy" and out["action"] == "buy"
    assert "http" not in json.dumps(out) and "evil.example" not in json.dumps(out)
    assert "Second line." in out["narrative"]                  # line structure kept
    assert "evidence section MISSING: news" in out["evidence_gaps"]
    assert out["data_quality"]["grade"] == "partial"


def test_finalize_overrides_a_model_action_that_contradicts_its_score():
    out = tp._finalize(pm_answer(score=25, action="buy"), {"missing": []})
    assert (out["action"], out["decision_type"]) == ("reduce", "sell")
    assert any("buy -> reduce" in a for a in out["gate_adjustments"])


def test_finalize_one_sided_score_with_do_nothing_action_becomes_a_flagged_watch():
    out = tp._finalize(pm_answer(score=75, action="hold"), {"missing": []})
    assert out["action"] == "watch" and out["decision_type"] == "hold"
    assert out["guardrail_reason"] == tp.CONFLICT_REASON


def test_finalize_rejects_a_missing_score_or_narrative():
    with pytest.raises(tp.PipelineError):
        tp._finalize(pm_answer(score=None), {"missing": []})
    with pytest.raises(tp.PipelineError):
        tp._finalize(pm_answer(narrative="  "), {"missing": []})


def test_finalize_caps_the_narrative():
    out = tp._finalize(pm_answer(narrative="x" * 5000), {"missing": []})
    assert len(out["narrative"]) <= tp.NARRATIVE_CHARS


# ------------------------------------------------------ write_report round trip

def test_write_report_id_resolves_through_the_reports_jail(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "RESULTS_DIR", str(tmp_path / "results"))
    decision = tp._finalize(pm_answer(), {"missing": []})
    stages = {"market_report": "m", "news_report": "n", "fundamentals_report": "f",
              "bull_researcher_report": "BULL", "bear_researcher_report": "BEAR",
              "research_plan": "PLAN", "trader_investment_plan": "TRADE",
              "risk_assessment": "RISK"}
    pack = {"quote": {"price": 100.0, "currency": "EUR", "as_of": "t"}, "missing": []}
    meta = {"mode": "standard", "source": "user", "lane": "L", "model": "M",
            "lane_fallbacks": []}
    rid = tp.write_report("ASML", "ASML", "2026-10-03", stages, decision, pack, meta)
    assert rid == "ASML@2026-10-03"
    data = reports.read_report(rid)
    assert data["decision_v2"]["score"] == 72 and data["lane"] == "L"
    md = asyncio.run(reports.get_report_content(rid))["content"]
    assert "BULL" in md and "PLAN" in md and "RISK" in md

    # a weaker same-day run must not clobber the stronger report
    weak = tp.write_report("ASML", "ASML", "2026-10-03", stages, decision, pack,
                           {**meta, "mode": "quick"})
    assert weak != rid and weak.startswith("ASML@2026-10-03_quick-")
    assert reports.read_report(rid)["mode"] == "standard"
    assert reports.read_report(weak)["mode"] == "quick"


def test_job_turns_cover_every_stage_plus_the_repair():
    assert tp.JOB_TURNS["quick"] == tp.stage_count("quick") + tp.REPAIR_TURNS
    assert tp.JOB_TURNS["quick"] < tp.JOB_TURNS["standard"] < tp.JOB_TURNS["deep"]
