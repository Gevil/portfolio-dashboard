"""Paper-trading execution (contract §3): order placement, reconciliation,
end-of-window cancel, kill switch / halt, rebalance and seed.

DEMO ONLY (contract §0): the broker is always ``broker.get_broker("demo")``.

One ``asyncio.Lock`` serialises every execute / reconcile / halt, so a worker
pass and an HTTP rebalance can never interleave their read-modify-write of
``paper_orders.json``. Dry runs only read and take no lock.

Placement (plan §6.3), per intent:

1. the row is written ``pending`` (attempts 0) BEFORE anything is sent;
2. ``open_orders(fresh=True)`` + ``order_history(fresh=True)`` are fetched once
   per batch; a broker order with the same ticker and side, quantity within
   one step, created at/after ``createdAt - 5 s`` and attached to no other row
   is ATTACHED (state from the order) and nothing is sent;
3. otherwise the attempt is persisted (``attempts += 1``) and the order is
   sent exactly once: an ``Order`` -> ``submitted``; timeout / http /
   rate_limited / bad_response -> ``unknown`` (never resent now: the order may
   exist); rejected / bad_request -> ``rejected``; auth / forbidden / disabled
   -> ``not_sent`` (refused before it could be placed);
4. a reconcile pass runs step 2 again for ``unknown`` rows; after
   ``UNKNOWN_PASSES_MAX`` (2) passes without a match the row is ``not_sent``
   and, while its venue's trading window is open, the mode is ``demo``, the
   bucket is not halted and ``attempts < MAX_ATTEMPTS`` (2), ONE resend
   follows; otherwise it stays ``not_sent``.

Decisions the contract leaves open (kept small, documented here):

* A ``pending`` row found by a reconcile pass (a crash left it): ``attempts``
  0 -> never sent -> ``not_sent`` (no trading on a stale intent); ``attempts``
  >= 1 -> the send may have happened -> ``unknown``.
* "Bucket open orders" (end-of-window cancel, halt) are the orders the ledger
  tracks (a row with a ``brokerOrderId``); the owner's manual orders on the
  same tickers are left alone.
* The kill switch (market triggers: drawdown, vs baseline, daily loss; and the
  consecutive-error trigger) runs ONLY while mode is ``demo``: in off/shadow
  no order can flow and an all-cash bucket would trip vs-baseline on any rally.
  The idle pass (off/shadow, or no season) makes no broker call at all.
  Consecutive errors are counted per worker PASS (any broker error result in
  the pass = one error pass). ``auth`` halts at once in any mode, but only
  when the call belonged to a reconcile/cancel of non-terminal rows or to a
  demo-mode action: never from plain status polling or a shadow-mode read.
* A resume re-arms the kill switch: the first evaluation after it records the
  equity and the vs-baseline gap as the new reference (``halt.rearm``) and
  triggers nothing; drawdown is then measured from the peak since the resume,
  vs-baseline from the gap at the resume, daily loss from the resume equity
  when it happened today. Without this a still-true condition would re-halt
  one pass later and the owner could never resume. Changing the mode TO demo
  re-arms the same way (the all-cash history of off/shadow must not count).
* An owner halt cancels orders and pushes like a trigger (one push per halt).
* A push whose Delivery is ``failed`` is retried on the next pass; every other
  status is consumed (``halt.notifiedAt``) per ``skills/workers-and-alerts``.
* Buckets (phase 5): ``daily`` and ``intraday`` share this one execution lock,
  ``paper_orders.json`` and the broker, and are otherwise independent: rows
  carry a ``bucket`` (``daily`` when missing), a bucket's holdings and cash
  are its own ledger fills, the mode gate, halt, kill switch (with the
  bucket's own limits), season and snapshots are per bucket. A reconcile and a
  duplicate check look at EVERY row (a broker order attached to a row of any
  bucket is never attached again). An ``auth`` error halts every enabled
  bucket with ONE push; every other halt is the bucket's alone.
"""
import asyncio
import dataclasses
import datetime
import logging
import math
import time

from app.api import broker as broker_mod
from app.api import (calendars, config_store, notify, paper_buckets, paper_eval,
                     paper_ledger, paper_risk, prices, registry, textsafe)

log = logging.getLogger("paper_exec")
DAILY = paper_buckets.DAILY

MODES = ("off", "shadow", "demo")
MATCH_SLACK_S = 5
UNKNOWN_PASSES_MAX = 2
MAX_ATTEMPTS = 2
MAX_CONSECUTIVE_ERRORS = 3
HISTORY_LIMIT = 50
DEFAULT_KILL_SWITCH = {"drawdownPct": 15.0, "vsBaselinePct": 10.0, "dailyLossPct": 5.0}
_UNKNOWN_ERRORS = ("timeout", "http", "rate_limited", "bad_response")
_REJECT_ERRORS = ("rejected", "bad_request")
_ORDER_STATE = {"open": "submitted", "partially_filled": "partially_filled",
                "filled": "filled", "cancelled": "cancelled", "rejected": "rejected"}

_state = {"lastReconcileAt": None, "consecutiveErrors": 0, "lastError": None}
_lock: asyncio.Lock | None = None
_lock_loop = None


