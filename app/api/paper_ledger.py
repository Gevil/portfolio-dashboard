"""Paper-trading ledger: season, daily snapshot, order-intent and halt stores,
plus the bucket accounting that turns the order ledger into each bucket's own
cash/positions (plan §3, contract §3.1-3.2, phase 5 contract §2).

Buckets (``paper_buckets``): ``daily`` (the original) and ``intraday`` share
the ONE demo account. Every function that touches a per-bucket store takes
``bucket`` (default ``daily``). ``jsonstore`` files, all covered by the daily
backup (``data/``); the intraday variant of a name is ``<stem>.intraday.json``:

* ``data/paper_season.json`` - one row (or absent): ``{"book","bucket",
  "strategyVersion","inceptionDate","startedAt","baselineWeights"}``. Created
  by ``POST /api/paper/season`` (``paper_eval.create_season``).
* ``data/paper_snapshots.json`` - a list of daily rows ``{"date","equityEur",
  "cashEur","positions":[{id,ticker,quantity,priceEur,valueEur}],
  "accountTotalEur","accountCashEur","takenAt"}``, one per LOCAL date (a
  second snapshot the same day replaces the first), capped at
  ``MAX_SNAPSHOT_ROWS``.
* ``data/paper_orders.json`` - ONE shared list of order-intent rows (see
  ``STATES``), each with a ``bucket`` (``daily`` when the key is missing: every
  pre-phase-5 row). Ledger cash is computed from these rows' fills and
  reservations, so a row with a fill (``filledQuantity`` > 0) and a
  non-terminal row are NEVER dropped. Above ``MAX_ORDER_ROWS`` only terminal
  rows without fills (shadow, rejected, not_sent, cancelled-without-fill) are
  pruned, oldest first: they have no effect on cash, holdings or reservations,
  so pruning never changes the accounting (the file can exceed the cap when
  fill rows alone do).
* ``data/paper_halt.json`` - ``{"halted","reason","at","by","resumedAt",
  "resumedBy"}`` plus ``notifiedAt`` (the halt push was consumed) and
  ``rearm`` (kill-switch reference taken after a resume), see ``paper_exec``.

Bucket accounting (phase 5 contract §2): a bucket's holdings and ``cashEur``
come ONLY from its own ledger fills: net filled quantity per id, and
``budgetEur`` - Sigma filled buy value + Sigma filled sell value - cash
reserved for open/unknown buys (remaining quantity x limit price). Broker
positions are no longer split between buckets; instead ONE broker-wide
reconciliation compares the broker quantity per ticker with the Sigma over all
buckets of the ledger net quantity: a difference of more than the instrument
step is the warning ``ledger_mismatch``; a position no ledger fill explains is
``untracked_position``. Both are surfaced (in every bucket), never traded away.
Equity counts the reserved cash (it is still the bucket's money while an order
is open).

Broker objects are consumed duck-typed (dataclass OR plain dict with the
same field names) so tests never need ``broker_t212``/``httpx``.
"""
import logging
import os
import pathlib

from app.api import jsonstore, paper_buckets

log = logging.getLogger("paper_ledger")

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
SEASON_FILE = DATA_DIR / "paper_season.json"
SNAPSHOTS_FILE = DATA_DIR / "paper_snapshots.json"
ORDERS_FILE = DATA_DIR / "paper_orders.json"
HALT_FILE = DATA_DIR / "paper_halt.json"
BRIEFS_FILE = DATA_DIR / "paper_briefs.json"
MAX_SNAPSHOT_ROWS = 800
MAX_BRIEF_ROWS = 400
MAX_ORDER_ROWS = 2000

STATES = ("shadow", "pending", "submitted", "unknown", "not_sent", "rejected",
          "partially_filled", "filled", "cancelled")
TERMINAL_STATES = frozenset({"shadow", "rejected", "filled", "cancelled", "not_sent"})
NON_TERMINAL_STATES = frozenset({"pending", "submitted", "unknown", "partially_filled"})
PRUNABLE_STATES = frozenset({"shadow", "rejected", "not_sent", "cancelled"})   # when without fills
DEFAULT_DECIMALS = 2          # contract: quantity_decimals falls back to 2

HALT_DEFAULT = {"halted": False, "reason": None, "at": None, "by": None,
                "resumedAt": None, "resumedBy": None}


# --------------------------------------------------------------- helpers

