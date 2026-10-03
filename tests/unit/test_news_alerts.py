"""news_alerts: keyword classes, partial-delivery flush, seen-only-when-shown."""
import asyncio
import time

import pytest

from app.api import newsdedupe, news_alerts, notify, topnews


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(news_alerts, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(news_alerts, "LAST_ALERT_FILE", tmp_path / "last.json")
    monkeypatch.setattr(newsdedupe, "FILE", tmp_path / "dedupe.json")
    monkeypatch.setattr(news_alerts, "TRIAGE_ON", False)
    monkeypatch.setattr(news_alerts, "in_quiet_hours", lambda c, hour=None: False)

    pushes = []
    stored = []
    outcomes = {}

    async def push(title, body, severity="warning", **kw):
        pushes.append((title, body, kw))
        return outcomes.get(title.split()[0], notify.Delivery(notify.SENT))

    def store(ticker, source, title, body="", **kw):
        stored.append((ticker, title, body))
        return "id"

    monkeypatch.setattr(news_alerts.notify, "push", push)
    monkeypatch.setattr(news_alerts.notify, "store", store)
    return type("Env", (), {"pushes": pushes, "stored": stored,
                            "outcomes": outcomes})


def conf():
    c = dict(news_alerts.DEFAULTS)
    c["aliases"] = {"NVDA": ["nvidia", "nvda"]}
    return c


def item(headline, summary="", **kw):
    return {"headline": headline, "summary": summary, "url": "https://x.test/a",
            "source": "wire", "published_at": time.time(), **kw}


# ------------------------------------------------------------- keyword classes

def test_hot_words_split_precise_from_broad():
    c = conf()
    assert news_alerts.match(item("Nvidia downgraded by Acme"), c, "NVDA") == 5
    # the broad words that produced false priority-5 pushes are capped at 4
    for word in ("sanction", "tariff", "probe", "lawsuit", "antitrust",
                 "recall", "insider selling"):
        assert news_alerts.match(item(f"Nvidia faces {word} news"), c,
                                 "NVDA") == 4, word
    assert news_alerts.match(item("Nvidia earnings preview"), c, "NVDA") == 3


def test_item_must_mention_the_ticker(env):
    assert news_alerts.match(item("Tariff podcast: Intel downgraded"), conf(),
                             "NVDA") == 0


# ------------------------------------------------------------- flush_pending

def test_flush_pending_partial_delivery(env):
    pending = {
        "AAA": [{"prio": 3, "items": [
            {"headline": f"AAA story {i}", "source": "w", "url": ""}
            for i in range(20)]}],
        "BBB": [{"prio": 5, "items": [
            {"headline": "BBB crash", "source": "w", "url": ""}]}],
    }
    env.outcomes["BBB"] = notify.Delivery(notify.FAILED)
    flushed = asyncio.run(news_alerts.flush_pending(pending))
    assert flushed == 1
    # BBB stays queued untouched; AAA is gone
    assert list(pending) == ["BBB"]
    assert pending["BBB"][0]["items"][0]["headline"] == "BBB crash"
    # ONE grouped push per ticker, every title kept (no silent [:5] drop)
    assert [p[0] for p in env.pushes] == ["AAA overnight news (20)",
                                          "BBB overnight news (1)"]
    stored_aaa = [s for s in env.stored if s[0] == "AAA"]
    assert len(stored_aaa) == 1
    assert all(f"AAA story {i}" in stored_aaa[0][2] for i in range(20))
    assert not [s for s in env.stored if s[0] == "BBB"]


def test_flush_pending_understands_legacy_rows(env):
    pending = {"AAA": [["t", "old body line", 3]]}
    assert asyncio.run(news_alerts.flush_pending(pending)) == 1
    assert pending == {}
    assert "old body line" in env.pushes[0][1]


# ------------------------------------------------------------- seen handling

def run_ticker(items, c=None):
    c = c or conf()
    topnews.get_top_news = lambda ticker=None: {"ts": time.time(),
                                                "items": items}
    return asyncio.run(news_alerts.process_ticker(None, "NVDA", c, {}))


@pytest.fixture(autouse=True)
def _restore_topnews():
    saved = topnews.get_top_news
    yield
    topnews.get_top_news = saved


def test_cooldown_hold_back_does_not_mark_items_seen(env):
    news_alerts.jsonstore.save(news_alerts.LAST_ALERT_FILE,
                               {"NVDA": time.time()})
    n = run_ticker([item("Nvidia earnings beat")])
    assert n == 0 and env.pushes == []
    key = newsdedupe.news_key("Nvidia earnings beat")
    assert not newsdedupe.seen("NVDA", key, 86400)


def test_failed_delivery_keeps_items_unseen_then_sent_marks_them(env):
    env.outcomes["NVDA:"] = notify.Delivery(notify.FAILED)
    items = [item("Nvidia earnings beat"), item("Nvidia guidance raised")]
    assert run_ticker(items) == 0
    for it in items:
        assert not newsdedupe.seen(
            "NVDA", newsdedupe.news_key(it["headline"]), 86400)

    env.outcomes.clear()
    assert run_ticker(items) == 1
    # stored notification lists every headline; all are now seen
    assert "Nvidia earnings beat" in env.stored[-1][2]
    assert "Nvidia guidance raised" in env.stored[-1][2]
    assert run_ticker(items) == 0            # deduped: no second alert


def test_headlines_are_sanitised_before_push_and_store(env):
    n = run_ticker([item("Nvidia\u202e earnings\x00 beat http://evil.test/x")])
    assert n == 1
    title, body, _ = env.pushes[0]
    assert "\u202e" not in body and "\x00" not in body
    assert "evil.test" not in body
    assert "evil.test" not in env.stored[-1][2]
