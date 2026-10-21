"""Regime feed: keyless ECB + FRED observations kept point in time (docs/research/data-sources.md section 4 item 2),
plus the plumbing the other data-feed modules (``consensus_log``, ``regulators``, ``data_feeds``) share: one HTTP
seam with per-source spacing, the ``FeedError`` envelope, cp1252-safe decoding, a signature-cached JSONL reader, the
as-of parser and the company-name matcher.

Series (every value is stored exactly as published, with its observation date and a source string):

* ECB Data Portal CSV (``format=csvdata``): ``mro`` / ``dfr`` (FM main refinancing and deposit facility rates),
  ``estr`` (EST), ``aaa10y`` (YC AAA 10Y spot), ``eurusd`` (EXR reference rate, USD per EUR). ``dfrLastChange`` is the
  date the deposit rate last changed, derived from the DFR series (the only derived value; its source says so).
* FRED ``fredgraph.csv`` (no key): ``vix`` (VIXCLS, Cboe data reprinted by FRED) and ``us10y`` (DGS10). FRED tarpits
  both a ``Mozilla/...`` UA and our descriptive UA (15 s read timeout, probed 2026-10-21 from the pod) but answers the
  plain library UA in ~50 ms, so FRED requests send httpx's default ``User-Agent`` (``SOURCE_UA``). Crawl-delay 1 s is
  honoured by the spacing table.

Store: ``data/regime/<YYYY-MM>.jsonl`` (UTC month of ``ingestedAt``, append-only) with
``{key, value, date, source, ingestedAt}``. A row is appended when the latest observation CHANGED or when none was
stored yet this UTC month, so a point-in-time lookup never needs more than the last two months.

Reader: :func:`pit` ``(as_of_iso)`` = for every key the newest observation among the rows with ``ingestedAt <= as_of``
(a value first stored after the as-of never leaks into an earlier packet). Each value is ``{value, date, source}`` or
the literal ``"MISSING"``.
"""
import asyncio
import csv
import datetime
import io
import logging
import math
import os
import pathlib
import re
import time
import unicodedata

import httpx

from app.api import universe_news

log = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
REGIME_DIR = DATA_DIR / "regime"
MISSING = "MISSING"
USER_AGENT = "portfolio-dashboard/1.0 (personal research; polite rate limits)"
# Per-source UA override: FRED hangs on descriptive and browser UAs (see module docstring).
SOURCE_UA = {"fred": httpx.Headers({}).get("user-agent") or f"python-httpx/{httpx.__version__}"}
UTC = datetime.timezone.utc

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
# key -> (dataflow, series key); the rate series carry the history needed for the last-change date
ECB_SERIES = {
    "mro": ("FM", "B.U2.EUR.4F.KR.MRR_FR.LEV"),
    "dfr": ("FM", "B.U2.EUR.4F.KR.DFR.LEV"),
    "estr": ("EST", "B.EU000A2X2A25.WT"),
    "aaa10y": ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"),
    "eurusd": ("EXR", "D.USD.EUR.SP00.A"),
}
ECB_KEYS = ("mro", "dfr", "dfrLastChange", "estr", "aaa10y", "eurusd")
FRED_SERIES = {"vix": "VIXCLS", "us10y": "DGS10"}
RATE_KEYS = ("mro", "dfr")
RATE_HISTORY_DAYS = 1100              # about 3 years of FM observations: enough to find the last rate change
RECENT_OBS = 10
FRED_RECENT_DAYS = 45
LOOKBACK_S = 70 * 86400               # a pit lookup reads at most this far back (one row per key per month is kept)

SPACING_S = {"ecb": 1.0, "fred": 1.0, "afm": 2.0, "bdif": 1.2, "openfigi": 2.6, "consensus": 1.0}
MAX_BYTES = 6_000_000
TIMEOUT_S = 25

_client: httpx.AsyncClient | None = None
_last_call: dict[str, float] = {}
_read_cache: dict = {}


