"""scoreboard v3: grading math on synthetic series (T+5 / T+20 separately), the
verdict band, sign-adjusted excess, Wilson intervals, n semantics, the
insufficient-sample rule, the missing-benchmark path, hold/watch without a
directional verdict, the probabilistic block (Brier/BSS/log score/reliability/
Spearman IC), the baselines, the v2 -> v3 regrade and the EUR benchmark path
(no FX). tmp files only."""
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
    monkeypatch.setattr(sb.config_store, "read", lambda: {})

    async def no_closes(ticker, period="2y"):
        return []
    monkeypatch.setattr(sb.prices, "daily_closes", no_closes, raising=False)


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
    assert sb.verdict("hold", 0.5, 1.0) is None            # no direction: no hit/miss
    assert sb.verdict("watch", 5.0, 1.0) is None
    assert sb.verdict("trim", -5.0, 1.0) == "hit"
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


def test_hold_rows_are_graded_probabilistically_with_no_directional_verdict():
    row = fresh(advice(action="hold", rating="HOLD"))
    assert row["graded_as"] == "probabilistic_only"
    grade(row, ctx_with(series("ASML", own_fn(100.1, 110.0))))
    for h in ("5", "20"):
        assert row["horizons"][h]["verdict"] is None
        assert row["horizons"][h]["edge"] is None
        assert row["horizons"][h]["graded_as"] == "probabilistic_only"
    assert row["horizons"]["20"]["excess"] == pytest.approx(10.0, abs=0.01)
    assert row["eval_status"] == "completed"               # graded = excess stored


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


# ------------------------------------------------- v3: probabilistic metrics

PAIRS = [(0.8, 1), (0.6, 1), (0.3, 0), (0.2, 0)]


def test_brier_bss_and_log_score_known_answer():
    m = sb.prob_metrics(PAIRS)
    assert m["n"] == 4
    assert m["brier"] == pytest.approx((0.04 + 0.16 + 0.09 + 0.04) / 4, abs=1e-4)   # 0.0825
    assert m["brierRef"] == pytest.approx(0.25, abs=1e-4)           # base rate 0.5
    assert m["bss"] == pytest.approx(1 - 0.0825 / 0.25, abs=1e-4)   # 0.67
    expected_ll = -(math.log(0.8) + math.log(0.6) + math.log(0.7) + math.log(0.8)) / 4
    assert m["logScore"] == pytest.approx(expected_ll, abs=1e-4)


def test_constant_base_rate_forecast_has_zero_skill_and_degenerate_rows_have_none():
    m = sb.prob_metrics([(0.5, 1), (0.5, 0)])
    assert m["bss"] == pytest.approx(0.0, abs=1e-6)
    all_up = sb.prob_metrics([(0.6, 1), (0.7, 1)])                  # base rate 1 -> ref 0
    assert all_up["brierRef"] == 0.0 and all_up["bss"] is None
    empty = sb.prob_metrics([])
    assert empty["n"] == 0 and empty["brier"] is None and len(empty["reliability"]) == 5


def test_reliability_bins():
    rel = sb.prob_metrics(PAIRS + [(0.99, 1), (0.0, 0)])["reliability"]
    assert [(b["lo"], b["hi"]) for b in rel] == [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6),
                                                 (0.6, 0.8), (0.8, 1.0)]
    assert [b["n"] for b in rel] == [1, 2, 0, 1, 2]
    assert rel[1]["meanP"] == pytest.approx(0.25) and rel[1]["observed"] == 0.0
    assert rel[3]["meanP"] == pytest.approx(0.6) and rel[3]["observed"] == 1.0
    assert rel[4]["meanP"] == pytest.approx(0.895) and rel[4]["observed"] == 1.0   # 0.8 and 0.99
    assert rel[2]["meanP"] is None and rel[2]["observed"] is None


def test_spearman_with_ties_uses_average_ranks():
    # x ranks 1, 2.5, 2.5, 4 ; y ranks 1, 3, 2, 4 -> 4.5 / sqrt(4.5 * 5)
    assert sb.spearman([1, 2, 2, 3], [1, 3, 2, 4]) == pytest.approx(0.9487, abs=1e-4)
    assert sb.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert sb.spearman([1, 2, 3], [5, 5, 5]) is None               # constant side
    assert sb.spearman([1, 2], [1, 2]) is None                     # too few pairs


