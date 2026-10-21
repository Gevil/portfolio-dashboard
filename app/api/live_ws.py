"""US live reference feeds (Twelve Data + Finnhub WebSocket) and the engine
lifecycle (feed manager + listing poller).

These feeds stream the NASDAQ/NYSE listing in USD. They are a *reference*:
ticks only land in ``live_prices`` (labelled USD) and are exposed on price
items as ``usLive``. They are never written into the listing series that
valuation, charts and history use (see prices.py) - mixing the two is what
made the 1D chart spike.

Only symbols the registry maps to a streaming provider are subscribed. The
manager re-reads the registry every WATCHLIST_POLL_S and (re)starts a feed when
its symbol set changed, so a watchlist edit needs no restart. One symbol the
provider rejects is dropped (per-symbol fallback) instead of killing the feed.
"""
import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

from app.api import prices as prices_mod
from app.api import registry

log = logging.getLogger(__name__)

TWELVE_DATA_API_KEY = (os.getenv("TWELVE_DATA_API_KEY") or "").strip()
FINNHUB_API_KEY = (os.getenv("FINNHUB_API_KEY") or "").strip()

# {internal id: {"price": float, "ts": int, "source": str, "currency": "USD"}}
# US reference ticks ONLY - not the listing price.
live_prices: dict[str, dict] = {}

# WS connection state ("is the feed streaming right now")
ws_connected: dict[str, bool] = {"twelvedata": False, "finnhub": False}

_feed_tasks: dict[str, tuple[frozenset, asyncio.Task]] = {}
_manager_task: asyncio.Task | None = None
_poll_task: asyncio.Task | None = None
_running = False

# Reconnect policy
WS_BACKOFF_START = 1
WS_BACKOFF_MAX = 60
HEALTHY_SESSION_S = 30        # only a session this long resets the backoff
WS_FAST_FAIL_LIMIT = 3        # sessions shorter than HEALTHY_SESSION_S in a row -> park
WS_FAST_FAIL_PARK = 600
TD_HEARTBEAT_INTERVAL = 10    # Twelve Data drops idle sessions without a heartbeat
WATCHLIST_POLL_S = 15

# A tick older than this is never accepted (providers replay cached payloads).
STALE_TICK_MAX_AGE = 120

# A feed whose every symbol was rejected is not restarted for this symbol set
# until the park expires or the set changes: {provider: (signature, until_ts)}.
FEED_PARK_S = 24 * 3600
_parked: dict[str, tuple[frozenset, int]] = {}

FINNHUB_MAX_SUBS = 50          # free-plan WebSocket symbol limit
# {wire symbol: id the subscription stores ticks under} (watchlist id wins a shared symbol)
_wire_owner: dict[str, str] = {}


def _fx_rate() -> dict:
    """Cached USD->EUR (test seam); never fetches."""
    from app.api import forex
    return forex.cached_rate()


_SESSION_TZ = {"XNYS": "America/New_York", "XETR": "Europe/Berlin"}


def _session_open(mic: str, now: float) -> bool:
    """True while `mic` is inside its regular session at `now` (holidays and
    early closes from the calendars module)."""
    from zoneinfo import ZoneInfo
    from app.api import calendars
    local = datetime.fromtimestamp(now, ZoneInfo(_SESSION_TZ[mic]))
    bounds = calendars.session_bounds(mic, local.date())
    if bounds is None:
        return False
    return bounds[0] <= local < bounds[1]


def us_lead(pid: str, now: float | None = None) -> dict | None:
    """The US price of a universe name as a leading REFERENCE for its XETRA
    listing: {usPrice (USD), eurusd (USD per EUR), impliedEur, xetraDelayedEur,
    gapPct (implied vs XETRA, %), ageS (of the US tick)}. Only while the US and
    the XETRA sessions overlap, and only when every input exists and is fresh
    (US tick, forex rate, XETRA quote); else None. Read-only: nothing here is
    ever written into a listing series."""
    return us_lead_explain(pid, now)[0]


