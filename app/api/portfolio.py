"""EUR-native portfolio valuation (replaces analytics.py).

Position model (config ``portfolio[id] = {"shares": float,
"investedAmount": float | None, "lots": [...]}``): ``investedAmount`` is the
EUR cost basis, ``None`` = unknown. ``lots`` (optional, validated and
reflected into ``shares`` / ``investedAmount`` by ``config_edit``) are dated
buys/sells that drive the history basis (``performance``). Each holding is
valued at ``shares x`` the quote of its single EUR listing
(``prices.listing_quote``) - the same series the chart uses. The US live
feeds are a separate reference and are never mixed in.

Rules:

* A position without a cost basis is valued (it is in ``totals.valueEur``,
  the weights and the day P/L) but has no P/L and is NOT in ``investedEur`` /
  ``pnlEur``; it is listed in ``totals.costMissing`` plus a ``cost_missing``
  warning. Totals therefore describe only the positions that can be judged.
* A position with no usable price is excluded from every total and reported
  as ``no_price`` - nothing is ever valued at a guessed price.
* Day P/L uses ``prevClose`` of the same listing.
* Every row carries ``priceAsOf`` / ``priceSource`` / ``stale``; stale quotes
  and FX problems are surfaced as ``warnings`` entries ``{code, message,
  ids}``.
* Numbers are rounded only when the payload is built (money 2 dp, percent 2
  dp, price 4 dp); all sums are computed from unrounded values.
"""
import asyncio
import copy
import datetime
import logging
import time

from app.api import config_store, forex, performance, prices, registry

log = logging.getLogger(__name__)

HISTORY_RANGES = performance.RANGES
BASIS_LOTS = "lots"
BASIS_CURRENT = "current_shares"
MDD_RANGE = "1Y"
CACHE_TTL_S = 30.0

_cache: dict = {"key": None, "ts": 0.0, "snap": None}


# ---------------------------------------------------------------- helpers

def _r(value, nd: int = 2):
    return None if value is None else round(float(value), nd)


_num = performance.num
_day_ts = performance.day_ts


async def _safe(coro, what: str):
    """Await a provider call; a failure becomes None + a log line, so one
    broken listing cannot take the whole portfolio down."""
    try:
        return await coro
    except Exception as e:
        log.warning("portfolio: %s failed: %s", what, e)
        return None


def _max_drawdown_pct(closes: list[float]) -> float | None:
    """Peak-to-trough drawdown in percent (<= 0) over the series."""
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < 2:
        return None
    peak, worst = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, c / peak - 1.0)
    return worst * 100.0


def _pct(fraction) -> float | None:
    """Fraction -> percent rounded for the payload (None stays None)."""
    return None if fraction is None else _r(fraction * 100.0)


def _holdings() -> tuple[list[dict], dict, list[str]]:
    """(holding entries that have a position, portfolio dict, orphan ids)."""
    portfolio = config_store.read().get("portfolio") or {}
    if not isinstance(portfolio, dict):
        portfolio = {}
    entries = [e for e in registry.entries() if e.get("role") == "holding"
               and e["id"] in portfolio]
    known = {e["id"] for e in entries}
    orphans = [pid for pid in portfolio if pid not in known]
    return entries, portfolio, orphans


def _shares(pos) -> float | None:
    v = _num((pos or {}).get("shares"))
    return v if v is not None and v > 0 else None


def _invested(pos) -> float | None:
    v = _num((pos or {}).get("investedAmount"))
    return v if v is not None and v >= 0 else None


def _eur_factor(currency: str, fx: dict) -> float | None:
    """Multiplier turning one unit of ``currency`` into EUR (None: no rate)."""
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return 1.0
    if cur == "USD":
        rate = _num((fx or {}).get("rate"))
        return rate if rate and rate > 0 else None
    return None


def _warn(warnings: list[dict], code: str, message: str, ids=None) -> None:
    if ids is None or ids:
        warnings.append({"code": code, "message": message,
                         "ids": list(ids or [])})


# --------------------------------------------------------------- snapshot

