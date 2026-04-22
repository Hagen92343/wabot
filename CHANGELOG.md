# Changelog

Alle nennenswerten Änderungen am `whatsbot`-Repo. Format: phasen-/checkpoint-basiert,
neueste oben. Sieh dazu `.claude/rules/current-phase.md` für den Live-Stand.

## [Unreleased]

### Phase 2 — Projekt-Management + Smart-Detection (in progress)

#### C2.7 — `/rm` mit 60s-Fenster, PIN + Trash ✅

- **`whatsbot/domain/pending_deletes.py`**: pure Dataclass `PendingDelete`
  mit `is_expired` + `seconds_left`. Konstante `CONFIRM_WINDOW_SECONDS = 60`
  wird vom Handler geteilt, damit Text und DB-Deadline nicht auseinanderlaufen
  können. `compute_deadline(now_ts, window)` als freies Helper, verweigert
  negative Fenster.
- **`whatsbot/ports/pending_delete_repository.py`** + **`adapters/sqlite_pending_delete_repository.py`**:
  UPSERT (zweites `/rm` vor Ablauf resettet nur die Deadline), `get`,
  `delete` (bool), `delete_expired(now_ts)` für Sweeper. Gegen die
  `pending_deletes`-Tabelle aus Spec §19, die keine FK zu `projects` hat —
  der Service ist für das Cleanup zuständig.
- **`whatsbot/application/delete_service.py`**:
  - `request_delete(name)` validiert Name + Existenz, setzt Deadline,
    upserted Row, gibt `PendingDelete` zurück.
  - `confirm_delete(name, pin)` prüft: Pending-Row existiert →
    Deadline nicht abgelaufen (abgelaufen räumt stale Row direkt weg) →
    PIN via `hmac.compare_digest` gegen Keychain `panic-pin` → `mv`
    Projekt-Tree nach `~/.Trash/whatsbot-<name>-<YYYYMMDDTHHMMSS>`
    (mit Kollisions-Suffix falls exakt gleiche Sekunde) → `projects`-Row
    löschen (CASCADE wipet `allow_rules`, `claude_sessions`, `session_locks`)
    → pending Row wegräumen → aktives Projekt clearen wenn es der gelöschte
    Name war.
  - `cleanup_expired()` für späteren Sweeper-Einsatz.
  - Fünf distinkte Exception-Klassen (`NoPendingDeleteError`,
    `PendingDeleteExpiredError`, `InvalidPinError`, `PanicPinNotConfiguredError`
    + bestehende `ProjectNotFoundError` / `InvalidProjectNameError`) —
    der Command-Handler mappt sie in unterschiedliche WhatsApp-Replies.
  - Clock ist injizierbar (`clock: Callable[[], int]`), Tests simulieren
    die 60s-Frist deterministisch statt mit `time.sleep`.
- **`whatsbot/application/command_handler.py`**: `/rm <name>` + `/rm <name>
  <PIN>` routen zu Request bzw. Confirm. Ein-Argument-Fall listet die 60s
  im Reply, Wrong-PIN und Expired liefern getrennte Emojis (`⚠️` / `⌛`).
  `/rm` ohne Argumente fällt wie `/new` auf den Pure-Router als `<unknown>`
  durch (Arity-Match via Prefix).
- **`whatsbot/main.py`**: `DeleteService` wird gewired, `SqliteAppStateRepository`
  wandert aus der Active-Project-Initialisierung in eine geteilte Variable
  (Delete-Service braucht sie für den Active-Project-Clear).
- Tests (26 neu, 373 total): `test_pending_deletes` (5),
  `test_sqlite_pending_delete_repository` (8), `test_delete_service` (13),
  `/rm`-Abschnitt in `test_command_handler` (10). Abgedeckt:
  Expired-Window mit gestepptem Clock, Wrong-PIN behält Pending-Row,
  CASCADE wiped `allow_rules`, aktives Projekt wird gecleart, fehlende
  Panic-PIN surfaced als klare Fehlermeldung statt stillschweigend jede
  PIN akzeptieren, missing Project-Dir (User hat manuell gelöscht) führt
  trotzdem zu cleanem DB-Confirm. mypy strict grün.
