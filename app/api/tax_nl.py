"""Dutch box 3 (taxResidency ``"NL"``): informational estimate, not tax advice.

Spec and cited parameters: ``docs/research/nl-tax.md`` ("Implementable rules for
the dashboard"). Everything is derived from ``config.portfolio[id].lots``,
``config.taxNL`` and the listing's raw daily closes; nothing is stored. The
parameter table below is code on purpose (no env override); a change is one
edit here plus the docs.

Per tax year Y (2025 ... current year):

- **Forfait** (peildatum 1 Jan): the dashboard's investments are *overige
  bezittingen* valued at ``shares held at the start of 1 Jan (lots dated
  before it) x last close on or before 31 Dec of Y-1``. Banktegoeden, other
  assets and debts come only from ``taxNL.otherAssets[Y]``; without them the
  result is labelled ``investments_only`` (warning ``other_assets_missing``).
  A position without lots is valued with its CURRENT shares, and only for the
  current year (warning ``history_basis_current_shares``); for a past year it
  is left out (warning ``lots_missing``, ``complete`` false).
- **Tegenbewijs**: ``max(0, vEnd - vStart - deposits + withdrawals +
  dividends)``, no heffingvrij, no debt threshold, no cost deduction (a buy
  deposits ``shares x price``, fees are ignored). Completed year: vEnd = value
  at the 31 Dec close. Current year: year to date, vEnd = today's value,
  labelled estimate. Dividends are the gross per-share dividends with an
  ex-date in the year x shares held before the ex-date
  (``events.dividend_history``, yfinance). Only the dashboard's investments are
  covered: with other assets the tegenbewijs is flagged ``partial``.
- ``tax = min(forfait, tegenbewijs)``; ``applies`` names the smaller one
  (a tie is the forfait). Net = tax - dividendbelasting credit. For Dutch
  stocks (ISIN ``NL...`` from the watchlist entry, else an Amsterdam ``.AS``
  equity listing) 15 % of the gross dividends is credited as an ESTIMATE
  unless ``taxNL.dividendTaxWithheldEur[Y]`` gives the real figure. Foreign
  withholding is information only, never credited.
- **Projection**: next year's forfait from today's value, one scenario per
  law/proposal parameter set, always an estimate.
"""
import bisect
import datetime
import logging

from app.api import config_store, events, performance, portfolio, prices, registry, tax

log = logging.getLogger("tax_nl")

RESIDENCY_NL = tax.RESIDENCY_NL
DIVIDEND_TAX_RATE = 0.15
FIRST_YEAR = 2025
_EPS = 1e-9

DISCLAIMER = ("Informational estimate from your own lots and the inputs in "
              "taxNL, not tax advice. Parameters marked unverified or "
              "provisional can change; check the Belastingdienst figures "
              "before filing.")

_WETIB = "https://wetten.overheid.nl/BWBR0011353/2026-02-21"
_WETIB25 = "https://wetten.overheid.nl/BWBR0011353/2025-01-01"
_STOLWIJK = "https://stolwijkkennisnetwerk.nl/forfaits-box-3-2025-en-2026/"
_INFOBAK = "https://infobak.nl/financien/fictief-rendement-box-3/"
_KB = ("https://www.eerstekamer.nl/brief_in/20260930/voorstellen_op_box_3_koopkracht/"
       "f=/vn1fdr15xiz6.pdf")
_DELOITTE = ("https://www.deloitte.com/nl/nl/services/tax/perspectives/"
             "kamerbrief-toekomst-box-3.html")
_EK = "https://www.eerstekamer.nl/wetsvoorstel/36748_wet_werkelijk_rendement_box"
_RO = "https://www.rijksoverheid.nl/themas/werk/inkomstenbelasting/plannen-werkelijk-rendement-box-3"

