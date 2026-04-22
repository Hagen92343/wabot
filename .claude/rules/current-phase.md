# Aktueller Stand

**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.5 (Webhook + Echo)
**Letzter abgeschlossener Checkpoint**: C1.4 (LaunchAgent + Backup-Agent)

## Was als Nächstes zu tun ist

C1.5 laut `phase-1.md` §6 + §7 + §8:

1. `whatsbot/http/meta_webhook.py` (oder im `main.py` als Router):
   - `GET /webhook` — Subscribe-Challenge: `hub.mode=subscribe` +
     `hub.verify_token` matchen → echo `hub.challenge`, sonst 403
   - `POST /webhook` — Signatur-Verifikation (HMAC-SHA256 mit
     `meta-app-secret` aus Keychain, gegen Raw-Body, Header
     `X-Hub-Signature-256: sha256=<hex>`). Ungültig → 200 OK +
     silent drop + WARN-Log.
2. Sender-Whitelist (`domain/whitelist.py` pure):
   - Keychain-`allowed-senders` (kommasepariert) parsen
   - Pro Webhook-Payload: `entry[].changes[].value.messages[].from`
     gegen Whitelist; Mismatch → 200 OK + silent drop + WARN-Log
   - **Constant-Time-Padding** auf `/webhook` durch Aktivierung von
     `ConstantTimeMiddleware(paths=("/webhook",))` in `main.py`
3. `whatsbot/domain/commands.py` — pures Routing:
   - `/ping` → `pong · <version> · uptime <s>`
   - `/status` → System-Info (Uptime, Heartbeat-Age, DB-Status)
   - `/help` → Liste der in C1.5 verfügbaren Commands
4. Outbound: in dev-mode Antwort nur loggen (Spec §17), in prod-mode
   echtes Meta-Send-API. Adapter `adapters/whatsapp_sender.py` mit
   Skelett (vollwertig in C2.x wenn Projekte dazukommen).
5. `tests/fixtures/meta_*.json` — echte Meta-Payloads für `/ping`,
   `/status`, signed/unsigned, allowed/disallowed sender
6. `tests/integration/test_webhook.py` (FastAPI TestClient gegen alle
   Permutationen)
7. `tests/send_fixture.sh <name>` — schickt fixture an
   `http://127.0.0.1:8000/webhook`

Verifikation (C1.5 done):
- `tests/send_fixture.sh meta_ping` (in dev-mode) → 200 OK + Log-Eintrag
  `command_routed` mit msg_id + Response-Payload als WARN-Log
- Falsche Signatur → 200 OK + WARN-Log, kein Routing
- Fremder Sender → 200 OK + WARN-Log, kein Routing
- `make test` grün, Coverage ≥80%

## Format-Konvention für Updates

Wenn du einen Checkpoint abschließt, update diese Datei so:

```
**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.6 (Tests grün)
**Letzter abgeschlossener Checkpoint**: C1.5 (Webhook + Echo)
```

Wenn du eine ganze Phase abschließt:

```
**Aktive Phase**: Phase 2 – Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: noch keiner
**Letzter abgeschlossener Checkpoint**: C1.7 (DB-Backup) — Phase 1 komplett
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-<N>.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
