"""approvals: per-approval HMAC tokens for the ntfy action buttons."""
import time
import urllib.parse

import pytest

from app.api import approvals


@pytest.fixture
def appr(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "APPROVALS_FILE", tmp_path / "approvals.json")
    monkeypatch.setenv("APPROVAL_TOKEN", "s3cret")
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setenv("NTFY_CLICK_URL", "http://dashboard.example:8601/")
    return approvals.create("ASML", "deep dive on the EUV story", "src-1")


def token_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["token"][0]


def test_token_is_bound_to_id_and_action(appr):
    exp = int(time.time()) + 600
    tok = approvals.make_token(appr["id"], "approve", exp)
    assert approvals.check_token(tok, appr["id"], "approve")
    assert not approvals.check_token(tok, appr["id"], "deny")
    assert not approvals.check_token(tok, "someotherid", "approve")


def test_leaked_button_url_cannot_approve_another_item(appr):
    other = approvals.create("NVDA", "another proposal", "src-2")
    urls = {b["label"]: b["url"] for b in approvals.action_urls(appr)}
    tok = token_of(urls["Run deep-dive"])
    assert approvals.check_token(tok, appr["id"], "approve")
    assert not approvals.check_token(tok, other["id"], "approve")


def test_expired_and_tampered_tokens_are_rejected(appr):
    past = int(time.time()) - 5
    assert not approvals.check_token(
        approvals.make_token(appr["id"], "approve", past), appr["id"], "approve")
    exp = int(time.time()) + 600
    tok = approvals.make_token(appr["id"], "approve", exp)
    forged = f"{exp + 3600}.{tok.split('.', 1)[1]}"       # extend the expiry
    assert not approvals.check_token(forged, appr["id"], "approve")


@pytest.mark.parametrize("bad", ["", None, "garbage", ".", "12.", "x.y",
                                 "9999999999.\u00e9\u00fc\u4e2d",
                                 "9999999999." + "\ud800"])
def test_malformed_and_non_ascii_tokens_are_false_not_errors(appr, bad):
    assert approvals.check_token(bad, appr["id"], "approve") is False


def test_non_ascii_secret_works(appr, monkeypatch):
    monkeypatch.setenv("APPROVAL_TOKEN", "s\u00e9cret-\u00fc\u4e2d")
    exp = int(time.time()) + 600
    tok = approvals.make_token(appr["id"], "deny", exp)
    assert approvals.check_token(tok, appr["id"], "deny")


def test_missing_secret_fails_closed(appr, monkeypatch):
    tok = approvals.make_token(appr["id"], "approve", int(time.time()) + 600)
    monkeypatch.delenv("APPROVAL_TOKEN")
    assert not approvals.check_token(tok, appr["id"], "approve")
    assert approvals.action_urls(appr) == []


def test_action_urls_use_click_url_base_and_distinct_tokens(appr):
    buttons = approvals.action_urls(appr)
    assert [b["method"] for b in buttons] == ["POST", "POST"]
    approve, deny = buttons[0]["url"], buttons[1]["url"]
    prefix = f"http://dashboard.example:8601/api/approvals/{appr['id']}"
    assert approve.startswith(prefix + "/approve?token=")
    assert deny.startswith(prefix + "/deny?token=")
    assert token_of(approve) != token_of(deny)
    # the raw secret never appears in a URL
    assert "s3cret" not in approve + deny


def test_token_lifetime_follows_the_approval_ttl(appr):
    tok = token_of(approvals.action_urls(appr)[0]["url"])
    expiry = int(tok.split(".", 1)[0])
    assert expiry == int(appr["created_at"] + approvals.TTL_S)
    assert approvals.check_token(tok, appr["id"], "approve",
                                 now=appr["created_at"] + approvals.TTL_S - 1)
    assert not approvals.check_token(tok, appr["id"], "approve",
                                     now=appr["created_at"] + approvals.TTL_S + 1)
