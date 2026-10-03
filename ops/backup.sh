#!/usr/bin/env bash
# Portfolio-dashboard state backup: config (portfolio positions), data/ and
# results/. env.secrets is deliberately NOT included (secrets stay out of
# backups that land on a shared NAS); re-create it from the password manager.
#
# Writes a timestamped tar.zst to a local dir. If DASHBOARD_BACKUP_DEST is set
# it also copies the archive there (e.g. a NAS share) - when DASHBOARD_NAS_MOUNT
# is set too, only if that mount is alive. Keeps the newest $KEEP archives in each place.
set -euo pipefail

SRC="${DASHBOARD_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCAL="${DASHBOARD_BACKUP_LOCAL:-$HOME/backups/portfolio-dashboard}"
NAS_MOUNT="${DASHBOARD_NAS_MOUNT:-}"      # optional mountpoint that must be alive
NAS_DEST="${DASHBOARD_BACKUP_DEST:-}"     # optional second copy; empty = local only
KEEP="${DASHBOARD_BACKUP_KEEP:-21}"

stamp="$(date +%Y%m%d-%H%M%S)"
archive="$LOCAL/pd-$stamp.tar.zst"
mkdir -p "$LOCAL"

members=()
for p in config config.json data results; do
  [ -e "$SRC/$p" ] && members+=("$p")
done
[ "${#members[@]}" -gt 0 ] || { echo "nothing to back up under $SRC" >&2; exit 1; }

tar --zstd -C "$SRC" -cf "$archive.part" "${members[@]}"
zstd -t -q "$archive.part"          # refuse to keep an unreadable archive
mv "$archive.part" "$archive"
echo "local backup: $archive ($(du -h "$archive" | cut -f1))"

if [ -n "$NAS_DEST" ]; then
  if [ -z "$NAS_MOUNT" ] || mountpoint -q "$NAS_MOUNT"; then
    mkdir -p "$NAS_DEST"
    cp "$archive" "$NAS_DEST/"
    echo "second copy: $NAS_DEST/$(basename "$archive")"
    ls -1t "$NAS_DEST"/pd-*.tar.zst 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
  else
    echo "mount $NAS_MOUNT not alive: kept local copy only" >&2
  fi
fi

ls -1t "$LOCAL"/pd-*.tar.zst 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
