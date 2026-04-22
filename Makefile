# whatsbot — developer convenience targets.
#
# Conventions:
#   - All commands assume the project venv at ./venv (use `make install` to create).
#   - `make help` lists targets.
#   - Commands are intentionally explicit; no magic.

# ---- Config -----------------------------------------------------------------

PYTHON      ?= /opt/homebrew/opt/python@3.12/bin/python3.12
VENV        ?= venv
PIP         := $(VENV)/bin/pip
PY          := $(VENV)/bin/python
PYTEST      := $(VENV)/bin/pytest
MYPY        := $(VENV)/bin/mypy
RUFF        := $(VENV)/bin/ruff
UVICORN     := $(VENV)/bin/uvicorn

DOMAIN      ?= local                       # used by deploy-launchd
ENV         ?= prod                        # WHATSBOT_ENV passed into the LaunchAgent
PORT        ?= 8000
REPO_DIR    := $(abspath .)
LAUNCH_DIR  := $(HOME)/Library/LaunchAgents
LOG_DIR     := $(HOME)/Library/Logs/whatsbot
APP_SUPPORT := $(HOME)/Library/Application\ Support/whatsbot
DB_PATH     := $(APP_SUPPORT)/state.db
BACKUP_DIR  := $(HOME)/Backups/whatsbot

.DEFAULT_GOAL := help
.PHONY: help install venv deps dev-deps run-dev test test-unit test-integration smoke \
        lint format typecheck setup-secrets deploy-launchd undeploy-launchd \
        reset-db backup-db clean

# ---- Help -------------------------------------------------------------------

help: ## Diese Liste anzeigen
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---- Setup ------------------------------------------------------------------

install: venv deps dev-deps ## venv anlegen + alle Dependencies installieren
	@echo "✅ install complete — activate with: source $(VENV)/bin/activate"

venv: ## Python 3.12 venv anlegen (idempotent)
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@$(PIP) install --upgrade pip setuptools wheel >/dev/null

deps: ## Runtime-Deps installieren
	$(PIP) install -r requirements.txt

dev-deps: ## Dev-Deps installieren (pytest, mypy, ruff, ...)
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .

# ---- Run --------------------------------------------------------------------

run-dev: ## FastAPI mit Reload starten (WHATSBOT_ENV=dev, Signature-Check aus)
	WHATSBOT_ENV=dev $(UVICORN) whatsbot.main:create_app --factory \
		--host 127.0.0.1 --port 8000 --reload

# ---- Tests ------------------------------------------------------------------

test: ## Alle Tests + Coverage
	$(PYTEST) --cov=whatsbot --cov-report=term-missing

test-unit: ## Nur Unit-Tests (pure domain)
	$(PYTEST) -m unit tests/unit

test-integration: ## Nur Integration-Tests (FastAPI TestClient etc.)
	$(PYTEST) -m integration tests/integration

smoke: ## End-to-End gegen Mock-Meta-Server (lokal, nicht in CI)
	$(PYTEST) -m smoke tests/smoke.py

# ---- Quality ----------------------------------------------------------------

lint: ## Ruff lint + format-check
	$(RUFF) check whatsbot tests hooks
	$(RUFF) format --check whatsbot tests hooks

format: ## Ruff format apply
	$(RUFF) format whatsbot tests hooks
	$(RUFF) check --fix whatsbot tests hooks

typecheck: ## mypy --strict
	$(MYPY) whatsbot

# ---- Operations -------------------------------------------------------------

setup-secrets: ## Interaktiv die 7 Keychain-Secrets setzen
	bash bin/setup-secrets.sh

deploy-launchd: ## LaunchAgents (Bot+Backup) rendern und bei launchd registrieren. Vars: DOMAIN= ENV= PORT=
	bash bin/render-launchd.sh deploy "$(DOMAIN)" "$(ENV)" "$(PORT)" "$(REPO_DIR)" "$(LAUNCH_DIR)" "$$SSH_AUTH_SOCK"

undeploy-launchd: ## LaunchAgents (Bot+Backup) abmelden + Plist-Files entfernen. Vars: DOMAIN=
	bash bin/render-launchd.sh undeploy "$(DOMAIN)" "$(LAUNCH_DIR)"

reset-db: ## State-DB neu anlegen mit frischem Schema (DESTRUCTIVE — nur Dev)
	@echo "⚠️  This deletes $(DB_PATH). Press Ctrl+C to abort, Enter to continue."
	@read _
	rm -f $(DB_PATH) $(DB_PATH)-wal $(DB_PATH)-shm
	$(PY) -c "from whatsbot.adapters.sqlite_repo import open_state_db; open_state_db().close(); print('✅ state.db (re)created with fresh schema')"

backup-db: ## SQLite .backup nach ~/Backups/whatsbot/state.db.<date> (Retention 30 Tage)
	bash bin/backup-db.sh

# ---- Cleanup ----------------------------------------------------------------

clean: ## Caches und venv löschen
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist
	find . -type d -name __pycache__ -exec rm -rf {} +
