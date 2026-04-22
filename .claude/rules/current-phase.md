# Aktueller Stand

**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.2 (`/new <name> git <url>` mit Smart-Detection)
**Letzter abgeschlossener Checkpoint**: C2.1 (`/new <name>` empty + `/ls`)

## Was C2.1 geliefert hat

- `Project` Domain-Model + Validierung (filesystem-/router-safe Namen)
- `ProjectRepository` Port + SQLite-Adapter (CRUD gegen `projects` Tabelle)
- `ProjectService` Use-Cases (`create_empty`, `list_all`, mit FS+DB rollback)
- `CommandHandler` mit Dispatch für `/new`, `/ls` (+ Pass-through für Phase-1
  Commands)
- 66 neue Tests (201 total), Coverage 95.30%
- Live-verifiziert: tmp-DB + tmp-`~/projekte/`, alle Edge-Cases sauber

## Was als Nächstes zu tun ist (C2.2)

C2.2 laut `phase-2.md` §"Git-Clone-Logik" + §"CLAUDE.md-Template":

1. **URL-Whitelist** für git-Clone:
   - github.com / gitlab.com / bitbucket.org via `https://`, `git@`, `ssh://`
   - Andere Hosts → klare Ablehnung
2. **`/new <name> git <url>`** im CommandHandler aktiv (statt C2.2-Hint):
   - Validiert URL-Whitelist
   - Validiert Project-Name (gleiche Regeln wie /new empty)
   - `git clone --depth 50 <url> ~/projekte/<name>` mit 180s timeout
   - Bei Fehler: cleanup + klare Fehlermeldung
   - Persist mit `source_mode='git'`, `source=<url>`
3. **Post-Clone-Steps**:
   - `.claudeignore` aus Template generieren (Spec §12 Layer 5)
   - `.whatsbot/config.json` mit Projekt-Metadaten anlegen
   - `CLAUDE.md`-Template generieren
4. **Smart-Detection-Stub** (vorbereiten, aber nur 1-2 Stacks für C2.2;
   alle 9 kommen in C2.3):
   - `domain/smart_detection.py` mit Scanner für package.json + .git
   - Output: `~/projekte/<name>/.whatsbot/suggested-rules.json`
5. Tests:
   - `test_smart_detection.py` (für die 1-2 Stacks)
   - `test_url_whitelist.py` (allowed/denied URLs)
   - Integration: `test_command_handler` für `/new git` mit echter
     Git-Clone-Mock

Verifikation (C2.2 done):
- `/new testgit git https://github.com/octocat/Hello-World.git` (echter
  öffentlicher Clone, klein) → 200 OK + Projekt im /ls + dir mit .git
- `/new badurl git https://evil.example.com/x` → 🚫 Ablehnung
- `~/projekte/testgit/.claudeignore` existiert
- `~/projekte/testgit/.whatsbot/suggested-rules.json` existiert

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.3 (Smart-Detection für 9 Artefakte)
**Letzter abgeschlossener Checkpoint**: C2.2 (`/new git` + URL-Whitelist)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-2.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
