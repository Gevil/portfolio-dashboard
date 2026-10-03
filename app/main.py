import os
import json
import time
import asyncio
import logging
import base64
import hmac
import httpx
from collections import deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from sse_starlette.sse import EventSourceResponse

from app.api import prices, alerts, chat, live_ws
from app.api import reports
from app.api import kb_sync, news_alerts, digest
from app.api import indicators as indicators_mod, rules as rules_mod
from app.api import config_edit, config_store, registry
from app.api import portfolio as portfolio_mod
from app.api import scoreboard as scoreboard_mod
from app.api import edgar, macro
from app.api import (evidence, filings, fundamentals, lane_client, rule_eval,
                     shortvolume)
from app.api import jsonstore, market_light, notify, ops_watch, price_alerts, runlog
from app.api import approvals, jobs, ta_pipeline, triage

# Background workers absorbed from retired sidecar containers/timers.
# Every module here implements start()/async stop()/status(). One that is
# imported but not listed never runs: scoreboard and ops_watch were dead code
# exactly that way (their endpoints kept answering from stale files).
BACKGROUND_MODULES = [notify, kb_sync, news_alerts, jobs, digest, scoreboard_mod,
                      edgar, macro, shortvolume, filings, fundamentals,
                      rule_eval, market_light, triage, ops_watch, price_alerts]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Keep provider API keys out of journald: httpx logs request URLs at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
from app.api import topnews

log = logging.getLogger(__name__)

# Strong refs for supervised background tasks (an unreferenced task can be
# garbage-collected mid-await and its exception is then never seen).
_supervised: set[asyncio.Task] = set()


def _supervise(name: str, factory, *, base_delay: float = 5.0,
               max_delay: float = 300.0, healthy_after: float = 120.0) -> asyncio.Task:
    """Run `factory()` (a coroutine function) under a restarting supervisor.

    An exception is logged with its traceback and the coroutine is restarted
    after an exponential backoff (reset once a run survived `healthy_after`
    seconds); cancellation stops it; a clean return ends it. This replaces the
    old per-task `except Exception: pass` loops that died silently.
    """
    async def runner():
        delay = base_delay
        while True:
            started = time.monotonic()
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("supervised task %s crashed; restarting in %.0fs", name, delay)
            if time.monotonic() - started >= healthy_after:
                delay = base_delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    task = asyncio.create_task(runner(), name=f"supervised:{name}")
    _supervised.add(task)
    task.add_done_callback(_supervised.discard)
    return task


async def _stop_supervised():
    tasks = list(_supervised)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await live_ws.start()
    # The runner is injected so jobs.py never imports the pipeline. This wiring
    # was once missing entirely, which left the queue inert: digest enqueued
    # jobs that no consumer ever ran. The queue must start before digest.
    jobs.set_runner(ta_pipeline.run_job)
    # Proposals left pending by a previous process are resolved at boot.
    approvals.boot_reap()
    _start_background_tasks()
    for m in BACKGROUND_MODULES:
        try:
            m.start()
        except Exception:
            log.exception("background module %s failed to start", m.__name__)
    yield
    await live_ws.stop()
    await _stop_supervised()
    try:
        await prices.flush_stores()
    except Exception:
        log.exception("history/valuation flush on shutdown failed")
    for mod_name in ("app.api.forex", "app.api.providers.twelvedata",
                     "app.api.providers.finnhub"):
        try:
            import importlib
            aclose = getattr(importlib.import_module(mod_name), "aclose", None)
            if aclose:
                await aclose()
        except Exception:
            log.debug("provider client close skipped for %s", mod_name, exc_info=True)
    for m in reversed(BACKGROUND_MODULES):
        try:
            await m.stop()
        except Exception:
            log.debug("background module %s stop failed", m.__name__,
                      exc_info=True)
    try:
        await kb_sync.close()
    except Exception:
        log.debug("kb_sync client close failed", exc_info=True)
    for mod in (shortvolume, filings, fundamentals):
        try:
            await mod.close()
        except Exception:
            log.debug("%s client close failed", mod.__name__, exc_info=True)

app = FastAPI(title="Portfolio Dashboard", lifespan=lifespan)