async def _build_snapshot() -> dict:
    entries, portfolio, orphans = _holdings()
    bench_id = registry.benchmark_id()
    bench_entry = next((e for e in registry.entries()
                        if e["id"] == bench_id), None) if bench_id else None

    fx_task = _safe(forex.get_rate_usd_eur(), "fx rate")
    quote_tasks = [_safe(prices.listing_quote(e["id"]), f"quote {e['id']}")
                   for e in entries]
    bar_tasks = [_safe(prices.listing_bars(e["id"], MDD_RANGE),
                       f"bars {e['id']}") for e in entries]
    bench_task = (_safe(prices.listing_quote(bench_id), "benchmark quote")
                  if bench_id else asyncio.sleep(0, result=None))
    fx, bench_q, quotes, bars = await asyncio.gather(
        fx_task, bench_task, asyncio.gather(*quote_tasks),
        asyncio.gather(*bar_tasks))
    fx = fx if isinstance(fx, dict) else {"rate": None, "source": None,
                                          "asOf": None, "stale": True}

    warnings: list[dict] = []
    rows, raw = [], []
    stale_ids, no_price, fx_missing, no_prev = [], [], [], []
    for e, q, b in zip(entries, quotes, bars):
        pid = e["id"]
        pos = portfolio[pid]
        shares = _shares(pos)
        invested = _invested(pos)
        row = {"id": pid, "label": e.get("label") or pid,
               "kind": e.get("kind"), "shares": shares, "priceEur": None,
               "prevCloseEur": None, "valueEur": None, "weightPct": None,
               "investedEur": None, "pnlEur": None, "pnlPct": None,
               "dayPnlEur": None, "dayPct": None, "mddPct": None,
               "priceAsOf": None, "priceSource": None, "stale": None}
        q = q if isinstance(q, dict) else {}
        row["prevCloseSource"] = q.get("prevCloseSource")
        price = _num(q.get("price"))
        if shares is None or price is None or price <= 0:
            no_price.append(pid)
            row.update(priceAsOf=q.get("asOf"), priceSource=q.get("source"),
                       stale=bool(q.get("stale")) if q else None)
            rows.append(row)
            raw.append(None)
            continue
        factor = _eur_factor(q.get("currency") or
                             (e.get("listing") or {}).get("currency"), fx)
        if factor is None:
            fx_missing.append(pid)
            rows.append(row)
            raw.append(None)
            continue
        price_eur = price * factor
        prev = _num(q.get("prevClose"))
        prev_eur = prev * factor if prev and prev > 0 else None
        value = shares * price_eur
        d = {"id": pid, "value": value, "invested": invested,
             "day": shares * (price_eur - prev_eur) if prev_eur else None,
             "prev_value": shares * prev_eur if prev_eur else None}
        if invested is not None:
            d["pnl"] = value - invested
        raw.append(d)
        if prev_eur is None:
            no_prev.append(pid)
        if q.get("stale"):
            stale_ids.append(pid)
        closes = [_num(x.get("c")) for x in (b or []) if isinstance(x, dict)]
        row.update(
            priceEur=price_eur, prevCloseEur=prev_eur, valueEur=value,
            investedEur=invested,
            pnlEur=d.get("pnl"),
            pnlPct=(d["pnl"] / invested * 100.0
                    if invested and "pnl" in d else None),
            dayPnlEur=d["day"],
            dayPct=(price_eur / prev_eur - 1.0) * 100.0 if prev_eur else None,
            mddPct=_max_drawdown_pct([c for c in closes if c is not None]),
            priceAsOf=q.get("asOf"), priceSource=q.get("source"),
            stale=bool(q.get("stale")))
        rows.append(row)

    priced = [d for d in raw if d]
    total_value = sum(d["value"] for d in priced)
    cost_known = [d for d in priced if d["invested"] is not None]
    total_invested = sum(d["invested"] for d in cost_known)
    total_pnl = sum(d["pnl"] for d in cost_known)
    day_known = [d for d in priced if d["day"] is not None]
    day_pnl = sum(d["day"] for d in day_known)
    day_base = sum(d["prev_value"] for d in day_known)
    cost_missing = [d["id"] for d in priced if d["invested"] is None]

    for row, d in zip(rows, raw):
        if d and total_value > 0:
            row["weightPct"] = d["value"] / total_value * 100.0

    _warn(warnings, "stale_price",
          "Price older than expected (market closed or feed lagging): "
          + ", ".join(stale_ids), stale_ids)
    _warn(warnings, "cost_missing",
          "No cost basis: shown in value, excluded from invested and P/L: "
          + ", ".join(cost_missing), cost_missing)
    _warn(warnings, "no_price",
          "No usable price: excluded from every total: " + ", ".join(no_price),
          no_price)
    _warn(warnings, "prev_close_missing",
          "No previous close: excluded from day P/L: " + ", ".join(no_prev),
          no_prev)
    _warn(warnings, "fx_missing",
          "No USD/EUR rate: USD listing cannot be valued: "
          + ", ".join(fx_missing), fx_missing)
    _warn(warnings, "unknown_position",
          "Position is not a holding on the watchlist: " + ", ".join(orphans),
          orphans)
    usd_used = any(((q or {}).get("currency") or "").upper() == "USD"
                   for q in quotes if isinstance(q, dict))
    if usd_used and fx.get("stale"):
        _warn(warnings, "fx_stale",
              "USD/EUR rate is stale (source: %s)" % fx.get("source"), None)

    benchmark = None
    if bench_id:
        bq = bench_q if isinstance(bench_q, dict) else None
        benchmark = {
            "id": bench_id,
            "label": (bench_entry or {}).get("label") or bench_id,
            "price": _r(bq and bq.get("price"), 4),
            "currency": (bq or {}).get("currency"),
            "dayPct": _r(bq and bq.get("changePct")),
            "priceAsOf": (bq or {}).get("asOf"),
            "priceSource": (bq or {}).get("source"),
            "stale": bool(bq.get("stale")) if bq else None,
        }
        if bq is None:
            _warn(warnings, "benchmark_unavailable",
                  f"No quote for benchmark {bench_id}", [bench_id])
        elif bq.get("stale"):
            _warn(warnings, "stale_price",
                  f"Benchmark {bench_id} price is stale", [bench_id])

    for row in rows:
        for k, nd in (("shares", 6), ("priceEur", 4), ("prevCloseEur", 4),
                      ("valueEur", 2), ("weightPct", 2), ("investedEur", 2),
                      ("pnlEur", 2), ("pnlPct", 2), ("dayPnlEur", 2),
                      ("dayPct", 2), ("mddPct", 2)):
            row[k] = _r(row[k], nd)

    return {
        "asOf": int(time.time()),
        "currency": "EUR",
        "fx": {"rate": fx.get("rate"), "source": fx.get("source"),
               "asOf": fx.get("asOf"), "stale": bool(fx.get("stale"))},
        "totals": {
            "valueEur": _r(total_value),
            "investedEur": _r(total_invested) if cost_known else None,
            "pnlEur": _r(total_pnl) if cost_known else None,
            "pnlPct": _r(total_pnl / total_invested * 100.0
                         if total_invested > 0 else None),
            "dayPnlEur": _r(day_pnl) if day_known else None,
            "dayPnlPct": _r(day_pnl / day_base * 100.0
                            if day_base > 0 else None),
            "costMissing": cost_missing,
        },
        "positions": rows,
        "benchmark": benchmark,
        "warnings": warnings,
    }


