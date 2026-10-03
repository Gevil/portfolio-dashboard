"""
Euronext (Amsterdam) intraday data via yfinance, no API key.

yfinance "ASML.AS" resolves to Euronext Amsterdam (EUR). We pull 15-minute
bars (reliable, 60-day lookback) to cover the EU trading session, which the
Twelve Data / Finnhub free tiers do NOT serve (they only know the NASDAQ "ASML"
listing).

Bars are returned in EUR. The caller converts to USD (or keeps EUR) as needed.
"""
import logging
import math
import asyncio

import yfinance as yf

log = logging.getLogger(__name__)

# Throttle: Yahoo free intraday is fine, but don't hammer. One 15-min fetch
# per symbol per 10 minutes is plenty for a live-ish EU session chart.
_last_fetch: dict[str, float] = {}
_MIN_INTERVAL = 600  # 10 minutes per symbol
_LOCK = asyncio.Lock()


def _eu_symbol_for(symbol: str, label: str | None) -> str | None:
    """
    Given an internal symbol (e.g. "ASML") and its label (e.g. "ASML.AS"),
    return the Euronext yfinance symbol if this is a European-listed name.
    Returns None for US-only symbols.
    """
    cand = (label or symbol or "").strip().upper()
    # Euronext Amsterdam suffix used by yfinance
    if cand.endswith(".AS") or cand.endswith(".DE") or cand.endswith(".PA"):
        return cand
    return None


def _yf_bars(eu_symbol: str, days: int = 2) -> list[dict] | None:
    """Blocking yfinance 15-min bars for a Euronext symbol. Returns [{t, c}] (c in EUR)."""
    tk = yf.Ticker(eu_symbol)
    hist = tk.history(period=f"{days}d", interval="15m")
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    pts = []
    for ts, row in hist.iterrows():
        c = row.get("Close")
        if c is None:
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if math.isnan(c):
            continue
        pts.append({"t": int(ts.timestamp()), "c": c})
    return pts or None


async def fetch_eu_15m(eu_symbol: str, days: int = 2) -> list[dict] | None:
    """
    Async, throttled 15-min Euronext bars for a yfinance Euronext symbol.
    Returns list of {"t": unix_ts, "c": price_in_EUR} or None (throttled/error).
    """
    import time as _t
    now = _t.time()
    if now - _last_fetch.get(eu_symbol, 0) < _MIN_INTERVAL:
        return None
    async with _LOCK:
        _last_fetch[eu_symbol] = now
        try:
            pts = await asyncio.get_running_loop().run_in_executor(None, _yf_bars, eu_symbol, days)
            if pts:
                log.debug("Euronext %s: %d 15m bars", eu_symbol, len(pts))
            return pts
        except Exception as e:
            log.warning("Euronext 15m fetch failed for %s: %s", eu_symbol, e)
            return None