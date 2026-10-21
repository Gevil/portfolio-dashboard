"""Forecaster experiment: arms, append-only forecast log, trial ledger, labels and grading
(docs/plans/forecaster.md, docs/research/plan-critique.md section 5, phase 6 contract section 3).

The question every arm answers: will this name beat the median of the universe's EUR returns over
the next 5 / 20 sessions (XETR calendar), close to close from the as-of close?

Arms (all on the same frozen packet, see ``forecast_packets``):
  R  constant 0.5.
  Q  code: cross-sectional rank of 12-1 momentum and (negated) 1-week return -> p by a fixed logistic map.
  A  ONE local sample (slot 0 of the pool), clipped.
  B  local pool of N=8 samples (temperature 0.8, fixed seeds, half asked "outperform", half "underperform"
     -> 1-p): trimmed logit mean (drop min/max), shrunk p = 0.5 + 0.3 (p - 0.5), clipped to [0.05, 0.95].
  D  one blind Opus call per name on the fixed 30-name subset (stored, chosen by a fixed hash of the ids).
  E  sigmoid(0.65 logit B + 0.35 logit D), only where B (local) and D (opus) both exist.

Fallback (frozen policy): local first. When no lane answers before the cohort deadline the WHOLE B row
of a name is replaced by one blind call, Opus then Sonnet, tagged ``role=fallback`` and ``source=opus|sonnet``,
else a ``hold`` row (``p`` null). The same Opus call serves arm D and the fallback when both are due. Sources are
never pooled: grading keys every cell by arm x source x horizon, cohorts with > 10 % fallback are reported apart.

State (all through ``jsonstore`` except the append-only log):
  data/forecast_log.jsonl    one row per sample + one per aggregate (never rewritten)
  data/trial_ledger.json     config hash per day, a row per change
  data/forecast_subset.json  the fixed D / comparison subset
  data/forecast_cohorts.json cohort records (worker state)
  data/forecast_labels.json  realised labels, written once per (as-of date, horizon) when matured
"""
import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import pathlib
import statistics
import time

from app.api import calendars, forecast_packets as packets, jsonstore, lane_client, paper_metrics, paper_universe, scoreboard, ta_pipeline, textsafe

log = logging.getLogger(__name__)

ARMS = ("R", "Q", "A", "B", "D", "E")
HORIZONS = (5, 20)
MIC = "XETR"
N_SAMPLES = 8
TEMPERATURE = 0.8
SEEDS = (1103, 2207, 3301, 4409, 5501, 6607, 7703, 8803)     # fixed per slot; see SEED_SENT
SEED_SENT = False          # lane_client.chat has no seed parameter: seeds are logged, not transmitted
KAPPA = 0.3
CLIP = (0.05, 0.95)
BLEND_B, BLEND_D = 0.65, 0.35
MIN_VALID = 5              # valid samples a pool needs (the trim drops two)
Q_SLOPE = 1.0              # p = sigmoid(Q_SLOPE * (mean percentile rank - 0.5)), i.e. 0.38 .. 0.62
SUBSET_SIZE = 30
SUBSET_SALT = "forecast-subset-v1"
LOCAL, CODE, BLEND, NONE = "local", "code", "blend", "none"
MAX_TOKENS = 600
LANE_TIMEOUT_S = float(os.getenv("FORECAST_CALL_TIMEOUT_S", "180"))
CLAUDE_TIMEOUT_S = 240.0
CLAUDE_MAX_TOKENS = 800
BACKOFF_S = (30.0, 60.0, 120.0, 300.0, 600.0)     # lane busy: retry with growing waits until the deadline
MIXED_SHARE = 0.10         # cohorts with more fallback than this are reported separately
UNDERPOWERED_SHARE = 0.25
MIN_PAIRS = 30
READOUT_WEEKS = {4: "sanity", 8: "interim", 12: "verdict"}
LABEL_MIN_SHARE = 0.8
STALE_ENTRY_DAYS = 5
BOOT_N = 2000
GRADED_ROLES = ("scheduled", "fallback")
STALE_ROLE = "excluded"                 # status rows of names left out for stale data: never graded
PLAYBOOK = ta_pipeline.PLAYBOOK_DIR / "forecaster.md"

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
LOG_FILE = DATA_DIR / "forecast_log.jsonl"
LEDGER_FILE = DATA_DIR / "trial_ledger.json"
SUBSET_FILE = DATA_DIR / "forecast_subset.json"
COHORTS_FILE = DATA_DIR / "forecast_cohorts.json"
LABELS_FILE = DATA_DIR / "forecast_labels.json"

QUESTION = ("Will this name {verb} the median of the universe's EUR returns over the next {hz} sessions "
            "(XETR calendar), close to close from the as-of close?")
VERBS = {"outperform": "beat", "underperform": "fall short of"}
COMPARISONS = (("B-A", "B", "A"), ("B-Q", "B", "Q"), ("D-B", "D", "B"), ("E-B", "E", "B"))
COMPARE_SOURCE = {"A": LOCAL, "B": LOCAL, "Q": CODE, "D": "opus", "E": BLEND}


class ForecastLogError(Exception):
    """The forecast log could not be written: the run stops rather than lose rows."""


# ------------------------------------------------------------------ seams (tests patch these)

def _now() -> float:
    return time.time()


def _mono() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _claude():
    from app.api import claude_client       # Slice C module, imported lazily
    return claude_client


async def _closes_of(pid: str) -> list:
    return await paper_universe.daily_closes(pid, "2y")


# ------------------------------------------------------------------ math

def logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def clip(p: float) -> float:
    return min(max(float(p), CLIP[0]), CLIP[1])


def pool_b(ps: list[float]) -> dict | None:
    """Arm B from the sample probabilities of one horizon (already flipped to p(outperform)):
    trimmed logit mean (min and max dropped), shrunk towards 0.5 by KAPPA, clipped. None below MIN_VALID."""
    if len(ps) < MIN_VALID:
        return None
    xs = sorted(logit(p) for p in ps)[1:-1]
    raw = sigmoid(sum(xs) / len(xs))
    return {"p": clip(0.5 + KAPPA * (raw - 0.5)), "raw": raw, "n": len(ps), "spread": statistics.pstdev(ps)}


def blend_e(p_b: float, p_d: float) -> float:
    return clip(sigmoid(BLEND_B * logit(p_b) + BLEND_D * logit(p_d)))


