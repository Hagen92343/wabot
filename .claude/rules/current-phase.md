# Aktueller Stand

**Projekt-Status**: **Phase 1-11 + Mini-Phase 12 Code + Tests komplett
✅.** Bot produktiv seit 2026-04-24 15:25 UTC. Phase 11 fügte
`/import` hinzu (Migration 001). Mini-Phase 12 fixt den
`claude_sessions.session_id UNIQUE`-Bug (Migration 002, partial
unique index). Bot läuft mit Mini-Phase-12-Code (PID 98777).

**C11.6 Live-Verifikation vom Handy — ausstehend.** Siehe Abschnitt
"Wie für nächste Session weitermachen" unten für die 4 konkreten
Handy-Tests. Mini-Phase 12 hat den UNIQUE-Blocker für Test 3
(`/p wabot ...`) beseitigt — sollte jetzt durchlaufen.

SIM-Port-Lock beim Carrier bleibt User-Action außerhalb Code.

## Mini-Phase 12 — `claude_sessions.session_id` partial unique

**Trigger**: Beim ersten Phase-11-Bot-Restart kollidierte ein
`claude_sessions`-INSERT mit `UNIQUE constraint failed:
claude_sessions.session_id`. Domain benutzt `''` als Platzhalter,
zwei `''`-Rows verbietet aber die UNIQUE-Spalte. Phase-4-Erbe.

**Geliefert (2026-04-25)**:
- Migration 002: rename-copy-drop-rename, `NULLIF(session_id, '')`,
  `CREATE UNIQUE INDEX … ON claude_sessions(session_id) WHERE
  session_id IS NOT NULL`. PRAGMA user_version=2.
- `sql/schema.sql` auf gleichen Endzustand (Fresh-Install bypasst
  Migration).
- `sqlite_claude_session_repository.upsert`: `session.session_id or
  None` — leere Strings landen als NULL auf Disk; Read-Pfad
  konvertiert NULL→`""` zurück (Z. 145, unverändert).
- 9 neue Tests (4 migration, 5 repo). Bestehende Migration-Tests
  auf `[1, 2]` + `user_version == 2` aktualisiert.

**Tests-Stand**: 1640 passed + 1 live-skipped + 3 pre-existing
integration-failures (siehe Hygienepunkt unten). mypy --strict clean
auf 120 source files, ruff clean.

**Live-Verifikation**: user_version=2, scratch session_id=NULL,
wabot-ID intakt, partial index präsent, integrity_check ok. Bot
restart sauber, kein neuer IntegrityError im stderr.

## Bekannte Hygienepunkte (post Mini-Phase 12)

1. **Settings.db_path-Default leakt in Tests**: einige
   `tests/integration/`-Tests bauen `Settings(env=Environment.PROD)`
   ohne `db_path`-Override. `Settings.db_path` defaultet auf den
   Live-Pfad → `open_state_db` läuft tatsächlich auf der Live-DB.
   C11.5 (`extra=forbid`) hat den Typo-Fall gefixt, aber nicht den
   fehlenden-Field-Fall. Heute hat das die Mini-Phase-12-Migration
   ungewollt vor dem Bot-Restart ausgelöst (funktional kein Schaden,
   DB war im Zielzustand). Fix-Kandidat: conftest-Fixture die
   `db_path` auf tmp_path zwingt, oder Pflicht-Override für
   `env != TEST` in `Settings`.
2. **3 pre-existing integration-failures**:
   `test_command_still_dispatches_after_injection_detection`,
   `test_unknown_command_still_replies_with_hint`,
   `test_image_without_active_project_prompts_user_to_set_one`.
   Symptom: `recorder.sent` hat 20+ Messages statt 1 — sieht nach
   Cross-Test-Pollution aus (geteilter MessageSender-Singleton oder
   Module-State-Leak). Reproduzieren auf bare `main` ohne meine
   Mini-Phase-12-Changes — also nicht von mir verursacht. Separate
   Cleanup-Phase.

## Deployment-Stand (Stand 2026-04-23 abend)

