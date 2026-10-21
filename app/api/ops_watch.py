"""Ops watchdog: GPU-lane / host-dashboard health, deduped ntfy alerts.

Absorbs the retired host-side ``lane_ensure`` timer. Every few minutes it reads
the host-dashboard bridge (``:8443/state``, which is ``lanes-status --json`` plus
service states) and raises only what an operator can act on:

* a lane unit is ``running`` but nothing is serving (``lane_activity.lane``
  null) — the model process is up and the engine is not answering;
* the ACTIVE lane reports ``degraded`` for longer than a grace window;
* a lane boot is stuck past its own ``boot_wait_s`` while VRAM is exhausted.

Two deliberate departures from the literal rule set, both against alert
fatigue on this box:

* ``degraded`` from the bridge means "unit up, 2 s health curl failed". Under a
  loaded lane (the model serving this very session) that is ordinary slowness,
  so before alerting the finding is re-checked with ``lane_client.active_lane()``
  — a longer timeout plus a model-id match. If the lane answers there, the
  finding is recorded as ``slow`` and stays silent.
* ``gpu.free_mib < 1000`` is the NORMAL steady state with a 29 GB model loaded,
  so low free VRAM alone is only surfaced in ``status()``. It becomes an alert
  when a lane is stuck ``starting`` past its boot wait — the case where free
  VRAM actually blocks a switch.

Findings dedupe on a stable key with a 24 h re-alert interval, so a persistent
condition nags once a day instead of once per pass. Each alert is a priority-3
warning from source ``ops_watch`` (``notify`` routes it to the ops topic).
When a finding that had alerted clears, an info "Resolved" row is written to
the notification store (alert center only, never pushed).
"""
import asyncio
import logging
import os
import pathlib
import time

import httpx

from app.api import jsonstore, lane_client, notify, runlog

log = logging.getLogger("ops_watch")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "ops_watch_state.json"

STATE_URL = os.getenv("HOST_DASHBOARD_URL",
                      "http://host.containers.internal:8443/state")
INTERVAL_S = int(os.getenv("OPS_WATCH_INTERVAL_S", "300"))
DEGRADED_GRACE_S = int(os.getenv("OPS_WATCH_DEGRADED_GRACE_S", "600"))
LOW_FREE_MIB = int(os.getenv("OPS_WATCH_LOW_FREE_MIB", "1000"))
REALERT_S = int(os.getenv("OPS_WATCH_REALERT_S", "86400"))   # 24 h
STUCK_GRACE_S = 120          # a "starting" lane is not stuck for the first 2 min

_user = os.getenv("DASHBOARD_USER", "")
_password = os.getenv("DASHBOARD_PASS", "")

_client: httpx.AsyncClient | None = None
_task: asyncio.Task | None = None
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "alerts": 0,
          "source": "", "low_free_mib": False, "findings": 0}


def _load() -> dict:
    data = jsonstore.load(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    jsonstore.save(STATE_FILE, data)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            auth=(_user, _password) if _user and _password else None)
    return _client


async def _fetch_state() -> dict | None:
    try:
        r = await _http().get(STATE_URL)
        if r.status_code != 200:
            log.info("host-dashboard state HTTP %s", r.status_code)
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError) as e:
        log.debug("host-dashboard state unavailable: %s", e)
        return None


async def _lane_answers() -> bool:
    """True when the lane abstraction itself can reach a serving model — the
    authoritative check the dashboard actually uses (longer timeout, and it
    verifies the model id, which a bare /health probe does not)."""
    try:
        return await lane_client.active_lane() is not None
    except Exception as e:                       # never let a probe raise outward
        log.debug("lane probe failed: %s", e)
        return False


