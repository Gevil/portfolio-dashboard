"""Alert-rule storage: per-ticker alert rules in config.json (v2).

The ``alertRules`` block v2 holds a LIST of rules per ticker, each with a
``kind``:

  "alertRules": {
    "version": 2,
    "default":   {"thresholdPct": 3.0, "hotPct": 5.0,
                  "direction": "both", "cooldownMin": 120},
    "perTicker": {"ASML": [
       {"kind": "pct-move", "thresholdPct": 2.5, "direction": "down",
        "cooldownMin": 240},
       {"kind": "absolute", "condition": "ABOVE", "targetPrice": 1900,
        "oneShot": true, "expiresAt": "2026-12-31"},
       {"kind": "ema_cross", "fast": 9, "slow": 21,
        "direction": "bullish_cross"},
       {"kind": "rsi_threshold", "period": 14, "threshold": 70,
        "condition": "above"},
       {"kind": "volume-spike", "factor": 3.0},
       {"kind": "earnings-day", "leadDays": 1},
       {"kind": "short-ratio-spike", "sigma": 2.0},
       {"kind": "market-light-drop", "from": "green", "to": "yellow"}
    ]}
  }

v1 stored ONE dict per ticker (a pct-move rule with no ``kind``). Such a
block stays valid: it is read as a single-element list, so nothing has to be
migrated; the first save from the UI rewrites it in v2 shape.

``resolve()`` returns the LIST of effective rules for a ticker — the default
block merged UNDER every entry, so any kind carries a threshold, a direction
and a cooldown. pct-move delivery itself stays in price_alerts.py (it owns
the live-quote baseline); it reads its single merged rule through
``pct_move()``, which answers None when a ticker's rule list has no pct-move
entry (removing the rule in the UI must silence the pct alert).

Env defaults (ALERT_THRESHOLD_PCT / ALERT_HOT_THRESHOLD_PCT /
PRICE_ALERT_COOLDOWN_MIN) seed ``default`` when the block is absent, so the
existing deployment keeps identical behaviour until rules are edited from
the UI.
"""
import datetime
import logging
import os

from app.api import config_store

log = logging.getLogger("rules")

DIRECTIONS = ("both", "up", "down")
CROSS_DIRECTIONS = ("bullish_cross", "bearish_cross")
LEVEL_CONDITIONS = ("ABOVE", "BELOW")
RSI_CONDITIONS = ("above", "below")
# Ascending: a market-light "drop" moves to a LOWER rank.
LIGHT_STATES = ("red", "yellow", "green")

KINDS = ("pct-move", "absolute", "ema_cross", "rsi_threshold",
         "volume-spike", "earnings-day", "short-ratio-spike",
         "market-light-drop")

BLOCK_VERSION = 2

# key -> (low, high, integer). Bounds are per-field, not per-kind: a rule
# outside them is a typo, and a typo silently dropped would leave the rule
# running on an inherited default the user never typed.
_NUM_SPECS = {
    "thresholdPct": (0.1, 50.0, False),
    "hotPct": (0.1, 50.0, False),
    "cooldownMin": (1, 24 * 60, True),
    "targetPrice": (0.0001, 1_000_000_000.0, False),
    "factor": (1.1, 100.0, False),
    "sigma": (0.5, 10.0, False),
    "threshold": (1.0, 99.0, False),
    "period": (2, 200, True),
    "fast": (2, 200, True),
    "slow": (3, 400, True),
    "leadDays": (0, 45, True),
}

# kind -> (required keys, optional keys). Keys outside these two sets are
# dropped. cooldownMin is accepted on EVERY kind: rule_eval throttles every
# rule with it, so a per-rule override must be storable for all of them.
_KIND_KEYS = {
    "pct-move": ((), ("thresholdPct", "hotPct", "direction", "cooldownMin")),
    "absolute": (("condition", "targetPrice"),
                 ("oneShot", "expiresAt", "cooldownMin")),
    "ema_cross": (("fast", "slow", "direction"), ("cooldownMin",)),
    "rsi_threshold": (("period", "threshold", "condition"), ("cooldownMin",)),
    "volume-spike": (("factor",), ("cooldownMin",)),
    "earnings-day": (("leadDays",), ("cooldownMin",)),
    "short-ratio-spike": (("sigma",), ("cooldownMin",)),
    "market-light-drop": (("from", "to"), ("cooldownMin",)),
}


class RuleError(ValueError):
    """The submitted block is invalid; the endpoint answers 400 with str()."""


def _defaults() -> dict:
    return {
        "thresholdPct": float(os.getenv("ALERT_THRESHOLD_PCT", "3.0")),
        "hotPct": float(os.getenv("ALERT_HOT_THRESHOLD_PCT", "5.0")),
        "direction": "both",
        "cooldownMin": int(os.getenv("PRICE_ALERT_COOLDOWN_MIN", "120")),
    }


