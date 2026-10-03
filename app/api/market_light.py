"""Market light: one daily green/yellow/red read on the whole tape.

Three dimensions, each scored red=0 / yellow=50 / green=100:

- ``breadth``  — share of ALERTABLE watchlist tickers above their 20-day MA
  (>=60% green, 40-60% yellow, <40% red). Index tickers are excluded by
  ``prices.is_alertable``: GSPC is the market itself, not a holding.
- ``index``    — benchmark close vs its 200-day MA: above green, below red,
  with a +/-2% yellow band so a tape sitting *on* the average does not flip
  the light every day.
- ``momentum`` — benchmark 5-day return: >+1% green, -1..+1% yellow, <-1% red.

The top-level ``status`` is derived from the mean of the AVAILABLE dimensions:
>=66.7 with no red dimension -> green, <=33.3 (or any red dimension while the
mean is under 50) -> red, otherwise yellow. Fewer than two available
dimensions sets ``data_quality: "limited"`` — the light stays advisory, and the
consumer can tell a genuinely mixed tape from a starved one.

One snapshot per day at 21:00 local (``TZ=Europe/Prague``) written to
``data/market_light.json``. A status DROP against the previous snapshot
(green->yellow, green->red, yellow->red) pushes one ntfy warning at priority
4: the light is a risk instrument, so only deteriorations are loud.

Everything is computed from the existing history cache
(``prices.fetch_price_history``) — no new upstream, no new credentials.
The newest daily bar at 21:00 local is the US session's close-so-far (the
light is a live risk read, not a settlement record); MAs are computed over the
daily buckets exactly as the chart shows them.
"""
import asyncio
import datetime
import logging
import os
import pathlib
import time

from app.api import config_store, indicators, jsonstore, notify, prices, runlog

log = logging.getLogger("market_light")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
RETRY_S = 900             # announce not accepted: retry the pass this often
MAX_RETRIES = 4
SNAPSHOT_FILE = DATA_DIR / "market_light.json"

GREEN, YELLOW, RED = "green", "yellow", "red"
# Worst-of-the-tape ordering: a drop is any move DOWN this ladder.
RANK = {GREEN: 2, YELLOW: 1, RED: 0}
SCORE = {GREEN: 100, YELLOW: 50, RED: 0}
LIGHT_TAG = {GREEN: "green_circle", YELLOW: "yellow_circle", RED: "red_circle"}

DEFAULTS = {
    "hour": 21,
    "minute": 0,
    "index": "GSPC",
    "breadth_period": 20,
    "index_period": 200,
    "momentum_days": 5,
    # Breadth needs at least two constituents before it says anything about
    # a tape; with one holding it is just that holding's own trend.
    "min_symbols": 2,
    "breadth_range": "3M",
    "index_range": "1Y",
    "yellow_band_pct": 2.0,
    "green_momentum_pct": 1.0,
    "red_momentum_pct": -1.0,
}

_task: asyncio.Task | None = None
_warming = False
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "running": False,
          "next_run": 0.0, "status": None, "data_quality": None,
          "unpublished": False}


def cfg() -> dict:
    merged = dict(DEFAULTS)
    try:
        block = config_store.read().get("marketLight") or {}
        if isinstance(block, dict):
            merged.update(block)
    except Exception as e:
        log.warning("market_light config read failed: %s", e)
    return merged


def _load(path: pathlib.Path, default):
    return jsonstore.load(path, default)


def _save(path: pathlib.Path, data) -> bool:
    return jsonstore.save(path, data, indent=2)


def current() -> dict | None:
    """The published snapshot (None before the first pass has run)."""
    snap = _load(SNAPSHOT_FILE, None)
    return snap if isinstance(snap, dict) else None


# ---------------------------------------------------------------- dimensions

async def _daily_closes(symbol: str, range_key: str) -> list[float]:
    """Daily closes for one symbol; [] when the history feed has nothing.

    Provider/parse noise degrades the dimension to unavailable instead of
    failing the whole pass — a partial light beats no light.
    """
    try:
        res = await prices.fetch_price_history(symbol, range_key)
    except Exception as e:
        log.debug("history %s (%s) failed: %s", symbol, range_key, e)
        return []
    closes: list[float] = []
    for p in (res or {}).get("data") or []:
        try:
            closes.append(float(p["c"]))
        except (KeyError, TypeError, ValueError):
            continue
    return closes


