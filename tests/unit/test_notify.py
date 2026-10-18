"""notify: Delivery statuses, durable outbox retry, notification store, and the
push policy (quiet hours, per-source caps, ops routing, click deep links)."""
import asyncio
import datetime
import json

import pytest

from app.api import jsonstore, notify

_real_send = notify._send
ACTION = [{"action": "http", "label": "Approve", "url": "http://x/a"}]


def ts(day, hour, minute=0):
    """Container-local wall clock -> epoch (the policy works in local time)."""
    return datetime.datetime(2026, 10, day, hour, minute).timestamp()


@pytest.fixture
def ntfy(tmp_path, monkeypatch):
    """Isolated state files, a frozen clock and a scripted ntfy endpoint
    (notify._send). Quiet hours are off until a test opts in."""
    monkeypatch.setattr(notify, "OUTBOX_FILE", str(tmp_path / "outbox.json"))
    monkeypatch.setattr(notify, "NOTIF_STORE", str(tmp_path / "notif.json"))
    monkeypatch.setattr(notify, "CAPS_FILE", str(tmp_path / "caps.json"))
    monkeypatch.setattr(notify, "NTFY_TOPIC", "stocks")
    monkeypatch.setattr(notify, "_down_until", 0.0)
    for var in ("NOTIFY_MIN_SEVERITY", "NOTIFY_SOURCE_CAPS", "NTFY_TOPIC_OPS",
                "NTFY_CLICK_URL", "DASHBOARD_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "")
    notify._dedupe.clear()

    class Ntfy:
        def __init__(self):
            self.up = True
            self.posts = []
            self.topics = []
            self.now = ts(14, 12)

        def at(self, hour, minute=0, day=14):
            self.now = ts(day, hour, minute)

        async def send(self, headers, body, topic=None):
            self.posts.append((headers, body))
            self.topics.append(topic)
            if not self.up:
                notify._down_until = 0.0     # a real failure holds 15 s
            return self.up

    n = Ntfy()
    monkeypatch.setattr(notify, "_send", n.send)
    monkeypatch.setattr(notify, "_now", lambda: n.now)
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


def test_store_pins_a_caller_allocated_row_id(ntfy):
    assert notify.store("ASML", "price", "t", row_id="abc123") == "abc123"
    rows = jsonstore.load(notify.NOTIF_STORE, {})["ASML"]
    assert [r["id"] for r in rows] == ["abc123"]


def test_alert_stores_only_accepted(ntfy):
    run(notify.alert("A", "price", "t", "b", priority=3))
    run(notify.alert("B", "price", "t", "b", priority=1, severity="info"))
    store = jsonstore.load(notify.NOTIF_STORE, {})
    assert "A" in store and "B" not in store


# ------------------------------------------------------------- quiet hours

@pytest.mark.parametrize("hour,minute,day,until", [
    (23, 30, 14, (15, 7)),      # evening side of the window
    (22, 0, 14, (15, 7)),       # first held minute
    (3, 0, 15, (15, 7)),        # after midnight
    (6, 59, 15, (15, 7)),       # last held minute
])
def test_quiet_hours_queue_with_not_before_then_drain_releases(
        ntfy, monkeypatch, hour, minute, day, until):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22-7")
    ntfy.at(hour, minute, day)
    d = run(notify.push("t", "b", priority=4, source="news"))
    assert d.status == notify.QUEUED and d.reason == "quiet hours"
    assert d and d.consumed
    assert ntfy.posts == []
    [row] = jsonstore.load(notify.OUTBOX_FILE, [])
    assert row["not_before"] == ts(until[0], until[1])

    # still inside the window: not even a forced drain sends it
    run(notify.drain_once(force=True))
    assert ntfy.posts == [] and notify.outbox_size() == 1

    ntfy.at(7, 1, 15)
    res = run(notify.drain_once())
    assert res["sent"] == 1 and res["pending"] == 0
    assert len(ntfy.posts) == 1


@pytest.mark.parametrize("hour,minute", [(7, 0), (12, 0), (21, 59)])
def test_outside_quiet_hours_sends_immediately(ntfy, monkeypatch, hour,
                                               minute):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22-7")
    ntfy.at(hour, minute)
    assert run(notify.push("t", "b", source="news")).status == notify.SENT


def test_quiet_hours_bypass_for_hot_price_policy_and_approvals(
        ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22-7")
    ntfy.at(23, 0)
    assert run(notify.push("t", "b", priority=5,
                           source="price")).status == notify.SENT
    assert run(notify.push("t", "b", priority=5,
                           source="policy")).status == notify.SENT
    assert run(notify.push("t", "b", priority=3, source="triage",
                           actions=ACTION)).status == notify.SENT
    assert len(ntfy.posts) == 3
    # no bypass: priority 4 price, priority 5 from another source
    assert run(notify.push("t", "b", priority=4,
                           source="price")).reason == "quiet hours"
    assert run(notify.push("t", "b", priority=5,
                           source="news")).reason == "quiet hours"
    assert run(notify.push("t", "b", priority=5)).reason == "quiet hours"
    assert len(ntfy.posts) == 3 and notify.outbox_size() == 3


def test_quiet_hours_failed_outbox_write_is_failed_and_keeps_state(
        ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22-7")
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "news=1")
    ntfy.at(23, 0)
    monkeypatch.setattr(notify, "_enqueue", lambda *a, **k: False)
    d = run(notify.push("t", "b", source="news", dedupe_key="k"))
    assert d.status == notify.FAILED and not d.consumed
    assert notify.policy_status()["counts_24h"] == {}
    assert "k" not in notify._dedupe


def test_quiet_window_parsing(monkeypatch):
    monkeypatch.delenv("NOTIFY_QUIET_HOURS", raising=False)
    assert notify.quiet_window() == (22, 7)
    for raw, want in (("23-6", (23, 6)), ("9 - 17", (9, 17)), ("", None),
                      ("off", None), ("5-5", None), ("nonsense", (22, 7)),
                      ("25-3", (22, 7))):
        monkeypatch.setenv("NOTIFY_QUIET_HOURS", raw)
        assert notify.quiet_window() == want, raw


def test_non_wrapping_window_ends_same_day(ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "9-17")
    assert notify.quiet_until(ts(14, 10)) == ts(14, 17)
    assert notify.quiet_until(ts(14, 8)) is None
    assert notify.quiet_until(ts(14, 17)) is None


# ---------------------------------------------------------------- caps

def test_source_cap_stops_pushes_but_alert_still_stores_the_row(
        ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "news=2")
    for i in range(2):
        d = run(notify.alert("A", "news", f"t{i}", "b", priority=3))
        assert d.status == notify.SENT
    d = run(notify.alert("A", "news", "t2", "b", priority=3))
    assert d.status == notify.FILTERED and d.reason == "cap"
    assert d.capped and d.consumed and not d
    assert len(ntfy.posts) == 2
    titles = [r["title"] for r in jsonstore.load(notify.NOTIF_STORE, {})["A"]]
    assert sorted(titles) == ["t0", "t1", "t2"]
    # another source is not affected by news' cap
    assert run(notify.push("t", "b", source="insider")).status == notify.SENT


def test_cap_is_a_rolling_24h_window_and_survives_restart(ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "market_light=1")
    assert run(notify.push("t", "b", source="market_light")).status == \
        notify.SENT
    ntfy.at(12, 0, 15)                 # 24 h later: the first push expired
    assert run(notify.push("t", "b", source="market_light")).status == \
        notify.SENT
    assert run(notify.push("t", "b", source="market_light")).capped
    # counts live in the caps file, not in process memory
    assert jsonstore.load(notify.CAPS_FILE, {})["market_light"] == [ntfy.now]


def test_approval_pushes_are_never_capped(ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "triage=1")
    assert run(notify.push("t", "b", source="triage")).status == notify.SENT
    assert run(notify.push("t", "b", source="triage")).capped
    for _ in range(3):
        d = run(notify.push("t", "b", source="triage", actions=ACTION))
        assert d.status == notify.SENT
    assert "X-Actions" in ntfy.posts[-1][0]


def test_failed_push_does_not_use_up_the_cap(ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "news=1")
    ntfy.up = False
    monkeypatch.setattr(notify, "_enqueue", lambda *a, **k: False)
    assert run(notify.push("t", "b", source="news")).status == notify.FAILED
    assert notify.policy_status()["counts_24h"] == {}
    ntfy.up = True
    assert run(notify.push("t", "b", source="news")).status == notify.SENT


def test_caps_parsing_keeps_defaults_and_ignores_bad_entries(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOURCE_CAPS", raising=False)
    caps = notify.source_caps()
    assert caps["ops_watch"] == 3 and caps["ops"] == 3
    assert caps["triage"] == 6 and caps["news"] == 6
    assert caps["analysis"] == 4 and caps["price"] == 8
    assert caps["insider"] == 6 and caps["policy"] == 3
    assert caps["market_light"] == 2
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "a=1, bad, b=x ,c=5")
    assert notify.source_caps() == {"a": 1, "c": 5}
    monkeypatch.setenv("NOTIFY_SOURCE_CAPS", "")
    assert notify.source_caps() == {}


# -------------------------------------------------------------- ops routing

def test_ops_sources_go_to_the_ops_topic_with_priority_capped_at_3(ntfy):
    for source in ("ops_watch", "ops"):
        run(notify.push("t", "b", priority=5, source=source))
    run(notify.push("t", "b", priority=5, source="price"))
    assert ntfy.topics == ["stocks-ops", "stocks-ops", None]
    assert [p[0]["Priority"] for p in ntfy.posts] == ["3", "3", "5"]


def test_ops_topic_env_override_and_queued_entry_keeps_its_topic(
        ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_OPS", "alerts-ops")
    ntfy.up = False
    assert run(notify.push("t", "b", source="ops_watch")).status == \
        notify.QUEUED
    assert jsonstore.load(notify.OUTBOX_FILE, [])[0]["topic"] == "alerts-ops"
    ntfy.up = True
    run(notify.drain_once(force=True))
    assert ntfy.topics[-1] == "alerts-ops"


def test_alert_stores_the_capped_ops_priority(ntfy):
    run(notify.alert("OPS", "ops_watch", "t", "b", priority=5,
                     severity="error"))
    assert ntfy.posts[0][0]["Priority"] == "3"
    [row] = jsonstore.load(notify.NOTIF_STORE, {})["OPS"]
    assert row["priority"] == 3


def test_send_posts_to_the_requested_topic(monkeypatch):
    urls = []

    class Resp:
        status_code = 200

    class Client:
        async def post(self, url, **kw):
            urls.append(url)
            return Resp()

    monkeypatch.setattr(notify, "_client_get", lambda: Client())
    monkeypatch.setattr(notify, "NTFY_URL", "http://ntfy")
    monkeypatch.setattr(notify, "NTFY_TOPIC", "stocks")
    assert run(_real_send({}, "b"))
    assert run(_real_send({}, "b", "stocks-ops"))
    assert urls == ["http://ntfy/stocks", "http://ntfy/stocks-ops"]


# ------------------------------------------------------------ click deep link

def test_click_url_carries_the_id_of_the_stored_row(ntfy, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "http://dash.lan:8601/")
    monkeypatch.setenv("NTFY_CLICK_URL", "http://other/")
    d = run(notify.alert("ASML", "price", "t", "b", priority=3))
    assert d.status == notify.SENT
    [row] = jsonstore.load(notify.NOTIF_STORE, {})["ASML"]
    assert ntfy.posts[0][0]["X-Click"] == \
        f"http://dash.lan:8601/#alerts/{row['id']}"


def test_click_url_falls_back_to_ntfy_click_url_and_is_absent_without(
        ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_CLICK_URL", "http://lan:8601/")
    run(notify.alert("A", "price", "t", "b", priority=3))
    [row] = jsonstore.load(notify.NOTIF_STORE, {})["A"]
    assert ntfy.posts[0][0]["X-Click"] == f"http://lan:8601/#alerts/{row['id']}"

    monkeypatch.delenv("NTFY_CLICK_URL")
    run(notify.alert("B", "price", "t", "b", priority=3))
    assert "X-Click" not in ntfy.posts[1][0]


def test_held_alert_is_stored_and_its_outbox_click_matches_the_row(
        ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22-7")
    monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "http://dash.lan:8601")
    ntfy.at(23, 0)
    d = run(notify.alert("A", "news", "t", "b", priority=3))
    assert d.status == notify.QUEUED
    [row] = jsonstore.load(notify.NOTIF_STORE, {})["A"]
    [held] = jsonstore.load(notify.OUTBOX_FILE, [])
    assert held["headers"]["X-Click"] == f"http://dash.lan:8601#alerts/{row['id']}"


def test_alert_stores_a_longer_body_than_it_pushes(ntfy):
    run(notify.alert("A", "news", "t", "short", priority=3,
                     store_body="short plus every headline"))
    assert ntfy.posts[0][1] == "short"
    [row] = jsonstore.load(notify.NOTIF_STORE, {})["A"]
    assert row["body"] == "short plus every headline"


# ------------------------------------------------------------ policy status

def test_policy_status_reports_counts_window_and_held(ntfy, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "22-7")
    ntfy.at(23, 0)
    run(notify.push("t", "b", source="news"))                 # held
    run(notify.push("t", "b", source="price", priority=5))    # bypass, sent
    st = notify.policy_status()
    assert st["quiet_hours"] == {"window": "22-7", "active": True,
                                 "until": ts(15, 7)}
    assert st["counts_24h"] == {"news": 1, "price": 1}
    assert st["held"] == 1 and st["caps"]["price"] == 8
    assert st["ops_topic"] == "stocks-ops"
    assert notify.status()["policy"] == st
