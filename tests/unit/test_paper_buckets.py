"""Paper-trading buckets: ids, validation, merged config and per-bucket paths
(phase 5 contract §1, §2, §5).

Run inside the service image (see test_prices.py for the command).
"""
import pathlib

import pytest

from app.api import paper_buckets as pb


@pytest.fixture
def conf(monkeypatch):
    holder = {"paperTrading": {}}
    monkeypatch.setattr(pb.config_store, "read", lambda: holder)
    return holder["paperTrading"], holder


def test_ids_are_daily_then_intraday():
    assert pb.ids() == ["daily", "intraday"]
    pb.ids().append("x")                       # a copy: callers cannot corrupt the registry
    assert pb.ids() == ["daily", "intraday"]


@pytest.mark.parametrize("raw,expected", [(None, "daily"), ("", "daily"), ("daily", "daily"),
                                          ("intraday", "intraday"), (" Intraday ", "intraday"),
                                          ("DAILY", "daily")])
def test_validate_normalises_and_defaults_to_daily(raw, expected):
    assert pb.validate(raw) == expected


@pytest.mark.parametrize("raw", ["weekly", "daily,intraday", 3, ["daily"], {"a": 1}, True])
def test_validate_rejects_anything_else(raw):
    with pytest.raises(pb.BadBucket) as e:
        pb.validate(raw)
    assert isinstance(e.value, ValueError) and "daily, intraday" in str(e.value)


def test_paths_keep_the_daily_names_and_suffix_the_others(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "DATA_DIR", tmp_path)
    for name in ("season", "snapshots", "decisions", "halt", "summary", "reports", "briefs"):
        assert pb.path("daily", name) == tmp_path / f"paper_{name}.json"
        assert pb.path("intraday", name) == tmp_path / f"paper_{name}.intraday.json"
    assert pb.path(None, "season") == tmp_path / "paper_season.json"
    with pytest.raises(pb.BadBucket):
        pb.path("weekly", "season")


def test_the_order_ledger_is_one_shared_file(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "DATA_DIR", tmp_path)
    assert pb.path("daily", "orders") == pb.path("intraday", "orders") == tmp_path / "paper_orders.json"


def test_suffixed_follows_any_daily_path(tmp_path):
    p = tmp_path / "x" / "season_file.json"
    assert pb.suffixed(p, "daily") == p
    assert pb.suffixed(p, "intraday") == tmp_path / "x" / "season_file.intraday.json"


def test_the_daily_bucket_is_the_top_level_paper_trading_keys(conf):
    block, _ = conf
    block.update(mode="shadow", strategyVersion="s7", budgetEur=750,
                 universe={"ASML": {"t212": "ASMLa_EQ"}}, limits={"minTradeEur": 40},
                 costModel={"spreadBps": 4})
    c = pb.cfg("daily")
    assert (c["bucket"], c["enabled"], c["mode"], c["strategyVersion"], c["budgetEur"]) == (
        "daily", True, "shadow", "s7", 750.0)
    assert c["universe"] == {"ASML": {"t212": "ASMLa_EQ"}} and c["limits"] == {"minTradeEur": 40}
    assert c["costModel"] == {"spreadBps": 4.0, "commissionEur": 0.0, "fxBps": 0.0}
    assert pb.cfg() == c


def test_the_daily_defaults_without_a_block(conf):
    c = pb.cfg("daily")
    assert (c["mode"], c["strategyVersion"], c["budgetEur"], c["universe"], c["limits"]) == (
        "off", "s1", 1000.0, {}, {})
    assert c["costModel"] == {"spreadBps": 10.0, "commissionEur": 0.0, "fxBps": 0.0}


def test_an_absent_intraday_block_is_disabled_and_off(conf):
    block, _ = conf
    block.update(mode="demo", universe={"ASML": {"t212": "ASMLa_EQ"}})
    c = pb.cfg("intraday")
    assert c["bucket"] == "intraday" and c["enabled"] is False and c["mode"] == "off"
    assert c["strategyVersion"] == "i1" and c["budgetEur"] == 1000.0
    assert c["intervalMin"] == 60 and c["flatAtClose"] is False
    assert c["universe"] == {"ASML": {"t212": "ASMLa_EQ"}}               # the universe is shared


