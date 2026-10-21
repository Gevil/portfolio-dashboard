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
  contribution, trend filter), ``taxResidency`` (``"CZ"``, ``"NL"`` or null),
  ``taxNL`` (NL box 3 inputs: ``fiscalPartner``, ``otherAssets`` per year,
  ``dividendTaxWithheldEur`` per year; unknown keys rejected).

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
_RESIDENCIES = ("CZ", "NL")
_TAX_NL_KEYS = ("fiscalPartner", "otherAssets", "dividendTaxWithheldEur")
_TAX_NL_ASSET_KEYS = ("bankEur", "otherEur", "debtsEur")
_TAX_NL_YEARS = (2020, 2100)
_PAPER_MODES = ("off", "shadow", "demo")
_PAPER_LIVE_MODES = ("live-approve", "live-auto")
_PAPER_WEIGHT_KINDS = ("equity", "etf", "index")
_PAPER_UNIVERSE_SOURCES = ("watchlist", "paper_universe")
_PAPER_KILL_SWITCH_KEYS = ("drawdownPct", "vsBaselinePct", "dailyLossPct")
# costModel key -> max (all >= 0)
_PAPER_COST_MODEL_MAX = {"spreadBps": 500.0, "commissionEur": 50.0,
                         "fxBps": 500.0}
# key -> (min, max) inclusive
_PAPER_LIMIT_RANGES = {
    "minCashPct": (0.0, 100.0),
    "maxTurnoverPctPerDay": (0.0, 200.0),
    "maxOrdersPerDay": (1.0, 100.0),
    "minTradeEur": (0.0, 100000.0),
    "rebalanceBandPct": (0.0, 100.0),
    "maxSpreadOrStaleS": (1.0, 86400.0),
    "limitSlippageBps": (0.0, 1000.0),
    # open universe (universeSource "paper_universe")
    "maxPositions": (1.0, 100.0),
    "maxSectorPct": (1.0, 100.0),
    "minPositionEur": (0.0, 100000.0),
    "adjustFraction": (0.0, 1.0),
}


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
    raise ConfigInvalid("taxResidency must be 'CZ', 'NL' or null")


def _tax_nl_year(key, ctx: str) -> str:
    text = str(key).strip() if isinstance(key, (str, int)) and not isinstance(key, bool) else ""
    if not (text.isascii() and text.isdigit()
            and _TAX_NL_YEARS[0] <= int(text) <= _TAX_NL_YEARS[1]):
        raise ConfigInvalid(f"{ctx}: year must be {_TAX_NL_YEARS[0]}..{_TAX_NL_YEARS[1]}, "
                            f"got {key!r}")
    return str(int(text))


def _tax_nl_amount(value, ctx: str) -> float:
    v = _number(value, ctx)
    if v < 0:
        raise ConfigInvalid(f"{ctx} must be >= 0")
    return v