def us_lead_explain(pid: str, now: float | None = None) -> tuple[dict | None, str]:
    """(lead or None, reason): ``ok`` or the first input that is missing
    (no_us_symbol | sessions_closed | no_tick | fx | xetra_quote | xetra_price)."""
    from app.api import paper_universe
    now = time.time() if now is None else float(now)
    sym = paper_universe.us_symbol(pid)
    if not sym:
        return None, "no_us_symbol"
    if not (_session_open("XNYS", now) and _session_open("XETR", now)):
        return None, "sessions_closed"
    key = str(pid).strip().upper()
    tick = live_prices.get(key) or live_prices.get(_wire_owner.get(sym.upper(), ""))
    if not tick:
        return None, "no_tick"
    fx = _fx_rate() or {}
    rate = fx.get("rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or fx.get("stale") \
            or not math.isfinite(rate) or rate <= 0:
        return None, "fx"
    quote = paper_universe.cached_quote(key)
    as_of = quote.get("asOf") if quote else None
    if not quote or quote.get("stale") or not isinstance(as_of, (int, float)) \
            or now - as_of > prices_mod.STALE_OPEN_S:        # the delayed listing must itself be current
        return None, "xetra_quote"
    xetra = quote.get("price")
    if not isinstance(xetra, (int, float)) or isinstance(xetra, bool) \
            or not math.isfinite(xetra) or xetra <= 0:
        return None, "xetra_price"
    us_price = float(tick["price"])
    implied = us_price * float(rate)
    return {
        "usPrice": round(us_price, 4),
        "eurusd": round(1.0 / float(rate), 5),
        "impliedEur": round(implied, 4),
        "xetraDelayedEur": round(float(xetra), 4),
        "gapPct": round((implied / float(xetra) - 1.0) * 100.0, 3),
        "ageS": max(0, int(now - int(tick["ts"]))),
    }, "ok"


def _current_ts() -> int:
    return int(time.time())


def _parse_epoch_ts(value) -> int | None:
    """Provider timestamp -> unix seconds. Accepts seconds/millis (int/float/
    numeric string) and ISO-8601 strings; None when absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or value <= 0:
            return None
        return prices_mod.normalize_ts(int(value))
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return prices_mod.normalize_ts(int(float(raw)))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _apply_tick(ticker: str, price: float, ts: int, source: str,
                max_stale: int | None = STALE_TICK_MAX_AGE):
    """Record a US reference tick. A tick whose own timestamp is older than
    `max_stale` is ignored (replayed/cached quote). Never touches the
    persisted listing series."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return
    if not math.isfinite(price) or price <= 0:
        return
    ts = prices_mod.normalize_ts(ts)
    if max_stale is not None and _current_ts() - ts > max_stale:
        log.debug("Skipping stale tick: %s age=%ds source=%s", ticker, _current_ts() - ts, source)
        return
    existing = live_prices.get(ticker)
    if existing is None or ts >= existing["ts"]:
        live_prices[ticker] = {"price": price, "ts": ts, "source": source, "currency": "USD"}


def _after_session(connected_at: float | None, now: float, backoff: float,
                   fast_fails: int) -> tuple[float, float, int]:
    """Reconnect policy after a session ended -> (sleep_for, backoff, fast_fails).

    - never connected: plain exponential backoff;
    - lasted >= HEALTHY_SESSION_S: healthy, backoff and fast-fail count reset
      (a socket that only said hello and dropped must NOT reset the backoff);
    - shorter: counts as a fast failure; WS_FAST_FAIL_LIMIT in a row park the
      feed for WS_FAST_FAIL_PARK instead of churning reconnects.
    """
    if connected_at is None:
        return backoff, min(backoff * 2, WS_BACKOFF_MAX), fast_fails
    if now - connected_at >= HEALTHY_SESSION_S:
        return WS_BACKOFF_START, WS_BACKOFF_START, 0
    fast_fails += 1
    if fast_fails >= WS_FAST_FAIL_LIMIT:
        return WS_FAST_FAIL_PARK, backoff, 0
    return backoff, min(backoff * 2, WS_BACKOFF_MAX), fast_fails


def _td_symbol_of(entry) -> str | None:
    if isinstance(entry, dict):
        entry = entry.get("symbol")
    entry = str(entry or "").strip().upper()
    return entry or None


async def _twelvedata_ws_loop(sub_map: dict[str, str]):
    """Twelve Data WebSocket client for {wire symbol: internal id}.

    Verified protocol: API key in the connect URL; subscribe payload is
    {"action":"subscribe","params":{"symbols":"AAPL,MSFT"}}; events:
    connection / subscribe-status / price / heartbeat. The feed counts as live
    only after the subscribe ack (or a first price).

    Per-symbol fallback: if the combined subscribe is rejected outright the next
    session subscribes one symbol per message; a symbol reported in `fails` is
    remembered and dropped, the rest keep streaming.
    """
    import websockets

    if not TWELVE_DATA_API_KEY or not sub_map:
        return

    url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={TWELVE_DATA_API_KEY}"
    backoff, fast_fails = WS_BACKOFF_START, 0
    bad: set[str] = set()
    per_symbol = False

    while _running:
        connected_at = None
        active = {w: i for w, i in sub_map.items() if w not in bad}
        if not active:
            parked_until = _current_ts() + FEED_PARK_S
            _parked["twelvedata"] = (frozenset(sub_map.items()), parked_until)
            log.warning("Twelve Data: no usable symbols left (rejected: %s); "
                        "parking this symbol set for %d h", sorted(bad), FEED_PARK_S // 3600)
            return
        protocol_ok = False
        try:
            async with websockets.connect(url) as ws:
                log.info("Twelve Data WebSocket connected; subscribing %s (%s)",
                         sorted(active), "per symbol" if per_symbol else "combined")
                connected_at = time.time()
                if per_symbol:
                    for wire in active:
                        await ws.send(json.dumps({"action": "subscribe",
                                                  "params": {"symbols": wire}}))
                else:
                    await ws.send(json.dumps({"action": "subscribe",
                                              "params": {"symbols": ",".join(active)}}))
                while _running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=TD_HEARTBEAT_INTERVAL)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"action": "heartbeat"}))
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event = str(msg.get("event") or "").strip().lower()
                    status = str(msg.get("status") or "").strip().lower()
                    failed = status in ("error", "fail", "failed")

                    if event == "connection":
                        if failed:
                            log.warning("Twelve Data WS connection rejected: %s",
                                        msg.get("messages") or msg.get("error") or msg)
                            break
                        continue

                    if event == "subscribe-status":
                        ok_list = msg.get("success") or []
                        fails = msg.get("fails") or []
                        bad_now = {s for s in map(_td_symbol_of, fails) if s}
                        if bad_now:
                            bad |= {w for w in bad_now if w in sub_map}
                            log.warning("Twelve Data rejected %s; dropping them, "
                                        "keeping the rest", sorted(bad_now))
                        if (failed or fails) and not ok_list and not protocol_ok:
                            if not per_symbol and len(active) > 1 and not bad_now:
                                per_symbol = True
                                log.warning("Twelve Data combined subscribe failed; "
                                            "retrying per symbol")
                            break
                        if ok_list and not protocol_ok:
                            protocol_ok = True
                            ws_connected["twelvedata"] = True
                            log.info("Twelve Data WS subscribed: %s", ok_list)
                        continue

                    if failed:
                        log.warning("Twelve Data WS error status: %s",
                                    msg.get("messages") or msg.get("error") or msg)
                        break

                    if event == "price" or ("price" in msg and "symbol" in msg):
                        if not protocol_ok:
                            protocol_ok = True
                            ws_connected["twelvedata"] = True
                        wire = str(msg.get("symbol") or "").strip().upper()
                        ticker = sub_map.get(wire)
                        if not ticker:
                            continue
                        try:
                            price = float(msg["price"])
                        except (TypeError, ValueError, KeyError):
                            continue
                        ts = _parse_epoch_ts(msg.get("timestamp"))
                        _apply_tick(ticker, price, ts if ts is not None else _current_ts(),
                                    "twelvedata")
        except (asyncio.CancelledError, KeyboardInterrupt):
            ws_connected["twelvedata"] = False
            raise
        except Exception as e:
            log.warning("Twelve Data WebSocket error: %s", e)
        finally:
            ws_connected["twelvedata"] = False

        if not _running:
            return
        sleep_for, backoff, fast_fails = _after_session(
            connected_at, time.time(), backoff, fast_fails)
        if sleep_for >= WS_FAST_FAIL_PARK:
            log.warning("Twelve Data WS fast-failed %dx; parking %ds",
                        WS_FAST_FAIL_LIMIT, sleep_for)
        else:
            log.warning("Twelve Data WebSocket session ended, reconnecting in %.1fs", sleep_for)
        await asyncio.sleep(sleep_for)


