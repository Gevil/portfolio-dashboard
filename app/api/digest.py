"""Scheduled analysis digest: one in-process run per analysable holding, ONE
consolidated push per batch.

At each configured local time (default 08:45 and 16:00, matching the old
``ai-stock-analyses.timer``) this worker enqueues one quick analysis per
analysable holding (``registry.holdings()`` filtered by
``prices.is_analyzable``: the benchmark index and the ETF are excluded) on the
in-process job queue (``app.api.jobs``), waits for each to settle, reads the
decision out of the report the pipeline wrote and diffs it against the previous
advice state.

What the owner receives is a single push per batch (``format_digest``): per
ticker the EUR price and day change, rating / action / score / confidence, the
stop and target of the battle plan, what changed since the last run, the
data-quality gaps - and an explicit line for every ticker that did not run and
why. A failed ticker is retried once after a delay; a batch with any failure is
recorded ``ok=False`` and its push is titled ``digest incomplete: ...``.

Honesty rules:

* the rating is ``decision_v2.decision_type`` whenever valid; only a legacy
  report falls back to text parsing (``norm_rating``: markdown stripped, the
  explicit ``Rating:`` line first, SELL/HOLD checked before BUY, negated phrases
  such as "would not buy" never count). A ticker whose rating cannot be read
  is a failure, never an ``OTHER`` push;
* change detection compares rating, action and score band ONLY - never the
  free-text excerpt;
* advice state and the advice log advance only after the push was delivered
  (``Delivery.status != 'failed'``), so an undelivered change is reported
  again next run instead of being lost;
* no model-written URL reaches a push (``textsafe``).

Two gates keep an unattended batch honest: a lane must be serving, and the
digest's turn allowance in the daily budget must have room. A batch that fails
a gate is deferred and retried in ``LANE_RETRY_S``; the skip is recorded in the
worker-run ring. A batch interrupted by a restart is re-run for the tickers
that did not finish (``_resume``). Enabled only when ``DIGEST_ENABLED=1``.
Container-local time is used for the schedule: run with ``TZ=Europe/Prague``.
"""
import asyncio
import datetime
import logging
import math
import os
import pathlib
import re
import time

from app.api import (config_store, jobs, jsonstore, lane_client, notify,
                     portfolio, prices, registry, reports, runlog, textsafe)

log = logging.getLogger("digest")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "advice_state.json"
ADVICE_LOG = DATA_DIR / "advice_log.json"
# Capped far above a year of digests; graded outcomes live in the scoreboard's
# own store and are never pruned by this cap.
ADVICE_LOG_CAP = 2000
LANE_RETRY_S = 1800
PUSH_BODY_CHARS = 3600
SCALE_VERSION = "ds-v1"

DEFAULTS = {
    "times": ["08:45", "16:00"],
    "status_poll_s": 30,
    "run_timeout_min": 30,
    "digest_excerpt_chars": 240,
    "retry_delay_s": 120,
    "resume_max_age_h": 6,
    "resume_delay_s": 60,
}

VALID_RATINGS = ("BUY", "HOLD", "SELL")

_task: asyncio.Task | None = None
_resume_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "running": False,
          "next_run": 0.0, "skip_reason": "", "last_batch": None}


def cfg() -> dict:
    merged = dict(DEFAULTS)
    block = config_store.read().get("digest")
    if isinstance(block, dict):
        merged.update(block)
    return merged


# --------------------------------------------------------------------------
# rating
# --------------------------------------------------------------------------

_NEGATED = re.compile(
    r"\b(?:not|never|no|don'?t|do not|doesn'?t|does not|won'?t|wouldn'?t|"
    r"would not|shouldn'?t|should not|cannot|can'?t|avoid|against|"
    r"rather than|instead of)\s+(?:\w+\s+){0,2}?"
    r"(?:strong\s+)?(?:buy|buying|add|accumulate|overweight|sell|selling|"
    r"hold|trim|reduce)\b")