class _StaticNoCache:
    """
    Pure-ASGI middleware: add "Cache-Control: no-cache" to "/" and "/static/*"
    responses. Browsers then revalidate every load (ETag 304s are cheap), so a
    rebuilt image is never hidden behind a stale heuristic browser cache.
    Only wraps the response-start message — the body stream is untouched, so
    SSE routes are unaffected.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/" or path.startswith("/static/")):
            async def send_with_cache_header(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.append("Cache-Control", "no-cache")
                    headers.append(
                        "Content-Security-Policy",
                        "default-src 'self'; "
                        "img-src 'self' data: https://s3.tradingview.com "
                        "https://www.tradingview.com; "
                        "script-src 'self' https://s3.tradingview.com "
                        "https://www.tradingview.com; "
                        "frame-src https://s3.tradingview.com "
                        "https://www.tradingview.com; "
                        "connect-src 'self'; "
                        "style-src 'self'; "
                        "form-action 'self'; base-uri 'self'; "
                        "frame-ancestors 'none'; object-src 'none'")
                await send(message)

            await self.app(scope, receive, send_with_cache_header)
        else:
            await self.app(scope, receive, send)


app.add_middleware(_StaticNoCache)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")


def _is_approval_callback(method: str, path: str) -> bool:
    """``POST /api/approvals/<id>/approve|deny`` are ntfy action buttons: the
    request comes from the phone with no Basic header, so the per-approval HMAC
    token in the URL is the credential there (checked in the handler). This
    exemption covers exactly those two POST routes; every other method/path
    stays gated."""
    if method != "POST" or not path.startswith("/api/approvals/"):
        return False
    parts = path.rstrip("/").split("/")
    return (len(parts) == 5 and bool(parts[3])
            and parts[4] in ("approve", "deny"))


def _basic_credentials_ok(request: Request) -> bool:
    """Decode + constant-time compare of the Authorization header.

    One implementation shared by the auth gate below and the approval POSTs, so
    there is exactly one credential check in the app. Fail closed: with no
    credential configured (dev) this is False — the gate stays fail-open by
    short-circuiting before it ever calls here.
    """
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        return False
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    # NOTE: the try must cover ONLY the credential decode/compare. Wrapping
    # call_next in it turned any endpoint exception into a misleading
    # "malformed Authorization" 401 (it masked a real 500 in /api/background);
    # endpoint failures must surface as 500.
    try:
        user, _, password = base64.b64decode(header[6:]).decode(
            "utf-8", "replace").partition(":")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII str, which
        # would reject a correct non-ASCII password as "malformed".
        return (hmac.compare_digest(user.encode("utf-8"),
                                    DASHBOARD_USER.encode("utf-8")) and
                hmac.compare_digest(password.encode("utf-8"),
                                    DASHBOARD_PASS.encode("utf-8")))
    except Exception:
        log.warning("malformed Authorization header from %s",
                    request.client.host if request.client else "?")
        return False


@app.middleware("http")
async def basic_auth_gate(request: Request, call_next):
    """Single-user HTTP Basic gate for a LAN-exposed dashboard.

    Enabled only when BOTH DASHBOARD_USER and DASHBOARD_PASS are set (dev
    stays fail-open). /health is exempt (host-dashboard status card);
    everything else — SPA shell, /static assets, /stream (EventSource) and
    all APIs — requires the header. Browsers replay cached Basic
    credentials for every same-origin request once authenticated.
    """
    if not DASHBOARD_USER or not DASHBOARD_PASS or request.url.path in (
            "/health", "/favicon.ico") or _is_approval_callback(
                request.method, request.url.path):
        return await call_next(request)
    if _basic_credentials_ok(request):
        return await call_next(request)
    return Response(status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="stock"'})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """No icon shipped; 204 stops the browser's default request (was a 500
    behind the auth gate)."""
    return Response(status_code=204)


@app.get("/api/background")
async def background_status():
    """Status of the absorbed background workers (kb/news/digest)."""
    return JSONResponse({m.__name__.rsplit(".", 1)[-1]: m.status()
                         for m in BACKGROUND_MODULES})


@app.get("/api/triage")
async def triage_state(limit: int = Query(30, ge=1, le=200)):
    """Triage worker status + its decision log (including what it suppressed —
    the suppression gate is only auditable if the misses are kept)."""
    return JSONResponse({"status": triage.status(),
                         "recent": triage.recent(limit)})


@app.get("/api/approvals")
async def approvals_state(limit: int = Query(30, ge=1, le=200)):
    """Approval queue: what the AI proposed, what is still open, what became of
    it. ``ttl_left_s`` lets the UI show the window before auto-expiry."""
    rows = approvals.list_rows(limit)
    return JSONResponse({
        "counts": approvals.counts(),
        "items": [{**r, "age_s": approvals.age_of(r),
                   "ttl_left_s": approvals.ttl_remaining(r)} for r in rows]})


async def _decide_approval(approval_id: str, approve: bool, token: str,
                           request: Request):
    """Shared body of the two decision endpoints — the one place authorization
    for a decision is checked.

    Two credentials are accepted because there are two callers and neither can
    use the other's: the ntfy action button is tapped on a phone with no session
    and no header, so its capability URL carries ``?token=``; the dashboard UI
    is already a Basic-auth session (the browser replays the credential on every
    same-origin request) and must never learn that token — putting it in a GET
    payload would make every read of the queue a way to mint deep-dive runs.
    The token path is unchanged, and a request proving neither is denied exactly
    as before. Only these two POSTs consult Basic credentials.
    """
    action = "approve" if approve else "deny"
    if (not approvals.check_token(token, approval_id, action)
            and not _basic_credentials_ok(request)):
        return JSONResponse({"ok": False, "error": "invalid token"},
                            status_code=403)
    row, reason = approvals.decide(approval_id, approve)
    if row is None:
        return JSONResponse({"ok": False, "error": reason}, status_code=404)
    if reason != "ok":
        return JSONResponse({"ok": False, "error": reason, "item": row})
    out = {"ok": True, "status": row["status"], "item": row,
           "job_id": None, "deduped": False}
    if approve:
        # The human said go; the queue still serializes it behind any run in
        # flight, and a duplicate for the same ticker dedupes rather than
        # stacking a second full spine on the lane.
        enq = jobs.enqueue(row["ticker"], "deep", source="approval")
        out.update(job_id=enq["job_id"], deduped=enq["deduped"])
        log.info("approval %s %s -> %s job %s%s", row["ticker"], row["id"],
                 row["status"], enq["job_id"], " (deduped)" if enq["deduped"] else "")
    return JSONResponse(out)


@app.post("/api/approvals/{approval_id}/approve")
async def approvals_approve(approval_id: str, request: Request,
                            token: str = Query("")):
    return await _decide_approval(approval_id, True, token, request)


@app.post("/api/approvals/{approval_id}/deny")
async def approvals_deny(approval_id: str, request: Request,
                         token: str = Query("")):
    return await _decide_approval(approval_id, False, token, request)


@app.get("/api/market-light")
async def get_market_light():
    """Daily green/yellow/red market light (app/api/market_light.py).

    A fresh deploy has no snapshot yet: kick one pass instead of serving an
    empty panel until the next scheduled 21:00.
    """
    if market_light.current() is None:
        await market_light.warm_if_missing()
    snap = market_light.current()
    if snap is None:
        return JSONResponse({"available": False, "date": None, "status": None,
                             "score": None, "reasons": [], "dimensions": {},
                             "data_quality": "limited"})
    return JSONResponse(snap)


@app.get("/api/worker-runs")
async def get_worker_runs(limit: int = Query(100, ge=1, le=600),
                          worker: str | None = Query(None)):
    """Worker-run rings merged newest-first (each worker keeps its own ring):
    which worker did what, when, and what it skipped. ``worker`` narrows it to
    one ring. Bare array — the market view renders it directly."""
    return JSONResponse(runlog.recent(limit, worker))


@app.get("/api/indicators/{symbol}")
async def get_indicators(symbol: str, range: str = Query("1M"),
                         ema: str = Query("20,50"), rsi: int = Query(14)):
    sym = symbol.upper()
    if not prices.is_watched(sym):
        return JSONResponse({"detail": "unknown symbol"}, status_code=404)
    rng = (range or "").strip().upper()
    if rng not in prices.RANGES:
        return JSONResponse(
            {"detail": f"range must be one of {', '.join(prices.RANGES)}"},
            status_code=400)
    try:
        periods = tuple(int(x) for x in ema.split(",") if x.strip())
    except ValueError:
        return JSONResponse(
            {"detail": "ema must be comma-separated integers, e.g. 20,50"},
            status_code=400)
    if len(periods) > 4 or any(not 1 <= p <= 499 for p in periods):
        return JSONResponse(
            {"detail": "ema takes at most 4 periods, each between 1 and 499"},
            status_code=400)
    data = await indicators_mod.indicators(
        sym, rng, periods or (20, 50), max(2, min(100, rsi)))
    if not data:
        return JSONResponse({"error": "no data"}, status_code=404)
    return JSONResponse(data)


@app.get("/api/alert-rules")
async def get_alert_rules():
    return JSONResponse(rules_mod.load_public())


@app.post("/api/alert-rules")
async def post_alert_rules(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"},
                            status_code=400)
    try:
        clean = rules_mod.save(body)
    except rules_mod.RuleError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except (config_store.ConfigWriteError,
            config_store.ConfigUnreadable) as e:
        log.error("alert rules not saved: %s", e)
        return JSONResponse({"detail": "config could not be written"},
                            status_code=503)
    return JSONResponse(clean)


@app.get("/api/portfolio")
async def get_portfolio():
    """EUR-native positions, totals, benchmark and warnings."""
    return JSONResponse(await portfolio_mod.snapshot())


@app.get("/api/portfolio/history")
async def get_portfolio_history(range: str = Query("3M")):
    rng = range.upper()
    if rng not in portfolio_mod.HISTORY_RANGES:
        return JSONResponse(
            {"error": "range must be one of "
                      + ", ".join(portfolio_mod.HISTORY_RANGES)},
            status_code=400)
    return JSONResponse(await portfolio_mod.history(rng))


@app.get("/api/advice")
async def get_advice():
    """Latest tracked LLM advice per ticker (digest module state)."""
    return JSONResponse(digest.advice_history())


@app.get("/api/scoreboard")
async def get_scoreboard():
    """Advice-vs-realized-returns scoreboard (T+5/T+20 vs GSPC)."""
    return JSONResponse(await scoreboard_mod.scoreboard())


@app.post("/api/scoreboard/feedback")
async def post_scoreboard_feedback(request: Request):
    """Operator vote on one digest call (``{"ticker","ts","vote":"up|down"}``).

    The dashboard chat is Basic-auth'd already, so no capability token here —
    unlike the ntfy approval links, this is only ever clicked from the UI.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    vote = str(body.get("vote") or "").strip().lower()
    if vote not in ("up", "down"):
        return JSONResponse({"detail": "vote must be 'up' or 'down'"},
                            status_code=400)
    try:
        ts = float(body.get("ts") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"detail": "ts must be a unix timestamp"},
                            status_code=400)
    ticker = str(body.get("ticker") or "").strip().upper()
    if not ticker or not ts:
        return JSONResponse({"detail": "ticker and ts are required"},
                            status_code=400)
    scoreboard_mod.record_feedback(ticker, ts, vote)
    return JSONResponse(scoreboard_mod.feedback_counts())