def _block() -> dict:
    block = config_store.read().get("alertRules") or {}
    return block if isinstance(block, dict) else {}


# ------------------------------------------------------------------ validate

def _num(src: dict, key: str, ctx: str, required: bool = False):
    lo, hi, is_int = _NUM_SPECS[key]
    raw = src.get(key)
    if raw is None or raw == "":
        if required:
            raise RuleError(f"{ctx}: {key} is required")
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise RuleError(f"{ctx}: {key} must be a number") from None
    if not lo <= v <= hi:
        raise RuleError(f"{ctx}: {key} must be between {lo:g} and {hi:g}")
    return int(round(v)) if is_int else v


def _choice(src: dict, key: str, allowed, ctx: str,
            required: bool = False, upper: bool = False):
    raw = src.get(key)
    if raw is None or raw == "":
        if required:
            raise RuleError(f"{ctx}: {key} must be one of: "
                            + ", ".join(allowed))
        return None
    val = str(raw).strip().upper() if upper else str(raw).strip()
    if val not in allowed:
        raise RuleError(f"{ctx}: {key} must be one of: " + ", ".join(allowed))
    return val


def _date(src: dict, key: str, ctx: str):
    raw = src.get(key)
    if raw is None or raw == "":
        return None
    try:
        return datetime.date.fromisoformat(str(raw).strip()[:10]).isoformat()
    except ValueError:
        raise RuleError(f"{ctx}: {key} must be an ISO date (YYYY-MM-DD)") \
            from None


def _clean_rule(src, ctx: str) -> dict:
    """One submitted rule -> the stored form, or RuleError."""
    if not isinstance(src, dict):
        raise RuleError(f"{ctx}: rule must be an object")
    kind = str(src.get("kind") or "pct-move").strip()
    if kind not in KINDS:
        raise RuleError(f"{ctx}: unknown rule kind: {kind}")
    ctx = f"{ctx} ({kind})"
    required, optional = _KIND_KEYS[kind]
    out: dict = {"kind": kind}
    for key in tuple(required) + tuple(optional):
        if key in _NUM_SPECS:
            val = _num(src, key, ctx, key in required)
        elif key == "direction":
            allowed = CROSS_DIRECTIONS if kind == "ema_cross" else DIRECTIONS
            val = _choice(src, key, allowed, ctx, key in required)
        elif key == "condition":
            val = _choice(src, key,
                          LEVEL_CONDITIONS if kind == "absolute"
                          else RSI_CONDITIONS,
                          ctx, key in required, upper=kind == "absolute")
        elif key == "oneShot":
            val = bool(src.get(key))
        elif key == "expiresAt":
            val = _date(src, key, ctx)
        elif key in ("from", "to"):
            val = _choice(src, key, LIGHT_STATES, ctx, key in required)
        else:
            val = None
        if val is not None:
            out[key] = val
    if kind == "ema_cross" and out["slow"] <= out["fast"]:
        raise RuleError(f"{ctx}: slow must be greater than fast")
    if kind == "market-light-drop" and \
            LIGHT_STATES.index(out["to"]) >= LIGHT_STATES.index(out["from"]):
        raise RuleError(f"{ctx}: to must be a drop from {out['from']} "
                        "(yellow or red)")
    return out


def _clean_default(src) -> dict:
    """The baseline block: pct-move fields only, no kind."""
    if src is None:
        return {}
    if not isinstance(src, dict):
        raise RuleError("default must be an object")
    out: dict = {}
    for key in ("thresholdPct", "hotPct", "cooldownMin"):
        val = _num(src, key, "default")
        if val is not None:
            out[key] = val
    direction = _choice(src, "direction", DIRECTIONS, "default")
    if direction:
        out["direction"] = direction
    return out


# -------------------------------------------------------------------- merge

def _base_rule(block: dict) -> dict:
    """Env defaults <- the stored default block (the v1 merge)."""
    rule = _defaults()
    src = block.get("default")
    if isinstance(src, dict):
        for key in ("thresholdPct", "hotPct", "cooldownMin"):
            try:
                if src.get(key) is not None:
                    rule[key] = float(src[key])
            except (TypeError, ValueError):
                pass
        d = src.get("direction")
        if d in DIRECTIONS:
            rule["direction"] = d
    return rule


def _stored(block: dict, ticker: str) -> list | None:
    """Stored entries for a ticker, always as a list.

    None means the ticker has no per-ticker block at all (so the caller
    falls back to the default rule); an empty list means the user removed
    every rule, which must silence the ticker.
    """
    pt = block.get("perTicker")
    if not isinstance(pt, dict):
        return None
    if ticker in pt:
        src = pt[ticker]
    else:
        src = next((val for key, val in pt.items()
                    if str(key).upper() == ticker), None)
        if src is None:
            return None
    if isinstance(src, dict):        # v1: one dict == one pct-move rule
        return [src]
    if isinstance(src, list):
        return [e for e in src if isinstance(e, dict)]
    return []