def _cache_key() -> str:
    cfg = config_store.read()
    return repr((sorted((cfg.get("portfolio") or {}).items()),
                 [e["id"] for e in registry.entries()],
                 registry.benchmark_id()))


async def snapshot() -> dict:
    """Portfolio snapshot (served as GET /api/portfolio); recomputed at most
    every ``CACHE_TTL_S`` seconds, immediately after a config change."""
    key = _cache_key()
    if (_cache["snap"] is not None and _cache["key"] == key
            and time.monotonic() - _cache["ts"] <= CACHE_TTL_S):
        return copy.deepcopy(_cache["snap"])
    snap = await _build_snapshot()
    _cache.update(key=key, ts=time.monotonic(), snap=snap)
    return copy.deepcopy(snap)


def position_for(id: str) -> dict | None:
    """Row of ``positions[]`` for ``id`` from the snapshot cache (<= 30 s
    old), else None. Sync on purpose: async callers refresh the cache with
    ``await snapshot()`` first."""
    snap = _cache["snap"]
    if snap is None or time.monotonic() - _cache["ts"] > CACHE_TTL_S:
        return None
    pid = str(id or "").strip().upper()
    for row in snap["positions"]:
        if row["id"] == pid:
            return copy.deepcopy(row)
    return None


# ---------------------------------------------------------------- history

def _indexed(series: dict[str, float], start: str) -> list[dict]:
    base = series.get(start)
    if not base:
        return []
    return [{"t": _day_ts(d), "pct": _r((v / base - 1.0) * 100.0)}
            for d, v in sorted(series.items()) if d >= start]


