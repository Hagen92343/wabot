# MODES

Drei Modi pro Projekt, jederzeit umschaltbar. Spec §6.

## Überblick

| Modus | Claude-Start-Flags | Verhalten | Default für |
|---|---|---|---|
| 🟢 **Normal** | `--permission-mode default` | Allow-Rules pre-approved, Hook als Zusatz-Layer, Rückfrage bei Ungewöhnlichem | Neue Projekte |
| 🔵 **Strict** | `--permission-mode dontAsk` | Nur Allow-List läuft, alles andere auto-denied **silent** | Sensitive Projekte |
| 🔴 **YOLO** | `--dangerously-skip-permissions` | Alle Permission-Prompts weg, nur Hook blockt, `.git` / `.claude` / `.vscode` / `.idea` trotzdem protected | Autonome Runs (manuell) |

Status-Bar in tmux zeigt den Modus farblich:

- `[🟢 NORMAL] · 🤖 BOT [wb-alpha]`
- `[🔵 STRICT] · — FREE [wb-api]`
- `[🔴 YOLO] · — FREE [wb-experiment]` (rote Status-Leiste)

## Wechsel

Vom Handy:

```
/mode              → zeigt aktuellen Modus
/mode normal
/mode strict
/mode yolo
```

Jeder Switch recycled die tmux-Session: kill → neu starten mit passenden Flags → `claude --resume <session-id>` bewahrt Context.

**Kein PIN** für Mode-Switch (Spec §26 Schwäche #1 — bewusste Entscheidung).

## Default-Verhalten

- **Neue Projekte**: Normal.
- **Reboot-Reset**: Alle YOLO-Projekte werden beim Bot-Start auf Normal zurückgesetzt (Spec §6 Invariante). Mode-Event `reboot_reset` landet im Audit-Log.
- **`/panic`**: setzt ebenfalls alle YOLOs auf Normal. Mode-Event `panic_reset`.
- **Strict-Escape**: Kein automatischer Escape-Hatch. Bei Bedarf `/mode normal` → prompt → `/mode strict`.

## Smart-Detection bei `/new git`

Sobald du ein Repo klonst, scannt der Bot die Artefakte und schlägt Allow-Rules vor:

| Artefakt | Vorschläge |
|---|---|
| `package.json` | `Bash(npm test)`, `Bash(npm run *)`, `Bash(npm install)`, `Bash(npx *)` |
| `pyproject.toml` | `Bash(pytest)`, `Bash(uv *)`, `Bash(ruff *)`, `Bash(python -m *)` |
| `Cargo.toml` | `Bash(cargo build)`, `Bash(cargo test)`, `Bash(cargo check)` |
| `go.mod` | `Bash(go build)`, `Bash(go test)`, `Bash(go run *)` |
| `Makefile` | `Bash(make *)` |
| `docker-compose.yml` | `Bash(docker compose ps)`, `Bash(docker compose logs *)` |
| `.git/` | `Bash(git status)`, `Bash(git diff *)`, `Bash(git log *)`, `Bash(git branch *)` |

WhatsApp-Flow:

```
/new myapp git https://github.com/you/myapp
→ "✅ Geklont. 12 Rule-Vorschläge aus package.json, .git."
/allow batch review      → zeigt alle einzeln
/allow batch approve     → übernimmt alle
```

## FAQ

### Warum kein PIN auf `/mode yolo`?

Explizite User-Entscheidung (§26 Schwäche #1). Begründung: Carrier-PIN + SIM-Port-Lock + separate Bot-SIM reduzieren das Risiko; im Ernstfall ist das Handy eh ungesperrt, wenn es jemand hat. Minimalismus schlägt Theater.

Was trotzdem schützt, selbst in YOLO:
- Deny-Patterns (17 Muster)
- Output-Redaction (Secrets werden geblackt)
- Native Write-Protection auf `.git`, `.claude`, `.vscode`, `.idea`

### Warum YOLO-Reset bei Reboot?

YOLO ist "autonomes Experimentieren". Wenn der Mac gebootet wird, nehmen wir an: alter Kontext verloren, neuer Tag, zurück auf sicheren Default. Verhindert, dass ein YOLO-Projekt dauerhaft im Auto-Mode stehen bleibt.

### Wie escape ich aus Strict, wenn Claude einen neuen Command braucht?

Kein Auto-Escape-Hatch by design — Strict ist strict. Workflow:

```
/mode normal               → zurück auf Normal (Rückfrage statt Silent-Deny)
<prompt>                   → lass Claude den Command probieren, bestätige die Rückfrage
/allow "Bash(new-pattern)" → Pattern permanent machen
/mode strict               → zurück zu Strict, jetzt mit neuem Allow-Eintrag
```

### Was passiert bei `/mode yolo` wenn ein Prompt gerade läuft?

Die aktive Session wird unterbrochen (tmux kill). Der laufende Turn wird abgebrochen — Claude hat keine Garantie, die Änderungen persistiert zu haben. Warnung wird vor dem Switch auf WhatsApp geschickt.

### Strict-Mode: Unterschied zu "einfach keine Allow-Rules in Normal"?

Normal fragt bei Ungewöhnlichem per PIN-Rückfrage. Strict fragt nie — unbekannte Commands werden silent gedropt, Claude sieht einen Deny-Return-Code und antwortet "kann ich nicht".