def _pct_ranks(values: dict[str, float]) -> dict[str, float]:
    """(average rank - 0.5) / n in (0, 1); ties share the average of their positions."""
    order = sorted(values, key=lambda k: (values[k], k))
    n = len(order)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = ((i + j) / 2 + 1 - 0.5) / n
        i = j + 1
    return out


def q_probabilities(pkts: dict[str, dict]) -> dict[str, float]:
    """Arm Q over the packets of one run: the mean of the percentile rank of 12-1 momentum and of the
    NEGATED 1-week return (short-term reversal), through a fixed logistic map. An id with one of the two
    features uses that one; with neither it is absent. The same p serves every horizon."""
    mom, rev = {}, {}
    for pid, pk in pkts.items():
        m, w = pk.get("momentum12_1"), pk.get("week1")
        if isinstance(m, dict) and packets._num(m.get("pct")) is not None:
            mom[pid] = float(m["pct"])
        if isinstance(w, dict) and packets._num(w.get("pct")) is not None:
            rev[pid] = -float(w["pct"])
    rm, rr = _pct_ranks(mom), _pct_ranks(rev)
    out = {}
    for pid in pkts:
        parts = [r[pid] for r in (rm, rr) if pid in r]
        if parts:
            out[pid] = clip(sigmoid(Q_SLOPE * (sum(parts) / len(parts) - 0.5)))
    return out


# ------------------------------------------------------------------ cohorts and subset

def weekly_id(as_of: datetime.date) -> str:
    return f"w{as_of.isoformat()}"


def daily_id(as_of: datetime.date) -> str:
    return f"d{as_of.isoformat()}"


def cohort_role(cohort: str) -> str:
    """w<date> weekly (scheduled), d<date> daily (analysed separately), anything else adhoc."""
    return {"w": "scheduled", "d": "daily"}.get((cohort or "")[:1], "adhoc") if _is_dated(cohort) else "adhoc"


def _is_dated(cohort: str) -> bool:
    try:
        datetime.date.fromisoformat((cohort or "")[1:])
        return True
    except ValueError:
        return False


def universe_hash(ids) -> str:
    return hashlib.sha256("\n".join(sorted(str(i) for i in ids)).encode()).hexdigest()


def stored_subset() -> list[str]:
    doc = jsonstore.load(SUBSET_FILE, None)
    ids = doc.get("ids") if isinstance(doc, dict) else None
    return [str(i) for i in ids] if isinstance(ids, list) and ids else []


def ensure_subset(candidates: list[str]) -> list[str]:
    """The fixed D / comparison subset: the stored one, else the first SUBSET_SIZE candidates by
    sha256(salt:id), stored for good (a later change of the universe never reshuffles it)."""
    have = stored_subset()
    if have:
        return have
    ids = sorted({str(c).upper() for c in candidates},
                 key=lambda i: (hashlib.sha256(f"{SUBSET_SALT}:{i}".encode()).hexdigest(), i))[:SUBSET_SIZE]
    if ids and not jsonstore.save(SUBSET_FILE, {"salt": SUBSET_SALT, "size": SUBSET_SIZE, "ids": ids,
                                                "universeHash": universe_hash(candidates), "createdAt": _now()}):
        log.warning("forecasts: could not store the subset")
    return ids


def read_cohorts() -> dict[str, dict]:
    doc = jsonstore.load(COHORTS_FILE, {})
    rows = doc.get("cohorts") if isinstance(doc, dict) else None
    return {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}


def put_cohort(rec: dict) -> bool:
    rows = read_cohorts()
    rows[rec["id"]] = rec
    return jsonstore.save(COHORTS_FILE, {"cohorts": rows})


# ------------------------------------------------------------------ forecast log (append-only JSONL)

_log_cache: dict = {"sig": None, "rows": [], "agg": None}


def _sig():
    try:
        st = LOG_FILE.stat()
    except OSError:
        return None
    return (str(LOG_FILE), st.st_size, st.st_mtime_ns)


def read_log() -> list[dict]:
    """Every row, oldest first (corrupt lines skipped). The list is cached while the file is
    unchanged: callers must not mutate it."""
    sig = _sig()
    if sig is None:
        return []
    if _log_cache["sig"] == sig:
        return _log_cache["rows"]
    rows = []
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as e:
        log.warning("forecast log unreadable: %s", e)
        return []
    _log_cache.update(sig=sig, rows=rows, agg=None)
    return rows


def append_rows(rows: list[dict]) -> bool:
    """Append rows (serialised first, fsynced). True on success; a failure writes nothing."""
    if not rows:
        return True
    try:
        payload = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for r in rows)
    except (TypeError, ValueError) as e:
        log.warning("forecast log: serialise failed: %s", e)
        return False
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        lead = ""
        if LOG_FILE.exists() and LOG_FILE.stat().st_size:
            with open(LOG_FILE, "rb") as fh:          # a crash mid-line must not glue two rows together
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    lead = "\n"
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(lead + payload)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as e:
        log.warning("forecast log: write failed: %s", e)
        return False


def _agg_index() -> dict:
    """{(id, arm, horizon): newest aggregate row with a probability} over the log."""
    rows = read_log()
    if _log_cache["agg"] is None or _log_cache["rows"] is not rows:
        idx: dict = {}
        for r in rows:
            if r.get("kind") != "aggregate" or r.get("p") is None:
                continue
            key = (r.get("id"), r.get("arm"), r.get("horizon"))
            old = idx.get(key)
            if old is None or (r.get("asOf") or "", r.get("createdAt") or 0) >= (old.get("asOf") or "", old.get("createdAt") or 0):
                idx[key] = r
        _log_cache["agg"] = idx
    return _log_cache["agg"]


def latest(ids: list[str], horizon: int = 20, *, arm: str = "B") -> dict[str, dict]:
    """The newest forecast of ``arm`` per id (ids without one are absent):
    {id: {"p", "arm", "source", "asOf", "n", "spread", "packetHash", "role", "cohort"}}. The source tag is
    part of the answer: a fallback p is another instrument than a local pool and must not be mixed."""
    idx = _agg_index()
    out = {}
    for pid in ids or []:
        pid = str(pid).strip().upper()
        r = idx.get((pid, arm, horizon))
        if r is not None:
            out[pid] = {"p": r["p"], "arm": r["arm"], "source": r.get("source"), "asOf": r.get("asOf"),
                        "n": r.get("n"), "spread": r.get("spread"), "packetHash": r.get("packetHash"),
                        "role": r.get("role"), "cohort": r.get("cohort")}
    return out


