# Phase 11: `/import` — bestehende Projekte an den Bot anhängen

**Aufwand**: 1-2 Sessions (Mini-Phase nach Phase-10-Vorbild)
**Abhängigkeiten**: Phase 1-10 komplett ✅ (Bot produktiv, alle
Kern-Services stehen).
**Parallelisierbar mit**: —
**Spec-Referenzen**: §4 (Pfade + Projekt-Ordner-Konvention), §11
(Command-Referenz), §12 (Pfad-Rules Layer 3), §19 (DB-Schema), §21
Phase 2 (`/new`-Muster als Template).

## Ziel der Phase

**Bestehende Arbeitsordner fernsteuerbar machen.** Im MVP konnte
man Projekte nur per `/new <name>` (leerer Ordner in `~/projekte/`)
oder `/new <name> git <url>` (frischer Clone dorthin) anlegen. Für
den Alltag "ich hab 20 Repos auf dem Mac und will die vom Handy
aus steuern" fehlt der Import-Pfad. Phase 11 schließt das.

Neu: ein Command `/import <name> <absoluter-pfad>`, der:

1. Den existierenden Ordner unter beliebigem Pfad als Projekt
   registriert (DB-Row in `projects` mit dem tatsächlichen Pfad,
   nicht `projects_root / name`).
2. Die Projekt-Metadaten-Artefakte (`CLAUDE.md`, `.claudeignore`,
   `.claude/settings.json`, `.whatsbot/config.json`) idempotent
   anlegt, ohne existierende Dateien zu überschreiben.
3. Smart-Detection auf dem Bestands-Ordner laufen lässt und
   `suggested-rules.json` schreibt — genau wie bei `/new git`.

Phase 11 endet damit, dass:

- `/import wabot /Users/hagenmarggraf/whatsbot` den whatsbot-Repo
  selbst als Projekt fernsteuerbar macht.
- `/ls` listet importierte Projekte mit einer Marker-Spalte.
- `/p <name> <prompt>` funktioniert — Claude-Session öffnet im
  tatsächlichen Projekt-Pfad, Pre-Tool-Hook akzeptiert Writes im
  Projekt-Scope.
- Die Hook-Pfad-Rules brauchen **keine** Code-Änderung — sie
  arbeiten bereits mit `project_cwd`-Parameter (siehe
  `whatsbot/domain/path_rules.py::classify_path`). Einzige Aufgabe:
  `SessionService.ensure_started` muss den `project_cwd` aus der
  DB lesen statt `projects_root / name` zu bauen.

## Voraussetzungen

- Phase 1-10 komplett.
- Tests-Stand: 1572 passed + 1 live-skipped, mypy --strict clean.
- Grundverständnis von Spec §12 Layer 3 (Protected Paths bleiben
  verboten, d.h. `.git`, `.vscode`, `.idea`, `.claude` (außer
  `commands|agents|skills`) sind auch in importierten Projekten
  geschützt).

## Was gebaut wird

### 1. Schema-Migration

**Problem**: Die `projects`-Tabelle hat aktuell kein `path`-Feld.
Der Projekt-Pfad wird implicit als `projects_root / name`
berechnet. Außerdem hat `source_mode` einen CHECK-Constraint auf
`('empty', 'git')`.

**Lösung**: Eine explizite Migration via `PRAGMA user_version`:

- **Neue Migration-Infrastruktur**: `whatsbot/adapters/sqlite_repo.py`
  bekommt `run_migrations(conn)` analog zu `apply_schema`.
  Migrations-Dateien unter `sql/migrations/NNN_*.sql`, werden
  in Reihenfolge angewendet, Version per `PRAGMA user_version`.
  Fresh-DB setzt `user_version` direkt auf den latest-Wert nach
  `apply_schema` (keine Re-Runs der Migrations auf fresh-install).
- **Migration `001_project_path.sql`**:
  - `ALTER TABLE projects ADD COLUMN path TEXT` (nullable —
    Legacy-Rows behalten NULL, bedeutet "use projects_root/name").
  - Rebuild-Table-Pattern für `source_mode`: `'empty' | 'git' |
    'imported'`. Standard SQLite-Rename-Copy-Pattern (neue Tabelle
    mit erweitertem CHECK, INSERT SELECT, DROP old, RENAME).
- **Schema-Datei** (`sql/schema.sql`) wird parallel mit der
  Ziel-Struktur aktualisiert, damit Fresh-Installs direkt auf dem
  finalen Stand starten.

### 2. Domain