@app.get("/api/insider")
async def get_insider(symbol: str | None = None):
    """Recent parsed SEC Form 4 filings (US issuers only)."""
    return JSONResponse({"ok": True, "entries": edgar.recent(symbol),
                         "status": edgar.status()})


@app.get("/api/macro")
async def get_macro():
    """Treasury yield curve + FRED 10Y/2Y (cache-first)."""
    curve = await macro.curve()
    fred = {}
    if macro.FRED_KEY:
        fred = {"DGS10": await macro.refresh_fred("DGS10", 5),
                "DGS2": await macro.refresh_fred("DGS2", 5)}
    return JSONResponse({"ok": True, "curve": curve, "fred": fred,
                         "cot": await macro.cot(),
                         "status": macro.status()})


@app.get("/api/earnings")
async def get_earnings():
    """Next earnings dates for the watchlist (Nasdaq calendar cache)."""
    return JSONResponse({"ok": True, "earnings": await macro.earnings()})


app.include_router(shortvolume.router)   # GET /api/shortvolume/{symbol}
app.include_router(filings.router)       # GET /api/filings?symbol=&limit=
app.include_router(fundamentals.router)  # GET /api/fundamentals/{symbol}

app.mount("/static", StaticFiles(directory="static"), name="static")


def _start_background_tasks():
    """
    Start the supervised periodic tasks. Nothing here awaits: the history seed
    runs in its own task, so a slow/hung seed can no longer delay (or take
    down) the prewarm, cleanup, valuation, health-check and top-news loops.
    A crashed task is logged with its traceback and restarted with backoff.
    """
    _supervise("history-seed", _initial_history_seed)
    _supervise("chart-prewarm", _periodic_chart_prewarm)
    _supervise("history-cleanup", _periodic_history_cleanup)
    _supervise("valuation-refresh", _periodic_valuation_refresh)
    _supervise("history-health", _periodic_history_health_check)
    _supervise("topnews", _periodic_topnews)