async def collect() -> tuple[list[dict], dict]:
    """(findings, snapshot) for one pass."""
    state = await _fetch_state()
    findings: list[dict] = []
    snap = {"source": "bridge" if state else "lane-probe",
            "low_free_mib": False, "lane": None, "starting": []}

    if state is None:
        # Bridge down (host-dashboard stopped / bridge unreachable): degrade to
        # lane-health-only, which is still the question operators ask.
        if not await _lane_answers():
            findings.append({
                "key": "no-serving-lane",
                "title": "GPU lane: no serving model",
                "body": "host-dashboard :8443 is unreachable AND no lane answers "
                        "the dashboard's own health+model probe. Analysis, "
                        "digest and chat are degraded."})
        return findings, snap

    lanes = state.get("lanes") if isinstance(state.get("lanes"), dict) else {}
    activity = state.get("lane_activity") or {}
    gpu = state.get("gpu") or {}
    lane_name = activity.get("lane")
    snap["lane"] = lane_name
    try:
        free = int(gpu.get("free_mib"))
    except (TypeError, ValueError):
        free = None
    snap["free_mib"] = free
    snap["low_free_mib"] = free is not None and free < LOW_FREE_MIB

    running = [n for n, l in lanes.items()
               if isinstance(l, dict) and l.get("state") == "running"]
    snap["running"] = running

    # 1. A unit is up but nothing is serving.
    if not lane_name and running:
        if not await _lane_answers():
            findings.append({
                "key": "running-but-not-serving",
                "title": "GPU lane: unit up, nothing serving",
                "body": f"lanes running: {', '.join(running)}; "
                        "lane_activity has no active lane and the engine does "
                        "not answer. The model process is loaded but the API "
                        "is dead — a lane restart is needed."})

    # 2. Active lane degraded past the grace window (load-induced unless the
    #    dashboard's own probe also fails).
    now = time.time()
    store = _load()
    active = lanes.get(lane_name) if lane_name else None
    degraded = bool(isinstance(active, dict)
                    and active.get("health") == "degraded")
    degraded_since = store.get("degraded_since") or {}
    if degraded:
        first = degraded_since.setdefault(lane_name, now)
        age = now - first
        snap["degraded_age_s"] = int(age)
        if age >= DEGRADED_GRACE_S:
            if not await _lane_answers():
                findings.append({
                    "key": f"degraded:{lane_name}",
                    "title": f"GPU lane {lane_name} degraded",
                    "body": f"health check failing for {int(age // 60)} min and "
                            "the dashboard's own probe cannot reach the model. "
                            "Free VRAM or restart the lane."})
            else:
                snap["degraded_note"] = ("slow under load, engine still "
                                         "answers the dashboard probe")
    # Only the ACTIVE lane's degradation ages; recovery or a lane switch resets.
    degraded_since = {k: v for k, v in degraded_since.items()
                      if degraded and k == lane_name}

    # 3. A boot stuck past its own wait while VRAM is exhausted.
    known = store.get("starting_since") or {}
    starting_now = {}
    for name, lan in lanes.items():
        if not isinstance(lan, dict) or lan.get("state") != "starting":
            continue
        since = known.get(name) or now
        starting_now[name] = since
        wait = int(lan.get("boot_wait_s") or 0)
        snap["starting"].append({"lane": name, "age_s": int(now - since),
                                 "boot_wait_s": wait})
        if wait and now - since > wait + STUCK_GRACE_S and snap["low_free_mib"]:
            findings.append({
                "key": f"boot-stuck:{name}",
                "title": f"GPU lane {name} boot stuck",
                "body": f"starting for {int((now - since) // 60)} min "
                        f"(boot_wait {wait}s) with only {free} MiB free. "
                        "Something is holding VRAM — evict or stop it."})
    _save({**store, "starting_since": starting_now,
           "degraded_since": degraded_since, "ts": now})
    return findings, snap


STALE_FACTOR = 2     # a worker is stalled after this many missed intervals


def _watched_workers() -> list[tuple[str, int]]:
    """(runlog worker name, pass interval in seconds) of every alert-path
    worker that is switched on."""
    from app.api import edgar, news_alerts, price_alerts, rule_eval
    out = [("price_alerts", price_alerts.INTERVAL_S),
           ("rule_eval", rule_eval.INTERVAL_S),
           ("notify_outbox", notify.OUTBOX_POLL_S)]
    if os.getenv("NEWS_ALERTS_ENABLED", "0") == "1":
        out.append(("news_alerts", news_alerts.NEWS_INTERVAL_S))
    if os.getenv("EDGAR_ENABLED", "0") == "1":
        out.append(("edgar", edgar.POLL_INTERVAL_S))
    from app.api import paper_trader
    if paper_trader.enabled():
        out.append(("paper_trader", paper_trader.IDLE_PASS_S))   # = status()["interval_s"]
    from app.api import forecast_worker
    if forecast_worker.enabled():
        out.append(("forecast_worker", forecast_worker.POLL_S))
    from app.api import universe_news
    if universe_news.enabled():
        out.append(("universe_news", universe_news.WATCH_INTERVAL_S))   # = status()["interval_s"]: a pass can run minutes
    from app.api import data_feeds
    if data_feeds.enabled():
        out.append(("data_feeds", data_feeds.WATCH_INTERVAL_S))   # = status()["interval_s"]: a pass can run minutes
    return out


