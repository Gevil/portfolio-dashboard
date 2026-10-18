"""Price-move alerts for the watchlist's held listings (replaces the
standalone stock-watcher pod).

Every ``PRICE_ALERT_INTERVAL_S`` each alertable watchlist ticker is evaluated
against its ``pct-move`` rule (``rules.pct_move``) on ONE price series - the
held listing's quote from ``prices.listing_quote`` (fresh, stale-gated,
market-open only):

* **session move** - ``price`` vs the first listing bar of the current session
  (``prices.session_open_price``): an intraday swing;
* **gap** - ``price`` vs the previous close: the overnight gap the session
  anchor hides (a -6% gap-down opens at the new level, so its session move is
  ~0 and the session rule would never fire).

Rule modes (``rules`` v3):

* ``sigma`` (default) - a move is scaled by the ticker's daily volatility:
  sigma is the EWMA (lambda 0.94) of log returns of ``prices.daily_closes(id,
  "1y")``, excluding the current session's bar. z = move% / sigma%. A move
  alerts when |z| >= zAlert and |move| >= minPct (direction per rule) and is
  hot (priority 5) at |z| >= zHot. The session move climbs a persisted
  **ladder**: after an alert the same direction re-alerts only at a higher
  integer level of |z| or on reaching hot; the ladder re-arms when |z| drops
  below rearmZ or at the next session. A fresh alert in the SAME direction as
  the last one (after a re-arm) also respects ``cooldownMin``. Without sigma
  (history unavailable, too short) the pass falls back to the pct thresholds
  rather than going silent.
* ``pct`` - the fixed thresholdPct / hotPct with a ``cooldownMin`` per key.

The gap rule (both modes) is evaluated only within ``gapWindowMin`` minutes
after the open (``calendars.session_bounds``), at most one alert per session
per ticker, and never when prevClose is None (an unknown previous close must
not be guessed). When both rules fire in one pass they go out as ONE push. A
move beyond ``SANITY_BAND_PCT`` is treated as a possible bad print and needs
the same reading on two consecutive passes before it may fire.

Delivery discipline: state is consumed for every alert ``notify`` accepted
(sent, queued in the durable outbox, filtered by the source cap or severity);
only a ``failed`` delivery leaves it untouched so the next pass retries. State
(``data/price_alerts_state.json``) is saved per accepted push:
``"<ID>#move"`` last pct-mode alert ts, ``"<ID>#z"`` the sigma ladder
``{session, dir, level, hot, ts}``, ``"<ID>#gap_session"`` the session of the
last gap alert (old ``#gap`` cooldown floats are ignored). A ladder re-arm is
persisted on its own (it consumes no alert). The legacy
``price_alert_state.json`` is NOT migrated: it is superseded and ignored.

Every pass also publishes each alertable id's sigma to ``rules.set_sigma`` for
``GET /api/alert-rules``.

Runs as a ``BACKGROUND_MODULES`` worker (start/stop/status/close); each pass
is recorded in ``runlog`` and each ticker is isolated by its own try/except.
"""
import asyncio
import datetime
import logging
import math
import os
import pathlib
import time

from app.api import calendars, jsonstore, notify, prices, registry, runlog
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
# Volatility: RiskMetrics decay over daily log returns; fewer returns than
# MIN_RETURNS is not a volatility estimate.
EWMA_LAMBDA = 0.94
MIN_RETURNS = 20

_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "alerts": 0,
          "running": False, "last_note": ""}
_suspect: dict[str, tuple[float, float]] = {}   # key -> (pct, seen ts)