def _indexed_all(pv: dict[str, float], others: dict[str, dict]) -> dict:
    """Rebase the portfolio and every non-empty series of ``others`` to 0 % at
    the first date present in ALL of them; each is then aligned on the union
    of their days (from that date) with last-value carry-forward. Empty
    series stay ``[]``."""
    out = {"portfolio": [], **{k: [] for k in others}}
    live = {k: s for k, s in others.items() if s}
    if not pv or not live:
        return out
    common = sorted(set(pv).intersection(*live.values()))
    if not common:
        return out
    axis = sorted(d for d in set(pv).union(*live.values()) if d >= common[0])
    out["portfolio"] = _indexed(performance.carry(pv, axis), common[0])
    for k, s in live.items():
        out[k] = _indexed(performance.carry(s, axis), common[0])
    return out


def _perf_benchmark_id(cfg: dict, by_id: dict, default_id: str | None,
                       warnings: list[dict]) -> str | None:
    """``performanceBenchmark`` when it is on the watchlist, else the role
    benchmark (a configured-but-unknown id is reported, never guessed)."""
    want = cfg.get("performanceBenchmark")
    if isinstance(want, str) and want.strip():
        want = want.strip().upper()
        if want in by_id:
            return want
        _warn(warnings, "perf_benchmark_unknown",
              f"performanceBenchmark {want} is not on the watchlist; "
              f"using {default_id}", [want])
    return default_id


