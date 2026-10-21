"""Practice-book evaluation (Phase 1): the ``?book=practice`` payloads of
``/api/portfolio``, ``/history``, ``/performance`` plus ``/api/paper/status``
and season creation (plan §4, §8.1).

Enabled only when ``PAPER_TRADING_ENABLED=1`` AND the demo broker is
configured (``broker.get_broker("demo")`` is not None - that already covers
"T212_DEMO_KEY/SECRET set").

Baselines (``history``/``performance``): ``buy_hold``, ``trend_10m`` and
``momentum_12_1`` on ``prices.daily_closes`` of the universe's EUR listings,
weights frozen at the season's inception and renormalised to 100. ``buy_hold``
never rebalances. The two signal baselines re-evaluate at each month end
(``scoreboard.baseline_signals``, the SAME trend/momentum definitions used by
the advice scoreboard): an id whose signal is SELL sits out in cash
(contributes its frozen weight at a flat value) until the next re-evaluation;
BUY or "not enough history yet" (``None``) stays invested. No dividends, no
rebalancing band, no cost model - this is a Phase 1 simplification deliberately
simpler than the plan's full §8.1 evaluation (Phase 4).
"""
import asyncio
import datetime
import logging
import os
import time

from app.api import broker as broker_mod
from app.api import calendars
from app.api import paper_buckets, paper_ledger, paper_universe, performance, prices, registry, scoreboard
from app.api import portfolio as portfolio_mod

log = logging.getLogger("paper_eval")

DAILY = paper_buckets.DAILY
BOOK = "practice"
BASELINE_IDS = ("buy_hold", "trend_10m", "momentum_12_1")
OPEN_PRIMARY = "EUNL"          # open universe: the primary buy & hold benchmark
OPEN_SECONDARY = (("sxr8", "Buy & hold SXR8"), ("real", "Buy & hold real weights (s1)"))
BASELINE_LABELS = {
    "buy_hold": "Buy & hold (your weights)",
    "trend_10m": "Trend 10m",
    "momentum_12_1": "Momentum 12-1",
}
BASELINE_HISTORY_PERIOD = "2y"       # matches scoreboard's own lookback
CAPABILITIES = {"lots": False, "policy": False, "tax": False,
                "editPositions": False, "alertActions": False,
                "attribution": False, "risk": False, "analysis": True}


class PaperDisabled(RuntimeError):
    """Paper trading is not enabled (no keys, or PAPER_TRADING_ENABLED unset).
    The endpoint answers 503 {"error": "paper_disabled"}."""


class PaperUnavailable(RuntimeError):
    """Broker unreachable and no snapshot exists yet. 503 {"error":
    "broker_unreachable"}."""


class PaperBadRequest(ValueError):
    """Readable validation failure; the endpoint answers 400 with str()."""


class PaperConflict(RuntimeError):
    """A season already exists (and the reset preconditions are not met).
    The endpoint answers 409 with str()."""


# ------------------------------------------------------------------ config

def _broker():
    return broker_mod.get_broker("demo")


def enabled() -> bool:
    return os.getenv("PAPER_TRADING_ENABLED", "0") == "1" and _broker() is not None


def _num(value) -> float | None:
    return performance.num(value)


def _r(value, nd: int = 2):
    return None if value is None else round(float(value), nd)


def _paper_cfg(bucket: str = DAILY) -> dict:
    """The bucket's merged config (``paper_buckets.cfg``); the seam tests patch."""
    return paper_buckets.cfg(bucket)


def _banner(mode: str, bucket: str = DAILY) -> dict:
    label = "agent" if bucket == DAILY else f"{bucket} agent"
    return {"text": f"PRACTICE \u2013 simulated money \u00b7 Trading 212 demo "
                    f"\u00b7 {label}: {mode}",
            "mode": mode, "account": "t212-demo", "bucket": bucket}


def enabled_buckets() -> list[str]:
    """The buckets that may trade: daily always, intraday when configured and
    enabled (read through ``_paper_cfg``, the config seam)."""
    return [b for b in paper_buckets.ids() if _paper_cfg(b).get("enabled", True)]


def budget_total() -> float:
    """Sum of the enabled buckets' budgets: what the demo account must cover."""
    return sum(_paper_cfg(b)["budgetEur"] for b in enabled_buckets())


# ------------------------------------------------------------------ broker I/O

async def _broker_read():
    """-> (account, positions) read live from the demo broker, or
    (None, None) when the broker is absent, errors, or raises."""
    brk = _broker()
    if brk is None:
        return None, None
    try:
        acct, pos = await asyncio.gather(brk.account(), brk.positions())
    except Exception as e:
        log.warning("paper_eval: broker read failed: %s", e)
        return None, None
    if broker_mod.is_error(acct) or broker_mod.is_error(pos):
        return None, None
    return acct, pos


def _open(cfg: dict | None = None) -> bool:
    """True when the practice buckets trade the open universe
    (``paperTrading.universeSource == "paper_universe"``)."""
    try:
        src = (_paper_cfg() if cfg is None else cfg).get("universeSource")
    except Exception:
        return False
    return src == paper_buckets.PAPER_UNIVERSE