- **Live-Smoke**: noch ausstehend (wird mit C2.8 zusammen gemacht).

#### C2.4 / C2.5 — Allow-Rule-Management + `/p` Active-Project ✅
*(C2.4 + C2.5 zusammen abgehandelt — die Manual-Rules-Commands aus C2.5 fielen
beim Wiren des batch-Flows quasi mit ab.)*

- **`whatsbot/domain/allow_rules.py`**: pure Pattern-Logik. `parse_pattern`
  konsumiert `Tool(pattern)`, validiert gegen `ALLOWED_TOOLS = {Bash, Write,
  Edit, Read, Grep, Glob}`, lehnt unbalancierte Klammern + leere Patterns ab.
  `format_pattern` für Round-Trip + WhatsApp-Output. `AllowRuleSource`
  StrEnum (default / smart_detection / manual) matcht den
  Spec-§19-CHECK-Constraint.
- **`whatsbot/ports/allow_rule_repository.py`** + **`adapters/sqlite_allow_rule_repository.py`**:
  Idempotentes `add` (Duplikat → bestehende Row zurück), `remove` mit
  Boolean-Indikator, `list_for_project` in Insertion-Reihenfolge.
- **`whatsbot/ports/app_state_repository.py`** + **`adapters/sqlite_app_state_repository.py`**:
  Kleines Key/Value gegen die `app_state`-Tabelle mit reservierten Keys
  (`active_project`, `lockdown`, `version`, `last_heartbeat`). UPSERT via
  `ON CONFLICT(key) DO UPDATE`.
- **`whatsbot/application/settings_writer.py`**: schreibt das per-Projekt
  `.claude/settings.json` atomar (tmp + `os.replace`), bewahrt andere Top-
  Level-Keys (`hooks` etc.) und überschreibt nur `permissions.allow`.
- **`whatsbot/application/active_project_service.py`**: 2 Methoden,
  `get_active` heilt sich selbst wenn die persistierte Auswahl auf ein
  gelöschtes Projekt zeigt; `set_active` validiert + checkt Existenz.
- **`whatsbot/application/allow_service.py`**: orchestriert die drei
  Storage-Layer (DB, settings.json, `.whatsbot/suggested-rules.json`).
  Use-Cases: `add_manual`, `remove`, `list_rules`, `batch_review` (read-
  only), `batch_approve` (idempotent: bereits vorhandene Rules werden
  nicht doppelt geschrieben, klassifiziert in `added` vs. `already_present`,
  am Ende ein `_sync_settings`-Call statt N Calls).
- **`whatsbot/application/command_handler.py`** erweitert um:
  - `/p` (zeigt aktives Projekt) und `/p <name>` (setzt aktiv)
  - `/allowlist` (gruppiert nach Source: default / smart_detection / manual)
  - `/allow <pattern>` (manual single-rule add)
  - `/deny <pattern>` (manual single-rule remove)
  - `/allow batch approve` (übernimmt suggested-rules.json komplett)
  - `/allow batch review` (nummerierte Liste der offenen Vorschläge)
  - `/ls` markiert das aktive Projekt jetzt mit `▶`.
- **`whatsbot/main.py`**: `AllowService` + `ActiveProjectService` werden
  beim Bot-Start gewired; CommandHandler bekommt sie via DI.
- Tests (76 neu, 336 total): `test_allow_rules` (16), `test_sqlite_allow_rule_repository`
  (10), `test_sqlite_app_state_repository` (6), erweiterte
  `test_command_handler` (16 neue Tests für `/p`, `/allow`, `/deny`,
  `/allowlist`, batch-Flows). **Coverage 93.77%**, mypy strict + ruff
  format/lint clean.
- **Live-Smoke verifiziert** (echter Clone von `octocat/Hello-World`):
  ```
  /p                       → "kein aktives Projekt"
  /new hello git ...       → geklont, 7 .git-Vorschläge
  /p hello                 → "▶ aktiv: hello"
  /ls                      → "▶ 🟢 hello (git)"
  /allow batch review      → 7 nummerierte Vorschläge
  /allow batch approve     → "✅ 7 neue Rules" + Datei gelöscht
  /allowlist               → 7 Einträge unter [smart_detection]
  /allow Bash(make test)   → "✅ Rule hinzugefügt"
  /allowlist               → 7 + 1 unter [smart_detection] / [manual]
  /deny Bash(make test)    → "🗑 Rule entfernt"
  ```
  `~/projekte/hello/.claude/settings.json` enthält stets exakt die aktuelle
  `permissions.allow`-Liste, `~/projekte/hello/.whatsbot/suggested-rules.json`
  ist nach `batch approve` weg.

