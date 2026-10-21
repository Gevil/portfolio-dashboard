"""Trading 212 DEMO broker client (paper trading, Phase 2 — orders).

Talks to the Trading 212 Public API (beta) over ``httpx.AsyncClient`` with
HTTP Basic auth (API key as username, API secret as password — ``httpx``
builds the header, this module never touches it). Only the demo environment
is used in Phase 1; ``DEMO_URL`` is the only base URL referenced by code
(``get_broker("live")`` returns ``None`` before any of this runs).

Field names below come from the published OpenAPI bundle
(https://docs.trading212.com/_bundle/api.yaml): ``AccountSummary`` (cash,
currency, id, investments, totalValue) and its nested ``Cash``
(availableToTrade, ...) and ``Investments`` (currentValue, ...);
``Position`` (averagePricePaid, currentPrice, instrument, quantity,
walletImpact.unrealizedProfitLoss); ``TradableInstrument`` (currencyCode,
isin, name, shortName, ticker, type, workingScheduleId) for
``/equity/metadata/instruments``; ``Order`` for ``/equity/orders``; and
``PaginatedResponseHistoricalOrder`` (items, nextPagePath) for
``/equity/history/orders``.

Throttling is per endpoint (``INTERVALS_S``): a call made sooner than the
endpoint's minimum interval returns the last good cached result instead of
hitting the network; with no good cache yet it waits out the remainder
instead of firing early. Concurrent GETs of one endpoint are single-flight:
they share ONE in-flight request. A ``fresh=True`` caller only joins a
request that is fresh itself and was not sent before a POST/DELETE that
overlapped or followed it, so it never receives a result read before it
asked. A 429 additionally pins the endpoint's next allowed
call to the ``x-ratelimit-reset`` Unix timestamp the API returns. The
instrument catalogue is kept for 24 h independently of the per-call
throttle — it changes rarely and ``instrument(ticker)`` is a pure lookup
against it.

Every public method is a no-raise/envelope boundary: network, auth, shape
and rate-limit failures all come back as ``broker.err(...)``, never an
exception, and ``detail`` strings are built from the request path and HTTP
status only — never from the body, headers or credentials (see
``skills/secrets-handling``).
"""
import asyncio
import os
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Callable

import httpx

from app.api.broker import Account, Instrument, Order, Position, err, is_error

DEMO_URL = "https://demo.trading212.com/api/v0"

# Minimum seconds between two real calls to one endpoint. "exchanges" has no
# caller yet (Phase 1's Broker protocol has no exchanges() method) but is
# kept here so the full per-endpoint table matches the contract.
INTERVALS_S: dict[str, float] = {
    "summary": 5.0,
    "instruments": 50.0,
    "exchanges": 30.0,
    "positions": 1.0,
    "orders": 5.0,        # GET /equity/orders: 1 req / 5 s
    "history": 3.0,       # GET /equity/history/orders: 20 req / 1 min
    "order_post": 2.0,    # POST /equity/orders/limit: 1 req / 2 s
    "order_delete": 1.2,  # DELETE /equity/orders/{id}: 50 req / 1 min
}
INSTRUMENTS_TTL_S = 24 * 3600.0
REQUEST_TIMEOUT_S = 15.0
# Backoff applied on a 429 whose x-ratelimit-reset header is missing/unparseable.
DEFAULT_429_BACKOFF_S = 5.0
DEFAULT_QUANTITY_DECIMALS = 2
# Error kinds that mean "cannot reach / authenticate": only these flip
# status()["reachable"] to False. rate_limited (throttling) and the write
# rejections (rejected / bad_request) leave it as it was.
CONNECTIVITY_ERRORS = frozenset({"timeout", "http", "auth", "forbidden", "bad_response"})