def _get_lock() -> asyncio.Lock:
    """The one execution lock (re-created if the running loop changed, which
    only happens across ``asyncio.run`` calls in tests)."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def _now() -> float:
    """The module clock (tests monkeypatch it)."""
    return time.time()


def _today(now: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(_now() if now is None else now).date().isoformat()


def _broker():
    return broker_mod.get_broker("demo")


def _f(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def reset_state() -> None:
    _state.update(lastReconcileAt=None, consecutiveErrors=0, lastError=None)


def worker_state() -> dict:
    return dict(_state)


def _require_enabled() -> None:
    if not paper_eval.enabled():
        raise paper_eval.PaperDisabled()


def _save(rows: list[dict]) -> bool:
    ok = paper_ledger.save_orders(rows)
    if not ok:
        log.error("paper_orders.json could not be written")
    return ok


# ------------------------------------------------------------------ sessions

def _bounds(mic: str, now: float):
    """(open, close, now) tz-aware for ``mic``'s session on the local date of
    ``now``; None when it is not a session day."""
    today = datetime.datetime.fromtimestamp(now).date()
    b = calendars.session_bounds(mic, today)
    if b is None:
        return None
    return b[0], b[1], datetime.datetime.fromtimestamp(now, b[0].tzinfo)


def window_state(mic: str, now: float) -> dict:
    """{"open", "opensAt", "closesAt"}: ``open`` is ``paper_risk.trading_window``
    (open + 30 min <= now < close - 30 min); the two stamps are that window's
    epoch seconds (None on a non-session day)."""
    b = _bounds(mic, now)
    if b is None:
        return {"open": False, "opensAt": None, "closesAt": None}
    open_dt, close_dt, now_dt = b
    return {
        "open": paper_risk.trading_window(open_dt, close_dt, now_dt),
        "opensAt": int((open_dt + datetime.timedelta(minutes=paper_risk.OPEN_DELAY_MIN)).timestamp()),
        "closesAt": int((close_dt - datetime.timedelta(minutes=paper_risk.CUTOFF_BEFORE_CLOSE_MIN)).timestamp()),
    }


def _window_over(row: dict, now: float) -> bool:
    """True when the row's trading window has ended: no session today, today's
    cutoff (close - 30 min) passed, or the row predates today's open."""
    b = _bounds(paper_eval.mic_of(row["id"]), now)
    if b is None:
        return True
    open_dt, close_dt, now_dt = b
    cutoff = close_dt - datetime.timedelta(minutes=paper_risk.CUTOFF_BEFORE_CLOSE_MIN)
    return now_dt >= cutoff or _f(row.get("createdAt")) < open_dt.timestamp()


def trading_windows(universe: dict, now: float | None = None) -> dict:
    """{mic: window_state} for the venues of the universe's ids."""
    ts = _now() if now is None else now
    mics = sorted({paper_eval.mic_of(pid) for pid in universe})
    return {mic: window_state(mic, ts) for mic in mics}


# --------------------------------------------------------------- order rows

def _intent_dict(intent) -> dict:
    return dataclasses.asdict(intent) if dataclasses.is_dataclass(intent) else dict(intent)


def _bucket_of(cfg: dict) -> str:
    return cfg.get("bucket") or DAILY


def _new_row(intent: dict, *, rows: list[dict], source: str, cfg: dict,
             state: str, now: float) -> dict:
    bucket = _bucket_of(cfg)
    return {
        "intentId": paper_ledger.next_intent_id(
            rows, _today(now), cfg["strategyVersion"], source, intent["id"],
            intent["side"], bucket),
        "book": paper_eval.BOOK, "bucket": bucket, "strategyVersion": cfg["strategyVersion"],
        "source": source, "createdAt": now, "id": intent["id"],
        "ticker": intent["ticker"], "side": intent["side"],
        "quantity": intent["quantity"], "limitPrice": intent["limit_price"],
        "refPrice": intent["ref_price"], "refPriceAsOf": intent.get("ref_price_as_of"),
        "reason": intent.get("reason") or "", "state": state, "brokerOrderId": None,
        "filledQuantity": 0.0, "avgFillPrice": None, "filledValueEur": None,
        "unknownPasses": 0, "attempts": 0, "message": "", "updatedAt": now,
    }


def _apply_order(row: dict, o, now: float, *, keep_cancelled: bool = False) -> None:
    """Copy a broker ``Order``'s id, status and fills onto the row. Fills never
    decrease. ``keep_cancelled``: we just cancelled it - only a race to
    ``filled`` may override that."""
    row["brokerOrderId"] = str(o.id)
    fq = max(_f(row.get("filledQuantity")), _f(o.filled_quantity))
    state = _ORDER_STATE.get(o.status)
    if state is None:          # a broker status we cannot map: it exists, nothing more
        state = "partially_filled" if fq > 0 else "submitted"
    if state == "filled" and fq <= 0:
        fq = _f(o.quantity)
        row["message"] = "filled; fill quantity taken from the order quantity"
    if state == "submitted" and fq > 0:
        state = "partially_filled"
    if keep_cancelled and state != "filled":
        state = "cancelled"
    elif state in ("cancelled", "rejected") and o.raw_status:
        row["message"] = f"broker status {o.raw_status}"
    row["state"] = state
    row["filledQuantity"] = fq
    if o.avg_fill_price is not None:
        row["avgFillPrice"] = _f(o.avg_fill_price)
    if o.filled_value is not None:
        row["filledValueEur"] = _f(o.filled_value)
    row["updatedAt"] = now


def _find_match(row: dict, orders, taken: set, step: float):
    """The broker order that is this row's (see module docstring step 2)."""
    cands = []
    for o in orders:
        if o.id in taken or o.ticker != row["ticker"] or o.side != row["side"]:
            continue
        if abs(_f(o.quantity) - _f(row["quantity"])) > step + 1e-9:
            continue
        if o.created_at is None or o.created_at < _f(row["createdAt"]) - MATCH_SLACK_S:
            continue
        cands.append(o)
    cands.sort(key=lambda o: (o.created_at, str(o.id)))
    return cands[0] if cands else None


def _taken_ids(rows: list[dict], row: dict) -> set:
    return {r["brokerOrderId"] for r in rows
            if r.get("brokerOrderId") and r is not row}