#### C2.3 — Smart-Detection für alle 9 Artefakt-Stacks ✅
- `whatsbot/domain/smart_detection.py` erweitert von 2 auf alle
  9 Artefakte aus Spec §6 / phase-2.md:
  - `yarn.lock` → 3 yarn-Rules
  - `pnpm-lock.yaml` → 2 pnpm-Rules
  - `pyproject.toml` → 5 Python-Tooling-Rules (uv, pytest, python -m, ruff, mypy)
  - `requirements.txt` → 3 pip-Rules
  - `Cargo.toml` → 5 cargo-Rules (build/test/check/clippy/fmt)
  - `go.mod` → 4 go-Rules
  - `Makefile` → 1 make-Rule
  - `docker-compose.yml` / `docker-compose.yaml` → 4 docker-compose-Rules
- Detection-Reihenfolge ist stabil (file-Artefakte in
  Deklarationsreihenfolge, dann docker-compose, dann `.git/` als letztes)
  damit die WhatsApp-Listing-Ausgabe lesbar bleibt.
- `_ARTEFACT_RULES`-Dict + `_rules_for()`-Helper ersetzen die
  C2.2-tuple-per-artefact-Pattern; neue Stacks lassen sich künftig in
  einer Zeile ergänzen.
- Defensive Guards: jedes Datei-Artefakt MUSS eine Datei sein (kein
  Verzeichnis mit dem gleichen Namen → kein Match), `.git` MUSS ein
  Verzeichnis sein (Submodul-Pointer-Datei `gitdir: ../...` matcht NICHT).
- Tests: 14 neue Tests in `test_smart_detection.py`. Coverage pro Stack
  + Combo-Cases (Python+Make+Compose+git → 17 Rules), Listing-Order-Test,
  Universal-Bash-Tool-Check, parametrisierter "muss Datei sein"-Guard.
  **280 Tests grün, Coverage 95.17%**.

#### C2.2 — `/new <name> git <url>` + URL-Whitelist + Smart-Detection-Stub ✅
- `whatsbot/domain/git_url.py`: URL-Whitelist (Spec §13). Pure Validation,
  drei Schemas (https / git@ / ssh://), drei Hosts (github / gitlab /
  bitbucket). Lehnt http://, ftp://, file:// und Shell-Injection-Versuche
  ab. `DisallowedGitUrlError` mit klarer Fehlermeldung.
- `whatsbot/domain/smart_detection.py`: C2.2-Subset des Scanners aus
  `phase-2.md`. Erkennt `package.json` (5 npm-Rules) und `.git/` (7
  git-Rules). Restliche 7 Stacks (yarn, pnpm, pyproject, requirements,
  Cargo, go.mod, Makefile, docker-compose) kommen in C2.3.
- `whatsbot/ports/git_clone.py`: `GitClone` Protocol mit
  `clone(url, dest, depth=50, timeout_seconds=180.0)`. `GitCloneError`
  für alle Failure-Modes (timeout / non-zero exit / git missing).
- `whatsbot/adapters/subprocess_git_clone.py`: echte
  `subprocess.run(["git", "clone", "--depth", "<n>", "--quiet", url, dest])`
  Implementation. stderr-Tail (500 chars) im Error-Output. Konstruierbar
  mit alternativem `git_binary` für Tests.
- `whatsbot/application/post_clone.py`: 4 reine Schreib-Funktionen für
  Post-Clone-Scaffolding (`.claudeignore` mit Spec-§12-Layer-5 Patterns,
  `.whatsbot/config.json`, `CLAUDE.md` Template **nur wenn upstream-Repo
  keines mitbringt**, `.whatsbot/suggested-rules.json` aus
  `DetectionResult` wenn Rules vorhanden).