def _load_state() -> dict:
    """Per-key state; entries of an unexpected type are dropped."""
    raw = jsonstore.load(STATE_FILE, {})
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        k = str(k)
        if k.endswith("#z"):
            if isinstance(v, dict):
                out[k] = v
        elif k.endswith("#gap_session"):
            if isinstance(v, str):
                out[k] = v
        else:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def ewma_sigma(closes: list[float], lam: float = EWMA_LAMBDA,
               min_returns: int = MIN_RETURNS) -> float | None:
    """EWMA daily volatility of the log returns of ``closes`` (oldest first,
    positive): var starts at the first squared return, then
    ``var = lam * var + (1 - lam) * r**2`` per return. None when there are
    fewer than ``min_returns`` returns or the result is not positive."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])
            if a > 0 and b > 0]
    if len(rets) < min_returns:
        return None
    var = rets[0] ** 2
    for r in rets[1:]:
        var = lam * var + (1 - lam) * r * r
    sigma = math.sqrt(var)
    return sigma if sigma > 0 else None


async def sigma_for(sym: str, now: float) -> tuple[float, str] | None:
    """(sigma in %, as-of date of the last close used) for one id, or None.
    The bar of the current session (``calendars.quote_session``) is excluded:
    it is still forming while the alert evaluates against it."""
    rows = await prices.daily_closes(sym, "1y")
    today = calendars.quote_session(calendars.mic_for(sym), now).isoformat()
    series = []
    for d, c in rows or []:
        c = _num(c)
        if c and c > 0 and str(d) < today:
            series.append((str(d), c))
    sigma = ewma_sigma([c for _, c in series])
    if sigma is None:
        return None
    return sigma * 100.0, series[-1][0]


def priority_for(rule: dict, pct: float) -> int | None:
    """ntfy priority (5 = hot, 3 = normal) when ``pct`` breaches the fixed
    pct thresholds of the rule, else None. Direction comes from the rule."""
    up = pct > 0
    direction = rule.get("direction", "both")
    if (direction == "up" and not up) or (direction == "down" and up):
        return None
    if abs(pct) < float(rule["thresholdPct"]):
        return None
    return 5 if abs(pct) >= float(rule["hotPct"]) else 3


def sigma_priority(rule: dict, z: float, pct: float) -> int | None:
    """Priority (5 hot, 3 normal) of a move with z-score ``z`` under the
    sigma rule: |z| >= zAlert and |pct| >= minPct, direction respected."""
    direction = rule.get("direction", "both")
    if (direction == "up" and z < 0) or (direction == "down" and z > 0):
        return None
    if abs(z) < float(rule["zAlert"]) or abs(pct) < float(rule["minPct"]):
        return None
    return 5 if abs(z) >= float(rule["zHot"]) else 3


def ladder_step(st: dict | None, session: str, z: float, pct: float,
                rule: dict, now: float) -> tuple[dict, int | None]:
    """One evaluation of the session-move ladder -> (new state, priority).

    ``st`` is the stored ``{session, dir, level, hot, ts}`` (None = never
    alerted). The priority is None when nothing should fire. The caller
    commits the returned state only once the alert was accepted. ``level`` is
    the highest integer |z| alerted in the current ladder (0 = armed)."""
    st = dict(st) if isinstance(st, dict) else {}
    if st.get("session") != session:          # next session: re-armed
        st = {"session": session, "dir": 0, "level": 0, "hot": False,
              "ts": _num(st.get("ts")) or 0.0}
    az = abs(z)
    if az < float(rule["rearmZ"]):
        st["level"], st["hot"] = 0, False
        return st, None
    prio = sigma_priority(rule, z, pct)
    if prio is None:
        return st, None
    sign = 1 if z > 0 else -1
    level = int(az)
    hot = az >= float(rule["zHot"])
    if st.get("dir") == sign and st.get("level"):
        # armed ladder, same direction: only a higher level or reaching hot
        if not (level > int(st["level"]) or (hot and not st.get("hot"))):
            return st, None
    elif st.get("dir") == sign and now - (_num(st.get("ts")) or 0.0) \
            < float(rule["cooldownMin"]) * 60:
        return st, None                       # re-armed, same direction
    st.update(dir=sign, level=level, hot=hot, ts=now)
    return st, prio


def in_gap_window(mic: str, session: datetime.date, now: float,
                  window_min: float) -> bool:
    """True within ``window_min`` minutes after ``session``'s open."""
    bounds = calendars.session_bounds(mic, session)
    if not bounds:
        return False
    return 0 <= now - bounds[0].timestamp() <= float(window_min) * 60


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


