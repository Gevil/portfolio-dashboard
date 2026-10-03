"""Price-move alerts for the watchlist's held listings (replaces the
standalone stock-watcher pod).

Every ``PRICE_ALERT_INTERVAL_S`` each alertable watchlist ticker is evaluated
against its ``pct-move`` rule (``rules.pct_move``: threshold, hot threshold,
direction, cooldown) on ONE price series - the held listing's quote from
``prices.listing_quote`` (fresh, stale-gated, market-open only):

* **session move** - ``price`` vs the first listing bar of the current session
  (``prices.session_open_price``): an intraday swing;
* **gap** - ``price`` vs the previous close: the overnight gap the session
  anchor hides (a -6% gap-down opens at the new level, so its session move is
  ~0 and the session rule would never fire).

Each has its own cooldown key. When both fire in one pass they go out as ONE
push. A move beyond ``SANITY_BAND_PCT`` is treated as a possible bad print and
needs the same reading on two consecutive passes before it may fire.

Delivery discipline: a cooldown is consumed for every alert ``notify`` accepted
(sent, queued in the durable outbox, or deliberately filtered); only a
``failed`` delivery leaves it untouched so the next pass retries. State
(``data/price_alerts_state.json``: ``{"<ID>#move"|"<ID>#gap": last alert ts}``)
is saved per accepted push. The legacy ``price_alert_state.json`` (one
``{ticker: {ts, pct}}`` cooldown map for the old single rule) is NOT migrated:
it is superseded and ignored - at worst one alert repeats once after deploy.

Runs as a ``BACKGROUND_MODULES`` worker (start/stop/status/close); each pass
is recorded in ``runlog`` and each ticker is isolated by its own try/except.
"""
import asyncio
import logging
import os
import pathlib
import time

from app.api import jsonstore, notify, prices, registry, runlog
from app.api import rules as rules_mod

log = logging.getLogger(__name__)

INTERVAL_S = int(os.getenv("PRICE_ALERT_INTERVAL_S", "60"))
STATE_FILE = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data")) \
    / "price_alerts_state.json"
# A move larger than this may be a bad print or a split; it must repeat on two
# consecutive passes (within SANITY_CONFIRM_PCT of each other) before it fires.
SANITY_BAND_PCT = 15.0
SANITY_CONFIRM_PCT = 2.0
# The first bar of a session must sit this close to the exchange open to count
# as the session anchor (a feed that connected mid-session has a first bar,
# but it is not the open).
ANCHOR_MAX_LAG_S = 900

_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "alerts": 0,
          "running": False, "last_note": ""}
_suspect: dict[str, tuple[float, float]] = {}   # key -> (pct, seen ts)