- `whatsbot/application/project_service.py`: neuer Use-Case
  `create_from_git(name, url) -> GitCreationOutcome`. Ablauf: validate
  name + URL → reserve path → `git clone` → post-clone files → smart
  detect → write suggested-rules → INSERT row. Cleanup via
  `shutil.rmtree(ignore_errors=True)` bei jedem Fehler ab Schritt 3.
- `whatsbot/application/command_handler.py`: `/new <name> git <url>` ist
  jetzt aktiv (statt C2.2-Hint). Reply enthält Anzahl Rule-Vorschläge +
  Hinweis auf `/allow batch approve` (kommt in C2.4).
- `whatsbot/main.py`: zusätzliche DI-Parameter `git_clone` und
  `projects_root` für Tests; default ist `SubprocessGitClone()` und
  `~/projekte/`.
- Tests (59 neu, 260 total): `test_git_url` (15 — happy/disallowed,
  shell-injection-Versuche, Hostnamen-Subtilitäten wie github.io vs
  github.com), `test_smart_detection` (7), `test_post_clone` (10),
  `test_subprocess_git_clone` (6 — fake-git Skript via PATH-Override:
  exit-zero Pfad, --depth/--quiet Args, non-zero-exit, stderr-Tail,
  git-binary-missing, timeout). Erweiterte `test_command_handler` mit
  einem `StubGitClone`, der die `octocat/Hello-World`-ähnliche Layout
  schreibt (4 neue Tests für `/new git`).
  **Coverage 95.09%**, mypy strict + ruff clean.
- **Live-Smoke** mit echtem Git-Clone:
  - `/new badurl git https://evil.example.com/x/y` → 🚫 URL nicht erlaubt
  - `/new hello git https://github.com/octocat/Hello-World` → ✅ geklont
    + 7 Rule-Vorschläge aus `.git` (Hello-World hat keine package.json)
  - `/ls` zeigt `hello (git)` mit 🟢 NORMAL emoji
  - Filesystem: vollständiges `.git/` aus dem Clone, plus
    `.claudeignore`, `.whatsbot/config.json`, `.whatsbot/outputs/`,
    `.whatsbot/suggested-rules.json` (7 git-Rules), `CLAUDE.md` Template
    (Hello-World hat keine eigene)
  - Duplicate-Detection greift bei zweitem `/new hello git ...`

#### C2.1 — `/new <name>` + `/ls` (empty projects) ✅
- `whatsbot/domain/projects.py`: `Project` dataclass mirrors the spec-§19
  ``projects`` row, `Mode`/`SourceMode` StrEnums, `validate_project_name`
  (2-32 chars, lowercase + digits + `_`/`-`, no leading underscore, no
  reserved words like `ls` / `new` / `.` / `..`), `format_listing` for
  the `/ls` output with mode-emoji + active-marker.
- `whatsbot/ports/project_repository.py`: Protocol + the two structured
  errors (`ProjectAlreadyExistsError`, `ProjectNotFoundError`).
- `whatsbot/adapters/sqlite_project_repository.py`: real SQLite-backed
  CRUD; integrity-error disambiguation (duplicate name vs. CHECK
  constraint trip).
- `whatsbot/application/project_service.py`: `create_empty` (validate →
  check duplicates in DB *and* on disk → mkdir → INSERT, with directory
  rollback if INSERT fails); `list_all` with optional `active_name`
  marker.
- `whatsbot/application/command_handler.py`: refactor of
  `domain.commands.route` into a stateful handler that owns the services.
  Phase-1 commands (`/ping`/`/status`/`/help`) still delegate to the pure
  `domain.commands.route`. New: `/new <name>` (with `/new <name> git
  <url>` rejected with a clear "kommt in C2.2" hint), `/ls`.
- `whatsbot/main.py`: opens the spec-§4 state DB once, builds
  `ProjectService` + `CommandHandler`, hands them to `build_webhook_router`.
  Tests pass an in-memory connection.
- `whatsbot/http/meta_webhook.py`: `build_router` now takes a
  `command_handler` instead of raw version/uptime/db-callback args.
