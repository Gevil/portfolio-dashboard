"""Portfolio policy: drift bands, single-stock cap, contribution routing, the
monthly trend filter - and the daily watcher that pushes the edges.

``policy_payload()`` backs ``GET /api/policy`` (shape: plan contract C9). The
owner's rules live in ``config.investmentPolicy`` (``targets`` %, ``maxSingleStockPct``,
``band {absPct, relPct}``, ``monthlyContributionEur``, ``trendFilter``); the
current weights always come from ``portfolio.snapshot()`` priced positions
(``valueEur`` / sum of priced values), so an unpriced holding never skews them.
Without ``investmentPolicy`` the payload says ``configured: false`` and still
carries the current weights.

Methods and invariants:

- **Band (5/25 rule).** Half-width = ``min(absPct, target x relPct / 100)``
  (defaults 5 / 25); bounds are ``target -/+ half`` (lower bound floored at 0).
  ``status`` is ``over`` / ``under`` / ``ok``, decided on the values as shown
  (rounded to 0.01) so a number on the edge is ``ok``. A target whose holding
  exists but is unpriced is left out of ``drift`` and named in ``warnings``.
- **Max single stock.** ``breaches`` = holdings whose kind is ``equity`` (an ETF
  never counts) above ``maxSingleStockPct``.
- **Contribution.** Exact water-filling without selling: find the level L with
  ``sum(max(0, L x t_i - V_i)) = C`` (t_i = target share among the eligible
  ids, V_i = current EUR value, C = ``monthlyContributionEur``); the allocation
  is ``a_i = max(0, L x t_i - V_i)``, rounded to cents by largest remainder so
  the allocations sum to C exactly. Holdings without a target are outside the
  plan.
- **Trend.** Month-end closes from ``prices.daily_closes(id, "2y")`` over COMPLETED
  calendar months only (the running month never counts). ``sma10m`` = mean of
  the last 10 completed month-end closes; ``state`` is ``above`` when the last
  month-end close is strictly above it, else ``below``; ``changed`` = the state
  differs from the one at the previous month-end. Computed only when
  ``trendFilter`` is true, for the holdings plus the role benchmark; an id
  without 10 completed month-ends or whose newest month-end is not last month's
  is skipped (named in ``warnings``).

Worker ``policy`` (``POLICY_WATCH_ENABLED``, default on): once a day at 18:00
local - only when XETR or XAMS had a session that day - it pushes through
``notify.alert("POLICY", "policy", ...)`` at priority 3, edge-triggered:

- a band breach entered or left (per id; ``over``<->``under`` counts as an edge);
- a max-single-stock breach entered or left (per id);
- a monthly trend flip: the first pass after a month end whose row has
  ``changed`` true (once per id and month end);
- a reminder every 7 days while any id is out of band (the clock starts when
  a band alert is delivered; a pass that delivers a band alert restarts it).

State (``data/policy_state.json``, via ``jsonstore``) is advanced per edge only
when ``Delivery.consumed`` (every status but ``failed``); a failed edge is
retried on the next pass. ``status()`` carries ``interval_s=86400``.
"""
import asyncio
import datetime
import logging
import math
import os
import pathlib
import time

from app.api import (calendars, config_store, jsonstore, notify, portfolio,
                     prices, registry, runlog)

log = logging.getLogger("policy")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "policy_state.json"

DEFAULT_BAND = {"absPct": 5.0, "relPct": 25.0}
TREND_MONTHS = 10
HOUR, MINUTE = 18, 0
INTERVAL_S = 86400
REMINDER_S = 7 * 86400
RETRY_S = 900             # a delivery failed: retry the pass this often
MAX_RETRIES = 4
PRIORITY = 3
SESSION_MICS = ("XETR", "XAMS")

_task: asyncio.Task | None = None
_stats = {"running": False, "last_run": 0.0, "next_run": 0.0, "runs": 0,
          "errors": 0, "pushed": 0, "note": ""}


