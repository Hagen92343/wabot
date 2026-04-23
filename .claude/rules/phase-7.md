# Phase 7: Medien-Pipeline

**Aufwand**: 2-3 Sessions
**Abhängigkeiten**: Phase 4 komplett ✅ (SessionService für Prompt-
Forwarding, MetaWebhook für Inbound-Routing)
**Parallelisierbar mit**: Phase 8
**Spec-Referenzen**: §4 (Medien-Cache-Pfad), §9 (Nachrichtentypen),
§16 (Medien-Handling: Bilder, PDFs, Audio, Reject-Pfade),
§20 (Performance-Budgets: 30s-Audio <5s, 60s-Audio <10s),
§21 Phase 7 (Checkpoints, Success-Criteria, Abbruch-Kriterien)

## Ziel der Phase

**Bilder, PDFs und Voice-Messages vom Handy aus an Claude.** Bilder
und PDFs werden gecached und Claude kriegt nur den Pfad zugespielt
(spart Token, Claude liest selbst). Voice-Messages laufen durch eine
ffmpeg→Whisper-Pipeline, das Transkript geht als normaler Text-Prompt
durch (mit Input-Sanitization in Normal-Mode aus Phase 3 — wichtig:
Voice ist genauso untrusted-input wie geschriebener Text).

Unsupportete Typen (Video, Location, Sticker, Contact) werden mit
einer freundlichen Erklär-Message abgelehnt, nicht silent gedroppt.

Plus die Cache-Wartung: TTL 7 Tage mit Secure-Delete (Spec §16),
1 GB Größen-Cap mit Oldest-First-Eviction. Sweeper läuft als
Background-Task im FastAPI-Lifespan (wie der Heartbeat-Pumper aus
Phase 6).

Phase 7 endet damit, dass:

1. Eine inbound WhatsApp-Image-Message → Meta-Media-Download →
   Validierung (MIME, Magic-Bytes, Größe) → Cache → Prompt
   `analysiere /path/to/<msg_id>.jpg: <begleittext>` an die aktive
   Claude-Session.
2. Eine inbound PDF-Document-Message → analoge Pipeline mit
   PDF-Magic-Byte-Check, max 20 MB.
3. Eine inbound Voice-Message → Sofort-Ack `🎙 Transkribiere…` →
   ffmpeg OGG→WAV 16k mono → whisper.cpp small-multilingual → der
   Transkript-Text läuft den ganzen Spec-§9-Sanitize-Pfad durch
   und landet via SessionService.send_prompt in tmux.
4. Video / Location / Sticker / Contact werden mit einer freundlichen
   Reject-Reply geantwortet (Spec §9).
5. Cache-Sweeper läuft alle ~10 min und räumt: Items älter 7 Tage
   (Secure-Delete: nullen vor unlink), und bei Cache-Größe >1 GB
   die ältesten Items bis wieder unter Schwelle.

## Voraussetzungen

- **Phase 4** komplett (SessionService.send_prompt für die
  Prompt-Forwarding-Hand-off-Stelle).
- **Phase 3** komplett (Sanitization-Layer für Voice-Transkripte —
  ein Voice-Prompt kann genauso wie ein geschriebener Prompt
  Injection-Telegraphen enthalten).
- **System-Dependencies** (Spec §4):
  - `ffmpeg` — `brew install ffmpeg` (User hat das wahrscheinlich
    schon, INSTALL.md fügt es zur Liste hinzu).
  - `whisper.cpp` — `brew install whisper-cpp` plus das `small`-
    Modell muss heruntergeladen werden. Pfad konfigurierbar via
    `Settings.whisper_binary` + `Settings.whisper_model_path`.
- **Meta-Access-Token** im Keychain (Phase 1 Secret) — Meta-Media-
  Download braucht Bearer-Auth.

## Was gebaut wird

### 1. Domain — Medien-Validierung (pure)

- **`whatsbot/domain/media.py`** — pure:
  - `MediaKind` StrEnum: `IMAGE`, `AUDIO`, `DOCUMENT`, `VIDEO`,
    `LOCATION`, `STICKER`, `CONTACT`, `UNKNOWN`. `UNKNOWN`-Bucket
    fängt zukünftige Meta-Typen ohne Crash.
  - `SUPPORTED_KINDS = frozenset({IMAGE, AUDIO, DOCUMENT})` —
    der Rest wird gerejected.
  - `MAX_BYTES_PER_KIND` Mapping (Spec §16):
    - IMAGE: 10 MB
    - DOCUMENT: 20 MB
    - AUDIO: 25 MB (großzügig — Whisper-Chunks)
  - `ALLOWED_MIMES_PER_KIND` Mapping:
    - IMAGE: `image/jpeg`, `image/png`, `image/webp`, `image/heic`
    - DOCUMENT: `application/pdf`
    - AUDIO: `audio/ogg`, `audio/opus`, `audio/mp4`, `audio/mpeg`,
      `audio/wav`
  - `validate_size(kind, bytes_count) -> None` raise't bei
    Überschreitung.
  - `validate_mime(kind, mime) -> None` raise't bei nicht-erlaubt.
  - `MediaValidationError` — konkrete Exception mit Reason für
    User-facing Reply.

