"""Czech time-test and annual-limit bookkeeping from the owner's dated lots.

Informational only, never tax advice. Everything is derived from
``config.portfolio[id].lots`` (``{date, shares, priceEur, feesEur, side}``,
validated by ``config_edit``) and ``config.taxResidency``; nothing is stored.

Rules (all of them in this module, so a change is one edit):

- **FIFO.** Per holding, lots are ordered by date (same day: buys first) and
  every sell consumes the oldest remaining buy lots first.
- **Time test.** A buy lot is exempt once ``today >= unlockDate`` where
  ``unlockDate = purchase date + 3 years + 1 day``: the period must be MORE than
  three years, so the 3-year anniversary itself is still taxable. A purchase on
  29 Feb has no anniversary in a common year; the period then ends on the last
  day of February and the lot unlocks on 1 Mar.
- **Annual limit.** ``ytd.grossProceedsCzk`` is the sum over this calendar
  year's sells of ``shares x priceEur x EURCZK(sale date)`` - gross, no fee
  deduction; the rate is the ECB EUR/CZK reference rate of the sale date
  (``forex.eur_rate_on``, Frankfurter v1). ``grossProceedsTaxableLotsCzk`` counts
  only the part of each sale matched to lots that were still younger than 3
  years on the sale date. Whether time-test-exempt sales count toward the CZK
  100,000 limit is NOT decided here (see ``notes``): the warning at >= 80 % of
  the limit uses the conservative all-sales total.
- ``NL`` is dispatched to ``tax_nl`` (box 3 estimate); ``sell_tax_status`` then
  carries ``statusText`` and ``taxable`` None (sales are not taxed individually).
  Any other residency gets an empty payload (``lots: []``, null YTD
  figures): the rules above are Czech.

``tax_payload()`` backs ``GET /api/tax``. ``sell_tax_status()`` is the sync
helper other modules call (evidence ``policy_context``, digest tax tag).
"""
import asyncio
import calendar
import datetime
import logging

from app.api import config_store, forex

log = logging.getLogger("tax")

RESIDENCY_CZ = "CZ"
RESIDENCY_NL = "NL"
NL_SELL_STATUS = "NL box 3: sales are not taxed individually; assessed on 1 Jan value"
THRESHOLD_CZK = 100_000
WARN_PCT = 80.0
TIME_TEST_YEARS = 3
RATE_SOURCE = ("ECB EUR/CZK reference rate of each sale date (Frankfurter v1; "
               "the latest reference day on or before the sale)")
_EPS = 1e-9

NOTES = (
    "Informational bookkeeping from your own lots, not tax advice.",
    "Sales are matched to purchases FIFO; fees are not deducted (gross proceeds).",
    "A lot is time-test exempt once today >= unlockDate (purchase date + 3 "
    "years + 1 day; a 29 Feb purchase unlocks on 1 Mar).",
    "Whether sales of time-test-exempt lots count toward the CZK 100,000 "
    "annual limit must be verified with a tax adviser. grossProceedsCzk (and "
    "the warning) conservatively counts ALL sales this calendar year; "
    "grossProceedsTaxableLotsCzk counts only sales of lots younger than 3 years.",
)


def _today() -> datetime.date:
    return datetime.date.today()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def unlock_date(bought: datetime.date) -> datetime.date:
    """First day a lot bought on ``bought`` passes the time test."""
    year = bought.year + TIME_TEST_YEARS
    day = min(bought.day, calendar.monthrange(year, bought.month)[1])
    return datetime.date(year, bought.month, day) + datetime.timedelta(days=1)


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _residency(cfg: dict) -> str | None:
    raw = cfg.get("taxResidency")
    code = str(raw).strip().upper() if isinstance(raw, str) else ""
    return code or None


def _portfolio(cfg: dict) -> dict:
    pf = cfg.get("portfolio")
    return pf if isinstance(pf, dict) else {}


