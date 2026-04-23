# Phase 5: Input-Lock + Multi-Session

**Aufwand**: 1-2 Sessions
**Abhängigkeiten**: Phase 4 komplett ✅
**Parallelisierbar mit**: Phase 6
**Spec-Referenzen**: §7 (Input-Lock + Zero-Width-Space-Prefix), §11
(`/force`, `/release`), §19 (`session_locks`-Tabelle)

## Ziel der Phase

Wenn der User lokal am Terminal tippt **und** per Handy prompten
will, darf nur eine Seite gleichzeitig in Claude schreiben. Lokales
Terminal hat Vorrang (Soft-Preemption). Die Domain kennt drei
Owner-Zustände — `free`, `bot`, `local` — persistiert in
`session_locks`.

Phase 5 endet damit, dass:

1. Ein `/p <name> <prompt>` abgelehnt wird, solange lokal getippt
   wurde (Lock auf `local`). Reply: `🔒 Terminal aktiv. /force
   <name> <prompt>`.
2. Lokaler Input im tmux-Pane (Transcript zeigt einen
   *non-ZWSP* User-Turn) zieht den Lock auf `local`.
3. `/force <name> <prompt>` (PIN-gated) überschreibt den Lock und
   sendet den Prompt trotzdem.
4. `/release [name]` stellt den Lock zurück auf `free`.
5. Nach 60s Inaktivität wird ein `local`-Lock automatisch auf
   `free` zurückgesetzt.
6. Die tmux-Statuszeile zeigt den Owner (BOT / LOCAL / FREE).

## Was gebaut wird

### Domain (pure)

- **`whatsbot/domain/locks.py`**
  - `LockOwner`-StrEnum: `BOT` / `LOCAL` / `FREE`.
  - `SessionLock`-Dataclass: project_name, owner, acquired_at,
    last_activity_at.
  - `AcquireOutcome`-Enum: `GRANTED` / `DENIED_LOCAL_HELD` /
    `AUTO_RELEASED_THEN_GRANTED`.
  - `evaluate_bot_attempt(current, now, timeout_s) -> (outcome,
    new_state)` — pure. Wenn current None / FREE / BOT → grant.
    Wenn LOCAL + mehr als 60s idle → auto-release + grant. Sonst
    DENIED_LOCAL_HELD.
  - `mark_local_input(current, now) -> SessionLock` — owner
    LOCAL, activity-timestamp jetzt.
  - `LOCK_TIMEOUT_SECONDS = 60`.

### Port + Adapter

- **`whatsbot/ports/session_lock_repository.py`** Protocol:
  `get(project)` / `upsert(lock)` / `delete(project)` /
  `list_all()`.
- **`whatsbot/adapters/sqlite_session_lock_repository.py`** — gegen
  die bereits vorhandene `session_locks`-Tabelle.

### Application

- **`whatsbot/application/lock_service.py`** — Use-Cases:
  - `acquire_for_bot(project)` → gibt `AcquireOutcome` zurück. Ruft
    `evaluate_bot_attempt`, persistiert bei Grant.
  - `note_local_input(project)` → setzt Lock auf LOCAL + aktualisiert
    activity_at. Wird aus dem Transcript-Ingest aufgerufen, wenn ein
    User-Event *ohne* ZWSP-Prefix und mit nicht-leerem Text
    eintrifft.
  - `release(project)` → löscht/setzt auf FREE.
  - `force_bot(project)` → setzt unconditional auf BOT.
  - `sweep_expired(now=None)` → iteriert alle Locks, auto-released
    abgelaufene. Optional als Hintergrund-Task; minimal: lazy in
    acquire_for_bot.

### Wiring

- **`TranscriptIngest._handle_user`** (bereits da): nach der
  existierenden "bot-prefixed skip" / "empty skip"-Logik ruft er
  `lock_service.note_local_input(project)` auf. Lock-Service ist
  optionaler Constructor-Param wie `on_auto_compact`.
- **`SessionService.send_prompt`**: vor dem ZWSP-Send ruft es
  `lock_service.acquire_for_bot(project)`. Bei DENIED_LOCAL_HELD
  raise eine `LocalTerminalHoldsLockError` — die wird im
  CommandHandler abgefangen und als `🔒 Terminal aktiv...`-Reply
  gerendert.