def test_an_enabled_intraday_block_is_merged(conf):
    block, _ = conf
    block.update(mode="demo", strategyVersion="s1", budgetEur=1000, universe={"ASML": {"t212": "A"}},
                 limits={"maxOrdersPerDay": 5, "minCashPct": 7, "rebalanceBandPct": 3,
                         "maxWeightPct": {"equity": 40, "etf": 90},
                         "killSwitch": {"drawdownPct": 12, "dailyLossPct": 4}})
    block["buckets"] = {"intraday": {"enabled": True, "mode": "shadow", "strategyVersion": "i2",
                                     "budgetEur": 500, "intervalMin": 30, "flatAtClose": True,
                                     "limits": {"minTradeEur": 10,
                                                "killSwitch": {"dailyLossPct": 2}}}}
    c = pb.cfg("intraday")
    assert (c["enabled"], c["mode"], c["strategyVersion"], c["budgetEur"]) == (True, "shadow", "i2", 500.0)
    assert (c["intervalMin"], c["flatAtClose"]) == (30, True)
    lim = c["limits"]
    # daily limits, then the intraday defaults (12 orders, 100 % turnover, 2 % band, EUR 25), then overrides
    assert lim["maxOrdersPerDay"] == 12 and lim["maxTurnoverPctPerDay"] == 100
    assert lim["rebalanceBandPct"] == 2 and lim["minTradeEur"] == 10
    assert lim["minCashPct"] == 7 and lim["maxWeightPct"] == {"equity": 40, "etf": 90}
    assert lim["killSwitch"] == {"drawdownPct": 12, "dailyLossPct": 2}   # same as daily, key by key
    assert pb.cfg("daily")["limits"]["maxOrdersPerDay"] == 5              # the daily bucket is unchanged


def test_the_default_intraday_limits_apply_without_an_override(conf):
    block, _ = conf
    block["buckets"] = {"intraday": {"enabled": True, "mode": "demo"}}
    assert pb.cfg("intraday")["limits"] == {"maxOrdersPerDay": 12, "maxTurnoverPctPerDay": 100,
                                            "rebalanceBandPct": 2, "minTradeEur": 25}


def test_a_disabled_intraday_bucket_can_never_be_in_a_trading_mode(conf):
    block, _ = conf
    block["buckets"] = {"intraday": {"enabled": False, "mode": "demo"}}
    c = pb.cfg("intraday")
    assert c["enabled"] is False and c["mode"] == "off" and c["configuredMode"] == "demo"
    block["buckets"] = {"intraday": {"mode": "demo"}}                       # no 'enabled' key at all
    assert pb.cfg("intraday")["mode"] == "off"
    block["buckets"] = {"intraday": {"enabled": "yes", "mode": "demo"}}     # only a real true enables
    assert pb.cfg("intraday")["enabled"] is False


@pytest.mark.parametrize("raw,expected", [(15, 15), (120, 120), (45, 60), ("x", 60), (None, 60)])
def test_an_unusable_interval_falls_back_to_60(conf, raw, expected):
    block, _ = conf
    block["buckets"] = {"intraday": {"enabled": True, "intervalMin": raw}}
    assert pb.cfg("intraday")["intervalMin"] == expected


def test_garbage_config_never_raises(conf):
    block, holder = conf
    holder["paperTrading"] = "nope"
    assert pb.cfg("daily")["mode"] == "off" and pb.cfg("intraday")["enabled"] is False
    holder["paperTrading"] = {"buckets": "x", "limits": [1]}
    assert pb.cfg("intraday")["enabled"] is False and pb.cfg("daily")["limits"] == {}


def test_cfg_validates_the_bucket(conf):
    with pytest.raises(pb.BadBucket):
        pb.cfg("weekly")
    assert isinstance(pathlib.Path(pb.DATA_DIR), pathlib.Path)


