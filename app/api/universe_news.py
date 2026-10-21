"""Open-universe news: a free-source collector and the point-in-time (PIT) store behind the forecast packets
(docs/plans/forecaster.md, docs/research/predictive-signals.md section 2).

Sources, per registry id (``paper_universe``), each isolated from the others:
``yfinance`` (``Ticker(yahoo).news``), ``google`` (Google News RSS by company name + id; English plus the home
language de/nl/fr taken from the ISIN country), ``gdelt`` (DOC API, through ``topnews``' spacing and persisted
backoff marker), ``finnhub`` (company-news, US ISINs only, needs ``FINNHUB_API_KEY``) and ``topnews`` (the existing
watchlist cache, read from disk). Every source has its own spacing, health row and exponential backoff: a failing
source costs that source's items only. ETFs get yfinance + topnews (a name search is noise for them).

Cadence (container-local clock): held names + the top 30 candidates every 30 min between 07:00 and 22:00, every other
name (and the hot names outside that window) every 3 h, and a FULL sweep of the registry in the 2.5 h before the
close of the week's last XETR session (the weekly cohort's as-of), so the Friday packets see a fresh store.

Store: ``data/universe_news/<YYYY-MM>.jsonl`` (UTC month of ``ingestedAt``), one row per (id, title)
``{id, title, url, domain, source, publishedAt, ingestedAt, lang, titleHash}``. ``ingestedAt`` is when this process
stored the row (rounded UP to the second, never backdated); ``publishedAt`` is the source's own time, clamped to
``ingestedAt`` when a feed dates an item in the future and dropped when missing. Titles are ``textsafe``-cleaned with
the prompt frame delimiters removed; no summary and no article body is stored. The URL is kept for the owner
(``GET /api/paper/news``) and never leaves this module towards a model, a packet or a notification:
:func:`pit_news` has no URL field.

Packets read the store only through :func:`pit_news` (``publishedAt`` AND ``ingestedAt`` <= the as-of, last 7 days)
and :func:`next_earnings` (the newest yfinance-calendar observation fetched at or before the as-of). The daily
earnings observations live in ``data/universe_news/earnings.jsonl`` (one row per successful check).

Event cards (``event_cards.py``) are fed from here for held + top-30 names; they run in their own task, so a lane
outage never delays the ingestion pass. They are log-only.

Gate: ``UNIVERSE_NEWS_ENABLED`` (default: on when ``FORECAST_ENABLED=1``), read at ``start()``.
"""
import asyncio
import datetime
import email.utils
import hashlib
import html
import json
import logging
import math
import os
import pathlib
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
import yfinance as yf

from app.api import calendars, jsonstore, paper_universe, prices, runlog, textsafe, topnews
from app.api.providers import finnhub

log = logging.getLogger(__name__)

MIC = "XETR"
DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
NEWS_DIR = DATA_DIR / "universe_news"
STATE_FILE = NEWS_DIR / "state.json"
EARNINGS_FILE = NEWS_DIR / "earnings.jsonl"
_BERLIN = ZoneInfo("Europe/Berlin")

# ---- cadence
POLL_S = 60
HOT_INTERVAL_S = 1800
COLD_INTERVAL_S = 3 * 3600
HOT_WINDOW = (7, 22)                    # local hours [7, 22)
SWEEP_LEAD_S = 9000                     # the full sweep opens this long before the week's last close
PASS_BUDGET_S = 240                     # no new name is started after this many seconds of one pass
EARNINGS_EVERY_S = 20 * 3600
EARNINGS_PER_PASS = 20
HOT_TOP = 30
# ``ops_watch`` calls a worker stalled after 2 x interval_s: one pass can run PASS_BUDGET_S plus the request in flight,
# the earnings step and the poll gap.
WATCH_INTERVAL_S = POLL_S + PASS_BUDGET_S + 300

# ---- data rules
TITLE_CHARS = 240
URL_CHARS = 500
MAX_AGE_S = 10 * 86400                  # an item published longer ago than this is not stored
NEWS_WINDOW_S = 7 * 86400               # what a packet may show
CARD_MAX_AGE_S = 48 * 3600              # an event card is only asked for items this fresh
SEEN_MONTHS_BACK = 1

# ---- sources
SOURCES = ("yfinance", "google", "gdelt", "finnhub", "topnews", "calendar")
SPACING_S = {"yfinance": 0.5, "google": 2.0, "finnhub": 1.1, "calendar": 0.5}
BACKOFF_BASE_S = 120.0
BACKOFF_MAX_S = 3600.0
FAILS_BEFORE_BACKOFF = 3                # a rate-limit answer backs off at once
USER_AGENT = "portfolio-dashboard/1.0 (personal research; polite rate limits)"
GOOGLE_NEWS = "https://news.google.com/rss/search"
MAX_XML_CHARS = 2_000_000
PER_CALL_ITEMS = 25
YF_KEY = "universe_news"                # prices._yf backoff key: Yahoo news never blocks price fetches
# hosts that only relay a story: the publisher name is the better domain
AGGREGATOR_HOSTS = {"finance.yahoo.com", "yahoo.com", "uk.finance.yahoo.com", "de.finance.yahoo.com", "news.google.com",
                    "finnhub.io", "api.gdeltproject.org"}