async def _finnhub_ws_loop(sub_map: dict[str, str]):
    """Finnhub trade WebSocket for {wire symbol: internal id}.

    Token in the connect URL, one {"type":"subscribe","symbol":S} per symbol,
    server sends {"type":"trade","data":[...]} / ping / error. A feed-level
    error drops the session; there is no per-symbol ack, so a symbol that never
    answers simply stays silent - the registry already excludes unmapped ones.
    """
    import websockets

    if not FINNHUB_API_KEY or not sub_map:
        return

    url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
    backoff, fast_fails = WS_BACKOFF_START, 0

    while _running:
        connected_at = None
        protocol_ok = False
        try:
            async with websockets.connect(url) as ws:
                log.info("Finnhub WebSocket connected; subscribing %s", sorted(sub_map))
                connected_at = time.time()
                for wire in sub_map:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": wire}))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msg_type = msg.get("type")
                    if msg_type == "error":
                        log.warning("Finnhub WS error: %s", msg.get("msg") or msg)
                        break
                    if msg_type in ("trade", "ping") and not protocol_ok:
                        protocol_ok = True
                        ws_connected["finnhub"] = True
                    if msg_type != "trade":
                        continue
                    for t in msg.get("data") or []:
                        ticker = sub_map.get(str(t.get("s") or "").strip().upper())
                        price, ts_raw = t.get("p"), t.get("t")
                        if not ticker or price is None or ts_raw is None:
                            continue
                        ts = _parse_epoch_ts(ts_raw)
                        if ts is not None:
                            _apply_tick(ticker, price, ts, "finnhub")
        except (asyncio.CancelledError, KeyboardInterrupt):
            ws_connected["finnhub"] = False
            raise
        except Exception as e:
            log.warning("Finnhub WebSocket error: %s", e)
        finally:
            ws_connected["finnhub"] = False

        if not _running:
            return
        sleep_for, backoff, fast_fails = _after_session(
            connected_at, time.time(), backoff, fast_fails)
        if sleep_for >= WS_FAST_FAIL_PARK:
            log.warning("Finnhub WS fast-failed %dx; parking %ds", WS_FAST_FAIL_LIMIT, sleep_for)
        else:
            log.warning("Finnhub WebSocket session ended, reconnecting in %.1fs", sleep_for)
        await asyncio.sleep(sleep_for)


