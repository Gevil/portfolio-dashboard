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
* hysteresis (``resolve``): the candidate (rating, action, band) is compared
  with the PUBLISHED state. A different candidate is only ``pending`` until it
  is confirmed: the same candidate on the next run, or a score at least
  ``STRONG_MOVE_PTS`` beyond the edge of the band it crossed into. A
  candidate equal to the published state clears the pending one, so A->B->A
  never pushes and A->B->B pushes once;
* drafts: a job answered by a lane outside ``DIGEST_PUBLISH_LANES`` (comma
  list; default: the first lane in the preference order) is a draft. It is
  logged for the scoreboard but never confirms or publishes anything;
* push policy (``format_digest``): only a batch with a confirmed change (or a
  failure) is pushed - priority 3 for a confirmed change, 4 for a confirmed
  move INTO the sell family or a score move of ``BIG_DELTA_PTS`` or more, never
  5. A quiet batch is only stored (info) unless ``DIGEST_PUSH_NO_CHANGE=1``.
  The batch push carries ``source="analysis"``, the "digest not run" notice
  ``source="ops"``;
* advice state and the advice log advance only after the push was delivered
  (``Delivery.status != 'failed'``) - or at once when nothing was pushed - so
  an undelivered change is reported again next run instead of being lost;
* no model-written URL reaches a push (``textsafe``).

Two gates keep an unattended batch honest: a lane must be serving, and the
digest's turn allowance in the daily budget must have room. A batch that fails
a gate is deferred and retried in ``LANE_RETRY_S``; the deferral is recorded in
the worker-run ring as ok=True with a note that starts ``deferred:`` (a real
failure stays ok=False). The scheduled slots run on trading days only: a slot
where no analysable holding's venue has a session today (``calendars``) is
recorded ``deferred: no session today``. A batch interrupted by a restart is
re-run for the tickers that did not finish (``_resume``). Enabled only when
``DIGEST_ENABLED=1``. Container-local time is used for the schedule: run with
``TZ=Europe/Prague``.