def _get(obj, name):
    """Attribute or dict-key access: broker objects duck-type as either a
    frozen dataclass (``Position``, ``Account``) or a plain dict in tests."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _ticker_map(universe: dict | None) -> dict[str, str]:
    """{t212 ticker: watchlist id} from the ``paperTrading.universe`` block."""
    out = {}
    for pid, spec in (universe or {}).items():
        if isinstance(spec, dict):
            t212 = spec.get("t212")
            if isinstance(t212, str) and t212.strip():
                out[t212.strip()] = pid
    return out


def step_for(decimals: int | None) -> float:
    """Smallest tradable quantity step for ``decimals`` places."""
    return 10.0 ** -(DEFAULT_DECIMALS if decimals is None else int(decimals))


# ----------------------------------------------------------------- season

DAILY = paper_buckets.DAILY


def _file(daily_file: pathlib.Path, bucket: str) -> pathlib.Path:
    """The bucket's variant of a per-bucket store (``daily_file`` itself for
    daily; the module globals stay the test seams)."""
    return paper_buckets.suffixed(daily_file, bucket)


def read_season(bucket: str = DAILY) -> dict | None:
    row = jsonstore.load(_file(SEASON_FILE, bucket), None)
    return row if isinstance(row, dict) else None


def write_season(row: dict, bucket: str = DAILY) -> bool:
    return jsonstore.save(_file(SEASON_FILE, bucket), row, indent=2)


# -------------------------------------------------------------- snapshots

def read_snapshots(bucket: str = DAILY) -> list[dict]:
    rows = jsonstore.load(_file(SNAPSHOTS_FILE, bucket), [])
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def latest_snapshot(bucket: str = DAILY) -> dict | None:
    rows = [r for r in read_snapshots(bucket) if r.get("date")]
    return max(rows, key=lambda r: r["date"]) if rows else None


def snapshot_on(date: str, bucket: str = DAILY) -> dict | None:
    for r in read_snapshots(bucket):
        if r.get("date") == date:
            return r
    return None


def save_snapshot_row(row: dict, bucket: str = DAILY) -> bool:
    """Insert/replace the row for ``row["date"]`` (one row per local date);
    keeps at most ``MAX_SNAPSHOT_ROWS`` rows, oldest dropped first."""
    date = row.get("date")
    rows = [r for r in read_snapshots(bucket) if r.get("date") != date]
    rows.append(row)
    rows.sort(key=lambda r: r.get("date") or "")
    if len(rows) > MAX_SNAPSHOT_ROWS:
        rows = rows[-MAX_SNAPSHOT_ROWS:]
    return jsonstore.save(_file(SNAPSHOTS_FILE, bucket), rows, indent=2)


# ------------------------------------------------------------------ briefings

def read_briefs(bucket: str = DAILY) -> list[dict]:
    """The bucket's daily briefings (``paper_briefs[.intraday].json``), oldest
    first: one row per session date, built after the close from stored data."""
    rows = jsonstore.load(_file(BRIEFS_FILE, bucket), [])
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save_brief(row: dict, bucket: str = DAILY) -> bool:
    """Insert/replace the row for ``row["date"]``; keeps at most
    ``MAX_BRIEF_ROWS`` rows, oldest dropped first."""
    rows = [r for r in read_briefs(bucket) if r.get("date") != row.get("date")]
    rows.append(row)
    rows.sort(key=lambda r: r.get("date") or "")
    return jsonstore.save(_file(BRIEFS_FILE, bucket), rows[-MAX_BRIEF_ROWS:], indent=1)


# ------------------------------------------------------------ order intents

def read_orders() -> list[dict]:
    """All intent rows, oldest first (insertion order)."""
    rows = jsonstore.load(ORDERS_FILE, [])
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def is_prunable(row: dict) -> bool:
    """A terminal row with no fill: dropping it cannot change cash, holdings or
    reservations."""
    return (row.get("state") in PRUNABLE_STATES
            and not (_num(row.get("filledQuantity")) or 0.0) > 0)


def save_orders(rows: list[dict]) -> bool:
    """Persist ``rows``. Above ``MAX_ORDER_ROWS`` the OLDEST prunable rows
    (``is_prunable``) are dropped until the cap is met or none are left."""
    excess = len(rows) - MAX_ORDER_ROWS
    if excess > 0:
        kept = []
        for r in rows:
            if excess > 0 and is_prunable(r):
                excess -= 1
                continue
            kept.append(r)
        rows = kept
    return jsonstore.save(ORDERS_FILE, rows, indent=1)


def non_terminal(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("state") in NON_TERMINAL_STATES]


def row_bucket(row: dict) -> str:
    """The bucket a row belongs to; ``daily`` when the key is missing (every
    row written before phase 5)."""
    return row.get("bucket") or DAILY


def bucket_rows(rows: list[dict], bucket: str = DAILY) -> list[dict]:
    return [r for r in rows if row_bucket(r) == bucket]


def next_intent_id(rows: list[dict], date: str, strategy_version: str,
                   source: str, pid: str, side: str, bucket: str = DAILY) -> str:
    """``<date>-<bucket>-<strategyVersion>-<source>-<id>-<side>-<n>`` with
    ``n`` the per-day sequence of that prefix (1-based, over the rows passed
    in). The bucket is part of the prefix, so two buckets never collide in the
    shared order file."""
    prefix = f"{date}-{bucket}-{strategy_version}-{source}-{pid}-{side}-"
    seq = 0
    for r in rows:
        iid = r.get("intentId")
        if isinstance(iid, str) and iid.startswith(prefix) and iid[len(prefix):].isdigit():
            seq = max(seq, int(iid[len(prefix):]))
    return f"{prefix}{seq + 1}"


def remaining_quantity(row: dict) -> float:
    q = _num(row.get("quantity")) or 0.0
    f = _num(row.get("filledQuantity")) or 0.0
    return max(0.0, q - f)


def fill_value(row: dict) -> float:
    """EUR value of the row's fills: the broker's filled value when known,
    else filled quantity x average fill price, else x the reference price."""
    fq = _num(row.get("filledQuantity")) or 0.0
    if fq <= 0:
        return 0.0
    v = _num(row.get("filledValueEur"))
    if v is not None:
        return v
    px = _num(row.get("avgFillPrice")) or _num(row.get("refPrice")) or 0.0
    return fq * px


def day_usage(rows: list[dict], date: str) -> tuple[int, float]:
    """(orders, turnover EUR) the rows created on LOCAL ``date`` (the intentId
    date prefix) already use against ``maxOrdersPerDay`` /
    ``maxTurnoverPctPerDay``. Shadow, rejected and not_sent rows never reached
    the market and do not count; turnover is the fill value, or the committed
    quantity x ref price while the order is still open."""
    count, turnover = 0, 0.0
    for r in rows:
        iid = r.get("intentId")
        if not (isinstance(iid, str) and iid.startswith(date + "-")):
            continue
        state = r.get("state")
        if state in ("shadow", "rejected", "not_sent"):
            continue
        count += 1
        if (_num(r.get("filledQuantity")) or 0.0) > 0:
            turnover += fill_value(r)
        elif state in NON_TERMINAL_STATES:
            turnover += (_num(r.get("quantity")) or 0.0) * (_num(r.get("refPrice")) or 0.0)
    return count, turnover


def open_exposure(rows: list[dict]) -> dict[str, float]:
    """{id: Sigma remaining open buy quantity - Sigma remaining open sell
    quantity} over non-terminal rows: what is already in flight, so a second
    rebalance does not order the same delta twice."""
    out: dict[str, float] = {}
    for r in non_terminal(rows):
        rem = remaining_quantity(r)
        if rem <= 0:
            continue
        sign = 1.0 if r.get("side") == "buy" else -1.0
        out[r["id"]] = out.get(r["id"], 0.0) + sign * rem
    return out


def open_sell_proceeds(rows: list[dict]) -> float:
    """Expected EUR proceeds of the open sells (remaining quantity x limit
    price). A rebalance counts them as cash while it counts the sold shares
    as gone, so the planner's equity does not shrink while a sell is in flight."""
    return sum(remaining_quantity(r) * (_num(r.get("limitPrice")) or 0.0)
               for r in non_terminal(rows) if r.get("side") == "sell")


