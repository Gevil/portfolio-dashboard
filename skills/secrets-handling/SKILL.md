---
name: secrets-handling
description: How to work with env.secrets, Basic-auth credentials, the approval token and API keys without leaking them into tool output, session transcripts, logs, git or backups. Read before touching credentials or scanning for exposure.
---

# Secrets handling

`env.secrets` (mode 0600, gitignored, dockerignored, **excluded from backups**) is a systemd
`EnvironmentFile`; a pod restart picks up edits. Keys present: `DASHBOARD_USER/PASS`, `APPROVAL_TOKEN`,
`NTFY_PASS`, `OPEN_WEBUI_API_KEY`, `FINNHUB_API_KEY`, `TWELVE_DATA_API_KEY`, `FRED_API_KEY`,
`DASHBOARD_PUBLIC_URL`, plus feature gates (`DIGEST_ENABLED`, `EDGAR_ENABLED`, `KB_SYNC_ENABLED`,
`NEWS_ALERTS_ENABLED`, `TRIAGE_ENABLED`, `*_ENABLED=1`).

## Rule: tool output must never contain a secret value
Tool output is persisted in session transcripts and re-inlined into later context. One `grep` on
`env.secrets` can produce dozens of copies.
- Do **not** `cat`/`read`/`grep` `env.secrets`. Do not run `env`, `printenv`, `podman inspect` (shows
  Env), or `systemctl show` on the unit without filtering.
- Load values into the **process** and print only hashes/lengths/`file:line`:
  ```bash
  set -a; . ./env.secrets; set +a        # shell: safe to USE in curl -u "$U:$P"; never echo
  ```
  ```python
  env = dict(l.rstrip('\n').split('=',1) for l in open('env.secrets') if '=' in l and not l.startswith('#'))
  # compare by hash:  hashlib.sha256(v.encode()).hexdigest()[:10]
  ```
- Feature gates are not secrets; check them by key presence (`KEY=1`), never by dumping the file.
- When showing config for debugging, mask: `sed -E 's/((PASS|TOKEN|KEY|SECRET)[A-Z_]*=).*/\1<masked>/'`.

## Scanning for exposure
Scan in **bytes** (`Path.read_bytes()` + bytes regex). `read_text(errors="ignore")` silently destroys
ASCII runs next to invalid UTF-8 (SQLite/WAL). Exposure surfaces beyond config files: session `.jsonl`
transcripts (including subagent ones), Open WebUI's `webui.db` (stores the dashboard Basic-auth Valve
values + API key), `podman logs`, ntfy's message cache, git history. Rotating a credential does not
erase old bytes from SQLite; stop the service, then VACUUM.

## Places a secret can leak in this project (all handled — keep them that way)
- **Approval buttons**: ntfy action URLs carry a **per-approval HMAC** (id + action + expiry, derived from
  `APPROVAL_TOKEN`), not the raw token. uvicorn runs with `--no-access-log` so query strings are not
  persisted. Do not re-enable access logs without filtering the query.
- **Basic auth**: `/health` and the two `POST /api/approvals/{id}/approve|deny` routes are the only
  unauthenticated paths (the approval routes then check the HMAC; non-ASCII tokens must give 403, not 500).
- **Browser tests**: pass credentials via `http_credentials`, never in the URL.
- **Self-made placeholders**: a scrubbed unit line like `TOKEN=SCRUBBED` proves nothing if you "validate"
  it against the provider (401 from the word SCRUBBED).
- **Git**: `env.secrets`, `data/`, `results/` and the real `config/config.json` (positions) are gitignored; only
  `config/config.example.json` is tracked. Before any commit run `git status --short` and make sure none of them appear.
- **Backups** (`ops/backup.sh`) include `config/`, `data/`, `results/` — and deliberately **not**
  `env.secrets`. Keep a copy of `env.secrets` in a password manager.
