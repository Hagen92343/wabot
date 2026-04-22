# Aktueller Stand

**Aktive Phase**: Phase 1 KOMPLETT — Phase 2 wartet auf User-Freigabe
**Aktiver Checkpoint**: noch keiner
**Letzter abgeschlossener Checkpoint**: C1.7 (DB-Backup-Script) — Phase 1 komplett

## Phase-1-Abschluss

Alle 12 Success-Criteria aus `phase-1.md` erfüllt:

| # | Kriterium | Wo erfüllt |
|---|-----------|------------|
| 1 | Bot läuft als LaunchAgent, startet auto nach Login | C1.4 (live verifiziert) |
| 2 | `/health` antwortet JSON | C1.3 (live) |
| 3 | Meta-Signature-Check rejected ungültig (silent + log) | C1.5 (8 tests) |
| 4 | Fremde Sender silent gedroppt | C1.5 (live + tests) |
| 5 | `/ping` per Fixture → Echo | C1.5 (test_ping_fixture_routes_to_pong_reply) |
| 6 | Alle 7 Keychain-Secrets ladbar | C1.2 (KeychainProvider + setup-secrets.sh) |
| 7 | `PRAGMA integrity_check` bei Startup | C1.2 (open_state_db) |
| 8 | Logs als JSON mit Correlation-ID | C1.3 (live in launchd-log) |
| 9 | `make test` grün, >80% Domain-Coverage | 135/135 grün, 96.17% gesamt, domain 100% |
| 10 | Tägliches DB-Backup-Script lauffähig | C1.7 (live verifiziert) |
| 11 | `mypy --strict whatsbot/` grün | 18 source files, 0 issues |
| 12 | CHANGELOG mit Phase-1-Einträgen | alle 7 Checkpoints dokumentiert |

## Was Phase 1 dem User liefert

- Bot empfängt Meta WhatsApp Webhooks (signiert, sender-whitelisted)
- Antwortet auf `/ping`, `/status`, `/help`
- Läuft als macOS LaunchAgent (`make deploy-launchd ENV=prod DOMAIN=hagen`)
- Tägliches DB-Backup um 03:00 mit 30-Tage-Retention
- Strukturierte JSON-Logs mit ULID-Correlation-IDs
- Hexagonal-Architektur, mypy strict, 96% Test-Coverage

## Was Phase 1 noch NICHT liefert (kommt in späteren Phasen)

- Projekt-Verwaltung (`/new`, `/ls`, `/p`, `/rm`) — Phase 2
- Pre-Tool-Hook + Allow/Deny-Rules — Phase 3
- Claude-Launch in tmux + 3-Modi-System — Phase 4
- Input-Lock + lokales Terminal preempt — Phase 5
- Kill-Switch + Watchdog + Sleep-Handling — Phase 6
- Bilder/PDF/Audio (Whisper) — Phase 7
- Limit-Tracking + Metrics + `/log`/`/errors`/`/ps` — Phase 8
- INSTALL/RUNBOOK/Docs + Smoke-Test — Phase 9

## Nächster Schritt: User-Freigabe für Phase 2

User entscheidet ob:
- (a) Direkt mit Phase 2 weitermachen (`phase-2.md` ist bereits geschrieben)
- (b) Erst `make setup-secrets` durchziehen + echten LaunchAgent-Run mit
  `make deploy-launchd ENV=prod DOMAIN=hagen` testen, dann Phase 2
- (c) Pause / Cleanup / sonstiges

`phase-2.md` Scope: Projekt-Management (`/new`, `/ls`, `/p`, `/info`,
`/rm`, `/cat`, `/tail`), Smart-Detection für `/new git`, Allow-Rule-
Vorschläge, Trash-Mechanismus mit PIN. Aufwand: 2-3 Sessions.

## Format-Konvention für Updates

Wenn Phase 2 startet:

```
**Aktive Phase**: Phase 2 – Projekt-Management + Smart-Detection
**Aktiver Checkpoint**: C2.1 (/new empty)
**Letzter abgeschlossener Checkpoint**: C1.7 (DB-Backup) — Phase 1 komplett
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (`.claude/rules/phase-<N>.md`) — um zu wissen, was zu tun ist
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
