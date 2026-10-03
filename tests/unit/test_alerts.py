"""Alert center: items / unread / ack over the local notification store."""
import json

import pytest

from app.api import alerts, notify


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "NOTIF_STORE", str(tmp_path / "notif.json"))
    monkeypatch.setattr(alerts, "ACK_FILE", str(tmp_path / "ack.json"))

    def write(rows_by_ticker):
        with open(notify.NOTIF_STORE, "w") as fh:
            json.dump(rows_by_ticker, fh)

    write({
        "ASML": [{"id": "a2", "ts": 300.0, "title": "ASML gap", "body": "b",
                  "priority": 5, "source": "price", "ticker": "ASML",
                  "severity": "urgent", "url": "http://x"},
                 {"id": "a1", "ts": 100.0, "title": "old", "body": "",
                  "priority": 3, "source": "news"}],
        "NVDA": [{"id": "n1", "ts": 200.0, "title": "NVDA news", "body": "",
                  "priority": 1, "source": "news"}],
    })
    return write


def test_items_newest_first_with_contract_fields(store):
    snap = alerts.snapshot()
    assert [i["id"] for i in snap["items"]] == ["a2", "n1", "a1"]
    first = snap["items"][0]
    assert set(first) == {"id", "time", "title", "message", "priority",
                          "severity", "source", "ticker", "url", "acked"}
    assert first["severity"] == "urgent" and first["ticker"] == "ASML"
    # legacy rows (no id / ticker / severity) are normalised, not dropped
    legacy = snap["items"][2]
    assert legacy["ticker"] == "ASML" and legacy["severity"] == "warn"
    assert snap["items"][1]["severity"] == "info"
    assert snap["unread"] == 3


def test_limit_does_not_change_unread_count(store):
    snap = alerts.snapshot(limit=1)
    assert len(snap["items"]) == 1 and snap["unread"] == 3


def test_ack_ids_then_all_then_new_alert_is_unread(store):
    assert alerts.ack(["n1"]) == {"unread": 2}
    by_id = {i["id"]: i["acked"] for i in alerts.snapshot()["items"]}
    assert by_id == {"a2": False, "n1": True, "a1": False}

    assert alerts.ack(ack_all=True) == {"unread": 0}
    assert all(i["acked"] for i in alerts.snapshot()["items"])

    store({"ASML": [{"id": "a3", "ts": 400.0, "title": "new", "body": "",
                     "priority": 3, "source": "price"}]})
    assert alerts.snapshot()["unread"] == 1


def test_ack_persists_to_disk(store):
    alerts.ack(["a1", "a2"])
    saved = json.load(open(alerts.ACK_FILE))
    assert saved["ids"] == ["a1", "a2"] and saved["watermark"] == 0.0
    assert alerts.snapshot()["unread"] == 1


def test_ack_requires_ids_or_all(store):
    with pytest.raises(ValueError):
        alerts.ack([])
    with pytest.raises(ValueError):
        alerts.ack(None)


def test_empty_or_corrupt_store_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "NOTIF_STORE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(alerts, "ACK_FILE", str(tmp_path / "ack.json"))
    assert alerts.snapshot() == {"items": [], "unread": 0}
    (tmp_path / "nope.json").write_text("{broken")
    assert alerts.snapshot() == {"items": [], "unread": 0}
