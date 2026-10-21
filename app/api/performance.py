"""Performance maths: held shares from lots, TWR, XIRR and daily-close loading.

Everything is EUR and runs on calendar days (``YYYY-MM-DD``, UTC for ``t``
bars, exchange-local for ``prices.daily_closes``). The pure functions take
plain dicts/lists so they are testable without a network; the async part only
fetches rows (``fetch_rows``).

Method:

* **Held shares** (trade-date accounting): the shares held at the end of day
  ``d`` are the sum of buys minus sells of all lots dated ``<= d``. Within one
  date buys come before sells. A lot dated on a non-trading day counts from
  the next trading day of the series.
* **External flows** ``F``: a buy is ``+(shares x price + fees)``, a sell is
  ``-(shares x price - fees)``. A flow is booked on the first series day
  ``>=`` the lot date; lots dated on or before the window's first day are part
  of its opening value, not flows.
* **TWR**: daily geometric linking with flows at the end of the day,
  ``r_d = (V_d - F_d) / V_{d-1} - 1``. A day whose previous value is not
  positive is skipped (nothing to compound on). Buy fees and the gap between a
  lot's price and that day's close therefore show up as the day's return, not
  as a flow.
* **XIRR**: annualised money-weighted return on dated flows (investor sign:
  money in is negative) plus the terminal value, ``sum cf x (1 + r)^-(days /
  365) = 0``, solved by bisection on a bracket whose end signs are checked
  first (no sign change -> None, never a guess).
* **Data**: ranges up to 1Y use ``prices.listing_bars``; ``ALL`` uses
  ``prices.daily_closes`` (raw unadjusted closes) with a period picked from
  the age of the oldest lot.
"""
import bisect
import datetime
import logging

from app.api import forex, prices

log = logging.getLogger(__name__)

RANGES = ("1M", "3M", "6M", "1Y", "ALL")
RANGE_DAYS = {"1M": 31, "3M": 92, "6M": 183, "1Y": 366}
ALL_FALLBACK_DAYS = 5 * 365          # ALL without any dated lot
_PERIODS = (("6mo", 183), ("1y", 366), ("2y", 731), ("5y", 1826))
_EPS = 1e-9
_XIRR_LO, _XIRR_HI = -0.999999, 1e6


def today() -> datetime.date:
    return datetime.date.today()


# ---------------------------------------------------------------- helpers

def num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def day_of(t) -> str:
    return datetime.datetime.fromtimestamp(
        float(t), datetime.timezone.utc).date().isoformat()


def day_ts(day: str) -> int:
    return int(datetime.datetime.fromisoformat(day).replace(
        tzinfo=datetime.timezone.utc).timestamp())


def _iso(value) -> str | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def entry_currency(entry: dict) -> str:
    """Currency of the entry's price series: listing currency, else the quote
    currency (index entries have no listing), else EUR."""
    return ((entry.get("listing") or {}).get("currency")
            or entry.get("quoteCurrency") or "EUR").upper()


def usd_factor(rates: dict):
    """``factor_for_day`` turning USD into EUR with ``forex.rate_on`` (None on
    days without a rate, or when there are no rates at all)."""
    return lambda day: num(forex.rate_on(rates, day)) if rates else None


def factor_for(entry: dict, rates: dict):
    """Per-day EUR multiplier for the entry's series: 1 for EUR, the USD/EUR
    rate for USD, None (series dropped) for any other currency."""
    cur = entry_currency(entry)
    if cur == "EUR":
        return lambda _day: 1.0
    if cur == "USD":
        return usd_factor(rates)
    return lambda _day: None


def to_days(rows, factor_for_day=None) -> dict[str, float]:
    """{iso_day: last close of that day} from ``[{t, c}]`` bars or
    ``[(iso_date, close)]`` rows (ascending or not). With ``factor_for_day``
    each close is converted; days without a factor are dropped."""
    pts = []
    for r in rows or []:
        if isinstance(r, dict):
            t = num(r.get("t"))
            if t is None:
                continue
            pts.append((t, day_of(t), num(r.get("c"))))
        elif isinstance(r, (tuple, list)) and len(r) == 2:
            day = _iso(r[0])
            if day is None:
                continue
            pts.append((day_ts(day), day, num(r[1])))
    pts.sort(key=lambda p: p[0])
    out: dict[str, float] = {}
    for _, day, c in pts:
        if c is None or c <= 0:
            continue
        if factor_for_day is not None:
            f = factor_for_day(day)
            if f is None:
                continue
            c = c * f
        out[day] = c
    return out