def ledger_cash(rows: list[dict], budget_eur: float) -> dict:
    """Contract §3.2: -> {"cashEur", "reservedEur", "filledBuyEur",
    "filledSellEur"}. ``cashEur`` is the FREE cash (reservation deducted) and
    never negative; ``reservedEur`` is remaining quantity x limit price of the
    non-terminal buys (a ``pending``/``unknown`` buy may be live at the broker,
    so it is reserved too)."""
    buys = sells = reserved = 0.0
    for r in rows:
        if r.get("state") == "shadow":
            continue
        value = fill_value(r)
        if r.get("side") == "buy":
            buys += value
            if r.get("state") in NON_TERMINAL_STATES:
                reserved += remaining_quantity(r) * (_num(r.get("limitPrice")) or 0.0)
        elif r.get("side") == "sell":
            sells += value
    cash = (budget_eur or 0.0) - buys + sells - reserved
    return {"cashEur": max(0.0, cash), "reservedEur": reserved,
            "filledBuyEur": buys, "filledSellEur": sells}


def ledger_net_quantity(rows: list[dict]) -> tuple[dict[str, float], set[str]]:
    """({id: net filled quantity}, {ids with at least one fill}) from the
    non-shadow rows."""
    net: dict[str, float] = {}
    filled_ids: set[str] = set()
    for r in rows:
        if r.get("state") == "shadow":
            continue
        fq = _num(r.get("filledQuantity")) or 0.0
        if fq <= 0:
            continue
        filled_ids.add(r["id"])
        net[r["id"]] = net.get(r["id"], 0.0) + (fq if r.get("side") == "buy" else -fq)
    return net, filled_ids


