"""Technical indicators computed server-side from the history store.

Series are aligned 1:1 with the chart points timestamps and padded with None
until the window is full, so the frontend can overlay them directly on the
existing chart without re-indexing.
"""
import math
import logging

from app.api import prices

log = logging.getLogger("indicators")

MAX_POINTS = 2000


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = sum(values[:period])
    for i in range(period - 1, len(values)):
        if i >= period:
            window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI on the close series."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period

    def _val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _val(ag, al)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = _val(ag, al)
    return out


async def indicators(symbol: str, range_key: str = "1M",
                     ema_periods: tuple[int, ...] = (20, 50),
                     rsi_period: int = 14) -> dict | None:
    try:
        res = await prices.fetch_price_history(symbol, range_key)
    except ValueError:
        return None                      # unknown range: no series
    points = [p for p in ((res or {}).get("data") or [])
              if isinstance(p, dict) and isinstance(p.get("c"), (int, float))
              and math.isfinite(p["c"])]
    if len(points) < 3:
        return None
    if len(points) > MAX_POINTS:
        points = points[-MAX_POINTS:]
    closes = [float(p["c"]) for p in points]
    return {
        "symbol": symbol,
        "range": range_key,
        "t": [p["t"] for p in points],
        "ema": {str(n): ema(closes, n) for n in ema_periods if 0 < n < 500},
        "rsi": {str(rsi_period): rsi(closes, rsi_period)} if rsi_period > 0 else {},
        "last": closes[-1],
    }