async def _fetch_orders(brk, errs: list) -> dict | None:
    """{order id: Order} over open orders + recent history, both fresh; None
    (error kind appended to ``errs``) when either call failed."""
    open_ = await brk.open_orders(fresh=True)
    if broker_mod.is_error(open_):
        errs.append(open_["error"])
        return None
    hist = await brk.order_history(limit=HISTORY_LIMIT, fresh=True)
    if broker_mod.is_error(hist):
        errs.append(hist["error"])
        return None
    by_id = {o.id: o for o in open_}
    by_id.update({o.id: o for o in hist})      # history carries the final fill data
    return by_id


async def _send_locked(brk, row: dict, rows: list[dict], errs: list) -> None:
    """Send the row's order once. The attempt is persisted first: a crash
    after the send leaves ``pending`` with attempts >= 1, which a reconcile
    pass treats as ``unknown`` (never as unsent)."""
    row["attempts"] = int(row.get("attempts") or 0) + 1
    row["state"] = "pending"
    row["updatedAt"] = _now()
    if not _save(rows):
        row.update(state="not_sent", attempts=row["attempts"] - 1,
                   message="order ledger not writable; nothing was sent")
        return
    res = await brk.place_limit_order(row["ticker"], row["side"], row["quantity"],
                                      row["limitPrice"])
    now = _now()
    if not broker_mod.is_error(res):
        _apply_order(row, res, now)
        if row["state"] == "pending":
            row["state"] = "submitted"
        return
    kind = res["error"]
    errs.append(kind)
    detail = textsafe.clean_text(str(res.get("detail") or ""), 200)
    if kind in _UNKNOWN_ERRORS:
        row.update(state="unknown", unknownPasses=0, updatedAt=now,
                   message=f"send outcome unknown ({kind}); not resent until reconciled")
    elif kind in _REJECT_ERRORS:
        row.update(state="rejected", updatedAt=now,
                   message=f"{kind}: {detail}".strip(": "))
    else:      # auth / forbidden / disabled: refused before it could be placed
        row.update(state="not_sent", updatedAt=now,
                   message=f"{kind}: {detail}".strip(": "))


def _gate(cfg: dict) -> None:
    """Mode / halt gate (the bucket's own) for anything that places orders
    (409 otherwise)."""
    if cfg["mode"] not in ("shadow", "demo"):
        raise paper_eval.PaperConflict(
            f"paper trading mode is {cfg['mode']!r}: set it to shadow or demo first")
    halt = paper_ledger.read_halt(_bucket_of(cfg))
    if halt["halted"]:
        raise paper_eval.PaperConflict(
            f"trading is halted ({halt.get('reason')}); resume first")


async def _place_locked(intents: list, *, source: str, cfg: dict, errs: list) -> list[dict]:
    _gate(cfg)
    mode = cfg["mode"]
    if not intents:
        return []
    now = _now()
    rows = paper_ledger.read_orders()
    new = []
    for it in intents:
        row = _new_row(_intent_dict(it), rows=rows, source=source, cfg=cfg,
                       state="shadow" if mode == "shadow" else "pending", now=now)
        rows.append(row)
        new.append(row)
    if not _save(rows):                       # 1. the intent exists BEFORE any send
        raise paper_eval.PaperUnavailable("could not persist the order ledger")
    if mode == "shadow":
        return new
    brk = _broker()
    snap = await _fetch_orders(brk, errs)     # 2. duplicate check (fresh)
    if snap is None:
        for row in new:
            row.update(state="not_sent", updatedAt=_now(),
                       message=f"could not verify existing broker orders ({errs[-1]}); nothing sent")
        _save(rows)
        return new
    for row in new:
        if paper_ledger.read_halt(_bucket_of(cfg))["halted"]:    # Halt pressed mid-batch: nothing further is sent
            row.update(state="not_sent", updatedAt=_now(),
                       message="halted before this order was sent")
            _save(rows)
            continue
        step = paper_ledger.step_for(brk.quantity_decimals(row["ticker"]))
        match = _find_match(row, snap.values(), _taken_ids(rows, row), step)
        if match is not None:
            _apply_order(row, match, _now())
            row["message"] = "attached to an existing broker order; not sent"
        else:
            await _send_locked(brk, row, rows, errs)   # 3. exactly once
        _save(rows)
    return new


# ----------------------------------------------------------------- reconcile

async def _reconcile_locked(brk, errs: list) -> None:
    rows = paper_ledger.read_orders()
    active = paper_ledger.non_terminal(rows)
    if not active:
        return
    snap = await _fetch_orders(brk, errs)
    if snap is None:
        return
    now = _now()
    cfgs = {b: paper_eval._paper_cfg(b) for b in paper_buckets.ids()}
    halts = {b: paper_ledger.read_halt(b)["halted"] for b in paper_buckets.ids()}
    for row in active:
        if row["state"] == "pending":
            if int(row.get("attempts") or 0) == 0:
                row.update(state="not_sent", updatedAt=now,
                           message="never sent (the process stopped before the send)")
                continue
            row.update(state="unknown", unknownPasses=0, updatedAt=now,
                       message="send outcome unknown after a restart")
        if row.get("brokerOrderId") and row["state"] in ("submitted", "partially_filled"):
            o = snap.get(row["brokerOrderId"])
            if o is not None:
                _apply_order(row, o, now)
            else:
                row["message"] = "order not found at the broker (open orders / recent history)"
            continue
        if row["state"] != "unknown":
            continue
        step = paper_ledger.step_for(brk.quantity_decimals(row["ticker"]))
        match = _find_match(row, snap.values(), _taken_ids(rows, row), step)
        if match is not None:
            _apply_order(row, match, now)
            row["message"] = "found at the broker after an unknown send"
            continue
        row["unknownPasses"] = int(row.get("unknownPasses") or 0) + 1
        row["updatedAt"] = now
        if row["unknownPasses"] < UNKNOWN_PASSES_MAX:
            row["message"] = f"not found at the broker (pass {row['unknownPasses']} of {UNKNOWN_PASSES_MAX})"
            continue
        bkt = paper_ledger.row_bucket(row)
        can_resend = (int(row.get("attempts") or 0) < MAX_ATTEMPTS
                      and (cfgs.get(bkt) or {}).get("mode") == "demo"
                      and not halts.get(bkt, True)
                      and window_state(paper_eval.mic_of(row["id"]), now)["open"])
        if can_resend:
            row["message"] = (f"not found after {UNKNOWN_PASSES_MAX} passes (not_sent); "
                              "resent once")
            await _send_locked(brk, row, rows, errs)
        else:
            row.update(state="not_sent",
                       message=f"not found after {UNKNOWN_PASSES_MAX} passes; not resent")
    _save(rows)


