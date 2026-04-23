# Aktueller Stand

**Aktive Phase**: Phase 7 — Medien-Pipeline ✅ (inhaltlich komplett,
wartet auf User-Freigabe für Phase 8)
**Aktiver Checkpoint**: — (Phase 7 geschlossen)
**Letzter abgeschlossener Checkpoint**: **C7.5** — Cache-Sweeper

## Was End-to-End vom Handy aus funktioniert (Stand nach Phase 7)

Alle Nachrichtentypen laufen durch. Images + PDFs + Voice-Messages
fließen von WhatsApp durchs Meta-Webhook, durch Validation
(MIME + Size + Magic-Bytes), in den Cache, und — für Voice —
durch ffmpeg + whisper-cli → Text → SessionService.send_prompt →
tmux → Claude. User bekommt bei Voice-Messages sofort einen
"🎙 Transkribiere…"-Ack vor der Transkriptions-Latenz.
Unsupported Kinds (Video/Location/Sticker/Contact) bekommen
freundliche Reject-Replies. Cache-Sweeper räumt 7-Tage-alte
Dateien und hält Gesamt-Cache unter 1 GB mit secure-delete.

## Wie ich für Phase 8 wiedereinsteige

1. Diese Datei lesen.
2. `phases-3-to-9.md` Phase-8-Stub als Startpunkt
   (Observability + Max-Limit-Handling: /log, /errors, /ps,
   /metrics, /status, Circuit-Breaker für externe Adapter,
   max_limits-Tabelle füllen + auswerten).
3. **Vor dem Bauen**: `.claude/rules/phase-8.md` schreiben
   (gleiche Struktur wie phase-7.md), User reviewen lassen,
   *dann* erst implementieren.
4. `git log --oneline -32` für den Commit-Stand bis
   Phase-7-Close.
5. `venv/bin/pytest tests/unit/ tests/integration/
   --ignore=tests/unit/test_hook_common.py
   --ignore=tests/integration/test_hook_script.py
   --ignore=tests/integration/test_hook_fail_closed.py`
   sollte **1330/1330 grün** (+ 1 skipped wenn ffmpeg fehlt)
   zeigen. mypy --strict clean auf 107 source files. ruff
   clean (bis auf pre-existing E731 in
   `delete_service.py`).
6. Phase-8-Scope aus `SPEC.md` §21 Phase 8 + §14 (Max-Limit)
   + §15 (Observability):
   - `domain/limits.py` — pure Parser für Transcript-Error-Events
     (`usage_limit_reached`) als primäre Quelle + Status-Line
     Regex als Fallback.
   - `application/limit_service.py` — persistiert `max_limits`-
     Rows, triggert proaktive Warnung bei <10% remaining,
     auto-switch Opus → Sonnet bei Opus-Sub-Limit.
   - `/log <msg_id>`, `/errors`, `/ps`, `/metrics`,
     `/status` Commands ausbauen (Phase-1-Stubs sind da).
   - Prometheus-Text-Format Exposition auf `/metrics` —
     **nur localhost**, nicht über Tunnel erreichbar.
   - Circuit-Breaker-Decorator für alle externen Adapters
     (Meta-API, Whisper, Hook-IPC): 5 Fehler in 60s → 5min
     Pause → Half-Open-Test.

## Pre-existing Schuld (nicht-blockierend für Phase 8)

`claude_sessions.session_id TEXT UNIQUE` kollidiert wenn zwei
frische Sessions beide leeren session_id haben. Fix gehört in
einen Phase-4-Cleanup-Commit (NULL statt empty oder UNIQUE drop).

`whatsbot/application/delete_service.py:48` — E731
(lambda-Assignment für `_DEFAULT_CLOCK`). Phase-2-Erbe;
trivialer `def`-Rewrite, aber außerhalb Phase-7-Scope.

## Pre-existing Schuld (nicht-blockierend für Phase 7)