class _State:
    """The rows of one cohort, indexed for resuming a run."""

    def __init__(self, cohort: str):
        self.samples: dict[tuple, dict] = {}       # (id, arm, slot) -> {horizon: row}
        self.agg: dict[tuple, dict] = {}           # (id, arm, horizon, source) -> row
        for r in read_log():
            if r.get("cohort") == cohort:
                self.add(r)

    def add(self, r: dict) -> None:
        if r.get("kind") == "sample":
            self.samples.setdefault((r["id"], r["arm"], r.get("slot")), {})[r.get("horizon")] = r
        elif r.get("kind") == "aggregate":
            self.agg[(r["id"], r["arm"], r.get("horizon"), r.get("source"))] = r

    def has_agg(self, pid: str, arm: str, h: int, source: str | None = None) -> bool:
        if source is not None:
            return (pid, arm, h, source) in self.agg
        return any(k[:3] == (pid, arm, h) for k in self.agg)

    def agg_p(self, pid: str, arm: str, h: int, source: str) -> float | None:
        r = self.agg.get((pid, arm, h, source))
        return None if r is None else r.get("p")

    def slot_ps(self, pid: str, h: int) -> list[float]:
        return [rows[h]["p"] for (i, arm, _s), rows in sorted(self.samples.items(), key=lambda kv: str(kv[0]))
                if i == pid and arm == "B" and h in rows and rows[h].get("p") is not None
                and rows[h].get("role") != "fallback" and rows[h].get("source") == LOCAL]


# ------------------------------------------------------------------ trial ledger