# ---- universeSource (phase 6) ----------------------------------------------------

ROWS = [
    {"id": "SAP", "yahoo": "SAP.DE", "venue": "XETR", "kind": "equity", "sector": "Tech",
     "name": "SAP", "t212": "SAPd_EQ"},
    {"id": "ASML", "yahoo": "ASML.AS", "venue": "XAMS", "kind": "equity", "sector": "Tech",
     "name": "ASML", "t212": None},                                      # unmapped: not investable
    {"id": "VOW", "yahoo": "VOW3.DE", "venue": "XETR", "kind": "equity", "sector": "Autos",
     "name": "VW", "t212": "VOW3d_EQ"},
    {"id": "EUNL", "yahoo": "EUNL.DE", "venue": "XETR", "kind": "etf", "role": "benchmark",
     "name": "World", "t212": "EUNLd_EQ"},                              # benchmark: not investable
]


@pytest.fixture
def open_rows(monkeypatch, tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(__import__("json").dumps({"entries": ROWS}))
    monkeypatch.setattr(pb.paper_universe, "STORE_FILE", tmp_path / "store.json")
    monkeypatch.setattr(pb.paper_universe, "SEED_FILE", seed)
    monkeypatch.setattr(pb.paper_universe, "_load_cache", {"sig": None, "doc": None})


def test_the_default_universe_source_is_the_watchlist(conf):
    block, _ = conf
    block["universe"] = {"ASML": {"t212": "ASMLa_EQ"}}
    assert pb.universe_source() == "watchlist" and pb.cfg()["universeSource"] == "watchlist"
    block["universeSource"] = "bogus"
    assert pb.cfg()["universeSource"] == "watchlist"


def test_paper_universe_builds_the_universe_from_mapped_candidates(conf, open_rows):
    block, _ = conf
    block.update(universeSource="paper_universe", universe={"ASML": {"t212": "x"}})
    c = pb.cfg()
    assert c["universeSource"] == "paper_universe"
    assert c["universe"] == {"SAP": {"t212": "SAPd_EQ", "kind": "equity", "sector": "Tech", "name": "SAP"},
                             "VOW": {"t212": "VOW3d_EQ", "kind": "equity", "sector": "Autos", "name": "VW"}}


def test_open_universe_defaults_sit_under_the_configured_limits(conf, open_rows):
    block, _ = conf
    block.update(universeSource="paper_universe", limits={"minTradeEur": 40, "maxPositions": 6,
                                                          "maxWeightPct": {"equity": 25}})
    lim = pb.cfg()["limits"]
    assert lim["maxPositions"] == 6                                # configured key wins
    assert lim["maxSectorPct"] == 40 and lim["minPositionEur"] == 50          # defaults fill the rest
    assert lim["maxWeightPct"] == {"equity": 25, "etf": 40}       # sub-object merges key by key
    assert lim["minTradeEur"] == 40
    block["universeSource"] = "watchlist"
    assert pb.cfg()["limits"] == {"minTradeEur": 40, "maxPositions": 6, "maxWeightPct": {"equity": 25}}


def test_open_versions_default_to_s2_and_i2_and_intraday_inherits_the_limits(conf, open_rows):
    block, _ = conf
    block.update(universeSource="paper_universe")
    block["buckets"] = {"intraday": {"enabled": True, "mode": "shadow"}}
    assert pb.cfg("daily")["strategyVersion"] == "s2" and pb.cfg("intraday")["strategyVersion"] == "i2"
    lim = pb.cfg("intraday")["limits"]
    assert lim["maxPositions"] == 10 and lim["maxSectorPct"] == 40 and lim["maxOrdersPerDay"] == 12
    assert set(pb.cfg("intraday")["universe"]) == {"SAP", "VOW"}
    block["strategyVersion"] = "s9"
    assert pb.cfg("daily")["strategyVersion"] == "s9"             # an explicit version always wins