async def _initial_history_seed():
    """Clean the stored series once, then backfill every thin one."""
    prices._cleanup_history_store()
    await _seed_missing_history(get_watchlist())


async def _periodic_chart_prewarm():
    """
    Every 100s (1D history-cache TTL is 120s; per-range TTLs live in
    prices.HISTORY_CACHE_TTL_BY_RANGE) warm the 1D chart cache of every
    listing so a user request finds it warm.
    """
    while True:
        await prices.prewarm_charts()
        await asyncio.sleep(100)


async def _periodic_history_cleanup():
    """Every 60 seconds: enforce the age-based retention."""
    while True:
        await asyncio.sleep(60)
        prices._cleanup_history_store()


async def _periodic_valuation_refresh():
    """Every 10 minutes: refresh valuation/context (equities only)."""
    await asyncio.sleep(30)
    while True:
        await prices.refresh_valuation()
        await asyncio.sleep(600)


async def _periodic_history_health_check():
    """Every 12 hours: ensure each series has enough history; backfill if not."""
    while True:
        await asyncio.sleep(12 * 3600)
        await _seed_missing_history(get_watchlist())


async def _periodic_topnews():
    """
    Every TOPNEWS_INTERVAL_S (default 15min): fetch per-ticker top news
    (Finnhub today, else GDELT 24h) into the shared cache. When every ticker
    failed in the last pass (provider outage / rate limit), wait twice the
    interval instead of hammering the endpoints again.
    """
    while True:
        watchlist = get_watchlist()
        stats = await topnews.fetch_top_news(watchlist)
        delay = topnews.INTERVAL_S
        if stats and stats.get("total") and not stats.get("ok"):
            delay = topnews.INTERVAL_S * 2
            log.warning("topnews: all providers failed for %s - next pass in %ss",
                        ",".join(stats.get("failed") or []), delay)
        await asyncio.sleep(delay)


async def _seed_missing_history(watchlist: list[str]):
    """Backfill thin series via the registry's yahoo listing symbols."""
    await prices.seed_missing_history(watchlist)