HOME_LANG = {"DE": ("de", "DE"), "AT": ("de", "AT"), "NL": ("nl", "NL"), "FR": ("fr", "FR")}
LANG_NAMES = {"english": "en", "german": "de", "dutch": "nl", "french": "fr", "spanish": "es", "italian": "it",
              "portuguese": "pt", "swedish": "sv", "danish": "da", "finnish": "fi", "norwegian": "no", "polish": "pl"}

_task: asyncio.Task | None = None
_cards_task: asyncio.Task | None = None
_stats = {"running": False, "enabled": False, "last_run": 0.0, "runs": 0, "errors": 0, "lastError": None,
          "ingested": 0, "lastPass": None}
_health: dict[str, dict] = {}
_state: dict | None = None
_seen: dict = {"keys": None}
_last_call: dict[str, float] = {}
_client: httpx.AsyncClient | None = None
_read_cache: dict = {}                  # path -> (signature, parsed rows)


class SourceError(Exception):
    """A source call that failed. ``kind``: rate_limited | timeout | network | http | parse."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


class SourceSkip(Exception):
    """A source that does not apply to this name right now (not a failure, not a success)."""


# ------------------------------------------------------------------ clocks and seams (tests patch these)

def _now() -> float:
    return time.time()


def _mono() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def enabled() -> bool:
    v = os.getenv("UNIVERSE_NEWS_ENABLED", "").strip()
    if not v:
        v = os.getenv("FORECAST_ENABLED", "0").strip()
    return v == "1"


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
    return _client


async def _get(url: str, params: dict | None = None) -> tuple[int, str]:
    """One HTTP GET -> (status, text). The only place this module touches the network over httpx."""
    r = await _client_get().get(url, params=params)
    return r.status_code, r.text


async def _gdelt_pace() -> None:
    await topnews._gdelt_space_out()


async def _finnhub_news(symbol: str, frm: str, to: str) -> tuple[str, object]:
    """(finnhub result kind, parsed JSON | None). ``providers.finnhub._get`` owns the key, the 429 backoff."""
    kind, r = await finnhub._get("/company-news", {"symbol": symbol, "from": frm, "to": to})
    if kind != finnhub.OK or r is None:
        return kind, None
    try:
        return kind, r.json()
    except ValueError:
        return "bad_json", None


def focus_ids() -> tuple[list[str], list[str]]:
    """(held ids, top-30 candidate ids): the names that get the 30-minute cadence and event cards."""
    from app.api import forecast_worker           # lazy: forecast_worker -> packets -> this module
    held = [p for p in forecast_worker.held_ids() if paper_universe.get(p)]
    return held, forecast_worker.top_candidates(HOT_TOP)


def _yf_news_blocking(sym: str) -> list:
    return list(yf.Ticker(sym).news or [])


def _yf_calendar_blocking(sym: str, today_iso: str) -> tuple:
    """(earliest earnings date on/after today as ISO | None,) from ``Ticker.calendar``."""
    cal = yf.Ticker(sym).calendar
    raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if raw is None:
        return (None,)
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    days = []
    for x in raw:
        d = _to_date(x)
        if d is not None and d >= today_iso:
            days.append(d)
    return (min(days) if days else None,)


def _to_date(x) -> str | None:
    if isinstance(x, datetime.datetime):
        return x.date().isoformat()
    if isinstance(x, datetime.date):
        return x.isoformat()
    if hasattr(x, "to_pydatetime"):
        try:
            return x.to_pydatetime().date().isoformat()
        except Exception:
            return None
    if isinstance(x, str) and re.match(r"^\d{4}-\d{2}-\d{2}", x):
        return x[:10]
    return None


# ------------------------------------------------------------------ pure helpers

def _epoch(value) -> float | None:
    """Epoch seconds of an epoch (s or ms), ISO-8601, RFC-2822 or GDELT ``seendate`` stamp; None when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:
            v /= 1000.0
        return v if math.isfinite(v) and v > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if re.match(r"^\d{8}T\d{6}Z?$", s):
        v = topnews._gdelt_published_at(s)
        return float(v) if v else None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    v = dt.timestamp()
    return v if math.isfinite(v) and v > 0 else None


def iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).replace(microsecond=0).isoformat()


def iso_ceil(epoch: float) -> str:
    """Second-resolution stamp that is never earlier than the instant (a row is not older than it is)."""
    return iso(math.ceil(epoch))


_NORM = re.compile(r"[\W_]+", re.UNICODE)