Abgeschlossen:
- ✅ **Schritt 1** — Brew-Pakete installiert (python@3.12, tmux,
  ffmpeg, cloudflared, whisper-cpp).
- ✅ **Schritt 2** — Whisper-Modell `ggml-small.bin` unter
  `~/Library/whisper-cpp/models/`.
- ✅ **Schritt 3** — Claude Code installiert, `claude /login` mit
  **Subscription**, nicht API.
- ✅ **Schritt 4** — Repo unter `~/whatsbot`, `make install` durch.
- ✅ **Schritt 5** — Meta-App angelegt, WhatsApp-Produkt, System-
  User-Access-Token generiert, 4 Werte notiert (App Secret,
  Verify Token, Access Token, Phone Number ID).
- ✅ **Schritt 6** — `make setup-secrets` durch, alle 7 Keychain-
  Einträge gesetzt (inkl. panic-pin + hook-shared-secret).
- ✅ **Schritt 7** — Cloudflare Named Tunnel `whatsbot` läuft
  systemweit als root (pid 71253, `/etc/cloudflared/config.yml`),
  via `sudo cloudflared service install`. Ingress:
  `bot.lhconsulting.services` → `http://127.0.0.1:8000`. User-
  Config unter `~/.cloudflared/config.yml` (gleiche Route).
  `curl https://bot.lhconsulting.services/health` → 200.
- ✅ **Schritt 8** — Drei LaunchAgents aktiv: `com.local.whatsbot`,
  `com.local.whatsbot.watchdog`, `com.local.whatsbot.backup`. Bot
  bindet sauber auf `127.0.0.1:8000`.
- ✅ **Schritt 9** — Meta-Webhook in Developer Console
  eingetragen. `subscribe_challenge_ok` 2026-04-23T20:23:57Z —
  Verify-Handshake grün. Subscription auf `messages` aktiv,
  eigene Handy-Nummer als Test-Recipient eingetragen.
- ✅ **Schritt 11 (Inbound-Seite)** — erster echter `/ping` vom
  Handy kam an, wurde zum `command_routed`-Event, Reply
  `pong · v0.1.0 · uptime 105s` wurde generiert (siehe
  `app.jsonl` um 2026-04-23T21:05:44Z).

- ✅ **Schritt 11 (Outbound-Seite · C10.5)** — Live-Re-Deploy
  2026-04-24 15:25 UTC mit Permanent-System-User-Token. Bot kickstarted,
  vom Handy `/ping` → `outbound_message_sent message_id=wamid.HBgMNDkx...HFAA==`,
  Reply `pong` kam zurück. Circuit-Breaker griff vorher wie designed
  (5 × 401 → OPEN), wurde durch Bot-Restart zurückgesetzt.

Offen (User-Action, außerhalb Code-Scope):
- ⏭ **Schritt 10** — SIM-Port-Lock beim Carrier aktivieren
  (Spec §24, gegen SIM-Swap). Spec-Pflicht vor Produktiv-Betrieb.

## Phase 10 — WhatsAppCloudSender (C10.1-C10.4 done)

**Trigger**: Impl-Debt aus dem Live-Deployment:
`WhatsAppCloudSender.send_text` war seit Phase 1 ein
`NotImplementedError`-Skelett. Alle Phase-2-bis-9-Tests liefen
gegen den `LoggingMessageSender`, daher nie aufgefallen. Konsequenz
vorher: Bot empfängt Webhook → `outbound_message_dev` im Log → aus
Handy-Sicht schweigt der Bot.

**Geliefert (2026-04-24)**:
- **C10.1** — `WhatsAppCloudSender.send_text` echt: httpx-POST
  gegen `POST https://graph.facebook.com/v23.0/{phone_number_id}/
  messages`, Bearer-Auth, Body-Shape pro Meta-Spec, Timeouts
  5/30s, tenacity-Retry (3x, 5xx + Netzwerk), 4xx short-circuit
  mit `MessageSendError`. Phone-Normalisierung (strip leading `+`/
  whitespace). Logging von `to_tail4` + `body_len` + `message_id`.
  `@resilient(META_SEND_SERVICE)` bleibt, 3 Retries = 1 Breaker-
  Failure (Invariante wie MetaMediaDownloader).
