---
name: ops-backup-and-troubleshooting
description: Operate the portfolio-dashboard pod on Bazzite - quadlet/units, daily backup and restore, rollback, health, log patterns and a symptom-to-cause table (empty panels, lane down, IPv6 RST, stale lanes-enrich, missing data, noisy alerts, old orphan containers). Use when something is broken or before changing deployment.
---

# Ops, backup and troubleshooting

## Deployment facts
- Units: `ops/quadlet/portfolio-dashboard.container` + `.build` (repo-owned), symlinked into
  `~/.config/containers/systemd/`. After editing: `systemctl --user daemon-reload` then restart.
  `systemctl --user cat portfolio-dashboard` shows what systemd actually runs.
- Mounts: `config/` (dir, rw) → `/app/config`; `data/` → `/app/data`; `results/` → `/app/results`;
  `~/.local/bin/gpu-lanes` (dir, ro) → `/app/lanes`. `env.secrets` is the `EnvironmentFile`.
  Network: `aistock` (ntfy, open-webui, ollama by name) + `podman`.
- Self-healing: `Restart=on-failure`, `HealthCmd` → `/health` (3 fails → kill → restart), `MemoryMax=768M`.
- Host side (not in this repo): `lanes-enrich.timer` (writes `lanes-enrich.json`), GPU lane quadlets, `host-dashboard`
  (`:8443`), ntfy. Open WebUI also mounts `results/` read-only.

## Backup / restore / rollback
- `ops/backup.sh` (timer `portfolio-dashboard-backup.timer`, 03:30, persistent): tar.zst of `config/ data/ results/` →
  `~/backups/portfolio-dashboard/` and, if `DASHBOARD_BACKUP_DEST` is set in the service unit, a second location (e.g. a NAS share), keeps 21,
  integrity-tests the archive before keeping it. **`env.secrets` is excluded.** Run manually before risky changes.
  `systemctl --user list-timers | grep portfolio`; `journalctl --user -u portfolio-dashboard-backup -n 20`.
- **Config safety**: `config/config.json` (watchlist, positions, alert rules) is gitignored and never baked into the
  image; `.dockerignore` excludes `config/`. Deploy with `ops/deploy.sh` (refuses on missing/invalid config, snapshots
  first, verifies unchanged). The app also keeps `data/config.lastgood.json` and serves it if the file is damaged.
  Restore just the config: `bash ops/restore-config.sh [archive]` (keeps the current file as `config.json.before-restore-*`).
- Destructive git commands: `git clean -fdx`, `git stash --all` and a fresh clone delete/hide ignored files
  (`config/config.json`, `env.secrets`, `data/`, `results/`). Run `bash ops/backup.sh` first. Safe: `pull`, `checkout`,
  `reset --hard`, `git clean -fd`.
- New machine: clone, create `env.secrets`, `bash ops/deploy.sh --init` (seeds the fictional sample), then enter your
  real tickers/positions in Settings (or restore with `ops/restore-config.sh`).
- Full restore: `systemctl --user stop portfolio-dashboard` → extract the archive into the repo dir
  (`tar --zstd -xf pd-….tar.zst -C ~/Work/Personal/portfolio-dashboard`) → start → check `/api/portfolio`.
- Code rollback: `git revert`/checkout → `bash ops/deploy.sh`. Data is untouched by code rollbacks.
- Config sanity: `GET /api/config` answers 503 only when the stored config is unreadable and no last-good exists;
  PUT never overwrites an unreadable config with defaults.

## Health at a glance
```bash
podman ps --format '{{.Names}} {{.Status}}' | grep portfolio
podman healthcheck run portfolio-dashboard && echo HEALTHY
podman logs --since 10m portfolio-dashboard 2>&1 | grep -iE 'error|traceback|warning' | cut -c1-200 | sort | uniq -c | sort -rn | head
# creds via env.secrets sourced in the shell (see skills/secrets-handling):
curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASS" http://127.0.0.1:8601/api/background | jq -r 'to_entries[]|"\(.key) running=\(.value.running) err=\(.value.errors)"'
curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASS" http://127.0.0.1:8601/api/worker-runs | jq '.[0:10]'
```
AI Ops view in the UI shows lanes, budget, workers (stale/failing), jobs and approvals.

## Symptom → likely cause
| Symptom | Check / cause |
|---|---|
| Lane chip "down" / AI jobs end `lane_down` | Only one GPU lane runs; `GET /api/lane-status` shows candidates + state. `lanes-switch` is host-side. If a lane is `ok` on host but the pod says down: `lanes-enrich.json` older than 120 s (timer stopped) → no container path; host ports are loopback-only. |
| Lane fails only for fresh connections, existing sessions work | Half-dead pasta forwarder: IPv6 RST on `:800x`. `localhost` resolves `::1` first — probe `127.0.0.1`; `ss -ltnp \| grep :8002` shows the listener owner; fix is `systemctl --user restart` of the lane service (restart drops active sessions). |
| Digest/triage idle, `budget` errors | Daily turn budget spent (`/api/lane-status` → `budget`); digest has a 16-turn reserve. Resets on local day change. |
| Panel shows an error/stale chip | The UI now never hides HTTP errors; hit the endpoint directly and read `podman logs`. |
| Price missing / `stale_price` warning | Yahoo hiccup or market closed (holidays aren't modelled). `usLive` missing is normal for SXR8/GSPC (null providers). |
| `fx_stale` warning | Frankfurter + Yahoo FX both failed >24 h; check outbound network. |
| No push notifications | `ntfy` container up? `NTFY_PASS` in `env.secrets`? `/api/background` → `notify` outbox size >0 means queued retries; severity filter `NOTIFY_MIN_SEVERITY`. |
| Duplicate/odd price alerts | Another process posting to the same ntfy topic (e.g. an old watcher container). Note that podman's quadlet generator also scans **subfolders** of `~/.config/containers/systemd/`, so files parked in a `retired/` subfolder still run — move them out of the tree and `daemon-reload`. |
| SEC data missing | `SEC_USER_AGENT` must be quoted in the unit (unquoted values are truncated at the first space → sec.gov 403). |
| Change "not live" | The image was not rebuilt (`skills/ship-and-verify`). |
| Quadlet change ignored | Forgot `daemon-reload`, or editing a symlink target in the wrong copy — `systemctl --user cat` to confirm. |
| Report "not found" | Report ids are opaque `TICKER@<stem>`; reports live under `results/<TICKER>/full_states_log_*.json`. |
| Podman build dirs read-only inside containers on this host | SELinux `user_home_t` labels: use `:z` on mounts as in the quadlet. |

## Things not to do
- Do not `podman exec … rm` state files to "reset" a worker; stop the pod, move the file, start the pod.
- Do not edit files inside the running container — they vanish on restart; edit the repo and rebuild.
- Do not run lane-switching or GPU-heavy commands from here; the GPU is shared with the owner's other work.
