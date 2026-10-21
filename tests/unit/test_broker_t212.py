"""Trading 212 broker client: field mapping, error envelopes, per-endpoint
throttle/cache, 429 backoff, instrument-cache expiry, get_broker gating, and
that no secret ever reaches an err()/status() payload. No network (httpx
MockTransport only); time is fully injected (no real sleeps).
"""
import asyncio

import httpx
import pytest

from app.api import broker, broker_t212

FAKE_KEY = "demo-key-should-never-leak-7788"
FAKE_SECRET = "demo-secret-should-never-leak-99"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_broker_singletons(monkeypatch):
    monkeypatch.setattr(broker, "_singletons", {})


class FakeClock:
    """Monotonic-looking fake clock + a sleep that just advances it (no
    real waiting, per skills/testing: time-dependent logic takes an
    injectable clock, never a sleep)."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


def make_broker(handler, clock: "FakeClock | None" = None, **kw):
    """A T212Broker wired to an httpx.MockTransport, a FakeClock, and a
    call counter (per path) the test can assert on."""
    clock = clock or FakeClock()
    calls: list[httpx.Request] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    b = broker_t212.T212Broker(FAKE_KEY, FAKE_SECRET, env="demo",
                               base_url=broker_t212.DEMO_URL,
                               clock=clock.time, client=client,
                               sleep_fn=clock.sleep, **kw)
    return b, clock, calls


def json_handler(status: int = 200, body=None, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})
    return handler


ACCOUNT_JSON = {
    "id": 555,
    "currency": "EUR",
    "totalValue": 5123.45,
    "cash": {"availableToTrade": 812.3, "inPies": 0.0, "reservedForOrders": 0.0},
    "investments": {"currentValue": 4311.15, "realizedProfitLoss": 10.0,
                    "totalCost": 4000.0, "unrealizedProfitLoss": 311.15},
}

POSITIONS_JSON = [
    {"averagePricePaid": 100.0, "currentPrice": 112.5, "quantity": 3.0,
     "quantityAvailableForTrading": 3.0, "quantityInPies": 0.0,
     "instrument": {"ticker": "AAPL_US_EQ", "currency": "USD", "isin": "US0378331005",
                     "name": "Apple Inc"},
     "walletImpact": {"currency": "EUR", "currentValue": 337.5, "totalCost": 300.0,
                       "unrealizedProfitLoss": 37.5, "fxRate": 0.92}},
]

INSTRUMENTS_JSON = [
    {"ticker": "AAPL_US_EQ", "isin": "US0378331005", "name": "Apple Inc",
     "shortName": "Apple", "currencyCode": "USD", "type": "STOCK",
     "workingScheduleId": 1, "addedOn": "2020-01-01T00:00:00Z",
     "extendedHours": True, "maxOpenQuantity": 1000.0},
    {"ticker": "MSFT_US_EQ", "isin": "US5949181045", "name": "Microsoft Corp",
     "shortName": "Microsoft", "currencyCode": "USD", "type": "STOCK",
     "workingScheduleId": 1},
]

ORDERS_JSON = [
    {"id": 1, "ticker": "AAPL_US_EQ", "side": "BUY", "status": "NEW", "quantity": 1.0},
]

HISTORY_JSON = {
    "items": [{"order": {"id": 1, "ticker": "AAPL_US_EQ", "side": "BUY",
                         "status": "FILLED", "quantity": 1.0},
               "fill": {"id": 10, "price": 100.0, "quantity": 1.0}}],
    "nextPagePath": None,
}


# --------------------------------------------------------------- field mapping

def test_account_maps_fields():
    b, _, calls = make_broker(json_handler(200, ACCOUNT_JSON))
    acc = run(b.account())
    assert not broker.is_error(acc)
    assert acc.currency == "EUR"
    assert acc.total == 5123.45
    assert acc.free_cash == 812.3
    assert acc.invested == 4311.15
    assert isinstance(acc.fetched_at, float)
    assert len(calls) == 1
    assert calls[0].url.path == "/api/v0/equity/account/summary"


def test_positions_maps_fields():
    b, _, _ = make_broker(json_handler(200, POSITIONS_JSON))
    rows = run(b.positions())
    assert not broker.is_error(rows)
    assert len(rows) == 1
    p = rows[0]
    assert p.ticker == "AAPL_US_EQ"
    assert p.quantity == 3.0
    assert p.average_price == 100.0
    assert p.current_price == 112.5
    assert p.ppl == 37.5
    assert p.currency == "USD"


def test_instruments_maps_fields_and_instrument_looks_up_from_cache():
    b, _, calls = make_broker(json_handler(200, INSTRUMENTS_JSON))
    rows = run(b.instruments())
    assert not broker.is_error(rows)
    assert [i.ticker for i in rows] == ["AAPL_US_EQ", "MSFT_US_EQ"]
    aapl = rows[0]
    assert aapl.isin == "US0378331005"
    assert aapl.name == "Apple Inc"
    assert aapl.currency == "USD"
    assert aapl.type == "STOCK"
    assert aapl.short_name == "Apple"
    assert aapl.schedule_id == 1

    found = run(b.instrument("MSFT_US_EQ"))
    assert found.ticker == "MSFT_US_EQ"
    missing = run(b.instrument("NOPE_US_EQ"))
    assert missing is None
    # instrument() used the cache instruments() already filled -> one HTTP call total.
    assert len(calls) == 1


def test_open_orders_maps_to_order_dataclass():
    b, _, calls = make_broker(json_handler(200, ORDERS_JSON))
    rows = run(b.open_orders())
    assert [(o.id, o.ticker, o.side, o.status) for o in rows] == \
        [("1", "AAPL_US_EQ", "buy", "open")]
    assert calls[0].url.path == "/api/v0/equity/orders"


def test_order_history_maps_items_and_passes_limit():
    b, _, calls = make_broker(json_handler(200, HISTORY_JSON))
    rows = run(b.order_history(limit=5))
    assert len(rows) == 1 and rows[0].id == "1"
    assert rows[0].filled_quantity == 1.0 and rows[0].avg_fill_price == 100.0
    assert calls[0].url.path == "/api/v0/equity/history/orders"
    assert calls[0].url.params["limit"] == "5"


def test_order_history_limit_clamped_to_50():
    b, _, calls = make_broker(json_handler(200, HISTORY_JSON))
    run(b.order_history(limit=999))
    assert calls[0].url.params["limit"] == "50"


# ------------------------------------------------------------------- errors

def test_401_maps_to_auth():
    b, _, _ = make_broker(json_handler(401, {"detail": "bad key"}))
    res = run(b.account())
    assert res == {"error": "auth", "status": 401, "detail": res["detail"]}
    assert FAKE_KEY not in res["detail"] and FAKE_SECRET not in res["detail"]


def test_403_maps_to_forbidden():
    b, _, _ = make_broker(json_handler(403, {"detail": "scope missing"}))
    res = run(b.positions())
    assert res["error"] == "forbidden"
    assert res["status"] == 403


def test_429_maps_to_rate_limited():
    b, _, _ = make_broker(json_handler(429, {}, {"x-ratelimit-reset": "1000100"}))
    res = run(b.account())
    assert res["error"] == "rate_limited"
    assert res["status"] == 429


def test_timeout_maps_to_timeout():
    def handler(request):
        raise httpx.TimeoutException("boom", request=request)
    b, _, _ = make_broker(handler)
    res = run(b.positions())
    assert res["error"] == "timeout"


def test_other_non_2xx_maps_to_http():
    b, _, _ = make_broker(json_handler(500, {"detail": "boom"}))
    res = run(b.open_orders())
    assert res["error"] == "http"
    assert res["status"] == 500


def test_invalid_json_maps_to_bad_response():
    def handler(request):
        return httpx.Response(200, content=b"not json")
    b, _, _ = make_broker(handler)
    res = run(b.account())
    assert res["error"] == "bad_response"


def test_account_missing_field_maps_to_bad_response():
    bad = {"id": 1, "currency": "EUR", "totalValue": 100.0,
           "cash": {}, "investments": {"currentValue": 90.0}}
    b, _, _ = make_broker(json_handler(200, bad))
    res = run(b.account())
    assert res["error"] == "bad_response"


def test_positions_not_a_list_maps_to_bad_response():
    b, _, _ = make_broker(json_handler(200, {"oops": True}))
    res = run(b.positions())
    assert res["error"] == "bad_response"


def test_history_missing_items_maps_to_bad_response():
    b, _, _ = make_broker(json_handler(200, {"nextPagePath": None}))
    res = run(b.order_history())
    assert res["error"] == "bad_response"


# ---------------------------------------------------------------- throttle

def test_throttle_returns_cache_within_interval():
    b, clock, calls = make_broker(json_handler(200, ACCOUNT_JSON))
    first = run(b.account())
    assert not broker.is_error(first)
    clock.now += 1.0            # < summary's 5s interval
    second = run(b.account())
    assert second is first      # served from cache, not re-fetched
    assert len(calls) == 1


def test_throttle_waits_remaining_time_when_no_good_cache_yet():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500)   # errors are never cached
        return httpx.Response(200, json=ACCOUNT_JSON)

    b, clock, calls = make_broker(handler)
    first = run(b.account())
    assert first["error"] == "http"
    before = clock.now
    second = run(b.account())           # inside the 5s window, no cache yet
    assert not broker.is_error(second)
    assert len(calls) == 2
    # the fake sleep advanced the clock by (close to) the remaining interval
    assert clock.now >= before


def test_throttle_proceeds_immediately_after_interval_elapses():
    b, clock, calls = make_broker(json_handler(200, ACCOUNT_JSON))
    run(b.account())
    clock.now += broker_t212.INTERVALS_S["summary"] + 0.01
    run(b.account())
    assert len(calls) == 2


# ------------------------------------------------------------------ 429

def test_429_honours_x_ratelimit_reset_before_next_real_call():
    clock = FakeClock()
    reset_at = clock.now + 42.0
    seq = {"n": 0}

    def handler(request):
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset": str(reset_at)})
        return httpx.Response(200, json=ACCOUNT_JSON)

    b, clock, calls = make_broker(handler, clock=clock)
    first = run(b.account())
    assert first["error"] == "rate_limited"

    # A call issued right after still has to wait for the reset, not just
    # the endpoint's ordinary 5s interval.
    before = clock.now
    second = run(b.account())
    assert not broker.is_error(second)
    assert len(calls) == 2
    assert clock.now - before >= 42.0 - 5.0 - 1e-6   # waited roughly to reset_at


def test_429_without_reset_header_uses_default_backoff():
    b, clock, calls = make_broker(json_handler(429, {}))
    res = run(b.account())
    assert res["error"] == "rate_limited"
    assert broker_t212.INTERVALS_S["summary"] <= \
        (b._rate_limited_until["summary"] - clock.now) <= broker_t212.DEFAULT_429_BACKOFF_S + 1e-6


# ------------------------------------------------------------ instruments TTL

def test_instruments_cache_expires_after_24h():
    b, clock, calls = make_broker(json_handler(200, INSTRUMENTS_JSON))
    run(b.instruments())
    assert len(calls) == 1
    clock.now += 3600.0         # well inside 24h and inside the 50s interval
    run(b.instruments())
    assert len(calls) == 1      # still the same catalogue, no network call
    clock.now += broker_t212.INSTRUMENTS_TTL_S + 1.0
    run(b.instruments())
    assert len(calls) == 2      # 24h elapsed -> refetched


def test_instruments_serves_stale_catalogue_on_fetch_error_after_ttl():
    seq = {"n": 0}

    def handler(request):
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(200, json=INSTRUMENTS_JSON)
        return httpx.Response(500)

    b, clock, calls = make_broker(handler)
    first = run(b.instruments())
    assert not broker.is_error(first)
    clock.now += broker_t212.INSTRUMENTS_TTL_S + 1.0
    second = run(b.instruments())
    assert second == first      # network failed -> stale catalogue served, not an error
    assert len(calls) == 2


# ------------------------------------------------------------------ get_broker

def test_get_broker_demo_none_without_keys(monkeypatch):
    monkeypatch.delenv("T212_DEMO_KEY", raising=False)
    monkeypatch.delenv("T212_DEMO_SECRET", raising=False)
    assert broker.get_broker("demo") is None


def test_get_broker_demo_returns_t212_with_keys(monkeypatch):
    monkeypatch.setenv("T212_DEMO_KEY", f' "{FAKE_KEY}" ')
    monkeypatch.setenv("T212_DEMO_SECRET", f" '{FAKE_SECRET}' ")
    b = broker.get_broker("demo")
    assert b is not None
    assert b.name == "t212"
    assert b.env == "demo"
    # quotes/whitespace were stripped
    assert b._key == FAKE_KEY
    assert b._secret == FAKE_SECRET


def test_get_broker_demo_is_a_cached_singleton(monkeypatch):
    monkeypatch.setenv("T212_DEMO_KEY", FAKE_KEY)
    monkeypatch.setenv("T212_DEMO_SECRET", FAKE_SECRET)
    assert broker.get_broker("demo") is broker.get_broker("demo")


def test_get_broker_live_always_none(monkeypatch):
    monkeypatch.setenv("T212_DEMO_KEY", FAKE_KEY)
    monkeypatch.setenv("T212_DEMO_SECRET", FAKE_SECRET)
    assert broker.get_broker("live") is None


def test_from_env_requires_both_key_and_secret(monkeypatch):
    monkeypatch.setenv("T212_DEMO_KEY", FAKE_KEY)
    monkeypatch.delenv("T212_DEMO_SECRET", raising=False)
    assert broker_t212.from_env("demo") is None


# -------------------------------------------------------- secrets never leak

def test_status_never_contains_secret():
    b, _, _ = make_broker(json_handler(401, {}))
    run(b.account())
    s = b.status()
    blob = repr(s)
    assert FAKE_KEY not in blob and FAKE_SECRET not in blob
    assert s == {"name": "t212", "env": "demo", "configured": True,
                 "reachable": False, "rateLimitedUntil": None,
                 "lastOkAt": None, "lastError": s["lastError"],
                 "calls": 1, "errors": 1}
    assert s["lastError"]["error"] == "auth"


def test_err_detail_never_contains_secret_across_all_error_kinds():
    cases = [
        json_handler(401, {}),
        json_handler(403, {}),
        json_handler(429, {}),
        json_handler(500, {}),
    ]
    for handler in cases:
        b, _, _ = make_broker(handler)
        res = run(b.account())
        assert broker.is_error(res)
        assert FAKE_KEY not in res["detail"]
        assert FAKE_SECRET not in res["detail"]
        assert "authorization" not in res["detail"].lower()


def test_is_error_and_err_shape():
    e = broker.err("timeout", status=None, detail="x")
    assert e == {"error": "timeout", "status": None, "detail": "x"}
    assert broker.is_error(e)
    assert not broker.is_error({"ok": True})
    assert not broker.is_error([1, 2, 3])


# ====================================================== Phase 2: demo-only §0

def test_constructor_rejects_non_demo_base_url_and_env():
    live_url = "https://" + "live" + ".trading212.com/api/v0"
    with pytest.raises(ValueError):
        broker_t212.T212Broker(FAKE_KEY, FAKE_SECRET, base_url=live_url)
    with pytest.raises(ValueError):
        broker_t212.T212Broker(FAKE_KEY, FAKE_SECRET, base_url=broker_t212.DEMO_URL + "/")
    with pytest.raises(ValueError):
        broker_t212.T212Broker(FAKE_KEY, FAKE_SECRET, env="live")
    with pytest.raises(ValueError):
        broker_t212.T212Broker(FAKE_KEY, FAKE_SECRET, env="Demo")


def test_request_url_guard_rejects_paths_leaving_the_demo_host():
    b, _, calls = make_broker(json_handler(200, {}))
    assert b._url("/equity/orders") == broker_t212.DEMO_URL + "/equity/orders"
    with pytest.raises(AssertionError):
        b._url("@elsewhere.example/x")
    with pytest.raises(AssertionError):
        run(b._raw_get("orders", ".evil.example/x"))
    assert calls == []


@pytest.mark.parametrize("env", ["live", "paper", "", "DEMO", "prod"])
def test_get_broker_non_demo_is_none(monkeypatch, env):
    monkeypatch.setenv("T212_DEMO_KEY", FAKE_KEY)
    monkeypatch.setenv("T212_DEMO_SECRET", FAKE_SECRET)
    assert broker.get_broker(env) is None
    assert broker_t212.from_env(env) is None
    assert env not in broker._singletons


# ====================================================== Phase 2: fresh / reads

def test_fresh_bypasses_cache_and_waits_out_interval():
    b, clock, calls = make_broker(json_handler(200, ACCOUNT_JSON))
    run(b.account())
    t0 = clock.now
    clock.now += 1.0                       # inside the 5 s window: plain call is cached
    run(b.account())
    assert len(calls) == 1
    before = clock.now
    run(b.account(fresh=True))
    assert len(calls) == 2
    assert clock.now - t0 >= broker_t212.INTERVALS_S["summary"] - 1e-9
    assert clock.now > before              # it waited, it did not fire early


def test_fresh_open_orders_and_history_hit_network_each_time():
    b, clock, calls = make_broker(lambda r: httpx.Response(
        200, json=ORDERS_JSON if r.url.path.endswith("/orders") else HISTORY_JSON))
    run(b.open_orders(fresh=True))
    run(b.open_orders(fresh=True))
    run(b.order_history(fresh=True))
    run(b.order_history(fresh=True))
    assert len(calls) == 4


def test_malformed_position_row_is_bad_response_naming_index():
    rows = [POSITIONS_JSON[0], {"instrument": {"ticker": "X"}, "quantity": "nan?"}]
    b, _, _ = make_broker(json_handler(200, rows))
    res = run(b.positions())
    assert res["error"] == "bad_response"
    assert "row 1" in res["detail"]


def test_malformed_open_order_row_is_bad_response_naming_index():
    rows = [ORDERS_JSON[0], {"id": 2, "ticker": "AAPL_US_EQ"}]
    b, _, _ = make_broker(json_handler(200, rows))
    res = run(b.open_orders())
    assert res["error"] == "bad_response"
    assert "row 1" in res["detail"]


@pytest.mark.parametrize("raw,expected", [
    ("LOCAL", "open"), ("UNCONFIRMED", "open"), ("CONFIRMED", "open"),
    ("NEW", "open"), ("CANCELLING", "open"), ("REPLACING", "open"),
    ("PARTIALLY_FILLED", "partially_filled"), ("FILLED", "filled"),
    ("CANCELLED", "cancelled"), ("REJECTED", "rejected"),
    ("REPLACED", "unknown"), ("SOMETHING_NEW", "unknown"),
])
def test_status_mapping_table(raw, expected):
    row = {"id": 7, "ticker": "AAPL_US_EQ", "side": "BUY", "status": raw,
           "quantity": 2.0, "filledQuantity": 0.5, "limitPrice": 10.5,
           "createdAt": "2026-10-19T08:00:00Z"}
    b, _, _ = make_broker(json_handler(200, [row]))
    (o,) = run(b.open_orders())
    assert o.status == expected
    assert o.raw_status == raw
    assert (o.id, o.ticker, o.side, o.quantity) == ("7", "AAPL_US_EQ", "buy", 2.0)
    assert o.filled_quantity == 0.5 and o.limit_price == 10.5
    assert o.created_at == 1792396800.0


def test_open_order_sell_negative_quantity_is_absolute():
    row = {"id": 8, "ticker": "AAPL_US_EQ", "side": "SELL", "status": "NEW",
           "quantity": -3.0, "filledQuantity": -1.0}
    b, _, _ = make_broker(json_handler(200, [row]))
    (o,) = run(b.open_orders())
    assert (o.side, o.quantity, o.filled_quantity) == ("sell", 3.0, 1.0)


def test_history_fill_mapping_and_aggregation():
    order = {"id": 5, "ticker": "AAPL_US_EQ", "side": "BUY", "status": "FILLED",
             "quantity": 3.0, "limitPrice": 101.0, "createdAt": "2026-10-19T08:00:00Z"}
    body = {"items": [
        {"order": order, "fill": {"id": 1, "price": 100.0, "quantity": 1.0,
                                  "walletImpact": {"netValue": -92.0}}},
        {"order": order, "fill": {"id": 2, "price": 101.0, "quantity": 2.0,
                                  "walletImpact": {"netValue": -186.0}}},
        {"order": {"id": 6, "ticker": "MSFT_US_EQ", "side": "SELL", "status": "CANCELLED",
                   "quantity": -1.0}},
    ], "nextPagePath": None}
    b, _, _ = make_broker(json_handler(200, body))
    a, c = run(b.order_history())
    assert (a.id, a.status, a.filled_quantity) == ("5", "filled", 3.0)
    assert a.avg_fill_price == pytest.approx((100.0 + 202.0) / 3)
    assert a.filled_value == pytest.approx(278.0)
    assert (c.id, c.side, c.status, c.filled_quantity) == ("6", "sell", "cancelled", 0.0)
    assert c.avg_fill_price is None and c.filled_value is None


# ================================================== Phase 2: POST / DELETE

ORDER_RESPONSE = {"id": 99, "ticker": "AAPL_US_EQ", "side": "BUY", "status": "NEW",
                  "type": "LIMIT", "quantity": 1.23, "limitPrice": 10.0}


def test_place_limit_order_buy_payload_and_mapping():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=ORDER_RESPONSE)
    b, _, calls = make_broker(handler)
    o = run(b.place_limit_order("AAPL_US_EQ", "buy", 1.239, 10.0))
    assert o.id == "99" and o.status == "open" and o.side == "buy"
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == broker_t212.DEMO_URL + "/equity/orders/limit"
    import json as _json
    assert _json.loads(req.content) == {"ticker": "AAPL_US_EQ", "quantity": 1.23,
                                        "limitPrice": 10.0, "timeValidity": "DAY"}


def test_place_limit_order_sell_sends_negative_quantity():
    import json as _json
    bodies = []

    def handler(request):
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json={**ORDER_RESPONSE, "side": "SELL", "quantity": -2.0})
    b, _, _ = make_broker(handler)
    o = run(b.place_limit_order("AAPL_US_EQ", "sell", 2.0, 10.0))
    assert bodies[0]["quantity"] == -2.0
    assert (o.side, o.quantity) == ("sell", 2.0)


def test_quantity_rounded_down_and_decimals_default():
    import json as _json
    bodies = []

    def handler(request):
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json=ORDER_RESPONSE)
    b, _, _ = make_broker(handler)
    assert b.quantity_decimals("AAPL_US_EQ") == 2
    run(b.place_limit_order("AAPL_US_EQ", "buy", 0.2999999, 10.0))
    assert bodies[0]["quantity"] == 0.29


@pytest.mark.parametrize("args", [
    ("AAPL_US_EQ", "hold", 1.0, 10.0),
    ("AAPL_US_EQ", "buy", 0.0, 10.0),
    ("AAPL_US_EQ", "buy", -1.0, 10.0),
    ("AAPL_US_EQ", "buy", 0.004, 10.0),      # rounds down to 0
    ("AAPL_US_EQ", "buy", 1.0, 0.0),
    ("AAPL_US_EQ", "buy", 1.0, -5.0),
    ("AAPL_US_EQ", "buy", float("nan"), 10.0),
    ("AAPL_US_EQ", "buy", 1.0, float("nan")),
    ("", "buy", 1.0, 10.0),
])
def test_place_limit_order_invalid_input_is_bad_request_without_network(args):
    b, _, calls = make_broker(json_handler(200, ORDER_RESPONSE))
    res = run(b.place_limit_order(*args))
    assert res["error"] == "bad_request"
    assert calls == []


def test_400_maps_to_rejected_with_broker_message_and_no_secret():
    body = {"code": "x", "message": f"Insufficient funds {FAKE_KEY}"}
    b, _, _ = make_broker(json_handler(400, body))
    res = run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    assert res["error"] == "rejected" and res["status"] == 400
    assert "Insufficient funds" in res["detail"]
    assert FAKE_KEY not in res["detail"] and FAKE_SECRET not in res["detail"]


def test_post_timeout_is_timeout_and_never_retried():
    def handler(request):
        raise httpx.TimeoutException("boom", request=request)
    b, _, calls = make_broker(handler)
    res = run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    assert res["error"] == "timeout"
    assert len(calls) == 1


def test_post_5xx_is_http_error_never_retried():
    b, _, calls = make_broker(json_handler(502, {}))
    res = run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    assert res["error"] == "http" and res["status"] == 502
    assert len(calls) == 1


def test_post_never_cached_and_throttled_by_waiting():
    b, clock, calls = make_broker(json_handler(200, ORDER_RESPONSE))
    run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    t0 = clock.now
    run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    assert len(calls) == 2                      # second POST really sent, no cache
    assert clock.now - t0 >= broker_t212.INTERVALS_S["order_post"] - 1e-9


def test_post_429_not_retried_and_pins_next_call():
    clock = FakeClock()
    reset = clock.now + 30.0
    b, clock, calls = make_broker(
        json_handler(429, {}, {"x-ratelimit-reset": str(reset)}), clock=clock)
    res = run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    assert res["error"] == "rate_limited"
    assert len(calls) == 1
    assert b._rate_limited_until["order_post"] == reset


def test_post_invalid_order_response_is_bad_response():
    b, _, _ = make_broker(json_handler(200, {"unexpected": True}))
    res = run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0))
    assert res["error"] == "bad_response"


def test_cancel_order_deletes_and_waits():
    b, clock, calls = make_broker(lambda r: httpx.Response(200))
    assert run(b.cancel_order("42")) is True
    t0 = clock.now
    assert run(b.cancel_order(42)) is True
    assert [c.method for c in calls] == ["DELETE", "DELETE"]
    assert calls[0].url.path == "/api/v0/equity/orders/42"
    assert clock.now - t0 >= broker_t212.INTERVALS_S["order_delete"] - 1e-9


def test_cancel_order_rejects_non_numeric_id_and_maps_errors():
    b, _, calls = make_broker(json_handler(404, {}))
    assert run(b.cancel_order("../x"))["error"] == "bad_request"
    assert calls == []
    res = run(b.cancel_order("5"))
    assert res["error"] == "http" and res["status"] == 404


def test_no_secret_in_any_write_detail_or_status():
    for handler in (json_handler(401, {}), json_handler(403, {}), json_handler(429, {}),
                    json_handler(500, {}), json_handler(400, {"message": FAKE_SECRET})):
        b, _, _ = make_broker(handler)
        results = [run(b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0)),
                   run(b.cancel_order("1"))]
        blob = repr(results) + repr(b.status())
        assert FAKE_KEY not in blob and FAKE_SECRET not in blob
        assert "authorization" not in blob.lower()


# ============================================ single-flight GETs + reachability

async def settle():
    """Let every runnable task reach its next real suspension point (no
    waiting: cooperative yields only)."""
    for _ in range(25):
        await asyncio.sleep(0)


def positions_body(quantity: float):
    return [{**POSITIONS_JSON[0], "quantity": quantity}]


BODY_BY_PATH = {
    "/api/v0/equity/account/summary": ACCOUNT_JSON,
    "/api/v0/equity/positions": POSITIONS_JSON,
    "/api/v0/equity/orders": ORDERS_JSON,
}


@pytest.mark.parametrize("method", ["account", "positions", "open_orders"])
def test_burst_of_concurrent_reads_is_one_http_request(method):
    async def scenario():
        gate = asyncio.Event()

        async def handler(request):
            await gate.wait()           # the request stays in flight
            return httpx.Response(200, json=BODY_BY_PATH[request.url.path])

        b, _, calls = make_broker(handler)
        tasks = [asyncio.ensure_future(getattr(b, method)()) for _ in range(5)]
        await settle()
        assert len(calls) == 1          # four callers joined the first one
        gate.set()
        results = await asyncio.gather(*tasks)
        assert len(calls) == 1
        assert not broker.is_error(results[0])
        assert all(r is results[0] for r in results)
        # the burst did not reset the throttle: the next plain call is the cache
        assert (await getattr(b, method)()) is results[0]
        assert len(calls) == 1
    run(scenario())


def test_burst_error_is_shared_and_counted_once():
    async def scenario():
        gate = asyncio.Event()

        async def handler(request):
            await gate.wait()
            return httpx.Response(500)

        b, _, calls = make_broker(handler)
        tasks = [asyncio.ensure_future(b.positions()) for _ in range(5)]
        await settle()
        gate.set()
        results = await asyncio.gather(*tasks)
        assert len(calls) == 1
        assert all(r["error"] == "http" for r in results)
        assert b.status()["errors"] == 1
    run(scenario())


def test_cancelling_one_caller_does_not_abort_the_shared_request():
    async def scenario():
        gate = asyncio.Event()

        async def handler(request):
            await gate.wait()
            return httpx.Response(200, json=POSITIONS_JSON)

        b, _, calls = make_broker(handler)
        t1 = asyncio.ensure_future(b.positions())
        t2 = asyncio.ensure_future(b.positions())
        await settle()
        t1.cancel()
        await settle()
        gate.set()
        rows = await t2
        assert not broker.is_error(rows) and len(rows) == 1
        assert len(calls) == 1
        assert t1.cancelled()
    run(scenario())


def test_fresh_caller_shares_an_in_flight_fresh_request():
    async def scenario():
        gate = asyncio.Event()
        seen = {"n": 0}

        async def handler(request):
            seen["n"] += 1
            await gate.wait()
            return httpx.Response(200, json=positions_body(float(seen["n"])))

        b, _, calls = make_broker(handler)
        t1 = asyncio.ensure_future(b.positions(fresh=True))
        await settle()
        t2 = asyncio.ensure_future(b.positions(fresh=True))   # arrives while t1's request is out
        await settle()
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert len(calls) == 1
        assert r1 is r2 and r1[0].quantity == 1.0
    run(scenario())


def test_fresh_caller_never_gets_the_cache_served_to_a_plain_caller():
    async def scenario():
        seen = {"n": 0}

        async def handler(request):
            seen["n"] += 1
            return httpx.Response(200, json=positions_body(float(seen["n"])))

        b, clock, calls = make_broker(handler)
        first = await b.positions()
        assert first[0].quantity == 1.0
        clock.now += 0.1                # inside the 1 s window: a plain call is the cache
        plain, fresh = await asyncio.gather(b.positions(), b.positions(fresh=True))
        assert plain is first           # plain caller: the cached (older) rows
        assert fresh[0].quantity == 2.0   # fresh caller: a request made after it asked
        assert len(calls) == 2
    run(scenario())


def test_fresh_caller_does_not_join_a_plain_in_flight_request_and_waits_the_interval():
    async def scenario():
        gate = asyncio.Event()
        seen = {"n": 0}

        async def handler(request):
            seen["n"] += 1
            n = seen["n"]
            if n == 1:
                await gate.wait()
            return httpx.Response(200, json=positions_body(float(n)))

        b, clock, calls = make_broker(handler)
        t1 = asyncio.ensure_future(b.positions())
        await settle()
        t0 = clock.now
        t2 = asyncio.ensure_future(b.positions(fresh=True))
        await settle()
        assert len(calls) == 1          # the fresh call is queued, not sharing
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1[0].quantity == 1.0    # sent before the fresh call existed
        assert r2[0].quantity == 2.0
        assert len(calls) == 2
        assert clock.now - t0 >= broker_t212.INTERVALS_S["positions"] - 1e-9
    run(scenario())


def test_fresh_caller_does_not_join_a_read_that_was_sent_before_a_write():
    async def scenario():
        gate = asyncio.Event()
        seen = {"gets": 0}

        async def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json=ORDER_RESPONSE)
            seen["gets"] += 1
            if seen["gets"] == 1:
                await gate.wait()
            return httpx.Response(200, json=ORDERS_JSON)

        b, _, calls = make_broker(handler)
        t1 = asyncio.ensure_future(b.open_orders(fresh=True))
        await settle()                  # request 1 is out, before the order exists
        placed = await b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0)
        assert not broker.is_error(placed)
        t2 = asyncio.ensure_future(b.open_orders(fresh=True))
        await settle()
        gate.set()
        await asyncio.gather(t1, t2)
        assert [c.method for c in calls].count("GET") == 2
    run(scenario())


def test_rate_limited_does_not_flip_reachable_and_is_reported_separately():
    async def scenario():
        clock = FakeClock()
        reset = clock.now + 30.0
        seq = [lambda: httpx.Response(200, json=ACCOUNT_JSON),
               lambda: httpx.Response(429, headers={"x-ratelimit-reset": str(reset)}),
               lambda: httpx.Response(200, json=ACCOUNT_JSON)]
        n = {"i": 0}

        def handler(request):
            n["i"] += 1
            return seq[n["i"] - 1]()

        b, clock, _ = make_broker(handler, clock=clock)
        assert b.status()["reachable"] is False and b.status()["rateLimitedUntil"] is None
        assert not broker.is_error(await b.account())
        assert b.status()["reachable"] is True

        limited = await b.account(fresh=True)
        assert limited["error"] == "rate_limited"
        s = b.status()
        assert s["reachable"] is True                     # throttled, not unreachable
        assert s["rateLimitedUntil"] == reset
        assert s["lastError"]["error"] == "rate_limited"

        assert not broker.is_error(await b.account(fresh=True))   # waited out the reset
        s = b.status()
        assert s["reachable"] is True and s["rateLimitedUntil"] is None
    run(scenario())


def test_rate_limited_before_any_success_stays_unreachable():
    b, clock, _ = make_broker(json_handler(429, {}, {"x-ratelimit-reset": "1000100"}))
    assert run(b.account())["error"] == "rate_limited"
    s = b.status()
    assert s["reachable"] is False
    assert s["rateLimitedUntil"] == 1000100.0


@pytest.mark.parametrize("failure", [401, 403, 500, "timeout", "bad_json"])
def test_connectivity_failures_flip_reachable_and_success_restores_it(failure):
    async def scenario():
        n = {"i": 0}

        def handler(request):
            n["i"] += 1
            if n["i"] == 2:
                if failure == "timeout":
                    raise httpx.TimeoutException("boom", request=request)
                if failure == "bad_json":
                    return httpx.Response(200, content=b"not json")
                return httpx.Response(failure, json={})
            return httpx.Response(200, json=ACCOUNT_JSON)

        b, _, _ = make_broker(handler)
        assert not broker.is_error(await b.account())
        assert b.status()["reachable"] is True
        assert broker.is_error(await b.account(fresh=True))
        assert b.status()["reachable"] is False
        assert not broker.is_error(await b.account(fresh=True))
        assert b.status()["reachable"] is True
    run(scenario())


def test_write_429_keeps_reachable_but_write_timeout_flips_it():
    async def scenario():
        n = {"i": 0}

        def handler(request):
            n["i"] += 1
            if request.method == "GET":
                return httpx.Response(200, json=ACCOUNT_JSON)
            if n["i"] == 2:
                return httpx.Response(429, headers={"x-ratelimit-reset": "1000100"})
            raise httpx.TimeoutException("boom", request=request)

        b, _, _ = make_broker(handler)
        assert not broker.is_error(await b.account())
        res = await b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0)
        assert res["error"] == "rate_limited"
        assert b.status()["reachable"] is True
        res = await b.place_limit_order("AAPL_US_EQ", "buy", 1.0, 10.0)
        assert res["error"] == "timeout"
        assert b.status()["reachable"] is False
    run(scenario())