def _prompt_hashes() -> dict:
    def h(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        playbook = PLAYBOOK.read_text(encoding="utf-8")
    except OSError:
        playbook = ""
    return {"forecaster": h(playbook), "untrustedClause": h(ta_pipeline.UNTRUSTED_CLAUSE),
            "question": h(json.dumps([QUESTION, VERBS], sort_keys=True))}


def config(candidates: list[str] | None = None) -> dict:
    """Everything whose change makes a new trial: prompt hashes, N, kappa, blend, arms, universe, subset."""
    cand = candidates if candidates is not None else paper_universe.ids("candidate")
    return {"schema": 1, "arms": list(ARMS), "horizons": list(HORIZONS), "nSamples": N_SAMPLES,
            "temperature": TEMPERATURE, "seeds": list(SEEDS), "seedSent": SEED_SENT, "kappa": KAPPA,
            "clip": list(CLIP), "blend": {"B": BLEND_B, "D": BLEND_D}, "minValid": MIN_VALID, "qSlope": Q_SLOPE,
            "subsetSize": SUBSET_SIZE, "subsetHash": universe_hash(stored_subset()),
            "universeHash": universe_hash(cand), "prompts": _prompt_hashes(),
            "packetSchema": packets.SCHEMA_VERSION, "packetMaxChars": packets.PACKET_MAX_CHARS,
            "maxTokens": MAX_TOKENS, "fallback": "lane -> opus -> sonnet -> hold",
            "label": "beat the universe median EUR return, close to close, XETR sessions"}


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_config(now: float | None = None, candidates: list[str] | None = None) -> dict:
    """Record today's config hash; append a ledger row when it differs from the last row's (or none
    exists). -> {"hash", "changed", "day"}. Any change is a trial: the experiment freezes the config."""
    now = _now() if now is None else now
    day = datetime.date.fromtimestamp(now).isoformat()
    cfg = config(candidates)
    h = config_hash(cfg)
    doc = jsonstore.load(LEDGER_FILE, {})
    doc = doc if isinstance(doc, dict) else {}
    rows = list(doc["rows"]) if isinstance(doc.get("rows"), list) else []
    daily = dict(doc["daily"]) if isinstance(doc.get("daily"), dict) else {}
    last = rows[-1] if rows else None
    changed = last is None or last.get("hash") != h
    if changed:
        prev = (last or {}).get("config") or {}
        keys = sorted(k for k in set(cfg) | set(prev) if cfg.get(k) != prev.get(k)) if last else ["initial"]
        rows.append({"day": day, "hash": h, "config": cfg, "changedKeys": keys, "createdAt": now})
    new_day = daily.get(day) != h
    daily[day] = h
    for old in sorted(daily)[:-400]:
        del daily[old]
    if (changed or new_day) and not jsonstore.save(LEDGER_FILE, {"rows": rows, "daily": daily}):
        log.warning("forecasts: trial ledger not saved")
    return {"hash": h, "changed": changed, "day": day}


def ledger() -> dict:
    doc = jsonstore.load(LEDGER_FILE, {})
    return doc if isinstance(doc, dict) else {}


# ------------------------------------------------------------------ prompts and parsing

_playbook_cache: dict = {"sig": None, "text": ""}


def system_prompt() -> str:
    """Playbook + the untrusted-data clause. Raises OSError when the playbook is missing or empty."""
    st = PLAYBOOK.stat()
    sig = (st.st_mtime_ns, st.st_size)
    if _playbook_cache["sig"] != sig:
        text = PLAYBOOK.read_text(encoding="utf-8")
        if not text.strip():
            raise OSError("forecaster playbook is empty")
        _playbook_cache.update(sig=sig, text=text)
    return f"{_playbook_cache['text']}\n\n---\n\n{ta_pipeline.UNTRUSTED_CLAUSE}"


def user_prompt(packet: dict, direction: str, horizons) -> str:
    hz = " / ".join(str(h) for h in horizons)
    keys = ", ".join(f"p_{direction}_{h}d" for h in horizons)
    return (f"{QUESTION.format(verb=VERBS[direction], hz=hz)}\n"
            f"Direction of this question: {direction.upper()}. Answer with the keys {keys} and crux.\n\n"
            f"{packets.render(packet)}")


def _prob(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v) if 0.0 < v < 1.0 else None


def parse_forecast(content, direction: str, horizons) -> dict | None:
    """{"p": {h: p(outperform)}, "pAsked": {h: p as answered}, "crux"} or None. The underperform
    question is answered with ``p_underperform_<h>d`` and flipped (1 - p)."""
    obj = ta_pipeline._extract_json(content) if isinstance(content, str) else None
    if not isinstance(obj, dict):
        return None
    asked, p = {}, {}
    for h in horizons:
        v = _prob(obj.get(f"p_{direction}_{h}d"))
        if v is not None:
            asked[h] = v
            p[h] = v if direction == "outperform" else 1.0 - v
    if not p:
        return None
    return {"p": p, "pAsked": asked, "crux": textsafe.clean_text(obj.get("crux"), 200)}


def slot_direction(slot: int) -> str:
    """Half the pool is asked OUTPERFORM (even slots), half UNDERPERFORM."""
    return "outperform" if slot % 2 == 0 else "underperform"


# ------------------------------------------------------------------ the run

def _round(p):
    return None if p is None else round(float(p), 6)


def _emit(st: _State, rows: list[dict]) -> None:
    if not append_rows(rows):
        raise ForecastLogError("forecast log not writable")
    for r in rows:
        st.add(r)


async def _ask_local(packet: dict, direction: str, horizons) -> dict:
    """One local sample -> {"parsed"|None, "error"|None ('lane' envelope error), "lane", "model", "latencyS"}."""
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": user_prompt(packet, direction, horizons)}]
    t0 = _mono()
    res = await lane_client.chat(messages, max_tokens=MAX_TOKENS, temperature=TEMPERATURE, json_mode=True,
                                 enable_thinking=False, autonomous=True, purpose="forecast", timeout=LANE_TIMEOUT_S)
    latency = round(_mono() - t0, 2)
    if not isinstance(res, dict) or res.get("error"):
        err = str((res or {}).get("error") or "bad_reply") if isinstance(res, dict) else "bad_reply"
        return {"parsed": None, "error": err, "lane": None, "model": None, "latencyS": latency}
    return {"parsed": parse_forecast(res.get("content"), direction, horizons), "error": None,
            "lane": res.get("lane"), "model": res.get("model"), "latencyS": latency}


async def _ask_blind(packet: dict, model: str, horizons) -> dict:
    """One blind Opus / Sonnet call -> {"parsed", "error", "model", "source", "latencyS"}."""
    env = await _claude().complete(system_prompt(), user_prompt(packet, "outperform", horizons), model=model,
                                   max_tokens=CLAUDE_MAX_TOKENS, timeout_s=CLAUDE_TIMEOUT_S, purpose="forecast")
    if not isinstance(env, dict) or env.get("error"):
        return {"parsed": None, "error": str((env or {}).get("error") or "bad_reply") if isinstance(env, dict) else "bad_reply",
                "model": model, "source": model, "latencyS": None}
    src = env.get("source") if env.get("source") in ("opus", "sonnet") else model
    return {"parsed": parse_forecast(env.get("content"), "outperform", horizons), "error": None,
            "model": env.get("model") or model, "source": src, "latencyS": env.get("latencyS")}


def _base(cohort: str, role: str, as_of: str, pid: str, packet_hash: str) -> dict:
    return {"cohort": cohort, "role": role, "asOf": as_of, "id": pid, "packetHash": packet_hash,
            "createdAt": round(_now(), 3)}


def _agg(base: dict, arm: str, source: str, h: int, p, *, model=None, n=None, spread=None, role=None, **extra) -> dict:
    row = {**base, "kind": "aggregate", "arm": arm, "source": source, "horizon": h, "p": _round(p), "model": model,
           "n": n, "spread": _round(spread), "seed": None, "latencyS": None, **extra}
    if role:
        row["role"] = role
    return row


async def run(ids, *, arms=("B",), horizons=HORIZONS, cohort: str | None = None, role: str | None = None,
              as_of: datetime.date | None = None, deadline: float | None = None, priority=(),
              progress: dict | None = None) -> dict:
    """Run ``arms`` for ``ids`` in ``cohort`` (resumable: rows already in the log are never redone).

    ``deadline`` (epoch) switches on the cohort policy: a lane that does not answer is retried with
    growing waits until the deadline, then the unfinished names fall back (Opus, then Sonnet, then hold).
    Without a deadline a failing lane ends the local part of the run at once (names stay pending).
    ``priority`` ids go first and take the Opus quota first. Raises ValueError for unknown arms or
    horizons and :class:`ForecastLogError` when the log cannot be written."""
    arms = tuple(dict.fromkeys(arms))
    horizons = tuple(sorted({int(h) for h in horizons}))
    if not arms or any(a not in ARMS for a in arms):
        raise ValueError(f"unknown arm in {arms}")
    if not horizons or any(h not in HORIZONS for h in horizons):
        raise ValueError(f"horizons must be within {HORIZONS}")
    now = _now()
    as_of = as_of or packets.last_completed_session(now)
    as_of_iso = as_of.isoformat()
    cohort = cohort or f"adhoc-{as_of_iso}"
    role = role or cohort_role(cohort)
    prog = progress if progress is not None else {}
    out: dict = {"cohort": cohort, "role": role, "asOf": as_of_iso, "arms": list(arms), "horizons": list(horizons),
                 "unknown": [], "packetFailed": {}, "stale": {}, "samples": {"asked": 0, "valid": 0, "invalid": 0, "laneErrors": 0},
                 "fallback": {"opus": [], "sonnet": [], "hold": []}, "d": {"done": [], "failed": []},
                 "invalidPool": [], "pending": [], "complete": False}

    want, seen = [], set()
    pri = [str(p).strip().upper() for p in priority]
    for pid in pri + [str(i).strip().upper() for i in ids or []]:
        if pid in seen:
            continue
        seen.add(pid)
        (want if paper_universe.get(pid) else out["unknown"]).append(pid)
    # priority ids that were not in `ids` still run (they are the caller's most urgent names)
    prog.update(total=len(want), done=0, phase="packets")

    st = _State(cohort)
    pk: dict[str, dict] = {}
    ctx = None
    for pid in want:
        try:
            doc = packets.load(cohort, pid)
            if doc is None:
                ctx = ctx or await packets.context(as_of)
                doc = packets.freeze(cohort, pid, await packets.build(pid, as_of, ctx))
            pk[pid] = doc
        except asyncio.CancelledError:
            raise
        except Exception as e:
            out["packetFailed"][pid] = f"{type(e).__name__}: {str(e)[:100]}"
            log.warning("forecasts: packet %s/%s failed: %s", cohort, pid, type(e).__name__)
    for pid in want:
        if pid in pk and packets.is_stale(pk[pid]["packet"]):
            ds = pk[pid]["packet"]["dataStatus"]
            out["stale"][pid] = (f"last close {ds.get('lastClose')} is older than the last completed {ds.get('venue')} "
                                 f"session {ds.get('expectedSession')}")
            log.warning("forecasts: %s/%s excluded: %s", cohort, pid, out["stale"][pid])
    ids_ok = [p for p in want if p in pk and p not in out["stale"]]

    def base(pid):
        return _base(cohort, role, as_of_iso, pid, pk[pid]["sha256"])

    # a stale name gets one status row per horizon (p null, role "excluded": never graded, never a hold forecast)
    _emit(st, [_agg(base(pid), "B", NONE, h, None, role=STALE_ROLE, error=packets.STALE_STATUS, status=packets.STALE_STATUS)
               for pid in out["stale"] for h in horizons if not st.has_agg(pid, "B", h, NONE)])

    # ---- code arms
    prog["phase"] = "code arms"
    rows = []
    if "R" in arms:
        for pid in ids_ok:
            rows += [_agg(base(pid), "R", CODE, h, 0.5, n=0) for h in horizons if not st.has_agg(pid, "R", h, CODE)]
    if "Q" in arms:
        q = q_probabilities({pid: pk[pid]["packet"] for pid in ids_ok})
        for pid in ids_ok:
            if pid in q:
                rows += [_agg(base(pid), "Q", CODE, h, q[pid], n=0) for h in horizons if not st.has_agg(pid, "Q", h, CODE)]
    _emit(st, rows)

    # ---- local pool (arms B and A)
    slots = list(range(N_SAMPLES)) if "B" in arms else ([0] if "A" in arms else [])
    blocked: list[str] = []
    expired = False
    prog["phase"] = "local samples"
    for idx, pid in enumerate(ids_ok):
        if not slots:
            break
        if "B" in arms and all(st.has_agg(pid, "B", h) for h in horizons):
            _aggregate_local(st, base(pid), pid, arms, horizons, slots, out, _emit)     # adds a missing A only
            prog["done"] = idx + 1
            continue
        stop = False
        for slot in slots:
            if (pid, "B", slot) in st.samples:
                continue
            direction, attempt = slot_direction(slot), 0
            while True:
                res = await _ask_local(pk[pid]["packet"], direction, horizons)
                out["samples"]["asked"] += 1
                if res["error"] is None:
                    break
                out["samples"]["laneErrors"] += 1
                if deadline is None or _now() >= deadline:
                    expired = deadline is not None
                    stop = True
                    break
                prog["laneWaiting"] = res["error"]
                await _sleep(min(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)], max(1.0, deadline - _now())))
                attempt += 1
            if stop:
                break
            prog.pop("laneWaiting", None)
            parsed = res["parsed"]
            out["samples"]["valid" if parsed else "invalid"] += 1
            _emit(st, [{**base(pid), "kind": "sample", "arm": "B", "source": LOCAL, "slot": slot, "direction": direction,
                        "model": res["model"], "lane": res["lane"], "seed": SEEDS[slot], "seedSent": SEED_SENT,
                        "horizon": h, "p": _round(parsed["p"].get(h)) if parsed else None,
                        "pAsked": _round(parsed["pAsked"].get(h)) if parsed else None,
                        "crux": parsed["crux"] if parsed else None, "error": None if parsed else "unparseable",
                        "latencyS": res["latencyS"]} for h in horizons])
        if stop:
            blocked = ids_ok[idx:]
            break
        prog["done"] = idx + 1
        _aggregate_local(st, base(pid), pid, arms, horizons, slots, out, _emit)

    # ---- blind calls: fallback for names the lane never served, arm D on the subset
    d_ids: list[str] = []
    if "D" in arms or "E" in arms:
        sub = set(ensure_subset(paper_universe.ids("candidate")))
        d_ids = [p for p in ids_ok if p in sub]
    fb_ids = [p for p in blocked if "B" in arms and expired]
    blind_cache: dict[str, dict] = {}
    cs = {"dead": False, "opus_blocked": False}
    pr_set = set(pri)
    worklist = [p for p in fb_ids if p in pr_set] + d_ids + fb_ids
    prog["phase"] = "blind calls"
    for pid in dict.fromkeys(worklist):
        need_d = pid in d_ids and "D" in arms and not st.has_agg(pid, "D", horizons[0])
        need_fb = pid in fb_ids and not all(st.has_agg(pid, "B", h) for h in horizons)
        if not need_d and not need_fb:
            continue
        res = await _blind_for(pk[pid]["packet"], horizons, cs, need_fb)
        if res is None:
            if need_fb:
                _emit(st, [_agg(base(pid), "B", NONE, h, None, role="fallback", error="hold_default") for h in horizons])
                out["fallback"]["hold"].append(pid)
            if need_d and deadline is not None and _now() >= deadline:
                _emit(st, [_agg(base(pid), "D", "opus", h, None, error="opus_unavailable") for h in horizons])
                out["d"]["failed"].append(pid)
            continue
        parsed, src, model = res["parsed"], res["source"], res["model"]
        arm_of_call = "D" if need_d else "B"
        _emit(st, [{**base(pid), "kind": "sample", "arm": arm_of_call, "source": src, "slot": None, "direction": "outperform",
                    "model": model, "lane": None, "seed": None, "horizon": h, "p": _round(parsed["p"].get(h)),
                    "pAsked": _round(parsed["pAsked"].get(h)), "crux": parsed["crux"], "error": None,
                    "latencyS": res["latencyS"], "role": "fallback" if arm_of_call == "B" else role} for h in horizons])
        if need_d and src == "opus":
            _emit(st, [_agg(base(pid), "D", "opus", h, clip(parsed["p"][h]), model=model, n=1) for h in horizons if h in parsed["p"]])
            out["d"]["done"].append(pid)
        elif need_d:
            out["d"]["failed"].append(pid)
        if need_fb:
            _emit(st, [_agg(base(pid), "B", src, h, clip(parsed["p"][h]), model=model, n=1, role="fallback",
                            reused="D" if need_d else None) for h in horizons if h in parsed["p"]])
            out["fallback"][src].append(pid)

    # ---- E: blend where B (local) and D (opus) both exist
    if "E" in arms:
        rows = []
        for pid in ids_ok:
            for h in horizons:
                pb, pd = st.agg_p(pid, "B", h, LOCAL), st.agg_p(pid, "D", h, "opus")
                if pb is not None and pd is not None and not st.has_agg(pid, "E", h, BLEND):
                    rows.append(_agg(base(pid), "E", BLEND, h, blend_e(pb, pd), n=2))
        _emit(st, rows)

    # ---- what is left
    for pid in ids_ok:
        if slots and "B" in arms and not all(st.has_agg(pid, "B", h) for h in horizons):
            out["pending"].append(pid)
        elif slots and "A" in arms and "B" not in arms and not all(st.has_agg(pid, "A", h) for h in horizons):
            out["pending"].append(pid)
    out["pendingD"] = [p for p in d_ids if "D" in arms and not st.has_agg(p, "D", horizons[0])]
    out["complete"] = not out["pending"] and not out["pendingD"] and not out["packetFailed"]
    prog["phase"] = "done" if out["complete"] else "partial"
    return out


