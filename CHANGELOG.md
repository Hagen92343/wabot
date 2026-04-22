# Changelog

Alle nennenswerten Änderungen am `whatsbot`-Repo. Format: phasen-/checkpoint-basiert,
neueste oben. Sieh dazu `.claude/rules/current-phase.md` für den Live-Stand.

## [Unreleased]

### Phase 1 — Fundament + Echo-Bot (in progress)

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

