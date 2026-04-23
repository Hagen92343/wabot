# Phase 6: Kill-Switch + Watchdog + Sleep-Handling

**Aufwand**: 1-2 Sessions
**Abhängigkeiten**: Phase 4 komplett ✅ (Phase 5 nicht zwingend, aber
faktisch durch — wir nutzen `LockService` für Per-Projekt-Releases bei
`/panic`)
**Parallelisierbar mit**: Phase 7
**Spec-Referenzen**: §4 (Heartbeat-Pfad, Panic-Marker, Watchdog-Plist),
§5 (PIN-Schutz für `/unlock`), §6 (YOLO→Normal bei Panic — analog zur
Bot-Reboot-Recovery), §7 (Kill-Switch + Dead-Man's-Switch),
§11 (Commands), §15 (Audit-Log), §19 (`app_state.lockdown`,
`mode_events`), §21 Phase 6, §23 (Recovery-Playbooks),
§25 FMEA #12 (Laptop-Sleep)

## Ziel der Phase

**Notfall-Kontrolle steht.** Egal ob Claude ein Tool falsch laufen
hat, ob die Subscription leer läuft, ob der Bot kaputt geht oder ob
der User einfach nervös wird — vom Handy aus muss man jederzeit
verlässlich alles abschießen können. Vier Eskalationsstufen:

1. **`/stop [name]`** — Soft cancel: schickt Ctrl+C in den Pane.
   Claude bricht den aktuellen Turn ab, Session lebt weiter.
2. **`/kill [name]`** — Hard kill: tmux-Session weg, Claude-
   Subprocess (über tmux) auch weg. DB-Row `claude_sessions` bleibt
   (für späteren Resume), Lock wird released.
3. **`/panic`** — Vollkatastrophe: alle `wb-*`-Sessions + harter
   `pkill -9 -f claude`, YOLO-Projekte werden auf Normal zurück-
   gesetzt, Lockdown-Marker wird gesetzt, Locks werden gecleart.
4. **`/unlock <PIN>`** — PIN-gated, hebt Lockdown auf.

Plus die **Defense-in-Depth-Backstop**: ein separater LaunchAgent
(„Watchdog") der unabhängig vom Bot läuft. Wenn der Bot kein
Heartbeat-File mehr aktualisiert (Bot abgeraucht / Loop-hängt /
launchd hat ihn nicht restart-bekommen), killt der Watchdog
nach 2 min alle `wb-*`-Sessions + Claude-Prozesse — sicherer
Hard-Stop selbst wenn die Hauptkontrollebene weg ist.

Plus **Sleep-Awareness**: macOS-Sleep darf den Watchdog nicht
auslösen. pmset-Sleep-Events pausieren den Heartbeat-Check, Wake-
Events nehmen ihn wieder auf.

Phase 6 endet damit, dass:

1. `/stop`, `/kill`, `/panic`, `/unlock` über das `/webhook`-Routing
   funktionieren, mit allen Side-Effects.
2. Lockdown-Marker (in `app_state` + Touch-File `/tmp/whatsbot-PANIC`)
   persistent ist und neue Bot-Starts respektieren ihn (keine
   Auto-Recovery in Lockdown).
3. Bot schreibt periodisch `/tmp/whatsbot-heartbeat`.
4. Watchdog-LaunchAgent läuft als separater Prozess, schaut alle
   30 s auf den Heartbeat, killt bei >120 s Lücke.
5. Sleep-Monitor pausiert die Watchdog-Check-Logik bei pmset
   Sleep-Events und nimmt sie bei Wake-Events wieder auf.

## Voraussetzungen

- **Phase 4** komplett (TmuxController, SessionService, ModeService,
  StartupRecovery, claude_sessions-Repo).
- **`bin/preflight.sh`** + **`bin/safe-claude`** aus Phase 1 (für die
  Watchdog-Subprocess-Boundary).
- **`/tmp`** schreibbar für die Bot-PID + Watchdog-PID. Wir
  setzen *nicht* auf einen privilegierten Pfad — Single-User-Mac,
  und die Marker sind Touch-Files, kein Secret-Material.
- macOS `pmset`-CLI verfügbar (Standard auf macOS 14+).

## Was gebaut wird

### 1. Domain — Lockdown + Heartbeat (pure)

- **`whatsbot/domain/lockdown.py`** — pure:
  - `LockdownState`-Dataclass: `engaged: bool`, `engaged_at:
    datetime | None`, `reason: str | None`, `engaged_by: str |
    None` (`/panic`, `watchdog`, `manual`).
  - `engage(state, *, now, reason, engaged_by)` — Idempotent: ein
    bereits-engaged Lockdown wird nicht erneut „initialed", die
    Original-Timestamps bleiben (Forensik).
  - `disengage(state)` — gibt frischen NOT-ENGAGED-State zurück.
  - Konstanten: `KEY_LOCKDOWN` (für AppStateRepository),
    `LOCKDOWN_REASON_PANIC = "panic"`, `LOCKDOWN_REASON_WATCHDOG
    = "watchdog"`, `LOCKDOWN_REASON_MANUAL = "manual"`.

- **`whatsbot/domain/heartbeat.py`** — pure:
  - `HEARTBEAT_INTERVAL_SECONDS = 30` (wie oft der Bot schreibt).
  - `HEARTBEAT_STALE_AFTER_SECONDS = 120` (Spec §7 — Watchdog-
    Threshold).
  - `is_heartbeat_stale(mtime, now, *, threshold=...)` — Pure
    Compare. Returnt `True` wenn `now - mtime >= threshold`.
  - `format_heartbeat_payload(now)` — String, der ins File
    geschrieben wird (ISO-Timestamp, plus Bot-PID via Caller).
    Touch wäre eigentlich genug, aber wir schreiben Inhalt
    mit, damit `cat` das Debugging vereinfacht.

### 2. Ports

- **`whatsbot/ports/heartbeat_writer.py`** — Protocol:
  - `write(payload: str) -> None`
  - `last_mtime() -> float | None` — für Self-Check beim Bot-
    Start (Crash-Indikator: wenn der Heartbeat noch frisch ist
    obwohl wir gerade erst starten, läuft eventuell ein zweiter
    Bot — Warn-Log + abort).

- **`whatsbot/ports/sleep_monitor.py`** — Protocol:
  - `start(on_sleep: Callable[[], None], on_wake: Callable[[], None])
    -> None`
  - `stop() -> None`
  - In Phase 6 hat **nur der Watchdog** echten Bedarf — der
    Bot-Prozess selbst ist von macOS sowieso eingefroren während
    Sleep. Wir machen den Port trotzdem im whatsbot-Paket, damit
    er testbar bleibt, und der Watchdog-Shell-Code spiegelt die
    Logik.

- **`whatsbot/ports/notification_sender.py`** — Protocol für die
  macOS-Notification (Bot-Down, Tunnel-Down, Watchdog-Action).
  - `send(title: str, body: str, *, sound: bool = False) -> None`

### 3. Adapter

- **`whatsbot/adapters/file_heartbeat_writer.py`** — schreibt
  `/tmp/whatsbot-heartbeat` atomar (`tmp + os.replace`). `last_mtime`
  via `os.stat`.

- **`whatsbot/adapters/pmset_sleep_monitor.py`** — startet einen
  `pmset -g log | tail -F`-Subprocess, parsed Sleep/Wake-Events
  zeilenweise, ruft die Callbacks. Stoppen sauber via `terminate +
  wait` mit Timeout. **Achtung**: pmset benötigt
  Volldisk-Berechtigung für Background-Processes seit
  macOS 14.4 — Install-Doc-Hinweis (in §22 ergänzen).

- **`whatsbot/adapters/osascript_notifier.py`** —
  `osascript -e 'display notification ...'` für native macOS-
  Notifications. Ohne externe deps. Optional sound via
  `with sound name "Submarine"`.

### 4. Application

- **`whatsbot/application/kill_service.py`** — die drei
  Eskalationsstufen `/stop`, `/kill`, `/panic`:
  - `stop(project_name | None)` — wenn name: tmux send-keys C-c in
    den Pane. Wenn None: aktives Projekt aus AppState.
  - `kill(project_name | None)` — tmux kill-session. claude_sessions-
    Row bleibt (Resume-fähig). Lock wird released.
  - `panic()` — Reihenfolge:
    1. **Sofort** Lockdown setzen + Touch-File schreiben (so dass
       konkurrierende Webhooks die Boot-Recovery nicht restarten
       können).
    2. Alle `wb-*`-Sessions enumerieren und kill_session pro
       Session.
    3. `pkill -9 -f claude` als zweiter Backstop.
    4. Alle YOLO-Projekte auf Normal zurücksetzen
       (`mode_events.event='panic_reset'`, analog zur
       Reboot-Recovery aus Phase 4).
    5. Alle Locks releasen (LockService.release pro Projekt aus
       der Liste).
    6. macOS-Notification: `🚨 PANIC engaged`.
  - Latenz-Budget: P95 < 2 s (Spec §21 Phase 6 C6.2).

- **`whatsbot/application/lockdown_service.py`** — `engage`,
  `disengage(pin)`, `current()`. Persistiert in AppState +
  Touch-File. PIN-Verify wieder über das geteilte Muster aus
  `delete_service` / `force_service` (gleicher `panic-pin`-Key).

- **`whatsbot/application/heartbeat_pumper.py`** — eigener
  asyncio-Loop, der alle 30 s den Heartbeat schreibt. Wird vom
  `create_app`-Lifecycle gestartet/gestoppt.
  - Bei Schreibfehler: WARN-Log, weitermachen — der Watchdog wird
    eh greifen, kein Sinn am Pumper hängen zu bleiben.
  - Bei Bot-Shutdown (FastAPI lifespan): Heartbeat-File löschen,
    damit ein Neustart nicht ein „frisches" altes Heartbeat sieht.

- **`whatsbot/application/startup_recovery.py`** — schon da aus
  Phase 4. Erweitert um:
  - **Lockdown-Check**: wenn Lockdown engaged, *keine*
    Session-Recovery. Stattdessen Audit-Log + WhatsApp-Notice
    an den Default-Recipient.
  - **Heartbeat-Selbstcheck**: wenn `/tmp/whatsbot-heartbeat` jünger
    als 60 s ist, vermutlich läuft schon ein anderer Bot — abort
    mit klarer Fehlermeldung statt Doppel-Bot.

### 5. Watchdog (separater LaunchAgent)

- **`bin/watchdog.sh`** — Shell-Script (kein Python — minimaler
  Footprint, funktioniert auch wenn das venv kaputt ist):
  - Liest Heartbeat-File alle 30 s.
  - Bei Stale (>120 s) **und nicht-Sleep**: tötet alle
    `wb-*`-Sessions (`tmux list-sessions -F '#{session_name}' |
    grep '^wb-' | xargs -n1 tmux kill-session -t`), dann
    `pkill -9 -f claude`. Schreibt Lockdown-Touch-File. Sendet
    macOS-Notification.
  - Sleep-Awareness via `pmset -g log` parsing oder einfacher: das
    Skript prüft `pmset -g state | grep -q "Currently drawing
    from .* Battery Power"` als Proxy — wenn Sleep-Detection nicht
    möglich, fällt es auf einen *langen* Heartbeat-Threshold (10
    min statt 2 min) zurück.
  - Strukturiertes JSON-Logging in
    `~/Library/Logs/whatsbot/watchdog.jsonl`.

- **`launchd/com.DOMAIN.whatsbot.watchdog.plist.template`**:
  - `KeepAlive` (mit `SuccessfulExit=false`).
  - `RunAtLoad=true`.
  - `StartInterval=30` — alle 30 s einmal `bin/watchdog.sh`
    aufrufen, statt einer eigenen Loop im Skript. Robuster gegen
    Skript-Crashes.
  - `EnvironmentVariables`: `PATH`, `HOME`,
    `WHATSBOT_HEARTBEAT_PATH`, `WHATSBOT_PANIC_MARKER`.

### 6. Commands + HTTP-Routing

- **`/stop`** und **`/stop <name>`** — neue Routen, an
  `KillService.stop`.
- **`/kill`** und **`/kill <name>`** — an `KillService.kill`.
- **`/panic`** — keine Args, **kein PIN** (Spec §5: bewusst
  niedrige Hürde — wer in Panik ist soll nicht erst einen PIN
  tippen müssen).
- **`/unlock <PIN>`** — PIN-gated `disengage`.

- **`CommandHandler`**: 4 neue Routen, alle mit klaren WhatsApp-
  Acks:
  - `/stop alpha` → `🛑 Ctrl+C an 'alpha' geschickt.`
  - `/kill alpha` → `🪓 'alpha' tmux-Session beendet.`
  - `/panic` → `🚨 PANIC! 4 Sessions getötet, YOLO→Normal,
    Lockdown engaged. /unlock <PIN> wenn fertig.`
  - `/unlock 1234` → `🔓 Lockdown aufgehoben.` /
    `⚠️ Falsche PIN.`

- **Lockdown-Filter**: Wenn Lockdown engaged ist, dropt
  `CommandHandler.handle` alle Commands außer `/unlock`,
  `/status`, `/help`, `/ping` mit der Antwort
  `🔒 Bot ist im Lockdown. /unlock <PIN> zum Aufheben.` Das ist
  defense-in-depth — selbst wenn jemand das Handy klaut während
  Lockdown an ist, kommt er nicht an `/p` ran ohne den PIN.

### 7. Wiring

- **`whatsbot/main.py`**:
  - `KillService` und `LockdownService` werden gewired (nur wenn
    `tmux` vorhanden).
  - Heartbeat-Pumper als FastAPI-Lifespan-Task starten/stoppen.
  - StartupRecovery liest `KEY_LOCKDOWN` und überspringt Recovery
    wenn engaged.
  - CommandHandler bekommt KillService + LockdownService
    + Lockdown-Filter.
  - Notifier wird wired (wenn macOS).

- **`bin/render-launchd.sh`** und das Makefile-Target
  `deploy-launchd` werden um den Watchdog-Plist erweitert
  (analog zum bestehenden DB-Backup-Plist).

## Checkpoints

### C6.1 — `/stop` und `/kill` (atomar) ✅

- KillService.stop sendet Ctrl+C, stop ohne Args nutzt das aktive
  Projekt.
- KillService.kill sched tmux kill-session, lock release.
- 2 unit + 1 e2e (`/webhook` → `/stop` → tmux send_text("C-c") +
  `/webhook` → `/kill` → tmux kill_session aufgerufen).

### C6.2 — `/panic` ≤2 s, vollständige Eskalation

- KillService.panic in Reihenfolge: lockdown + touchfile → enum
  sessions → kill_session pro session → pkill -9 → YOLO→Normal
  pro Projekt → Locks release pro Projekt → notification.
- Tests mit FakeTmux (3 wb-* Sessions, 2 davon YOLO, alle mit
  Locks): ein einziger panic()-Call → alle 4 Schritte landen,
  mode_events bekommt 'panic_reset'-Rows, Lockdown-State engaged.
- Latenz-Budget asserted: < 2 s mit FakeTmux.

### C6.3 — YOLO-Reset bei Panic (Pure-Layer)

- `mode_events`-Row mit `event='panic_reset'`,
  `from_mode='yolo'`, `to_mode='normal'` pro getroffenes Projekt.
- `projects.mode` ist 'normal' für alle vormals-YOLO-Projekte.
- Audit-Log enthält `panic_engaged` mit project-count + reason.

### C6.4 — Heartbeat-Pumper + Bot-Down → Watchdog killt

- Heartbeat-Pumper schreibt alle 30 s nach
  `/tmp/whatsbot-heartbeat`.
- watchdog.sh checkt Stale-Threshold, killt wb-* + claude bei
  >120 s. Test: synthetisches stale heartbeat-File + manueller
  watchdog.sh-Aufruf in einem Tempo-Pfad → tmux-Sessions weg.
- Integration-Test mit echtem tmux (skipped wenn tmux missing,
  wie in Phase 5 e2e).

### C6.5 — Sleep/Wake-Handling

- pmset_sleep_monitor.py liest Sleep/Wake-Events und triggert
  Callbacks. **Test**: wir mocken den Subprocess-stdout mit
  Sleep/Wake-Lines → Callback-Counter assertet.
- watchdog.sh nimmt Sleep-Status in seine Decision auf
  (Pause oder Long-Threshold-Fallback).
- Live-Smoke (manuell): Mac in Sleep schicken, 5 min warten,
  aufwachen → Watchdog hat NICHT gekillt, Bot reconnected sauber.

### C6.6 — `/unlock` + Lockdown-Filter

- Lockdown-Filter im CommandHandler dropt alle Commands außer
  Allow-Liste während Lockdown.
- `/unlock <PIN>` mit korrekter PIN: Lockdown-State weg,
  Touch-File weg, Audit-Log `lockdown_disengaged`.
- `/unlock <falscher PIN>` → `⚠️ Falsche PIN.`, Lockdown bleibt.
- StartupRecovery beim Bot-Restart in Lockdown: keine Sessions
  starten, WhatsApp-Notice.

### C6.7 — `/unlock` ohne PIN-Setup + macOS-Notification optional

- Wenn `panic-pin` nicht im Keychain (Setup-Bug):
  `/unlock` schlägt klar verständlich fehl statt silent zu
  akzeptieren.
- Notifier ist optional — fehlende `osascript`-Verfügbarkeit
  (Linux-CI) fällt auf Log-Only zurück.

## Success Criteria

- [ ] `/stop`, `/kill`, `/panic`, `/unlock` durch /webhook
      end-to-end testbar.
- [ ] `/panic` in <2 s alle wb-* Sessions tot + YOLO→Normal
      + Lockdown engaged + Locks geclear t.
- [ ] StartupRecovery respektiert Lockdown (Spec §6 Invariante).
- [ ] Heartbeat-File wird im Live-Bot alle ~30 s aktualisiert.
- [ ] Watchdog-LaunchAgent kill bei stale Heartbeat (Spec §7
      „Dead-Man's-Switch").
- [ ] Sleep-Awareness verhindert False-Positive-Kills nach
      Laptop-Sleep.
- [ ] Lockdown-Filter blockt alle Non-Allowlist-Commands während
      Lockdown.
- [ ] Alle Domain/Service-Tests grün, mypy --strict clean,
      ruff clean.
- [ ] Spec §23 Recovery-Playbook „Bot-Crash" ist mit der neuen
      Heartbeat+Watchdog-Kette dokumentierbar.

## Abbruch-Kriterien

- **`pmset -g log`-Subprocess hängt oder produziert kein
  parseable output** auf macOS 14.4+: Stop. Fallback auf
  Heartbeat-Grace-Period (Watchdog-Threshold von 120 s auf
  600 s erhöhen, Sleep-Detection optional). RUNBOOK-Eintrag.
- **`pkill -9 -f claude` killt unverdächtige Claude-Instanzen**
  (User hat parallel `claude` in einem anderen tmux-Pane laufen,
  außerhalb von wb-*). Stop. Match auf cwd statt auf Process-Name
  (`pgrep -f -d',' "claude --resume"`-Filter), oder zuerst
  saubere tmux kill-Sessions, dann pkill nur als Fallback wenn
  immer noch Reste laufen.
- **Lockdown-Filter blockt versehentlich `/unlock`**: Stop. Test
  schreiben, der genau das nachweist und behebt.
- **Watchdog-LaunchAgent bekommt keine Permissions** (TCC für
  pmset / pkill): Stop. Install-Doc um die Permission-Setup-
  Schritte erweitern.

## Was in Phase 6 NICHT gebaut wird

- **Cloudflare-Tunnel-Health-Check + Notification** (Spec §21
  C6.6): bewusst zurückgestellt nach Phase 8 (Observability).
  Der Phase-6-Notifier ist die *Infrastruktur* dafür, der
  Tunnel-Probe selbst kommt mit `/metrics`.
- **Auto-Sweep der expired Locks** durch den Watchdog: einfach
  ist es, den Pumper aus C6.4 zusätzlich `lock_service.sweep_expired`
  aufrufen zu lassen — aber das mischt zwei Themen. Wir machen
  es als kleinen Folge-Commit, nicht als Phase-6-Pflicht.
- **`/status`-Erweiterung um Lockdown-State**: gehört in Phase 8
  (Observability). Wir loggen, aber wir bauen kein
  `/status`-Dashboard.

## Architektur-Hinweise

- KillService darf nicht von HookService abhängen — der
  Pre-Tool-Hook würde sich sonst in einer Panic selbst
  triggern (Tool-Call → Hook → DB-Read → Lockdown engaged →
  Hook deny). Klare Schicht-Trennung: KillService greift auf
  TmuxController + ProjectRepository + ClaudeSessionRepository
  + LockService + ModeEventRepository + LockdownService zu,
  aber NICHT auf HookService.

- Lockdown-Touch-File `/tmp/whatsbot-PANIC` ist **redundant** zur
  AppState-Row, aber bewusst: `/tmp` überlebt Bot-Crashes nicht
  (Reboot räumt es auf), AppState überlebt — und der Watchdog
  liest *nur* das Touch-File, weil er kein DB-Handle hat. Der
  Bot ist die Single-Source-of-Truth, der Watchdog ist nur
  Backstop.

- Heartbeat-Pumper läuft im selben asyncio-Loop wie der
  Meta-Webhook-Listener. **Wichtig**: blockierende File-IO darf
  nicht den Loop blockieren — `await asyncio.to_thread(...)` für
  den `os.replace`. Bei einem 30-s-Intervall ist das im Zweifel
  egal, aber wir wollen die Disziplin halten.

- Test-Strategie: `KillService` mit FakeTmux + In-Memory-DB,
  Watchdog-Skript mit Bash-spezifischen Tests
  (`tests/integration/test_watchdog_script.py` analog zu
  `test_backup_db.py` aus Phase 1). pmset-Monitor gegen Mock-
  stdout, kein echter pmset-Aufruf in CI.

## Nach Phase 6

Update `.claude/rules/current-phase.md` auf Phase 7. Phase 7
(Medien-Pipeline) ist parallel zu Phase 8 machbar. Warte auf
User-Freigabe.