async def _refresh_history_for_symbols(symbols: list[str]):
    """
    Re-seed history for the given ids (new ticker / changed listing). A series
    is swapped only when the new data arrived; a failed refresh keeps the old.
    """
    for symbol in symbols:
        if not await prices.refresh_history(symbol):
            log.warning("reseed: %s failed; stored history kept", symbol)
        await asyncio.sleep(1.5)


# The config file is owned by config_store (atomic writes, last-good copy).
config_store.ensure()


def get_watchlist() -> list[str]:
    """Watchlist ticker ids (prices, analysis, alerts)."""
    return [e["id"] for e in registry.entries()]


def get_watchlist_entries() -> list[dict]:
    """Normalized watchlist entries (see registry.normalize_entry)."""
    return registry.entries()


def get_chat_model() -> str:
    m = str(config_store.read().get("chatModel") or "").strip()

    # Empty config -> "lane" sentinel: chat.py resolves it to the active
    # lane's own model id. Any other value is passed through verbatim.
    return m or "lane"


with open("static/index.html", encoding="utf-8") as f:
    INDEX_HTML = f.read()


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/api/prices")
async def get_prices():
    """One item per watchlist id from its listing series (EUR for holdings).
    The US live feed is a separate, labelled ``usLive`` reference field."""
    return JSONResponse(await prices.fetch_prices(get_watchlist()))


@app.get("/api/ticker/{symbol}")
async def get_ticker_info(symbol: str):
    symbol = symbol.upper()
    if not prices.is_watched(symbol):
        return JSONResponse({"detail": "unknown symbol"}, status_code=404)
    data = await prices.fetch_ticker_info(symbol)
    if not data:
        return JSONResponse({
            "ticker": symbol,
            "status": "no_data",
            "message": "No data available right now (rate limited or not found).",
        })
    return JSONResponse(data)


@app.get("/api/history/{symbol}")
async def get_history(symbol: str, range: str = "1W"):
    symbol = symbol.upper()
    if not prices.is_watched(symbol):
        return JSONResponse({"detail": "unknown symbol"}, status_code=404)
    rng = (range or "").strip().upper()
    if rng not in prices.RANGES:
        return JSONResponse(
            {"detail": f"range must be one of {', '.join(prices.RANGES)}"},
            status_code=400)
    data = await prices.fetch_price_history(symbol, rng)
    if not data:
        return JSONResponse({"error": "no data"}, status_code=404)
    return JSONResponse(data)


@app.get("/api/alerts")
async def get_alerts(limit: int = Query(50, ge=1, le=200)):
    """Alert center: newest-first rows from the local notification store plus
    the unread count (app/api/alerts.py)."""
    return JSONResponse(alerts.snapshot(limit))


@app.post("/api/alerts/ack")
async def post_alerts_ack(request: Request):
    """Mark alerts read: ``{"ids": [...]}`` or ``{"all": true}``."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"},
                            status_code=400)
    ids = body.get("ids")
    ack_all = body.get("all") is True
    if ids is not None and (not isinstance(ids, list) or not all(
            isinstance(i, str) for i in ids)):
        return JSONResponse({"error": "ids must be a list of strings"},
                            status_code=400)
    if not ack_all and not ids:
        return JSONResponse({"error": "ids or all required"}, status_code=400)
    try:
        return JSONResponse({"ok": True, **alerts.ack(ids, ack_all)})
    except OSError as e:
        log.error("alert ack not saved: %s", e)
        return JSONResponse({"ok": False, "error": "ack not saved"},
                            status_code=503)


@app.post("/api/analyse/{symbol}")
async def analyse_ticker(symbol: str, request: Request):
    """Queue a pipeline run — the single start path for analysis.

    ``{"mode": "quick|standard|deep"}`` body (empty body = standard). A ticker
    already queued/running at the same or a STRONGER mode is deduped onto that
    run (``deduped: true``); a weaker in-flight run does not swallow the
    request: the new run is queued behind it. ``mode`` in the answer is the
    mode that will actually run.
    """
    raw = await request.body()
    body: object = {}
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "body must be a JSON object"},
                            status_code=400)
    mode = body.get("mode", "standard")
    if not isinstance(mode, str):
        return JSONResponse({"detail": "mode must be a string"}, status_code=400)
    mode = mode.strip().lower() or "standard"
    sym = symbol.upper()
    if not registry.is_watched(sym):
        return JSONResponse({"detail": f"{sym} is not on the watchlist"},
                            status_code=404)
    if not prices.is_analyzable(sym):
        return JSONResponse({"detail": f"{sym} is not analysable (benchmark, "
                                       "index or fund)"}, status_code=400)
    try:
        return JSONResponse(jobs.enqueue(sym, mode, source="user"))
    except ValueError as e:               # unknown mode / empty symbol
        return JSONResponse({"detail": str(e)}, status_code=400)


@app.get("/api/analysis/{job_id}")
async def analysis_status(job_id: str):
    """The job dict: status/message/mode/decision/result_path."""
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"detail": "job not found (server restarted?)"},
                            status_code=404)
    return JSONResponse(job)


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Honest cancel: only queued work can be stopped — a running spine is
    already spending the lane, and pretending otherwise would lie to the UI."""
    if jobs.cancel(job_id):
        return JSONResponse({"cancelled": True})
    return JSONResponse({"detail": "only a queued job can be cancelled"},
                        status_code=409)