# Per person. ``unverified`` lists the fields that are [UNVERIFIED] (spec table)
# or carried over from 2026 as an assumption (2027).
PARAMS: dict[int, dict] = {
    2025: {"pBank": 0.0137, "pOther": 0.0588, "pDebt": 0.0270,
           "taxFree": 57_684.0, "debtThreshold": 3_800.0, "rate": 0.36,
           "status": "final",
           "unverified": ["pOther", "debtThreshold", "rate"],
           "sources": [_WETIB25, _STOLWIJK, _INFOBAK]},
    2026: {"pBank": 0.0128, "pOther": 0.0600, "pDebt": 0.0270,
           "taxFree": 59_357.0, "debtThreshold": 3_800.0, "rate": 0.36,
           "status": "provisional",     # bank/debt provisional until early 2027
           "unverified": ["pBank", "pDebt"],
           "sources": [_WETIB, _STOLWIJK, _INFOBAK]},
    2027: {"pBank": 0.0128, "pOther": 0.0637, "pDebt": 0.0270,
           "taxFree": 59_357.0, "debtThreshold": 3_800.0, "rate": 0.36,
           "status": "proposal",
           "unverified": ["pBank", "pDebt", "taxFree", "debtThreshold", "rate"],
           "sources": [_KB, _DELOITTE, _WETIB],
           "proposal": {"pBank": 0.0128, "pOther": 0.0787, "pDebt": 0.0270,
                        "taxFree": 30_846.0, "debtThreshold": 3_800.0,
                        "rate": 0.36, "status": "proposal",
                        "unverified": ["pBank", "pDebt", "debtThreshold", "rate"],
                        "sources": [_KB, _DELOITTE, _EK, _RO]}},
}


def _today() -> datetime.date:
    return tax._today()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _r(value, nd: int = 2):
    return None if value is None else round(value, nd)


def _closes(ident: str, period: str):
    return prices.daily_closes(ident, period)


def _dividends(ident: str):
    return events.dividend_history(ident)


def params_for(year: int, label: str = "law") -> dict | None:
    """Parameter set of ``year`` for the ``law`` or ``proposal`` scenario
    (None when the table has none)."""
    base = PARAMS.get(year)
    if base is None:
        return None
    if label == "proposal":
        return base.get("proposal")
    return {k: v for k, v in base.items() if k != "proposal"}


def _public_params(p: dict) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in p.items()
           if k != "proposal"}
    if "proposal" in p:
        out["proposal"] = _public_params(p["proposal"])
    return out


def forfait(bank: float, other: float, debt: float, p: dict, partner: bool) -> dict:
    """Spec formulas 1-4. Partners: heffingvrij and debt threshold x2, joint base."""
    mult = 2 if partner else 1
    threshold = p["debtThreshold"] * mult
    free = p["taxFree"] * mult
    debt_eff = max(0.0, debt - threshold)
    rg = bank + other - debt_eff
    ret = max(0.0, bank * p["pBank"] + other * p["pOther"] - debt_eff * p["pDebt"])
    if rg <= 0:
        return {"base": rg, "taxFree": free, "returnEur": ret,
                "effectiveReturnPct": 0.0, "taxEur": 0.0, "debtEur": debt_eff}
    taxable = max(0.0, rg - free)
    return {"base": rg, "taxFree": free, "returnEur": ret,
            "effectiveReturnPct": ret / rg * 100.0,
            "taxEur": p["rate"] * taxable * (ret / rg), "debtEur": debt_eff}


def tegenbewijs(v_start: float, v_end: float, deposits: float,
                withdrawals: float, dividends: float, rate: float) -> dict:
    """Spec formulas 5-6: floored at 0, no heffingvrij, no cost deduction."""
    raw = v_end - v_start - deposits + withdrawals + dividends
    actual = max(0.0, raw)
    return {"rawReturnEur": raw, "actualReturnEur": actual, "taxEur": rate * actual}


class _Warnings:
    def __init__(self):
        self._rows: dict[str, tuple[str, list[str]]] = {}

    def add(self, code: str, message: str, ident: str | None = None) -> None:
        msg, ids = self._rows.setdefault(code, (message, []))
        if ident is not None and ident not in ids:
            ids.append(ident)

    def out(self) -> list[dict]:
        return [{"code": c, "message": m + (": " + ", ".join(ids) if ids else ""),
                 "ids": ids} for c, (m, ids) in self._rows.items()]


def _is_dutch(pid: str) -> bool:
    entry = registry.get(pid) or {}
    isin = entry.get("isin")
    if isinstance(isin, str) and isin.strip():
        return isin.strip().upper().startswith("NL")
    sym = str((entry.get("listing") or {}).get("symbol") or "").upper()
    return entry.get("kind") == "equity" and sym.endswith(".AS")


def _held(lots: list[dict], before: datetime.date) -> float:
    """Shares held at the start of ``before`` (lots dated earlier)."""
    total = 0.0
    for lot in lots:
        if lot["date"] < before:
            total += lot["shares"] if lot["side"] == "buy" else -lot["shares"]
    return max(total, 0.0)


def _close_on_or_before(data: dict, day: datetime.date) -> float | None:
    i = bisect.bisect_right(data["dates"], day.isoformat()) - 1
    return data["closes"][i] if i >= 0 else None


def _taxnl(cfg: dict) -> dict:
    raw = cfg.get("taxNL")
    return raw if isinstance(raw, dict) else {}