def _parse_lots(raw) -> list[dict]:
    """Well-formed lots sorted (date, buys first). A malformed row (the config
    is validated, but may be hand-edited) is skipped, never fatal."""
    out = []
    for lot in raw if isinstance(raw, list) else []:
        if not isinstance(lot, dict):
            continue
        try:
            day = datetime.date.fromisoformat(str(lot.get("date")))
        except ValueError:
            continue
        shares, price = _num(lot.get("shares")), _num(lot.get("priceEur"))
        side = str(lot.get("side") or "").strip().lower()
        if shares is None or shares <= 0 or price is None or side not in ("buy", "sell"):
            continue
        out.append({"date": day, "shares": shares, "priceEur": price, "side": side})
    out.sort(key=lambda lot: (lot["date"], lot["side"] != "buy"))
    return out


def _lots_by_id(cfg: dict) -> dict[str, list[dict]]:
    out = {}
    for key, pos in _portfolio(cfg).items():
        lots = _parse_lots(pos.get("lots")) if isinstance(pos, dict) else []
        if lots:
            out[str(key).strip().upper()] = lots
    return out


def _fifo(lots: list[dict]) -> tuple[list[dict], list[dict]]:
    """FIFO over sorted lots -> (buy lots with ``remaining``, sells with the
    buy lots they consumed). A sell beyond the held shares (invalid input)
    reports the excess as ``unmatched``."""
    buys: list[dict] = []
    sells: list[dict] = []
    queue: list[dict] = []
    for lot in lots:
        if lot["side"] == "buy":
            rec = {"date": lot["date"], "shares": lot["shares"],
                   "remaining": lot["shares"], "priceEur": lot["priceEur"],
                   "unlock": unlock_date(lot["date"])}
            buys.append(rec)
            queue.append(rec)
            continue
        need = lot["shares"]
        matched = []
        while need > _EPS and queue:
            head = queue[0]
            take = min(head["remaining"], need)
            head["remaining"] -= take
            need -= take
            matched.append({"buyDate": head["date"], "unlock": head["unlock"],
                            "shares": take})
            if head["remaining"] <= _EPS:
                head["remaining"] = 0.0
                queue.pop(0)
        sells.append({"date": lot["date"], "shares": lot["shares"],
                      "priceEur": lot["priceEur"], "matched": matched,
                      "unmatched": max(need, 0.0)})
    return buys, sells


def _r(value: float, nd: int = 8) -> float:
    return round(value, nd)


def _empty(residency: str | None, year: int) -> dict:
    return {"residency": residency, "lots": [],
            "ytd": {"year": year, "grossProceedsCzk": None,
                    "grossProceedsTaxableLotsCzk": None,
                    "thresholdCzk": THRESHOLD_CZK, "pct": None, "rate": None,
                    "complete": None, "sales": []},
            "warnings": [], "notes": [], "asOf": _now_iso()}