def _load_state() -> dict[str, float]:
    raw = jsonstore.load(STATE_FILE, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def priority_for(rule: dict, pct: float) -> int | None:
    """ntfy priority (5 = hot, 3 = normal) when ``pct`` breaches the rule, else
    None. Direction and threshold come from the rule."""
    up = pct > 0
    direction = rule.get("direction", "both")
    if (direction == "up" and not up) or (direction == "down" and up):
        return None
    if abs(pct) < float(rule["thresholdPct"]):
        return None
    return 5 if abs(pct) >= float(rule["hotPct"]) else 3


def _confirmed(key: str, pct: float, now: float) -> bool:
    """True when ``pct`` is inside the sanity band, or repeats a reading from
    the previous pass."""
    if abs(pct) <= SANITY_BAND_PCT:
        _suspect.pop(key, None)
        return True
    prev = _suspect.get(key)
    _suspect[key] = (pct, now)
    return bool(prev and now - prev[1] <= 3 * INTERVAL_S
                and abs(prev[0] - pct) <= SANITY_CONFIRM_PCT)


async def _session_anchor(sym: str) -> float | None:
    """Price of the first listing bar of the current session, only when that
    bar is the open (within ANCHOR_MAX_LAG_S of it)."""
    found = await prices.session_open_price(sym)
    if not found:
        return None
    price, ts = found
    window = registry.session_window(sym)
    if window and ts - window[0] > ANCHOR_MAX_LAG_S:
        return None
    price = _num(price)
    return price if price and price > 0 else None


async def check_ticker(sym: str, state: dict[str, float],
                       now: float | None = None) -> str:
    """Evaluate one ticker; returns 'alerted', 'skipped' or 'quiet'. Mutates
    ``state`` and persists it only for an accepted push."""
    now = time.time() if now is None else now
    rule = rules_mod.pct_move(sym)
    if rule is None:          # no pct-move rule: the user silenced this ticker
        return "skipped"
    quote = await prices.listing_quote(sym)
    if not quote or quote.get("stale") or not quote.get("marketOpen"):
        return "skipped"
    price = _num(quote.get("price"))
    if not price or price <= 0:
        return "skipped"
    ccy = quote.get("currency") or ""
    session = await _session_anchor(sym)
    bases = (("move", session, "session open"),
             ("gap", _num(quote.get("prevClose")), "prev close"))
    cooldown_s = float(rule["cooldownMin"]) * 60
    fired: list[tuple[str, float, float, str, int]] = []
    for kind, base, label in bases:
        if not base or base <= 0:
            continue
        key = f"{sym}#{kind}"
        if now - state.get(key, 0.0) < cooldown_s:
            continue
        pct = (price - base) / base * 100
        prio = priority_for(rule, pct)
        if prio is None:
            _suspect.pop(key, None)
            continue
        if not _confirmed(key, pct, now):
            log.info("%s: %s %+.1f%% beyond the sanity band, waiting for a "
                     "second reading", sym, kind, pct)
            continue
        fired.append((key, pct, base, label, prio))
    if not fired:
        return "quiet"

    prio = max(f[4] for f in fired)
    lead = max(fired, key=lambda f: abs(f[1]))
    parts = {"move": "in session", "gap": "gap vs prev close"}
    title = f"{sym} " + ", ".join(
        f"{f[1]:+.1f}% {parts[f[0].split('#')[1]]}" for f in fired)
    lines = [f"{sym} at {price:.2f} {ccy}".rstrip()]
    for _, pct, base, label, _ in fired:
        lines.append(f"{pct:+.1f}% vs {label} {base:.2f}")
    lines.append(f"Threshold {float(rule['thresholdPct']):.1f}%.")
    delivery = await notify.alert(
        sym, "price", title, "\n".join(lines), priority=prio,
        tags=("chart_with_upwards_trend" if lead[1] > 0
              else "chart_with_downwards_trend"),
        severity="error" if prio >= 5 else "warning")
    if not delivery.consumed:
        log.warning("price alert for %s not delivered (%s): cooldown not "
                    "consumed", sym, delivery.reason)
        return "skipped"
    for key, *_ in fired:
        state[key] = now
    if not jsonstore.save(STATE_FILE, state):
        log.error("price-alert state save failed - %s may alert again", sym)
    if delivery:
        _stats["alerts"] += 1
    return "alerted"


async def check_price_alerts() -> dict:
    """One pass over every alertable ticker; a failing ticker never aborts the
    rest."""
    state = _load_state()
    out: dict[str, int] = {"alerted": 0, "skipped": 0, "quiet": 0, "errors": 0}
    now = time.time()
    for entry in registry.entries():
        if not entry.get("alertable"):
            continue
        sym = entry["id"]
        try:
            out[await check_ticker(sym, state, now)] += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            out["errors"] += 1
            log.warning("price-alert check failed for %s: %s", sym, e)
    return out


async def _loop() -> None:
    d = rules_mod.load_public()["default"]
    log.info("price alerts: threshold=%.1f%% interval=%ss cooldown=%smin",
             d["thresholdPct"], INTERVAL_S, d["cooldownMin"])
    while True:
        started = time.time()
        ok = True
        res: dict = {}
        try:
            res = await check_price_alerts()
            ok = res["errors"] == 0
            note = ", ".join(f"{k} {v}" for k, v in res.items() if v)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ok = False
            note = str(e)[:200]
            log.exception("price-alert pass failed")
        if not ok:
            _stats["errors"] += 1
        _stats["runs"] += 1
        _stats["last_run"] = started
        _stats["last_note"] = note
        runlog.record("price_alerts", ok, time.time() - started, note,
                      idle=ok and not res.get("alerted"))
        await asyncio.sleep(INTERVAL_S)


def start() -> None:
    global _task
    if _task is None or _task.done():
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


async def close() -> None:
    """No client of its own (quotes come through prices)."""


def status() -> dict:
    return {**_stats, "interval_s": INTERVAL_S}