@app.get("/api/jobs")
async def list_jobs(limit: int = Query(50, ge=1, le=200)):
    return JSONResponse(jobs.list_jobs(limit))


@app.post("/api/digest/run")
async def digest_run_now():
    """Trigger a digest batch immediately (the scheduled loop is time-based).
    409 while one is already in flight — the lane is serialised, a second batch
    would only queue behind the first."""
    code, message = await digest.request_run()
    return JSONResponse({"started": code == 202, "message": message},
                        status_code=code)


@app.get("/api/analysis-markers/{symbol}")
async def analysis_markers(symbol: str):
    markers = await reports.get_analysis_markers(symbol.upper())
    return JSONResponse(markers)


@app.get("/api/evidence/{symbol}")
async def evidence_pack(symbol: str):
    """The anti-fabrication pack the spine is allowed to cite: every section
    carries as_of+source or the literal MISSING. Cache reads only — no lane
    call, no provider fan-out (the one FRED exception is TTL-cached)."""
    return JSONResponse(await evidence.build_pack(symbol.upper()))


@app.get("/api/lane-status")
async def lane_status():
    """GPU-lane state for the UI chip and AI Ops: the lane that serves now
    (``lane``/``serving_model``/``model``/``base_url``), its ``role``
    (primary|fallback), the preference order, every candidate lane's probe
    state and today's turn budget. Resolved natively from lanes.conf with a
    few-seconds probe cache; the host-side lane gate is the only thing that
    may (auto-)load a model."""
    return JSONResponse(await lane_client.lane_status())


@app.get("/api/ticker-reports/{symbol}")
async def ticker_reports(symbol: str):
    rows = await reports.get_ticker_reports(symbol.upper())
    return JSONResponse(rows)


@app.get("/api/report/{report_id:path}")
async def report_detail(report_id: str):
    result = await reports.get_report_content(report_id)
    return JSONResponse(result)


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    return await chat.chat_response(body, get_chat_model())


@app.get("/api/models")
async def list_models():
    """
    List available models for the UI selector:
    - Active GPU lane first (the "lane" sentinel always resolves to whichever
      lane is live now; a persisted concrete model id would break on switch).
    - Then Open WebUI models (custom presets); Ollama as fallback.
    """
    lane = await lane_client.active_lane()
    if lane:
        return JSONResponse({"models": [
            {"id": "lane",
             "name": f"Active GPU lane — {lane['name']} ({lane['model_id']})"}]})

    open_webui_url = os.getenv("OPEN_WEBUI_URL", "http://open-webui:8080")
    ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
    api_key = os.getenv("OPEN_WEBUI_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # Try Open WebUI first
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{open_webui_url}/api/v1/models", headers=headers or None)
            if r.status_code == 200:
                data = r.json()
                models = []
                for m in data if isinstance(data, list) else data.get("data", []):
                    iid = (m.get("id") or "").strip()
                    name = (m.get("name") or iid).strip()
                    if iid:
                        models.append({"id": iid, "name": name or iid})
                if models:
                    models.insert(0, {"id": "lane", "name": "Active GPU lane"})
                    return JSONResponse({"models": models})
    except Exception:
        pass

    # Fallback: Ollama
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ollama_url}/api/tags")
            if r.status_code == 200:
                data = r.json()
                models = []
                for m in data.get("models", []):
                    name = (m.get("name") or "").strip()
                    if name:
                        models.append({"id": name, "name": name})
                models.insert(0, {"id": "lane", "name": "Active GPU lane"})
                return JSONResponse({"models": models})
    except Exception:
        pass

    return JSONResponse({"models": []})


@app.get("/api/config")
async def get_config():
    st = config_store.status()
    if st["source"] == "default" and st["error"]:
        return JSONResponse(
            {"error": "stored config is unreadable: " + st["error"]},
            status_code=503)
    return JSONResponse(config_store.read())


@app.get("/api/watchlist")
async def get_watchlist_entries_endpoint():
    """
    Return watchlist with id/symbol/label for frontend.
    """
    entries = get_watchlist_entries()
    return JSONResponse(entries)