`claude_sessions.session_id TEXT UNIQUE` kollidiert wenn zwei
frische Sessions (beide leeren session_id) parallel existieren.
Das e2e-Pattern aus Phase 6 (Test seedet das zweite Projekt
DB-direkt) reicht für Tests — Live-Bot ist betroffen wenn ein
User mehr als ein Projekt frisch startet bevor Claude die erste
session_id zurückgibt. Fix gehört in einen Phase-4-Cleanup-
Commit (NULL statt empty oder UNIQUE drop). Wenn das in Phase 7
stört, vorher fixen.

## Was End-to-End vom Handy aus funktioniert (Stand vor Phase 7)

- **Phase 1–4**: Projekte anlegen (`/new` + `/new git`), aktiv-
  Projekt setzen (`/p`), Prompts senden (`/p <name> <prompt>`,
  bare prompt), Mode wechseln (`/mode normal|strict|yolo`),
  Allow-Rules verwalten (`/allow`, `/deny`, `/allowlist`,
  `/allow batch *`).
- **Phase 5**: Lock-Soft-Preemption mit `/release` + PIN-gated
  `/force`. tmux-Status-Bar zeigt Owner-Badge live.
- **Phase 6**: Vier Eskalationsstufen: `/stop` (Ctrl+C) →
  `/kill` (tmux kill-session) → `/panic` (Vollkatastrophe in
  <2s) → `/unlock <PIN>` (Lockdown aufheben). Heartbeat-Pumper
  + Watchdog-LaunchAgent als unabhängiger Backstop. Sleep-
  Awareness (PID-Liveness + Boot-Grace). Lockdown-Filter blockt
  alle Commands außer `/unlock`/`/help`/`/ping`/`/status`.
  StartupRecovery skippt bei Lockdown.

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

- ✅ **C6.4** — Heartbeat-Pumper + Watchdog-LaunchAgent:
  - `domain/heartbeat.py` (pure): `HEARTBEAT_INTERVAL_SECONDS=30`,
    `HEARTBEAT_STALE_AFTER_SECONDS=120`, `is_heartbeat_stale`,
    `format_heartbeat_payload` (header + version + pid + ISO ts).
  - `ports/heartbeat_writer.py` + `adapters/file_heartbeat_writer.py`
    — atomic write (`<path>.tmp` → `os.replace`), parent-dir auto-
    create, `last_mtime`, idempotent `remove`.
  - `application/heartbeat_pumper.py` — async background loop:
    erste Schreibung sofort in `start()` (Watchdog sieht das File
    bei t=0, nicht erst nach 30 s), File-IO über `asyncio.to_thread`
    damit der event loop nie blockiert, Schreibfehler werden
    geloggt aber brechen die Loop nie. `stop()` cancelt sauber +
    löscht das File (damit ein Restart kein stale-mtime sieht).
  - `main.create_app(heartbeat_writer=..., enable_heartbeat=...)`
    + FastAPI `lifespan`-Context: in PROD/DEV automatisch on
    (FileHeartbeatWriter gegen `settings.heartbeat_path`), in TEST
    opt-in. TestClient-Lifespan startet/stoppt den Pumper.
  - `bin/watchdog.sh` — bash-only (kein Python — funktioniert auch
    bei kaputtem venv): liest heartbeat-mtime via portable
    `stat -f %m` / `stat -c %Y` Fallback, kurz-circuited bei
    panic-Marker, killt nur `wb-*` tmux-Sessions (nicht foreign
    sessions), `pkill -9 -f safe-claude` als Backstop, schreibt
    panic-Marker damit der Bot nach Restart in Lockdown bleibt,
    feuert macOS-Notification, JSON-strukturiertes Logging.
    Konfigurierbar via Env-Vars (heartbeat path, panic marker,
    threshold, log path, tmux/pkill/notifier binaries).
  - `launchd/com.DOMAIN.whatsbot.watchdog.plist.template` — neue
    LaunchAgent-Plist: `RunAtLoad=true` + `StartInterval=30`,
    `KeepAlive=false` (Skript ist short-lived per invocation).
    Env-Vars für die Pfade.
  - `bin/render-launchd.sh` rollt jetzt **drei** Plists raus
    (Bot + Backup + Watchdog), validiert + boostraps + enabled
    sie alle. `make undeploy-launchd` cleant alle drei.
  - 8 unit `test_heartbeat.py` (alle Stale-Edges, Payload-Format,
    Konstanten-Sanity).
  - 8 unit `test_file_heartbeat_writer.py` (atomicity-Trace via
    no-tmp-sibling, parent-dir-auto-create, idempotent remove).
  - 9 unit `test_heartbeat_pumper.py` (asyncio): start-idempotent,
    erst-Schreibung in start, stop cancelt + entfernt, write-failure
    crasht Loop nicht, remove-failure brecht stop nicht ab,
    Payload-Format inkl. pid/version/ts.
  - 8 integration `test_watchdog_script.py`: subprocess-getestet
    mit no-op-Stubs auf PATH (tmux/pkill/osascript) — alive-/
    stale-Pfade, panic-Marker-Short-Circuit, only-wb-*-Killing,
    panic-Marker-Touch, Notification, JSON-Log-Format.
  - 1 integration `test_heartbeat_lifespan.py`: TestClient → File
    appears bei startup, verschwindet bei shutdown.
