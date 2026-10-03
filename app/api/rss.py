"""Keyless RSS news fallback (Google News / CNBC / MarketWatch).

Yahoo Finance RSS is dead from this host (429/404 — P4 probe report); these
three are live-verified. Google News is UA-agnostic and symbol-scoped
(items marked ``direct`` bypass the keyword gate in news_alerts); CNBC needs
a browser UA (Akamai 403s plain clients); MarketWatch accepts anything.

Used by ``news_alerts`` when Finnhub + GDELT produced nothing inside the
lookback window, and by ``topnews`` to backfill the news panel.
"""
import email.utils
import html
import logging
import os
import time
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger("rss")

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
GOOGLE_NEWS = ("https://news.google.com/rss/search"
               "?q=%22{q}%22+when%3A{days}d&hl=en-US&gl=US&ceid=US%3Aen")
GLOBAL_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]
MAX_ITEMS = 25

_client: httpx.AsyncClient | None = None


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers={"User-Agent": BROWSER_UA},
                                    timeout=15, follow_redirects=True)
    return _client


def _parse_rss(xml_text: str, source: str, direct: bool) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("rss parse failed (%s): %s", source, e)
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub = it.findtext("pubDate") or ""
        try:
            ts = email.utils.parsedate_to_datetime(pub).timestamp()
        except (TypeError, ValueError):
            ts = time.time()
        desc = html.unescape((it.findtext("description") or "").strip())
        out.append({
            "headline": html.unescape(title)[:300],
            "summary": desc[:300],
            "url": link,
            "source": source,
            "published_at": ts,
            "direct": direct,
        })
        if len(out) >= MAX_ITEMS:
            break
    return out


async def fetch_symbol(ticker: str, days: int = 2) -> list[dict]:
    """Symbol-scoped items (bypass keyword gate as ``direct``)."""
    url = GOOGLE_NEWS.format(q=ticker, days=days)
    try:
        r = await _client_get().get(url)
        if not r.is_success:
            log.warning("google news %s HTTP %s", ticker, r.status_code)
            return []
        return _parse_rss(r.text, "Google News", direct=True)
    except httpx.HTTPError as e:
        log.warning("google news %s failed: %s", ticker, e)
        return []


async def fetch_global() -> list[dict]:
    """Top-story feeds; keyword-gated downstream (not ``direct``)."""
    out: list[dict] = []
    for name, url in GLOBAL_FEEDS:
        try:
            r = await _client_get().get(url)
            if r.is_success:
                out.extend(_parse_rss(r.text, name, direct=False))
            else:
                log.warning("rss %s HTTP %s", name, r.status_code)
        except httpx.HTTPError as e:
            log.warning("rss %s failed: %s", name, e)
    return out


async def close() -> None:
    if _client and not _client.is_closed:
        await _client.aclose()