async def _cancel_locked(brk, errs: list, *, only_over: bool,
                         buckets: set[str] | None = None) -> int:
    """Cancel the ledger-tracked open orders of ``buckets`` (None: every
    bucket): those whose trading window is over (``only_over``) or all of them
    (halt). Fills that land before the cancel are re-read afterwards."""
    rows = paper_ledger.read_orders()
    now = _now()
    targets = [r for r in rows
               if r.get("state") in ("submitted", "partially_filled") and r.get("brokerOrderId")
               and (buckets is None or paper_ledger.row_bucket(r) in buckets)
               and (not only_over or _window_over(r, now))]
    if not targets:
        return 0
    cancelled = []
    for r in targets:
        res = await brk.cancel_order(r["brokerOrderId"])
        if broker_mod.is_error(res):
            errs.append(res["error"])
        elif res is True:
            r.update(state="cancelled", updatedAt=now,
                     message=("cancelled at the end of the trading window" if only_over
                              else "cancelled by halt"))
            cancelled.append(r)
        # False: the broker did not cancel it (already done?) - reconcile resolves it
    if cancelled:
        _save(rows)
        snap = await _fetch_orders(brk, errs)
        if snap is not None:
            for r in cancelled:
                o = snap.get(r["brokerOrderId"])
                if o is not None:
                    _apply_order(r, o, _now(), keep_cancelled=True)
            _save(rows)
    return len(cancelled)


# --------------------------------------------------------------- kill switch

def kill_switch_reason(*, limits: dict | None, equity: float, peak: float | None,
                       bucket_pct: float | None, buy_hold_pct: float | None,
                       gap_ref_pct: float | None, day_ref_equity: float | None) -> str | None:
    """First tripped kill-switch condition as a short reason, else None.

    drawdown: (peak - equity) / peak >= drawdownPct. vs baseline: the bucket's
    return since inception minus buy-and-hold's, relative to its value at the
    last resume (``gap_ref_pct``), <= -vsBaselinePct. daily loss: equity vs the
    reference equity (previous snapshot, or the resume equity) <= -dailyLossPct."""
    ks = {**DEFAULT_KILL_SWITCH, **(limits or {})}
    if peak and peak > 0:
        dd = (peak - equity) / peak * 100.0
        if dd >= ks["drawdownPct"]:
            return f"drawdown {dd:.1f}% from the bucket peak (limit {ks['drawdownPct']:g}%)"
    if bucket_pct is not None and buy_hold_pct is not None:
        gap = bucket_pct - buy_hold_pct - (gap_ref_pct or 0.0)
        if gap <= -ks["vsBaselinePct"]:
            return (f"bucket trails buy-and-hold by {-gap:.1f} points "
                    f"(limit {ks['vsBaselinePct']:g})")
    if day_ref_equity and day_ref_equity > 0:
        chg = (equity - day_ref_equity) / day_ref_equity * 100.0
        if chg <= -ks["dailyLossPct"]:
            return f"daily loss {-chg:.1f}% (limit {ks['dailyLossPct']:g}%)"
    return None


async def _buy_hold_pct(season: dict, quotes: dict) -> float | None:
    """Buy-and-hold return since inception on the frozen weights: each id's
    live quote (last daily close when there is none) over its close on the
    first session at/after the inception date. None when any id lacks data."""
    weights = season.get("baselineWeights") or {}
    inception = season.get("inceptionDate")
    total = sum(_f(w) for w in weights.values())
    if not weights or not inception or total <= 0:
        return None
    pct = 0.0
    for pid, w in weights.items():
        closes = sorted(await paper_eval.daily_closes(pid, paper_eval.BASELINE_HISTORY_PERIOD))
        anchor = next((c for d, c in closes if d >= inception), None)
        q = quotes.get(pid)
        if not q and pid not in quotes and paper_eval._open():
            q = await paper_eval.quote(pid)      # a benchmark (EUNL) is not in the priced universe
        q = q or {}
        now_px = q.get("price") if _f(q.get("price")) > 0 else (closes[-1][1] if closes else None)
        if not anchor or anchor <= 0 or not now_px:
            return None
        pct += _f(w) / total * (now_px / anchor - 1.0) * 100.0
    return pct


