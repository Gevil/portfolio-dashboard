"""USD->EUR provider chain, provenance/staleness and daily rates (no network).

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import json
import time

import httpx
import pytest

from app.api import forex


@pytest.fixture(autouse=True)
def fresh_forex(monkeypatch, tmp_path):
    monkeypatch.setenv("HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(forex, "_state", {"rate": None, "source": None, "asOf": None})
    monkeypatch.setattr(forex, "_loaded", False)
    monkeypatch.setattr(forex, "_next_retry", 0.0)
    monkeypatch.setattr(forex, "_daily", {})
    monkeypatch.setattr(forex, "_daily_fetched_at", 0.0)
    monkeypatch.setattr(forex, "_daily_span", 0)
    monkeypatch.setattr(forex, "_client", None)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def providers(monkeypatch, *chain):
    """chain: (name, rate-or-exception-or-None) in order."""
    funcs = []
    for name, outcome in chain:
        async def fetch(_client, outcome=outcome):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        funcs.append((name, fetch))
    monkeypatch.setattr(forex, "_PROVIDERS", tuple(funcs))


# ---------------------------------------------------------------- latest

def test_first_provider_wins_and_result_carries_provenance(monkeypatch):
    providers(monkeypatch, ("frankfurter", 0.88), ("yahoo", 0.5))
    res = run(forex.get_rate_usd_eur())
    assert res["rate"] == 0.88 and res["source"] == "frankfurter"
    assert res["stale"] is False and abs(res["asOf"] - time.time()) < 5


def test_chain_falls_through_failures_to_yahoo(monkeypatch):
    providers(monkeypatch, ("frankfurter", RuntimeError("301")), ("yahoo", 0.91))
    res = run(forex.get_rate_usd_eur())
    assert (res["rate"], res["source"]) == (0.91, "yahoo")


def test_no_provider_and_no_cache_gives_null_rate_never_a_constant(monkeypatch):
    providers(monkeypatch, ("frankfurter", None), ("yahoo", RuntimeError("down")))
    res = run(forex.get_rate_usd_eur())
    assert res == {"rate": None, "source": None, "asOf": None, "stale": True}
    assert not hasattr(forex, "FALLBACK_RATE")
    assert not hasattr(forex, "get_rate_usd_eur_display")


def test_last_cached_rate_is_used_and_flagged_stale_after_24h(monkeypatch):
    providers(monkeypatch, ("frankfurter", None), ("yahoo", None))
    forex._state.update(rate=0.87, source="frankfurter", asOf=time.time() - 30 * 3600)
    forex._loaded = True
    res = run(forex.get_rate_usd_eur())
    assert res["rate"] == 0.87 and res["stale"] is True and res["source"] == "frankfurter"


def test_recent_cache_is_fresh_and_not_refetched(monkeypatch):
    providers(monkeypatch, ("frankfurter", RuntimeError("must not be called")))
    forex._state.update(rate=0.9, source="yahoo", asOf=time.time() - 60)
    forex._loaded = True
    res = run(forex.get_rate_usd_eur())
    assert res["rate"] == 0.9 and res["stale"] is False


def test_failed_refresh_backs_off_instead_of_hammering(monkeypatch):
    calls = []

    async def failing(_client):
        calls.append(1)
        return None
    monkeypatch.setattr(forex, "_PROVIDERS", (("frankfurter", failing),))

    async def twice():
        await forex.get_rate_usd_eur()
        await forex.get_rate_usd_eur()
    run(twice())
    assert len(calls) == 1


def test_rate_survives_a_restart_via_disk(monkeypatch, fresh_forex):
    providers(monkeypatch, ("frankfurter", 0.89))
    run(forex.get_rate_usd_eur())
    saved = json.loads((fresh_forex / "forex.json").read_text())
    assert saved["rate"] == 0.89 and saved["source"] == "frankfurter"

    # "restart": memory gone, providers dead -> the persisted value comes back
    monkeypatch.setattr(forex, "_state", {"rate": None, "source": None, "asOf": None})
    monkeypatch.setattr(forex, "_loaded", False)
    providers(monkeypatch, ("frankfurter", None))
    assert forex.cached_rate()["rate"] == 0.89


def test_insane_rates_are_rejected():
    assert forex._sane(0.89) == 0.89
    assert forex._sane(89) is None and forex._sane("x") is None and forex._sane(None) is None


# ---------------------------------------------------------------- frankfurter

def test_client_follows_redirects():
    async def make():
        return forex._client_for_loop().follow_redirects
    assert run(make()) is True


def test_frankfurter_latest_uses_the_current_host_and_parses_rate():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"amount": 1.0, "base": "USD", "date": "2026-10-02",
                                         "rates": {"EUR": 0.89087}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await forex._frankfurter_latest(c)
    assert run(go()) == 0.89087
    req = seen[0]
    assert req.url.host == "api.frankfurter.dev" and req.url.path == "/v1/latest"
    assert dict(req.url.params) == {"base": "USD", "symbols": "EUR"}


def test_frankfurter_non_200_is_no_rate():
    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(301))
        async with httpx.AsyncClient(transport=transport) as c:
            return await forex._frankfurter_latest(c)
    assert run(go()) is None


# ---------------------------------------------------------------- daily

def test_daily_rates_parses_range_and_caches(monkeypatch):
    calls = []

    async def fake(client, start, end):
        calls.append((start, end))
        return {"2026-09-18": 0.8726, "2026-09-21": 0.87032}
    monkeypatch.setattr(forex, "_frankfurter_daily", fake)

    async def go():
        a = await forex.daily_rates(5000)
        b = await forex.daily_rates(30)
        return a, b
    a, b = run(go())
    assert len(calls) == 1                      # second call served from cache
    assert a["2026-09-18"] == 0.8726


def test_daily_rates_failure_returns_empty_dict_and_never_raises(monkeypatch):
    async def boom(client, start, end):
        raise RuntimeError("down")

    async def no_yahoo(days):
        raise RuntimeError("down too")
    monkeypatch.setattr(forex, "_frankfurter_daily", boom)
    monkeypatch.setattr(forex, "_yahoo_daily", no_yahoo)
    assert run(forex.daily_rates(30)) == {}


def test_daily_rates_falls_back_to_yahoo(monkeypatch):
    async def empty(client, start, end):
        return {}

    async def yahoo(days):
        return {"2026-09-21": 0.87}
    monkeypatch.setattr(forex, "_frankfurter_daily", empty)
    monkeypatch.setattr(forex, "_yahoo_daily", yahoo)
    assert run(forex.daily_rates(400)) == {"2026-09-21": 0.87}


def test_rate_on_carries_the_last_reference_day_over_weekends():
    rates = {"2026-09-18": 0.8726, "2026-09-21": 0.87032}
    assert forex.rate_on(rates, "2026-09-21") == 0.87032
    assert forex.rate_on(rates, "2026-09-19") == 0.8726     # Saturday -> Friday
    assert forex.rate_on(rates, "2026-09-20") == 0.8726     # Sunday -> Friday
    assert forex.rate_on(rates, "2026-09-17") is None       # nothing earlier
    assert forex.rate_on(rates, "2026-12-01") is None       # beyond the lookback
    assert forex.rate_on(rates, "garbage") is None