- **`whatsbot/domain/magic_bytes.py`** — pure:
  - `looks_like_pdf(payload: bytes) -> bool` — `payload[:5] == b"%PDF-"`
    plus optional `%PDF-1.X`-Versions-Check.
  - `looks_like_image(payload: bytes, mime: str) -> bool` — JPEG
    `\xFF\xD8\xFF`, PNG `\x89PNG\r\n\x1a\n`, WEBP `RIFF....WEBP`,
    HEIC `....ftyp`. Pure-Lookup.
  - `looks_like_audio(payload: bytes, mime: str) -> bool` — OGG
    `OggS`, MP3 `ID3` / sync-bytes, MP4 `....ftyp`, WAV `RIFF....WAVE`.
  - Alle return False bei zu kurzem Payload statt zu raisen.

- **`whatsbot/domain/transcription.py`** — pure:
  - `clean_transcript(raw: str) -> str` — strip Whisper-Header-
    Annotations (`[BLANK_AUDIO]`, `[Music]`, etc.), trim, collapse
    multi-line whitespace.
  - `MAX_TRANSCRIPT_CHARS = 4000` — über das hinaus → trunkieren mit
    `…`-Suffix (sonst sprengen wir den Prompt-Buffer).

- **`whatsbot/domain/media_cache.py`** — pure:
  - `CACHE_TTL_SECONDS = 7 * 24 * 3600` (7 Tage, Spec §16).
  - `CACHE_MAX_BYTES = 1 * 1024 * 1024 * 1024` (1 GB, Spec §16).
  - `CachedItem`-Dataclass: path, size_bytes, mtime.
  - `is_expired(item, *, now, ttl) -> bool` — pure compare.
  - `select_for_eviction(items, *, current_size, max_size) ->
    list[CachedItem]` — sortiert by mtime ascending, picked oldest
    until under max. Pure list-out.

### 2. Ports

- **`whatsbot/ports/media_downloader.py`** — Protocol:
  - `download(media_id: str) -> DownloadedMedia` —
    `DownloadedMedia(payload: bytes, mime: str, sha256: str)`.
  - Errors: `MediaDownloadError` (network, 4xx/5xx).

- **`whatsbot/ports/audio_converter.py`** — Protocol:
  - `to_wav_16k_mono(input_path: Path, output_path: Path) -> None` —
    fail-fast bei ffmpeg-Errors.
  - `AudioConversionError`.

- **`whatsbot/ports/audio_transcriber.py`** — Protocol:
  - `transcribe(wav_path: Path, *, language: str | None = None)
    -> str`.
  - `TranscriptionError`.

- **`whatsbot/ports/media_cache.py`** — Protocol:
  - `store(media_id: str, payload: bytes, suffix: str) -> Path` —
    schreibt atomar (tmp + replace) nach
    `<cache_dir>/<media_id><suffix>`. Returnt den Pfad.
  - `path_for(media_id: str, suffix: str) -> Path` — read-only
    Pfad-Resolver.
  - `list_all() -> list[CachedItem]` — für den Sweeper.
  - `secure_delete(path: Path) -> None` — Best-Effort: open in
    r+b, write zeros, fsync, unlink.

### 3. Adapter

- **`whatsbot/adapters/meta_media_downloader.py`** — httpx-basiert:
  - 2-step Meta-Media-API:
    1. `GET https://graph.facebook.com/v23.0/<media_id>` →
       JSON mit `url` field.
    2. `GET <url>` → bytes + `Content-Type` + `Content-Length`.
  - Beide Calls mit Bearer-Auth (Keychain `meta-access-token`).
  - Timeouts: connect 5 s, read 30 s.
  - SHA-256 über payload für Duplicate-Detection.
  - tenacity-Retries (3x, exponential) für Network-Fehler — gleiches
    Pattern wie `WhatsAppCloudSender` (Phase 1).

