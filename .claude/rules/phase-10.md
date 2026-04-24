# Phase 10: WhatsAppCloudSender — Outbound über Meta Graph API

**Aufwand**: 1 Session (gezielte Mini-Phase, kein Feature-Creep)
**Abhängigkeiten**: Phase 1-9 komplett ✅ (alle Sender-Wrapper-Schichten
— Redaction, Metrics, Circuit-Breaker — sind live; nur der innerste
Adapter fehlt.)
**Parallelisierbar mit**: —
**Spec-Referenzen**: §1 (Projektziel: Claude-Code per WhatsApp
steuern), §9 (WhatsApp-Integration + Permanent Access Token), §10
(Output-Format — Redaction läuft via Decorator, nicht hier drinnen),
§20 (Performance-Budget: WhatsApp-Send 300 ms P95), §22 (Deploy —
Permanent Access Token via Meta System User), §25 FMEA #1 (Meta-API-
Outage → Circuit-Breaker greift bereits via `@resilient`-Decorator),
`current-phase.md` Abschnitt „Impl-Debt: WhatsAppCloudSender ist
Phase-1-Skelett".

## Ziel der Phase

**Outbound-Zustellung schließen.** Der Bot empfängt Meta-Webhooks
Ende-zu-Ende, verarbeitet Commands, rendert Replies, läuft durch
Redaction + Metrics + Circuit-Breaker — nur der letzte Schritt, der
HTTP-POST an `graph.facebook.com`, ist nie gebaut worden. Phase-1 hat
ihn bewusst als Skelett hinterlassen („deferred to C2.x"), C2.x hat
es nicht eingelöst, Phase 9 hat per Live-Deployment aufgedeckt, dass
die Schuld noch da ist (`app.jsonl` 2026-04-23T21:05:44Z zeigt
`command_routed` + `outbound_message_dev`, aber kein HTTP-Call).

Phase 10 ist schmal und präzise:

1. **`WhatsAppCloudSender.send_text` implementieren** — httpx-POST
   gegen Meta Graph API. Gleiche Patterns wie
   `MetaMediaDownloader`: `@resilient`-Decorator ist schon drauf,
   tenacity für Netzwerk-Retries, strenge Timeouts, 4xx short-
   circuiten, 5xx retryen.
2. **`main.py` Sender-Auswahl** umstellen auf *fact-based* Detection:
   wenn Environment PROD **und** beide Keychain-Secrets ladbar sind
   → `WhatsAppCloudSender`; sonst `LoggingMessageSender`. Kein
   zusätzliches Env-Flag — reiner Settings-Lookup.
3. **Unit-Tests** via `httpx.MockTransport` (happy-path, 4xx, 5xx,
   timeout → retry → circuit-trip), analog zu
   `test_meta_media_downloader.py`.
4. **Live-Integration-Test** gegen echte Meta-API, skipped wenn
   `WHATSBOT_LIVE_META` nicht gesetzt. Schickt einen Test-Body an
   die eigene Handy-Nummer.
5. **Betriebs-Re-Test**: vom Handy `/ping` → Reply kommt zurück aufs
   Handy. Live-Deployment ist dann produktiv.

Phase 10 endet damit, dass:

- `WhatsAppCloudSender.send_text(to="491716598519", body="pong …")`
  tatsächlich einen HTTP-POST an `graph.facebook.com` macht und
  der User die Antwort aufs Handy kriegt.
- Der Bot erkennt automatisch beim Start, ob Live-Sender oder
  Dev-Sender gewählt werden muss (keine manuelle Konfiguration).
- Retry + Circuit-Breaker + Redaction greifen unverändert — wir
  ändern **nur** den innersten Adapter, nicht die Wrapper-Schichten.

## Voraussetzungen

- **Phase 1-9 komplett**. Alle 1542 Tests grün. Der Build ist stabil.
- **Keychain-Secrets** `meta-access-token` + `meta-phone-number-id`
  sind gesetzt (Live-Deployment §6 bereits erledigt).
- **`Settings.env = PROD`** im Live-Betrieb. Tests laufen weiter mit
  `TEST`, CI-Smoke mit `DEV` — beide bekommen den Logging-Sender.
- **Meta-App Test-Recipient** ist die eigene Handy-Nummer (§9
  bereits konfiguriert).

## Was gebaut wird

### 1. Domain

Keine neue pure Domain-Logik. Die Body-Shape ist Meta-Protokoll, also
gehört sie in den Adapter. Die Redaction läuft schon oberhalb des
Senders (via `RedactingMessageSender`-Decorator aus Phase 3).

### 2. Port

`whatsbot/ports/message_sender.py` existiert seit Phase 1 und ist
stabil (`send_text(*, to: str, body: str) -> None`). Keine Änderung.

### 3. Adapter — `WhatsAppCloudSender.send_text`

**Datei**: `whatsbot/adapters/whatsapp_sender.py` (existing, nur
`send_text` ersetzen). Die Struktur spiegelt `MetaMediaDownloader`:

- **HTTP-Endpoint**: `POST {graph_base_url}/{api_version}/{phone_number_id}/messages`
  mit Default `https://graph.facebook.com` und `v23.0`.
- **Headers**: `Authorization: Bearer <access_token>` +
  `Content-Type: application/json`.
- **Body** (Meta-Spec für text-messages):
  ```json
  {
    "messaging_product": "whatsapp",
    "recipient_type": "individual",
    "to": "<normalized-phone>",
    "type": "text",
    "text": {
      "preview_url": false,
      "body": "<body>"
    }
  }
  ```
- **Timeouts**: `connect=5 s`, `read=30 s`, `write=30 s`, `pool=5 s`
  (identisch zu MetaMediaDownloader).
- **Retry** via tenacity: 3 Versuche, exponential backoff
  (`multiplier=1, max=16`), nur bei `_RetryableSendError` (Netzwerk-
  Fehler + 5xx). 4xx short-circuiten direkt mit
  `MessageSendError`.
- **`@resilient(META_SEND_SERVICE)`**: bleibt wie jetzt. Die drei
  tenacity-Retries zählen als **ein** Circuit-Breaker-Failure —
  gleiche Invariante wie bei MetaMediaDownloader (siehe
  Kommentar dort, Z. 92-95).
- **`to`-Normalisierung**: Meta erwartet die Nummer *ohne* `+` in
  der Body-URL. Adapter stripped führende `+` / Whitespace einmal
  am Eingang (`to.lstrip("+").strip()`). Keine zusätzliche
  Validation — der Caller (CommandHandler) hat die Nummer aus dem
  Webhook-Payload, die ist schon sauber.
- **Logging**: `outbound_message_sent` mit `to` (redacted: nur
  letzte 4 Digits), `body_len`, `message_id` aus Response. Bei
  Error: `outbound_message_failed` mit `status_code` (nicht den
  Body — Meta error-bodies enthalten gelegentlich Echo vom Input).
- **Client-Management**: wie bei MetaMediaDownloader — optionaler
  `client: httpx.Client | None`-Parameter für Tests (MockTransport);
  in Prod wird pro Call ein kurzlebiger Client gebaut und
  geschlossen, damit Connection-State bei Errors nicht leakt.

**Neue Exception**: `MessageSendError` in
`whatsbot/ports/message_sender.py` (Spiegel zu
`MediaDownloadError`). Der `MetricsMessageSender`-Wrapper (Phase 8)
fängt Exceptions schon ab (countet nur bei success), CircuitBreaker
übersetzt sie in `CircuitOpenError` — beide Pfade sind bereits
getestet, wir ergänzen nur die Test-Matrix um 4xx/5xx.

### 4. Wiring — `main.py`

Ersatz für Zeilen 200-202 (`raw_sender = message_sender if ...`):

```python
raw_sender: MessageSender = _build_outbound_sender(
    settings=settings,
    secrets=secrets,
    override=message_sender,
)
```

Neue Helper-Funktion in `main.py` (oder besser: `main_helpers.py`
falls der Modul-Top zu voll wird — Entscheidung zur Implementierung):

```python
def _build_outbound_sender(
    *,
    settings: Settings,
    secrets: SecretsProvider,
    override: MessageSender | None,
) -> MessageSender:
    if override is not None:
        return override  # Tests injizieren ihren eigenen Sender
    if settings.env is not Environment.PROD:
        return LoggingMessageSender()
    token = secrets.get(KEY_META_ACCESS_TOKEN) or ""
    phone_number_id = secrets.get(KEY_META_PHONE_NUMBER_ID) or ""
    if not token or not phone_number_id:
        _log.warning("meta_credentials_missing_falling_back_to_logging_sender")
        return LoggingMessageSender()
    return WhatsAppCloudSender(
        access_token=token,
        phone_number_id=phone_number_id,
    )
```

**Fact-based, keine Env-Flags**:
- TEST/DEV → immer Logging (kein Live-Traffic während Tests).
- PROD + komplette Secrets → Live.
- PROD + fehlende Secrets → Logging mit WARN. Der Phase-1-Startup-
  Check für fehlende Secrets bleibt aktiv — der `_build_*`-Fallback
  ist nur ein zweiter Safety-Net, nicht die Primärprüfung.

Die `override`-Option bleibt für alle bestehenden Integration-Tests
unverändert (alle ~30 Stellen, die `message_sender=RecordingSender()`
durchreichen, arbeiten weiter).

### 5. Tests

#### `tests/unit/test_whatsapp_sender.py` — neue Datei

8-10 Tests mit `httpx.MockTransport` (kein echter Socket):

1. `test_send_text_happy_path` — 200 OK mit `messages:[{id}]`-
   Response → send_text returned ohne Exception, Logs zeigen
   `message_id`.
2. `test_send_text_normalises_phone_number` — Input `"+491716598519"`
   landet als `"491716598519"` im POST-Body.
3. `test_send_text_includes_expected_body_shape` — `messaging_product`,
   `recipient_type`, `to`, `type`, `text.body`, `text.preview_url=false`.
4. `test_send_text_includes_bearer_auth` — `Authorization`-Header.
5. `test_send_text_4xx_raises_immediately` — HTTP 400 → eine einzige
   Request (kein Retry), raise't `MessageSendError`.
6. `test_send_text_5xx_retries_then_raises` — MockTransport liefert
   3x HTTP 503 → 3 Requests, raise't `MessageSendError`.
7. `test_send_text_network_error_retries` — `httpx.ConnectError`
   Pfad, 3 Retries.
8. `test_send_text_5xx_then_success` — 503 → 503 → 200 → 3 Requests,
   kein Raise.
9. `test_send_text_rejects_empty_access_token` — Constructor raise't.
10. `test_send_text_empty_body_still_sends` — Meta erlaubt leere
    Bodies nicht (400 von Meta-Seite), aber der Adapter prüft das
    nicht — wir dokumentieren das und validieren: 400 → Exception.

Zusätzlich **1 Test für die Circuit-Breaker-Interaktion**: 3
tenacity-Retries zählen als *eins*, nicht drei.

11. `test_send_text_three_retries_are_one_breaker_failure` — nach
    einem Call mit 3x 503 hat der `meta_send`-Breaker 1 Failure,
    nicht 3 (Analog zum `MetaMediaDownloader`-Test).

#### `tests/unit/test_main_sender_selection.py` — neue Datei

5 Tests für die neue `_build_outbound_sender`-Helper-Funktion:

1. `test_override_takes_precedence` — injected Sender wins.
2. `test_test_env_always_uses_logging_sender`.
3. `test_dev_env_always_uses_logging_sender`.
4. `test_prod_env_with_full_secrets_uses_cloud_sender` — Type-Check
   `isinstance(result, WhatsAppCloudSender)`.
5. `test_prod_env_with_missing_token_falls_back_to_logging` —
   + WARN-Log.
6. `test_prod_env_with_missing_phone_number_id_falls_back`.

#### `tests/integration/test_whatsapp_sender_live.py` — neue Datei

Ein einziger Test, `@pytest.mark.skipif(WHATSBOT_LIVE_META not set)`:

- Liest Secrets aus Env-Vars (`WHATSBOT_LIVE_META_TOKEN`,
  `WHATSBOT_LIVE_META_PHONE_NUMBER_ID`, `WHATSBOT_LIVE_META_TO`).
- Baut echten WhatsAppCloudSender, schickt
  `body="whatsbot phase-10 live test · <iso-timestamp>"`.
- Assertet nichts über Handy-Seite (manueller Check: Message
  kommt aufs Handy).
- Wird **nicht** in `make test` / `make smoke` ausgeführt. Explizit
  manuell via `WHATSBOT_LIVE_META=1 WHATSBOT_LIVE_META_TOKEN=... pytest tests/integration/test_whatsapp_sender_live.py`.

#### Regression-Guards

Die ~30 Integration-Tests, die `message_sender=RecordingSender()`
injizieren, laufen unverändert (override-Pfad). Kein Anfassen dieser
Tests.

### 6. Keine Domain-, Port- oder Application-Änderung

Phase 10 ist bewusst ein **reiner Adapter-Swap**. Die Wrapper-
Schichten (Redaction, Metrics, Circuit-Breaker) bleiben unangefasst.
Falls während der Implementation eine Wrapper-Änderung nötig scheint
— Stop, Scope-Creep.

## Checkpoints

### C10.1 — `WhatsAppCloudSender.send_text` implementiert

- `NotImplementedError`-Stub durch echten httpx-POST ersetzt.
- `MessageSendError` in `ports/message_sender.py` hinzugefügt (o.
  in einem separaten `exceptions.py`-Modul — Entscheidung zur
  Implementation, wichtig ist der saubere Import-Pfad).
- Unit-Tests aus §5 (#1-#10) grün.
- `ruff check` + `mypy --strict` clean auf `whatsapp_sender.py`.

### C10.2 — Circuit-Breaker-Integration verifiziert

- Test #11 grün: 3 tenacity-Retries → 1 Breaker-Failure.
- Nach 5 aufeinanderfolgenden Send-Calls mit 5xx → 6ter Call
  short-circuited ohne HTTP (reuse CircuitOpenError-Pattern aus
  MetaMediaDownloader-Tests).

### C10.3 — `main.py` Sender-Selection umgestellt

- `_build_outbound_sender`-Helper existiert (in `main.py` oder
  `main_helpers.py`).
- Zeilen 200-202 ersetzt durch den Helper-Call.
- Unit-Tests aus §5 (alle 6) grün.
- Alle bestehenden Integration-Tests weiter grün (kein
  Override-Pfad-Bruch).

### C10.4 — Live-Integration-Test existiert (skipped default)

- `tests/integration/test_whatsapp_sender_live.py` angelegt mit
  korrektem `@pytest.mark.skipif`-Guard.
- Dokumentiert im Header, wie man ihn manuell ausführt.
- Läuft in `make test` / `make smoke` **nicht** mit.

### C10.5 — Live-Re-Deployment + Handy-Test

- Bot via `launchctl kickstart` neu starten.
- Vom Handy `/ping` schicken.
- Reply `pong · v0.1.0 · uptime Xs` kommt **aufs Handy**.
- `app.jsonl` zeigt `outbound_message_sent` mit
  `status_code=200` + `message_id=<wamid>`.
- `/errors` vom Handy → keine Errors in der letzten Stunde.
- `/ps` vom Handy → zeigt keinen Drift (kein Session-Recycle-Bug).

## Success Criteria

- [ ] `WhatsAppCloudSender.send_text` macht einen echten HTTP-POST
      und liefert bei 200 OK clean zurück.
- [ ] 4xx von Meta → `MessageSendError` ohne Retry.
- [ ] 5xx von Meta → 3 Retries, dann `MessageSendError`.
- [ ] Netzwerk-Fehler → 3 Retries, dann `MessageSendError`.
- [ ] 3 Retries zählen als 1 Circuit-Breaker-Failure.
- [ ] `main.py` wählt den Sender fact-based (PROD + Secrets →
      Cloud; sonst Logging).
- [ ] Alle ~30 existierenden Integration-Tests laufen weiter
      unverändert grün.
- [ ] `make test` grün (1550+ Tests).
- [ ] `mypy --strict` clean, `ruff check` clean.
- [ ] Handy-Test (C10.5) bestätigt: `/ping` → `pong` kommt aufs
      Handy.
- [ ] `CHANGELOG.md` Phase-10-Einträge pro Checkpoint.
- [ ] `current-phase.md` auf „Phase 10 komplett — Bot produktiv"
      aktualisiert.

## Abbruch-Kriterien

- **Meta Graph v23.0 ist nicht mehr aktuell** / Body-Shape hat sich
  geändert: Stop. Offizielle Meta-Docs gegen-checken, konkreten
  Shape-Fehler dokumentieren, Spec §9 + Adapter-Kommentare
  nachziehen. Kein Raten.
- **Rate-Limit von Meta greift** bereits im ersten Test-Call:
  unwahrscheinlich, aber Spec §26 Schwäche #3 erwähnt Rate-Limit
  explizit als *nicht* implementiert. Falls es doch triggert:
  Stop, manuellen Backoff im Meta-Developer-Dashboard absenken,
  `/status` checken.
- **Access-Token ist temporär** (nicht System-User-Token, sondern
  24h-Test-Token): Stop. Spec §9 + INSTALL.md-Schritt 5 bestehen
  auf Permanent Access Token. Live-Deployment §5 (current-phase.md)
  hat das bestätigt — wenn es trotzdem nach 24 h brechen würde,
  Re-Check der Meta-App-Setup.
- **Phone-Number-Format in Meta-Payload** stimmt nicht
  (z.B. Meta liefert `+`-Präfix in outbound, während wir
  stripped haben): Stop. Phone-Normalisierung auf beiden Seiten
  (inbound in `is_allowed`, outbound in `send_text`) homogenisieren
  — siehe current-phase.md „Follow-up: whitelist.py".

## Was in Phase 10 NICHT gebaut wird

- **Meta Media-Upload (outbound images/audio)** — Spec §16 sieht
  inbound-Medien vor; outbound-Medien sind nicht Scope des MVP.
  Text reicht für `/ping`, `/status`, `/log`, `/cat`-Previews.
- **Meta-Template-Messages** (für session-outside messaging) —
  nur nötig wenn 24h-Session-Fenster abgelaufen, Spec § nennt
  das nicht; der User schreibt eh aktiv rein.
- **Delivery-Status-Tracking** (Meta schickt `statuses`-Events
  für sent/delivered/read). Wäre nice-to-have für `/status`, aber
  aktueller Webhook-Handler routet nur `messages`, nicht
  `statuses`. Follow-up-Phase wenn gewünscht.
- **Rate-Limit pro Sender** (Spec §26 Schwäche #3 — bewusst nicht).
- **Read-Receipts aus dem Bot** (Meta `mark_as_read`) — kein
  Business-Need für den Single-User-Case.
- **Retry-Policy-Tuning** (aktuell 3 Versuche, exponential). Ist
  konsistent mit `MetaMediaDownloader`; wenn Meta-Outages länger
  dauern, greift der Circuit-Breaker. Kein Anpassung jetzt.
- **Phone-Normalisierung auf Whitelist-Seite** (`parse_whitelist`
  / `is_allowed` stripped `+`): current-phase.md Follow-up, aber
  low-priority und nicht Phase-10-blockierend. Entscheidung
  separat.

## Architektur-Hinweise

- **Prior Art**: `MetaMediaDownloader` (Phase 7 C7.1) ist der
  Template. Gleicher Keychain-Zugriff, gleicher httpx-Pattern,
  gleicher `@resilient`-Decorator, gleiche tenacity-Retry-Signatur.
  Jeder Review-Punkt, der sich an diesen Dateien orientiert, gilt
  auch hier. Die einzigen Unterschiede: POST statt GET, JSON-Body,
  einstufig (kein 2-Step-Meta-Protokoll).
- **Redaction ist oben drauf, nicht im Adapter**: der Adapter
  empfängt schon redacted bodies (RedactingMessageSender aus Phase
  3 ist der outer wrapper). Auf Adapter-Ebene *nicht* nochmal
  redacten — würde nur Side-Effects verdoppeln.
- **MetricsMessageSender sitzt außen**: `send_text`-Exceptions
  führen dort dazu, dass `whatsbot_messages_total{direction="out"}`
  **nicht** incrementiert wird (Phase 8 C8.4-Invariante). Das
  bleibt so — wir wollen Erfolg messen, nicht Intent.
- **Logging**: `to` wird als `tail4="8519"` geloggt, nicht als volle
  Nummer (Privacy — der einzige User ist zwar der User selbst,
  aber die Log-Files liegen 30 Tage im Filesystem).
- **Test-Strategie**: `httpx.MockTransport` statt echter Socket-
  Calls. Analog zu `test_meta_media_downloader.py`. Keine neue
  Infrastruktur nötig.
- **Kein `env`-Parameter-Flag**: Die Entscheidung Cloud-vs-Logging
  ist rein Fact-based aus `Settings.env` + Secrets-Präsenz. Das
  hält die Konfiguration-Surface minimal (keine `WHATSBOT_USE_CLOUD`-
  Variable, die divergieren kann).

## Nach Phase 10

1. `current-phase.md`: Abschnitt „Impl-Debt" entfernen, Status auf
   „Phase 10 komplett ✅ — Bot produktiv, Live-Deployment Schritte
   10 (SIM-Port-Lock, User-Action) offen".
2. `CHANGELOG.md` Phase-10-Sammelabschnitt.
3. Commit `feat(phase-10): outbound via WhatsApp Cloud API`.
4. Live-Re-Test (C10.5) vom Handy aus verifiziert.
5. User-Follow-up (nicht Phase-11): Phone-Normalisierung in
   `whitelist.py` klären — entweder Dokstring + INSTALL.md auf
   „ohne `+`" festzurren oder beidseitig normalisieren.
6. User-Follow-up optional: Delivery-Status-Tracking (Meta
   `statuses`-Events) wenn Bedarf besteht.