def _now() -> float:
    return time.time()


def _today() -> datetime.date:
    return datetime.date.today()


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ------------------------------------------------------------------ pure math

def band_for(target: float, band: dict | None = None) -> tuple[float, float]:
    """(lower, upper) % bounds: half-width = min(absPct, target x relPct / 100)."""
    band = band if isinstance(band, dict) else {}
    abs_pct = _num(band.get("absPct"))
    rel_pct = _num(band.get("relPct"))
    abs_pct = DEFAULT_BAND["absPct"] if abs_pct is None else abs_pct
    rel_pct = DEFAULT_BAND["relPct"] if rel_pct is None else rel_pct
    half = min(abs_pct, target * rel_pct / 100.0)
    return max(0.0, target - half), target + half


def band_status(current: float, lo: float, hi: float) -> str:
    return "over" if current > hi else "under" if current < lo else "ok"


def water_fill(values: list[float], weights: list[float], amount: float) -> list[float]:
    """Allocation ``a_i = max(0, L x w_i - V_i)`` with ``sum(a) = amount``.

    Money goes to the holdings furthest below their target first (lowest
    ``V_i / w_i``), raising them to a common level L; nothing is ever sold.
    Entries with a non-positive weight get 0. Exact (floats), not yet rounded.
    """
    n = len(values)
    out = [0.0] * n
    live = [i for i in range(n) if weights[i] > 0]
    if amount <= 0 or not live:
        return out
    live.sort(key=lambda i: values[i] / weights[i])
    sum_v = sum_w = 0.0
    level = None
    for k, i in enumerate(live):
        sum_v += values[i]
        sum_w += weights[i]
        level = (amount + sum_v) / sum_w
        if k + 1 == len(live):
            break
        nxt = live[k + 1]
        if level <= values[nxt] / weights[nxt] + 1e-12:
            break
    for i in live:
        out[i] = max(0.0, level * weights[i] - values[i])
    return out


def to_cents(raw: list[float], amount: float) -> list[float]:
    """Round euro allocations to cents so they sum to ``amount`` exactly
    (largest-remainder; only positive entries receive or lose a cent)."""
    total = round(amount * 100)
    scaled = [x * 100.0 for x in raw]
    cents = [math.floor(x + 1e-9) for x in scaled]
    pos = [i for i, x in enumerate(scaled) if x > 1e-9]
    if not pos:
        return [0.0] * len(raw)
    diff = total - sum(cents)
    if diff > 0:
        order = sorted(pos, key=lambda i: (-(scaled[i] - cents[i]), i))
    else:
        order = sorted((i for i in pos if cents[i] > 0),
                       key=lambda i: (scaled[i] - cents[i], i))
    step = 1 if diff > 0 else -1
    for j in range(abs(diff)):
        if not order:
            break
        cents[order[j % len(order)]] += step
    return [c / 100.0 for c in cents]


def month_ends(closes, today: datetime.date) -> list[tuple[str, float]]:
    """[(date, close)] of the last bar of each COMPLETED calendar month
    (strictly before ``today``'s month), oldest first."""
    current = today.isoformat()[:7]
    last: dict[str, tuple[str, float]] = {}
    for row in sorted((r for r in closes or [] if len(r) == 2), key=lambda r: str(r[0])):
        day, close = str(row[0]), _num(row[1])
        if close is None or close <= 0 or len(day) < 10 or day[:7] >= current:
            continue
        last[day[:7]] = (day[:10], close)
    return [last[k] for k in sorted(last)]


