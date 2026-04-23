# whatsbot

Ein persönlicher WhatsApp-Bot, der Claude Code auf deinem Mac fernsteuert. Single-User, kein Enterprise-Scope, kein Cloud-Hosting.

**Status**: Phase 1-9 ✅ — produktiv auf macOS 14+ (Apple Silicon).

## Was ist das

Du schickst eine WhatsApp-Nachricht von unterwegs → der Bot auf deinem Mac nimmt sie entgegen → startet oder routet sie in eine `claude`-Session in tmux → die Antwort kommt als WhatsApp-Message zurück. Am Schreibtisch kannst du dieselben Sessions im Terminal live mitschreiben.

Drei Sicherheitsmodi pro Projekt:

- 🟢 **Normal** — Defense-in-Depth, Rückfrage bei Ungewöhnlichem (Default für neue Projekte).
- 🔵 **Strict** — Nur explizite Allow-Rules, alles andere silent-denied.
- 🔴 **YOLO** — `--dangerously-skip-permissions`, aber Hook + Deny-Patterns + Write-Protection greifen trotzdem.

Zusätzlich: `/panic` killt alles, Watchdog-LaunchAgent als unabhängiger Dead-Man's-Switch, Output-Redaction gegen Key-Leaks.

## Architektur

- Hexagonal (Domain / Ports / Adapters / Application).
- Python 3.12, FastAPI, SQLite (WAL), structlog.
- tmux pro Projekt, Claude-Code via Max-20x-Subscription.
- Cloudflare Tunnel für den Meta-Webhook auf `127.0.0.1:8000`.
- Secrets ausschließlich in macOS Keychain.
- macOS `launchd` für Bot + DB-Backup + Watchdog.

## Start

```bash
git clone <repo> ~/whatsbot
cd ~/whatsbot
# Dann: docs/INSTALL.md folgen.
```

Siehe [docs/INSTALL.md](docs/INSTALL.md) für das komplette Setup (Brew-Pakete, Keychain, Cloudflare, Meta-App, LaunchAgents).

## Dokumentation

| Thema | Datei |
|---|---|
| Vollständige Installation | [docs/INSTALL.md](docs/INSTALL.md) |
| Betrieb & Recovery | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Security-Modell | [docs/SECURITY.md](docs/SECURITY.md) |
| Die drei Modi | [docs/MODES.md](docs/MODES.md) |
| Troubleshooting | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| Command-Referenz (eine Seite) | [docs/CHEAT-SHEET.md](docs/CHEAT-SHEET.md) |
| Vollständige Spec | [SPEC.md](SPEC.md) |
| Änderungslog | [CHANGELOG.md](CHANGELOG.md) |

## Nicht-Ziele

- Kein Multi-User, keine Cloud-Variante.
- Keine API-Abrechnung (vierfacher Subscription-Lock gegen versehentliches Umschwenken).
- Kein Telegram / anderer Messenger.
- Kein Auto-Agent-zu-Agent.