def ledger_warnings(rows: list[dict], broker_qty: dict[str, float],
                    decimals_by_id: dict[str, int] | None) -> list[dict]:
    """``untracked_position`` (broker holds an id no ledger fill explains) and
    ``ledger_mismatch`` (broker quantity differs from the ledger's net filled
    quantity by more than the instrument step)."""
    net, filled_ids = ledger_net_quantity(rows)
    untracked, mismatched = [], []
    for pid in sorted(set(broker_qty) | filled_ids):
        qty = broker_qty.get(pid, 0.0)
        if pid not in filled_ids:
            if qty > 1e-9:
                untracked.append(pid)
            continue
        step = step_for((decimals_by_id or {}).get(pid))
        if abs(qty - net.get(pid, 0.0)) > step + 1e-9:
            mismatched.append(pid)
    out = []
    if mismatched:
        out.append({"code": "ledger_mismatch",
                    "message": "Broker quantity differs from the net filled quantity of "
                               "the order ledger (all buckets) for: " + ", ".join(mismatched)
                               + " (surfaced only; nothing is traded to fix it)",
                    "ids": mismatched})
    if untracked:
        out.append({"code": "untracked_position",
                    "message": "Position(s) no ledger fill explains (opened outside "
                               "the dashboard?): " + ", ".join(untracked),
                    "ids": untracked})
    return out


# ------------------------------------------------------------------- halt

def read_halt(bucket: str = DAILY) -> dict:
    row = jsonstore.load(_file(HALT_FILE, bucket), None)
    return {**HALT_DEFAULT, **row} if isinstance(row, dict) else dict(HALT_DEFAULT)


def write_halt(row: dict, bucket: str = DAILY) -> bool:
    return jsonstore.save(_file(HALT_FILE, bucket), row, indent=2)


# ---------------------------------------------------------- bucket accounting

def ledger_holdings(rows: list[dict]) -> dict[str, dict]:
    """{id: {"ticker", "quantity", "costEur"}} for the ids a set of rows (ONE
    bucket's) holds net > 0: net filled quantity with an average-cost basis
    (a sell removes cost pro rata). Shadow rows never count."""
    held: dict[str, dict] = {}
    for r in rows:
        if r.get("state") == "shadow":
            continue
        fq = _num(r.get("filledQuantity")) or 0.0
        if fq <= 0:
            continue
        h = held.setdefault(r["id"], {"ticker": r.get("ticker"), "quantity": 0.0, "costEur": 0.0})
        if r.get("side") == "buy":
            h["quantity"] += fq
            h["costEur"] += fill_value(r)
        else:
            sold = min(fq, h["quantity"])
            if h["quantity"] > 0:
                h["costEur"] -= h["costEur"] * sold / h["quantity"]
            h["quantity"] -= fq
    return {pid: {**h, "quantity": h["quantity"], "costEur": max(0.0, h["costEur"])}
            for pid, h in held.items() if h["quantity"] > 1e-9}