def mic_of(pid: str) -> str:
    """Listing venue (MIC) of a paper id: the paper_universe row's venue when
    the open universe is active and knows the id, else the watchlist's."""
    if _open():
        row = paper_universe.get(pid)
        if row:
            return row["venue"]
    return calendars.mic_for(pid)


def kind_of(pid: str, spec=None) -> str:
    """``equity`` | ``etf``: the universe spec's kind, else the watchlist's."""
    if isinstance(spec, dict) and spec.get("kind") in paper_universe.KINDS:
        return spec["kind"]
    return (registry.get(pid) or {}).get("kind") or (paper_universe.get(pid) or {}).get("kind") or "equity"


def entry_of(pid: str) -> dict:
    """``{label, kind}`` for display: the watchlist entry, else the paper_universe row."""
    e = registry.get(pid)
    if e:
        return {"label": e.get("label") or pid, "kind": e.get("kind")}
    row = paper_universe.get(pid) or {}
    return {"label": row.get("name") or pid, "kind": row.get("kind")}


def sector_of(pid) -> str | None:
    """The paper_universe sector of a registry entry, None for any other id."""
    row = paper_universe.get(pid) if isinstance(pid, str) else None
    return (row or {}).get("sector") or None


async def _safe_quote(pid: str, open_: bool = False):
    try:
        if open_ and paper_universe.get(pid):
            return await paper_universe.listing_quote(pid)
        return await prices.listing_quote(pid)
    except Exception as e:
        log.warning("paper_eval: quote %s failed: %s", pid, e)
        return None


async def quote(pid: str):
    """One id's listing quote through the active price source."""
    return await _safe_quote(pid, _open())


async def daily_closes(pid: str, period: str = "2y") -> list:
    """[(date, close)] of the id through the active price source (the
    paper_universe symbol when the open universe is active and knows the id)."""
    if _open() and paper_universe.get(pid):
        return await paper_universe.daily_closes(pid, period)
    return await prices.daily_closes(pid, period)


async def _safe_bars(pid: str):
    try:
        return await prices.listing_bars(pid, portfolio_mod.MDD_RANGE)
    except Exception as e:
        log.warning("paper_eval: bars %s failed: %s", pid, e)
        return []


def active_ids() -> set:
    """Ids any bucket holds or has a live order on (what must be priced)."""
    rows = paper_ledger.read_orders()
    ids = {r["id"] for r in paper_ledger.non_terminal(rows) if r.get("id")}
    for b in paper_buckets.ids():
        ids |= set(paper_ledger.ledger_holdings(paper_ledger.bucket_rows(rows, b)))
    return ids


async def _quote_universe(universe: dict, ids=None) -> dict:
    """{id: quote or None}. Open universe: only ``ids`` (default: the ids some
    bucket holds or has orders on) - never the whole ~130-name registry. The
    watchlist universe is always priced in full (``ids`` is ignored)."""
    open_ = _open()
    if not open_:
        wanted = list(universe)
    elif ids is None:
        held = active_ids()
        wanted = [pid for pid in universe if pid in held]
    else:
        wanted = [pid for pid in dict.fromkeys(ids) if pid in universe]
    if not wanted:
        return {}
    quotes = await asyncio.gather(*[_safe_quote(pid, open_) for pid in wanted])
    return dict(zip(wanted, quotes))


async def quotes_for(universe: dict, ids) -> dict:
    """Quotes of ``ids`` (open universe) or of the whole watchlist universe;
    the one call sites use when they know which ids matter."""
    if _open():
        return await _quote_universe(universe, ids)
    return await _quote_universe(universe)


async def _bars_universe(universe: dict) -> dict:
    """MDD bars of the watchlist universe (the open universe has no bar store:
    its rows show no drawdown)."""
    if _open():
        return {}
    ids = list(universe)
    if not ids:
        return {}
    bars = await asyncio.gather(*[_safe_bars(pid) for pid in ids])
    return dict(zip(ids, bars))


def decimals_by_id(universe: dict) -> dict[str, int]:
    """{id: quantity decimals} from the broker's instrument metadata (the
    broker falls back to 2 when it has none)."""
    brk = _broker()
    out: dict[str, int] = {}
    if brk is None:
        return out
    for pid, spec in universe.items():
        ticker = spec.get("t212") if isinstance(spec, dict) else None
        if isinstance(ticker, str) and ticker.strip():
            out[pid] = int(brk.quantity_decimals(ticker.strip()))
    return out


# --------------------------------------------------------------- snapshotting