class FeedError(Exception):
    """A failed feed call. ``kind``: rate_limited | timeout | network | http | parse."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


# ------------------------------------------------------------------ clocks and the HTTP seam (tests patch these)

def _now() -> float:
    return time.time()


def _mono() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def now() -> float:
    return _now()


def mono() -> float:
    return _mono()


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True)
    return _client


async def _request(method: str, url: str, params: dict | None, headers: dict | None,
                   json_body) -> tuple[int, bytes]:
    """One HTTP call -> (status, body bytes). The only place the data-feed modules touch the network."""
    r = await _client_get().request(method, url, params=params, headers=headers, json=json_body)
    return r.status_code, r.content


async def pace(src: str) -> None:
    """Hold the source's minimum spacing between two requests."""
    gap = SPACING_S.get(src, 0.0)
    last = _last_call.get(src)
    if gap and last is not None:
        wait = gap - (_mono() - last)
        if wait > 0:
            await _sleep(wait)
    _last_call[src] = _mono()


async def fetch_bytes(src: str, url: str, *, params: dict | None = None, headers: dict | None = None,
                      json_body=None, method: str = "GET") -> bytes:
    """Paced request; any answer but 200 raises ``FeedError`` (429 = ``rate_limited``)."""
    await pace(src)
    if src in SOURCE_UA:
        headers = {**(headers or {}), "User-Agent": SOURCE_UA[src]}
    try:
        status, body = await _request(method, url, params, headers, json_body)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        raise FeedError("timeout")
    except Exception as e:
        raise FeedError("network", type(e).__name__)
    if status == 429:
        raise FeedError("rate_limited", "HTTP 429")
    if status != 200:
        raise FeedError("http", f"HTTP {status}")
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BYTES:
        raise FeedError("parse", "empty or oversized body")
    return bytes(body)


async def close() -> None:
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception:
            log.debug("regime_feed client close failed", exc_info=True)


def reset_state() -> None:
    """Forget the in-memory caches (tests; a restarted process starts from the files)."""
    _last_call.clear()
    _read_cache.clear()


# ------------------------------------------------------------------ shared pure helpers

def decode_text(raw) -> str:
    """UTF-8 (BOM allowed) first, cp1252 when the bytes are not valid UTF-8 (the AFM archive export is)."""
    if isinstance(raw, str):
        return raw.lstrip("\ufeff")
    try:
        return bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError:
        return bytes(raw).decode("cp1252", errors="replace")


def as_of_epoch(as_of_iso) -> float:
    """Epoch seconds of an as-of: a datetime ISO (naive = UTC) or a bare date, which means the END of that UTC day
    (everything stored during the day was known on it). Raises ValueError on anything else."""
    if not isinstance(as_of_iso, str) or not as_of_iso.strip():
        raise ValueError("as_of must be an ISO date or datetime string")
    s = as_of_iso.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        d = datetime.date.fromisoformat(s)
        return datetime.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC).timestamp()
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def iso_epoch(value) -> float | None:
    """Epoch of a stored stamp; None when it is not usable."""
    try:
        return as_of_epoch(value)
    except (ValueError, TypeError):
        return None


def iso_ceil(epoch: float) -> str:
    return universe_news.iso_ceil(epoch)


def utc_month(epoch: float) -> str:
    d = datetime.datetime.fromtimestamp(epoch, UTC)
    return f"{d.year:04d}-{d.month:02d}"


def cached_rows(path: pathlib.Path, build):
    """``build(read_jsonl(path))``, cached until the file changes."""
    try:
        st = path.stat()
    except OSError:
        return build([])
    sig = (st.st_size, st.st_mtime_ns)
    hit = _read_cache.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    value = build(universe_news.read_jsonl(path))
    if len(_read_cache) > 16:
        _read_cache.clear()
    _read_cache[str(path)] = (sig, value)
    return value


_LEGAL = {"se", "nv", "ag", "sa", "plc", "inc", "corp", "corporation", "ltd", "limited", "holding", "holdings",
          "group", "spa", "the", "co", "company", "sab", "oyj", "ab", "asa", "as", "kgaa", "reg", "registered",
          "adr", "class", "cl", "ord", "shs", "common", "stock", "de", "cie", "et", "of", "and", "bv", "sas",
          "srl", "ssa", "nom", "vz", "st", "inh", "na", "ucits", "etf"}


