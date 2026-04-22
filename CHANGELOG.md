# Changelog

Alle nennenswerten Änderungen am `whatsbot`-Repo. Format: phasen-/checkpoint-basiert,
neueste oben. Sieh dazu `.claude/rules/current-phase.md` für den Live-Stand.

## [Unreleased]

### Phase 1 — Fundament + Echo-Bot (in progress)

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