# ------------------------------------------------------------------ exchange sessions
# Decided by the exchange of the listing that produced the price (registry),
# not by a hard-coded ticker set.

def is_eu_symbol(ticker: str) -> bool:
    """True when the entry's listing trades on a European venue."""
    return registry.is_eu(ticker)


def market_open(ticker: str) -> bool:
    """True when the entry's listing venue is in its regular session."""
    return registry.market_open(ticker)


# ------------------------------------------------------------------ feed manager

def _desired(provider: str) -> dict[str, str]:
    """{wire symbol: internal id} for every entry the registry maps to `provider`."""
    out: dict[str, str] = {}
    for e in registry.entries():
        wire = (e.get("providers") or {}).get(provider)
        if wire:
            out[wire.upper()] = e["id"]
    return out


def _universe_source() -> str:
    """``paperTrading.universeSource`` (test seam); anything unreadable -> watchlist."""
    try:
        from app.api import paper_buckets
        return paper_buckets.universe_source()
    except Exception:
        return "watchlist"


def _universe_us_wires() -> dict[str, str]:
    """{US wire symbol: paper_universe id} of the open universe's US names
    (Trading 212-mapped, i.e. investable, names first); {} unless the open
    universe is the source."""
    from app.api import paper_buckets, paper_universe
    if _universe_source() != paper_buckets.PAPER_UNIVERSE:
        return {}
    rows = [r for r in paper_universe.entries() if r.get("role") == "candidate" and r.get("us")]
    rows.sort(key=lambda r: not r.get("t212"))        # stable: mapped first, file order inside
    return {r["us"].upper(): r["id"] for r in rows}