- **`whatsbot/adapters/ffmpeg_audio_converter.py`** — subprocess-
  basiert:
  - `ffmpeg -i <input> -ar 16000 -ac 1 -f wav <output>` mit -y für
    Overwrite.
  - 30 s Timeout (>30 s Audio = ungewöhnlich groß).
  - stderr-Tail bei non-zero exit für sinnvolle Error-Messages.
  - Konfigurierbares `ffmpeg_binary` für Tests.

- **`whatsbot/adapters/whisper_cpp_transcriber.py`** — subprocess-
  basiert:
  - `whisper-cli -m <model> -l <lang|auto> -f <wav> -nt -np`
    (no-timestamps, no-progress).
  - Output via stdout + cleanup via `domain/transcription.clean_transcript`.
  - 60 s Timeout (Spec §20: 60s-Audio in <10s — wir geben 6x
    Spielraum).

- **`whatsbot/adapters/file_media_cache.py`** — filesystem-cache:
  - Cache-Dir aus `Settings.media_cache_dir` (default
    `~/Library/Caches/whatsbot/media/`).
  - `store` schreibt atomar.
  - `secure_delete`: `os.urandom`(? — eigentlich: nullen ist günstiger
    und matched Spec §16 wörtlich), `fsync`, `os.unlink`.
  - `list_all` via `Path.iterdir` + `stat()`.

### 4. Application

- **`whatsbot/application/media_service.py`** — Orchestrator:
  - `process_image(media_id, mime, sender, caption) -> ImageOutcome`
    pipeline:
    1. Download via Meta-API.
    2. Validate MIME + size + magic-bytes.
    3. Cache.
    4. Build Claude-Prompt: `analysiere {path}: {caption}`
       (caption optional).
    5. Forward to active project's Claude session via
       `SessionService.send_prompt`.
    Returnt strukturiertes `ImageOutcome(prompt_sent, cache_path)`.
  - `process_pdf(media_id, mime, sender, caption) -> PdfOutcome` —
    analog, andere Magic-Bytes + Size-Cap.
  - `process_audio(media_id, mime, sender) -> AudioOutcome`:
    1. Download.
    2. Validate.
    3. Cache (OGG-Original).
    4. ffmpeg → WAV 16k mono in den Cache.
    5. Whisper → Transkript.
    6. Cleanup transcript.
    7. Forward via SessionService.send_prompt — geht als normaler
       Text-Prompt durch, mit Input-Sanitization.
  - `process_unsupported(kind) -> RejectionReply` — friendly
    explain-message per Kind (Spec §9 wording).

- **`whatsbot/application/media_sweeper.py`** — Background-Loop
  analog zum HeartbeatPumper (FastAPI-Lifespan-Task):
  - alle `MEDIA_SWEEP_INTERVAL_SECONDS` (default 600 s = 10 min):
    - TTL-Sweep: alle Items älter 7d → secure_delete.
    - Size-Sweep: wenn total >1 GB → oldest-first secure_delete bis
      unter Schwelle.
  - Failures (z.B. Datei kann nicht geschrieben/gelesen werden)
    werden geloggt, Sweep läuft weiter.

### 5. HTTP — Webhook erweitert

- **`whatsbot/http/meta_webhook.py`**:
  - `iter_media_messages(payload)` neu — analog zu
    `iter_text_messages`, extrahiert non-text messages mit
    `(kind, media_id, mime, caption, sender, msg_id)`-tuples.
  - Im POST-Handler: nach `iter_text_messages` zusätzlich
    `iter_media_messages` durchlaufen.
  - Pro Media-Message:
    - Sender-Whitelist + Signatur-Check sind schon weiter oben.
    - Sofort-Ack bei AUDIO (`🎙 Transkribiere…`), bei IMAGE/PDF
      kein Ack nötig (Claude antwortet wenn fertig).
    - Dispatch an `media_service.process_<kind>`.
    - Bei Validation-Fail → friendly Error-Reply, nicht silent.
  - Bei UNSUPPORTED-Kinds → friendly reject reply.

### 6. Wiring

- **`whatsbot/main.py`**:
  - `MediaService` baut wenn `tmux` + `session_service` vorhanden
    sind (braucht send_prompt). Default-Adapters (HTTP-Downloader,
    ffmpeg, whisper-cpp, file-cache) im non-test-env.
  - Test-injectable: `media_downloader=`, `audio_converter=`,
    `audio_transcriber=`, `media_cache=` Params an create_app.
  - `MediaSweeper` als zweiter FastAPI-Lifespan-Task (gleiches
    Pattern wie HeartbeatPumper). Default ON in PROD/DEV, opt-in
    via `enable_media_sweeper=True` in TEST.