def _aggregate_local(st, base, pid, arms, horizons, slots, out, emit) -> None:
    """A and B rows of one name from its sample rows. A pool with fewer than MIN_VALID valid samples after
    every slot was asked is final with ``p`` null (the lane worked, the answers did not: no fallback)."""
    rows = []
    for h in horizons:
        if "A" in arms and not st.has_agg(pid, "A", h, LOCAL):
            s0 = st.samples.get((pid, "B", 0), {}).get(h)
            if s0 is not None and s0.get("p") is not None:
                rows.append(_agg(base, "A", LOCAL, h, clip(s0["p"]), model=s0.get("model"), n=1))
        if "B" in arms and not st.has_agg(pid, "B", h):
            asked = sum(1 for s in slots if (pid, "B", s) in st.samples)
            pooled = pool_b(st.slot_ps(pid, h))
            if pooled is not None:
                rows.append(_agg(base, "B", LOCAL, h, pooled["p"], n=pooled["n"], spread=pooled["spread"],
                                 raw=_round(pooled["raw"]), model=_model_of(st, pid)))
            elif asked == len(slots):
                rows.append(_agg(base, "B", LOCAL, h, None, n=len(st.slot_ps(pid, h)), error="too_few_valid_samples"))
                if pid not in out["invalidPool"]:
                    out["invalidPool"].append(pid)
    emit(st, rows)