def _assets(cfg: dict, year) -> dict | None:
    rows = _taxnl(cfg).get("otherAssets")
    row = rows.get(str(year)) if isinstance(rows, dict) else None
    if not isinstance(row, dict):
        return None
    return {k: float(tax._num(row.get(k)) or 0.0)
            for k in ("bankEur", "otherEur", "debtsEur")}


def _position_year(pid: str, d: dict, year: int, today: datetime.date,
                   snap_value: float | None, warn: _Warnings) -> dict | None:
    """vStart / vEnd / flows / dividends of one position in ``year``;
    None = position left out (warned)."""
    ytd = year == today.year
    lots = d["lots"]
    jan1 = datetime.date(year, 1, 1)
    if lots:
        s0 = _held(lots, jan1)
    elif ytd:
        s0 = d["shares"]
        warn.add("history_basis_current_shares",
                 "No dated lots: current shares used for the whole year", pid)
    else:
        warn.add("lots_missing", f"No dated lots, {year} value unknown for", pid)
        return None
    v_start = 0.0
    if s0 > _EPS:
        p0 = _close_on_or_before(d, datetime.date(year - 1, 12, 31))
        if p0 is None:
            warn.add("price_missing", f"No close on or before 31 Dec {year - 1} for", pid)
            return None
        v_start = s0 * p0
    if ytd:
        v_end = snap_value
        if v_end is None:
            warn.add("price_missing", "No current value for", pid)
    else:
        s1 = _held(lots, datetime.date(year + 1, 1, 1))
        p1 = _close_on_or_before(d, datetime.date(year, 12, 31)) if s1 > _EPS else 0.0
        if p1 is None:
            warn.add("price_missing", f"No close on or before 31 Dec {year} for", pid)
        v_end = None if p1 is None else s1 * p1
    last = min(datetime.date(year, 12, 31), today)
    deposits = withdrawals = 0.0
    for lot in lots:
        if jan1 <= lot["date"] <= last:
            amount = lot["shares"] * lot["priceEur"]
            if lot["side"] == "buy":
                deposits += amount
            else:
                withdrawals += amount
    dividends = 0.0
    if not d["div_ok"]:
        warn.add("dividends_unavailable", "Dividend history unavailable for", pid)
    for ex, per_share in d["divs"]:
        if jan1 <= ex <= last:
            shares = _held(lots, ex) if lots else d["shares"]
            dividends += shares * per_share
    return {"vStart": v_start, "vEnd": v_end, "deposits": deposits,
            "withdrawals": withdrawals, "dividends": dividends,
            "dutch": _is_dutch(pid)}


