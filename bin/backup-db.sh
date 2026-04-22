#!/usr/bin/env bash
# bin/backup-db.sh — daily SQLite .backup of state.db.
#
# C1.4 Stub. Real implementation lands in C1.7:
#   - sqlite3 "$DB" ".backup '$BACKUP_DIR/state.db.$(date +%F)'"
#   - find "$BACKUP_DIR" -mtime +30 -delete
#
# For now we just emit a structured log line so we can verify the
# StartCalendarInterval fired without touching the production DB.

set -euo pipefail

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"event":"backup_stub","ts":"%s","note":"C1.7 will implement real backup"}\n' "$ts"
exit 0
