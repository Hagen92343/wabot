# CHEAT-SHEET

Alle Commands auf einer Seite. Spec §11.

## Projekt-Management

| Command | Wirkung | Auth |
|---|---|---|
| `/new <name>` | Leeres Projekt anlegen (Normal-Mode) | — |
| `/new <name> git <url>` | Git-Clone + Smart-Detection | — |
| `/ls` | Alle Projekte mit Mode + Status | — |
| `/p` | Zeigt aktives Projekt | — |
| `/p <name>` | Aktives Projekt wechseln, startet tmux+Claude | — |
| `/p <name> <prompt>` | Einmaliger Prompt ohne aktiv-Wechsel | — |
| `/info` | Details zum aktiven Projekt | — |
| `/rm <name>` | Löschen initiieren (60 s Fenster) | — |
| `/rm <name> <PIN>` | Bestätigen, verschiebt in Trash | **PIN** |
| `/cat <timestamp>` | Lang-Output aus Datei abrufen | — |
| `/tail [lines]` | Transcript-Tail | — |

## Modes

| Command | Wirkung |
|---|---|
| `/mode` | Aktuellen Modus zeigen |
| `/mode normal` | 🟢 Normal (Defense-in-Depth, Rückfragen) |
| `/mode strict` | 🔵 Strict (nur Allow-List läuft) |
| `/mode yolo` | 🔴 YOLO (`--dangerously-skip-permissions`) |

Session-Recycle via `--resume <id>` bewahrt Context über Mode-Switches.

## Allow-Rules

| Command | Wirkung |
|---|---|
| `/allowlist` | Aktuelle Rules zeigen, gruppiert nach Source |
| `/allow <pattern>` | Pattern zur Allow-Liste hinzufügen |
| `/deny <pattern>` | Aus Allow-Liste entfernen |
| `/allow batch approve` | Alle Smart-Detection-Vorschläge übernehmen |
| `/allow batch review` | Vorschläge einzeln zeigen |

Pattern-Format: `Tool(command-pattern)`, z.B. `Bash(npm test)`, `Bash(git diff *)`, `Read(~/projekte/**)`.

## Session-Kontrolle

| Command | Wirkung | Auth |
|---|---|---|
| `/stop` / `/stop <name>` | Ctrl+C in die Session (Soft cancel) | — |
| `/kill` / `/kill <name>` | tmux-Session hart beenden | — |
| `/panic` | Alles killen + Lockdown + YOLO→Normal-Reset | — |
| `/unlock <PIN>` | Lockdown aufheben | **PIN** |
| `/release` / `/release <name>` | Input-Lock freigeben | — |
| `/force <name> <PIN> <prompt>` | Lock überschreiben + prompten | **PIN** |

## Kontext + Modell

| Command | Wirkung |
|---|---|
| `/compact` | Manuelles `/compact` |
| `/reset` | Session neu starten (Kontext weg) |
| `/model sonnet` | Auf Sonnet wechseln |
| `/model opus` | Auf Opus wechseln (bei Sub-Limit auto-fallback) |

## Output-Dialog

Nach einer > 10 KB Response:

| Command | Wirkung |
|---|---|
| `/send` | Vollen Output in Chunks senden |
| `/discard` | Verwerfen + Datei löschen |
| `/save` | Nur Datei behalten, nicht senden |

## Observability

| Command | Wirkung |
|---|---|
| `/status` | System-Überblick: Uptime, DB, Sessions, Limits, Lockdown |
| `/log <msg_id>` | Event-Trace einer bestimmten Message |
| `/errors` | Letzte 10 WARNING/ERROR-Events |
| `/ps` | Aktive Sessions mit Mode, Lock, Tokens, Turns |
| `/metrics` | Tages-Digest (WhatsApp-Variante) |
| `/update` | Manueller Claude-Code-Update-Hinweis |

## Mode-Badges (tmux-Status)

- 🟢 **NORMAL** — Default
- 🔵 **STRICT** — nur Allow-List
- 🔴 **YOLO** — `--dangerously-skip-permissions`

## Lock-Badges

- `🤖 BOT` — Bot hält den Lock, du prompt'st vom Handy
- `👤 LOCAL` — Terminal hat Vorrang, Handy-Prompts abgelehnt
- `— FREE` — niemand, beides möglich