- **`whatsbot/config.py`**:
  - `media_cache_dir: Path = ~/Library/Caches/whatsbot/media`
  - `whisper_binary: str = "whisper-cli"`
  - `whisper_model_path: Path = …` — Default per `brew install
    whisper-cpp`-Standard-Pfad. Wenn fehlend: log warning, audio
    wird abgelehnt mit klarer Fehlermeldung statt Crash.
  - `ffmpeg_binary: str = "ffmpeg"`

## Checkpoints

### C7.1 — Image-Pipeline (a) + Reject-Pfade

Zwei in einem Commit weil die HTTP-Layer für beide gemeinsam
gebraucht wird:

- Domain: `MediaKind` + `validate_size` + `validate_mime` +
  `looks_like_image`.
- Port + Adapter: `MediaDownloader`, `MediaCache` mit `store` +
  `secure_delete`.
- Application: `MediaService.process_image` + `process_unsupported`.
- HTTP: `iter_media_messages` + Webhook-Routing + Reject-Replies
  für Video/Location/Sticker/Contact.
- Tests:
  - 8+ unit für Domain (Magic-Bytes pro Image-Format, Size-Edges,
    MIME-Allow-List).
  - 4+ unit für `MediaService.process_image` (mit FakeDownloader,
    FakeCache, FakeSessionService).
  - 6+ unit für `process_unsupported` (jeder Reject-Kind).
  - 1+ integration für `iter_media_messages` (echte Meta-Payloads
    in `tests/fixtures/meta_image_*.json`).
  - 1 e2e: signed `/webhook` mit Image-Payload + FakeDownloader →
    cache + Claude-Prompt enthält den richtigen Pfad.

### C7.2 — PDF-Pipeline

- Domain: `looks_like_pdf` + 20 MB-Cap.
- Application: `MediaService.process_pdf` (analog zu image).
- HTTP: PDF-Document-Routing.
- Tests: 4 unit + 1 e2e (analog C7.1).

### C7.3 — Audio-Pipeline (download + ffmpeg)

- Port + Adapter: `AudioConverter` + `FfmpegAudioConverter` (echter
  Subprocess gegen `tests/fixtures/audio/short.ogg`, skipped wenn
  ffmpeg fehlt — analog `test_subprocess_git_clone`).
- Application: Stage-1 `process_audio_to_wav` (Download + Validate +
  Cache + Convert).
- Tests: 5 unit (Convert mit FakeFfmpeg + Failure-Containment) +
  1 integration (echter ffmpeg auf Test-Fixture).

### C7.4 — Whisper-Transkription

- Port + Adapter: `AudioTranscriber` + `WhisperCppTranscriber`
  (echter Subprocess gegen einen Test-WAV, skipped wenn whisper-cli
  fehlt).
- Domain: `clean_transcript` (strip Markup, trim, truncate).
- Application: `MediaService.process_audio` zieht den Text durch
  und ruft `SessionService.send_prompt`.
- HTTP: Sofort-Ack `🎙 Transkribiere…` bevor wir die Pipeline
  starten.
- Tests: 6 unit (clean_transcript-Edge-Cases, FakeWhisper im
  Service) + 1 e2e (real ffmpeg + real whisper, marked
  `@pytest.mark.slow`, default skip in CI).

### C7.5 — Cache-Sweeper

- Application: `MediaSweeper` mit asyncio-Loop, identisches
  Pattern wie HeartbeatPumper (start/stop in Lifespan).
- Domain: `CACHE_TTL_SECONDS` + `CACHE_MAX_BYTES` +
  `select_for_eviction`.
- Adapter: `secure_delete` schreibt Nullen über die Datei-Größe,
  fsync, unlink.
- Tests:
  - 5 unit für `select_for_eviction` (oldest-first ordering,
    under-cap no-op).
  - 4 unit für `MediaSweeper` (TTL-only, size-only, both,
    failure-containment).
  - 2 integration für `secure_delete` (Datei tatsächlich genullt
    + entfernt) und für die Lifespan-Anbindung (Sweeper läuft im
    TestClient, manuell-getriggerter Sweep findet Items).

## Success Criteria

- [ ] Image- und PDF-Inbound landet im Cache + ergibt einen
      validen Pfad-Prompt im aktiven Projekt.
- [ ] Voice-Inbound (Test-Fixture, 5 s) ergibt ein Transkript +
      Text-Prompt im aktiven Projekt.
- [ ] Reject-Replies für Video/Location/Sticker/Contact sind
      friendly + konsistent mit Spec §9.