- **`whatsbot/domain/projects.py`**:
  - `Project`-Dataclass bekommt optional `path: Path | None`
    (default None). None bedeutet "berechne via projects_root".
  - Neuer Helper `resolved_path(project, projects_root) -> Path`
    (pure Funktion): return `project.path` wenn gesetzt, sonst
    `projects_root / project.name`.
  - `SourceMode`-Enum um `IMPORTED = "imported"` erweitern.
- **`whatsbot/domain/path_rules.py`** unverändert — arbeitet schon
  mit `project_cwd` und respektiert den von außen übergebenen
  Pfad. Das ist der Schlüssel, warum der Hook ohne Änderung
  funktioniert.

### 3. Port + Adapter

- **`ports/project_repository.py`**: `Project`-Dataclass-Change
  zieht durch, `add(project)` akzeptiert den optionalen `path`.
- **`adapters/sqlite_project_repository.py`**:
  - INSERT/UPDATE-Statements um `path`-Column erweitert.
  - Row-Mapping: `path=row["path"]` (NULL → None in Dataclass).
  - Keine andere Schema-abhängige Logik ändert sich.

### 4. Application

- **`application/project_service.py::import_existing(name, path)`**:
  - Validation:
    - `name` via `validate_project_name` (gleiches Pattern wie
      `/new`).
    - `path` muss absolut sein, existieren, ein Verzeichnis sein.
    - Deny-List geschützter Pfade: `~/Library`, `~/.ssh`,
      `~/.aws`, `~/.gnupg`, `/etc`, `/System`, `/Library`, `/usr`.
    - Warn-List TCC-protected Pfade: `~/Desktop`, `~/Documents`,
      `~/Downloads`, `~/Pictures`. Import darf trotzdem, aber
      Reply enthält klare Warnung: "Full Disk Access könnte nötig
      sein, siehe docs/OPERATING.md".
    - Kein Projekt mit diesem Namen darf bereits existieren.
    - Kein anderes Projekt mit demselben Pfad darf existieren
      (Idempotenz-Schutz).
  - DB-Row einfügen mit `source_mode='imported'`,
    `source=str(path)`, `path=path`, `mode='normal'`.
  - Artefakte idempotent anlegen:
    - `CLAUDE.md` nur wenn nicht vorhanden (sonst: Reply-Hinweis).
    - `.claudeignore` nur wenn nicht vorhanden.
    - `.claude/settings.json`: Merge-Pattern. Wenn existierend:
      nicht überschreiben, stattdessen die whatsbot-Default-
      Permissions (§12 Deny-Rules) anhängen falls fehlend.
    - `.whatsbot/config.json` anlegen wenn nicht vorhanden.
  - Smart-Detection laufen lassen → `suggested-rules.json`.
  - Reply-Shape: ähnlich `/new git`, plus "Importiert von: `<path>`"
    Zeile und ggf. Warnings.

### 5. Pfad-Helper-Refactoring

Alle Stellen, die bisher `projects_root / name` hardcoded gebaut
haben, müssen jetzt `resolved_path(project, projects_root)`
verwenden:

- `application/session_service.py::ensure_started` Line ~122
  (project_path = …).
- `application/kill_service.py` wenn es Projekt-Pfade referenziert.
- `application/delete_service.py` — Trash-Move muss den
  tatsächlichen Pfad verschieben, nicht `projects_root / name`
  blind. **Achtung**: Importierte Projekte NICHT löschen! Nur
  DB-Row entfernen, den Bestands-Ordner beim User lassen.
  Neuer Parameter `physically_delete: bool` in `delete_service`
  (default False für imported, True für empty/git).
- `application/post_clone.py` arbeitet mit beliebigen Pfaden,
  sollte agnostisch sein — Check.

### 6. Command-Handler

- **`application/command_handler.py::_handle_import`**:
  - Parse: `/import <name> <absoluter-pfad>`. Leerzeichen im
    Pfad via Quoting nicht nötig (User kann `/import a /Users/x`
    tippen, Rest als Pfad nehmen via `split(maxsplit=1)`).
  - Kein PIN-Gating (Spec §5 sagt PIN nur für destruktive Ops,
    `/import` ist rein addierend).
  - `/import` ohne Args → Usage-Hint.
  - `/import <name>` ohne Pfad → Usage-Hint.
  - Validation-Errors friendly rendern.
- **`/ls`-Darstellung**: Importierte Projekte zeigen ihren Pfad
  statt nur den Namen: `wabot [imported: ~/whatsbot]`.
  Reguläre Projekte bleiben wie bisher (`scratch [empty]`).