_RATING_LINE = re.compile(
    r"^\s*(?:final\s+)?(?:trade\s+)?(?:rating|recommendation|decision)"
    r"\s*[:\-\u2013\u2014=]\s*(.+)$", re.I | re.M)
_SELL = re.compile(r"\bsell(?:ing)?\b|underweight|\breduce\b|\btrim\b|\bexit\b"
                   r"|\bshort\b")
_HOLD = re.compile(r"\bhold\b|neutral|\bno change\b|\bwait\b|\bwatch\b"
                   r"|equal.weight|market perform")
_BUY = re.compile(r"\bbuy\b|overweight|accumulate|outperform")


def _plain(text: str) -> str:
    """Markdown stripped (emphasis, code, headings, quotes), lines kept."""
    text = str(text or "").replace("_", " ")
    return re.sub(r"[*`#>~]+", "", text)


def _bucket(phrase: str) -> str | None:
    """SELL / HOLD / BUY for a lower-cased phrase, in that order of suspicion
    (a text saying both 'hold' and 'buy' is a hold); None when none match.
    Negated phrases ('would not buy') are removed first - a refusal to buy
    says nothing about buying."""
    cleaned = _NEGATED.sub(" ", phrase)
    if _SELL.search(cleaned):
        return "SELL"
    if _HOLD.search(cleaned):
        return "HOLD"
    if _BUY.search(cleaned):
        return "BUY"
    return None


