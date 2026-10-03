# Portfolio Dashboard — agent guide

Single-user, LAN-only portfolio dashboard (FastAPI + vanilla ES modules) that runs as a **rootless
podman quadlet** on the Bazzite workstation, port **8601**, HTTP Basic auth. It values the owner's EUR
holdings, alerts via ntfy, and runs a local-LLM analysis/digest pipeline on shared GPU lanes.

This file is the map. **Procedures live in `skills/`** — read the matching skill before acting
(table below). Keep this file short; put detail in a skill.

## What it is (facts that shape every decision)
- **Example setup** (EU venues, everything **EUR**): holdings ASML (`ASML.AS`), NVDA (`NVD.DE`), iShares Core
  S&P 500 UCITS ETF (`SXR8`, `SXR8.DE`) — all configurable. **GSPC (`^GSPC`) is a benchmark only**: shown,
  never alerted on, never analysed by the LLM, never in the portfolio. There is no USD display toggle.
- **One price series per holding**: the EUR listing (yahoo). The US live feeds (Twelve Data / Finnhub WS)
  are a labelled `usLive` reference and are **never merged** into the listing series (that merge caused
  1D chart spikes once).
- **Position model**: `portfolio[id] = {shares, investedAmount|null}` (EUR cost basis; `null` = unknown →
  P/L shown n/a and listed in `totals.costMissing`, never silently dropped).
- **LLM lanes** (shared with the owner's other GPU work — be frugal): primary `ninfer-nvfp4`
  (`qwen3.8-27b`), fallback `exllama` (`Qwen3.8-Flash-Next-exl3`). Autonomous budget 30 turns/day
  (digest reserve 16, triage cap 10).

## Layout
```
app/main.py            routes, auth gate, SSE hub, periodic-task supervisor, BACKGROUND_MODULES
app/api/registry.py    watchlist entry schema, provider symbols, venue hours (source of truth)
app/api/config_store.py  the ONLY way to read/write config (atomic, last-good fallback)
app/api/portfolio.py   EUR valuation: /api/portfolio, /api/portfolio/history, position_for()
app/api/prices.py live_ws.py forex.py indicators.py   data layer
app/api/lane_client.py   lane preference, probing, pinning, per-purpose budget
app/api/digest.py jobs.py ta_pipeline.py evidence.py scoreboard.py reports.py chat.py   LLM side
app/api/notify.py (outbox + Delivery) alerts.py approvals.py rule_eval.py price_alerts.py
  news_alerts.py triage.py edgar.py market_light.py ops_watch.py runlog.py ...   alert/worker side
app/api/jsonstore.py textsafe.py   shared atomic-JSON + untrusted-text helpers
app/playbooks/*.md     LLM prompts (all carry the "pack text is untrusted DATA" clause)
static/index.html      shell; js/main.js is the ES-module entry; js/views/*, css/*
config/config.json     YOUR watchlist/portfolio/alertRules (gitignored; sample: config.example.json; bind-mounted DIR → /app/config)
data/  results/        runtime state / analysis reports (gitignored data/ results/; backed up)
ops/quadlet/*          the deployed unit files (symlinked into ~/.config/containers/systemd/)
ops/backup.sh          daily backup (timer 03:30) → local dir, optional second copy
tests/unit  tests/integration
env.secrets            credentials + feature gates — NEVER print, cat, grep, or commit
```

Project overview, features, configuration, deploy/backup/dev commands: [`README.md`](README.md).

## Skills — read the one that matches the task
| If you are about to… | Read |
|---|---|
| change code and deploy it, or verify the live pod | `skills/ship-and-verify/SKILL.md` |
| touch `env.secrets`, credentials, logs that may contain tokens | `skills/secrets-handling/SKILL.md` |
| add/remove a ticker, change a holding, cost basis, symbols, providers, FX, history | `skills/watchlist-and-data-layer/SKILL.md` |
| touch the LLM pipeline, lanes, budget, digest, scoreboard, playbooks, chat | `skills/lanes-and-llm-pipeline/SKILL.md` |
| add a background worker, an alert, a notification, approvals, quiet hours | `skills/workers-and-alerts/SKILL.md` |
| edit anything under `static/` (UI, CSS, vendored libs, CSP) | `skills/frontend-modules/SKILL.md` |
| write or run tests | `skills/testing/SKILL.md` |
| back up, restore, roll back, or debug the pod/quadlet/host timers | `skills/ops-backup-and-troubleshooting/SKILL.md` |

## Non-negotiable rules
1. **Image-baked code**: `app/` and `static/` are copied into the image. An edit is **not live** until
   the image is rebuilt (`systemctl --user start portfolio-dashboard-build.service`) and the pod restarted.
   Only `config/`, `data/`, `results/`, `env.secrets` and `~/.local/bin/gpu-lanes` are mounts.
2. **Config only through `config_store`** (`read()` / `update(mutator)`). Never `json.loads` the file,
   never write it directly. A read error must never be "healed" by writing defaults back.
3. **State files only through `jsonstore`** (`load`/`save`: serialise first, tmp + fsync + `os.replace`).
4. **Notifications return a `Delivery`.** Consume dedupe/cooldown/seen state for every status except
   `failed`. Never mark something "seen" before it was sent or queued.
5. **Unsupported provider symbol → skip the request.** `registry.provider_symbol()` returns `None`;
   no raw-id fallback, no negative-cache entry.
6. **Untrusted text** (headlines, filings, model output) goes through `textsafe` before prompts,
   pushes or storage; the UI renders markdown only via `renderMarkdown` (DOMPurify, fail-closed).
   No model-written URLs in notifications.
7. **CSP is strict**: `script-src 'self' + tradingview`, `style-src 'self'`. No inline `style=`/`<script>`
   in the frontend; set dynamic styles via CSSOM.
8. **Do not call or load the GPU lanes casually** (no ad-hoc LLM calls in tests/scripts; the owner's other
   work shares them). Autonomous LLM calls must go through `lane_client.chat(..., autonomous=True, purpose=...)`.
9. **Background workers** implement `start()/stop()/status()/close()`, wrap each pass in
   `except Exception`, record to `runlog`, and are listed in `main.BACKGROUND_MODULES` — otherwise
   they are dead code whose endpoints answer from stale files.
10. **Never commit secrets or personal data**: `env.secrets`, `data/`, `results/` and the real
    `config/config.json` (share counts, cost basis) are gitignored; only `config/config.example.json` is tracked.
    Commit only when the owner asks.

## Quick commands
```bash
cd ~/Work/Personal/portfolio-dashboard
systemctl --user start portfolio-dashboard-build.service && systemctl --user restart portfolio-dashboard
podman healthcheck run portfolio-dashboard && echo HEALTHY
podman logs --since 5m portfolio-dashboard 2>&1 | grep -iE 'error|traceback'
bash ops/backup.sh
```
Anything involving credentials: use the patterns in `skills/secrets-handling/SKILL.md` instead of
reading `env.secrets` directly.
