# TROUBLESHOOTING

Vom Symptom zur Ursache. Vom Handy + vom Mac aus.

## Diagnose vom Handy

| Command | Liefert |
|---|---|
| `/status` | Version, Uptime, DB-OK, aktive Sessions, Heartbeat-Alter, laufende Max-Limits |
| `/ps` | Alle Claude-Sessions mit Mode, Lock-Owner, Tokens, Turn-Count, Context-Fill |
| `/errors` | Letzte 10 WARNING/ERROR/CRITICAL aus `app.jsonl` |
| `/log <msg_id>` | Vollständiger Event-Trace für eine Message (aus Response-Header oder `/errors`) |
| `/ping` | Lebenszeichen |
| `/update` | Hinweise auf manuellen Claude-Code-Update-Prozess |

`<msg_id>` ist eine ULID, die in den Bot-Log-Events auftaucht und im `X-Correlation-Id`-Header jeder HTTP-Response.

## Diagnose am Mac

### Logs tail'en

```bash
tail -f ~/Library/Logs/whatsbot/app.jsonl | jq .
# Filter auf einen bestimmten Event-Typ:
tail -f ~/Library/Logs/whatsbot/app.jsonl | jq 'select(.event == "command_routed")'
# Filter auf Error-Level:
tail -f ~/Library/Logs/whatsbot/app.jsonl | jq 'select(.level | IN("error","warning","critical"))'
# Full-Trace einer Message:
grep '"wa_msg_id":"wamid.abc..."' ~/Library/Logs/whatsbot/app.jsonl | jq .
```

Weitere Sinks in `~/Library/Logs/whatsbot/`:

- `hook.jsonl` — Hook-Events (classify_bash / classify_write).
- `access.jsonl` — HTTP-Access-Log.
- `audit.jsonl` — Security-kritische Events (PIN, Lockdown, Panic, Mode-Switch).
- `mode-changes.jsonl` — Mode-Transition-Forensik.
- `watchdog.jsonl` — Watchdog-Entscheidungen.

### Heartbeat + LaunchAgents

```bash
stat /tmp/whatsbot-heartbeat           # sollte < 60 s alt sein
cat /tmp/whatsbot-heartbeat            # "whatsbot-heartbeat v=0.1.0 pid=<PID> ts=..."
launchctl list | grep whatsbot         # Bot + Backup + Watchdog gelistet
launchctl print gui/$UID/com.<DOMAIN>.whatsbot | grep state
```

### tmux-Sessions

```bash
tmux list-sessions | grep ^wb-         # alle Bot-Sessions
tmux attach -t wb-alpha                # live in eine Session schauen
```

### DB-Integrity

```bash
sqlite3 "$HOME/Library/Application Support/whatsbot/state.db" \
  "PRAGMA integrity_check;"            # "ok" erwartet
sqlite3 "$HOME/Library/Application Support/whatsbot/state.db" \
  ".tables"                            # alle Spec-§19-Tabellen
```

### Cloudflare Tunnel

```bash
cloudflared tunnel list
curl https://whatsbot.<deine-domain>.de/health     # ok:true
```

### Metrics (lokal)

```bash
curl http://127.0.0.1:8000/metrics | grep whatsbot_messages_total
curl http://127.0.0.1:8000/metrics | grep whatsbot_circuit_state
```

**Nicht** über den Tunnel — `/metrics` bindet nur an localhost (Spec §15).

## Häufige Symptome

### "Bot antwortet nicht"

1. Tunnel erreichbar? `curl https://<your-tunnel-url>/health` — JSON mit `ok:true`?
2. Bot-LaunchAgent läuft? `launchctl list | grep whatsbot` — PID != `-`?
3. Heartbeat frisch? `stat /tmp/whatsbot-heartbeat` — Alter < 60 s?
4. Lockdown aktiv? `cat /tmp/whatsbot-PANIC` vorhanden → `/unlock <PIN>` senden.
5. Circuit-Breaker offen? `/errors` zeigt `circuit_opened`?

Wenn 1-4 OK aber Bot antwortet nicht: Logs auf `signature_invalid` oder `sender_not_allowed` prüfen — hast du das richtige App-Secret / die richtige Nummer in `allowed-senders`?

### "Prompt läuft nicht durch"

1. Aktives Projekt gesetzt? `/p` → "aktives Projekt: ▶ ..."?
2. Lock-Owner prüfen: `/ps` → Lock auf `🤖 BOT` oder `— FREE`? Bei `👤 LOCAL`: Terminal pausieren oder `/force <name> <PIN> <prompt>`.
3. Max-Limit aktiv? `/status` → "⏸ Max-Limit erreicht [session_5h] · Reset in 3h 22m"?
4. Mode-Switch fehlgeschlagen? `/mode` zeigt den falschen Modus?

### "Output wird in Stücken gesendet"

Das ist die Spec-§10 Size-Pipeline: Outputs > 10 KB lösen eine Bestätigungs-Rückfrage aus.

```
⚠️ Claude will ~15KB senden (15234 chars).
/send    – senden
/discard – verwerfen
/save    – nur speichern, nicht senden
```

Später `/cat <timestamp>` zum Abrufen.

### "Claude schlägt beim Tool-Call fehl"

Pre-Tool-Hook hat geblockt. Check `~/Library/Logs/whatsbot/hook.jsonl`:

- `deny_pattern_matched` → einer der 17 hard-denies (Spec §12). Command wirklich so gefährlich, oder falsches Pattern? Edit `hooks/pre_tool.py` mit Vorsicht.
- `ask_user_pending` → PIN-Rückfrage offen, Handy prüfen.
- `circuit_open` → externer Service down, erst Circuit-Breaker nachziehen.

### "Transkription fehlgeschlagen"

- `which whisper-cli` → Binary vorhanden?
- `ls ~/Library/whisper-cpp/models/ggml-small.bin` → Modell da? Siehe INSTALL §2.
- `~/Library/Logs/whatsbot/app.jsonl | jq 'select(.event=="audio_transcription_failed")'` → Fehlermeldung.
- Fallback: Bitte den User, als Text zu schicken.

### "DB-Backup schlägt fehl"

```bash
bin/backup-db.sh
# Erwartet: "Backup successful to ~/Backups/whatsbot/state.db.<YYYY-MM-DD>"
```

Wenn das an Permissions scheitert: `chmod 755 bin/backup-db.sh` und Backup-LaunchAgent neu deployen.

### "Meta-Webhook-Signature-Check scheitert"

Check im Log auf `signature_invalid`. Häufige Ursachen:

- Cloudflare-Tunnel macht ein Re-Encoding — stell sicher, dass kein Response-Rewrite aktiv ist.
- App-Secret wurde rotiert in Meta-Console aber nicht im Keychain (RUNBOOK #7).
- Payload-Body wurde verändert (Proxy / Middleware) — unser Check nutzt den raw Body.

### "tmux-Session startet nicht"

1. `which tmux` → installiert?
2. `claude --version` → installiert? Subscription?
3. `bin/safe-claude echo ok` am Mac direkt — Environment sauber?
4. `tail -f ~/Library/Logs/whatsbot/app.jsonl | jq 'select(.event|test("session|tmux"))'` während du `/p <name>` schickst.

## Wenn nichts hilft

1. `/panic` vom Handy — killt alles, geht in Lockdown. Spec §7.
2. Am Mac: `launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot`.
3. DB-Reset (NUKE, **löscht Projekt-Metadaten**): `make reset-db`.
4. Rollback auf letzten bekannten-guten Commit (siehe RUNBOOK).

Bug reports: Issue mit `msg_id` + relevantem `app.jsonl`-Snippet.