def _clean(value: str | None) -> str:
    """Strip whitespace and one layer of surrounding quotes — env.secrets
    values are sometimes pasted with quotes still on."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _optional_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _optional_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_reset(value: str | None, fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _abs_optional(v: object) -> float | None:
    f = _optional_float(v)
    return abs(f) if f is not None else None


def _round_down(value: float, decimals: int) -> float:
    q = Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
    return float(q)


def _epoch(v: object) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# T212 order status (OpenAPI Order.status enum) -> normalised status.
#   LOCAL, UNCONFIRMED, CONFIRMED, NEW, CANCELLING, REPLACING -> open
#       (still working; CANCELLING/REPLACING are not final yet)
#   PARTIALLY_FILLED -> partially_filled   FILLED -> filled
#   CANCELLED -> cancelled                 REJECTED -> rejected
#   REPLACED (superseded order; the API has no replace) and anything
#   unrecognised -> unknown
STATUS_MAP: dict[str, str] = {
    "LOCAL": "open", "UNCONFIRMED": "open", "CONFIRMED": "open", "NEW": "open",
    "CANCELLING": "open", "REPLACING": "open",
    "PARTIALLY_FILLED": "partially_filled", "FILLED": "filled",
    "CANCELLED": "cancelled", "REJECTED": "rejected",
}


def _map_order(row: object) -> Order | None:
    """OpenAPI ``Order`` -> broker.Order, or None when id/ticker/side/quantity
    are missing or invalid."""
    if not isinstance(row, dict):
        return None
    oid, ticker = row.get("id"), row.get("ticker")
    if oid is None or isinstance(oid, bool) or not ticker:
        return None
    qty = _optional_float(row.get("quantity"))
    if qty is None or qty == 0:
        return None
    side = str(row.get("side") or "").lower()
    if side not in ("buy", "sell"):
        side = "sell" if qty < 0 else "buy"
    raw_status = row.get("status")
    raw_status = raw_status if isinstance(raw_status, str) else None
    return Order(
        id=str(oid),
        ticker=str(ticker),
        side=side,
        quantity=abs(qty),
        filled_quantity=_abs_optional(row.get("filledQuantity")) or 0.0,
        limit_price=_optional_float(row.get("limitPrice")),
        avg_fill_price=None,
        filled_value=_abs_optional(row.get("filledValue")),
        status=STATUS_MAP.get(raw_status or "", "unknown"),
        created_at=_epoch(row.get("createdAt")),
        raw_status=raw_status,
    )



class _Flight:
    """One in-flight GET shared by every concurrent caller of an endpoint."""
    __slots__ = ("fresh", "sent_epoch", "task")

    def __init__(self, fresh: bool):
        self.fresh = fresh
        self.sent_epoch: int | None = None   # write epoch when the request went out
        self.task: asyncio.Future | None = None


class T212Broker:
    name = "t212"

    def __init__(self, key: str, secret: str, env: str = "demo",
                 base_url: str = DEMO_URL,
                 clock: Callable[[], float] = time.time,
                 client: httpx.AsyncClient | None = None,
                 sleep_fn: Callable[[float], object] = asyncio.sleep):
        # §0 DEMO ONLY: refuse to exist for anything but the demo host/env.
        if env != "demo":
            raise ValueError("T212Broker only supports env='demo'")
        if base_url != DEMO_URL:
            raise ValueError("T212Broker only talks to the Trading 212 demo API")
        self.env = env
        self._key = _clean(key)
        self._secret = _clean(secret)
        self._base_url = DEMO_URL
        self._clock = clock
        self._client = client
        self._own_client = client is None
        # Awaited when a throttled call must wait (see _get/_write). Tests
        # inject a fake that advances the fake clock instead of waiting.
        self._sleep = sleep_fn
        self._write_lock = asyncio.Lock()
        self._last_call: dict[str, float] = {}
        self._rate_limited_until: dict[str, float] = {}
        self._cache: dict[str, object] = {}
        self._flights: dict[str, _Flight] = {}
        self._read_locks: dict[str, asyncio.Lock] = {}
        # Bumped when a POST/DELETE starts and when it ends: a read sent while
        # or before one of those may predate its effect.
        self._write_epoch = 0
        self._reachable = False

        self._instruments: list[Instrument] | None = None
        self._instruments_at: float = 0.0
        self._instruments_by_ticker: dict[str, Instrument] = {}

        self._last_ok_at: float | None = None
        self._last_error: dict | None = None
        self._calls = 0
        self._errors = 0

    # ---------------------------------------------------------- transport
    def _client_ref(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(auth=(self._key, self._secret),
                                             timeout=REQUEST_TIMEOUT_S)
            self._own_client = True
        return self._client

    async def close(self) -> None:
        if self._own_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        """Every request URL is built from DEMO_URL and checked here (§0)."""
        url = f"{DEMO_URL}{path}"
        if not url.startswith(DEMO_URL + "/"):
            raise AssertionError("refusing request outside the Trading 212 demo API")
        return url

    def _status_error(self, key: str, r: httpx.Response, path: str,
                      write: bool = False) -> dict | None:
        """Envelope for a non-2xx response, else None. Details come from the
        path/status only — except a write's 400, whose broker message is
        sanitised and bounded (never credentials)."""
        if r.status_code == 401:
            return err("auth", status=401, detail=f"{path}: invalid API key/secret")
        if r.status_code == 403:
            return err("forbidden", status=403, detail=f"{path}: missing scope")
        if r.status_code == 429:
            reset = _parse_reset(r.headers.get("x-ratelimit-reset"),
                                 self._clock() + DEFAULT_429_BACKOFF_S)
            self._rate_limited_until[key] = reset
            return err("rate_limited", status=429, detail=f"{path}: rate limited")
        if write and r.status_code == 400:
            return err("rejected", status=400,
                       detail=f"{path}: {self._broker_message(r)}")
        if not (200 <= r.status_code < 300):
            return err("http", status=r.status_code,
                       detail=f"{path}: HTTP {r.status_code}")
        return None

    def _broker_message(self, r: httpx.Response) -> str:
        text = ""
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            for k in ("message", "clarification", "detail", "title", "code"):
                v = body.get(k)
                if isinstance(v, str) and v.strip():
                    text = v
                    break
        for secret in (self._key, self._secret):
            if secret:
                text = text.replace(secret, "***")
        text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
        return text[:200] or "order rejected by broker"

    async def _raw_get(self, key: str, path: str,
                       params: dict | None = None) -> object:
        url = self._url(path)
        try:
            r = await self._client_ref().get(url, params=params)
        except httpx.TimeoutException:
            return err("timeout", detail=f"{path}: request timed out")
        except httpx.HTTPError as e:
            return err("http", detail=f"{path}: {type(e).__name__}")
        failure = self._status_error(key, r, path)
        if failure is not None:
            return failure
        try:
            return r.json()
        except ValueError:
            return err("bad_response", detail=f"{path}: response was not valid JSON")

    # ------------------------------------------------- throttle + cache + map
    async def _get(self, key: str, path: str, *, params: dict | None = None,
                   fresh: bool = False,
                   map_fn: Callable[[object], object] | None = None) -> object:
        """Single-flight GET: concurrent callers of one endpoint share ONE
        request. A caller that cannot join the current flight starts the next
        one, which queues on the endpoint's lock and so still respects the
        minimum interval. The flight runs in its own task, so one caller being
        cancelled never aborts it for the others."""
        self._calls += 1
        flight = self._flights.get(key)
        if flight is None or not self._joinable(flight, fresh):
            flight = _Flight(fresh)
            self._flights[key] = flight
            flight.task = asyncio.ensure_future(
                self._fly(flight, key, path, params, map_fn))
        return await asyncio.shield(flight.task)

    def _joinable(self, flight: _Flight, fresh: bool) -> bool:
        if flight.task.done():
            return False
        if not fresh:
            return True      # any result is good enough for a plain read
        if not flight.fresh:
            return False     # may be the cache, or a request older than this call
        return flight.sent_epoch is None or flight.sent_epoch == self._write_epoch

    async def _fly(self, flight: _Flight, key: str, path: str,
                   params: dict | None,
                   map_fn: Callable[[object], object] | None) -> object:
        try:
            lock = self._read_locks.setdefault(key, asyncio.Lock())
            async with lock:
                return await self._fetch(flight, key, path, params, map_fn)
        finally:
            if self._flights.get(key) is flight:
                del self._flights[key]

    async def _fetch(self, flight: _Flight, key: str, path: str,
                     params: dict | None,
                     map_fn: Callable[[object], object] | None) -> object:
        now = self._clock()
        next_allowed = self._rate_limited_until.get(key, 0.0)
        last = self._last_call.get(key)
        if last is not None:
            next_allowed = max(next_allowed, last + INTERVALS_S[key])
        if now < next_allowed:
            # A fresh flight never serves the cache: wait out the interval.
            if key in self._cache and not flight.fresh:
                return self._cache[key]
            await self._sleep(next_allowed - now)
        flight.sent_epoch = self._write_epoch
        raw = await self._raw_get(key, path, params)
        self._last_call[key] = self._clock()
        if is_error(raw):
            return self._record_error(raw)
        mapped = map_fn(raw) if map_fn else raw
        if is_error(mapped):
            return self._record_error(mapped)
        self._mark_ok(self._last_call[key])
        self._cache[key] = mapped
        return mapped

    def _mark_ok(self, at: float) -> None:
        self._last_ok_at = at
        self._reachable = True

    def _record_error(self, e: dict) -> dict:
        self._errors += 1
        self._last_error = e
        if e.get("error") in CONNECTIVITY_ERRORS:
            self._reachable = False
        return e

    async def _write(self, key: str, method: str, path: str, *,
                     json_body: dict | None, allow_empty: bool = False) -> object:
        """POST/DELETE: serialised, throttled by waiting, never cached and
        never retried (a timeout/5xx leaves the outcome UNKNOWN to the
        caller)."""
        url = self._url(path)
        async with self._write_lock:
            self._write_epoch += 1
            try:
                self._calls += 1
                now = self._clock()
                next_allowed = self._rate_limited_until.get(key, 0.0)
                last = self._last_call.get(key)
                if last is not None:
                    next_allowed = max(next_allowed, last + INTERVALS_S[key])
                if now < next_allowed:
                    await self._sleep(next_allowed - now)
                try:
                    r = await self._client_ref().request(method, url, json=json_body)
                except httpx.TimeoutException:
                    self._last_call[key] = self._clock()
                    return self._record_error(err("timeout", detail=f"{path}: request timed out"))
                except httpx.HTTPError as e:
                    self._last_call[key] = self._clock()
                    return self._record_error(err("http", detail=f"{path}: {type(e).__name__}"))
                self._last_call[key] = self._clock()
            finally:
                self._write_epoch += 1
        failure = self._status_error(key, r, path, write=True)
        if failure is not None:
            return self._record_error(failure)
        try:
            return r.json()
        except ValueError:
            if allow_empty:
                return {}
            return self._record_error(
                err("bad_response", detail=f"{path}: response was not valid JSON"))

    # -------------------------------------------------------------- mapping
    def _map_account(self, data: object) -> Account | dict:
        if not isinstance(data, dict):
            return err("bad_response", detail="account summary: not an object")
        cash = data.get("cash")
        investments = data.get("investments")
        if not isinstance(cash, dict) or not isinstance(investments, dict):
            return err("bad_response",
                       detail="account summary: missing cash/investments block")
        try:
            if data.get("id") is None:
                raise KeyError("id")
            return Account(
                currency=str(data["currency"]),
                total=float(data["totalValue"]),
                free_cash=float(cash["availableToTrade"]),
                invested=float(investments["currentValue"]),
                fetched_at=self._clock(),
            )
        except (KeyError, TypeError, ValueError):
            return err("bad_response",
                       detail="account summary: missing or invalid field")

    def _map_position(self, row: object) -> Position | None:
        if not isinstance(row, dict):
            return None
        instrument = row.get("instrument")
        if not isinstance(instrument, dict):
            return None
        ticker = instrument.get("ticker")
        if not ticker:
            return None
        try:
            quantity = float(row["quantity"])
            average_price = float(row["averagePricePaid"])
        except (KeyError, TypeError, ValueError):
            return None
        wallet = row.get("walletImpact")
        ppl = _optional_float(wallet.get("unrealizedProfitLoss")) \
            if isinstance(wallet, dict) else None
        return Position(
            ticker=str(ticker),
            quantity=quantity,
            average_price=average_price,
            current_price=_optional_float(row.get("currentPrice")),
            ppl=ppl,
            currency=instrument.get("currency"),
        )

    def _map_positions_list(self, raw: object) -> list[Position] | dict:
        if not isinstance(raw, list):
            return err("bad_response", detail="positions: expected a list")
        out: list[Position] = []
        for i, row in enumerate(raw):
            p = self._map_position(row)
            if p is None:
                return err("bad_response", detail=f"positions: row {i} could not be mapped")
            out.append(p)
        return out

    def _map_instrument(self, row: object) -> Instrument | None:
        if not isinstance(row, dict):
            return None
        ticker = row.get("ticker")
        if not ticker:
            return None
        return Instrument(
            ticker=str(ticker),
            isin=row.get("isin"),
            name=row.get("name"),
            currency=row.get("currencyCode"),
            type=row.get("type"),
            short_name=row.get("shortName"),
            schedule_id=_optional_int(row.get("workingScheduleId")),
        )

    def _map_instruments_list(self, raw: object) -> list[Instrument] | dict:
        if not isinstance(raw, list):
            return err("bad_response", detail="instruments: expected a list")
        return [i for i in (self._map_instrument(row) for row in raw) if i is not None]

    def _map_open_orders(self, raw: object) -> list[Order] | dict:
        if not isinstance(raw, list):
            return err("bad_response", detail="orders: expected a list")
        out: list[Order] = []
        for i, row in enumerate(raw):
            order = _map_order(row)
            if order is None:
                return err("bad_response", detail=f"orders: row {i} could not be mapped")
            out.append(order)
        return out

    def _map_history(self, raw: object) -> list[Order] | dict:
        """Each history item is {order, fill}. A partially filled order can
        appear once per fill: fills of one order id are aggregated
        (summed quantity/value, quantity-weighted average price)."""
        if not isinstance(raw, dict):
            return err("bad_response", detail="order history: expected an object")
        items = raw.get("items")
        if not isinstance(items, list):
            return err("bad_response", detail="order history: missing items")
        orders: dict[str, Order] = {}
        for i, item in enumerate(items):
            order = _map_order(item.get("order")) if isinstance(item, dict) else None
            if order is None:
                return err("bad_response",
                           detail=f"order history: item {i} could not be mapped")
            fill = item.get("fill")
            fq = fp = fv = None
            if isinstance(fill, dict):
                fq = _abs_optional(fill.get("quantity"))
                fp = _optional_float(fill.get("price"))
                wallet = fill.get("walletImpact")
                if isinstance(wallet, dict):
                    fv = _abs_optional(wallet.get("netValue"))
            if fq is None:
                fq = order.filled_quantity
            if fp is None:
                fp = order.avg_fill_price
            if fv is None:
                fv = order.filled_value
            prev = orders.get(order.id)
            if prev is not None and isinstance(fill, dict):
                total_q = prev.filled_quantity + fq
                if total_q > 0 and fp is not None and prev.avg_fill_price is not None:
                    fp = (prev.avg_fill_price * prev.filled_quantity + fp * fq) / total_q
                fv = fv if prev.filled_value is None else (
                    prev.filled_value + (fv or 0.0))
                fq = total_q
            orders[order.id] = replace(order, filled_quantity=fq,
                                       avg_fill_price=fp, filled_value=fv)
        return list(orders.values())

    # ---------------------------------------------------------------- Broker
    async def account(self, fresh: bool = False) -> Account | dict:
        return await self._get("summary", "/equity/account/summary", fresh=fresh,
                               map_fn=self._map_account)

    async def positions(self, fresh: bool = False) -> list[Position] | dict:
        return await self._get("positions", "/equity/positions", fresh=fresh,
                               map_fn=self._map_positions_list)

    async def instruments(self) -> list[Instrument] | dict:
        now = self._clock()
        if self._instruments is not None and now - self._instruments_at < INSTRUMENTS_TTL_S:
            return self._instruments
        result = await self._get("instruments", "/equity/metadata/instruments",
                                 map_fn=self._map_instruments_list)
        if is_error(result):
            # Serve a stale catalogue rather than fail once we've ever had one.
            return self._instruments if self._instruments is not None else result
        self._instruments = result
        self._instruments_at = now
        self._instruments_by_ticker = {i.ticker: i for i in result}
        return result

    async def instrument(self, ticker: str) -> Instrument | None | dict:
        result = await self.instruments()
        if is_error(result):
            return result
        return self._instruments_by_ticker.get(ticker)

    async def open_orders(self, fresh: bool = False) -> list[Order] | dict:
        return await self._get("orders", "/equity/orders", fresh=fresh,
                               map_fn=self._map_open_orders)

    async def order_history(self, limit: int = 50,
                            fresh: bool = False) -> list[Order] | dict:
        limit = max(1, min(int(limit), 50))
        return await self._get("history", "/equity/history/orders", fresh=fresh,
                               params={"limit": limit}, map_fn=self._map_history)

    def quantity_decimals(self, ticker: str) -> int:
        """The OpenAPI TradableInstrument schema exposes no quantity
        precision/step, so every instrument uses 2 decimals."""
        return DEFAULT_QUANTITY_DECIMALS

    async def place_limit_order(self, ticker: str, side: str, quantity: float,
                                limit_price: float) -> Order | dict:
        """POST /equity/orders/limit (timeValidity DAY). Never cached, never
        retried; a sell is sent as a negative quantity."""
        path = "/equity/orders/limit"
        if not isinstance(ticker, str) or not ticker.strip():
            return err("bad_request", detail=f"{path}: ticker required")
        if side not in ("buy", "sell"):
            return err("bad_request", detail=f"{path}: side must be buy or sell")
        try:
            qty = _round_down(float(quantity), self.quantity_decimals(ticker))
            price = float(limit_price)
        except (TypeError, ValueError, OverflowError):
            return err("bad_request", detail=f"{path}: quantity/limit_price not numeric")
        if not (qty > 0) or not (price > 0) or price == float("inf"):
            return err("bad_request",
                       detail=f"{path}: quantity and limit_price must be > 0 after rounding")
        body = {"ticker": ticker, "quantity": -qty if side == "sell" else qty,
                "limitPrice": price, "timeValidity": "DAY"}
        raw = await self._write("order_post", "POST", path, json_body=body)
        if is_error(raw):
            return raw
        order = _map_order(raw)
        if order is None:
            return self._record_error(
                err("bad_response", detail=f"{path}: response was not a valid order"))
        self._mark_ok(self._clock())
        return order

    async def cancel_order(self, order_id: str) -> bool | dict:
        """DELETE /equity/orders/{id}. True when the broker accepted the cancel
        request (cancellation itself is not guaranteed — reconcile after)."""
        oid = str(order_id).strip()
        if not oid.isascii() or not oid.isdigit():
            return err("bad_request", detail="/equity/orders/{id}: order id must be numeric")
        raw = await self._write("order_delete", "DELETE", f"/equity/orders/{oid}",
                                json_body=None, allow_empty=True)
        if is_error(raw):
            return raw
        self._mark_ok(self._clock())
        return True

    def status(self) -> dict:
        now = self._clock()
        pinned = [t for t in self._rate_limited_until.values() if t > now]
        return {
            "name": self.name,
            "env": self.env,
            "configured": bool(self._key and self._secret),
            # Connectivity/auth only: throttling (rate_limited) never flips it.
            "reachable": self._reachable,
            "rateLimitedUntil": max(pinned) if pinned else None,
            "lastOkAt": self._last_ok_at,
            "lastError": self._last_error,
            "calls": self._calls,
            "errors": self._errors,
        }


def from_env(env: str = "demo") -> "T212Broker | None":
    """A T212Broker when T212_DEMO_KEY/T212_DEMO_SECRET are set in the
    environment, else None. Anything but "demo" is None — DEMO_URL is the
    only base URL this module ever talks to and no live variables are read."""
    if env != "demo":
        return None
    key = _clean(os.environ.get("T212_DEMO_KEY"))
    secret = _clean(os.environ.get("T212_DEMO_SECRET"))
    if not key or not secret:
        return None
    return T212Broker(key, secret, env="demo", base_url=DEMO_URL)
