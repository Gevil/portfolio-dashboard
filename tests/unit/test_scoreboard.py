"""scoreboard v3: grading math on synthetic series (T+5 / T+20 separately), the
verdict band, sign-adjusted excess, Wilson intervals, n semantics, the
insufficient-sample rule, and the missing-benchmark path. tmp files only."""
import asyncio
import datetime as dt
import json
import math

import pytest

from app.api import scoreboard as sb

UTC = dt.timezone.utc
ANCHOR = dt.date(2026, 6, 15)          # a Monday
TODAY = dt.date(2026, 8, 1)            # T+20 has long matured


def ts_of(day: dt.date, hour: int = 12) -> float:
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()


def weekdays(start: dt.date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


DAYS = weekdays(dt.date(2026, 4, 1), 120)


def series(symbol, fn):
    """_Series over DAYS with close = fn(day)."""
    return sb._Series(symbol, [{"t": ts_of(d, 16), "c": fn(d)} for d in DAYS])


def flat_noise(d):
    """~0.14% daily sigma before the anchor: band floor (0.25) applies."""
    return 100.0 + (0.1 if d.toordinal() % 2 else 0.0)


def own_fn(t5, t20):
    t5_day = sb.macro.advance_trading_days("XAMS", ANCHOR, 5)
    t20_day = sb.macro.advance_trading_days("XAMS", ANCHOR, 20)

    def f(d):
        if d == t5_day:
            return t5
        if d == t20_day:
            return t20
        return 100.0 if d <= ANCHOR else flat_noise(d)
    return f


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sb.macro, "market_for_ticker", lambda t: "XAMS")
    monkeypatch.setattr(sb, "OUTCOMES", tmp_path / "outcomes.json")
    monkeypatch.setattr(sb, "FEEDBACK", tmp_path / "feedback.json")
    monkeypatch.setattr(sb, "ADVICE_LOG", tmp_path / "advice_log.json")
    monkeypatch.setattr(sb.registry, "benchmark_id", lambda: "GSPC")


def ctx_with(own, bench_fn=lambda d: 5000.0, bench_reason="", rate=0.9):
    c = sb._Ctx()
    c.series["ASML"] = own
    c.bench_id = "GSPC"
    c.bench = series("GSPC", bench_fn)
    c.bench_reason = bench_reason
    c.rates = {d.isoformat(): rate for d in DAYS}
    return c


def advice(action="buy", rating="BUY", price=100.0, day=ANCHOR, **kw):
    return {"ticker": "ASML", "ts": ts_of(day), "date": day.isoformat(),
            "rating": rating, "action": action, "score": 70, "confidence": "high",
            "priceAtAdvice": price, "priceAsOf": ts_of(day, 10), "priceCurrency": "EUR",
            "data_quality": "full", "lane": "L", "model": "M", **kw}


def fresh(adv):
    return sb._fresh_row(sb._key(adv["ts"], "ASML"), adv["ts"], "ASML", adv)


def grade(row, ctx, today=TODAY):
    return asyncio.run(sb._grade(row, ctx, today))


# ---------------------------------------------------------------- pure maths

def test_wilson_interval():
    assert sb.wilson(0, 0) is None
    lo, hi = sb.wilson(15, 30)
    assert 33.0 < lo < 34.0 and 66.0 < hi < 67.0
    lo, hi = sb.wilson(30, 30)
    assert 88.0 < lo < 89.5 and hi == 100.0
    lo, hi = sb.wilson(0, 10)
    assert lo == 0.0 and 0 < hi < 35


def test_band_scales_with_horizon_and_has_a_floor():
    assert sb.band_pct(0.0, 5) == sb.BAND_FLOOR_PCT
    s = 2.0
    assert sb.band_pct(s, 20) == pytest.approx(sb.BAND_K * s * math.sqrt(20))
    assert sb.band_pct(s, 20) > sb.band_pct(s, 5) > sb.BAND_FLOOR_PCT


def test_edge_is_sign_adjusted_and_hold_has_no_direction():
    assert sb.edge("buy", 3.0) == 3.0 and sb.edge("add", -2.0) == -2.0
    assert sb.edge("sell", -3.0) == 3.0 and sb.edge("reduce", 2.0) == -2.0
    assert sb.edge("hold", 3.0) is None and sb.edge("buy", None) is None