async def model(range_key: str = "3M") -> dict:
    """Everything the history / performance / attribution / risk views share
    for ``range_key`` (1M|3M|6M|1Y|ALL); raises ValueError on a bad range.

    Method (daily closes, UTC days, EUR):

    * Holdings: the daily close of the holding's EUR listing
      (``performance.fetch_rows``: listing bars up to 1Y, ``prices.daily_closes``
      for ALL). A holding with no bars at all is left out and reported in
      ``warnings`` (``history_missing``).
    * ``basis`` ``"lots"`` (every included holding has dated lots): shares
      held on each day come from the lots (trade-date accounting), the axis
      starts on the first day with a positive value and a close for every
      held holding, and external flows / TWR / net invested are computed.
      Otherwise ``"current_shares"``: today's share counts are applied to the
      whole window, the axis starts on the first day on which EVERY holding
      has a close, and warning ``history_basis_current_shares`` lists the
      holdings without dated lots. Days a holding did not trade (other
      exchange holiday) carry its previous close forward.
    * ``cost`` is the constant EUR cost basis of the book, or None when any
      included holding has no cost basis.
    * Benchmarks: ``bench`` is the role benchmark, ``perf`` the performance
      benchmark (``performanceBenchmark``, default the role benchmark; kind
      ``total_return`` for an ETF, else ``price``). Both are converted to EUR
      with the ECB rate of the same day (``forex.daily_rates``, last earlier
      rate on days without one); days without any rate are dropped, no rate
      at all -> empty series + ``fx_missing`` warning.
    * ``twr`` is the cumulative time-weighted return of the book as
      ``[(day, fraction)]`` for both bases (without lots there are no flows,
      so it is the value index).
    """
    rng = range_key.upper()
    if rng not in HISTORY_RANGES:
        raise ValueError(f"range must be one of {', '.join(HISTORY_RANGES)}")
    entries, portfolio, _ = _holdings()
    cfg = config_store.read()
    by_id = {e["id"]: e for e in registry.entries()}
    bench_id = registry.benchmark_id()
    warnings: list[dict] = []
    perf_id = _perf_benchmark_id(cfg, by_id, bench_id, warnings)

    lots_by_id = {e["id"]: performance.clean_lots(portfolio[e["id"]])
                  for e in entries}
    first_lot = min((lot["date"] for lots in lots_by_id.values()
                     for lot in lots), default=None)
    if rng != "ALL":
        days = performance.RANGE_DAYS[rng] + 10   # buffer so day 1 has a rate
    elif first_lot:
        days = (performance.today()
                - datetime.date.fromisoformat(first_lot)).days + 10
    else:
        days = performance.ALL_FALLBACK_DAYS + 10
    period = performance.period_for(days)
    if rng == "ALL" and not first_lot:
        period = "5y"                      # exactly 5 years, never 'max'
    # Lots-based ALL starts at the first lot (no trim); every other window is
    # cut to the same calendar floor so holdings and benchmarks agree.
    cutoff = (None if rng == "ALL" and first_lot
              else (performance.today()
                    - datetime.timedelta(days=days)).isoformat())

    ids = list(dict.fromkeys([e["id"] for e in entries]
                             + [i for i in (bench_id, perf_id) if i]))
    rates, rows = await asyncio.gather(
        _safe(forex.daily_rates(days), "daily rates"),
        asyncio.gather(*[performance.fetch_rows(i, rng, period)
                         for i in ids]))
    rates = rates if isinstance(rates, dict) else {}
    rows_by_id = dict(zip(ids, rows))

    def closes(pid: str) -> dict[str, float]:
        e = by_id.get(pid)
        if not e:
            return {}
        pts = performance.to_days(rows_by_id.get(pid),
                                  performance.factor_for(e, rates))
        return ({d: c for d, c in pts.items() if d >= cutoff}
                if cutoff else pts)

    included, series, missing = [], {}, []
    for e in entries:
        s = closes(e["id"])
        if s and _shares(portfolio[e["id"]]):
            included.append(e)
            series[e["id"]] = s
        else:
            missing.append(e["id"])
    _warn(warnings, "history_missing",
          "No price history, left out of the series: " + ", ".join(missing),
          missing)

    no_lots = [e["id"] for e in included if not lots_by_id[e["id"]]]
    basis = BASIS_LOTS if included and not no_lots else BASIS_CURRENT
    _warn(warnings, "history_basis_current_shares",
          "No dated lots, today's share counts are applied to the whole "
          "window: " + ", ".join(no_lots), no_lots)

    axis: list[str] = []
    pv: dict[str, float] = {}
    carried: dict[str, dict[str, float]] = {}
    if included and basis == BASIS_LOTS:
        full = sorted({d for s in series.values() for d in s})
        carried = {pid: performance.carry(s, full)
                   for pid, s in series.items()}
        held = {pid: performance.shares_by_day(lots_by_id[pid], full)
                for pid in series}
        for d in full:
            if any(held[pid][d] > 0 and d not in carried[pid]
                   for pid in series):
                continue
            v = sum(held[pid][d] * carried[pid][d]
                    for pid in series if held[pid][d] > 0)
            if not axis and v <= 0:
                continue
            axis.append(d)
            pv[d] = v
    elif included:
        start = max(min(s) for s in series.values())
        axis = sorted({d for s in series.values() for d in s if d >= start})
        carried = {pid: performance.carry(s, axis)
                   for pid, s in series.items()}
        for d in axis:
            pv[d] = sum(_shares(portfolio[pid]) * carried[pid][d]
                        for pid in series)

    flows: dict[str, float] = {}
    net: dict[str, float] = {}
    twr: list[tuple[str, float]] = []
    if axis:
        if basis == BASIS_LOTS:
            for pid in series:
                for d, f in performance.flows_by_day(lots_by_id[pid],
                                                     axis).items():
                    flows[d] = flows.get(d, 0.0) + f
                for d, v in performance.net_invested_by_day(lots_by_id[pid],
                                                            axis).items():
                    net[d] = net.get(d, 0.0) + v
        twr = performance.twr_series(pv, flows, axis)
    costs = [_invested(portfolio[e["id"]]) for e in included]
    cost = sum(costs) if costs and all(c is not None for c in costs) else None

    def bench_series(pid: str | None) -> dict[str, float]:
        if not pid or pid not in by_id:
            return {}
        if not rows_by_id.get(pid):
            _warn(warnings, "history_missing",
                  f"No price history for benchmark {pid}", [pid])
            return {}
        pts = closes(pid)
        cur = performance.entry_currency(by_id[pid])
        if not pts and cur != "EUR":
            _warn(warnings, "fx_missing",
                  "No USD/EUR rates: benchmark cannot be converted to EUR",
                  [pid])
        elif (rates and cur != "EUR"
                and not any(w["code"] == "fx_stale" for w in warnings)):
            age = (datetime.date.today()
                   - datetime.date.fromisoformat(max(rates))).days
            if age > 5:
                _warn(warnings, "fx_stale",
                      f"Newest USD/EUR rate is {age} days old", None)
        return pts

    bench_pts = bench_series(bench_id)
    perf_pts = bench_pts if perf_id == bench_id else bench_series(perf_id)

    def label(pid: str | None) -> str | None:
        return (by_id.get(pid) or {}).get("label") or pid

    return {
        "range": rng, "basis": basis, "axis": axis, "value": pv,
        "included": [e["id"] for e in included],
        "entries": {e["id"]: e for e in included},
        "series": series, "carried": carried, "lots": lots_by_id,
        "noLots": no_lots, "flows": flows, "invested": net, "twr": twr,
        "cost": cost, "rates": rates,
        "bench": {"id": bench_id, "label": label(bench_id),
                  "days": bench_pts},
        "perf": {"id": perf_id, "label": label(perf_id),
                 "kind": ("total_return"
                          if (by_id.get(perf_id) or {}).get("kind") == "etf"
                          else "price"),
                 "days": perf_pts},
        "warnings": warnings,
    }