- ✅ **C6.6** — `/unlock <PIN>` + Lockdown-Filter:
  - `application/unlock_service.py` — `UnlockService.unlock(pin)`:
    PIN-Verify via `hmac.compare_digest` gegen Keychain-`panic-pin`
    + `lockdown_service.disengage()`. Pin-Check läuft auch wenn
    Lockdown nicht engaged ist (kein info-leak via timing).
    Wiederverwendet `InvalidPinError` + `PanicPinNotConfiguredError`
    aus `delete_service`.
  - `CommandHandler` Lockdown-Filter ganz oben in `handle()`:
    während Lockdown engaged ist, wird *jeder* Command außer
    `/unlock <PIN>`, `/help`, `/ping`, `/status` mit
    `🔒 Bot ist im Lockdown. /unlock <PIN> zum Aufheben.` geblockt.
    Auch nackte Prompts (das gefährlichste Angriffs-Surface) sind
    geblockt.
  - `CommandHandler._handle_unlock(pin)`: parse'd PIN, ruft
    `unlock_service.unlock`. Replies:
    - korrekte PIN + war engaged → `🔓 Lockdown aufgehoben.`
    - korrekte PIN + nicht engaged → `🔓 Bot war nicht im Lockdown.`
    - falsche PIN → `⚠️ Falsche PIN.` (Lockdown bleibt)
    - missing keychain → `⚠️ Panic-PIN ist im Keychain nicht gesetzt.`
    - bare `/unlock` → `Verwendung: /unlock <PIN>`
  - `StartupRecovery` akzeptiert optional `lockdown_service`-Param.
    Wenn engaged: skip YOLO-Reset + skip session-restore, return
    `RecoveryReport(skipped_for_lockdown=True)` mit `warning`-log.
    Bot bleibt up um `/unlock` zu beantworten, aber relauncht
    keine Claudes.
  - `main.py` baut UnlockService immer (LockdownService ist immer
    da), wired ins CommandHandler-`unlock_service` + `lockdown_service`-
    Params, und reicht LockdownService an StartupRecovery durch.
  - 6 unit `test_unlock_service.py` (PIN-Pfade, constant-time-compare,
    leeres PIN, missing keychain, no-info-leak bei nicht-engaged).
  - 13 unit `test_unlock_command.py` (Reply-Format für alle Pfade,
    Lockdown-Filter blockt /ls /new /p bare-prompts, allows
    /unlock /help /ping /status, no-op wenn LockdownService fehlt).
  - 3 unit `test_startup_recovery_lockdown.py` (skip bei engaged,
    normal bei clear, backward-compat ohne LockdownService).
  - 1 e2e `test_unlock_e2e.py` (real tmux + signed /webhook):
    `/p` → `/panic` → blockierte Replies auf `/ls`/`/p`/bare-prompt
    → wrong PIN → right PIN → `/ls` funktioniert wieder.
