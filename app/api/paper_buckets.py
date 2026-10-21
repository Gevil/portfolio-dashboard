"""Paper-trading buckets (phase 5 contract §1, §2, §5): which strategy buckets
share the ONE Trading 212 DEMO account, each one's merged configuration and
its per-bucket data files.

Bucket ids: ``daily`` (the original bucket; every pre-phase-5 file, row and
decision belongs to it) and ``intraday``. Pure config/path helpers: no broker,
no ledger, no I/O except ``config_store.read``.

* ``ids()`` - every known bucket id.
* ``validate(bucket)`` - the normalised id; ``None`` / ``""`` is ``daily``;
  anything else unknown raises ``BadBucket`` (the routes answer 400).
* ``cfg(bucket)`` - the merged config the rest of the code consumes: ``mode``,
  ``strategyVersion``, ``budgetEur``, ``universe`` (shared), ``limits``,
  ``costModel`` plus ``bucket`` and ``enabled`` (and ``intervalMin`` /
  ``flatAtClose`` for intraday). The daily bucket is the top-level
  ``paperTrading`` keys, as before. The intraday bucket is
  ``paperTrading.buckets.intraday``: absent or ``enabled: false`` means mode
  ``off`` (``configuredMode`` keeps what was written) so a disabled bucket can
  never place an order. Intraday limits = daily limits, then
  ``INTRADAY_DEFAULT_LIMITS``, then the bucket's own overrides (the
  ``maxWeightPct`` / ``killSwitch`` sub-objects merge key by key).
* ``path(bucket, name)`` - ``data/paper_<name>.json`` for daily,
  ``data/paper_<name>.intraday.json`` otherwise; ``orders`` is ONE shared file
  (the broker-wide reconciliation needs every bucket's rows).
* ``universeSource`` (top-level ``paperTrading.universeSource``, phase 6):
  ``watchlist`` (default, s1 behaviour: the ``universe`` block) or
  ``paper_universe``: the investable ids are the ``paper_universe`` candidates
  that have a Trading 212 ticker, ``cfg["universe"]`` is built from them
  (``{id: {t212, kind, sector, name}}``), the default strategy versions become
  ``s2`` / ``i2`` and the open-universe limit defaults (``OPEN_UNIVERSE_LIMITS``)
  sit UNDER the configured limits (a configured key always wins).
"""
import os
import pathlib

from app.api import config_store, paper_universe

DAILY = "daily"
INTRADAY = "intraday"
BUCKETS = (DAILY, INTRADAY)
MODES = ("off", "shadow", "demo")
INTERVALS = (15, 30, 60, 120)

DEFAULT_MODE = "off"
DEFAULT_BUDGET_EUR = 1000.0
DEFAULT_STRATEGY_VERSION = {DAILY: "s1", INTRADAY: "i1"}
DEFAULT_INTERVAL_MIN = 60
OPEN_STRATEGY_VERSION = {DAILY: "s2", INTRADAY: "i2"}
WATCHLIST = "watchlist"
PAPER_UNIVERSE = "paper_universe"
UNIVERSE_SOURCES = (WATCHLIST, PAPER_UNIVERSE)
# phase 6 plan: <= 10 positions, <= 20 % per stock / 40 % per ETF, <= 40 % per
# sector, >= EUR 50 per position (min cash 2 % is already a global default)
OPEN_UNIVERSE_LIMITS = {"maxPositions": 10, "maxWeightPct": {"equity": 20, "etf": 40},
                        "maxSectorPct": 40, "minPositionEur": 50}
DEFAULT_COST_MODEL = {"spreadBps": 10.0, "commissionEur": 0.0, "fxBps": 0.0}
INTRADAY_DEFAULT_LIMITS = {"maxOrdersPerDay": 12, "maxTurnoverPctPerDay": 100,
                           "rebalanceBandPct": 2, "minTradeEur": 25, "adjustFraction": 0.5}
SHARED_FILES = ("orders",)

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))


class BadBucket(ValueError):
    """An unknown bucket id (or one that is not a string); HTTP 400."""


def ids() -> list[str]:
    return list(BUCKETS)


def validate(bucket) -> str:
    """The bucket id; ``None`` / ``""`` default to ``daily``; unknown -> ``BadBucket``."""
    if bucket is None or bucket == "":
        return DAILY
    if not isinstance(bucket, str) or bucket.strip().lower() not in BUCKETS:
        raise BadBucket(f"bucket must be one of {', '.join(BUCKETS)}")
    return bucket.strip().lower()


