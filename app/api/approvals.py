"""Approval queue: the AI proposes, a human decides.

Deep-dive analysis is expensive (a full debate spine burns several lane turns),
so nothing autonomous starts one. ``triage`` files a proposal here; the ntfy
message carries Approve/Dismiss buttons that hit the token-gated endpoints, and
only an approval enqueues the job.

Lifecycle: ``pending`` -> ``approved`` / ``denied`` / ``expired`` (TTL, checked on
read) / ``abandoned`` (still pending when the pod restarted — a stale proposal
must not survive into a new day's context).

Button URLs carry a PER-APPROVAL token in the query string (ntfy action URLs
cannot carry headers): ``<expiry>.<hmac>`` where the HMAC (SHA-256) covers the
approval id, the action (``approve`` / ``deny``) and the expiry, under a key
derived from ``APPROVAL_TOKEN``. A leaked button URL therefore opens exactly one
door - that action on that approval, until its TTL - and the secret itself never
leaves the server. Tokens are compared as bytes with ``hmac.compare_digest``
(a non-ASCII token is simply wrong, never a 500) and the auth middleware
exempts only the two POST routes.
"""
import hashlib
import hmac
import logging
import os
import pathlib
import time

from app.api import jsonstore

log = logging.getLogger("approvals")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
APPROVALS_FILE = DATA_DIR / "approvals.json"

TTL_S = int(os.getenv("APPROVAL_TTL_S", "1800"))
MAX_ROWS = 200
ACTIONS = ("approve", "deny")
# Last-resort base for the ntfy action URLs when neither env var is set.
FALLBACK_BASE = "http://localhost:8601"


def _secret() -> str:
    return os.getenv("APPROVAL_TOKEN", "")


def public_base() -> str:
    """Address the PHONE can reach (it is not on the pod network): the
    dashboard's public URL, else the same LAN URL ntfy uses as its tap target
    (NTFY_CLICK_URL)."""
    return (os.getenv("DASHBOARD_PUBLIC_URL") or os.getenv("NTFY_CLICK_URL")
            or FALLBACK_BASE).rstrip("/")


def _load() -> list[dict]:
    rows = jsonstore.load(APPROVALS_FILE, [])
    return [r for r in rows if isinstance(r, dict)] \
        if isinstance(rows, list) else []


def _save(rows: list[dict]) -> bool:
    ok = jsonstore.save(APPROVALS_FILE, rows[-MAX_ROWS:])
    if not ok:
        log.error("approvals write failed - state not persisted")
    return ok


def _mac(approval_id: str, action: str, expiry: int) -> str:
    # Derived key: the raw APPROVAL_TOKEN is never used as a MAC key directly.
    key = hmac.new(_secret().encode("utf-8"), b"approval-action-v1",
                   hashlib.sha256).digest()
    msg = f"{approval_id}|{action}|{expiry}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def make_token(approval_id: str, action: str, expiry: int) -> str:
    """``<expiry>.<hex hmac>`` for one approval + action (needs the secret)."""
    return f"{int(expiry)}.{_mac(approval_id, action, int(expiry))}"


def check_token(token: str | None, approval_id: str, action: str,
                now: float | None = None) -> bool:
    """True only for an unexpired token minted for exactly this approval id and
    action. A missing secret denies (fail closed); any malformed or non-ASCII
    token is False, never an exception."""
    if not _secret() or not token or action not in ACTIONS:
        return False
    expiry_s, _, supplied = str(token).partition(".")
    try:
        expiry = int(expiry_s)
    except ValueError:
        return False
    if (time.time() if now is None else now) > expiry:
        return False
    expected = _mac(approval_id, action, expiry).encode("ascii")
    return hmac.compare_digest(expected, supplied.encode("utf-8", "replace"))


def _expire(rows: list[dict], now: float) -> tuple[list[dict], int]:
    """Flip past-TTL pendings to expired (the plan's TTL is checked on read)."""
    n = 0
    for r in rows:
        if r.get("status") == "pending" and now - (r.get("created_at") or 0) > TTL_S:
            r["status"] = "expired"
            r["decided_at"] = now
            n += 1
    return rows, n