def test_verdicts():
    assert sb.verdict("buy", 5.0, 1.0) == "hit"
    assert sb.verdict("buy", -5.0, 1.0) == "miss"
    assert sb.verdict("buy", 0.5, 1.0) == "neutral"
    assert sb.verdict("sell", -5.0, 1.0) == "hit"          # fell vs benchmark: right
    assert sb.verdict("sell", 5.0, 1.0) == "miss"
    assert sb.verdict("hold", 0.5, 1.0) == "hit"           # stayed inside the band
    assert sb.verdict("watch", 5.0, 1.0) == "miss"
    assert sb.verdict("buy", None, 1.0) is None


# ------------------------------------------------------------- grading rows

def test_horizons_are_graded_separately_with_sign_and_benchmark_in_eur():
    row = fresh(advice())
    assert row["anchor_basis"] == "none" or row["anchor_basis"] == "priceAtAdvice"
    assert row["anchor_basis"] == "priceAtAdvice" and row["anchor_price"] == 100.0
    newly = grade(row, ctx_with(series("ASML", own_fn(110.0, 90.0))))
    assert newly == 2 and row["eval_status"] == "completed"
    h5, h20 = row["horizons"]["5"], row["horizons"]["20"]
    assert h5["ret"] == pytest.approx(10.0, abs=0.01) and h5["bench"] == pytest.approx(0.0, abs=0.01)
    assert h5["verdict"] == "hit" and h5["edge"] == pytest.approx(10.0, abs=0.01)
    assert h20["ret"] == pytest.approx(-10.0, abs=0.01) and h20["verdict"] == "miss"
    assert h5["band"] >= sb.BAND_FLOOR_PCT


def test_a_constant_fx_rate_does_not_move_the_eur_benchmark():
    row = fresh(advice())
    grade(row, ctx_with(series("ASML", own_fn(110.0, 110.0)), rate=0.5))
    assert row["horizons"]["5"]["bench"] == pytest.approx(0.0, abs=0.01)


def test_benchmark_moves_in_eur_when_the_fx_rate_moves():
    row = fresh(advice())
    ctx = ctx_with(series("ASML", own_fn(110.0, 110.0)))
    t5 = sb.macro.advance_trading_days("XAMS", ANCHOR, 5)
    ctx.rates = {d.isoformat(): (0.9 if d <= ANCHOR else 0.99) for d in DAYS}
    grade(row, ctx)
    assert row["horizons"]["5"]["bench"] == pytest.approx(10.0, abs=0.01)   # 0.99/0.9 - 1
    assert t5 > ANCHOR


def test_sell_advice_that_falls_is_a_hit():
    row = fresh(advice(action="sell", rating="SELL"))
    grade(row, ctx_with(series("ASML", own_fn(90.0, 90.0))))
    assert row["horizons"]["5"]["verdict"] == "hit"
    assert row["horizons"]["5"]["edge"] == pytest.approx(10.0, abs=0.01)


def test_hold_inside_the_band_is_a_hit_outside_a_miss():
    row = fresh(advice(action="hold", rating="HOLD"))
    grade(row, ctx_with(series("ASML", own_fn(100.1, 110.0))))
    assert row["horizons"]["5"]["verdict"] == "hit"
    assert row["horizons"]["20"]["verdict"] == "miss"


def test_only_matured_horizons_are_graded_and_the_row_stays_pending():
    row = fresh(advice())
    t5 = sb.macro.advance_trading_days("XAMS", ANCHOR, 5)
    newly = grade(row, ctx_with(series("ASML", own_fn(110.0, 90.0))),
                  today=t5 + dt.timedelta(days=3))
    assert newly == 1 and set(row["horizons"]) == {"5"}
    assert row["eval_status"] == "pending"
    # the next pass (after T+20) finishes the row without regrading T+5
    first = dict(row["horizons"]["5"])
    assert grade(row, ctx_with(series("ASML", own_fn(110.0, 90.0)))) == 1
    assert row["horizons"]["5"] == first and row["eval_status"] == "completed"


def test_missing_benchmark_marks_the_row_unable_and_retryable():
    row = fresh(advice())
    ctx = ctx_with(series("ASML", own_fn(110.0, 90.0)), bench_reason="missing_benchmark_price")
    assert grade(row, ctx) == 0
    assert row["eval_status"] == "unable" and row["unable_reason"] == "missing_benchmark_price"
    assert row["retry_at"] > 0 and row["horizons"] == {}