def stale_worker_findings(now: float | None = None) -> list[dict]:
    """A worker whose last SUCCESSFUL pass is older than STALE_FACTOR x its
    interval is stalled: a dead loop looks exactly like a quiet one until
    someone checks the timestamps. Idle passes count as successful (the
    in-process liveness stamp), so a quiet worker is not flagged."""
    now = time.time() if now is None else now
    findings: list[dict] = []
    for name, interval in _watched_workers():
        limit = STALE_FACTOR * interval
        last = runlog.last_success(name)
        if last is None:
            if runlog.uptime_s() <= limit + 120:
                continue          # still inside its first window after boot
            body = (f"no successful pass since the dashboard started "
                    f"{int(runlog.uptime_s() // 60)} min ago "
                    f"(interval {interval}s)")
        elif now - last > limit:
            body = (f"last successful pass {int((now - last) // 60)} min ago "
                    f"(interval {interval}s, limit {limit}s)")
        else:
            continue
        findings.append({"key": f"worker-stale:{name}",
                         "title": f"Worker stalled: {name}", "body": body})
    return findings


PAPER_HALT_GRACE_S = 4 * 3600     # halted this long -> the owner has not noticed
PAPER_MISMATCH_CODES = ("ledger_mismatch", "untracked_position")
PAPER_INVALID_RUN = 3             # consecutive scheduled decisions


def _paper_bucket_findings(bucket: str, now: float) -> list[dict]:
    """The three paper findings of ONE bucket. The daily bucket keeps the
    pre-phase-5 keys and titles; the others add ``:<bucket>`` / ``(<bucket>)``."""
    from app.api import paper_ledger, paper_pm
    sfx = "" if bucket == "daily" else f":{bucket}"
    tag = "" if bucket == "daily" else f" ({bucket})"
    findings: list[dict] = []
    halt = paper_ledger.read_halt(bucket)
    at = halt.get("at")
    if halt.get("halted") and isinstance(at, (int, float)) and now - at > PAPER_HALT_GRACE_S:
        findings.append({
            "key": f"paper-halted{sfx}", "title": f"Paper trading halted{tag}",
            "body": f"halted {int((now - at) // 3600)} h ago by {halt.get('by') or 'unknown'}; "
                    "resume it from the Agent card or leave it off on purpose."})
    snaps = sorted((r for r in paper_ledger.read_snapshots(bucket) if r.get("date")),
                   key=lambda r: r["date"])[-2:]
    if len(snaps) == 2 and all(
            set(PAPER_MISMATCH_CODES) & set(s.get("warnings") or []) for s in snaps):
        findings.append({
            "key": f"paper-ledger-mismatch{sfx}", "title": f"Paper ledger mismatch{tag}",
            "body": f"ledger_mismatch/untracked_position on the snapshots of "
                    f"{snaps[0]['date']} and {snaps[1]['date']}: the demo account and "
                    "the order ledger disagree (nothing is traded to fix it)."})
    sched = [r for r in paper_pm.read_decisions(bucket)
             if r.get("trigger") == "schedule" and not r.get("deferred")]
    last = sched[-PAPER_INVALID_RUN:]
    if len(last) == PAPER_INVALID_RUN and all(not r.get("valid") for r in last):
        findings.append({
            "key": f"paper-decisions-invalid{sfx}", "title": f"Paper decisions invalid{tag}",
            "body": f"the last {PAPER_INVALID_RUN} scheduled decisions "
                    f"({last[0].get('date')} .. {last[-1].get('date')}) were unusable: "
                    "check the PM playbook and the lane's output."})
    return findings


def paper_findings(now: float | None = None) -> list[dict]:
    """Paper-trading findings per enabled bucket, same shape as the others
    (deduped and re-alerted by ``run_pass``). Nothing when the paper worker is off."""
    from app.api import paper_eval, paper_trader
    if not paper_trader.enabled():
        return []
    now = time.time() if now is None else now
    findings: list[dict] = []
    for bucket in paper_eval.enabled_buckets():
        findings += _paper_bucket_findings(bucket, now)
    return findings


CLAUDE_TOKEN_LIFE_DAYS = 350      # setup-tokens live one year; warn 15 days ahead
_CLAUDE_FIX = ("run `claude setup-token`, replace CLAUDE_CODE_OAUTH_TOKEN with the new token, "
               "then restart the pod.")