def trend_row(ident: str, closes, today: datetime.date) -> dict | None:
    """The C9 ``trend`` row of one id, or None when the history is too short or
    stale (newest completed month-end is not last month's)."""
    me = month_ends(closes, today)
    if len(me) < TREND_MONTHS:
        return None
    first_of_month = today.replace(day=1)
    last_month = (first_of_month - datetime.timedelta(days=1)).isoformat()[:7]
    if me[-1][0][:7] != last_month:
        return None

    def at(end: int) -> tuple[str, float]:
        window = [c for _, c in me[end - TREND_MONTHS + 1:end + 1]]
        sma = sum(window) / TREND_MONTHS
        return ("above" if me[end][1] > sma else "below"), sma

    state, sma = at(len(me) - 1)
    changed = len(me) > TREND_MONTHS and at(len(me) - 2)[0] != state
    return {"id": ident, "monthEnd": me[-1][0], "close": round(me[-1][1], 4),
            "sma10m": round(sma, 4), "state": state, "changed": changed}


# ------------------------------------------------------------------ payload

def _targets(policy: dict) -> dict[str, float]:
    raw = policy.get("targets")
    out = {}
    for key, pct in (raw.items() if isinstance(raw, dict) else []):
        v = _num(pct)
        if v is not None and v >= 0:
            out[str(key).strip().upper()] = v
    return out


async def _trend_rows(policy: dict, warnings: list[dict]) -> list[dict]:
    if policy.get("trendFilter") is not True:
        return []
    ids = list(registry.holdings())
    bench = registry.benchmark_id()
    if bench and bench not in ids:
        ids.append(bench)

    async def closes(ident):
        try:
            return await prices.daily_closes(ident, "2y")
        except Exception as e:
            log.warning("trend closes %s failed: %s", ident, e)
            return []

    series = await asyncio.gather(*[closes(i) for i in ids])
    today = _today()
    rows, skipped = [], []
    for ident, data in zip(ids, series):
        row = trend_row(ident, data, today)
        if row is None:
            skipped.append(ident)
        else:
            rows.append(row)
    if skipped:
        warnings.append({"code": "trend_unavailable", "ids": skipped,
                         "message": "Fewer than 10 completed month-end closes "
                                    "(or a stale series) for: " + ", ".join(skipped)})
    return rows


async def policy_payload() -> dict:
    """``GET /api/policy``. Never raises on missing config, prices or history."""
    cfg = config_store.read()
    raw_policy = cfg.get("investmentPolicy")
    policy = raw_policy if isinstance(raw_policy, dict) and raw_policy else None
    warnings: list[dict] = []
    try:
        snap = await portfolio.snapshot()
    except Exception as e:
        log.warning("policy: portfolio snapshot failed: %s", e)
        snap = {}
        warnings.append({"code": "portfolio_unavailable",
                         "message": "Portfolio snapshot failed; no weights"})
    rows = [r for r in (snap or {}).get("positions") or [] if isinstance(r, dict)]
    held = {str(r.get("id")).upper() for r in rows}
    values, kinds = {}, {}
    for r in rows:
        ident, v = str(r.get("id")).upper(), _num(r.get("valueEur"))
        kinds[ident] = r.get("kind")
        if v is not None and v >= 0:
            values[ident] = v
    total = sum(values.values())
    weights = {i: v * 100.0 / total for i, v in values.items()} if total > 0 else {}
    unpriced = sorted(held - set(values))
    if unpriced:
        warnings.append({"code": "unpriced", "ids": unpriced,
                         "message": "No usable price, left out of the weights: "
                                    + ", ".join(unpriced)})

    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    out = {"configured": policy is not None,
           "targets": {}, "current": {i: round(w, 2) for i, w in ranked},
           "drift": [], "maxSingleStock": {"limitPct": None, "breaches": []},
           "contribution": {"amountEur": 0.0, "allocation": {}},
           "trend": [], "warnings": warnings,
           "asOf": datetime.datetime.now().isoformat(timespec="seconds")}
    if policy is None:
        return out

    targets = _targets(policy)
    out["targets"] = targets
    if total <= 0:
        warnings.append({"code": "no_priced_positions",
                         "message": "No priced position: weights, drift and "
                                    "contribution need at least one"})
    else:
        band = policy.get("band")
        for tid, tpct in targets.items():
            if tid in unpriced:
                continue
            cur = round(weights.get(tid, 0.0), 2)
            lo, hi = (round(x, 2) for x in band_for(tpct, band))
            out["drift"].append({"id": tid, "targetPct": tpct, "currentPct": cur,
                                 "bandLoPct": lo, "bandHiPct": hi,
                                 "status": band_status(cur, lo, hi)})
        untargeted = sorted(i for i in values if i not in targets)
        if targets and untargeted:
            warnings.append({"code": "no_target", "ids": untargeted,
                             "message": "Held without a target (outside the "
                                        "contribution plan): " + ", ".join(untargeted)})

    limit = _num(policy.get("maxSingleStockPct"))
    out["maxSingleStock"]["limitPct"] = limit
    if limit is not None:
        out["maxSingleStock"]["breaches"] = [
            i for i, w in ranked if kinds.get(i) == "equity" and round(w, 2) > limit]

    amount = _num(policy.get("monthlyContributionEur"))
    amount = round(amount, 2) if amount is not None and amount > 0 else 0.0
    eligible = [t for t in targets if t not in unpriced]
    shares = [targets[t] for t in eligible]
    share_sum = sum(shares)
    if total > 0 and share_sum > 0:
        alloc = to_cents(water_fill([values.get(t, 0.0) for t in eligible],
                                    [s / share_sum for s in shares], amount), amount)
    else:
        alloc = [0.0] * len(eligible)
    out["contribution"] = {"amountEur": amount,
                           "allocation": {t: round(a, 2) for t, a in zip(eligible, alloc)}}
    out["trend"] = await _trend_rows(policy, warnings)
    return out


