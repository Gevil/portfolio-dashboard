"""Rule engine v2: evaluate every non-pct-move rule and push on edges.

price_alerts.py keeps the pct-move pass (it owns the live-quote baseline and
the sanity band); this module evaluates every other kind in
``rules.resolve()`` once per pass, driven from the same loop at the same
interval.

Edge discipline: the bar-based kinds (ema_cross, rsi_threshold,
volume-spike) are evaluated on CLOSED daily bars only — the bar of the
current session is dropped while the ticker's own exchange is still trading
— and each closed bar is evaluated exactly once, so a signal fires on the
crossing bar and never again on the flat bars that follow it.

Delivery discipline is price_alerts' exactly: cooldown, one-shot and edge
state are consumed when ``notify.push`` ACCEPTED the alert (sent, queued in
the durable outbox, or deliberately filtered by NOTIFY_MIN_SEVERITY). Only a
``failed`` delivery (ntfy down AND the outbox unwritable) rolls the rule's
state entry back to what it was before this pass, so that alert is retried
on the next pass instead of being silently consumed.

Absolute-level rules read ``prices.listing_quote`` (the held listing, the same
series the UI shows) and degrade on a stale or missing quote. Benchmark and
other non-alertable symbols are never evaluated (``prices.is_alertable``).

State lives in ``data/rule_eval_state.json``, one entry per
``"<TICKER>#<rule index>#<kind>"`` key:

  {"ASML#0#ema_cross": {"last_alert_ts": 1789..., "last_bar_t": 1789...,
                        "touch": 1789...},
   "NVDA#1#absolute":  {"last_alert_ts": ..., "side": "above",
                        "level": 1900.0, "fired": true,
                        "fired_at": "2026-09-13", "touch": ...}}

The rule index (not a hash of the rule) keeps the identity stable across
field edits — editing a threshold must not reset a running cooldown.
"""
import asyncio
import datetime
import json
import logging
import os
import pathlib
import time
from datetime import time as dt_time

from app.api import indicators, jsonstore, live_ws, macro, notify, prices, rules
from app.api import shortvolume

log = logging.getLogger("rule_eval")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "rule_eval_state.json"
# Entries untouched for this long are dropped (a deleted rule must not
# leave its cooldown behind forever).
STATE_MAX_AGE_S = 30 * 86400
# ``touch`` is refreshed on every pass a rule exists, but the state file is
# only rewritten for it this often (one write a minute per entry is waste).
TOUCH_REFRESH_S = 86400

# Same cadence as the price-alert loop this pass runs behind.
INTERVAL_S = int(os.getenv("PRICE_ALERT_INTERVAL_S", "60"))
HISTORY_RANGE = "1Y"          # daily bars, prices' per-range cache TTL
MAX_BARS = 260
VOLUME_WINDOW = 20            # prior bars a volume spike is measured against
MIN_VOLUME_BARS = 30          # below this the volume rule can only degrade
MIN_VOLUME_PRIOR = 5
MIN_SHORT_DAYS = 5            # FINRA rows before sigma means anything
EARNINGS_LOCAL = dt_time(9, 0)    # both earnings pushes wait for 09:00 local


_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "running": False,
          "pushed": 0, "last_note": ""}
_task: asyncio.Task | None = None
_in_pass = False


# -------------------------------------------------------------------- state

def _load(path: pathlib.Path, default):
    return jsonstore.load(path, default)


def _save(path: pathlib.Path, data) -> bool:
    return jsonstore.save(path, data, indent=2)


def _state_key(ticker: str, index: int, kind: str) -> str:
    return f"{ticker}#{index}#{kind}"


