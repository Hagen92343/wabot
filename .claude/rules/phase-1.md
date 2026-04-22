# Phase 1: Fundament + Echo-Bot

**Aufwand**: 3-4 Sessions
**Spec-Referenzen**: §4 (Plattform), §5 (Auth), §17-19 (Code, Tests, DB), §20 (NFRs)

## Ziel der Phase

Bot läuft als LaunchAgent, empfängt Webhooks von Meta, antwortet auf `/ping` mit einem Echo. Noch kein Claude, keine Projekte. Basis-Infrastruktur steht.

## Was gebaut wird

### 1. Repo-Struktur

Genau wie in Spec §18. Lege an:
- `whatsbot/` mit allen Unterordnern (`domain/`, `ports/`, `adapters/`, `application/`, `http/`)
- `hooks/`, `bin/`, `launchd/`, `sql/`, `tests/`, `docs/`

Leere `__init__.py` pro Python-Package. Docstrings auf Package-Ebene erklären den Zweck.

### 2. Python-Setup

- `pyproject.toml` mit Python 3.12
- `requirements.txt` mit genau diesen Dependencies (pinned):
  ```
  fastapi==0.115.*
  uvicorn[standard]==0.32.*
  pydantic==2.10.*
  structlog==24.4.*
  python-ulid==3.0.*
  keyring==25.*
  tenacity==9.*
  python-multipart==0.0.*
  ```
- `Makefile` mit Targets: `install`, `test`, `test-unit`, `test-integration`, `smoke`, `lint`, `typecheck`, `deploy-launchd`, `setup-secrets`, `reset-db`

NIEMALS `claude-agent-sdk`. Siehe CLAUDE.md.

### 3. Keychain-Provider (`adapters/keychain_provider.py`)

Port `SecretsProvider` in `ports/secrets_provider.py` mit Methoden:
- `get(key: str) -> str`
- `set(key: str, value: str) -> None`
- `rotate(key: str, new_value: str) -> None`

Adapter nutzt `keyring`-Library. Service-Name: `"whatsbot"`. Die 7 Keychain-Einträge aus Spec §4.

Beim App-Start: alle 7 Secrets probeweise laden, bei Fehler harter Abbruch mit klarer Fehlermeldung welches Secret fehlt.

### 4. SQLite-Schema (`sql/schema.sql`)

Exakt wie in Spec §19. Plus `PRAGMA`-Setup beim Connection-Open:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

Beim Startup: `PRAGMA integrity_check;` – bei Fehler Auto-Restore aus `~/Backups/whatsbot/state.db.<yesterday>`, falls vorhanden; sonst harter Abbruch.

### 5. Logging (`whatsbot/logging_setup.py`)

structlog mit JSON-Formatter. Felder: `ts`, `level`, `logger`, `msg_id`, `session_id`, `project`, `mode`, `event`, plus beliebige Event-spezifische Felder.

Log-Files wie in Spec §15. RotatingFileHandler mit den dort genannten Limits.

Niemals `print()`. Niemals Python-Standard-`logging` außer als Sink für Libraries, die nicht anders können.

### 6. FastAPI-App (`whatsbot/main.py`)

Endpoints:
- `GET /health` → `{"ok": true, "version": "...", "uptime_seconds": ...}`
- `GET /metrics` → Prometheus-Text-Format (leer in Phase 1, vorbereitet)
- `POST /webhook` → Command-Router, zunächst nur `/ping` + `/status` + `/help`
- `GET /webhook` → Meta-Subscribe-Challenge (echo `hub.challenge`)

Middleware:
- `CorrelationIdMiddleware`: vergibt ULID pro Request, hängt an Log-Context
- `ConstantTimeMiddleware`: ergänzt Antwort auf min. 200ms bei Rejections

### 7. Meta-Webhook-Logik

Signatur-Verification nach Meta-Spezifikation:
- Header `X-Hub-Signature-256`
- HMAC-SHA256 mit `meta-app-secret` aus Keychain
- Body exakt wie empfangen verifizieren (kein re-JSON-dumping)
- Ungültig → 200 OK, silent drop, structured WARN log

Sender-Whitelist:
- Parse `allowed-senders` aus Keychain (kommasepariert)
- Absender-Nummer aus Payload: `entry[].changes[].value.messages[].from`
- Nicht gelistet → 200 OK, silent drop, structured WARN log

### 8. Command-Router (minimal)

In Phase 1 nur:
- `/ping` → `"pong · <version> · <uptime>"`
- `/status` → System-Info: Uptime, Heartbeat-Age, DB-Status
- `/help` → Liste der aktuell verfügbaren Commands

Routing-Logik im Domain-Core (`domain/commands.py`, pure), HTTP-Wiring im Application-Layer.

### 9. Konfiguration (`whatsbot/config.py`)

Lädt beim Start:
- Alle 7 Secrets aus Keychain
- Pfade (hardcoded wie Spec §4)
- `WHATSBOT_ENV` (`prod` | `dev` | `test`) – `dev` umgeht Signature-Check
- `WHATSBOT_DRY_RUN` (0/1) – keine externen Effekte

Pydantic-Model für Type-Safety. Bei Ladeproblem: harter Abbruch mit präziser Fehlermeldung.

### 10. LaunchAgent

Template in `launchd/com.DOMAIN.whatsbot.plist.template`. Beim `make deploy-launchd`:
- Template nach `~/Library/LaunchAgents/` kopieren, `<DOMAIN>` ersetzen
- `launchctl load -w <path>`
- Ein zweiter LaunchAgent für DB-Backup (täglich 03:00)

