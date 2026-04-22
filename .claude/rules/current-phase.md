# Aktueller Stand

**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.4 (LaunchAgent)
**Letzter abgeschlossener Checkpoint**: C1.3 (Health-Endpoint)

## Was als Nächstes zu tun ist

C1.4 laut `phase-1.md` §10:

1. `launchd/com.DOMAIN.whatsbot.plist.template` — Bot-LaunchAgent
   - `KeepAlive` mit `SuccessfulExit: false`, `RunAtLoad: true`
   - `EnvironmentVariables` (`SSH_AUTH_SOCK`, `WHATSBOT_ENV=prod`)
   - `StandardErrorPath` / `StandardOutPath` ins Logs-Verzeichnis
   - `WorkingDirectory` auf das Repo
2. `launchd/com.DOMAIN.whatsbot.backup.plist.template` — täglich 03:00,
   ruft `bin/backup-db.sh` (Stub für jetzt, echtes Skript in C1.7)
3. `Makefile`: `deploy-launchd` rendert die Templates mit `DOMAIN=$(DOMAIN)`,
   kopiert nach `~/Library/LaunchAgents/`, `launchctl bootstrap`/`enable`/`kickstart`.
   `undeploy-launchd` umgekehrt.
4. Tests: `tests/unit/test_launchd_template.py` (Template rendert valides plist,
   alle Pflicht-Keys vorhanden, ENV-Var-Section korrekt)

Verifikation (C1.4 done):
- `make deploy-launchd DOMAIN=local`
- `launchctl list | grep whatsbot` → Bot + Backup-Agent aktiv
- `tail ~/Library/Logs/whatsbot/app.jsonl` → `startup_complete`-Event als JSON
- `make undeploy-launchd DOMAIN=local` läuft sauber

## Format-Konvention für Updates

Wenn du einen Checkpoint abschließt, update diese Datei so:

```
**Aktive Phase**: Phase 1 – Fundament + Echo-Bot
**Aktiver Checkpoint**: C1.5 (Webhook + Echo)
**Letzter abgeschlossener Checkpoint**: C1.4 (LaunchAgent)
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