def prob_row(p, excess, score=None, exp=None, h="5"):
    return {"p20": p, "score": score, "expExcess20Pct": exp,
            "horizons": {h: {"excess": excess, "verdict": None}}}


def test_probabilistic_block_excludes_rows_without_p_and_ic_falls_back_to_expected_excess():
    rows = [prob_row(0.8, 2.0, score=70), prob_row(0.6, 1.0, score=60),
            prob_row(0.3, -1.0, score=50), prob_row(0.2, -2.0, score=40),
            prob_row(None, 3.0, score=80),                 # no p20: not in Brier ...
            prob_row(None, -5.0, exp=-9.0)]                # ... IC uses expExcess20Pct
    out = sb._probabilistic(rows, 5)
    assert out["n"] == 4 and out["n_without_p"] == 2
    assert out["brier"] == pytest.approx(0.0825, abs=1e-4)
    assert out["ic"] == {"spearman": 1.0, "n": 6}
    empty = sb._probabilistic(rows, 20)                    # nothing graded at T+20
    assert empty["n"] == 0 and empty["n_without_p"] == 0 and empty["ic"]["n"] == 0


def test_payload_probabilistic_block_per_horizon():
    rows = []
    for i, (p, v) in enumerate([(0.8, "hit"), (0.6, "hit"), (0.3, "miss"), (0.2, "miss"), (None, "hit")]):
        r = graded_row(i, v)
        r["p20"] = p
        rows.append(r)
    write_store(rows)
    out = payload()
    assert out["engine_version"] == "decision-signal-v3"
    assert set(out["probabilistic"]) == {"5", "20"}
    p5 = out["probabilistic"]["5"]
    assert set(p5) == {"n", "n_without_p", "brier", "brierRef", "bss", "logScore",
                       "reliability", "ic"}
    assert p5["n"] == 4 and p5["n_without_p"] == 1
    assert p5["brier"] == pytest.approx(0.0825, abs=1e-4) and p5["bss"] == pytest.approx(0.67, abs=1e-4)
    assert out["probabilistic"]["20"]["n"] == 0 and out["probabilistic"]["20"]["brier"] is None


# ------------------------------------------------------------------ baselines

