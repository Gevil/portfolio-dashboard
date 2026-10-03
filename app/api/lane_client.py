"""GPU-lane abstraction: find the lane to use and talk OpenAI-completions to it.

Every LLM call used to be pinned to one engine (opentrade/Open WebUI -> the
ninfer lane), so the whole AI path went dark whenever a different lane held the
GPU. Instead, the host's ``lanes.conf`` (bind-mounted read-only at
``/app/lanes.conf``) is the single source of truth: each line carries a 6th
field, the engine-reported OpenAI model id. A lane is usable only when

  1. its ``health_url`` answers 200, AND
  2. ``GET {base}/v1/models`` lists exactly that model id.

Lanes with an empty/absent 6th field are never eligible (vllm today). The file
is re-read behind a short TTL cache because a ``lanes-switch`` does NOT restart
this pod. The engine base URL is derived from the health URL's port and
reached through ``host.containers.internal`` (the lanes run on the host, not on
the ``aistock`` network).

Which eligible lane wins is an explicit policy, not file order:
``LANE_PREFERENCE`` (comma list, default ``ninfer-nvfp4,exllama``) is tried
first, in that order; eligible lanes it does not name follow in lanes.conf
order. Probe results are cached for a few seconds (positive and negative, never
two concurrent probes of one lane), and a lane that failed its last probe is
re-probed with a short timeout, so a blackholed lane costs seconds, not 35 s,
per call.

A report must not mix models: ``pin()`` binds every call made inside it to one
lane+model. If the pinned lane dies mid-job the pin falls back ONCE to the next
preferred lane and records that (``Pin.fallbacks``).

The daily budget for autonomous (model-initiated) turns is per purpose:
``digest`` has a reserve nobody else may eat into, ``triage`` is capped, the
rest (approvals/other) gets what remains. ``reservation()`` holds a whole job's
turns BEFORE the job starts; check-and-charge is one synchronous step (no
``await`` between the check and the charge) and a turn that never reached the
model (lane down, timeout, HTTP >= 500) is refunded.

Errors are returned as envelopes and NEVER raised into callers, so background
workers can defer instead of fabricating an answer:
``{"error": "lane_down"}``, ``{"error": "model_missing", "lane": name}``,
``{"error": "http", "status": n}``, ``{"error": "timeout"}``,
``{"error": "budget"}``. (The one exception is :class:`BudgetError`, raised by
``reservation()`` itself.)
"""
import asyncio
import contextlib
import contextvars
import datetime
import json
import logging
import os
import pathlib
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from app.api import jsonstore

log = logging.getLogger(__name__)

LANES_CONF = pathlib.Path(os.getenv("LANES_CONF", "/app/lanes.conf"))
# Host-side enrichment written by lanes-status: lane -> IP-resolved container
# base (the default 'podman' bridge has no built-in DNS for containers).
LANES_ENRICH = pathlib.Path(os.getenv("LANES_ENRICH", "/app/lanes-enrich.json"))
ENRICH_MAX_AGE_S = 120
# Lanes live on the host; containers reach the host on this name.
HOST_BASE = os.getenv("LANE_HOST_BASE", "http://host.containers.internal")
# Generous: /health is cheap, but the engine's HTTP loop can sit behind a long
# prefill (an interactive session pushing 100k+ prompt tokens) and answer late.
# A 3s probe then reported "lane_down" and killed a running job mid-spine — a
# busy lane is not a dead lane.
PROBE_TIMEOUT_S = float(os.getenv("LANE_PROBE_TIMEOUT_S", "10"))
# Second attempt when the first times out: the engine may simply be mid-prefill.
PROBE_GRACE_S = float(os.getenv("LANE_PROBE_GRACE_S", "25"))
# A lane that failed its last probe gets ONE short attempt: a blackholed host
# (no RST, no answer) otherwise costs PROBE_TIMEOUT_S + PROBE_GRACE_S per base.
PROBE_DOWN_TIMEOUT_S = float(os.getenv("LANE_PROBE_DOWN_TIMEOUT_S", "3"))
# Probe result cache. Short enough that a lanes-switch is noticed quickly, long
# enough that a burst of calls (chat turn, lane chip, workers) probes once.
PROBE_OK_TTL_S = float(os.getenv("LANE_PROBE_OK_TTL_S", "5"))
PROBE_DOWN_TTL_S = float(os.getenv("LANE_PROBE_DOWN_TTL_S", "10"))
# Lane switches happen without restarting this pod, so the config must go stale
# quickly — but not on every call (the lane chip and every chat turn probe).
CONF_TTL_S = 2.0
# Preferred lane order (names as in lanes.conf); read at call time.
DEFAULT_PREFERENCE = "ninfer-nvfp4,exllama"
# Budget: total / digest reserve / triage cap per local day (env at call time).
DEFAULT_TURN_BUDGET = 30
DEFAULT_DIGEST_RESERVE = 16
DEFAULT_TRIAGE_CAP = 10
# A server that rejects an optional request field is remembered this long, so
# every call does not pay a 400 round trip.
REJECT_TTL_S = 3600.0
OPTIONAL_FIELDS = ("response_format", "chat_template_kwargs")
PURPOSES = ("digest", "triage", "approval", "other")
BUDGET_FILE = os.path.join(os.getenv("HISTORY_DIR", "/app/data"),
                           "lane_budget.json")