- **`/rm`-Flow**: bei `source_mode='imported'` die Reply-Message
  anpassen: "Projekt-Eintrag entfernt. Ordner bleibt unter
  `<path>`" statt "in Trash verschoben".

### 7. Tests

- **Domain**: 3 Tests für `resolved_path` (explicit path, default
  from root, Path vs None).
- **Project-Repository**: 2 Tests (insert mit path, insert ohne path).
- **ProjectService.import_existing**: 10+ Tests für die
  Validation-Matrix (nicht-absolut, nonexistent, protected,
  TCC-warn, Name bereits belegt, Pfad bereits belegt, existierende
  Artefakte nicht überschreiben, happy-path).
- **Command-Handler**: 5+ Tests (usage-Hints, Validation-Fehler,
  happy-path, idempotenz bei Re-Import).
- **DeleteService**: 2 neue Tests für imported (DB-Row weg, Ordner
  bleibt).
- **Integration/E2E**: 1 Test via signed `/webhook` mit einem
  tmp_path als Import-Ziel, danach `/ls` + `/p` + `/rm`.
- **Migration-Test**: 1 Test der eine DB ohne die neue Column
  simuliert und die Migration laufen lässt, verifiziert
  `user_version`-Bump + Column-Existenz + bestehende Rows intact.

### 8. Docs

- **`docs/CHEAT-SHEET.md`**: `/import`-Zeile im Projekt-Management-
  Block.
- **`docs/OPERATING.md`**: Abschnitt "Weg B — Symlink-Trick" wird
  durch "Weg 2 — `/import`-Command" ersetzt; Symlink wird als
  Legacy-Workaround zurückgestuft.
- **`docs/INSTALL.md`**: kleiner Hinweis, dass zum Steuern
  bestehender Ordner `/import` der Weg ist.
- **`CLAUDE.md`** (Projekt-Template): keine Änderung nötig — gilt
  für importierte Projekte genauso.

## Checkpoints

### C11.1 — Migration-Framework + Schema-Änderung

- `sql/migrations/001_project_path.sql` angelegt.
- `sqlite_repo.py::run_migrations(conn)` + `PRAGMA user_version`-
  Bookkeeping.
- `apply_schema` setzt nach Fresh-Install die
  `user_version` direkt auf latest.
- Migration-Test grün.
- Bestehende Live-DB upgradet sauber beim nächsten Bot-Restart
  (manuell verifizieren: Backup vorher, `user_version`-Check
  nachher).

### C11.2 — Domain + Repo-Änderungen

- `Project`-Dataclass mit optionalem `path`.
- `SourceMode.IMPORTED`.
- `resolved_path`-Helper.
- SQLite-Repo liest/schreibt `path`.
- Domain-Tests + Repo-Tests grün.

### C11.3 — `ProjectService.import_existing` + `SessionService`-Pfad-Lookup

- `import_existing` mit voller Validation + Artefakt-Handling.
- `SessionService.ensure_started` zieht den cwd über
  `resolved_path` aus der Project-Row.
- Unit-Tests grün.

### C11.4 — Command + CommandHandler + `/ls`/`/rm` anpassen

- `/import` route.
- `/ls` zeigt Imported-Pfad.
- `/rm` für Imported ist non-destructive (nur DB-Row).
- Command-Handler-Tests grün.

### C11.5 — Docs + E2E

- CHEAT-SHEET, OPERATING, INSTALL aktualisiert.
- E2E-Test via /webhook über den kompletten Import→Start→Prompt→Kill→Remove-Zyklus.
- CHANGELOG-Eintrag.
- Abschluss-Commit.

### C11.6 — Live-Verifikation mit echtem Bestands-Ordner

- Auf dem Live-Bot: `/import wabot /Users/hagenmarggraf/whatsbot`
  vom Handy.
- `/ls` zeigt die Zeile mit Pfad.
- `/p wabot zeig mir die letzten 3 Commits` → Claude listet
  aus dem echten Repo.
- `/p wabot` ist tatsächlich im echten Pfad (Verifikation:
  `tmux attach -t wb-wabot` → `pwd` zeigt
  `/Users/hagenmarggraf/whatsbot`).
- `/rm wabot` entfernt den Eintrag ohne den Ordner zu löschen.

## Success Criteria

- [ ] Schema-Migration läuft einmalig beim Bot-Restart gegen die
      Live-DB, bumpt `user_version`, bestehende Daten intact.
