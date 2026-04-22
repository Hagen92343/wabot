# Aktueller Stand

**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.3 (Smart-Detection für 9 Artefakt-Types)
**Letzter abgeschlossener Checkpoint**: C2.2 (`/new git` + URL-Whitelist)

## Was C2.2 geliefert hat

- URL-Whitelist (3 Hosts × 3 Schemas) mit fail-closed gegen alles andere
- `GitClone` Port + `SubprocessGitClone` Adapter (`git clone --depth 50
  --quiet`)
- Post-Clone Scaffolding: `.claudeignore`, `.whatsbot/config.json`,
  `CLAUDE.md` (nur wenn upstream keines hat), `suggested-rules.json`
- Smart-Detection-Skelett (`package.json` + `.git`)
- `/new <name> git <url>` aktiv im CommandHandler
- 59 neue Tests (260 total), Coverage 95.09%
- Live-verifiziert mit `octocat/Hello-World` Clone

## Was als Nächstes zu tun ist (C2.3)

C2.3 laut `phase-2.md` Smart-Detection-Tabelle: alle 9 Artefakt-Types.
Aktuell sind 2 von 9 implementiert (`package.json`, `.git`). Hinzu:

| Datei | Rules |
|-------|-------|
| `yarn.lock` | `Bash(yarn *)`, `Bash(yarn install)`, `Bash(yarn test)` |
| `pnpm-lock.yaml` | `Bash(pnpm *)`, `Bash(pnpm install)` |
| `pyproject.toml` | `Bash(uv *)`, `Bash(pytest)`, `Bash(python -m *)`, `Bash(ruff *)`, `Bash(mypy *)` |
| `requirements.txt` | `Bash(pip install -r requirements.txt)`, `Bash(python -m *)`, `Bash(pytest)` |
| `Cargo.toml` | `Bash(cargo build)`, `Bash(cargo test)`, `Bash(cargo check)`, `Bash(cargo clippy)`, `Bash(cargo fmt)` |
| `go.mod` | `Bash(go build)`, `Bash(go test ./*)`, `Bash(go run *)`, `Bash(go mod tidy)` |
| `Makefile` | `Bash(make *)` |
| `docker-compose.yml`/`.yaml` | `Bash(docker compose ps)`, `Bash(docker compose logs *)`, `Bash(docker compose up -d)`, `Bash(docker compose down)` |

Verifikation (C2.3 done):
- Tests für alle 9 Artefakte (auch Combo-Cases, z.B. Cargo + Makefile)
- Manuelle Smoke gegen 5 echte Repos pro Stack (oder simulierte Layouts)
- `/allow batch approve` schreibt die Rules in `.claude/settings.json`
  (kommt eigentlich erst in C2.4 — aber der Detection-Output wird hier
  verifiziert)

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 2 — Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.4 (`/allow batch approve` + `/allow batch review`)
**Letzter abgeschlossener Checkpoint**: C2.3 (Smart-Detection 9 Artefakte)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-2.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
