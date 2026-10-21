"""Broker abstraction (paper trading, Phase 2 — demo orders).

A thin, provider-agnostic interface consumed by the paper-trading backend.
The only implementation is Trading 212's DEMO account
(``broker_t212.T212Broker``); ``get_broker`` returns ``None`` for anything
but ``"demo"`` — the repo is demo-only (contract §0).

Methods never raise: failures are returned as ``err(...)`` envelopes, the
same shape ``lane_client`` uses for its own errors
(``{"error": kind, "status": ..., "detail": ...}``), so a caller always
checks with ``is_error(x)`` instead of wrapping every call in try/except.
``detail`` must never contain the API key/secret or an ``Authorization``
header value — see ``skills/secrets-handling``.
"""
from dataclasses import dataclass
from typing import Protocol

ERRORS = ("auth", "forbidden", "rate_limited", "timeout", "http",
          "bad_response", "disabled", "bad_request", "rejected")


def err(kind: str, status: int | None = None, detail: str = "") -> dict:
    """Error envelope. ``kind`` is expected to be one of ``ERRORS`` (not
    enforced here — a mismatch is a code bug to catch in review/tests, not a
    condition to guard against at runtime)."""
    return {"error": kind, "status": status, "detail": detail}


def is_error(x: object) -> bool:
    return isinstance(x, dict) and "error" in x


@dataclass(frozen=True)
class Account:
    currency: str
    total: float
    free_cash: float
    invested: float
    fetched_at: float


@dataclass(frozen=True)
class Position:
    ticker: str
    quantity: float
    average_price: float
    current_price: float | None
    ppl: float | None
    currency: str | None


@dataclass(frozen=True)
class Instrument:
    ticker: str
    isin: str | None
    name: str | None
    currency: str | None
    type: str | None
    short_name: str | None
    schedule_id: int | None


@dataclass(frozen=True)
class Order:
    id: str                    # broker order id as str
    ticker: str
    side: str                  # "buy" | "sell"
    quantity: float            # absolute requested quantity (> 0)
    filled_quantity: float     # absolute, 0 when none
    limit_price: float | None
    avg_fill_price: float | None
    filled_value: float | None  # absolute, account currency when known
    status: str                # "open" | "partially_filled" | "filled" | "cancelled" | "rejected" | "unknown"
    created_at: float | None   # epoch seconds
    raw_status: str | None     # broker status string as received


class Broker(Protocol):
    name: str          # "t212"
    env: str            # always "demo" (contract §0)

    async def account(self, fresh: bool = False) -> Account | dict: ...
    async def positions(self, fresh: bool = False) -> list[Position] | dict: ...
    async def instruments(self) -> list[Instrument] | dict: ...
    async def instrument(self, ticker: str) -> Instrument | None | dict: ...
    async def open_orders(self, fresh: bool = False) -> list[Order] | dict: ...
    async def order_history(self, limit: int = 50, fresh: bool = False) -> list[Order] | dict: ...
    async def place_limit_order(self, ticker: str, side: str, quantity: float,
                                limit_price: float) -> Order | dict: ...
    async def cancel_order(self, order_id: str) -> bool | dict: ...
    def quantity_decimals(self, ticker: str) -> int: ...
    def status(self) -> dict: ...  # {"name","env","configured","lastOkAt","lastError","calls","errors"} — never secrets
    async def close(self) -> None: ...


# Cached singleton per env — constructing a broker reads env.secrets-backed
# environment variables once and owns one httpx.AsyncClient.
_singletons: dict[str, "Broker | None"] = {}


def get_broker(env: str = "demo") -> "Broker | None":
    """demo: the T212 demo broker when T212_DEMO_KEY and T212_DEMO_SECRET are
    set, else None. ANY other env (including "live") is None — the repo is
    demo-only (contract §0). Cached singleton for demo."""
    if env != "demo":
        return None
    if env not in _singletons:
        from app.api import broker_t212
        _singletons[env] = broker_t212.from_env(env)
    return _singletons[env]