- [ ] `/import <name> <path>` registriert einen Bestands-Ordner.
- [ ] Validation fängt alle in §4 genannten Fehler-Pfade.
- [ ] Importierte Projekte sind voll nutzbar via `/p`, Claude
      arbeitet im tatsächlichen Pfad, Pre-Tool-Hook greift mit
      `project_cwd` = tatsächlicher Pfad.
- [ ] `/rm` löscht für importierte Projekte nur die DB-Row, nicht
      den Ordner.
- [ ] Tests-Stand: ≥ 1600 passed (+~30 neue Tests).
- [ ] mypy --strict + ruff clean.
- [ ] `docs/OPERATING.md` + CHEAT-SHEET + INSTALL spiegeln den
      neuen Flow.
- [ ] Live-Test (C11.6) bestätigt End-to-End am Bestands-Repo.

## Abbruch-Kriterien

- **Migration läuft nicht clean auf der Live-DB** (Spalten-
  ALTER-TABLE + CHECK-rebuild in SQLite WAL): Stop. Backup
  restoren. Migration in Test-DB erst debuggen. Tools-Pattern
  für `sqlite3-dump → sed → sqlite3 <` als Fallback dokumentieren.
- **`SessionService` verwendet `project_name` statt `project_cwd`
  an anderen Stellen**, die wir übersehen haben: Stop. `grep -rn
  "projects_root" whatsbot/` im Scope, jede Stelle anfassen und
  über den Helper leiten.
- **TCC-Protection in `~/Desktop` etc. blockt tatsächlich Writes**,
  obwohl der Hook allow sagt: Stop. Der LaunchAgent braucht
  Full-Disk-Access — Install-Doc-Hinweis nachziehen, Eintrag in
  `docs/TROUBLESHOOTING.md`.
- **Importiertes Projekt hat Claude-Config, die mit unserem
  Default-Hook kollidiert** (z.B. eigene `PreToolUse`-Hook):
  Stop. Merge-Strategie dokumentieren — beide Hooks laufen
  lassen (Claude unterstützt mehrere), keine Überschreibung.

## Was in Phase 11 NICHT gebaut wird

- **`/export`** (umgekehrter Vorgang: bot-managed Projekt in einen
  anderen Pfad verschieben) — nicht nötig, `/rm` + externer
  `mv`-Befehl reichen.
- **Symlink-Erkennung**: wir prüfen `path` as-is; wenn der User
  einen Symlink reingibt, arbeitet tmux dort, Claude sieht den
  realen Pfad via `pwd -P` — das ist gewollt.
- **Multi-Root-Projekte** (z.B. monorepo-Workspace): Scope außer
  Reichweite. Ein Projekt = ein Root-Verzeichnis.
- **DB-Auto-Sync mit externer `.whatsbot/config.json`** (wenn der
  User die Datei manuell editiert): nicht im MVP. DB bleibt
  Single-Source-of-Truth.

## Architektur-Hinweise

- Die Hook-Pfad-Rules in `whatsbot/domain/path_rules.py` sind
  bereits richtig flexibel — sie nehmen `project_cwd` als
  Parameter. Hook-Endpoint zieht den cwd aus der
  `claude_sessions`/`projects`-Kopplung. Diese Kette muss nur den
  resolved-path verwenden, dann funktioniert alles.
- Die Migration ist der riskanteste Teil. Strategie:
  1. Backup via `bin/backup-db.sh` vor dem Upgrade (manuell + als
     Auto-Check im Bot-Startup).
  2. Migration läuft in Transaction — Rollback bei Fehler.
  3. `PRAGMA integrity_check` nach Migration.
  4. Nur wenn grün: `user_version` bumpen.
- `/import` ist **idempotent by design**: wenn die DB-Row schon
  existiert, Reply mit klarer Info + kein Artefakt-Overwrite. Der
  User kann den gleichen Pfad mehrfach versuchen ohne Datenverlust.
- Test-Strategie spiegelt Phase 2 (`/new`) und Phase 10 (`/send`-
  Adapter) — gleiche Struktur, viele Unit-Tests + ein E2E.

## Nach Phase 11

1. `.claude/rules/current-phase.md` aktualisieren: "Phase 11
   komplett ✅ — bestehende Projekte importierbar".
2. CHANGELOG.md mit Phase-11-Abschnitt.
3. Commit `feat(phase-11): /import — bestehende Projekte
   anhängen`.
4. Push.
5. Live-Verifikation C11.6 (siehe oben).
6. Memory-Update (`project_wabot.md`).