Event-driven deep runs: at a scheduled slot (``run_batch(event_driven=True)``)
a held ticker runs mode ``deep`` instead of ``quick`` when, since its last
digest run (the state row ``ts``), (a) its earnings date is today or was
yesterday (macro's cache, never a network scan), (b) a new 8-K / 6-K appears
in ``filings.recent`` / ``edgar.recent`` (newer than the state row's
``filingMark``), or (c) ``triage.recent`` holds a verdict with relevance >=
``DEEP_TRIAGE_RELEVANCE`` for it. At most one deep run per ticker per local
day (state ``deepDate``); it is charged as purpose ``digest`` and falls back to
quick when ``lane_client.can_start('digest', ta_pipeline.JOB_TURNS['deep'])``
is False. The fired trigger (``earnings`` | ``filing`` | ``triage`` | None) is
recorded on the outcome, the state row, the advice-log row and the push line.
``ta_pipeline._playbook_for`` picks ``deep_earnings`` itself for a deep run
with an earnings date 0..10 days ahead; the digest passes only mode + source.

Tax tag: a CONFIRMED move INTO the sell family of a ticker whose
``tax.sell_tax_status`` is ``taxable is True`` (config ``taxResidency`` ==
``CZ``) adds ``taxTag`` to the state/advice rows and a line to the push.
"""
import asyncio
import datetime
import importlib
import inspect
import logging
import math
import os
import pathlib
import re
import time

from app.api import (calendars, config_store, edgar, filings, jobs, jsonstore,
                     lane_client, macro, notify, portfolio, prices, registry,
                     reports, runlog, textsafe, triage)

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

# ds-v1 decision bands (mirrors ``ta_pipeline._band``; a test pins them
# together): the lowest score of each tier, weakest first.
BAND_FLOORS = (("sell", 0), ("reduce", 20), ("watch", 40), ("buy", 60),
               ("strong_buy", 80))
BAND_ORDER = [tier for tier, _ in BAND_FLOORS]
BAND_FLOOR = dict(BAND_FLOORS)
# A change crossing into a band is confirmed at once when the score is this far
# past the edge it crossed; a score move this big (or one INTO the sell family)
# raises the push priority to 4. The priority is never 5.
STRONG_MOVE_PTS = 10
BIG_DELTA_PTS = 15
SELL_FAMILY = ("sell", "reduce", "trim", "avoid")
P20_MIN, P20_MAX = 0.01, 0.99
# notify.store rows of an unpushed batch: only these states are worth a row.
STORE_STATES = ("confirmed", "first", "pending", "draft")
# Event-driven deep runs (see the module docstring).
DEEP_FORMS = ("8-K", "6-K")
DEEP_TRIAGE_RELEVANCE = 0.85

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


def _clip_p20(value) -> float | None:
    """The pipeline already clips ``p_outperform_20d``; this is the belt for a
    report written by another build: None or a probability in 0.01-0.99."""
    v = _num(value)
    return None if v is None else round(min(P20_MAX, max(P20_MIN, v)), 4)


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
        "p20": _clip_p20(v2.get("p_outperform_20d")),
        "expExcess20Pct": _num(v2.get("expected_excess_20d_pct")),
        "benchmark": (str(v2.get("benchmark") or "").strip() or None),
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
# hysteresis: candidate -> pending -> published
# --------------------------------------------------------------------------

def publish_lanes() -> list[str]:
    """Lanes whose answers may publish (``DIGEST_PUBLISH_LANES``, comma list);
    default: the first lane of the ``lane_client`` preference order."""
    names = [p.strip() for p in os.getenv("DIGEST_PUBLISH_LANES", "").split(",")
             if p.strip()]
    if names:
        return names
    pref = lane_client.preference() or lane_client.DEFAULT_PREFERENCE.split(",")
    return [pref[0].strip()]


def push_no_change() -> bool:
    """``DIGEST_PUSH_NO_CHANGE=1``: also push batches without a confirmed change."""
    return os.getenv("DIGEST_PUSH_NO_CHANGE", "0") == "1"


def band_of(score) -> str | None:
    """The ds-v1 tier of a score, None when it is not a number."""
    s = _num(score)
    if s is None:
        return None
    return [tier for tier, floor in BAND_FLOORS if s >= floor][-1]


def candidate(row: dict | None) -> tuple:
    """(rating, action, band) - what a change is detected and confirmed on."""
    row = row or {}
    return (row.get("rating"), row.get("action"), row.get("scale_tier"))


def published_of(row: dict | None) -> dict | None:
    """The PUBLISHED state of a stored advice row: its ``published`` block, or
    - a row written before hysteresis, which was published by definition - the
    row itself. None when nothing was ever published."""
    if not isinstance(row, dict) or not row:
        return None
    if "published" in row:
        pub = row["published"]
        return pub if isinstance(pub, dict) and pub.get("rating") else None
    return row if row.get("rating") else None


def beyond_band_edge(pub: dict, cur: dict, pts: int = STRONG_MOVE_PTS) -> bool:
    """True when ``cur``'s score lies >= ``pts`` beyond the edge of the band it
    crossed into: up into a band = its lowest score + pts; down into a band =
    the lowest score of the band above it - pts. No band crossed (or no score)
    is never a strong move."""
    score = _num(cur.get("score"))
    if score is None:
        return False
    new = cur.get("scale_tier") if cur.get("scale_tier") in BAND_FLOOR else band_of(score)
    old = pub.get("scale_tier")
    if old not in BAND_FLOOR:
        old = band_of(pub.get("score"))
    if new is None or old is None or new == old:
        return False
    ni, oi = BAND_ORDER.index(new), BAND_ORDER.index(old)
    if ni > oi:
        return score >= BAND_FLOOR[new] + pts
    return score <= BAND_FLOOR[BAND_ORDER[ni + 1]] - pts


def resolve(prev_row: dict | None, cur: dict, *, draft: bool, now: float) -> dict:
    """Hysteresis for one ticker's new answer ``cur`` (a ``parse_decision`` dict
    plus lane/model) against its stored row.

    -> ``{state, change, confirmed, publish, pending, prev, why}``:
    ``state`` is draft | first | unchanged | confirmed | pending; ``publish`` says
    whether the published state advances to ``cur``; ``confirmed`` is the advice
    log flag (the row's rating/action/band IS the published state); ``pending``
    is the unconfirmed candidate to store (None clears it); ``prev`` is the
    published state it was compared with; ``why`` names the confirmation
    (``repeat`` | ``strong``)."""
    pub = published_of(prev_row)
    pending = (prev_row or {}).get("pending") if isinstance(prev_row, dict) else None
    pending = pending if isinstance(pending, dict) and pending.get("rating") else None
    prev = ({k: pub.get(k) for k in ("rating", "action", "scale_tier", "score")}
            if pub else None)
    if draft:
        who = cur.get("lane") or "an unlisted lane"
        return {"state": "draft", "confirmed": False, "publish": False,
                "pending": pending, "prev": prev, "why": "",
                "change": {"kind": "draft", "parts": [
                    f"draft answer from {who}: not published"]}}
    if pub is None:
        return {"state": "first", "confirmed": True, "publish": True,
                "pending": None, "prev": None, "why": "",
                "change": detect_change(None, cur)}
    ch = detect_change(pub, cur)
    if ch["kind"] not in ("rating", "action", "band"):
        return {"state": "unchanged", "confirmed": True, "publish": True,
                "pending": None, "prev": prev, "why": "", "change": ch}
    why = ""
    if pending is not None and candidate(pending) == candidate(cur):
        why = "repeat"
    elif beyond_band_edge(pub, cur):
        why = "strong"
    if why:
        return {"state": "confirmed", "confirmed": True, "publish": True,
                "pending": None, "prev": prev, "why": why, "change": ch}
    held = {"rating": cur.get("rating"), "action": cur.get("action"),
            "scale_tier": cur.get("scale_tier"), "score": cur.get("score"),
            "since": datetime.date.fromtimestamp(now).isoformat(), "ts": now}
    return {"state": "pending", "confirmed": False, "publish": False,
            "pending": held, "prev": prev, "why": "",
            "change": {"kind": "pending", "parts": ch["parts"]}}


def _sells(action, rating) -> bool:
    """In the sell family: a sell-family action, else (no action on a pre-v2
    row) a SELL rating."""
    return action in SELL_FAMILY if action else rating == "SELL"


def change_priority(o: dict) -> int:
    """Push priority of one CONFIRMED change: 4 when it moves INTO the sell
    family or the score moved ``BIG_DELTA_PTS`` or more, else 3. Never 5."""
    prev = o.get("prev") or {}
    into_sell = (_sells(o.get("action"), o.get("rating"))
                 and not _sells(prev.get("action"), prev.get("rating")))
    a, b = _num(prev.get("score")), _num(o.get("score"))
    big = a is not None and b is not None and abs(b - a) >= BIG_DELTA_PTS
    return 4 if into_sell or big else 3


# --------------------------------------------------------------------------
# event-driven deep runs and the tax tag
# --------------------------------------------------------------------------

def _iso_day(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _cached_earnings_date(ticker: str) -> str | None:
    """The ticker's date in macro's earnings cache (ISO), no network."""
    cache = jsonstore.load(macro.EARNINGS_CACHE, {})
    row = cache.get(ticker.upper()) if isinstance(cache, dict) else None
    day = _iso_day(row.get("date")) if isinstance(row, dict) else None
    return day.isoformat() if day else None


def _filing_dates(ticker: str) -> list[str]:
    """ISO dates of the ticker's known 8-K / 6-K filings (any source)."""
    out: list[str] = []
    for source in (filings.recent, edgar.recent):
        try:
            rows = source(ticker)
        except Exception as e:
            log.warning("%s: filing lookup failed: %s", ticker, e)
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            form = str(row.get("form") or "").upper().split("/")[0]
            day = _iso_day(row.get("filed") or row.get("date"))
            if form in DEEP_FORMS and day:
                out.append(day.isoformat())
    return out


def _triage_hit(ticker: str, since: float) -> bool:
    try:
        rows = triage.recent(triage.LOG_RING)
    except Exception as e:
        log.warning("%s: triage log lookup failed: %s", ticker, e)
        return False
    sym = ticker.upper()
    for row in rows:
        rel, ts = _num(row.get("relevance")), _num(row.get("ts"))
        if (str(row.get("ticker") or "").upper() == sym and rel is not None
                and rel >= DEEP_TRIAGE_RELEVANCE and ts is not None
                and ts > since):
            return True
    return False


def deep_trigger(ticker: str, prev_row: dict | None, now: float) -> dict:
    """Which event, if any, calls for a deep run of ``ticker``.

    -> ``{trigger, filing_mark, earnings_date}``: ``trigger`` is earnings |
    filing | triage | None (checked in that order); ``filing_mark`` (newest
    8-K/6-K date known now) and ``earnings_date`` are stored on the state row
    so the next run compares against them. Filing and triage events need a
    previous run to be "new since"; earnings is date-based."""
    prev = prev_row if isinstance(prev_row, dict) else {}
    today = datetime.date.fromtimestamp(now)
    near = {today.isoformat(), (today - datetime.timedelta(days=1)).isoformat()}
    since = _num(prev.get("ts")) if prev else None
    cached = _cached_earnings_date(ticker)
    known_earnings = {d for d in (cached, prev.get("earningsDate")) if d}
    dates = _filing_dates(ticker)
    mark = max(dates) if dates else None
    trigger = None
    if known_earnings & near:
        trigger = "earnings"
    elif since is not None and mark is not None:
        floor = (_iso_day(prev.get("filingMark"))
                 or datetime.date.fromtimestamp(since))
        if any(d > floor.isoformat() for d in dates):
            trigger = "filing"
    if trigger is None and since is not None and _triage_hit(ticker, since):
        trigger = "triage"
    return {"trigger": trigger, "filing_mark": mark,
            "earnings_date": cached or prev.get("earningsDate")}


def deep_turns() -> int | None:
    """Turns one deep job holds (``ta_pipeline.JOB_TURNS``); None when the
    pipeline cannot be imported."""
    try:
        return int(importlib.import_module("app.api.ta_pipeline").JOB_TURNS["deep"])
    except Exception as e:
        log.warning("deep turns unavailable: %s", e)
        return None


def plan_run(ticker: str, prev_row: dict | None, now: float) -> dict:
    """-> ``{mode, trigger, deep_skipped, filing_mark, earnings_date}`` for one
    scheduled run. ``mode`` is ``deep`` only when a trigger fired, no deep run
    of the ticker happened today and the digest budget has room for it;
    otherwise quick, with ``deep_skipped`` = ``daily cap`` | ``budget``."""
    plan = {"mode": "quick", "trigger": None, "deep_skipped": "",
            "filing_mark": None, "earnings_date": None}
    try:
        plan.update(deep_trigger(ticker, prev_row, now))
    except Exception:
        log.exception("%s: deep trigger check failed", ticker)
        return plan
    if plan["trigger"] is None:
        return plan
    today = datetime.date.fromtimestamp(now).isoformat()
    if isinstance(prev_row, dict) and prev_row.get("deepDate") == today:
        plan["deep_skipped"] = "daily cap"
        return plan
    turns = deep_turns()
    if turns is None or not lane_client.can_start("digest", turns):
        plan["deep_skipped"] = "budget"
        return plan
    plan["mode"] = "deep"
    return plan


async def _tax_status(ident: str) -> dict | None:
    """``tax.sell_tax_status(ident)``, imported lazily; None when the module
    is missing or the call fails."""
    try:
        tax = importlib.import_module("app.api.tax")
        res = tax.sell_tax_status(ident)
        if inspect.isawaitable(res):
            res = await res
    except ImportError:
        return None
    except Exception as e:
        log.warning("%s: tax status failed: %s", ident, e)
        return None
    return res if isinstance(res, dict) else None


async def _tax_tag(ticker: str, cur: dict, r: dict) -> str | None:
    """The push/state tag for a CONFIRMED move into the sell family of a
    Czech tax resident's still-taxable lots, else None."""
    if r.get("state") != "confirmed" or not _sells(cur.get("action"),
                                                    cur.get("rating")):
        return None
    prev = r.get("prev") or {}
    if _sells(prev.get("action"), prev.get("rating")):
        return None
    if config_store.read().get("taxResidency") != "CZ":
        return None
    st = await _tax_status(ticker)
    if not st or st.get("taxable") is not True:
        return None
    unlock = textsafe.clean_text(st.get("unlockDate") or "", 10)
    return "taxable: lots < 3y" + (f" (unlock {unlock})" if unlock else "")


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
    if o.get("draft"):
        verdict.append("DRAFT")
    lines.append("  " + " \u00b7 ".join(verdict))
    if o.get("trigger"):
        how = ("deep run" if o.get("mode") == "deep"
               else f"quick run, deep skipped: {o.get('deep_skipped') or 'n/a'}")
        lines.append(f"  event: {o['trigger']} -> {how}")
    if o.get("p20") is not None:
        odds = f"  p(outperform 20d) {o['p20']:.0%}"
        if _num(o.get("expExcess20Pct")) is not None:
            odds += f" \u00b7 expected excess {o['expExcess20Pct']:+.1f}pp"
        if o.get("benchmark"):
            odds += f" vs {o['benchmark']}"
        lines.append(odds)
    if o.get("stop") is not None or o.get("target") is not None:
        lines.append(f"  stop {_eur(o.get('stop'))} \u00b7 target "
                     f"{_eur(o.get('target'))}")
    ch = o.get("change") or {}
    parts = "; ".join(ch.get("parts") or ["no change"])
    if o.get("state") == "pending":
        lines.append(f"  pending, not published: {parts} (confirms on a "
                     f"matching next run or a {STRONG_MOVE_PTS}+ point move "
                     "past the band edge)")
    elif o.get("state") == "draft":
        lines.append(f"  {parts}")
    elif ch:
        lines.append("  changed: " + parts)
    if o.get("taxTag"):
        lines.append("  " + o["taxTag"])
    gaps = list(o.get("missing") or []) or list(o.get("gaps") or [])
    if gaps:
        lines.append(f"  gaps: {', '.join(gaps[:4])}"
                     + (f" (data quality {o['data_quality']})"
                        if o.get("data_quality") else ""))
    if o.get("excerpt"):
        lines.append(f"  \u201c{o['excerpt']}\u201d")
    return lines


def format_digest(outcomes: list[dict], when: datetime.datetime) -> dict:
    """The ONE consolidated message of a batch -> ``{title, body, priority,
    tags, push, source}``.

    A confirmed change (state confirmed|first) is pushed as ``analysis`` at
    priority 3 or 4; a batch with only failures as ``ops`` at 3; a quiet batch
    at 2, pushed only with ``DIGEST_PUSH_NO_CHANGE=1``. Never priority 5.
    Pure apart from that env read; model-written text was cleaned by
    ``textsafe`` at parse time, so no URL can appear here."""
    ok = [o for o in outcomes if o["status"] == "ok"]
    bad = [o for o in outcomes if o["status"] != "ok"]
    confirmed = [o for o in ok if o.get("state") in ("confirmed", "first")]
    if confirmed:
        first = confirmed[0]
        parts = (first.get("change") or {}).get("parts") or ["changed"]
        title = (f"\U0001f4c8 Digest: {first['ticker']} {parts[0]}"
                 + (f" (+{len(confirmed) - 1} more)" if len(confirmed) > 1 else ""))
        priority = max(change_priority(o) for o in confirmed)
        tags = "rotating_light" if priority >= 4 else "chart_with_upwards_trend"
        push, source = True, "analysis"
    elif bad:
        what = ", ".join(f"{o['ticker']} ({textsafe.clean_text(o.get('reason'), 40)})"
                         for o in bad)
        title = f"\u26a0\ufe0f digest incomplete: {what}"
        priority, tags, push, source = 3, "warning", True, "ops"
    else:
        title = f"\U0001f4c8 Digest {when:%H:%M}: {len(ok)} ticker(s), no change"
        priority, tags, push, source = 2, "newspaper", push_no_change(), "analysis"
    lines: list[str] = []
    for o in outcomes:
        lines.extend(_ticker_lines(o))
        lines.append("")
    body = "\n".join(lines).strip()
    if len(body) > PUSH_BODY_CHARS:
        body = body[:PUSH_BODY_CHARS - 1].rstrip() + "\u2026"
    return {"title": title[:140], "body": body, "priority": priority,
            "tags": tags, "push": push, "source": source}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def _load_state() -> dict:
    """The raw advice state file (every stored row, unfiltered)."""
    state = jsonstore.load(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def _published_block(row: dict | None) -> dict | None:
    """The ``published`` block of a stored row: its own, or - for a legacy row
    written before hysteresis, published by definition - one derived from the
    row itself. None when nothing was ever published."""
    if not isinstance(row, dict) or not row:
        return None
    if "published" in row:
        pub = row["published"]
        return pub if isinstance(pub, dict) and pub.get("rating") else None
    if not row.get("rating"):
        return None
    return {"rating": row.get("rating"), "action": row.get("action"),
            "scale_tier": row.get("scale_tier"), "score": row.get("score"),
            "date": row.get("date"), "report": row.get("report"),
            "ts": row.get("ts")}


def advice_history() -> dict:
    """Latest tracked advice per analysable ticker (the ``/api/advice`` view).

    Every value carries ``published`` ({rating, action, score, date} or None,
    derived for a legacy row), ``pending`` ({rating, action, score, since} or
    None), ``p20``, ``expExcess20Pct`` and ``draft`` (bool)."""
    out = {}
    for ident, row in _load_state().items():
        if not isinstance(row, dict) or not registry.is_analyzable(ident):
            continue
        pub = _published_block(row)
        pend = row.get("pending")
        pend = pend if isinstance(pend, dict) and pend.get("rating") else None
        out[ident] = {
            **row,
            "published": ({k: pub.get(k) for k in ("rating", "action", "score",
                                                   "date")} if pub else None),
            "pending": ({k: pend.get(k) for k in ("rating", "action", "score",
                                                  "since")} if pend else None),
            "p20": row.get("p20"),
            "expExcess20Pct": row.get("expExcess20Pct"),
            "draft": bool(row.get("draft")),
        }
    return out


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
        "p20": o.get("p20"), "expExcess20Pct": o.get("expExcess20Pct"),
        "benchmark": o.get("benchmark"), "draft": bool(o.get("draft")),
        "confirmed": bool(o.get("confirmed")), "state": o.get("state"),
        "mode": o.get("mode") or "quick", "trigger": o.get("trigger"),
        "taxTag": o.get("taxTag"),
    }


def _commit(outcomes: list[dict], now: float) -> None:
    """Advance advice state and append the advice log - called ONLY after the
    batch push was delivered (or none was needed). Works on the raw state so no
    stored row is ever dropped; every written row has a ``published`` key."""
    state = _load_state()
    rows = jsonstore.load(ADVICE_LOG, [])
    rows = rows if isinstance(rows, list) else []
    date = datetime.date.fromtimestamp(now).isoformat()
    for o in outcomes:
        prior = state.get(o["ticker"])
        prior = prior if isinstance(prior, dict) else {}
        if o["status"] != "ok":
            continue
        if o.get("publish"):
            published = {"rating": o["rating"], "action": o.get("action"),
                         "scale_tier": o.get("scale_tier"),
                         "score": o.get("score"), "date": date,
                         "report": o.get("report"), "ts": now}
        else:
            published = _published_block(state.get(o["ticker"]))
        state[o["ticker"]] = {
            "rating": o["rating"], "excerpt": o.get("excerpt", ""),
            "report": o.get("report"), "ts": now, "date": date,
            "action": o.get("action"), "score": o.get("score"),
            "confidence": o.get("confidence"),
            "scale_tier": o.get("scale_tier"),
            "lane": o.get("lane"), "model": o.get("model"),
            "published": published, "pending": o.get("pending"),
            "draft": bool(o.get("draft")), "state": o.get("state"),
            "p20": o.get("p20"), "expExcess20Pct": o.get("expExcess20Pct"),
            "benchmark": o.get("benchmark"),
            "mode": o.get("mode") or "quick", "trigger": o.get("trigger"),
            "taxTag": o.get("taxTag"),
            "deepDate": date if o.get("mode") == "deep" else prior.get("deepDate"),
            "filingMark": o.get("filing_mark") or prior.get("filingMark"),
            "earningsDate": o.get("earnings_date") or prior.get("earningsDate"),
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


async def run_one(ticker: str, c: dict, mode: str = "quick") -> tuple[dict | None, str]:
    """Enqueue one run of ``mode`` and poll the in-process queue until it
    settles. -> (job, "") on success, (None, reason) otherwise."""
    need = 1 if mode == "quick" else (deep_turns() or 1)
    if not lane_client.can_start("digest", need):
        return None, "budget: digest allowance spent"
    enq = jobs.enqueue(ticker, mode, source="digest")
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


async def _attempt(ticker: str, c: dict, prev: dict | None,
                   plan: dict | None = None) -> dict:
    """One run of one ticker -> an outcome dict (never raises)."""
    plan = plan or {}
    mode = plan.get("mode") or "quick"
    out = {"ticker": ticker, "status": "failed", "reason": "", "retryable": True,
           "mode": mode, "trigger": plan.get("trigger"),
           "deep_skipped": plan.get("deep_skipped") or "",
           "filing_mark": plan.get("filing_mark"),
           "earnings_date": plan.get("earnings_date")}
    try:
        job, reason = await run_one(ticker, c, mode)
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
    lane = job.get("lane") or parsed.get("lane")
    draft = bool(lane) and lane not in publish_lanes()
    r = resolve(prev, {**parsed, "lane": lane}, draft=draft, now=time.time())
    try:
        tax_tag = await _tax_tag(ticker, parsed, r)
    except Exception:                              # the tag is garnish, never a failure
        log.exception("%s: tax tag failed", ticker)
        tax_tag = None
    out.update(parsed)
    out.update(status="ok", reason="", retryable=False, report=report,
               lane=lane,
               model=job.get("model") or parsed.get("model"),
               price=display, price_at_advice=advice_price, phase=phase,
               change=r["change"], state=r["state"], prev=r["prev"],
               confirmed=r["confirmed"], publish=r["publish"],
               pending=r["pending"], why=r["why"], draft=draft, taxTag=tax_tag)
    return out


async def _run_ticker(ticker: str, c: dict, prev: dict | None,
                      plan: dict | None = None) -> dict:
    """``_attempt`` plus ONE retry after ``retry_delay_s`` for a failure that
    can plausibly heal (a lane blip, a truncated PM answer), not for a spent
    budget."""
    out = await _attempt(ticker, c, prev, plan)
    if out["status"] == "ok" or not out["retryable"]:
        return out
    log.warning("%s: failed (%s) - retrying once in %ss", ticker, out["reason"],
                c["retry_delay_s"])
    await asyncio.sleep(max(0, c["retry_delay_s"]))
    again = await _attempt(ticker, c, prev, plan)
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


async def run_batch(tickers: list[str] | None = None, *,
                    event_driven: bool = False) -> dict:
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
    state = _load_state()
    outcomes: dict[str, dict] = {}
    for ticker in todo:
        plan = plan_run(ticker, state.get(ticker), time.time()) if event_driven else None
        outcomes[ticker] = await _run_ticker(ticker, c, state.get(ticker), plan)
    ordered = [outcomes[t] for t in todo]
    now = time.time()
    msg = format_digest(ordered, datetime.datetime.fromtimestamp(now))
    delivery = None
    if msg["push"]:
        delivery = await notify.push(msg["title"], msg["body"],
                                     priority=msg["priority"], tags=msg["tags"],
                                     source=msg["source"])
    pushed = delivery.status if delivery is not None else None
    ok_n = sum(1 for o in ordered if o["status"] == "ok")
    if pushed == "failed":
        # Nothing was delivered or queued: keep state and log where they were,
        # so the next run reports the same changes again.
        log.error("digest push failed (%s): advice state NOT advanced",
                  delivery.reason)
        _stats["errors"] += 1
    else:
        _commit(ordered, now)
        _stats["runs"] += ok_n
        if delivery is None or delivery:
            for o in ordered:
                if o["status"] != "ok":
                    continue
                if delivery is None and o.get("state") not in STORE_STATES:
                    continue
                word = (f" [{o['state']}]"
                        if o.get("state") in ("pending", "draft") else "")
                change = o.get("change") or {}
                notify.store(o["ticker"], "analysis",
                             f"{o['ticker']} {o['rating']}{word}"
                             + (f" ({change['parts'][0]})"
                                if change.get("kind") != "none" else ""),
                             "\n".join(_ticker_lines(o)),
                             priority=msg["priority"] if delivery is not None else 1)
    result = {"ok": ok_n == len(ordered) and pushed != "failed",
              "deferred": False, "reason": "", "outcomes": outcomes,
              "pushed": pushed}
    _stats["last_batch"] = {
        "ts": now, "ok": result["ok"], "pushed": pushed,
        "tickers": {t: (o["status"] if o["status"] == "ok" else o["reason"])
                    for t, o in outcomes.items()}}
    return result


def _note(result: dict, label: str) -> str:
    if result.get("deferred"):
        return (f"deferred: {label} batch: "
                f"{result['reason'] or 'gate not open'}")
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


async def _batch_recorded(label: str, tickers: list[str] | None = None,
                          event_driven: bool = False) -> dict:
    """``run_batch`` + its worker-run row: ok=False when any ticker failed,
    the batch was skipped, or the push was lost."""
    started = time.time()
    try:
        result = await run_batch(tickers, event_driven=event_driven)
    except Exception as e:
        _stats["errors"] += 1
        runlog.record("digest", False, time.time() - started,
                      f"{label} batch failed: {str(e)[:160]}")
        log.exception("%s digest batch failed", label)
        return {"ok": False, "deferred": False, "reason": str(e),
                "outcomes": {}, "pushed": None}
    runlog.record("digest", bool(result["ok"] or result.get("deferred")),
                  time.time() - started, _note(result, label))
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


async def _run_with_gate_retries(label: str, tickers: list[str] | None,
                                 event_driven: bool = False) -> None:
    """A lane that is mid-switch (another engine holds the VRAM) is the normal
    skip reason: retry a few times before giving up until the next slot, and
    say so ONCE when the last attempt is still deferred."""
    result: dict = {}
    for attempt in range(4):
        result = await _batch_recorded(label, tickers, event_driven)
        if not result.get("deferred"):
            return
        log.info("digest: batch deferred, retry %d/4 in %ds", attempt + 1,
                 LANE_RETRY_S)
        await asyncio.sleep(LANE_RETRY_S)
    await notify.push("\u26a0\ufe0f digest not run",
                      f"{label} digest could not start: "
                      f"{textsafe.clean_text(result.get('reason'), 200)}",
                      priority=3, tags="warning", source="ops")


def slot_has_session(tickers: list[str], day: datetime.date) -> bool:
    """True when at least one ticker's listing venue has a session on ``day``."""
    return any(calendars.is_session(calendars.mic_for(t), day) for t in tickers)


async def _scheduled_slot(day: datetime.date) -> None:
    """One scheduled slot: a day without a session at any analysable holding's
    venue is recorded as deferred (no lane gate, no push); otherwise the batch
    runs with the gate retries. Manual runs and ``_resume`` are never gated."""
    targets = _targets(None)
    if targets and not slot_has_session(targets, day):
        _stats["skip_reason"] = "no session today"
        runlog.record("digest", True, 0.0,
                      "deferred: no session today (scheduled)")
        return
    await _run_with_gate_retries("scheduled", None, event_driven=True)


async def _loop() -> None:
    log.info("digest loop started (times=%s local)", cfg()["times"])
    while True:
        await asyncio.sleep(_seconds_until_next(cfg()["times"]))
        await _scheduled_slot(datetime.date.today())
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