- **C10.2** — Circuit-Breaker-Integration verifiziert: 5
  sequentielle retry-ausschöpfende Failures trippen `meta_send`,
  6ter Call short-circuited ohne HTTP; Cooldown → HALF_OPEN-
  Probe → CLOSED bei Erfolg.
- **C10.3** — `main.py::_build_outbound_sender`: fact-based
  Selection. override > TEST/DEV → Logging; PROD + beide Secrets
  → Cloud; PROD + fehlende Secrets → Logging mit WARN. Kein
  neuer Env-Flag. ~30 Integration-Tests laufen weiter
  unverändert via `override=message_sender`-Param.
- **C10.4** — `tests/integration/test_whatsapp_sender_live.py`
  liegt vor, skipped default, opt-in via `WHATSBOT_LIVE_META=1`.

**Neue Files**:
- `whatsbot/adapters/whatsapp_sender.py` — echter Adapter.
- `whatsbot/ports/message_sender.py` — `MessageSendError` hinzu,
  Protocol-Docstring korrigiert.
- `tests/unit/test_whatsapp_sender.py` — 16 Tests via
  `httpx.MockTransport`.
- `tests/unit/test_main_sender_selection.py` — 11 Tests für
  `_build_outbound_sender`.
- `tests/integration/test_whatsapp_sender_circuit.py` — 2 Tests
  analog zu `test_resilience_circuit_integration.py` für
  `meta_send`-Breaker.
- `tests/integration/test_whatsapp_sender_live.py` — 1 Test
  (skipped default).
- `.claude/rules/phase-10.md` — Phase-Rules.

**Tests-Stand**: 1572 passed + 1 live-skipped (Baseline Phase-9
1542 + 30 neue Phase-10-Tests − 1 live-skip). mypy --strict clean
auf allen 7 Phase-10-Files. ruff clean.

## C10.5 — Live verified (2026-04-24 15:25 UTC) ✅

**Root-Cause des vorherigen 401-Blockers**: Der Token, der am
2026-04-23 abend gesetzt wurde, war ein Temporary 24h-Token aus
der Meta-Dashboard-"API Setup"-Seite, kein Permanent System-
User-Token. Meta antwortete nach genau 24 h mit
`OAuthException code=190, error_subcode=463, "Session has expired
on Thursday, 23-Apr-26 13:00:00 PDT"`.

**Fix-Pfad heute**:
1. Zwei Debug-Curls gegen `/v23.0/me` + `/v23.0/1130442710144663`
   → beide mit `code=190, subcode=463, Session has expired` → Token
   eindeutig abgelaufen.
2. Neuer Permanent-Token via Meta Business Suite → **Nutzer → Systemnutzer**
   → `whatsbot` (ID 61564726272665, Admin-Zugriff) → **Assets
   zuweisen** (WhatsApp-App "wibot", Vollzugriff) → **Token
   generieren** mit Expiration=**Nie**, Permissions
   `whatsapp_business_messaging` + `whatsapp_business_management`.
   Token-Länge 206 Zeichen.
3. Keychain-Update: `security add-generic-password -U -s whatsbot
   -a meta-access-token -w '<token>'`. zsh-History nachträglich
   via `sed -i '' '/<token-prefix>/d' ~/.zsh_history` gescrubbt.
4. Verifikation mit `GET /v23.0/1130442710144663` → liefert
   `verified_name: "Test Number"`, `display_phone_number: +1
   555-633-0519`, `webhook_configuration.application:
   https://bot.lhconsulting.services/webhook`,
   `platform_type: CLOUD_API` — Token valid + Phone-Number-Asset
   zugänglich + Webhook korrekt registriert.
5. Bot-Restart via `launchctl kickstart -k
   gui/$UID/com.local.whatsbot` (resettet auch den OPEN
   `meta_send`-Circuit-Breaker, weil die Registry module-level
   liegt und Restart sie verwirft — gewollte Invariante).
