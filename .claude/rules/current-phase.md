# Aktueller Stand

**Aktive Phase**: Phase 3 — Security-Core (Hook + Allow/Deny + Redaction)
**Aktiver Checkpoint**: C3.3 (Redaction-Pipeline, 4 Stages)
**Letzter abgeschlossener Checkpoint**: C3.2 (Deny-Patterns + PIN-Rückfrage, End-to-End)

## Phase 3 — bisheriger Fortschritt

- ✅ C3.1 — `hooks/pre_tool.py` + Shared-Secret-IPC-Endpoint auf `127.0.0.1:8001`
- ✅ C3.2 — Deny-Patterns + PIN-Rückfrage:
  - `domain/deny_patterns.py` (17 Patterns + robuster Matcher, 71 Unit-Tests)
  - `domain/hook_decisions.evaluate_bash` (Spec §12 Decision-Matrix)
  - `domain/pending_confirmations.py` + Port + SQLite-Adapter (15 Unit-Tests)
  - `application/confirmation_coordinator.py` (Futures + WhatsApp + DB)
  - `application/hook_service.py` rewritten (async, optional-deps)
  - `http/meta_webhook.py` intercepts PIN / "nein" vor Command-Router
  - Hook-Endpoint async + fail-closed bei Service-Crash
  - `tests/fixtures/deny/*.json` — 17 Fixtures
  - `tests/integration/test_deny_patterns_e2e.py` — 20 E2E-Cases (YOLO + deny)

**Tests**: 562/562 passing, mypy --strict clean auf whatsbot/ + C3.2-Tests, ruff clean.

## Was als Nächstes (C3.3 → C3.6)

Verbleibende C3-Checkpoints aus `phase-3.md`:

- **C3.3** — Redaction-Pipeline 4 Stages (Spec §10):
  - Stage 1: bekannte Key-Muster (AWS, GitHub, OpenAI, Stripe, JWT, Bearer)
  - Stage 2: strukturell (`KEY=VALUE`, PEM, SSH-Privates, DB-URLs)
  - Stage 3: Entropy (Shannon > 4.5, ≥40 Zeichen)
  - Stage 4: Pfade (`~/.ssh`, `~/.aws`, Keychain)
  - Integration: jede ausgehende WhatsApp-Nachricht durchreichen
- **C3.4** — Input-Sanitization wrappt verdächtige Prompts (nur Normal-Mode)
- **C3.5** — Output-Size-Warning + `/send` / `/discard` / `/save`
- **C3.6** — Fail-closed bei Unreachable/401/Crash/Timeout (bereits weitgehend
  drin; expliziter Integration-Test fehlt noch)

Noch offen als Schuld aus C3.2:
- Write-Hook hat noch den Stub-Pfad (`classify_write` = allow). Die echte
  Path-Rules-Policy (Spec §12 Layer 3) ist im `phase-3.md`-Scope, aber nicht
  als eigener Checkpoint vergeben — idealerweise als Teil von C3.3/C3.5
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
