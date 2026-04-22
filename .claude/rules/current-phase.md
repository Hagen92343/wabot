# Aktueller Stand

**Aktive Phase**: Phase 3 — Security-Core ✅ COMPLETE
**Aktiver Checkpoint**: — (Phase 4 freigabebereit)
**Letzter abgeschlossener Checkpoint**: C3.6 (Fail-closed Hook-Integration-Smoke)

## Phase 3 abgeschlossen ✅

Alle 6 Checkpoints grün, Phase 3 komplett gebaut und verifiziert.

- ✅ C3.1 — `hooks/pre_tool.py` + Shared-Secret-IPC-Endpoint auf `127.0.0.1:8001`
- ✅ C3.2 — Deny-Patterns (17) + PIN-Rückfrage End-to-End
- ✅ C3.3 — Redaction-Pipeline (4 Stages) + globaler Sender-Decorator
- ✅ C3.4 — Input-Sanitization + Audit-Log
- ✅ C3.5 — Output-Size-Warning (>10KB) + `/send` / `/discard` / `/save`
- ✅ C3.6 — Fail-closed Hook-Integration-Smoke

**Tests**: 689/689 passing, mypy --strict clean, ruff clean (bis auf
einen pre-existing E731 in `delete_service.py` aus Phase 2).

Defense in Depth steht:

- **Layer 1**: Input-Sanitization (Normal-Mode wrappt suspekte Prompts,
  Strict/YOLO Bypass). Audit-Log feuert in allen Modi.
- **Layer 2**: Pre-Tool-Hook mit 17 Deny-Patterns + Mode-Matrix
  (`evaluate_bash`). 5-min-PIN-Rückfrage über async Coordinator,
  FIFO-Routing für PIN/"nein"-Antworten.
- **Layer 3 (teilweise)**: Path-Rules für Write/Edit als Stub
  (allow-by-default) — nachzuziehen.
- **Layer 4**: 4-Stage-Redaction auf allem Outbound (known keys,
  struktur, entropy, sensitive paths) + Output-Size-Dialog ab 10KB.

## Was als Nächstes: Phase 4

Phase 4 — **Mode-System + Claude-Launch** (4-5 Sessions, größte Phase).
Voraussetzungen: Phase 2 + Phase 3 beide durch ✅.

Zu bauen (Spec §6, §7, §8; Gotchas aus `phases-3-to-9.md`):

- tmux-Session-Management pro Projekt
- `--resume <session-id>` + Session-ID-Persistenz
- Transcript-Watching (event-basiert via watchdog, nicht polling)
- Token-Count aus `message.usage`-Feldern
- Mode-Switch via `/mode <normal|strict|yolo>` mit Session-Recycle
  (kill + neu starten mit passendem Flag, ID via `--resume` bewahrt)
- YOLO→Normal-Reset bei Reboot (nicht optional)
- Auto-Compact bei 80% Context-Fill
- Bot-Prompts mit Zero-Width-Space-Prefix markieren (damit das
  Transcript-Watching Bot- von User-Input unterscheiden kann)

**Vor Beginn**: `.claude/rules/phase-4.md` schreiben (gleiche Struktur
wie `phase-1.md`/`phase-2.md`/`phase-3.md`, basierend auf Spec §21
Phase 4). User-Freigabe einholen. Dann erst bauen.

Offene Schuld aus Phase 3 (nicht-blockierend):
- Write-Hook-Stub (`classify_write` = allow). Die echte Path-Rules-
  Policy (Spec §12 Layer 3) sinnvollerweise als Teil von Phase 4
  nachziehen, wenn Write von Claude tatsächlich getriggert wird.

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
