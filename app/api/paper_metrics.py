"""Honest evaluation of the paper-trading bucket (Phase 4, contract §2-§3).

Pure functions: every input is passed in, nothing is read from disk or the
network. Wilson and Spearman come from ``scoreboard`` (not copied).

Conventions
- ``equity`` / ``buy_hold`` are ``{iso_date: level}``; only ratios are used, so
  buy_hold may be a growth factor or an index level.
- ``sessions`` is the number of daily returns on the common dates.
- Percent figures are in percent / percentage points, rounded to 4 decimals.
- ``maxDrawdownPct`` is a positive magnitude.
- ``excessCI95`` is a CI for the cumulative (summed) daily excess, in pp.
"""
import datetime
import math
import random
from zoneinfo import ZoneInfo

from app.api import calendars
from app.api.scoreboard import MIN_SAMPLES, WILSON_Z, spearman, wilson

TRADING_DAYS = 252
MIN_SESSIONS = 20
DEFAULT_COST = {"spreadBps": 10.0, "commissionEur": 0.0, "fxBps": 0.0}
MIC = "XETR"
_TZ = ZoneInfo("Europe/Berlin")


def _round(v, nd: int = 4):
    return None if v is None else round(float(v), nd)


def _std(xs: list[float]) -> float | None:
    """Sample standard deviation (ddof=1); None below 2 observations."""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _percentile(sorted_vals: list[float], q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def block_bootstrap_ci(daily_excess: list[float], block: int = 5, n: int = 2000,
                       seed: int = 7) -> tuple[float, float] | None:
    """95 % CI of the cumulative (summed) excess, in the units of the input
    x 100 (daily excess as fractions -> percentage points), from a moving-block
    bootstrap. Deterministic for a given seed. None below 20 observations."""
    data = [float(x) for x in daily_excess]
    size = len(data)
    if size < MIN_SESSIONS:
        return None
    block = max(1, min(int(block), size))
    rng = random.Random(seed)
    n_blocks = -(-size // block)
    last_start = size - block
    sums = []
    for _ in range(max(1, int(n))):
        total = 0.0
        taken = 0
        for _b in range(n_blocks):
            start = rng.randint(0, last_start)
            take = min(block, size - taken)
            total += sum(data[start:start + take])
            taken += take
        sums.append(total * 100.0)
    sums.sort()
    return (_percentile(sums, 0.025), _percentile(sums, 0.975))


def _to_date(value) -> str | None:
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(float(value), _TZ).date().isoformat()
    return None


def _fill_date(row: dict) -> str | None:
    for key in ("date", "updatedAt", "createdAt"):
        d = _to_date(row.get(key))
        if d:
            return d
    return None


def _fill_value(row: dict) -> float | None:
    for key in ("filledValueEur", "filledValue", "value"):
        v = row.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return abs(float(v))
    return None


def _cost_params(cost: dict | None) -> tuple[float, float, float]:
    merged = dict(DEFAULT_COST)
    merged.update({k: v for k, v in (cost or {}).items() if k in DEFAULT_COST and v is not None})
    return (float(merged["spreadBps"]), float(merged["commissionEur"]), float(merged["fxBps"]))


def fill_cost(value: float, cost: dict | None) -> float:
    """Cost of one fill: value x (spreadBps/2 + fxBps)/1e4 + commissionEur."""
    spread, commission, fx = _cost_params(cost)
    return abs(value) * (spread / 2.0 + fx) / 1e4 + commission


def _empty(sessions: int) -> dict:
    return {"sessions": sessions, "returnPct": None, "buyHoldPct": None, "excessPct": None,
            "netReturnPct": None, "netExcessPct": None, "costDragPct": None,
            "maxDrawdownPct": None, "volatilityPct": None, "trackingErrorPct": None,
            "informationRatio": None, "sharpe": None, "sortino": None,
            "betaToBuyHold": None, "turnoverPct": None, "excessCI95": None,
            "verdict": "insufficient_sample"}


def _risk_ratios(rets: list[float], bh_rets: list[float]) -> tuple:
    """(sharpe, sortino, beta to buy & hold) from the daily returns; each None
    below ``MIN_SESSIONS`` returns or when its denominator is zero. Risk-free
    rate 0; sharpe/sortino annualised by sqrt(252); sortino's downside
    deviation is the root mean square of the negative returns over ALL
    returns (target 0); beta is cov / var (sample) against buy & hold."""
    n = len(rets)
    if n < MIN_SESSIONS:
        return None, None, None
    root = math.sqrt(TRADING_DAYS)
    mean = sum(rets) / n
    std = _std(rets)
    sharpe = (mean / std) * root if std and std > 1e-12 else None
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / n)
    sortino = (mean / downside) * root if downside > 1e-12 else None
    bh_mean = sum(bh_rets) / n
    bh_var = sum((b - bh_mean) ** 2 for b in bh_rets) / (n - 1)
    beta = None
    if bh_var > 1e-18:
        cov = sum((a - mean) * (b - bh_mean) for a, b in zip(rets, bh_rets)) / (n - 1)
        beta = cov / bh_var
    return sharpe, sortino, beta


def decision_counts(rows: list[dict]) -> dict:
    """{valid, noChange, invalid, deferred} over stored decision rows: a
    ``deferred`` row is not a decision; of the rest, ``noChange`` is a valid
    no-change answer, ``invalid`` an unusable one, ``valid`` the targets."""
    deferred = sum(1 for r in rows if r.get("deferred"))
    live = [r for r in rows if not r.get("deferred")]
    no_change = sum(1 for r in live if r.get("valid") and r.get("noChange"))
    invalid = sum(1 for r in live if not r.get("valid"))
    return {"valid": len(live) - no_change - invalid, "noChange": no_change,
            "invalid": invalid, "deferred": deferred}


def bucket_metrics(equity: dict[str, float], buy_hold: dict[str, float], fills: list[dict],
                   cost: dict, flows: dict[str, float] | None = None) -> dict:
    """Metrics of the bucket vs buy & hold on the common dates (contract §2)."""
    dates = sorted(d for d in equity if d in buy_hold
                   and isinstance(equity[d], (int, float)) and equity[d] > 0
                   and isinstance(buy_hold[d], (int, float)) and buy_hold[d] > 0)
    sessions = max(0, len(dates) - 1)
    if sessions < 1:
        return _empty(sessions)
    flows = flows or {}

    rets: list[float] = []
    bh_rets: list[float] = []
    for prev, cur in zip(dates, dates[1:]):
        rets.append((equity[cur] - float(flows.get(cur) or 0.0)) / equity[prev] - 1.0)
        bh_rets.append(buy_hold[cur] / buy_hold[prev] - 1.0)
    excess = [a - b for a, b in zip(rets, bh_rets)]

    growth = 1.0
    for r in rets:
        growth *= 1.0 + r
    bh_growth = buy_hold[dates[-1]] / buy_hold[dates[0]]
    ret_pct = (growth - 1.0) * 100.0
    bh_pct = (bh_growth - 1.0) * 100.0
    excess_pct = ret_pct - bh_pct

    first, last = dates[0], dates[-1]
    total_cost = 0.0
    traded = 0.0
    for row in fills or []:
        d, v = _fill_date(row), _fill_value(row)
        if d is None or v is None or d > last:
            continue
        total_cost += fill_cost(v, cost)
        traded += v
    drag_pct = total_cost / equity[first] * 100.0
    net_pct = ret_pct - drag_pct
    net_excess_pct = excess_pct - drag_pct

    peak, mdd = equity[dates[0]], 0.0
    for d in dates:
        peak = max(peak, equity[d])
        mdd = max(mdd, (peak - equity[d]) / peak)

    root = math.sqrt(TRADING_DAYS)
    vol = _std(rets)
    te = _std(excess)
    mean_excess = sum(excess) / len(excess)
    ir = None
    if sessions >= MIN_SESSIONS and te:
        ir = (mean_excess * TRADING_DAYS) / (te * root)
    mean_equity = sum(equity[d] for d in dates) / len(dates)
    sharpe, sortino, beta = _risk_ratios(rets, bh_rets)

    ci = block_bootstrap_ci(excess)
    if ci is None:
        verdict = "insufficient_sample"
    elif ci[0] > 0:
        verdict = "ahead"
    elif ci[1] < 0:
        verdict = "behind"
    else:
        verdict = "inconclusive"
    return {
        "sessions": sessions,
        "returnPct": _round(ret_pct), "buyHoldPct": _round(bh_pct), "excessPct": _round(excess_pct),
        "netReturnPct": _round(net_pct), "netExcessPct": _round(net_excess_pct),
        "costDragPct": _round(drag_pct),
        "maxDrawdownPct": _round(mdd * 100.0),
        "volatilityPct": _round(vol * root * 100.0) if vol is not None else None,
        "trackingErrorPct": _round(te * root * 100.0) if te is not None else None,
        "informationRatio": _round(ir),
        "sharpe": _round(sharpe), "sortino": _round(sortino),
        "betaToBuyHold": _round(beta),
        "turnoverPct": _round(traded / mean_equity * 100.0),
        "excessCI95": None if ci is None else [_round(ci[0]), _round(ci[1])],
        "verdict": verdict,
    }


def version_segments(decisions: list[dict], orders: list[dict], inception) -> list[dict]:
    """Strategy-version activity windows: a version starts on the date of its
    first decision/order row (the earliest version starts at ``inception``);
    ``to`` is the next version's start (None for the latest)."""
    start = _to_date(inception)
    first: dict[str, str] = {}
    for row in list(decisions or []) + list(orders or []):
        ver = row.get("strategyVersion")
        day = _to_date(row.get("date")) or _fill_date(row)
        if not ver or not day:
            continue
        ver = str(ver)
        if ver not in first or day < first[ver]:
            first[ver] = day
    if not first:
        return []
    ordered = sorted(first.items(), key=lambda kv: (kv[1], kv[0]))
    out = []
    for i, (ver, day) in enumerate(ordered):
        frm = start if (i == 0 and start) else day
        if start and frm < start:
            frm = start
        out.append({"strategyVersion": ver, "from": frm,
                    "to": ordered[i + 1][1] if i + 1 < len(ordered) else None})
    return out


# ---- decision grading (contract §3) ---------------------------------------

def _close_on_or_before(series: list[tuple[str, float]], day: str):
    best = None
    for d, c in series:
        if d <= day:
            best = (d, c)
        else:
            break
    return best


def _decision_weights(row: dict) -> dict[str, float] | None:
    out = row.get("output") or {}
    targets = out.get("targets")
    if not isinstance(targets, dict) or not targets:
        plan = row.get("plan") or {}
        targets = plan.get("targetsApplied")
    if not isinstance(targets, dict) or not targets:
        return None
    clean = {}
    for k, v in targets.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            clean[str(k)] = float(v)
    return clean or None


def _horizon_cell(h: int, graded: list[dict], pending: int, unpriced: int,
                  pairs_a: list[float], pairs_r: list[float]) -> dict:
    n = len(graded)
    hits = sum(1 for g in graded if g["outcomePct"] > 0)
    interval = wilson(hits, n, WILSON_Z)
    return {
        "horizon": h, "n": n, "pending": pending, "unpriced": unpriced, "hits": hits,
        "hitRatePct": _round(hits / n * 100.0) if n else None,
        "wilson95": None if interval is None else [_round(interval[0]), _round(interval[1])],
        "meanOutcomePct": _round(sum(g["outcomePct"] for g in graded) / n) if n else None,
        "ic": _round(spearman(pairs_a, pairs_r)),
        "icPairs": len(pairs_a),
        "minSamples": MIN_SAMPLES,
        "insufficient_sample": n < MIN_SAMPLES,
    }


def grade_decisions(decisions: list[dict], closes: dict[str, list[tuple[str, float]]],
                    horizons=(5, 20), baseline: dict[str, float] | None = None) -> dict:
    """Grade valid decisions by active weight (target - baseline, percent) x
    listing return over T+h XETR sessions (T = the decision date's session).

    no_change rows are graded as "held" at the weights in effect (last valid
    targets, else the baseline). Rows whose exit session has no close yet are
    pending. -> {"horizons": {"5": cell, ...}, "rows": [...], "warnings": [...]}
    """
    baseline = {str(k): float(v) for k, v in (baseline or {}).items()}
    series = {k: sorted((str(d), float(c)) for d, c in v) for k, v in (closes or {}).items()}
    warnings: list[str] = []
    warned: set[str] = set()

    def warn(msg: str) -> None:
        if msg not in warned:
            warned.add(msg)
            warnings.append(msg)

    rows_in = [r for r in decisions or [] if r.get("valid") and not r.get("deferred")]
    rows_in.sort(key=lambda r: (str(r.get("date") or ""), float(r.get("createdAt") or 0)))

    current = dict(baseline)
    rows_out: list[dict] = []
    acc = {int(h): {"graded": [], "pending": 0, "unpriced": 0, "a": [], "r": []} for h in horizons}

    for row in rows_in:
        day = _to_date(row.get("date"))
        if not day:
            continue
        targets = None if row.get("noChange") else _decision_weights(row)
        kind = "held" if targets is None else "targets"
        if targets is not None:
            current = dict(targets)
        weights = current
        ids = sorted(set(weights) | set(baseline))
        active = {i: weights.get(i, 0.0) - baseline.get(i, 0.0) for i in ids}
        moving = {i: a for i, a in active.items() if abs(a) > 1e-12}
        out_row = {"decisionId": row.get("decisionId"), "date": day,
                   "strategyVersion": row.get("strategyVersion"), "kind": kind,
                   "active": {i: _round(a) for i, a in active.items()}, "horizons": {}}
        rows_out.append(out_row)
        if not moving:
            for h in acc:
                out_row["horizons"][str(h)] = {"status": "flat"}
            continue
        t_day = calendars.session_on_or_before(MIC, datetime.date.fromisoformat(day))
        for h, cell in acc.items():
            exit_day = calendars.add_sessions(MIC, t_day, h).isoformat()
            rets: dict[str, float] = {}
            status = "graded"
            for i in ids:
                s = series.get(i)
                if not s:
                    if i in moving:
                        warn(f"no closes for {i}")
                        status = "unpriced"
                    continue
                entry = _close_on_or_before(s, t_day.isoformat())
                if entry is None or entry[1] <= 0:
                    if i in moving:
                        warn(f"no entry close for {i} on {t_day.isoformat()}")
                        status = "unpriced"
                    continue
                if s[-1][0] < exit_day:
                    if i in moving and status == "graded":
                        status = "pending"
                    continue
                exit_ = _close_on_or_before(s, exit_day)
                rets[i] = (exit_[1] / entry[1] - 1.0) * 100.0
            if status == "pending":
                cell["pending"] += 1
                out_row["horizons"][str(h)] = {"status": "pending", "maturesOn": exit_day}
                continue
            if status == "unpriced":
                cell["unpriced"] += 1
                out_row["horizons"][str(h)] = {"status": "unpriced"}
                continue
            outcome = sum(moving[i] / 100.0 * rets[i] for i in moving)
            cell["graded"].append({"outcomePct": outcome})
            for i in ids:
                if i in rets:
                    cell["a"].append(active[i])
                    cell["r"].append(rets[i])
            out_row["horizons"][str(h)] = {
                "status": "graded", "exitDate": exit_day, "outcomePct": _round(outcome),
                "hit": outcome > 0, "returnsPct": {i: _round(v) for i, v in rets.items()}}

    for r in rows_out:
        sts = [c.get("status") for c in r["horizons"].values()]
        r["status"] = ("graded" if "graded" in sts else "pending" if "pending" in sts
                       else (sts[0] if sts else "pending"))
    cells = {str(h): _horizon_cell(h, c["graded"], c["pending"], c["unpriced"], c["a"], c["r"])
             for h, c in acc.items()}
    return {"horizons": cells, "rows": rows_out, "warnings": warnings}