6. Handy-Test: vom Handy `/ping` geschickt → `app.jsonl` zeigt
   `command_routed` → `HTTP POST 200 OK` →
   `outbound_message_sent to_tail4=8519 body_len=26
   message_id=wamid.HBgMNDkx...HFAA==` → Reply `pong` kam auf
   dem Handy an. End-to-End verifiziert.

**Bonus-Beweis Phase-8**: der Circuit-Breaker trippte während
der 401-Serie (5 × 401 in <14 s, `circuit_opened service=meta_send
threshold=5 window_s=60`) und short-circuitete weitere Calls
ohne HTTP — Phase-8-C8.3-Design hat in freier Wildbahn
funktioniert.

## Wie für nächste Session weitermachen

1. Diese Datei lesen — Stand ist "Phase 1-11 Code fertig,
   C11.6 Handy-Test ausstehend".
2. `git log --oneline -10` für den Commit-Stand (letzte Commits
   sind `ed77c1e chore(config): Settings extra=forbid` und davor
   die fünf `feat(phase-11): C11.x`-Commits).
3. Test-Baseline: `venv/bin/pytest tests/unit/ tests/integration/
   tests/smoke.py --ignore=tests/unit/test_hook_common.py
   --ignore=tests/integration/test_hook_script.py
   --ignore=tests/integration/test_hook_fail_closed.py` sollte
   **1631 passed + 1 live-skipped** zeigen. mypy --strict clean
   auf 120 source files, ruff clean.
4. Bot-Zustand prüfen: `launchctl print gui/$UID/com.local.whatsbot
   | grep -E "state|pid"` zeigt running. `curl -s
   http://127.0.0.1:8000/health` liefert 200.

### C11.6 — Live-Verifikation (vom Handy)

Schicke der Reihe nach vom Handy:

```
/import wabot /Users/hagenmarggraf/whatsbot
```

Erwartung: ✅-Reply mit Pfad, Rule-Vorschläge + Liste Neu-angelegt.

```
/ls
```

Erwartung: `wabot (imported) → /Users/hagenmarggraf/whatsbot`.

```
/p wabot zeig mir die letzten 3 Commits
```

Erwartung: Claude startet im echten Repo, `git log --oneline -3`
liefert Phase-11-Commits + Settings-Hardening.

```
/rm wabot
/rm wabot <panic-PIN>
```

Erwartung: `🗑 'wabot' entregistriert (Ordner unberührt).` Der
Repo-Ordner bleibt auf der Platte, nur die DB-Row geht.

Wenn alles grün ist, Abschluss-Commit:
`chore(phase-11): C11.6 live verified`. Danach ist Phase 11
vollständig abgeschlossen.

### Bekannte Post-Phase-11-Hygienepunkte

- **claude_sessions.session_id UNIQUE Impl-Debt**: seit Phase 4
  bekannt, bisher nicht-blockierend. Zwei frische Sessions mit
  leerer session_id kollidieren — heute beim Bot-Restart zum
  ersten Mal sichtbar in `launchd-stderr.log`
  (UNIQUE-constraint-Error aus `sqlite_claude_session_repository`).
  Fix: Schema auf NULL statt empty normalisieren oder UNIQUE
  droppen. Nächste Mini-Phase-Kandidat.
