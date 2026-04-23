# Changelog

Alle nennenswerten Änderungen am `whatsbot`-Repo. Format: phasen-/checkpoint-basiert,
neueste oben. Sieh dazu `.claude/rules/current-phase.md` für den Live-Stand.

## [Unreleased]

### Phase 7 — Medien-Pipeline ✅ (complete)

Alle 5 Checkpoints grün. End-to-End-Medien-Pipeline steht: Bilder,
PDFs, Voice-Messages vom Handy fließen durch Meta Graph → Validation
→ Cache → (ffmpeg + whisper für Voice) → `SessionService.send_prompt`
→ tmux → Claude. Unsupportete Kinds (Video/Location/Sticker/Contact)
bekommen freundliche Reject-Replies. Der Cache wird von einem
async Sweeper unter Spec §16 Retention-Policy gehalten (7 Tage TTL,
1 GB Size-Cap, secure-delete mit Zero-Overwrite).

- ✅ C7.1 — Image-Pipeline + Reject-Pfade
- ✅ C7.2 — PDF-Pipeline
- ✅ C7.3 — Audio-Pipeline (Download + ffmpeg)
- ✅ C7.4 — Whisper-Transkription + Audio-Send
- ✅ C7.5 — Cache-Sweeper

**Tests**: 1330/1330 passing + 1 skipped (ffmpeg-real), +226 von
Phase-6-Baseline. mypy --strict clean auf 107 source files.

#### C7.5 — Cache-Sweeper ✅

Der Media-Cache wird jetzt automatisch unter Spec §16 Retention-
Policy gehalten: alle 10 Minuten läuft ein async Sweeper, der erst
TTL-abgelaufene Items (>7 Tage) entfernt, dann bei Gesamtcache
>1 GB oldest-first weitere Items bis unter Cap evictet. Pattern
identisch zum Phase-6-HeartbeatPumper (async Task + idempotent
start/stop + asyncio.to_thread für Disk-IO).

- **Domain (pure)**: `domain/media_cache.py` —
  `CACHE_TTL_SECONDS = 7*86400`, `CACHE_MAX_BYTES = 1 GiB`,
  `is_expired(item, now, ttl)` (>=-boundary verhindert Flicker
  zwischen Sweeps), `select_expired` + `select_for_eviction`
  (oldest-first, respektiert vom Caller gelieferte
  `current_size`-Summe).
- **Application**: `application/media_sweeper.py` — asyncio-Loop
  mit `DEFAULT_SWEEP_INTERVAL_SECONDS = 600`, idempotente
  start/stop, `sweep_now` für On-Demand-Aufrufe, `SweepReport`-
  Dataclass mit ttl_deleted / size_deleted / bytes_freed. Jede
  Exception (list_failure, delete_failure) wird log-only; der
  Sweeper kann nie am Disk-Problem sterben, nur ticken.
- **Wiring**: `main.py` baut `cache_impl` jetzt unconditional
  (auch ohne MediaService — Sweeper kümmert sich um stale Files
  aus vorherigen Prod-Läufen). Sweeper läuft als zweiter
  FastAPI-Lifespan-Task neben HeartbeatPumper. Default ON in
  prod/dev, `enable_media_sweeper=True` opt-in in TEST damit
  bestehende Test-Suites nicht plötzlich einen Background-Loop
  bekommen.
- **Tests**: 14 unit für Domain (`test_media_cache_domain.py`:
  is_expired-Edges + Boundary, select_expired happy + empty +
  all-fresh, select_for_eviction empty + under-cap + exact-cap +
  oldest-first + single-large-item + stops-at-cap +
  caller-supplied current_size). 12 unit für Sweeper
  (`test_media_sweeper.py`: TTL-only, size-only, combined,
  no-op, list-failure Containment, delete-failure Containment,
  initial sweep in start(), start/stop Idempotenz, periodic
  loop fires). 3 integration
  (`test_media_sweeper_lifespan.py`: echter FileMediaCache +
  FastAPI-Lifespan räumt stale Files bei startup, Sweeper ist
  disabled-by-default in TEST, secure_delete zeros-vor-unlink
  als Regression-Check).

**Tests**: 1330/1330 passing (+29 vs. C7.4), mypy --strict
clean, ruff clean.

#### C7.4 — Whisper-Transkription + Audio-Send ✅

Voice-Messages gehen jetzt end-to-end vom Handy zu Claude: OGG
herunterladen, mit ffmpeg auf 16 kHz mono WAV normalisieren,
über whisper.cpp transkribieren, den Text bereinigen
(`clean_transcript`) und als Prompt an das aktive Projekt
senden. Der User bekommt sofort nach dem Empfang einen
"🎙 Transkribiere…"-Ack, damit die 2-10 s Whisper-Latenz keine
Verwirrung stiftet.

- **Domain (pure)**: `domain/transcription.py` —
  `clean_transcript` strippt Whisper-Bracket-Annotations
  ([BLANK_AUDIO], [Music], [Laughter], …), entfernt
  Timestamp-Prefixes (`[00:00:01.000 --> 00:00:04.500]`),
  normalisiert Whitespace und trunkiert bei
  `MAX_TRANSCRIPT_CHARS = 4000` mit `…`-Suffix. Pure-Funktion,
  testbar ohne I/O.
- **Port**: `AudioTranscriber`-Protocol + `TranscriptionError`
  (`whatsbot/ports/audio_transcriber.py`). Kontrakt:
  `transcribe(wav_path, language=None) -> str`. Sprache-Default
  ist `None` (whisper autodetect), passt zum DE/EN-Mix auf dem
  Bot-Handy.
- **Adapter**: `WhisperCppTranscriber`
  (`whatsbot/adapters/whisper_cpp_transcriber.py`) — shell-freier
  Subprocess-Aufruf `whisper-cli -m <model> -l <lang|auto>
  -f <wav> -nt -np -otxt -of <stem>`. Liest primär aus der
  `<stem>.txt`-Ausgabe (vermeidet Info-Noise auf stdout), mit
  stdout-Fallback für ältere whisper.cpp-Builds, die `-otxt`
  ignorieren. 60 s Timeout (6x Spec §20 Budget). Fehlender
  Binary oder Modell-File → klare `TranscriptionError`-Message.
- **Application**: `MediaService.process_audio` = Stage-1
  (`process_audio_to_wav` aus C7.3) + Stage-2 (transcribe →
  clean_transcript → `SessionService.send_prompt`). Neue
  Outcome-Kinds `transcription_failed` und `empty_transcript`
  (whisper hat gelaufen aber keinen Text geliefert — reine
  Stille / Hintergrundrauschen). Der Voice-Prompt durchläuft
  den Spec-§9-Sanitize-Pfad in `send_prompt` automatisch —
  Voice-Inhalte sind genauso untrusted wie geschriebene Prompts.
- **HTTP**: `_dispatch_media` routet `MediaKind.AUDIO` jetzt
  zu `process_audio` (nicht mehr `process_unsupported`). Der
  POST-Handler sendet `"🎙 Transkribiere…"` VOR dem
  dispatch — zwei Messages gehen raus (ack + final), in
  dieser Reihenfolge. Der Ack läuft nur, wenn MediaService UND
  `media_id` gesetzt sind, damit ein misskonfigurierter Bot
  keinen Ack vor "⚠️ Medien werden gerade nicht angenommen"
  sendet.
- **Settings**: `whisper_binary` default `whisper-cli`,
  `whisper_model_path` default
  `~/Library/whisper-cpp/models/ggml-small.bin`. INSTALL.md-
  Thema: die brew-Version von whisper.cpp bringt kein Modell
  mit — User muss `./models/download-ggml-model.sh small`
  einmal ausführen.
- **Wiring**: `main.py` baut `WhisperCppTranscriber` default in
  prod/dev, test-injectable via `create_app(audio_transcriber=
  ...)`. Fehlendes Modell-File stoppt den Start NICHT — die
  Adapter-Konstruktion loggt eine Warnung und der erste
  Audio-Call fällt auf `transcription_failed` zurück; besser
  als silent disable.
- **Tests**: 28 unit für `clean_transcript`
  (`test_transcription.py`: pass-through, whitespace,
  non-string defensive, 11 Bracket-Annotation-Varianten,
  timestamp-prefixes in 3 Formaten, non-annotation brackets
  bleiben, blank-line collapse, per-line trim, Truncation-Edges).
  8 unit für `MediaService.process_audio`
  (`test_media_service_audio_e2e.py`: happy path,
  stage-1-Failure-propagation (no_active_project,
  download_failed, conversion_failed), transcription_failed,
  unwired transcriber, empty_transcript für reinen
  `[BLANK_AUDIO]`-Output, cleaned transcript reaches
  send_prompt ohne Markup). 1 dispatcher-test
  (`test_iter_media_audio_dispatch.py`: signed /webhook →
  audio payload → genau 2 replies in Reihenfolge (ack +
  📨-final), stub-MediaService empfängt media_id + mime +
  sender korrekt).

**Tests**: 1301/1301 passing + 1 skipped (ffmpeg-real),
+37 vs. C7.3. mypy --strict clean auf 105 source files,
ruff clean (bis auf pre-existing E731 in `delete_service.py`).

**Open**: C7.5 — Cache-Sweeper (TTL 7 Tage + 1 GB Cap).
Real-whisper-e2e-Test (ffmpeg-Silence → whisper → empty_transcript)
ist bewusst nicht gebaut, weil er ohne installed whisper-cli
auf der Entwicklungsmaschine eh skippt und die FakeTranscriber-
basierten Tests dieselbe Logik abdecken.

#### C7.3 — Audio-Pipeline (Download + ffmpeg) ✅

