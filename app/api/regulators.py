"""Regulator registers as typed event rows (docs/research/data-sources.md section 3.6 and 4 item 6).

Sources (keyless, polite, partial coverage by design):

* **AFM net short positions** (current export CSV: holder, issuer, ISIN, net short %, position date). Matched to the
  registry by ISIN. The export is ``;``-delimited UTF-8 with a BOM; the archive export is cp1252, so decoding tries
  UTF-8 and falls back to cp1252. Positions of 0.5 % or more only; a position that drops out of the list is not
  tracked. [UNVERIFIED: the header wording was not captured in the probe; columns are found by keyword and fall back
  to the documented order when the header has exactly five columns; an unrecognisable header is a parse failure.]
* **AMF BDIF** directors' dealings (``DD``) and net-short notices (``VAD``) for French registry names (ISIN ``FR``):
  one search by legal name per name and type, metadata only (the amounts live in a PDF). [UNVERIFIED: the JSON field
  names of the API were not captured in the probe: items are read key-tolerantly (see ``_pick``), an item with no
  usable date is dropped, and an item that names a different company than the search is dropped.]
  BaFin and the AFM MAR export are NOT used (robots / no amounts); no Stooq, Euronext or Boerse Frankfurt either.

Rows go to the universe news store (``universe_news.NEWS_DIR / <YYYY-MM>.jsonl``) with ``source: "regulator"`` and a
typed ``kind`` (``short_position`` | ``directors_dealing``): ``{id, kind, source, regulator, eventDate, ingestedAt,
eventHash, ...}`` plus ``holder`` / ``netShortPct`` (AFM) or ``docId`` / ``url`` (AMF; the URL stays in the store).
The rows deliberately carry NO ``title``: ``universe_news.pit_news`` / ``recent`` skip title-less rows, so these never
show up as headlines. The reader is :func:`pit_events`, which has no URL and no holder text.

``ingestedAt`` is when this process stored the row (never backdated), so the first run stores every standing position
with today's stamp and a PIT lookup before that does not see them. Dedupe is by ``eventHash`` in
``regulators_state.json`` (next to the store); readers also collapse duplicates by hash, so a lost state file only
costs extra rows.
"""
import asyncio
import csv
import datetime
import hashlib
import json
import logging
import math
import re

from app.api import jsonstore, paper_universe, textsafe, universe_news
from app.api.regime_feed import (FeedError, UTC, as_of_epoch, cached_rows, decode_text, fetch_bytes, iso_ceil,
                                 iso_epoch, mono, names_match, now as _clock_now)

log = logging.getLogger(__name__)

KINDS = ("short_position", "directors_dealing")
AFM_URL = "https://www.afm.nl/export.aspx"
AFM_PARAMS = {"type": "8a46a4ef-f196-4467-a7ab-1ae1cb58f0e7", "format": "csv"}
BDIF_URL = "https://bdif.amf-france.org/back/api/v1/informations"
BDIF_HOST = "https://bdif.amf-france.org"
BDIF_TYPES = (("DD", "directors_dealing"), ("VAD", "short_position"))
BDIF_FIRST_DAYS = 30
BDIF_OVERLAP_DAYS = 3
BDIF_SIZE = 50
MAX_SEEN = 20000
KEEP_SEEN = 15000

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_state: dict | None = None


# ------------------------------------------------------------------ state (dedupe hashes)

def _state_path():
    return universe_news.NEWS_DIR / "regulators_state.json"


def _load_state() -> dict:
    global _state
    if _state is None:
        doc = jsonstore.load(_state_path(), {})
        doc = doc if isinstance(doc, dict) else {}
        seen = doc.get("seen") if isinstance(doc.get("seen"), dict) else {}
        last = doc.get("bdifLastOk")
        _state = {"seen": dict(seen),
                  "bdifLastOk": float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else None}
    return _state


def _save_state() -> bool:
    st = _load_state()
    if len(st["seen"]) > MAX_SEEN:
        newest = sorted(st["seen"].items(), key=lambda kv: str(kv[1]), reverse=True)[:KEEP_SEEN]
        st["seen"] = dict(newest)
    return jsonstore.save(_state_path(), st)