def _model_of(st: _State, pid: str) -> str | None:
    for (i, arm, _s), rows in st.samples.items():
        if i == pid and arm == "B":
            for r in rows.values():
                if r.get("model"):
                    return r["model"]
    return None


async def _blind_for(packet: dict, horizons, cs: dict, allow_sonnet: bool) -> dict | None:
    """Opus first; Sonnet only for a fallback and only when Opus is out of quota or unusable.
    None when no model produced a usable answer. ``cs`` remembers a blocked / dead Claude for the run."""
    c = _claude()
    for model in ("opus", "sonnet"):
        if model == "sonnet" and not allow_sonnet:
            break
        if cs["dead"] or (model == "opus" and cs["opus_blocked"]):
            continue
        try:
            if not c.available("forecast"):
                cs["dead"] = True
                break
        except Exception:
            cs["dead"] = True
            break
        res = await _ask_blind(packet, model, horizons)
        err = res["error"]
        if err in ("quota", "limit"):
            if model == "opus":
                cs["opus_blocked"] = True
            else:
                cs["dead"] = True
            continue
        if err in ("disabled", "auth", "cli"):
            cs["dead"] = True
            break
        if res["parsed"] is not None and all(h in res["parsed"]["p"] for h in horizons):
            return res
    return None


async def forecast_ids(ids: list[str], *, arms=("B",), horizons=(5, 20), cohort: str | None = None) -> dict:
    """Contract section 1: run ``arms`` for ``ids`` now and log every row. No deadline: a failing lane ends
    the local part of the run and the names stay pending. -> the run summary (see ``run``); read the
    probabilities back with :func:`latest`."""
    return await run(ids, arms=arms, horizons=horizons, cohort=cohort)


# ------------------------------------------------------------------ labels

def read_labels() -> dict:
    doc = jsonstore.load(LABELS_FILE, {})
    rows = doc.get("labels") if isinstance(doc, dict) else None
    return rows if isinstance(rows, dict) else {}


def label_universe(as_of_iso: str, cohorts: dict[str, dict] | None = None) -> list[str]:
    """The names the median is taken over: the ids of the newest weekly cohort on/before the as-of, else
    today's candidates. Pinned per as-of date by the cohort record, so a later universe change never moves it."""
    cohorts = read_cohorts() if cohorts is None else cohorts
    weekly = [c for c in cohorts.values() if c.get("kind") == "weekly" and str(c.get("asOf")) <= as_of_iso and c.get("ids")]
    if weekly:
        return list(max(weekly, key=lambda c: c["asOf"])["ids"])
    return paper_universe.ids("candidate")


def compute_label(as_of: datetime.date, h: int, universe: list[str], closes: dict[str, list]) -> dict | None:
    """Label of every name of ``universe``: 1 when its EUR close-to-close return from the as-of close to the
    close ``h`` XETR sessions later beats the universe median, 0 when below; a name exactly at the median is
    left out. None while fewer than LABEL_MIN_SHARE of the universe has both prices."""
    target = calendars.add_sessions(MIC, as_of, h).isoformat()
    as_of_iso = as_of.isoformat()
    floor = (as_of - datetime.timedelta(days=STALE_ENTRY_DAYS)).isoformat()
    rets: dict[str, float] = {}
    for pid in universe:
        series = packets.clean_closes(closes.get(pid), "9999-12-31")
        if not series or series[-1][0] < target:
            continue
        entry = [(d, c) for d, c in series if d <= as_of_iso]
        exit_ = [(d, c) for d, c in series if d <= target]
        if not entry or entry[-1][0] < floor or not exit_:
            continue
        rets[pid] = (exit_[-1][1] / entry[-1][1] - 1.0) * 100.0
    if len(rets) < max(10, LABEL_MIN_SHARE * len(universe)):
        return None
    med = statistics.median(rets.values())
    return {"target": target, "n": len(rets), "median": round(med, 4),
            "y": {p: (1 if r > med else 0) for p, r in sorted(rets.items()) if r != med},
            "ex": {p: round(r - med, 4) for p, r in sorted(rets.items()) if r != med}}