def bucket_holds_positions(rows: list[dict], bucket: str = DAILY) -> bool:
    """True when the bucket's own ledger fills leave it holding anything (the
    season-reset and seed rule: they need an empty bucket)."""
    return bool(ledger_holdings(bucket_rows(rows, bucket)))


def bucket_from_positions(*, positions, account, universe: dict | None,
                          budget_eur: float, prices_by_id: dict | None,
                          orders: list[dict] | None,
                          decimals_by_id: dict | None = None,
                          bucket: str = DAILY) -> dict:
    """-> {"positions": [...], "warnings": [...], "equityEur", "cashEur",
    "reservedEur", "bucketCostEur", "accountTotalEur", "accountCashEur"}.

    ``orders``: ALL ``paper_orders`` rows (``[]`` when none). The bucket's
    holdings and cash come from ITS rows only (``bucket``, phase 5 §2); the
    reconciliation warnings compare the broker quantity with the Sigma over
    ALL buckets' rows. ``positions``: broker ``Position``-shaped objects/dicts,
    used for that reconciliation and as a price fallback. ``account``: a
    broker ``Account``-shaped object/dict or None. ``universe``: the
    ``paperTrading.universe`` block. ``prices_by_id``: {watchlist id:
    ``prices.listing_quote``-shaped dict or None}, already fetched by the
    caller (this function does no I/O). ``decimals_by_id``: quantity decimals
    per id for the mismatch step (default 2).

    Each returned position row: id, ticker, quantity, averagePrice,
    investedEur (the ledger's average-cost basis), priceEur, prevCloseEur,
    valueEur (None when no usable price), priceAsOf, priceSource, stale.
    ``equityEur`` = Sigma priced valueEur + free cash + reserved cash.
    """
    tmap = _ticker_map(universe)
    prices_by_id = prices_by_id or {}
    unmapped: list[str] = []
    broker_qty: dict[str, float] = {}
    broker_price: dict[str, float] = {}
    for p in positions or []:
        ticker = _get(p, "ticker")
        pid = tmap.get(ticker)
        if pid is None:
            if ticker:
                unmapped.append(ticker)
            continue
        broker_qty[pid] = broker_qty.get(pid, 0.0) + (_num(_get(p, "quantity")) or 0.0)
        cur = _num(_get(p, "current_price"))
        if cur is not None and cur > 0:
            broker_price[pid] = cur
    order_rows = orders or []
    held = ledger_holdings(bucket_rows(order_rows, bucket))
    id_ticker = {pid: t for t, pid in tmap.items()}
    rows: list[dict] = []
    cost = 0.0
    for pid, h in held.items():
        qty, invested = h["quantity"], h["costEur"]
        cost += invested
        q = prices_by_id.get(pid)
        price = _num((q or {}).get("price"))
        price_source = (q or {}).get("source")
        price_as_of = (q or {}).get("asOf")
        stale = bool((q or {}).get("stale")) if q else None
        prev = _num((q or {}).get("prevClose"))
        if (price is None or price <= 0) and pid in broker_price:
            price, price_source, stale, prev = broker_price[pid], "t212", False, None
        value = qty * price if (price is not None and price > 0) else None
        rows.append({
            "id": pid, "ticker": id_ticker.get(pid) or h["ticker"], "quantity": qty,
            "averagePrice": invested / qty, "investedEur": invested,
            "priceEur": price, "prevCloseEur": prev,
            "valueEur": value, "priceAsOf": price_as_of,
            "priceSource": price_source, "stale": stale,
        })
    warnings = []
    if unmapped:
        ids = sorted(set(unmapped))
        warnings.append({
            "code": "unmapped_broker_position",
            "message": "Demo account position(s) not in the paperTrading "
                       "universe, excluded from the buckets: " + ", ".join(ids),
            "ids": ids,
        })
    warnings.extend(ledger_warnings(order_rows, broker_qty, decimals_by_id))
    cash = ledger_cash(bucket_rows(order_rows, bucket), budget_eur)
    priced_value = sum(r["valueEur"] for r in rows if r["valueEur"] is not None)
    equity = priced_value + cash["cashEur"] + cash["reservedEur"]
    return {
        "positions": rows, "warnings": warnings,
        "equityEur": equity, "cashEur": cash["cashEur"],
        "reservedEur": cash["reservedEur"], "bucketCostEur": cost,
        "accountTotalEur": _num(_get(account, "total")),
        "accountCashEur": _num(_get(account, "free_cash")),
    }
