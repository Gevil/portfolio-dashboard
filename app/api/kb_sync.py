"""Knowledge-base sync (absorbs the standalone knowledge-sync container).

Watches the shared TradingAgents results dir for new
``full_states_log_*.json`` reports, converts each to markdown, uploads it to
Open WebUI, waits for embedding, and adds it to the ``KB_NAME`` knowledge
base. Enabled only when ``KB_SYNC_ENABLED=1`` (set at the cutover that
retires the old container).

Seen-file compatibility: the old container keyed entries as
``/results/<rel-path>:<sha256>``. We mount the same host directory at
``/app/results`` but keep the ``/results/`` key prefix so cutover does not
re-upload every historical report.
"""
import asyncio
import hashlib
import json
import logging
import os
import pathlib
import time

import httpx

from app.api import runlog

log = logging.getLogger("kb_sync")

RESULTS_DIR = pathlib.Path(os.getenv("RESULTS_DIR", "/app/results"))
OPENWEBUI_URL = os.getenv("OPEN_WEBUI_URL", "http://open-webui:8080").rstrip("/")
# The variable the quadlet/env.secrets define is OPEN_WEBUI_API_KEY. Reading
# the old spelling here silently produced an empty bearer token, so every
# upload was rejected while the worker still reported itself healthy.
API_KEY = os.getenv("OPEN_WEBUI_API_KEY", "")
KB_NAME = os.getenv("KB_NAME", "Stock")
POLL_INTERVAL = int(os.getenv("KB_POLL_INTERVAL", "30"))

SEEN_FILE = RESULTS_DIR / ".seen_files.txt"
# Key prefix used by the retired knowledge-sync container (see module doc).
_KEY_PREFIX = "/results"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
}

_client: httpx.AsyncClient | None = None
_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "synced": 0, "errors": 0, "running": False}


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60)
    return _client


async def _request(method: str, url: str, *, retries: int = 2, **kw
                   ) -> httpx.Response:
    """GET/POST with the old container's 5xx-retry behaviour."""
    last: Exception | httpx.Response | None = None
    client = _client_get()
    for attempt in range(1 + retries):
        try:
            r = await client.request(method, url, headers=HEADERS, **kw)
            if r.status_code in (502, 503, 504):
                last = r
                await asyncio.sleep(3 * attempt)
                continue
            return r
        except httpx.HTTPError as e:
            last = e
            await asyncio.sleep(3 * attempt)
    if isinstance(last, httpx.Response):
        return last
    raise last or RuntimeError(f"{method} {url} failed")


def convert_to_markdown(json_path: pathlib.Path) -> str:
    """TradingAgents full_states_log JSON -> KB-friendly markdown."""
    data = json.loads(json_path.read_text())
    state = data[-1] if isinstance(data, list) else data
    ticker = state.get("company_of_interest",
                       json_path.parts[-3] if len(json_path.parts) >= 3
                       else "UNKNOWN")
    date = state.get("trade_date", str(time.strftime("%Y-%m-%d")))
    decision = state.get("final_trade_decision", "N/A")

    lines = [
        f"# TradingAgents Analysis: {ticker}",
        f"**Date:** {date}  ",
        f"**Final Decision:** {decision}",
        "",
    ]
    for key, label in [
        ("market_report",          "📊 Technical Analysis"),
        ("sentiment_report",       "💬 Sentiment Report"),
        ("news_report",            "📰 News Report"),
        ("fundamentals_report",    "📈 Fundamentals Report"),
        ("bull_researcher_report", "🐂 Bull Case"),
        ("bear_researcher_report", "🐻 Bear Case"),
        ("trader_investment_plan", "🧠 Trader Plan"),
        ("risk_assessment",        "⚠️ Risk Assessment"),
        ("final_trade_decision",   "✅ Final Decision"),
    ]:
        val = state.get(key)
        if val:
            lines += [f"## {label}", str(val), ""]
    return "\n".join(lines)


async def get_or_create_knowledge_base() -> str:
    r = await _request("GET", f"{OPENWEBUI_URL}/api/v1/knowledge/")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        kbs = data
    elif isinstance(data, dict):
        kbs = data.get("items") or []
    else:
        kbs = []
    for kb in kbs:
        if isinstance(kb, dict) and kb.get("name") == KB_NAME:
            return kb["id"]
    r = await _request(
        "POST", f"{OPENWEBUI_URL}/api/v1/knowledge/create",
        json={"name": KB_NAME,
              "description": "Auto-synced TradingAgents stock analysis reports"},
    )
    r.raise_for_status()
    out = r.json()
    kb_id = out.get("id") if isinstance(out, dict) else None
    if not kb_id:
        raise RuntimeError(f"KB create returned unexpected response: {out}")
    log.info("Created knowledge base %s (%s)", KB_NAME, kb_id)
    return kb_id


async def _upload(md_path: pathlib.Path) -> str:
    """Multipart upload (Accept header must not claim JSON for multipart)."""
    hdrs = {k: v for k, v in HEADERS.items() if k != "Accept"}
    last: Exception | None = None
    client = _client_get()
    for attempt in range(3):
        try:
            with open(md_path, "rb") as fh:
                r = await client.post(
                    f"{OPENWEBUI_URL}/api/v1/files/", headers=hdrs,
                    files={"file": (md_path.name, fh, "text/markdown")},
                    timeout=60,
                )
            if r.status_code in (502, 503, 504):
                await asyncio.sleep(3 * attempt)
                continue
            if r.status_code >= 400:      # httpx has no Response.ok
                raise RuntimeError(
                    f"Upload failed {r.status_code}: {r.text[:600]}")
            out = r.json()
            file_id = out.get("id") if isinstance(out, dict) else None
            if not file_id:
                raise RuntimeError(f"Upload returned unexpected response: {out}")
            return file_id
        except RuntimeError:
            raise
        except httpx.HTTPError as e:
            last = e
            await asyncio.sleep(3 * attempt)
    raise last or RuntimeError("upload failed")