def _desired_finnhub() -> dict[str, str]:
    """Finnhub subscriptions: the watchlist's symbols first, then the open
    universe's US symbols, never more than FINNHUB_MAX_SUBS in total (free plan)."""
    out = dict(list(_desired("finnhub").items())[:FINNHUB_MAX_SUBS])
    for wire, pid in _universe_us_wires().items():
        if len(out) >= FINNHUB_MAX_SUBS:
            break
        out.setdefault(wire, pid)
    return out


def _feed_desired(provider: str) -> dict[str, str]:
    return _desired_finnhub() if provider == "finnhub" else _desired(provider)


async def _cancel(task: asyncio.Task | None):
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _is_parked(provider: str, sig: frozenset) -> bool:
    """True while ``provider`` is parked for exactly this symbol-set signature.
    A changed signature or an expired park clears the entry."""
    entry = _parked.get(provider)
    if entry is None:
        return False
    if entry[0] != sig or _current_ts() >= entry[1]:
        _parked.pop(provider, None)
        return False
    return True


async def _sync_feeds_once():
    """Start/restart/stop each feed so it matches the registry's symbol set."""
    for provider, key, loop_fn in (
        ("twelvedata", TWELVE_DATA_API_KEY, _twelvedata_ws_loop),
        ("finnhub", FINNHUB_API_KEY, _finnhub_ws_loop),
    ):
        desired = _feed_desired(provider) if key else {}
        sig = frozenset(desired.items())
        cur = _feed_tasks.get(provider)
        if cur and cur[0] == sig and not cur[1].done():
            continue
        if cur:
            await _cancel(cur[1])
            _feed_tasks.pop(provider, None)
        if not desired:
            continue
        if _is_parked(provider, sig):
            continue
        if cur and cur[0] != sig:
            log.info("%s symbol set changed -> %s", provider, sorted(desired))
        _feed_tasks[provider] = (sig, asyncio.create_task(loop_fn(desired)))
    wanted = {i for p in ("twelvedata", "finnhub") for i in _feed_desired(p).values()}
    _wire_owner.clear()
    _wire_owner.update(_feed_desired("finnhub"))
    for ticker in list(live_prices):
        if ticker not in wanted:
            live_prices.pop(ticker, None)


async def _feed_manager():
    try:
        while _running:
            try:
                await _sync_feeds_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("feed manager pass failed")
            await asyncio.sleep(WATCHLIST_POLL_S)
    finally:
        for _, task in list(_feed_tasks.values()):
            await _cancel(task)
        _feed_tasks.clear()


async def start():
    """Start the feed manager (US reference ticks) and the listing poller.
    Call from app startup."""
    global _running, _manager_task, _poll_task
    if _running:
        return
    _running = True
    _manager_task = asyncio.create_task(_feed_manager())
    _poll_task = asyncio.create_task(prices_mod.listing_poll_loop())
    log.info("live data engine started for: %s", registry.ids())


async def stop():
    """Stop feeds and the poller. Call from app shutdown."""
    global _running, _manager_task, _poll_task
    _running = False
    for t in (_manager_task, _poll_task):
        await _cancel(t)
    _manager_task = None
    _poll_task = None
    for _, task in list(_feed_tasks.values()):
        await _cancel(task)
    _feed_tasks.clear()
    ws_connected.update({"twelvedata": False, "finnhub": False})