- [ ] Cache-Sweeper räumt TTL-Items + Size-Cap-Items.
- [ ] secure_delete schreibt Nullen vor unlink (wenigstens auf
      System mit shrinkable APFS — best-effort dokumentiert).
- [ ] Validation-Errors (zu groß, falsche MIME, magic-bytes
      mismatch) ergeben friendly Error-Replies, kein silent drop
      und kein Crash.
- [ ] Performance-Budget Spec §20: 30s-Audio < 5 s Whisper
      (manuell verifiziert auf M1, nicht in CI).
- [ ] Alle Domain/Service-Tests grün, mypy --strict clean,
      ruff clean.
- [ ] Phase-1-Schwester-Sweeper (HeartbeatPumper-Pattern) bewährt
      sich als wiederverwendbar.

## Abbruch-Kriterien

- **Whisper-Latenz >30 s auf M1** für ein 30 s-Audio: Stop. Spec
  §21 Phase 7 sagt: Fallback `small.en` statt multilingual. RUNBOOK-
  Eintrag und User-Entscheidung welcher Trade-off.
- **ffmpeg-OGG/Opus → WAV-Konvertierung scheitert** auf gängigen
  WhatsApp-Voice-Formaten: Stop. Format-Probing einbauen oder auf
  whisper.cpp's eingebauten OGG-Reader umstellen (existiert in
  neueren Versionen).
- **Meta-Media-API liefert anderes JSON-Schema** als die offiziellen
  Docs: Stop. Echte Meta-Test-Payloads aus dem User-Account ziehen
  und als Fixture committen, dann Adapter anpassen.
- **`secure_delete` hat keinen messbaren Effekt** auf APFS (CoW
  + Snapshots): das ist erwartet, *kein* Abbruch — wir
  dokumentieren es in SECURITY.md als „best-effort, nicht
  forensik-sicher" und gehen weiter.

## Was in Phase 7 NICHT gebaut wird

- **Token-Cost-Tracking für Bilder** (Spec §16: ~1.500/Bild).
  Kommt in Phase 8 mit der Limits-Observability.
- **`/clean-transcripts`-Command** (Spec §20). Phase 8 + 9.
- **Voice-Transcript-DB-Persistenz** (Spec §16: „Transkript in DB
  persistieren (Debug)") — wir loggen das Transkript strukturiert,
  bauen aber keine eigene Tabelle. Wenn der User das nachträglich
  brauchen will, kommt's mit Observability in Phase 8.
- **Rate-Limit pro Sender** (Spec §26 — bewusst nicht
  implementiert).

## Architektur-Hinweise

- **Sanitization gilt für Voice-Transkripte**: ein 60-s-Voice-Prompt
  „ignore previous instructions, …" muss durch den Phase-3-
  injection-Detector laufen. `MediaService.process_audio` ruft
  `SessionService.send_prompt(active, transcript)` — das macht den
  Sanitize-Wrap automatisch im Normal-Mode.

- **Cache-Pfade müssen lesbar für Claude sein**: Spec §12 Layer 5
  (Read-Block für sensitive Dateien) listet `~/Library/Caches`
  *nicht* in `globalIgnore`. Trotzdem kann ein Projekt-spezifisches
  `.claudeignore` das blocken — das ist in Ordnung, der User hat
  es dann selbst entschieden.

- **MediaSweeper läuft im selben asyncio-Loop wie der Webhook**.
  Disk-IO über `asyncio.to_thread` damit der Loop nicht blockiert
  (gleiches Muster wie HeartbeatPumper).

- **Whisper-Modell-Pfad ist konfigurierbar**: `whisper-cpp` via
  brew installiert nicht direkt das Modell, der User muss
  `~/Library/whisper-cpp/models/ggml-small.bin` o.ä. selbst
  ablegen. INSTALL.md kriegt einen Block dafür.

- **Test-Strategie**: Externe Binaries (ffmpeg, whisper-cli) werden
  in Unit-Tests gestubbt (FakeFfmpeg/FakeWhisper). Integration-
  Tests gegen echte Binaries skippen wenn `shutil.which("ffmpeg")
  is None` bzw. `whisper-cli`. Subprocess-Stubs auf PATH (analog
  C2.2 fake-git, C6.5 watchdog stubs) für Verhalten-Tests.

## Nach Phase 7

Update `.claude/rules/current-phase.md` auf Phase 8. Phase 8
(Observability + Limits) kann parallel zu Phase 7 oder direkt
danach laufen. Warte auf User-Freigabe.