# ------------------------------------------------------------------ worker

def _load_state() -> dict:
    raw = jsonstore.load(STATE_FILE, None)
    raw = raw if isinstance(raw, dict) else {}
    band, single, alerted = raw.get("band"), raw.get("single"), raw.get("trendAlerted")
    last_pass = raw.get("lastPass")
    return {
        "version": 1,
        "band": {str(k): v for k, v in band.items() if v in ("over", "under")}
        if isinstance(band, dict) else {},
        "single": sorted(str(x) for x in single) if isinstance(single, list) else [],
        "trendAlerted": {str(k): str(v) for k, v in alerted.items()}
        if isinstance(alerted, dict) else {},
        "lastReminder": _num(raw.get("lastReminder")),
        "lastPass": last_pass if isinstance(last_pass, str) else None,
    }


def _session_today(day: datetime.date) -> bool:
    try:
        return any(calendars.is_session(mic, day) for mic in SESSION_MICS)
    except Exception as e:
        log.warning("session check failed (%s): assuming a session", e)
        return True


def _band_text(row: dict) -> str:
    return (f"{row['currentPct']:.1f}% vs target {row['targetPct']:g}% "
            f"(band {row['bandLoPct']:.1f}-{row['bandHiPct']:.1f}%)")


def _edges(state: dict, payload: dict, today: datetime.date) -> list[dict]:
    """Every edge between the delivered ``state`` and ``payload``; each carries
    the message and a ``commit(new_state)`` that records it once delivered."""
    edges = []
    alloc = payload["contribution"]["allocation"]
    for row in payload["drift"]:
        tid, cur = row["id"], row["status"]
        prev = state["band"].get(tid, "ok")
        if prev == cur:
            continue
        if cur == "ok":
            title = f"Policy: {tid} back within its band"
            body = f"{tid} {_band_text(row)}"
        else:
            title = (f"Policy: {tid} {cur} its band" if prev == "ok"
                     else f"Policy: {tid} moved from {prev} to {cur} band")
            body = f"{tid} {_band_text(row)}"
            if cur == "under" and alloc.get(tid, 0) > 0:
                body += f"\nNext contribution: EUR {alloc[tid]:.2f} -> {tid}"

        def commit(new, tid=tid, cur=cur):
            if cur == "ok":
                new["band"].pop(tid, None)
            else:
                new["band"][tid] = cur
        edges.append({"kind": "band", "title": title, "body": body,
                      "tags": "balance_scale", "commit": commit,
                      "dedupe": f"policy:band:{tid}:{cur}:{today}"})

    limit = payload["maxSingleStock"]["limitPct"]
    now_over = set(payload["maxSingleStock"]["breaches"])
    was_over = set(state["single"])
    # an unpriced holding has an unknown weight: neither in nor out
    unknown = {i for w in payload["warnings"] if w["code"] == "unpriced" for i in w["ids"]}

    def single_in(new, tid):
        new["single"].append(tid)

    def single_out(new, tid):
        if tid in new["single"]:
            new["single"].remove(tid)

    for tid in sorted(now_over - was_over):
        edges.append({
            "kind": "single", "title": f"Policy: {tid} above the single-stock limit",
            "body": f"{tid} is {payload['current'].get(tid, 0):.1f}% of the portfolio, "
                    f"above the {limit:g}% single-stock limit",
            "tags": "warning", "dedupe": f"policy:single:{tid}:in:{today}",
            "commit": lambda new, tid=tid: single_in(new, tid)})
    for tid in sorted(was_over - now_over - unknown):
        pct = payload["current"].get(tid)
        edges.append({
            "kind": "single", "title": f"Policy: {tid} back under the single-stock limit",
            "body": (f"{tid} is {pct:.1f}% of the portfolio, " if pct is not None
                     else f"{tid} is no longer held, ")
            + (f"within the {limit:g}% single-stock limit" if limit is not None
               else "no single-stock limit is set"),
            "tags": "white_check_mark", "dedupe": f"policy:single:{tid}:out:{today}",
            "commit": lambda new, tid=tid: single_out(new, tid)})

    def trend_done(new, tid, month_end):
        new["trendAlerted"][tid] = month_end

    for row in payload["trend"]:
        tid = row["id"]
        if not row["changed"] or state["trendAlerted"].get(tid) == row["monthEnd"]:
            continue
        edges.append({
            "kind": "trend", "title": f"Policy: {tid} trend flipped {row['state']}",
            "body": f"{tid} month-end close {row['close']:g} is {row['state']} its "
                    f"10-month average {row['sma10m']:g} (month end {row['monthEnd']})",
            "tags": "chart_with_upwards_trend" if row["state"] == "above"
            else "chart_with_downwards_trend",
            "dedupe": f"policy:trend:{tid}:{row['monthEnd']}",
            "commit": lambda new, tid=tid, me=row["monthEnd"]: trend_done(new, tid, me)})
    return edges