- **`SessionService._paint_status_bar`**: die Statuszeile bekommt
  einen Owner-Teil ("BOT" / "LOCAL" / "FREE"), den wir beim
  Acquire / Release / note_local_input refreshen.
- **Commands**:
  - `/force <name> <prompt>` (PIN-gated, Spec §5): entschlüsselt
    `<prompt>`, ruft `lock_service.force_bot(name)` dann
    `send_prompt`. Wie `/rm` via Keychain-PIN.
  - `/release [name]`: setzt Lock auf FREE für das angegebene
    (oder aktive) Projekt.

## Checkpoints

### C5.1a — `domain/locks.py` pure + Tests

- Dataclass + Enums + `evaluate_bot_attempt` + `mark_local_input`.
- Unit-Tests: alle Zustands-Übergänge (free/bot/local × bot/local,
  vor + nach Timeout).

### C5.1b — Port + SQLite-Adapter + Tests

- Schema-Invarianten (CHECK-Constraint) durch die existierende
  `session_locks`-Tabelle abgedeckt.
- Round-Trip-Test für upsert / get / delete / list_all.

### C5.1c — LockService + Tests

- `acquire_for_bot` / `note_local_input` / `release` /
  `force_bot` / `sweep_expired`.
- Unit-Tests gegen in-memory DB.

### C5.2 — Wiring: TranscriptIngest + SessionService + Commands

- `TranscriptIngest` akzeptiert optional `on_local_input` +
  `on_user_note` Callbacks; main.py verdrahtet auf LockService.
- `SessionService.send_prompt` raise't bei denied acquire.
- `/force`, `/release` im CommandHandler.
- Statusleiste bekommt Owner-Teil.

### C5.3 — End-to-End Integration-Test

- Preseed `session_locks` mit Owner LOCAL + activity vor 10s.
- `/p alpha hi` via /webhook → reply enthält `🔒 Terminal aktiv`.
- `/force alpha <PIN> hi` → Prompt geht durch, Lock wechselt auf
  BOT, ack mit `📨`.

## Success Criteria

- [ ] Domain-Tests decken alle 9 Übergänge (3 owners × 3 events)
      plus Timeout-Fälle ab.
- [ ] `acquire_for_bot` auto-released abgelaufene LOCAL-Locks.
- [ ] `send_prompt` raise't `LocalTerminalHoldsLockError` bei
      Denial; CommandHandler rendert `🔒 Terminal aktiv` Hint.
- [ ] `/force <name> <prompt>` funktioniert PIN-gated (reuse
      der `/rm`-PIN-Logik).
- [ ] `/release` setzt den Lock zurück auf FREE.
- [ ] Statusleiste zeigt Lock-Owner (grüner / gelber / roter Badge).
- [ ] `TranscriptIngest` ruft `lock_service.note_local_input` bei
      non-ZWSP-User-Events — nicht bei Tool-Results (empty text),
      nicht bei Bot-prefixed Turns.
- [ ] Alle `make test`-Level grün, `mypy --strict` clean, ruff
      clean auf angefassten Files.

## Abbruch-Kriterien

- **Transcript-Watcher kann Bot- und lokal-getippte User-Turns
  nicht unterscheiden** — ZWSP-Prefix muss zuverlässig landen +
  geparst werden. Wenn hier eine Regression auftaucht (z.B. weil
  eine Claude-Update den Prefix droppt), Phase 5 stoppen und
  Prefix-Technik überdenken.
- **Lock-Auto-Release-Timing ist zu eng** (z.B. 60s sind für
  natürliche Terminal-Pausen zu kurz) — Timer erhöhen (Spec §7
  dokumentiert 60s als Default, nicht als harte Vorgabe).

## Was in Phase 5 NICHT gebaut wird

- **Kill-Switch + Watchdog + Sleep-Handling** — Phase 6.
- **Medien-Pipeline** — Phase 7.
- **Max-Limit-Handling** — Phase 8.
- `/panic` setzt zwar Locks zurück — aber die Implementierung landet
  in Phase 6 mit dem Rest des Panic-Flows.

## Nach Phase 5

Update `.claude/rules/current-phase.md` auf Phase 6. Phase 6 ist
parallel zu Phase 5 machbar, aber nach Fertigstellung von Phase 5
üblich sequenziell. Warte auf User-Freigabe.
