"""Report browsing for the analysis pipeline.

Reports are the files the in-process pipeline writes under
``<RESULTS_DIR>/<TICKER>/full_states_log_<stem>.json`` (``<stem>`` is the trade
date, plus ``_<mode>-HHMMSS`` when a weaker run must not clobber a stronger one
of the same day). The same files are what ``kb_sync`` uploads.

Report ids are opaque ``TICKER@<stem>`` strings. A server path never leaves this
module: every id is parsed against a strict grammar, mapped to exactly one file
name pattern and then checked with ``os.path.commonpath`` against the ticker
directory, so neither ``..`` segments, absolute paths nor sibling-prefix
directories (``results-x``) can escape ``RESULTS_DIR/<TICKER>``.

A report may carry ``decision_v2`` — the structured PM decision produced by the
in-process pipeline. When it does, it is authoritative for the
decision/rating/score/action; every extractor falls back to the legacy
``final_trade_decision`` text parsing when the block is absent or malformed, so
historical reports keep rendering. The markdown renderer shows the current
pipeline keys (bull/bear researcher, research plan, trader plan, risk
assessment) and falls back to the legacy debate-state keys only when the new
ones are absent.
"""

import os
import re
import json
import pathlib
import logging

log = logging.getLogger(__name__)

RESULTS_DIR = os.getenv("RESULTS_DIR", "/app/results")

_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,19}")
_STEM_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:_[A-Za-z0-9_-]{1,40})?")
_FILE_PREFIX = "full_states_log_"
_FILE_SUFFIX = ".json"

# decision_v2.decision_type is already in the BUY/SELL/HOLD vocabulary the
# chart markers, the history badges and the decision card speak; the tuple is
# the whitelist that keeps an unexpected value from becoming a rating.
_DECISION_V2_TYPES = ("BUY", "SELL", "HOLD")


# --------------------------------------------------------------------------
# Report ids and the path jail
# --------------------------------------------------------------------------

def results_root() -> pathlib.Path:
    """The report root (module attribute so tests and env can redirect it)."""
    return pathlib.Path(RESULTS_DIR)


def make_id(symbol: str, stem: str) -> str:
    """Opaque report id for ``<symbol>/full_states_log_<stem>.json``."""
    return f"{symbol}@{stem}"


def _split_id(report_id) -> tuple[str, str] | None:
    """(ticker, stem) of a well-formed id, None for anything else."""
    if not isinstance(report_id, str) or report_id.count("@") != 1:
        return None
    ticker, stem = report_id.split("@")
    if not _TICKER_RE.fullmatch(ticker) or not _STEM_RE.fullmatch(stem):
        return None
    return ticker, stem


