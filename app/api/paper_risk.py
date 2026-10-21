"""Paper-trading risk engine (plan §6.2, contract §2). PURE: no I/O, no broker/http/config/prices imports.

`plan_orders` turns PM target weights into order intents for the practice bucket. Ids absent from `targets`
are left untouched (no instruction means no trade). Caps skip a whole intent (never a partial trade) except
the cash and budget limits, which shrink a buy.

Open-universe limits (only enforced when the key is present in ``limits``; the
``paper_buckets`` merge supplies them for ``universeSource: paper_universe``):
``maxSectorPct`` scales the targets of one sector (``universe[id]["sector"]``)
down to the cap, ``minPositionEur`` zeroes a target worth less than that (a
held one is sold out, a new one is skipped) and ``maxPositions`` drops the
lowest-weighted NEW names beyond the count (held names are never dropped: no
instruction means no trade). Skipped reasons: ``below_min_position``,
``max_positions``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR

OPEN_DELAY_MIN = 30
CUTOFF_BEFORE_CLOSE_MIN = 30

# plan §5 defaults, used for keys missing from `limits`
DEFAULT_LIMITS = {
    "maxWeightPct": {"equity": 40, "etf": 100},
    "minCashPct": 2,
    "maxTurnoverPctPerDay": 30,
    "maxOrdersPerDay": 6,
    "minTradeEur": 25,
    "rebalanceBandPct": 3,
    "adjustFraction": 0.15,
    "maxSpreadOrStaleS": 900,
    "limitSlippageBps": 30,
}


@dataclass(frozen=True)
class Intent:
    id: str
    ticker: str
    side: str            # "buy" | "sell"
    quantity: float
    limit_price: float
    ref_price: float
    ref_price_as_of: int | None
    value_eur: float     # quantity * ref_price
    reason: str


def trading_window(open_dt: datetime, close_dt: datetime, now_dt: datetime) -> bool:
    """True when now >= open + 30 min and now < close - 30 min (close_dt is the real, possibly early, close)."""
    start = open_dt + timedelta(minutes=OPEN_DELAY_MIN)
    end = close_dt - timedelta(minutes=CUTOFF_BEFORE_CLOSE_MIN)
    return start <= now_dt < end


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _round_down(q: float, decimals: int) -> float:
    d = Decimal(repr(float(q))).quantize(Decimal(1).scaleb(-int(decimals)), rounding=ROUND_DOWN)
    return float(d)


def _limit_price(ref: float, side: str, bps: float) -> float:
    """ref * (1 +/- bps/1e4) to 2 decimals: buys round up, sells round down (exact decimal arithmetic)."""
    factor = Decimal(repr(float(bps))) / Decimal(10000)
    r = Decimal(repr(float(ref)))
    raw = r * (1 + factor) if side == "buy" else r * (1 - factor)
    mode = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    return float(raw.quantize(Decimal("0.01"), rounding=mode))


def _apply_open_limits(clean: dict, *, lim: dict, universe: dict, held: dict, prices: dict,
                       equity: float, skipped: list, warnings: list) -> None:
    """Mutate ``clean`` (target % per id) per the open-universe limits."""
    cap = _num(lim.get("maxSectorPct"))
    if cap is not None:
        by_sector: dict[str, list[str]] = {}
        for id_, w in clean.items():
            sector = (universe.get(id_) or {}).get("sector")
            if w > 0 and isinstance(sector, str) and sector.strip():
                by_sector.setdefault(sector.strip().lower(), []).append(id_)
        for sector, members in sorted(by_sector.items()):
            total = sum(clean[i] for i in members)
            if total > cap:
                scale = cap / total
                for i in members:
                    clean[i] *= scale
                warnings.append(f"sector_capped:{sector}")
    min_eur = _num(lim.get("minPositionEur"))
    if min_eur is not None and min_eur > 0:
        for id_ in sorted(clean):
            w = clean[id_]
            if 0 < w and w / 100.0 * equity < min_eur - 1e-9:
                if id_ in held:
                    clean[id_] = 0.0                 # a held name that shrank below the minimum is sold
                else:
                    del clean[id_]
                    skipped.append({"id": id_, "reason": "below_min_position"})
    max_pos = _num(lim.get("maxPositions"))
    if max_pos is not None:
        untouched = sum(1 for id_ in held if id_ in prices and id_ not in clean)
        wanted = [id_ for id_, w in clean.items() if w > 0]
        over = untouched + len(wanted) - int(max_pos)
        # new names first, lowest weight first, id as the tie-break
        new = sorted((id_ for id_ in wanted if id_ not in held), key=lambda i: (clean[i], i))
        for id_ in new[:max(0, over)]:
            del clean[id_]
            skipped.append({"id": id_, "reason": "max_positions"})


US_LEAD_MAX_GAP_PCT = 1.5
US_LEAD_MAX_AGE_S = 120
T212_REF_MAX_AGE_S = 120    # a broker (t212) price this fresh may be a SELL's reference


def _us_lead_blocks(us_lead: dict[str, dict] | None, id_: str) -> bool:
    """True when a BUY of ``id_`` must be skipped: its US lead (``live_ws.us_lead``
    result, passed in by the caller) shows |gapPct| > 1.5 and is <= 120 s old.
    A missing, malformed or older lead never blocks (it is a reference only)."""
    lead = (us_lead or {}).get(id_)
    if not isinstance(lead, dict):
        return False
    gap, age = _num(lead.get("gapPct")), _num(lead.get("ageS"))
    if gap is None or age is None:
        return False
    return abs(gap) > US_LEAD_MAX_GAP_PCT and age <= US_LEAD_MAX_AGE_S


def plan_orders(*, targets: dict[str, float], cash_pct: float | None,
                holdings: dict[str, float], cash_eur: float,
                quotes: dict[str, dict], universe: dict[str, dict],
                kinds: dict[str, str], limits: dict, budget_eur: float,
                decimals: dict[str, int], now: float,
                session_ok: dict[str, bool],
                orders_today: int, turnover_today_eur: float,
                exempt_caps: bool = False, us_lead: dict[str, dict] | None = None,
                held_prices: dict[str, dict] | None = None) -> dict:
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    max_w = {**DEFAULT_LIMITS["maxWeightPct"], **(lim.get("maxWeightPct") or {})}
    min_cash = float(lim["minCashPct"])
    band = float(lim["rebalanceBandPct"])
    min_trade = float(lim["minTradeEur"])
    max_age = float(lim["maxSpreadOrStaleS"])
    slip_bps = float(lim["limitSlippageBps"])
    slip = slip_bps / 1e4
    skipped: list[dict] = []
    warnings: list[str] = []

    # -- 1. universe filter, weight clipping, cash floor, renormalisation
    clean: dict[str, float] = {}
    for id_ in sorted(targets):
        if id_ not in universe:
            skipped.append({"id": id_, "reason": "not_in_universe"})
            continue
        w = _num(targets[id_])
        if w is None or w < 0:
            skipped.append({"id": id_, "reason": "bad_target"})
            continue
        if not exempt_caps:
            cap = _num(max_w.get(kinds.get(id_, "equity")))
            if cap is None:
                cap = 100.0
            if w > cap:
                w = cap
        clean[id_] = w
    total_w = sum(clean.values())
    cp = _num(cash_pct)
    cash_t = max(cp if cp is not None else 100.0 - total_w, min_cash)
    room = 100.0 - cash_t
    if total_w > room and total_w > 0:
        scale = max(room, 0.0) / total_w
        clean = {k: v * scale for k, v in clean.items()}
        total_w = sum(clean.values())
    applied = {k: round(v, 6) for k, v in clean.items()}

    # -- 2. valuation
    prices: dict[str, float] = {}
    for id_ in sorted(set(clean) | set(holdings)):
        q = quotes.get(id_) or {}
        p = _num(q.get("price"))
        if p is not None and p > 0:
            prices[id_] = p
    held: dict[str, float] = {}
    for id_, qty in holdings.items():
        f = _num(qty)
        if f is not None and f > 0 and id_ in universe:
            held[id_] = f
    positions = 0.0
    for id_, qty in sorted(held.items()):
        if id_ in prices:
            positions += qty * prices[id_]
        else:
            warnings.append(f"equity_excludes_unpriced:{id_}")
    equity = float(cash_eur) + positions
    result = {"intents": [], "skipped": skipped, "targetsApplied": applied,
              "equityEur": equity, "warnings": warnings}
    if equity <= 0:
        warnings.append("no_equity")
        for id_ in sorted(clean):
            skipped.append({"id": id_, "reason": "no_equity"})
        return result
    if any(k in lim for k in ("maxSectorPct", "minPositionEur", "maxPositions")) and not exempt_caps:
        _apply_open_limits(clean, lim=lim, universe=universe, held=held, prices=prices,
                           equity=equity, skipped=skipped, warnings=warnings)
        result["targetsApplied"] = {k: round(v, 6) for k, v in clean.items()}

    # -- 3. per-id pre-trade checks and raw trades
    frac = _num(lim.get("adjustFraction"))
    frac = frac if frac is not None and 0 < frac <= 1 else 1.0
    flatten = bool(clean) and cash_pct is not None and _num(cash_pct) == 100.0 \
        and all(w <= 0 for w in clean.values())   # flat-at-close / go-to-cash: full exit
    effective: dict[str, float] = {}
    result["targetsEffective"] = effective
    sells: list[tuple] = []   # (id, qty, price, actual, target)
    buys: list[tuple] = []    # (id, value_eur, price, actual, target)
    for id_ in sorted(clean):
        q = quotes.get(id_) or {}
        if id_ not in prices:
            skipped.append({"id": id_, "reason": "no_quote"})
            continue
        if q.get("stale"):
            skipped.append({"id": id_, "reason": "stale_quote"})
            continue
        as_of = _num(q.get("asOf"))
        if as_of is None:
            skipped.append({"id": id_, "reason": "quote_age_unknown"})
            continue
        if now - as_of > max_age:
            skipped.append({"id": id_, "reason": "quote_too_old"})
            continue
        if not session_ok.get(id_, False):
            skipped.append({"id": id_, "reason": "session_closed"})
            continue
        price = prices[id_]
        qty_held = held.get(id_, 0.0)
        actual = qty_held * price / equity * 100.0
        target = clean[id_]
        diff = target - actual
        if not exempt_caps and abs(diff) <= band:
            skipped.append({"id": id_, "reason": "within_band"})
            effective[id_] = round(actual, 6)
            continue
        if not exempt_caps and not flatten:
            min_pos = _num(lim.get("minPositionEur")) or 0.0
            min_step = max(min_trade, min_pos if qty_held <= 0 else 0.0) / equity * 100.0
            step = min(abs(diff), max(frac * abs(diff), min_step))
            target = actual + (step if diff > 0 else -step)
            if clean[id_] <= 0 and qty_held > 0 and min_pos > 0 \
                    and target / 100.0 * equity < min_pos - 1e-9:
                target = 0.0                       # the remainder would be dust: sell out fully
            diff = target - actual
        effective[id_] = round(target, 6)
        value = abs(diff) / 100.0 * equity
        if diff < 0:
            value = min(value, qty_held * price)
        if value < min_trade:
            skipped.append({"id": id_, "reason": "below_min_trade"})
            continue
        if diff < 0:
            raw = qty_held if target <= 0 else min(qty_held, value / price * (1 + 1e-9))
            sells.append((id_, raw, price, actual, target))
        else:
            if _us_lead_blocks(us_lead, id_):
                skipped.append({"id": id_, "reason": "us_lead_gap"})
                effective[id_] = round(actual, 6)
                continue
            buys.append((id_, value, price, actual, target))

    # -- 4. caps state
    if exempt_caps:
        orders_left = math.inf
        turnover_left = math.inf
    else:
        orders_left = int(lim["maxOrdersPerDay"]) - int(orders_today)
        turnover_left = float(lim["maxTurnoverPctPerDay"]) / 100.0 * equity - float(turnover_today_eur)

    intents: list[Intent] = []

    def _mk(id_, side, qty, price, actual, target, as_of=None) -> Intent:
        if as_of is None:
            as_of = _num(quotes[id_].get("asOf"))
        return Intent(
            id=id_, ticker=str(universe[id_].get("t212")), side=side, quantity=qty,
            limit_price=_limit_price(price, side, slip_bps), ref_price=price,
            ref_price_as_of=int(as_of) if as_of is not None else None,
            value_eur=qty * price, reason=f"rebalance {actual:.1f}%->{target:.1f}%")

    def _sell_ref(id_, price):
        """(price, asOf) of a SELL: the broker's own price of the held name when
        it is at most T212_REF_MAX_AGE_S old, else the quote's (asOf None = the
        quote's). Buys never use it."""
        t = (held_prices or {}).get(id_)
        p, a = _num((t or {}).get("price")), _num((t or {}).get("asOf"))
        if p is not None and p > 0 and a is not None and 0 <= now - a <= T212_REF_MAX_AGE_S:
            return p, a
        return price, None

    def _caps_ok(id_, value) -> bool:
        nonlocal orders_left, turnover_left
        if orders_left < 1:
            skipped.append({"id": id_, "reason": "order_cap"})
            return False
        if value > turnover_left + 1e-9:
            skipped.append({"id": id_, "reason": "turnover_cap"})
            return False
        orders_left -= 1
        turnover_left -= value
        return True

    # -- 5. sells first (never above held)
    proceeds = 0.0
    sold_value = 0.0
    for id_, raw, price, actual, target in sells:
        price, ref_as_of = _sell_ref(id_, price)
        qty = _round_down(min(raw, held[id_]), decimals.get(id_, 2))
        if qty <= 0:
            skipped.append({"id": id_, "reason": "zero_quantity"})
            continue
        if _limit_price(price, "sell", slip_bps) <= 0:
            skipped.append({"id": id_, "reason": "bad_price"})
            continue
        value = qty * price
        if not _caps_ok(id_, value):
            continue
        intents.append(_mk(id_, "sell", qty, price, actual, target, ref_as_of))
        proceeds += value * (1 - slip)
        sold_value += value

    # -- 6. buys limited by cash (+ expected sell proceeds) and by the budget
    avail = float(cash_eur) + proceeds
    notional = positions - sold_value
    for id_, value, price, actual, target in buys:
        dec = decimals.get(id_, 2)
        limit = _limit_price(price, "buy", slip_bps)
        if limit <= 0:
            skipped.append({"id": id_, "reason": "bad_price"})
            continue
        want = value / price
        by_cash = avail / limit
        by_budget = (float(budget_eur) - notional) / price
        qty = _round_down(max(0.0, min(want, by_cash, by_budget)), dec)
        if qty <= 0:
            reason = "budget_cap" if by_budget <= by_cash else "insufficient_cash"
            if _round_down(want, dec) <= 0:
                reason = "zero_quantity"
            skipped.append({"id": id_, "reason": reason})
            continue
        if qty < _round_down(want, dec):
            warnings.append(f"buy_reduced:{id_}")
        if not _caps_ok(id_, qty * price):
            continue
        intents.append(_mk(id_, "buy", qty, price, actual, target))
        avail -= qty * limit
        notional += qty * price

    result["intents"] = intents
    return result