async def update_labels(now: float | None = None) -> dict:
    """Label every (as-of date, horizon) of the log and the cohort records whose target session is complete.
    Labels are written once and never recomputed. -> {"added", "pending"}."""
    now = _now() if now is None else now
    done_through = packets.last_completed_session(now)
    cohorts = read_cohorts()
    dates = {str(c["asOf"]) for c in cohorts.values() if c.get("asOf")} | {r["asOf"] for r in read_log() if r.get("asOf")}
    labels = read_labels()
    added = pending = 0
    cache: dict[str, list] = {}
    for d in sorted(dates):
        try:
            as_of = datetime.date.fromisoformat(d)
        except ValueError:
            continue
        for h in HORIZONS:
            if str(h) in labels.get(d, {}):
                continue
            if calendars.add_sessions(MIC, as_of, h) > done_through:
                pending += 1
                continue
            uni = label_universe(d, cohorts)
            for pid in uni:
                if pid not in cache:
                    try:
                        cache[pid] = await _closes_of(pid)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("forecasts: closes %s failed: %s", pid, type(e).__name__)
                        cache[pid] = []
            lab = compute_label(as_of, h, uni, cache)
            if lab is None:
                pending += 1
                continue
            labels.setdefault(d, {})[str(h)] = lab
            added += 1
    if added and not jsonstore.save(LABELS_FILE, {"labels": labels}):
        log.warning("forecasts: labels not saved")
    return {"added": added, "pending": pending}


# ------------------------------------------------------------------ grading

def calibration_slope(ps: list[float], ys: list[int]) -> float | None:
    """Slope b of the logistic fit y ~ a + b logit(p) (Newton, tiny ridge); 1 is perfectly calibrated.
    None below 30 pairs, with a constant p or one label class, or when the fit does not converge."""
    n = len(ps)
    if n < 30 or len(set(ys)) < 2:
        return None
    xs = [logit(p) for p in ps]
    if max(xs) - min(xs) < 1e-9:
        return None
    a = b = 0.0
    for _ in range(50):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            mu = sigmoid(a + b * x)
            w = mu * (1.0 - mu)
            g0 += y - mu
            g1 += (y - mu) * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        g1 -= 1e-6 * b
        h11 += 1e-6
        h00 += 1e-9
        det = h00 * h11 - h01 * h01
        if det <= 1e-12:
            return None
        da, db = (h11 * g0 - h01 * g1) / det, (h00 * g1 - h01 * g0) / det
        a, b = a + da, b + db
        if not (math.isfinite(a) and math.isfinite(b)) or abs(b) > 50:
            return None
        if abs(da) + abs(db) < 1e-8:
            return round(b, 3)
    return None


def _cell(pairs: list[dict]) -> dict:
    n = len(pairs)
    brier = sum((p["p"] - p["y"]) ** 2 for p in pairs) / n
    ref = sum((0.5 - p["y"]) ** 2 for p in pairs) / n          # arm R: constant 0.5
    called = [p for p in pairs if p["p"] != 0.5]
    hits = sum(1 for p in called if (p["p"] > 0.5) == (p["y"] == 1))
    by_cohort: dict[str, list] = {}
    for p in pairs:
        by_cohort.setdefault(p["cohort"], []).append(p)
    ics = [ic for ic in (scoreboard.spearman([q["p"] for q in rows], [q["ex"] for q in rows])
                         for rows in by_cohort.values() if len(rows) >= 5) if ic is not None]
    return {"n": n, "cohorts": len(by_cohort), "brier": round(brier, 5), "brierR": round(ref, 5),
            "bss": round(1.0 - brier / ref, 4) if ref > 0 else None,
            "ic": round(sum(ics) / len(ics), 4) if ics else None, "icCohorts": len(ics),
            "calibrationSlope": calibration_slope([p["p"] for p in pairs], [p["y"] for p in pairs]),
            "hitRate": {"hits": hits, "n": len(called), "wilson95": scoreboard.wilson(hits, len(called))},
            "insufficient": n < MIN_PAIRS}


def _graded_pairs(rows: list[dict], labels: dict) -> tuple[list[dict], int]:
    """Aggregate rows joined to their labels -> (pairs, number still without a label)."""
    pairs, pending = [], 0
    for r in rows:
        lab = (labels.get(r.get("asOf")) or {}).get(str(r.get("horizon")))
        if lab is None:
            pending += 1
            continue
        y = lab["y"].get(r["id"])
        if y is None:
            continue
        pairs.append({"cohort": r["cohort"], "id": r["id"], "arm": r["arm"], "source": r.get("source"),
                      "horizon": r["horizon"], "p": r["p"], "y": y, "ex": lab["ex"][r["id"]], "asOf": r["asOf"]})
    return pairs, pending


def _cells(pairs: list[dict]) -> list[dict]:
    groups: dict[tuple, list] = {}
    for p in pairs:
        groups.setdefault((p["arm"], p["source"], p["horizon"]), []).append(p)
    order = {a: i for i, a in enumerate(ARMS)}
    return [{"arm": a, "source": s, "horizon": h, **_cell(rows)}
            for (a, s, h), rows in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[0][1] or "", kv[0][2]))]


def cohort_composition(rows: list[dict]) -> dict[str, dict]:
    """Per scheduled cohort: ids, B source counts, fallback share and whether it is 'mixed' (> 10 %)."""
    by: dict[str, dict[str, str]] = {}
    for r in rows:
        if r.get("kind") == "aggregate" and r.get("arm") == "B" and r.get("role") in GRADED_ROLES:
            by.setdefault(r["cohort"], {}).setdefault(r["id"], r.get("source") or NONE)
            if r.get("p") is not None:
                by[r["cohort"]][r["id"]] = r.get("source") or NONE
    out = {}
    for cohort, srcs in by.items():
        counts: dict[str, int] = {}
        for s in srcs.values():
            counts[s] = counts.get(s, 0) + 1
        n = len(srcs)
        share = (n - counts.get(LOCAL, 0)) / n if n else 0.0
        out[cohort] = {"ids": n, "bySource": counts, "fallbackShare": round(share, 3), "mixed": share > MIXED_SHARE,
                       "pure": max(counts.values()) / n >= 0.9 if n else False}
    return out