- Tests (66 new, 201 total): `test_projects` (15 — name validation, dataclass
  defaults, listing format), `test_sqlite_project_repository` (12 — CRUD,
  duplicate detection, CHECK constraints), `test_project_service` (10 —
  filesystem layout, error paths, rollback on INSERT failure),
  `test_command_handler` (12 — pass-through to phase-1 commands plus the
  new `/new` and `/ls` paths). **Coverage 95.30%** (target ≥80%);
  `main.py` 100%, `domain/projects.py` 100%, `application/*` 100%,
  `adapters/sqlite_project_repository.py` 100%.
- **Live-smoke verified** with a tmp DB + tmp `~/projekte/` against the
  real `CommandHandler`:
  - `/ls` (empty) → friendly hint
  - `/new alpha` → DB row + dir layout (`alpha/`, `alpha/.whatsbot/`,
    `alpha/.whatsbot/outputs/`) + structured `project_created` log line
  - `/new BAD` → `⚠️ ... ist kein gueltiger Projektname...`
  - `/new alpha` again → `⚠️ Projekt 'alpha' existiert schon.`
  - `/new beta` → second project + dirs
  - `/ls` → alphabetical listing with 🟢 (NORMAL) emoji.

### Phase 1 — Fundament + Echo-Bot ✅ (komplett)

Alle 12 Success-Criteria aus `phase-1.md` erfüllt. Bot läuft als
LaunchAgent, antwortet auf Meta-Webhooks (signiert + whitelisted) mit
Echo-Reply, und macht tägliches DB-Backup. Hexagonal-Architektur mit
135 Tests grün und 96.17% Coverage.

#### C1.7 — DB-Backup-Skript + Retention ✅
- `bin/backup-db.sh`: echtes Skript statt Stub.
  - Nutzt `VACUUM INTO` (SQLite 3.27+) statt `.backup`: produziert eine
    konsolidierte Single-File-DB ohne `-wal`/`-shm` Sidecars,
    read-consistent auch wenn der Bot währenddessen schreibt.
  - Atomares `tmp → mv`: konkurrierende Reads sehen nie eine
    halb-geschriebene Datei.
  - `PRAGMA integrity_check` auf das frische Backup vor Publish, abort+
    löschen bei Fehler statt silent garbage.
  - 30-Tage-Retention via `find -mtime +N`. ENV-Variablen
    `WHATSBOT_DB`/`WHATSBOT_BACKUP_DIR`/`WHATSBOT_BACKUP_RETENTION_DAYS`
    machen das Skript test-isoliert.
  - Strukturierte JSON-Logs (`backup_complete`/`backup_skipped_no_db`/
    `backup_failed`/`backup_integrity_failed`), portable `stat` (BSD+GNU).
