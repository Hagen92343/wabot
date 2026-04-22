#!/usr/bin/env bash
# bin/backup-db.sh — daily SQLite .backup of the whatsbot state DB.
#
# Spec §4 + §22:
#   - source: $WHATSBOT_DB         (default: ~/Library/Application Support/whatsbot/state.db)
#   - target: $WHATSBOT_BACKUP_DIR (default: ~/Backups/whatsbot)
#   - retention: $WHATSBOT_BACKUP_RETENTION_DAYS (default: 30)
#
# Triggered by the `com.<DOMAIN>.whatsbot.backup` LaunchAgent at 03:00.
# Output is one structured JSON line so it joins the rest of our logs cleanly
# (the LaunchAgent captures stdout into launchd-backup-stdout.log).
#
# The script is online-safe: `sqlite3 .backup` acquires a shared lock on the
# source DB, copies pages while the bot keeps running, and never blocks
# writers for more than a few ms per page (WAL mode).

set -euo pipefail

DB="${WHATSBOT_DB:-${HOME}/Library/Application Support/whatsbot/state.db}"
BACKUP_DIR="${WHATSBOT_BACKUP_DIR:-${HOME}/Backups/whatsbot}"
RETENTION_DAYS="${WHATSBOT_BACKUP_RETENTION_DAYS:-30}"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
date_tag="$(date +%Y-%m-%d)"

emit() {
  # $1 = event, remaining args = key=value pairs (values must already be
  # JSON-safe — we only use ASCII / paths).
  local event="$1"
  shift
  local extras=""
  for kv in "$@"; do
    local k="${kv%%=*}"
    local v="${kv#*=}"
    extras+=",\"$k\":$v"
  done
  printf '{"event":"%s","ts":"%s"%s}\n' "$event" "$ts" "$extras"
}

if [[ ! -f "$DB" ]]; then
  emit "backup_skipped_no_db" "db=\"$DB\""
  exit 0
fi

mkdir -p "$BACKUP_DIR"
target="$BACKUP_DIR/state.db.$date_tag"

# `VACUUM INTO` (SQLite 3.27+) is the right primitive for backups: single
# consolidated file, default journal mode (no -wal/-shm sidecars), and a
# read-consistent snapshot even with the bot writing concurrently. It's a
# strict superset of the older `.backup` API for our use case.
#
# We write into a `.tmp` and rename so concurrent reads (a previous
# backup-restore in flight, say) never see a half-written file.
tmp_target="${target}.tmp"
rm -f "$tmp_target" "${tmp_target}-wal" "${tmp_target}-shm"
sqlite3 "$DB" "VACUUM INTO '$tmp_target'"

if [[ ! -s "$tmp_target" ]]; then
  emit "backup_failed" "target=\"$target\"" >&2
  rm -f "$tmp_target"
  exit 1
fi

# Quick integrity check before we publish the backup.
integrity="$(sqlite3 "$tmp_target" "PRAGMA integrity_check;" 2>&1 | head -n1)"
if [[ "$integrity" != "ok" ]]; then
  emit "backup_integrity_failed" "target=\"$target\"" "result=\"$integrity\"" >&2
  rm -f "$tmp_target"
  exit 1
fi

mv -f "$tmp_target" "$target"

# Retention: delete files older than N days. find(1) handles the date math
# from mtime; macOS and Linux behave the same here.
deleted=0
while IFS= read -r -d '' f; do
  rm -f "$f"
  deleted=$((deleted + 1))
done < <(
  find "$BACKUP_DIR" -name 'state.db.*' -type f -mtime "+$RETENTION_DAYS" -print0 2>/dev/null
)

# stat formatting differs between BSD (macOS) and GNU.
size="$(stat -f %z "$target" 2>/dev/null || stat -c %s "$target")"

emit "backup_complete" \
  "target=\"$target\"" \
  "size_bytes=$size" \
  "retention_days=$RETENTION_DAYS" \
  "deleted_old=$deleted"