Wichtige Plist-Keys:
- `KeepAlive` mit `SuccessfulExit: false`
- `RunAtLoad: true`
- `EnvironmentVariables` inklusive `SSH_AUTH_SOCK` (für Phase 2+)
- `StandardErrorPath` und `StandardOutPath` in Logs-Verzeichnis

### 11. Shell-Scripts

- `bin/preflight.sh`: prüft `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_*` – bei Treffer Exit 1 mit Fehlermeldung
- `bin/safe-claude`: unsetzt die Variablen, dann `exec claude "$@"`
- `bin/backup-db.sh`: SQLite `.backup` nach `~/Backups/whatsbot/state.db.<YYYY-MM-DD>`, löscht alles älter als 30 Tage
- Alle Scripts mit `set -euo pipefail`, shellcheck-clean

### 12. Tests

- `tests/conftest.py` mit Fixtures für DB-in-Memory, Mock-Keychain, Dry-Run-Flag
- `tests/unit/test_commands.py`: pure Tests für Command-Router (`/ping`, `/status`, `/help`)
- `tests/unit/test_config.py`: Secret-Loading mit Mock-Keychain
- `tests/integration/test_webhook.py`: FastAPI-TestClient gegen `/webhook`
- `tests/fixtures/meta_ping.json`: echte Meta-Payload mit Text "`/ping`"
- `tests/send_fixture.sh`: Bash-Script das eine Fixture an `http://localhost:8000/webhook` schickt

## Checkpoints

### C1.1 – Repo-Struktur + Python-Setup

```bash
make install
# Erwartung: venv angelegt, alle Dependencies installiert, keine Errors
python -c "import whatsbot; print(whatsbot.__version__)"
# Erwartung: Version-String
```

### C1.2 – Keychain + DB

```bash
make setup-secrets
# Interaktiver Prompt für alle 7 Secrets, ablegen im Keychain
sqlite3 ~/Library/Application\ Support/whatsbot/state.db ".schema"
# Erwartung: alle Tabellen aus schema.sql
```

### C1.3 – Health-Endpoint

```bash
make run-dev
# In zweitem Terminal:
curl http://localhost:8000/health
# Erwartung: JSON mit ok:true
```

### C1.4 – LaunchAgent

```bash
make deploy-launchd
launchctl list | grep whatsbot
# Erwartung: Bot läuft, DB-Backup-Agent registriert
tail ~/Library/Logs/whatsbot/app.jsonl
# Erwartung: Startup-Events als JSON
```

### C1.5 – Webhook + Echo

```bash
# Terminal: tail Logs
tail -f ~/Library/Logs/whatsbot/app.jsonl

# Anderes Terminal: Fixture-Request
tests/send_fixture.sh meta_ping
# Erwartung:
# - 200 OK Response
# - Log-Eintrag "command_routed" mit msg_id
# - In Dev-Mode: Antwort-Message geloggt statt gesendet
```

### C1.6 – Tests grün

```bash
make test
# Erwartung: alle Unit + Integration Tests grün, Coverage >80% für domain/
```

### C1.7 – DB-Backup

```bash
bin/backup-db.sh
ls ~/Backups/whatsbot/
# Erwartung: state.db.<heute>
```

## Success Criteria (alle erfüllen!)

- [ ] Bot läuft als LaunchAgent, startet automatisch nach User-Login
- [ ] `/health` antwortet JSON
- [ ] Meta-Signature-Check rejected ungültige Requests (silent, geloggt)
- [ ] Fremde Sender werden silent gedroppt
- [ ] `/ping` per Fixture → Echo-Response
- [ ] Alle 7 Keychain-Secrets ladbar
- [ ] `PRAGMA integrity_check` bei Startup grün
- [ ] Logs als JSON in `app.jsonl`, mit Correlation-ID
- [ ] `make test` grün mit >80% Domain-Coverage
- [ ] Tägliches DB-Backup-Script lauffähig
- [ ] `mypy --strict whatsbot/` grün
- [ ] `CHANGELOG.md` mit Phase-1-Einträgen

## Abbruch-Kriterien

- **LaunchAgent kann nicht registriert werden** (nach 3 ernsten Versuchen): Stop. Melde das Problem mit exakter Fehlermeldung. User muss Architektur-Review machen.
- **Keychain-Zugriff dauerhaft verweigert**: Stop. Das ist kein Code-Problem, sondern ein System-Berechtigungs-Problem. User muss `security unlock-keychain` laufen lassen oder Terminal-App Full Disk Access geben.
- **Meta-Webhook-Signatur-Algorithmus unklar**: Stop. Verifiziere gegen offizielle Meta-Docs bevor du weiterbaust. Niemals raten.
- **Dependencies mit Sicherheitslücken**: Stop. Update die Version und logge das im CHANGELOG.

## Was in Phase 1 NICHT gebaut wird

- Claude-Launch (kommt in Phase 4)
- Projekt-Management (Phase 2)
- Hook-Script (Phase 3)
- tmux-Integration (Phase 4)
- Allow/Deny-Rules (Phase 3)
- Redaction-Pipeline (Phase 3)
- Medien-Handling (Phase 7)

Widerstehe der Versuchung, "nebenbei" Teile aus späteren Phasen vorzubereiten. Das bricht das Phasenmodell.

## Nach Phase 1

Update `.claude/rules/current-phase.md` auf Phase 2. Phase-2-Rules liegen in `.claude/rules/phase-2.md`. Warte auf User-Freigabe bevor du Phase 2 beginnst.