async def history(range_key: str = "3M") -> dict:
    """Portfolio value vs benchmarks over ``range_key`` (1M|3M|6M|1Y|ALL).

    Built from ``model`` (see there for the valuation, axis and benchmark
    rules). Keys: ``range``, ``basis``, ``points`` [{t, valueEur,
    investedEur}] (``investedEur`` = the constant current cost basis, None
    when unknown), ``benchmark`` (role benchmark in EUR), ``perfBenchmark``
    {id, label, kind, points [{t, c}]}, ``indexed`` {portfolio, benchmark,
    perfBenchmark} (each rebased to 0 % at the first date present in all
    non-empty series), ``warnings`` and, with basis ``lots`` only,
    ``invested`` [{t, eur}] (net money put in by lots up to that day) and
    ``twr`` [{t, pct}] (cumulative time-weighted return).
    """
    m = await model(range_key)
    axis, pv = m["axis"], m["value"]
    bench, perf = m["bench"], m["perf"]
    out = {
        "range": m["range"],
        "basis": m["basis"],
        "points": [{"t": _day_ts(d), "valueEur": _r(pv[d]),
                    "investedEur": _r(m["cost"])} for d in axis],
        "benchmark": {
            "id": bench["id"],
            "label": bench["label"],
            "points": [{"t": _day_ts(d), "c": _r(c, 4)}
                       for d, c in sorted(bench["days"].items())],
            "currency": "EUR",
        },
        "perfBenchmark": {
            "id": perf["id"],
            "label": perf["label"],
            "kind": perf["kind"],
            "points": [{"t": _day_ts(d), "c": _r(c, 4)}
                       for d, c in sorted(perf["days"].items())],
        },
        "indexed": _indexed_all(pv, {"benchmark": bench["days"],
                                     "perfBenchmark": perf["days"]}),
        "warnings": m["warnings"],
    }
    if m["basis"] == BASIS_LOTS:
        out["invested"] = [{"t": _day_ts(d), "eur": _r(m["invested"][d])}
                           for d in axis]
        out["twr"] = [{"t": _day_ts(d), "pct": _r(x * 100.0)}
                      for d, x in m["twr"]]
    return out


async def performance_summary(range_key: str = "3M") -> dict:
    """Return of the book over ``range_key`` (served as GET
    /api/portfolio/performance).

    ``twrPct`` is the time-weighted return (flow-neutral with basis ``lots``,
    plain value change of the constant share counts otherwise),
    ``xirrPct`` the annualised money-weighted return (None unless basis is
    ``lots``; warning ``xirr_needs_dated_lots``), ``perfBenchmarkPct`` the
    EUR price change of the performance benchmark between the same first and
    last day, ``excessPct`` = ``twrPct - perfBenchmarkPct`` (percentage
    points). ``netFlowsEur`` is the sum of external flows inside the window
    (None without lots). ``asOf`` is the last valuation day.
    """
    m = await model(range_key)
    warnings, axis, pv, perf = m["warnings"], m["axis"], m["value"], m["perf"]
    out = {"range": m["range"], "basis": m["basis"], "twrPct": None,
           "xirrPct": None, "perfBenchmarkId": perf["id"],
           "perfBenchmarkPct": None, "excessPct": None,
           "startValueEur": None, "endValueEur": None, "netFlowsEur": None,
           "asOf": None, "warnings": warnings}
    if not axis:
        return out
    start, end = axis[0], axis[-1]
    twr = m["twr"][-1][1] * 100.0 if len(axis) > 1 else None
    pc = performance.carry(perf["days"], [start, end])
    bench_pct = ((pc[end] / pc[start] - 1.0) * 100.0
                 if len(pc) == 2 else None)
    out.update(
        twrPct=_r(twr), perfBenchmarkPct=_r(bench_pct),
        excessPct=(_r(twr - bench_pct)
                   if twr is not None and bench_pct is not None else None),
        startValueEur=_r(pv[start]), endValueEur=_r(pv[end]), asOf=end)
    if m["basis"] == BASIS_LOTS:
        out["netFlowsEur"] = _r(sum(m["flows"].values()))
        if len(axis) > 1:
            x = performance.xirr_for_window(
                {pid: m["lots"][pid] for pid in m["included"]}, start, end,
                pv[start], pv[end], m["range"] == "ALL")
            out["xirrPct"] = _r(x * 100.0) if x is not None else None
            if x is None:
                _warn(warnings, "xirr_unavailable",
                      "XIRR has no solution for these flows", None)
    else:
        _warn(warnings, "xirr_needs_dated_lots",
              "XIRR needs dated lots for every holding"
              + (": " + ", ".join(m["noLots"]) if m["noLots"] else ""),
              m["noLots"] or None)
    return out


