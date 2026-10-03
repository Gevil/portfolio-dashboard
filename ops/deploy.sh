#!/usr/bin/env bash
# Safe (re)deploy of the portfolio-dashboard quadlet from this checkout.
#
#   ops/deploy.sh            snapshot -> preflight -> build image -> restart -> verify config survived
#   ops/deploy.sh --check    preflight only (no snapshot, build or restart)
#   ops/deploy.sh --init     first run on a new machine: seed config/config.json from the sample
#
# Your real config (watchlist, positions, alert rules) lives in config/config.json. It is gitignored
# and is NOT part of the image, so a rebuild or `git pull` never touches it. This script adds the
# belt and braces: it refuses to deploy when the config is missing/invalid (instead of letting the
# app bootstrap an empty one), takes a verified backup first, and compares the config before/after.
set -euo pipefail

ROOT="${DASHBOARD_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
UNIT="${DASHBOARD_UNIT:-portfolio-dashboard}"
CFG="$ROOT/config/config.json"
SAMPLE="$ROOT/config/config.example.json"
MODE="deploy"
case "${1:-}" in
  --check) MODE="check" ;;
  --init)  MODE="init" ;;
  "")      ;;
  *) echo "usage: $0 [--check|--init]" >&2; exit 2 ;;
esac

die() { echo "deploy: $*" >&2; exit 1; }
cd "$ROOT"

# --- 1. config present? ------------------------------------------------------------------------
if [ ! -f "$CFG" ]; then
  if [ "$MODE" = "init" ]; then
    [ -f "$SAMPLE" ] || die "sample $SAMPLE missing"
    mkdir -p "$ROOT/config"
    cp "$SAMPLE" "$CFG"
    echo "deploy: seeded $CFG from the SAMPLE (fictional positions) - edit it or use Settings in the UI."
  else
    latest="$(ls -1t "${DASHBOARD_BACKUP_LOCAL:-$HOME/backups/portfolio-dashboard}"/pd-*.tar.zst 2>/dev/null | head -n1 || true)"
    echo "deploy: $CFG is MISSING - refusing to deploy (the app would start with an empty watchlist)." >&2
    [ -n "$latest" ] && echo "        restore your config:  bash ops/restore-config.sh   (latest backup: $latest)" >&2
    echo "        new machine:           bash ops/deploy.sh --init" >&2
    exit 1
  fi
fi

# --- 2. config valid? --------------------------------------------------------------------------
python3 - "$CFG" "$SAMPLE" <<'PY' || die "config preflight failed (see above)"
import json, sys
cfg_path, sample_path = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(cfg_path, encoding="utf-8"))
except Exception as e:
    sys.exit(f"config.json is not valid JSON: {e}")
if not isinstance(cfg, dict):
    sys.exit("config.json root must be an object")
wl = cfg.get("watchlist")
if not isinstance(wl, list) or not wl:
    sys.exit("config.json has an empty/missing watchlist")
ids = {e.get("id") for e in wl if isinstance(e, dict)}
pf = cfg.get("portfolio") or {}
orphans = sorted(set(pf) - ids)
if orphans:
    sys.exit(f"portfolio positions without a watchlist entry: {orphans}")
try:
    sample = json.load(open(sample_path, encoding="utf-8"))
    if pf and pf == sample.get("portfolio"):
        print("WARNING: config.json portfolio is identical to the SAMPLE (fictional) positions", file=sys.stderr)
except Exception:
    pass
print(f"config ok: {len(ids)} watchlist entries, {len(pf)} positions")
PY

[ -f "$ROOT/env.secrets" ] || die "env.secrets missing (the unit's EnvironmentFile)"
[ "$MODE" = "check" ] && { echo "deploy: preflight passed"; exit 0; }

BEFORE="$(mktemp)"; trap 'rm -f "$BEFORE"' EXIT
cp "$CFG" "$BEFORE"

# --- 3. verified snapshot (config/, data/, results/) --------------------------------------------
echo "deploy: snapshot..."
SNAP="$(bash "$ROOT/ops/backup.sh" | sed -n 's/^local backup: \([^ ]*\).*/\1/p')"
[ -n "$SNAP" ] && [ -s "$SNAP" ] || die "snapshot failed - aborting before touching anything"
echo "deploy: snapshot at $SNAP"

# --- 4. build + restart ------------------------------------------------------------------------
systemctl --user daemon-reload
echo "deploy: building image..."
systemctl --user start "$UNIT-build.service"
echo "deploy: restarting $UNIT..."
systemctl --user restart "$UNIT"

ok=0
for _ in $(seq 1 60); do
  if podman healthcheck run "$UNIT" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ "$ok" = 1 ] || die "pod did not become healthy in 3 min - check: podman logs --tail 50 $UNIT ; config snapshot: $SNAP"

# --- 5. prove the config survived -----------------------------------------------------------------
python3 - "$BEFORE" "$CFG" <<'PY' || die "CONFIG CHANGED during deploy - restore with: bash ops/restore-config.sh $SNAP"
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8"))
b = json.load(open(sys.argv[2], encoding="utf-8"))
if a != b:
    sys.exit("config.json differs before/after the deploy")
print("config unchanged by the deploy")
PY

if [ -f "$ROOT/env.secrets" ]; then
  set -a; . "$ROOT/env.secrets"; set +a
  if [ -n "${DASHBOARD_USER:-}" ] && [ -n "${DASHBOARD_PASS:-}" ]; then
    n="$(curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASS" http://127.0.0.1:8601/api/portfolio |
         python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d["positions"]))' 2>/dev/null || echo "?")"
    echo "deploy: live /api/portfolio reports $n positions"
  fi
fi
echo "deploy: done ($UNIT healthy, image $(podman inspect -f '{{.Image}}' "$UNIT" | cut -c1-12))"
