# Aktueller Stand

**Aktive Phase**: Phase 4 — Mode-System + Claude-Launch (in progress)
**Aktiver Checkpoint**: **C4.1d** — SessionService.ensure_started + `/p`-Wiring + C4.1-Smoke
**Letzter abgeschlossener Checkpoint**: C4.1c (TmuxController + Subprocess-Adapter)

## Phase 4 — laufender Stand (zum Wiederaufnehmen)

- ✅ **C4.1a** — `domain/modes.py` (claude_flags / status-colors / valid_transition)
  + `domain/sessions.py` (ClaudeSession dataclass + context-fill-helpers)
- ✅ **C4.1b** — `ports/claude_session_repository.py` +
  `adapters/sqlite_claude_session_repository.py` (CRUD + 4 hot-path
  partial updates: update_activity / bump_turn / update_mode /
  mark_compact)
- ✅ **C4.1c** — `ports/tmux_controller.py` +
  `adapters/tmux_subprocess.py` (has_session / new_session /
  send_text / kill_session / list_sessions / set_status).
  Integration-Tests skippen wenn `tmux` fehlt.
- ⏭ **C4.1d** — *nächster Schritt morgen*:
  - `application/session_service.py` mit **`ensure_started(project)`** als
    Minimal-Use-Case: mode aus projects-Tabelle, session row aus
    `claude_sessions` (für `--resume`), `tmux new-session` +
    `send_text("safe-claude ...")`, transcript_path persistieren sobald
    Claude das Transcript-File erzeugt.
  - `CommandHandler._handle_set_active` erweitern: `/p <name>` ruft
    `session_service.ensure_started(name)` wenn noch keine Session
    läuft. Status-Bar wird nach `modes.status_bar_color` + `mode_badge`
    gesetzt.
  - `main.py`-Wiring: `SubprocessTmuxController` +
    `SqliteClaudeSessionRepository` + `SessionService` konstruieren.
  - **Tests**:
    - Unit: `SessionService.ensure_started` mit Fake-TmuxController +
      In-Memory-Repo. Fälle: no-session-yet, session-running-already,
      session-tot-neu-starten, mode aus projects lesen.
    - Integration: End-to-end `/p <name>` via `/webhook` → tmux-Session
      existiert → `claude_sessions`-Row befüllt. Skipped wenn `tmux`
      oder `claude` fehlen. `safe-claude` wird mit injectable Binary
      überschrieben; Default-Stub schreibt ein Transcript-JSONL und
      exitiert, damit keine echte Claude-Subscription benötigt wird.

**Tests Stand**: 752/752 passing (+ 3 skipped wegen fehlendem tmux),
mypy --strict clean, ruff clean. Commit-History:

```
f4fb514 feat(phase-4): C4.1c TmuxController port + subprocess adapter
ff5ab93 feat(phase-4): C4.1b claude-session repository
9a09f58 feat(phase-4): C4.1a modes + sessions domain (pure)
d9e34be docs(phase-4): phase 4 rules — Mode-System + Claude-Launch
```

### System-Prerequisites für C4.1d-Smoke

- `tmux ≥ 3.4` via `brew install tmux` — User hat das gerade erledigt.
  Zum Bestätigen am Start der nächsten Session: `which tmux && tmux -V`.
- `claude` CLI ist schon auf `~/.local/bin/claude` (verifiziert).
- Headless-Claude-Stub (`tests/fixtures/headless_claude.py`) muss ich
  in C4.1d bauen — kleines Python-Script, das stdin liest und ein
  plausibles Transcript-JSONL in
  `~/.claude/projects/<encoded>/sessions/<uuid>.jsonl` schreibt.

### Wie wir morgen wieder einsteigen

1. Diese Datei lesen (`.claude/rules/current-phase.md`).
2. `.claude/rules/phase-4.md` für den Gesamt-Phase-Plan.
3. `git log --oneline -6` zeigt den Commit-Stand.
4. `tmux -V` ausführen — wenn grün, sollten die Integration-Tests in
   `tests/integration/test_tmux_subprocess_real.py` jetzt nicht mehr
   skippen, sondern grün durchlaufen (`pytest
   tests/integration/test_tmux_subprocess_real.py -v`).
5. Mit C4.1d anfangen — SessionService bauen.

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