@app.post("/api/refresh-history/{symbol}")
async def refresh_history(symbol: str):
    """
    Re-seed history for a watchlist symbol (new ticker / changed listing).
    The stored series is swapped only if the new data arrives.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)
    if not prices.is_watched(symbol):
        return JSONResponse({"detail": "unknown symbol"}, status_code=404)
    _supervise(f"refresh-history:{symbol}",
               lambda: _refresh_history_for_symbols([symbol]))
    return JSONResponse({"status": "started", "symbol": symbol})


@app.put("/api/config")
async def update_config(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"},
                            status_code=400)

    added: list[str] = []

    def mutate(cfg: dict) -> None:
        added[:] = config_edit.apply_update(cfg, body)

    try:
        cfg = await asyncio.to_thread(config_store.update, mutate)
    except config_edit.ConfigInvalid as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except config_store.ConfigUnreadable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except (config_store.ConfigWriteError, TypeError, ValueError) as e:
        log.error("config update failed: %s", e)
        return JSONResponse({"error": f"config not saved: {e}"},
                            status_code=500)

    # Auto-seed history for newly added tickers
    if added:
        asyncio.create_task(_refresh_history_for_symbols(added))

    return JSONResponse(cfg)


from app.api import forex as forex_mod


@app.get("/api/forex/USD/EUR")
async def get_forex_usd_eur():
    """USD->EUR with provenance: {rate, source, asOf, stale}. ``rate`` is null
    when no provider ever answered; a ``stale`` rate (>24h) is display-only."""
    return JSONResponse(await forex_mod.get_rate_usd_eur())


@app.get("/api/alerts-news")
async def get_alerts_news(
    ticker: str | None = Query(None),
    limit: int = Query(20),
):
    """
    Unified alerts/news feed: merges the per-ticker notification store
    (price/news/analysis/lane notes) with the top-news cache, sorted by
    date/time (newest first), last 7 days, capped at 20.
    """
    limit = max(1, min(limit, 20))
    cutoff = time.time() - 7 * 86400

    store = jsonstore.load(notify.NOTIF_STORE, {})
    if not isinstance(store, dict):
        store = {}

    news_cache = topnews.get_top_news()

    def notif_entry(t: str, n: dict) -> dict:
        return {
            "ts": n.get("ts", 0), "kind": n.get("source", "alert"),
            "title": n.get("title", ""), "body": n.get("body", ""),
            "source": n.get("source", ""), "url": n.get("url", ""),
            "priority": n.get("priority", 3), "ticker": t,
        }

    def news_entry(t: str, n: dict) -> dict:
        return {
            "ts": n.get("published_at", 0), "kind": "news",
            "title": n.get("headline", ""), "body": n.get("summary", ""),
            "source": n.get("source", ""), "url": n.get("url", ""),
            "priority": 3, "ticker": t,
        }

    entries: list[dict] = []
    if ticker:
        t = ticker.upper()
        for n in store.get(t, []):
            if n.get("ts", 0) >= cutoff:
                entries.append(notif_entry(t, n))
        for n in news_cache.get(t, {}).get("items", []):
            entries.append(news_entry(t, n))
    else:
        for t, lst in store.items():
            for n in lst:
                if n.get("ts", 0) >= cutoff:
                    entries.append(notif_entry(t, n))
        for t, block in news_cache.items():
            for n in block.get("items", []):
                entries.append(news_entry(t, n))

    entries.sort(key=lambda e: e["ts"], reverse=True)
    return JSONResponse({"ticker": ticker.upper() if ticker else None,
                         "entries": entries[:limit]})


@app.get("/api/top-news")
async def get_top_news(
    ticker: str | None = Query(None),
):
    """Per-ticker top news (Finnhub today / GDELT 24h). Omit ticker -> all."""
    data = topnews.get_top_news(ticker)
    return JSONResponse({"data": data})


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN/inf) with None so the payload
    is valid JSON (the browser's JSON.parse rejects NaN)."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _sse_json(obj) -> str:
    return json.dumps(_json_safe(obj), allow_nan=False, default=str)


class _SseHub:
    """One shared producer for every SSE client.

    The producer polls the (cache-backed) listing prices once per tick for the
    WHOLE watchlist and the alert store every ALERTS_INTERVAL; each client only
    owns a bounded queue. Previously every connected browser ran its own
    polling loop. A price event is emitted per symbol when anything the UI
    shows changed (price, previousClose, change, staleness, US reference), and
    every watchlist symbol is emitted - with its status when it has no price.
    """

    TICK_S = 2.0
    ALERTS_INTERVAL = 30.0
    IDLE_STOP_S = 30.0
    QUEUE_MAX = 256

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()
        self.items: dict[str, dict] = {}
        self._fingerprints: dict[str, tuple] = {}
        # Bounded alert de-dup: the deque keeps insertion order and evicts the
        # oldest id, the mirrored set keeps membership checks O(1).
        self._alert_order: deque = deque(maxlen=1000)
        self._alert_ids: set[str] = set()
        self.recent_alerts: deque = deque(maxlen=50)
        self._task: asyncio.Task | None = None

    @staticmethod
    def _fingerprint(item: dict) -> tuple:
        us = item.get("usLive") or {}
        return (item.get("price"), item.get("previousClose"), item.get("change24h"),
                item.get("change"), item.get("stale"), item.get("status"),
                us.get("price"))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        for item in self.items.values():          # instant snapshot for a fresh browser
            q.put_nowait(("price", _sse_json(item)))
        # No alert replay here: history belongs to the alert center (GET /api/alerts);
        # replaying it made every page load toast the whole backlog.
        self.clients.add(q)
        if self._task is None or self._task.done():
            self._task = _supervise("sse-producer", self._run)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.clients.discard(q)

    def _publish(self, event: str, payload: str):
        for q in list(self.clients):
            if q.full():                           # slow client: drop its oldest event
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass

    async def _prices_pass(self):
        symbols = get_watchlist()
        try:
            fetched = await asyncio.wait_for(prices.fetch_prices(symbols), timeout=30)
        except asyncio.TimeoutError:
            log.warning("SSE: fetch_prices timed out for %s", ",".join(symbols))
            return
        for item in fetched:
            tid = item["ticker"]
            fp = self._fingerprint(item)
            self.items[tid] = item
            if self._fingerprints.get(tid) != fp:
                self._fingerprints[tid] = fp
                self._publish("price", _sse_json(item))
        for tid in list(self.items):                # dropped symbols
            if tid not in symbols:
                self.items.pop(tid, None)
                self._fingerprints.pop(tid, None)

    async def _alerts_pass(self):
        try:
            alert_list = await asyncio.wait_for(alerts.fetch_alerts(), timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("SSE: fetch_alerts unavailable: %s", e)
            return
        first_pass = not getattr(self, "_alerts_primed", False)
        self._alerts_primed = True
        for a in reversed(alert_list or []):        # oldest first, like a live feed
            aid = a.get("id")
            if not aid or aid in self._alert_ids:
                continue
            if len(self._alert_order) == self._alert_order.maxlen:
                self._alert_ids.discard(self._alert_order[0])
            self._alert_order.append(aid)
            self._alert_ids.add(aid)
            if first_pass:                          # existing backlog: remember, never push
                continue
            self.recent_alerts.append(a)
            self._publish("alert", _sse_json(a))

    async def _run(self):
        last_alerts = 0.0
        idle_since: float | None = None
        while True:
            if self.clients:
                idle_since = None
                await self._prices_pass()
                if time.time() - last_alerts > self.ALERTS_INTERVAL:
                    last_alerts = time.time()
                    await self._alerts_pass()
            else:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > self.IDLE_STOP_S:
                    return
            await asyncio.sleep(self.TICK_S)


_sse_hub = _SseHub()


@app.get("/stream")
async def sse_stream(request: Request):
    """SSE endpoint for live price + alert updates (shared producer)."""

    async def event_generator():
        q = _sse_hub.subscribe()
        try:
            yield {"event": "ping", "data": json.dumps({"t": time.time()})}
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=_SseHub.TICK_S)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"t": time.time()})}
                    continue
                yield {"event": event, "data": data}
        finally:
            _sse_hub.unsubscribe(q)

    return EventSourceResponse(event_generator())


@app.post("/api/seed-history")
async def seed_history(
    symbol: str | None = Query(None),
    interval: str = Query("1day"),
    outputsize: int = Query(90),
):
    """
    Backfill the listing series from yahoo (executor, never on the event loop).
    Bars are merged through the validated persist path: non-finite or
    non-positive prices are dropped and the stored series is never replaced.

    Usage:
      - All supported watchlist symbols: POST /api/seed-history
      - One symbol: POST /api/seed-history?symbol=ASML
      - Custom: POST /api/seed-history?interval=5min&outputsize=500
    interval: 1min|5min|15min|30min|1h|1day, outputsize: 1..prices.MAX_SEED_OUTPUTSIZE.
    """
    if interval not in prices.SEED_INTERVALS:
        return JSONResponse(
            {"detail": f"interval must be one of {', '.join(prices.SEED_INTERVALS)}"},
            status_code=400)
    if not 1 <= outputsize <= prices.MAX_SEED_OUTPUTSIZE:
        return JSONResponse(
            {"detail": f"outputsize must be between 1 and {prices.MAX_SEED_OUTPUTSIZE}"},
            status_code=400)
    result: dict = {"seeded": [], "failed": []}
    if symbol:
        sym = symbol.strip().upper()
        if not prices.is_watched(sym):
            return JSONResponse({"detail": f"{sym} is not on the watchlist"},
                                status_code=400)
        if not registry.listing(sym):
            return JSONResponse({"detail": f"{sym} has no yahoo listing (unsupported)"},
                                status_code=400)
        targets = [sym]
    else:
        targets = []
        for sym in get_watchlist():
            if registry.listing(sym):
                targets.append(sym)
            else:
                result["failed"].append({"symbol": sym, "reason": "unsupported"})

    for i, sym in enumerate(targets):
        if i:
            await asyncio.sleep(2.0)
        try:
            n = await prices.seed_listing_history(sym, interval, outputsize)
            result["seeded"].append({"symbol": sym, "points": n, "source": "yahoo"})
        except ValueError as e:
            result["failed"].append({"symbol": sym, "reason": str(e)})
    return JSONResponse(result)


@app.get("/health")
async def health():
    return {"status": "ok", "watchlist": get_watchlist()}
