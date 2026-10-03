"""reports: opaque ids, the path jail, the scan, and the markdown renderer.

tmp_path only: ``reports.RESULTS_DIR`` is pointed at a scratch tree, with a
sibling directory that shares the results directory's name as a prefix (the case
a ``startswith`` jail lets through).
"""
import asyncio
import json
import os

import pytest

from app.api import reports


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


NEW = {
    "company_of_interest": "ASML", "trade_date": "2026-10-03", "mode": "standard",
    "lane": "ninfer-nvfp4", "model": "qwen3.8-27b",
    "price_at_analysis": {"price": 1432.5, "currency": "EUR", "as_of": "x"},
    "final_trade_decision": "**Rating**: Hold\n\nNothing decisive.",
    "market_report": "## Market\ntrend up", "news_report": "n", "sentiment_report": "s",
    "fundamentals_report": "f",
    "bull_researcher_report": "BULL-ARGUMENT", "bear_researcher_report": "BEAR-ARGUMENT",
    "research_plan": "RESEARCH-PLAN-TEXT", "trader_investment_plan": "TRADER-PLAN-TEXT",
    "risk_assessment": "RISK-ASSESSMENT-TEXT",
    "decision_v2": {"decision_type": "hold", "score": 52, "action": "hold",
                    "confidence": "medium", "scale_version": "ds-v1",
                    "data_quality": {"grade": "partial", "missing": ["news"]},
                    "core_conclusion": {"one_sentence": "Balanced setup.",
                                        "time_sensitivity": "weeks"},
                    "evidence_gaps": ["evidence section MISSING: news"]},
}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "results"
    monkeypatch.setattr(reports, "RESULTS_DIR", str(root))
    put(root / "ASML" / "full_states_log_2026-10-03.json", NEW)
    put(root / "ASML" / "full_states_log_2026-10-02_quick-083000.json",
        {**NEW, "mode": "quick"})
    put(root / "ASML" / "full_states_log_2026-10-01.meta.json", {"job": "x"})
    put(root / "ASML" / "notes.json", {"x": 1})
    # sibling of the results dir whose name has the results dir as a prefix
    put(tmp_path / "results-evil" / "ASML" / "full_states_log_2026-10-03.json",
        {"secret": "outside"})
    put(tmp_path / "outside.json", {"secret": "outside"})
    return root


def test_ids_are_opaque_and_listing_skips_sidecars(tree):
    rows = reports.list_reports("asml")
    assert [r["id"] for r in rows] == ["ASML@2026-10-03", "ASML@2026-10-02_quick-083000"]
    assert all(str(tree) not in r["id"] for r in rows)
    assert reports.make_id("ASML", "2026-10-03") == rows[0]["id"]


@pytest.mark.parametrize("bad", [
    "/app/results/ASML/full_states_log_2026-10-03.json",     # old absolute-path id
    "ASML@../../results-evil/ASML/full_states_log_2026-10-03",
    "ASML@2026-10-03/../../x",
    "../results-evil/ASML@2026-10-03",
    "ASML@2026-10-01.meta",                                  # sidecar
    "ASML@2026-10-03\n",                                     # trailing newline
    "asml@2026-10-03",                                       # ids are upper-case
    "ASML@", "@2026-10-03", "ASML", "", None, 5,
])
def test_resolve_rejects_malformed_and_escaping_ids(tree, bad):
    assert reports.resolve(bad) is None
    assert reports.read_report(bad) is None
    out = asyncio.run(reports.get_report_content(bad))
    assert "error" in out and "content" not in out


def test_sibling_prefix_directory_is_outside_the_jail(tree, tmp_path):
    assert reports._inside(str(tmp_path / "results" / "ASML"), str(tmp_path / "results"))
    assert not reports._inside(str(tmp_path / "results-evil"), str(tmp_path / "results"))
    assert reports.resolve("ASML@2026-10-03") == str(
        tree / "ASML" / "full_states_log_2026-10-03.json")


def test_symlinked_report_is_not_served_or_listed(tree, tmp_path):
    link = tree / "ASML" / "full_states_log_2026-10-04.json"
    os.symlink(tmp_path / "outside.json", link)
    assert reports.resolve("ASML@2026-10-04") is None
    assert "ASML@2026-10-04" not in [r["id"] for r in reports.list_reports("ASML")]
    # a symlinked ticker directory pointing outside the root is refused too
    os.symlink(tmp_path / "results-evil" / "ASML", tree / "EVIL")
    assert reports.resolve("EVIL@2026-10-03") is None
    assert reports.list_reports("EVIL") == []