def title_hash(title: str) -> str:
    """16 hex chars of the case/punctuation-insensitive title: the per-id dedupe key."""
    norm = _NORM.sub(" ", unicodedata.normalize("NFKC", title or "").casefold()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def host_of(url) -> str:
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def slug(name) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").casefold()).strip("-")[:40]


def publisher_domain(url, publisher) -> str:
    """The publisher's host; a relay host (Yahoo, Google, Finnhub) gives way to the publisher's name."""
    host = host_of(url)
    if host and host not in AGGREGATOR_HOSTS:
        return host
    return slug(publisher) or host or "unknown"


def _lang(value) -> str:
    if not isinstance(value, str):
        return "und"
    v = value.strip().lower()
    if v in LANG_NAMES:
        return LANG_NAMES[v]
    return v[:2] if re.match(r"^[a-z]{2}([-_][a-z]{2,})?$", v) else "und"


def _clean_url(value) -> str:
    if not isinstance(value, str):
        return ""
    url = textsafe.clean_text(value, URL_CHARS + 1, strip_urls=False)
    if not url or " " in url or len(url) > URL_CHARS:
        return ""
    try:
        return url if urllib.parse.urlsplit(url).scheme in ("http", "https") else ""
    except ValueError:
        return ""


def make_row(pid: str, cand: dict, source: str, ingested: float) -> dict | None:
    """A store row from a source candidate ``{title, url, domain, published, lang}``, or None when it is unusable
    (no title, no usable publication time, too old)."""
    title = textsafe.clean_headline(cand.get("title"), TITLE_CHARS).replace("<<<", "").replace(">>>", "").strip()
    pub = _epoch(cand.get("published"))
    if not title or pub is None:
        return None
    ing = math.ceil(ingested)
    if ing - pub > MAX_AGE_S:
        return None
    pub = min(pub, ing)                  # a feed that dates a story in the future is wrong about the clock
    domain = textsafe.clean_text(cand.get("domain"), 80).lower().replace("<<<", "").replace(">>>", "") or "unknown"
    return {"id": pid, "title": title, "url": _clean_url(cand.get("url")), "domain": domain, "source": source,
            "publishedAt": iso(pub), "ingestedAt": iso(ing), "lang": _lang(cand.get("lang")),
            "titleHash": title_hash(title)}


# ------------------------------------------------------------------ per-source health and backoff

def _h(src: str) -> dict:
    return _health.setdefault(src, {"ok": 0, "fail": 0, "skipped": 0, "items": 0, "consecutiveFails": 0,
                                    "lastOkAt": None, "lastFailAt": None, "lastError": None, "backoffUntil": 0.0})


def source_ready(src: str, now: float) -> bool:
    return now >= _h(src)["backoffUntil"]


def note_ok(src: str, new_items: int, now: float) -> None:
    h = _h(src)
    h.update(ok=h["ok"] + 1, items=h["items"] + new_items, consecutiveFails=0, lastOkAt=now, lastError=None)


def note_fail(src: str, kind: str, detail: str, now: float) -> None:
    """Count a failure and back the source off: at once for a rate limit, after FAILS_BEFORE_BACKOFF in a row
    otherwise; the pause doubles per further failure up to BACKOFF_MAX_S."""
    h = _h(src)
    h["fail"] += 1
    h["consecutiveFails"] += 1
    h["lastFailAt"] = now
    h["lastError"] = f"{kind}: {detail}"[:120] if detail else kind
    n = h["consecutiveFails"] if kind == "rate_limited" else h["consecutiveFails"] - FAILS_BEFORE_BACKOFF + 1
    if n >= 1:
        h["backoffUntil"] = now + min(BACKOFF_BASE_S * 2 ** (n - 1), BACKOFF_MAX_S)


def note_skip(src: str) -> None:
    _h(src)["skipped"] += 1


def yahoo_enabled() -> bool:
    """Yahoo news returns nothing from this host: off unless UNIVERSE_NEWS_YAHOO=1."""
    return os.getenv("UNIVERSE_NEWS_YAHOO", "").strip() == "1"


def health() -> dict:
    out = {s: dict(_h(s)) for s in SOURCES}
    if not yahoo_enabled():
        out["yfinance"] = {**out["yfinance"], "disabled": True, "consecutiveFails": 0,
                           "backoffUntil": 0, "lastError": None}
    return out


async def _pace(src: str) -> None:
    """Hold the source's minimum spacing between two requests."""
    gap = SPACING_S.get(src, 0.0)
    last = _last_call.get(src)
    if gap and last is not None:
        wait = gap - (_mono() - last)
        if wait > 0:
            await _sleep(wait)
    _last_call[src] = _mono()


async def _checked_get(url: str, params: dict | None = None) -> str:
    try:
        status, text = await _get(url, params)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        raise SourceError("timeout")
    except Exception as e:
        raise SourceError("network", type(e).__name__)
    if status == 429:
        raise SourceError("rate_limited", "HTTP 429")
    if status != 200:
        raise SourceError("http", f"HTTP {status}")
    return text if isinstance(text, str) else ""


# ------------------------------------------------------------------ source fetchers -> candidate dicts

def _parse_yf_item(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    c = item["content"] if isinstance(item.get("content"), dict) else item
    link = None
    for key in ("canonicalUrl", "clickThroughUrl"):
        v = c.get(key)
        if isinstance(v, dict) and v.get("url"):
            link = v["url"]
            break
    link = link or c.get("link") or item.get("link")
    prov = c.get("provider")
    publisher = prov.get("displayName") if isinstance(prov, dict) else c.get("publisher")
    canon = c.get("canonicalUrl")
    lang = canon.get("lang") if isinstance(canon, dict) else None
    published = c.get("pubDate") or c.get("displayTime") or item.get("providerPublishTime")
    return {"title": c.get("title"), "url": link, "domain": publisher_domain(link, publisher),
            "published": published, "lang": lang}


async def _fetch_yfinance(row: dict) -> list[dict]:
    await _pace("yfinance")
    raw = await prices._yf(YF_KEY, _yf_news_blocking, row["yahoo"])
    if raw is None:
        raise SourceError("http", "yfinance news unavailable (error or rate limit)")
    return [c for c in map(_parse_yf_item, raw[:PER_CALL_ITEMS]) if c]


def _google_query(row: dict) -> str:
    name = textsafe.clean_text(row.get("name"), 80).replace('"', "") or row["id"]
    pid = row["id"]
    if pid.lower() in name.lower().split() or pid.lower() == name.lower():
        return f'"{name}" when:3d'
    return f'"{name}" {pid} when:3d'


def home_language(row: dict) -> tuple[str, str] | None:
    isin = row.get("isin")
    return HOME_LANG.get(isin[:2]) if isinstance(isin, str) and len(isin) >= 2 else None


def _parse_google(text: str, lang: str) -> list[dict]:
    if len(text) > MAX_XML_CHARS:
        raise SourceError("parse", "feed too large")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise SourceError("parse", "rss xml")
    out = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        src_el = it.find("source")
        publisher = (src_el.text or "").strip() if src_el is not None else ""
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].rstrip()
        out.append({"title": title, "url": (it.findtext("link") or "").strip(),
                    "domain": publisher_domain(src_el.get("url") if src_el is not None else "", publisher),
                    "published": it.findtext("pubDate"), "lang": lang})
        if len(out) >= PER_CALL_ITEMS:
            break
    return out


async def _fetch_google(row: dict, lang: str) -> list[dict]:
    if lang == "en":
        hl, gl = "en-US", "US"
    else:
        home = home_language(row)
        if home is None or home[0] != lang:
            raise SourceSkip("no home language")
        hl, gl = lang, home[1]
    await _pace("google")
    text = await _checked_get(GOOGLE_NEWS, {"q": _google_query(row), "hl": hl, "gl": gl, "ceid": f"{gl}:{lang}"})
    return _parse_google(text, lang)


async def _fetch_gdelt(row: dict) -> list[dict]:
    name = textsafe.clean_text(row.get("name"), 80).replace('"', "")
    if len(name) < 4:
        raise SourceSkip("query too short for GDELT")
    if topnews._gdelt_backoff_until() > time.time():          # topnews' marker is on the wall clock, not our seam
        raise SourceSkip("GDELT backoff marker active")
    await _gdelt_pace()
    params = {"query": f'"{name}"' if " " in name else name, "mode": "ArtList", "maxrecords": PER_CALL_ITEMS,
              "sort": "DateDesc", "format": "json", "timespan": "1d"}
    try:
        text = await _checked_get(topnews.GDELT_DOC, params)
    except SourceError as e:
        if e.kind == "rate_limited":
            topnews._set_gdelt_backoff(topnews.GDELT_BACKOFF_S)      # the shared marker: topnews backs off too
        raise
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError:
        raise SourceError("parse", "gdelt json")
    arts = data.get("articles") if isinstance(data, dict) else None
    if arts is None:
        return []
    if not isinstance(arts, list):
        raise SourceError("parse", "gdelt articles")
    return [{"title": a.get("title"), "url": a.get("url"),
             "domain": (str(a.get("domain") or "").lower().removeprefix("www.") or publisher_domain(a.get("url"), None)),
             "published": a.get("seendate"), "lang": a.get("language")} for a in arts if isinstance(a, dict)]


def _is_us_equity(row: dict) -> bool:
    return row.get("kind") == "equity" and isinstance(row.get("isin"), str) and row["isin"].startswith("US")


async def _fetch_finnhub(row: dict) -> list[dict]:
    if not finnhub.API_KEY:
        raise SourceSkip("no FINNHUB_API_KEY")
    if not _is_us_equity(row):
        raise SourceSkip("not a US name")
    await _pace("finnhub")
    today = datetime.datetime.fromtimestamp(_now(), datetime.timezone.utc).date()
    kind, data = await _finnhub_news(row["id"].replace("-", "."), (today - datetime.timedelta(days=2)).isoformat(),
                                     today.isoformat())
    if kind == finnhub.RATE_LIMITED:
        raise SourceError("rate_limited", "finnhub")
    if kind == finnhub.UNKNOWN_SYMBOL:
        return []                                 # 403 / 404: nothing for this symbol on this plan
    if kind != finnhub.OK:
        raise SourceError("http" if kind == finnhub.NO_DATA else "parse", kind)
    if not isinstance(data, list):
        raise SourceError("parse", "finnhub payload")
    return [{"title": a.get("headline"), "url": a.get("url"), "domain": publisher_domain(a.get("url"), a.get("source")),
             "published": a.get("datetime"), "lang": "en"} for a in data[:PER_CALL_ITEMS] if isinstance(a, dict)]


async def _fetch_topnews(row: dict) -> list[dict]:
    entry = topnews.get_top_news(row["id"])
    items = entry.get("items") if isinstance(entry, dict) else None
    if not isinstance(items, list) or not items:
        raise SourceSkip("no topnews cache entry")
    return [{"title": it.get("headline"), "url": it.get("url"),
             "domain": (str(it.get("domain") or "").lower().removeprefix("www.")
                        or publisher_domain(it.get("url"), it.get("source"))),
             "published": it.get("published_at"), "lang": None} for it in items if isinstance(it, dict)]


def calls_for(row: dict) -> list[tuple[str, object, tuple]]:
    """(source, fetcher, extra args) in the order they run for one registry row."""
    calls: list[tuple[str, object, tuple]] = [("yfinance", _fetch_yfinance, ())] if yahoo_enabled() else []
    if row.get("kind") == "equity":
        calls.append(("google", _fetch_google, ("en",)))
        home = home_language(row)
        if home is not None:
            calls.append(("google", _fetch_google, (home[0],)))
        calls.append(("gdelt", _fetch_gdelt, ()))
        calls.append(("finnhub", _fetch_finnhub, ()))
    calls.append(("topnews", _fetch_topnews, ()))
    return calls


# ------------------------------------------------------------------ the store (append-only monthly JSONL)

def month_path(epoch: float, directory: pathlib.Path | None = None) -> pathlib.Path:
    d = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return (directory or NEWS_DIR) / f"{d.year:04d}-{d.month:02d}.jsonl"


def append_jsonl(path: pathlib.Path, rows: list[dict]) -> bool:
    """Append rows (serialised first, fsynced; a crash mid-line never glues two rows). True on success."""
    if not rows:
        return True
    try:
        payload = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for r in rows)
    except (TypeError, ValueError) as e:
        log.warning("%s: serialise failed: %s", path.name, e)
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lead = ""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    lead = "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lead + payload)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as e:
        log.warning("%s: write failed: %s", path.name, e)
        return False