async def take_snapshot(date: str | None = None, bucket: str = DAILY) -> dict | None:
    """Build + persist one bucket snapshot row (plan §3 shape); None when the
    broker is unavailable. Called by the worker (per bucket at the close, plus
    at startup when today's row is missing)."""
    bucket = paper_buckets.validate(bucket)
    cfg = _paper_cfg(bucket)
    acct, pos = await _broker_read()
    if acct is None:
        return None
    quotes = await _quote_universe(cfg["universe"])
    bucket_state = paper_ledger.bucket_from_positions(
        positions=pos, account=acct, universe=cfg["universe"],
        budget_eur=cfg["budgetEur"], prices_by_id=quotes,
        orders=paper_ledger.read_orders(),
        decimals_by_id=decimals_by_id(cfg["universe"]), bucket=bucket)
    day = date or datetime.date.today().isoformat()
    row = {
        "date": day, "bucket": bucket,
        "equityEur": _r(bucket_state["equityEur"]), "cashEur": _r(bucket_state["cashEur"]),
        "positions": [{"id": r["id"], "ticker": r["ticker"],
                       "quantity": _r(r["quantity"], 6), "priceEur": _r(r["priceEur"], 4),
                       "valueEur": _r(r["valueEur"])} for r in bucket_state["positions"]],
        "accountTotalEur": _r(bucket_state["accountTotalEur"]),
        "accountCashEur": _r(bucket_state["accountCashEur"]),
        "takenAt": time.time(),
        "warnings": [w["code"] for w in bucket_state["warnings"]
                     if w.get("code") in ("ledger_mismatch", "untracked_position")],
    }
    paper_ledger.save_snapshot_row(row, bucket)
    return row


# ---------------------------------------------------------------- /api/portfolio

def _format_rows(bucket_rows: list[dict], equity: float | None, bars: dict) -> list[dict]:
    out = []
    for r in bucket_rows:
        e = entry_of(r["id"])
        value, invested, price, prev = (r["valueEur"], r["investedEur"],
                                        r["priceEur"], r["prevCloseEur"])
        qty = r["quantity"]
        pnl = (value - invested) if (value is not None and invested is not None) else None
        pnl_pct = (pnl / invested * 100.0) if (pnl is not None and invested) else None
        day = (qty * (price - prev)) if (price is not None and prev is not None) else None
        day_pct = ((price / prev - 1.0) * 100.0) if (price is not None and prev) else None
        weight = (value / equity * 100.0) if (value is not None and equity) else None
        closes = [_num(x.get("c")) for x in (bars.get(r["id"]) or []) if isinstance(x, dict)]
        closes = [c for c in closes if c is not None]
        mdd = portfolio_mod._max_drawdown_pct(closes) if closes else None
        out.append({
            "id": r["id"], "label": e.get("label") or r["id"], "kind": e.get("kind"),
            "sector": sector_of(r["id"]),
            "shares": _r(qty, 6), "priceEur": _r(price, 4), "prevCloseEur": _r(prev, 4),
            "valueEur": _r(value), "weightPct": _r(weight), "investedEur": _r(invested),
            "pnlEur": _r(pnl), "pnlPct": _r(pnl_pct), "dayPnlEur": _r(day),
            "dayPct": _r(day_pct), "mddPct": _r(mdd),
            "priceAsOf": r["priceAsOf"], "priceSource": r["priceSource"],
            "stale": r["stale"], "ticker": r["ticker"],
        })
    return out


def _rows_from_snapshot(snap: dict, equity: float | None) -> list[dict]:
    """Fallback formatting from the last stored snapshot (no cost/prevClose
    data there, so pnl/day fields stay None)."""
    out = []
    for r in snap.get("positions") or []:
        pid = r.get("id")
        e = entry_of(pid)
        value = _num(r.get("valueEur"))
        weight = (value / equity * 100.0) if (value is not None and equity) else None
        out.append({
            "id": pid, "label": e.get("label") or pid, "kind": e.get("kind"),
            "sector": sector_of(pid),
            "shares": _r(r.get("quantity"), 6), "priceEur": _r(r.get("priceEur"), 4),
            "prevCloseEur": None, "valueEur": _r(value), "weightPct": _r(weight),
            "investedEur": None, "pnlEur": None, "pnlPct": None, "dayPnlEur": None,
            "dayPct": None, "mddPct": None, "priceAsOf": snap.get("takenAt"),
            "priceSource": "snapshot", "stale": True, "ticker": r.get("ticker"),
        })
    return out


