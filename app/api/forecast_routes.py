"""HTTP surface of the forecaster experiment (phase 6 contract section 3). main.py includes ``router``.

* ``GET  /api/paper/forecasts?book=practice&cohort=&id=``  logged forecasts (aggregate rows; ``samples=1`` adds
  the per-sample rows). No filter = the newest cohort. Source and role are on every row: they are never pooled.
  Rows also carry ``name`` / ``sector`` (paper_universe, None otherwise) and ``held`` (the ``bucket`` query
  parameter, default daily, holds the id net > 0).
* ``GET  /api/paper/forecasts/packet?book=practice&cohort=&id=``  the frozen packet of one (cohort, id);
  names are strict ``[A-Za-z0-9._-]+``, the path must stay inside ``data/forecast_packets``; 404 when absent.
* ``GET  /api/paper/arms?book=practice``                    grading per arm x source x horizon, the 4
  pre-registered comparisons, experiment status, readout schedule and the worker status.
* ``POST /api/paper/forecast-run {book, kind}``              manual trigger (``cohort`` | ``daily``); starts the
  run in the background and answers 202; 409 when it cannot start; 503 when the forecaster is disabled.

Every route needs ``book: "practice"`` (anything else is a 400), like the rest of ``/api/paper/*``. Mutating
requests must declare ``application/json`` (main.py's paper-route guard covers this prefix).
"""
import os
import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api import forecast_packets, forecast_worker, forecasts, paper_buckets, paper_ledger, paper_universe

router = APIRouter()

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")

MAX_ITEMS = 2000
COHORT_LIST = 20
KINDS = ("cohort", "daily")


def _held_ids(bucket: str) -> set[str]:
    """Ids the bucket holds net > 0 (its own ledger fills). A failing store holds nothing."""
    try:
        rows = paper_ledger.bucket_rows(paper_ledger.read_orders(), bucket)
        return {pid for pid, h in paper_ledger.ledger_holdings(rows).items()
                if isinstance(h, dict) and (h.get("quantity") or 0) > 0}
    except Exception:
        return set()


def _decorate(items: list[dict], held: set[str]) -> list[dict]:
    """Rows plus registry ``name`` / ``sector`` (None when the id is not a registry entry) and ``held``."""
    meta: dict[str, dict | None] = {}
    out = []
    for r in items:
        pid = r.get("id")
        if pid not in meta:
            meta[pid] = paper_universe.get(pid) if isinstance(pid, str) else None
        row = meta[pid]
        out.append({**r, "name": row.get("name") if row else None,
                    "sector": row.get("sector") if row else None, "held": pid in held})
    return out


def _book_error(book):
    if book == "practice":
        return None
    return JSONResponse({"error": "book must be 'practice'"}, status_code=400)


def _cohort_rows(rows: list[dict], records: dict[str, dict]) -> list[dict]:
    seen = {r["cohort"]: r.get("asOf") for r in rows if r.get("cohort")}
    out = []
    for cid in set(seen) | set(records):
        rec = records.get(cid, {})
        role = forecasts.cohort_role(cid)
        out.append({"id": cid, "kind": rec.get("kind") or {"scheduled": "weekly"}.get(role, role),
                    "asOf": rec.get("asOf") or seen.get(cid),
                    "status": rec.get("status"), "deadline": rec.get("deadline"), "ids": len(rec.get("ids") or []),
                    "arms": rec.get("arms")})
    out.sort(key=lambda c: (c["asOf"] or "", c["id"]), reverse=True)
    return out[:COHORT_LIST]


@router.get("/api/paper/forecasts")
async def get_forecasts(book: str = Query(""), cohort: str = Query(""), id: str = Query(""),
                        limit: int = Query(500), samples: bool = Query(False),
                        bucket: str = Query("")):
    bad = _book_error(book)
    if bad:
        return bad
    try:
        bucket = paper_buckets.validate(bucket)
    except paper_buckets.BadBucket as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    rows = forecasts.read_log()
    records = forecasts.read_cohorts()
    listing = _cohort_rows(rows, records)
    chosen = cohort.strip()
    pid = id.strip().upper()
    if chosen and chosen not in {c["id"] for c in listing} and chosen not in records:
        return JSONResponse({"error": "unknown cohort"}, status_code=404)
    if not chosen and not pid and listing:
        chosen = listing[0]["id"]
    kinds = ("aggregate", "sample") if samples else ("aggregate",)
    items = [r for r in rows if r.get("kind") in kinds and (not chosen or r.get("cohort") == chosen)
             and (not pid or r.get("id") == pid)]
    items = items[-max(1, min(int(limit), MAX_ITEMS)):][::-1]
    return JSONResponse({"book": "practice", "cohort": chosen or None, "id": pid or None,
                         "items": _decorate(items, _held_ids(bucket)),
                         "cohorts": listing, "arms": list(forecasts.ARMS), "subset": forecasts.stored_subset()})


@router.get("/api/paper/forecasts/packet")
async def get_forecast_packet(book: str = Query(""), cohort: str = Query(""), id: str = Query("")):
    """The frozen packet of one (cohort, id). Both names are strict ``[A-Za-z0-9._-]+`` (not all dots) and
    the resolved path must stay inside ``data/forecast_packets``."""
    bad = _book_error(book)
    if bad:
        return bad
    for name in (cohort, id):
        if not _SAFE_ID.fullmatch(name) or not name.strip("."):
            return JSONResponse({"error": "invalid cohort or id"}, status_code=400)
    pid = id.upper()
    base = os.path.abspath(forecast_packets.PACKET_DIR)
    target = os.path.abspath(os.path.join(base, cohort, f"{pid}.json.gz"))
    if os.path.commonpath([base, target]) != base or os.path.dirname(os.path.dirname(target)) != base:
        return JSONResponse({"error": "invalid cohort or id"}, status_code=400)
    try:
        doc = forecast_packets.load(cohort, pid)
    except forecast_packets.PacketError:
        return JSONResponse({"error": "packet unreadable"}, status_code=500)
    if doc is None:
        return JSONResponse({"error": "packet not found"}, status_code=404)
    return JSONResponse({"book": "practice", "cohort": cohort, "id": pid, "sha256": doc.get("sha256"),
                         "packet": doc.get("packet")})


@router.get("/api/paper/arms")
async def get_arms(book: str = Query("")):
    bad = _book_error(book)
    if bad:
        return bad
    return JSONResponse({**forecasts.arms_payload(), "worker": forecast_worker.status()})


@router.post("/api/paper/forecast-run")
async def post_forecast_run(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    bad = _book_error(body.get("book"))
    if bad:
        return bad
    kind = body.get("kind")
    if kind not in KINDS:
        return JSONResponse({"error": "kind must be 'cohort' or 'daily'"}, status_code=400)
    try:
        info = await forecast_worker.request_run(kind)
    except forecast_worker.RunRefused as e:
        if e.code == "disabled":
            return JSONResponse({"error": "forecast_disabled", "detail": str(e)}, status_code=503)
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse(info, status_code=202)
