# Phase 4: Mode-System + Claude-Launch

**Aufwand**: 4-5 Sessions (größte Phase)
**Abhängigkeiten**: Phase 2 + Phase 3 beide komplett ✅
**Parallelisierbar mit**: —
**Spec-Referenzen**: §5 (Auth-Lock), §6 (3-Modi-System),
§7 (tmux, Transcript, Pre-Tool-Hook), §8 (Context), §19 (`claude_sessions`)

## Ziel der Phase

Claude läuft jetzt *wirklich*. Pro Projekt gibt es eine tmux-Session
mit einem `safe-claude`-Wrapper-Subprocess im passenden Permission-
Modus; Bot-Prompts fließen per `tmux send-keys` in die Session;
Claude-Antworten werden aus dem Transcript-JSONL geparsed und landen
über die bereits-gebaute Redaction + Output-Pipeline auf WhatsApp.
`/mode` recycelt die Session mit `--resume`, so dass der Context
erhalten bleibt. Bei 80 % Token-Fill feuert automatisch `/compact`.
Nach Reboot werden alle Sessions wiederhergestellt — außer YOLO, das
wird deterministisch zu Normal zurückgesetzt.

Phase 4 endet mit einem Bot, der End-to-End vom Handy fernsteuerbar
ist: Projekt auswählen, Prompt schicken, Antwort zurückbekommen, Mode
wechseln, alles über Reboots hinweg stabil.

## Voraussetzungen

- **Claude Code** installiert, mit `claude /login` + Max-20x-Subscription
  authentifiziert. `claude /status` zeigt "subscription", nicht "API".
  `bin/preflight.sh` aus Phase 1 prüft das beim Bot-Start.
- **tmux ≥ 3.4** (für `@variables` und `set-option`-Theming). Schon
  in `brew install`-Liste seit Phase 1.
- **watchdog**-Library in `requirements.txt` — die wird in diesem
  Phase-Checkpoint eingeführt und gepinnt.

## Was gebaut wird

### 1. Domain — Modes + Sessions (pure)

- **`whatsbot/domain/modes.py`** — Mode-State-Transitions.
  Pure Logik über `Mode` (existiert in `domain/projects.py` aus Phase 2):
  - `valid_transition(from_mode, to_mode)` — alle 6 Transitions sind
    erlaubt; Funktion existiert trotzdem, damit zukünftige Regeln
    (z.B. "YOLO → Strict erzwingt Intermediate-Normal") ein Zuhause
    haben.
  - `claude_flags(mode)` — liefert die CLI-Flags pro Modus
    (`[]` / `["--permission-mode", "dontAsk"]` /
    `["--dangerously-skip-permissions"]`). Pure Lookup, keine
    Magic-Strings in den Adapters.
  - `status_bar_color(mode)` — `"green"` / `"blue"` / `"red"` für
    die tmux-Statuszeile.

- **`whatsbot/domain/sessions.py`** — `ClaudeSession`-Dataclass, die
  die `claude_sessions`-Tabelle aus Spec §19 spiegelt:
  `project_name`, `session_id`, `transcript_path`, `started_at`,
  `turns_count`, `tokens_used`, `context_fill_ratio`,
  `last_compact_at`, `last_activity_at`, `current_mode`.
  - Helper `context_fill_ratio(tokens_used, limit=200_000)` — reine
    Rechnung, Input für Auto-Compact.
  - `AUTO_COMPACT_THRESHOLD = 0.80` + `should_auto_compact(ratio)`.

- **`whatsbot/domain/transcript.py`** — pure Parser für die
  Claude-Code-JSONL-Events. Nimmt eine Zeile rein, gibt ein
  typisiertes Event raus (`UserEvent`, `AssistantEvent`,
  `ToolUseEvent`, `ToolResultEvent`, `SystemEvent`, `ErrorEvent`).
  - Token-Extraktion: `message.usage.input_tokens` +
    `message.usage.output_tokens` +
    `message.usage.cache_creation_input_tokens` +
    `message.usage.cache_read_input_tokens`.
  - Sidechain-Filter: `isSidechain=true` → skip (Subagent).
  - `isApiErrorMessage=true` → skip.
  - Bot-Prefix erkennen: Events deren User-Message mit
    `​` (Zero-Width-Space) beginnt, sind Bot-eigene Prompts.

### 2. Ports

- **`whatsbot/ports/tmux_controller.py`** — Protocol:
  - `has_session(name) -> bool`
  - `new_session(name, cwd)` — legt tmux-Session an.
  - `start_claude(name, session_id, mode, resume=True)` — schickt
    `safe-claude --resume <id>` + Flags in die Session.
  - `send_keys(name, text)` — schickt Prompt als Keypress-Sequenz.
  - `kill_session(name)` — `tmux kill-session`.
  - `list_sessions()` — alle `wb-*`-Namen.
  - `set_status(name, mode, owner)` — Statuszeile refreshen
    (farbkodiert).
  - `capture_pane(name, lines=20)` — Fallback für Max-Limit-Parsing
    (Phase 8).

