# Aktueller Stand

**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.2 (Keychain + DB)
**Letzter abgeschlossener Checkpoint**: C1.1 (Repo-Struktur + Python-Setup)

## Was als Nächstes zu tun ist

C1.2 laut `phase-1.md` §3 + §4:

1. `ports/secrets_provider.py` mit `SecretsProvider`-Protocol (get/set/rotate)
2. `adapters/keychain_provider.py` mit `keyring`-Library, Service `"whatsbot"`,
   die 7 Secret-Keys aus Spec §4 als Konstanten
3. `bin/setup-secrets.sh` interaktiver Prompt für alle 7 Einträge
4. `sql/schema.sql` aus Spec §19 (alle Tabellen + Indizes + Constraints)
5. SQLite-Connection-Helper mit den 4 PRAGMAs (WAL, synchronous=NORMAL,
   busy_timeout=5000, foreign_keys=ON)
6. Startup-Hook: `PRAGMA integrity_check`, bei Fehler Auto-Restore aus
   `~/Backups/whatsbot/state.db.<yesterday>`, sonst harter Abbruch
7. Tests: `tests/unit/test_secrets.py` mit Mock-Keychain,
   `tests/unit/test_db_init.py` mit In-Memory SQLite

## Format-Konvention für Updates

Wenn du einen Checkpoint abschließt, update diese Datei so:

```
**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.3 (Fixture-Test)
**Letzter abgeschlossener Checkpoint**: C1.2 (Health-Endpoint)
```

Wenn du eine ganze Phase abschließt:

```
**Aktive Phase**: Phase 2 – Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: noch keiner
**Letzter abgeschlossener Checkpoint**: C1.5 (DB-Backup-Script) — Phase 1 komplett
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-<N>.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