def _unavailable(detail: str) -> dict:
    return {"score": None, "available": False, "status": None,
            "detail": detail}


async def _dim_breadth(symbols: list[str], c: dict) -> dict:
    """% of alertable watchlist tickers above their MA."""
    period = int(c["breadth_period"])
    above = total = 0
    for sym in symbols:
        closes = await _daily_closes(sym, c["breadth_range"])
        if len(closes) < period + 1:
            continue
        ma = indicators.sma(closes, period)[-1]
        if ma is None or ma <= 0:
            continue
        total += 1
        if closes[-1] > ma:
            above += 1
    if total < int(c["min_symbols"]):
        return _unavailable(f"only {total} of {len(symbols)} alertable tickers "
                            f"have {period + 1}+ daily bars")
    pct = 100.0 * above / total
    status = GREEN if pct >= 60 else YELLOW if pct >= 40 else RED
    return {"score": SCORE[status], "available": True, "status": status,
            "detail": f"{above}/{total} alertable tickers above "
                      f"MA{period} ({pct:.0f}%)"}


def _dim_index(closes: list[float], c: dict) -> dict:
    """Benchmark close vs its long moving average, with a yellow band."""
    period = int(c["index_period"])
    if len(closes) < period + 1:
        return _unavailable(f"{c['index']} has {len(closes)} daily bars, "
                            f"needs {period + 1}")
    ma = indicators.sma(closes, period)[-1]
    if ma is None or ma <= 0:
        return _unavailable(f"MA{period} unavailable for {c['index']}")
    dev = closes[-1] / ma - 1.0
    band = float(c["yellow_band_pct"]) / 100.0
    status = YELLOW if abs(dev) <= band else (GREEN if dev > 0 else RED)
    return {"score": SCORE[status], "available": True, "status": status,
            "detail": f"{c['index']} {dev * 100:+.1f}% vs MA{period}"}


def _dim_momentum(closes: list[float], c: dict) -> dict:
    """Benchmark 5-day return."""
    days = int(c["momentum_days"])
    if len(closes) < days + 1:
        return _unavailable(f"{c['index']} has {len(closes)} daily bars, "
                            f"needs {days + 1}")
    base = closes[-(days + 1)]
    if base <= 0:
        return _unavailable(f"{c['index']} {days}-day base price is not usable")
    ret = (closes[-1] / base - 1.0) * 100.0
    status = (GREEN if ret > float(c["green_momentum_pct"])
              else RED if ret < float(c["red_momentum_pct"]) else YELLOW)
    return {"score": SCORE[status], "available": True, "status": status,
            "detail": f"{c['index']} {days}-day return {ret:+.1f}%"}


def _aggregate(dims: dict) -> tuple[str | None, float | None, list[str]]:
    """-> (status, score, reasons) from the per-dimension results."""
    avail = [d for d in dims.values() if d.get("available")]
    reasons = [f"{name}: {d['detail']} ({d['status']})"
               for name, d in dims.items() if d.get("available")]
    reasons += [f"{name}: unavailable — {d['detail']}"
                for name, d in dims.items() if not d.get("available")]
    if not avail:
        return None, None, reasons + ["no dimension could be computed"]
    score = round(sum(d["score"] for d in avail) / len(avail), 1)
    reds = [d for d in avail if d["status"] == RED]
    if score >= 66.7 and not reds:
        status = GREEN
    elif score <= 33.3 or (reds and score < 50):
        status = RED
    else:
        status = YELLOW
    return status, score, reasons