def claude_findings(now: float | None = None) -> list[dict]:
    """Claude subscription adapter health (nothing when it is switched off). Never carries token bytes."""
    import datetime
    from app.api import claude_client
    if not claude_client.enabled():
        return []
    now = time.time() if now is None else now
    st = claude_client.status()
    findings: list[dict] = []
    last = st.get("lastError") or {}
    if last.get("error") == "auth":
        findings.append({
            "key": "claude-auth", "title": "Claude authentication failing",
            "body": "the last Claude call ended in an auth error (token expired or revoked): " + _CLAUDE_FIX})
    if st.get("tokenPresent") and st.get("tokenFormatOk") is False:
        findings.append({
            "key": "claude-token-format", "title": "Claude token has the wrong format",
            "body": "CLAUDE_CODE_OAUTH_TOKEN is not a setup-token (expected sk-ant-oat...), so no Claude "
                    "call is made: " + _CLAUDE_FIX})
    seen = st.get("tokenFirstSeen")
    if seen:
        try:
            first = datetime.date.fromisoformat(str(seen))
        except ValueError:
            first = None
        today = datetime.datetime.fromtimestamp(now).date()
        if first and first + datetime.timedelta(days=CLAUDE_TOKEN_LIFE_DAYS) <= today:
            findings.append({
                "key": "claude-token-expiring", "title": "Claude token expires soon",
                "body": f"the current token was first seen on {seen} and setup-tokens live one year: "
                        + _CLAUDE_FIX})
    return findings


EARNINGS_NULL_MAX = 0.25          # more than this share of equity candidates without an upcoming earnings date -> finding
STALE_PACKET_MAX = 0.10           # more than this share of the newest cohort left out for stale data -> finding


def earnings_findings(now: float | None = None) -> list[dict]:
    """`earnings-null-rate`: more than 25 % of the open universe's equity candidates lack ``nextEarnings`` (no
    observation yet, an empty calendar after the carry-forward, or a date already passed). Nothing while the news
    worker is off or no name was ever observed (a cold start is the worker-stale finding's business); clears by
    itself when the share drops back."""
    import datetime
    from app.api import paper_universe, universe_news
    if not universe_news.enabled():
        return []
    now = time.time() if now is None else now
    today = datetime.datetime.fromtimestamp(now, universe_news._BERLIN).date().isoformat()
    ids = [r["id"] for r in paper_universe.entries() if r["role"] == "candidate" and r["kind"] == "equity"]
    if not ids:
        return []
    obs = {pid: universe_news.next_earnings(pid, now) for pid in ids}
    if all(o is None for o in obs.values()):
        return []
    lacking = sorted(pid for pid, o in obs.items() if o is None or not o["date"] or o["date"] < today)
    share = len(lacking) / len(ids)
    if share <= EARNINGS_NULL_MAX:
        return []
    return [{"key": "earnings-null-rate", "title": "Earnings dates missing for many names",
             "body": f"{len(lacking)} of {len(ids)} equity candidates ({share * 100:.0f} %) have no upcoming earnings "
                     f"date (limit {EARNINGS_NULL_MAX * 100:.0f} %); their forecast packets carry nextEarnings MISSING. "
                     f"First: {', '.join(lacking[:5])}."}]


def stale_packet_findings(now: float | None = None) -> list[dict]:
    """`forecast-stale-packets`: more than 10 % of the newest forecast cohort's names were left out because their
    last close is older than the last completed session of their venue (a price feed that stopped, not a
    holiday: the check uses each venue's own calendar). Nothing while the forecaster is off."""
    from app.api import forecast_worker, forecasts
    if not forecast_worker.enabled():
        return []
    cohorts = [c for c in forecasts.read_cohorts().values() if isinstance(c.get("result"), dict) and c.get("ids")]
    if not cohorts:
        return []
    newest = max(cohorts, key=lambda c: (str(c.get("asOf") or ""), str(c.get("id"))))
    stale = newest["result"].get("stale") or {}
    share = len(stale) / len(newest["ids"])
    if share <= STALE_PACKET_MAX:
        return []
    return [{"key": "forecast-stale-packets", "title": "Forecast packets excluded for stale data",
             "body": f"cohort {newest['id']}: {len(stale)} of {len(newest['ids'])} names ({share * 100:.0f} %) have a "
                     f"last close older than their venue's last completed session and were not forecast "
                     f"(limit {STALE_PACKET_MAX * 100:.0f} %). First: {', '.join(sorted(stale)[:5])}."}]


IDENTITY_LINES = 6               # mismatches named in the finding body