- `Makefile backup-db`: Target ruft jetzt `bin/backup-db.sh` (statt Stub).
- Tests: `tests/integration/test_backup_db.py` — 7 echte subprocess-Tests
  (happy-path, intact schema, structured-log, idempotent same-day, skip
  on missing DB, retention deletes >30d, retention spares <30d, retention=0
  spares today's freshly-written backup). Alle grün.
- **Live-Smoke verifiziert**: Test-DB seeded, `bash bin/backup-db.sh` →
  `state.db.<heute>` 118KB, sqlite3 read-back zeigt seed-row, JSON-Log:
  `{"event":"backup_complete","ts":"...","target":"...","size_bytes":118784,
  "retention_days":30,"deleted_old":0}`.

#### C1.5 — Webhook + Echo (Signatur, Whitelist, Command-Router) ✅
- `whatsbot/domain/whitelist.py`: pure Parser für `allowed-senders` aus Spec
  §4 (kommasepariert, dedupe via `frozenset`, fail-closed bei leerer Liste).
- `whatsbot/domain/commands.py`: pures Routing für `/ping`, `/status`,
  `/help` mit `StatusSnapshot`-Dataclass für die nicht-pure Inputs (Version,
  Uptime, DB-OK, Env). Unbekannte Commands liefern friendly hint, raisen
  nicht — Phase 4 ersetzt diesen Branch durch "an aktive Claude-Session
  weiterleiten".
- `whatsbot/http/meta_webhook.py`:
  - `verify_signature()` — HMAC-SHA256 vs raw Body, `compare_digest`,
    fail-closed bei missing/malformed Header.
  - `check_subscribe_challenge()` — Meta-Subscribe-Handshake; gibt
    `hub.challenge` nur zurück wenn `hub.mode==subscribe` und
    `hub.verify_token` matched (constant-time compare).
  - `iter_text_messages()` — defensive Extraktion von `entry[].changes[]
    .value.messages[]` mit `type==text`; skipt malformed/non-text/missing
    silent statt zu raisen (Meta wiederholt eh).
  - `build_router(...)` — `APIRouter`-Factory mit `GET /webhook` (challenge)
    und `POST /webhook` (signature → whitelist → routing → sender).
    Sig-Check wird im non-prod env mit fehlendem app-secret übersprungen
    (für `make run-dev` ohne `make setup-secrets`).
- `whatsbot/ports/message_sender.py`: `MessageSender`-Protocol (send_text).
- `whatsbot/adapters/whatsapp_sender.py`:
  - `LoggingMessageSender` — schreibt struktured Log statt zu senden,
    Phase-1 Default und Test-Adapter.
  - `WhatsAppCloudSender` — Skelett, raised `NotImplementedError`. Echte
    httpx-/tenacity-Implementierung in C2.x sobald Projekte antworten.
- `whatsbot/main.py`:
  - Akzeptiert `message_sender`-DI-Param (Default `LoggingMessageSender`).
  - Wired `build_webhook_router` ein, plus `ConstantTimeMiddleware(
    paths=("/webhook",), min_duration_ms=200)` gegen Timing-Enumeration
    der Sender-Whitelist (Spec §5).
  - Test-Env: `_EmptySecretsProvider` Fallback wenn kein Provider
    injiziert wird, sodass Unit-Tests die Webhook-Routes ohne Mock-Keychain
    bauen können.
- `tests/fixtures/meta_*.json`: 6 echte Meta-Payloads (ping, status, help,
  unknown_command, unknown_sender, non_text/image).
- `tests/send_fixture.sh`: schickt Fixture an `:8000/webhook` mit
  HMAC-SHA256-Signatur (Secret aus Keychain falls vorhanden, sonst Dummy).
- Tests: `test_whitelist.py` (9), `test_commands.py` (8),
  `test_meta_webhook.py` (15 — Signatur, Challenge, iter_text_messages),
  `test_webhook_routing.py` (17 — End-to-End mit StubSecrets +
  RecordingSender, alle silent-drop-Pfade, Constant-Time-Padding).
  **128 Tests grün, Coverage 96.17%** (Ziel ≥80%).
- **Live-Smoke verifiziert**:
  - dev-bot via uvicorn → `tests/send_fixture.sh meta_ping` → 200 OK + ULID
  - JSON-Log zeigt: `signature_check_skipped_dev_mode` →
    `sender_not_allowed` (fail-closed, weil `allowed-senders` Secret fehlt)
  - `meta_unknown_sender` ebenfalls silent-drop mit `sender_not_allowed`
  - **Happy-Path** (gültige Signatur + gültiger Sender → `command_routed` +
    `outbound_message_dev`) ist via Integration-Tests mit `StubSecrets`
    + `RecordingSender` voll abgedeckt.

#### C1.4 — LaunchAgent + Backup-Agent + Repo-Migration ✅
- `launchd/com.DOMAIN.whatsbot.plist.template`: Bot-Agent. `KeepAlive`
  mit `SuccessfulExit=False` (restart on crash, nicht auf graceful exit;
  wichtig für `/panic`). `RunAtLoad=true`, `ProcessType=Background`.
  `EnvironmentVariables`: `WHATSBOT_ENV`, `SSH_AUTH_SOCK` (für Phase 2 git
  clone gegen private repos), `PATH`, `HOME`. `ProgramArguments` startet
  uvicorn `--factory whatsbot.main:create_app`.
- `launchd/com.DOMAIN.whatsbot.backup.plist.template`: täglich 03:00 via
  `StartCalendarInterval` (Hour=3 Minute=0). `RunAtLoad=false`. Ruft
  `bin/backup-db.sh`.
- `bin/backup-db.sh`: **Stub** — gibt strukturierte JSON-Zeile aus.
  Echtes `sqlite3 .backup` + 30-Tage-Retention kommt in C1.7.
- `bin/render-launchd.sh`: deploy/undeploy via `launchctl bootstrap`/
  `bootout`, idempotent (bootout vor bootstrap), `plutil -lint` vor jedem
  load. Refused, falls Placeholders nicht ersetzt sind.
- `Makefile`: `deploy-launchd` und `undeploy-launchd` mit `DOMAIN=`/
  `ENV=`/`PORT=` Variablen. Default `ENV=prod`, `PORT=8000`,
  `REPO_DIR=$(abspath .)`.
- Tests: `tests/unit/test_launchd_template.py` — 13 Plist-Tests
  (Label, KeepAlive, RunAtLoad, ProgramArguments, EnvironmentVariables,
  ProcessType, StartCalendarInterval). **79 Tests grün, Coverage 95.97%**.
- **Repo-Migration nach `~/whatsbot/`** (Spec §4 Default): macOS TCC
  schützt `~/Desktop`, `~/Documents`, `~/Downloads` vor
  LaunchAgent-Zugriff (Repo war anfangs unter
  `~/Desktop/projects/wabot/` — der vom LaunchAgent gespawnte uvicorn
  bekam `PermissionError` beim Lesen von `venv/pyvenv.cfg`). Nach `mv`
  läuft alles. Symlink `~/Desktop/projects/wabot → ~/whatsbot` erhalten
  als Convenience für die User-Convention "alle Projekte unter
  ~/Desktop/projects/".
- **Live-verifiziert**: `make deploy-launchd ENV=dev DOMAIN=local PORT=8000`
  → `launchctl list` zeigt Bot mit echtem PID + Backup-Agent scheduled
  → `curl /health` → 200 JSON inkl. `X-Correlation-Id` ULID
  → `launchctl print` `state=running, active count=1`
  → `app.jsonl` enthält frische `startup_complete`-Events
  → `launchd-stderr.log` bleibt leer (sauberer Run)
  → `make undeploy-launchd DOMAIN=local` → keine Agents mehr,
    Port 8000 frei, Plists entfernt.

#### C1.3 — Logging + Config + Health-Endpoint ✅
- `whatsbot/logging_setup.py`: structlog mit JSONRenderer, contextvars merge
  (für `msg_id/session_id/project/mode`), TimeStamper (ISO UTC, key `ts`),
  RotatingFileHandler nach Spec §15 (`app.jsonl`, 10 MB × 5 backups).
  Idempotent — sichere Doppelaufrufe.
- `whatsbot/config.py`: `Settings` (Pydantic BaseModel) mit Defaults aus
  Spec §4 (log_dir, db_path, backup_dir, bind_host/port, hook_bind_host/port).
  `Settings.from_env()` liest `WHATSBOT_ENV` (prod|dev|test) und
  `WHATSBOT_DRY_RUN`. `assert_secrets_present()`: prod → harter Abbruch
  (`SecretsValidationError`), dev → Warning + missing-Liste, test → skip.
- `whatsbot/http/middleware.py`:
  - `CorrelationIdMiddleware`: ULID pro Request, in structlog contextvars
    gebunden, als `X-Correlation-Id`-Header gespiegelt, Token-Reset garantiert
    keine Cross-Request-Kontamination.
  - `ConstantTimeMiddleware`: padding-fähig, Path-Filter (default leer = alle,
    in C1.5 wird es auf `("/webhook",)` gesetzt). Verhindert Timing-Enumeration
    der Sender-Whitelist (Spec §5).
- `whatsbot/main.py`: `create_app()`-Factory. configure_logging einmalig,
  Secrets-Gate (skip in test, warn in dev, raise in prod), CorrelationIdMiddleware
  global, `/health` (ok/version/uptime_seconds/env), `/metrics`-Stub
  (PlainTextResponse, leer — echtes Prometheus in Phase 8).
- `Makefile`: `run-dev` nutzt jetzt `--factory whatsbot.main:create_app`.
- Tests: `test_logging.py` (6), `test_config.py` (10), `test_middleware.py` (6),
  `test_health.py` (6). conftest hat jetzt `_reset_logging_state` autouse-Fixture.
  **66 Tests grün, Coverage 95.97%** (Ziel ≥80%). middleware.py und
  logging_setup.py jeweils 100%, config.py 100%, main.py 80% (dev-warning-Pfad
  ungetestet — wird via Live-Smoke statt Unit verifiziert).
- **Live-Smoke verifiziert**: `make run-dev` startet den Bot, `curl /health`
  liefert das erwartete JSON inkl. `X-Correlation-Id`-Header (26-char ULID),
  `/metrics` liefert leeres text/plain, `/does-not-exist` liefert 404 mit
  Header (Middleware tagt auch Errors), zwei Requests bekommen verschiedene
  Correlation-IDs, JSON-Logs schreiben sauber `secrets_missing_dev_mode` und
  `startup_complete` mit allen Spec-§15-Feldern.

#### C1.2 — Keychain-Provider + SQLite-Schema + Integrity-Restore ✅
- `whatsbot/ports/secrets_provider.py`: `SecretsProvider`-Protocol (get/set/rotate),
  Service-Konstante `whatsbot`, die 7 Pflicht-Keys aus Spec §4 als Konstanten,
  `verify_all_present()` für den Startup-Check.
- `whatsbot/adapters/keychain_provider.py`: macOS-Keychain-Implementierung via
  `keyring`-Library. `SecretNotFoundError` mit klarer Hinweis-Message bei
  fehlendem Eintrag. `rotate()` löscht erst, dann setzt neu.
- `bin/setup-secrets.sh`: interaktiver Bash-Prompt für alle 7 Secrets,
  `set -euo pipefail`, Bestehende-Werte-Confirm, Final-Verifikation,
  Exit-Code 1 bei fehlenden Einträgen.
- `sql/schema.sql`: alle 10 Tabellen + 5 Indizes exakt aus Spec §19
  (PRAGMAs separat im Adapter, weil per-connection).
- `whatsbot/adapters/sqlite_repo.py`: `connect()` setzt die 4 Pflicht-PRAGMAs
  (WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON);
  `apply_schema()`, `integrity_check()`, `latest_backup()`,
  `restore_from_latest_backup()` (mit WAL/SHM-Cleanup),
  `open_state_db()` als High-Level-Orchestrator (fresh-or-existing → check →
  restore-and-recheck → fail).
- `Makefile`: `setup-secrets` ruft jetzt `bin/setup-secrets.sh`,
  `reset-db` legt frisches Schema via `open_state_db()` an.
- Tests: `tests/conftest.py` mit `mock_keyring` (monkeypatch),
  `tmp_db_path`, `tmp_backup_dir`. 13 Secret-Tests + 17 DB-Tests.
  **30 Tests grün, Coverage 96.99%** (Ziel: ≥80%). mypy strict + ruff lint
  + ruff format alle clean.

#### C1.1 — Repo-Struktur + Python-Setup ✅
- Hexagonal layout angelegt: `whatsbot/{domain,ports,adapters,application,http}`,
  plus `hooks/`, `bin/`, `launchd/`, `sql/migrations/`, `tests/{unit,integration,fixtures}`,
  `docs/`. Package-Docstrings dokumentieren die Layer-Grenzen.
- `pyproject.toml` mit Python 3.12 constraint, pytest + coverage (fail_under=80) +
  mypy strict + ruff (E/W/F/I/B/UP/SIM/S/TID/RUF) konfiguriert.
- `requirements.txt` mit gepinnten Runtime-Deps (FastAPI 0.115, Uvicorn 0.32, Pydantic 2.10,
  structlog 24.4, python-ulid 3.0, keyring 25, tenacity 9, python-multipart 0.0).
  **Spec §5 Verriegelung 1**: kein `claude-agent-sdk`.
- `requirements-dev.txt` mit pytest 8 + asyncio + cov, httpx 0.27 (TestClient),
  mypy 1.13, ruff 0.7.
- `Makefile` mit Targets `install / test / test-unit / test-integration / smoke / lint /
  format / typecheck / setup-secrets / deploy-launchd / reset-db / backup-db / clean`.
  Operations-Targets sind Stubs mit `TODO Phase 1 C1.x` — werden in C1.2/C1.4/C1.7 befüllt.
- Verifiziert: `venv/bin/python -c "import whatsbot"` → `0.1.0`; `mypy whatsbot` clean;
  `ruff check` clean; `find_spec('claude_agent_sdk') is None`.

