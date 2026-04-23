# INSTALL

Komplette Installation von einem leeren Mac (macOS 14+, Apple Silicon) bis zum ersten `/ping` vom Handy.

Setze die Schritte in der Reihenfolge um, die hier steht. Quer-Referenzen in die Spec sind bewusst — wenn du etwas genauer verstehen willst, lies die Spec-Abschnitte parallel.

## 0. Voraussetzungen

Vor Start musst du haben:

- **Mac mit Apple Silicon** (M1 oder neuer) und macOS 14+.
- **Claude Max 20x Subscription** (wird vom vierfachen Auth-Lock in Spec §5 erzwungen — API-Billing ist nicht vorgesehen).
- **Handy mit WhatsApp**, das du als Bot-Nummer nutzt (Prepaid-SIM empfehlenswert — siehe Spec §29 Kostenmodell).
- **Persönliches Handy mit WhatsApp**, von dem du prompten willst.
- **Cloudflare-Account** (Free-Tier reicht, 2FA zwingend).
- **GitHub-Account** mit `gh auth login` fertig (für private Repo-Clones).

## 1. Homebrew-Pakete

```bash
brew install python@3.12 tmux ffmpeg cloudflared whisper-cpp
```

Versionen validieren:

```bash
python3.12 --version   # Python 3.12.x
tmux -V                # tmux 3.4 oder neuer
ffmpeg -version        # ffmpeg 7+
cloudflared --version  # 2024.x
whisper-cli --help 2>&1 | head -1
```

## 2. Whisper-Modell herunterladen

`whisper-cpp` via Brew installiert das Binary, aber kein Modell. Wir nutzen das `small`-Modell (multilingual, ca. 466 MB, Spec §16).

```bash
mkdir -p ~/Library/whisper-cpp/models
cd /tmp
git clone --depth 1 https://github.com/ggerganov/whisper.cpp wcpp-tmp
cd wcpp-tmp
bash ./models/download-ggml-model.sh small
cp models/ggml-small.bin ~/Library/whisper-cpp/models/
cd ~ && rm -rf /tmp/wcpp-tmp
```

Sanity-Check:

```bash
ls -l ~/Library/whisper-cpp/models/ggml-small.bin
# -rw-r--r--  1 user  staff  466M ...
```

## 3. Claude Code installieren

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude /login      # Max-Subscription wählen (nicht API!)
claude /status     # muss "subscription" anzeigen, nicht "API"
```

Wenn `/status` "API" sagt, logge dich mit `claude /logout` aus und starte `/login` neu. Sonst bricht der `preflight.sh` beim Bot-Start ab.

## 4. Repo klonen + Python-Setup

```bash
git clone <dein-repo> ~/whatsbot
cd ~/whatsbot
make install
```

`make install` legt `~/whatsbot/venv/` an, installiert alle Dependencies aus `requirements.txt`, legt die SQLite-DB unter `~/Library/Application Support/whatsbot/state.db` an.

## 5. Keychain-Secrets

Der Bot liest alle sensiblen Werte aus dem macOS Keychain (Spec §4, Service-Name `whatsbot`):

| Eintrag | Zweck |
|---|---|
| `meta-app-secret` | Meta-Webhook-Signature-Verifikation |
| `meta-verify-token` | Meta-Webhook-Subscribe-Handshake |
| `meta-access-token` | Meta Send-API + Media-Download (Bearer) |
| `meta-phone-number-id` | Bot-Nummer-ID aus Meta-Dev-Konsole |
| `allowed-senders` | Deine Handy-Nummer(n), kommasepariert, E.164 |
| `panic-pin` | PIN für `/rm`, `/force`, `/unlock` |
| `hook-shared-secret` | IPC-Token zwischen Hook-Script und Bot-Endpoint |

Interaktives Setup:

```bash
make setup-secrets
```

Es fragt dich die sieben Werte nacheinander ab. `hook-shared-secret` kannst du mit `openssl rand -hex 32` generieren, `panic-pin` ist eine 4-8-stellige Zahl deiner Wahl.

Verifikation:

```bash
security find-generic-password -s whatsbot -a meta-app-secret -w
# → gibt deinen Secret-String aus
```

## 6. Meta WhatsApp Cloud API

1. In [developers.facebook.com](https://developers.facebook.com/apps/) eine neue App anlegen (Typ: Business).
2. Produkt **WhatsApp** hinzufügen.
3. Unter **API Setup** die Test-Nummer notieren und bis zu 5 Empfänger verifizieren (eigene Nummer reicht für Single-User).
4. Permanent Access Token via **System User** ausstellen (nicht den 24h-Test-Token nehmen).
5. Unter **Configuration → Webhooks** kommen Callback-URL und Verify-Token später in Schritt 7 — **jetzt noch nicht setzen**.
6. Die drei Secrets aus §5 ins Keychain übertragen (`make setup-secrets` kann bei Bedarf einzeln auch via `security add-generic-password -U -s whatsbot -a <name> -w` überschrieben werden).

Parallel dazu: Business-Verifikation starten (Gewerbeschein oder Webseite mit Impressum) — dauert Tage, blockiert aber nicht den Test-Modus.

## 7. Cloudflare Tunnel

```bash
cloudflared tunnel login                   # öffnet Browser, Account wählen
cloudflared tunnel create whatsbot
```

Die UUID aus der Ausgabe merken. Dann Config:

```bash
mkdir -p ~/.cloudflared
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <UUID>
credentials-file: $HOME/.cloudflared/<UUID>.json
ingress:
  - hostname: whatsbot.<deine-domain>.de
    service: http://127.0.0.1:8000
  - service: http_status:404
