# Aktueller Stand

**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.4 (`/allow batch approve` + `/allow batch review` + `/allow`/`/deny`/`/allowlist`)
**Letzter abgeschlossener Checkpoint**: C2.3 (Smart-Detection für 9 Artefakt-Stacks)

## Phase-2-Fortschritt: 3/8 Checkpoints

- ✅ C2.1 — `/new <name>` empty + `/ls`
- ✅ C2.2 — `/new <name> git <url>` + URL-Whitelist + Smart-Detection-Stub
- ✅ C2.3 — Smart-Detection für alle 9 Artefakt-Stacks
- ⏳ C2.4 — Allow-Rule-Management (folgt jetzt)
- ⏳ C2.5 — `/allow` / `/deny` / `/allowlist` manuell
- ⏳ C2.6 — URL-Whitelist Tests (eigentlich schon in C2.2 abgedeckt — evtl. zusammenfassen)
- ⏳ C2.7 — `/rm <n>` mit PIN + Trash
- ⏳ C2.8 — Tests grün

## Was als Nächstes zu tun ist (C2.4)

C2.4 laut `phase-2.md` "Allow-Rule-Management":

1. `domain/allow_rules.py` — pure Logic
   - `parse_pattern("Bash(npm test)") → AllowRule(tool="Bash", pattern="npm test")`
   - Validierung: Tool aus erlaubter Liste (Bash, Write, Edit, Read, Grep, Glob),
     pattern non-empty, kein nested ()
2. `ports/allow_rule_repository.py` + sqlite-adapter
   - Persistiert in `allow_rules`-Tabelle (Spec §19)
   - source: 'default', 'smart_detection', 'manual'
3. `application/allow_service.py`
   - `add_rule(project, tool, pattern, source)` — DB + .claude/settings.json sync
   - `remove_rule(...)` — analog
   - `list_rules(project)` — gruppiert nach source
   - `apply_suggested(project)` — liest .whatsbot/suggested-rules.json,
     ruft add_rule für jede, löscht die Vorschlags-Datei
4. `command_handler.py`:
   - `/allow batch approve` (per active project)
   - `/allow batch review` — listet Vorschläge mit numbers
   - `/allowlist` — zeigt aktuelle Liste
5. Voraussetzung: Active-Project-Tracking (`/p <name>`) — ein leichter
   Vorgriff aus C2.5, weil Allow-Rules per Projekt sind. Lege ich
   minimal in C2.4 mit an: `app_state` Row `active_project`,
   `/p <n>` und `/p` (zeigt aktives) Commands.

Verifikation (C2.4 done):
- `/new alpha git https://github.com/octocat/Hello-World` → Vorschläge
- `/p alpha` → setzt aktiv
- `/allow batch review` → zeigt 7 Vorschläge nummeriert
- `/allow batch approve` → schreibt Rules, löscht suggested-rules.json
- `~/projekte/alpha/.claude/settings.json` enthält permissions.allow Array
- `/allowlist` → 7 Einträge gruppiert nach `smart_detection`

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.5 (`/allow`/`/deny`/`/allowlist` manuell)
**Letzter abgeschlossener Checkpoint**: C2.4 (Allow-Rule-Management batch)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-2.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
