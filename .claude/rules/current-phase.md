# Aktueller Stand

**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.7 (`/rm <name>` + PIN + Trash)
**Letzter abgeschlossener Checkpoint**: C2.4 + C2.5 (Allow-Rule-Management + Active-Project)

## Phase-2-Fortschritt: 5/8 Checkpoints

- ✅ C2.1 — `/new <name>` empty + `/ls`
- ✅ C2.2 — `/new <name> git <url>` + URL-Whitelist + Smart-Detection-Stub
- ✅ C2.3 — Smart-Detection für alle 9 Artefakt-Stacks
- ✅ C2.4 — `/allow batch approve` + `/allow batch review`
- ✅ C2.5 — `/allow <pat>` + `/deny <pat>` + `/allowlist` + `/p`/`/p <name>`
       (zusammen mit C2.4 abgeschlossen)
- ⏳ C2.6 — URL-Whitelist Tests (eigentlich schon in C2.2 voll abgedeckt;
       als separater Checkpoint nicht nötig — wird mit C2.7 zusammengezogen)
- ⏳ C2.7 — `/rm <n>` mit 60s-Confirm + PIN + Trash (folgt jetzt)
- ⏳ C2.8 — Tests grün + finale Phase-2-Verifikation

## Was als Nächstes zu tun ist (C2.7)

C2.7 laut `phase-2.md` "Trash-Mechanismus":

1. `domain/pending_deletes.py` — pure Logic für Deadline-Checks (60s)
2. `ports/pending_delete_repository.py` + sqlite-adapter (gegen
   `pending_deletes`-Tabelle, Spec §19)
3. `application/delete_service.py` — Use-Cases:
   - `request_delete(name)` → erzeugt pending_deletes-Row mit
     `deadline_ts = now + 60s`
   - `confirm_delete(name, pin)` → PIN gegen Keychain `panic-pin`,
     mv project to ~/.Trash/whatsbot-<name>-<timestamp>, DELETE row,
     CASCADE entfernt allow_rules etc.
   - `cleanup_expired()` → löscht abgelaufene pending_deletes-Rows
4. `command_handler.py`:
   - `/rm <name>` → request_delete
   - `/rm <name> <PIN>` → confirm_delete
5. PIN-Auth via existing `KeychainProvider.get(KEY_PANIC_PIN)`
6. Tests + Live-Smoke

Verifikation (C2.7 done):
- `/new alpha`
- `/rm alpha` → "🗑 Bestätige mit /rm alpha <PIN>"
- 70s warten → expired (oder via cleanup-trigger)
- `/rm alpha` (neu) + `/rm alpha <wrong-pin>` → "⚠️ falsche PIN"
- `/rm alpha <correct-pin>` → "🗑 Gelöscht (in Trash)"
- `~/.Trash/whatsbot-alpha-*` existiert
- `/ls` zeigt alpha nicht mehr
- `allow_rules` für alpha sind via CASCADE weg

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.8 (Tests grün + Phase-2-Verifikation)
**Letzter abgeschlossener Checkpoint**: C2.7 (`/rm` + PIN + Trash)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-2.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