def test_missing_fx_rate_for_a_day_is_unable_not_graded_against_nothing():
    row = fresh(advice())
    ctx = ctx_with(series("ASML", own_fn(110.0, 90.0)))
    ctx.rates = {}
    grade(row, ctx)
    assert row["eval_status"] == "unable" and "benchmark" in row["unable_reason"]


def test_short_history_means_unknown_volatility_so_unable():
    row = fresh(advice())
    short = sb._Series("ASML", [{"t": ts_of(d, 16), "c": 100.0} for d in DAYS[-5:]])
    grade(row, ctx_with(short))
    assert row["eval_status"] == "unable"


def test_row_without_a_parseable_price_anchors_on_the_next_executable_close():
    adv = advice()
    adv.pop("priceAtAdvice")
    row = fresh(adv)
    assert row["anchor_basis"] == "next_close"
    nxt = sb.macro.advance_trading_days("XAMS", ANCHOR, 1)
    assert row["anchor_date"] == nxt.isoformat()                 # strictly after the advice day
    ctx = ctx_with(series("ASML", own_fn(110.0, 110.0)))
    grade(row, ctx)
    assert row["anchor_price"] is not None and row["anchor_date"] == nxt.isoformat()


def test_non_eur_price_at_advice_is_not_used_as_the_anchor():
    row = fresh(advice(priceCurrency="USD"))
    assert row["anchor_basis"] == "next_close"


# -------------------------------------------------- statistics / n semantics

def graded_row(i, verdict5="hit", verdict20=None, ticker=None, day_offset=None):
    day = DAYS[10 + (i if day_offset is None else day_offset)]
    adv = advice(day=day)
    adv["ticker"] = ticker or f"T{i:03d}"
    row = sb._fresh_row(f"k{i}", ts_of(day), adv["ticker"], adv)
    row["horizons"] = {}
    if verdict5:
        row["horizons"]["5"] = {"verdict": verdict5, "excess": 2.0 if verdict5 == "hit" else -2.0,
                                "edge": 2.0 if verdict5 == "hit" else -2.0, "band": 0.5}
    if verdict20:
        row["horizons"]["20"] = {"verdict": verdict20, "excess": 3.0, "edge": 3.0, "band": 1.0}
    row["eval_status"] = "completed" if verdict20 else "pending"
    return row


def write_store(rows):
    sb.OUTCOMES.write_text(json.dumps({"rows": rows, "engine_version": sb.ENGINE_VERSION,
                                       "last_pass": {}}))


def payload():
    return asyncio.run(sb.scoreboard())


def test_cell_n_counts_graded_rows_of_that_horizon_only():
    rows = [graded_row(i, "hit", "hit") for i in range(5)]
    rows += [graded_row(i, "miss", None) for i in range(5, 12)]       # T+20 not yet
    write_store(rows)
    cell = payload()["summary"]["BUY"]
    assert cell["byHorizon"]["5"]["n"] == 12
    assert cell["byHorizon"]["20"]["n"] == 5            # NOT 12: no "T+20 else T+5"
    assert cell["n"] == 5                               # flat fields = the T+20 cell
    assert cell["byHorizon"]["5"]["hits"] == 5 and cell["byHorizon"]["5"]["misses"] == 7


def test_insufficient_sample_below_min_samples_and_wilson_when_enough():
    rows = [graded_row(i, "hit" if i % 2 == 0 else "miss") for i in range(sb.MIN_SAMPLES - 1)]
    write_store(rows)
    c5 = payload()["summary"]["BUY"]["byHorizon"]["5"]
    assert c5["insufficient_sample"] is True and c5["n"] == sb.MIN_SAMPLES - 1
    write_store([graded_row(i, "hit" if i % 2 == 0 else "miss") for i in range(sb.MIN_SAMPLES)])
    c5 = payload()["summary"]["BUY"]["byHorizon"]["5"]
    assert c5["insufficient_sample"] is False
    assert c5["hitRatePct"] == 50.0 and len(c5["wilson95"]) == 2
    assert c5["wilson95"][0] < 50.0 < c5["wilson95"][1]