def suffixed(daily_path: pathlib.Path, bucket: str) -> pathlib.Path:
    """The bucket's variant of the DAILY file ``daily_path`` (``x.json`` ->
    ``x.intraday.json``): per-bucket files sit beside the daily one."""
    if validate(bucket) == DAILY:
        return daily_path
    return daily_path.with_name(f"{daily_path.stem}.{bucket}{daily_path.suffix}")


def path(bucket, name: str) -> pathlib.Path:
    """``data/paper_<name>.json`` (daily) / ``data/paper_<name>.<bucket>.json``;
    ``orders`` is shared by every bucket."""
    daily_file = DATA_DIR / f"paper_{name}.json"
    if name in SHARED_FILES:
        validate(bucket)
        return daily_file
    return suffixed(daily_file, validate(bucket))


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _cost_model(raw) -> dict:
    """``paperTrading.costModel`` merged over the defaults (10 / 0 / 0)."""
    out = dict(DEFAULT_COST_MODEL)
    if isinstance(raw, dict):
        for key in out:
            v = _num(raw.get(key))
            if v is not None and v >= 0:
                out[key] = v
    return out


def _raw_block() -> dict:
    raw = config_store.read().get("paperTrading")
    return raw if isinstance(raw, dict) else {}


def _intraday_block(raw: dict) -> dict | None:
    buckets = raw.get("buckets")
    block = buckets.get(INTRADAY) if isinstance(buckets, dict) else None
    return block if isinstance(block, dict) else None


def _merge_limits(*layers: dict) -> dict:
    out: dict = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            if key in ("maxWeightPct", "killSwitch") and isinstance(value, dict):
                out[key] = {**(out.get(key) or {}), **value}
            else:
                out[key] = value
    return out


def universe_source(raw: dict | None = None) -> str:
    """``watchlist`` (default) or ``paper_universe`` (``paperTrading.universeSource``)."""
    src = (_raw_block() if raw is None else raw).get("universeSource")
    return src if src in UNIVERSE_SOURCES else WATCHLIST


def open_universe() -> dict:
    """``{id: {t212, kind, sector, name}}`` of the paper_universe candidates
    that have a Trading 212 ticker (the only investable names of the open
    universe); file order."""
    return {r["id"]: {"t212": r["t212"], "kind": r["kind"], "sector": r.get("sector"),
                      "name": r.get("name")}
            for r in paper_universe.entries() if r["role"] == "candidate" and r.get("t212")}


def cfg(bucket=DAILY) -> dict:
    """The bucket's merged configuration (see the module docstring)."""
    b = validate(bucket)
    raw = _raw_block()
    source = universe_source(raw)
    open_ = source == PAPER_UNIVERSE
    universe = raw.get("universe")
    daily_limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    base_limits = _merge_limits(OPEN_UNIVERSE_LIMITS if open_ else {}, daily_limits)
    versions = OPEN_STRATEGY_VERSION if open_ else DEFAULT_STRATEGY_VERSION
    out: dict = {
        "bucket": b,
        "universeSource": source,
        "universe": (open_universe() if open_
                     else universe if isinstance(universe, dict) else {}),
        "costModel": _cost_model(raw.get("costModel")),
    }
    if b == DAILY:
        out.update(
            enabled=True,
            mode=raw.get("mode") or DEFAULT_MODE,
            strategyVersion=raw.get("strategyVersion") or versions[DAILY],
            budgetEur=_num(raw.get("budgetEur")) or DEFAULT_BUDGET_EUR,
            limits=base_limits)
        return out
    block = _intraday_block(raw)
    enabled = bool(block and block.get("enabled") is True)
    configured = (block or {}).get("mode") or DEFAULT_MODE
    interval = _num((block or {}).get("intervalMin"))
    own_limits = (block or {}).get("limits")
    out.update(
        enabled=enabled,
        mode=configured if enabled else DEFAULT_MODE,
        configuredMode=configured,
        strategyVersion=(block or {}).get("strategyVersion") or versions[INTRADAY],
        budgetEur=_num((block or {}).get("budgetEur")) or DEFAULT_BUDGET_EUR,
        intervalMin=int(interval) if interval in INTERVALS else DEFAULT_INTERVAL_MIN,
        flatAtClose=bool((block or {}).get("flatAtClose")),
        limits=_merge_limits(base_limits, INTRADAY_DEFAULT_LIMITS,
                             own_limits if isinstance(own_limits, dict) else {}))
    return out
