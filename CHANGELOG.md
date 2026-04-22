# Changelog

Alle nennenswerten Änderungen am `whatsbot`-Repo. Format: phasen-/checkpoint-basiert,
neueste oben. Sieh dazu `.claude/rules/current-phase.md` für den Live-Stand.

## [Unreleased]

### Phase 2 — Projekt-Management + Smart-Detection (in progress)

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