async def _deliver(edge: dict) -> bool:
    """Push one message; True when its state may be consumed (not ``failed``)."""
    try:
        delivery = await notify.alert(
            "POLICY", "policy", edge["title"], edge["body"], priority=PRIORITY,
            tags=edge["tags"], dedupe_key=edge["dedupe"], dedupe_s=86400)
    except Exception:
        log.exception("policy alert failed: %s", edge["title"])
        return False
    if not delivery.consumed:
        log.error("policy alert not accepted (%s): %s", delivery.reason, edge["title"])
    return delivery.consumed


async def run_once() -> dict:
    """One pass -> ``{ok, idle, pushed, failed, note}``. State is saved only
    with the edges whose delivery was consumed."""
    today = _today()
    if not _session_today(today):
        return {"ok": True, "idle": True, "pushed": 0, "failed": 0,
                "note": "no XETR/XAMS session today"}
    payload = await policy_payload()
    watching = (payload["drift"] or payload["maxSingleStock"]["limitPct"] is not None
                or payload["trend"])
    if not payload["configured"] or not watching:
        return {"ok": True, "idle": True, "pushed": 0, "failed": 0,
                "note": "no investmentPolicy rules to watch"}
    if not payload["current"]:
        return {"ok": True, "idle": True, "pushed": 0, "failed": 0,
                "note": "no priced positions"}

    state = _load_state()
    new = _load_state()
    pushed = failed = 0
    band_sent = False
    for edge in _edges(state, payload, today):
        if await _deliver(edge):
            edge["commit"](new)
            pushed += 1
            band_sent = band_sent or edge["kind"] == "band"
        else:
            failed += 1
    for tid in [t for t in new["band"] if t not in payload["targets"]]:
        del new["band"][tid]             # target removed: nothing left to watch

    now = _now()
    out_of_band = [r for r in payload["drift"] if r["status"] != "ok"]
    if not out_of_band:
        new["lastReminder"] = None
    elif band_sent or new["lastReminder"] is None:
        new["lastReminder"] = now
    elif now - new["lastReminder"] >= REMINDER_S:
        reminder = {
            "title": f"Policy reminder: {len(out_of_band)} out of band",
            "body": "\n".join(f"{r['id']} {r['status']}: {_band_text(r)}"
                              for r in out_of_band),
            "tags": "bell", "dedupe": f"policy:reminder:{today}"}
        if await _deliver(reminder):
            new["lastReminder"] = now
            pushed += 1
        else:
            failed += 1
    if failed == 0:
        new["lastPass"] = today.isoformat()
    saved = jsonstore.save(STATE_FILE, new, indent=2)
    if not saved:
        log.error("policy state not saved: the delivered edges will repeat")
    note = (f"{pushed} pushed, {failed} failed; {len(out_of_band)} out of band, "
            f"{len(payload['maxSingleStock']['breaches'])} over the single-stock limit")
    return {"ok": failed == 0 and saved, "idle": False, "pushed": pushed,
            "failed": failed, "note": note}


