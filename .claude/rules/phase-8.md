# Phase 8: Observability + Limits

**Aufwand**: 2 Sessions
**Abhängigkeiten**: Phase 4 komplett ✅ (TranscriptIngest +
ClaudeSessionRepository vorhanden)
**Parallelisierbar mit**: Phase 5, 6, 7 (alle erledigt)
**Spec-Referenzen**: §11 (Commands), §14 (Max-Limit-Handling),
§15 (Observability), §20 (Performance + Resilience, Circuit
Breaker), §25 (FMEA #1 Meta-API-Outage), §21 Phase 8

## Ziel der Phase

**Vom Handy aus debuggbar.** Nach Phase 8 kann ich live sehen, was
der Bot gerade tut, welche Max-Limits laufen, welche Fehler die
letzten Stunden hatten, und der Bot bricht nicht stumm zusammen
wenn eine externe Abhängigkeit wackelt.

Phase 8 ist vier Themen, die zusammen den Observability-Loop
schließen:

1. **Max-Limit-Persistenz + proaktive Warnung**: Die
   `max_limits`-Tabelle (aus Schema seit Phase 1) wird endlich
   *befüllt* — die `UsageLimitEvent`-Callback-Hookup aus Phase 4
   läuft in einen echten Service, der pro Kind einen Reset-Timer
   hält und bei <10% Remaining genau einmal pro Fenster eine
   WhatsApp-Warnung schickt. Prompts, die während eines aktiven
   Reset-Fensters reinkommen, werden abgelehnt statt gequeued
   (Spec §14: "Sofort ablehnen, keine Queue").

2. **Diagnose-Commands**: `/log <msg_id>` zeigt den vollen Trace
   einer Message (Event-Kette aus den JSONL-Logs), `/errors`
   listet die letzten 10 Errors, `/ps` die laufenden tmux-Sessions
   mit Tokens/Turns/Mode/Lock-Badge, `/update` erklärt wie man
   Claude Code manuell aktualisiert. Die bestehenden `/status`
   + `/help` werden erweitert.

3. **Circuit-Breaker für externe Adapter**: Meta-Outbound-Sender,
   Meta-Media-Downloader, Whisper-Transcriber bekommen einen
   einheitlichen `@resilient("service_name")`-Decorator: 5 Fehler
   in 60s → 5min OPEN → Half-Open-Probe → CLOSED. Kaputte
   externe Dienste drücken den Bot nicht mehr über die Klippe;
   sie blockieren sich selbst kurz und geben den anderen
   Request-Pfaden CPU + Log-Ruhe.

4. **Prometheus `/metrics`-Endpoint**: Der Phase-1-Stub auf
   `GET /metrics` wird real. Counters + gauges per Spec §15:
   outbound-/inbound-Messages, Claude-Turns, Tool-Denies,
   Output-Redactions, Latenz-Histogramme, Lock-State, aktive
   Sessions, Token-Verbrauch. Gebunden an `127.0.0.1` (Phase-1-
   Invariante), niemals über den Tunnel erreichbar.

Phase 8 endet damit, dass:

1. Ein künstlicher `usage_limit_reached`-Event aus dem Transcript
   landet als `max_limits`-Row; ein Prompt ins selbe Projekt wird
   mit `"⏸ Max-Limit erreicht [session] · Reset in 3h 22m"`
   abgelehnt; bei <10% Remaining kommt genau eine WhatsApp-
   Warnung pro Fenster.
2. `/log <msg_id>` zeigt die vollständige Event-Kette einer
   Inbound-Message (webhook → sanitize → command → outbound).
3. Mock-Meta-Outage: 5 Fehler in 60s → nächste Calls sofort
   abgelehnt mit CircuitOpen-Error + Alert in `/errors`. Nach
   5min läuft ein einzelner Half-Open-Probe-Request; Erfolg
   schließt den Breaker, Fehler hält ihn offen.
4. `curl localhost:8000/metrics` liefert Prometheus-Text-Format
   mit populated Werten. Der Endpoint ist *nicht* über die
   Cloudflare-Tunnel-URL erreichbar.

## Voraussetzungen

- **Phase 4** komplett (TranscriptIngest liefert die
  `UsageLimitEvent` via Callback; `ClaudeSessionRepository` + tmux
  sind da).
- **Phase 1** `structlog`-Setup (JSON-Logs + Correlation-IDs) ist
  seit C1.5 live. Phase 8 konsumiert nur, schreibt nicht neu.
- **Phase 1** `/metrics`-Stub existiert und bindet an localhost.

## Was gebaut wird

### 1. Domain — Max-Limits (pure)

- **`whatsbot/domain/limits.py`** — pure:
  - `LimitKind`-StrEnum: `SESSION_5H`, `WEEKLY`, `OPUS_SUB` (exakt
    die drei Kinds aus Spec §14 + §19 Schema).
  - `MaxLimit`-Dataclass: `kind`, `reset_at_ts` (Unix epoch
    seconds), `warned_at_ts` (nullable), `remaining_pct` (0.0–1.0).
  - `LOW_REMAINING_THRESHOLD = 0.10` (Spec §14 "Proaktive Warnung
    bei <10%").
  - `format_reset_duration(reset_at_ts, now) -> str` — pure
    Formatter, gibt `"3h 22m"` / `"42m"` / `"15s"` zurück.
  - `should_warn(limit, now) -> bool` — pure: True wenn
    remaining_pct < 10% UND ((warned_at_ts ist None) ODER
    (reset_at_ts > warned_at_ts > reset_at_ts − fenstergröße);
    letzteres damit eine Rotation (neues Fenster) die Warn-Uhr
    resettet).
  - `is_active(limit, now) -> bool` — True solange now < reset_at_ts.
  - `shortest_active(limits, now) -> MaxLimit | None` — Spec §14
    "Bei mehreren aktiv: kürzester Countdown in Antwort".
  - `parse_reset_at(raw) -> int | None` — robust ISO-Parser, tolerant
    gegen Millisekunden-Suffixe und fehlende TZ-Info (assumes UTC).

### 2. Port + Adapter

- **`whatsbot/ports/max_limits_repository.py`** — Protocol:
  `get(kind) -> MaxLimit | None`, `upsert(limit) -> None`,
  `list_all() -> list[MaxLimit]`, `delete(kind) -> None`,
  `mark_warned(kind, ts) -> None`.
- **`whatsbot/adapters/sqlite_max_limits_repository.py`** —
  Standard-sqlite-Pattern wie z.B.
  `sqlite_session_lock_repository`. `mark_warned` ist ein
  targeted partial-update statt Full-upsert (nur eine Column).

### 3. Application

- **`whatsbot/application/limit_service.py`** — Use-Cases:
  - `record(event: UsageLimitEvent)` — wird vom
    TranscriptIngest-Callback gerufen. Upsertet die Row und
    loggt `limit_recorded`.
  - `check_guard(project_name) -> MaxLimit | None` — wird vom
    `SessionService.send_prompt` aufgerufen; wenn ein `kind`
    noch aktiv ist (now < reset_at_ts), returnt die kürzeste
    verbliebene Row → Prompt-Send raise't
    `MaxLimitActiveError`, den der CommandHandler in
    `"⏸ Max-Limit erreicht [session] · Reset in 3h 22m"`
    rendert.
  - `maybe_warn(default_recipient)` — wird periodisch aus dem
    TranscriptIngest-Callback nachgeschaltet (nicht auf einem
    eigenen Timer — hält den Service synchron). Prüft alle
    `list_all()`-Rows auf `should_warn`, feuert genau eine
    WhatsApp-Warnung pro Fenster, ruft `mark_warned(now)`.
  - `sweep_expired(now)` — löscht Rows deren `reset_at_ts`
    vergangen ist (read-path optimiert, nicht strikt nötig —
    `check_guard` filtert auch auf `is_active`, aber eine
    abgelaufene Row sollte nicht für immer rumliegen).
  - `opus_auto_switch_if_needed(project_name) -> bool` — wenn
    Opus-Sub-Limit < 10% und Projekt hat
    `default_model="opus"`, temporär auf `sonnet` switchen +
    User informieren. Spec §14.

- **`whatsbot/application/diagnostics_service.py`** —
  Read-only-Backend für `/log`, `/errors`, `/ps`:
  - `read_trace(msg_id) -> list[LogEntry]` — Pfad: tail die JSONL-
    App-Logs aus `settings.log_dir`, grep auf
    `wa_msg_id=msg_id`, chrono-sort. Bounded (z.B. 500 Zeilen
    pro Request, neueste zuerst).
  - `recent_errors(n=10) -> list[LogEntry]` — gleiches Muster,
    filter auf `level="error"` + `level="warning"`, take last
    `n`.
  - `active_sessions() -> list[SessionSnapshot]` — liest
    `claude_sessions` + vergleicht mit `tmux.list_sessions()`
    (tmux ist Truth für "läuft gerade"); zeigt Mode, Tokens,
    Turn-Count, Lock-Owner, Last-Activity.

### 4. Circuit-Breaker

- **`whatsbot/adapters/resilience.py`** — neu:
  - `CircuitBreaker`-Klasse: CLOSED / OPEN / HALF_OPEN-State,
    Fehler-Counter mit Zeitfenster (5 Fehler in 60s → OPEN),
    OPEN-Dauer 5min, ein Half-Open-Probe pro Cool-Down.
  - `@resilient(service_name)`-Decorator um beliebige Callables.
    Der Decorator hält einen `CircuitBreaker` pro
    `service_name` im Modul-Scope (Process-lokal; kein DB-
    State). Bei OPEN raise't er `CircuitOpenError(
    service_name, reopens_at)`.
  - Structured logging auf jedem State-Transition:
    `circuit_opened`, `circuit_half_open`, `circuit_closed`.
- Anwendung:
  - `whatsbot/adapters/whatsapp_sender.py` — `send_text` durch
    Decorator geschützt (relevant wenn `WhatsAppCloudSender`
    live geht; `LoggingMessageSender` ignoriert den Decorator
    harmlos).
  - `whatsbot/adapters/meta_media_downloader.py` — `download`
    bekommt den Decorator.
  - `whatsbot/adapters/whisper_cpp_transcriber.py` —
    `transcribe`.
- CommandHandler-Fehlermeldung bei CircuitOpenError:
  `"⚠️ [service_name] momentan nicht erreichbar, re-try in
  4m 32s."`.

### 5. Metrics-Endpoint

- **`whatsbot/http/metrics.py`** — eigener Router:
  - `MetricsRegistry`-Klasse — Counters, Gauges, Histograms als
    einfache In-Memory-Dicts. Keine externe `prometheus_client`-
    Dependency (Spec §15 sagt "Prometheus-Naming", nicht
    "Prometheus-Client-Library"; wir bleiben minimal).
  - Render-Funktion: produziert das Prometheus-Text-Format
    (`# HELP`, `# TYPE`, Metric-Linien).
  - Registry ist ein `app.state.metrics_registry`-Singleton so
    dass alle Application-Services die gleichen Counters
    inkrementieren können.
- Metriken (Spec §15 Liste):
  - `whatsbot_messages_total{direction,kind}` — counter
  - `whatsbot_claude_turns_total{project,model,mode}` — counter
  - `whatsbot_pattern_match_total{severity}` — counter (Layer 2
    Deny-Hits)
  - `whatsbot_redaction_applied_total{pattern}` — counter
  - `whatsbot_response_latency_seconds{percentile}` — gauge
    (einfache running-quantiles; keine T-Digest)
  - `whatsbot_tokens_used_total{project,model}` — counter
  - `whatsbot_session_active_gauge` — gauge
  - `whatsbot_mode_duration_seconds{mode}` — gauge
  - `whatsbot_hook_decisions_total{tool,decision}` — counter
  - `whatsbot_circuit_state{service,state}` — gauge (0/1 per
    state so Prometheus kann `sum by state` machen)
- Router-Guard: `X-Forwarded-For`-Check ist unnötig weil der
  Endpoint nur an 127.0.0.1 gebunden ist (Phase-1-Invariante).

### 6. CommandHandler-Updates

- **`/log <msg_id>`** — neue Route, ruft
  `diagnostics_service.read_trace`. Rendert Event-Liste als
  mehrzeilige WhatsApp-Message (truncated bei 500 Zeilen /
  >4KB Body → long-output-Pipeline greift).
- **`/errors`** — `diagnostics_service.recent_errors(10)`.
- **`/ps`** — `diagnostics_service.active_sessions()`.
- **`/metrics`** (WhatsApp-Version, nicht der HTTP-Endpoint) —
  kurzer Tages-Digest aus dem Metrics-Registry.
- **`/update`** — rein informational: "Claude Code updates laufen
  manuell via `./install-claude-code.sh`; siehe RUNBOOK.md §Update."
- **`/status`**-Erweiterung: laufende Max-Limits (kind + remaining),
  Heartbeat-Alter, aktive Session-Count, Circuit-Breaker-State
  pro Service.

### 7. Wiring

- **`whatsbot/main.py`**:
  - `SqliteMaxLimitsRepository(conn)` + `LimitService` +
    `DiagnosticsService` + `MetricsRegistry` bauen.
  - TranscriptIngest bekommt `on_usage_limit=limit_service.record`.
  - SessionService bekommt `limit_service` optional im ctor;
    `send_prompt` ruft `check_guard` vor dem acquire_for_bot.
  - CommandHandler bekommt `diagnostics_service`,
    `limit_service`, `metrics_registry`.
  - Metrics-Registry in `app.state.metrics_registry`.
  - Periodic sweep-task im Lifespan (`MaxLimitSweeper` analog
    zum `MediaSweeper` — alle ~60s `limit_service.sweep_expired`
    + `maybe_warn`). Optional: kein eigenes Adapter-Port nötig,
    reiner Lifespan-Task.

## Checkpoints

### C8.1 — Max-Limit-Persistenz + 10%-Warnung

- Domain + Port + Adapter aus §1–§3.
- TranscriptIngest.on_usage_limit wird vom
  LimitService konsumiert; `max_limits`-Row landet in DB.
- SessionService.send_prompt ruft LimitService.check_guard —
  während aktives Fenster raise't es `MaxLimitActiveError`; der
  CommandHandler rendert `"⏸ Max-Limit erreicht [kind] · Reset
  in 3h 22m"`.
- Periodic sweep-task (`MaxLimitSweeper` im Lifespan) feuert
  `maybe_warn` bei <10%. Warn-Row in `max_limits.warned_at_ts`
  wird gesetzt — eine zweite Prüfung in derselben Periode ist
  no-op.
- Tests: 6+ unit für `domain/limits.py`
  (format_reset_duration-Edges, should_warn-Matrix,
  shortest_active, parse_reset_at), 6+ unit für LimitService
  (record, check_guard active vs. expired, maybe_warn
  once-per-window, sweep_expired), 1 e2e via /webhook (signed
  POST mit einem künstlich vorgeseeded max_limits-Row →
  `/p <project> hi` → reply enthält `⏸ Max-Limit`).

### C8.2 — /log + /errors + /ps

- `DiagnosticsService.read_trace` / `recent_errors` /
  `active_sessions` — liest JSONL-Logs aus `settings.log_dir`,
  filtert auf `wa_msg_id` / Level / Live-Tmux-Sessions.
- CommandHandler-Routes für `/log <msg_id>`, `/log` (ohne Args
  → hint), `/errors`, `/ps`, `/update`.
- `/log`-Output kann lang werden → durchläuft die existierende
  Spec §10 Output-Size-Pipeline (C3.5).
- Tests: 4+ unit für DiagnosticsService (Tail-Logik,
  msg_id-Filter, non-JSON-Zeilen robust skippen, leere Log-
  Directory → leere Liste), 1 integration via /webhook (signed
  POST `/log abc123` → reply enthält Event-Chain; nutzt Fixture-
  JSONL-Datei im tmp_path).

### C8.3 — Circuit-Breaker für externe Adapter

- `adapters/resilience.py` mit `CircuitBreaker` +
  `@resilient`-Decorator.
- 3 Adapter dekoriert: WhatsAppCloudSender, MetaMediaDownloader,
  WhisperCppTranscriber.
- CircuitOpenError-Handling in den Application-Services:
  MediaService-Pfad gibt strukturierten `circuit_open`-outcome;
  Webhook rendert user-facing Reply.
- Tests: 8+ unit für CircuitBreaker (CLOSED → OPEN nach 5
  failures in window; OPEN raise't sofort ohne Adapter-Call;
  5min-Cool-Down → HALF_OPEN; Half-Open-Probe-success → CLOSED
  + counter-reset; Half-Open-Probe-failure → OPEN + neue
  Cool-Down; Thread-Safety wenn Sync-Adapter von async event-
  loop via `asyncio.to_thread`; Decorator bindet pro
  service_name eigenen Breaker). 2 integration (echter
  MetaMediaDownloader mit httpx MockTransport, der 5x HTTP 503
  zurückgibt → 6ter Call raise't CircuitOpenError ohne HTTP-
  Kontakt; nach `advance_clock(5*60+1)` → ein Probe-Call wird
  wieder gemacht).

### C8.4 — Prometheus /metrics

- `http/metrics.py` Router ersetzt den Phase-1-Stub. Registry ist
  `app.state.metrics_registry`.
- Application-Services rufen `metrics.increment(...)` an den
  richtigen Stellen — nicht viele: meta_webhook POST-handler
  (inbound counter), WhatsAppSender.send_text (outbound),
  TranscriptIngest Turn-End (claude_turns + tokens),
  HookService.classify_bash (hook_decisions), OutputPipeline
  (redaction_applied), LockService (session_active_gauge).
- Response-Latenz-Histogramm via ASGI-Middleware analog zu
  `CorrelationIdMiddleware`.
- Circuit-State-Gauge wird vom CircuitBreaker bei jedem
  State-Transition aktualisiert.
- Tests: 4+ unit für MetricsRegistry (Counter.inc,
  Gauge.set, Histogram.observe + percentile-compute,
  render-Format mit korrekten `# HELP`/`# TYPE`-Lines), 2
  integration (GET `/metrics` über TestClient nach einigen
  `/webhook`-Calls → Body enthält populated Counters; GET
  `/metrics` über externe URL = 403 / nicht erreichbar wenn
  Bind-Host localhost ist).

## Success Criteria

- [ ] `max_limits`-Tabelle wird gefüllt, sobald Claude einen
      `usage_limit_reached`-Event im Transcript meldet.
- [ ] Prompts während aktiven Reset-Fensters werden mit
      `⏸ Max-Limit erreicht`-Reply abgelehnt, nicht gequeued.
- [ ] Warnung bei <10% Remaining feuert genau einmal pro Fenster.
- [ ] `/log <msg_id>`, `/errors`, `/ps`, `/metrics`, `/update`
      antworten sinnvoll.
- [ ] Mock-Meta-Outage nach 5 Fehlern in 60s: weitere Calls
      raise't CircuitOpenError *ohne* Adapter zu kontaktieren;
      nach 5min wird ein Probe gemacht.
- [ ] `curl http://127.0.0.1:8000/metrics` liefert gültiges
      Prometheus-Text-Format mit ≥5 populated Counters nach
      einem /webhook-Test-Ping.
- [ ] `/metrics` ist *nicht* über die Cloudflare-Tunnel-URL
      erreichbar (Invariante aus Phase 1 + §15).
- [ ] Alle Domain/Service-Tests grün, mypy --strict clean, ruff
      clean.
- [ ] CHANGELOG-Einträge pro Checkpoint.

## Abbruch-Kriterien

- **Max-Limit-Parser unzuverlässig** (künstliche Transcript-
  Events landen nicht in der DB-Tabelle, oder Status-Line-
  Fallback produziert falsche reset_at): Stop. Eigene Heuristik
  aus Status-Line-Regex als primärer Pfad, Transcript-Event als
  Fallback. Spec §21 Phase 8 nennt das explizit als Abbruch-
  Kriterium.
- **CircuitBreaker Thread-Safety verletzt** (Race zwischen
  Parallel-Calls produziert falsche State-Transitions): Stop.
  Breaker mit `threading.Lock` serialisieren und Tests
  erweitern.
- **JSONL-Log-Tail langsam** (>500ms für `/log`-Request auf
  großem Log): Stop. Entweder Log-Größe-Cap aggressiver (Spec
  §15 RotatingFileHandler schon da, 10MB × 5) oder Log-Tail mit
  `tail -n 1000`-Shell-Pipe statt Python-Read.
- **Prometheus-Text-Format-Parse-Fehler** (Prometheus oder
  Grafana-Test weigert sich, unseren Body zu akzeptieren): Stop.
  Auf `prometheus_client`-Library umsteigen und
  Dependency-Trade-off akzeptieren.

## Was in Phase 8 NICHT gebaut wird

- **`tests/smoke.py`** (End-to-End mit Mock-Meta-Server) — Phase 9.
- **Docs-Vervollständigung** (RUNBOOK-Einträge für die neuen
  Commands, SECURITY.md-Updates) — Phase 9.
- **Log-Retention-Policy-Cleanup** (`history.jsonl` pro Projekt
  wird nicht automatisch rotiert) — Phase-9-Thema.
- **Grafana-Dashboards** / externe Metrics-Drain — explizit
  außerhalb Scope (Spec §26 Schwäche #2 sagt Audit-Log ist nicht
  Append-Only und external drain ist optional).
- **Dogfood-Integration** zwischen LimitService und BypassService:
  `/force` ignoriert Max-Limit-Guard bewusst nicht — wer
  `/force` tippt, weiß was er tut. Dokumentieren in RUNBOOK Phase 9.

## Architektur-Hinweise

- **LimitService + TranscriptIngest**: der Callback-Hookup aus
  Phase 4 (`on_usage_limit`) ist intentional minimal. Phase 8
  fügt *nicht* einen zweiten Callback für "maybe-warn" dazu —
  stattdessen läuft ein leichter Lifespan-Task (60s-Tick), der
  `maybe_warn` auf allen aktiven Limits ausführt. Macht Testing
  einfacher: LimitService bleibt sync + stateful ohne asyncio-
  Kopplung.

- **DiagnosticsService reads JSONL logs**: Spec §15 schreibt die
  Logs *bereits* als JSONL. Der Service macht `tail + grep` in
  Python (nicht shell-exec, sauberer weil kein subprocess +
  portable in tests). Bounded to last 1000 lines per request so
  ein 10MB-Log nicht den Event-Loop blockiert.

- **CircuitBreaker thread-safety**: die Decorator-Variante wickelt
  sync + async Callables gleich — async-Sites werden per
  `asyncio.Lock` serialisiert, sync-Sites per `threading.Lock`.
  Der Breaker-State selber ist atomic genug (drei Felder + Counter)
  dass ein TOCTOU-Race nur eine *extra* Exception/Probe kostet,
  nicht Daten-Korruption.

- **Metrics ohne prometheus_client**: wir vermeiden die externe
  Dependency bewusst. Die Exposition ist einfach genug zum
  Handrollen (Counter.inc, Gauge.set, Histogram.observe +
  Percentile-Compute), und jede zusätzliche Dependency ist in
  Spec §5 vierfach-verriegelter Subscription-Welt Risiko für
  Side-Effect-Crashes.

## Nach Phase 8

Update `.claude/rules/current-phase.md` auf Phase 9. Phase 9
(Docs + Smoke-Tests + Polish) ist die Abschluss-Phase — hängt an
allen vorigen + `tests/smoke.py` läuft nur sinnvoll wenn
Observability-Commands da sind. Warte auf User-Freigabe.