async def _kill_switch_locked(brk, errs: list, bucket: str = DAILY) -> None:
    season = paper_ledger.read_season(bucket)
    halt = paper_ledger.read_halt(bucket)
    if season is None or halt["halted"]:
        return
    cfg = paper_eval._paper_cfg(bucket)
    if cfg["mode"] != "demo":        # the market triggers only mean something while orders can flow
        return
    pos = await brk.positions()
    if broker_mod.is_error(pos):
        errs.append(pos["error"])
        return
    quotes = await paper_eval.valuation_quotes(cfg["universe"])
    state = paper_ledger.bucket_from_positions(
        positions=pos, account=None, universe=cfg["universe"],
        budget_eur=cfg["budgetEur"], prices_by_id=quotes,
        orders=paper_ledger.read_orders(),
        decimals_by_id=paper_eval.decimals_by_id(cfg["universe"]), bucket=bucket)
    if any(r["valueEur"] is None for r in state["positions"]) or cfg["budgetEur"] <= 0:
        log.warning("kill switch skipped (%s): a held position has no price", bucket)
        return
    equity = state["equityEur"]
    now = _now()
    bh = await _buy_hold_pct(season, {k: v for k, v in quotes.items() if v})
    bucket_pct = (equity / cfg["budgetEur"] - 1.0) * 100.0
    gap = None if bh is None else bucket_pct - bh
    resumed = halt.get("resumedAt")
    rearm = halt.get("rearm") if resumed else None
    if resumed and not rearm:
        halt["rearm"] = {"at": now, "equityEur": equity, "gapPct": gap}
        paper_ledger.write_halt(halt, bucket)
        return
    since = _f(rearm["at"]) if rearm else 0.0
    inception = season.get("inceptionDate") or ""
    all_snaps = paper_ledger.read_snapshots(bucket)
    snaps = [s for s in all_snaps
             if (s.get("date") or "") >= inception and _f(s.get("takenAt")) >= since]
    peak = max([equity] + [_f(s.get("equityEur")) for s in snaps]
               + ([_f(rearm["equityEur"])] if rearm else []))
    today = _today(now)
    prev = [s for s in all_snaps
            if (s.get("date") or "") < today and (s.get("date") or "") >= inception]
    day_ref = _f(max(prev, key=lambda s: s["date"]).get("equityEur")) if prev else None
    if rearm and _today(_f(rearm["at"])) == today:
        day_ref = _f(rearm["equityEur"])
    reason = kill_switch_reason(
        limits=(cfg["limits"] or {}).get("killSwitch"), equity=equity, peak=peak,
        bucket_pct=bucket_pct, buy_hold_pct=bh,
        gap_ref_pct=(rearm or {}).get("gapPct"), day_ref_equity=day_ref)
    if reason:
        await _halt_locked(brk, reason if bucket == DAILY else f"{bucket}: {reason}",
                           "kill_switch", bucket)


# ---------------------------------------------------------------------- halt

async def _push_halt_locked(rows: dict) -> None:
    """ONE push (priority 4, source ``paper``) for the halt of the buckets in
    ``rows`` {bucket: halt row} raised together. Consumed for every Delivery
    status except ``failed``, which is retried by the next pass."""
    buckets = sorted(rows, key=paper_buckets.BUCKETS.index)
    first = rows[buckets[0]]
    daily_only = buckets == [DAILY]
    title = "Paper trading halted" if daily_only else f"Paper trading halted ({', '.join(buckets)})"
    key = f"paper_halt:{first.get('at')}" + ("" if daily_only else ":" + "+".join(buckets))
    d = await notify.alert(
        "PAPER", "paper", title,
        textsafe.clean_text(f"{first.get('reason')} ({first.get('by')})", 200),
        priority=4, dedupe_key=key)
    if d.consumed:
        for b, row in rows.items():
            row["notifiedAt"] = _now()
            row["notifyStatus"] = d.status
            paper_ledger.write_halt(row, b)


async def _flush_halt_push() -> None:
    groups: dict = {}
    for b in paper_buckets.ids():
        row = paper_ledger.read_halt(b)
        if row["halted"] and not row.get("notifiedAt"):
            groups.setdefault(row.get("at"), {})[b] = row
    for rows in groups.values():
        await _push_halt_locked(rows)


def _halt_row(reason: str, by: str) -> dict:
    return {"halted": True, "reason": textsafe.clean_text(reason, 300) or "halted",
            "at": _now(), "by": by, "resumedAt": None, "resumedBy": None,
            "notifiedAt": None, "rearm": None}


async def _halt_effects_locked(brk, rows: dict, persisted: bool = True) -> None:
    """What a halt does besides setting the flag: cancel the halted buckets'
    open orders and push once. A bucket whose halt is no longer the current
    one (an owner resume got the lock first) is skipped; when the flag could not
    be written (``persisted`` False) there is nothing to compare, and the
    safety actions run anyway."""
    live: dict = {}
    for b, row in rows.items():
        cur = paper_ledger.read_halt(b)
        if persisted:
            if not cur["halted"] or cur.get("at") != row["at"]:
                continue
            live[b] = cur
        else:
            live[b] = row
    if not live:
        return
    if brk is not None:
        await _cancel_locked(brk, [], only_over=False, buckets=set(live))
    await _push_halt_locked(live)


async def _halt_buckets_locked(brk, buckets, reason: str, by: str) -> dict:
    """Halt every bucket of ``buckets`` that is not halted yet, with ONE
    cancel pass and ONE push. -> {bucket: the new halt row}."""
    base = _halt_row(reason, by)
    rows: dict = {}
    persisted = True
    for b in buckets:
        if paper_ledger.read_halt(b)["halted"]:
            continue
        rows[b] = dict(base)
        persisted = paper_ledger.write_halt(rows[b], b) and persisted
    if rows:
        await _halt_effects_locked(brk, rows, persisted)
    if not persisted:
        raise paper_eval.PaperUnavailable("could not persist the halt")
    return rows


async def _halt_locked(brk, reason: str, by: str, bucket: str = DAILY) -> dict:
    rows = await _halt_buckets_locked(brk, [bucket], reason, by)
    return rows.get(bucket) or paper_ledger.read_halt(bucket)