def _year_row(year: int, parts: dict[str, dict], cfg: dict, today: datetime.date,
              partner: bool, warn: _Warnings, complete: bool) -> dict:
    p = params_for(year, "law")
    ytd = year == today.year
    assets = _assets(cfg, year)
    if assets is None:
        warn.add("other_assets_missing",
                 "No taxNL.otherAssets: result covers the dashboard's investments only, "
                 "for year", str(year))
    vals = list(parts.values())
    v_start = sum(x["vStart"] for x in vals)
    f = forfait((assets or {}).get("bankEur", 0.0),
                v_start + (assets or {}).get("otherEur", 0.0),
                (assets or {}).get("debtsEur", 0.0), p, partner)
    deposits = sum(x["deposits"] for x in vals)
    withdrawals = sum(x["withdrawals"] for x in vals)
    dividends = sum(x["dividends"] for x in vals)
    ends = [x["vEnd"] for x in vals]
    v_end = None if any(e is None for e in ends) else sum(ends)
    teg = None
    if v_end is not None:
        t = tegenbewijs(v_start, v_end, deposits, withdrawals, dividends, p["rate"])
        teg = {"vStart": _r(v_start), "vEnd": _r(v_end), "deposits": _r(deposits),
               "withdrawals": _r(withdrawals), "dividends": _r(dividends),
               "rawReturnEur": _r(t["rawReturnEur"]),
               "actualReturnEur": _r(t["actualReturnEur"]), "taxEur": _r(t["taxEur"]),
               "partial": bool(assets and any(assets.values())),
               "estimate": ytd}
    applies = "forfait"
    tax_eur = f["taxEur"]
    if teg is not None and teg["taxEur"] < round(f["taxEur"], 2):
        applies, tax_eur = "tegenbewijs", teg["taxEur"]

    withheld = _taxnl(cfg).get("dividendTaxWithheldEur")
    real = tax._num(withheld.get(str(year))) if isinstance(withheld, dict) else None
    if real is not None:
        credit, credit_est = real, False
    else:
        credit = DIVIDEND_TAX_RATE * sum(x["dividends"] for x in vals if x["dutch"])
        credit_est = credit > 0
    notes = ["Forfait ignores trades during the year; the tegenbewijs counts unrealised "
             "changes, deducts no costs, has no heffingvrij and no loss carry-forward.",
             "Foreign dividend withholding is information only, not credited."]
    if p["status"] != "final":
        notes.append(f"{year} parameters are {p['status']}"
                     + (" (bank/debt percentages final only in early 2027)"
                        if year == 2026 else "") + ".")
    if ytd:
        notes.append("Current year: tegenbewijs is year to date at today's value (estimate).")
    if credit_est:
        notes.append("Dividendbelasting credit is 15% of gross Dutch dividends (estimate); "
                     "set taxNL.dividendTaxWithheldEur for the real figure.")
    if teg and teg["partial"]:
        notes.append("Tegenbewijs covers the dashboard's investments only; returns of "
                     "other assets are not included.")
    if not complete:
        notes.append("Incomplete: some positions were left out (see warnings).")
    return {
        "year": year, "peildatum": f"{year}-01-01",
        "portfolioJan1Eur": _r(v_start),
        "otherAssets": assets,
        "scope": "investments_only" if assets is None else "with_other_assets",
        "forfait": {"base": _r(f["base"]), "taxFree": _r(f["taxFree"]),
                    "returnEur": _r(f["returnEur"]), "debtEur": _r(f["debtEur"]),
                    "effectiveReturnPct": _r(f["effectiveReturnPct"], 4),
                    "taxEur": _r(f["taxEur"])},
        "tegenbewijs": teg, "applies": applies, "taxEur": _r(tax_eur),
        "dividendTaxCreditEur": _r(credit), "dividendTaxCreditEstimate": credit_est,
        "netEur": _r(tax_eur - credit),
        "estimate": ytd or p["status"] != "final" or credit_est or not complete,
        "complete": complete, "notes": notes,
    }


def _projection(cfg: dict, today: datetime.date, total: float | None,
                partner: bool, warn: _Warnings) -> dict:
    year = today.year + 1
    out = {"year": year, "basedOn": "today's value of the dashboard's investments",
           "valueEur": _r(total), "estimate": True, "scenarios": []}
    if total is None:
        warn.add("projection_value_missing", "No current portfolio value for projection")
        return out
    carried = None
    for key in sorted(_taxnl(cfg).get("otherAssets") or {}, reverse=True):
        if key.isdigit() and int(key) <= today.year and _assets(cfg, key) is not None:
            carried = key
            break
    assets = _assets(cfg, carried) if carried else None
    if assets is None:
        warn.add("other_assets_missing",
                 "No taxNL.otherAssets: result covers the dashboard's investments only, "
                 "for year", "projection")
    else:
        out["otherAssetsFromYear"] = int(carried)
    for label in ("law", "proposal"):
        p = params_for(year, label)
        if p is None:
            continue
        f = forfait((assets or {}).get("bankEur", 0.0),
                    total + (assets or {}).get("otherEur", 0.0),
                    (assets or {}).get("debtsEur", 0.0), p, partner)
        out["scenarios"].append({
            "label": label, "year": year, "status": p["status"], "params": _public_params(p),
            "baseEur": _r(f["base"]), "taxFreeEur": _r(f["taxFree"]),
            "effectiveReturnPct": _r(f["effectiveReturnPct"], 4),
            "taxEur": _r(f["taxEur"]), "estimate": True})
    if not out["scenarios"]:
        warn.add("projection_params_missing", f"No box 3 parameters for {year}")
    return out


