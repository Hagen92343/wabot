# Aktueller Stand

**Aktive Phase**: Phase 3 — Security-Core (Hook + Allow/Deny + Redaction)
**Aktiver Checkpoint**: C3.5 (Output-Size-Warning + /send / /discard / /save)
**Letzter abgeschlossener Checkpoint**: C3.4 (Input-Sanitization + Audit-Log)

## Phase 3 — bisheriger Fortschritt

- ✅ C3.1 — `hooks/pre_tool.py` + Shared-Secret-IPC-Endpoint auf `127.0.0.1:8001`
- ✅ C3.2 — Deny-Patterns + PIN-Rückfrage (End-to-End + 17 Fixtures)
- ✅ C3.3 — Redaction-Pipeline 4 Stages + globaler Sender-Decorator
- ✅ C3.4 — Input-Sanitization:
  - `domain/injection.py` mit `detect_triggers` + `sanitize(text, mode)`
  - Wrap nur im Normal-Mode (Strict/YOLO Bypass)
  - Webhook loggt `injection_suspected`-Audit-Event auf jeden Hit
  - 30 Unit-Tests + 3 Integration-Tests

**Tests**: 639/639 passing, mypy --strict clean, ruff clean.

## Was als Nächstes (C3.5 → C3.6)

Verbleibende C3-Checkpoints aus `phase-3.md`:

- **C3.5** — Output-Size-Warning (>10KB) + `/send` / `/discard` /
  `/save` + `pending_outputs`-Zeile. Gilt in allen Modi.
- **C3.6** — Fail-closed Integration-Test: Unreachable / 401 / Crash /
  Timeout (die Logik ist bereits drin, aber expliziter End-to-End-
  Test gegen das Hook-Script fehlt).

Noch offen als Schuld aus C3.2:
- Write-Hook hat noch den Stub-Pfad (`classify_write` = allow). Die echte
  Path-Rules-Policy (Spec §12 Layer 3) ist im `phase-3.md`-Scope, aber nicht
  als eigener Checkpoint vergeben — idealerweise als Teil von C3.5
  nachziehen.

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
