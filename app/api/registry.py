"""Symbol registry: the single source of truth for what a watchlist id IS.

Every watchlist entry is a dict (stored under ``watchlist`` in the config):

    {id, symbol, label, kind, role, quoteCurrency,
     providers: {yahoo, twelvedata, finnhub},     # null / missing = unsupported
     listing:   {symbol, venue, currency},        # the series the dashboard shows
     alertable, analyzeable}

* ``kind``   equity | etf | index
* ``role``   holding | benchmark (a benchmark is shown, never alerted on,
             never analysed by the LLM, never part of the portfolio)
* ``providers`` hold the OUTBOUND symbol per provider. ``provider_symbol``
  returns None for an unsupported mapping and callers must SKIP the request
  (no raw-id fallback, no negative-cache entry: nothing was asked).
* ``listing`` is the one venue/currency whose series feeds valuation, charts
  and the stored history. The US live feeds (Twelve Data / Finnhub WS) are a
  separate reference and never enter that series.
* ``alertable`` / ``analyzeable`` derive from ``kind`` unless overridden:
  index -> neither, etf -> alertable only, equity -> both.

The venue table below carries the trading hours, so "is this market open" is
decided by the exchange of the listing that produced the price instead of a
hard-coded ticker set.
"""
import copy
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

PROVIDERS = ("yahoo", "twelvedata", "finnhub")
KINDS = ("equity", "etf", "index")
ROLES = ("holding", "benchmark")
CURRENCIES = ("EUR", "USD")

# MIC -> trading session. Weekends are closed; exchange holidays are not
# modelled (the data layer reports such a day as stale instead).
VENUES: dict[str, dict] = {
    "XAMS": {"tz": "Europe/Amsterdam", "open": (9, 0), "close": (17, 30), "region": "EU"},
    "XPAR": {"tz": "Europe/Paris", "open": (9, 0), "close": (17, 30), "region": "EU"},
    "XBRU": {"tz": "Europe/Brussels", "open": (9, 0), "close": (17, 30), "region": "EU"},
    "XETR": {"tz": "Europe/Berlin", "open": (9, 0), "close": (17, 30), "region": "EU"},
    "XMIL": {"tz": "Europe/Rome", "open": (9, 0), "close": (17, 30), "region": "EU"},
    "XNAS": {"tz": "America/New_York", "open": (9, 30), "close": (16, 0), "region": "US"},
    "XNYS": {"tz": "America/New_York", "open": (9, 30), "close": (16, 0), "region": "US"},
}
DEFAULT_BENCHMARK_VENUE = "XNYS"

_KIND_FLAGS = {  # kind -> (alertable, analyzeable)
    "equity": (True, True),
    "etf": (True, False),
    "index": (False, False),
}


# ------------------------------------------------------------------ normalize