def boot_reap() -> int:
    """Called at startup: proposals from a dead process are resolved honestly.

    Past-TTL rows must become ``expired`` (the human simply never answered), not
    ``abandoned`` (which means the process died holding it open) — so the TTL
    pass runs first, exactly like every read path.
    """
    rows = _load()
    now = time.time()
    rows, expired = _expire(rows, now)
    abandoned = 0
    for r in rows:
        if r.get("status") == "pending":
            r["status"] = "abandoned"
            r["decided_at"] = now
            abandoned += 1
    _save(rows)
    if abandoned or expired:
        log.info("approvals: reaped a previous run — %d abandoned, %d expired",
                 abandoned, expired)
    return abandoned + expired


def create(ticker: str, intent: str, source_id: str = "",
           raw: str = "") -> dict:
    """File one proposal. ``intent`` is what approving will do, in one line."""
    rows = _load()
    now = time.time()
    row = {
        "id": hashlib.sha1(f"{ticker}|{source_id}|{now}".encode()).hexdigest()[:10],
        "ticker": (ticker or "").upper(),
        "intent": intent[:200],
        "source_id": source_id[:200],
        "status": "pending",
        "created_at": now,
        "decided_at": None,
        "raw": raw[:500],
    }
    # One open proposal per ticker+intent: triage runs every 10 minutes and the
    # same story must not stack a queue of identical buttons.
    for r in rows:
        if (r.get("status") == "pending" and r.get("ticker") == row["ticker"]
                and r.get("intent") == row["intent"]):
            return r
    rows, _ = _expire(rows, now)
    rows.append(row)
    _save(rows)
    log.info("approval proposed: %s %s (%s)", row["ticker"], row["id"],
             row["intent"][:60])
    return row


def get(approval_id: str) -> dict | None:
    rows = _load()
    rows, n = _expire(rows, time.time())
    if n:
        _save(rows)
    for r in rows:
        if r.get("id") == approval_id:
            return r
    return None


def decide(approval_id: str, approve: bool) -> tuple[dict | None, str]:
    """-> (row, reason). reason explains a refusal ('gone', 'expired', ...)."""
    rows = _load()
    now = time.time()
    rows, n = _expire(rows, now)
    for r in rows:
        if r.get("id") != approval_id:
            continue
        if r.get("status") != "pending":
            if n:
                _save(rows)
            return r, f"already {r.get('status')}"
        r["status"] = "approved" if approve else "denied"
        r["decided_at"] = now
        _save(rows)
        return r, "ok"
    if n:
        _save(rows)
    return None, "not found"


def list_rows(limit: int = 50) -> list[dict]:
    rows = _load()
    rows, n = _expire(rows, time.time())
    if n:
        _save(rows)
    return sorted(rows, key=lambda r: r.get("created_at") or 0,
                  reverse=True)[:limit]


def counts() -> dict[str, int]:
    rows = _load()
    rows, n = _expire(rows, time.time())
    if n:
        _save(rows)
    out: dict[str, int] = {}
    for r in rows:
        st = str(r.get("status") or "unknown")
        out[st] = out.get(st, 0) + 1
    return out


def action_urls(row: dict) -> list[dict]:
    """ntfy action buttons (``notify.push(actions=...)`` renders them). Each
    URL carries its own HMAC token, valid for that action until the proposal's
    TTL runs out."""
    if not _secret():
        return []
    expiry = int((row.get("created_at") or time.time()) + TTL_S)
    base = f"{public_base()}/api/approvals/{row['id']}"

    def url(action: str) -> str:
        return f"{base}/{action}?token={make_token(row['id'], action, expiry)}"

    return [
        {"label": "Run deep-dive", "url": url("approve"), "method": "POST"},
        {"label": "Dismiss", "url": url("deny"), "method": "POST"},
    ]


def age_of(row: dict) -> int:
    return int(time.time() - (row.get("created_at") or time.time()))


def ttl_remaining(row: dict) -> int:
    return max(0, int(TTL_S - age_of(row)))
