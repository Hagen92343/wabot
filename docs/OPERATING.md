# OPERATING — Tag-zu-Tag-Betrieb

Dieser Guide ist für den Alltag *nach* dem Live-Deployment (Phase 1-10
abgeschlossen, Bot antwortet vom Handy aus). Er beantwortet die Fragen,
die beim produktiven Betrieb auftauchen: Mac-Setup, Wo-liegen-Projekte,
Git-Workflow, Live-Monitoring.

Für Installation: siehe [INSTALL.md](INSTALL.md).
Für Recovery bei Ausfällen: siehe [RUNBOOK.md](RUNBOOK.md).
Für Symptom-Diagnose: siehe [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## 1. Der Bot läuft auf deinem Mac

**Mental Model**: Der Bot ist nicht in der Cloud, nicht auf einem
Server, nicht bei Anthropic. Er läuft als `launchd`-User-LaunchAgent
auf genau dem Mac, auf dem du ihn installiert hast, und bindet
`127.0.0.1:8000`. Cloudflare Tunnel macht diesen lokalen Port für
den Meta-Webhook öffentlich erreichbar (`bot.lhconsulting.services`
→ `http://127.0.0.1:8000`).

Alles was der Bot tut, passiert **auf deiner Festplatte**. Neue
Projekte, Claude-Sessions, Logs, DB — alles lokal.

## 2. Mac-Vorbedingungen für produktiven Betrieb

### 2.1 Mac darf nicht schlafen

Der wichtigste Punkt. Wenn der Mac schläft, schläft der Bot mit —
`/ping`s vom Handy kommen dann erst durch, wenn du den Mac aufweckst
(Meta queued die Message 24 h, also geht nichts dauerhaft verloren,
aber "live" ist anders).

Zwei Strategien:

**Option A — Schlaf am Netzteil verhindern** (pragmatisch, wenn
MacBook meist am Schreibtisch hängt):

```
Systemeinstellungen → Batterie → Netzteil →
  "Ruhezustand bei ausgeschaltetem Display verhindern"  ✓
```

**Option B — Dedizierter Mac Mini**: ein kleiner M1/M2 Mac Mini,
Ethernet, dauerhaft an, als reiner Bot-Host. Für den kompromisslosen
24/7-Betrieb.

Die Phase-6-Sleep-Awareness (pmset-Monitor + Watchdog-Grace) verhindert
nur Watchdog-Fehlalarme beim Sleep. Sie macht den Bot nicht
"funktional im Schlaf".

### 2.2 Auto-Login aktivieren

Der Bot läuft als **User-LaunchAgent** (nicht System-Daemon). Er
startet erst, wenn du dich einloggst. Heißt: nach Reboot ohne Login
läuft der Bot nicht.

```
Systemeinstellungen → Benutzer:innen & Gruppen →
  Automatische Anmeldung  ✓  (dein Benutzer wählen)
```

Nur aktivieren, wenn der Mac physisch sicher steht. Nicht im Café.

### 2.3 Notification-Permission für macOS-Alerts

Der Bot feuert macOS-Notifications bei `/panic`, Watchdog-Aktionen
und Tunnel-Ausfällen. Damit sie sichtbar sind:

```
Systemeinstellungen → Mitteilungen → Script Editor / osascript
  "Mitteilungen zulassen"  ✓
```

Beim ersten Panic fragt macOS einmal — kannst du vorab erlauben.

### 2.4 Reboot-Recovery einmalig testen

Bevor du dich auf das Setup verlässt, einmal den kompletten Reboot
durchspielen:

```bash
sudo reboot
# Einloggen (oder Auto-Login)
# Dann prüfen:
launchctl print gui/$UID/com.local.whatsbot | grep state
sudo launchctl list | grep cloudflared
curl -s https://bot.lhconsulting.services/health
```

Alle drei sollten grün sein. Dann vom Handy `/ping` → muss durchkommen.
Das ist der Reboot-Recovery-Invariante aus Phase 4.

### 2.5 SIM-Port-Lock beim Carrier

Nicht Mac, aber Teil der produktiven Härtung: Bei deinem Mobilfunk-
Provider der Bot-SIM einen Carrier-PIN setzen, der SIM-Swap blockiert.
Spec §24 (STRIDE-Threat-Model) besteht darauf vor dem als "produktiv"
eingestuften Betrieb. Anruf oder App beim Anbieter, PIN setzen, fertig.

---

## 3. Wo liegen meine Projekte?

**Nicht auf dem Desktop**. Sondern unter:

```
~/projekte/<name>/
```

Das ist Spec §4 und hardcoded. Grund: `~/Desktop`, `~/Documents`,
`~/Downloads` sind auf macOS TCC-protected (iCloud-Sync,
Sandbox-Barrieren). `~/projekte/` ist ein sauberer nicht-geschützter
Pfad.

Pro Projekt existieren:

```
~/projekte/<name>/
├── <dein code>                        # aus Git-Clone oder frisch
├── .claude/
│   └── settings.json                  # Allow/Deny + Pre-Tool-Hook
├── .claudeignore                      # blockt .env, *.pem, ~/.ssh, …
├── .whatsbot/
│   ├── config.json                    # Projekt-Metadaten
│   ├── outputs/<timestamp>.md         # lange Claude-Outputs (>10 KB)
│   └── suggested-rules.json           # Smart-Detection-Vorschläge
└── CLAUDE.md                          # Projekt-Instruction für Claude
```

Die WhatsApp-`/ls`-Liste zieht alle Namen aus der zentralen SQLite:

```
~/Library/Application Support/whatsbot/state.db
```

Anschauen im Finder:

```bash
open ~/projekte/
```

---

## 4. Bestehende Projekte an den Bot anhängen

Ab Phase 11: `/import` hängt einen existierenden Ordner als Projekt
an, ohne ihn zu verschieben oder zu kopieren.

```
/import <name> <absoluter-pfad>
```

Beispiel für den whatsbot-Repo selbst:

```
/import wabot /Users/hagenmarggraf/whatsbot
```

Was passiert:
1. DB-Row wird angelegt mit `source_mode=imported` und dem echten
   Pfad.
2. Der Bot legt in den Ordner fehlende dotfiles ab: `.whatsbot/`,
   `.whatsbot/outputs/`, `CLAUDE.md`, `.claudeignore`. **Bereits
   vorhandene Dateien werden nicht überschrieben** — du siehst im
   Reply eine Liste "Unverändert gelassen".
3. Smart-Detection läuft und schlägt Allow-Rules vor (per
   `/allow batch approve` übernehmen).
4. Die Pfad-Rules des Pre-Tool-Hooks erkennen den Ordner jetzt als
   "Projekt-Scope" — Writes da drin laufen wie bei `/new`-Projekten
   ohne PIN-Rückfragen.

`/rm <name>` für importierte Projekte entfernt nur die DB-Zeile,
der Ordner bleibt unangetastet (wir haben ihn ja nicht angelegt).

### Einschränkungen

- **Geschützte Pfade werden abgelehnt**: `~/Library`, `~/.ssh`,
  `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.1password`, `/etc`,
  `/System`, `/Library`, `/usr`, `/bin`, `/sbin`.
- **TCC-Warnung**: bei Pfaden unter `~/Desktop`, `~/Documents`,
  `~/Downloads`, `~/Pictures`, `~/Movies`, `~/Music` akzeptiert der
  Bot den Import mit Warnhinweis — macOS TCC-Protection kann trotz
  allem Writes blockieren. Lösung: LaunchAgent-Binary via
  Systemeinstellungen → Datenschutz & Sicherheit → "Vollständiger
  Festplattenzugriff" freigeben.
- **Doppelte Registrierung** (gleicher Name oder gleicher Pfad) wird
  erkannt und abgelehnt.
- **`/new` bleibt für neue Projekte**: Frisches leeres Projekt oder
  Git-Clone landen weiterhin in `~/projekte/<name>`.

### Wenn du ohne Import arbeiten willst

Alternative: neue Bot-Projekte per `/new` in `~/projekte/` anlegen
und dort arbeiten. Alte Ordner im Desktop / Documents lässt du wo
sie sind und arbeitest dort klassisch im Terminal / Cursor / VS
Code weiter. Keine Verwirrung, keine doppelten Arbeitskopien.

---

## 5. Git-Workflow — wie kommen Bot-Änderungen auf GitHub

Weil der Bot lokal läuft, sind Claude-Änderungen ab der ersten
Sekunde physisch auf deiner Platte. Für die "auf GitHub kommen"-
Frage: `git push` ist der einzige Übertragungsweg.

### Variante 1 — Claude pusht selbst

Vom Handy einmalig die Allow-Rule setzen:

```
/p foo /allow "Bash(git push)"
```

Danach kann Claude vom Handy-Prompt aus selbst committen + pushen:

```
/p foo commit alle Änderungen und pushe sie
```

Claude führt `git add -A && git commit && git push` aus. Änderungen
sind auf GitHub, aktualisierst du dann auf anderen Devices per
`git pull`.

**Was bewusst NICHT erlaubt ist**:

- `Bash(git push --force*)` steht in der Deny-Liste, auch in YOLO
  (Spec §12). Kein versehentlicher History-Rewrite.
- CLAUDE.md-Template enthält: "Never push to main/master without
  explicit instruction". Claude bittet explizit um Bestätigung, wenn
  der aktuelle Branch main/master ist.

### Variante 2 — Du pushst manuell

```bash
cd ~/projekte/foo
git status
git diff
git add -A
git commit -m "…"
git push
```

Vorteil: volle Kontrolle über Commit-Struktur.

### Variante 3 — GitHub als Sync-Hub

Arbeitest du auf mehreren Macs oder parallel mit Desktop-Clone +
Bot-Clone, lass GitHub der "source of truth" sein:

1. Bot-Clone pusht (Variante 1 oder 2).
2. Auf deinem anderen Device: `git pull` im jeweiligen Clone.

Kein Sync-Gefuddel, standard Git-Workflow.

---

## 6. Live-Monitoring vom Mac aus

### 6.1 Status-Snapshot

```bash
# Bot-LaunchAgent läuft?
launchctl print gui/$UID/com.local.whatsbot | grep -E "state|pid" | head -5

# Cloudflare Tunnel läuft (als root)?
sudo launchctl list | grep cloudflared

# Bot antwortet lokal?
curl -s http://127.0.0.1:8000/health

# Heartbeat frisch (älter als 2 min = Bot hängt)?
stat -f "mtime: %Sm" /tmp/whatsbot-heartbeat

# Aktive Claude-Sessions?
tmux ls | grep ^wb-
```

### 6.2 Live-Tail der Bot-Events

Zweites Terminal-Fenster, einer der folgenden:

**Lesbar, alle Events:**

```bash
tail -f ~/Library/Logs/whatsbot/app.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line)
        ts = e.get('ts','')[11:19]
        lvl = e.get('level','').upper()[:4]
        ev = e.get('event','-')
        extra = {k:v for k,v in e.items()
                 if k not in ('ts','level','event','logger','msg_id','wa_msg_id','sender')}
        print(f'{ts} [{lvl}] {ev}  {extra}', flush=True)
    except: pass
"
```

**Nur relevante Events (simpler):**

```bash
tail -f ~/Library/Logs/whatsbot/app.jsonl | \
  grep --line-buffered -E "command_routed|outbound_|circuit_|error|panic_|mode_switch"
```

### 6.3 Prometheus-Metrics

```bash
curl -s http://127.0.0.1:8000/metrics | head -60
```

Interessante Serien:
- `whatsbot_messages_total{direction="in",kind="text"}`
- `whatsbot_messages_total{direction="out",kind="text"}`
- `whatsbot_response_latency_seconds_*`
- `whatsbot_circuit_state{service="meta_send",state="closed"}`
- `whatsbot_session_active_gauge`
- `whatsbot_hook_decisions_total{tool,decision}`

### 6.4 DB-Blick

```bash
sqlite3 ~/Library/Application\ Support/whatsbot/state.db \
  "SELECT name, mode, last_used_at FROM projects ORDER BY last_used_at DESC;"

sqlite3 ~/Library/Application\ Support/whatsbot/state.db \
  "SELECT project_name, owner, datetime(acquired_at,'unixepoch') FROM session_locks;"

sqlite3 ~/Library/Application\ Support/whatsbot/state.db \
  "SELECT kind, datetime(reset_at_ts,'unixepoch'), remaining_pct FROM max_limits;"
```

---

## 7. Live-Monitoring vom Handy aus

Wenn du unterwegs keinen Mac-Zugriff hast:

| Command | Zeigt |
|---|---|
| `/status` | System-Überblick (Uptime, DB, Sessions, Limits, Lockdown-State) |
| `/ps` | Aktive Claude-Sessions (Mode, Lock-Owner, Tokens, Turns) |
| `/errors` | Letzte 10 WARNING/ERROR-Events |
| `/log <msg_id>` | Voller Event-Trace einer bestimmten Nachricht |
| `/metrics` | WhatsApp-Tages-Digest |

`msg_id` kriegst du aus jedem Bot-Footer oder aus der Antwort auf
deine eigene Message.

---

## 8. Backups

Die DB wird täglich 03:00 via separatem LaunchAgent nach
`~/Backups/whatsbot/state.db.<datum>` gesichert, mit 30 Tage
Retention. Check:

```bash
ls -lt ~/Backups/whatsbot/ | head -10
```

Nach ein paar Tagen Betrieb solltest du dort mehrere Einträge sehen.

Medien-Cache (`~/Library/Caches/whatsbot/media/`) wird nach 7 Tagen
mit Secure-Delete (Nullen überschreiben, dann unlink) aufgeräumt.

Repo selbst: push regelmäßig nach GitHub (`git push origin main`).

---

## 9. Wenn etwas nicht funktioniert

1. **`/status` vom Handy** → erster Check, meist sprechend.
2. **`~/Library/Logs/whatsbot/app.jsonl`** → strukturierte Events.
3. **`docs/TROUBLESHOOTING.md`** → häufige Symptome + Fix.
4. **`docs/RUNBOOK.md`** → alle 9 Recovery-Playbooks (Bot-Crash,
   Meta-Outage, Claude-Update bricht `--resume`, etc.).
5. **Bot neu starten**:
   ```bash
   launchctl kickstart -k gui/$UID/com.local.whatsbot
   ```

Bot-Restart ist die schärfste Waffe: löst OPEN Circuit-Breaker,
bringt Pumper + Sweeper neu, re-loadet Secrets aus Keychain.

---

## 10. Bekannte Follow-ups (nicht-blockierend)

Stand 2026-04-24, eine bewusst kleine Liste:

- **Whitelist-Normalisierung**: `domain/whitelist.py::is_allowed`
  macht exakten String-Match. Meta liefert Absender ohne `+`
  (`491716598519`). Der Keychain-Eintrag `allowed-senders` muss
  deshalb auch ohne `+` gesetzt sein. Entweder Dokstring + INSTALL
  auf "ohne +" festzurren oder beidseitig `.lstrip('+')`.
- **Meta Delivery-Status-Tracking**: Meta schickt `statuses`-Events
  (sent/delivered/read). Aktuell routen wir nur `messages`. Nice-
  to-have für ein "hat der User gelesen"-Anzeiger.
- **Token-Rotation**: Der aktuelle Meta-System-User-Token ist "Never
  expires", aber Meta kann ihn bei App-Permission-Änderungen
  revozieren. Bei plötzlichen 401-Serien: siehe RUNBOOK
  §Secret-Rotation, Token regenerieren, Keychain-Update,
  `launchctl kickstart`.

Keiner davon blockiert den Produktivbetrieb. Entscheidung offen
bis sie konkret weh tun.
