"""Validation + merge of a ``PUT /api/config`` body into the stored config.

``apply_update`` runs INSIDE ``config_store.update`` so the read, the checks
and the write are one atomic step; any ``ConfigInvalid`` aborts the update and
nothing is written. Unknown top-level keys are ignored (``aliases`` and
``alertRules`` have their own editors and survive a save untouched).

Validated top-level keys (all optional; ``null`` resets one to its default,
except ``taxResidency`` where ``null`` is a value):

* ``portfolio[id].lots`` - dated buy/sell lots. When non-empty the server
  derives ``shares`` (sum of buys - sells) and ``investedAmount`` (FIFO cost
  of the remaining shares, buy fees included) and stores both, so every
  reader of the plain position keeps working. A position body WITHOUT a
  ``lots`` key keeps its stored lots (and re-derives from them); ``"lots":
  []`` clears them.
* ``performanceBenchmark`` (watchlist id), ``exposureCurrency`` (id -> ISO
  currency code), ``lookThrough`` (fund id -> ``{asOf, source, weights}``),
  ``investmentPolicy`` (targets, single-stock cap, rebalancing band, monthly
  contribution, trend filter), ``taxResidency`` (``"CZ"`` or null).

Ids in these keys are checked against the watchlist of the SAME update; a
later watchlist edit that removes a referenced id does not rewrite them, so
readers must tolerate an unknown id.
"""
import datetime
import math
import re

from app.api import registry

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EPS = 1e-9
_SIDES = ("buy", "sell")
_TARGET_SUM_TOL = 0.5
_WEIGHT_SUM_MAX = 100.5
_RESIDENCIES = ("CZ",)


class ConfigInvalid(ValueError):
    """Readable validation failure; the endpoint answers 400 with str()."""


def _today() -> datetime.date:
    return datetime.date.today()


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


def _iso_day(value, ctx: str) -> datetime.date:
    if isinstance(value, str) and _ISO_DAY.match(value):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigInvalid(f"{ctx} must be an ISO date (YYYY-MM-DD)")


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


def _roles(entries: list) -> dict:
    """{watchlist id: role} of the entries that normalise."""
    roles = {}
    for e in entries:
        try:
            n = registry.normalize_entry(e)
        except ValueError:
            continue
        roles[n["id"]] = n.get("role")
    return roles


def _lots(raw, ctx: str) -> tuple[list[dict], float, float]:
    """Validate ``raw`` lots; return (sorted lots, remaining shares, FIFO cost).

    Each lot: ISO ``date`` not in the future, ``shares`` > 0, ``priceEur`` > 0,
    ``feesEur`` >= 0 (default 0), ``side`` buy|sell. Lots are sorted by date
    (same day: buys before sells, so a same-day round trip is legal) and a
    sell may not exceed the shares held at that point. Sells consume the
    oldest buys first (FIFO); a buy's fee is spread over its shares, a sell's
    fee only reduces proceeds, so the cost of the remaining shares is
    ``sum(remaining x (shares x price + fees) / shares)``.
    """
    if not isinstance(raw, list):
        raise ConfigInvalid(f"{ctx} must be a list of lots")
    today = _today()
    parsed = []
    for i, lot in enumerate(raw):
        c = f"{ctx}[{i}]"
        if not isinstance(lot, dict):
            raise ConfigInvalid(f"{c} must be an object")
        day = _iso_day(lot.get("date"), f"{c}.date")
        if day > today:
            raise ConfigInvalid(f"{c}.date {day.isoformat()} is in the future")
        shares = _number(lot.get("shares"), f"{c}.shares")
        if shares <= 0:
            raise ConfigInvalid(f"{c}.shares must be > 0")
        price = _number(lot.get("priceEur"), f"{c}.priceEur")
        if price <= 0:
            raise ConfigInvalid(f"{c}.priceEur must be > 0")
        fees = lot.get("feesEur")
        fees = 0.0 if fees is None else _number(fees, f"{c}.feesEur")
        if fees < 0:
            raise ConfigInvalid(f"{c}.feesEur must be >= 0")
        side = lot.get("side")
        side = side.strip().lower() if isinstance(side, str) else side
        if side not in _SIDES:
            raise ConfigInvalid(f"{c}.side must be buy or sell")
        parsed.append((day, i, {"date": day.isoformat(), "shares": shares,
                                "priceEur": price, "feesEur": fees,
                                "side": side}))
    parsed.sort(key=lambda t: (t[0], t[2]["side"] != "buy", t[1]))

    queue: list[list[float]] = []            # [remaining shares, cost/share]
    for day, i, lot in parsed:
        if lot["side"] == "buy":
            queue.append([lot["shares"],
                          (lot["shares"] * lot["priceEur"] + lot["feesEur"])
                          / lot["shares"]])
            continue
        need = lot["shares"]
        held = sum(q[0] for q in queue)
        if need > held + _EPS:
            raise ConfigInvalid(
                f"{ctx}[{i}]: sell of {need:g} shares on {day.isoformat()} "
                f"exceeds the {held:g} held")
        while need > _EPS and queue:
            take = min(queue[0][0], need)
            queue[0][0] -= take
            need -= take
            if queue[0][0] <= _EPS:
                queue.pop(0)
    shares = sum(q[0] for q in queue)
    cost = sum(q[0] * q[1] for q in queue)
    return ([lot for _, _, lot in parsed],
            round(shares, 8) if shares > _EPS else 0.0, round(cost, 2))


