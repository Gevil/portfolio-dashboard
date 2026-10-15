"""Risk view of the book (served as GET /api/portfolio/risk).

Inputs: the valuation snapshot (weights) and ``portfolio.model("1Y")`` (daily
EUR series). Methods:

* **Exposures** - ``direct`` = each priced holding's share of the book (%).
  ``lookThrough[id]`` = direct weight + sum over held funds of (fund weight x
  ``lookThrough[fund].weights[id]`` / 100): "how much of the book is exposed
  to ``id``, directly or through a fund". The rows overlap (a fund and the
  stock inside it both count that stock) so they do not sum to 100.
  ``byCurrency`` groups the direct weights by ``exposureCurrency[id]``
  (default: the listing currency).
* **Volatility** - sample standard deviation of daily log returns over the
  1Y window x sqrt(252), in %. The book's returns are flow-neutral
  (``performance.daily_returns``); the benchmark is the performance
  benchmark's EUR series on the same days. None below ``MIN_OBS`` returns.
* **Beta** - cov(book, benchmark) / var(benchmark) on the days both exist.
* **Drawdown** - on the TWR index (so a deposit is never a "recovery"):
  ``fromPeakPct`` today vs the window's peak (``peakDate``),
  ``maxDrawdownPct`` the worst peak-to-trough in the window.
* **Correlation** - Pearson on daily log returns of the holdings' EUR closes
  (previous close carried over days a venue was shut); symmetric, 1 on the
  diagonal, None where fewer than ``MIN_OBS`` common returns exist.
* **Concentration** - from the direct weights.
"""
import datetime
import math

from app.api import config_store, performance, portfolio, registry

WINDOW = "1Y"
MIN_OBS = 20
TRADING_DAYS = 252
STALE_LOOKTHROUGH_DAYS = 120


def today() -> datetime.date:
    return datetime.date.today()


def _r(value, nd: int = 2):
    return None if value is None else round(float(value), nd)


# --------------------------------------------------------------- statistics

