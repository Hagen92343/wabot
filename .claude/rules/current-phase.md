# Aktueller Stand

**Aktive Phase**: Phase 3 — Security-Core (Hook + Allow/Deny + Redaction)
**Aktiver Checkpoint**: C3.0 (Phase-3-Rules schreiben, User-Freigabe einholen)
**Letzter abgeschlossener Checkpoint**: C2.8 (Phase-2-Verifikation)

## Phase 2 abgeschlossen ✅

Alle 8 Checkpoints grün, Phase 2 komplett gebaut und verifiziert.

- ✅ C2.1 — `/new <name>` empty + `/ls`
- ✅ C2.2 — `/new <name> git <url>` + URL-Whitelist + Smart-Detection-Stub
- ✅ C2.3 — Smart-Detection für alle 9 Artefakt-Stacks
- ✅ C2.4 — `/allow batch approve` + `/allow batch review`
- ✅ C2.5 — `/allow <pat>` + `/deny <pat>` + `/allowlist` + `/p`/`/p <name>`
- ✅ C2.6 — URL-Whitelist (in C2.2 + C2.8-Smoke abgedeckt)
- ✅ C2.7 — `/rm <n>` mit 60s-Confirm + PIN + Trash
- ✅ C2.8 — Tests grün + Domain-Coverage 100 % + Smoke 18/18

**Tests**: 373/373 passing, mypy --strict clean, ruff clean.
**Smoke**: `tests/smoke_phase2.py` — 18/18 grün (in-process, temp-Dir, :memory:).

## Was als Nächstes zu tun ist (Phase 3, Start)

Phase 3 ist **Security-Core** (Hook + Allow/Deny-Enforcement + Redaction),
parallelisierbar mit Phase 2 wäre gewesen — da Phase 2 jetzt durch ist,
starte Phase 3 sequentiell.

Laut `phase-2.md`-Konvention + `phases-3-to-9.md`:

1. **Erst `.claude/rules/phase-3.md` schreiben** — gleiche Struktur wie
   `phase-1.md` und `phase-2.md` (Scope, Checkpoints mit Test-Commands,
   Success-Criteria, Abbruch-Kriterien, Abgrenzung zu späteren Phasen),
   basierend auf Spec §21 Phase 3 + Spec §7 / §10 / §12.
2. **User-Freigabe einholen** bevor implementiert wird.
3. Wichtigste Gotchas (aus `phases-3-to-9.md` + Spec §12):
   - Hook-Shared-Secret zwischen `hooks/pre_tool.py` und Bot zwingend
   - Hook-Endpoint bindet nur an `127.0.0.1:8001`
   - Fail-closed für Bash bei Hook-Endpoint-Unreachable
   - Die 17 Deny-Patterns aus Spec §12 exakt übernehmen
   - PIN-Rückfrage-Flow mit 5min-Timeout + `pending_confirmations`-Tabelle
   - Redaction-Pipeline 4 Stages: bekannte Keys → strukturelle Patterns →
     Entropie → Pfade (Spec §10)
   - Output-Size-Warning (>10KB) mit `/send` / `/discard` / `/save`

## Format-Konvention für Updates

```
**Aktive Phase**: Phase 3 — Security-Core
**Aktiver Checkpoint**: C3.1 (Hook-Script + Shared-Secret-IPC)
**Letzter abgeschlossener Checkpoint**: C2.8 (Phase-2-Verifikation)
```

## Hinweis bei Session-Start

Lies immer zuerst:
1. Diese Datei (`current-phase.md`) — um zu wissen, wo wir stehen
2. Die Rules für die aktive Phase (sobald `.claude/rules/phase-3.md` existiert)
3. Die Spec (`SPEC.md`) — wenn du Details zu einer Komponente brauchst

Nicht jedes Mal die komplette Spec durchlesen. Sie ist die Referenz, nicht die Leseliste.