def _prune(state: dict, live: set[str]) -> dict:
    """Drop entries whose rule is gone AND that were untouched for
    STATE_MAX_AGE_S. An entry whose rule still exists is never dropped,
    whatever its age - a fired one-shot or a running edge baseline is the only
    record that the alert already happened."""
    cutoff = time.time() - STATE_MAX_AGE_S
    keep = {k: v for k, v in state.items() if isinstance(v, dict)
            and (k in live or float(v.get("touch") or 0) >= cutoff)}
    if len(keep) != len(state):
        log.info("rule state pruned: %d -> %d", len(state), len(keep))
    return keep


def _fingerprint(state: dict) -> str:
    """State identity ignoring the housekeeping ``touch`` stamp."""
    return json.dumps({k: {f: x for f, x in v.items() if f != "touch"}
                       for k, v in state.items() if isinstance(v, dict)},
                      sort_keys=True)


def _iso(ts: float) -> str:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _bar_date(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _env(status: str, *, kind: str = "", observed=None, threshold=None,
         data_timestamp: str = "", reason: str = "",
         detail: dict | None = None) -> dict:
    """The evaluation envelope every kind returns."""
    return {"status": status, "observed": observed, "threshold": threshold,
            "data_timestamp": data_timestamp, "kind": kind,
            "reason": reason, "detail": detail or {}}


def _want(rule: dict, key: str, integer: bool = False):
    """A field the kind cannot evaluate without (hand-edited config -> None).

    resolve() already coerces and range-checks; a None here means the entry
    was edited by hand into something unparseable, which must degrade the
    rule rather than raise out of the pass.
    """
    raw = rule.get(key)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return int(round(v)) if integer else v


# ------------------------------------------------------------------- inputs

async def _closed_bars(ticker: str) -> list[dict]:
    """Daily bars with the in-progress bar removed.

    fetch_price_history buckets the last close per UTC DAY and keeps the
    newest point's own timestamp, so "today's partial bar" is the bar that
    falls in the current UTC-day bucket while the ticker's exchange is still
    trading. Once that session has ended the same bar is the day's close and
    is kept — otherwise a signal would always wait for the next session.
    """
    res = await prices.fetch_price_history(ticker, HISTORY_RANGE)
    points = (res or {}).get("data") or []
    bars = sorted((p for p in points
                   if isinstance(p, dict) and p.get("t") and p.get("c")),
                  key=lambda p: p["t"])
    bars = bars[-MAX_BARS:]
    if bars and live_ws.market_open(ticker) and \
            int(bars[-1]["t"]) // 86400 == int(time.time()) // 86400:
        bars = bars[:-1]
    return bars


async def _bars_for(ticker: str, ctx: dict) -> list[dict]:
    """Per-pass memo: one history derivation per ticker, however many rules."""
    if ticker not in ctx["bars"]:
        try:
            ctx["bars"][ticker] = await _closed_bars(ticker)
        except Exception as e:
            log.warning("history unavailable for %s: %s", ticker, e)
            ctx["bars"][ticker] = []
    return ctx["bars"][ticker]


async def _earnings_for(ctx: dict) -> dict:
    if ctx["earnings"] is None:
        try:
            ctx["earnings"] = await macro.earnings() or {}
        except Exception as e:
            log.warning("earnings calendar unavailable: %s", e)
            ctx["earnings"] = {}
    return ctx["earnings"]


def _light(ctx: dict) -> dict:
    """Market-light snapshot (read-only observability feed, may be absent)."""
    if ctx["light"] is None:
        try:
            from app.api import market_light
            ctx["light"] = market_light.current() or {}
        except Exception as e:
            log.debug("market light unavailable: %s", e)
            ctx["light"] = {}
    return ctx["light"]


# ---------------------------------------------------------------- evaluators

async def _eval_absolute(ticker: str, rule: dict, st: dict, ctx: dict) -> dict:
    """Level breach on the live quote, edge-triggered on the side flip."""
    kind = "absolute"
    target = _want(rule, "targetPrice")
    cond = rule.get("condition")
    if target is None or cond not in rules.LEVEL_CONDITIONS:
        return _env("degraded", kind=kind,
                    reason="rule needs targetPrice and ABOVE/BELOW")
    thr = f"{cond} {target:g}"
    try:
        quote = await prices.listing_quote(ticker)
    except Exception as e:
        log.warning("listing quote failed for %s: %s", ticker, e)
        quote = None
    if not quote or quote.get("stale"):
        return _env("degraded", kind=kind, threshold=thr,
                    reason="no fresh listing quote")
    try:
        price = float(quote.get("price"))
        ts = float(quote.get("asOf") or 0)
    except (TypeError, ValueError):
        price, ts = 0.0, 0.0
    if price <= 0:
        return _env("degraded", kind=kind, threshold=thr,
                    reason="no usable listing quote")
    if st.get("level") != target:
        # The level moved: the remembered side describes the old one. Only
        # the edge is re-baselined — cooldown/one-shot belong to the rule,
        # not to the number, so editing a target must not re-arm an alert.
        st.pop("side", None)
        st["level"] = target
    side = "above" if price >= target else "below"
    prev = st.get("side")
    st["side"] = side
    env = _env("not_triggered", kind="absolute", observed=price,
               threshold=thr, data_timestamp=_iso(ts))
    if prev is None:
        env["reason"] = f"baseline: {side} {target:g}"
        return env
    env["observed"] = f"{prev} -> {side}"
    wanted = "above" if cond == "ABOVE" else "below"
    if prev == side or side != wanted:
        # The opposite breach is this rule's reset, not its signal.
        return env
    env["status"] = "triggered"
    env["detail"] = {"price": price, "target": target, "condition": cond}
    return env


async def _eval_ema_cross(ticker: str, rule: dict, st: dict,
                          ctx: dict) -> dict:
    kind = "ema_cross"
    fast = _want(rule, "fast", True)
    slow = _want(rule, "slow", True)
    direction = rule.get("direction")
    if fast is None or slow is None or slow <= fast or \
            direction not in rules.CROSS_DIRECTIONS:
        return _env("degraded", kind=kind,
                    reason="rule needs fast < slow and a cross direction")
    need = fast + slow + 1
    thr = f"EMA{fast}/{slow} {direction}"
    bars = await _bars_for(ticker, ctx)
    if not bars:
        return _env("degraded", kind=kind, reason="no price history",
                    threshold=thr)
    if len(bars) < need:
        return _env("degraded", kind=kind,
                    reason=f"{len(bars)} closed bars, need {need}",
                    threshold=thr, data_timestamp=_bar_date(bars[-1]["t"]))
    bar_t = int(bars[-1]["t"])
    if st.get("last_bar_t") == bar_t:
        return _env("skipped_closed", kind=kind, threshold=thr,
                    data_timestamp=_bar_date(bar_t),
                    reason="closed bar already evaluated")
    closes = [float(b["c"]) for b in bars]
    e_fast, e_slow = indicators.ema(closes, fast), indicators.ema(closes, slow)
    f_now, f_prev = e_fast[-1], e_fast[-2]
    s_now, s_prev = e_slow[-1], e_slow[-2]
    if None in (f_now, f_prev, s_now, s_prev):
        return _env("degraded", kind=kind, reason="indicator warm-up",
                    threshold=thr)
    # One evaluation per closed bar, crossing or not: a closed bar can never
    # change its mind, and this keeps the pass cheap.
    st["last_bar_t"] = bar_t
    bullish = direction == "bullish_cross"
    crossed = (f_prev <= s_prev and f_now > s_now) if bullish \
        else (f_prev >= s_prev and f_now < s_now)
    env = _env("triggered" if crossed else "not_triggered", kind=kind,
               observed=round(f_now - s_now, 4), threshold=thr,
               data_timestamp=_bar_date(bar_t))
    env["detail"] = {"ema_fast": round(f_now, 4), "ema_slow": round(s_now, 4),
                     "close": closes[-1]}
    return env


async def _eval_rsi(ticker: str, rule: dict, st: dict, ctx: dict) -> dict:
    kind = "rsi_threshold"
    period = _want(rule, "period", True)
    threshold = _want(rule, "threshold")
    cond = rule.get("condition")
    if period is None or threshold is None or \
            cond not in rules.RSI_CONDITIONS:
        return _env("degraded", kind=kind,
                    reason="rule needs period, threshold and a condition")
    need = period + 2
    thr = f"RSI{period} {cond} {threshold:g}"
    bars = await _bars_for(ticker, ctx)
    if not bars:
        return _env("degraded", kind=kind, reason="no price history",
                    threshold=thr)
    if len(bars) < need:
        return _env("degraded", kind=kind,
                    reason=f"{len(bars)} closed bars, need {need}",
                    threshold=thr, data_timestamp=_bar_date(bars[-1]["t"]))
    bar_t = int(bars[-1]["t"])
    if st.get("last_bar_t") == bar_t:
        return _env("skipped_closed", kind=kind, threshold=thr,
                    data_timestamp=_bar_date(bar_t),
                    reason="closed bar already evaluated")
    values = indicators.rsi([float(b["c"]) for b in bars], period)
    now_rsi, prev_rsi = values[-1], values[-2]
    if None in (now_rsi, prev_rsi):
        return _env("degraded", kind=kind, reason="indicator warm-up",
                    threshold=thr)
    st["last_bar_t"] = bar_t
    crossed = (prev_rsi < threshold <= now_rsi) if cond == "above" \
        else (prev_rsi > threshold >= now_rsi)
    env = _env("triggered" if crossed else "not_triggered", kind=kind,
               observed=round(now_rsi, 2), threshold=thr,
               data_timestamp=_bar_date(bar_t))
    env["detail"] = {"rsi": round(now_rsi, 2), "prev_rsi": round(prev_rsi, 2)}
    return env


async def _eval_volume(ticker: str, rule: dict, st: dict, ctx: dict) -> dict:
    kind = "volume-spike"
    factor = _want(rule, "factor")
    if factor is None:
        return _env("degraded", kind=kind, reason="rule needs a factor")
    thr = f"{factor:g}x {VOLUME_WINDOW}-bar mean"
    bars = await _bars_for(ticker, ctx)
    with_vol = [b for b in bars if b.get("v") is not None]
    if len(with_vol) < MIN_VOLUME_BARS:
        return _env("degraded", kind=kind, threshold=thr,
                    reason=f"only {len(with_vol)} bars carry volume, "
                           f"need {MIN_VOLUME_BARS}")
    if not bars or bars[-1].get("v") is None:
        return _env("degraded", kind=kind, threshold=thr,
                    reason="newest closed bar carries no volume")
    bar_t = int(bars[-1]["t"])
    if st.get("last_bar_t") == bar_t:
        return _env("skipped_closed", kind=kind, threshold=thr,
                    data_timestamp=_bar_date(bar_t),
                    reason="closed bar already evaluated")
    prior = [float(b["v"]) for b in bars[:-1] if b.get("v") is not None]
    prior = prior[-VOLUME_WINDOW:]
    if len(prior) < MIN_VOLUME_PRIOR:
        return _env("degraded", kind=kind, threshold=thr,
                    reason=f"only {len(prior)} prior volume bars")
    mean = sum(prior) / len(prior)
    volume = float(bars[-1]["v"])
    st["last_bar_t"] = bar_t
    if mean <= 0:
        return _env("degraded", kind=kind, threshold=thr,
                    reason="mean volume is zero")
    ratio = volume / mean
    env = _env("triggered" if ratio >= factor else "not_triggered", kind=kind,
               observed=round(ratio, 2), threshold=thr,
               data_timestamp=_bar_date(bar_t))
    env["detail"] = {"volume": int(volume), "mean": round(mean),
                     "bars": len(prior)}
    return env


async def _eval_earnings(ticker: str, rule: dict, st: dict,
                         ctx: dict) -> dict:
    """One push at T-leadDays (09:00 local) and one on the day itself."""
    lead = int(rule.get("leadDays") or 0)
    kind = "earnings-day"
    thr = f"lead {lead}d"
    row = (await _earnings_for(ctx)).get(ticker) or {}
    raw_date = str(row.get("date") or "")
    try:
        event = datetime.date.fromisoformat(raw_date)
    except ValueError:
        return _env("degraded", kind=kind, threshold=thr,
                    reason="no earnings date cached")
    today = datetime.date.today()
    days = (event - today).days
    if days < 0:
        return _env("skipped_closed", kind=kind, threshold=thr,
                    data_timestamp=raw_date, reason="date has passed")
    slot = "day" if days == 0 else ("lead" if days == lead else None)
    env = _env("not_triggered", kind=kind, observed=days, threshold=thr,
               data_timestamp=raw_date)
    if slot is None:
        return env
    if datetime.datetime.now().time() < EARNINGS_LOCAL:
        env["reason"] = "waiting for 09:00 local"
        return env
    if (st.get("fired_dates") or {}).get(slot) == raw_date:
        env["status"] = "skipped_closed"
        env["reason"] = f"{slot} alert already sent"
        return env
    env["status"] = "triggered"
    env["detail"] = {"slot": slot, "when": row.get("time") or "",
                     "eps_forecast": row.get("epsForecast") or ""}
    # Recorded on the envelope, applied to the state only once the push is
    # delivered (the caller rolls the entry back on a failed push).
    fired = dict(st.get("fired_dates") or {})
    fired[slot] = raw_date
    env["consume"] = {"fired_dates": fired}
    return env


async def _eval_short_ratio(ticker: str, rule: dict, st: dict,
                            ctx: dict) -> dict:
    sigma = float(rule.get("sigma") or 2.0)
    kind = "short-ratio-spike"
    try:
        mean, std, n = shortvolume.mean_ratio(ticker)
        latest = shortvolume.latest(ticker) or {}
    except Exception as e:
        log.warning("short interest unavailable for %s: %s", ticker, e)
        mean, std, n, latest = None, None, 0, {}
    if not latest or mean is None or n < MIN_SHORT_DAYS \
            or latest.get("ratio") is None:
        return _env("degraded", kind=kind,
                    reason=f"short interest history insufficient "
                           f"({n} days)", threshold=f"mean + {sigma:g}σ")
    threshold = mean + sigma * std
    ratio = float(latest["ratio"])
    date = str(latest.get("date") or "")
    thr = f"{threshold:.4f} (mean {mean:.4f} + {sigma:g}σ {std:.4f})"
    env = _env("triggered" if ratio > threshold else "not_triggered",
               kind=kind, observed=round(ratio, 4), threshold=thr,
               data_timestamp=date)
    env["detail"] = {"days": n, "short_volume": latest.get("shortVolume")}
    return env


async def _eval_market_light(ticker: str, rule: dict, st: dict,
                             ctx: dict) -> dict:
    """A drop of the daily market light from one status to another."""
    kind = "market-light-drop"
    frm, to = rule.get("from"), rule.get("to")
    if frm not in rules.LIGHT_STATES or to not in rules.LIGHT_STATES:
        return _env("degraded", kind=kind, reason="rule needs from and to")
    thr = f"{frm} -> {to}"
    snap = _light(ctx)
    cur = snap.get("status")
    stamp = str(snap.get("date") or "")
    if not cur:
        return _env("degraded", kind=kind, threshold=thr,
                    reason="no market light snapshot yet")
    prev = st.get("light_status")
    env = _env("not_triggered", kind=kind,
               observed=cur if prev is None else f"{prev} -> {cur}",
               threshold=thr, data_timestamp=stamp)
    if prev is None:
        st["light_status"] = cur
        env["reason"] = f"baseline: {cur}"
        return env
    if prev == cur:
        return env
    if (prev, cur) != (frm, to):
        # A different transition happened: advance the baseline (nothing for
        # this rule to fire on) so the next drop is still detected.
        st["light_status"] = cur
        env["reason"] = f"{prev} -> {cur} is not this rule's transition"
        return env
    env["status"] = "triggered"
    env["detail"] = {"reasons": [str(r) for r in (snap.get("reasons") or [])],
                     "score": snap.get("score"),
                     "data_quality": snap.get("data_quality")}
    env["consume"] = {"light_status": cur}
    return env


_EVALUATORS = {
    "absolute": _eval_absolute,
    "ema_cross": _eval_ema_cross,
    "rsi_threshold": _eval_rsi,
    "volume-spike": _eval_volume,
    "earnings-day": _eval_earnings,
    "short-ratio-spike": _eval_short_ratio,
    "market-light-drop": _eval_market_light,
}


# ------------------------------------------------------------------ delivery

def _message(ticker: str, kind: str, rule: dict, env: dict) -> tuple:
    """(title, body, priority, tags) for a triggered rule."""
    detail = env.get("detail") or {}
    stamp = env.get("data_timestamp") or ""
    if kind == "absolute":
        cond = str(rule["condition"]).lower()
        target = float(rule["targetPrice"])
        price = float(detail.get("price") or 0)
        return (f"{ticker} crossed {cond} {target:g}",
                f"{ticker} last {price:,.2f} crossed {target:g} "
                f"({cond} rule).",
                None, "target")
    if kind == "ema_cross":
        fast, slow = int(rule["fast"]), int(rule["slow"])
        bullish = rule["direction"] == "bullish_cross"
        return (f"{ticker} EMA{fast}/{slow} "
                f"{'bullish' if bullish else 'bearish'} cross",
                f"Closed bar {stamp}: EMA{fast} "
                f"{float(detail.get('ema_fast') or 0):,.2f} vs EMA{slow} "
                f"{float(detail.get('ema_slow') or 0):,.2f} "
                f"(close {float(detail.get('close') or 0):,.2f}).",
                None, "chart_with_upwards_trend" if bullish
                else "chart_with_downwards_trend")
    if kind == "rsi_threshold":
        return (f"{ticker} RSI{int(rule['period'])} "
                f"{rule['condition']} {float(rule['threshold']):g}",
                f"Closed bar {stamp}: RSI "
                f"{float(detail.get('rsi') or 0):.1f} (previous "
                f"{float(detail.get('prev_rsi') or 0):.1f}) crossed "
                f"{float(rule['threshold']):g}.",
                None, "thermometer")
    if kind == "volume-spike":
        return (f"{ticker} volume spike",
                f"Closed bar {stamp}: "
                f"{int(detail.get('volume') or 0):,} vs "
                f"{int(detail.get('mean') or 0):,} mean over "
                f"{int(detail.get('bars') or 0)} bars = "
                f"{float(env.get('observed') or 0):.1f}x "
                f"(threshold {float(rule['factor']):g}x).",
                None, "warning")
    if kind == "earnings-day":
        lead = int(rule.get("leadDays") or 0)
        when = str(detail.get("when") or "")
        head = f"{ticker} reports earnings today" if env["detail"]["slot"] \
            == "day" else f"{ticker} earnings in {lead} day{'s' if lead != 1 else ''}"
        bits = [f"Date: {stamp}"]
        if when:
            bits.append(f"Session: {when}")
        if detail.get("eps_forecast"):
            bits.append(f"EPS forecast: {detail['eps_forecast']}")
        return (head, ". ".join(bits) + ".", None, "calendar")
    if kind == "short-ratio-spike":
        return (f"{ticker} short-interest spike",
                f"{stamp}: short ratio {float(env.get('observed') or 0):.2f} "
                f"exceeds the 20-day mean + "
                f"{float(rule.get('sigma') or 2.0):g}σ "
                f"({int(detail.get('days') or 0)} days of FINRA data, "
                f"{int(detail.get('short_volume') or 0):,} shares short).",
                None, "warning")
    if kind == "market-light-drop":
        reasons = "; ".join(detail.get("reasons") or []) or "see MARKET view"
        return (f"Market light: {rule['from']} -> {rule['to']}",
                f"Daily market light dropped from {rule['from']} to "
                f"{rule['to']} ({stamp}). {reasons}.",
                4, "rotating_light")
    return (f"{ticker}: {kind}", f"{kind} triggered ({stamp}).", None,
            "warning")


async def _deliver(ticker: str, kind: str, title: str, body: str,
                   priority, tags) -> "notify.Delivery":
    """Push + store through the shared helper; never raises."""
    return await notify.alert(ticker, kind, title, body,
                              priority=priority or 3, tags=tags,
                              severity="warning")


async def _guard_and_eval(ticker: str, index: int, kind: str, rule: dict,
                          st: dict, ctx: dict) -> dict:
    """Expiry / one-shot / cooldown, then the kind's own evaluation."""
    before = dict(st)
    today = datetime.date.today().isoformat()
    expires = rule.get("expiresAt")
    if expires and str(expires) < today:
        return _env("skipped_closed", kind=kind,
                    reason=f"expired {expires}", threshold=rule.get("targetPrice"))
    if rule.get("oneShot") and st.get("fired"):
        return _env("skipped_closed", kind=kind,
                    reason=f"one-shot alert sent {st.get('fired_at')}")
    cooldown = float(rule.get("cooldownMin") or 0) * 60
    if time.time() - float(st.get("last_alert_ts") or 0) < cooldown:
        return _env("skipped_closed", kind=kind, reason="cooldown")

    env = await _EVALUATORS[kind](ticker, rule, st, ctx)
    if env["status"] != "triggered":
        return env

    title, body, priority, tags = _message(ticker, kind, rule, env)
    delivery = await _deliver(ticker, kind, title, body, priority, tags)
    if not delivery.consumed:
        # Neither sent nor queued: rewind the entry (edge baseline included)
        # so the next pass retries instead of treating the signal as consumed.
        st.clear()
        st.update(before)
        env["reason"] = f"push failed ({delivery.reason}); state rolled back"
        return env
    st["last_alert_ts"] = time.time()
    st.update(env.pop("consume", None) or {})
    if rule.get("oneShot"):
        st["fired"] = True
        st["fired_at"] = today
    if delivery:
        _stats["pushed"] += 1
    else:
        env["reason"] = f"alert {delivery.status}"
    log.info("rule alert %s: %s (%s)", ticker, title, delivery.status)
    return env


async def _evaluate_rule(ticker: str, index: int, rule: dict,
                         state: dict, ctx: dict) -> dict:
    kind = str(rule.get("kind") or "")
    if kind == "pct-move":
        # Delivered by price_alerts.check_price_alerts in the same loop.
        return _env("skipped_closed", kind=kind,
                    reason="delivered by the pct-move pass")
    if kind not in rules.KINDS:
        return _env("degraded", kind=kind, reason=f"unknown rule kind: {kind}")
    if not prices.is_alertable(ticker):
        return _env("skipped_closed", kind=kind,
                    reason="not an alertable symbol")
    key = _state_key(ticker, index, kind)
    entry = state.get(key)
    st = dict(entry) if isinstance(entry, dict) else {}
    env = await _guard_and_eval(ticker, index, kind, rule, st, ctx)
    st["touch"] = time.time()       # the rule exists: it is live
    state[key] = st
    return env


async def _evaluate_pass() -> dict:
    from app.main import get_watchlist
    state = _load(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    snapshot = _fingerprint(state)
    touched = {k: float(v.get("touch") or 0) for k, v in state.items()
               if isinstance(v, dict)}
    live: set[str] = set()
    ctx: dict = {"bars": {}, "earnings": None, "light": None}
    summary = {"tickers": 0, "rules": 0, "triggered": 0, "pushed": 0,
               "degraded": 0, "skipped": 0}
    pushed0 = _stats["pushed"]
    for sym in get_watchlist():
        if not prices.is_alertable(sym):
            continue
        summary["tickers"] += 1
        for i, rule in enumerate(rules.resolve(sym)):
            live.add(_state_key(sym, i, str(rule.get("kind") or "")))
            summary["rules"] += 1
            try:
                env = await _evaluate_rule(sym, i, rule, state, ctx)
            except Exception as e:
                log.exception("rule %s#%d (%s) failed", sym, i,
                              rule.get("kind"))
                env = _env("degraded", kind=str(rule.get("kind") or ""),
                           reason=f"evaluation error: {e}")
            status = env.get("status")
            if status == "triggered":
                summary["triggered"] += 1
            elif status == "degraded":
                summary["degraded"] += 1
            elif status == "skipped_closed":
                summary["skipped"] += 1
            if env["status"] == "degraded" and env.get("reason"):
                log.debug("rule %s#%d %s degraded: %s", sym, i,
                          env.get("kind"), env["reason"])
    state = _prune(state, live)
    now = time.time()
    refresh_due = any(now - touched.get(k, 0.0) > TOUCH_REFRESH_S
                      for k in live if k in state)
    summary["changed"] = _fingerprint(state) != snapshot
    if summary["changed"] or refresh_due:
        _save(STATE_FILE, state)
    summary["pushed"] = _stats["pushed"] - pushed0
    return summary


async def evaluate_all() -> dict:
    """One pass over every alertable ticker's resolved rule list."""
    global _in_pass
    if _in_pass:
        return {"skipped": "previous pass still running"}
    _in_pass = True
    started = time.time()
    ok = True
    note = ""
    changed = False
    summary: dict = {}
    try:
        summary = await _evaluate_pass()
        note = (f"{summary.get('pushed', 0)} pushed, "
                f"{summary.get('degraded', 0)} degraded, "
                f"{summary.get('skipped', 0)} skipped")
        changed = bool(summary.get("changed") or summary.get("pushed"))
    except Exception as e:
        ok = False
        note = str(e)[:200]
        _stats["errors"] += 1
        log.exception("rule evaluation pass failed")
    finally:
        _in_pass = False
        _stats["runs"] += 1
        _stats["last_run"] = time.time()
        _stats["last_note"] = note
        _record_run(ok, time.time() - started, note, idle=ok and not changed)
    return summary


def _record_run(ok: bool, duration_s: float, note: str, *,
                idle: bool = False) -> None:
    """Worker observability — must never break the alert pass itself. A quiet
    pass (nothing pushed, no state change) is recorded as idle: it keeps the
    worker's liveness fresh but only leaves a row once an hour."""
    try:
        from app.api import runlog
        runlog.record("rule_eval", ok, duration_s, note, idle=idle)
    except Exception as e:
        log.debug("runlog unavailable: %s", e)


async def _loop() -> None:
    await asyncio.sleep(15)   # let prices/history warm up first
    while True:
        try:
            await evaluate_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rule evaluation loop failed")
        await asyncio.sleep(INTERVAL_S)


def start() -> None:
    """Standalone worker (registered in main.py BACKGROUND_MODULES)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
        _stats["running"] = True
        log.info("rule evaluation: interval=%ss state=%s",
                 INTERVAL_S, STATE_FILE)


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _stats["running"] = False
    _task = None


def status() -> dict:
    # "running" is the worker-lifecycle flag main.py's /api/background panel
    # reads; the per-pass state is _in_pass (re-entrancy guard), reported
    # separately so the two meanings never overwrite each other.
    return {**_stats, "in_pass": _in_pass}