def carry(series: dict[str, float], days: list[str]) -> dict[str, float]:
    """Carry the last known value forward over ``days`` (ascending); days
    before the series' first value stay absent."""
    out, last = {}, None
    idx = sorted(series)
    j = 0
    for d in days:
        while j < len(idx) and idx[j] <= d:
            last = series[idx[j]]
            j += 1
        if last is not None:
            out[d] = last
    return out


def period_for(days: int) -> str:
    """Smallest ``prices.daily_closes`` period covering ``days`` back."""
    for name, span in _PERIODS:
        if days <= span:
            return name
    return "max"


async def fetch_rows(ident: str, rng: str, period: str = "2y") -> list:
    """Raw closes of ``ident``: listing bars for 1M..1Y, daily closes for ALL.
    Never raises ([] on failure)."""
    try:
        if rng == "ALL":
            return await prices.daily_closes(ident, period) or []
        return await prices.listing_bars(ident, rng) or []
    except Exception as e:
        log.warning("performance: closes %s %s failed: %s", ident, rng, e)
        return []


# ------------------------------------------------------------------- lots

def clean_lots(pos) -> list[dict]:
    """Well-formed lots of a stored position, sorted by date (buys first on
    the same day). Malformed rows are skipped: readers never raise on stored
    data (``config_edit`` is where lots are validated)."""
    out = []
    for lot in (pos or {}).get("lots") or []:
        if not isinstance(lot, dict):
            continue
        day = _iso(lot.get("date"))
        shares, price = num(lot.get("shares")), num(lot.get("priceEur"))
        fees = num(lot.get("feesEur")) or 0.0
        side = lot.get("side")
        if (day is None or side not in ("buy", "sell") or not shares
                or shares <= 0 or not price or price <= 0 or fees < 0):
            continue
        out.append({"date": day, "shares": shares, "priceEur": price,
                    "feesEur": fees, "side": side})
    out.sort(key=lambda lot: (lot["date"], lot["side"] != "buy"))
    return out


def lot_flow(lot: dict) -> float:
    """External flow of one lot: buy = +cost incl. fees, sell = -proceeds net
    of fees."""
    gross = lot["shares"] * lot["priceEur"]
    return gross + lot["feesEur"] if lot["side"] == "buy" \
        else -(gross - lot["feesEur"])


def shares_by_day(lots: list[dict], axis: list[str]) -> dict[str, float]:
    """{day: shares held at the end of the day} for ``axis`` (ascending);
    ``lots`` sorted by date."""
    out, held, j = {}, 0.0, 0
    for d in axis:
        while j < len(lots) and lots[j]["date"] <= d:
            held += lots[j]["shares"] if lots[j]["side"] == "buy" \
                else -lots[j]["shares"]
            j += 1
        out[d] = held if held > _EPS else 0.0
    return out


def flows_by_day(lots: list[dict], axis: list[str]) -> dict[str, float]:
    """External flows inside the window, booked on the first ``axis`` day on
    or after the lot date. Lots dated on/before ``axis[0]`` (opening value) or
    after ``axis[-1]`` are not flows."""
    out: dict[str, float] = {}
    if len(axis) < 2:
        return out
    for lot in lots:
        if lot["date"] <= axis[0] or lot["date"] > axis[-1]:
            continue
        d = axis[bisect.bisect_left(axis, lot["date"])]
        out[d] = out.get(d, 0.0) + lot_flow(lot)
    return out


def net_invested_by_day(lots: list[dict], axis: list[str]) -> dict[str, float]:
    """{day: money put in minus money taken out by lots dated <= day} (all
    history, including lots before the window)."""
    out, total, j = {}, 0.0, 0
    for d in axis:
        while j < len(lots) and lots[j]["date"] <= d:
            total += lot_flow(lots[j])
            j += 1
        out[d] = total
    return out


# -------------------------------------------------------------------- TWR

