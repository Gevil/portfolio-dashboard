"""Validation + merge of a ``PUT /api/config`` body into the stored config.

``apply_update`` runs INSIDE ``config_store.update`` so the read, the checks
and the write are one atomic step; any ``ConfigInvalid`` aborts the update and
nothing is written. Unknown top-level keys are ignored (``aliases`` and
``alertRules`` have their own editors and survive a save untouched).
"""
import math

from app.api import registry


class ConfigInvalid(ValueError):
    """Readable validation failure; the endpoint answers 400 with str()."""


def _number(value, ctx: str) -> float:
    if isinstance(value, bool):
        raise ConfigInvalid(f"{ctx} must be a number")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ConfigInvalid(f"{ctx} must be a number") from None
    if not math.isfinite(v):
        raise ConfigInvalid(f"{ctx} must be a finite number")
    return v


def _watchlist(raw_list, current: list) -> list[dict]:
    if not isinstance(raw_list, list):
        raise ConfigInvalid("watchlist must be a list of entries")
    if not raw_list:
        raise ConfigInvalid("watchlist cannot be empty")
    old_by_id = {}
    for e in current or []:
        if isinstance(e, dict):
            old_by_id[str(e.get("id") or "").strip().upper()] = e
    merged_list = []
    for i, raw in enumerate(raw_list):
        if isinstance(raw, str):
            raw = {"id": raw}
        if not isinstance(raw, dict):
            raise ConfigInvalid(
                f"watchlist[{i}] must be an object or a ticker string")
        eid = str(raw.get("id") or raw.get("symbol") or "").strip().upper()
        # Keys the client did not send (providers, listing, flags...) keep
        # their stored values: a sparse settings form must not erase them.
        merged_list.append({**old_by_id.get(eid, {}), **raw,
                            **({"id": eid} if eid else {})})
    try:
        return registry.validate_entries(merged_list)
    except ValueError as e:
        raise ConfigInvalid(str(e)) from None


def _portfolio(raw, entries: list) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("portfolio must be an object keyed by ticker id")
    roles = {}
    for e in entries:
        try:
            n = registry.normalize_entry(e)
        except ValueError:
            continue
        roles[n["id"]] = n.get("role")
    out = {}
    for key, pos in raw.items():
        pid = str(key).strip().upper()
        if pid not in roles:
            raise ConfigInvalid(f"portfolio.{pid}: not on the watchlist")
        if roles[pid] != "holding":
            raise ConfigInvalid(f"portfolio.{pid}: only holdings can have a "
                                "position")
        if not isinstance(pos, dict):
            raise ConfigInvalid(f"portfolio.{pid} must be an object")
        shares = _number(pos.get("shares"), f"portfolio.{pid}.shares")
        if shares <= 0:
            raise ConfigInvalid(f"portfolio.{pid}.shares must be > 0")
        invested = pos.get("investedAmount")
        if invested is not None:
            invested = _number(invested, f"portfolio.{pid}.investedAmount")
            if invested < 0:
                raise ConfigInvalid(
                    f"portfolio.{pid}.investedAmount must be >= 0 or null")
        if pid in out:
            raise ConfigInvalid(f"portfolio: duplicate id {pid}")
        out[pid] = {"shares": shares, "investedAmount": invested}
    return out


def apply_update(cfg: dict, body: dict) -> list[str]:
    """Merge ``body`` into ``cfg`` in place; return ids newly added to the
    watchlist (the caller seeds their history). Raises ``ConfigInvalid``."""
    old_ids = set()
    for e in cfg.get("watchlist") or []:
        if isinstance(e, dict):
            old_ids.add(str(e.get("id") or "").strip().upper())

    wl_changed = False
    if body.get("watchlist") is not None:
        cfg["watchlist"] = _watchlist(body["watchlist"],
                                      cfg.get("watchlist") or [])
        wl_changed = True

    model = body.get("chatModel")
    if model is not None:
        if not isinstance(model, str):
            raise ConfigInvalid("chatModel must be a string")
        if model.strip():
            cfg["chatModel"] = model.strip()

    if body.get("portfolio") is not None:
        cfg["portfolio"] = _portfolio(body["portfolio"],
                                      cfg.get("watchlist") or [])
    elif wl_changed:
        # Watchlist changed alone: stored positions must still point at a
        # holding that remains on it (never silently drop a cost basis).
        _portfolio(cfg.get("portfolio") or {}, cfg["watchlist"])

    if not wl_changed:
        return []
    return [e["id"] for e in cfg["watchlist"] if e["id"] not in old_ids]