def _portfolio(raw, entries: list, current: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("portfolio must be an object keyed by ticker id")
    roles = _roles(entries)
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
        if pid in out:
            raise ConfigInvalid(f"portfolio: duplicate id {pid}")
        stored_lots = ((current or {}).get(pid) or {}).get("lots")
        if "lots" in pos and pos["lots"]:
            lots, shares, cost = _lots(pos["lots"], f"portfolio.{pid}.lots")
            if shares <= 0:
                raise ConfigInvalid(
                    f"portfolio.{pid}.lots leave no shares; remove the "
                    "position instead")
            out[pid] = {"shares": shares, "investedAmount": cost,
                        "lots": lots}
            continue
        shares = _number(pos.get("shares"), f"portfolio.{pid}.shares")
        if shares <= 0:
            raise ConfigInvalid(f"portfolio.{pid}.shares must be > 0")
        invested = pos.get("investedAmount")
        if invested is not None:
            invested = _number(invested, f"portfolio.{pid}.investedAmount")
            if invested < 0:
                raise ConfigInvalid(
                    f"portfolio.{pid}.investedAmount must be >= 0 or null")
        if "lots" not in pos and stored_lots:
            # Stored lots stay authoritative: a plain save may only repeat
            # the lot-derived values, never silently override them.
            lots, d_shares, d_cost = _lots(stored_lots,
                                           f"portfolio.{pid}.lots")
            if (d_shares <= 0 or abs(shares - d_shares) > _EPS
                    or invested is None or abs(invested - d_cost) > 0.01):
                raise ConfigInvalid(
                    f"portfolio.{pid} has dated lots; edit the lots, or "
                    'send "lots": [] to drop them')
            out[pid] = {"shares": d_shares, "investedAmount": d_cost,
                        "lots": lots}
            continue
        out[pid] = {"shares": shares, "investedAmount": invested}
    return out


def _known_id(value, roles: dict, ctx: str) -> str:
    ident = str(value).strip().upper()
    if ident not in roles:
        raise ConfigInvalid(f"{ctx}: {ident or '(empty)'} is not on the "
                            "watchlist")
    return ident


def _performance_benchmark(value, roles: dict) -> str:
    if not isinstance(value, str):
        raise ConfigInvalid("performanceBenchmark must be a watchlist id")
    return _known_id(value, roles, "performanceBenchmark")


def _exposure_currency(raw, roles: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("exposureCurrency must be an object keyed by "
                            "ticker id")
    out = {}
    for key, code in raw.items():
        ident = _known_id(key, roles, "exposureCurrency")
        if not (isinstance(code, str) and len(code.strip()) == 3
                and code.strip().isascii() and code.strip().isalpha()):
            raise ConfigInvalid(f"exposureCurrency.{ident} must be a "
                                "3-letter currency code")
        out[ident] = code.strip().upper()
    return out


def _look_through(raw, roles: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("lookThrough must be an object keyed by fund id")
    out = {}
    for key, spec in raw.items():
        fid = _known_id(key, roles, "lookThrough")
        ctx = f"lookThrough.{fid}"
        if not isinstance(spec, dict):
            raise ConfigInvalid(f"{ctx} must be an object")
        weights_raw = spec.get("weights")
        if not isinstance(weights_raw, dict):
            raise ConfigInvalid(f"{ctx}.weights must be an object "
                                "{id: percent}")
        weights = {}
        for wkey, pct in weights_raw.items():
            wid = _known_id(wkey, roles, f"{ctx}.weights")
            if wid == fid:
                raise ConfigInvalid(f"{ctx}.weights cannot contain the fund "
                                    "itself")
            v = _number(pct, f"{ctx}.weights.{wid}")
            if not 0 < v <= 100:
                raise ConfigInvalid(f"{ctx}.weights.{wid} must be in "
                                    "(0, 100]")
            weights[wid] = v
        total = sum(weights.values())
        if total > _WEIGHT_SUM_MAX:
            raise ConfigInvalid(f"{ctx}.weights sum to {total:g}% (max 100)")
        as_of = spec.get("asOf")
        if as_of is not None:
            as_of = _iso_day(as_of, f"{ctx}.asOf").isoformat()
        source = spec.get("source")
        if source is not None:
            if not isinstance(source, str):
                raise ConfigInvalid(f"{ctx}.source must be a string")
            source = source.strip()[:200]
        out[fid] = {"asOf": as_of, "source": source, "weights": weights}
    return out


def _investment_policy(raw, roles: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("investmentPolicy must be an object")
    out = {}
    if raw.get("targets") is not None:
        targets_raw = raw["targets"]
        if not isinstance(targets_raw, dict) or not targets_raw:
            raise ConfigInvalid("investmentPolicy.targets must be a "
                                "non-empty object {id: percent}")
        targets = {}
        for key, pct in targets_raw.items():
            tid = _known_id(key, roles, "investmentPolicy.targets")
            if roles[tid] != "holding":
                raise ConfigInvalid(f"investmentPolicy.targets.{tid}: only "
                                    "holdings can have a target")
            v = _number(pct, f"investmentPolicy.targets.{tid}")
            if not 0 <= v <= 100:
                raise ConfigInvalid(f"investmentPolicy.targets.{tid} must be "
                                    "in [0, 100]")
            targets[tid] = v
        total = sum(targets.values())
        if abs(total - 100.0) > _TARGET_SUM_TOL:
            raise ConfigInvalid("investmentPolicy.targets must sum to 100 "
                                f"(+- {_TARGET_SUM_TOL:g}), got {total:g}")
        out["targets"] = targets
    if raw.get("maxSingleStockPct") is not None:
        v = _number(raw["maxSingleStockPct"],
                    "investmentPolicy.maxSingleStockPct")
        if not 0 < v <= 100:
            raise ConfigInvalid("investmentPolicy.maxSingleStockPct must be "
                                "in (0, 100]")
        out["maxSingleStockPct"] = v
    if raw.get("band") is not None:
        band_raw = raw["band"]
        if not isinstance(band_raw, dict):
            raise ConfigInvalid("investmentPolicy.band must be an object")
        band = {}
        for k in ("absPct", "relPct"):
            if band_raw.get(k) is not None:
                v = _number(band_raw[k], f"investmentPolicy.band.{k}")
                if v < 0:
                    raise ConfigInvalid(f"investmentPolicy.band.{k} must be "
                                        ">= 0")
                band[k] = v
        out["band"] = band
    if raw.get("monthlyContributionEur") is not None:
        v = _number(raw["monthlyContributionEur"],
                    "investmentPolicy.monthlyContributionEur")
        if v < 0:
            raise ConfigInvalid("investmentPolicy.monthlyContributionEur "
                                "must be >= 0")
        out["monthlyContributionEur"] = v
    if raw.get("trendFilter") is not None:
        if not isinstance(raw["trendFilter"], bool):
            raise ConfigInvalid("investmentPolicy.trendFilter must be true "
                                "or false")
        out["trendFilter"] = raw["trendFilter"]
    return out


def _tax_residency(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip().upper() in _RESIDENCIES:
        return value.strip().upper()
    raise ConfigInvalid("taxResidency must be 'CZ' or null")


def _set_optional(cfg: dict, body: dict, key: str, check, roles: dict) -> None:
    """Apply ``body[key]`` through ``check``; null/"" removes the stored key
    (reader default). An absent key leaves the stored value untouched."""
    if key not in body:
        return
    value = body[key]
    if value is None or value == "":
        cfg.pop(key, None)
    else:
        cfg[key] = check(value, roles)


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
                                      cfg.get("watchlist") or [],
                                      cfg.get("portfolio"))
    elif wl_changed:
        # Watchlist changed alone: stored positions must still point at a
        # holding that remains on it (never silently drop a cost basis).
        _portfolio(cfg.get("portfolio") or {}, cfg["watchlist"])

    roles = _roles(cfg.get("watchlist") or [])
    _set_optional(cfg, body, "performanceBenchmark", _performance_benchmark,
                  roles)
    _set_optional(cfg, body, "exposureCurrency", _exposure_currency, roles)
    _set_optional(cfg, body, "lookThrough", _look_through, roles)
    _set_optional(cfg, body, "investmentPolicy", _investment_policy, roles)
    if "taxResidency" in body:
        cfg["taxResidency"] = _tax_residency(body["taxResidency"])

    if not wl_changed:
        return []
    return [e["id"] for e in cfg["watchlist"] if e["id"] not in old_ids]