def daily_returns(values: dict[str, float], flows: dict[str, float],
                  axis: list[str]) -> list[tuple[str, float]]:
    """[(day, r)] with ``r = (V_d - F_d) / V_{d-1} - 1`` for every day after
    the first whose previous value is positive. A day with ``V_d - F_d <= 0``
    (a loss of 100 % or more, impossible for a long-only book) means the day's
    flow is inconsistent with the value it added (e.g. a lot whose ``priceEur``
    holds the order total instead of the per-share price); it is skipped, never
    compounded, and :func:`inconsistent_flow_days` reports it."""
    out = []
    for prev, d in zip(axis, axis[1:]):
        v0 = values.get(prev)
        v1 = values.get(d)
        if v0 is None or v1 is None or v0 <= 0:
            continue
        net = v1 - flows.get(d, 0.0)
        if net <= 0:
            continue
        out.append((d, net / v0 - 1.0))
    return out


def inconsistent_flow_days(values: dict[str, float], flows: dict[str, float],
                           axis: list[str]) -> list[str]:
    """Days skipped by :func:`daily_returns` because ``V_d - F_d <= 0``."""
    out = []
    for prev, d in zip(axis, axis[1:]):
        v0, v1 = values.get(prev), values.get(d)
        if v0 is None or v1 is None or v0 <= 0:
            continue
        if v1 - flows.get(d, 0.0) <= 0:
            out.append(d)
    return out


def twr_series(values: dict[str, float], flows: dict[str, float],
               axis: list[str]) -> list[tuple[str, float]]:
    """[(day, cumulative TWR as a fraction)], 0.0 on ``axis[0]``."""
    if not axis:
        return []
    rets = dict(daily_returns(values, flows, axis))
    out, idx = [(axis[0], 0.0)], 1.0
    for d in axis[1:]:
        idx *= 1.0 + rets.get(d, 0.0)
        out.append((d, idx - 1.0))
    return out


# ------------------------------------------------------------------- XIRR

def xirr(flows) -> float | None:
    """Annualised rate (fraction) at which ``sum cf x (1 + r)^-(days/365)`` is
    zero for ``[(date or ISO str, amount)]``; None without both signs, or when
    the bracket [-99.9999 %, +1e8 %] holds no sign change."""
    pts = []
    for d, a in flows:
        a = num(a)
        if not a:
            continue
        if not isinstance(d, datetime.date):
            d = datetime.date.fromisoformat(str(d)[:10])
        pts.append((d, a))
    if (len(pts) < 2 or not any(a < 0 for _, a in pts)
            or not any(a > 0 for _, a in pts)):
        return None
    t0 = min(d for d, _ in pts)
    terms = [((d - t0).days / 365.0, a) for d, a in pts]

    def npv(r: float) -> float:
        return sum(a * (1.0 + r) ** -t for t, a in terms)

    try:
        lo, hi = _XIRR_LO, _XIRR_HI
        f_lo, f_hi = npv(lo), npv(hi)
        if f_lo == 0:
            return lo
        if f_hi == 0:
            return hi
        if (f_lo > 0) == (f_hi > 0):
            return None
        for _ in range(200):
            mid = (lo + hi) / 2.0
            f_mid = npv(mid)
            if f_mid == 0:
                return mid
            if (f_mid > 0) == (f_lo > 0):
                lo, f_lo = mid, f_mid
            else:
                hi = mid
            if hi - lo < 1e-13:
                break
        return (lo + hi) / 2.0
    except OverflowError:
        return None


def xirr_for_window(lots_by_id: dict[str, list[dict]], start: str, end: str,
                    start_value: float, end_value: float,
                    all_history: bool = False) -> float | None:
    """XIRR of a window: the opening value as an outflow on ``start`` (unless
    ``all_history``: then every lot up to ``end`` is a dated flow and there is
    no opening value), the window's lots, and the terminal value on ``end``."""
    flows = []
    if not all_history:
        flows.append((start, -start_value))
    for lots in lots_by_id.values():
        for lot in lots:
            if lot["date"] > end:
                continue
            if not all_history and lot["date"] <= start:
                continue
            flows.append((lot["date"], -lot_flow(lot)))
    flows.append((end, end_value))
    return xirr(flows)


def first_on_or_after(axis: list[str], day: str) -> str | None:
    """First ``axis`` day (ascending) on or after ``day``; None past the end."""
    i = bisect.bisect_left(axis, day)
    return axis[i] if i < len(axis) else None
