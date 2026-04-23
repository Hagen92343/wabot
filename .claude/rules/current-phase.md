# Aktueller Stand

**Aktive Phase**: Phase 6 — Kill-Switch + Watchdog + Sleep-Handling (in progress)
**Aktiver Checkpoint**: **C6.4** — Heartbeat-Pumper + Watchdog-LaunchAgent
**Letzter abgeschlossener Checkpoint**: C6.2/C6.3 (`/panic` + YOLO-Reset + Lockdown)

## Phase 6 — laufender Stand (zum Wiederaufnehmen)

- ✅ **C6.1** — `/stop` + `/kill`:
  - `TmuxController.interrupt(name)`-Protocol-Methode neu (sendet
    `C-c` als tmux key event, kein Enter, kein `-l`-Literal). Adapter
    + alle 5 FakeTmux-Varianten in den Tests aktualisiert.
  - `application/kill_service.py` mit `stop(name)` (Soft-Cancel via
    `tmux interrupt`, Session bleibt am Leben) und `kill(name)`
    (Hard-Kill via `tmux kill_session` + `lock_service.release`).
    Lock-Release-Failures werden geloggt aber nie hochpropagiert
    (Pane war ja schon weg). `claude_sessions`-Row bleibt bei
    `/kill` — Resume-fähig auf next `/p`.
  - `CommandHandler` routet `/stop`, `/stop <name>`, `/kill`,
    `/kill <name>`. Helper `_resolve_target_project` defaultet auf
    aktives Projekt, validiert Name, liefert sauberen Hint wenn
    kein aktives Projekt + kein Argument. Replies:
    `🛑 Ctrl+C an '...' geschickt.` /
    `🪓 '...' tmux-Session beendet · Lock freigegeben.`.
    Friendly `'...' hatte keine aktive Session.` wenn Pane
    schon tot.
  - `main.py` baut KillService nur wenn tmux vorhanden,
    wired ins CommandHandler-`kill_service`-Param.
  - 9 unit tests `test_kill_service.py` (Soft-Cancel, Hard-Kill +
    Lock-Release, Lock-Failure-Containment, no-LockService-Pfad,
    TmuxError-Propagation, InvalidProjectName).
  - 11 unit tests `test_kill_command.py` (mit-Name + ohne-Name,
    no-active-Pfad, dead-Session-Friendly-Reply, no-config-Guard,
    Lock-Suffix nur wenn was zum Releasen war).
  - 2 e2e `test_kill_e2e.py` (real tmux, signed /webhook,
    `/stop` lässt Session leben, `/kill` killt + released).
