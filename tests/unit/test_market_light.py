"""market_light: the status comes from the index + momentum dimensions only;
the holdings breadth is the informational ``holdingsTrend``; drop alerts go out
through notify.alert and never mention the holdings."""
import asyncio
import sys
import types

import pytest

from app.api import market_light, notify


def series(growth, n=250):
    return [100.0 * growth ** i for i in range(n)]


RISING = series(1.003)      # index +33% vs MA200, 5-day return +1.5%: green/green
FALLING = series(0.997)     # -27% vs MA200, 5-day return -1.5%: red/red
HOLDINGS = ("ASML", "NVDA", "SXR8")


@pytest.fixture
def ml(tmp_path, monkeypatch):
    monkeypatch.setattr(market_light, "SNAPSHOT_FILE", tmp_path / "light.json")
    monkeypatch.setattr(market_light, "cfg",
                        lambda: dict(market_light.DEFAULTS))
    monkeypatch.setattr(market_light.prices, "is_alertable",
                        lambda s: s != "GSPC")
    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(
        get_watchlist=lambda: list(HOLDINGS) + ["GSPC"]))
    h = {"closes": {"GSPC": RISING, **{s: series(0.997, 30) for s in HOLDINGS}},
         "delivery": notify.Delivery(notify.SENT), "alerts": []}

    async def daily_closes(symbol, range_key):
        return h["closes"].get(symbol, [])

    async def alert(ticker, source, title, body, **kw):
        h["alerts"].append({"ticker": ticker, "source": source,
                            "title": title, "body": body, **kw})
        return h["delivery"]

    monkeypatch.setattr(market_light, "_daily_closes", daily_closes)
    monkeypatch.setattr(market_light.notify, "alert", alert)
    return h


def snap():
    return asyncio.run(market_light.snapshot())


def test_status_ignores_the_holdings_even_when_every_one_is_below_its_ma(ml):
    s = snap()
    assert s["status"] == "green" and s["score"] == 100.0
    assert s["holdingsTrend"]["available"] is True
    assert s["holdingsTrend"]["above"] == 0 and s["holdingsTrend"]["total"] == 3
    assert s["holdingsTrend"]["pct"] == 0.0


def test_holdings_rising_cannot_rescue_a_red_index(ml):
    ml["closes"] = {"GSPC": FALLING, **{s: series(1.003, 30) for s in HOLDINGS}}
    s = snap()
    assert s["status"] == "red" and s["score"] == 0.0
    assert s["holdingsTrend"]["pct"] == 100.0


def test_payload_dims_are_index_and_momentum_only(ml):
    s = snap()
    assert set(s["dims"]) == {"index", "momentum"}
    assert s["dimensions"] == s["dims"]            # legacy key, same content
    assert "breadth" not in s["dims"]
    assert not any("holdings" in r or "breadth" in r for r in s["reasons"])


def test_holdings_trend_is_labelled_and_informational(ml):
    t = snap()["holdingsTrend"]
    assert t["label"] == "your holdings above MA20" and t["period"] == 20
    assert "0/3 your holdings above MA20 (0%)" == t["detail"]


def test_too_few_holdings_leave_trend_unavailable_but_status_untouched(ml):
    ml["closes"] = {"GSPC": RISING, "ASML": series(0.997, 30)}   # 1 < min 2
    s = snap()
    assert s["holdingsTrend"]["available"] is False
    assert s["holdingsTrend"]["total"] == 1
    assert s["status"] == "green" and s["data_quality"] == "ok"


def test_data_quality_needs_both_index_and_momentum(ml):
    ml["closes"] = {"GSPC": [100.0] * 10,           # no MA200, 5-day return 0
                    **{s: series(1.003, 30) for s in HOLDINGS}}
    s = snap()
    assert s["dims"]["index"]["available"] is False
    assert s["dims"]["momentum"]["available"] is True
    assert s["data_quality"] == "limited"
    assert s["status"] == "yellow"                  # momentum alone; holdings idle


# ------------------------------------------------------------- drop alerts

def seed_previous(status):
    market_light._save(market_light.SNAPSHOT_FILE, {"status": status})


def test_drop_alert_is_a_market_light_alert_that_never_mentions_holdings(ml):
    seed_previous("green")
    ml["closes"] = {"GSPC": FALLING, **{s: series(1.003, 30) for s in HOLDINGS}}
    s = asyncio.run(market_light.run_once())
    assert s["status"] == "red"
    [a] = ml["alerts"]
    assert (a["ticker"], a["source"]) == ("MARKET", "market_light")
    assert a["title"] == "Market light green -> red"
    assert a["priority"] == 4 and a["severity"] == "warning"
    assert "index:" in a["body"] and "momentum:" in a["body"]
    assert "holdings" not in a["body"].lower()
    assert "breadth" not in a["body"].lower()
    assert market_light.current()["status"] == "red"


def test_no_alert_when_only_the_holdings_fall(ml):
    seed_previous("green")                          # holdings already falling
    asyncio.run(market_light.run_once())
    assert ml["alerts"] == []
    assert market_light.current()["status"] == "green"


def test_failed_drop_alert_keeps_the_previous_snapshot_for_retry(ml):
    seed_previous("green")
    ml["closes"]["GSPC"] = FALLING
    ml["delivery"] = notify.Delivery(notify.FAILED)
    asyncio.run(market_light.run_once())
    assert market_light.current()["status"] == "green"
    assert market_light._stats["unpublished"] is True

    ml["delivery"] = notify.Delivery(notify.SENT)
    asyncio.run(market_light.run_once())
    assert market_light.current()["status"] == "red"
    assert market_light._stats["unpublished"] is False


@pytest.mark.parametrize("status,reason", [
    (notify.FILTERED, "cap"), (notify.QUEUED, "quiet hours"),
    (notify.DUPLICATE, "")])
def test_a_consumed_delivery_publishes_the_new_snapshot(ml, status, reason):
    seed_previous("green")
    ml["closes"]["GSPC"] = FALLING
    ml["delivery"] = notify.Delivery(status, reason)
    asyncio.run(market_light.run_once())
    assert market_light.current()["status"] == "red"