async def portfolio_snapshot(bucket: str = DAILY) -> dict:
    """``GET /api/portfolio?book=practice`` payload for one bucket."""
    bucket = paper_buckets.validate(bucket)
    if not enabled():
        raise PaperDisabled()
    cfg = _paper_cfg(bucket)
    orders = paper_ledger.read_orders()
    acct, pos = await _broker_read()
    stale = False
    warnings: list[dict] = []
    if acct is not None:
        quotes, bars = await asyncio.gather(_quote_universe(cfg["universe"]),
                                            _bars_universe(cfg["universe"]))
        bucket_state = paper_ledger.bucket_from_positions(
            positions=pos, account=acct, universe=cfg["universe"],
            budget_eur=cfg["budgetEur"], prices_by_id=quotes,
            orders=orders, decimals_by_id=decimals_by_id(cfg["universe"]), bucket=bucket)
        rows = _format_rows(bucket_state["positions"], bucket_state["equityEur"], bars)
        warnings = bucket_state["warnings"]
        equity, cash, cost = (bucket_state["equityEur"], bucket_state["cashEur"],
                              bucket_state["bucketCostEur"])
        reserved = bucket_state["reservedEur"]
        account_total, account_cash = (bucket_state["accountTotalEur"],
                                       bucket_state["accountCashEur"])
    else:
        last = paper_ledger.latest_snapshot(bucket)
        if last is None:
            raise PaperUnavailable()
        stale = True
        equity = _num(last.get("equityEur"))
        cash = _num(last.get("cashEur"))
        cost = None
        account_total, account_cash = _num(last.get("accountTotalEur")), _num(last.get("accountCashEur"))
        reserved = paper_ledger.ledger_cash(paper_ledger.bucket_rows(orders, bucket),
                                            cfg["budgetEur"])["reservedEur"]
        rows = _rows_from_snapshot(last, equity)
        warnings = [{"code": "broker_unreachable",
                    "message": "Trading 212 demo is unreachable; showing the "
                               "last snapshot",
                    "ids": []}]

    season = paper_ledger.read_season(bucket)
    priced_pnl = [r["pnlEur"] for r in rows if r["pnlEur"] is not None]
    priced_day = [r["dayPnlEur"] for r in rows if r["dayPnlEur"] is not None]
    payload = {
        "asOf": int(time.time()), "currency": "EUR",
        "fx": {"rate": None, "source": None, "asOf": None, "stale": False},
        "totals": {
            "valueEur": _r(equity), "investedEur": _r(cost),
            "pnlEur": _r(sum(priced_pnl)) if priced_pnl else None,
            "pnlPct": (_r(sum(priced_pnl) / cost * 100.0)
                      if priced_pnl and cost else None),
            "dayPnlEur": _r(sum(priced_day)) if priced_day else None,
            "dayPnlPct": None,
            "costMissing": [],
        },
        "positions": rows, "benchmark": None, "warnings": warnings,
        "book": BOOK, "capabilities": dict(CAPABILITIES),
        "banner": _banner(cfg["mode"], bucket),
        "bucket": {
            "id": bucket,
            "budgetEur": _r(cfg["budgetEur"]), "cashEur": _r(cash),
            "equityEur": _r(equity), "accountTotalEur": _r(account_total),
            "accountCashEur": _r(account_cash),
            "strategyVersion": cfg["strategyVersion"],
            "reservedEur": _r(reserved),
            "inceptionDate": (season or {}).get("inceptionDate"),
            "seasonStarted": season is not None,
        },
    }
    if stale:
        payload["stale"] = True
    return payload


# -------------------------------------------------------------------- season

async def _open_benchmarks() -> dict:
    """The secondary benchmarks frozen with an open-universe season: SXR8
    buy & hold and the owner's REAL weights (the s1 starting point). A failing
    real snapshot only drops the second one."""
    out: dict = {"sxr8": {"SXR8": 100.0}}
    try:
        real = await portfolio_mod.snapshot()
        w = {r.get("id"): _num(r.get("weightPct")) for r in real.get("positions") or []
             if r.get("id") and _num(r.get("weightPct"))}
        total = sum(w.values())
        if total > 0:
            out["real"] = {pid: round(v / total * 100.0, 4) for pid, v in w.items()}
    except Exception as e:
        log.warning("paper_eval: real-weights benchmark unavailable: %s", e)
    return out


async def create_season(book: str = BOOK, reset: bool = False, bucket: str = DAILY,
                        strategy_version: str | None = None) -> dict:
    """Start the bucket's season. Watchlist universe: freeze the real portfolio's
    weights as the baseline. Open universe (``universeSource: paper_universe``):
    the baseline is buy & hold of the primary benchmark EUNL, with SXR8 and the
    real weights frozen as ``benchmarks``. A reset keeps the old season in
    ``previous`` (so s1 stays readable). 409 when the bucket is disabled, when
    its season exists (without ``reset`` after liquidation), or when the enabled
    buckets' budgets together exceed the demo account's total value."""
    bucket = paper_buckets.validate(bucket)
    if book != BOOK:
        raise PaperBadRequest(f"unsupported book {book!r}")
    if not enabled():
        raise PaperDisabled()
    cfg = _paper_cfg(bucket)
    universe = cfg["universe"]
    if not universe:
        raise PaperBadRequest(
            "paperTrading.universe is empty; configure it before starting a season")
    if not cfg.get("enabled", True):
        raise PaperConflict(
            f"bucket {bucket!r} is not enabled (paperTrading.buckets.{bucket}.enabled)")
    existing = paper_ledger.read_season(bucket)
    if existing is not None and not reset:
        raise PaperConflict(
            "a season already exists; pass reset: true after the bucket "
            "is liquidated")
    acct, _pos = await _broker_read()
    if acct is None:
        raise PaperUnavailable()
    if existing is not None and paper_ledger.bucket_holds_positions(
            paper_ledger.read_orders(), bucket):
        raise PaperConflict(
            "cannot reset: the bucket still holds positions")
    account_total = _num(paper_ledger._get(acct, "total"))
    if account_total is None:
        raise PaperUnavailable("demo account total value unknown")
    budgets = budget_total()
    if budgets > account_total + 1e-9:
        raise PaperConflict(
            f"the enabled buckets' budgets (EUR {budgets:,.2f}) exceed the demo "
            f"account's total value (EUR {account_total:,.2f})")

    sv = strategy_version or cfg["strategyVersion"]
    benchmarks = None
    if _open(cfg):
        weights = {OPEN_PRIMARY: 100.0}
        benchmarks = await _open_benchmarks()
    else:
        real = await portfolio_mod.snapshot()
        weights = {}
        for row in real.get("positions") or []:
            pid = row.get("id")
            w = _num(row.get("weightPct"))
            if pid in universe and w:
                weights[pid] = w
        total = sum(weights.values())
        if total <= 0:
            raise PaperBadRequest(
                "the real portfolio holds none of the paperTrading.universe ids; "
                "cannot derive baseline weights")
        weights = {pid: round(w / total * 100.0, 4) for pid, w in weights.items()}
    row = {
        "book": BOOK, "bucket": bucket, "strategyVersion": sv,
        "inceptionDate": datetime.date.today().isoformat(),
        "startedAt": time.time(), "baselineWeights": weights,
    }
    if benchmarks is not None:
        row["benchmarks"] = benchmarks
        row["universeSource"] = paper_buckets.PAPER_UNIVERSE
    if existing is not None:
        row["previous"] = [*(existing.get("previous") or []),
                           {k: v for k, v in existing.items() if k != "previous"}][-10:]
    if not paper_ledger.write_season(row, bucket):
        raise PaperUnavailable("could not persist the season")
    return row


