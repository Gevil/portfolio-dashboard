---
name: workers-and-alerts
description: Add or change a background worker, alert rule, notification, ntfy action button or the alert center. Covers the worker contract, the Delivery/outbox rules, runlog, quiet hours, approvals (HMAC) and dedupe stores. Use before editing notify, alerts, approvals, rule_eval, price_alerts, news_alerts, triage, edgar, market_light, ops_watch, runlog.
---

# Workers and alerts

## Worker contract
A background module exposes `start()` (sync, creates the task), `async stop()` (main.py awaits it at shutdown for every module in the list), `async close()` (only if it holds an HTTP client; main.py awaits it explicitly), `status() -> dict` (include
`running`, `enabled`, error counters) and is listed in **`main.BACKGROUND_MODULES`** — a module that
implements the contract but is missing from the list is dead code whose endpoints answer from stale files.
Rules:
- Wrap each pass in `except Exception` + log; one bad pass must not kill the loop (the task can look
  `running` while doing nothing).
- Record every pass: `runlog.record(worker, ok, duration_s, note, idle=False)`. Rings are **per worker**;
  idle passes are recorded only on change plus an hourly heartbeat. `ops_watch` flags workers whose last
  success is older than 2× their interval — set `interval_s` in `status()`.
- Feature-gate with an env var (`*_ENABLED`), read at `start()`; log when disabled.
- State files via `jsonstore.load/save` only. Config via `config_store.read()`.
- Per-ticker `try/except` inside the pass so one symbol cannot starve the rest.
- Honour `registry`: only `is_alertable` symbols alert; benchmarks and non-alertable entries are skipped.

## Sending notifications: the Delivery contract
`await notify.push(title, body, severity, *, priority=None, tags=None, actions=None, dedupe_key=None, …)`
returns a `Delivery` (truthy iff accepted) with `.status` ∈ `sent | queued | filtered | duplicate | failed`.
- The durable outbox (`data/ntfy_outbox.json`, drained every `NTFY_OUTBOX_POLL_S`, backoff, max age) retries
  failed sends, so a down ntfy produces `queued`, not loss. `failed` happens only if even the outbox write failed.
- **Consume your dedupe/cooldown/seen/baseline state for every status except `failed`.** Never mark an item
  seen *before* the push result; never pop a pending queue on `failed`. (Past bugs: edgar marked filings seen
  before processing; flush_pending popped undelivered alerts; market-light/digest saved state regardless.)
- Severity: `info | warn | urgent`; `NOTIFY_MIN_SEVERITY` filters (→ `filtered`, which is a consumed outcome —
  do not re-queue and re-classify filtered items, that burns LLM turns). Quiet hours hold non-urgent pushes.
- Keep push bodies short, one grouped push per ticker/batch (no per-trade or per-headline bursts), no URLs
  taken from model output, text through `textsafe`.
- Each push is also stored in `data/notifications.json` (id, source, ticker, severity, url) — this store feeds
  the alert center, **not** the live ntfy stream.

## Alert center API
`GET /api/alerts?limit=` → `{items:[{id,time,title,message,priority,severity,source,ticker,url,acked}], unread}`;
`POST /api/alerts/ack` `{ids:[…]}` or `{all:true}`; ack state in `data/alerts_ack.json`. SSE emits an `alert`
event only for alerts created **after** the hub started (no backlog replay — replaying history toasts the whole
backlog on every page load).

## Rules and detectors
- `price_alerts`: session-move rule and **gap-vs-previous-close** rule, separate cooldown keys; reads
  `prices.listing_quote`; saves state only per accepted push; ignores stale quotes.
- `rule_eval` (rules v2, 8 kinds): closed-bar `ema_cross`/`rsi`/`volume_spike`, absolute level (edge-detected,
  oneShot + expiry), earnings-day lead, short-ratio sigma spike, market-light drop. State pruning must never drop
  fired one-shot entries or live edge baselines (refresh `touch` each pass while the rule exists).
  Rules are edited through `POST /api/alert-rules` (400 on invalid body) and stored via `config_store.update`.
- `news_alerts` + `topnews` + `triage`: consume the topnews cache (GDELT only as fallback; it 429s and backs off);
  one title-hash dedupe store (`data/news_dedupe.json`); hot keywords go to triage unless they match the short
  high-precision list, else priority ≤ 4; triage candidates expire (~6 h); triage spends only the `triage` budget.
- `edgar`: Form 4 / 8-K; seen-set updated after delivery; seed only when ≥ 1 SEC fetch succeeded; needs the quoted
  `SEC_USER_AGENT`.

## Approvals (ntfy action buttons)
`POST /api/approvals/{id}/approve|deny?token=…` are the only unauthenticated mutating routes. The token is a
**per-approval HMAC** (id + action + expiry) derived from `APPROVAL_TOKEN`; compare as bytes
(`hmac.compare_digest(a.encode(), b.encode())` — non-ASCII must yield 403, never 500). TTL = `APPROVAL_TTL_S`
(1800). Buttons are built from `NTFY_CLICK_URL`/`DASHBOARD_PUBLIC_URL`. uvicorn runs `--no-access-log`; keep it.

## Checklist for a new worker/alert
1. Module with contract + `*_ENABLED` gate + `runlog.record`; add to `BACKGROUND_MODULES`.
2. State via `jsonstore`; consume state per Delivery rules; unit tests (fake `notify.push` returning each status).
3. Surfaces: `/api/background` shows it; AI Ops worker table (UI) picks it up from there — set `interval_s`.
4. Rebuild + verify per `skills/ship-and-verify`; confirm no `error|traceback` in logs for a few passes.
