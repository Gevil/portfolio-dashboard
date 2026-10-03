"""One dedupe store for everything the news path can alert on.

Replaces three overlapping stores (``news_seen.json`` URL hashes,
``news_title_hashes.json`` title hashes, ``triage_seen.json`` candidate ids):
the same story arrives through several URLs and through two producers
(``news_alerts`` keyword hits and ``triage`` candidates), so the identity that
works for all of them is the NORMALISED TITLE, kept per ticker in
``data/news_dedupe.json`` as ``{ticker: {key: first_seen_ts}}``.

A key is ``news:<title-hash>`` for headlines (:func:`news_key`) or any other
stable id a producer owns (``filing:...``, ``short-ratio:...``). Callers mark a
key only when the alert was actually shown / decided - a key that was merely
fetched stays unmarked so it can still alert on a later pass.
"""
import hashlib
import os
import pathlib
import re
import time

from app.api import jsonstore

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
FILE = DATA_DIR / "news_dedupe.json"
MAX_AGE_S = 14 * 86400          # nothing older is ever consulted
MAX_PER_TICKER = 2000

# Words that carry no ticker signal: two syndicated copies of one headline
# differ mostly in these.
STOPWORDS = {"the", "a", "an", "of", "to", "in", "for", "on"}


def news_key(title: str | None) -> str | None:
    """Dedupe key for a headline: lower-case, alphanumerics only, stopwords
    dropped, remaining tokens sorted (a re-cut word order hashes the same).
    None when nothing is left to hash."""
    words = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split()
    words = sorted(w for w in words if w not in STOPWORDS)
    if not words:
        return None
    return "news:" + hashlib.sha1(" ".join(words).encode()).hexdigest()[:16]


def _load() -> dict:
    data = jsonstore.load(FILE, {})
    return data if isinstance(data, dict) else {}


def seen(ticker: str, key: str | None, ttl_s: float) -> bool:
    """True when ``key`` was marked for ``ticker`` within ``ttl_s``."""
    if not key:
        return False
    ts = (_load().get(ticker.upper()) or {}).get(key)
    return isinstance(ts, (int, float)) and time.time() - ts < ttl_s


def mark(ticker: str, keys) -> bool:
    """Mark keys seen now. False when the write failed."""
    keys = [k for k in keys if k]
    if not keys:
        return True
    store = _load()
    now = time.time()
    sym = ticker.upper()
    rows = {k: v for k, v in (store.get(sym) or {}).items()
            if isinstance(v, (int, float)) and now - v < MAX_AGE_S}
    rows.update({k: now for k in keys})
    if len(rows) > MAX_PER_TICKER:
        rows = dict(sorted(rows.items(), key=lambda kv: kv[1])[-MAX_PER_TICKER:])
    store[sym] = rows
    return jsonstore.save(FILE, store)


def unmark(ticker: str, keys) -> None:
    store = _load()
    rows = store.get(ticker.upper())
    if not isinstance(rows, dict):
        return
    for k in keys:
        rows.pop(k, None)
    jsonstore.save(FILE, store)