def read_jsonl(path: pathlib.Path) -> list[dict]:
    """Every parseable object row, oldest first; a missing/unreadable file or a corrupt line is skipped."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("%s unreadable: %s", path.name, e)
    return rows


def _cached_rows(path: pathlib.Path, build):
    """``build(read_jsonl(path))``, cached until the file changes."""
    try:
        st = path.stat()
    except OSError:
        return build([])
    sig = (st.st_size, st.st_mtime_ns)
    hit = _read_cache.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    value = build(read_jsonl(path))
    if len(_read_cache) > 8:
        _read_cache.clear()
    _read_cache[str(path)] = (sig, value)
    return value


def _index_news(rows: list[dict]) -> dict[str, list[dict]]:
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        pid, pub, ing = r.get("id"), _epoch(r.get("publishedAt")), _epoch(r.get("ingestedAt"))
        if not isinstance(pid, str) or not isinstance(r.get("title"), str) or pub is None or ing is None:
            continue
        by_id.setdefault(pid, []).append({"row": r, "pub": pub, "ing": ing})
    return by_id


def month_paths(start: float, end: float, directory: pathlib.Path | None = None) -> list[pathlib.Path]:
    """The monthly store files (UTC months) that can hold rows between two instants, oldest first."""
    a = datetime.datetime.fromtimestamp(start, datetime.timezone.utc)
    b = datetime.datetime.fromtimestamp(end, datetime.timezone.utc)
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append((directory or NEWS_DIR) / f"{y:04d}-{m:02d}.jsonl")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def pit_news(pid: str, cutoff: float, window_s: float = NEWS_WINDOW_S) -> list[dict]:
    """Point-in-time headlines of ``pid``: ``publishedAt`` AND ``ingestedAt`` <= ``cutoff``, published within
    ``window_s`` before it. Newest first. ``{headline, domain, source, published_at, ingested_at}`` (epoch seconds):
    deliberately no URL."""
    out = []
    for path in month_paths(cutoff - window_s, cutoff):
        for e in _cached_rows(path, _index_news).get(pid, []):
            if e["ing"] <= cutoff and e["pub"] <= cutoff and e["pub"] > cutoff - window_s:
                r = e["row"]
                out.append({"headline": r["title"], "domain": r.get("domain") or "unknown", "source": r.get("source"),
                            "published_at": e["pub"], "ingested_at": e["ing"]})
    out.sort(key=lambda x: (-x["published_at"], x["headline"]))
    return out


def recent(pid: str | None, limit: int, now: float | None = None) -> list[dict]:
    """The newest stored rows (this and the previous month), newest ``ingestedAt`` first, optionally one id.
    Rows as stored (the URL included: this feeds the owner's own view, never a model)."""
    now = _now() if now is None else now
    rows = []
    for path in month_paths(now - 31 * 86400, now):
        index = _cached_rows(path, _index_news)
        for key, entries in ([(pid, index.get(pid, []))] if pid else index.items()):
            rows.extend(entries)
    rows.sort(key=lambda e: (-e["ing"], -e["pub"]))
    return [dict(e["row"]) for e in rows[:max(0, limit)]]


def _seen_keys(now: float) -> set:
    if _seen["keys"] is None:
        keys: set = set()
        for path in month_paths(now - SEEN_MONTHS_BACK * 31 * 86400, now):
            for r in read_jsonl(path):
                if isinstance(r.get("id"), str) and isinstance(r.get("titleHash"), str):
                    keys.add((r["id"], r["titleHash"]))
        _seen["keys"] = keys
    return _seen["keys"]


def ingest(pid: str, cands: list[dict], source: str, now: float | None = None) -> list[dict]:
    """Store the candidates that are new for ``pid`` (title-hash dedupe, across sources and months). Returns the rows
    written; nothing is returned (and nothing marked seen) when the write fails, so the next pass retries."""
    now = _now() if now is None else now
    seen = _seen_keys(now)
    fresh: list[dict] = []
    added: list[tuple] = []
    for cand in cands:
        if not isinstance(cand, dict):
            continue
        row = make_row(pid, cand, source, now)
        if row is None or (pid, row["titleHash"]) in seen:
            continue
        seen.add((pid, row["titleHash"]))
        added.append((pid, row["titleHash"]))
        fresh.append(row)
    if fresh and not append_jsonl(month_path(math.ceil(now)), fresh):
        seen.difference_update(added)
        return []
    return fresh


# ------------------------------------------------------------------ earnings observations (PIT)

CARRIED = "carried"                 # observation source: the last known future date, kept while the calendar is empty
CALENDAR_SOURCE = "yfinance.calendar"


def _index_earnings(rows: list[dict]) -> dict[str, list[tuple]]:
    by_id: dict[str, list[tuple]] = {}
    for r in rows:
        pid, fetched = r.get("id"), _epoch(r.get("fetchedAt"))
        day = r.get("date")
        if not isinstance(pid, str) or fetched is None or not (day is None or _to_date(day)):
            continue
        by_id.setdefault(pid, []).append((fetched, _to_date(day) if day else None,
                                          CARRIED if r.get("source") == CARRIED else CALENDAR_SOURCE))
    for obs in by_id.values():
        obs.sort(key=lambda o: o[0])
    return by_id


def next_earnings(pid: str, cutoff: float) -> dict | None:
    """The newest observation fetched at or before ``cutoff``: ``{"date": ISO | None, "fetchedAt": epoch}``, or None
    when none exists yet. ``date`` None = the calendar had no upcoming date then. A date kept alive by the
    carry-forward also has ``"source": "carried"``."""
    obs = [o for o in _cached_rows(EARNINGS_FILE, _index_earnings).get(pid, []) if o[0] <= cutoff]
    if not obs:
        return None
    fetched, day, src = obs[-1]
    out = {"date": day, "fetchedAt": fetched}
    if src == CARRIED:
        out["source"] = CARRIED
    return out


def carried_earnings(pid: str, today_iso: str) -> str | None:
    """The date to carry forward when the calendar has none today: the date of the newest observation that had one,
    while it is today or later (it passes the day after). None when no such observation exists or it has passed."""
    for _fetched, day, _src in reversed(_cached_rows(EARNINGS_FILE, _index_earnings).get(pid, [])):
        if day is not None:
            return day if day >= today_iso else None
    return None


def record_earnings(pid: str, day: str | None, fetched: float, source: str = CALENDAR_SOURCE) -> bool:
    return append_jsonl(EARNINGS_FILE, [{"id": pid, "date": day, "fetchedAt": iso_ceil(fetched),
                                         "source": source}])


# ------------------------------------------------------------------ worker state and schedule

def _load_state() -> dict:
    global _state
    if _state is None:
        doc = jsonstore.load(STATE_FILE, {})
        doc = doc if isinstance(doc, dict) else {}
        _state = {"last": dict(doc.get("last") or {}) if isinstance(doc.get("last"), dict) else {},
                  "earnings": dict(doc.get("earnings") or {}) if isinstance(doc.get("earnings"), dict) else {},
                  "sweep": dict(doc.get("sweep") or {}) if isinstance(doc.get("sweep"), dict) else {}}
    return _state


def _save_state() -> None:
    if _state is not None and not jsonstore.save(STATE_FILE, _state):
        log.warning("universe_news: state not saved")


def reset_state() -> None:
    """Forget every in-memory cache (tests; a restarted worker starts from the files)."""
    global _state, _task, _cards_task
    _state, _task, _cards_task = None, None, None
    _seen["keys"] = None
    _health.clear()
    _last_call.clear()
    _read_cache.clear()
    _stats.update(running=False, enabled=False, last_run=0.0, runs=0, errors=0, lastError=None, ingested=0,
                  lastPass=None)


def in_hot_window(now: float) -> bool:
    return HOT_WINDOW[0] <= datetime.datetime.fromtimestamp(now).hour < HOT_WINDOW[1]


def sweep_start(now: float) -> float | None:
    """Start of the full-sweep window when ``now`` lies in it: the SWEEP_LEAD_S before the close of the XETR session
    that ends this ISO week (Friday, or Thursday before a holiday Friday); else None."""
    day = datetime.datetime.fromtimestamp(now, _BERLIN).date()
    monday = day - datetime.timedelta(days=day.weekday())
    sessions = calendars.sessions_between(MIC, monday, monday + datetime.timedelta(days=6))
    if not sessions or day != max(sessions):
        return None
    bounds = calendars.session_bounds(MIC, day)
    if bounds is None:
        return None
    close = bounds[1].timestamp()
    return close - SWEEP_LEAD_S if close - SWEEP_LEAD_S <= now < close else None


def plan(now: float, ids: list[str], hot: list[str], last: dict) -> list[str]:
    """Ids due now: hot names first (then the most overdue); inside the sweep window every name not yet fetched since
    the window opened is due."""
    hot_set = set(hot)
    window = in_hot_window(now)
    start = sweep_start(now)
    due = []
    for pid in ids:
        seen = float(last.get(pid) or 0.0)
        interval = HOT_INTERVAL_S if (pid in hot_set and window) else COLD_INTERVAL_S
        if now - seen >= interval or (start is not None and seen < start):
            due.append((pid not in hot_set, seen, pid))
    return [p for _, _, p in sorted(due)]


# ------------------------------------------------------------------ one name, one pass

async def fetch_name(row: dict) -> dict:
    """Run every source for one name. A source failing (or skipping) never stops the others. Returns
    ``{"new": [rows], "attempted": n, "ok": n}``."""
    pid = row["id"]
    out = {"new": [], "attempted": 0, "ok": 0}
    for src, fn, args in calls_for(row):
        now = _now()
        if not source_ready(src, now):
            continue
        try:
            cands = await fn(row, *args)
            new = ingest(pid, cands, src, _now())
        except asyncio.CancelledError:
            raise
        except SourceSkip:
            note_skip(src)
            continue
        except SourceError as e:
            out["attempted"] += 1
            note_fail(src, e.kind, e.detail, _now())
            continue
        except Exception as e:
            out["attempted"] += 1
            note_fail(src, "error", type(e).__name__, _now())
            log.warning("universe_news: %s %s failed: %s", src, pid, type(e).__name__)
            continue
        out["attempted"] += 1
        note_ok(src, len(new), _now())
        out["ok"] += 1
        out["new"].extend(new)
    return out


async def _earnings_step(rows: dict[str, dict], now: float, deadline: float) -> int:
    """Daily yfinance-calendar check of up to EARNINGS_PER_PASS equities; one PIT observation per success."""
    st = _load_state()
    due = [r for r in rows.values() if r.get("kind") == "equity"
           and now - float(st["earnings"].get(r["id"]) or 0.0) >= EARNINGS_EVERY_S]
    due.sort(key=lambda r: (float(st["earnings"].get(r["id"]) or 0.0), r["id"]))
    done = 0
    today = datetime.datetime.fromtimestamp(now, _BERLIN).date().isoformat()
    for row in due[:EARNINGS_PER_PASS]:
        if _mono() > deadline or not source_ready("calendar", _now()):
            break
        try:
            await _pace("calendar")
            res = await prices._yf(YF_KEY, _yf_calendar_blocking, row["yahoo"], today)
            if res is None:
                raise SourceError("http", "yfinance calendar unavailable")
            fetched = _now()
            day, src = res[0], CALENDAR_SOURCE
            if day is None:                      # an empty calendar must not erase a known future date
                day = carried_earnings(row["id"], today)
                src = CARRIED if day else CALENDAR_SOURCE
            if not record_earnings(row["id"], day, fetched, src):
                raise SourceError("http", "earnings store not writable")
        except asyncio.CancelledError:
            raise
        except SourceError as e:
            note_fail("calendar", e.kind, e.detail, _now())
            continue
        except Exception as e:
            note_fail("calendar", "error", type(e).__name__, _now())
            continue
        st["earnings"][row["id"]] = fetched
        note_ok("calendar", 1, fetched)
        done += 1
    return done


def _start_cards(new_rows: list[dict]) -> None:
    """Hand fresh items of the focus names to the event-card queue and make sure ONE drain task runs. Own task: a
    hanging or absent lane never delays ingestion."""
    global _cards_task
    from app.api import event_cards
    if not event_cards.enabled():
        return
    if new_rows:
        event_cards.enqueue(new_rows)
    if (_cards_task is None or _cards_task.done()) and event_cards.pending():
        _cards_task = asyncio.create_task(event_cards.drain_safely())


async def _tick() -> tuple[bool, str, bool]:
    """One supervisor pass -> (ok, note, acted). Never raises."""
    now = _now()
    started = _mono()
    notes: list[str] = []
    ok, acted = True, False
    try:
        rows = {r["id"]: r for r in paper_universe.entries()}
        try:
            held, top = focus_ids()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            held, top = [], []
            notes.append(f"focus: {type(e).__name__}")
        hot = list(dict.fromkeys(held + top))
        hot_set = set(hot)
        st = _load_state()
        sweep = sweep_start(now)
        due = plan(now, list(rows), hot, st["last"])
        processed = ok_sources = 0
        focus_new: list[dict] = []
        for pid in due:
            if _mono() - started > PASS_BUDGET_S:
                notes.append("budget reached")
                break
            try:
                res = await fetch_name(rows[pid])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok = False
                notes.append(f"{pid}: {type(e).__name__}")
                log.exception("universe_news name %s failed", pid)
                continue
            if res["attempted"] == 0:
                continue                            # every source of this name is backing off: try again later
            st["last"][pid] = _now()
            processed += 1
            ok_sources += res["ok"]
            if pid in hot_set:
                focus_new.extend(r for r in res["new"] if _now() - (_epoch(r["publishedAt"]) or 0) <= CARD_MAX_AGE_S)
            _stats["ingested"] += len(res["new"])
            acted = acted or bool(res["new"])
        if processed:
            acted = True
            notes.append(f"{processed} names, {_stats['ingested']} rows total")
            if processed >= 3 and ok_sources == 0:
                ok = False
                notes.append("every source failed")
        if sweep is not None and float(st["sweep"].get("doneAt") or 0.0) < sweep \
                and all(float(st["last"].get(p) or 0.0) >= sweep for p in rows):
            st["sweep"] = {"start": sweep, "doneAt": _now()}
            notes.append("full sweep done")
            acted = True
        n = await _earnings_step(rows, now, started + PASS_BUDGET_S + 60)
        if n:
            acted = True
            notes.append(f"earnings {n}")
        _save_state()
        try:
            _start_cards(focus_new)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            notes.append(f"cards: {type(e).__name__}")
            log.exception("universe_news cards hand-off failed")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ok = False
        notes.append(f"pass: {type(e).__name__}")
        _stats["lastError"] = f"{type(e).__name__}: {str(e)[:150]}"
        log.exception("universe_news pass failed")
    if not notes:
        notes.append("idle")
    return ok, "; ".join(notes), acted


async def _pass() -> bool:
    t0 = time.time()
    ok, note, acted = await _tick()
    _stats["last_run"] = time.time()
    _stats["runs"] += 1
    _stats["lastPass"] = {"at": _stats["last_run"], "ok": ok, "note": note}
    if not ok:
        _stats["errors"] += 1
    runlog.record("universe_news", ok, time.time() - t0, note, idle=(ok and not acted))
    return ok


async def _loop() -> None:
    log.info("universe_news loop started (poll %ss)", POLL_S)
    await _pass()
    while True:
        await asyncio.sleep(POLL_S)
        await _pass()


def start() -> None:
    global _task
    _stats["enabled"] = enabled()
    if not _stats["enabled"]:
        log.info("universe_news disabled (UNIVERSE_NEWS_ENABLED / FORECAST_ENABLED unset)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True


async def stop() -> None:
    global _task, _cards_task
    _stats["running"] = False
    for t in (_task, _cards_task):
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    _task = _cards_task = None


async def close() -> None:
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception:
            log.debug("universe_news client close failed", exc_info=True)


def status() -> dict:
    from app.api import event_cards
    st = _load_state()
    return {**_stats, "enabled": enabled(), "interval_s": WATCH_INTERVAL_S, "pollS": POLL_S,
            "sources": health(), "tracked": len(st["last"]), "lastFullSweep": dict(st["sweep"]) or None,
            "eventCards": event_cards.status()}
