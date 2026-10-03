#!/usr/bin/env bash
# Restore ONLY config/config.json (watchlist, positions, alert rules) from a backup archive.
#
#   ops/restore-config.sh                 newest archive in $DASHBOARD_BACKUP_LOCAL
#   ops/restore-config.sh path/to/pd-*.tar.zst
#
# A config that already exists is kept as config/config.json.before-restore-<stamp>. The pod picks the
# restored file up on its next read (it re-reads on mtime change); data/ and results/ are not touched.
set -euo pipefail

ROOT="${DASHBOARD_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ARCHIVE="${1:-$(ls -1t "${DASHBOARD_BACKUP_LOCAL:-$HOME/backups/portfolio-dashboard}"/pd-*.tar.zst 2>/dev/null | head -n1 || true)}"
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || { echo "restore: no archive found (pass one explicitly)" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar --zstd -C "$TMP" -xf "$ARCHIVE" config/config.json 2>/dev/null \
  || { echo "restore: $ARCHIVE has no config/config.json" >&2; exit 1; }
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); assert c.get("watchlist"), "empty watchlist"' "$TMP/config/config.json" \
  || { echo "restore: the archived config is not usable" >&2; exit 1; }

mkdir -p "$ROOT/config"
if [ -f "$ROOT/config/config.json" ]; then
  keep="$ROOT/config/config.json.before-restore-$(date +%Y%m%d-%H%M%S)"
  cp "$ROOT/config/config.json" "$keep"; echo "restore: kept current file as $keep"
fi
cp "$TMP/config/config.json" "$ROOT/config/config.json.new"
mv "$ROOT/config/config.json.new" "$ROOT/config/config.json"      # same-dir rename = atomic
echo "restore: config/config.json restored from $ARCHIVE"