# ------------------------------------------------------------------ status

def _season_block(season: dict | None) -> dict:
    if season is None:
        return {"started": False, "inceptionDate": None, "baselineWeights": {}, "previous": []}
    # a previous season ended when the next one started: the following row's startedAt, the current
    # season's for the last one
    rows = [r for r in (season.get("previous") or []) if isinstance(r, dict)]
    starts = [r.get("startedAt") for r in rows[1:]] + [season.get("startedAt")]
    previous = [{"strategyVersion": r.get("strategyVersion"), "inceptionDate": r.get("inceptionDate"),
                 "endedAt": ended} for r, ended in zip(rows, starts)]
    return {"started": True, "inceptionDate": season.get("inceptionDate"),
            "baselineWeights": season.get("baselineWeights") or {}, "previous": previous}


def _bucket_status(bucket: str) -> dict:
    """One entry of ``status()["buckets"]``. ``nextSlot`` is the intraday
    scheduler's next decision slot (epoch s); None until one is scheduled."""
    from app.api import paper_pm, paper_trader      # lazy: both import this module
    cfg = _paper_cfg(bucket)
    rows = paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
    return {
        "enabled": bool(cfg.get("enabled", True)), "mode": cfg["mode"],
        "halted": bool(paper_ledger.read_halt(bucket).get("halted")),
        "budgetEur": cfg["budgetEur"], "strategyVersion": cfg["strategyVersion"],
        "season": _season_block(paper_ledger.read_season(bucket)),
        "decision": paper_pm.decision_status(bucket=bucket),
        "openIntents": len(paper_ledger.non_terminal(rows)),
        "nextSlot": (paper_trader.next_slot(cfg) if bucket != DAILY
                     and not paper_ledger.read_halt(bucket).get("halted") else None),
    }


def status(bucket: str = DAILY) -> dict:
    """``GET /api/paper/status`` payload: the requested bucket's block (daily
    by default, as before) plus ``buckets`` {id: summary} for every bucket (the
    caller adds "worker" from ``paper_trader.status()`` - kept out of this
    module to avoid a import cycle, since the worker imports ``paper_eval``)."""
    bucket = paper_buckets.validate(bucket)
    cfg = _paper_cfg(bucket)
    brk = _broker()
    is_enabled = enabled()
    bstat = brk.status() if brk is not None else None
    season = paper_ledger.read_season(bucket)
    out = {
        "enabled": is_enabled, "mode": cfg["mode"], "liveAvailable": False,
        "bucketId": bucket, "universeSource": cfg.get("universeSource") or paper_buckets.WATCHLIST,
        "broker": {
            "name": (bstat or {}).get("name", "t212"),
            "env": (bstat or {}).get("env", "demo"),
            "configured": brk is not None,
            "reachable": bool(bstat and bstat.get("reachable")),
            "rateLimitedUntil": (bstat or {}).get("rateLimitedUntil"),
            "lastOkAt": (bstat or {}).get("lastOkAt", 0),
            # a past error is history once the broker answers again: show it only
            # while the broker is unreachable or rate limited
            "lastError": ((bstat or {}).get("lastError")
                          if bstat and (not bstat.get("reachable")
                                        or bstat.get("rateLimitedUntil")) else None),
        },
        "bucket": {"budgetEur": cfg["budgetEur"], "strategyVersion": cfg["strategyVersion"]},
        "season": _season_block(season),
        "banner": _banner(cfg["mode"], bucket),
        "universe": {pid: {"t212": spec.get("t212"),
                           "kind": kind_of(pid, spec)}
                     for pid, spec in cfg["universe"].items() if isinstance(spec, dict)},
    }
    from app.api import paper_pm      # lazy: paper_pm imports this module
    out["decision"] = paper_pm.decision_status(bucket=bucket)
    out["buckets"] = {b: _bucket_status(b) for b in paper_buckets.ids()}
    if not is_enabled:
        out["reason"] = ("PAPER_TRADING_ENABLED is not set"
                         if os.getenv("PAPER_TRADING_ENABLED", "0") != "1"
                         else "demo broker not configured (T212_DEMO_KEY/SECRET)")
    return out


# ---------------------------------------------------------------- baselines

