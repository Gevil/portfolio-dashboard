"""notify: Delivery statuses, durable outbox retry, notification store."""
import asyncio
import json

import pytest

from app.api import jsonstore, notify


@pytest.fixture
def ntfy(tmp_path, monkeypatch):
    """Isolated state files + a scripted ntfy endpoint (notify._send)."""
    monkeypatch.setattr(notify, "OUTBOX_FILE", str(tmp_path / "outbox.json"))
    monkeypatch.setattr(notify, "NOTIF_STORE", str(tmp_path / "notif.json"))
    monkeypatch.setattr(notify, "_down_until", 0.0)
    monkeypatch.delenv("NOTIFY_MIN_SEVERITY", raising=False)
    notify._dedupe.clear()

    class Ntfy:
        up = True
        posts: list = []

        async def send(self, headers, body):
            self.posts.append((headers, body))
            if not self.up:
                notify._down_until = 0.0     # a real failure holds 15 s
            return self.up

    n = Ntfy()
    n.posts = []
    monkeypatch.setattr(notify, "_send", n.send)
    return n


def run(coro):
    return asyncio.run(coro)


def test_sent_when_ntfy_accepts(ntfy):
    d = run(notify.push("t", "b", priority=4))
    assert d.status == notify.SENT and d and d.consumed
    assert ntfy.posts[0][0]["Priority"] == "4"
    assert notify.outbox_size() == 0


def test_queued_when_ntfy_down_then_drained(ntfy):
    ntfy.up = False
    d = run(notify.push("t", "b"))
    assert d.status == notify.QUEUED and d and d.consumed
    assert notify.outbox_size() == 1

    ntfy.up = True
    res = run(notify.drain_once(force=True))
    assert res["sent"] == 1 and res["pending"] == 0
    assert notify.outbox_size() == 0


def test_failed_drain_backs_off_and_keeps_entry(ntfy):
    ntfy.up = False
    run(notify.push("t", "b"))
    before = jsonstore.load(notify.OUTBOX_FILE, [])[0]
    assert before["attempts"] == 0

    run(notify.drain_once(force=True))
    after = jsonstore.load(notify.OUTBOX_FILE, [])[0]
    assert after["attempts"] == 1
    assert after["next_try"] > before["next_try"]
    # not due yet: a normal (non-forced) drain does not even try
    sent_before = len(ntfy.posts)
    run(notify.drain_once())
    assert len(ntfy.posts) == sent_before


def test_expired_outbox_entry_is_dropped(ntfy, monkeypatch):
    ntfy.up = False
    run(notify.push("t", "b"))
    monkeypatch.setattr(notify, "OUTBOX_MAX_AGE_S", -1)
    res = run(notify.drain_once(force=True))
    assert res["dropped"] == 1 and notify.outbox_size() == 0


def test_failed_only_when_outbox_write_fails(ntfy, monkeypatch):
    ntfy.up = False
    monkeypatch.setattr(notify.jsonstore, "save", lambda *a, **k: False)
    d = run(notify.push("t", "b"))
    assert d.status == notify.FAILED
    assert not d and not d.consumed


def test_filtered_below_min_severity_is_consumed_not_accepted(ntfy):
    d = run(notify.push("t", "b", severity="info"))
    assert d.status == notify.FILTERED
    assert not d and d.consumed
    assert ntfy.posts == []


def test_dedupe_key_suppresses_repeat(ntfy):
    assert run(notify.push("t", "b", dedupe_key="k")).status == notify.SENT
    second = run(notify.push("t", "b", dedupe_key="k"))
    assert second.status == notify.DUPLICATE and second.consumed
    assert len(ntfy.posts) == 1


def test_non_ascii_title_never_raises(ntfy):
    d = run(notify.push("ASML \u2192 \U0001F4C8", "b"))
    assert d.status == notify.SENT
    assert ntfy.posts[0][0]["Title"].isascii()


def test_store_rows_carry_alert_fields(ntfy):
    row_id = notify.store("ASML", "price", "title", "body", priority=5,
                          url="http://x")
    rows = json.loads(open(notify.NOTIF_STORE).read())["ASML"]
    assert rows[0]["id"] == row_id
    assert rows[0]["severity"] == "urgent"
    assert rows[0]["ticker"] == "ASML" and rows[0]["source"] == "price"
    assert rows[0]["url"] == "http://x"


def test_alert_stores_only_accepted(ntfy):
    run(notify.alert("A", "price", "t", "b", priority=3))
    run(notify.alert("B", "price", "t", "b", priority=1, severity="info"))
    store = jsonstore.load(notify.NOTIF_STORE, {})
    assert "A" in store and "B" not in store