async def check_ticker(sym: str, state: dict,
                       now: float | None = None) -> str:
    """Evaluate one ticker; returns 'alerted', 'skipped' or 'quiet'. Mutates
    ``state`` and persists it only for an accepted push (or a ladder re-arm)."""
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
    mic = calendars.mic_for(sym)
    session_date = calendars.quote_session(mic, now)
    session = session_date.isoformat()
    sigma = None
    if rule.get("mode", "sigma") == "sigma":
        sigma = await sigma_for(sym, now)
    sig_pct = sigma[0] if sigma else None
    cooldown_s = float(rule["cooldownMin"]) * 60
    # (kind, pct, base, label, prio, z)
    fired: list[tuple[str, float, float, str, int, float | None]] = []
    updates: dict = {}
    rearmed = False

    anchor = await _session_anchor(sym)
    if anchor:
        key = f"{sym}#move"
        pct = (price - anchor) / anchor * 100
        if sig_pct:
            zkey = f"{sym}#z"
            prev = state.get(zkey)
            new, prio = ladder_step(prev, session, pct / sig_pct, pct, rule,
                                    now)
            if prio is None:
                _suspect.pop(key, None)
                if prev and (prev.get("level") or prev.get("hot")) \
                        and not (new["level"] or new["hot"]):
                    state[zkey] = new          # re-armed: persisted below
                    rearmed = True
            elif not _confirmed(key, pct, now):
                log.info("%s: move %+.1f%% beyond the sanity band, waiting "
                         "for a second reading", sym, pct)
            else:
                fired.append(("move", pct, anchor, "session open", prio,
                              pct / sig_pct))
                updates[zkey] = new
        elif now - state.get(key, 0.0) >= cooldown_s:
            prio = priority_for(rule, pct)
            if prio is None:
                _suspect.pop(key, None)
            elif not _confirmed(key, pct, now):
                log.info("%s: move %+.1f%% beyond the sanity band, waiting "
                         "for a second reading", sym, pct)
            else:
                fired.append(("move", pct, anchor, "session open", prio, None))
                updates[key] = now

    prev_close = _num(quote.get("prevClose"))
    gkey = f"{sym}#gap_session"
    if prev_close and prev_close > 0 and state.get(gkey) != session \
            and in_gap_window(mic, session_date, now, rule["gapWindowMin"]):
        pct = (price - prev_close) / prev_close * 100
        z = pct / sig_pct if sig_pct else None
        prio = (sigma_priority(rule, z, pct) if sig_pct
                else priority_for(rule, pct))
        if prio is None:
            _suspect.pop(f"{sym}#gap", None)
        elif not _confirmed(f"{sym}#gap", pct, now):
            log.info("%s: gap %+.1f%% beyond the sanity band, waiting for a "
                     "second reading", sym, pct)
        else:
            fired.append(("gap", pct, prev_close, "prev close", prio, z))
            updates[gkey] = session

    if not fired:
        if rearmed and not jsonstore.save(STATE_FILE, state):
            log.error("price-alert state save failed - %s ladder not re-armed "
                      "on disk", sym)
        return "quiet"

    prio = max(f[4] for f in fired)
    lead = max(fired, key=lambda f: abs(f[1]))
    parts = {"move": "in session", "gap": "gap vs prev close"}
    title = f"{sym} " + ", ".join(
        f"{f[1]:+.1f}% {parts[f[0]]}" for f in fired)
    lines = [f"{sym} at {price:.2f} {ccy}".rstrip()]
    for _, pct, base, label, _, z in fired:
        line = f"{pct:+.1f}% vs {label} {base:.2f}"
        if z is not None:
            line = f"{sym} {line} (z {z:+.1f}, \u03c3 {sig_pct:.1f}%)"
        lines.append(line)
    if sig_pct:
        lines.append(f"Alert at |z| >= {float(rule['zAlert']):g} "
                     f"(min {float(rule['minPct']):g}%).")
    else:
        lines.append(f"Threshold {float(rule['thresholdPct']):.1f}%.")
    delivery = await notify.alert(
        sym, "price", title, "\n".join(lines), priority=prio,
        tags=("chart_with_upwards_trend" if lead[1] > 0
              else "chart_with_downwards_trend"),
        severity="error" if prio >= 5 else "warning")
    if not delivery.consumed:
        log.warning("price alert for %s not delivered (%s): state not "
                    "consumed", sym, delivery.reason)
        return "skipped"
    state.update(updates)
    if not jsonstore.save(STATE_FILE, state):
        log.error("price-alert state save failed - %s may alert again", sym)
    if delivery:
        _stats["alerts"] += 1
    return "alerted"


async def _publish_sigma(now: float) -> None:
    """Hand every alertable id's current sigma to the rules endpoint."""
    snapshot: dict[str, dict] = {}
    for entry in registry.entries():
        if not entry.get("alertable"):
            continue
        try:
            found = await sigma_for(entry["id"], now)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("sigma for %s failed: %s", entry["id"], e)
            continue
        if found:
            snapshot[entry["id"]] = {"sigmaPct": round(found[0], 3),
                                     "asOf": found[1]}
    rules_mod.set_sigma(snapshot)


async def check_price_alerts() -> dict:
    """One pass over every alertable ticker; a failing ticker never aborts the
    rest."""
    state = _load_state()
    out: dict[str, int] = {"alerted": 0, "skipped": 0, "quiet": 0, "errors": 0}
    now = time.time()
    await _publish_sigma(now)
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
    log.info("price alerts: mode=%s zAlert=%.1f interval=%ss cooldown=%smin",
             d["mode"], d["zAlert"], INTERVAL_S, d["cooldownMin"])
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