def _month_end_dates(dates: list[str]) -> set:
    """Last date of each (year, month) group present in ``dates`` (any order)."""
    last_of_month: dict[str, str] = {}
    for d in dates:
        ym = d[:7]
        if d > last_of_month.get(ym, ""):
            last_of_month[ym] = d
    return set(last_of_month.values())


def _id_index_series(axis: list[str], axis_prices: dict, full_closes: list,
                     signal_name: str | None) -> dict:
    """Growth factor (1.0 = inception) for ONE id over ``axis``.

    ``signal_name`` None: plain buy & hold (always invested). Otherwise the
    id is re-evaluated (``scoreboard.baseline_signals`` on ``full_closes``,
    which carries the pre-inception history needed for the trend/momentum
    lookback) at inception and at every month end present in ``axis``: a
    SELL signal takes it to cash (flat) until the next re-evaluation; BUY or
    unknown (not enough history) keeps tracking the price.
    """
    if not axis:
        return {}
    rebalance_dates = _month_end_dates(axis) | {axis[0]}
    out: dict[str, float] = {}
    base_value = 1.0
    anchor_price = axis_prices.get(axis[0])
    in_signal = True
    for d in axis:
        p = axis_prices.get(d)
        # Value as of today under the regime decided at the LAST rebalance
        # (captures the full move up to and including a flip day, so the
        # id bears its own month's move before switching to cash).
        value = (base_value * (p / anchor_price)
                if (p is not None and in_signal and anchor_price) else base_value)
        if d in rebalance_dates:
            base_value = value
            if signal_name is not None:
                sig = scoreboard.baseline_signals(full_closes, d).get(signal_name)
                in_signal = sig != "SELL"
            if p is not None:
                anchor_price = p
            value = base_value       # today IS the new anchor: ratio 1.0
        out[d] = value
    return out


def baseline_index(closes_by_id: dict, weights: dict, axis: list,
                   signal_name: str | None) -> dict:
    """{date: weighted growth factor} for the frozen-weight basket (Sigma
    w_id/100 x idx_id(date)) over ``axis``."""
    if not axis or not weights:
        return {}
    total_w = sum(weights.values()) or 1.0
    axis_prices_by_id = {pid: dict(closes_by_id.get(pid) or []) for pid in weights}
    series_by_id = {
        pid: _id_index_series(axis, axis_prices_by_id[pid], closes_by_id.get(pid) or [],
                              signal_name)
        for pid in weights
    }
    return {d: sum((weights[pid] / total_w) * series_by_id[pid].get(d, 1.0)
                   for pid in weights)
            for d in axis}


def _range_start_date(rng: str, inception: str) -> str:
    if rng == "ALL":
        return inception
    days = performance.RANGE_DAYS.get(rng)
    if not days:
        return inception
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return max(start, inception)


def _indexed_from_values(pv: dict, axis: list) -> list:
    if not axis:
        return []
    base = pv.get(axis[0])
    if not base:
        return []
    return [{"t": performance.day_ts(d), "pct": _r((pv[d] / base - 1.0) * 100.0)}
            for d in axis if d in pv]


async def _baseline_axis(weights: dict, start: str) -> tuple[dict, list]:
    """({id: daily closes}, sorted common dates >= ``start``) for the frozen
    baseline ids; the axis is empty when any id has no history."""
    ids = list(weights)
    closes_by_id = {}
    for pid in ids:
        try:
            closes_by_id[pid] = await daily_closes(pid, BASELINE_HISTORY_PERIOD)
        except Exception as e:
            log.warning("paper_eval: baseline closes %s failed: %s", pid, e)
            closes_by_id[pid] = []
    common = (set.intersection(*[{d for d, _ in closes_by_id[pid]} for pid in ids
                                 if closes_by_id[pid]])
             if ids and all(closes_by_id[pid] for pid in ids) else set())
    return closes_by_id, sorted(d for d in common if d >= start)


