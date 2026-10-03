#!/usr/bin/env python3
"""Seed FICTIONAL demo state for the docs media. Writes only under $DEMO_DIR (default /tmp/pd-demo)."""
import json, os, time, datetime, hashlib, pathlib

R = pathlib.Path(os.environ.get("DEMO_DIR", "/tmp/pd-demo"))
D, RES = R / "data", R / "results"
D.mkdir(exist_ok=True); RES.mkdir(exist_ok=True)
NOW = time.time()
H, DAY = 3600, 86400


def dump(name, obj):
    (D / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def ts_ago(days=0, hours=0):
    return NOW - days * DAY - hours * H


# ---------------------------------------------------------------- notifications
N = [  # (days_ago, hours_extra, ticker, source, severity, priority, title, body)
    (0, 2, "ASML", "price", "warn", 4, "ASML up 3.4% vs previous close", "Gap up at the open: +3.4% (EUR listing, Amsterdam). Cooldown 120 min."),
    (0, 5, "NVDA", "rule", "info", 3, "NVDA RSI(14) crossed above 70", "Daily RSI 71.2 on the latest closed bar. Rule: rsi_threshold above 70."),
    (0, 9, "*", "market_light", "info", 3, "Market light: green", "Breadth 2/2 above MA20, momentum positive, index drawdown shallow."),
    (1, 3, "ASML", "digest", "info", 3, "Digest: ASML HOLD (unchanged)", "Action: watch. Score 64/100, confidence medium. Stop 1,540 / target 1,820."),
    (1, 4, "NVDA", "digest", "warn", 4, "Digest: NVDA HOLD -> BUY", "Rating change on improved evidence. Score 71/100, confidence medium."),
    (2, 6, "NVDA", "edgar", "warn", 4, "NVDA Form 4: planned insider sale", "Officer sale under a 10b5-1 plan, about 0.02% of holdings. Routine."),
    (3, 2, "ASML", "news", "info", 3, "ASML: supplier outlook headline", "1 relevant headline in the last 12 h. Triage: informational."),
    (4, 8, "NVDA", "price", "urgent", 5, "NVDA down 5.2% intraday", "Sharp session move beyond the hot threshold (5%). Check the news feed."),
    (5, 1, "ASML", "rule", "warn", 4, "ASML crossed 1,900 (absolute rule)", "One-shot rule fired: price above 1,900 EUR."),
    (6, 7, "NVDA", "news", "warn", 4, "NVDA: export-rule headline", "Triage relevance 0.74, severity warning. Approval proposed."),
    (8, 4, "*", "market_light", "warn", 4, "Market light: yellow", "Breadth narrowed; momentum fading. Drop-only alert."),
    (10, 5, "ASML", "edgar", "info", 3, "ASML 6-K filing", "New foreign-private-issuer report filed."),
    (12, 3, "NVDA", "digest", "info", 3, "Digest: NVDA HOLD (unchanged)", "Action: watch. Score 58/100, confidence low."),
    (15, 6, "ASML", "price", "info", 3, "ASML down 2.6% vs previous close", "Pullback after a strong week."),
]
notif = {}
for i, (d, h, tk, src, sev, pr, title, body) in enumerate(N):
    t = ts_ago(d, h)
    row = {"id": hashlib.sha1(f"demo{i}".encode()).hexdigest()[:12], "ts": t, "ticker": None if tk == "*" else tk,
           "source": src, "title": title, "body": body, "priority": pr, "severity": sev, "url": ""}
    notif.setdefault(tk, []).append(row)
dump("notifications.json", notif)
dump("alerts_ack.json", {"watermark": ts_ago(6), "ids": []})

# ---------------------------------------------------------------- advice (the scoreboard grades these on real prices)
today = datetime.date.fromtimestamp(NOW)
def weekday_back(n):
    d, c = today, 0
    while True:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            c += 1
            if c == n:
                return d
rows = []
plan = [  # trading-days-back, ticker, rating, action, score, confidence, lane, model
    (26, "ASML", "BUY", "buy", 72, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (25, "NVDA", "HOLD", "watch", 55, "low", "ninfer-nvfp4", "qwen3.8-27b"),
    (23, "ASML", "HOLD", "watch", 63, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (22, "NVDA", "BUY", "buy", 70, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (20, "ASML", "HOLD", "watch", 61, "medium", "exllama", "Qwen3.8-Flash-Next-exl3"),
    (19, "NVDA", "HOLD", "watch", 57, "low", "ninfer-nvfp4", "qwen3.8-27b"),
    (17, "ASML", "BUY", "buy", 69, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (16, "NVDA", "SELL", "reduce", 38, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (14, "ASML", "HOLD", "watch", 62, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (12, "NVDA", "HOLD", "watch", 54, "low", "ninfer-nvfp4", "qwen3.8-27b"),
    (10, "ASML", "HOLD", "watch", 64, "high", "ninfer-nvfp4", "qwen3.8-27b"),
    (8, "NVDA", "BUY", "buy", 68, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (5, "ASML", "HOLD", "watch", 64, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
    (2, "NVDA", "HOLD", "watch", 66, "medium", "ninfer-nvfp4", "qwen3.8-27b"),
]
exc = {"BUY": "Constructive set-up with supportive trend; accumulate on pullbacks toward support.",
       "HOLD": "Quality business in an intact uptrend; wait for a better entry rather than chase.",
       "SELL": "Momentum has rolled over and risk/reward has deteriorated; reduce exposure."}
for back, tk, rating, action, score, conf, lane, model in plan:
    d = weekday_back(back)
    ts = datetime.datetime.combine(d, datetime.time(8, 45)).timestamp()
    rows.append({"ts": ts, "date": d.isoformat(), "ticker": tk, "rating": rating, "excerpt": exc[rating],
                 "action": action, "score": score, "confidence": conf, "scale_version": "ds-v1", "phase": "premarket",
                 "lane": lane, "model": model, "priceAtAdvice": None, "priceAsOf": None, "priceCurrency": "EUR",
                 "data_quality": "partial", "report": None})
dump("advice_log.json", rows)
state = {}
for tk in ("ASML", "NVDA"):
    last = [r for r in rows if r["ticker"] == tk][-1]
    state[tk] = {"rating": last["rating"], "excerpt": last["excerpt"], "report": f"{tk}@{(today - datetime.timedelta(days=1)).isoformat()}",
                 "ts": last["ts"], "date": last["date"], "action": last["action"], "score": last["score"],
                 "confidence": last["confidence"], "scale_tier": last["action"], "lane": last["lane"], "model": last["model"]}
dump("advice_state.json", state)

# ---------------------------------------------------------------- jobs / approvals / triage / news / budget
yday = (today - datetime.timedelta(days=1)).isoformat()
older = weekday_back(10).isoformat()
jobs = [
    {"id": "d1a2b3c4", "ticker": "ASML", "mode": "standard", "source": "ui", "status": "done", "message": "done: standard playbook, quality partial",
     "decision": "HOLD", "result_path": f"ASML@{yday}", "created_at": ts_ago(1, 3), "finished_at": ts_ago(1, 2.7), "lane": "ninfer-nvfp4", "model": "qwen3.8-27b"},
    {"id": "e5f6a7b8", "ticker": "NVDA", "mode": "quick", "source": "digest", "status": "done", "message": "done: digest playbook, quality partial",
     "decision": "HOLD", "result_path": f"NVDA@{yday}", "created_at": ts_ago(1, 4), "finished_at": ts_ago(1, 3.9), "lane": "ninfer-nvfp4", "model": "qwen3.8-27b"},
    {"id": "c9d0e1f2", "ticker": "ASML", "mode": "deep", "source": "approval", "status": "done", "message": "done: deep_earnings playbook, quality good",
     "decision": "HOLD", "result_path": f"ASML@{older}", "created_at": ts_ago(10, 2), "finished_at": ts_ago(10, 1.5), "lane": "exllama", "model": "Qwen3.8-Flash-Next-exl3"},
    {"id": "a3b4c5d6", "ticker": "NVDA", "mode": "quick", "source": "digest", "status": "error", "message": "lane_down: analyst",
     "decision": None, "result_path": None, "created_at": ts_ago(9, 5), "finished_at": ts_ago(9, 5)},
]
dump("analysis_jobs.json", jobs)
dump("approvals.json", [
    {"id": "a1b2c3d4e5", "ticker": "NVDA", "intent": "Deep-dive NVDA: export-rule headline", "source_id": "demo-tri-1",
     "status": "pending", "created_at": NOW - 240, "decided_at": None, "raw": "Fictional demo headline about export rules."},
    {"id": "f6e5d4c3b2", "ticker": "ASML", "intent": "Deep-dive ASML: supplier outlook", "source_id": "demo-tri-2",
     "status": "approved", "created_at": ts_ago(10, 2.2), "decided_at": ts_ago(10, 2.1), "raw": "Fictional demo headline about suppliers."},
])
tri = []
heads = [("ASML", "Supplier flags softer order timing for next year", "info", 0.41, "skip"),
         ("NVDA", "Regulator weighs new export rules for accelerators", "warning", 0.74, "deep-dive"),
         ("ASML", "Analyst raises price target ahead of results", "info", 0.52, "watch"),
         ("NVDA", "Data-center partner announces multi-year capacity deal", "info", 0.63, "watch"),
         ("ASML", "Trade-press roundup: lithography demand outlook", "info", 0.33, "skip"),
         ("NVDA", "Short-seller blog questions accounting treatment", "warning", 0.58, "watch")]
for i, (tk, h, sev, rel, hint) in enumerate(heads):
    tri.append({"id": f"demo-tri-{i}", "severity": sev, "relevance": rel, "thesis": "Fictional demo classification.", "action_hint": hint,
                "ticker": tk, "kind": "news", "headline": h, "source": "Demo Wire", "ts": ts_ago(i * 0.7, 1), "delivered": sev == "warning", "approval": None})
dump("triage_log.json", tri); dump("triage_candidates.json", [])
def news(tk, items):
    return {"ts": NOW - 600, "source": "demo",
            "items": [{"headline": h, "published_at": ts_ago(0, 2 + i * 5), "source": s, "summary": sm, "url": ""} for i, (h, s, sm) in enumerate(items)]}
dump("topnews.json", {
    "ASML": news("ASML", [("Lithography demand outlook stays firm into next year", "Demo Wire", "Fictional summary."),
                          ("Supplier flags softer order timing", "Demo Markets", "Fictional summary."),
                          ("Analyst lifts target ahead of quarterly results", "Demo Research", "Fictional summary.")]),
    "NVDA": news("NVDA", [("Capacity deal extends data-center pipeline", "Demo Wire", "Fictional summary."),
                          ("Export-rule discussion weighs on sentiment", "Demo Markets", "Fictional summary.")])})
dump("lane_budget.json", {"day": today.isoformat(), "used": 11, "purposes": {"digest": 8, "triage": 3, "approval": 0, "other": 0}, "cap": 30})


# ---------------------------------------------------------------- reports
def report(tk, name, day, price, decision, rating, score, conf, one):
    px = price
    d2 = {"score": score, "action": decision, "decision_type": rating.lower(), "confidence": conf, "guardrail_reason": "",
          "core_conclusion": {"one_sentence": one, "signal_type": "neutral", "time_sensitivity": "Next 2-4 weeks"},
          "data_perspective": {"trend_status": "Uptrend above the 50- and 200-day averages", "ma5": round(px * 0.995, 1),
                               "ma20": round(px * 0.97, 1), "ma200": round(px * 0.86, 1), "support": round(px * 0.94, 1),
                               "resistance": round(px * 1.05, 1), "volume_note": "Volume near its 20-day average; no climax."},
          "battle_plan": {"ideal_buy": round(px * 0.95, 1), "secondary_buy": round(px * 0.91, 1), "stop_loss": round(px * 0.88, 1),
                          "take_profit": round(px * 1.12, 1),
                          "entry_plan": "Scale in on pullbacks toward support; avoid adding into strength before the next report.",
                          "action_checklist": ["Confirm the close holds above support", "Check the evidence pack for fresh filings",
                                               "Size the add as a small fraction of a large position", "Re-review after earnings"]},
          "intelligence": {"risk_alerts": ["Valuation is elevated versus history", "Policy headlines can move the sector quickly"],
                           "positive_catalysts": ["Order backlog visibility", "Improving margin mix"],
                           "earnings_outlook": "Quarterly results expected in about two weeks; implied move is moderate."},
          "signal_attribution": {"technical": 4, "news": 1, "fundamentals": 2, "market_conditions": 1},
          "evidence_gaps": ["filings"], "narrative": f"{tk} remains a high-quality franchise in an intact uptrend. The set-up favours patience: hold the existing position, add only on pullbacks, and let the next report decide whether the thesis strengthens. This is a research aid, not advice.",
          "scale_version": "ds-v1", "scale_tier": decision, "gate_adjustments": [],
          "data_quality": {"grade": "partial", "missing": ["filings"], "sections": 7}}
    body = {
        "company_of_interest": tk, "trade_date": day,
        "final_trade_decision": f"**Rating**: {rating.title()}\n\n{one}",
        "market_report": f"### Technical view\n\n{tk} trades above its 20/50/200-day averages. RSI is in the upper half of its range without a divergence. Support sits near {round(px*0.94,1)} EUR and resistance near {round(px*1.05,1)} EUR.\n\n| Indicator | Reading |\n|---|---|\n| RSI(14) | 61 |\n| EMA20 vs EMA50 | bullish |\n| Volume vs 20d avg | 1.0x |",
        "sentiment_report": "Headline tone is mixed-to-positive. No crowding signals in the short-interest data.",
        "news_report": "Three relevant headlines in the window: demand outlook (positive), supplier timing (mild negative), analyst target change (positive). All are treated as claims, not facts.",
        "fundamentals_report": "Margins are stable and the balance sheet is strong. Forward valuation is above the five-year median, which limits the margin of safety.",
        "bull_researcher_report": "Bull case: durable demand, pricing power and an expanding services mix support earnings growth above consensus.",
        "bear_researcher_report": "Bear case: the multiple already discounts a strong cycle; a policy shock or order delay could compress it quickly.",
        "research_plan": "Hold the core position; accumulate only below the secondary-buy level; revisit after earnings.",
        "trader_investment_plan": f"Hold. Optional add near {round(px*0.95,1)} EUR with a stop below {round(px*0.88,1)} EUR. Express any add as a small share of the existing large position.",
        "risk_assessment": "Moderate risk. Main exposures: valuation, policy headlines, single-name concentration. Position sizing should respect an existing large weight.",
        "decision_v2": d2, "lane": "ninfer-nvfp4", "model": "qwen3.8-27b",
    }
    p = RES / tk; p.mkdir(exist_ok=True)
    (p / f"full_states_log_{day}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False))

report("ASML", "ASML", yday, 1680.0, "watch", "HOLD", 64, "medium", "Hold the position; the trend is intact but the valuation argues for patience on additions.")
report("ASML", "ASML", older, 1655.0, "watch", "HOLD", 62, "high", "Hold ahead of the print; no edge in adding before results.")
report("NVDA", "NVDA", yday, 205.0, "watch", "HOLD", 66, "medium", "Hold; momentum is constructive but headline risk is elevated.")
print("seeded", len(rows), "advice rows,", sum(len(v) for v in notif.values()), "notifications")
