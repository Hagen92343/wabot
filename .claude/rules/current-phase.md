# Aktueller Stand

**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.8 (Tests grün + finale Phase-2-Verifikation, Live-Smoke)
**Letzter abgeschlossener Checkpoint**: C2.7 (`/rm` + PIN + Trash)

## Phase-2-Fortschritt: 6/8 Checkpoints

- ✅ C2.1 — `/new <name>` empty + `/ls`
- ✅ C2.2 — `/new <name> git <url>` + URL-Whitelist + Smart-Detection-Stub
- ✅ C2.3 — Smart-Detection für alle 9 Artefakt-Stacks
- ✅ C2.4 — `/allow batch approve` + `/allow batch review`
- ✅ C2.5 — `/allow <pat>` + `/deny <pat>` + `/allowlist` + `/p`/`/p <name>`
       (zusammen mit C2.4 abgeschlossen)
- ⏳ C2.6 — URL-Whitelist Tests (schon in C2.2 voll abgedeckt; zieht sich
       in C2.8-Verifikation mit)
- ✅ C2.7 — `/rm <n>` mit 60s-Confirm + PIN + Trash
- ⏳ C2.8 — Tests grün + finale Phase-2-Verifikation (inkl. Live-Smoke)

## Was als Nächstes zu tun ist (C2.8)

Finale Phase-2-Verifikation:

1. **`make test` komplett grün**: aktueller Stand 373/373 passing,
   mypy --strict clean, ruff format/lint OK.
2. **Coverage Domain-Core prüfen**: `make test-coverage` → Ziel >80%.
3. **Live-Smoke** gegen den laufenden LaunchAgent:
   - `/new smoketest` → angelegt
   - `/new smokegit git https://github.com/octocat/Hello-World` → geklont
     mit Smart-Detection-Vorschlägen
   - `/allow batch approve` gegen smokegit
   - `/p smokegit`, `/allow Bash(echo hi)`, `/allowlist`, `/deny Bash(echo hi)`
   - `/rm smoketest` → 60s-Prompt → `/rm smoketest <PIN>` → in Trash
   - Falsche PIN testen (muss Pending-Row erhalten)
   - 70s warten → `/rm smokegit <PIN>` → muss "abgelaufen" liefern
4. **`CHANGELOG.md` finalisieren** mit Phase-2-Abschluss-Eintrag.
5. **Commit**: `feat(phase-2): complete phase 2`.
6. **Warten auf User-Freigabe** bevor Phase 3 beginnt.

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 3 — Security-Core
**Aktiver Checkpoint**: C3.1 (Hook-Script + Shared-Secret-IPC)
**Letzter abgeschlossener Checkpoint**: C2.8 (Phase-2-Verifikation)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-2.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