Voice-Messages (OGG/Opus, MP3, MP4, WAV, WebM) werden jetzt
durch den Download/Validate/Cache-Pfad gezogen und mit ffmpeg
auf 16 kHz Mono-WAV normalisiert. Damit steht die Stage-1-
Infrastruktur — die eigentliche Transkription landet in C7.4.
Der Webhook-Dispatcher routet AUDIO weiterhin auf
`process_unsupported` (und ist aus Sicht des Users noch
„nicht unterstützt"), bis Whisper in C7.4 das Prompt liefert.

- **Port**: `AudioConverter`-Protocol + `AudioConversionError`
  (`whatsbot/ports/audio_converter.py`). Kontrakt:
  `to_wav_16k_mono(input_path, output_path)`.
- **Adapter**: `FfmpegAudioConverter`
  (`whatsbot/adapters/ffmpeg_audio_converter.py`) — shell-freier
  `subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
  "-y", "-i", ..., "-ar", "16000", "-ac", "1", "-f", "wav", ...])`.
  30 s Timeout. stderr-Tail (letzte 500 Zeichen) im Error-Message,
  auto-`mkdir -p` des Output-Parent-Dir, Sanity-Check auf
  exit-0-with-empty-output.
- **Application**: `MediaService.process_audio_to_wav(media_id,
  mime, sender)` — Stage-1-Pipeline:
  1. Guard auf aktives Projekt.
  2. Download via MediaDownloader.
  3. Validate MIME (audio/*-Allow-List), Size (25 MB Cap
     per Spec §16), Magic-Bytes
     (`domain.magic_bytes.looks_like_audio`).
  4. Cache die Source-Blob unter Original-Suffix.
  5. Konvertiere via AudioConverter → WAV im selben Cache-Dir.
  6. Return `MediaOutcome(kind="audio_staged", wav_path=...)`.
  Jeder Fehlermodus produziert einen eigenen `kind`-String
  (no_active_project, download_failed, validation_failed,
  conversion_failed) damit C7.4 sauber verzweigen kann.
- **Wiring**: `main.py` baut `FfmpegAudioConverter` default in
  prod/dev, injection via `create_app(audio_converter=...)` in
  Tests. MediaService akzeptiert den Converter als optionalen
  ctor-Param — fehlt er, fällt `process_audio_to_wav` fast-fail
  auf `conversion_failed` zurück (ohne Download), damit ein
  misskonfigurierter Bot nicht CPU verbrennt.
- **Tests**: 10 unit (`test_media_service_audio.py`: happy path,
  no_active_project, download failure, disallowed MIME, 26 MB
  oversize, magic-bytes mismatch, ffmpeg-failure containment +
  cached source überlebt, missing converter wiring, Graph-MIME
  vs. Hint-Präferenz) + 9 unit (`test_ffmpeg_audio_converter.py`
  mit Fake-ffmpeg auf PATH: happy, auto-mkdir, missing input,
  non-zero exit mit stderr-tail, exit-0-but-empty, empty
  written file, missing binary, timeout, argv-Schema) + 1
  integration (`test_ffmpeg_real.py`: echter ffmpeg, OGG/Opus
  Silence → 16 kHz mono WAV; RIFF/WAVE-Header und PCM/mono/16k-
  Felder verifiziert; skipped wenn ffmpeg fehlt).

**Tests**: 1264/1264 passing + 1 skipped (real-ffmpeg,
+19 vs. C7.2), mypy --strict clean auf 102 source files,
ruff clean (bis auf pre-existing E731 in `delete_service.py`).

#### C7.2 — PDF-Pipeline ✅

PDFs landen jetzt genauso zuverlässig bei Claude wie Bilder.
`MediaService.process_pdf` nutzt dasselbe download → validate →
cache → send-Skelett wie `process_image`, mit folgenden
PDF-spezifischen Unterschieden:

- MIME-Allow-List: `application/pdf`.
- Size-Cap: 20 MB (Spec §16).
- Magic-bytes-Gate: `%PDF-`-Prefix via
  `domain.magic_bytes.looks_like_pdf`.
- Cache-Suffix: `.pdf` via `suffix_for_mime(DOCUMENT, ...)`.
- Prompt-Form: `lies <path>: <caption>` (statt `analysiere ...`
  für Bilder). Ohne Caption: reines `lies <path>`.
- Reply-Label: `PDF an '<project>' gesendet.` (statt `Bild ...`).

Infrastruktur (Downloader, Cache, Webhook-Dispatch für
`MediaKind.DOCUMENT`, Kind-Parsing in `iter_media_messages` via
`message["document"]`) wurde defensiv bereits in C7.1 gebaut, so
dass C7.2 nur Tests + Cleanup war.

- **Tests**: 7 neue unit (`test_media_service_pdf.py`: happy path,
  without caption, no_active_project, wrong MIME, 21 MB oversize,
  magic-bytes mismatch — JPEG bytes + application/pdf MIME,
  download failure). 2 neue e2e (`test_media_e2e.py`: real tmux +
  signed /webhook with document payload — happy path + 21 MB
  oversize-reject).
- **Cleanup**: Datei `test_media_image_e2e.py` → `test_media_e2e.py`
  umbenannt (enthält jetzt image + pdf + video e2e).
  Docstring-Hinweis "Placeholder — C7.2 wires this up" in
  `MediaService.process_pdf` entfernt. File-Header-Docstring um
  C7.2-Status aktualisiert.

**Tests**: 1245/1245 passing (+9 vs. C7.1-Baseline),
mypy --strict clean auf 100 source files, ruff clean (bis auf
pre-existing E731 in `delete_service.py`).

#### C7.1 — Image-Pipeline + Reject-Pfade ✅

Inbound WhatsApp-Images, -PDFs (Gerüst) und unsupportete Kinds
(Video/Location/Sticker/Contact) fließen durchs MetaWebhook in den
neuen `MediaService`. Bilder werden per Meta Graph API (zwei
Schritte + Bearer) gezogen, magic-bytes- + MIME- + Size-validiert,
atomar in `~/Library/Caches/whatsbot/media/` abgelegt und als
`analysiere <path>: <caption>`-Prompt an das aktive Projekt
weitergereicht. Unsupportete Kinds bekommen freundliche
Reject-Replies (Spec §9 — kein silent drop mehr).

- **Domain (pure)**: `domain/media.py` (MediaKind, Size/MIME-
  Allow-Lists, MediaValidationError, classify_meta_kind,
  suffix_for_mime), `domain/magic_bytes.py` (looks_like_image
  für JPEG/PNG/WEBP/HEIC/GIF, looks_like_pdf, looks_like_audio
  für OGG/MP3/MP4/WAV/WebM).
- **Ports**: `MediaDownloader`-Protocol (DownloadedMedia +
  MediaDownloadError), `MediaCache`-Protocol (CachedItem +
  store/path_for/list_all/secure_delete).
- **Adapter**: `MetaMediaDownloader` (httpx, tenacity-Retries,
  5s connect + 30s read, 4xx = permanent, 5xx = retry),
  `FileMediaCache` (atomic `<name>.tmp` + `os.replace`,
  secure_delete = zeros + fsync + unlink, media_id-Sanitize
  gegen Path-Traversal).
- **Application**: `MediaService` mit `process_image`,
  `process_pdf` (Stub für C7.2) und `process_unsupported`.
  Strukturierte `MediaOutcome` mit kind-Kategorie (sent,
  no_active_project, validation_failed, download_failed,
  unsupported).
- **HTTP**: `iter_media_messages` + `MediaMessage`-Dataclass
  analog zu `iter_text_messages`, neuer Dispatch-Loop im
  POST-Handler der Video/Location/Sticker/Contact/Unknown
  mit friendly-reject replies beantwortet.
- **Wiring**: `MediaService` wird in `main.py` nur gebaut wenn
  `tmux + session_service + access_token` da sind; fehlende
  Voraussetzungen fallen auf "Medien gerade nicht angenommen"
  (statisch) bzw. static reject replies zurück. `Settings`
  bekommt `media_cache_dir` (default
  `~/Library/Caches/whatsbot/media/`).
- **Tests**: 73 unit (domain), 10 unit
  (MetaMediaDownloader mit httpx-MockTransport), 14 unit
  (FileMediaCache), 15 unit (MediaService), 14 unit
  (iter_media_messages), 2 e2e (real tmux + signed /webhook:
  image happy path, video reject). 1 pre-existing
  integration-test aktualisiert (C7.1 ändert bewusst das
  silent-drop-Verhalten für non-text messages auf
  friendly reply pro Spec §9).

**Tests**: 1236/1236 passing (+132 vs. Phase-6-Baseline),
mypy --strict clean auf 100 source files (+7), ruff clean
(bis auf pre-existing E731 in `delete_service.py`).

**Open Debt (für spätere Checkpoints)**:
- `MediaService.process_pdf` Stub → C7.2 real e2e + edge tests.
- Audio/Voice-Pipeline → C7.3 (ffmpeg) + C7.4 (whisper).
- Cache-Sweeper (TTL 7d + 1 GB-Cap) → C7.5.
- httpx explicit in `requirements.txt` aufgenommen (war
  bereits transitive via FastAPI, jetzt gepinnt).

### Phase 6 — Kill-Switch + Watchdog + Sleep-Handling ✅ (complete)

Alle Kern-Checkpoints grün (C6.1–C6.6) plus optional C6.5 Sleep-
Awareness. Spec §7 Notfall-Infrastruktur steht End-to-End: vier
Eskalationsstufen vom Handy aus (`/stop` → `/kill` → `/panic` →
`/unlock`), Heartbeat-Pumper + Watchdog-LaunchAgent als
unabhängiger Backstop, Lockdown-Filter blockt alle Commands
außer `/unlock` während engaged, StartupRecovery respektiert
Lockdown.

- ✅ C6.1 — `/stop` (Ctrl+C) + `/kill` (tmux kill-session + lock release)
- ✅ C6.2 — `/panic` Vollkatastrophe in <2s mit 6-Step-Playbook
- ✅ C6.3 — YOLO→Normal bei Panic (mode_events `panic_reset`)
- ✅ C6.4 — Heartbeat-Pumper + Watchdog-LaunchAgent
- ✅ C6.5 — Watchdog Sleep-Awareness (PID-Liveness + Boot-Grace)
- ✅ C6.6 — `/unlock <PIN>` + Lockdown-Filter + StartupRecovery-Skip

**Tests**: 1104/1104 passing (912 → 1104 = +192 für Phase 6),
mypy --strict clean (93 source files), ruff clean.

#### C6.6 — `/unlock <PIN>` + Lockdown-Filter ✅

- **`whatsbot/application/unlock_service.py`**: PIN-Verify via
  `hmac.compare_digest` gegen Keychain-`panic-pin` +
  `lockdown_service.disengage()`. Spiegelt das ForceService-
  Pattern. PIN-Check läuft AUCH wenn Lockdown nicht engaged ist
  — kein info-leak via Timing.
- **`CommandHandler` Lockdown-Filter** ganz oben in `handle()`:
  während Lockdown engaged jeder Command außer `/unlock <PIN>`,
  `/help`, `/ping`, `/status` wird mit `🔒 Bot ist im Lockdown.
  /unlock <PIN> zum Aufheben.` geblockt. Auch nackte Prompts
  (das gefährlichste Surface bei Handy-Diebstahl) sind geblockt.
- **`CommandHandler._handle_unlock`**: 5 Reply-Pfade — korrekte
  PIN+engaged → `🔓 Lockdown aufgehoben.`, PIN+nicht-engaged
  → `🔓 Bot war nicht im Lockdown.`, falsche PIN → `⚠️ Falsche
  PIN.`, missing keychain → `⚠️ Panic-PIN ist im Keychain
  nicht gesetzt.`, bare `/unlock` → `Verwendung: /unlock <PIN>`.
- **`StartupRecovery`** akzeptiert optional `lockdown_service`-
  Param. Wenn engaged: skip YOLO-Reset + skip session-restore,
  return `RecoveryReport(skipped_for_lockdown=True)`. Bot bleibt
  up um `/unlock` zu beantworten, relauncht aber keine Claudes.
- Tests: 6 unit `test_unlock_service.py`, 13 unit
  `test_unlock_command.py` (Filter blockt /ls /new /p bare-prompts,
  lässt /unlock /help /ping /status durch), 3 unit
  `test_startup_recovery_lockdown.py`, 1 e2e `test_unlock_e2e.py`
  (real tmux + signed /webhook → /panic → blockierte Replies →
  wrong PIN → right PIN → /ls funktioniert wieder).

#### C6.5 — Watchdog Sleep-Awareness ✅

Zwei einfache Heuristiken im `bin/watchdog.sh` ohne pmset-log-
Parsing:

- **PID-Liveness-Grace**: Heartbeat enthält die Bot-PID (C6.4-
  Format). Wenn die PID via `kill -0 <pid>` lebt, war die
  Heartbeat-Staleness wahrscheinlich Mac-Sleep-Artefakt (Bot
  war suspended, nicht tot). Watchdog skippt engage und
  loggt `watchdog_grace_pid_alive`.
- **Boot-Grace**: System-Uptime via portable `sysctl
  -n kern.boottime` (macOS) / `/proc/uptime` (Linux) /
  `WHATSBOT_WATCHDOG_FAKE_UPTIME` (tests). Bei missing-
  heartbeat + Uptime <300 s skippt der Watchdog (LaunchAgent
  könnte den Bot noch hochfahren). Loggt
  `watchdog_grace_recent_boot`.

Beide Pfade fallen sauber durch zu engage wenn die Heuristik
nicht greift (PID dead → engage, Uptime >grace + missing
heartbeat → engage). LaunchAgent-Plist exposed neue Env-Var
`WHATSBOT_WATCHDOG_BOOT_GRACE_SECONDS=300`.

5 neue Integration-Tests in `test_watchdog_script.py`:
PID-alive grace mit own-PID, dead-PID engaged, boot-grace bei
fake_uptime=10, no boot-grace bei fake_uptime=99999, Backwards-
compat ohne pid= line in heartbeat.

Bonus-Fix in watchdog.sh: pipeline-failures unter `set -euo
pipefail` mit `|| true` abgesichert (grep no-match returns 1,
würde sonst den ganzen Skript abbrechen).

#### C6.4 — Heartbeat-Pumper + Watchdog-LaunchAgent ✅

Spec §7 dead-man's-switch. Bot schreibt alle 30 s ein touch-File
nach `/tmp/whatsbot-heartbeat`. Ein separater LaunchAgent
(Watchdog) prüft alle 30 s die mtime — ist sie >120 s alt, killt
der Watchdog alle wb-* tmux-Sessions, feuert pkill -9 -f
safe-claude als Backstop, schreibt den panic-Marker damit der
Bot beim Restart in Lockdown bleibt, und feuert eine macOS-
Notification.

- **`domain/heartbeat.py`** (pure): Konstanten + `is_heartbeat_stale`
  + `format_heartbeat_payload` (header + version + pid + ISO ts).
- **`ports/heartbeat_writer.py`** + **`adapters/file_heartbeat_writer.py`**:
  atomic write (tmp + os.replace), parent-dir auto-create.
- **`application/heartbeat_pumper.py`**: async background loop.
  Erste Schreibung sofort in start() (Watchdog sieht das File
  bei t=0). File-IO über asyncio.to_thread damit der event loop
  nie blockiert. Schreibfehler werden geloggt aber brechen die
  Loop nie. stop() cancelt sauber + löscht das File.
- **`main.create_app(heartbeat_writer=..., enable_heartbeat=...)`**
  + FastAPI `lifespan`-Context: in PROD/DEV automatisch on,
  in TEST opt-in.
- **`bin/watchdog.sh`** — bash-only (kein Python — funktioniert
  auch bei kaputtem venv): mtime via portable stat -f %m /
  stat -c %Y, nur wb-* tmux-Sessions, narrow `safe-claude`-
  pattern für pkill, JSON-strukturiertes Logging.
- **`launchd/com.DOMAIN.whatsbot.watchdog.plist.template`**:
  RunAtLoad + StartInterval=30, KeepAlive=false (jeder Tick
  ist ein short-lived shell — robuster als long-running loop).
- **`bin/render-launchd.sh`** rolled jetzt drei Plists (Bot +
  Backup + Watchdog).

Tests: 33 (8 heartbeat domain, 8 file writer, 9 pumper async,
8 watchdog script, 1 lifespan integration).

#### C6.2 / C6.3 — `/panic` + YOLO-Reset + Lockdown ✅

Sechs-stufiger /panic-Flow in `PanicService`, in genau dieser
Reihenfolge:

1. **Lockdown engage** (DB row + Touch-File `/tmp/whatsbot-PANIC`).
   Muss zuerst, damit eine race-condition-Webhook nichts wieder
   hochfährt was wir gerade abreißen.
2. **wb-* tmux-Sessions** enumerieren + tmux kill-session pro
   Session. tmux SIGHUP cascade triggert Claude graceful exit.
3. **`pkill -9 -f safe-claude`** als Backstop für stuck Claudes.
   Pattern bewusst eng (safe-claude statt claude) — keine
   fremden Claude-Instanzen werden mit-getötet (Spec §21
   Phase 6 Abbruch-Kriterium).
4. **YOLO → Normal** pro Projekt + `mode_events.event='panic_reset'`
   pro YOLO-Projekt (Spec §6 Invariante).
5. **Locks release** pro Projekt — bot-state ist weg, Locks
   wären sonst irreführend.
6. **macOS-Notification** mit Sound (osascript), no-op auf Linux.

Architektur-Bricks:
- `domain/lockdown.py` (pure): LockdownState + engage/disengaged.
  Idempotent — first-trigger-Metadata bleibt erhalten (Forensik).
- `application/lockdown_service.py`: persistiert in
  app_state.lockdown als JSON + Touch-File `/tmp/whatsbot-PANIC`.
  Tolerant gegen JSON-Garble + Partial-Rows.
- `ports/process_killer.py` + `adapters/subprocess_process_killer.py`:
  pkill -9 -f wrapper, exit 1 (no-match) ist success.
- `ports/notification_sender.py` + `adapters/osascript_notifier.py`:
  display notification via osascript, no-op fallback.
- `application/panic_service.py`: orchestriert alle 6 Schritte,
  klobige Failures (notifier, killer, audit) werden geloggt
  aber brechen die anderen Schritte nie. Idempotent.

CommandHandler: `/panic` ohne PIN per Spec §5 (low friction in
emergency). Reply mit Counts + Lockdown-Hinweis + /unlock-Tipp.
Settings: neue Felder `panic_marker_path` + `heartbeat_path`
(default `/tmp/whatsbot-PANIC`, `/tmp/whatsbot-heartbeat`).

Tests: 29 (5 lockdown domain, 10 LockdownService, 9 PanicService,
4 panic command, 1 e2e).

#### C6.1 — `/stop` + `/kill` ✅

Zwei per-Projekt emergency-control Verben. `KillService.stop`
(Soft-Cancel via `tmux interrupt`, Session bleibt am Leben) +
`KillService.kill` (Hard-Kill via `tmux kill_session` +
`lock_service.release`). Lock-Release-Failures werden geloggt
aber nie hochpropagiert. claude_sessions-Row bleibt bei /kill
— Resume auf next /p ist intentional.

- **`TmuxController.interrupt(name)`**-Protocol-Methode neu
  (sendet `C-c` als tmux key event, kein Enter, kein -l-Literal).
  Adapter + alle 5 FakeTmux-Varianten in den Tests aktualisiert.
- **`application/kill_service.py`** mit `stop(name)` + `kill(name)`.
- **`CommandHandler`** routet `/stop`, `/stop <name>`, `/kill`,
  `/kill <name>`. Helper `_resolve_target_project` defaultet
  auf aktives Projekt, validiert Name. Replies:
  `🛑 Ctrl+C an '...' geschickt.` /
  `🪓 '...' tmux-Session beendet · Lock freigegeben.`.

Tests: 22 (9 KillService unit, 11 CommandHandler unit, 2 e2e).

### Phase 5 — Input-Lock + Multi-Session ✅ (complete)

Alle 5 Checkpoints grün. Spec §7 Soft-Preemption + `/release` +
PIN-gated `/force` + tmux-Status-Bar Lock-Owner-Badge stehen.
Bot und lokales Terminal können sicher parallel an derselben
Claude-Session arbeiten — lokales Terminal hat Vorrang, der Bot
respektiert es ohne Lock-stehlen, und der User hat einen
expliziten Override per WhatsApp.

- ✅ C5.1 — Lock-Domain + SQLite-Repository + LockService (a/b/c)
- ✅ C5.2 — Wiring (TranscriptIngest + SessionService.send_prompt
  + CommandHandler + `/release`)
- ✅ C5.3 — End-to-End Integration-Smoke via `/webhook`
- ✅ C5.4 — `/force <name> <PIN> <prompt>` PIN-gated Override
- ✅ C5.5 — tmux-Status-Bar Lock-Owner-Badge + Live-Repaint

**Tests**: 993/993 passing, mypy --strict clean (80 source files),
ruff clean auf allen Phase-5-Files.

#### C5.5 — tmux-Status-Bar Lock-Owner-Badge ✅

- **`whatsbot/domain/locks.py`** — pure `lock_owner_badge(owner)`:
  BOT → `🤖 BOT`, LOCAL → `👤 LOCAL`, FREE/None → `— FREE`.
- **`SessionService._paint_status_bar`** liest jetzt den Owner via
  `_locks.current(project)` und rendert
  `{mode_badge} · {owner_badge} [tmux_name]`, z.B.
  `🟢 NORMAL · 🤖 BOT [wb-alpha]`.
- **`SessionService.repaint_status_bar(project)`** — neue public
  API für Live-Updates. No-op bei totem tmux oder fehlendem
  Project, swallowt Exceptions (rein kosmetisch — darf nie eine
  Lock-Op fail-closen).
- **`LockService.__init__(on_owner_change=...)`-Callback** — feuert
  nur bei tatsächlichen Owner-*Wechseln*, nicht bei no-op-Refreshes
  (Bot re-acquires, repeated local-input pulses). Pro Operation:
  - `acquire_for_bot` → fire bei erst-grant
  - `force_bot` → fire bei flip from non-BOT
  - `note_local_input` → fire bei flip from non-LOCAL
  - `release` → fire wenn row existierte
  - `sweep_expired` → fire pro reaped project
  Callback-Failures werden geloggt, brechen aber die Lock-Op nie.
- **`whatsbot/main.py`** verdrahtet `LockService.on_owner_change` →
  `SessionService.repaint_status_bar` via Forward-Ref-Liste
  (`session_service_status_ref`) — gleiche Pattern wie für
  auto-compact, weil SessionService nach LockService gebaut wird.
- Test-Regression: `test_session_service.py` Label-Assertion von
  `🟢 NORMAL [wb-alpha]` auf `🟢 NORMAL · — FREE [wb-alpha]`
  nachgezogen.
- Tests (17 neu, 993 total):
  - `test_lock_status_badge` (17): 4 pure-helper-Tests, 5 paint-
    Layer-Tests (BOT/LOCAL/FREE-Badge, repaint-no-op-Pfade bei
    totem tmux + missing project), 8 callback-Tests (alle
    Operationen × no-op-vs-flip + Callback-Failure-Containment).

#### C5.4 — `/force <name> <PIN> <prompt>` PIN-gated Lock-Override ✅

Power-Tool für den Fall, dass der lokale Lock stale ist (User
ist weg vom Mac aber das Lock-Row liegt noch unter 60s — vor
Auto-Release). Statt zu warten kann der User per WhatsApp den
Lock mit der `panic-pin` aus dem Keychain übernehmen.

- **`whatsbot/application/force_service.py`** —
  `ForceService.force(name, pin)`: validate name → check project
  exists (FK-safety, sonst sqlite IntegrityError) → PIN-Check via
  `hmac.compare_digest` gegen Keychain-`panic-pin` →
  `lock_service.force_bot(name)`. Wiederverwendet
  `InvalidPinError` und `PanicPinNotConfiguredError` aus
  `delete_service` — beide Commands keyen auf denselben
  Keychain-Eintrag, gleiche Semantik.
- **`CommandHandler._handle_force(args)`** — parse'd 3 Tokens via
  `split(maxsplit=2)`, sodass der Prompt Leerzeichen + sogar
  weitere PIN-artige Strings enthalten darf. Bei PIN-Match →
  `force_service.force` + `session_service.send_prompt`. Reply:
  `🔓 Lock fuer 'name' uebernommen.\n📨 an name: <preview>`.
  Bei PIN-Miss → `⚠️ Falsche PIN`, Lock bleibt LOCAL, kein Prompt
  zugestellt.
- **Bonus-Fix**: `_dispatch_prompt`-Hint korrigiert auf die echte
  Syntax `/force <name> <PIN> <prompt>` (war vorher misleading
  ohne PIN). Mit Regression-Test.
- **`whatsbot/main.py`** baut ForceService nur wenn lock_service
  und session_service vorhanden sind; wired ins
  CommandHandler-`force_service`-Param.
- Tests (20 neu, 976 total):
  - `test_force_service` (7): PIN-Pfade, Project-FK,
    Constant-Time-Compare, Lock unverändert bei Mismatch, missing
    Panic-PIN.
  - `test_force_command` (12): Parsing-Edges (Whitespace, Multi-
    Token-Prompts, fehlende Args), no-config-Guard,
    Hint-Korrektur-Regression, Idempotenz ohne Vorlock.
  - `test_lock_e2e::test_force_overrides_local_lock_with_pin`
    (1): real tmux, /webhook, signed payload, wrong-PIN → keep
    LOCAL, right-PIN → flip to BOT + 📨.

#### C5.3 — Lock-Soft-Preemption End-to-End via `/webhook` ✅

- **`tests/integration/test_lock_e2e.py`** — 2 Tests gegen einen
  full-wired TestClient mit echtem `SubprocessTmuxController` und
  `safe-claude=/bin/true`:
  - Preseed local lock → `/p alpha hi` → 🔒-Reply, Lock bleibt
    LOCAL, kein Prompt landet im tmux-Pane.
  - Preseed local lock → `/release alpha` → Lock weg →
    `/p alpha ready now` läuft durch → 📨-Ack.

#### C5.2 — LockService-Wiring ✅

Hängt die LockService-Instanz an die drei Hot-Path-Komponenten
und führt die `/release`-Commands ein.

- **`TranscriptIngest`** — neuer Konstruktor-Param
  `on_local_input: Callable[[str], None] | None`. Feuert aus
  `_handle_user`, wenn ein non-ZWSP + non-empty user-turn landet
  (also der Mensch direkt im tmux-Pane getippt hat).
  Tool-Result-Events und Bot-prefixed-Turns triggern NICHT.
- **`SessionService.__init__(lock_service=...)`** — `send_prompt`
  ruft `lock_service.acquire_for_bot(project)` vor `tmux.send_text`.
  Bei `LocalTerminalHoldsLockError` propagiert die Exception
  hoch, der Prompt landet nicht im Pane.
- **`CommandHandler._dispatch_prompt`** fängt
  `LocalTerminalHoldsLockError` und rendert
  `🔒 Terminal aktiv auf '<name>'. /force <name> <PIN> <prompt>
  oder /release zum Freigeben`.
- **Neue Commands `/release` + `/release <name>`** — setzt Lock
  auf FREE für aktives oder benanntes Projekt. Idempotent
  (nothing-to-release liefert eine friendly confirmation).
- **`whatsbot/main.py`** verdrahtet *eine* LockService-Instanz
  in TranscriptIngest, SessionService und CommandHandler — der
  Sweeper-Hook ist vorbereitet (sweep_expired existiert),
  Auto-Sweep per LaunchAgent-Heartbeat in einer späteren Phase.
- Tests: 3 neue Wiring-Tests (`test_lock_wiring.py`) +
  Anpassungen in test_command_handler / test_session_service.

#### C5.1 — Lock-Domain + Repository + Service (a/b/c) ✅

In drei atomaren Sub-Commits, Bottom-up.

- **C5.1a `domain/locks.py`** (pure):
  - `LockOwner` StrEnum (`free` / `bot` / `local`) — matcht den
    `CHECK(owner IN (...))`-Constraint aus `session_locks`.
  - `SessionLock`-Dataclass mit `project_name`, `owner`,
    `acquired_at`, `last_activity_at`.
  - `evaluate_bot_attempt(current, *, now, timeout_seconds,
    project_name) → (AcquireOutcome, SessionLock)` — pure
    State-Transition. Free/Bot → grant; Local idle past timeout
    → auto-release-then-grant; Local fresh → DENIED_LOCAL_HELD.
  - `mark_local_input(current, *, now, project_name)` — pure
    Local-Pre-Emption.
  - `is_expired(lock, *, now, timeout_seconds)` für den Sweeper.
  - `LOCK_TIMEOUT_SECONDS = 60` (Spec §7).
  - 14 unit tests inkl. aller 9 Owner×Event-Übergänge plus
    Timeout-Edge-Cases.
- **C5.1b Port + SQLite-Adapter**:
  - `whatsbot/ports/session_lock_repository.py` — Protocol
    (`get` / `upsert` / `delete` / `list_all`).
  - `whatsbot/adapters/sqlite_session_lock_repository.py` —
    gegen die existierende `session_locks`-Tabelle mit
    Round-Trip-Tests (8) inkl. CHECK-Constraint-Regression.
- **C5.1c `application/lock_service.py`**:
  - `acquire_for_bot(project)` raise't `LocalTerminalHoldsLockError`
    bei DENIED_LOCAL_HELD; sonst persistiert + returnt
    `AcquireResult(outcome, lock)`.
  - `note_local_input(project)` — Local-Pre-Emption.
  - `release(project)` — Boolean (existed-or-not).
  - `force_bot(project)` — Unconditional (Basis für `/force`).
  - `sweep_expired()` — räumt idle-LOCAL ab.
  - `current(project)` — read-only Lookup für Status-Bar.
  - Clock-injectable für Tests. 16 unit tests.

### Phase 3 — Security-Core ✅ (complete)

Alle 6 Checkpoints grün, Phase 3 komplett gebaut und verifiziert.

- ✅ C3.1 — Hook-Script + Shared-Secret-IPC-Endpoint
- ✅ C3.2 — Deny-Patterns + PIN-Rückfrage (End-to-End + 17 Fixtures)
- ✅ C3.3 — Redaction-Pipeline 4 Stages + globaler Sender-Decorator
- ✅ C3.4 — Input-Sanitization + Audit-Log
- ✅ C3.5 — Output-Size-Warning + `/send` / `/discard` / `/save`
- ✅ C3.6 — Fail-closed Hook-Integration-Smoke

**Tests**: 689/689 passing, mypy --strict clean, ruff clean.
**Offene Schuld**: Write-Hook hat noch den Stub-Pfad (`classify_write` = allow).
Path-Rules-Policy (Spec §12 Layer 3) wird in Phase 4 oder als C3.7-Nachzug
gebaut — C3-Checkpoints sind sonst alle geliefert.

#### C3.6 — Fail-closed Hook-Integration-Smoke ✅

Schließt die Fail-Closed-Matrix für die Pre-Tool-Hook. Die bereits
vorhandenen Tests (unreachable, wrong secret, malformed stdin,
unknown tool) werden ergänzt um explizite Boundary-Smokes für die
server-seitigen Fehlerpfade.

- **`tests/integration/test_hook_fail_closed.py`** — pro Szenario
  eine eigene FastAPI-App auf einem Ephemeral-Port, `hooks/pre_tool.py`
  per Subprocess gefeuert, Exit-Code + Stderr asserted:
  - 500er mit JSON-Body der nicht dem Contract entspricht → Exit 2.
  - Response mit `text/plain`-Body → Exit 2 (malformed JSON).
  - Valid-JSON-aber-top-level-String → Exit 2 (non-object).
  - `hookSpecificOutput`-Block fehlt → Exit 2.
  - Unbekannter `permissionDecision`-Wert → Exit 2.
  - Endpoint schläft länger als `READ_TIMEOUT` → Exit 2 (~10s Laufzeit).
- 689/689 total, mypy + ruff clean.

#### C3.5 — Output-Size-Warning (>10KB) ✅

Spec §10 10KB-Schwelle + `/send` / `/discard` / `/save`-Dialog,
komplett integriert in den Outbound-Pfad. In drei atomaren Commits
(a: Domain, b: Port+Adapter, c: Service+Wiring).

- **`whatsbot/domain/output_guard.py`** — pure: `THRESHOLD_BYTES =
  10*1024` (UTF-8-Bytes, nicht Chars — Umlaute zählen richtig),
  `is_oversized`, `format_warning` (exakter Spec-§10-Dialog mit
  `⚠️ Claude will ~X KB senden ...`), `chunk_for_whatsapp` (3800-Char-
  Chunks mit `(i/n)`-Präfix für n>1, kein Präfix für Single-Chunk).
- **`whatsbot/domain/pending_outputs.py`** — `PendingOutput`-Dataclass
  gemäß Spec-§19-Schema, 24h-Default-Deadline (länger als der
  5-min-Hook-Fenster, weil User ggf. überlegen will).
- **`whatsbot/ports/pending_output_repository.py`** +
  **`whatsbot/adapters/sqlite_pending_output_repository.py`** — CRUD +
  `latest_open()` (LIFO: `ORDER BY created_at DESC`, Single-User-
  Szenario) + `delete_expired()`-Sweeper.
- **`whatsbot/application/output_service.py`** — Orchestrator:
  - `deliver(to, body, project_name)`: ≤10KB → direct-send; sonst
    Body nach `<data-dir>/outputs/<msg_id>.md`, Pending-Row, Warnung.
    FS-Fehler → Log + direct-send (lieber spill als drop).
  - `resolve_send(to)` → Body lesen, chunken, Chunks senden,
    Row+Datei löschen. `ResolveOutcome(kind="sent", chunks_sent=n)`.
  - `resolve_discard(to)` → Row + Datei weg. `kind="discarded"`.
  - `resolve_save(to)` → nur Row weg, Datei bleibt. `kind="saved"`.
  - `none` + `missing` für no-pending / weg-von-Platte-Edge-Cases.
- **`whatsbot/http/meta_webhook.py`** fängt `/send` · `/discard` ·
  `/save` *vor* dem Command-Router ab (gleiches Muster wie der
  PIN-Resolver). Jede sonstige Reply läuft jetzt durch
  `output_service.deliver` — zukünftige >10KB-Antworten triggern
  automatisch den Dialog.
- Tests (38 neu, 683 total):
  - `test_output_guard` (15) — Threshold-Edge-Cases, UTF-8-Byte-
    Counting, Chunker-Nummerierung + Content-Preservation.
  - `test_sqlite_pending_output_repository` (12) — CRUD, LIFO-
    Ordering, Duplicate-ID-Rejection, Expiry-Sweep.
  - `test_output_service` (11) — alle Pfade inkl. FS-Write-Failure-
    Fallback + Missing-File-nach-`/send`.
  - `test_output_dialog` (6) — echter TestClient über
    `/webhook`, 3-Chunk-Send, Discard, Save, no-pending-Pfade.

#### C3.4 — Input-Sanitization + Audit-Log ✅

Spec-§9-Telegraphen-Detection + Normal-Mode-Wrap. Phase 4 wird die
wrapped Variante an Claude weiterreichen; heute nur Detection +
Audit-Log, damit eine Forensik-Spur entsteht.

- **`whatsbot/domain/injection.py`** — pure:
  - `detect_triggers(text)`: word-boundary, case-insensitive Regex-
    Scan auf die 5 Spec-§9-Phrasen
    (`ignore previous`, `disregard`, `system:`, `you are now`,
    `your new task`). Gibt Tupel der getriggerten Labels zurück.
  - `sanitize(text, *, mode)`: `SanitizeResult`. Trigger-Liste immer
    populiert. Wrap nur in Normal-Mode — Strict blockt eh über
    `dontAsk`, YOLO ist explizites "I accept the risk".
- **`whatsbot/http/meta_webhook.py`** — jeder whitelisted Inbound
  läuft durch `detect_triggers`. Bei Hits feuert ein strukturiertes
  `injection_suspected`-WARN-Event mit `triggers`, `text_len` +
  bereits gebundenen Correlation-Fields (`msg_id`, `wa_msg_id`,
  `sender`). Command-Dispatch läuft danach weiter — wir auditten,
  aber droppen nichts still.
- Tests (33 neu, 639 total):
  - `test_injection` (30) — jeder Trigger × jeder Mode, Multi-Hit-
    Reihenfolge, False-Positive-Kontrollen
    (`disregarded by the compiler`, `system is online`, etc.).
  - `test_injection_audit` (3) — End-to-End-`/webhook`-POST, JSON-
    Log-Parsing aus stderr (structlog schreibt direkt, caplog sieht
    es nicht), Happy-Path und Clean-Path.

#### C3.3 — Redaction-Pipeline (4 Stages) ✅

Spec §10 Redaction komplett durch. In zwei Commits (a: Domain + Tests,
b: Decorator + global wiring).

- **`whatsbot/domain/redaction.py`** — 4-stage pure Pipeline:
  - **Stage 1** known keys: AWS (`AKIA`), GitHub
    (`ghp_`/`ghs_`/`github_pat_`), OpenAI (`sk-`/`sk-proj-`), Stripe
    (`sk_live_`/`rk_live_`), JWT, Bearer.
  - **Stage 2** struktureller Patterns: PEM-Blocks, SSH-Pubkeys,
    DB-URLs mit Credentials, `KEY=VALUE` mit sensitiven Keys
    (incl. JSON-Style `"password": "..."`).
  - **Stage 3** Entropy: ≥40-Char-Tokens mit Shannon > 4.5 UND
    mindestens einer Ziffer (letzterer Guard filtert camelCase-
    False-Positives), URLs übersprungen.
  - **Stage 4** Sensitive-Path-Line-Content (~/.ssh, ~/.aws, etc.):
    lange Tokens auf Zeilen, die einen sensitiven Pfad erwähnen,
    als `<REDACTED:path-content>`.
  - Labels `<REDACTED:aws-key>` / `<REDACTED:env:password>` etc. —
    Debugging bleibt möglich ohne Secret-Leak.
  - CLI: `python -m whatsbot.domain.redaction` (stdin-Smoke).
- **`whatsbot/adapters/redacting_sender.py`** — Decorator um
  `MessageSender`, loggt Hit-Labels bei Anwendung. Wrappt den
  injizierten Sender in `main.create_app` — jeder Outbound-Pfad
  (Command-Reply, Hook-Confirmation-Prompt, PIN-Ack, zukünftige
  kill/stop-Notifications) bekommt automatisch Redaction.
- Tests (44 neu, 606 total):
  - `test_redaction` (37) — jede Stage, jeder Secret-Typ (≥10),
    False-Positive-Controls auf normaler Prosa, URLs, Hex-Hashes,
    camelCase-Identifier, Pipeline-Idempotenz auf bereits-
    redacted Output.
  - `test_redacting_sender` (5) — Passthrough, AWS-Key gescrubbt,
    env:password gescrubbt, Cross-Call-Isolation.
  - `test_redaction_wired` (2) — End-to-End via `/webhook`-POST
    mit `/new AKIA...` (Command-Handler echot den invaliden Namen
    → `<REDACTED:aws-key>` landet beim RecordingSender).

#### C3.2 — Deny-Patterns + PIN-Rückfrage (End-to-End) ✅

Die Security-Policy-Keule. In vier atomaren Commits
(a: Deny-Patterns+Matrix, b: Pending-Confirmation-Repo, c: Async-
Coordinator+Wiring, Smoke: 17 Fixtures + E2E).

- **`whatsbot/domain/deny_patterns.py`** — die 17 Patterns aus
  Spec §12 als Konstante + `match_bash_command(cmd) -> DenyMatch | None`.
  Matcher normalisiert Whitespace (mehrfach → einfach) und einfache
  Quotes (`rm -rf "/"` → `rm -rf /`) vor dem `fnmatch.fnmatchcase`-
  Vergleich. `bash -c '...'`-Wrappings und Command-Chaining via
  `&&` sind *nicht* abgedeckt — defense-in-depth-Layer, nicht
  Shell-Parser. 71 Unit-Tests.
- **`whatsbot/domain/hook_decisions.evaluate_bash(command, *, mode,
  allow_patterns)`** — Spec-§12-Decision-Matrix: Deny gewinnt
  immer (auch YOLO), Allow-Rule short-circuits AskUser, Mode-
  Fall-Through ist Normal→AskUser, Strict→Deny, YOLO→Allow. 13
  neue Tests für die Matrix, darunter "Allow-Rule schlägt Deny
  nicht" als explizite Invariante.
- **`whatsbot/domain/pending_confirmations.py`** +
  **`whatsbot/ports/pending_confirmation_repository.py`** +
  **`whatsbot/adapters/sqlite_pending_confirmation_repository.py`**
  — 5-min-Fenster, `ConfirmationKind` enum
  (`hook_bash` / `hook_write`), opaque JSON `payload`. 15 Unit-Tests
  gegen `:memory:`-SQLite.
- **`whatsbot/application/confirmation_coordinator.py`** —
  In-memory `asyncio.Future`-Registry + DB-Persistenz-Bridge.
  `ask_bash` öffnet eine Row, feuert ein WhatsApp-Prompt
  (best-effort), awaited die Future mit Timeout, collapsed zu
  Allow/Deny. `try_resolve(text, *, pin)` ist sync (kein await nötig)
  und matcht FIFO auf die älteste offene Row — PIN ist
  `hmac.compare_digest`, leerer PIN matcht nie (Fail-Safe).
- **`whatsbot/application/hook_service.py`** neu geschrieben:
  - `classify_bash` ist jetzt async, wrapt `evaluate_bash` + delegiert
    AskUser an den Coordinator.
  - Optional-Deps-Pattern: ohne Coordinator fällt der Service auf
    den C3.1-Stub zurück (allow-by-default), damit C3.1-Integration-
    Tests unverändert durchlaufen.
  - `_project_context(project)` failt-closed bei unbekanntem
    Projekt auf `Mode.NORMAL` + leere Allow-Liste.
- **`whatsbot/http/meta_webhook.py`** fängt PIN / "nein" *vor* dem
  Command-Router ab (sonst würde die PIN als unknown command
  interpretiert). `/webhook`-Router bekommt optionale Coordinator-
  Dep; bei Hit → Resolve + kurze Ack-Message.
- **`whatsbot/http/hook_endpoint.py`** wird async (`await
  service.classify_bash(...)`). Service-Exception → **explizit 200
  + deny** (Debugging-freundlicher als "keine Antwort").
- **`whatsbot/main.py`**: Coordinator + Default-Recipient (erste
  Nummer aus `allowed-senders`) global wired. `create_hook_app` kann
  optional den bestehenden `main_app` übernehmen und dessen
  Project-Repo + Allow-Rule-Repo + Coordinator wiederverwenden
  (Phase-4-Path; Phase-3-Stand-alone-Tests bleiben simpel).
- **`tests/fixtures/deny/*.json`** — 17 minimale JSON-Payloads, eine
  pro Pattern. Kann per `cat | hooks/pre_tool.py` manuell reproduziert
  werden.
- **`tests/integration/test_deny_patterns_e2e.py`** — 20 E2E-Tests:
  - Fixture-Integrity (17 Fixtures × 17 Patterns, keine Drift).
  - Jede Fixture gegen einen full-wired TestClient in **YOLO-Mode**.
    Deny muss auch dort feuern — das ist der Spec-§12-Fail-Closed-
    Beweis.
  - Negative Controls: `git status` in YOLO → `allow`; Quote-Tricks
    (`rm   -rf    "/"`) überleben HTTP + JSON-Roundtrip.
- Tests insgesamt: 562 → 562 total nach diesem Checkpoint (Numbering
  aus C3-Zwischenschritten — Gesamt-Sprung wurde inkrementell
  aufgebaut: 71 Deny-Pattern + 13 evaluate_bash + 15 Pending-
  Confirmation + 11 Coordinator + 13 HookService-Rewrite + 20 E2E).

#### C3.1 — Hook-Script + Shared-Secret-IPC ✅

Security-Infrastruktur steht, noch *ohne* echte Policy (allow-by-default
im `HookService`). Die Deny-Blacklist und der AskUser-Flow kommen in
C3.2 / C3.3, die APIs sind aber jetzt schon so aufgesetzt, dass nur
noch die Klassifikationslogik dazukommt — keine Re-Architektur nötig.

- **`whatsbot/domain/hook_decisions.py`**: `Verdict` (`ALLOW` / `DENY`
  / `ASK_USER`) als StrEnum, `HookDecision`-Dataclass mit
  Convenience-Konstruktoren `allow()`, `deny()`, `ask_user()`. `deny`
  und `ask_user` erzwingen eine nicht-leere `reason` — ein Deny ohne
  Grund wäre für den User am Handy nutzlos, und ein `ValueError` fängt
  das in Tests statt in Production.
- **`whatsbot/application/hook_service.py`**: `HookService.classify_bash`
  / `classify_write`. In C3.1 returnen beide `allow()` — aber die
  Logging-Struktur ist schon da (`hook_bash_classified` / `hook_write_classified`
  mit project, session_id, verdict), damit C3.2 nur die Entscheidung
  austauscht und die Log-Schema stabil bleibt. `_preview()`-Helper
  deckelt Command-Logs bei 200 Zeichen gegen Log-Flood.
- **`whatsbot/http/hook_endpoint.py`**: FastAPI-APIRouter mit
  `POST /hook/bash` + `POST /hook/write`.
  - **Shared-Secret**: Header `X-Whatsbot-Hook-Secret` wird bei
    Router-Build einmal aus Keychain (`hook-shared-secret`) geladen,
    pro Request mit `hmac.compare_digest` verglichen. Fehlende
    Keychain-Entry → jeder Request ist 401 (fail-closed by default,
    nie drift in allow).
  - **Decision-Serialisierung**: Spec-§7-Format
    `{"hookSpecificOutput": {"permissionDecision": "...", "permissionDecisionReason": "..."}}`.
    `ASK_USER` wird synchron auf `deny` collapsed — die echte
    async-PIN-Round-Trip-Logik kommt in C3.3.
  - **Fail-closed-Disziplin**: bad JSON → 400 + deny, fehlende
    Felder → 400 + deny, Service-Crash → **200 + deny** (expliziter
    Deny statt "keine Antwort", für Debugging besser).
  - Nur `127.0.0.1`-Bind enforced beim Uvicorn-Start (separater
    Listener auf `:8001`).
- **`whatsbot/main.py`**: neue Factory `create_hook_app()` für den
  zweiten Uvicorn-Listener. Teilt dieselbe Keychain, eigenes FastAPI-
  App-Objekt, eigener Health-Endpoint. launchd-Deploy (später in
  Phase 4-ish) startet sie via
  `uvicorn whatsbot.main:create_hook_app --factory --host 127.0.0.1 --port 8001`.
- **`hooks/_common.py`** + **`hooks/pre_tool.py`**:
  - Reines stdlib — importiert das `whatsbot`-Package nicht, damit der
    Hook auch aus einem anderen Venv oder einer kaputten Install-Pfad-
    Situation noch läuft.
  - Secret-Loading: `security find-generic-password -s whatsbot -a
    hook-shared-secret -w`; `WHATSBOT_HOOK_SECRET`-Env überschreibt
    für Tests.
  - HTTP-Client mit kurzen Timeouts (Connect 2s, Read 10s). Jede
    Fehlerart collapsed in `HookError` mit kurzer Begründung, die auf
    stderr landet.
  - Exit-Code-Contract:
    - Exit 0 + stdout-JSON allow → Claude lässt Tool laufen
    - Exit 0 + stdout-JSON deny → Claude refused mit Reason
    - Exit 2 + stderr-Reason → hook-intern gescheitert (unreachable,
      bad stdin, missing secret, unknown tool, …) — Claude behandelt
      es als Block
  - Read-only-Tools (`Read`/`Grep`/`Glob`) short-circuiten zu Exit 0
    **ohne** HTTP-Call — spart Latenz auf dem Hot-Path.
  - Unknown-Tool-Fallback ist fail-closed (Exit 2), damit neue
    Claude-Code-Tools in Zukunft nicht still durch die Hook rutschen.
- Tests (47 neu, 420 total):
  - `test_hook_decisions` (9): Verdict-Werte matchen Claude-Kontrakt,
    `deny`/`ask_user` erzwingen Reason, Frozen-Dataclass-Invariante.
  - `test_hook_service` (4): allow-by-default-Verhalten mit/ohne
    Projekt, huge-command-Preview.
  - `test_hook_common` (11): Env-Secret-Override, Security-CLI fehlt,
    Return-Code ≠ 0, empty secret, Response-Parsing mit malformed /
    non-object / missing-block / unknown-decision / missing-reason.
  - `test_hook_endpoint` (12): 401 bei fehlendem/falschem Secret,
    Server-ohne-Keychain denies all, happy-path allow, 400 bei
    malformed-JSON / missing-command, **service-crash → 200+deny**.
  - `test_hook_script` (11): Echter uvicorn auf Ephemeral-Port,
    Subprocess-Aufruf vom Hook-Script. Abgedeckt: happy-path Bash,
    Write mit `file_path`-Feld, Read-Bypass ohne HTTP, wrong-secret
    → stdout-deny, unreachable → Exit 2, empty/malformed stdin →
    Exit 2, missing tool → Exit 2, unknown tool → Exit 2, empty
    command → Exit 2.
- mypy --strict clean über `whatsbot/` + `hooks/` (46 Source-Files).

### Phase 2 — Projekt-Management + Smart-Detection ✅ (complete)

#### C2.8 — Phase-2-Verifikation ✅

- `make test` komplett grün: **373/373** Unit + Integration-Tests.
- **Domain-Core-Coverage 100 %** (`whatsbot/domain/*`), Ziel war >80 %.
  `allow_rules`, `commands`, `git_url`, `pending_deletes`, `projects`,
  `smart_detection`, `whitelist` haben jeweils 100 % Statement- und
  Branch-Coverage.
- `mypy --strict whatsbot/` clean, ruff format/lint clean.
- **In-process Smoke** (`tests/smoke_phase2.py`): 18/18 Checks grün.
  Deckt ab: `/new <name>`, `/new <name> git <url>`, Smart-Detection
  (12 Vorschläge aus npm + git), `/p` active-project, `/allow batch
  review` + `approve`, `/allow <pat>` manual, `/allowlist` (Sources),
  `/deny <pat>`, URL-Whitelist blockt nicht-gewhitelistete Hosts,
  `/rm <name>` 60s-Fenster, falsche PIN behält Projekt + Pending-Row,
  richtige PIN verschiebt nach Trash, `/ls` reflektiert den Delete,
  Unknown-Command-Fallback. Läuft komplett in einem Temp-Dir mit
  In-Memory-DB — kein Keychain, kein Netz, keine Nebenwirkungen.
- Smoke bestätigt die Hexagonal-Schicht-Invariante: der CommandHandler
  treibt die komplette Phase-2-Oberfläche ohne LaunchAgent, ohne
  Meta-Webhook, ohne Keychain — also sind Ports/Adapters sauber
  getrennt.

#### C2.7 — `/rm` mit 60s-Fenster, PIN + Trash ✅

- **`whatsbot/domain/pending_deletes.py`**: pure Dataclass `PendingDelete`
  mit `is_expired` + `seconds_left`. Konstante `CONFIRM_WINDOW_SECONDS = 60`
  wird vom Handler geteilt, damit Text und DB-Deadline nicht auseinanderlaufen
  können. `compute_deadline(now_ts, window)` als freies Helper, verweigert
  negative Fenster.
- **`whatsbot/ports/pending_delete_repository.py`** + **`adapters/sqlite_pending_delete_repository.py`**:
  UPSERT (zweites `/rm` vor Ablauf resettet nur die Deadline), `get`,
  `delete` (bool), `delete_expired(now_ts)` für Sweeper. Gegen die
  `pending_deletes`-Tabelle aus Spec §19, die keine FK zu `projects` hat —
  der Service ist für das Cleanup zuständig.
- **`whatsbot/application/delete_service.py`**:
  - `request_delete(name)` validiert Name + Existenz, setzt Deadline,
    upserted Row, gibt `PendingDelete` zurück.
  - `confirm_delete(name, pin)` prüft: Pending-Row existiert →
    Deadline nicht abgelaufen (abgelaufen räumt stale Row direkt weg) →
    PIN via `hmac.compare_digest` gegen Keychain `panic-pin` → `mv`
    Projekt-Tree nach `~/.Trash/whatsbot-<name>-<YYYYMMDDTHHMMSS>`
    (mit Kollisions-Suffix falls exakt gleiche Sekunde) → `projects`-Row
    löschen (CASCADE wipet `allow_rules`, `claude_sessions`, `session_locks`)
    → pending Row wegräumen → aktives Projekt clearen wenn es der gelöschte
    Name war.
  - `cleanup_expired()` für späteren Sweeper-Einsatz.
  - Fünf distinkte Exception-Klassen (`NoPendingDeleteError`,
    `PendingDeleteExpiredError`, `InvalidPinError`, `PanicPinNotConfiguredError`
    + bestehende `ProjectNotFoundError` / `InvalidProjectNameError`) —
    der Command-Handler mappt sie in unterschiedliche WhatsApp-Replies.
  - Clock ist injizierbar (`clock: Callable[[], int]`), Tests simulieren
    die 60s-Frist deterministisch statt mit `time.sleep`.
- **`whatsbot/application/command_handler.py`**: `/rm <name>` + `/rm <name>
  <PIN>` routen zu Request bzw. Confirm. Ein-Argument-Fall listet die 60s
  im Reply, Wrong-PIN und Expired liefern getrennte Emojis (`⚠️` / `⌛`).
  `/rm` ohne Argumente fällt wie `/new` auf den Pure-Router als `<unknown>`
  durch (Arity-Match via Prefix).
- **`whatsbot/main.py`**: `DeleteService` wird gewired, `SqliteAppStateRepository`
  wandert aus der Active-Project-Initialisierung in eine geteilte Variable
  (Delete-Service braucht sie für den Active-Project-Clear).
- Tests (26 neu, 373 total): `test_pending_deletes` (5),
  `test_sqlite_pending_delete_repository` (8), `test_delete_service` (13),
  `/rm`-Abschnitt in `test_command_handler` (10). Abgedeckt:
  Expired-Window mit gestepptem Clock, Wrong-PIN behält Pending-Row,
  CASCADE wiped `allow_rules`, aktives Projekt wird gecleart, fehlende
  Panic-PIN surfaced als klare Fehlermeldung statt stillschweigend jede
  PIN akzeptieren, missing Project-Dir (User hat manuell gelöscht) führt
  trotzdem zu cleanem DB-Confirm. mypy strict grün.
- **Live-Smoke**: noch ausstehend (wird mit C2.8 zusammen gemacht).

#### C2.4 / C2.5 — Allow-Rule-Management + `/p` Active-Project ✅
*(C2.4 + C2.5 zusammen abgehandelt — die Manual-Rules-Commands aus C2.5 fielen
beim Wiren des batch-Flows quasi mit ab.)*

- **`whatsbot/domain/allow_rules.py`**: pure Pattern-Logik. `parse_pattern`
  konsumiert `Tool(pattern)`, validiert gegen `ALLOWED_TOOLS = {Bash, Write,
  Edit, Read, Grep, Glob}`, lehnt unbalancierte Klammern + leere Patterns ab.
  `format_pattern` für Round-Trip + WhatsApp-Output. `AllowRuleSource`
  StrEnum (default / smart_detection / manual) matcht den
  Spec-§19-CHECK-Constraint.
- **`whatsbot/ports/allow_rule_repository.py`** + **`adapters/sqlite_allow_rule_repository.py`**:
  Idempotentes `add` (Duplikat → bestehende Row zurück), `remove` mit
  Boolean-Indikator, `list_for_project` in Insertion-Reihenfolge.
- **`whatsbot/ports/app_state_repository.py`** + **`adapters/sqlite_app_state_repository.py`**:
  Kleines Key/Value gegen die `app_state`-Tabelle mit reservierten Keys
  (`active_project`, `lockdown`, `version`, `last_heartbeat`). UPSERT via
  `ON CONFLICT(key) DO UPDATE`.
- **`whatsbot/application/settings_writer.py`**: schreibt das per-Projekt
  `.claude/settings.json` atomar (tmp + `os.replace`), bewahrt andere Top-
  Level-Keys (`hooks` etc.) und überschreibt nur `permissions.allow`.
- **`whatsbot/application/active_project_service.py`**: 2 Methoden,
  `get_active` heilt sich selbst wenn die persistierte Auswahl auf ein
  gelöschtes Projekt zeigt; `set_active` validiert + checkt Existenz.
- **`whatsbot/application/allow_service.py`**: orchestriert die drei
  Storage-Layer (DB, settings.json, `.whatsbot/suggested-rules.json`).
  Use-Cases: `add_manual`, `remove`, `list_rules`, `batch_review` (read-
  only), `batch_approve` (idempotent: bereits vorhandene Rules werden
  nicht doppelt geschrieben, klassifiziert in `added` vs. `already_present`,
  am Ende ein `_sync_settings`-Call statt N Calls).
- **`whatsbot/application/command_handler.py`** erweitert um:
  - `/p` (zeigt aktives Projekt) und `/p <name>` (setzt aktiv)
  - `/allowlist` (gruppiert nach Source: default / smart_detection / manual)
  - `/allow <pattern>` (manual single-rule add)
  - `/deny <pattern>` (manual single-rule remove)
  - `/allow batch approve` (übernimmt suggested-rules.json komplett)
  - `/allow batch review` (nummerierte Liste der offenen Vorschläge)
  - `/ls` markiert das aktive Projekt jetzt mit `▶`.
- **`whatsbot/main.py`**: `AllowService` + `ActiveProjectService` werden
  beim Bot-Start gewired; CommandHandler bekommt sie via DI.
- Tests (76 neu, 336 total): `test_allow_rules` (16), `test_sqlite_allow_rule_repository`
  (10), `test_sqlite_app_state_repository` (6), erweiterte
  `test_command_handler` (16 neue Tests für `/p`, `/allow`, `/deny`,
  `/allowlist`, batch-Flows). **Coverage 93.77%**, mypy strict + ruff
  format/lint clean.
- **Live-Smoke verifiziert** (echter Clone von `octocat/Hello-World`):
  ```
  /p                       → "kein aktives Projekt"
  /new hello git ...       → geklont, 7 .git-Vorschläge
  /p hello                 → "▶ aktiv: hello"
  /ls                      → "▶ 🟢 hello (git)"
  /allow batch review      → 7 nummerierte Vorschläge
  /allow batch approve     → "✅ 7 neue Rules" + Datei gelöscht
  /allowlist               → 7 Einträge unter [smart_detection]
  /allow Bash(make test)   → "✅ Rule hinzugefügt"
  /allowlist               → 7 + 1 unter [smart_detection] / [manual]
  /deny Bash(make test)    → "🗑 Rule entfernt"
  ```
  `~/projekte/hello/.claude/settings.json` enthält stets exakt die aktuelle
  `permissions.allow`-Liste, `~/projekte/hello/.whatsbot/suggested-rules.json`
  ist nach `batch approve` weg.

#### C2.3 — Smart-Detection für alle 9 Artefakt-Stacks ✅
- `whatsbot/domain/smart_detection.py` erweitert von 2 auf alle
  9 Artefakte aus Spec §6 / phase-2.md:
  - `yarn.lock` → 3 yarn-Rules
  - `pnpm-lock.yaml` → 2 pnpm-Rules
  - `pyproject.toml` → 5 Python-Tooling-Rules (uv, pytest, python -m, ruff, mypy)
  - `requirements.txt` → 3 pip-Rules
  - `Cargo.toml` → 5 cargo-Rules (build/test/check/clippy/fmt)
  - `go.mod` → 4 go-Rules
  - `Makefile` → 1 make-Rule
  - `docker-compose.yml` / `docker-compose.yaml` → 4 docker-compose-Rules
- Detection-Reihenfolge ist stabil (file-Artefakte in
  Deklarationsreihenfolge, dann docker-compose, dann `.git/` als letztes)
  damit die WhatsApp-Listing-Ausgabe lesbar bleibt.
- `_ARTEFACT_RULES`-Dict + `_rules_for()`-Helper ersetzen die
  C2.2-tuple-per-artefact-Pattern; neue Stacks lassen sich künftig in
  einer Zeile ergänzen.
- Defensive Guards: jedes Datei-Artefakt MUSS eine Datei sein (kein
  Verzeichnis mit dem gleichen Namen → kein Match), `.git` MUSS ein
  Verzeichnis sein (Submodul-Pointer-Datei `gitdir: ../...` matcht NICHT).
- Tests: 14 neue Tests in `test_smart_detection.py`. Coverage pro Stack
  + Combo-Cases (Python+Make+Compose+git → 17 Rules), Listing-Order-Test,
  Universal-Bash-Tool-Check, parametrisierter "muss Datei sein"-Guard.
  **280 Tests grün, Coverage 95.17%**.

#### C2.2 — `/new <name> git <url>` + URL-Whitelist + Smart-Detection-Stub ✅
- `whatsbot/domain/git_url.py`: URL-Whitelist (Spec §13). Pure Validation,
  drei Schemas (https / git@ / ssh://), drei Hosts (github / gitlab /
  bitbucket). Lehnt http://, ftp://, file:// und Shell-Injection-Versuche
  ab. `DisallowedGitUrlError` mit klarer Fehlermeldung.
- `whatsbot/domain/smart_detection.py`: C2.2-Subset des Scanners aus
  `phase-2.md`. Erkennt `package.json` (5 npm-Rules) und `.git/` (7
  git-Rules). Restliche 7 Stacks (yarn, pnpm, pyproject, requirements,
  Cargo, go.mod, Makefile, docker-compose) kommen in C2.3.
- `whatsbot/ports/git_clone.py`: `GitClone` Protocol mit
  `clone(url, dest, depth=50, timeout_seconds=180.0)`. `GitCloneError`
  für alle Failure-Modes (timeout / non-zero exit / git missing).
- `whatsbot/adapters/subprocess_git_clone.py`: echte
  `subprocess.run(["git", "clone", "--depth", "<n>", "--quiet", url, dest])`
  Implementation. stderr-Tail (500 chars) im Error-Output. Konstruierbar
  mit alternativem `git_binary` für Tests.
- `whatsbot/application/post_clone.py`: 4 reine Schreib-Funktionen für
  Post-Clone-Scaffolding (`.claudeignore` mit Spec-§12-Layer-5 Patterns,
  `.whatsbot/config.json`, `CLAUDE.md` Template **nur wenn upstream-Repo
  keines mitbringt**, `.whatsbot/suggested-rules.json` aus
  `DetectionResult` wenn Rules vorhanden).
- `whatsbot/application/project_service.py`: neuer Use-Case
  `create_from_git(name, url) -> GitCreationOutcome`. Ablauf: validate
  name + URL → reserve path → `git clone` → post-clone files → smart
  detect → write suggested-rules → INSERT row. Cleanup via
  `shutil.rmtree(ignore_errors=True)` bei jedem Fehler ab Schritt 3.
- `whatsbot/application/command_handler.py`: `/new <name> git <url>` ist
  jetzt aktiv (statt C2.2-Hint). Reply enthält Anzahl Rule-Vorschläge +
  Hinweis auf `/allow batch approve` (kommt in C2.4).
- `whatsbot/main.py`: zusätzliche DI-Parameter `git_clone` und
  `projects_root` für Tests; default ist `SubprocessGitClone()` und
  `~/projekte/`.
- Tests (59 neu, 260 total): `test_git_url` (15 — happy/disallowed,
  shell-injection-Versuche, Hostnamen-Subtilitäten wie github.io vs
  github.com), `test_smart_detection` (7), `test_post_clone` (10),
  `test_subprocess_git_clone` (6 — fake-git Skript via PATH-Override:
  exit-zero Pfad, --depth/--quiet Args, non-zero-exit, stderr-Tail,
  git-binary-missing, timeout). Erweiterte `test_command_handler` mit
  einem `StubGitClone`, der die `octocat/Hello-World`-ähnliche Layout
  schreibt (4 neue Tests für `/new git`).
  **Coverage 95.09%**, mypy strict + ruff clean.
- **Live-Smoke** mit echtem Git-Clone:
  - `/new badurl git https://evil.example.com/x/y` → 🚫 URL nicht erlaubt
  - `/new hello git https://github.com/octocat/Hello-World` → ✅ geklont
    + 7 Rule-Vorschläge aus `.git` (Hello-World hat keine package.json)
  - `/ls` zeigt `hello (git)` mit 🟢 NORMAL emoji
  - Filesystem: vollständiges `.git/` aus dem Clone, plus
    `.claudeignore`, `.whatsbot/config.json`, `.whatsbot/outputs/`,
    `.whatsbot/suggested-rules.json` (7 git-Rules), `CLAUDE.md` Template
    (Hello-World hat keine eigene)
  - Duplicate-Detection greift bei zweitem `/new hello git ...`

#### C2.1 — `/new <name>` + `/ls` (empty projects) ✅
- `whatsbot/domain/projects.py`: `Project` dataclass mirrors the spec-§19
  ``projects`` row, `Mode`/`SourceMode` StrEnums, `validate_project_name`
  (2-32 chars, lowercase + digits + `_`/`-`, no leading underscore, no
  reserved words like `ls` / `new` / `.` / `..`), `format_listing` for
  the `/ls` output with mode-emoji + active-marker.
- `whatsbot/ports/project_repository.py`: Protocol + the two structured
  errors (`ProjectAlreadyExistsError`, `ProjectNotFoundError`).
- `whatsbot/adapters/sqlite_project_repository.py`: real SQLite-backed
  CRUD; integrity-error disambiguation (duplicate name vs. CHECK
  constraint trip).
- `whatsbot/application/project_service.py`: `create_empty` (validate →
  check duplicates in DB *and* on disk → mkdir → INSERT, with directory
  rollback if INSERT fails); `list_all` with optional `active_name`
  marker.
- `whatsbot/application/command_handler.py`: refactor of
  `domain.commands.route` into a stateful handler that owns the services.
  Phase-1 commands (`/ping`/`/status`/`/help`) still delegate to the pure
  `domain.commands.route`. New: `/new <name>` (with `/new <name> git
  <url>` rejected with a clear "kommt in C2.2" hint), `/ls`.
- `whatsbot/main.py`: opens the spec-§4 state DB once, builds
  `ProjectService` + `CommandHandler`, hands them to `build_webhook_router`.
  Tests pass an in-memory connection.
- `whatsbot/http/meta_webhook.py`: `build_router` now takes a
  `command_handler` instead of raw version/uptime/db-callback args.
- Tests (66 new, 201 total): `test_projects` (15 — name validation, dataclass
  defaults, listing format), `test_sqlite_project_repository` (12 — CRUD,
  duplicate detection, CHECK constraints), `test_project_service` (10 —
  filesystem layout, error paths, rollback on INSERT failure),
  `test_command_handler` (12 — pass-through to phase-1 commands plus the
  new `/new` and `/ls` paths). **Coverage 95.30%** (target ≥80%);
  `main.py` 100%, `domain/projects.py` 100%, `application/*` 100%,
  `adapters/sqlite_project_repository.py` 100%.
- **Live-smoke verified** with a tmp DB + tmp `~/projekte/` against the
  real `CommandHandler`:
  - `/ls` (empty) → friendly hint
  - `/new alpha` → DB row + dir layout (`alpha/`, `alpha/.whatsbot/`,
    `alpha/.whatsbot/outputs/`) + structured `project_created` log line
  - `/new BAD` → `⚠️ ... ist kein gueltiger Projektname...`
  - `/new alpha` again → `⚠️ Projekt 'alpha' existiert schon.`
  - `/new beta` → second project + dirs
  - `/ls` → alphabetical listing with 🟢 (NORMAL) emoji.

### Phase 1 — Fundament + Echo-Bot ✅ (komplett)

Alle 12 Success-Criteria aus `phase-1.md` erfüllt. Bot läuft als
LaunchAgent, antwortet auf Meta-Webhooks (signiert + whitelisted) mit
Echo-Reply, und macht tägliches DB-Backup. Hexagonal-Architektur mit
135 Tests grün und 96.17% Coverage.

#### C1.7 — DB-Backup-Skript + Retention ✅
- `bin/backup-db.sh`: echtes Skript statt Stub.
  - Nutzt `VACUUM INTO` (SQLite 3.27+) statt `.backup`: produziert eine
    konsolidierte Single-File-DB ohne `-wal`/`-shm` Sidecars,
    read-consistent auch wenn der Bot währenddessen schreibt.
  - Atomares `tmp → mv`: konkurrierende Reads sehen nie eine
    halb-geschriebene Datei.
  - `PRAGMA integrity_check` auf das frische Backup vor Publish, abort+
    löschen bei Fehler statt silent garbage.
  - 30-Tage-Retention via `find -mtime +N`. ENV-Variablen
    `WHATSBOT_DB`/`WHATSBOT_BACKUP_DIR`/`WHATSBOT_BACKUP_RETENTION_DAYS`
    machen das Skript test-isoliert.
  - Strukturierte JSON-Logs (`backup_complete`/`backup_skipped_no_db`/
    `backup_failed`/`backup_integrity_failed`), portable `stat` (BSD+GNU).
- `Makefile backup-db`: Target ruft jetzt `bin/backup-db.sh` (statt Stub).
- Tests: `tests/integration/test_backup_db.py` — 7 echte subprocess-Tests
  (happy-path, intact schema, structured-log, idempotent same-day, skip
  on missing DB, retention deletes >30d, retention spares <30d, retention=0
  spares today's freshly-written backup). Alle grün.
- **Live-Smoke verifiziert**: Test-DB seeded, `bash bin/backup-db.sh` →
  `state.db.<heute>` 118KB, sqlite3 read-back zeigt seed-row, JSON-Log:
  `{"event":"backup_complete","ts":"...","target":"...","size_bytes":118784,
  "retention_days":30,"deleted_old":0}`.

#### C1.5 — Webhook + Echo (Signatur, Whitelist, Command-Router) ✅
- `whatsbot/domain/whitelist.py`: pure Parser für `allowed-senders` aus Spec
  §4 (kommasepariert, dedupe via `frozenset`, fail-closed bei leerer Liste).
- `whatsbot/domain/commands.py`: pures Routing für `/ping`, `/status`,
  `/help` mit `StatusSnapshot`-Dataclass für die nicht-pure Inputs (Version,
  Uptime, DB-OK, Env). Unbekannte Commands liefern friendly hint, raisen
  nicht — Phase 4 ersetzt diesen Branch durch "an aktive Claude-Session
  weiterleiten".
- `whatsbot/http/meta_webhook.py`:
  - `verify_signature()` — HMAC-SHA256 vs raw Body, `compare_digest`,
    fail-closed bei missing/malformed Header.
  - `check_subscribe_challenge()` — Meta-Subscribe-Handshake; gibt
    `hub.challenge` nur zurück wenn `hub.mode==subscribe` und
    `hub.verify_token` matched (constant-time compare).
  - `iter_text_messages()` — defensive Extraktion von `entry[].changes[]
    .value.messages[]` mit `type==text`; skipt malformed/non-text/missing
    silent statt zu raisen (Meta wiederholt eh).
  - `build_router(...)` — `APIRouter`-Factory mit `GET /webhook` (challenge)
    und `POST /webhook` (signature → whitelist → routing → sender).
    Sig-Check wird im non-prod env mit fehlendem app-secret übersprungen
    (für `make run-dev` ohne `make setup-secrets`).
- `whatsbot/ports/message_sender.py`: `MessageSender`-Protocol (send_text).
- `whatsbot/adapters/whatsapp_sender.py`:
  - `LoggingMessageSender` — schreibt struktured Log statt zu senden,
    Phase-1 Default und Test-Adapter.
  - `WhatsAppCloudSender` — Skelett, raised `NotImplementedError`. Echte
    httpx-/tenacity-Implementierung in C2.x sobald Projekte antworten.
- `whatsbot/main.py`:
  - Akzeptiert `message_sender`-DI-Param (Default `LoggingMessageSender`).
  - Wired `build_webhook_router` ein, plus `ConstantTimeMiddleware(
    paths=("/webhook",), min_duration_ms=200)` gegen Timing-Enumeration
    der Sender-Whitelist (Spec §5).
  - Test-Env: `_EmptySecretsProvider` Fallback wenn kein Provider
    injiziert wird, sodass Unit-Tests die Webhook-Routes ohne Mock-Keychain
    bauen können.
- `tests/fixtures/meta_*.json`: 6 echte Meta-Payloads (ping, status, help,
  unknown_command, unknown_sender, non_text/image).
- `tests/send_fixture.sh`: schickt Fixture an `:8000/webhook` mit
  HMAC-SHA256-Signatur (Secret aus Keychain falls vorhanden, sonst Dummy).
- Tests: `test_whitelist.py` (9), `test_commands.py` (8),
  `test_meta_webhook.py` (15 — Signatur, Challenge, iter_text_messages),
  `test_webhook_routing.py` (17 — End-to-End mit StubSecrets +
  RecordingSender, alle silent-drop-Pfade, Constant-Time-Padding).
  **128 Tests grün, Coverage 96.17%** (Ziel ≥80%).
- **Live-Smoke verifiziert**:
  - dev-bot via uvicorn → `tests/send_fixture.sh meta_ping` → 200 OK + ULID
  - JSON-Log zeigt: `signature_check_skipped_dev_mode` →
    `sender_not_allowed` (fail-closed, weil `allowed-senders` Secret fehlt)
  - `meta_unknown_sender` ebenfalls silent-drop mit `sender_not_allowed`
  - **Happy-Path** (gültige Signatur + gültiger Sender → `command_routed` +
    `outbound_message_dev`) ist via Integration-Tests mit `StubSecrets`
    + `RecordingSender` voll abgedeckt.

#### C1.4 — LaunchAgent + Backup-Agent + Repo-Migration ✅
- `launchd/com.DOMAIN.whatsbot.plist.template`: Bot-Agent. `KeepAlive`
  mit `SuccessfulExit=False` (restart on crash, nicht auf graceful exit;
  wichtig für `/panic`). `RunAtLoad=true`, `ProcessType=Background`.
  `EnvironmentVariables`: `WHATSBOT_ENV`, `SSH_AUTH_SOCK` (für Phase 2 git
  clone gegen private repos), `PATH`, `HOME`. `ProgramArguments` startet
  uvicorn `--factory whatsbot.main:create_app`.
- `launchd/com.DOMAIN.whatsbot.backup.plist.template`: täglich 03:00 via
  `StartCalendarInterval` (Hour=3 Minute=0). `RunAtLoad=false`. Ruft
  `bin/backup-db.sh`.
- `bin/backup-db.sh`: **Stub** — gibt strukturierte JSON-Zeile aus.
  Echtes `sqlite3 .backup` + 30-Tage-Retention kommt in C1.7.
- `bin/render-launchd.sh`: deploy/undeploy via `launchctl bootstrap`/
  `bootout`, idempotent (bootout vor bootstrap), `plutil -lint` vor jedem
  load. Refused, falls Placeholders nicht ersetzt sind.
- `Makefile`: `deploy-launchd` und `undeploy-launchd` mit `DOMAIN=`/
  `ENV=`/`PORT=` Variablen. Default `ENV=prod`, `PORT=8000`,
  `REPO_DIR=$(abspath .)`.
- Tests: `tests/unit/test_launchd_template.py` — 13 Plist-Tests
  (Label, KeepAlive, RunAtLoad, ProgramArguments, EnvironmentVariables,
  ProcessType, StartCalendarInterval). **79 Tests grün, Coverage 95.97%**.
- **Repo-Migration nach `~/whatsbot/`** (Spec §4 Default): macOS TCC
  schützt `~/Desktop`, `~/Documents`, `~/Downloads` vor
  LaunchAgent-Zugriff (Repo war anfangs unter
  `~/Desktop/projects/wabot/` — der vom LaunchAgent gespawnte uvicorn
  bekam `PermissionError` beim Lesen von `venv/pyvenv.cfg`). Nach `mv`
  läuft alles. Symlink `~/Desktop/projects/wabot → ~/whatsbot` erhalten
  als Convenience für die User-Convention "alle Projekte unter
  ~/Desktop/projects/".
- **Live-verifiziert**: `make deploy-launchd ENV=dev DOMAIN=local PORT=8000`
  → `launchctl list` zeigt Bot mit echtem PID + Backup-Agent scheduled
  → `curl /health` → 200 JSON inkl. `X-Correlation-Id` ULID
  → `launchctl print` `state=running, active count=1`
  → `app.jsonl` enthält frische `startup_complete`-Events
  → `launchd-stderr.log` bleibt leer (sauberer Run)
  → `make undeploy-launchd DOMAIN=local` → keine Agents mehr,
    Port 8000 frei, Plists entfernt.

#### C1.3 — Logging + Config + Health-Endpoint ✅
- `whatsbot/logging_setup.py`: structlog mit JSONRenderer, contextvars merge
  (für `msg_id/session_id/project/mode`), TimeStamper (ISO UTC, key `ts`),
  RotatingFileHandler nach Spec §15 (`app.jsonl`, 10 MB × 5 backups).
  Idempotent — sichere Doppelaufrufe.
- `whatsbot/config.py`: `Settings` (Pydantic BaseModel) mit Defaults aus
  Spec §4 (log_dir, db_path, backup_dir, bind_host/port, hook_bind_host/port).
  `Settings.from_env()` liest `WHATSBOT_ENV` (prod|dev|test) und
  `WHATSBOT_DRY_RUN`. `assert_secrets_present()`: prod → harter Abbruch
  (`SecretsValidationError`), dev → Warning + missing-Liste, test → skip.
- `whatsbot/http/middleware.py`:
  - `CorrelationIdMiddleware`: ULID pro Request, in structlog contextvars
    gebunden, als `X-Correlation-Id`-Header gespiegelt, Token-Reset garantiert
    keine Cross-Request-Kontamination.
  - `ConstantTimeMiddleware`: padding-fähig, Path-Filter (default leer = alle,
    in C1.5 wird es auf `("/webhook",)` gesetzt). Verhindert Timing-Enumeration
    der Sender-Whitelist (Spec §5).
- `whatsbot/main.py`: `create_app()`-Factory. configure_logging einmalig,
  Secrets-Gate (skip in test, warn in dev, raise in prod), CorrelationIdMiddleware
  global, `/health` (ok/version/uptime_seconds/env), `/metrics`-Stub
  (PlainTextResponse, leer — echtes Prometheus in Phase 8).
- `Makefile`: `run-dev` nutzt jetzt `--factory whatsbot.main:create_app`.
- Tests: `test_logging.py` (6), `test_config.py` (10), `test_middleware.py` (6),
  `test_health.py` (6). conftest hat jetzt `_reset_logging_state` autouse-Fixture.
  **66 Tests grün, Coverage 95.97%** (Ziel ≥80%). middleware.py und
  logging_setup.py jeweils 100%, config.py 100%, main.py 80% (dev-warning-Pfad
  ungetestet — wird via Live-Smoke statt Unit verifiziert).
- **Live-Smoke verifiziert**: `make run-dev` startet den Bot, `curl /health`
  liefert das erwartete JSON inkl. `X-Correlation-Id`-Header (26-char ULID),
  `/metrics` liefert leeres text/plain, `/does-not-exist` liefert 404 mit
  Header (Middleware tagt auch Errors), zwei Requests bekommen verschiedene
  Correlation-IDs, JSON-Logs schreiben sauber `secrets_missing_dev_mode` und
  `startup_complete` mit allen Spec-§15-Feldern.

#### C1.2 — Keychain-Provider + SQLite-Schema + Integrity-Restore ✅
- `whatsbot/ports/secrets_provider.py`: `SecretsProvider`-Protocol (get/set/rotate),
  Service-Konstante `whatsbot`, die 7 Pflicht-Keys aus Spec §4 als Konstanten,
  `verify_all_present()` für den Startup-Check.
- `whatsbot/adapters/keychain_provider.py`: macOS-Keychain-Implementierung via
  `keyring`-Library. `SecretNotFoundError` mit klarer Hinweis-Message bei
  fehlendem Eintrag. `rotate()` löscht erst, dann setzt neu.
- `bin/setup-secrets.sh`: interaktiver Bash-Prompt für alle 7 Secrets,
  `set -euo pipefail`, Bestehende-Werte-Confirm, Final-Verifikation,
  Exit-Code 1 bei fehlenden Einträgen.
- `sql/schema.sql`: alle 10 Tabellen + 5 Indizes exakt aus Spec §19
  (PRAGMAs separat im Adapter, weil per-connection).
- `whatsbot/adapters/sqlite_repo.py`: `connect()` setzt die 4 Pflicht-PRAGMAs
  (WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON);
  `apply_schema()`, `integrity_check()`, `latest_backup()`,
  `restore_from_latest_backup()` (mit WAL/SHM-Cleanup),
  `open_state_db()` als High-Level-Orchestrator (fresh-or-existing → check →
  restore-and-recheck → fail).
- `Makefile`: `setup-secrets` ruft jetzt `bin/setup-secrets.sh`,
  `reset-db` legt frisches Schema via `open_state_db()` an.
- Tests: `tests/conftest.py` mit `mock_keyring` (monkeypatch),
  `tmp_db_path`, `tmp_backup_dir`. 13 Secret-Tests + 17 DB-Tests.
  **30 Tests grün, Coverage 96.99%** (Ziel: ≥80%). mypy strict + ruff lint
  + ruff format alle clean.

#### C1.1 — Repo-Struktur + Python-Setup ✅
- Hexagonal layout angelegt: `whatsbot/{domain,ports,adapters,application,http}`,
  plus `hooks/`, `bin/`, `launchd/`, `sql/migrations/`, `tests/{unit,integration,fixtures}`,
  `docs/`. Package-Docstrings dokumentieren die Layer-Grenzen.
- `pyproject.toml` mit Python 3.12 constraint, pytest + coverage (fail_under=80) +
  mypy strict + ruff (E/W/F/I/B/UP/SIM/S/TID/RUF) konfiguriert.
- `requirements.txt` mit gepinnten Runtime-Deps (FastAPI 0.115, Uvicorn 0.32, Pydantic 2.10,
  structlog 24.4, python-ulid 3.0, keyring 25, tenacity 9, python-multipart 0.0).
  **Spec §5 Verriegelung 1**: kein `claude-agent-sdk`.
- `requirements-dev.txt` mit pytest 8 + asyncio + cov, httpx 0.27 (TestClient),
  mypy 1.13, ruff 0.7.
- `Makefile` mit Targets `install / test / test-unit / test-integration / smoke / lint /
  format / typecheck / setup-secrets / deploy-launchd / reset-db / backup-db / clean`.
  Operations-Targets sind Stubs mit `TODO Phase 1 C1.x` — werden in C1.2/C1.4/C1.7 befüllt.
- Verifiziert: `venv/bin/python -c "import whatsbot"` → `0.1.0`; `mypy whatsbot` clean;
  `ruff check` clean; `find_spec('claude_agent_sdk') is None`.