- **tests/integration/** leckten in die Live-DB bis Phase-11-C11.5
  (Pydantic ignorierte falsche Settings-Feldnamen silently).
  Gefixt via `ConfigDict(extra="forbid")` in `whatsbot/config.py`.
  Jeder zukünftige Test mit Typo im Settings-Feldnamen raised
  jetzt sofort.

## Bekannte Follow-ups, nicht-blockierend

- **Whitelist-Normalisierung**: `domain/whitelist.py::is_allowed`
  macht bewusst exakten String-Match. Realität: Meta liefert
  Absender ohne `+` (`491716598519`), Keychain musste heute
  entsprechend ohne `+` gesetzt werden. Entweder Dokstring +
  INSTALL.md klarstellen oder beidseitig `.lstrip('+')`.
  Entscheidung offen.
- **Meta Delivery-Status-Tracking**: Meta schickt `statuses`-
  Events für sent/delivered/read. Aktueller Webhook-Handler
  routet nur `messages`. Nice-to-have für `/status`, kein
  Blocker.
- **Schritt 10 Live-Deployment**: SIM-Port-Lock beim Carrier
  aktivieren (Spec §24). User-Action, außerhalb Code.
- **Token-Rotation-Reminder**: Der aktuelle Token ist
  "Never expires", aber Meta kann ihn bei App-Permission-
  Änderungen revozieren. Wenn in Zukunft plötzlich 401s
  auftauchen, in `docs/RUNBOOK.md` §Secret-Rotation nachsehen.

## Historische Notiz — Phase-9-Betriebs-Anpassung

Am 2026-04-23 wurde der Keychain-Eintrag `allowed-senders` von
`+491716598519,+4915228995372` auf `491716598519,4915228995372`
normalisiert, weil Meta die Absender-Nummern ohne `+` liefert und
`domain/whitelist.py::is_allowed` exakten String-Match macht.
Folge-Entscheidung (offen, siehe Follow-ups oben): Dokstring
klarstellen oder beidseitig `.lstrip('+')`.

## Phase 9 liefert (Live-Verhalten)

- `tests/smoke.py` — End-to-End-Journey via signed /webhook ohne
  Claude-Subprozess. 9 Schritte + 2 Guard-Tests. `make smoke`
  grün.
- Komplette Doc-Suite unter `docs/`: INSTALL (12-Schritt-Setup),
  RUNBOOK (9 Playbooks + Rotation + Updates + Rollback),
  SECURITY (Layer-Tabelle + 17 Denies + Threat-Model + akzeptierte
  Schwächen), MODES (3 Modi + FAQ), TROUBLESHOOTING (Diagnose vom
  Handy + Mac, häufige Symptome), CHEAT-SHEET (ein-Seiter aller
  Commands). Plus README.md mit Link-Matrix.
- `domain/text_sanitize.py` strippt Kontroll-Zeichen vor dem
  Command-Router — NULL, ESC, BEL, BS, DEL + C0 außer tab/LF/CR.
  Unicode + Emoji bleiben.
- E731-Erbe aus Phase 2 (`_DEFAULT_CLOCK = lambda …` in
  `delete_service.py`) auf `def` umgestellt. ruff clean auf allen
  angefassten Files.
- 35 neue Tests (21 text_sanitize unit + 14 edge_cases + 3 smoke
  + 2 docs-smoke-guards — Zählung inkl. Neu-Baseline).

## Phase 9 Architektur-Notes

- Smoke-Test ist bewusst **nicht** auf Claude-Subprocess-Ebene —
  Phase 4 C4.2-C4.7 decken das schon ab. `tests/smoke.py`
  arbeitet auf Command-Router-Level mit `RecordingSender`.
- Doc-Suite ist für Third-Party-Lesbarkeit geschrieben:
  INSTALL.md liest sich linear, RUNBOOK.md ist Symptom-first,
  SECURITY.md zitiert Spec-Abschnitte statt redundant zu sein.
- `text_sanitize` hat einen Fast-Path (`_needs_sanitize`), der
  saubere Strings ohne Allocation durchlässt. Hot-Path-fit.

## C8.4 liefert (Live-Verhalten)

- `GET /metrics` auf dem Hauptport (127.0.0.1:8000) rendert
  Prometheus-Textformat mit Counters + Gauges + Histograms.
  Tunnel-unreachable dank Phase-1-Invariante (Binding auf
  `settings.bind_host` default 127.0.0.1).
- `http/metrics.py` hält `MetricsRegistry` + `ResponseLatencyMiddleware`.
  Kein `prometheus_client`-Dependency — handrolled Format.
- Instrumentierte Hot-Paths:
  - Meta-Webhook POST → `whatsbot_messages_total{direction="in",
    kind="text"|"image"|"document"|"audio"|...}` nach Whitelist-Gate.
  - `MetricsMessageSender` als Outermost-Wrapper um den MessageSender
    → `whatsbot_messages_total{direction="out",kind="text"}` nur
    bei erfolgreichem send (kein Increment auf Exception-Pfad).
  - `ResponseLatencyMiddleware` (outermost) → histogram
    `whatsbot_response_latency_seconds{path,status_class}` mit
    coarse path-buckets (`webhook` / `hook` / `metrics` / `health` /
    `other`) × 2xx/3xx/4xx/5xx.
  - CircuitBreaker State-Transitions → `whatsbot_circuit_state
    {service,state}` Gauge (0/1 pro state). `set_state_observer(fn)`
    registriert die Callback in `adapters/resilience.py`; main.py
    verdrahtet das in die App-Registry.

## Phase-8-Architektur (C8.4-Layer)

- **HTTP** (new): `whatsbot/http/metrics.py` — `MetricsRegistry`-
  Klasse (Counter/Gauge/Histogram als in-memory dicts,
  `threading.Lock` geschützt), `_render_counters` /
  `_render_gauges` / `_render_histograms` für Text-Format.
  `_bucket_path` / `_bucket_status` halten Label-Kardinalität
  low. `ResponseLatencyMiddleware` subclasst `BaseHTTPMiddleware`,
  observiert jede Response über alle Pfade.
- **Adapter** (new): `whatsbot/adapters/metrics_sender.py` —
  dünner Wrapper um MessageSender.
- **Resilience-Hook**: `adapters/resilience.py` hat jetzt
  `set_state_observer(fn)` + module-level `_STATE_OBSERVER`. Das
  FSM feuert `_notify_state(service, new_state)` bei jeder echten
  State-Transition (CLOSED→OPEN, OPEN→HALF_OPEN, HALF_OPEN→CLOSED,
  HALF_OPEN→OPEN). Observer-Fehler werden via
  `contextlib.suppress(Exception)` geschluckt — Observer-Bugs
  dürfen den Breaker nie umwerfen.
- **main.py**: MetricsRegistry wird früh gebaut (damit der
  MetricsMessageSender den Outbound-Counter kriegt), an webhook
  router + state + Middleware gereicht. Circuit-Observer pipes
  state-transitions in die Registry.

**Tests-Stand**: 1501/1501 grün (C8.3 Baseline 1470 + 31 neu).
mypy --strict clean auf 119 source files. ruff clean auf allen
angefassten Files. Neue Tests:
- `tests/unit/test_metrics_registry.py` — 21 Tests (counter
  accumulate/value, gauge set/read, histogram buckets/sum/totals,
  render format inkl. label-escape + type comments + sort-stability,
  thread-safety smoke, parametrized label rendering).
- `tests/unit/test_metrics_wiring.py` — 6 Tests (MetricsMessageSender
  happy-path + no-count-on-failure + circuit-observer → gauge
  flip on open/close + observer-exception-safety).
- `tests/integration/test_metrics_e2e.py` — 5 Tests (signed
  /webhook → inbound/outbound counters populated + latency
  histogram series present + content-type text/plain +
  rejected sender does not bump counter + endpoint empty before
  traffic).

## Phase 8 — gesamt ✅

Alle vier Checkpoints durch. Phase 8 ist damit inhaltlich
abgeschlossen (Max-Limit-Persistenz + Diagnose-Commands +
Circuit-Breaker + Prometheus-Metrics). Wartet auf User-Freigabe
für **Phase 9 — Docs + Smoke-Tests + Polish**.

## C8.3 liefert (Live-Verhalten)

- `adapters/resilience.py` hält den `CircuitBreaker` pro
  `service_name` in einem module-scope Registry. `@resilient
  (service_name)`-Decorator wickelt beliebige Callables ein, zählt
  Fehler in einem Rolling-60s-Fenster, trippt nach 5 Fehlern auf
  OPEN für 5 min, promotet zu HALF_OPEN auf den ersten Call nach
  Cooldown, CLOSED bei Probe-Success / OPEN bei Probe-Failure.
- Drei Adapter dekoriert:
  - `WhatsAppCloudSender.send_text` → `meta_send`.
  - `MetaMediaDownloader.download` → `meta_media` (tenacity-retries
    zählen **eins**, nicht drei — das ist gewollt).
  - `WhisperCppTranscriber.transcribe` → `whisper`.
- `MediaService` fängt `CircuitOpenError` an allen drei Call-Sites
  (Image/PDF-Download + Audio-Download + Whisper-Transcribe),
  rendert user-facing `⚠️ [service] momentan nicht erreichbar,
  re-try in 4m 32s.` (Helper `_format_circuit_reply` +
  `_format_duration_seconds`). Neue `MediaOutcome.kind="circuit_open"`.
- CircuitBreaker loggt jede State-Transition strukturiert:
  `circuit_opened`, `circuit_half_open`, `circuit_closed`,
  `circuit_reopened_after_probe`. Phase-8-C8.4-Metrics-Layer
  greift später diese Events für den `circuit_state{service,state}`-
  Gauge ab.
- Thread-safety via `threading.Lock` auf Breaker-State +
  Registry-Lookup. Sync-Adapter werden von Tests und vom Webhook
  direkt aufgerufen; async-Pfade laufen über `asyncio.to_thread`
  und serialisieren damit ebenfalls über den ThreadPool.

## Phase-8-Architektur (C8.3-Layer)

- **Adapter** (new): `whatsbot/adapters/resilience.py`.
  - `CircuitState` StrEnum.
  - `CircuitOpenError(service_name, reopens_at)`.
  - `CircuitBreaker(service_name, failure_threshold=5,
    window_seconds=60, cooldown_seconds=300, clock=time.monotonic)`.
  - Test-Helper `_reset_registry_for_tests()` räumt die Registry
    zwischen Tests.
- **Decoration**: `@resilient(META_SEND_SERVICE)` /
  `@resilient(META_MEDIA_SERVICE)` / `@resilient(WHISPER_SERVICE)`
  über den drei Public-Methods. `LoggingMessageSender` bleibt
  explizit un-dekoriert (local no-op).
- **Application-Handling**: `media_service.py` importiert
  `CircuitOpenError`, fängt ihn vor den jeweiligen
  `MediaDownloadError`/`TranscriptionError`-Blöcken, und liefert
  `MediaOutcome(kind="circuit_open", reply=_format_circuit_reply(exc))`.

**Tests-Stand**: 1470/1470 passing (+1 skipped wenn ffmpeg fehlt).
mypy --strict clean auf 117 source files. ruff clean auf allen
angefassten Files. Neue Tests:
- `tests/unit/test_resilience.py` — 22 Tests (alle State-Transitions,
  Decorator-Semantik, gleicher service_name shares Breaker,
  Thread-Safety-Smoke).
- `tests/unit/test_media_service_circuit_open.py` — 3 Tests
  (image/pdf/audio-Pfad liefern `kind="circuit_open"` + User-Reply).
- `tests/integration/test_resilience_circuit_integration.py` — 3
  Tests mit echtem MetaMediaDownloader + httpx MockTransport:
  5x HTTP 503 trippen den `meta_media`-Breaker, 6ter Call
  short-circuited ohne httpx-Call; clock advance → probe →
  Recovery; probe-failure → OPEN mit frischer Cooldown.

## C8.2 liefert (Live-Verhalten)

- `/log <msg_id>` → rendert den chronologischen Trace aller
  JSONL-App-Events aus `settings.log_dir/app.jsonl`, die auf die
  `msg_id` matchen. Ohne Args: freundlicher Verwendungs-Hint.
  Bounded via `MAX_TRACE_EVENTS=200`; OutputService wickelt den
  >10KB-Dialog automatisch ab (Command-Handler → `output_service.
  deliver` ist schon C3.5-verdrahtet).
- `/errors` → letzte 10 WARNING/ERROR/CRITICAL-Events aus
  `app.jsonl`. `"keine Fehler in den letzten Events 🎉"` bei leerem
  Log.
- `/ps` → Aktive Claude-Sessions mit Mode-Badge + Lock-Owner-Badge
  + tmux-liveness + Tokens/Turns/Context-Fill. `DiagnosticsService.
  active_sessions()` joined `claude_sessions` + `session_locks` +
  `tmux.list_sessions(prefix="wb-")`. Ohne tmux (TEST-env) →
  `"keine aktiven Sessions."`.
- `/update` → Text-Hint auf manuellen Claude-Code-Update-Ablauf
  (Spec §22). Kein State-Zugriff, funktioniert auch wenn
  DiagnosticsService fehlt.
- Lockdown-Filter: `/errors`, `/ps`, `/log`, `/update` sind
  jetzt Teil der Lockdown-Allow-List (read-only Diagnostik, hilft
  dem User die Ursache zu finden bevor er den PIN tippt).

## Phase-8-Architektur (C8.2-Layer)

- **Domain** (pure): `whatsbot/domain/log_events.py` — `LogEntry`
  dataclass, `parse_log_line` (robust gegen Garbage), `filter_by_msg_id`,
  `filter_errors`. `ERROR_LEVELS = frozenset({"error", "warning",
  "critical"})`.
- **Port**: `whatsbot/ports/log_reader.py` — Protocol `LogReader.
  read_tail(*, max_lines)`.
- **Adapter**: `whatsbot/adapters/file_log_reader.py` — tail't
  `<log_dir>/<filename>` via `collections.deque(fh, maxlen=...)`,
  silent bei FileNotFound / OSError. Garbage-Zeilen werden von
  `parse_log_line` geschluckt, nie geraised.
- **Application**: `whatsbot/application/diagnostics_service.py` —
  `DiagnosticsService` mit 4 Public-Methoden (`read_trace`,
  `recent_errors`, `active_sessions`, `format_update_hint`) plus
  jeweilige `format_*`-Renderer. `SessionSnapshot` dataclass.
  Alle externen Services (tmux / locks / claude_sessions) sind
  optional — fehlt einer, degradiert die Funktion statt zu crashen.
- **Wiring** in `main.py`: `DiagnosticsService` wird unconditional
  gebaut (FileLogReader ist billig, fehlendes log_dir → `[]`) und
  an den CommandHandler via `diagnostics_service=`-Param übergeben.

**Tests-Stand**: 1442/1442 passing (+1 skipped wenn ffmpeg fehlt).
mypy --strict clean auf 116 source files, ruff clean auf allen
angefassten Files. Neue Tests:
- `tests/unit/test_log_events.py` — 10 Tests.
- `tests/unit/test_file_log_reader.py` — 7 Tests.
- `tests/unit/test_diagnostics_service.py` — 17 Tests.
- `tests/unit/test_diagnostics_commands.py` — 11 Tests.
- `tests/integration/test_diagnostics_e2e.py` — 5 Tests (signed
  /webhook, tmp_path-JSONL, tmux-less → /ps leer).

## C8.1 liefert (Live-Verhalten)

- `UsageLimitEvent` im Transcript → persistiert in
  `max_limits` mit `reset_at_ts`, preserviert `warned_at_ts`
  über Re-Emits.
- `/p <name> <prompt>` während aktives Fenster →
  `⏸ Max-Limit erreicht [session_5h] · Reset in 3h 22m`
  (kein tmux-Spin-up, kein send_keys, kein 📨-ack).
- `MaxLimitSweeper` tickt 60s, feuert WhatsApp-Warnung bei
  <10% Remaining einmal pro Window, prunt expired Rows.
- Lifespan-opt-in via `enable_media_sweeper`-Parallel-Pattern
  (sweeper auto-off in TEST, auto-on in prod/dev).

## Pre-existing Schuld (nicht-blockierend für Phase 8)

`claude_sessions.session_id TEXT UNIQUE` kollidiert wenn zwei
frische Sessions beide leeren session_id haben. Fix gehört in
einen Phase-4-Cleanup-Commit (NULL statt empty oder UNIQUE drop).

`whatsbot/application/delete_service.py:48` — E731
(lambda-Assignment für `_DEFAULT_CLOCK`). Phase-2-Erbe;
trivialer `def`-Rewrite, aber außerhalb Scope.

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