- **`whatsbot/ports/transcript_watcher.py`** — Protocol:
  - `watch(path, callback)` → liefert Token, mit dem man
    `unwatch()` machen kann.
  - `read_since(path, offset)` — cold-path, für Reboot-Recovery
    und Backfill.

- **`whatsbot/ports/claude_session_repository.py`** — CRUD über
  `claude_sessions`.

### 3. Adapter

- **`whatsbot/adapters/tmux_subprocess.py`** — konkreter
  `TmuxController`. Shell-freier Aufruf (`subprocess.run(["tmux",
  "...", ...], check=True, ...)`). `send_keys` escaped potentielle
  Meta-Characters, bevor sie an tmux gehen.

- **`whatsbot/adapters/watchdog_transcript_watcher.py`** — nutzt
  `watchdog.observers.Observer` + `FileSystemEventHandler`. Pro
  watch ein eigener `Observer`; `unwatch()` stoppt ihn.
  Ressourcen-sauber im `__aexit__`.

- **`whatsbot/adapters/sqlite_claude_session_repository.py`** —
  Standard-Sqlite-Pattern, gleicher Stil wie
  `sqlite_pending_confirmation_repository`.

### 4. Application

- **`whatsbot/application/session_service.py`** — zentraler
  Use-Case:
  - `ensure_started(project)` — wenn keine tmux-Session existiert
    oder Claude darin tot ist, neu starten. Liest Mode aus der
    `projects`-Tabelle, Session-ID aus `claude_sessions` (für
    `--resume`). Erzeugt einen frischen Session-Record, wenn keiner
    da ist; setzt `transcript_path` nach dem ersten geschriebenen
    Event (der Watcher triggert das).
  - `send_prompt(project, text)` — sanitize durch
    `domain/injection.sanitize` (mit Projekt-Mode), Zero-Width-
    Space-Prefix drankleben, `tmux send-keys`, auf
    Turn-Ende warten (Transcript-Stop-Kriterium).
  - `on_turn_complete(project)` — Token-Totals lesen, Auto-Compact
    triggern wenn ≥ 80 %, Response via Redaction + OutputService
    an WhatsApp schicken.
  - `recycle(project, new_mode)` — `tmux kill-session`, Mode in DB
    updaten, `ensure_started`, dabei kommt `--resume <id>` wieder
    rein (ID bleibt konstant über Mode-Wechsel).

- **`whatsbot/application/mode_service.py`** — dünne Hülle um
  `/mode`:
  - `change_mode(project, new_mode)` — validate Transition,
    persistieren, `session_service.recycle`, Audit-Event
    `mode_events` schreiben (Tabelle schon in Spec §19, neu
    benutzt).
  - Mode-Switch bearbeitet zusätzlich die per-projekt-
    `CLAUDE.md` (Re-render aus Template, sodass die
    `<untrusted_content>`-Instruction im Modus stimmt).

- **`whatsbot/application/transcript_ingest.py`** — orchestriert
  den Watcher-Callback:
  - Jede neue Transcript-Zeile parsen.
  - Bei `assistant`-Event ohne folgendes `tool_use` → Turn-Ende
    erkennen → `session_service.on_turn_complete`.
  - Token-Running-Sum aktualisieren und in
    `claude_sessions.tokens_used` persistieren.
  - Sidechain/Error-Events skippen.
  - `max_limits`-Tabelle füllen, wenn ein `usage_limit_reached`-
    Event kommt (Parser schon da, Handling vollständig in Phase 8).

- **`whatsbot/application/startup_recovery.py`** — Bot-Startup-
  Hook:
  - `reset_yolo_to_normal()` — `UPDATE projects SET mode='normal'
    WHERE mode='yolo'`, Audit-Event
    `mode_events.event='reboot_reset'`.
  - `restore_sessions()` — alle Rows aus `claude_sessions`, für
    jedes `session_service.ensure_started(project)`.

### 5. Wiring

- **`bin/safe-claude`** bekommt das komplette Env-Unset (schon aus
  Phase 1) plus Pass-through der Mode-Flags. Nichts zu ändern
  normalerweise, aber ein Checkpoint-Smoke testet, dass
  `safe-claude --permission-mode dontAsk -r <id>` tatsächlich
  Strict startet.

- **`whatsbot/main.py`** wired alles: Tmux-Adapter, Watcher-Adapter,
  SessionService, ModeService, TranscriptIngest, StartupRecovery,
  und hängt den SessionService an den bestehenden CommandHandler
  (neuer Eintrag: `/mode`, außerdem `/p <name> <prompt>` bekommt
  echte Durchreiche über `send_prompt`).

