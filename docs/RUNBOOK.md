# RUNBOOK

Betriebsanleitung für den whatsbot. Enthält alle Recovery-Playbooks aus Spec §23, Secret-Rotation, Updates.

## Täglicher Betrieb

- Der Bot läuft als User-LaunchAgent unter deiner UID — kein Root, kein separater User.
- Drei LaunchAgents: **Bot** (stetig), **DB-Backup** (03:00 täglich), **Watchdog** (alle 30 s).
- Logs sind JSONL in `~/Library/Logs/whatsbot/` — `app.jsonl` ist dein primärer Einstieg.
- `/ps` vom Handy zeigt laufende Claude-Sessions; `/errors` zeigt die letzten 10 Warnings/Errors.

## Recovery-Playbooks

### 1. Mac-Crash während Pending Confirmation

**Symptom**: User hatte eine PIN-Rückfrage offen, Mac ist abgestürzt. Nach Reboot wartet niemand mehr.

**Fix**: `StartupRecovery` räumt `pending_confirmations` mit abgelaufener `deadline_ts` automatisch. Keine Aktion nötig. Bei Bedarf manuelle User-Info: "Der vorige PIN-Request ist verfallen. Schick den Command nochmal."

### 2. Claude-Code-Update hat `--resume` gebrochen

**Symptom**: Nach `/update` schlägt `claude --resume <id>` fehl mit "Session not found". Bot loggt `fresh_session_fallback`.

**Fix**: Automatisch — `SessionService` fällt auf eine frische Session zurück, CLAUDE.md bleibt, die Allow-Rules auch. Der User sieht eine Warnung "Context verloren — neue Session".

Wenn das mehrfach passiert: gegen den Claude-Code-Release-Channel gewechselt (`claude /update` manuell) und prüfen, ob ein bekannter Bug dokumentiert ist.

### 3. Meta-API-Outage

**Symptom**: `/errors` zeigt `circuit_opened` für `meta_send` oder `meta_media`. Outbound-Nachrichten kommen nicht an.

**Fix**: Der Circuit-Breaker (Phase 8 C8.3) blockt den externen Call nach 5 Fehlern in 60 s für 5 Minuten. Nach Cool-Down startet ein Probe-Call automatisch. Kein manueller Eingriff nötig. Wenn Meta länger down ist, WhatsApp queued eingehende Messages 24 h clientseitig.

Verifikation: `/status` zeigt den Circuit-State. In `/metrics` (nur lokal): `whatsbot_circuit_state{service="meta_send",state="open"} 1`.

### 4. tmux-Server OOM-killed

**Symptom**: Alle tmux-Sessions weg. `tmux ls` gibt "no server running".

**Fix**: Der Watchdog bemerkt den Heartbeat-Gap und kill't den Bot-Prozess. LaunchAgent startet neu, `StartupRecovery` liest `claude_sessions`, spawn't tmux-Sessions frisch mit `safe-claude --resume <id>`. Context bleibt über die Transcript-JSONL erhalten.

Wenn die Sessions nicht zurückkommen: `launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot` manuell triggern.

### 5. Hook-Script Syntax-Error nach Update

**Symptom**: Claude blockiert *alle* Bash/Write/Edit-Calls. Bot-Log voll von `hook_endpoint_unreachable`.

**Fix**: Fail-closed ist Absicht (Spec §7). Fehler manuell fixen:

```bash
python3 ~/whatsbot/hooks/pre_tool.py < /dev/null   # führt Import aus
```

Stacktrace zeigt die kaputte Stelle. Nach Fix `launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot`.

### 6. PIN vergessen

**Symptom**: `/rm`, `/force`, `/unlock` scheitern mit "Falsche PIN".

**Fix** am Mac:

```bash
security add-generic-password -U -s whatsbot -a panic-pin -w
# Prompt: neue PIN eingeben, z.B. "1234"
```