def _inside(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` itself or below it (component-wise)."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:        # different drives / mixed absolute+relative
        return False


def resolve(report_id) -> str | None:
    """Absolute path of the report an id names, None when the id is malformed,
    escapes the jail (symlinks included) or the file does not exist."""
    parts = _split_id(report_id)
    if parts is None:
        return None
    ticker, stem = parts
    root = os.path.realpath(results_root())
    ticker_dir = os.path.join(root, ticker)
    path = os.path.realpath(os.path.join(ticker_dir, f"{_FILE_PREFIX}{stem}{_FILE_SUFFIX}"))
    ticker_real = os.path.realpath(ticker_dir)
    if not (_inside(ticker_real, root) and _inside(path, ticker_real)):
        return None
    if os.path.dirname(path) != ticker_real or not os.path.isfile(path):
        return None
    return path


def read_report(report_id) -> dict | None:
    """Parsed report JSON for an id, None when unresolvable/unreadable."""
    path = resolve(report_id)
    return _read_report(path) if path else None


# --------------------------------------------------------------------------
# Structured decision (decision_v2) accessors
# --------------------------------------------------------------------------

def _decision_v2_decision(d2: dict) -> str:
    """Marker-vocabulary rating from a structured decision, "" when unusable."""
    dec = _to_text(d2.get("decision_type")).upper()
    return dec if dec in _DECISION_V2_TYPES else ""


def _decision_v2(data: dict) -> dict:
    """Return the structured PM decision, or {} for a legacy report.

    A block without a usable ``decision_type`` counts as absent, so callers fall
    back to the text-parsing path instead of guessing a rating.
    """
    d2 = data.get("decision_v2") if isinstance(data, dict) else None
    if not isinstance(d2, dict):
        return {}
    return d2 if _decision_v2_decision(d2) else {}


def _d2_num(container: dict, key: str):
    """Numeric field of the structured decision as float, None when unusable."""
    v = container.get(key) if isinstance(container, dict) else None
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_price(n: float) -> str:
    """Price string without trailing zeros (1900.0 -> '1900', 123.45 -> '123.45')."""
    s = f"{float(n):.2f}"
    return s.rstrip("0").rstrip(".")


def _d2_price(d2: dict, key: str) -> str:
    """A battle-plan price level from the structured decision, "" when absent."""
    plan = d2.get("battle_plan") if isinstance(d2, dict) else None
    n = _d2_num(plan, key)
    if n is None or n <= 0:
        return ""
    return _fmt_price(n)


def _d2_list(d2: dict, key: str) -> list:
    """String list from the structured decision (gaps, catalysts, alerts)."""
    raw = d2.get(key) if isinstance(d2, dict) else None
    if not isinstance(raw, list):
        return []
    return [_to_text(item) for item in raw if _to_text(item)]


# --------------------------------------------------------------------------
# Report scan
# --------------------------------------------------------------------------

def _read_report(path: str):
    """Parse a report file into a dict, None when unreadable/not an object."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.loads(f.read())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_reports(ticker: str) -> list[dict]:
    """Reports of a ticker, newest first: ``{id, stem, date, path}``.

    Only ``full_states_log_<stem>.json`` files whose stem fits the id grammar
    are listed (that also excludes the ``*.meta.json`` sidecars of the retired
    writer and anything else dropped in the directory)."""
    ticker = (ticker or "").upper().strip()
    if not _TICKER_RE.fullmatch(ticker):
        return []
    root = os.path.realpath(results_root())
    base = os.path.realpath(os.path.join(root, ticker))
    if not _inside(base, root) or not os.path.isdir(base):
        return []
    try:
        names = os.listdir(base)
    except OSError:
        return []
    rows = []
    for name in names:
        if not (name.startswith(_FILE_PREFIX) and name.endswith(_FILE_SUFFIX)):
            continue
        stem = name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]
        if not _STEM_RE.fullmatch(stem):
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path) or os.path.realpath(path) != path:
            continue
        rows.append({"id": make_id(ticker, stem), "stem": stem,
                     "date": stem[:10], "path": path})
    rows.sort(key=lambda r: (r["date"], r["stem"]), reverse=True)
    return rows


async def get_analysis_markers(ticker: str) -> list[dict]:
    """
    Return list of { date, decision } for all reports of a ticker.
    Used to draw small BUY/HOLD/SELL markers on the price chart.
    """
    markers = []
    for r in list_reports(ticker):
        data = _read_report(r["path"])
        if data is None:
            continue
        decision = _extract_decision_from_data(data)
        if not decision:
            # Unknown decision: no marker rather than a fabricated HOLD.
            continue
        marker = {"date": r["date"], "decision": decision}
        # score/action ride along for consumers that show them (the chart only
        # reads date+decision); never present on legacy reports.
        d2 = _decision_v2(data)
        if d2:
            score = _d2_num(d2, "score")
            if score is not None:
                marker["score"] = int(round(score))
            action = _to_text(d2.get("action")).lower()
            if action:
                marker["action"] = action
        markers.append(marker)
    return markers


async def get_ticker_reports(ticker: str) -> list[dict]:
    """
    For a ticker, list reports and extract basic decision from file content.
    Returns list of { id, date, decision, summary, mode, score, action, lane, model }.
    No AI calls here so it loads fast.
    """
    result = []
    for r in list_reports(ticker):
        data = _read_report(r["path"])
        decision, short, mode = _quick_decision_from_data(data, r["stem"])
        entry = {
            "id": r["id"],
            "date": r["date"],
            "decision": decision,
            "summary": short,
            "mode": mode,
            # Structured-decision extras (None on legacy reports).
            "score": None,
            "action": None,
            "lane": _to_text((data or {}).get("lane")) or None,
            "model": _to_text((data or {}).get("model")) or None,
        }
        d2 = _decision_v2(data) if data is not None else {}
        if d2:
            score = _d2_num(d2, "score")
            if score is not None:
                entry["score"] = int(round(score))
            action = _to_text(d2.get("action")).lower()
            if action:
                entry["action"] = action
        result.append(entry)
    return result


def _risk_judge(data: dict) -> str:
    """Legacy ``risk_debate_state.judge_decision`` text ("" when absent)."""
    state = data.get("risk_debate_state") if isinstance(data, dict) else None
    return _to_text(state.get("judge_decision")) if isinstance(state, dict) else ""


def _quick_decision_from_data(data, stem: str) -> tuple[str, str, str]:
    """
    Decision, a short readable summary and the analysis mode of a parsed report.
    Returns (decision, short_summary, mode); ``data`` None = unreadable file.
    Summary: 1-2 lines from Executive Summary / thesis + optional target/horizon.
    No redundant decision word (badge already shows it).
    """
    if data is None:
        return ("", "Report available.", "standard")

    mode = _detect_mode(stem, data)
    decision = _extract_decision_from_data(data)
    price_target = _extract_price_target_from_data(data)

    ft = _to_text(data.get("final_trade_decision"))
    rd = _risk_judge(data)
    d2 = _decision_v2(data)
    time_horizon = ""
    if d2:
        time_horizon = _safe_str(d2, "core_conclusion", "time_sensitivity")
    if not time_horizon:
        time_horizon = _extract_field(ft, "Time Horizon") or _extract_field(rd, "Time Horizon")

    thesis = _extract_short_thesis(data)

    # Concise summary: thesis + optional target/horizon (no decision word)
    parts = []
    if thesis:
        parts.append(thesis)
    tail = []
    if price_target:
        tail.append(f"Target ${price_target}")
    if time_horizon:
        tail.append(time_horizon)
    if tail:
        parts.append(" · ".join(tail))
    summary = " ".join(parts).strip()
    if not summary:
        summary = "Report available."

    return (decision, summary, mode)


def _detect_mode(stem: str, data: dict) -> str:
    """quick/standard/deep: the report's own ``mode`` key when valid, else the
    legacy heuristics (stem hint, text hints, short body)."""
    stored = _to_text(data.get("mode")).lower()
    if stored in ("quick", "standard", "deep"):
        return stored

    name = (stem or "").lower()
    if "quick" in name:
        return "quick"
    if "deep" in name:
        return "deep"

    ft = _to_text(data.get("final_trade_decision"))
    if ft:
        u = ft.upper()
        if "QUICK ANALYSIS" in u or "QUICK MODE" in u:
            return "quick"
        if "DEEP ANALYSIS" in u or "DEEP MODE" in u or "DEEP DIVE" in u:
            return "deep"
        if len(ft) < 600:
            return "quick"

    return "standard"


def _safe_str(data: dict, *keys, default: str = "") -> str:
    """
    Safely traverse nested dict keys and return string value.
    """
    cur = data
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    if isinstance(cur, str):
        return cur.strip()
    if cur is not None:
        return str(cur).strip()
    return default


def _extract_decision_from_data(data: dict) -> str:
    """
    Extract BUY/SELL/HOLD from report JSON.
    Handles both:
    - Structured decision (decision_v2.decision_type) — authoritative when present
    - Nested dicts (final_trade_decision.Rating)
    - Markdown-style strings (final_trade_decision: 'Rating: BUY\n...')
    """
    # 0) Structured PM decision: the rating is decision_type, never re-derived
    d2 = _decision_v2(data)
    if d2:
        return _decision_v2_decision(d2)

    # 1) Try nested dict: final_trade_decision.Rating
    rating = _safe_str(data, "final_trade_decision", "Rating").upper()
    if rating in ("BUY", "SELL", "HOLD"):
        return rating

    # 2) Try nested dict: risk_debate_state.judge_decision.Recommendation
    judge = _safe_str(data, "risk_debate_state", "judge_decision", "Recommendation").upper()
    if judge in ("BUY", "SELL", "HOLD"):
        return judge

    # 3) Try nested dict: investment_plan.Recommendation
    plan = _safe_str(data, "investment_plan", "Recommendation").upper()
    if plan in ("BUY", "SELL", "HOLD"):
        return plan

    # 4) Try markdown-style text fields
    # Check final_trade_decision
    ft = _to_text(data.get("final_trade_decision"))
    d = _extract_decision_from_text(ft)
    if d:
        return d

    # Check risk_debate_state.judge_decision
    rd = _risk_judge(data)
    d = _extract_decision_from_text(rd)
    if d:
        return d

    # Check trader_investment_plan
    ti = _to_text(data.get("trader_investment_plan"))
    d = _extract_decision_from_text(ti)
    if d:
        return d

    # Fallback: decision unknown (never assume HOLD)
    return ""


def _extract_decision_from_text(text: str) -> str:
    """
    Extract BUY/SELL/HOLD from analysis text using strict patterns.
    Avoids false positives from words like "Holding" or narrative mentions.
    """
    if not text:
        return ""
    u = text.upper()

    # Patterns:
    # "Rating: BUY", "Decision: SELL", "FINAL DECISION: HOLD", etc.
    import re
    m = re.search(r"(?:RATING|DECISION|RECOMMENDATION)\s*[:\-]\s*(BUY|SELL|HOLD)", u)
    if m:
        return m.group(1)

    # Strong markers
    if "**BUY**" in u:
        return "BUY"
    if "**SELL**" in u:
        return "SELL"
    if "**HOLD**" in u:
        return "HOLD"

    # Transaction proposal
    if "FINAL TRANSACTION PROPOSAL: BUY" in u or "TRANSACTION PROPOSAL: BUY" in u:
        return "BUY"
    if "FINAL TRANSACTION PROPOSAL: SELL" in u or "TRANSACTION PROPOSAL: SELL" in u:
        return "SELL"

    return ""


def _extract_price_target_from_data(data: dict) -> str:
    """
    Extract price target as a number string if available.
    Handles the structured decision, nested dicts and markdown-style strings.
    """
    # 0) Structured decision: the take-profit level is the price target the
    # legacy reports spelled out in prose.
    d2 = _decision_v2(data)
    if d2:
        pt = _d2_price(d2, "take_profit")
        if pt:
            return pt

    # 1) Try nested dict: final_trade_decision.Price Target
    pt = _safe_str(data, "final_trade_decision", "Price Target")
    if pt:
        n = _extract_number(pt)
        if n:
            return n

    # 2) Try nested dict: risk_debate_state.judge_decision.Price Target
    pt2 = _safe_str(data, "risk_debate_state", "judge_decision", "Price Target")
    if pt2:
        n = _extract_number(pt2)
        if n:
            return n

    # 3) Try markdown-style fields
    ft = _to_text(data.get("final_trade_decision"))
    rd = _risk_judge(data)
    combined = (ft or "") + "\n" + (rd or "")
    if combined:
        n = _extract_price_target_from_text(combined)
        if n:
            return n

    return ""


def _extract_price_target_from_text(text: str) -> str:
    """
    Extract price target from markdown-style text.
    Looks for 'Price Target: $123.45' patterns.
    """
    if not text:
        return ""
    import re
    m = re.search(r"Price\s*Target\s*[:\-]\s*\$?([\d,]+\.\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1).replace(",", "")
    # Fallback: first reasonable number near "target"
    m2 = re.search(r"(?:target|price target)\s*[:\-]?\s*\$?([\d,]+\.\d+)", text, re.IGNORECASE)
    if m2:
        return m2.group(1).replace(",", "")
    return ""


def _extract_number(s: str) -> str:
    """
    Extract a numeric value from a string.
    """
    if not s:
        return ""
    import re
    nums = re.findall(r"[\d,]+\.?\d*", s.replace(",", ""))
    return nums[0] if nums else ""


def _to_text(v) -> str:
    """
    Convert value to text if possible.
    """
    if isinstance(v, str):
        return v.strip()
    if v is not None:
        return str(v).strip()
    return ""


def _stage_text(data: dict, key: str) -> str:
    """Text of a pipeline stage key; the writer's "(not produced: ...)" marker
    for a stage the mode does not run counts as absent."""
    text = _to_text(data.get(key))
    return "" if text.startswith("(not produced") else text


def _extract_short_thesis(data: dict) -> str:
    """
    Extract a very short thesis (1-2 sentences, max ~160 chars) from report.
    Avoids including the decision line (e.g. 'Rating: Buy').
    Avoids boilerplate like "Analysing ASML...", "Running technical...".
    Prefers:
      - the structured one-sentence conclusion
      - Executive Summary
      - Investment Thesis
      - For quick reports: short summary from final decision + key signals
    Strips markdown prefixes like "**Executive Summary**:".
    """
    # 0) Structured decision: the one-sentence conclusion IS the thesis.
    d2 = _decision_v2(data)
    if d2:
        one = _first_sentences(_safe_str(d2, "core_conclusion", "one_sentence"),
                              max_chars=160)
        if one:
            return one

    ft = _to_text(data.get("final_trade_decision"))
    rd = _risk_judge(data)
    source = ft or rd

    if not source:
        return ""

    import re

    # 1) Try Executive Summary
    m = re.search(r"(?:Executive\s*Summary)\s*[:\-]\s*(.+?)(?:\n\s*\n|\n#+|$)", source, re.DOTALL | re.IGNORECASE)
    if m:
        return _first_sentences(m.group(1).strip(), max_chars=160)

    # 2) Try Investment Thesis
    m = re.search(r"(?:Investment\s*Thesis)\s*[:\-]\s*(.+?)(?:\n\s*\n|\n#+|$)", source, re.DOTALL | re.IGNORECASE)
    if m:
        return _first_sentences(m.group(1).strip(), max_chars=160)

    # 3) For quick reports: extract short summary from final decision section
    # Look for "Final Decision" and take up to 2 short bullets
    m = re.search(r"(?:Final\s*Decision)\s*[:\-]\s*(.+?)(?:\n#+|$)", source, re.DOTALL | re.IGNORECASE)
    if m:
        section = m.group(1).strip()
        # Skip boilerplate lines
        lines = [l.strip() for l in section.split("\n") if l.strip()]
        bullets = []
        for line in lines:
            low = line.lower()
            # Skip boilerplate
            if any(x in low for x in [
                "analysing",
                "running technical",
                "fetching",
                "connecting",
                "loading",
                "initializing",
                "calling tool",
            ]):
                continue
            # Take short bullets
            if (line.startswith("- ") or line.startswith("* ")) and len(line) < 180:
                bullets.append(line.strip("- * ").strip())
            elif len(line) < 180 and not line.startswith("**"):
                bullets.append(line)
        if bullets:
            text = " ".join(bullets[:2])
            return _first_sentences(text, max_chars=160)

    # 4) Fallback: first paragraph, excluding a leading "Rating:" line and boilerplate
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", source) if p.strip()]
    for p in paragraphs:
        # If paragraph is just a rating line, skip
        if re.match(r"^\*\*Rating\*\*\s*:\s*\w+", p, re.IGNORECASE):
            continue
        if re.match(r"^Rating\s*:\s*\w+", p, re.IGNORECASE):
            continue
        # Skip boilerplate lines
        low = p.lower()
        if any(x in low for x in [
            "analysing",
            "running technical",
            "fetching",
            "connecting",
            "loading",
            "initializing",
            "calling tool",
        ]):
            continue
        if p.strip():
            return _first_sentences(p, max_chars=160)

    return ""


def _first_sentences(text: str, max_chars: int = 160) -> str:
    """
    Return first 1-2 sentences, truncated to max_chars.
    Strips markdown prefixes like "**Executive Summary**:".
    """
    if not text:
        return ""
    # Normalize whitespace
    t = text.replace("\n", " ").strip()
    t = " ".join(t.split())
    # Strip markdown-style prefixes
    import re
    t = re.sub(r"^\*\*[^*]+\*\*\s*[:\-]\s*", "", t, flags=re.IGNORECASE)
    t = t.strip()
    if len(t) <= max_chars:
        return t
    # Try to cut at sentence boundary
    truncated = t[:max_chars]
    last_period = truncated.rfind(". ")
    if last_period > 40:
        return truncated[: last_period + 1].strip()
    return truncated.strip()


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------

async def get_report_content(report_id: str) -> dict:
    """Markdown for a report id: ``{content}`` or ``{error}``. Never reads a
    file the id grammar + jail (``resolve``) does not map to a report."""
    if _split_id(report_id) is None:
        return {"error": "Invalid report ID."}
    path = resolve(report_id)
    if path is None:
        return {"error": "Report not found."}
    data = _read_report(path)
    if data is None:
        return {"error": "Failed to read report."}
    md = _build_markdown_report(data)
    if not md:
        return {"error": "The report has no content."}
    return {"content": md}


def _build_markdown_report(data: dict) -> str:
    """
    Build a comprehensive, clean markdown report from the JSON data.
    Includes:
    - Decision
    - Evidence Gaps (structured decisions only)
    - Executive Summary
    - Trader Plan
    - Technical Analysis
    - Sentiment
    - News
    - Fundamentals
    - Investment Debate (short)
    - Risk Debate (short)
    - Final Trade Decision
    Truncates long sections to keep it readable.
    """
    if not isinstance(data, dict):
        # Try to pass through raw content if it's already markdown
        return ""

    lines = []

    # Header
    ticker = data.get("company_of_interest") or "Unknown"
    trade_date = data.get("trade_date") or ""
    decision = _extract_decision_from_data(data)
    price_target = _extract_price_target_from_data(data)
    d2 = _decision_v2(data)

    lines.append(f"# {ticker} Analysis Report")
    if trade_date:
        lines.append(f"**Date:** {trade_date}")
    model_line = " / ".join(x for x in (_to_text(data.get("lane")), _to_text(data.get("model"))) if x)
    if model_line:
        lines.append(f"**Model:** {model_line}")
    paa = data.get("price_at_analysis")
    if isinstance(paa, dict) and _d2_num(paa, "price") is not None:
        cur = _to_text(paa.get("currency"))
        lines.append(f"**Price at analysis:** {_fmt_price(_d2_num(paa, 'price'))} {cur}".rstrip())
    lines.append("")

    # Decision
    lines.append("## Decision")
    lines.append(f"- **Rating:** {decision}")
    if d2:
        bits = []
        score = _d2_num(d2, "score")
        if score is not None:
            bits.append(f"**Score:** {int(round(score))}/100")
        action = _to_text(d2.get("action")).lower()
        if action:
            bits.append(f"**Action:** {action}")
        confidence = _to_text(d2.get("confidence")).lower()
        if confidence:
            bits.append(f"**Confidence:** {confidence}")
        if bits:
            lines.append("- " + " · ".join(bits))
        guardrail = _to_text(d2.get("guardrail_reason"))
        if guardrail:
            lines.append(f"- **Guardrail:** {guardrail}")
    if price_target:
        lines.append(f"- **Price Target:** ${price_target}")

    # Time horizon: try from final_trade_decision or risk_debate_state
    ft = _to_text(data.get("final_trade_decision"))
    rd = _risk_judge(data)
    time_horizon = ""
    if d2:
        time_horizon = _safe_str(d2, "core_conclusion", "time_sensitivity")
    if not time_horizon:
        time_horizon = _extract_field(ft, "Time Horizon") or _extract_field(rd, "Time Horizon")
    if time_horizon:
        lines.append(f"- **Time Horizon:** {time_horizon}")
    # Provenance of the decision: which scale mapped score -> action, and how
    # much of the evidence pack was actually available.
    provenance = []
    scale_version = _to_text(d2.get("scale_version")) if d2 else ""
    if scale_version:
        provenance.append(f"**Decision scale:** {scale_version}")
    dq = d2.get("data_quality") if d2 else None
    if isinstance(dq, dict):
        grade = _to_text(dq.get("grade"))
        miss = _d2_list(dq, "missing")
        data_quality = grade + (f" (MISSING: {', '.join(miss)})" if miss else "")
    else:
        data_quality = _to_text(dq)
    if data_quality:
        provenance.append(f"**Data quality:** {data_quality}")
    if provenance:
        lines.append("- " + " · ".join(provenance))
    lines.append("")

    # Evidence gaps: what the pipeline could not verify. Shown so a reader
    # never trusts a number in the narrative that had no source behind it.
    gaps = _d2_list(d2, "evidence_gaps") if d2 else []
    if gaps:
        lines.append("## Evidence Gaps")
        for gap in gaps[:8]:
            lines.append(f"- {gap}")
        lines.append("")

    # Executive Summary
    exec_summary = _extract_section(ft, "Executive Summary") or _extract_section(rd, "Executive Summary")
    if not exec_summary and d2:
        exec_summary = _to_text(_safe_str(d2, "core_conclusion", "one_sentence"))
    if exec_summary:
        lines.append("## Executive Summary")
        lines.append(_truncate_text(exec_summary, 800))
        lines.append("")

    # Trader Plan
    trader_plan = _stage_text(data, "trader_investment_plan")
    if trader_plan:
        lines.append("## Trader Plan")
        lines.append(_truncate_text(trader_plan, 1500))
        lines.append("")

    # Technical Analysis
    market_report = _to_text(data.get("market_report"))
    if market_report:
        lines.append("## Technical Analysis")
        lines.append(_truncate_text(market_report, 800))
        lines.append("")

    # Sentiment
    sentiment_report = _to_text(data.get("sentiment_report"))
    if sentiment_report:
        lines.append("## Sentiment")
        lines.append(_truncate_text(sentiment_report, 600))
        lines.append("")

    # News
    news_report = _to_text(data.get("news_report"))
    if news_report:
        lines.append("## News Highlights")
        lines.append(_truncate_text(news_report, 600))
        lines.append("")

    # Fundamentals
    fundamentals_report = _to_text(data.get("fundamentals_report"))
    if fundamentals_report:
        lines.append("## Fundamentals")
        lines.append(_truncate_text(fundamentals_report, 600))
        lines.append("")

    # Research debate: current pipeline keys first, the legacy debate-state
    # dicts only for reports written before the in-process pipeline.
    bull = _stage_text(data, "bull_researcher_report")
    bear = _stage_text(data, "bear_researcher_report")
    plan = _stage_text(data, "research_plan")
    risk = _stage_text(data, "risk_assessment")
    if bull or bear or plan:
        if bull or bear:
            lines.append("## Investment Debate")
            if bull:
                lines.append("### Bull Case")
                lines.append(_truncate_text(bull, 1200))
                lines.append("")
            if bear:
                lines.append("### Bear Case")
                lines.append(_truncate_text(bear, 1200))
                lines.append("")
        if plan:
            lines.append("## Research Plan")
            lines.append(_truncate_text(plan, 1500))
            lines.append("")
    else:
        inv_debate = data.get("investment_debate_state")
        if isinstance(inv_debate, dict):
            judge = _to_text(inv_debate.get("judge_decision"))
            lbull = _to_text(inv_debate.get("bull_history"))
            lbear = _to_text(inv_debate.get("bear_history"))
            if judge or lbull or lbear:
                lines.append("## Investment Debate")
                if judge:
                    lines.append("### Judge Decision")
                    lines.append(_truncate_text(judge, 600))
                    lines.append("")
                if lbull:
                    lines.append("### Bull Case (summary)")
                    lines.append(_truncate_text(lbull, 400))
                    lines.append("")
                if lbear:
                    lines.append("### Bear Case (summary)")
                    lines.append(_truncate_text(lbear, 400))
                    lines.append("")

    if risk:
        lines.append("## Risk Assessment")
        lines.append(_truncate_text(risk, 1500))
        lines.append("")
    else:
        risk_debate = data.get("risk_debate_state")
        if isinstance(risk_debate, dict):
            judge = _to_text(risk_debate.get("judge_decision"))
            agg = _to_text(risk_debate.get("aggressive_history"))
            cons = _to_text(risk_debate.get("conservative_history"))
            neut = _to_text(risk_debate.get("neutral_history"))
            if judge or agg or cons or neut:
                lines.append("## Risk Debate")
                if judge:
                    lines.append("### Judge Decision")
                    lines.append(_truncate_text(judge, 600))
                    lines.append("")
                if agg:
                    lines.append("### Aggressive View (summary)")
                    lines.append(_truncate_text(agg, 400))
                    lines.append("")
                if cons:
                    lines.append("### Conservative View (summary)")
                    lines.append(_truncate_text(cons, 400))
                    lines.append("")
                if neut:
                    lines.append("### Neutral View (summary)")
                    lines.append(_truncate_text(neut, 400))
                    lines.append("")

    # Final Trade Decision (full, if present)
    if ft:
        lines.append("## Final Trade Decision")
        lines.append(ft)
        lines.append("")

    return "\n".join(lines).strip()


def _extract_section(text: str, heading: str) -> str:
    """
    Extract text under a markdown-style heading like '## Executive Summary' or 'Executive Summary:'.
    """
    if not text:
        return ""
    import re
    # Try '## Heading' or '### Heading'
    m = re.search(r"##+\s+" + re.escape(heading) + r"\s*\n(.*?)(?:\n#+|\n\s*\n|$)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Try 'Heading: ...'
    m2 = re.search(re.escape(heading) + r"\s*[:\-]\s*(.*?)(?:\n\s*\n|$)", text, re.DOTALL | re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return ""


def _extract_field(text: str, field: str) -> str:
    """
    Extract a short field value like 'Time Horizon: 3-6 months'.
    """
    if not text:
        return ""
    import re
    m = re.search(re.escape(field) + r"\s*[:\-]\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _truncate_text(text: str, max_chars: int = 800) -> str:
    """
    Truncate text to max_chars, trying to respect line breaks.
    """
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    # Try to break at newline
    truncated = t[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:
        return truncated[:last_newline].strip() + "\n..."
    # Otherwise break at space
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        return truncated[:last_space].strip() + "..."
    return truncated.strip() + "..."