- **CLAUDE.md-Template**: pro-Projekt-`CLAUDE.md` wird von
  `ProjectService` bei `/new` erzeugt (das war Phase 2). Phase 4
  erweitert den Template-Renderer um den Mode-Hinweis und die
  `<untrusted_content>`-Instruction (Spec §9).

### 6. Commands

- **`/mode`** (ohne Args) → zeigt aktuellen Mode.
- **`/mode <normal|strict|yolo>`** → Session-Recycle.
- **`/p <name> <prompt>`** → echter Durchreichprompt (war in Phase 2
  nur geloggt).
- **Nicht-Slash-Text** → Prompt an aktives Projekt (war auch geloggt).

## Checkpoints

### C4.1 — tmux + Claude starten (Normal-Mode)

- SessionService.ensure_started legt `wb-testproj`-Session an,
  feuert `safe-claude -r ""` ab (leere Resume-ID → frische
  Session), liest die vergebene Session-UUID aus dem Transcript-
  Verzeichnis, schreibt sie in `claude_sessions`.
- tmux-Statuszeile zeigt `🟢 NORMAL`.

```bash
# manueller Smoke:
/new testproj
/p testproj
# -> tmux has-session -t wb-testproj → 0
# -> claude_sessions.session_id befüllt
```

### C4.2 — Prompt-Roundtrip

- `/p testproj hi` (oder nackter Text `hi` an aktives Projekt).
- `send_keys` landet den Prompt mit ZWSP-Prefix in der Session.
- Transcript-Watcher erkennt Turn-Ende.
- Response geht durch Redaction + Output-Pipeline an WhatsApp.

### C4.3 — /mode strict

- `/mode strict` recycelt die Session, blauer Status-Bar.
- DB: `projects.mode` + `claude_sessions.current_mode` = `strict`.
- `mode_events`-Row mit `event='switch'`, `from_mode='normal'`,
  `to_mode='strict'`.

### C4.4 — Strict denies unknown commands silently

- In Strict ein Prompt senden, der Claude zu einem Command
  außerhalb der Allow-List animiert (z.B. `"lösche alle docker
  volumes"` — Claude will `docker volume rm`, das ist in der
  Spec-§12-Deny-List *und* nicht in der Allow-List).
- Pre-Tool-Hook (aus Phase 3) blockt. Strict leitet kein
  Handy-Confirmation — Claude sieht einfach Deny und antwortet
  mit "kann ich nicht, ist nicht erlaubt".

### C4.5 — /mode yolo

- `/mode yolo` → roter Status-Bar, `--dangerously-skip-permissions`
  auf der Kommandozeile.
- **Deny-Patterns greifen trotzdem** (Phase 3 Layer-4-Invariante).
- Output-Size-Dialog + Redaction greifen trotzdem.

### C4.6 — Bot-Restart recovery

- Bot killen (`launchctl kickstart`), dann neuer Start.
- `StartupRecovery.restore_sessions` liest `claude_sessions`,
  legt tmux an, feuert `safe-claude --resume <id>`.
- Ein anschließender Prompt ans Projekt findet Claude mit
  komplettem Context wieder vor.

### C4.7 — YOLO → Normal bei Reboot

- YOLO-Projekt anlegen, Bot-Restart.
- Nach Restart: `projects.mode = 'normal'`,
  `mode_events.event='reboot_reset'`, tmux-Bar grün.

### C4.8 — Auto-Compact bei 80 %

- Künstlich tokens_used auf 165 000 setzen
  (`claude_sessions.tokens_used = 165000`) und einen weiteren Turn
  simulieren.
- Transcript-Ingest berechnet Ratio ≥ 0.80, feuert `/compact` in
  die Session, persistiert `last_compact_at`.

### C4.9 — Write/Edit Path-Rules (Nachzug aus Phase 3)

Kurzer Schlusscheckpoint: der `classify_write`-Stub (aus C3.2c) wird
durch die echte Spec-§12-Layer-3-Policy ersetzt, sobald Claude in
Phase 4 tatsächlich Write/Edit macht und wir die Policy live
validieren können.

- **`whatsbot/domain/path_rules.py`** — pure:
  - Erlaubt: `~/projekte/<current>/*` und `/tmp/*`.
  - Deny in allen Modi: `.git/`, `.vscode/`, `.idea/`, `.claude/`
    (außer `.claude/commands|agents|skills` — Spec §12 Layer 3).
  - Normal → AskUser bei Pfaden außerhalb des erlaubten Scopes.
  - Strict → Deny silent.
  - YOLO → Allow (außer Deny-Pfade).