def _checklist(row: dict | None, year: int, positions: dict[str, dict]) -> list[dict]:
    """Figures for the aangifte (spec section 5)."""
    def item(label, value, source, group="forfait"):
        return {"label": label, "year": year, "group": group,
                "valueEur": value, "source": source}
    assets = (row or {}).get("otherAssets")
    out = [item(f"Banktegoeden on 1 Jan {year}, per account",
                assets["bankEur"] if assets else None,
                "taxNL.otherAssets" if assets else "you provide")]
    for pid, x in sorted(positions.items()):
        out.append(item(f"{pid} on 1 Jan {year} (overige bezittingen)",
                        _r(x["vStart"]), "dashboard (lots x 31 Dec close)"))
    if assets:
        out.append(item(f"Other assets on 1 Jan {year}", assets["otherEur"], "taxNL.otherAssets"))
    out.append(item(f"Debts on 1 Jan {year}", assets["debtsEur"] if assets else None,
                    "taxNL.otherAssets" if assets else "you provide"))
    out.append(item("Dutch dividendbelasting withheld",
                    (row or {}).get("dividendTaxCreditEur"),
                    "taxNL.dividendTaxWithheldEur" if row and not row.get(
                        "dividendTaxCreditEstimate") else "estimate: 15% of gross; check broker statement"))
    out.append(item("Dividends received (gross)",
                    ((row or {}).get("tegenbewijs") or {}).get("dividends"),
                    "dashboard (ex-date x shares held)"))
    teg = (row or {}).get("tegenbewijs") or {}
    out += [item(f"Investments value on 31 Dec {year}", teg.get("vEnd"),
                 "dashboard (31 Dec close)", "tegenbewijs"),
            item("Deposits (purchases) with dates", teg.get("deposits"),
                 "dashboard lots", "tegenbewijs"),
            item("Withdrawals (sales) with dates", teg.get("withdrawals"),
                 "dashboard lots", "tegenbewijs"),
            item("Interest received", None, "you provide", "tegenbewijs"),
            item("Interest paid on box 3 debt", None, "you provide", "tegenbewijs")]
    return out


async def payload() -> dict:
    """``GET /api/tax`` for residency NL. Never raises on missing data."""
    cfg = config_store.read()
    today = _today()
    warn = _Warnings()
    partner = bool(_taxnl(cfg).get("fiscalPartner"))
    out = {"residency": RESIDENCY_NL, "fiscalPartner": partner,
           "params": {str(y): _public_params(p) for y, p in sorted(PARAMS.items())},
           "years": [], "projection": None, "checklist": [], "warnings": [],
           "disclaimer": DISCLAIMER, "asOf": _now_iso()}

    pf = tax._portfolio(cfg)
    lots_by_id = tax._lots_by_id(cfg)
    ids = sorted(str(k).strip().upper() for k, pos in pf.items()
                 if isinstance(pos, dict) and ((tax._num(pos.get("shares")) or 0) > 0
                                               or str(k).strip().upper() in lots_by_id))
    first = min([lots[0]["date"].year for lots in lots_by_id.values()] + [today.year])
    years = [y for y in sorted(PARAMS) if max(first, FIRST_YEAR) <= y <= today.year]
    if not ids or not years:
        out["warnings"] = warn.out()
        return out

    try:
        snap = await portfolio.snapshot()
    except Exception as e:                      # noqa: BLE001 - labelled, not fatal
        log.warning("tax_nl: portfolio snapshot failed: %s", e)
        snap = {}
    rows = {str(r.get("id")).upper(): r for r in (snap.get("positions") or [])
            if isinstance(r, dict)}

    days = (today - datetime.date(years[0] - 1, 12, 20)).days
    period = performance.period_for(days)

    async def load(pid):
        pos = pf.get(pid) or {}
        d = {"lots": lots_by_id.get(pid, []), "shares": tax._num(pos.get("shares")) or 0.0,
             "dates": [], "closes": [], "divs": [], "div_ok": True}
        try:
            closes = await _closes(pid, period)
        except Exception as e:                  # noqa: BLE001
            log.warning("tax_nl: closes %s failed: %s", pid, e)
            closes = []
        for day, close in sorted(closes or []):
            d["dates"].append(str(day)[:10])
            d["closes"].append(float(close))
        try:
            d["divs"] = list(await _dividends(pid))
        except Exception as e:                  # noqa: BLE001
            log.warning("tax_nl: dividends %s failed: %s", pid, e)
            d["div_ok"] = False
        return pid, d

    data = dict([await load(pid) for pid in ids])

    year_rows, parts_by_year = [], {}
    for year in years:
        parts, complete = {}, True
        for pid in ids:
            snap_value = (rows.get(pid) or {}).get("valueEur")
            part = _position_year(pid, data[pid], year, today, snap_value, warn)
            if part is None:
                complete = False
            else:
                parts[pid] = part
        parts_by_year[year] = parts
        year_rows.append(_year_row(year, parts, cfg, today, partner, warn, complete))
    out["years"] = year_rows

    priced = [r.get("valueEur") for r in rows.values() if r.get("valueEur") is not None]
    out["projection"] = _projection(cfg, today, sum(priced) if priced else None,
                                    partner, warn)
    aangifte = next((r for r in reversed(year_rows) if r["year"] < today.year),
                    year_rows[-1])
    out["checklist"] = _checklist(aangifte, aangifte["year"],
                                  parts_by_year[aangifte["year"]])
    out["warnings"] = warn.out()
    return out