- ✅ **C6.2 / C6.3** — `/panic` Vollkatastrophe + YOLO-Reset:
  - `domain/lockdown.py` (pure): `LockdownState`, `engage`,
    `disengaged`, `LOCKDOWN_REASON_*` Konstanten. Engage ist
    idempotent — first-trigger-Metadata bleibt erhalten
    (Forensik). Unbekannte Reason → ValueError.
  - `application/lockdown_service.py`: persistiert in
    `app_state.lockdown` (JSON-blob) + Touch-File
    `/tmp/whatsbot-PANIC` (für Watchdog). Touch-File-Failures
    blocken die DB nie. Tolerant gegen JSON-Garble +
    Partial-Rows beim Lesen.
  - `ports/process_killer.py` + `adapters/subprocess_process_killer.py`:
    `pkill -9 -f <pattern>` mit narrow default-Pattern
    `safe-claude` (Spec-Abbruch-Kriterium: keine fremden
    Claude-Instanzen killen). Exit 1 = no-match = success.
  - `ports/notification_sender.py` + `adapters/osascript_notifier.py`:
    macOS-Notification via `osascript -e 'display notification ...'`,
    no-op fallback wenn osascript fehlt. Failures swallowed.
  - `application/panic_service.py`: orchestriert die 6-step
    Spec-§7-Playbook in genau dieser Reihenfolge:
    (1) Lockdown engage → (2) wb-* enumerate + kill_session →
    (3) `pkill -9 -f safe-claude` Backstop →
    (4) YOLO → Normal pro Projekt + `mode_events.event='panic_reset'` →
    (5) Locks release pro Projekt →
    (6) macOS-Notification mit Sound.
    Idempotent (zweiter panic-call ist safe, lockdown_at bleibt).
    Klobeck-failures (notifier, killer, audit) werden geloggt
    aber brechen die anderen Schritte nie.
  - `CommandHandler._handle_panic`: keine PIN per Spec §5,
    Reply `🚨 PANIC! N Sessions getötet, M YOLO → Normal,
    K Locks freigegeben, in X ms.\nBot ist im Lockdown.
    /unlock <PIN> zum Aufheben.`. Innere Exceptions werden
    abgefangen, User sieht "Pruefe /errors am Mac".
  - `Settings`: neue Felder `panic_marker_path` (default
    `/tmp/whatsbot-PANIC`) und `heartbeat_path` (default
    `/tmp/whatsbot-heartbeat`, vorbereitet für C6.4).
  - `main.py` baut LockdownService immer (auch ohne tmux —
    z.B. wenn andere Layer eine Lockdown-Engage brauchen),
    PanicService nur wenn tmux + lock_service vorhanden.
    `process_killer` und `notifier` sind injectable für Tests;
    Default-Adapters in non-test-env.
  - 5 unit tests `test_lockdown.py` (alle Pure-Übergänge).
  - 10 unit tests `test_lockdown_service.py` (engage/disengage
    Roundtrip, Idempotenz, Marker-Failure-Containment, JSON-
    Tolerance bei Garble + Partial-Rows).
  - 9 unit tests `test_panic_service.py` (Full-Playbook,
    Lockdown-vor-Sessions-Ordering-Invariante, killer-failure-
    Containment, notifier-failure-Containment, non-wb-Sessions
    überleben, audit-Rows nur für YOLOs, Idempotenz, Latenz <2s).
  - 4 unit tests `test_panic_command.py` (Reply-Format, kein
    PIN-Parsing, no-config-Guard, Inner-Exception → friendly
    Reply).
  - 1 e2e `test_panic_e2e.py` (real tmux, signed /webhook —
    wb-* killed, foreign survives, BOTH YOLOs reset, audit-
    Rows, Lockdown engaged in DB, Touch-File auf Disk,
    `safe-claude` kommt im Killer-Pattern an).

**Tests-Stand**: 1044/1044 passing (1015 + 29 C6.2/C6.3-Tests).
mypy `--strict` clean auf allen 88 source files, ruff clean auf
allen angefassten Dateien.

**Pre-existing Schuld (außerhalb C6.2-Scope)**:
`claude_sessions.session_id TEXT UNIQUE` kollidiert wenn zwei
frische Sessions beide leeren session_id haben. e2e umgeht das
indem nur eine wb-*-Session per /p hochgefahren wird; das
zweite YOLO-Projekt wird DB-direkt geseedet. Fix gehört in
einen Phase-4-Cleanup-Commit (NULL statt empty oder UNIQUE drop).

### Was noch offen in Phase 6

- ⏭ **C6.4** — Heartbeat-Pumper + Watchdog-LaunchAgent.
- ⏭ **C6.5** — pmset Sleep/Wake-Handling.
- ⏭ **C6.6** — `/unlock <PIN>` + Lockdown-Filter im CommandHandler.
- ⏭ **C6.7** — Edge-Cases.

### Wie wiedereinsteigen

1. Diese Datei lesen.
2. `.claude/rules/phase-6.md` (Plan-Doc).
3. `git log --oneline -14` für den Commit-Stand.
4. `venv/bin/pytest tests/unit/ tests/integration/ --ignore=tests/unit/test_hook_common.py --ignore=tests/integration/test_hook_script.py --ignore=tests/integration/test_hook_fail_closed.py`
   sollte 1044/1044 grün zeigen.
