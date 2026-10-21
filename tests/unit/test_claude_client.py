"""Claude subscription adapter (phase 6 contract §2): flags, env, JSON parsing, error mapping,
timeout kill, quota windows. The CLI is never started: ``asyncio.create_subprocess_exec`` is replaced
by a fake process; the clock and the usage file are injected.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import hashlib
import json
import os
import pathlib
import time

import pytest

from app.api import claude_client as cc

TOKEN = "sk-ant-oat01-SECRET-TOKEN"
NOW = 1_800_000_000.0


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", rc=0, hang=False):
        self.pid = 4242
        self.returncode = rc
        self._out, self._err, self._hang = stdout, stderr, hang
        self.stdin_data = None
        self.waited = False

    async def communicate(self, data=None):
        self.stdin_data = data
        if self._hang:
            await asyncio.Event().wait()
        return self._out, self._err

    async def wait(self):
        self.waited = True
        return self.returncode


def ok_json(result="hello", **extra):
    d = {"type": "result", "subtype": "success", "is_error": False, "result": result,
         "usage": {"input_tokens": 10, "output_tokens": 5},
         "modelUsage": {"claude-opus-4-1-test": {"inputTokens": 10}}}
    d.update(extra)
    return json.dumps(d).encode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(cc, "USAGE_FILE", tmp_path / "claude_usage.json")
    monkeypatch.setattr(cc, "_now", lambda: clock["t"])
    monkeypatch.setattr(cc, "CLI", "claude-fake")
    monkeypatch.setattr(cc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("CLAUDE_ENABLED", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-MUSTNOTLEAK")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other-secret")
    for k in ("CLAUDE_CAP_5H", "CLAUDE_CAP_DAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cc, "_version_cache", {"value": None, "at": None})
    return clock


@pytest.fixture
def spawn(monkeypatch):
    """Replace the subprocess factory; returns a recorder with the next process to hand out."""
    rec = {"proc": FakeProc(ok_json()), "args": None, "kwargs": None, "cwd_existed": None, "killed": []}

    async def fake_exec(*args, **kwargs):
        rec["args"], rec["kwargs"] = list(args), kwargs
        rec["cwd_existed"] = pathlib.Path(kwargs["cwd"]).is_dir()
        rec["home"] = kwargs["env"]["HOME"]
        return rec["proc"]

    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cc, "_kill_group", lambda proc: rec["killed"].append(proc.pid))
    return rec


def run(**kw):
    return asyncio.run(cc.complete("SYS", "USER PROMPT", **kw))


# --- flags / env -------------------------------------------------------------------------------

def test_flags_have_no_tools_no_prompting_and_prompt_on_stdin(env, spawn):
    out = run(model="sonnet")
    assert out["content"] == "hello"
    a = spawn["args"]
    assert a[0] == "/usr/bin/claude-fake"
    assert "-p" in a and a[a.index("--output-format") + 1] == "json"
    assert a[a.index("--model") + 1] == "claude-sonnet-5-5"          # full id, never the CLI's moving alias
    assert a[a.index("--tools") + 1] == ""                          # no tools at all
    assert a[a.index("--permission-mode") + 1] == "dontAsk"          # denies instead of prompting
    assert "--dangerously-skip-permissions" not in a and "--allowedTools" not in a
    assert a[a.index("--system-prompt") + 1] == "SYS"
    assert "USER PROMPT" not in a                                    # the user prompt never rides argv
    assert spawn["proc"].stdin_data == b"USER PROMPT"
    assert spawn["kwargs"]["start_new_session"] is True              # own process group


def test_env_is_allow_list_with_token_and_without_api_key(env, spawn, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/real")
    run(max_tokens=777)
    e = spawn["kwargs"]["env"]
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "ANTHROPIC_API_KEY" not in e and "AWS_SECRET_ACCESS_KEY" not in e
    assert e["PATH"] == "/usr/bin"
    assert e["HOME"] != "/home/real" and e["HOME"] != os.environ["HOME"]
    assert e["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "777"
    assert not any(k.startswith("ANTHROPIC") for k in e)


def test_cwd_and_home_are_fresh_temp_dirs_removed_afterwards(env, spawn):
    run()
    assert spawn["cwd_existed"]
    assert not pathlib.Path(spawn["kwargs"]["cwd"]).exists()
    assert not pathlib.Path(spawn["home"]).exists()


# --- parsing -----------------------------------------------------------------------------------

def test_success_envelope(env, spawn):
    spawn["proc"] = FakeProc(ok_json("answer text"))
    env["t"] = NOW
    out = run(model="opus")
    assert out["content"] == "answer text"
    assert out["source"] == "opus"
    assert out["model"] == "claude-opus-4-1-test"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert out["latencyS"] == 0.0 and "error" not in out


def test_result_list_form_is_accepted(env, spawn):
    spawn["proc"] = FakeProc(json.dumps([{"type": "system"}, json.loads(ok_json("from list"))]).encode())
    assert run()["content"] == "from list"


@pytest.mark.parametrize("stdout", [b"not json at all", b"", ok_json(""), ok_json(None)])
def test_unusable_output_is_bad_response(env, spawn, stdout):
    spawn["proc"] = FakeProc(stdout)
    assert run()["error"] == "bad_response"


def test_unsupported_model_alias_never_spawns(env, spawn):
    out = run(model="gpt")
    assert out["error"] == "cli" and spawn["args"] is None


# --- error mapping -----------------------------------------------------------------------------

def err_json(msg, subtype="success"):
    return json.dumps({"type": "result", "subtype": subtype, "is_error": True, "result": msg}).encode()


@pytest.mark.parametrize("msg,kind", [
    ("Claude AI usage limit reached|1800003600", "limit"),
    ("5-hour limit reached - resets 3pm", "limit"),
    ("API Error: 429 rate limit", "limit"),
    ("Invalid API key - Please run /login", "auth"),
    ("OAuth token has expired", "auth"),
    ("something else broke", "cli"),
])
def test_cli_error_results_map_to_envelopes(env, spawn, msg, kind):
    spawn["proc"] = FakeProc(err_json(msg), rc=1)
    out = run()
    assert out["error"] == kind
    assert "content" not in out


def test_error_subtype_without_is_error_flag_is_an_error(env, spawn):
    spawn["proc"] = FakeProc(json.dumps({"type": "result", "subtype": "error_max_turns", "result": "x"}).encode())
    assert run()["error"] == "cli"


def test_nonzero_exit_without_json_classifies_stderr(env, spawn):
    spawn["proc"] = FakeProc(b"", b"Error: Invalid API key", rc=1)
    assert run()["error"] == "auth"
    spawn["proc"] = FakeProc(b"", b"segfault", rc=139)
    assert run()["error"] == "cli"


def test_detail_never_contains_the_token(env, spawn):
    spawn["proc"] = FakeProc(err_json(f"bad token {TOKEN} oauth"), rc=1)
    out = run()
    assert TOKEN not in json.dumps(out) and TOKEN not in json.dumps(cc.status())


def test_spawn_failure_is_cli_error_and_not_counted(env, monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", boom)
    assert run()["error"] == "cli"
    assert cc.status()["calls5h"] == 0


def test_missing_cli_on_path(env, spawn, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    assert run()["error"] == "cli"
    assert cc.available() is False


# --- gate --------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,value", [("CLAUDE_ENABLED", "0"), ("CLAUDE_ENABLED", ""),
                                        ("CLAUDE_CODE_OAUTH_TOKEN", "")])
def test_disabled_without_gate_or_token(env, spawn, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert run()["error"] == "disabled"
    assert spawn["args"] is None
    assert cc.available() is False


# --- timeout -----------------------------------------------------------------------------------

def test_timeout_kills_process_group_and_reports(env, spawn):
    spawn["proc"] = FakeProc(hang=True)
    out = run(timeout_s=0.01)
    assert out["error"] == "timeout"
    assert spawn["killed"] == [4242] and spawn["proc"].waited
    assert cc.status()["calls5h"] == 1                      # the call may have been served: it counts
    assert cc.status()["lastError"]["error"] == "timeout"


def test_cancellation_kills_process_group(env, spawn):
    spawn["proc"] = FakeProc(hang=True)

    async def go():
        t = asyncio.ensure_future(cc.complete("S", "U", timeout_s=60))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert spawn["killed"] == [4242]


def test_real_kill_group_ignores_missing_process(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(cc.os, "killpg", gone)
    cc._kill_group(FakeProc())          # must not raise


# --- quota -------------------------------------------------------------------------------------

def test_default_caps_and_env_override(env, monkeypatch):
    assert cc.caps() == {"5h": 20, "day": 40}
    monkeypatch.setenv("CLAUDE_CAP_5H", "3")
    monkeypatch.setenv("CLAUDE_CAP_DAY", "junk")
    assert cc.caps() == {"5h": 3, "day": 40}


def test_five_hour_cap_blocks_then_window_rolls(env, spawn, monkeypatch):
    monkeypatch.setenv("CLAUDE_CAP_5H", "2")
    assert "content" in run() and "content" in run()
    assert cc.available() is False
    out = run()
    assert out["error"] == "quota"
    assert spawn["proc"].stdin_data == b"USER PROMPT"        # third call never spawned (same proc, one stdin)
    env["t"] = NOW + 5 * 3600 + 1                             # both calls left the window
    assert cc.available() is True
    assert "content" in run()


def test_daily_cap_outlasts_the_five_hour_window(env, spawn, monkeypatch):
    monkeypatch.setenv("CLAUDE_CAP_5H", "10")
    monkeypatch.setenv("CLAUDE_CAP_DAY", "3")
    for i in range(3):
        env["t"] = NOW + i * 6 * 3600                         # spread over 12 h: the 5 h window never fills
        assert "content" in run()
    env["t"] = NOW + 18 * 3600
    st = cc.status()
    assert st["calls5h"] == 0 and st["callsDay"] == 3
    assert run()["error"] == "quota"
    env["t"] = NOW + 24 * 3600 + 1                            # the first call left the 24 h window
    assert "content" in run()


def test_usage_persists_across_restart(env, spawn, monkeypatch):
    monkeypatch.setenv("CLAUDE_CAP_5H", "1")
    run()
    stored = json.loads(cc.USAGE_FILE.read_text())
    assert stored["calls"] == [NOW]
    assert run()["error"] == "quota"                          # state comes from the file, not memory


def test_limit_error_blocks_until_reset_from_cli(env, spawn):
    reset = NOW + 3600
    spawn["proc"] = FakeProc(err_json(f"Claude AI usage limit reached|{int(reset)}"), rc=1)
    assert run()["error"] == "limit"
    spawn["proc"] = FakeProc(ok_json())
    env["t"] = NOW + 60
    out = run()
    assert out["error"] == "limit" and spawn["proc"].stdin_data is None   # not spawned
    assert cc.available() is False
    assert cc.status()["blockedUntil"] == reset
    env["t"] = reset + 1
    assert cc.available() is True
    assert "content" in run()


def test_limit_without_reset_time_blocks_for_five_hours(env, spawn):
    spawn["proc"] = FakeProc(err_json("5-hour limit reached"), rc=1)
    run()
    assert cc.status()["blockedUntil"] == NOW + 5 * 3600
    env["t"] = NOW + 5 * 3600 - 1
    assert cc.available() is False
    env["t"] = NOW + 5 * 3600 + 1
    assert cc.available() is True


def test_auth_error_does_not_block_later_calls(env, spawn):
    spawn["proc"] = FakeProc(err_json("Invalid API key - /login"), rc=1)
    assert run()["error"] == "auth"
    assert cc.available() is True


# --- status ------------------------------------------------------------------------------------

def test_status_has_no_secrets_and_reports_counts(env, spawn, monkeypatch):
    class R:
        returncode, stdout = 0, "2.1.63 (Claude Code)\n"
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: R())
    run()
    st = cc.status()
    assert st["enabled"] is True and st["cliVersion"] == "2.1.63"
    assert st["calls5h"] == 1 and st["callsDay"] == 1 and st["caps"] == {"5h": 20, "day": 40}
    blob = json.dumps(st)
    assert TOKEN not in blob and "MUSTNOTLEAK" not in blob


def test_status_version_failure_is_cached_until_retry_window(env, monkeypatch):
    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise OSError("nope")
    monkeypatch.setattr(cc.subprocess, "run", fail)
    assert cc.status()["cliVersion"] is None
    cc.status()
    assert len(calls) == 1
    env["t"] = NOW + cc.VERSION_RETRY_S + 1
    cc.status()
    assert len(calls) == 2


def test_corrupt_usage_file_starts_clean(env, spawn):
    cc.USAGE_FILE.write_text("{not json")
    assert "content" in run()


# --- token sanity ------------------------------------------------------------------------------

def _nover(monkeypatch):
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))


@pytest.mark.parametrize("tok,ok", [(TOKEN, True), ("sk-ant-api03-abc", False), ("garbage", False)])
def test_token_format_flag(env, monkeypatch, tok, ok):
    _nover(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", tok)
    st = cc.status()
    assert st["tokenFormatOk"] is ok
    assert tok not in json.dumps(st) and tok[:8] not in json.dumps(st)


def test_bad_format_never_spawns_the_cli(env, spawn, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-api03-LEAKME")
    out = run()
    assert out == {"error": "auth",
                   "detail": "CLAUDE_CODE_OAUTH_TOKEN is not a setup-token (expected sk-ant-oat...)"}
    assert spawn["args"] is None
    assert "LEAKME" not in json.dumps(out)


def test_token_hash_persisted_first_seen_stable_and_new_hash_records_today(env, monkeypatch):
    _nover(monkeypatch)
    day1 = time.strftime("%Y-%m-%d", time.localtime(NOW))
    assert cc.status()["tokenFirstSeen"] == day1
    env["t"] = NOW + 10 * 86400
    assert cc.status()["tokenFirstSeen"] == day1                       # same token: unchanged
    stored = json.loads(cc.USAGE_FILE.read_text())["tokens"]
    assert list(stored.values()) == [day1]
    [h] = stored
    assert len(h) == 16 and h == hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
    assert TOKEN not in cc.USAGE_FILE.read_text()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-ROTATED")
    day2 = time.strftime("%Y-%m-%d", time.localtime(NOW + 10 * 86400))
    assert cc.status()["tokenFirstSeen"] == day2
    assert "ROTATED" not in cc.USAGE_FILE.read_text()


def test_a_success_clears_the_last_error(env, spawn):
    spawn["proc"] = FakeProc(err_json("Invalid API key - /login"), rc=1)
    assert run()["error"] == "auth"
    assert cc.status()["lastError"]["error"] == "auth"
    spawn["proc"] = FakeProc(ok_json())
    assert "content" in run()
    assert cc.status()["lastError"] is None