def reset_state() -> None:
    global _state
    _state = None


# ------------------------------------------------------------------ parsing

def _day(value) -> str | None:
    """ISO day of an ISO / ``dd-mm-yyyy`` / ``dd/mm/yyyy`` string or an epoch (s or ms); None when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value) / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.datetime.fromtimestamp(v, UTC).date().isoformat() if v > 0 else None
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
        if not m:
            return None
        d, mo, y = m.groups()
    try:
        return datetime.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _pct(text) -> float | None:
    s = str(text or "").strip().replace("%", "").replace(" ", "").replace("\u00a0", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")          # 1.234,56
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) and 0 < v <= 100 else None


_AFM_NEEDLES = (("isin", ("isin",)), ("date", ("datum", "date")), ("holder", ("houder", "holder")),
                ("issuer", ("uitgevende", "issuer", "emittent")), ("pct", ("netto", "net ", "short", "%")))
_AFM_ORDER = ("holder", "issuer", "isin", "pct", "date")


def _afm_columns(header: list[str]) -> dict[str, int]:
    low = [h.strip().casefold() + " " for h in header]
    cols: dict[str, int] = {}
    for name, needles in _AFM_NEEDLES:
        for i, h in enumerate(low):
            if i not in cols.values() and any(n in h for n in needles):
                cols[name] = i
                break
    if len(cols) == 5:
        return cols
    if len(header) == 5:
        return {name: i for i, name in enumerate(_AFM_ORDER)}
    raise FeedError("parse", "unrecognised AFM header")


def parse_afm_csv(raw) -> list[dict]:
    """``[{holder, issuer, isin, netShortPct, date}]`` from the AFM export (bytes or text). Rows with an invalid ISIN,
    percentage or date are skipped; a file whose every row is invalid is a ``FeedError('parse')`` (a shifted layout)."""
    text = decode_text(raw)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise FeedError("parse", "empty AFM export")
    delim = ";" if lines[0].count(";") >= lines[0].count(",") else ","
    table = list(csv.reader(lines, delimiter=delim))
    cols = _afm_columns(table[0])
    out, bad = [], 0
    for r in table[1:]:
        try:
            isin = r[cols["isin"]].strip().upper()
            pct, day = _pct(r[cols["pct"]]), _day(r[cols["date"]])
            holder, issuer = r[cols["holder"]], r[cols["issuer"]]
        except IndexError:
            bad += 1
            continue
        if not _ISIN.match(isin) or pct is None or day is None:
            bad += 1
            continue
        out.append({"holder": textsafe.clean_text(holder, 80), "issuer": textsafe.clean_text(issuer, 80),
                    "isin": isin, "netShortPct": pct, "date": day})
    if len(table) > 1 and not out:
        raise FeedError("parse", "AFM export: no valid row")
    return out


_ID_KEYS = ("numero", "id", "identifiant", "uuid", "reference", "numeroDocument", "code")
_DATE_KEYS = ("datePublication", "dateInformation", "dateDepot", "dateMiseEnLigne", "publicationDate", "date")
_COMPANY_KEYS = ("emetteur", "societe", "raisonSociale", "nomSociete", "nom", "issuer")
_PATH_KEYS = ("urlDocument", "cheminDocument", "pdf", "chemin", "path", "lien", "url")
_LIST_KEYS = ("result", "results", "items", "data", "informations", "content", "hits")


def _pick(item: dict, keys: tuple):
    for k in keys:
        if item.get(k) not in (None, "", [], {}):
            return item[k]
    return None


def _company(item: dict) -> str:
    v = _pick(item, _COMPANY_KEYS)
    if v is None:
        socs = item.get("societes")
        v = socs[0] if isinstance(socs, list) and socs else None
    if isinstance(v, dict):
        v = _pick(v, ("raisonSociale", "nom", "name", "libelle"))
    return textsafe.clean_text(v, 80) if isinstance(v, str) else ""


def _items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in _LIST_KEYS:
            if isinstance(payload.get(k), list):
                return [x for x in payload[k] if isinstance(x, dict)]
        for v in payload.values():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
    return []


def parse_bdif(payload, name: str) -> list[dict]:
    """``[{docId, date, url}]`` of a BDIF ``informations`` answer for the registry name ``name``. Items without a usable
    date are dropped, as are items that name a different company than ``name``."""
    out = []
    for it in _items(payload):
        day = _day(_pick(it, _DATE_KEYS))
        company = _company(it)
        if day is None or (company and not names_match(name, company)):
            continue
        doc = _pick(it, _ID_KEYS)
        doc_id = str(doc) if isinstance(doc, (str, int)) and not isinstance(doc, bool) else \
            hashlib.sha256(json.dumps(it, sort_keys=True, default=str).encode()).hexdigest()[:16]
        path = _pick(it, _PATH_KEYS)
        url = ""
        if isinstance(path, str):
            url = BDIF_HOST + path if path.startswith("/") else path if path.startswith("https://") else ""
        out.append({"docId": textsafe.clean_text(doc_id, 60), "date": day,
                    "url": textsafe.clean_text(url, 500, strip_urls=False)})
    return out


# ------------------------------------------------------------------ rows and ingestion

def _event_hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _row(pid: str, kind: str, regulator: str, day: str, ingested: float, ehash: str, **extra) -> dict:
    return {"id": pid, "kind": kind, "source": "regulator", "regulator": regulator, "eventDate": day,
            "ingestedAt": iso_ceil(ingested), "eventHash": ehash, **extra}


def _ingest(rows: list[dict], now: float) -> int | None:
    """Append the rows not seen before; the new count, or None when the store was not writable (nothing is marked seen,
    the next pass retries)."""
    st = _load_state()
    fresh, batch = [], set()
    for r in rows:
        if r["eventHash"] not in st["seen"] and r["eventHash"] not in batch:
            batch.add(r["eventHash"])
            fresh.append(r)
    if not fresh:
        return 0
    if not universe_news.append_jsonl(universe_news.month_path(math.ceil(now), None), fresh):
        return None
    stamp = iso_ceil(now)
    for r in fresh:
        st["seen"][r["eventHash"]] = stamp
    if not _save_state():
        log.warning("regulators: state not saved (duplicates are collapsed by the readers)")
    return len(fresh)


def _isin_index() -> dict[str, str]:
    return {r["isin"]: r["id"] for r in paper_universe.entries() if r.get("isin")}


async def refresh_afm(now: float | None = None) -> dict:
    """Fetch the AFM export, store the new positions of registry names. Never raises:
    ``{"new", "parsed", "matched", "failed": {"afm": why}, "complete": bool}``."""
    now = _clock_now() if now is None else now
    try:
        parsed = parse_afm_csv(await fetch_bytes("afm", AFM_URL, params=AFM_PARAMS))
    except asyncio.CancelledError:
        raise
    except FeedError as e:
        return {"new": 0, "parsed": 0, "matched": 0, "failed": {"afm": str(e)}, "complete": False}
    by_isin = _isin_index()
    rows = [_row(by_isin[p["isin"]], "short_position", "AFM", p["date"], now,
                 _event_hash("afm", p["isin"], p["holder"].casefold(), p["date"], f"{p['netShortPct']:.3f}"),
                 holder=p["holder"], netShortPct=p["netShortPct"]) for p in parsed if p["isin"] in by_isin]
    new = _ingest(rows, now)
    if new is None:
        return {"new": 0, "parsed": len(parsed), "matched": len(rows), "failed": {"afm": "write: store not writable"},
                "complete": False}
    return {"new": new, "parsed": len(parsed), "matched": len(rows), "failed": {}, "complete": True}


async def refresh_bdif(now: float | None = None, deadline: float = math.inf) -> dict:
    """One DD and one VAD search per French registry name since the last full pass (minus an overlap). Never raises:
    ``{"new", "failed": {"<id>/<type>": why}, "complete"}``. ``complete`` is False when the pass stopped early (rate
    limit, deadline) or every call failed, so the caller retries; individual failed names wait for the next day."""
    now = _clock_now() if now is None else now
    st = _load_state()
    last = st["bdifLastOk"]
    since = datetime.datetime.fromtimestamp(
        (last - BDIF_OVERLAP_DAYS * 86400) if last else now - BDIF_FIRST_DAYS * 86400, UTC).date().isoformat()
    names = [r for r in paper_universe.entries()
             if r["kind"] == "equity" and isinstance(r.get("isin"), str) and r["isin"].startswith("FR")]
    new, calls, failed, aborted = 0, 0, {}, False
    for r in names:
        for code, kind in BDIF_TYPES:
            if mono() > deadline:
                aborted = True
                break
            params = {"TypesInformation": code, "RechercheTexte": textsafe.clean_text(r.get("name"), 80),
                      "DateDebut": since, "From": 0, "Size": BDIF_SIZE}
            try:
                payload = json.loads(decode_text(await fetch_bytes("bdif", BDIF_URL, params=params)))
            except asyncio.CancelledError:
                raise
            except FeedError as e:
                failed[f"{r['id']}/{code}"] = str(e)
                if e.kind == "rate_limited":
                    aborted = True
                    break
                continue
            except ValueError:
                failed[f"{r['id']}/{code}"] = "parse: not JSON"
                continue
            calls += 1
            rows = [_row(r["id"], kind, "AMF", ev["date"], now, _event_hash("amf", kind, r["id"], ev["docId"]),
                         docId=ev["docId"], url=ev["url"]) for ev in parse_bdif(payload, r.get("name") or r["id"])]
            got = _ingest(rows, now)
            if got is None:
                failed[f"{r['id']}/{code}"] = "write: store not writable"
            else:
                new += got
        if aborted:
            break
    complete = not aborted and (calls > 0 or not names)
    if complete:
        st["bdifLastOk"] = now
        _save_state()
    return {"new": new, "failed": failed, "complete": complete}


# ------------------------------------------------------------------ point-in-time reader

def _index(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("source") == "regulator" and r.get("kind") in KINDS and isinstance(r.get("id"), str) \
                and isinstance(r.get("eventHash"), str) and _day(r.get("eventDate")) \
                and iso_epoch(r.get("ingestedAt")) is not None:
            out.setdefault(r["id"], []).append(r)
    return out


def pit_events(pid: str, as_of_iso: str, days: int) -> list[dict]:
    """Regulator events of ``pid`` known at the as-of: ``ingestedAt <= as_of`` AND ``eventDate`` within the ``days``
    before the as-of day (inclusive). Newest event first, duplicates collapsed by hash (earliest ingestion wins).
    ``{kind, regulator, event_date, ingested_at (epoch), net_short_pct}``; deliberately no URL, holder or free text."""
    cutoff = as_of_epoch(as_of_iso)
    end_day = datetime.datetime.fromtimestamp(cutoff, UTC).date()
    start_day = (end_day - datetime.timedelta(days=max(0, int(days)))).isoformat()
    best: dict[str, tuple] = {}
    for path in universe_news.month_paths(cutoff - max(0, int(days)) * 86400, cutoff):
        for r in cached_rows(path, _index).get(pid, []):
            ing, day = iso_epoch(r["ingestedAt"]), _day(r["eventDate"])
            if ing is None or day is None or ing > cutoff or day > end_day.isoformat() or day < start_day:
                continue
            if r["eventHash"] not in best or ing < best[r["eventHash"]][0]:
                best[r["eventHash"]] = (ing, day, r)
    out = [{"kind": r["kind"], "regulator": r.get("regulator"), "event_date": day, "ingested_at": ing,
            "net_short_pct": r.get("netShortPct") if r["kind"] == "short_position" else None}
           for ing, day, r in best.values()]
    out.sort(key=lambda e: (e["event_date"], e["ingested_at"]), reverse=True)
    return out