EOF
```

DNS-Route setzen:

```bash
cloudflared tunnel route dns whatsbot whatsbot.<deine-domain>.de
```

Falls du keine eigene Domain willst: `cloudflared tunnel run whatsbot` im `try` gibt eine `*.trycloudflare.com`-URL aus, die du stattdessen in die Meta-Konsole einträgst.

Tunnel als User-LaunchAgent installieren:

```bash
cloudflared service install   # oder: manuelles Plist, siehe cloudflared-Docs
```

Verifikation: `curl https://whatsbot.<deine-domain>.de/health` muss in §9 funktionieren.

## 8. Bot-LaunchAgents deployen

Drei LaunchAgents werden installiert (Spec §4):

- **Bot** (`com.<DOMAIN>.whatsbot.plist`) — FastAPI auf `127.0.0.1:8000` + Hook auf `127.0.0.1:8001`.
- **DB-Backup** (`com.<DOMAIN>.whatsbot.backup.plist`) — täglich 03:00 nach `~/Backups/whatsbot/`.
- **Watchdog** (`com.<DOMAIN>.whatsbot.watchdog.plist`) — alle 30 s Heartbeat-Check, kills wb-*-Sessions bei stale heartbeat.

```bash
make deploy-launchd DOMAIN=<irgendwas>       # z.B. DOMAIN=local
launchctl list | grep whatsbot                # drei Einträge
```

Logs tail'en:

```bash
tail -f ~/Library/Logs/whatsbot/app.jsonl
```

## 9. Meta-Webhook-URL eintragen

Zurück in der Meta-Dev-Konsole (§6, Configuration → Webhooks):

- **Callback URL**: `https://whatsbot.<deine-domain>.de/webhook`
- **Verify Token**: gleicher Wert wie `meta-verify-token` aus dem Keychain.
- Klicke **Verify and Save** — Meta ruft `GET /webhook?hub.mode=subscribe&...` auf; der Bot antwortet mit dem Challenge-String.
- Unter **Webhook Fields** abonnieren: `messages`.

## 10. SIM-Port-Lock aktivieren

Spec §24 Threat Model: Carrier-PIN + SIM-Port-Lock gegen SIM-Swap. Beim Bot-Carrier anrufen oder im Kundenportal setzen. **Nicht vergessen**, sonst ist dein Max-Subscription-Zugang bei einem SIM-Swap-Angriff offen.

## 11. Erster Ping

Schicke vom Handy (Absender aus `allowed-senders`!) an die Bot-Nummer:

```
/ping
```

Erwartete Antwort binnen ~2 Sekunden:

```
pong · 0.1.0 · uptime 0d 0h 12m · env prod
━━━
```

Wenn das klappt: 🎉 Setup fertig.

## 12. Nächste Schritte

- `docs/CHEAT-SHEET.md` — alle Commands auf einer Seite.
- `docs/MODES.md` — wie die drei Modi funktionieren.
- `docs/RUNBOOK.md` — was zu tun ist, wenn etwas bricht.
- `/new alpha` vom Handy → dein erstes Projekt.
- Optional: `/new myapp git https://github.com/you/myapp` + `/allow batch approve`, um Smart-Detection-Regeln für Node/Python/Rust/Go/Make/Docker zu übernehmen.
