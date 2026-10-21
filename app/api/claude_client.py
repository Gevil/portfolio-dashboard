"""Claude subscription adapter: one blind completion through the Claude Code CLI.

``claude -p`` runs as a subprocess with NO tools, a permission mode that can never prompt or
execute, an empty temp working dir, a temp HOME and a minimal env allow-list that carries only
``CLAUDE_CODE_OAUTH_TOKEN`` (``ANTHROPIC_API_KEY`` is never passed). The prompt goes through
stdin. Nothing here logs a prompt, a token or the environment.

Gate: ``CLAUDE_ENABLED=1`` and a token. A persisted local quota guard (``data/claude_usage.json``:
rolling 5 h cap ``CLAUDE_CAP_5H`` default 20, rolling 24 h cap ``CLAUDE_CAP_DAY`` default 40) counts
every started call; a usage-limit answer of the CLI blocks further calls until its window ends.
Errors are envelopes, never exceptions: ``disabled | quota | auth | timeout | limit | cli | bad_response``.
"""
import asyncio
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import time

from . import jsonstore

log = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
USAGE_FILE = DATA_DIR / "claude_usage.json"
CLI = "claude"                      # resolved through PATH (tests monkeypatch)
MODELS = ("opus", "sonnet")
# The CLI's bare aliases resolve to whatever that CLI version considers current (2.1.63 gave the 4.6
# models), so the role -> full model id mapping is explicit and overridable.
MODEL_IDS = {"opus": os.getenv("CLAUDE_MODEL_OPUS", "claude-opus-5-5"),
             "sonnet": os.getenv("CLAUDE_MODEL_SONNET", "claude-sonnet-5-5")}
WINDOW_5H_S = 5 * 3600
WINDOW_DAY_S = 24 * 3600
MAX_CALL_ROWS = 400
DETAIL_MAX = 200
TOKEN_PREFIX = "sk-ant-oat"
VERSION_RETRY_S = 300
# Only these names reach the child (plus HOME and the token). Proxy/CA vars keep a proxied pod working.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")

_LIMIT_RE = re.compile(r"usage limit|limit reached|rate.?limit|too many requests|\b429\b|quota|"
                       r"credit balance|overloaded", re.I)
_AUTH_RE = re.compile(r"invalid api key|authenticat|unauthori[sz]ed|/login|oauth|\b401\b|\b403\b|"
                      r"invalid.{0,20}token|token.{0,20}(expired|revoked|invalid)|not logged in", re.I)
_RESET_RE = re.compile(r"\|\s*(\d{9,11})\b")

_version_cache: dict = {"value": None, "at": None}
_last_error: dict | None = None


def _now() -> float:
    """The module clock (tests monkeypatch it)."""
    return time.time()


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return v if v >= 0 else default


def enabled() -> bool:
    return os.getenv("CLAUDE_ENABLED", "").strip() == "1"


def _token() -> str:
    return os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()


def caps() -> dict:
    return {"5h": _int_env("CLAUDE_CAP_5H", 20), "day": _int_env("CLAUDE_CAP_DAY", 40)}


def _cli_path() -> str | None:
    return shutil.which(CLI)


# --- persisted usage ------------------------------------------------------------------------

def token_format_ok() -> bool:
    """True for a ``claude setup-token`` token (``sk-ant-oat...``). Only this boolean ever leaves the module."""
    return _token().startswith(TOKEN_PREFIX)


def _token_hash() -> str:
    return hashlib.sha256(_token().encode("utf-8")).hexdigest()[:16]


def _record_token_seen(u: dict, now: float) -> str:
    """Date (YYYY-MM-DD) the current token hash was first seen; a new hash records today."""
    h = _token_hash()
    seen = u.setdefault("tokens", {})
    if h not in seen:
        seen[h] = time.strftime("%Y-%m-%d", time.localtime(now))
    return seen[h]


def _load_usage() -> dict:
    d = jsonstore.load(USAGE_FILE, {})
    if not isinstance(d, dict):
        d = {}
    calls = [t for t in d.get("calls") or [] if isinstance(t, (int, float))]
    blocked = d.get("blockedUntil")
    tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}
    return {"calls": calls, "blockedUntil": blocked if isinstance(blocked, (int, float)) else None,
            "lastError": d.get("lastError") if isinstance(d.get("lastError"), dict) else None,
            "tokens": {str(k): str(v) for k, v in tokens.items()}}