async def halt(reason: str | None = None, bucket: str = DAILY) -> dict:
    """Owner halt (``POST /api/paper/halt``) of ONE bucket, idempotent. The flag
    is written AT ONCE, without waiting for the lock, so a placement already in
    progress sends nothing further; the cancel and the push follow under the lock."""
    bucket = paper_buckets.validate(bucket)
    cur = paper_ledger.read_halt(bucket)
    if cur["halted"]:
        return cur
    row = _halt_row(reason or "halted by the owner", "owner")
    persisted = paper_ledger.write_halt(row, bucket)
    async with _get_lock():
        await _halt_effects_locked(_broker(), {bucket: row}, persisted)
    if not persisted:
        raise paper_eval.PaperUnavailable("could not persist the halt")
    return row


async def resume(bucket: str = DAILY) -> dict:
    """Owner resume (``POST /api/paper/resume``) of ONE bucket: clears its halt,
    resets the error counter, and leaves the kill switch to re-arm on its next pass."""
    bucket = paper_buckets.validate(bucket)
    async with _get_lock():
        cur = paper_ledger.read_halt(bucket)
        if not cur["halted"]:
            return cur
        row = {**cur, "halted": False, "resumedAt": _now(), "resumedBy": "owner",
               "rearm": None}
        if not paper_ledger.write_halt(row, bucket):
            raise paper_eval.PaperUnavailable("could not persist the resume")
        _state.update(consecutiveErrors=0, lastError=None)
        return row


# ---------------------------------------------------------------------- pass

async def _step(name: str, coro, errs: list):
    try:
        return await coro
    except Exception as e:
        log.exception("paper_exec %s failed", name)
        errs.append(f"exception:{type(e).__name__}")
        return None


async def _register_errors(brk, errs: list, demo_buckets: list) -> None:
    """Error accounting for a pass. The consecutive-error trigger counts and
    halts only while a bucket is in demo mode (and halts those buckets).
    ``auth`` halts every enabled bucket in any mode: every error reaching here
    came from a reconcile / cancel of non-terminal rows or from a demo-mode
    evaluation, never from plain status polling (the idle path makes no call)."""
    if not errs:
        _state["consecutiveErrors"] = 0
        return
    if demo_buckets:
        _state["consecutiveErrors"] += 1
    _state["lastError"] = errs[-1]
    if "auth" in errs:
        await _halt_buckets_locked(brk, paper_eval.enabled_buckets(),
                                   "Trading 212 authentication failed", "errors")
    elif demo_buckets and _state["consecutiveErrors"] >= MAX_CONSECUTIVE_ERRORS:
        await _halt_buckets_locked(
            brk, demo_buckets,
            f"{_state['consecutiveErrors']} consecutive broker/reconcile errors "
            f"(last: {errs[-1]})", "errors")


async def run_pass() -> dict:
    """One worker pass: reconcile -> cancel (halted buckets: all their orders;
    others: window over) -> kill switch per demo bucket -> error accounting ->
    retry an unsent halt push."""
    brk = _broker()
    out = {"active": 0, "cancelled": 0, "errors": [], "halted": False}
    if brk is None:
        return out
    async with _get_lock():
        errs: list = []
        ids = paper_buckets.ids()
        demo_buckets = [b for b in ids if paper_eval._paper_cfg(b)["mode"] == "demo"]
        out["active"] = len(paper_ledger.non_terminal(paper_ledger.read_orders()))
        unsent_halt = any(h["halted"] and not h.get("notifiedAt")      # a PRIOR pass's failed push
                          for h in (paper_ledger.read_halt(b) for b in ids))
        if not out["active"] and not any(paper_ledger.read_season(b) is not None
                                         for b in demo_buckets):
            # no non-terminal rows to reconcile and nothing to evaluate: no broker call at all
            await _step("halt push", _flush_halt_push(), errs)
            return out
        await _step("reconcile", _reconcile_locked(brk, errs), errs)
        halted_ids = {b for b in ids if paper_ledger.read_halt(b)["halted"]}
        cancelled = 0
        if halted_ids:
            cancelled += await _step(
                "cancel halted", _cancel_locked(brk, errs, only_over=False, buckets=halted_ids),
                errs) or 0
        cancelled += await _step(
            "cancel", _cancel_locked(brk, errs, only_over=True, buckets=set(ids) - halted_ids),
            errs) or 0
        out["cancelled"] = cancelled
        for b in demo_buckets:
            await _step(f"kill switch {b}", _kill_switch_locked(brk, errs, b), errs)
        await _step("error accounting", _register_errors(brk, errs, demo_buckets), errs)
        if unsent_halt:             # a halt raised in THIS pass already pushed once; retry is next pass
            await _step("halt push", _flush_halt_push(), errs)
        if not errs:
            _state["lastReconcileAt"] = _now()
        out["errors"] = list(errs)
        out["halted"] = any(paper_ledger.read_halt(b)["halted"] for b in ids)
    return out


def has_open_rows() -> bool:
    return bool(paper_ledger.non_terminal(paper_ledger.read_orders()))


def needs_fast_pass(now: float | None = None) -> bool:
    """Worker cadence (§3.6): 60 s while a row is non-terminal or a trading
    window is open on a session day, else 300 s."""
    if has_open_rows():
        return True
    universe = paper_eval._paper_cfg()["universe"]
    return any(w["open"] for w in trading_windows(universe, now).values())


def status_extra(bucket: str = DAILY) -> dict:
    """Fields ``GET /api/paper/status`` gains (§3.7), for ``bucket``."""
    bucket = paper_buckets.validate(bucket)
    cfg = paper_eval._paper_cfg(bucket)
    rows = paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
    return {
        "halt": paper_ledger.read_halt(bucket),
        "openIntents": len(paper_ledger.non_terminal(rows)),
        "lastReconcileAt": _state["lastReconcileAt"],
        "tradingWindow": trading_windows(cfg["universe"]),
    }


# ---------------------------------------------------------- rebalance / seed