def test_neutrals_are_reported_but_not_folded_into_the_hit_rate():
    rows = [graded_row(0, "hit"), graded_row(1, "neutral"), graded_row(2, "miss")]
    write_store(rows)
    c5 = payload()["summary"]["BUY"]["byHorizon"]["5"]
    assert (c5["hits"], c5["misses"], c5["neutral"], c5["decided"]) == (1, 1, 1, 2)
    assert c5["hitRatePct"] == 50.0


def test_one_row_per_ticker_per_day_for_statistics():
    a = graded_row(0, "miss", ticker="ASML", day_offset=0)
    b = graded_row(1, "hit", ticker="ASML", day_offset=0)
    b["advice_ts"] = a["advice_ts"] + 3600                         # the day's latest call wins
    write_store([a, b])
    out = payload()
    assert out["summary"]["BUY"]["byHorizon"]["5"]["n"] == 1
    assert out["summary"]["BUY"]["byHorizon"]["5"]["hits"] == 1
    assert out["status"]["advice_days"] == 1


def test_baseline_row_treats_every_graded_call_as_a_long():
    rows = [graded_row(0, "hit"), graded_row(1, "hit")]
    rows[1]["action"], rows[1]["rating"] = "sell", "SELL"
    rows[1]["horizons"]["5"] = {"verdict": "hit", "excess": -4.0, "edge": 4.0, "band": 0.5}
    write_store(rows)
    out = payload()
    base = out["summary"]["BASELINE"]["byHorizon"]["5"]
    assert base["n"] == 2 and base["hits"] == 1 and base["misses"] == 1   # sell fell: long lost
    assert out["summary"]["SELL"]["byHorizon"]["5"]["hits"] == 1          # as a sell it won


def test_status_explains_when_nothing_can_be_graded():
    unable = graded_row(0, None)
    unable["eval_status"], unable["unable_reason"] = "unable", "missing_benchmark_price"
    write_store([unable])
    out = payload()
    assert out["status"]["ok"] is False and out["status"]["graded_rows"] == {"5": 0, "20": 0}
    assert out["status"]["unable"] == {"missing_benchmark_price": 1}
    assert any("0 graded rows" in w for w in out["warnings"])


def test_empty_store_is_a_valid_payload():
    out = payload()
    assert out["entries"] == [] and out["status"]["ok"] is False


# ----------------------------------------------------- grading pass / store

def test_run_pass_syncs_the_advice_log_and_never_prunes_outcomes(monkeypatch):
    adv = advice()
    sb.ADVICE_LOG.write_text(json.dumps([adv]))

    async def load_bench(self):
        self.bench_id, self.bench = "GSPC", series("GSPC", lambda d: 5000.0)
        self.rates = {d.isoformat(): 0.9 for d in DAYS}

    async def load_series(self, symbol):
        self.series[symbol] = series(symbol, own_fn(110.0, 90.0))
        return self.series[symbol]
    monkeypatch.setattr(sb._Ctx, "load_benchmark", load_bench)
    monkeypatch.setattr(sb._Ctx, "load_series", load_series)
    monkeypatch.setattr(sb.dt, "datetime", type("D", (dt.datetime,), {
        "now": classmethod(lambda cls, tz=None: dt.datetime(2026, 8, 1, tzinfo=tz))}))
    res = asyncio.run(sb.run_pass())
    assert res["graded"] == 2 and res["completed"] == 1
    # the advice log rotates: its row disappears, the outcome row must stay
    sb.ADVICE_LOG.write_text(json.dumps([]))
    asyncio.run(sb.run_pass())
    store = json.loads(sb.OUTCOMES.read_text())
    assert len(store["rows"]) == 1 and store["rows"][0]["eval_status"] == "completed"


def test_recent_lessons_stay_empty_until_there_is_a_sample():
    write_store([graded_row(i, "hit", "hit", ticker="ASML", day_offset=i)
                 for i in range(sb.MIN_SAMPLES - 1)])
    assert sb.recent_lessons("ASML", 5) == []
    write_store([graded_row(i, "hit", "hit", ticker="ASML", day_offset=i % 90)
                 for i in range(sb.MIN_SAMPLES + 5)])
    # rows older than LESSON_DAYS don't make lessons, but the gate itself passes
    assert isinstance(sb.recent_lessons("ASML", 5), list)