def _save_usage(u: dict, now: float) -> None:
    u["calls"] = [t for t in u["calls"] if t > now - WINDOW_DAY_S][-MAX_CALL_ROWS:]
    u["tokens"] = dict(sorted(u.get("tokens", {}).items(), key=lambda kv: kv[1])[-20:])
    jsonstore.save(USAGE_FILE, u)


def _counts(u: dict, now: float) -> tuple[int, int]:
    return (sum(1 for t in u["calls"] if t > now - WINDOW_5H_S),
            sum(1 for t in u["calls"] if t > now - WINDOW_DAY_S))


def _blocked_until(u: dict, now: float) -> float | None:
    b = u.get("blockedUntil")
    return b if b is not None and b > now else None


def _quota_left(u: dict, now: float) -> str | None:
    """None when a call may start, else the envelope error ('limit' or 'quota')."""
    if _blocked_until(u, now) is not None:
        return "limit"
    c5, cd = _counts(u, now)
    cap = caps()
    if c5 >= cap["5h"] or cd >= cap["day"]:
        return "quota"
    return None


def _record_error(u: dict, error: str, detail: str, now: float) -> None:
    global _last_error
    _last_error = {"error": error, "detail": detail, "at": now}
    u["lastError"] = _last_error


# --- public: availability / status ----------------------------------------------------------

def available(purpose: str = "forecast") -> bool:
    """Gate open, token present, CLI on PATH, no limit block and local quota left (caps are global)."""
    if not enabled() or not _token() or _cli_path() is None:
        return False
    return _quota_left(_load_usage(), _now()) is None


def _cli_version() -> str | None:
    now = _now()
    if _version_cache["value"] is not None:
        return _version_cache["value"]
    at = _version_cache["at"]
    if at is not None and now - at < VERSION_RETRY_S:
        return None
    _version_cache["at"] = now
    path = _cli_path()
    if path is None:
        return None
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, check=False,
                           env={"PATH": os.getenv("PATH", ""), "HOME": tempfile.gettempdir()})
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    _version_cache["value"] = out.split()[0] if r.returncode == 0 and out else None
    return _version_cache["value"]


def status() -> dict:
    """Operational snapshot, no secrets (the token is only reported as a format boolean + first-seen date)."""
    now = _now()
    u = _load_usage()
    c5, cd = _counts(u, now)
    blocked = _blocked_until(u, now)
    first_seen = None
    if _token():
        first_seen = _record_token_seen(u, now)
        _save_usage(u, now)
    return {"enabled": enabled(), "tokenPresent": bool(_token()), "tokenFormatOk": token_format_ok(),
            "tokenFirstSeen": first_seen, "cliVersion": _cli_version(),
            "calls5h": c5, "callsDay": cd, "caps": caps(),
            "blockedUntil": blocked, "lastError": u.get("lastError")}


# --- CLI invocation -------------------------------------------------------------------------

def build_args(cli: str, system: str, model: str) -> list[str]:
    """Flags of ``claude -p`` (verified against 2.1.63 ``--help``): JSON result, no tools at all, ``dontAsk``
    (anything needing permission is denied, never prompted), no session on disk, no skills/MCP/settings
    sources beyond the empty temp HOME and cwd."""
    return [cli, "-p", "--output-format", "json", "--model", model, "--tools", "",
            "--permission-mode", "dontAsk", "--no-session-persistence", "--disable-slash-commands",
            "--strict-mcp-config", "--system-prompt", system]


def build_env(home: str, max_tokens: int) -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env["HOME"] = home
    env["CLAUDE_CODE_OAUTH_TOKEN"] = _token()
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_tokens))
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError as e:
        log.warning("claude process group kill failed: %s", e)


def _clean(text: str, limit: int = DETAIL_MAX) -> str:
    tok = _token()
    if tok:
        text = text.replace(tok, "***")
    return " ".join(text.split())[:limit]


def _classify(text: str) -> str | None:
    if _LIMIT_RE.search(text):
        return "limit"
    if _AUTH_RE.search(text):
        return "auth"
    return None


def _reset_at(text: str, now: float) -> float:
    m = _RESET_RE.search(text)
    if m:
        t = float(m.group(1))
        if t > now:
            return t
    return now + WINDOW_5H_S