def _name_tokens(name) -> set[str]:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return {t for t in re.findall(r"[a-z0-9]+", text) if len(t) >= 2 and t not in _LEGAL}


def names_match(a, b) -> bool:
    """Do two company names plausibly name the same company? Accent-, case- and legal-form-insensitive: true when they
    share a token, or a token of one is a >= 4 character prefix of the other's (feeds truncate names). When either
    side has no usable token there is nothing to compare, which counts as a match (never a false alarm)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return True
    if ta & tb:
        return True
    return any(len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x)) for x in ta for y in tb)


# ------------------------------------------------------------------ CSV parsing

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _finite(text) -> float | None:
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_ecb_csv(raw) -> list[tuple[str, float]]:
    """``[(ISO day, value)]`` oldest first from an ECB ``csvdata`` answer (``TIME_PERIOD`` / ``OBS_VALUE`` columns).
    Rows without a daily date or a number are skipped; a header without the two columns raises ``FeedError('parse')``."""
    reader = csv.DictReader(io.StringIO(decode_text(raw)))
    names = reader.fieldnames or []
    if "TIME_PERIOD" not in names or "OBS_VALUE" not in names:
        raise FeedError("parse", "ECB csv without TIME_PERIOD/OBS_VALUE")
    out: dict[str, float] = {}
    for r in reader:
        day = (r.get("TIME_PERIOD") or "").strip()
        value = _finite(r.get("OBS_VALUE"))
        if _DAY.match(day) and value is not None:
            out[day] = value
    return sorted(out.items())


def parse_fred_csv(raw, series: str) -> list[tuple[str, float]]:
    """``[(ISO day, value)]`` oldest first from ``fredgraph.csv`` (``DATE``/``observation_date`` + the series column).
    ``.`` and empty cells are missing observations and are skipped."""
    reader = csv.reader(io.StringIO(decode_text(raw)))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise FeedError("parse", "FRED csv header")
    first = header[0].strip().lower()
    if first not in ("date", "observation_date"):
        raise FeedError("parse", "FRED csv without a date column")
    col = next((i for i, h in enumerate(header) if h.strip().upper() == series.upper()), 1)
    out: dict[str, float] = {}
    for r in reader:
        if len(r) <= col:
            continue
        day, value = r[0].strip(), _finite(r[col])
        if _DAY.match(day) and value is not None:
            out[day] = value
    return sorted(out.items())


def last_change(obs: list[tuple[str, float]]) -> str | None:
    """The date of the newest observation whose value differs from the one before it; None when it never changed."""
    for i in range(len(obs) - 1, 0, -1):
        if abs(obs[i][1] - obs[i - 1][1]) > 1e-9:
            return obs[i][0]
    return None


# ------------------------------------------------------------------ the point-in-time store

def _index(rows: list[dict]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for r in rows:
        key, day, ing = r.get("key"), r.get("date"), iso_epoch(r.get("ingestedAt"))
        if isinstance(key, str) and isinstance(day, str) and ing is not None and r.get("value") is not None:
            out.setdefault(key, []).append((day, ing, r))
    return out


def _latest(key: str, cutoff: float) -> tuple | None:
    """(date, ingested epoch, row) with the newest observation date (then the newest ingestion) among the rows
    ingested at or before ``cutoff``; looks at the newest month that has one."""
    best = None
    for path in reversed(universe_news.month_paths(cutoff - LOOKBACK_S, cutoff, REGIME_DIR)):
        for day, ing, row in cached_rows(path, _index).get(key, []):
            if ing <= cutoff and (best is None or (day, ing) > (best[0], best[1])):
                best = (day, ing, row)
        if best is not None:
            break
    return best


def _store(key: str, day: str, value, source: str, now_: float) -> str:
    """``written`` | ``same`` | ``failed``. Appends when the newest stored observation differs or none exists this month."""
    cur = _latest(key, now_)
    if cur is not None and cur[2].get("date") == day and cur[2].get("value") == value \
            and utc_month(cur[1]) == utc_month(now_):
        return "same"
    row = {"key": key, "value": value, "date": day, "source": source, "ingestedAt": iso_ceil(now_)}
    ok = universe_news.append_jsonl(universe_news.month_path(math.ceil(now_), REGIME_DIR), [row])
    return "written" if ok else "failed"


def _ecb_source(flow: str, series: str, derived: bool = False) -> str:
    return f"ECB Data Portal {flow}/{series}" + (" (last change derived from the series)" if derived else "")


async def refresh_ecb(now_: float | None = None) -> dict:
    """Fetch every ECB series and store what changed. Never raises: ``{"new": rows written, "failed": {key: why}}``;
    a rate-limit answer stops the pass (the rest would be refused too)."""
    now_ = _now() if now_ is None else now_
    start = (datetime.datetime.fromtimestamp(now_, UTC).date() - datetime.timedelta(days=RATE_HISTORY_DAYS)).isoformat()
    new, failed = 0, {}
    for key, (flow, series) in ECB_SERIES.items():
        params = {"format": "csvdata", "startPeriod": start} if key in RATE_KEYS \
            else {"format": "csvdata", "lastNObservations": RECENT_OBS}
        try:
            obs = parse_ecb_csv(await fetch_bytes("ecb", f"{ECB_BASE}/{flow}/{series}", params=params))
            if not obs:
                raise FeedError("parse", "no observations")
            day, value = obs[-1]
            results = [_store(f"ecb.{key}", day, value, _ecb_source(flow, series), now_)]
            if key == "dfr":
                changed = last_change(obs)
                if changed:
                    results.append(_store("ecb.dfrLastChange", day, changed, _ecb_source(flow, series, True), now_))
        except asyncio.CancelledError:
            raise
        except FeedError as e:
            failed[key] = str(e)
            if e.kind == "rate_limited":
                break
            continue
        new += results.count("written")
        if "failed" in results:
            failed[key] = "write: store not writable"
    return {"new": new, "failed": failed}


async def refresh_fred(now_: float | None = None) -> dict:
    """Same contract as :func:`refresh_ecb` for the keyless FRED CSV series."""
    now_ = _now() if now_ is None else now_
    since = (datetime.datetime.fromtimestamp(now_, UTC).date() - datetime.timedelta(days=FRED_RECENT_DAYS)).isoformat()
    new, failed = 0, {}
    for key, series in FRED_SERIES.items():
        try:
            obs = parse_fred_csv(await fetch_bytes("fred", FRED_URL, params={"id": series, "cosd": since}), series)
            if not obs:
                raise FeedError("parse", "no observations")
            day, value = obs[-1]
            result = _store(f"fred.{key}", day, value, f"FRED {series} (fredgraph.csv)", now_)
        except asyncio.CancelledError:
            raise
        except FeedError as e:
            failed[key] = str(e)
            if e.kind == "rate_limited":
                break
            continue
        new += result == "written"
        if result == "failed":
            failed[key] = "write: store not writable"
    return {"new": new, "failed": failed}


def _pit_value(key: str, cutoff: float):
    hit = _latest(key, cutoff)
    if hit is None:
        return MISSING
    row = hit[2]
    return {"value": row["value"], "date": row["date"], "source": row.get("source")}


def pit(as_of_iso: str) -> dict:
    """Point-in-time regime values: ``{"ecb": {mro, dfr, dfrLastChange, estr, aaa10y, eurusd}, "fred": {vix, us10y}}``;
    each ``{value, date, source}`` from the newest row with ``ingestedAt <= as_of``, else ``"MISSING"``. Raises
    ValueError on an unusable ``as_of_iso``."""
    cutoff = as_of_epoch(as_of_iso)
    return {"ecb": {k: _pit_value(f"ecb.{k}", cutoff) for k in ECB_KEYS},
            "fred": {k: _pit_value(f"fred.{k}", cutoff) for k in FRED_SERIES}}
