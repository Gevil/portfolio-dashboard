"""Risk view: exposures, look-through, volatility, beta, drawdown, correlation
(pure maths plus one payload test on a synthetic model; no network).

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime
import math
import statistics

import pytest

from app.api import risk

DIRECT = {"ASML": 49.4, "NVDA": 25.2, "SXR8": 25.4}


def day(i):
    return (datetime.date(2026, 8, 1) + datetime.timedelta(days=i)).isoformat()


# ----------------------------------------------------------- look-through

def test_look_through_adds_the_fund_share_to_the_direct_weight():
    out = risk.look_through(DIRECT, {"SXR8": {"weights": {"NVDA": 7.64}}})
    assert out["NVDA"] == pytest.approx(25.2 + 25.4 * 7.64 / 100.0)
    assert round(out["NVDA"], 2) == 27.14
    assert out["ASML"] == 49.4 and out["SXR8"] == 25.4   # rows overlap


def test_look_through_reaches_ids_that_are_not_held_directly():
    out = risk.look_through(DIRECT, {"SXR8": {"weights": {"AAPL": 6.5}}})
    assert out["AAPL"] == pytest.approx(25.4 * 0.065)


def test_look_through_sums_several_funds_and_ignores_unheld_ones():
    direct = {"A": 50.0, "F1": 30.0, "F2": 20.0}
    specs = {"F1": {"weights": {"A": 10.0}},
             "F2": {"weights": {"A": 5.0}},
             "F3": {"weights": {"A": 99.0}}}                 # not held
    assert risk.look_through(direct, specs)["A"] == pytest.approx(
        50.0 + 3.0 + 1.0)
    assert risk.look_through(direct, None) == direct


def test_weights_come_from_priced_rows_only():
    rows = [{"id": "A", "valueEur": 300.0}, {"id": "B", "valueEur": 100.0},
            {"id": "C", "valueEur": None}]
    assert risk.weights(rows) == {"A": 75.0, "B": 25.0}
    assert risk.weights([{"id": "C", "valueEur": None}]) == {}


def test_by_currency_groups_direct_weights():
    out = risk.by_currency(DIRECT, lambda pid: "EUR" if pid == "ASML" else "USD")
    assert out == {"EUR": 49.4, "USD": pytest.approx(50.6)}


def test_concentration():
    assert risk.concentration(DIRECT) == {"top2Pct": 74.8, "largestId": "ASML",
                                          "largestPct": 49.4}
    assert risk.concentration({}) == {"top2Pct": None, "largestId": None,
                                      "largestPct": None}
    assert risk.concentration({"A": 100.0})["top2Pct"] == 100.0


def test_notes_flag_missing_stale_and_overlapping_look_through(monkeypatch):
    monkeypatch.setattr(risk, "today", lambda: datetime.date(2027, 6, 1))
    specs = {"SXR8": {"asOf": "2026-10-14", "weights": {"NVDA": 7.64}}}
    notes = risk._notes(DIRECT, specs, {"SXR8": "etf", "NVDA": "equity"},
                        "lots", [])
    assert any("SXR8" in n and "230 days old" in n for n in notes)
    assert any("overlap" in n for n in notes)
    notes = risk._notes(DIRECT, {}, {"SXR8": "etf"}, "current_shares",
                        ["NVDA"])
    assert any("No look-through weights for SXR8" in n for n in notes)
    assert any("no dated lots for: NVDA" in n for n in notes)
    assert not any("overlap" in n for n in notes)


# ------------------------------------------------------------- statistics

def test_log_returns_skip_missing_closes():
    axis = [day(0), day(1), day(2), day(3)]
    out = risk.log_returns({day(0): 100.0, day(1): 110.0, day(3): 99.0}, axis)
    assert out == {day(1): pytest.approx(math.log(1.1))}


def test_ann_vol_needs_enough_observations():
    assert risk.ann_vol_pct([0.01] * (risk.MIN_OBS - 1)) is None
    rets = [0.01, -0.01] * 15
    assert risk.ann_vol_pct(rets) == pytest.approx(
        statistics.stdev(rets) * math.sqrt(252) * 100.0)


def test_beta_is_cov_over_variance_and_none_for_a_constant_benchmark():
    bench = [0.01, -0.02, 0.015, -0.005] * 8
    assert risk.beta([2 * x for x in bench], bench) == pytest.approx(2.0)
    assert risk.beta(bench, [0.0] * len(bench)) is None
    assert risk.beta(bench[:5], bench[:5]) is None


def test_drawdown_from_peak_and_maximum_on_the_twr_index():
    twr = [(day(0), 0.0), (day(1), 0.10), (day(2), 0.21), (day(3), -0.032),
           (day(4), 0.089)]
    assert risk.drawdown(twr) == {"fromPeakPct": -10.0, "peakDate": day(2),
                                  "maxDrawdownPct": -20.0}
    up = [(day(i), i * 0.01) for i in range(4)]
    assert risk.drawdown(up) == {"fromPeakPct": 0.0, "peakDate": day(3),
                                 "maxDrawdownPct": 0.0}
    assert risk.drawdown([(day(0), 0.0)])["fromPeakPct"] is None


def _returns(n, phase):
    return {day(i): math.sin(i * 0.7 + phase) * 0.01 + 0.0003 * i
            for i in range(n)}


def test_correlation_matrix_is_symmetric_with_unit_diagonal():
    rets = {"A": _returns(40, 0.0), "B": _returns(40, 1.1),
            "C": _returns(40, 2.9)}
    out = risk.correlation(rets)
    m = out["matrix"]
    assert out["ids"] == ["A", "B", "C"]
    assert all(m[i][j] == m[j][i] for i in range(3) for j in range(3))
    assert all(m[i][i] == 1.0 for i in range(3))
    assert all(-1.0 <= x <= 1.0 for row in m for x in row)
    days = sorted(rets["A"])
    assert m[0][1] == pytest.approx(statistics.correlation(
        [rets["A"][d] for d in days], [rets["B"][d] for d in days]), abs=1e-4)


def test_correlation_is_none_without_enough_common_days_or_variance():
    short = {day(i): 0.01 * i for i in range(5)}
    flat = {day(i): 0.0 for i in range(40)}
    out = risk.correlation({"A": _returns(40, 0.0), "S": short, "F": flat})
    m = out["matrix"]
    assert m[0][1] is None and m[1][0] is None             # 5 common returns
    assert m[2][2] is None and m[0][2] is None             # constant series
    assert m[1][1] is None                                 # too short


# ----------------------------------------------------------------- payload

def _model():
    """31 calendar days; the book alternates +1 % / -1 %, the benchmark moves
    half as much in log terms (beta 2), NVDA is the exact inverse of ASML."""
    axis = [day(i) for i in range(31)]
    values, v = {axis[0]: 100.0}, 100.0
    for i in range(1, 31):
        v *= 1.01 if i % 2 else 0.99
        values[axis[i]] = v
    return {
        "axis": axis, "value": values, "flows": {},
        "perf": {"id": "SXR8",
                 "days": {d: 100.0 * math.sqrt(x / 100.0)
                          for d, x in values.items()}},
        "included": ["ASML", "NVDA", "SXR8"],
        "series": {"ASML": dict(values),
                   "NVDA": {d: 1e4 / x for d, x in values.items()},
                   "SXR8": {d: x * x / 100.0 for d, x in values.items()}},
        "twr": [(d, x / 100.0 - 1.0) for d, x in values.items()],
        "basis": "lots", "noLots": []}


@pytest.fixture
def env(monkeypatch):
    model = _model()

    async def snapshot():
        return {"positions": [{"id": "ASML", "valueEur": 494.0},
                              {"id": "NVDA", "valueEur": 252.0},
                              {"id": "SXR8", "valueEur": 254.0}]}

    async def get_model(rng):
        assert rng == "1Y"
        return model

    cfg = {"exposureCurrency": {"NVDA": "usd", "SXR8": "USD"},
           "lookThrough": {"SXR8": {"asOf": "2026-10-14",
                                    "weights": {"NVDA": 7.64}}}}
    entries = [{"id": i, "kind": k, "listing": {"currency": "EUR"}}
               for i, k in (("ASML", "equity"), ("NVDA", "equity"),
                            ("SXR8", "etf"))]
    monkeypatch.setattr(risk.portfolio, "snapshot", snapshot)
    monkeypatch.setattr(risk.portfolio, "model", get_model)
    monkeypatch.setattr(risk.config_store, "read", lambda: cfg)
    monkeypatch.setattr(risk.registry, "entries", lambda: list(entries))
    monkeypatch.setattr(risk, "today", lambda: datetime.date(2026, 10, 20))
    return model


def test_risk_payload(env):
    out = asyncio.run(risk.risk())
    assert set(out) == {"asOf", "exposures", "volatility", "beta", "drawdown",
                        "correlation", "concentration"}
    assert out["asOf"] == "2026-08-31"
    ex = out["exposures"]
    assert ex["direct"] == {"ASML": 49.4, "SXR8": 25.4, "NVDA": 25.2}
    assert ex["lookThrough"]["NVDA"] == 27.14
    assert ex["byCurrency"] == {"EUR": 49.4, "USD": 50.6}
    assert any("overlap" in n for n in ex["notes"])
    logs = [math.log(1.01 if i % 2 else 0.99) for i in range(1, 31)]
    vol = statistics.stdev(logs) * math.sqrt(252) * 100.0
    assert out["volatility"]["window"] == "1Y"
    assert out["volatility"]["portfolioAnnPct"] == pytest.approx(vol, abs=0.01)
    assert out["volatility"]["perfBenchmarkAnnPct"] == pytest.approx(
        vol / 2.0, abs=0.01)
    assert out["beta"] == pytest.approx(2.0, abs=1e-3)
    peak = 101.0                                            # the first +1 %
    last = env["value"][env["axis"][-1]]
    assert out["drawdown"]["peakDate"] == env["axis"][1]
    assert out["drawdown"]["fromPeakPct"] == pytest.approx(
        (last / peak - 1.0) * 100.0, abs=0.01)
    assert out["drawdown"]["maxDrawdownPct"] == out["drawdown"]["fromPeakPct"]
    corr = out["correlation"]
    assert corr["ids"] == ["ASML", "NVDA", "SXR8"]
    assert corr["matrix"] == [[1.0, -1.0, 1.0], [-1.0, 1.0, -1.0],
                              [1.0, -1.0, 1.0]]
    assert out["concentration"] == {"top2Pct": 74.8, "largestId": "ASML",
                                    "largestPct": 49.4}
