# Aktueller Stand

**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.7 (DB-Backup-Script)
**Letzter abgeschlossener Checkpoint**: C1.5 (Webhook + Echo) + C1.6 (Tests grün)

## Hinweis zu C1.6

C1.6 verlangt nur „`make test` grün + Coverage > 80% für domain/". C1.5
liefert das bereits mit:

- **128 Tests grün** (49 davon neu in C1.5)
- **Coverage 96.17% gesamt**
- `domain/` 100% (`commands.py`, `whitelist.py`, alle `__init__.py`)
- Kein neuer Code in C1.6 nötig — der Checkpoint ist mit C1.5 abgegolten.
- Markiert in CHANGELOG als "C1.5 + C1.6".

## Was als Nächstes zu tun ist (C1.7)

C1.7 laut `phase-1.md` §11 + §12:

1. `bin/backup-db.sh` echt machen (aktuell Stub):
   - `sqlite3 "$DB" ".backup '$BACKUP_DIR/state.db.$(date +%F)'"`
   - `find "$BACKUP_DIR" -name 'state.db.*' -mtime +30 -delete`
   - `set -euo pipefail`, structured JSON log line, exit 0/1 sauber
   - Idempotent: existing backup mit gleichem Datum überschreiben (oder
     skip with note)
2. `Makefile`: `backup-db` Target ruft `bin/backup-db.sh`
3. Tests: `tests/integration/test_backup_db.py` — schreibt Test-DB,
   triggert Skript, asserted dass Backup existiert + parsable, asserted
   dass alte Dateien (>30d simuliert) gelöscht werden
4. Live-Smoke: `make backup-db` schreibt nach `~/Backups/whatsbot/state.db.<heute>`,
   re-run löscht nichts (idempotent), file ist via `sqlite3 .schema` lesbar

Verifikation (C1.7 done):
- `make backup-db` → erzeugt `state.db.YYYY-MM-DD` mit current schema
- Re-run überschreibt sauber
- Test-30d-File wird beim nächsten Lauf gelöscht
- LaunchAgent `com.local.whatsbot.backup` läuft das Skript → kein stderr

## Phase-1-Endzustand nach C1.7

Phase 1 wäre dann komplett. Nach C1.7:
- Update `current-phase.md`: "Phase 2 — Projekt-Management + Smart-Detection"
- Sicherheitscheck: alle 12 Success-Criteria aus phase-1.md durchgehen
- User-Freigabe abwarten bevor Phase 2 startet

## Format-Konvention für Updates

Wenn du einen Checkpoint abschließt:

```
**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: noch keiner — Phase 1 komplett
**Letzter abgeschlossener Checkpoint**: C1.7 (DB-Backup)
```

Bei ganzem Phasen-Wechsel:

```
**Aktive Phase**: Phase 2 – Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: noch keiner
**Letzter abgeschlossener Checkpoint**: C1.7 (DB-Backup) — Phase 1 komplett
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-<N>.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