- ✅ **C6.5** — Watchdog Sleep-Awareness (PID-Liveness + Boot-Grace):
  - **PID-Liveness-Grace** im `bin/watchdog.sh`: Heartbeat enthält
    die Bot-PID (C6.4-Format `pid=<n>`). Wenn `kill -0 <pid>`
    (echter no-op-signal-Test) lebt, war die Heartbeat-Staleness
    wahrscheinlich Mac-Sleep-Artefakt — Bot war suspended, nicht
    tot. Watchdog skippt engage und loggt `watchdog_grace_pid_alive`.
  - **Boot-Grace**: System-Uptime via portable `sysctl
    -n kern.boottime` (macOS) / `/proc/uptime` (Linux) /
    `WHATSBOT_WATCHDOG_FAKE_UPTIME` (tests). Bei missing-heartbeat
    + Uptime <`WHATSBOT_WATCHDOG_BOOT_GRACE_SECONDS` (default 300)
    skippt der Watchdog (LaunchAgent könnte den Bot noch hochfahren).
    Loggt `watchdog_grace_recent_boot`.
  - Beide Pfade fallen sauber zu engage durch wenn die Heuristik
    nicht greift (PID dead → engage, Uptime >grace + missing
    heartbeat → engage).
  - Plist exposed neue Env-Var
    `WHATSBOT_WATCHDOG_BOOT_GRACE_SECONDS=300`.
  - **Bonus-Fix in watchdog.sh**: pipeline-failures unter
    `set -euo pipefail` mit `|| true` abgesichert (grep no-match
    returns 1, würde sonst den ganzen Skript abbrechen).
  - 5 neue Integration-Tests in `test_watchdog_script.py`:
    PID-alive grace mit own-PID, dead-PID engaged, boot-grace
    bei fake_uptime=10, no boot-grace bei fake_uptime=99999,
    backwards-compat ohne pid= line.

**Tests-Stand**: 1104/1104 passing (1099 + 5 C6.5-Tests).
mypy `--strict` clean auf allen 93 source files, ruff clean auf
allen angefassten Dateien.

**Pre-existing Schuld (unverändert, außerhalb Phase-6-Scope)**:
`claude_sessions.session_id TEXT UNIQUE` kollidiert wenn zwei
frische Sessions beide leeren session_id haben. Fix gehört in
einen Phase-4-Cleanup-Commit (NULL statt empty oder UNIQUE drop).

### Phase 6 inhaltlich + close-commit komplett ✅

Vier Eskalationsstufen vom Handy aus (`/stop`, `/kill`, `/panic`,
`/unlock`), Heartbeat+Watchdog als unabhängiger Backstop,
Sleep-Awareness, Lockdown-Filter, StartupRecovery respektiert
Lockdown. C6.7 (StartupRecovery-Notice an Default-Recipient bei
Lockdown-Skip) wird bewusst nach Phase 8 (Observability)
verschoben.

### Wie für Phase 7 wiedereinsteigen

1. Diese Datei lesen.
2. `phases-3-to-9.md` Phase-7-Stub als Startpunkt
   (Medien-Pipeline: Whisper, ffmpeg, Bilder, PDFs, Cache mit
   Secure-Delete).
3. **Vor dem Bauen**: `.claude/rules/phase-7.md` schreiben
   (gleiche Struktur wie phase-6.md), User reviewen lassen,
   *dann* erst implementieren.
4. `git log --oneline -20` für den Commit-Stand bis Phase-6-Close.
5. `venv/bin/pytest tests/unit/ tests/integration/ --ignore=tests/unit/test_hook_common.py --ignore=tests/integration/test_hook_script.py --ignore=tests/integration/test_hook_fail_closed.py`
   sollte 1104/1104 grün zeigen — Phase-7-Baseline.

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
