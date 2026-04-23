# Aktueller Stand

**Aktive Phase**: Phase 5 — Input-Lock + Multi-Session (in progress)
**Aktiver Checkpoint**: **C5.4** — `/force <name> <PIN> <prompt>` (PIN-gated Lock-Override)
**Letzter abgeschlossener Checkpoint**: C5.3 (Lock-Soft-Preemption End-to-End via /webhook)

## Phase 5 — laufender Stand (zum Wiederaufnehmen)

- ✅ **C5.1a** — `domain/locks.py` (pure): `LockOwner` enum, `SessionLock`
  dataclass, `evaluate_bot_attempt`, `mark_local_input`, `is_expired`,
  `LOCK_TIMEOUT_SECONDS=60`. 14 unit tests.
- ✅ **C5.1b** — `ports/session_lock_repository.py` +
  `adapters/sqlite_session_lock_repository.py` (get/upsert/delete/list_all).
  8 unit tests inkl. CHECK-Constraint-Regression.
- ✅ **C5.1c** — `application/lock_service.py`:
  `acquire_for_bot` (raise `LocalTerminalHoldsLockError` bei Denial),
  `note_local_input`, `release`, `force_bot`, `sweep_expired`, `current`.
  Clock-injectable für Tests. 16 unit tests.
- ✅ **C5.2** — Wiring:
  - `TranscriptIngest.on_local_input`-Callback, fires aus `_handle_user`
    wenn non-ZWSP + non-empty user turn landet.
  - `SessionService.__init__(lock_service=...)`; `send_prompt` ruft
    `acquire_for_bot` vor `tmux.send_text`. `LocalTerminalHoldsLockError`
    propagiert nach oben.
  - `CommandHandler` fängt die Exception in `_dispatch_prompt` und
    rendert `🔒 Terminal aktiv. /force <name> <prompt> oder /release`.
  - Neue Commands `/release` + `/release <name>` (setzt Lock auf FREE).
  - `main.py` verdrahtet **eine** LockService-Instanz in Ingest +
    SessionService + CommandHandler + (vorbereitet für) Sweeper.
  - 3 neue Wiring-Tests (`test_lock_wiring.py`).
- ✅ **C5.3** — End-to-End Integration-Test via `/webhook`:
  preseed local lock → `/p alpha hi` → 🔒-Reply; `/release alpha` →
  Lock weg → `/p alpha hi` funktioniert. Real tmux,
  `safe-claude=/bin/true`. 2 Tests in `test_lock_e2e.py`.

**Tests-Stand**: 956/956 passing (941 + 15 Phase-5-Tests).
mypy `--strict` clean auf allen 79 source files, ruff clean auf
allen angefassten Dateien.

### Was noch offen in Phase 5

- ⏭ **C5.4** — `/force <name> <PIN> <prompt>`: PIN-gated Lock-Override.
  Nutzt die Keychain-`panic-pin` (wie `/rm`). Logisch:
  1. Parse `<name> <PIN> <prompt>` (PIN ist die zweite Token).
  2. Validiere PIN gegen `KEY_PANIC_PIN` via `SecretsProvider`.
  3. Bei match: `lock_service.force_bot(project)` + `send_prompt(project, prompt)`.
  4. Bei miss: `⚠️ Falsche PIN`-Reply.
  CommandHandler.__init__ braucht dafür `SecretsProvider` (oder analog
  zu `DeleteService` einen neuen `ForceService`). Vorschlag: Wiederverwendung
  des vorhandenen `DeleteService`-PIN-Musters oder kleine `ForceService`-
  Hülle, die `SecretsProvider.get(KEY_PANIC_PIN)` einmal liest.
- ⏭ **C5.5** — tmux-Status-Bar um Lock-Owner-Badge erweitern
  (`🟢 NORMAL · 🤖 BOT [wb-alpha]` / `· 👤 LOCAL` / `· — FREE`).
  Aufruf von `_paint_status_bar` muss den aktuellen Lock lesen
  (`lock_service.current(project)`). Kosmetisch, niedrige Prio.
- ⏭ **Phase-5-Close-Commit**: `feat(phase-5): complete phase 5`
  nach C5.4 + C5.5.

### Wie wiedereinsteigen

1. Diese Datei lesen.
2. `.claude/rules/phase-5.md` (Plan-Doc).
3. `git log --oneline -7` für den Commit-Stand seit Phase 4 close.
4. `venv/bin/pytest tests/unit/ tests/integration/ --ignore=tests/unit/test_hook_common.py --ignore=tests/integration/test_hook_script.py --ignore=tests/integration/test_hook_fail_closed.py`
   sollte 956/956 grün zeigen.
5. Mit **C5.4** starten — siehe Plan oben.

## Phase 4 abgeschlossen ✅

Alle 9 Checkpoints grün. Siehe Commit `eb48ca1`
(`feat(phase-4): complete phase 4`) für die volle Zusammenfassung.
**Was End-to-End funktioniert:**

- `/new <name> [git <url>]` legt Projekt an (inkl. Smart-Detection).
- `/p <name>` startet Claude in tmux.
- `/p <name> <prompt>` + nackter Text schickt Prompt; Antwort
  kommt async via Transcript-Watcher → Redaction → WhatsApp.
- `/mode normal|strict|yolo` recycelt die Session, schreibt Audit-
  Row, bewahrt Context via `--resume`.
- Pre-Tool-Hook honoriert den aktiven Mode für Bash **und**
  Write/Edit; Deny-Patterns + protected paths greifen auch in YOLO.
- Auto-Compact bei 80% Context-Fill.
- Bot-Restart resumed jede Session via `--resume` und coerct
  YOLO → Normal.

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
- ✅ **C4.1d** — `domain/launch.py` (pure argv builder +
  shell-safe render), `application/session_service.py` mit
  `ensure_started(project)` (tmux + `claude_sessions` + Statusbar),
  `CommandHandler` nimmt optionalen `SessionService` an und ruft
  `ensure_started` aus `/p <name>` auf, `main.py` wired
  `SubprocessTmuxController + SqliteClaudeSessionRepository +
  SessionService` und akzeptiert für Tests injectable
  `tmux_controller` + `safe_claude_binary`. Headless-Claude-Stub
  in `tests/fixtures/headless_claude.py` für C4.2+.
  Tests: 20 neue Unit-Tests (launch.py + session_service.py) + 3
  neue Command-Handler-Tests (session wiring) + 2
  Integration-Tests (`/p → tmux + claude_sessions` via `/webhook`,
  skipped ohne tmux). mypy --strict clean, ruff clean.

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