def _pct(value, what: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 100):
        raise paper_eval.PaperBadRequest(f"{what} must be a number in [0, 100]")
    return float(value)


def _validate_targets(targets, cash_pct) -> tuple[dict, float | None]:
    if not isinstance(targets, dict) or not targets:
        raise paper_eval.PaperBadRequest("targets must be a non-empty object {id: percent}")
    clean = {}
    for pid, pct in targets.items():
        if not isinstance(pid, str) or not pid.strip():
            raise paper_eval.PaperBadRequest("target ids must be non-empty strings")
        clean[pid.strip()] = _pct(pct, f"targets.{pid}")
    return clean, (None if cash_pct is None else _pct(cash_pct, "cashPct"))


async def _rebalance_body(cfg: dict, targets: dict, cash_pct: float | None, *,
                          dry_run: bool, source: str, exempt_caps: bool,
                          seed_checks: bool, errs: list) -> dict:
    bucket = _bucket_of(cfg)
    universe = cfg["universe"]
    brk = _broker()
    pos = await brk.positions(fresh=True)      # reachability / auth; holdings below are the ledger's
    if broker_mod.is_error(pos):
        errs.append(pos["error"])
        raise paper_eval.PaperUnavailable()
    all_rows = paper_ledger.read_orders()
    rows = paper_ledger.bucket_rows(all_rows, bucket)
    if seed_checks:
        tmap = {spec["t212"]: pid for pid, spec in universe.items()
                if isinstance(spec, dict) and spec.get("t212")}
        broker_qty: dict[str, float] = {}
        for p in pos:
            pid = tmap.get(p.ticker)
            if pid is not None:
                broker_qty[pid] = broker_qty.get(pid, 0.0) + _f(p.quantity)
        untracked = any(w["code"] == "untracked_position"
                        for w in paper_ledger.ledger_warnings(all_rows, broker_qty, None))
        if (paper_ledger.bucket_holds_positions(rows, bucket)
                or paper_ledger.non_terminal(rows) or untracked):
            raise paper_eval.PaperConflict(
                "the bucket already holds positions or open orders; seeding needs an empty bucket")
    holdings = {pid: 0.0 for pid in universe}      # the bucket's own ledger fills, never the broker's split
    for pid, held in paper_ledger.ledger_holdings(rows).items():
        if pid in holdings:
            holdings[pid] += held["quantity"]
    for pid, delta in paper_ledger.open_exposure(rows).items():   # already in flight
        if pid in holdings:
            holdings[pid] += delta
    holdings = {pid: max(0.0, q) for pid, q in holdings.items()}
    wanted = [*targets, *(pid for pid, qty in holdings.items() if qty > 0)]
    quotes = {pid: q for pid, q in (await paper_eval.quotes_for(universe, wanted)).items() if q}
    held_ids = [pid for pid, qty in holdings.items() if qty > 0]
    held_prices = await paper_eval.held_prices(universe) if held_ids else {}
    us_lead = {}
    if cfg.get("universeSource") == "paper_universe":
        from app.api import live_ws            # lazy: live_ws pulls in the websocket stack
        for pid in targets:
            try:
                lead = live_ws.us_lead(pid)
            except Exception:                  # a broken lead must never block a decision
                lead = None
            if lead:
                us_lead[pid] = lead
    now = _now()
    orders_today, turnover = paper_ledger.day_usage(rows, _today(now))
    plan = paper_risk.plan_orders(
        targets=targets, cash_pct=cash_pct, holdings=holdings,
        cash_eur=(paper_ledger.ledger_cash(rows, cfg["budgetEur"])["cashEur"]
                  + paper_ledger.open_sell_proceeds(rows)),
        quotes=quotes, held_prices=held_prices, universe=universe,
        kinds={pid: paper_eval.kind_of(pid, spec) for pid, spec in universe.items()},
        limits=cfg["limits"], budget_eur=cfg["budgetEur"],
        decimals=paper_eval.decimals_by_id(universe), now=now,
        session_ok={pid: window_state(paper_eval.mic_of(pid), now)["open"] for pid in universe},
        orders_today=orders_today, turnover_today_eur=turnover, exempt_caps=exempt_caps,
        us_lead=us_lead or None)
    intents = plan["intents"]
    plan = {**plan, "intents": [dataclasses.asdict(i) for i in intents]}
    if dry_run:
        if cfg["mode"] != "demo":
            plan["warnings"] = [*plan["warnings"], f"mode_{cfg['mode']}"]
        if paper_ledger.read_halt(bucket)["halted"]:
            plan["warnings"] = [*plan["warnings"], "halted"]
        return {"plan": plan, "rows": []}
    new = await _place_locked(intents, source=source, cfg=cfg, errs=errs)
    return {"plan": plan, "rows": [dict(r) for r in new]}


async def _run_rebalance(*, targets, cash_pct, dry_run: bool, source: str,
                         exempt_caps: bool, seed_checks: bool, bucket: str = DAILY) -> dict:
    bucket = paper_buckets.validate(bucket)
    _require_enabled()
    cfg = paper_eval._paper_cfg(bucket)
    if paper_ledger.read_season(bucket) is None:
        raise paper_eval.PaperConflict("no season started; start one before trading")
    targets, cash_pct = _validate_targets(targets, cash_pct)
    kw = dict(dry_run=dry_run, source=source, exempt_caps=exempt_caps,
              seed_checks=seed_checks)
    errs: list = []
    if dry_run:
        return await _rebalance_body(cfg, targets, cash_pct, errs=errs, **kw)
    async with _get_lock():
        _gate(cfg)
        try:
            return await _rebalance_body(cfg, targets, cash_pct, errs=errs, **kw)
        finally:
            if "auth" in errs and cfg["mode"] == "demo":     # shadow only READS the broker: no halt
                await _halt_buckets_locked(_broker(), paper_eval.enabled_buckets(),
                                           "Trading 212 authentication failed", "errors")