5. Mit **C6.4** starten — siehe Plan oben (Heartbeat + Watchdog).

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
- ✅ **C5.4** — `/force <name> <PIN> <prompt>` PIN-gated Lock-Override:
  - `application/force_service.py` — `ForceService.force(name, pin)`:
    validate name → check project exists (FK-safety) → PIN-Check via
    `hmac.compare_digest` gegen Keychain-`panic-pin` →
    `lock_service.force_bot(name)`. Wiederverwendet
    `InvalidPinError` + `PanicPinNotConfiguredError` aus
    `delete_service` (gleiche Semantik, gleicher Keychain-Key).
  - `CommandHandler._handle_force(args)`: parse'd 3 Tokens
    (`<name> <PIN> <prompt>`, Prompt darf Leerzeichen + weitere
    PIN-artige Strings enthalten via `split(maxsplit=2)`), bei
    PIN-Match → `force_service.force` + `session_service.send_prompt`,
    Reply `🔓 Lock fuer 'name' uebernommen.\n📨 an name: <preview>`.
    Bei PIN-Miss → `⚠️ Falsche PIN`. Lock bleibt LOCAL bei Fehler.
  - `_dispatch_prompt`-Hint korrigiert: `/force <name> <PIN> <prompt>`
    statt der irreführenden alten Version ohne PIN.
  - `main.py` baut ForceService nur, wenn lock_service + session_service
    vorhanden sind; wired ins CommandHandler-`force_service`-Param.
  - 7 unit tests `test_force_service.py` (PIN-Pfade, Project-FK,
    Constant-Time-Compare, Lock unverändert bei Mismatch).
  - 12 unit tests `test_force_command.py` (Parsing inkl.
    Whitespace-Edge, no-config-Guard, Hint-Korrektur-Regression,
    Idempotenz ohne Vorlock).
  - 1 e2e test `test_lock_e2e.py::test_force_overrides_local_lock_with_pin`
    (real tmux, /webhook, signed payload, wrong-PIN → keep LOCAL,
    right-PIN → flip to BOT + 📨).
- ✅ **C5.5** — tmux-Status-Bar Lock-Owner-Badge:
  - `domain/locks.py` — pure `lock_owner_badge(owner)`:
    BOT → `🤖 BOT`, LOCAL → `👤 LOCAL`, FREE/None → `— FREE`.
  - `SessionService._paint_status_bar` rendert jetzt
    `{mode_badge} · {owner_badge} [tmux_name]` (z.B.
    `🟢 NORMAL · 🤖 BOT [wb-alpha]`); liest Owner via `_locks.current`.
  - Neue public `SessionService.repaint_status_bar(project)` —
    no-op wenn tmux tot oder Project missing, swallowt
    Excepetions (rein kosmetisch, darf nie fail-closed werden).
  - `LockService.__init__(on_owner_change=...)`-Callback,
    feuert nur bei Owner-*Wechsel* (nicht bei no-op-Refresh):
    acquire_for_bot (erst-grant), force_bot (flip from non-BOT),
    note_local_input (flip from non-LOCAL), release (existing row),
    sweep_expired (per reaped project). Callback-Fehler werden
    geloggt, brechen aber die Lock-Op nie.
  - `main.py` verdrahtet `LockService.on_owner_change → 
    SessionService.repaint_status_bar` via Forward-Ref-Liste
    (gleiche Pattern wie für auto-compact).
  - Test-Regression: `test_session_service.py` fresh-start label
    von `🟢 NORMAL [wb-alpha]` auf `🟢 NORMAL · — FREE [wb-alpha]`
    angepasst.
  - 17 unit tests `test_lock_status_badge.py`: 4 pure-helper-Tests,
    5 paint-Layer-Tests (BOT/LOCAL/FREE-Badge, repaint-no-op-Pfade),
    8 callback-Tests (alle Operationen × no-op-vs-flip).

**Tests-Stand**: 993/993 passing (976 + 17 C5.5-Tests).
mypy `--strict` clean auf allen 80 source files, ruff clean auf
allen angefassten Dateien.

### Phase 5 inhaltlich abgeschlossen

Alle C5.x grün, CHANGELOG.md-Eintrag geschrieben,
`feat(phase-5): complete phase 5`-Sammel-Commit gemacht.
Wartet jetzt auf User-Freigabe für **Phase 6 — Kill-Switch +
Watchdog + Sleep-Handling** (siehe Spec §21 + `phases-3-to-9.md`).

### Wie für Phase 6 wiedereinsteigen

1. Diese Datei lesen.
2. `phases-3-to-9.md` Phase-6-Stub als Startpunkt.
3. **Vor dem Bauen**: `.claude/rules/phase-6.md` schreiben (gleiche
   Struktur wie phase-5.md), User reviewen lassen, *dann* erst
   implementieren.
4. `git log --oneline -12` für den Commit-Stand bis Phase-5-Close.
5. `venv/bin/pytest tests/unit/ tests/integration/ --ignore=tests/unit/test_hook_common.py --ignore=tests/integration/test_hook_script.py --ignore=tests/integration/test_hook_fail_closed.py`
   sollte 993/993 grün zeigen — die Phase-6-Baseline.

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