def identity_findings(now: float | None = None) -> list[dict]:
    """ONE grouped finding for the registry identity audit (monthly OpenFIGI check of every ISIN against its Yahoo
    symbol and venue; ``data_feeds``). Open while the last audit has mismatches and is younger than 14 days. Report
    only: the registry is never changed automatically."""
    from app.api import data_feeds
    bad = data_feeds.identity_mismatches(now)
    if not bad:
        return []
    lines = [f"{m['id']} {m.get('kind')}: {m.get('detail')}" for m in bad[:IDENTITY_LINES]]
    more = f" (+{len(bad) - IDENTITY_LINES} more)" if len(bad) > IDENTITY_LINES else ""
    return [{"key": "identity-mismatch", "title": f"Registry identity: {len(bad)} mismatch(es)",
             "body": "OpenFIGI disagrees with the registry (nothing is changed automatically): "
                     + "; ".join(lines) + more}]


async def run_pass() -> dict:
    started = time.time()
    findings, snap = await collect()
    findings += stale_worker_findings(started)
    try:
        findings += paper_findings(started)
    except Exception as e:                      # never let paper state break the lane watch
        log.warning("paper findings failed: %s", e)
    try:
        findings += claude_findings(started)
    except Exception as e:                      # never let the Claude adapter break the lane watch
        log.warning("claude findings failed: %s", e)
    try:
        findings += earnings_findings(started)
    except Exception as e:                      # never let the data checks break the lane watch
        log.warning("earnings findings failed: %s", e)
    try:
        findings += stale_packet_findings(started)
    except Exception as e:
        log.warning("stale packet findings failed: %s", e)
    try:
        findings += identity_findings(started)
    except Exception as e:                      # never let the identity audit break the lane watch
        log.warning("identity findings failed: %s", e)
    # Load AFTER collect(): collect() persists degraded_since/starting_since,
    # and this function's own save would silently drop them if the store had
    # been read before that write.
    store = _load()
    now = started
    known = store.get("findings") or {}
    fired = []
    resolved = []
    for f in findings:
        prev = known.get(f["key"]) or {}
        last = prev.get("last_alert_ts") or 0
        if now - last >= REALERT_S:
            delivery = await notify.alert(
                "OPS", "ops_watch", f["title"], f["body"], priority=3,
                tags="warning", severity="warning")
            ok = delivery.consumed
            if delivery:
                fired.append(f["key"])
                _stats["alerts"] += 1
            known[f["key"]] = {"first_ts": prev.get("first_ts") or now,
                               "last_alert_ts": now if ok else last,
                               "count": (prev.get("count") or 0) + 1,
                               "title": f["title"]}
        else:
            known[f["key"]] = {**prev, "last_seen": now}
    # A finding that disappeared is cleared so it can alert again if it
    # returns. One that had alerted leaves an info "resolved" row; if that
    # row cannot be stored the entry stays and the next pass retries.
    keys = {f["key"] for f in findings}
    for k in [k for k in known if k not in keys]:
        gone = known[k]
        if gone.get("last_alert_ts"):
            if not notify.store("OPS", "ops_watch",
                                f"Resolved: {gone.get('title') or k}",
                                "No longer firing.", priority=1,
                                severity="info"):
                continue
            resolved.append(k)
        del known[k]
    _save({**store, "findings": known, "ts": now, "snapshot": snap})

    _stats.update(last_run=now, runs=_stats["runs"] + 1, source=snap["source"],
                  low_free_mib=bool(snap.get("low_free_mib")),
                  findings=len(findings))
    note = (f"{len(fired)} alert(s), {len(resolved)} resolved, "
            f"{len(findings)} open finding(s), "
            f"lane={snap.get('lane')}, source={snap['source']}"
            + (", low free VRAM" if snap.get("low_free_mib") else ""))
    runlog.record("ops_watch", True, time.time() - started, note)
    log.info("ops_watch: %s", note)
    return {"findings": [f["key"] for f in findings], "fired": fired,
            "resolved": resolved, "snapshot": snap}


async def _loop() -> None:
    log.info("ops_watch loop started (every %ss, %s)", INTERVAL_S, STATE_URL)
    await asyncio.sleep(20)          # let the lane/bridge settle after boot
    while True:
        try:
            await run_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["errors"] += 1
            log.exception("ops_watch pass failed")
        await asyncio.sleep(INTERVAL_S)


def start() -> None:
    global _task
    if os.getenv("OPS_WATCH_ENABLED", "1") != "1":
        log.info("ops_watch disabled (set OPS_WATCH_ENABLED=0)")
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
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
    store = _load()
    return {**_stats, "running": bool(_task),
            "enabled": os.getenv("OPS_WATCH_ENABLED", "1") == "1",
            "interval_s": INTERVAL_S,
            "open_findings": list((store.get("findings") or {}).keys()),
            "snapshot": store.get("snapshot") or {}}