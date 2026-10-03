---
name: ship-and-verify
description: Build, deploy and prove a change on the live portfolio-dashboard pod (syntax gates, image build via the quadlet .build unit, restart, endpoint matrix, headless-browser check). Use after ANY edit to app/ or static/.
---

# Ship and verify

`app/` and `static/` are baked into the image; a running pod answering with old behaviour after a
restart means the image was not rebuilt, not that something is cached.

## 1. Gates (before building)
```bash
cd ~/Work/Personal/portfolio-dashboard
python3 -m py_compile app/main.py app/api/*.py          # syntax
for f in static/js/*.js static/js/views/*.js; do node --input-type=module --check < "$f" || echo "FAIL $f"; done
```
Then unit tests (see `skills/testing`). Bump the `?v=YYYYMMDD-N` suffix on **every** asset line in
`static/index.html` (css links, `theme-boot.js`, `dompurify.min.js`, `marked.min.js`, `js/main.js`)
when any static file changed; ES-module sub-imports are revalidated by ETag (the server sends
`Cache-Control: no-cache`).

## 2. Build + restart
```bash
systemctl --user start portfolio-dashboard-build.service      # podman build from this repo (~2 s with cached layers)
systemctl --user restart portfolio-dashboard
sleep 30; podman healthcheck run portfolio-dashboard && echo HEALTHY
podman inspect -f '{{.Image}}' portfolio-dashboard            # must equal:
podman images -q --no-trunc localhost/portfolio-dashboard:latest
```
Startup takes ~20–30 s (health `HealthStartPeriod=180s`; 3 failed checks → container is killed and
systemd restarts it, `Restart=on-failure`, `MemoryMax=768M`).

If you changed the **quadlet** (`ops/quadlet/*`): the files in `~/.config/containers/systemd/` are
symlinks into this repo → `systemctl --user daemon-reload` then restart. `Environment=` values with
spaces **must be quoted** or systemd truncates them at the first space.

## 3. Endpoint matrix (all must be 200; `/health` is auth-exempt, everything else needs Basic)
Read credentials **inside the shell/python process** (see `skills/secrets-handling`), never echo them:
```bash
set -a; . ./env.secrets; set +a
for p in /health /api/portfolio /api/portfolio/history?range=1M /api/prices /api/history/ASML?range=1D \
         /api/config /api/alerts /api/lane-status /api/background /api/market-light /api/macro \
         /api/alert-rules /api/advice /api/scoreboard /api/jobs /api/worker-runs /api/forex/USD/EUR; do
  printf '%-34s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' -u "$DASHBOARD_USER:$DASHBOARD_PASS" "http://127.0.0.1:8601$p")"
done
curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASS" http://127.0.0.1:8601/api/background | jq -r 'to_entries[]|"\(.key): running=\(.value.running) err=\(.value.errors)"'
```
Payload shapes that health checks get wrong: `/api/lane-status` → `lane, serving_model, model, base_url,
role, preference, candidates, budget`; `/api/background` values have no `ok` key (`running`/`errors`);
`/api/portfolio` → `totals, positions[], benchmark, warnings[], fx`.
Also check `podman logs --since 3m portfolio-dashboard 2>&1 | grep -ciE 'error|traceback'` is 0.

## 4. Browser verification (UI changes)
- Use a Python env with `playwright` installed (the `PLAYWRIGHT_PY` interpreter), with
  `http_credentials={'username':..,'password':..}` read from `env.secrets` **inside** the script.
- NEVER embed credentials in the page URL (userinfo before the host): Chromium then rejects every relative
  `fetch()` and leaks the userinfo.
- Assert: zero console errors/warnings (the CSP is strict, so violations show up there), no horizontal
  overflow at 390 px (`document.documentElement.scrollWidth === 390`), screenshots at 1440×900 and 390×844.
- Full regression: `$PLAYWRIGHT_PY -m pytest tests/integration -q`
  (~2 min, needs the pod up; reads creds from env or `env.secrets`).

## 5. Rollback
`git revert`/checkout the previous commit → rebuild → restart. Data is untouched by code rollbacks; for
data/config damage see `skills/ops-backup-and-troubleshooting`.

## Pitfalls
- `httpx.Response` here has `.is_success` (no `.ok`).
- Background loops must catch `Exception` per pass, or one error silently kills the worker while
  `/api/background` still says `running`.
- Cache files that mix payload and metadata (`_fetched`) need dict-comprehension filters that skip
  non-dict values.
- SEC endpoints need the quoted `SEC_USER_AGENT`; Nasdaq calendar and CNBC RSS need a browser UA;
  Yahoo RSS is dead (429/404) from this host.