_conf_cache: list[dict] = []
_conf_ts: float = 0.0


def _int_or(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm(value: str) -> str:
    """'-' is the lanes.conf sentinel for an intentionally empty column."""
    return "" if value == "-" else value


def _parse_conf() -> list[dict]:
    """lanes.conf -> [{name, unit, health_url, boot_wait_s, min_free_mib,
    model_id, container_base}] in file order. Comment/blank lines and rows
    with fewer than 5 fields are ignored; a missing 6th field means "not
    eligible"; container_base is the podman-network URI (lane host ports are
    loopback-published; containers on the 'podman' network reach the engine
    directly by name)."""
    try:
        text = LANES_CONF.read_text()
    except Exception as e:
        log.warning("lanes.conf unreadable (%s): %s", LANES_CONF, e)
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 5:
            continue
        out.append({
            "name": f[0],
            "unit": f[1],
            "health_url": f[2],
            "boot_wait_s": _int_or(f[3], 0),
            "min_free_mib": _int_or(f[4], 0),
            "model_id": _norm(f[5]) if len(f) > 5 else "",
            "container_base": _norm(f[6]) if len(f) > 6 else "",
        })
    return out

def lanes() -> list[dict]:
    """lanes.conf contents behind a CONF_TTL_S cache (keeps the last good
    parse when the file is briefly unreadable)."""
    global _conf_cache, _conf_ts
    now = time.monotonic()
    if now - _conf_ts < CONF_TTL_S:
        return _conf_cache
    parsed = _parse_conf()
    _conf_ts = now
    if parsed:
        _conf_cache = parsed
    return _conf_cache


_enrich_cache: dict = {}
_enrich_ts: float = 0.0
_enrich_warn_ts: float = 0.0
ENRICH_WARN_S = 900

def _enriched_bases() -> dict:
    """{lane_name: ip_base} from lanes-enrich.json (host-side lanes-status
    resolves the container names; stale file = no container path at all —
    an outdated container IP is worse than the host-port fallback)."""
    global _enrich_cache, _enrich_ts, _enrich_warn_ts
    now = time.monotonic()
    if now - _enrich_ts < CONF_TTL_S:
        return _enrich_cache
    _enrich_ts = now
    try:
        age = time.time() - LANES_ENRICH.stat().st_mtime
        if age > ENRICH_MAX_AGE_S:
            # Silent blindness here took the whole AI path down once: the lane
            # ports are published on 127.0.0.1 only, so without the container IP
            # there is no reachable base at all. Say it, rate-limited.
            if time.time() - _enrich_warn_ts > ENRICH_WARN_S:
                _enrich_warn_ts = time.time()
                log.warning(
                    "lanes-enrich.json is %ds old (> %ds): no container path for "
                    "any lane, and host-published lane ports are loopback-only, so "
                    "the GPU lane is unreachable from this pod. Check "
                    "lanes-enrich.timer and that the mount is the gpu-lanes "
                    "DIRECTORY (a single-file bind mount freezes when the writer "
                    "renames over it).", int(age), ENRICH_MAX_AGE_S)
            _enrich_cache = {}
        else:
            data = json.loads(LANES_ENRICH.read_text())
            bases = data.get("bases") if isinstance(data, dict) else None
            _enrich_cache = {k: str(v).rstrip("/")
                             for k, v in bases.items() if v} if bases else {}
    except FileNotFoundError:
        _enrich_cache = {}
    except Exception as e:
        log.debug("lanes-enrich read failed: %s", e)
        _enrich_cache = {}
    return _enrich_cache


def base_url(health_url: str) -> str:
    """Engine base through the host-published port: same port, host address."""
    try:
        port = urlsplit(health_url).port
    except ValueError:
        return ""
    if not port:
        return ""
    return f"{HOST_BASE}:{port}"


def _health_path(health_url: str) -> str:
    try:
        return urlsplit(health_url).path or "/"
    except ValueError:
        return "/"


def _candidate_bases(lane: dict) -> list[str]:
    """Reachable bases in preference order: IP-resolved podman-network URI
    (lane host ports are loopback-only by design; the bridge has no DNS, so
    the IP comes from the host-side lanes-enrich.json), then the
    host-published port via host.containers.internal."""
    out = []
    ip_base = _enriched_bases().get(lane["name"])
    if ip_base:
        out.append(ip_base)
    elif lane.get("container_base"):
        # Name is only usable if this container's resolver happens to know
        # it (custom DNS networks); harmless probe otherwise.
        out.append(lane["container_base"].rstrip("/"))
    host = base_url(lane["health_url"])
    if host and host not in out:
        out.append(host)
    return out


def _model_ids(resp: httpx.Response) -> set[str]:
    try:
        data = resp.json()
    except Exception:
        return set()
    rows = data.get("data") if isinstance(data, dict) else data
    ids = set()
    for m in rows or []:
        if isinstance(m, dict):
            mid = str(m.get("id") or "").strip()
            if mid:
                ids.add(mid)
    return ids


def _probe_budget(lane: dict) -> tuple[float, ...]:
    """Attempt timeouts for one lane: a generous pair normally, one short try
    when the previous probe found it down (see module docstring)."""
    if _last_state.get(lane["name"]) == "down":
        return (PROBE_DOWN_TIMEOUT_S,)
    return (PROBE_TIMEOUT_S, PROBE_GRACE_S)


async def _probe_lane(client: httpx.AsyncClient, lane: dict,
                      timeouts: tuple[float, ...]) -> tuple[str, str]:
    """Probe one lane -> ``("ok", base)`` / ``("wrong_model", "")`` /
    ``("down", "")``.

    Two attempts per base unless ``timeouts`` has one entry: a lane whose
    engine is mid-prefill for someone else answers ``/health`` late rather than
    never, and calling that ``lane_down`` kills running jobs on a merely busy
    GPU. The second attempt gets the grace window; a refused connection still
    fails fast (connect timeout).
    """
    path = _health_path(lane["health_url"])
    wrong_model = False
    for base in _candidate_bases(lane):
        for attempt, tmo in enumerate(timeouts):
            try:
                r = await client.get(f"{base}{path}", timeout=tmo)
                if r.status_code != 200:
                    break
                r = await client.get(f"{base}/v1/models", timeout=tmo)
                if r.status_code != 200:
                    break
            except httpx.TimeoutException:
                if attempt + 1 < len(timeouts):
                    continue          # maybe just busy: retry with more grace
                log.warning("lane %s did not answer the health probe within %ss"
                            " — busy or wedged?", lane["name"], tmo)
                break
            except httpx.HTTPError as e:
                # A refused port is the ordinary state of a stopped lane, and it
                # is re-probed every cache expiry: warning here would flood the
                # journal. A timeout (above) is the case worth flagging.
                log.debug("lane %s base %s refused (%s)", lane["name"], base,
                          type(e).__name__)
                break
            if lane["model_id"] in _model_ids(r):
                return "ok", base
            log.info("lane %s is up but not serving %s",
                     lane["name"], lane["model_id"])
            wrong_model = True
            break
    return ("wrong_model", "") if wrong_model else ("down", "")


# --------------------------------------------------------------------------
# Probe cache (positive + negative, single-flight per lane)
# --------------------------------------------------------------------------

_probe_cache: dict[str, tuple[float, str, str, tuple]] = {}
_last_state: dict[str, str] = {}
_probe_inflight: dict[str, asyncio.Task] = {}


def _sig(lane: dict) -> tuple:
    return (lane["health_url"], lane["model_id"])


def _probe_ttl(state: str) -> float:
    return PROBE_OK_TTL_S if state == "ok" else PROBE_DOWN_TTL_S


def reset_probe_cache() -> None:
    """Forget every cached probe (tests; a manual 'recheck now')."""
    _probe_cache.clear()
    _last_state.clear()
    _probe_inflight.clear()


def _invalidate(name: str) -> None:
    """A call to this lane just failed at the transport level: drop its cached
    'ok' and make the next probe the short one."""
    _probe_cache.pop(name, None)
    _last_state[name] = "down"


async def _probe_once(lane: dict) -> tuple[str, str]:
    """One uncached probe; never raises."""
    timeout = httpx.Timeout(PROBE_TIMEOUT_S, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await _probe_lane(client, lane, _probe_budget(lane))


async def _probe_and_store(lane: dict) -> tuple[str, str]:
    name = lane["name"]
    try:
        try:
            state, base = await _probe_once(lane)
        except Exception as e:                      # a probe must not raise
            log.warning("lane %s probe crashed: %s: %s", name,
                        type(e).__name__, e)
            state, base = "down", ""
        _probe_cache[name] = (time.monotonic(), state, base, _sig(lane))
        _last_state[name] = "down" if state == "down" else "up"
        return state, base
    finally:
        _probe_inflight.pop(name, None)


async def _probe_cached(lane: dict) -> tuple[str, str]:
    """``(state, base)`` for a lane from the cache, else one shared probe: two
    callers arriving together await the same request (the health URL is never
    hit twice concurrently)."""
    name = lane["name"]
    hit = _probe_cache.get(name)
    if (hit and hit[3] == _sig(lane)
            and time.monotonic() - hit[0] < _probe_ttl(hit[1])):
        return hit[1], hit[2]
    task = _probe_inflight.get(name)
    if task is None or task.done():
        task = asyncio.ensure_future(_probe_and_store(lane))
        _probe_inflight[name] = task
    return await asyncio.shield(task)


# --------------------------------------------------------------------------
# Lane policy: preference order, pinning
# --------------------------------------------------------------------------

_warned_pref: set[str] = set()


def preference() -> list[str]:
    """Preferred lane names, best first (``LANE_PREFERENCE``)."""
    raw = os.getenv("LANE_PREFERENCE", DEFAULT_PREFERENCE)
    return [p.strip() for p in raw.split(",") if p.strip()]


def order_lanes(all_lanes: list[dict], pref: list[str]) -> list[dict]:
    """Eligible lanes (a model id is configured) in call order: the preferred
    names first, in preference order, then the remaining eligible lanes in
    lanes.conf order."""
    eligible = [l for l in all_lanes if l.get("model_id")]
    by_name = {l["name"]: l for l in eligible}
    out: list[dict] = []
    seen: set[str] = set()
    for name in pref:
        lane = by_name.get(name)
        if lane is not None and name not in seen:
            out.append(lane)
            seen.add(name)
        elif lane is None and name not in _warned_pref:
            _warned_pref.add(name)
            log.warning("LANE_PREFERENCE names %r, which is not an eligible "
                        "lane in lanes.conf (ignored)", name)
    out.extend(l for l in eligible if l["name"] not in seen)
    return out


def ordered_lanes() -> list[dict]:
    return order_lanes(lanes(), preference())


class Pin:
    """The lane+model one job is bound to (see :func:`pin`)."""
    __slots__ = ("lane", "model", "fell_back", "fallbacks")

    def __init__(self) -> None:
        self.lane = ""
        self.model = ""
        self.fell_back = False
        self.fallbacks: list[dict] = []


_PIN: contextvars.ContextVar[Pin | None] = contextvars.ContextVar(
    "lane_pin", default=None)


@contextlib.contextmanager
def pin():
    """Bind every lane call made inside the ``with`` block (same task) to ONE
    lane+model: the first call picks by preference, later calls reuse it. When
    the pinned lane stops answering, the pin falls back once to the next
    preferred lane and records it in ``Pin.fallbacks``; a second loss is an
    error envelope like any other."""
    p = Pin()
    token = _PIN.set(p)
    try:
        yield p
    finally:
        _PIN.reset(token)


async def _first_ok(candidates: list[dict]) -> tuple[dict | None, str, str, str]:
    """First candidate (in order) that probes ok -> ``(lane, reason, name,
    base)``; reason ``lane_down`` / ``model_missing`` when none does."""
    wrong: str = ""
    for lane in candidates:
        state, base = await _probe_cached(lane)
        if state == "ok":
            return lane, "ok", lane["name"], base
        if state == "wrong_model" and not wrong:
            wrong = lane["name"]
    if wrong:
        return None, "model_missing", wrong, ""
    return None, "lane_down", "", ""


async def _pick_lane() -> tuple[dict | None, str, str, str]:
    """The lane for the next call: preference order, honouring the active pin.

    Returns ``(lane, reason, lane_name, base)`` with reason one of
    ``ok`` / ``no_lanes`` / ``lane_down`` (nothing answered healthy) /
    ``model_missing`` (a lane answered but serves a different id); ``base`` is
    the base URL that answered.
    """
    order = ordered_lanes()
    if not order:
        return None, "no_lanes", "", ""
    p = _PIN.get()
    if p is None or not p.lane:
        lane, reason, name, base = await _first_ok(order)
        if lane is not None and p is not None:
            p.lane, p.model = lane["name"], lane["model_id"]
        return lane, reason, name, base
    pinned = next((l for l in order if l["name"] == p.lane), None)
    if pinned is not None:
        state, base = await _probe_cached(pinned)
        if state == "ok":
            return pinned, "ok", pinned["name"], base
        if p.fell_back:
            if state == "wrong_model":
                return None, "model_missing", p.lane, ""
            return None, "lane_down", "", ""
    rest = [l for l in order if l["name"] != p.lane]
    lane, reason, name, base = await _first_ok(rest)
    if lane is None:
        return None, reason, name, base
    p.fallbacks.append({"from": p.lane, "to": lane["name"],
                        "model": lane["model_id"], "at": time.time()})
    log.warning("pinned lane %s is gone: falling back once to %s (%s)",
                p.lane, lane["name"], lane["model_id"])
    p.lane, p.model, p.fell_back = lane["name"], lane["model_id"], True
    return lane, "ok", name, base


def _role(lane_name: str) -> str:
    order = ordered_lanes()
    return "primary" if order and order[0]["name"] == lane_name else "fallback"


async def active_lane() -> dict | None:
    """``{name, base, model_id, health_ok, role}`` for the lane to use right
    now, or None when no lane qualifies. Uses the probe cache (a few seconds)."""
    lane, reason, name, base = await _pick_lane()
    if lane is None:
        log.info("no lane eligible (%s)", reason)
        return None
    return {"name": lane["name"], "base": base,
            "model_id": lane["model_id"], "health_ok": True,
            "role": _role(lane["name"])}


async def lane_status() -> dict:
    """Payload of ``GET /api/lane-status``: the serving lane (old keys kept),
    its role, the preference order, every lane's probe state and the budget."""
    all_lanes = lanes()
    order = order_lanes(all_lanes, preference())
    results = await asyncio.gather(*(_probe_cached(l) for l in order))
    candidates = [{"name": l["name"], "model_id": l["model_id"], "state": st}
                  for l, (st, _) in zip(order, results)]
    candidates += [{"name": l["name"], "model_id": "", "state": "no_model"}
                   for l in all_lanes if not l.get("model_id")]
    serving = next(((l, base) for l, (st, base) in zip(order, results)
                    if st == "ok"), None)
    out = {"lane": None, "serving_model": False, "model": None,
           "base_url": None, "role": None, "preference": preference(),
           "candidates": candidates, "budget": budget_state()}
    if serving is not None:
        lane, base = serving
        out.update(lane=lane["name"], serving_model=True,
                   model=lane["model_id"], base_url=base,
                   role="primary" if lane is order[0] else "fallback")
    return out


def _err(reason: str, lane_name: str = "") -> dict:
    if reason == "model_missing":
        return {"error": "model_missing", "lane": lane_name}
    return {"error": reason}


# --------------------------------------------------------------------------
# Daily budget for autonomous (model-initiated) turns.
# --------------------------------------------------------------------------

class BudgetError(Exception):
    """``reservation()`` could not hold the turns a job needs."""

    def __init__(self, purpose: str, need: int, left: int):
        super().__init__(f"{purpose}: need {need} turn(s), {left} left today")
        self.purpose, self.need, self.left = purpose, need, left


_budget_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    return max(0, _int_or(os.getenv(name, ""), default))


def _limits() -> dict:
    """Today's limits from the environment (call time, so tests and a changed
    env.secrets take effect without code changes)."""
    cap = _env_int("AUTONOMOUS_TURN_BUDGET", DEFAULT_TURN_BUDGET)
    reserve = min(cap, _env_int("DIGEST_TURN_RESERVE", DEFAULT_DIGEST_RESERVE))
    triage = min(cap - reserve, _env_int("TRIAGE_TURN_CAP",
                                         DEFAULT_TRIAGE_CAP))
    return {"cap": cap, "digest": reserve, "triage": triage,
            "other": cap - reserve - triage}


def _group(purpose: str | None) -> str:
    """digest / triage / other (approvals and unlabelled calls share 'other')."""
    return purpose if purpose in ("digest", "triage") else "other"


def _budget_default() -> dict:
    return {"day": datetime.date.today().isoformat(), "used": 0,
            "purposes": {p: 0 for p in PURPOSES}}


def _load_budget() -> dict:
    """Today's ledger. Tolerates the old ``{day, used, cap}`` file (its cap is
    ignored — the environment decides — and its unattributed ``used`` is kept
    against the total) and drops a ledger from another day."""
    b = _budget_default()
    stored = jsonstore.load(BUDGET_FILE, None)
    if isinstance(stored, dict) and stored.get("day") == b["day"]:
        try:
            b["used"] = max(0, int(stored.get("used") or 0))
        except (TypeError, ValueError):
            pass
        by = stored.get("purposes")
        if isinstance(by, dict):
            for p in PURPOSES:
                try:
                    b["purposes"][p] = max(0, int(by.get(p) or 0))
                except (TypeError, ValueError):
                    pass
    return b


def _save_budget(b: dict) -> None:
    jsonstore.save(BUDGET_FILE, {**b, "cap": _limits()["cap"]})


class Reservation:
    """Turns held for one job (see :func:`reservation`)."""
    __slots__ = ("purpose", "turns_left", "released")

    def __init__(self, purpose: str, turns: int) -> None:
        self.purpose = purpose
        self.turns_left = turns
        self.released = False


_reservations: list[Reservation] = []
_RES: contextvars.ContextVar[Reservation | None] = contextvars.ContextVar(
    "lane_reservation", default=None)


def _held(group: str | None = None) -> int:
    return sum(r.turns_left for r in _reservations
               if group is None or _group(r.purpose) == group)


def _used(b: dict, group: str) -> int:
    p = b["purposes"]
    if group == "other":
        return p["approval"] + p["other"]
    return p[group]


def _room(b: dict, group: str, turns: int) -> bool:
    """May ``turns`` more turns start for this group? (caller holds the lock)

    * never beyond the day's total (used + held);
    * triage and 'other' must leave the digest reserve untouched;
    * triage and 'other' also stay inside their own allowance.
    """
    lim = _limits()
    total_left = lim["cap"] - b["used"] - _held()
    if turns > total_left:
        return False
    if group == "digest":
        return True
    digest_unmet = max(0, lim["digest"] - _used(b, "digest") - _held("digest"))
    if turns > total_left - digest_unmet:
        return False
    return _used(b, group) + _held(group) + turns <= lim[group]


def _left(b: dict, group: str) -> int:
    lim = _limits()
    for t in range(max(0, lim["cap"] - b["used"] - _held()), 0, -1):
        if _room(b, group, t):
            return t
    return 0


def budget_state(purpose: str | None = None) -> dict:
    """``{day, used, cap, left, ...}`` for today's autonomous-turn budget.

    ``left`` is the day's remaining total, or — when ``purpose`` is given — the
    turns that purpose may still start right now. ``purposes`` shows every
    group's use against its allowance."""
    with _budget_lock:
        b = _load_budget()
        lim = _limits()
        groups = {g: {"used": _used(b, g), "allowance": lim[g],
                      "left": _left(b, g)} for g in ("digest", "triage", "other")}
        total_left = max(0, lim["cap"] - b["used"] - _held())
        out = {"day": b["day"], "used": b["used"], "cap": lim["cap"],
               "left": groups[_group(purpose)]["left"] if purpose
               else total_left,
               "held": _held(), "digest_reserve": lim["digest"],
               "triage_cap": lim["triage"], "purposes": groups}
        if purpose:
            out["purpose"] = purpose
        return out


def can_start(purpose: str | None, turns: int = 1) -> bool:
    """Is there room for a job of ``turns`` turns right now? (a check only —
    use :func:`reservation` to hold them)"""
    with _budget_lock:
        return _room(_load_budget(), _group(purpose), max(1, int(turns)))


@contextlib.contextmanager
def reservation(purpose: str, turns: int):
    """Hold ``turns`` turns for the job about to run; raises
    :class:`BudgetError` when they do not fit. Calls made inside the block that
    are autonomous draw from the hold first; whatever is unused is released at
    exit, so a quick job that needed 2 of its 3 reserved turns gives one back."""
    turns = max(1, int(turns))
    group = _group(purpose)
    with _budget_lock:
        b = _load_budget()
        if not _room(b, group, turns):
            raise BudgetError(purpose, turns, _left(b, group))
        res = Reservation(purpose, turns)
        _reservations.append(res)
    token = _RES.set(res)
    try:
        yield res
    finally:
        _RES.reset(token)
        with _budget_lock:
            res.released = True
            if res in _reservations:
                _reservations.remove(res)


def _charge(purpose: str | None):
    """Check AND charge one turn in a single synchronous step (no ``await``
    between the two, so concurrent workers cannot both spend the last turn).
    Returns a charge token for :func:`_refund`, or None when over budget."""
    res = _RES.get()
    if res is not None and (res.released or res.turns_left <= 0
                            or (purpose and _group(purpose) != _group(res.purpose))):
        res = None
    purpose = purpose or (res.purpose if res else None)
    group = _group(purpose)
    with _budget_lock:
        b = _load_budget()
        if res is not None:
            res.turns_left -= 1
        elif not _room(b, group, 1):
            return None
        b["used"] += 1
        key = purpose if purpose in PURPOSES else "other"
        b["purposes"][key] += 1
        _save_budget(b)
        return (b["day"], key, res)


def _refund(charge) -> None:
    """Give back a turn that never reached the model."""
    day, key, res = charge
    with _budget_lock:
        b = _load_budget()
        if b["day"] != day:
            return                       # the ledger rolled over meanwhile
        b["used"] = max(0, b["used"] - 1)
        b["purposes"][key] = max(0, b["purposes"][key] - 1)
        if res is not None and not res.released:
            res.turns_left += 1
        _save_budget(b)


def _should_refund(result: dict) -> bool:
    err = result.get("error")
    if err in ("lane_down", "timeout"):
        return True
    return err == "http" and int(result.get("status") or 0) >= 500


# One autonomous request at a time: the GPU is shared with the owner's own
# sessions, so triage, digest and approvals queue up instead of stacking
# prefill on the card.
_auto_sem: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _auto_semaphore() -> asyncio.Semaphore:
    global _auto_sem
    loop = asyncio.get_running_loop()
    if _auto_sem is None or _auto_sem[0] is not loop:
        _auto_sem = (loop, asyncio.Semaphore(1))
    return _auto_sem[1]


# --------------------------------------------------------------------------
# Chat completion
# --------------------------------------------------------------------------

_rejected: dict[tuple[str, str, str], float] = {}
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def _body(messages: list, model: str, max_tokens: int, temperature,
          json_mode: bool, stream: bool,
          enable_thinking: bool | None = None) -> dict:
    body: dict = {"model": model, "messages": messages,
                  "max_tokens": max_tokens, "stream": stream}
    if temperature is not None:
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if enable_thinking is not None:
        # Per-request only: the lane's own config default (``enable_thinking:
        # true``) stays untouched, so the interactive coding session keeps its
        # reasoning. A thinking model spends the whole completion budget on
        # hidden reasoning before any visible content, so machine-consumed
        # calls (pipeline stages, classifiers) opt out or they truncate to
        # ``content: null``.
        body["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    return body


def _drop_known_rejected(body: dict, lane_name: str, model: str) -> list[str]:
    """Remove optional fields this lane already refused (and still remembers)."""
    now = time.monotonic()
    dropped = []
    for f in OPTIONAL_FIELDS:
        ts = _rejected.get((lane_name, model, f))
        if ts is not None and now - ts < REJECT_TTL_S and f in body:
            body.pop(f)
            dropped.append(f)
    return dropped


def _reject_fields(body: dict, err_text: str, lane_name: str,
                   model: str) -> list[str]:
    """After an HTTP 400: pick the optional fields to drop. The ones the error
    text names; when it names none, every optional field present (a lane may
    not say which one it disliked). Remembers the choice per lane+model."""
    present = [f for f in OPTIONAL_FIELDS if f in body]
    text = err_text or ""
    named = [f for f in present if f in text]
    if "enable_thinking" in text and "chat_template_kwargs" in present \
            and "chat_template_kwargs" not in named:
        named.append("chat_template_kwargs")
    drop = named or present
    now = time.monotonic()
    for f in drop:
        body.pop(f, None)
        _rejected[(lane_name, model, f)] = now
    return drop


def _content(data: dict) -> str:
    """Visible answer only. ``reasoning_content`` is deliberately NOT a
    fallback: hidden chain-of-thought is not the deliverable, and passing it
    off as the answer would silently put rambling into reports and alerts. An
    engine that does not split reasoning off (a lane that ignored
    ``enable_thinking=False``) leaves an inline ``<think>`` block, which is
    removed here; an unterminated one means the answer never started."""
    for c in data.get("choices") or []:
        if not isinstance(c, dict):
            continue
        msg = c.get("message") or {}
        text = msg.get("content")
        if text is None:
            text = c.get("text")
        if text:
            text = _THINK.sub("", str(text), count=1)
            if text.lstrip().startswith("<think>"):
                return ""
            return text
    return ""


async def _post_completion(lane: dict, base: str, body: dict,
                           timeout: float) -> dict:
    """One non-streaming request against a chosen lane -> result envelope."""
    name, model = lane["name"], body["model"]
    dropped = _drop_known_rejected(body, name, model)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            url = f"{base}/v1/chat/completions"
            r = await client.post(url, json=body)
            # A lane that rejects an optional field (``response_format``, or
            # ``chat_template_kwargs`` on an engine with no such template
            # arg): retry without it and let the caller fall back to
            # fenced-JSON extraction / the engine's own thinking default.
            for _ in range(2):
                if r.status_code != 400:
                    break
                gone = _reject_fields(body, r.text, name, model)
                if not gone:
                    break
                log.info("lane %s rejected %s (HTTP 400): retrying without",
                         name, ", ".join(gone))
                dropped += gone
                r = await client.post(url, json=body)
    except httpx.TimeoutException:
        log.warning("lane %s chat timed out after %.0fs", name, timeout)
        return {"error": "timeout"}
    except httpx.HTTPError as e:
        log.warning("lane %s chat unreachable: %s", name, e)
        return {"error": "lane_down"}

    if r.status_code != 200:
        log.warning("lane %s chat HTTP %s: %s", name, r.status_code,
                    r.text[:200])
        return {"error": "http", "status": r.status_code}
    try:
        data = r.json()
    except Exception:
        log.warning("lane %s chat returned non-JSON", name)
        return {"error": "http", "status": r.status_code}
    choices = data.get("choices") or []
    finish = ""
    reasoning = ""
    if choices and isinstance(choices[0], dict):
        finish = str(choices[0].get("finish_reason") or "")
        reasoning = str((choices[0].get("message") or {}).get(
            "reasoning_content") or "")
    return {"content": _content(data), "finish_reason": finish,
            "lane": name, "model": model,
            "usage": data.get("usage") or {},
            "dropped_fields": dropped,
            # Diagnostic only: an empty content with a large reasoning count is
            # the "thinking ate the budget" signature, not a dead model.
            "reasoning_chars": len(reasoning)}


async def _complete(messages: list, *, max_tokens: int, temperature,
                    json_mode: bool, timeout: float, autonomous: bool,
                    model_hint: str, enable_thinking: bool | None,
                    purpose: str | None) -> dict:
    if not autonomous:
        return await _complete_once(messages, max_tokens, temperature,
                                    json_mode, timeout, False, model_hint,
                                    enable_thinking, purpose)
    if _RES.get() is None and not can_start(purpose, 1):
        return {"error": "budget"}
    async with _auto_semaphore():
        return await _complete_once(messages, max_tokens, temperature,
                                    json_mode, timeout, True, model_hint,
                                    enable_thinking, purpose)


async def _complete_once(messages, max_tokens, temperature, json_mode, timeout,
                         autonomous, model_hint, enable_thinking,
                         purpose) -> dict:
    """Pick a lane, charge (autonomous), post, refund when the turn never
    reached the model. A pinned job whose lane vanished retries once on the
    fallback the pin selects."""
    for attempt in range(2):
        lane, reason, name, base = await _pick_lane()
        if lane is None:
            return _err(reason, name)
        model = model_hint or lane["model_id"]
        body = _body(messages, model, max_tokens, temperature, json_mode,
                     False, enable_thinking)
        charge = None
        if autonomous:
            charge = _charge(purpose)       # check+charge: no await in between
            if charge is None:
                return {"error": "budget"}
        result = await _post_completion(lane, base, body, timeout)
        if charge is not None and _should_refund(result):
            _refund(charge)
        err = result.get("error")
        if err in ("lane_down", "timeout"):
            _invalidate(lane["name"])
        p = _PIN.get()
        if err == "lane_down" and p is not None and not p.fell_back \
                and attempt == 0:
            continue                         # _pick_lane falls back (once)
        if p is not None and not err:
            result["lane_fallbacks"] = list(p.fallbacks)
        return result
    return {"error": "lane_down"}


async def _stream(messages: list, *, max_tokens: int, temperature,
                  timeout: float, model_hint: str):
    """Async iterator over the lane's SSE reply: ``{"meta": {lane, model}}``
    first, then ``{"delta": text}`` per chunk and ``{"finish_reason": str}``
    when the engine states one; one ``{"error": ...}`` envelope (then stop) on
    any failure. Interactive only: no budget charge, no pin."""
    lane, reason, name, base = await _pick_lane()
    if lane is None:
        yield _err(reason, name)
        return
    model = model_hint or lane["model_id"]
    body = _body(messages, model, max_tokens, temperature, False, True)
    tmo = httpx.Timeout(timeout, connect=10.0)
    url = f"{base}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=tmo) as client:
            for attempt in range(2):
                async with client.stream("POST", url, json=body) as r:
                    if r.status_code == 400 and attempt == 0:
                        text = (await r.aread()).decode("utf-8", "replace")
                        if _reject_fields(body, text, lane["name"], model):
                            continue
                    if r.status_code != 200:
                        await r.aread()
                        log.warning("lane %s chat stream HTTP %s", lane["name"],
                                    r.status_code)
                        yield {"error": "http", "status": r.status_code}
                        return
                    yield {"meta": {"lane": lane["name"], "model": model}}
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except Exception:
                            continue
                        for c in obj.get("choices") or []:
                            if not isinstance(c, dict):
                                continue
                            delta = (c.get("delta") or {}).get("content")
                            if delta:
                                yield {"delta": str(delta)}
                            if c.get("finish_reason"):
                                yield {"finish_reason": str(c["finish_reason"])}
                    return
    except httpx.TimeoutException:
        log.warning("lane %s chat stream timed out after %.0fs", lane["name"],
                    timeout)
        _invalidate(lane["name"])
        yield {"error": "timeout"}
    except httpx.HTTPError as e:
        log.warning("lane %s chat stream unreachable: %s", lane["name"], e)
        _invalidate(lane["name"])
        yield {"error": "lane_down"}


def chat(messages: list, *, max_tokens: int = 4096, temperature=None,
         json_mode: bool = False, stream: bool = False, timeout: float = 900.0,
         autonomous: bool = False, model: str = "",
         enable_thinking: bool | None = None, purpose: str | None = None):
    """One completion against the best lane that is up.

    ``stream=False`` -> await this: ``await chat(msgs)`` returns
    ``{content, finish_reason, lane, model, usage}`` or an error envelope.
    Every success carries the ``lane`` and ``model`` that answered.

    ``stream=True`` -> do NOT await: ``async for ev in chat(msgs, stream=True)``
    yields ``{"meta"}``, ``{"delta": str}``, ``{"finish_reason"}`` events and at
    most one ``{"error": ...}`` envelope. Streaming is interactive-only.

    ``model`` overrides the lane's configured model id (empty = use it).
    ``autonomous=True`` is for model-initiated work: it waits for the single
    autonomous slot, charges the daily budget (``purpose`` picks the allowance:
    digest / triage / approval / other; inside :func:`reservation` the job's
    held turns are used first) and refuses with ``{"error": "budget"}`` when
    spent. ``enable_thinking=False`` suppresses the lane's hidden reasoning for
    this request only (machine-consumed calls; leave it None for interactive
    chat so the engine's own default stands).
    """
    if stream:
        if autonomous:
            raise ValueError("autonomous calls are not streamed")
        return _stream(messages, max_tokens=max_tokens,
                       temperature=temperature, timeout=timeout,
                       model_hint=model)
    return _complete(messages, max_tokens=max_tokens,
                     temperature=temperature, json_mode=json_mode,
                     timeout=timeout, autonomous=autonomous,
                     model_hint=model, enable_thinking=enable_thinking,
                     purpose=purpose)