def _parse_result(stdout: str) -> dict | None:
    try:
        d = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(d, list):
        d = next((x for x in reversed(d) if isinstance(x, dict) and x.get("type") == "result"), None)
    return d if isinstance(d, dict) else None


def _envelope(rc, stdout: str, stderr: str, alias: str, latency: float, now: float, u: dict) -> dict:
    """Map the CLI's answer to the public envelope; sets ``u['blockedUntil']`` on a usage limit."""
    d = _parse_result(stdout)
    if d is None:
        text = f"{stderr}\n{stdout}"
        kind = _classify(text) or ("cli" if rc else "bad_response")
        if kind == "limit":
            u["blockedUntil"] = _reset_at(text, now)
        return {"error": kind, "detail": _clean(stderr or stdout or f"exit {rc}")}
    result = d.get("result")
    msg = result if isinstance(result, str) else ""
    if d.get("is_error") or str(d.get("subtype") or "success") != "success":
        kind = _classify(msg) or _classify(stderr) or "cli"
        if kind == "limit":
            u["blockedUntil"] = _reset_at(msg or stderr, now)
        return {"error": kind, "detail": _clean(msg or str(d.get("subtype") or f"exit {rc}"))}
    if not msg.strip():
        return {"error": "bad_response", "detail": "empty result"}
    mu = d.get("modelUsage")
    model = next(iter(mu), alias) if isinstance(mu, dict) and mu else alias
    usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    return {"content": msg, "model": model, "source": alias, "usage": usage, "latencyS": round(latency, 3)}


async def complete(system: str, user: str, *, model: str = "opus", max_tokens: int = 1500,
                   timeout_s: float = 240, purpose: str = "forecast") -> dict:
    """One blind completion (see module docstring). Never raises for CLI/quota problems."""
    global _last_error
    if not enabled() or not _token():
        return {"error": "disabled", "detail": "CLAUDE_ENABLED=1 and CLAUDE_CODE_OAUTH_TOKEN are required"}
    if model not in MODELS:
        return {"error": "cli", "detail": f"unsupported model alias (use {'|'.join(MODELS)})"}
    if not token_format_ok():
        return {"error": "auth", "detail": "CLAUDE_CODE_OAUTH_TOKEN is not a setup-token (expected sk-ant-oat...)"}
    cli = _cli_path()
    if cli is None:
        return {"error": "cli", "detail": "claude CLI not found on PATH"}
    now = _now()
    u = _load_usage()
    _record_token_seen(u, now)
    refused = _quota_left(u, now)
    if refused:
        b = _blocked_until(u, now)
        detail = f"usage window blocked until {int(b)}" if b else "local quota reached"
        return {"error": refused, "detail": detail}
    u["calls"].append(now)              # counted when started: a timed-out call may still have been served
    _save_usage(u, now)

    t0 = _now()
    with tempfile.TemporaryDirectory(prefix="claude-call-") as tmp:
        work, home = pathlib.Path(tmp) / "work", pathlib.Path(tmp) / "home"
        work.mkdir()
        home.mkdir()
        try:
            proc = await asyncio.create_subprocess_exec(
                *build_args(cli, system, MODEL_IDS[model]), cwd=str(work), env=build_env(str(home), max_tokens),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as e:
            u["calls"].remove(now)
            _save_usage(u, now)
            return {"error": "cli", "detail": _clean(f"spawn failed: {e}")}
        try:
            out, err = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), timeout_s)
        except asyncio.TimeoutError:
            _kill_group(proc)
            await proc.wait()
            _record_error(u, "timeout", f"no answer within {timeout_s:g}s", _now())
            _save_usage(u, _now())
            return {"error": "timeout", "detail": f"no answer within {timeout_s:g}s"}
        except BaseException:           # cancellation: never leave the CLI (or its children) running
            _kill_group(proc)
            raise
    latency = _now() - t0
    now = _now()
    env = _envelope(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                    model, latency, now, u)
    if "error" in env:
        _record_error(u, env["error"], env["detail"], now)
        log.warning("claude call failed: %s (purpose=%s)", env["error"], purpose)
    elif u.get("lastError"):
        u["lastError"] = None           # a success clears the auth (or any) error state
    _save_usage(u, now)
    return env