def test_unknown_ticker_and_missing_report(tree):
    assert reports.list_reports("NOPE") == []
    assert reports.list_reports("../x") == []
    assert asyncio.run(reports.get_report_content("ASML@2026-09-01")) == {
        "error": "Report not found."}


def test_render_shows_current_pipeline_keys_and_provenance(tree):
    out = asyncio.run(reports.get_report_content("ASML@2026-10-03"))
    md = out["content"]
    for needle in ("BULL-ARGUMENT", "BEAR-ARGUMENT", "RESEARCH-PLAN-TEXT",
                   "TRADER-PLAN-TEXT", "RISK-ASSESSMENT-TEXT",
                   "ninfer-nvfp4 / qwen3.8-27b", "1432.5 EUR", "**Rating:** HOLD",
                   "partial (MISSING: news)", "evidence section MISSING: news"):
        assert needle in md, needle
    assert "{'grade'" not in md            # no dict repr leaks into the report


def test_render_skips_stages_the_mode_did_not_run(tree):
    quick = {**NEW, "bull_researcher_report": "(not produced: this mode does not run this stage)",
             "bear_researcher_report": "(not produced: this mode does not run this stage)",
             "research_plan": "(not produced: this mode does not run this stage)",
             "risk_assessment": "(not produced: this mode does not run this stage)"}
    md = reports._build_markdown_report(quick)
    assert "not produced" not in md
    assert "Investment Debate" not in md and "Risk Assessment" not in md
    assert "TRADER-PLAN-TEXT" in md


def test_legacy_debate_keys_are_only_a_fallback():
    legacy = {"company_of_interest": "NVDA",
              "final_trade_decision": "Rating: BUY\nExecutive Summary: strong",
              "investment_debate_state": {"judge_decision": "LEGACY-JUDGE",
                                          "bull_history": "LB", "bear_history": "LR"},
              "risk_debate_state": {"judge_decision": "LEGACY-RISK",
                                    "aggressive_history": "AG"}}
    md = reports._build_markdown_report(legacy)
    assert "LEGACY-JUDGE" in md and "LEGACY-RISK" in md
    both = {**legacy, "bull_researcher_report": "NEW-BULL", "risk_assessment": "NEW-RISK"}
    md = reports._build_markdown_report(both)
    assert "NEW-BULL" in md and "NEW-RISK" in md
    assert "LEGACY-JUDGE" not in md and "LEGACY-RISK" not in md


def test_null_blocks_do_not_crash_any_extractor():
    data = {"company_of_interest": "X", "risk_debate_state": None,
            "investment_debate_state": None, "final_trade_decision": None,
            "decision_v2": None, "price_at_analysis": None, "trader_investment_plan": None}
    assert reports._extract_decision_from_data(data) == ""
    assert reports._extract_price_target_from_data(data) == ""
    assert reports._extract_short_thesis(data) == ""
    assert reports._quick_decision_from_data(data, "2026-10-03")[0] == ""
    assert reports._build_markdown_report(data).startswith("# X Analysis Report")


def test_decision_v2_is_authoritative_over_text():
    data = {"final_trade_decision": "Rating: BUY", "decision_v2": {"decision_type": "sell"}}
    assert reports._extract_decision_from_data(data) == "SELL"
    junk = {"final_trade_decision": "Rating: BUY", "decision_v2": {"decision_type": "maybe"}}
    assert reports._extract_decision_from_data(junk) == "BUY"


def test_ticker_rows_carry_mode_lane_model_and_markers(tree):
    rows = asyncio.run(reports.get_ticker_reports("ASML"))
    top = rows[0]
    assert top["id"] == "ASML@2026-10-03" and top["mode"] == "standard"
    assert top["decision"] == "HOLD" and top["score"] == 52 and top["action"] == "hold"
    assert top["lane"] == "ninfer-nvfp4" and top["model"] == "qwen3.8-27b"
    assert rows[1]["mode"] == "quick"
    markers = asyncio.run(reports.get_analysis_markers("ASML"))
    assert {m["date"] for m in markers} == {"2026-10-03", "2026-10-02"}
    assert all(m["decision"] == "HOLD" for m in markers)


def test_unreadable_report_degrades_to_a_row_not_an_error(tree):
    put(tree / "ASML" / "full_states_log_2026-09-30.json", "{not json")
    rows = asyncio.run(reports.get_ticker_reports("ASML"))
    bad = next(r for r in rows if r["id"] == "ASML@2026-09-30")
    assert bad["decision"] == "" and bad["summary"] == "Report available."
    assert asyncio.run(reports.get_report_content("ASML@2026-09-30")) == {
        "error": "Failed to read report."}
