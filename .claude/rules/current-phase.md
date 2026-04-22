# Aktueller Stand

**Aktive Phase**: Phase 3 — Security-Core (Hook + Allow/Deny + Redaction)
**Aktiver Checkpoint**: C3.6 (Fail-closed Hook-Integration-Test)
**Letzter abgeschlossener Checkpoint**: C3.5 (Output-Size-Warning + /send / /discard / /save)

## Phase 3 — bisheriger Fortschritt

- ✅ C3.1 — `hooks/pre_tool.py` + Shared-Secret-IPC-Endpoint auf `127.0.0.1:8001`
- ✅ C3.2 — Deny-Patterns + PIN-Rückfrage (End-to-End + 17 Fixtures)
- ✅ C3.3 — Redaction-Pipeline 4 Stages + globaler Sender-Decorator
- ✅ C3.4 — Input-Sanitization + Audit-Log
- ✅ C3.5 — Output-Size-Warning (>10KB):
  - `domain/output_guard.py` (THRESHOLD 10KB, warning-text, chunker)
  - `domain/pending_outputs.py` + Port + SQLite-Adapter
  - `application/output_service.py` (deliver + resolve_send/discard/save)
  - Webhook intercepts `/send` · `/discard` · `/save` vor Command-Router
  - Webhook-Replies laufen jetzt über `output_service.deliver`
  - 38 neue Tests (27 Unit + 11 OutputService Unit + 6 Integration)

**Tests**: 683/683 passing, mypy --strict clean.

## Was als Nächstes (C3.6)

Letzter verbleibender Phase-3-Checkpoint:

- **C3.6** — Fail-closed Hook-Integration-Test.
  Die Logik ist schon drin (Hook-Script + Endpoint haben Fail-
  Closed-Pfade, Tests in `test_hook_script.py`/`test_hook_endpoint.py`
  decken einiges ab). Was noch fehlt ist ein **expliziter End-to-End-
  Smoke** gemäss phase-3.md C3.6:
    - Bot läuft NICHT (Port 8001 refused) → Hook-Script Exit 2
    - Shared-Secret-Mismatch → Hook-Script Exit 2 mit Stderr-Reason
    - Hook-Endpoint-Crash (500) → Exit 2
    - Malformed stdin → Exit 2
  Der existierende `tests/integration/test_hook_script.py` hat
  Bausteine dafür — ggf. reicht eine Zusammenfassung als
  „fail-closed summary smoke" und ein neuer Test für den 500er-Pfad.

Offen als Schuld aus C3.2 + C3.5:
- Write-Hook-Stub (`classify_write` = allow). Die echte Path-Rules-
  Policy (Spec §12 Layer 3) bleibt für Phase 4 oder einen
  nachgezogenen C3.7 liegen.

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 3 — Security-Core
**Aktiver Checkpoint**: C3.1 (Hook-Script + Shared-Secret-IPC)
**Letzter abgeschlossener Checkpoint**: C2.8 (Phase-2-Verifikation)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (sobald `.claude/rules/phase-3.md` existiert)
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