def _clean_str(value, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string or null")
    value = value.strip()
    return value or None


def _norm_provider_map(raw) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("providers must be an object")
    unknown = set(raw) - set(PROVIDERS)
    if unknown:
        raise ValueError("unknown provider key(s): " + ", ".join(sorted(map(str, unknown))))
    return {p: _clean_str(raw.get(p), f"providers.{p}") for p in PROVIDERS}


def _norm_listing(raw, providers: dict, quote_ccy: str, required: bool) -> dict | None:
    if raw is None:
        yahoo = providers.get("yahoo")
        if required or not yahoo:
            if required:
                raise ValueError("holding needs a listing {symbol, venue, currency}")
            return None
        return {"symbol": yahoo, "venue": DEFAULT_BENCHMARK_VENUE, "currency": quote_ccy}
    if not isinstance(raw, dict):
        raise ValueError("listing must be an object")
    symbol = _clean_str(raw.get("symbol"), "listing.symbol") or providers.get("yahoo")
    if not symbol:
        raise ValueError("listing.symbol is required")
    venue = (_clean_str(raw.get("venue"), "listing.venue") or DEFAULT_BENCHMARK_VENUE).upper()
    if venue not in VENUES:
        raise ValueError(f"unknown listing.venue {venue!r} (known: {', '.join(VENUES)})")
    ccy = (_clean_str(raw.get("currency"), "listing.currency") or quote_ccy).upper()
    if ccy not in CURRENCIES:
        raise ValueError(f"listing.currency must be one of {', '.join(CURRENCIES)}")
    out = dict(raw)
    out.update({"symbol": symbol, "venue": venue, "currency": ccy})
    return out


def normalize_entry(raw) -> dict:
    """Validated superset of ``raw`` (unknown keys are preserved).

    Raises ValueError with a readable message on anything invalid.
    """
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        raise ValueError("watchlist entry must be an object or a ticker string")
    eid = _clean_str(raw.get("id") if raw.get("id") is not None else raw.get("symbol"), "id")
    if not eid:
        raise ValueError("watchlist entry needs an id")
    eid = eid.upper()
    if len(eid) > 24 or not all(c.isalnum() or c in ".-_" for c in eid):
        raise ValueError(f"invalid id {eid!r} (letters, digits, . - _ only)")

    kind = (_clean_str(raw.get("kind"), "kind") or "equity").lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    role = _clean_str(raw.get("role"), "role")
    role = role.lower() if role else ("benchmark" if kind == "index" else "holding")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    default_ccy = "USD" if kind == "index" else "EUR"
    quote_ccy = (_clean_str(raw.get("quoteCurrency"), "quoteCurrency") or default_ccy).upper()
    if quote_ccy not in CURRENCIES:
        raise ValueError(f"quoteCurrency must be one of {', '.join(CURRENCIES)}")

    providers = _norm_provider_map(raw.get("providers"))
    listing = _norm_listing(raw.get("listing"), providers, quote_ccy,
                            required=(role == "holding"))
    if role == "holding" and listing and listing["currency"] != quote_ccy:
        raise ValueError(
            f"holding {eid} is quoted in {quote_ccy} but its listing trades in "
            f"{listing['currency']}: the held listing's currency is the quote currency")

    alertable, analyzeable = _KIND_FLAGS[kind]
    flags = {}
    for key, default in (("alertable", alertable), ("analyzeable", analyzeable)):
        val = raw.get(key)
        if val is None:
            flags[key] = default
        elif isinstance(val, bool):
            flags[key] = val
        else:
            raise ValueError(f"{key} must be true or false")

    out = dict(raw)
    out.update({
        "id": eid,
        "symbol": (_clean_str(raw.get("symbol"), "symbol") or eid).upper(),
        "label": _clean_str(raw.get("label"), "label") or eid,
        "kind": kind,
        "role": role,
        "quoteCurrency": quote_ccy,
        "providers": providers,
        "listing": listing,
        **flags,
    })
    return out


def validate_entries(raw_list) -> list[dict]:
    """Normalize a whole watchlist; rejects duplicate ids and a second benchmark."""
    if not isinstance(raw_list, list):
        raise ValueError("watchlist must be a list")
    out, seen = [], set()
    for i, raw in enumerate(raw_list):
        try:
            entry = normalize_entry(raw)
        except ValueError as e:
            raise ValueError(f"watchlist[{i}]: {e}") from None
        if entry["id"] in seen:
            raise ValueError(f"watchlist[{i}]: duplicate id {entry['id']}")
        seen.add(entry["id"])
        out.append(entry)
    return out


# ------------------------------------------------------------------ lookup

_memo_raw: list | None = None
_memo_entries: list[dict] = []
_warned: set[tuple[str, str]] = set()


def _raw_watchlist() -> list:
    """Watchlist as stored in the config (config_store owns reading/caching)."""
    from app.api import config_store
    cfg = config_store.read()
    wl = cfg.get("watchlist") if isinstance(cfg, dict) else None
    return wl if isinstance(wl, list) else []


def _entries_cached() -> list[dict]:
    """Normalized entries, re-derived only when the stored watchlist changed.

    A bad entry is logged once and skipped: one typo in the config must not
    take every price endpoint down.
    """
    global _memo_raw, _memo_entries
    raw = _raw_watchlist()
    if _memo_raw is not None and raw == _memo_raw:
        return _memo_entries
    entries, seen = [], set()
    for i, item in enumerate(raw):
        try:
            entry = normalize_entry(item)
        except ValueError as e:
            key = (str(i), str(e))
            if key not in _warned:
                _warned.add(key)
                log.warning("watchlist[%d] ignored: %s", i, e)
            continue
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        entries.append(entry)
    _memo_raw = copy.deepcopy(raw)
    _memo_entries = entries
    return entries


def _find(ident) -> dict | None:
    key = str(ident or "").strip().upper()
    if not key:
        return None
    for e in _entries_cached():
        if e["id"] == key:
            return e
    return None


def entries() -> list[dict]:
    """Normalized watchlist entries (copies; safe to mutate)."""
    return copy.deepcopy(_entries_cached())


def ids() -> list[str]:
    return [e["id"] for e in _entries_cached()]


def get(ident) -> dict | None:
    e = _find(ident)
    return copy.deepcopy(e) if e else None


def is_watched(ident) -> bool:
    return _find(ident) is not None


def kind(ident) -> str | None:
    e = _find(ident)
    return e["kind"] if e else None


def role(ident) -> str | None:
    e = _find(ident)
    return e["role"] if e else None


def is_benchmark(ident) -> bool:
    return role(ident) == "benchmark"


def benchmark_id() -> str | None:
    """Id of the benchmark entry (the first one when several are configured)."""
    for e in _entries_cached():
        if e["role"] == "benchmark":
            return e["id"]
    return None


def holdings() -> list[str]:
    return [e["id"] for e in _entries_cached() if e["role"] == "holding"]


def is_alertable(ident) -> bool:
    e = _find(ident)
    return bool(e and e["alertable"])


def is_analyzable(ident) -> bool:
    e = _find(ident)
    return bool(e and e["analyzeable"])


def quote_currency(ident) -> str | None:
    e = _find(ident)
    return e["quoteCurrency"] if e else None


def provider_symbol(ident, provider: str) -> str | None:
    """Outbound symbol for ``provider``; None when unsupported (skip the call)."""
    e = _find(ident)
    if not e:
        return None
    return e["providers"].get(provider)


def listing(ident) -> dict | None:
    e = _find(ident)
    return dict(e["listing"]) if e and e["listing"] else None


def listing_symbol(ident) -> str | None:
    lst = listing(ident)
    return lst["symbol"] if lst else None


def listing_currency(ident) -> str | None:
    lst = listing(ident)
    return lst["currency"] if lst else None


def venue(ident) -> dict | None:
    """Venue record {mic, tz, open, close, region} of the entry's listing."""
    lst = listing(ident)
    if not lst:
        return None
    return {"mic": lst["venue"], **VENUES[lst["venue"]]}


def is_eu(ident) -> bool:
    v = venue(ident)
    return bool(v and v["region"] == "EU")


# ------------------------------------------------------------------ sessions

def _session_bounds(v: dict, day) -> tuple[datetime, datetime]:
    tz = ZoneInfo(v["tz"])
    oh, om = v["open"]
    ch, cm = v["close"]
    return (datetime(day.year, day.month, day.day, oh, om, tzinfo=tz),
            datetime(day.year, day.month, day.day, ch, cm, tzinfo=tz))


def market_open(ident, now: float | None = None) -> bool:
    """True while the entry's listing venue is inside its regular session."""
    v = venue(ident)
    if not v:
        return False
    return venue_open(v, now)


def venue_open(v: dict, now: float | None = None) -> bool:
    import time as _t
    ts = _t.time() if now is None else now
    local = datetime.fromtimestamp(ts, ZoneInfo(v["tz"]))
    if local.weekday() >= 5:
        return False
    start, end = _session_bounds(v, local.date())
    return start <= local <= end


def session_window(ident, now: float | None = None) -> tuple[int, int] | None:
    """(open_ts, close_ts) of the latest session that has started.

    Mid-session or after the close that is today's session; before the open,
    or on a weekend, it is the previous weekday's. None without a listing.
    """
    import time as _t
    v = venue(ident)
    if not v:
        return None
    ts = _t.time() if now is None else now
    local = datetime.fromtimestamp(ts, ZoneInfo(v["tz"]))
    day = local.date()
    start, _ = _session_bounds(v, day)
    if day.weekday() >= 5 or local < start:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    start, end = _session_bounds(v, day)
    return int(start.timestamp()), int(end.timestamp())