Kein Bot-Restart nötig — der Secrets-Provider liest beim nächsten Command frisch.

### 7. Meta-App-Secret geleakt

**Symptom**: Du hast das App-Secret versehentlich committed / getwittert.

**Fix**:

1. In der Meta-Dev-Konsole **App Dashboard → Settings → Basic → App Secret → Show → Reset**.
2. Neuen Wert ins Keychain:
   ```bash
   security add-generic-password -U -s whatsbot -a meta-app-secret -w
   ```
3. LaunchAgent reload:
   ```bash
   launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
   ```
4. Prüfen: `tail -f ~/Library/Logs/whatsbot/app.jsonl` — nächste Signature-Verifikation muss grün durchlaufen.

### 8. DB corrupt

**Symptom**: Bot startet nicht, Log zeigt `PRAGMA integrity_check` failed.

**Fix**: Auto-Restore aus letztem Backup:

```bash
ls ~/Backups/whatsbot/
# state.db.2026-04-22, state.db.2026-04-21, ...
cp ~/Backups/whatsbot/state.db.<yesterday> \
   "$HOME/Library/Application Support/whatsbot/state.db"
launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
```

Komplettes Zurücksetzen (Notfall, **löscht alle Projekte**):

```bash
make reset-db
```

### 9. Laptop-Sleep mitten in Session

**Symptom**: Laptop war 8 h zugeklappt, Bot loggt riesigen Heartbeat-Gap nach Wake.

**Fix**: Der Watchdog hat mehrere Grace-Fallbacks (Phase 6 C6.5): PID-Liveness-Check (falls Bot-Prozess lebt, nicht kill'en), System-Uptime-Check (falls erst kürzlich gebootet, nicht kill'en). Wenn Bot lebt + Heartbeat steht: sleep-Artefakt, nichts passiert.

Wenn der Bot nach Wake nicht mehr antwortet: manuell `launchctl kickstart`.

## Secret-Rotation

Einheitliches Pattern für alle 7 Keychain-Einträge:

```bash
security add-generic-password -U -s whatsbot -a <name> -w
# Prompt: neuen Wert eingeben
launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
```

`<name>` ist einer von: `meta-app-secret`, `meta-verify-token`, `meta-access-token`, `meta-phone-number-id`, `allowed-senders`, `panic-pin`, `hook-shared-secret`.

## Updates

### Bot-Code

```bash
cd ~/whatsbot
git pull
make install
launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
```

Schema-Migrations laufen automatisch — `sqlite_repo.apply_schema` ist idempotent. Neue Felder werden als `ALTER TABLE` nachgezogen.

### Claude Code

Spec §5's vierfacher Subscription-Lock erlaubt keine Auto-Updates. Manuell:

```bash
claude /update
claude /status   # "subscription" prüfen, nicht "API"
launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
```

Siehe Recovery-Playbook #2 falls `--resume` nach Update Sessions verwirft.

### Python-Dependencies

```bash
cd ~/whatsbot
source venv/bin/activate
pip install -r requirements.txt --upgrade
make test   # alles grün?
launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
```

## Rollback

```bash
cd ~/whatsbot
git log --oneline -10         # letzten bekannten-guten Commit finden
git checkout <hash>
make install
launchctl kickstart -k gui/$UID/com.<DOMAIN>.whatsbot
```

DB-Rollback analog über `~/Backups/whatsbot/state.db.<tag>`.

## Deinstallation

```bash
make undeploy-launchd
rm -rf ~/whatsbot
# Secrets im Keychain manuell über Keychain-Access löschen, oder:
for k in meta-app-secret meta-verify-token meta-access-token \
         meta-phone-number-id allowed-senders panic-pin hook-shared-secret; do
  security delete-generic-password -s whatsbot -a $k
done
```

DB und Backups bleiben in `~/Library/Application Support/whatsbot/` und `~/Backups/whatsbot/` — manuell löschen, falls gewünscht.
