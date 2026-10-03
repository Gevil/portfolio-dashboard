---
name: testing
description: How to run and write tests for the portfolio-dashboard (container-side unit tests, Playwright integration suite, what to mock, what not to test). Use when adding features, fixing bugs, or verifying before a deploy.
---

# Testing

## Run
```bash
cd ~/Work/Personal/portfolio-dashboard
# Unit tests — in a throwaway python:3.11 container (matches the image; no pytest on the host)
podman run --rm -v "$PWD:/src:z" -w /src -e HISTORY_DIR=/tmp/t -e TZ=Europe/Prague \
  docker.io/library/python:3.11-slim sh -c 'pip install -q -r requirements.txt pytest && python -m pytest tests/unit -q'
# one file / one test
… python -m pytest tests/unit/test_digest.py -q -k rating
# Integration — needs the pod up; creds come from env or env.secrets; ~2 min
python -m pytest tests/integration -q            # in a Python env that has `playwright` installed
```
Baseline at the time of writing: 376 unit tests, 29 integration tests, all passing.

## Unit-test rules
- Deterministic, isolated, no network, no sleeps, no GPU lane calls. Use `tmp_path`, set `HISTORY_DIR`/`CONFIG_PATH`
  to temp dirs, and **restore** every monkeypatched module global (a past `prices.history_store` leak made tests
  order-dependent).
- Mock at the seams: `lane_client.chat` / `_pick_lane` (lane), `notify.push` (return a `Delivery` of each status),
  `prices.listing_quote` (portfolio), `httpx` transports (providers). Time-dependent logic takes an injectable clock
  or uses `monkeypatch` on the module's `time` helper.
- Test behaviour with plausible regressions: pure functions (rating parsing, Wilson interval, budget
  reservation/refund, path jail with sibling-prefix dirs, `_extract_json`, registry normalisation, config
  corruption → last-good), and state machines (cooldown consumed only on delivered push, edgar seen-after-delivery,
  flush_pending partial delivery, rule_eval prune, approval HMAC incl. non-ASCII token, ack/unread counting).
- Do **not** write tests that only re-assert wiring, mock echoes, source text, or "does not throw". Prefer
  deleting a stale test to re-pinning incidental behaviour.

## Bug workflow
Reproduce first (failing test or script against the live pod), fix, show the same check passing. For data bugs
verify against real files in `data/` (read-only) before and after.

## Integration suite
`tests/integration/test_dashboard.py` drives Chromium with `http_credentials` (never URL credentials) and a
`DASHBOARD_BASE` override. It asserts structure (tabs, hero strip, positions table, dialogs, alert center) and
API contracts; update it with DOM changes. CSP-safe `wait_js` helper is used instead of inline scripts. Secondary
views depend on `/api/filings`-style endpoints answering — a 404 there fails the "secondary views" test, not the page.

## Before declaring done
py_compile + `node --check` gates, unit suite, build + restart, endpoint matrix, zero error lines in
`podman logs --since 3m`, and (for UI) a headless screenshot at 1440×900 and 390×844. See `skills/ship-and-verify`.
