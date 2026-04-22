# Aktueller Stand

**Aktive Phase**: Phase 3 — Security-Core (Hook + Allow/Deny + Redaction)
**Aktiver Checkpoint**: C3.4 (Input-Sanitization für verdächtige Prompts)
**Letzter abgeschlossener Checkpoint**: C3.3 (Redaction-Pipeline, 4 Stages)

## Phase 3 — bisheriger Fortschritt

- ✅ C3.1 — `hooks/pre_tool.py` + Shared-Secret-IPC-Endpoint auf `127.0.0.1:8001`
- ✅ C3.2 — Deny-Patterns + PIN-Rückfrage (End-to-End + 17 Fixtures)
- ✅ C3.3 — Redaction-Pipeline 4 Stages:
  - `domain/redaction.py` (Stages: known-keys / struktur / entropy / path-content)
  - `adapters/redacting_sender.py` (Decorator, sitzt vor jedem Send)
  - Wired global in `main.create_app` — alle Outbound-Pfade bekommen Redaction
  - 37 Unit-Tests (≥10 Secret-Typen, false-positive-Kontrollen)
  - 7 Wire-Tests (Decorator + E2E via /webhook mit AKIA-Input)
  - CLI: `python -m whatsbot.domain.redaction` (stdin smoke)

**Tests**: 606/606 passing, mypy --strict clean, ruff clean.

## Was als Nächstes (C3.4 → C3.6)

Verbleibende C3-Checkpoints aus `phase-3.md`:

- **C3.4** — Input-Sanitization: verdächtige Prompts in
  `<untrusted_content suspected_injection="true">`-Tags wrappen
  (nur Normal-Mode; Strict/YOLO Bypass).
  Trigger-Phrasen: `"ignore previous"`, `"disregard"`, `"system:"`,
  `"you are now"`, `"your new task"` — case-insensitive.
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