def _effective(src: dict, base: dict) -> dict:
    """One stored entry with the merged default merged UNDER it."""
    kind = str(src.get("kind") or "pct-move").strip()
    if kind not in KINDS:
        kind = "pct-move"
    rule = dict(base)
    rule["kind"] = kind
    if kind == "ema_cross":
        # "direction" means something else here: never inherit "both".
        rule.pop("direction", None)
    required, optional = _KIND_KEYS[kind]
    for key in tuple(required) + tuple(optional):
        raw = src.get(key)
        if raw is None:
            continue
        if key in _NUM_SPECS:
            lo, hi, is_int = _NUM_SPECS[key]
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if not lo <= val <= hi:
                continue
            rule[key] = int(round(val)) if is_int else val
        elif key == "direction":
            allowed = CROSS_DIRECTIONS if kind == "ema_cross" else DIRECTIONS
            if raw in allowed:
                rule["direction"] = raw
        elif key == "condition":
            allowed = LEVEL_CONDITIONS if kind == "absolute" else RSI_CONDITIONS
            val = str(raw).upper() if kind == "absolute" else str(raw)
            if val in allowed:
                rule["condition"] = val
        elif key == "oneShot":
            rule["oneShot"] = bool(raw)
        elif key == "expiresAt":
            try:
                rule["expiresAt"] = datetime.date.fromisoformat(
                    str(raw).strip()[:10]).isoformat()
            except ValueError:
                pass
        elif key in ("from", "to"):
            if raw in LIGHT_STATES:
                rule[key] = raw
    return rule


def resolve(ticker: str) -> list[dict]:
    """Effective rule LIST for a ticker (default merged under every entry).

    A ticker with no per-ticker block resolves to the default pct-move rule,
    exactly like v1; a ticker whose block exists but is empty resolves to [].
    """
    block = _block()
    base = _base_rule(block)
    entries = _stored(block, str(ticker or "").upper().strip())
    if entries is None:
        entries = [{"kind": "pct-move"}]
    return [_effective(src, base) for src in entries]


def pct_move(ticker: str) -> dict | None:
    """The effective pct-move rule (v1 resolve() shape), or None."""
    for rule in resolve(ticker):
        if rule.get("kind") == "pct-move":
            return rule
    return None


def load_public() -> dict:
    """Current rules for the UI, in v2 shape.

    Entries are returned as STORED (kind-normalised, coerced), not merged
    with the default: the editor POSTs the whole block, so pre-filling every
    field with an inherited value would silently pin defaults the user never
    chose. The default row carries the inherited values.
    """
    block = _block()
    out = {"version": BLOCK_VERSION, "default": _base_rule(block),
           "perTicker": {}}
    pt = block.get("perTicker")
    if isinstance(pt, dict):
        for tick, src in pt.items():
            tick = str(tick).upper().strip()
            if not tick:
                continue
            entries = _stored(block, tick) or []
            try:
                out["perTicker"][tick] = [
                    _clean_rule(e, tick) for e in entries]
            except RuleError as e:
                # Hand-edited config: show it verbatim rather than dropping
                # it, so the next save cannot silently delete the rule.
                log.warning("stored rule for %s is invalid: %s", tick, e)
                out["perTicker"][tick] = [dict(e) for e in entries]
    return out


def save(block: dict) -> dict:
    """Validate + persist the alertRules block, preserving other config keys.

    Raises RuleError (endpoint -> 400) on an unknown kind or a bad field.
    """
    if not isinstance(block, dict):
        raise RuleError("body must be an object")
    clean: dict = {"version": BLOCK_VERSION,
                   "default": _clean_default(block.get("default")),
                   "perTicker": {}}
    pt = block.get("perTicker")
    if isinstance(pt, dict):
        for tick, src in pt.items():
            tick = str(tick).upper().strip()
            if not tick:
                continue
            # A v1 client may still POST one dict per ticker: store it as a
            # single-element list.
            entries = src if isinstance(src, list) else [src]
            clean["perTicker"][tick] = [
                _clean_rule(e, f"{tick} rule {i + 1}")
                for i, e in enumerate(entries)]

    # config_store serialises the read-modify-write under its lock and keeps
    # the last-good copy; other config keys are preserved untouched.
    def _apply(cfg: dict) -> None:
        cfg["alertRules"] = clean

    config_store.update(_apply)
    log.info("alert rules saved: default=%s perTicker=%s",
             clean["default"], {k: [r["kind"] for r in v]
                                for k, v in clean["perTicker"].items()})
    return load_public()