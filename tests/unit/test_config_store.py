"""config_store durability: a bad read must never turn into a wipe.

Run inside the service image (see test_prices.py for the command).
"""
import json
import threading

import pytest

from app.api import config_store, jsonstore

GOOD = {"watchlist": [{"id": "ASML"}], "portfolio": {"ASML": {"shares": 1.0}},
        "alertRules": {"version": 2}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_dir / "config.json"))
    monkeypatch.setenv("HISTORY_DIR", str(tmp_path / "data"))
    for name, val in (("_cache", None), ("_lastgood", None),
                      ("_logged_sig", None)):
        monkeypatch.setattr(config_store, name, val)
    return cfg_dir / "config.json"


def _seed(path):
    path.write_text(json.dumps(GOOD))
    assert config_store.read() == GOOD     # parses it and records last-good


def test_corrupt_file_serves_last_good_and_keeps_data_on_update(store):
    _seed(store)
    store.write_text('{"watchlist": [')            # torn write
    assert config_store.read() == GOOD
    assert config_store.status()["source"] == "lastgood"

    def mut(cfg):
        cfg["chatModel"] = "lane"
    new = config_store.update(mut)
    assert new["portfolio"] == GOOD["portfolio"]    # nothing wiped
    assert new["alertRules"] == GOOD["alertRules"]
    assert json.loads(store.read_text()) == new     # file healed
    assert list(store.parent.glob("config.json.corrupt-*"))  # evidence kept


def test_last_good_survives_a_restart(store):
    _seed(store)
    config_store.update(lambda c: c.update(chatModel="lane"))
    store.write_text("")
    config_store._cache = None
    config_store._lastgood = None                  # fresh process
    assert config_store.read()["portfolio"] == GOOD["portfolio"]


def test_unreadable_without_last_good_refuses_to_write(store):
    store.write_text("{not json")
    before = store.read_bytes()
    assert config_store.status()["source"] == "default"
    assert config_store.status()["error"]
    with pytest.raises(config_store.ConfigUnreadable):
        config_store.update(lambda c: c.update(x=1))
    assert store.read_bytes() == before            # never overwritten


def test_failed_serialise_never_truncates(store):
    _seed(store)
    before = store.read_bytes()
    lastgood = config_store.lastgood_path().read_bytes()
    with pytest.raises(TypeError):
        config_store.update(lambda c: c.update(bad={1, 2}))   # a set
    with pytest.raises(ValueError):
        config_store.update(lambda c: c.update(bad=float("nan")))
    assert store.read_bytes() == before
    assert config_store.lastgood_path().read_bytes() == lastgood
    assert not list(store.parent.glob("*.tmp"))
    assert config_store.read() == GOOD


def test_mutator_error_writes_nothing(store):
    _seed(store)
    before = store.read_bytes()

    def boom(cfg):
        cfg["portfolio"] = {}
        raise RuntimeError("validation failed")
    with pytest.raises(RuntimeError):
        config_store.update(boom)
    assert store.read_bytes() == before
    assert config_store.read() == GOOD


def test_disk_failure_leaves_file_and_cache_intact(store, monkeypatch):
    _seed(store)
    monkeypatch.setattr(jsonstore, "save", lambda *a, **k: False)
    with pytest.raises(config_store.ConfigWriteError):
        config_store.update(lambda c: c.update(chatModel="x"))
    assert config_store.read() == GOOD


def test_update_is_serialised(store):
    store.write_text(json.dumps({"n": 0}))
    config_store.read()

    def bump(cfg):
        cfg["n"] = cfg["n"] + 1

    threads = [threading.Thread(target=lambda: [config_store.update(bump)
                                                for _ in range(10)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(store.read_text())["n"] == 80
    assert config_store.read()["n"] == 80


def test_read_returns_private_copy(store):
    _seed(store)
    cfg = config_store.read()
    cfg["portfolio"].clear()
    assert config_store.read() == GOOD


def test_ensure_never_touches_existing_file_and_restores_last_good(store):
    store.write_text("{corrupt")
    config_store.ensure()
    assert store.read_text() == "{corrupt"
    store.unlink()
    config_store._lastgood = dict(GOOD)
    config_store.ensure()
    assert json.loads(store.read_text()) == GOOD
