# Aktueller Stand

**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.3 (Health-Endpoint)
**Letzter abgeschlossener Checkpoint**: C1.2 (Keychain + DB)

## Was als Nächstes zu tun ist

C1.3 laut `phase-1.md` §5 + §6 + §9:

1. `whatsbot/logging_setup.py` — structlog mit JSON-Renderer, RotatingFileHandler
   für `app.jsonl`, Felder `ts/level/logger/msg_id/session_id/project/mode/event/...`
2. `whatsbot/config.py` — Pydantic-Settings, lädt Secrets beim Start (Aufruf von
   `verify_all_present`), `WHATSBOT_ENV` (prod|dev|test), `WHATSBOT_DRY_RUN`
3. `whatsbot/http/middleware.py` — `CorrelationIdMiddleware` (ULID pro Request,
   in Log-Context binden), `ConstantTimeMiddleware` (min 200ms bei Rejection,
   gegen Timing-Enumeration)
4. `whatsbot/main.py` — FastAPI-App mit `/health` (`{ok, version, uptime_seconds}`)
   und `/metrics` (Prometheus-Stub leer in C1.3)
5. Tests: `tests/unit/test_logging.py` (Format-Felder),
   `tests/unit/test_config.py` (Secret-Loading, harter Abbruch bei Fehlen),
   `tests/integration/test_health.py` (FastAPI TestClient → 200 JSON)

Verifikation (C1.3 done):
- `make run-dev` startet ohne Errors
- `curl http://localhost:8000/health` → `{"ok": true, "version": "0.1.0", "uptime_seconds": ...}`
- `make test` grün, Coverage ≥80%

## Format-Konvention für Updates

Wenn du einen Checkpoint abschließt, update diese Datei so:

```
**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.4 (LaunchAgent)
**Letzter abgeschlossener Checkpoint**: C1.3 (Health-Endpoint)
```

Wenn du eine ganze Phase abschließt:

```
**Aktive Phase**: Phase 2 – Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: noch keiner
**Letzter abgeschlossener Checkpoint**: C1.7 (DB-Backup) — Phase 1 komplett
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-<N>.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