def norm_rating(text: str) -> str:
    """Map a free-text decision to BUY / HOLD / SELL, else OTHER.

    The explicit ``Rating:`` line wins (``**Rating**: Hold`` too: markdown is
    stripped first); only without a usable one is the whole text scanned."""
    plain = _plain(text).lower()
    for m in _RATING_LINE.finditer(plain):
        bucket = _bucket(m.group(1))
        if bucket:
            return bucket
    return _bucket(plain) or "OTHER"


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _epoch(value) -> float | None:
    """Epoch seconds from a number or an ISO-8601 string."""
    v = _num(value)
    if v is not None:
        return v
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _first_sentence_text(text: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", _plain(text)).strip()
    return textsafe.clean_text(t, max_len=limit)


def parse_decision(data: dict, excerpt_chars: int) -> dict:
    """Everything the digest needs from one parsed report.

    ``rating`` is ``decision_v2.decision_type`` whenever that is a valid
    BUY/HOLD/SELL, whatever the text says; legacy reports (no usable
    ``decision_v2``) fall back to ``norm_rating(final_trade_decision)``. The
    excerpt is the one-sentence conclusion (URLs and control characters
    removed), else the narrative / decision text without its rating line."""
    d = data if isinstance(data, dict) else {}
    v2 = d.get("decision_v2") if isinstance(d.get("decision_v2"), dict) else {}
    final_text = str(d.get("final_trade_decision") or "")
    dtype = str(v2.get("decision_type") or "").strip().upper()
    if dtype in VALID_RATINGS:
        rating, source = dtype, "decision_v2"
    else:
        rating, source = norm_rating(final_text), "text"
    core = v2.get("core_conclusion") if isinstance(v2.get("core_conclusion"),
                                                   dict) else {}
    one = str(core.get("one_sentence") or "").strip()
    if one and one.upper() != "MISSING":
        excerpt = _first_sentence_text(one, excerpt_chars)
    else:
        body = str(v2.get("narrative") or "") or _RATING_LINE.sub(
            "", _plain(final_text))
        excerpt = _first_sentence_text(body, excerpt_chars)
    plan = v2.get("battle_plan") if isinstance(v2.get("battle_plan"), dict) else {}
    dq = v2.get("data_quality") if isinstance(v2.get("data_quality"), dict) else {}
    gaps = [str(g) for g in (v2.get("evidence_gaps") or [])
            if str(g).strip() and str(g).upper() != "MISSING"]
    score = _num(v2.get("score"))
    price = d.get("price_at_analysis") if isinstance(
        d.get("price_at_analysis"), dict) else {}
    return {
        "rating": rating, "rating_source": source, "excerpt": excerpt,
        "action": (str(v2.get("action") or "").strip().lower() or None),
        "score": int(round(score)) if score is not None else None,
        "confidence": (str(v2.get("confidence") or "").strip().lower() or None),
        "scale_tier": (str(v2.get("scale_tier") or "").strip().lower() or None),
        "scale_version": v2.get("scale_version") or None,
        "data_quality": dq.get("grade"),
        "missing": [str(m) for m in (dq.get("missing") or [])],
        "gaps": [textsafe.clean_text(g, max_len=80) for g in gaps[:3]],
        "stop": _num(plan.get("stop_loss")),
        "target": _num(plan.get("take_profit")),
        "lane": d.get("lane") or None, "model": d.get("model") or None,
        "report_price": _num(price.get("price")),
        "report_price_currency": price.get("currency"),
        "report_price_asof": _epoch(price.get("as_of")),
    }


def parse_report(report_id: str, excerpt_chars: int) -> dict:
    """``parse_decision`` for a report id (see ``reports``); ValueError when the
    id does not resolve to a readable report."""
    data = reports.read_report(report_id)
    if data is None:
        raise ValueError(f"report {report_id!r} is not readable")
    return parse_decision(data, excerpt_chars)


# --------------------------------------------------------------------------
# change detection
# --------------------------------------------------------------------------

def detect_change(prev: dict | None, cur: dict) -> dict:
    """What changed since the last tracked advice, on rating, action and score
    band ONLY - never on excerpt text.

    -> ``{"kind": first|rating|action|band|none, "parts": [str]}``. ``kind`` is
    the strongest trigger; ``parts`` also lists the score move for context
    (a score drift inside one band is information, not a change). A previous
    row without action/band (a pre-v2 state file) is compared on rating only."""
    if not prev:
        return {"kind": "first", "parts": ["first tracked advice"]}
    parts: list[str] = []
    kind = "none"
    if prev.get("rating") != cur.get("rating"):
        kind = "rating"
        parts.append(f"rating {prev.get('rating') or '?'} \u2192 {cur['rating']}")
    for key, label, level in (("action", "action", "action"),
                              ("scale_tier", "band", "band")):
        a, b = prev.get(key), cur.get(key)
        if a and b and a != b:
            parts.append(f"{label} {a} \u2192 {b}")
            if kind == "none":
                kind = level
    ps, cs = prev.get("score"), cur.get("score")
    if isinstance(ps, (int, float)) and isinstance(cs, (int, float)) and ps != cs:
        parts.append(f"score {int(ps)} \u2192 {int(cs)}")
    if prev.get("confidence") and cur.get("confidence") \
            and prev["confidence"] != cur["confidence"]:
        parts.append(f"confidence {prev['confidence']} \u2192 {cur['confidence']}")
    return {"kind": kind, "parts": parts or ["no change"]}


# --------------------------------------------------------------------------
# push formatting
# --------------------------------------------------------------------------

def _eur(value) -> str:
    v = _num(value)
    return "n/a" if v is None else f"\u20ac{v:,.2f}"


def _clock(epoch) -> str:
    e = _num(epoch)
    if e is None:
        return "?"
    return datetime.datetime.fromtimestamp(e).strftime("%H:%M")


def _ticker_lines(o: dict) -> list[str]:
    t = o["ticker"]
    if o["status"] != "ok":
        return [f"{t}  NOT ANALYSED: {textsafe.clean_text(o.get('reason'), 120)}"
                + (" (after retry)" if o.get("retried") else "")]
    pr = o.get("price") or {}
    head = f"{t}  {_eur(pr.get('price'))}"
    if _num(pr.get("day_pct")) is not None:
        head += f" ({pr['day_pct']:+.2f}% day"
        head += ", stale" if pr.get("stale") else f", as of {_clock(pr.get('as_of'))}"
        head += ")"
    lines = [head]
    verdict = [o["rating"]]
    if o.get("action"):
        verdict.append(f"action {o['action']}")
    if o.get("score") is not None:
        verdict.append(f"score {o['score']}/100")
    if o.get("confidence"):
        verdict.append(f"confidence {o['confidence']}")
    lines.append("  " + " \u00b7 ".join(verdict))
    if o.get("stop") is not None or o.get("target") is not None:
        lines.append(f"  stop {_eur(o.get('stop'))} \u00b7 target "
                     f"{_eur(o.get('target'))}")
    ch = o.get("change") or {}
    if ch:
        lines.append("  changed: " + "; ".join(ch.get("parts") or ["no change"]))
    gaps = list(o.get("missing") or []) or list(o.get("gaps") or [])
    if gaps:
        lines.append(f"  gaps: {', '.join(gaps[:4])}"
                     + (f" (data quality {o['data_quality']})"
                        if o.get("data_quality") else ""))
    if o.get("excerpt"):
        lines.append(f"  \u201c{o['excerpt']}\u201d")
    return lines


def format_digest(outcomes: list[dict], when: datetime.datetime) -> dict:
    """The ONE consolidated push of a batch -> ``{title, body, priority, tags}``.

    Pure: ``outcomes`` is the per-ticker list ``run_batch`` built. Model-written
    text is cleaned by ``textsafe`` at parse time, so no URL can appear here."""
    ok = [o for o in outcomes if o["status"] == "ok"]
    bad = [o for o in outcomes if o["status"] != "ok"]
    changed = [o for o in ok if (o.get("change") or {}).get("kind") == "rating"]
    soft = [o for o in ok if (o.get("change") or {}).get("kind") in ("action",
                                                                     "band")]
    if bad:
        what = ", ".join(f"{o['ticker']} ({textsafe.clean_text(o.get('reason'), 40)})"
                         for o in bad)
        title = f"\u26a0\ufe0f digest incomplete: {what}"[:140]
        priority, tags = 4, "warning"
    elif changed:
        first = changed[0]
        title = (f"\u26a0\ufe0f Digest: {first['ticker']} "
                 f"{first['change']['parts'][0]}"
                 + (f" (+{len(changed) - 1} more)" if len(changed) > 1 else ""))
        priority, tags = 5, "rotating_light"
    elif soft:
        title = (f"\U0001f4c8 Digest {when:%H:%M}: "
                 f"{', '.join(o['ticker'] for o in soft)} moved band/action")
        priority, tags = 4, "chart_with_upwards_trend"
    else:
        firsts = [o["ticker"] for o in ok
                  if (o.get("change") or {}).get("kind") == "first"]
        title = (f"\U0001f4c8 Digest {when:%H:%M}: "
                 + (f"first tracked advice for {', '.join(firsts)}" if firsts
                    else f"{len(ok)} ticker(s), no change"))
        priority, tags = 3, "newspaper"
    if bad and changed:
        priority = 5
    lines: list[str] = []
    for o in outcomes:
        lines.extend(_ticker_lines(o))
        lines.append("")
    body = "\n".join(lines).strip()
    if len(body) > PUSH_BODY_CHARS:
        body = body[:PUSH_BODY_CHARS - 1].rstrip() + "\u2026"
    return {"title": title, "body": body, "priority": priority, "tags": tags}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def advice_history() -> dict:
    """Latest tracked advice per ticker (for the dashboard digest page)."""
    state = jsonstore.load(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def _advice_row(o: dict, now: float) -> dict:
    """One advice-log row: everything the scoreboard needs to grade the call
    and to stratify results by model/phase/data quality."""
    pr = o.get("price_at_advice") or {}
    return {
        "ts": now, "date": datetime.date.fromtimestamp(now).isoformat(),
        "ticker": o["ticker"], "rating": o["rating"],
        "excerpt": o.get("excerpt", "")[:200],
        "action": o.get("action"), "score": o.get("score"),
        "confidence": o.get("confidence"),
        "scale_version": o.get("scale_version") or SCALE_VERSION,
        "phase": o.get("phase") or "unknown",
        "lane": o.get("lane"), "model": o.get("model"),
        "priceAtAdvice": pr.get("price"), "priceAsOf": pr.get("as_of"),
        "priceCurrency": pr.get("currency"),
        "data_quality": o.get("data_quality"),
        "report": o.get("report"),
    }


def _commit(outcomes: list[dict], now: float) -> None:
    """Advance advice state and append the advice log - called ONLY after the
    batch push was delivered."""
    state = advice_history()
    rows = jsonstore.load(ADVICE_LOG, [])
    rows = rows if isinstance(rows, list) else []
    for o in outcomes:
        if o["status"] != "ok":
            continue
        state[o["ticker"]] = {
            "rating": o["rating"], "excerpt": o.get("excerpt", ""),
            "report": o.get("report"), "ts": now,
            "date": datetime.date.fromtimestamp(now).isoformat(),
            "action": o.get("action"), "score": o.get("score"),
            "confidence": o.get("confidence"),
            "scale_tier": o.get("scale_tier"),
            "lane": o.get("lane"), "model": o.get("model"),
        }
        rows.append(_advice_row(o, now))
    if not jsonstore.save(STATE_FILE, state, indent=2):
        log.warning("digest: advice state write failed")
    if not jsonstore.save(ADVICE_LOG, rows[-ADVICE_LOG_CAP:]):
        log.warning("digest: advice log write failed")


# --------------------------------------------------------------------------
# running a batch
# --------------------------------------------------------------------------

async def lane_gate() -> tuple[bool, str]:
    """Can an unattended run go now? -> (ready, reason-if-not).

    The lane probe is cached for a few seconds and a lane switch is picked up
    without restarting this pod. The digest's own allowance is checked here and
    again per job (each job reserves its turns before its first call)."""
    if await lane_client.active_lane() is None:
        return False, "no lane is serving its configured analysis model"
    b = lane_client.budget_state("digest")
    if b["left"] <= 0:
        return False, (f"autonomous turn budget spent "
                       f"({b['used']}/{b['cap']} today)")
    return True, ""


async def run_one(ticker: str, c: dict) -> tuple[dict | None, str]:
    """Enqueue one quick run and poll the in-process queue until it settles.
    -> (job, "") on success, (None, reason) otherwise."""
    if not lane_client.can_start("digest", 1):
        return None, "budget: digest allowance spent"
    enq = jobs.enqueue(ticker, "quick", source="digest")
    job_id = enq["job_id"]
    log.info("%s: job %s %s", ticker, job_id,
             f"deduped onto an in-flight {enq['mode']} run" if enq["deduped"]
             else "queued")
    deadline = time.time() + c["run_timeout_min"] * 60
    while True:
        job = jobs.get(job_id) or {}
        status = job.get("status")
        if status == "done":
            return job, ""
        if status == "error":
            reason = str(job.get("message") or "job failed")[:160]
            log.warning("%s: job failed: %s", ticker, reason)
            return None, reason
        if time.time() >= deadline:
            break
        await asyncio.sleep(c["status_poll_s"])
    log.warning("%s: job %s still %s after %dmin", ticker, job_id,
                (jobs.get(job_id) or {}).get("status", "gone"),
                c["run_timeout_min"])
    return None, f"timeout: no result after {c['run_timeout_min']} min"


async def _price_context(ticker: str, parsed: dict) -> tuple[dict, dict, str]:
    """-> (display price, price-at-advice, phase).

    Display: ``portfolio.position_for`` (EUR, with the day change), else the
    listing quote. Price at advice: the price the analysis itself saw (the
    report's evidence quote, EUR), else the listing quote now. Phase: whether
    the listing's market was open when the advice was given."""
    quote = None
    try:
        quote = await prices.listing_quote(ticker)
    except Exception as e:
        log.warning("%s: listing quote failed: %s", ticker, e)
    pos = None
    try:
        pos = portfolio.position_for(ticker)
    except Exception as e:
        log.warning("%s: position lookup failed: %s", ticker, e)
    q = quote or {}
    display = {"price": None, "day_pct": None, "as_of": None, "stale": False}
    if pos and _num(pos.get("priceEur")) is not None:
        display.update(price=_num(pos["priceEur"]), day_pct=_num(pos.get("dayPct")),
                       as_of=_epoch(pos.get("priceAsOf")),
                       stale=bool(pos.get("stale")))
    elif _num(q.get("price")) is not None:
        display.update(price=_num(q["price"]), day_pct=_num(q.get("changePct")),
                       as_of=_epoch(q.get("asOf")), stale=bool(q.get("stale")))
    advice = {"price": None, "as_of": None, "currency": None}
    rp = parsed.get("report_price")
    if rp and str(parsed.get("report_price_currency") or "EUR").upper() == "EUR" \
            and parsed.get("report_price_asof"):
        advice = {"price": rp, "as_of": parsed["report_price_asof"],
                  "currency": "EUR"}
    elif _num(q.get("price")) is not None and _epoch(q.get("asOf")):
        advice = {"price": _num(q["price"]), "as_of": _epoch(q["asOf"]),
                  "currency": str(q.get("currency") or "EUR").upper()}
    phase = "unknown"
    if quote is not None and "marketOpen" in q:
        phase = "market_open" if q["marketOpen"] else "market_closed"
    return display, advice, phase


async def _attempt(ticker: str, c: dict, prev: dict | None) -> dict:
    """One run of one ticker -> an outcome dict (never raises)."""
    out = {"ticker": ticker, "status": "failed", "reason": "", "retryable": True}
    try:
        job, reason = await run_one(ticker, c)
        if job is None:
            out["reason"] = reason
            out["retryable"] = not reason.startswith("budget")
            return out
        report = job.get("result_path") or ""
        parsed = parse_report(report, c["digest_excerpt_chars"])
        if parsed["rating"] not in VALID_RATINGS:
            out["reason"] = "rating unreadable in the report"
            return out
        display, advice_price, phase = await _price_context(ticker, parsed)
    except (ValueError, OSError) as e:
        out["reason"] = f"report unreadable: {str(e)[:100]}"
        return out
    except Exception as e:                         # a ticker must never kill the batch
        log.exception("%s: digest attempt crashed", ticker)
        out["reason"] = f"{type(e).__name__}: {str(e)[:100]}"
        return out
    out.update(parsed)
    out.update(status="ok", reason="", retryable=False, report=report,
               lane=job.get("lane") or parsed.get("lane"),
               model=job.get("model") or parsed.get("model"),
               price=display, price_at_advice=advice_price, phase=phase,
               change=detect_change(prev, parsed))
    return out


async def _run_ticker(ticker: str, c: dict, prev: dict | None) -> dict:
    """``_attempt`` plus ONE retry after ``retry_delay_s`` for a failure that
    can plausibly heal (a lane blip, a truncated PM answer), not for a spent
    budget."""
    out = await _attempt(ticker, c, prev)
    if out["status"] == "ok" or not out["retryable"]:
        return out
    log.warning("%s: failed (%s) - retrying once in %ss", ticker, out["reason"],
                c["retry_delay_s"])
    await asyncio.sleep(max(0, c["retry_delay_s"]))
    again = await _attempt(ticker, c, prev)
    again["retried"] = True
    if again["status"] != "ok":
        again["reason"] = again["reason"] or out["reason"]
    return again


def _targets(tickers: list[str] | None) -> list[str]:
    pool = registry.holdings() if tickers is None else list(tickers)
    seen: set[str] = set()
    out = []
    for t in pool:
        if t not in seen and prices.is_analyzable(t):
            seen.add(t)
            out.append(t)
    return out


async def run_batch(tickers: list[str] | None = None) -> dict:
    """One digest batch -> ``{ok, deferred, reason, outcomes, pushed}``.

    ``outcomes`` is ``{ticker: outcome}`` (status ok|failed with its reason);
    ``ok`` is True only when every ticker ran AND the push was not lost;
    ``deferred`` means a gate was closed and nothing ran (the caller retries)."""
    c = cfg()
    todo = _targets(tickers)
    log.info("digest run: %s", ", ".join(todo) or "(no analysable holding)")
    _stats["last_run"] = time.time()
    if not todo:
        return {"ok": True, "deferred": False, "reason": "no analysable holding",
                "outcomes": {}, "pushed": None}
    ready, reason = await lane_gate()
    if not ready:
        log.warning("digest batch deferred: %s", reason)
        _stats["skip_reason"] = reason
        return {"ok": False, "deferred": True, "reason": reason,
                "outcomes": {}, "pushed": None}
    _stats["skip_reason"] = ""
    state = advice_history()
    outcomes: dict[str, dict] = {}
    for ticker in todo:
        outcomes[ticker] = await _run_ticker(ticker, c, state.get(ticker))
    ordered = [outcomes[t] for t in todo]
    now = time.time()
    msg = format_digest(ordered, datetime.datetime.fromtimestamp(now))
    delivery = await notify.push(msg["title"], msg["body"],
                                 priority=msg["priority"], tags=msg["tags"])
    ok_n = sum(1 for o in ordered if o["status"] == "ok")
    if delivery.status == "failed":
        # Nothing was delivered or queued: keep state and log where they were,
        # so the next run reports the same changes again.
        log.error("digest push failed (%s): advice state NOT advanced",
                  delivery.reason)
        _stats["errors"] += 1
    else:
        _commit(ordered, now)
        _stats["runs"] += ok_n
        if delivery:
            for o in ordered:
                if o["status"] == "ok":
                    notify.store(o["ticker"], "analysis",
                                 f"{o['ticker']} {o['rating']}"
                                 + (f" ({o['change']['parts'][0]})"
                                    if o["change"]["kind"] != "none" else ""),
                                 "\n".join(_ticker_lines(o)),
                                 priority=msg["priority"])
    result = {"ok": ok_n == len(ordered) and delivery.status != "failed",
              "deferred": False, "reason": "", "outcomes": outcomes,
              "pushed": delivery.status}
    _stats["last_batch"] = {
        "ts": now, "ok": result["ok"], "pushed": delivery.status,
        "tickers": {t: (o["status"] if o["status"] == "ok" else o["reason"])
                    for t, o in outcomes.items()}}
    return result


def _note(result: dict, label: str) -> str:
    if result.get("deferred"):
        return f"{label}: skipped: {result['reason'] or 'gate not open'}"
    outcomes = result.get("outcomes") or {}
    if not outcomes:
        return f"{label}: {result.get('reason') or 'nothing to run'}"
    bad = [f"{t} ({o['reason']})" for t, o in outcomes.items()
           if o["status"] != "ok"]
    note = f"{label}: {len(outcomes) - len(bad)}/{len(outcomes)} ticker(s) done"
    if bad:
        note += "; failed: " + ", ".join(bad)
    if result.get("pushed") == "failed":
        note += "; PUSH LOST (state not advanced)"
    return note


async def _batch_recorded(label: str, tickers: list[str] | None = None) -> dict:
    """``run_batch`` + its worker-run row: ok=False when any ticker failed,
    the batch was skipped, or the push was lost."""
    started = time.time()
    try:
        result = await run_batch(tickers)
    except Exception as e:
        _stats["errors"] += 1
        runlog.record("digest", False, time.time() - started,
                      f"{label} batch failed: {str(e)[:160]}")
        log.exception("%s digest batch failed", label)
        return {"ok": False, "deferred": False, "reason": str(e),
                "outcomes": {}, "pushed": None}
    runlog.record("digest", bool(result["ok"]), time.time() - started,
                  _note(result, label))
    return result


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def _seconds_until_next(times: list[str]) -> float:
    """Seconds until the next configured local HH:MM (today or tomorrow)."""
    now = datetime.datetime.now()
    best: datetime.datetime | None = None
    for hhmm in times:
        try:
            hh, mm = (int(x) for x in hhmm.split(":"))
        except ValueError:
            log.warning("digest: bad time %r ignored", hhmm)
            continue
        when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now:
            when += datetime.timedelta(days=1)
        if best is None or when < best:
            best = when
    _stats["next_run"] = (time.time() + max(0.0, (best - now).total_seconds())
                          if best else 0.0)
    return max(30.0, (best - now).total_seconds()) if best else 3600.0


async def _run_with_gate_retries(label: str, tickers: list[str] | None) -> None:
    """A lane that is mid-switch (another engine holds the VRAM) is the normal
    skip reason: retry a few times before giving up until the next slot, and
    say so ONCE when the last attempt is still deferred."""
    result: dict = {}
    for attempt in range(4):
        result = await _batch_recorded(label, tickers)
        if not result.get("deferred"):
            return
        log.info("digest: batch deferred, retry %d/4 in %ds", attempt + 1,
                 LANE_RETRY_S)
        await asyncio.sleep(LANE_RETRY_S)
    await notify.push("\u26a0\ufe0f digest not run",
                      f"{label} digest could not start: "
                      f"{textsafe.clean_text(result.get('reason'), 200)}",
                      priority=4, tags="warning")


async def _loop() -> None:
    log.info("digest loop started (times=%s local)", cfg()["times"])
    while True:
        await asyncio.sleep(_seconds_until_next(cfg()["times"]))
        await _run_with_gate_retries("scheduled", None)
        # Avoid a double-fire if the batch finished within the same minute.
        await asyncio.sleep(90)


async def _resume(tickers: list[str]) -> None:
    """Re-run the digest jobs a restart cut off (see ``jobs.take_interrupted``),
    after a short settle delay so the lane probe and the quote caches are warm.
    The consolidated push covers just these tickers."""
    await asyncio.sleep(cfg()["resume_delay_s"])
    log.info("digest: resuming %s interrupted by the restart", tickers)
    await _run_with_gate_retries("resumed", tickers)


def start() -> None:
    global _task, _resume_task
    if os.getenv("DIGEST_ENABLED", "0") != "1":
        log.info("digest disabled (set DIGEST_ENABLED=1)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True
    c = cfg()
    interrupted = [j["ticker"] for j in jobs.take_interrupted(
        "digest", float(c["resume_max_age_h"]) * 3600)]
    if interrupted:
        _resume_task = asyncio.create_task(_resume(interrupted))


async def stop() -> None:
    global _task, _resume_task
    _stats["running"] = False
    for t in (_task, _resume_task):
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    _task = _resume_task = None


def status() -> dict:
    return {**_stats, "enabled": os.getenv("DIGEST_ENABLED", "0") == "1",
            "manual_running": bool(_manual and not _manual.done()),
            "resuming": bool(_resume_task and not _resume_task.done())}


_manual: asyncio.Task | None = None


async def request_run() -> tuple[int, str]:
    """Start a digest batch now. The loop is time-based only, so this is the
    only way to prove the pipeline end to end without waiting for 08:45."""
    global _manual
    if os.getenv("DIGEST_ENABLED", "0") != "1":
        return 409, "digest disabled (DIGEST_ENABLED != 1)"
    if _manual and not _manual.done():
        return 409, "a digest batch is already running"
    _manual = asyncio.create_task(_batch_recorded("manual"))
    return 202, "digest batch started"