- `HookService.classify_write` ruft `path_rules.evaluate_write`,
  AskUser geht über den bestehenden `ConfirmationCoordinator` (neuer
  Kind `hook_write`).
- Tests: Unit für path_rules, Integration analog zu
  `test_deny_patterns_e2e` (Fixture-Pack irrelevant, aber E2E gegen
  TestClient).

## Success Criteria

- [ ] Drei Modi funktionieren mit Session-Recycle, Status-Bar
      farbkorrekt.
- [ ] `--resume` bewahrt Context über Mode-Switches.
- [ ] Transcript-Watching erkennt Turn-Ende zuverlässig (nicht
      Polling-basiert).
- [ ] YOLO→Normal-Reset bei Bot-Restart passiert deterministisch.
- [ ] Auto-Compact triggert bei ≥ 80 % Token-Fill einmalig pro
      Turn.
- [ ] Sessions überleben Bot-Neustart via `--resume`.
- [ ] Spec-§12-Deny-Patterns greifen in allen drei Modi (Regression
      gegen die C3.2-Tests).
- [ ] Write/Edit läuft durch echte Path-Rules (kein Stub mehr).
- [ ] Alle C4.1–C4.9 Tests grün.
- [ ] `make test` grün, `mypy --strict` clean.

## Abbruch-Kriterien

- **`claude --resume <id>` bricht bei Mode-Wechsel ab** (z.B. weil
  die Permission-Flag-Änderung Claude-Code dazu bringt, die Session
  zu verwerfen): Stop. Fallback `Fresh Session` dokumentieren (neue
  Session-ID, `last_compact_at` übernehmen, CLAUDE.md re-rendern)
  und User-Review.
- **Transcript-Pfad-Schema hat sich geändert**: Spec §7 nennt den
  Pfad als verifiziert — wenn er bei einer neuen Claude-Code-
  Version nicht mehr stimmt, Stop und Pfad-Detection einbauen.
- **`--dangerously-skip-permissions` feuert den Pre-Tool-Hook
  nicht**: Das widerspricht Spec §7 (dort explizit "Hook feuert in
  allen Modi"). Stop, verifizieren, gegebenenfalls einen zusätzlichen
  Watchdog-Layer nachziehen.

## Was in Phase 4 NICHT gebaut wird

- **Input-Lock** (bot ↔ local terminal race condition) — Phase 5.
- **Kill-Switch + Watchdog + Sleep-Handling** — Phase 6.
- **Medien-Pipeline** (Bilder, Audio, PDFs) — Phase 7.
- **Max-Limit-Handling + proaktive Warnung** — Phase 8
  (`max_limits`-Tabelle wird in Phase 4 nur befüllt, nicht
  ausgewertet).
- **`/ps`, `/info`, `/tail`, `/metrics`** — Phase 8.

Der Write-Hook-Path-Rules-Nachzug aus Phase 3 läuft **in** Phase 4
als C4.9 mit — damit vermeiden wir, dass Claude während C4.1-C4.8
unbemerkt mit Writes durch die Gegend schießt, ohne dass es einen
zusätzlichen Vorab-Checkpoint braucht. Während der Zwischenzeit
gilt der Stub (allow-by-default) plus die native Claude-Code-
Protection von `.git`, `.claude` etc.

## Architektur-Hinweise

- Das einzige Subsystem, das Prozessstate außerhalb der DB hat,
  ist der in-memory `ConfirmationCoordinator` aus Phase 3 und der
  neue Transcript-Watcher. Beide laufen im gleichen Event-Loop wie
  der Meta-Webhook — SessionService darf sync sein, muss aber
  seine externen Callers async lassen (Command-Handler läuft sync,
  die Integration passt via `asyncio.to_thread` oder durch
  async-Methoden auf SessionService selbst).

- Die `claude_sessions`-Tabelle ist die Single-Source-of-Truth.
  tmux kann jederzeit abrauchen — wir verlassen uns nicht auf
  `tmux list-sessions` als State-Check, sondern machen
  `has_session + process_alive` aus tmux + ps.

- Test-Strategie: Tmux + Claude sind externe Prozesse, die wir in
  Unit-Tests stubben. Für Integration gibt es zwei Ebenen:
  1. Dry-Run-Adapter, der `send_keys`/`start_claude` nur loggt.
  2. Ein Headless-Claude-Stub (kleines Python-Script, das stdin
     liest und ein realistisches JSONL-Transcript schreibt). Dieser
     Stub reicht für C4.1-C4.8-Smokes ohne echte Claude-Subscription.

## Nach Phase 4

Update `.claude/rules/current-phase.md` auf Phase 5. Phase 5
(Input-Lock + Multi-Session) ist parallel zu Phase 6 parallelisierbar,
aber nicht zu Phase 4 — Phase 4 muss durch sein.