async def wait_for_processing(file_id: str, timeout: int = 300) -> None:
    url = f"{OPENWEBUI_URL}/api/v1/files/{file_id}/process/status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = await _request("GET", url, retries=0)
            if r.status_code < 400:
                body = r.json()
                status = (body or {}).get("status") if isinstance(body, dict) else None
                if status == "completed":
                    return
                if status == "failed":
                    raise RuntimeError(f"File processing failed: {body}")
        except RuntimeError:
            raise
        except Exception as e:
            log.debug("status poll error for %s: %s", file_id, e)
        await asyncio.sleep(3)
    log.warning("%s did not finish processing in %ss; continuing anyway",
                file_id, timeout)


async def add_to_knowledge_base(kb_id: str, file_id: str) -> None:
    r = await _request(
        "POST", f"{OPENWEBUI_URL}/api/v1/knowledge/{kb_id}/file/add",
        json={"file_id": file_id},
    )
    if r.status_code >= 400:
        body = r.text[:600]
        if "Duplicate content detected" in body:
            log.info("file %s already in KB %s (duplicate)", file_id, kb_id)
            return
        log.warning("KB file/add failed %s: %s", r.status_code, body)


def load_seen() -> set:
    if SEEN_FILE.exists():
        return {line for line in SEEN_FILE.read_text().splitlines() if line.strip()}
    return set()


def save_seen(seen: set) -> None:
    try:
        tmp = SEEN_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(sorted(seen)))
        os.replace(tmp, SEEN_FILE)
    except Exception as e:
        log.warning("seen-file write failed: %s", e)


def file_key_for(json_path: pathlib.Path) -> str:
    """Path (old-container spelling) + content hash, so an overwritten
    report is treated as new and re-synced."""
    h = hashlib.sha256(json_path.read_bytes()).hexdigest()
    rel = json_path.relative_to(RESULTS_DIR)
    return f"{_KEY_PREFIX}/{rel}:{h}"


async def process_new_file(json_path: pathlib.Path, kb_id: str) -> None:
    md_content = convert_to_markdown(json_path)
    md_path = json_path.with_suffix(".md")
    md_path.write_text(md_content)
    file_id = await _upload(md_path)
    await wait_for_processing(file_id)
    await add_to_knowledge_base(kb_id, file_id)
    log.info("Synced %s -> KB %s", json_path.name, kb_id)


async def run_sync_once(seen: set) -> None:
    kb_id = await get_or_create_knowledge_base()
    for json_path in sorted(RESULTS_DIR.rglob("full_states_log_*.json")):
        # Sidecar metadata written next to a report is not a report: syncing
        # it would add a content-free document to the knowledge base.
        if json_path.name.endswith(".meta.json"):
            continue
        key = file_key_for(json_path)
        if key in seen:
            continue
        try:
            await process_new_file(json_path, kb_id)
            _stats["synced"] += 1
        except (httpx.HTTPError, RuntimeError, OSError, ValueError,
                json.JSONDecodeError) as e:
            _stats["errors"] += 1
            log.warning("failed to process %s: %s", json_path.name, e)
        # Mark seen even on failure: the old container did the same, and
        # retrying a poison file every 30s would wedge the queue.
        seen.add(key)
        save_seen(seen)


async def _loop() -> None:
    seen = load_seen()
    log.info("kb_sync loop started (%d files already seen)", len(seen))
    while True:
        started = time.time()
        _stats["last_run"] = started
        before = _stats["synced"]
        errors_before = _stats["errors"]
        ok = False
        note = ""
        try:
            await run_sync_once(seen)
            ok = True
            uploaded = _stats["synced"] - before
            failed = _stats["errors"] - errors_before
            note = f"{uploaded} uploaded, {failed} failed, {len(seen)} seen"
            idle = uploaded == 0 and failed == 0
        except httpx.HTTPError as e:
            # Unreachable KB is a skip, not a crash — but it must be visible.
            note = f"Open WebUI unreachable: {e}"
            log.info("Open WebUI not reachable: %s", e)
            idle = False
        except Exception as e:
            _stats["errors"] += 1
            note = str(e)[:200]
            log.exception("kb_sync pass failed")
            idle = False
        runlog.record("kb_sync", ok, time.time() - started, note, idle=idle)
        await asyncio.sleep(POLL_INTERVAL)


def start() -> None:
    global _task
    if os.getenv("KB_SYNC_ENABLED", "0") != "1":
        log.info("kb_sync disabled (set KB_SYNC_ENABLED=1 to enable)")
        return
    _task = asyncio.create_task(_loop())
    _stats["running"] = True


async def stop() -> None:
    global _task
    _stats["running"] = False
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


async def close() -> None:
    if _client and not _client.is_closed:
        await _client.aclose()


def status() -> dict:
    return {**_stats, "enabled": os.getenv("KB_SYNC_ENABLED", "0") == "1",
            "seen_files": SEEN_FILE.exists()}