async def attribution(range_key: str = "3M") -> dict:
    """Currency attribution of each holding's EUR listing return over
    ``range_key`` (served as GET /api/portfolio/attribution).

    ``eur`` is the listing's EUR price return from the window's first day
    (the first day on or after the first lot when the position has lots) to
    its last day. ``fx`` is the change of EUR per 1 unit of the exposure
    currency (``exposureCurrency[id]``, default the listing currency) over
    the same days (``forex.daily_rates``); EUR exposure has ``fx`` 0. Then
    ``local = (1 + eur) / (1 + fx) - 1`` and ``cross = eur - local - fx``, so
    ``eur = local + fx + cross`` exactly. Only USD has a rate source; any
    other exposure currency gets None and a warning.
    """
    m = await model(range_key)
    exposure = config_store.read().get("exposureCurrency")
    exposure = exposure if isinstance(exposure, dict) else {}
    axis, rates = m["axis"], m["rates"]
    end = axis[-1] if axis else None

    def fx_change(start_day: str, end_day: str) -> float | None:
        r0 = _num(forex.rate_on(rates, start_day)) if rates else None
        r1 = _num(forex.rate_on(rates, end_day)) if rates else None
        return r1 / r0 - 1.0 if r0 and r1 else None

    positions, no_fx, bad_ccy = [], [], []
    for pid in m["included"]:
        ccy = str(exposure.get(pid)
                  or performance.entry_currency(m["entries"][pid])).upper()
        lots = m["lots"][pid] if m["basis"] == BASIS_LOTS else []
        start = (performance.first_on_or_after(axis, lots[0]["date"])
                 if lots else axis[0])
        c0 = m["carried"][pid].get(start) if start else None
        c1 = m["carried"][pid].get(end)
        eur = c1 / c0 - 1.0 if c0 and c1 else None
        if ccy == "EUR":
            fx = 0.0
        elif ccy == "USD" and start:
            fx = fx_change(start, end)
            if fx is None:
                no_fx.append(pid)
        else:
            fx = None
            bad_ccy.append(pid)
        local = ((1.0 + eur) / (1.0 + fx) - 1.0
                 if eur is not None and fx is not None else None)
        cross = (eur - local - fx
                 if local is not None and fx is not None else None)
        positions.append({"id": pid, "exposureCurrency": ccy,
                          "eurReturnPct": _pct(eur),
                          "localReturnPct": _pct(local),
                          "fxReturnPct": _pct(fx), "crossPct": _pct(cross)})

    warnings: list[dict] = []
    _warn(warnings, "fx_missing",
          "No USD/EUR rates: local return cannot be split out: "
          + ", ".join(no_fx), no_fx)
    _warn(warnings, "fx_unsupported_currency",
          "Only USD exposure has an FX source: " + ", ".join(bad_ccy),
          bad_ccy)
    usd = fx_change(axis[0], end) if axis else None
    return {"range": m["range"], "positions": positions,
            "fx": {"pair": "USD/EUR",
                   "changePct": None if usd is None else _r(usd * 100.0)},
            "warnings": warnings}