async def history(range_key: str = "3M", bucket: str = DAILY) -> dict:
    """``GET /api/portfolio/history?book=practice`` payload for one bucket."""
    bucket = paper_buckets.validate(bucket)
    if not enabled():
        raise PaperDisabled()
    rng = range_key.upper()
    if rng not in portfolio_mod.HISTORY_RANGES:
        raise ValueError(f"range must be one of {', '.join(portfolio_mod.HISTORY_RANGES)}")
    out: dict = {
        "range": rng, "basis": "broker_snapshots", "book": BOOK, "bucket": bucket,
        "points": [], "benchmark": None, "perfBenchmark": None,
        "baselines": [],
        "indexed": {"portfolio": [], "buy_hold": [], "trend_10m": [], "momentum_12_1": []},
        "warnings": [],
    }
    season = paper_ledger.read_season(bucket)
    if season is None:
        out["warnings"].append({"code": "season_not_started",
                                "message": "No season started yet", "ids": []})
        return out

    inception = season.get("inceptionDate")
    weights = season.get("baselineWeights") or {}
    start = _range_start_date(rng, inception)

    snaps = sorted((r for r in paper_ledger.read_snapshots(bucket)
                    if r.get("date") and r["date"] >= start),
                   key=lambda r: r["date"])
    pv = {r["date"]: _num(r.get("equityEur")) for r in snaps
          if _num(r.get("equityEur")) is not None}
    axis_portfolio = sorted(pv)
    out["points"] = [{"t": performance.day_ts(d), "valueEur": _r(pv[d]), "investedEur": None}
                     for d in axis_portfolio]
    out["indexed"]["portfolio"] = _indexed_from_values(pv, axis_portfolio)

    ids = list(weights)
    closes_by_id, axis = await _baseline_axis(weights, start)

    out["baselines"] = [{"id": b, "label": BASELINE_LABELS[b]} for b in BASELINE_IDS]
    for bid, signal in (("buy_hold", None), ("trend_10m", "trend_10m"),
                        ("momentum_12_1", "momentum_12_1")):
        idx = baseline_index(closes_by_id, weights, axis, signal)
        out["indexed"][bid] = [{"t": performance.day_ts(d), "pct": _r((idx[d] - 1.0) * 100.0)}
                               for d in axis]
    if _open() and isinstance(season.get("benchmarks"), dict):
        # primary buy_hold = EUNL; SXR8 and the s1 real weights run alongside
        out["baselines"][0] = {"id": "buy_hold", "label": f"Buy & hold {OPEN_PRIMARY} (primary)"}
        for key, label in OPEN_SECONDARY:
            w = season["benchmarks"].get(key)
            if not isinstance(w, dict) or not w:
                continue
            c_by_id, b_axis = await _baseline_axis(w, start)
            idx = baseline_index(c_by_id, w, b_axis, None)
            out["baselines"].append({"id": f"buy_hold_{key}", "label": label})
            out["indexed"][f"buy_hold_{key}"] = [
                {"t": performance.day_ts(d), "pct": _r((idx[d] - 1.0) * 100.0)} for d in b_axis]
    out["warnings"].append({"code": "baseline_price_only",
                            "message": "Baselines use listing prices only; dividends "
                                       "are not included yet",
                            "ids": ids})
    if not axis:
        out["warnings"].append({"code": "baseline_no_history",
                                "message": "No common EUR listing price history "
                                           "for the baseline universe",
                                "ids": ids})
    return out


def _iso_from_ts(ts) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()


async def buy_hold_factors(season: dict) -> dict:
    """{date: growth factor (1.0 = the first common session at/after
    inception)} of the frozen-weight buy & hold basket, unrounded (the
    metrics need the daily noise that the 2-decimal chart series drops)."""
    weights = season.get("baselineWeights") or {}
    closes_by_id, axis = await _baseline_axis(weights, season.get("inceptionDate") or "")
    return baseline_index(closes_by_id, weights, axis, None)


def _metrics_mod():
    """The metrics module (a seam: tests inject a fake)."""
    from app.api import paper_metrics
    return paper_metrics


def _decisions(bucket: str = DAILY) -> list[dict]:
    from app.api import paper_pm            # paper_pm imports this module
    return paper_pm.read_decisions(bucket)


def _slice(series: dict, start: str, end: str | None) -> dict:
    return {d: v for d, v in series.items() if d >= start and (end is None or d <= end)}


async def performance_metrics(bucket: str = DAILY) -> dict:
    """``{"metrics", "versions"}`` for the Practice performance card: the
    bucket against buy & hold since the CURRENT strategy version started, and
    the same per strategy version (all from this bucket's own snapshots,
    orders and decisions). Never raises: a failure is reported as
    ``metrics: None`` plus a warning, so the rest of the payload survives."""
    bucket = paper_buckets.validate(bucket)
    out: dict = {"metrics": None, "versions": [], "warnings": []}
    season = paper_ledger.read_season(bucket)
    if season is None:
        return out
    try:
        mm = _metrics_mod()
        cfg = _paper_cfg(bucket)
        inception = season.get("inceptionDate")
        equity = {r["date"]: _num(r.get("equityEur")) for r in paper_ledger.read_snapshots(bucket)
                  if r.get("date") and r["date"] >= inception
                  and _num(r.get("equityEur")) is not None}
        factors = await buy_hold_factors(season)
        orders = paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
        fills = [r for r in orders if (_num(r.get("filledQuantity")) or 0) > 0]
        segments = list(mm.version_segments(_decisions(bucket), orders, inception))
        current = cfg["strategyVersion"]
        if not any(s["strategyVersion"] == current for s in segments):
            segments.append({"strategyVersion": current, "from": inception, "to": None})
        for seg in segments:
            lo, hi, ver = seg["from"], seg.get("to"), seg["strategyVersion"]
            m = mm.bucket_metrics(
                _slice(equity, lo, hi), _slice(factors, lo, hi),
                [f for f in fills if f.get("strategyVersion") == ver], cfg["costModel"])
            out["versions"].append({"strategyVersion": ver, "from": lo, "to": hi,
                                    "metrics": m})
            if ver == current:
                out["metrics"] = m
    except Exception as e:
        log.warning("paper_eval: metrics failed: %s", e)
        out["warnings"].append({"code": "metrics_unavailable",
                                "message": "Evaluation metrics could not be computed",
                                "ids": []})
    return out


GRADING_HORIZONS = {DAILY: (5, 20), paper_buckets.INTRADAY: (1, 5)}