def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def stdev(xs: list[float]) -> float | None:
    """Sample standard deviation (None below two values)."""
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def covariance(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx, my = mean(xs), mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation clamped to [-1, 1]; None for fewer than ``MIN_OBS`` pairs
    or a constant series."""
    if len(xs) != len(ys) or len(xs) < MIN_OBS:
        return None
    sx, sy, cov = stdev(xs), stdev(ys), covariance(xs, ys)
    if not sx or not sy or cov is None:
        return None
    return max(-1.0, min(1.0, cov / (sx * sy)))


def ann_vol_pct(returns: list[float]) -> float | None:
    if len(returns) < MIN_OBS:
        return None
    sd = stdev(returns)
    return None if sd is None else sd * math.sqrt(TRADING_DAYS) * 100.0


def beta(book: list[float], bench: list[float]) -> float | None:
    """cov(book, bench) / var(bench) on aligned returns."""
    if len(book) != len(bench) or len(book) < MIN_OBS:
        return None
    var = stdev(bench)
    cov = covariance(book, bench)
    if not var or cov is None:
        return None
    return cov / (var * var)


def log_returns(closes: dict[str, float], axis: list[str]) -> dict[str, float]:
    """{day: ln(close_d / close_prev)} over consecutive ``axis`` days that both
    have a positive close."""
    out = {}
    for prev, d in zip(axis, axis[1:]):
        c0, c1 = closes.get(prev), closes.get(d)
        if c0 and c1 and c0 > 0 and c1 > 0:
            out[d] = math.log(c1 / c0)
    return out


def correlation(returns: dict[str, dict[str, float]]) -> dict:
    """{ids, matrix} of pairwise Pearson correlations of ``{id: {day: r}}``,
    computed once per pair and mirrored (so the matrix is exactly symmetric)."""
    ids = list(returns)
    n = len(ids)
    matrix: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i in range(n):
        ri = returns[ids[i]]
        if len(ri) >= MIN_OBS and stdev(list(ri.values())):
            matrix[i][i] = 1.0
        for j in range(i + 1, n):
            rj = returns[ids[j]]
            days = sorted(set(ri) & set(rj))
            c = pearson([ri[d] for d in days], [rj[d] for d in days])
            matrix[i][j] = matrix[j][i] = _r(c, 4)
    return {"ids": ids, "matrix": matrix}


def drawdown(twr: list[tuple[str, float]]) -> dict:
    """Drawdown of the TWR index ``[(day, cumulative fraction)]``: distance of
    the last point from the running peak, that peak's date and the worst
    peak-to-trough."""
    if len(twr) < 2:
        return {"fromPeakPct": None, "peakDate": None,
                "maxDrawdownPct": None}
    peak_day, peak = twr[0][0], 1.0 + twr[0][1]
    worst = dd = 0.0
    for d, f in twr:
        idx = 1.0 + f
        if idx > peak:
            peak_day, peak = d, idx
        dd = idx / peak - 1.0
        worst = min(worst, dd)
    return {"fromPeakPct": _r(dd * 100.0), "peakDate": peak_day,
            "maxDrawdownPct": _r(worst * 100.0)}


# ---------------------------------------------------------------- exposures

def weights(positions: list[dict]) -> dict[str, float]:
    """{id: % of the priced book} from snapshot rows with a ``valueEur``."""
    priced = {p["id"]: p["valueEur"] for p in positions
              if p.get("valueEur") is not None and p["valueEur"] > 0}
    total = sum(priced.values())
    return {pid: v / total * 100.0 for pid, v in priced.items()} if total \
        else {}


def look_through(direct: dict[str, float], specs: dict) -> dict[str, float]:
    """Direct weight + sum over HELD funds of (fund weight x weight of the id
    inside the fund / 100). ``specs`` is the config ``lookThrough`` block."""
    out = dict(direct)
    for fund, spec in (specs or {}).items():
        w_fund = direct.get(fund)
        if not w_fund or not isinstance(spec, dict):
            continue
        for pid, pct in (spec.get("weights") or {}).items():
            p = performance.num(pct)
            if p:
                out[pid] = out.get(pid, 0.0) + w_fund * p / 100.0
    return out


def by_currency(direct: dict[str, float], currency_of) -> dict[str, float]:
    out: dict[str, float] = {}
    for pid, w in direct.items():
        ccy = currency_of(pid)
        out[ccy] = out.get(ccy, 0.0) + w
    return out


def concentration(direct: dict[str, float]) -> dict:
    ranked = sorted(direct.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return {"top2Pct": None, "largestId": None, "largestPct": None}
    return {"top2Pct": _r(sum(w for _, w in ranked[:2])),
            "largestId": ranked[0][0], "largestPct": _r(ranked[0][1])}


def _ranked(d: dict[str, float]) -> dict[str, float]:
    return {k: _r(v) for k, v in
            sorted(d.items(), key=lambda kv: kv[1], reverse=True)}


def _notes(direct: dict[str, float], specs: dict, kinds: dict,
           basis: str, no_lots: list[str]) -> list[str]:
    notes = []
    for pid in direct:
        spec = (specs or {}).get(pid)
        if spec is None and kinds.get(pid) == "etf":
            notes.append(f"No look-through weights for {pid}: what it holds "
                         "is counted only under its own id.")
        elif isinstance(spec, dict) and spec.get("asOf"):
            try:
                age = (today() - datetime.date.fromisoformat(
                    spec["asOf"])).days
            except ValueError:
                continue
            if age > STALE_LOOKTHROUGH_DAYS:
                notes.append(f"Look-through weights of {pid} are {age} days "
                             f"old (asOf {spec['asOf']}).")
    if any(isinstance(s, dict) and (s.get("weights") or {}) for s in
           (specs or {}).values()):
        notes.append("Look-through rows overlap (a fund and the stock inside "
                     "it both count that stock); they do not sum to 100.")
    if basis != portfolio.BASIS_LOTS and no_lots:
        notes.append("Volatility, beta and drawdown apply today's share "
                     "counts to the whole window (no dated lots for: "
                     + ", ".join(no_lots) + ").")
    return notes


# --------------------------------------------------------------------- view

async def risk() -> dict:
    """Payload of GET /api/portfolio/risk (see the module docstring)."""
    snap = await portfolio.snapshot()
    m = await portfolio.model(WINDOW)
    cfg = config_store.read()
    entries = {e["id"]: e for e in registry.entries()}
    exposure = cfg.get("exposureCurrency")
    exposure = exposure if isinstance(exposure, dict) else {}
    specs = cfg.get("lookThrough")
    specs = specs if isinstance(specs, dict) else {}

    def currency_of(pid: str) -> str:
        e = entries.get(pid)
        return str(exposure.get(pid) or (performance.entry_currency(e)
                                         if e else "EUR")).upper()

    direct = weights(snap["positions"])
    axis = m["axis"]

    port_r = {d: math.log(1.0 + r) for d, r in performance.daily_returns(
        m["value"], m["flows"], axis) if 1.0 + r > 0}
    bench_r = log_returns(performance.carry(m["perf"]["days"], axis), axis)
    days = sorted(port_r)
    book_all = [port_r[d] for d in days]
    bench_days = [d for d in days if d in bench_r]

    ids = [pid for pid in m["included"]
           if len(m["series"][pid]) > MIN_OBS]
    corr_returns: dict[str, dict[str, float]] = {}
    if ids:
        start = max(min(m["series"][pid]) for pid in ids)
        caxis = sorted({d for pid in ids for d in m["series"][pid]
                        if d >= start})
        corr_returns = {pid: log_returns(
            performance.carry(m["series"][pid], caxis), caxis)
            for pid in ids}

    return {
        "asOf": axis[-1] if axis else None,
        "exposures": {
            "direct": _ranked(direct),
            "lookThrough": _ranked(look_through(direct, specs)),
            "byCurrency": _ranked(by_currency(direct, currency_of)),
            "notes": _notes(direct, specs,
                            {pid: e.get("kind") for pid, e in entries.items()},
                            m["basis"], m["noLots"]),
        },
        "volatility": {
            "portfolioAnnPct": _r(ann_vol_pct(book_all)),
            "perfBenchmarkAnnPct": _r(ann_vol_pct(
                [bench_r[d] for d in bench_days])),
            "window": WINDOW,
        },
        "beta": _r(beta([port_r[d] for d in bench_days],
                        [bench_r[d] for d in bench_days]), 3),
        "drawdown": drawdown(m["twr"]),
        "correlation": correlation(corr_returns),
        "concentration": concentration(direct),
    }