async def snapshot() -> dict:
    """One reading. Does not touch the published file."""
    c = cfg()
    from app.main import get_watchlist
    symbols = [s for s in get_watchlist() if prices.is_alertable(s)]
    # One benchmark series for both of its dimensions (same cache key, so the
    # second dimension is free — but a cold cache must not fetch it twice).
    idx_closes = await _daily_closes(str(c["index"]), c["index_range"])
    dims = {
        "breadth": await _dim_breadth(symbols, c),
        "index": _dim_index(idx_closes, c),
        "momentum": _dim_momentum(idx_closes, c),
    }
    status, score, reasons = _aggregate(dims)
    avail = sum(1 for d in dims.values() if d.get("available"))
    return {
        "date": datetime.date.today().isoformat(),
        "status": status,
        "score": score,
        "reasons": reasons,
        "dimensions": dims,
        "data_quality": "ok" if avail >= 2 else "limited",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ts": time.time(),
    }


async def _announce(old: str, new: str, snap: dict) -> "notify.Delivery":
    title = f"Market light {old} -> {new}"
    lines = [f"• {r}" for r in snap["reasons"][:6]]
    if snap["data_quality"] == "limited":
        lines.append("• data quality: limited (<2 dimensions available)")
    body = "\n".join(lines)
    delivery = await notify.push(title, body, severity="warning", priority=4,
                                 tags=LIGHT_TAG[new])
    if delivery:
        notify.store("MARKET", "market_light", title, body, priority=4)
    return delivery


async def run_once() -> dict:
    """Compute, announce a status drop, and publish. The snapshot is persisted
    only once the drop alert was accepted (sent, queued in the outbox, or
    deliberately filtered): the drop is detected by comparing against the
    PREVIOUS published snapshot, so publishing first would swallow an
    undelivered alert for good. ``_stats['unpublished']`` tells the loop to
    retry."""
    prev = current() or {}
    snap = await snapshot()
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    _stats["status"] = snap["status"]
    _stats["data_quality"] = snap["data_quality"]
    old, new = prev.get("status"), snap["status"]
    log.info("market light: %s score=%s quality=%s (%s)", new, snap["score"],
             snap["data_quality"], "; ".join(snap["reasons"]))
    if old in RANK and new in RANK and RANK[new] < RANK[old]:
        delivery = await _announce(old, new, snap)
        if not delivery.consumed:
            _stats["unpublished"] = True
            log.error("market light drop alert not accepted (%s) - snapshot "
                      "not persisted, will retry", delivery.reason)
            return snap
    _stats["unpublished"] = not _save(SNAPSHOT_FILE, snap)
    return snap


async def warm_if_missing() -> None:
    """One pass on the first request of a fresh deploy, so the market view is
    not empty until 21:00."""
    global _warming
    if _warming or current() is not None:
        return
    _warming = True
    try:
        await run_once()
    except Exception:
        _stats["errors"] += 1
        log.exception("market_light cold start failed")
    finally:
        _warming = False


def _seconds_until_next(hour: int, minute: int) -> float:
    """Seconds until the next local HH:MM (today or tomorrow)."""
    now = datetime.datetime.now()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += datetime.timedelta(days=1)
    _stats["next_run"] = time.time() + (when - now).total_seconds()
    return max(30.0, (when - now).total_seconds())


async def _loop() -> None:
    log.info("market_light loop started (daily %02d:%02d local)",
             int(cfg()["hour"]), int(cfg()["minute"]))
    while True:
        conf = cfg()
        await asyncio.sleep(_seconds_until_next(int(conf["hour"]),
                                                int(conf["minute"])))
        for attempt in range(MAX_RETRIES + 1):
            started = time.time()
            ok = False
            note = ""
            try:
                snap = await run_once()
                ok = not _stats["unpublished"]
                note = f"{snap['status']} score={snap['score']} " \
                       f"quality={snap['data_quality']}"
                if not ok:
                    note += " (not published)"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _stats["errors"] += 1
                note = str(e)[:200]
                log.exception("market_light pass failed")
            runlog.record("market_light", ok, time.time() - started, note)
            if ok:
                break
            await asyncio.sleep(RETRY_S)


def start() -> None:
    global _task
    if os.getenv("MARKET_LIGHT_ENABLED", "1") != "1":
        log.info("market_light disabled (set MARKET_LIGHT_ENABLED=0)")
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
    return {**_stats, "enabled": os.getenv("MARKET_LIGHT_ENABLED", "1") == "1"}