async def grading(bucket: str = DAILY) -> dict:
    """``GET /api/paper/grading?book=practice``: the bucket's decisions graded
    against the frozen baseline weights at 5 / 20 sessions (daily) or 1 / 5
    sessions (intraday)."""
    bucket = paper_buckets.validate(bucket)
    if not enabled():
        raise PaperDisabled()
    mm = _metrics_mod()
    season = paper_ledger.read_season(bucket) or {}
    weights = season.get("baselineWeights") or {}
    decisions = _decisions(bucket)
    if _open():
        used = {pid for d in decisions for pid in ((d.get("output") or {}).get("targets") or {})}
        ids = sorted(used | set(weights))
    else:
        ids = sorted(set(_paper_cfg(bucket)["universe"]) | set(weights))
    closes = {}
    for pid in ids:
        try:
            closes[pid] = await daily_closes(pid, BASELINE_HISTORY_PERIOD)
        except Exception as e:
            log.warning("paper_eval: grading closes %s failed: %s", pid, e)
            closes[pid] = []
    return mm.grade_decisions(decisions, closes, horizons=GRADING_HORIZONS[bucket],
                              baseline=weights or None)


def _fill_day(row: dict) -> str:
    return str(row.get("intentId") or "")[:10]


async def compare() -> dict:
    """``GET /api/paper/compare?book=practice``: every bucket and buy & hold on
    equal terms. The window starts at the LATEST inception among the started
    seasons; each bucket is measured against the buy-and-hold basket of its own
    season weights over that window. ``turnoverPct`` / ``costDragPct`` are
    {bucket: value}; ``buyHold`` is the first started bucket's basket (daily
    first) measured over the same window. A bucket without a season, or whose
    metrics fail, is None (with a warning on failure)."""
    if not enabled():
        raise PaperDisabled()
    mm = _metrics_mod()
    out: dict = {"asOf": int(time.time()), "from": None, "buckets": {}, "buyHold": None,
                 "decisions": {}, "turnoverPct": {}, "costDragPct": {}, "warnings": []}
    seasons = {b: paper_ledger.read_season(b) for b in paper_buckets.ids()}
    started = [s for s in seasons.values() if s and s.get("inceptionDate")]
    start = max(s["inceptionDate"] for s in started) if started else None
    out["from"] = start
    orders = paper_ledger.read_orders()
    for b in paper_buckets.ids():
        out["decisions"][b] = mm.decision_counts(_decisions(b))
        out["buckets"][b] = None
        season = seasons[b]
        if not season or not season.get("inceptionDate"):
            continue
        try:
            cost = _paper_cfg(b)["costModel"]
            equity = {r["date"]: _num(r.get("equityEur")) for r in paper_ledger.read_snapshots(b)
                      if r.get("date") and r["date"] >= start
                      and _num(r.get("equityEur")) is not None}
            factors = _slice(await buy_hold_factors(season), start, None)
            fills = [r for r in paper_ledger.bucket_rows(orders, b)
                     if (_num(r.get("filledQuantity")) or 0) > 0 and _fill_day(r) >= start]
            out["buckets"][b] = mm.bucket_metrics(equity, factors, fills, cost)
            if out["buyHold"] is None:
                bh = mm.bucket_metrics(factors, factors, [], cost)
                out["buyHold"] = {k: bh.get(k) for k in (
                    "sessions", "returnPct", "maxDrawdownPct", "volatilityPct",
                    "sharpe", "sortino")}
        except Exception as e:
            log.warning("paper_eval: compare %s failed: %s", b, e)
            out["warnings"].append({"code": "metrics_unavailable", "bucket": b,
                                    "message": f"Metrics for the {b} bucket could not be computed",
                                    "ids": []})
    for b, m in out["buckets"].items():
        out["turnoverPct"][b] = (m or {}).get("turnoverPct")
        out["costDragPct"][b] = (m or {}).get("costDragPct")
    return out


async def performance_summary(range_key: str = "3M", bucket: str = DAILY) -> dict:
    """``GET /api/portfolio/performance?book=practice`` payload."""
    h = await history(range_key, bucket)
    out: dict = {
        "range": h["range"], "basis": h["basis"], "book": BOOK,
        "twrPct": None, "xirrPct": None, "perfBenchmarkId": "buy_hold",
        "perfBenchmarkPct": None, "excessPct": None,
        "startValueEur": None, "endValueEur": None, "netFlowsEur": None,
        "asOf": None, "warnings": h["warnings"],
    }
    pts = h.get("points") or []
    if pts:
        out["startValueEur"] = pts[0]["valueEur"]
        out["endValueEur"] = pts[-1]["valueEur"]
        out["asOf"] = _iso_from_ts(pts[-1]["t"])
    if h["indexed"]["portfolio"]:
        out["twrPct"] = h["indexed"]["portfolio"][-1]["pct"]
    if h["indexed"]["buy_hold"]:
        out["perfBenchmarkPct"] = h["indexed"]["buy_hold"][-1]["pct"]
    if out["twrPct"] is not None and out["perfBenchmarkPct"] is not None:
        out["excessPct"] = _r(out["twrPct"] - out["perfBenchmarkPct"])
    extra = await performance_metrics(bucket)
    out["metrics"] = extra["metrics"]
    out["versions"] = extra["versions"]
    out["warnings"] = list(out["warnings"]) + extra["warnings"]
    return out