def _seconds_until_next(hour: int = HOUR, minute: int = MINUTE) -> float:
    now = datetime.datetime.now()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += datetime.timedelta(days=1)
    _stats["next_run"] = time.time() + (when - now).total_seconds()
    return max(30.0, (when - now).total_seconds())


def _catch_up_due() -> bool:
    """Started after 18:00 and today's pass has not completed (a deploy or
    restart must not skip the day)."""
    now = datetime.datetime.now()
    if (now.hour, now.minute) < (HOUR, MINUTE):
        return False
    return _load_state()["lastPass"] != _today().isoformat()


async def _pass_with_retries() -> None:
    for _ in range(MAX_RETRIES + 1):
        started = time.time()
        ok, idle, note = False, False, ""
        try:
            res = await run_once()
            ok, idle, note = res["ok"], res["idle"], res["note"]
            _stats["pushed"] += res["pushed"]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("policy pass failed")
        _stats.update(last_run=time.time(), note=note)
        _stats["runs"] += 1
        runlog.record("policy", ok, time.time() - started, note, idle=idle)
        if ok:
            return
        await asyncio.sleep(RETRY_S)


async def _loop() -> None:
    log.info("policy watch started (daily %02d:%02d local, XETR/XAMS sessions)",
             HOUR, MINUTE)
    first = True
    while True:
        if not (first and _catch_up_due()):
            await asyncio.sleep(_seconds_until_next())
        first = False
        await _pass_with_retries()


def start() -> None:
    global _task
    if os.getenv("POLICY_WATCH_ENABLED", "1") != "1":
        log.info("policy watch disabled (set POLICY_WATCH_ENABLED=1 to enable)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True


async def stop() -> None:
    global _task
    _stats["running"] = False
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


def status() -> dict:
    return {**_stats, "enabled": os.getenv("POLICY_WATCH_ENABLED", "1") == "1",
            "interval_s": INTERVAL_S}