def holm(pvals: list[float | None]) -> list[float | None]:
    """Holm step-down adjustment over all m comparisons (a missing p counts as 1 and stays None)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: 1.0 if pvals[i] is None else pvals[i])
    adj: list[float | None] = [None] * m
    run_max = 0.0
    for rank, i in enumerate(order):
        p = 1.0 if pvals[i] is None else pvals[i]
        run_max = max(run_max, min(1.0, (m - rank) * p))
        adj[i] = None if pvals[i] is None else round(run_max, 4)
    return adj


def _paired(pairs: list[dict], first: str, second: str, horizon: int, subset: set[str]) -> dict:
    """Paired Brier difference first - second on the common (cohort, id) of the pre-registered
    comparison, restricted to the fixed subset and to one source per arm."""
    def pick(arm):
        return {(p["cohort"], p["id"]): p for p in pairs
                if p["arm"] == arm and p["source"] == COMPARE_SOURCE[arm] and p["horizon"] == horizon and p["id"] in subset}
    a, b = pick(first), pick(second)
    keys = sorted(set(a) & set(b), key=lambda k: (a[k]["asOf"], k))
    deltas = [(a[k]["p"] - a[k]["y"]) ** 2 - (b[k]["p"] - b[k]["y"]) ** 2 for k in keys]
    per_cohort: dict[str, int] = {}
    for c, _ in keys:
        per_cohort[c] = per_cohort.get(c, 0) + 1
    out = {"n": len(deltas), "cohorts": len(per_cohort), "meanDeltaBrier": None, "ci95": None, "pApprox": None}
    if not deltas:
        return out
    mean = sum(deltas) / len(deltas)
    out["meanDeltaBrier"] = round(mean, 5)
    # week-block bootstrap: one block spans a whole cohort (the names of one week move together)
    ci = paper_metrics.block_bootstrap_ci(deltas, block=max(per_cohort.values()), n=BOOT_N)
    if ci is not None:
        lo, hi = ci[0] / 100.0 / len(deltas), ci[1] / 100.0 / len(deltas)
        out["ci95"] = [round(lo, 5), round(hi, 5)]
        se = (hi - lo) / (2 * 1.96)
        out["pApprox"] = round(math.erfc(abs(mean) / se / math.sqrt(2)), 4) if se > 0 else (1.0 if mean == 0 else 0.0)
    return out


def comparison_status(n: int, cohorts: int) -> str:
    """pending (no matured pair) / insufficient (< 30 pairs or < 4 cohorts) / sanity (4-7 cohorts, no
    inference) / interim (8-11) / verdict (12+): the week 4 / 8 / 12 readout schedule."""
    if n == 0:
        return "pending"
    if n < MIN_PAIRS or cohorts < 4:
        return "insufficient"
    if cohorts < 8:
        return "sanity"
    return "interim" if cohorts < 12 else "verdict"


def comparisons(pairs: list[dict], subset: set[str]) -> list[dict]:
    rows = []
    for horizon in HORIZONS:
        for cid, first, second in COMPARISONS:
            res = _paired(pairs, first, second, horizon, subset)
            status = comparison_status(res["n"], res["cohorts"])
            rows.append({"id": cid, "first": first, "second": second, "horizon": horizon, "primary": horizon == 5,
                         "subset": True, "status": status, **res, "pHolm": None, "significant": None, "favours": None})
    prim = [r for r in rows if r["primary"]]
    for r, adj in zip(prim, holm([r["pApprox"] for r in prim])):
        r["pHolm"] = adj
        if r["status"] == "verdict" and adj is not None:
            r["significant"] = adj < 0.05
            if r["significant"]:
                r["favours"] = r["first"] if r["meanDeltaBrier"] < 0 else r["second"]
    return rows


def readout(n_weekly: int) -> dict:
    nxt = next(((w, k) for w, k in READOUT_WEEKS.items() if w > n_weekly), None)
    return {"weeks": list(READOUT_WEEKS), "kinds": {str(w): k for w, k in READOUT_WEEKS.items()},
            "currentWeek": n_weekly, "next": None if nxt is None else {"week": nxt[0], "kind": nxt[1]},
            "firstVerdict": "week 12", "absoluteSkillVsR": "around week 26",
            "frozenForWeeks": 12, "primary": "paired Brier difference, 5-day label, Holm over 4"}


def arms_payload(now: float | None = None) -> dict:
    """Grading of the experiment from the log and the stored labels (no network): per arm x source x horizon
    cells (Brier, Brier skill vs R, IC, calibration slope, hit rate + Wilson, n) for pure cohorts, the same
    for mixed cohorts and for the daily role apart, the 4 pre-registered paired comparisons, the experiment
    status and the readout schedule."""
    now = _now() if now is None else now
    rows = read_log()
    labels = read_labels()
    cohorts = read_cohorts()
    comp = cohort_composition(rows)
    graded = [r for r in rows if r.get("kind") == "aggregate" and r.get("p") is not None and r.get("role") in GRADED_ROLES]
    pure_ids = {c for c, v in comp.items() if not v["mixed"]}
    pure_pairs, pending = _graded_pairs([r for r in graded if r["cohort"] in pure_ids], labels)
    mixed_pairs, _ = _graded_pairs([r for r in graded if r["cohort"] not in pure_ids], labels)
    daily_pairs, _ = _graded_pairs([r for r in rows if r.get("kind") == "aggregate" and r.get("p") is not None
                                    and r.get("role") == "daily"], labels)
    weekly = {c: v for c, v in cohorts.items() if v.get("kind") == "weekly"}
    mixed = sorted(c for c, v in comp.items() if v["mixed"])
    share_mixed = len(mixed) / len(comp) if comp else 0.0
    subset = stored_subset()
    matured = sorted({p["cohort"] for p in pure_pairs if p["horizon"] == 5})
    ledger_rows = ledger().get("rows") or []
    return {
        "book": "practice", "asOf": now, "arms": list(ARMS), "horizons": list(HORIZONS),
        "cells": _cells(pure_pairs), "mixedCells": _cells(mixed_pairs), "dailyCells": _cells(daily_pairs),
        "comparisons": comparisons(pure_pairs, set(subset)),
        "experiment": {
            "cohorts": len(weekly), "cohortStatus": {c: v.get("status") for c, v in sorted(weekly.items())},
            "maturedCohorts5d": matured, "ungradedRows": pending,
            "composition": comp, "mixedCohorts": mixed, "fallbackCohortShare": round(share_mixed, 3),
            "underpowered": share_mixed > UNDERPOWERED_SHARE,
            "subset": subset, "labelDates": len(labels),
            "trial": {"hash": ledger_rows[-1]["hash"] if ledger_rows else None, "rows": len(ledger_rows)},
            "sourcePolicy": "sources are never pooled; cells are keyed by arm x source x horizon",
        },
        "readout": readout(len(weekly)),
    }