def cal_closes(values, start=dt.date(2025, 1, 1)):
    return [((start + dt.timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def test_baseline_signals_on_synthetic_series():
    rising = cal_closes([100 + i for i in range(300)])
    assert sb.baseline_signals(rising, rising[299][0]) == {"trend_10m": "BUY", "momentum_12_1": "BUY"}
    falling = cal_closes([500 - i for i in range(300)])
    assert sb.baseline_signals(falling, falling[299][0]) == {"trend_10m": "SELL", "momentum_12_1": "SELL"}
    # rally then a sharp drop: close below its 210-session SMA (SELL) while
    # close[t-21] is still far above close[t-252] (momentum BUY)
    vals = [100 + i for i in range(270)] + [369 - 8 * (i - 269) for i in range(270, 300)]
    mixed = cal_closes(vals)
    assert sb.baseline_signals(mixed, mixed[299][0]) == {"trend_10m": "SELL", "momentum_12_1": "BUY"}


def test_baseline_signals_need_enough_history_and_a_bar_near_the_anchor():
    rising = cal_closes([100 + i for i in range(300)])
    assert sb.baseline_signals(rising, rising[208][0])["trend_10m"] is None
    assert sb.baseline_signals(rising, rising[209][0])["trend_10m"] == "BUY"
    assert sb.baseline_signals(rising, rising[251][0])["momentum_12_1"] is None
    assert sb.baseline_signals(rising, rising[252][0])["momentum_12_1"] == "BUY"
    assert sb.baseline_signals(rising, "2030-01-01") == {"trend_10m": None, "momentum_12_1": None}
    assert sb.baseline_signals([], "2025-06-01") == {"trend_10m": None, "momentum_12_1": None}


def test_baseline_cell_counts_hits_misses_brier_and_edge():
    rows = [{"sig": "BUY", "horizons": {"20": {"excess": 5.0, "band": 1.0}}},
            {"sig": "BUY", "horizons": {"20": {"excess": -5.0, "band": 1.0}}},
            {"sig": "SELL", "horizons": {"20": {"excess": 0.5, "band": 1.0}}},     # neutral
            {"sig": None, "horizons": {"20": {"excess": 9.0, "band": 1.0}}}]       # no signal: left out
    b = sb._baseline_cell(rows, lambda r: r["sig"], 20)
    assert (b["n"], b["hits"], b["misses"], b["neutral"]) == (3, 1, 1, 1)
    assert b["hitRatePct"] == 50.0 and len(b["wilson95"]) == 2
    assert b["brier"] == pytest.approx((0.45 ** 2 + 0.55 ** 2 + 0.55 ** 2) / 3, abs=1e-4)
    assert b["avgExcessPct"] == pytest.approx((5.0 - 5.0 - 0.5) / 3, abs=0.01)


def test_payload_baselines_on_the_same_anchor_rows(monkeypatch):
    closes = cal_closes([100 + i for i in range(520)])            # to 2026-05-25, rising

    async def daily(ticker, period="2y"):
        assert period == "2y"
        return closes
    monkeypatch.setattr(sb.prices, "daily_closes", daily, raising=False)
    rows = [graded_row(i, "hit", "hit") for i in range(3)]        # T+20: excess +3, band 1 -> buy hits
    rows.append(graded_row(3, "hit", None))                       # not graded at T+20: not a baseline row
    write_store(rows)
    bl = payload()["baselines"]
    assert set(bl) == {"always_hold", "trend_10m", "momentum_12_1"}
    for name in ("trend_10m", "momentum_12_1"):
        assert set(bl[name]) >= {"n", "hits", "misses", "hitRatePct", "wilson95", "brier", "avgExcessPct"}
        assert (bl[name]["n"], bl[name]["hits"], bl[name]["misses"]) == (3, 3, 0)
        assert bl[name]["hitRatePct"] == 100.0 and bl[name]["avgExcessPct"] == 3.0
        assert bl[name]["brier"] == pytest.approx(0.45 ** 2, abs=1e-4)   # BUY p .55, outperformed
    hold = bl["always_hold"]
    assert hold["n"] == 3 and hold["hits"] == hold["misses"] == 0 and hold["hitRatePct"] is None
    assert hold["brier"] == 0.25 and hold["avgExcessPct"] is None


def test_baselines_are_empty_without_daily_closes():
    write_store([graded_row(i, "hit", "hit") for i in range(3)])  # daily_closes -> []
    bl = payload()["baselines"]
    assert bl["trend_10m"]["n"] == 0 and bl["trend_10m"]["brier"] is None
    assert bl["always_hold"]["n"] == 3


# ------------------------------------------- hold rows and hit-rate scoping

def test_hit_rates_cover_directional_actions_only():
    buy = graded_row(0, "hit", "hit")
    hold = graded_row(1, "hit", "hit")
    hold["action"], hold["rating"], hold["graded_as"] = "hold", "HOLD", "probabilistic_only"
    for h in hold["horizons"].values():
        h["verdict"], h["edge"] = None, None
    write_store([buy, hold])
    out = payload()
    assert out["summary"]["BUY"]["byHorizon"]["5"]["hits"] == 1
    assert out["summary"]["HOLD"]["n"] == 0 and out["summary"]["HOLD"]["hitRatePct"] is None
    assert out["summary"]["HOLD"]["graded_as"] == "probabilistic_only"
    assert out["summary"]["BUY"]["graded_as"] == "directional"
    assert "hold" not in out["calibration"]["by_action"]
    assert out["summary"]["BASELINE"]["byHorizon"]["5"]["n"] == 1       # directional rows only
    assert out["status"]["graded_rows"] == {"5": 2, "20": 2}            # hold still graded (probabilistic)
    entry = [e for e in out["entries"] if e["action"] == "hold"][0]
    assert entry["verdict"] is None and entry["graded_as"] == "probabilistic_only"


# ------------------------------------------------------- v2 -> v3 regrade

def patch_pass(monkeypatch, bench_series=lambda d: 5000.0):
    async def load_bench(self):
        self.bench_id, self.bench = "GSPC", series("GSPC", bench_series)
        self.rates = {d.isoformat(): 0.9 for d in DAYS}

    async def load_series(self, symbol):
        self.series[symbol] = series(symbol, own_fn(110.0, 90.0))
        return self.series[symbol]
    monkeypatch.setattr(sb._Ctx, "load_benchmark", load_bench)
    monkeypatch.setattr(sb._Ctx, "load_series", load_series)
    monkeypatch.setattr(sb.dt, "datetime", type("D", (dt.datetime,), {
        "now": classmethod(lambda cls, tz=None: dt.datetime(2026, 8, 1, tzinfo=tz))}))


def v2_row(action="hold", rating="HOLD"):
    row = fresh(advice(action=action, rating=rating))
    row["engine_version"] = "decision-signal-v2"
    row["horizons"] = {"5": {"verdict": "hit", "excess": 0.1, "edge": None, "band": 0.5},
                       "20": {"verdict": "miss", "excess": 9.0, "edge": None, "band": 1.0}}
    row["eval_status"] = "completed"
    return row


def test_run_pass_regrades_v2_rows_under_v3_and_keeps_v2_results(monkeypatch):
    patch_pass(monkeypatch)
    old = v2_row()
    old_horizons = json.loads(json.dumps(old["horizons"]))
    write_store([old])
    res = asyncio.run(sb.run_pass())
    row = json.loads(sb.OUTCOMES.read_text())["rows"][0]
    assert res["graded"] == 2 and row["engine_version"] == "decision-signal-v3"
    assert row["v2"]["horizons"] == old_horizons                       # nothing lost
    assert row["v2"]["engine_version"] == "decision-signal-v2" and row["v2"]["eval_status"] == "completed"
    assert row["horizons"]["5"]["verdict"] is None and row["horizons"]["5"]["graded_as"] == "probabilistic_only"
    assert row["horizons"]["5"]["excess"] == pytest.approx(10.0, abs=0.01)      # fresh v3 numbers
    assert row["anchor_price"] == 100.0 and row["anchor_date"] == old["anchor_date"]
    # a second pass neither regrades nor overwrites the preserved v2 block
    asyncio.run(sb.run_pass())
    again = json.loads(sb.OUTCOMES.read_text())["rows"][0]
    assert again["v2"] == row["v2"] and again["horizons"] == row["horizons"]


def test_regrade_keeps_the_oldest_v2_block_and_copies_advice_fields(monkeypatch):
    patch_pass(monkeypatch)
    old = v2_row(action="buy", rating="BUY")
    write_store([old])
    sb.ADVICE_LOG.write_text(json.dumps([advice(p20=0.62, expExcess20Pct=1.5, draft=True)]))
    asyncio.run(sb.run_pass())
    row = json.loads(sb.OUTCOMES.read_text())["rows"][0]
    assert (row["p20"], row["expExcess20Pct"], row["draft"]) == (0.62, 1.5, True)
    assert row["v2"]["horizons"]["5"]["verdict"] == "hit"
    assert row["horizons"]["5"]["verdict"] == "hit" and row["horizons"]["20"]["verdict"] == "miss"
    assert row["graded_as"] == "directional"


def test_fresh_rows_record_engine_version_and_advice_fields(monkeypatch):
    row = fresh(advice(p20=0.62, expExcess20Pct=-0.7, draft=False))
    assert row["engine_version"] == "decision-signal-v3"
    assert (row["p20"], row["expExcess20Pct"], row["draft"]) == (0.62, -0.7, False)
    bad = fresh(advice(p20=1.5))                                      # not a probability
    assert bad["p20"] is None and bad["draft"] is False


def test_payload_ignores_rows_not_yet_regraded_and_reports_them():
    old = v2_row(action="buy", rating="BUY")
    write_store([old, graded_row(5, "hit", "hit")])
    out = payload()
    assert out["status"]["regrade_pending"] == 1
    assert out["summary"]["BUY"]["byHorizon"]["5"]["n"] == 1          # only the v3 row counts
    assert len(out["entries"]) == 2


# -------------------------------------------------- performance benchmark

SXR8 = {"id": "SXR8", "kind": "etf", "quoteCurrency": "EUR", "listing": {"currency": "EUR"}}


def no_fx(monkeypatch):
    async def boom(days):
        raise AssertionError("an EUR listing benchmark must not need FX")
    monkeypatch.setattr(sb.forex, "daily_rates", boom)


def test_eur_performance_benchmark_is_graded_without_any_fx_call(monkeypatch):
    monkeypatch.setattr(sb.config_store, "read", lambda: {"performanceBenchmark": "sxr8"})
    monkeypatch.setattr(sb.registry, "get", lambda ident: dict(SXR8) if ident.upper() == "SXR8" else None)
    no_fx(monkeypatch)

    async def load_series(self, symbol):
        fn = (lambda d: 200.0 if d <= ANCHOR else 220.0) if symbol == "SXR8" else own_fn(110.0, 110.0)
        self.series[symbol] = series(symbol, fn)
        return self.series[symbol]
    monkeypatch.setattr(sb._Ctx, "load_series", load_series)
    ctx = sb._Ctx()
    asyncio.run(ctx.load_benchmark())
    assert (ctx.bench_id, ctx.bench_kind, ctx.bench_fx, ctx.bench_reason) == ("SXR8", "total_return", False, "")
    assert ctx.rates == {}
    row = fresh(advice())
    grade(row, ctx)
    h5 = row["horizons"]["5"]
    assert h5["bench"] == pytest.approx(10.0, abs=0.01)               # 220/200 - 1, untouched by any rate
    assert h5["excess"] == pytest.approx(0.0, abs=0.01)


def test_payload_names_the_performance_benchmark_and_its_kind(monkeypatch):
    monkeypatch.setattr(sb.config_store, "read", lambda: {"performanceBenchmark": "SXR8"})
    monkeypatch.setattr(sb.registry, "get", lambda ident: dict(SXR8))
    out = payload()
    assert out["benchmark"] == "SXR8" and out["benchmark_kind"] == "total_return"
    assert "no FX" in out["benchmark_currency"]


def test_unset_performance_benchmark_falls_back_to_the_role_benchmark_in_eur(monkeypatch):
    spec = sb._benchmark_spec()
    assert spec == {"id": "GSPC", "kind": "price", "fx": True, "reason": ""}
    calls = []

    async def rates(days):
        calls.append(days)
        return {d.isoformat(): 0.9 for d in DAYS}

    async def load_series(self, symbol):
        self.series[symbol] = series(symbol, lambda d: 5000.0)
        return self.series[symbol]
    monkeypatch.setattr(sb.forex, "daily_rates", rates)
    monkeypatch.setattr(sb._Ctx, "load_series", load_series)
    ctx = sb._Ctx()
    asyncio.run(ctx.load_benchmark())
    assert calls and ctx.bench_fx is True and ctx.bench_kind == "price" and not ctx.bench_reason
    assert payload()["benchmark"] == "GSPC" and payload()["benchmark_kind"] == "price"


def test_unknown_or_unsupported_performance_benchmark_is_a_reason_not_a_silent_fallback(monkeypatch):
    monkeypatch.setattr(sb.config_store, "read", lambda: {"performanceBenchmark": "NOPE"})
    monkeypatch.setattr(sb.registry, "get", lambda ident: None)
    ctx = sb._Ctx()
    asyncio.run(ctx.load_benchmark())
    assert ctx.bench_reason == "unknown_performance_benchmark"
    monkeypatch.setattr(sb.registry, "get", lambda ident: {
        "id": "X", "kind": "etf", "quoteCurrency": "JPY", "listing": {"currency": "JPY"}})
    ctx = sb._Ctx()
    asyncio.run(ctx.load_benchmark())
    assert ctx.bench_reason == "unsupported_benchmark_currency"