async def tax_payload() -> dict:
    """``GET /api/tax``. Never raises on missing config, lots or rates."""
    cfg = config_store.read()
    residency = _residency(cfg)
    today = _today()
    out = _empty(residency, today.year)
    if residency == RESIDENCY_NL:
        from app.api import tax_nl      # lazy: tax_nl imports this module
        return await tax_nl.payload()
    if residency != RESIDENCY_CZ:
        return out

    by_id = _lots_by_id(cfg)
    rows, sales = [], []
    for pid, lots in sorted(by_id.items()):
        buys, sells = _fifo(lots)
        for b in buys:
            rows.append({"id": pid, "date": b["date"].isoformat(),
                         "shares": _r(b["shares"]),
                         "remainingShares": _r(b["remaining"]),
                         "ageDays": (today - b["date"]).days,
                         "unlockDate": b["unlock"].isoformat(),
                         "exempt": today >= b["unlock"]})
        sales += [(pid, s) for s in sells if s["date"].year == today.year]
    rows.sort(key=lambda r: (r["date"], r["id"]))

    warnings = []
    no_lots = sorted(
        str(k).strip().upper() for k, pos in _portfolio(cfg).items()
        if isinstance(pos, dict) and str(k).strip().upper() not in by_id
        and (_num(pos.get("shares")) or 0) > 0)
    if no_lots:
        warnings.append({"code": "lots_missing", "ids": no_lots,
                         "message": "No dated lots: age and time-test status "
                                    "unknown for " + ", ".join(no_lots)})

    dates = sorted({s["date"].isoformat() for _, s in sales})
    found = await asyncio.gather(*[forex.eur_rate_on("CZK", d) for d in dates])
    rates = dict(zip(dates, found))

    gross = taxable = 0.0
    sale_rows, missing = [], []
    for pid, s in sales:
        day = s["date"].isoformat()
        rate = rates.get(day)
        if rate is None:
            missing.append(f"{pid} {day}")
            continue
        total = s["shares"] * s["priceEur"] * rate
        young = s["unmatched"] + sum(m["shares"] for m in s["matched"]
                                     if s["date"] < m["unlock"])
        young_total = young * s["priceEur"] * rate
        gross += total
        taxable += young_total
        sale_rows.append({"id": pid, "date": day, "shares": _r(s["shares"]),
                          "priceEur": s["priceEur"], "rateCzk": rate,
                          "grossCzk": _r(total, 2),
                          "taxableLotsGrossCzk": _r(young_total, 2)})
    if missing:
        warnings.append({"code": "rate_missing", "ids": missing,
                         "message": "No EUR/CZK rate for: " + ", ".join(missing)
                                    + "; those sales are NOT in the YTD totals"})
    pct = gross * 100.0 / THRESHOLD_CZK
    if pct >= 100.0:
        warnings.append({"code": "czk_limit_exceeded",
                         "message": f"Gross sale proceeds this year are {pct:.0f}% of "
                                    f"the CZK {THRESHOLD_CZK:,} limit (all sales counted)"})
    elif pct >= WARN_PCT:
        warnings.append({"code": "czk_limit_near",
                         "message": f"Gross sale proceeds this year are {pct:.0f}% of "
                                    f"the CZK {THRESHOLD_CZK:,} limit (all sales counted)"})

    out.update(
        lots=rows, warnings=warnings, notes=list(NOTES),
        ytd={"year": today.year, "grossProceedsCzk": _r(gross, 2),
             "grossProceedsTaxableLotsCzk": _r(taxable, 2),
             "thresholdCzk": THRESHOLD_CZK, "pct": _r(pct, 2),
             "rate": {"source": RATE_SOURCE, "byDate": {d: r for d, r in rates.items()
                                                       if r is not None}},
             "complete": not missing, "sales": sale_rows})
    return out


def sell_tax_status(ident, shares=None) -> dict:
    """Time-test status of selling ``shares`` of ``ident`` now (the whole
    position when None), from the config lots; the shares sold are the
    FIFO-next ones.

    -> ``{taxable, youngestLotDate, unlockDate, exemptShares, taxableShares,
    residency, lotsKnown}``. ``youngestLotDate`` / ``unlockDate`` belong to the
    youngest lot the sale touches (the date the whole sale turns exempt).
    No dated lots -> ``taxable`` None and ``lotsKnown`` False. ``taxable`` is
    None as well for a residency other than CZ (the 3-year rule is Czech) or an
    unusable ``shares``; True iff any touched share is younger than the time
    test. Sync, no I/O besides ``config_store.read()``.
    """
    pid = str(ident or "").strip().upper()
    cfg = config_store.read()
    out = {"taxable": None, "youngestLotDate": None, "unlockDate": None,
           "exemptShares": 0.0, "taxableShares": 0.0,
           "residency": _residency(cfg), "lotsKnown": False}
    if out["residency"] == RESIDENCY_NL:
        out["statusText"] = NL_SELL_STATUS
    pos = _portfolio(cfg).get(pid)
    lots = _parse_lots(pos.get("lots")) if isinstance(pos, dict) else []
    if not lots:
        return out
    out["lotsKnown"] = True
    queue = [b for b in _fifo(lots)[0] if b["remaining"] > _EPS]
    held = sum(b["remaining"] for b in queue)
    want = held if shares is None else _num(shares)
    if want is None:
        return out
    need = min(max(want, 0.0), held)
    exempt = young = 0.0
    last = None
    today = _today()
    for b in queue:
        if need <= _EPS:
            break
        take = min(b["remaining"], need)
        need -= take
        if today >= b["unlock"]:
            exempt += take
        else:
            young += take
        last = b
    out.update(exemptShares=_r(exempt), taxableShares=_r(young),
               youngestLotDate=last["date"].isoformat() if last else None,
               unlockDate=last["unlock"].isoformat() if last else None)
    if out["residency"] == RESIDENCY_CZ:
        out["taxable"] = young > _EPS
    return out