def _tax_nl(raw, roles: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("taxNL must be an object")
    unknown = sorted(str(k) for k in raw if k not in _TAX_NL_KEYS)
    if unknown:
        raise ConfigInvalid("taxNL: unknown key(s) " + ", ".join(unknown))
    out = {}
    if raw.get("fiscalPartner") is not None:
        if not isinstance(raw["fiscalPartner"], bool):
            raise ConfigInvalid("taxNL.fiscalPartner must be true or false")
        out["fiscalPartner"] = raw["fiscalPartner"]
    if raw.get("otherAssets") is not None:
        assets_raw = raw["otherAssets"]
        if not isinstance(assets_raw, dict):
            raise ConfigInvalid("taxNL.otherAssets must be an object keyed by year")
        assets = {}
        for key, row in assets_raw.items():
            year = _tax_nl_year(key, "taxNL.otherAssets")
            if not isinstance(row, dict):
                raise ConfigInvalid(f"taxNL.otherAssets.{year} must be an object")
            bad = sorted(str(k) for k in row if k not in _TAX_NL_ASSET_KEYS)
            if bad:
                raise ConfigInvalid(f"taxNL.otherAssets.{year}: unknown key(s) "
                                    + ", ".join(bad))
            assets[year] = {k: _tax_nl_amount(row[k], f"taxNL.otherAssets.{year}.{k}")
                            for k in _TAX_NL_ASSET_KEYS if row.get(k) is not None}
        out["otherAssets"] = assets
    if raw.get("dividendTaxWithheldEur") is not None:
        div_raw = raw["dividendTaxWithheldEur"]
        if not isinstance(div_raw, dict):
            raise ConfigInvalid("taxNL.dividendTaxWithheldEur must be an object "
                                "keyed by year")
        out["dividendTaxWithheldEur"] = {
            _tax_nl_year(k, "taxNL.dividendTaxWithheldEur"):
            _tax_nl_amount(v, f"taxNL.dividendTaxWithheldEur.{k}")
            for k, v in div_raw.items()}
    return out


def _paper_max_weight_pct(raw) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise ConfigInvalid("paperTrading.limits.maxWeightPct must be a "
                            "non-empty object {kind: percent}")
    out = {}
    for key, pct in raw.items():
        k = str(key).strip().lower()
        if k not in _PAPER_WEIGHT_KINDS:
            raise ConfigInvalid(
                f"paperTrading.limits.maxWeightPct.{key}: unknown kind "
                f"(expected {', '.join(_PAPER_WEIGHT_KINDS)})")
        v = _number(pct, f"paperTrading.limits.maxWeightPct.{k}")
        if not 0 < v <= 100:
            raise ConfigInvalid(
                f"paperTrading.limits.maxWeightPct.{k} must be in (0, 100]")
        out[k] = v
    return out


def _paper_kill_switch(raw) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise ConfigInvalid("paperTrading.limits.killSwitch must be a "
                            "non-empty object")
    unknown = set(raw) - set(_PAPER_KILL_SWITCH_KEYS)
    if unknown:
        raise ConfigInvalid("paperTrading.limits.killSwitch: unknown key(s) "
                            + ", ".join(sorted(map(str, unknown))))
    out = {}
    for key in _PAPER_KILL_SWITCH_KEYS:
        if key in raw:
            v = _number(raw[key], f"paperTrading.limits.killSwitch.{key}")
            if not 0 < v <= 100:
                raise ConfigInvalid(
                    f"paperTrading.limits.killSwitch.{key} must be in "
                    "(0, 100]")
            out[key] = v
    return out


def _paper_limits(raw) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("paperTrading.limits must be an object")
    unknown = (set(raw) - set(_PAPER_LIMIT_RANGES)
              - {"maxWeightPct", "killSwitch"})
    if unknown:
        raise ConfigInvalid("paperTrading.limits: unknown key(s) "
                            + ", ".join(sorted(map(str, unknown))))
    out = {}
    if "maxWeightPct" in raw:
        out["maxWeightPct"] = _paper_max_weight_pct(raw["maxWeightPct"])
    if "killSwitch" in raw:
        out["killSwitch"] = _paper_kill_switch(raw["killSwitch"])
    for key, (lo, hi) in _PAPER_LIMIT_RANGES.items():
        if key in raw:
            v = _number(raw[key], f"paperTrading.limits.{key}")
            if not lo <= v <= hi or (key == "adjustFraction" and v <= 0):
                raise ConfigInvalid(
                    f"paperTrading.limits.{key} must be in "
                    + (f"(0, {hi:g}]" if key == "adjustFraction" else f"[{lo:g}, {hi:g}]"))
            out[key] = v
    return out


def _paper_universe(raw, roles: dict) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise ConfigInvalid("paperTrading.universe must be a non-empty "
                            "object {id: {t212: ticker}}")
    out = {}
    for key, spec in raw.items():
        pid = _known_id(key, roles, "paperTrading.universe")
        if roles[pid] != "holding":
            raise ConfigInvalid(f"paperTrading.universe.{pid}: only "
                                "holdings can be traded")
        if not isinstance(spec, dict):
            raise ConfigInvalid(f"paperTrading.universe.{pid} must be an "
                                "object {t212: ticker}")
        t212 = spec.get("t212")
        if not isinstance(t212, str) or not t212.strip():
            raise ConfigInvalid(f"paperTrading.universe.{pid}.t212 must be "
                                "a non-empty string")
        out[pid] = {"t212": t212.strip()}
    return out


def _paper_cost_model(raw) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("paperTrading.costModel must be an object")
    unknown = set(raw) - set(_PAPER_COST_MODEL_MAX)
    if unknown:
        raise ConfigInvalid("paperTrading.costModel: unknown key(s) "
                            + ", ".join(sorted(map(str, unknown))))
    out = {}
    for key, hi in _PAPER_COST_MODEL_MAX.items():
        if key in raw:
            v = _number(raw[key], f"paperTrading.costModel.{key}")
            if not 0 <= v <= hi:
                raise ConfigInvalid(
                    f"paperTrading.costModel.{key} must be in [0, {hi:g}]")
            out[key] = v
    return out


_PAPER_BUCKET_IDS = ("intraday",)
_PAPER_BUCKET_KEYS = ("enabled", "mode", "strategyVersion", "budgetEur",
                      "intervalMin", "flatAtClose", "limits")
_PAPER_INTERVALS = (15, 30, 60, 120)


def _paper_bucket(bid: str, raw) -> dict:
    ctx = f"paperTrading.buckets.{bid}"
    if not isinstance(raw, dict):
        raise ConfigInvalid(f"{ctx} must be an object")
    unknown = set(raw) - set(_PAPER_BUCKET_KEYS)
    if unknown:
        hint = (" (the universe is shared with the daily bucket)"
                if "universe" in unknown else "")
        raise ConfigInvalid(f"{ctx}: unknown key(s) "
                            + ", ".join(sorted(map(str, unknown))) + hint)
    out = {}
    for key in ("enabled", "flatAtClose"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ConfigInvalid(f"{ctx}.{key} must be true or false")
            out[key] = raw[key]
    if "mode" in raw:
        mode = raw["mode"]
        if not isinstance(mode, str):
            raise ConfigInvalid(f"{ctx}.mode must be a string")
        m = mode.strip().lower()
        if m in _PAPER_LIVE_MODES:
            raise ConfigInvalid(f"{ctx}.mode {m!r} is not available yet "
                                "(live trading is a later phase)")
        if m not in _PAPER_MODES:
            raise ConfigInvalid(f"{ctx}.mode must be one of "
                                + ", ".join(_PAPER_MODES))
        out["mode"] = m
    if "strategyVersion" in raw:
        sv = raw["strategyVersion"]
        if not isinstance(sv, str) or not sv.strip():
            raise ConfigInvalid(f"{ctx}.strategyVersion must be a non-empty string")
        sv = sv.strip()
        if len(sv) > 32:
            raise ConfigInvalid(f"{ctx}.strategyVersion must be <= 32 characters")
        out["strategyVersion"] = sv
    if "budgetEur" in raw:
        v = _number(raw["budgetEur"], f"{ctx}.budgetEur")
        if not 10 <= v <= 100000:
            raise ConfigInvalid(f"{ctx}.budgetEur must be in [10, 100000]")
        out["budgetEur"] = v
    if "intervalMin" in raw:
        v = _number(raw["intervalMin"], f"{ctx}.intervalMin")
        if v not in _PAPER_INTERVALS:
            raise ConfigInvalid(f"{ctx}.intervalMin must be one of "
                                + ", ".join(map(str, _PAPER_INTERVALS)))
        out["intervalMin"] = int(v)
    if "limits" in raw:
        out["limits"] = _paper_limits(raw["limits"])
    return out


def _paper_buckets(raw) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("paperTrading.buckets must be an object")
    unknown = [str(k) for k in raw if k not in _PAPER_BUCKET_IDS]
    if unknown:
        raise ConfigInvalid("paperTrading.buckets: unknown bucket(s) "
                            + ", ".join(sorted(unknown))
                            + f" (only {', '.join(_PAPER_BUCKET_IDS)} is allowed)")
    return {bid: _paper_bucket(bid, spec) for bid, spec in raw.items()}


def _paper_trading(raw, roles: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigInvalid("paperTrading must be an object")
    out = {}
    if "mode" in raw:
        mode = raw["mode"]
        if not isinstance(mode, str):
            raise ConfigInvalid("paperTrading.mode must be a string")
        m = mode.strip().lower()
        if m in _PAPER_LIVE_MODES:
            raise ConfigInvalid(
                f"paperTrading.mode {m!r} is not available yet (live "
                "trading is a later phase)")
        if m not in _PAPER_MODES:
            raise ConfigInvalid("paperTrading.mode must be one of "
                                + ", ".join(_PAPER_MODES))
        out["mode"] = m
    if "strategyVersion" in raw:
        sv = raw["strategyVersion"]
        if not isinstance(sv, str) or not sv.strip():
            raise ConfigInvalid(
                "paperTrading.strategyVersion must be a non-empty string")
        sv = sv.strip()
        if len(sv) > 32:
            raise ConfigInvalid(
                "paperTrading.strategyVersion must be <= 32 characters")
        out["strategyVersion"] = sv
    if "budgetEur" in raw:
        v = _number(raw["budgetEur"], "paperTrading.budgetEur")
        if not 10 <= v <= 100000:
            raise ConfigInvalid("paperTrading.budgetEur must be in "
                                "[10, 100000]")
        out["budgetEur"] = v
    if "universeSource" in raw:
        src = raw["universeSource"]
        if not isinstance(src, str) or src.strip().lower() not in _PAPER_UNIVERSE_SOURCES:
            raise ConfigInvalid("paperTrading.universeSource must be one of "
                                + ", ".join(_PAPER_UNIVERSE_SOURCES))
        out["universeSource"] = src.strip().lower()
    if "universe" in raw:
        out["universe"] = _paper_universe(raw["universe"], roles)
    if "limits" in raw:
        out["limits"] = _paper_limits(raw["limits"])
    if "costModel" in raw:
        out["costModel"] = _paper_cost_model(raw["costModel"])
    if "buckets" in raw:
        out["buckets"] = _paper_buckets(raw["buckets"])
    if "schedule" in raw:
        sched = raw["schedule"]
        if not isinstance(sched, dict):
            raise ConfigInvalid("paperTrading.schedule must be an object")
        out["schedule"] = sched
    return out


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
    _set_optional(cfg, body, "paperTrading", _paper_trading, roles)
    _set_optional(cfg, body, "taxNL", _tax_nl, roles)
    if "taxResidency" in body:
        cfg["taxResidency"] = _tax_residency(body["taxResidency"])

    if not wl_changed:
        return []
    return [e["id"] for e in cfg["watchlist"] if e["id"] not in old_ids]