async def rebalance(*, targets, cash_pct=None, dry_run: bool = True,
                    source: str = "manual", bucket: str = DAILY) -> dict:
    """``POST /api/paper/rebalance``: ``{"plan": plan_orders output (intents as
    dicts), "rows": [written rows]}``; ``rows`` is empty on a dry run, which
    never writes or sends. A real run needs the bucket's mode shadow|demo and
    no halt of that bucket (409)."""
    return await _run_rebalance(targets=targets, cash_pct=cash_pct, dry_run=dry_run,
                                source=source, exempt_caps=False, seed_checks=False,
                                bucket=bucket)


async def seed(*, dry_run: bool = True, bucket: str = DAILY) -> dict:
    """``POST /api/paper/seed``: rebalance to the bucket season's frozen baseline
    weights with ``exempt_caps`` (source "seed"); 409 unless the bucket's own
    ledger holds no positions and no non-terminal rows (a dry run is checked too)."""
    bucket = paper_buckets.validate(bucket)
    if paper_eval._open():
        raise paper_eval.PaperConflict(
            "seeding copies the s1 baseline weights; with the open universe the "
            "benchmark is EUNL and the PM builds the portfolio")
    season = paper_ledger.read_season(bucket)
    weights = (season or {}).get("baselineWeights") or {}
    if season is not None and not weights:
        raise paper_eval.PaperConflict("the season has no baseline weights to seed")
    return await _run_rebalance(targets=weights, cash_pct=None, dry_run=dry_run,
                                source="seed", exempt_caps=True, seed_checks=True,
                                bucket=bucket)


def _rearm_locked(bucket: str = DAILY) -> None:
    """Switching to demo re-arms the kill switch the way a resume does: the
    first evaluation afterwards records the reference equity / baseline gap,
    and the error counter starts again. A standing halt is left alone (its
    resume re-arms)."""
    _state.update(consecutiveErrors=0, lastError=None)
    cur = paper_ledger.read_halt(bucket)
    if cur["halted"]:
        return
    if not paper_ledger.write_halt(
            {**cur, "resumedAt": _now(), "resumedBy": "mode", "rearm": None}, bucket):
        log.error("kill switch re-arm could not be persisted")


async def set_mode(mode, bucket: str = DAILY) -> str:
    """``POST /api/paper/mode``: writes the bucket's mode (``paperTrading.mode``
    for daily, ``paperTrading.buckets.intraday.mode`` otherwise - the bucket must
    be configured and enabled, else 409) through ``config_store.update`` under
    the execution lock. Never touches existing orders. Changing to ``demo`` from
    another mode re-arms that bucket's kill switch."""
    bucket = paper_buckets.validate(bucket)
    m = mode.strip().lower() if isinstance(mode, str) else ""
    if m not in MODES:
        raise paper_eval.PaperBadRequest(f"mode must be one of {', '.join(MODES)}")
    _require_enabled()

    def mutate(cfg: dict) -> None:
        block = cfg.get("paperTrading")
        if not isinstance(block, dict):
            block = {}
            cfg["paperTrading"] = block
        if bucket == DAILY:
            block["mode"] = m
            return
        buckets = block.get("buckets")
        spec = buckets.get(bucket) if isinstance(buckets, dict) else None
        if not isinstance(spec, dict) or spec.get("enabled") is not True:
            raise paper_eval.PaperConflict(
                f"bucket {bucket!r} is not enabled (paperTrading.buckets.{bucket}.enabled)")
        spec["mode"] = m

    async with _get_lock():
        previous = paper_eval._paper_cfg(bucket)["mode"]
        config_store.update(mutate)
        if m == "demo" and previous != "demo":
            _rearm_locked(bucket)
    return m


async def new_season(*, book: str = paper_eval.BOOK, bucket: str = DAILY,
                     strategy_version: str | None = None) -> dict:
    """``POST /api/paper/new-season``: start a fresh season for the bucket (the
    owner moves to ``s2`` / ``i2``). The bucket must be EMPTY (no position, no
    live order; 409), the old season is kept in the new row's ``previous`` and
    every old snapshot/decision/order stays readable (versions are segmented by
    ``strategyVersion``). ``strategy_version`` is written to the bucket's config
    (``paperTrading.strategyVersion`` / ``buckets.intraday.strategyVersion``)
    under the execution lock so the new season and the decisions agree."""
    bucket = paper_buckets.validate(bucket)
    sv = None
    if strategy_version is not None:
        if not isinstance(strategy_version, str) or not strategy_version.strip() \
                or len(strategy_version.strip()) > 32:
            raise paper_eval.PaperBadRequest(
                "strategyVersion must be a non-empty string of at most 32 characters")
        sv = strategy_version.strip()
    _require_enabled()
    async with _get_lock():
        rows = paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
        if paper_ledger.bucket_holds_positions(rows, bucket) or paper_ledger.non_terminal(rows):
            raise paper_eval.PaperConflict(
                "the bucket must be empty (no positions, no open orders) before a new season")
        row = await paper_eval.create_season(book, reset=True, bucket=bucket, strategy_version=sv)
        if sv is not None:
            def mutate(cfg: dict) -> None:
                block = cfg.get("paperTrading")
                if not isinstance(block, dict):
                    block = {}
                    cfg["paperTrading"] = block
                if bucket == DAILY:
                    block["strategyVersion"] = sv
                    return
                spec = (block.get("buckets") or {}).get(bucket)
                if not isinstance(spec, dict):
                    raise paper_eval.PaperConflict(
                        f"bucket {bucket!r} is not configured (paperTrading.buckets.{bucket})")
                spec["strategyVersion"] = sv
            config_store.update(mutate)
    return row
