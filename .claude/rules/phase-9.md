# Phase 9: Docs + Smoke-Tests + Polish

**Aufwand**: 1-2 Sessions
**Abhängigkeiten**: Phase 1-8 komplett ✅
**Parallelisierbar mit**: —
**Spec-Referenzen**: §17 (Test-Strategie Smoke), §21 Phase 9,
§22 (Deploy + Update), §23 (Recovery-Playbooks), §28 (Glossar)

## Ziel der Phase

**Produktiv-ready.** Die Phasen 1-8 haben funktional alles gebaut,
was der Bot braucht. Phase 9 macht aus dem Build ein Produkt: ein
End-to-End-Smoke-Test bestätigt, dass eine WhatsApp-Nachricht vom
Handy tatsächlich als Claude-Prompt ankommt und die Antwort zurück
geht; die Dokumentation im `docs/`-Verzeichnis wird vollständig
geschrieben, sodass ein Dritter den Bot von einem leeren Mac aus
installieren kann; Error-Messages werden auf Edge-Cases gehärtet;
CHEAT-SHEET + README liefern den Einstieg.

Phase 9 endet damit, dass:

1. `make smoke` End-to-End grün läuft — Mock-Meta-Server nimmt
   eine Text-Nachricht entgegen, füttert sie in den Bot, liest
   die Claude-Antwort aus dem Response-Channel ab, assertet
   sinnvolle Werte (Mode-Emoji, Timing-Footer, Redaction).
2. `docs/INSTALL.md` + `docs/RUNBOOK.md` + `docs/SECURITY.md` +
   `docs/MODES.md` + `docs/TROUBLESHOOTING.md` +
   `docs/CHEAT-SHEET.md` + `README.md` existieren, sind
   konsistent und basieren auf den Spec-§4/§22/§23-Inhalten.
3. Edge-Cases sind abgedeckt: leere Prompts, Unicode in
   Projektnamen, Überlange Commands, Kontroll-Zeichen.
4. `make lint` und `mypy --strict` sind clean; das E731-Erbe in
   `delete_service.py` wird hier endlich behoben.

## Voraussetzungen

- **Phase 1-8** komplett (alle 26 Checkpoints grün).
- Tests-Stand 1501/1501 grün (+ 1 skipped ffmpeg).
- Commit-Stand bis `82b9cbd feat(phase-8): complete phase 8 —
  C8.4 prometheus /metrics endpoint`.

## Was gebaut wird

### 1. `tests/smoke.py` — End-to-End-Smoke

Ein einzelner Test, der den kompletten Stack übt — vergleichbar mit
`test_diagnostics_e2e.py` aber mit einer zusätzlichen Mock-Meta-
Outbound-Komponente, damit wir Response-Rendering wirklich
beobachten können.

- **Setup** (pytest fixture): TmpPath-DB + `projects_root` +
  `log_dir`. `StubSecrets` mit vollständigen 7 Keys. `RecordingSender`
  ersetzt den raw_sender.
- **Journey**:
  1. Signed `POST /webhook` mit `/ping` → erwartet `pong · <version>
     · <uptime>` + Footer `━━━`.
  2. Signed `POST /webhook` mit `/new alpha` → erwartet `✅ Projekt
     'alpha' angelegt`.
  3. Signed `POST /webhook` mit `/ls` → Listing enthält `alpha`.
  4. `POST /webhook` mit `/mode` (ohne Args) → zeigt aktuellen
     Mode-Hint.
  5. `POST /webhook` mit ungültig signiertem Body → 200 OK + kein
     Send.
  6. `POST /webhook` mit nicht-whitelisted Sender → 200 OK + kein
     Send.
  7. `GET /metrics` → enthält `whatsbot_messages_total{direction="in",
     kind="text"} >= 4`.
  8. `GET /metrics` → enthält `whatsbot_messages_total{direction=
     "out",kind="text"} >= 4`.
  9. Signed `POST /webhook` mit einer AWS-Key-haltigen Text-Message
     → Response (falls gesendet) enthält **nicht** den Key, sondern
     `<REDACTED:aws-key>`.
- **Mark**: `@pytest.mark.smoke` + in `pyproject.toml`-Markers
  registrieren falls nicht schon da. `make smoke` existiert schon.
- **Wichtig**: Der Smoke-Test darf **keinen** `claude`-Subprozess
  starten (kein echter Prompt-Roundtrip). Der Ping/New/Ls-Flow
  deckt den Command-Pfad; der `safe-claude`-Aufruf wird als
  separater Integration-Test in Phase 4 (C4.2 bis C4.7) bereits
  abgedeckt.

### 2. Dokumentation

Ein `docs/`-Verzeichnis existiert noch nicht im Repo. Phase 9 legt
sechs Markdown-Files an. Grundsatz: **ein Dritter muss ab
`git clone` mit der Docs-Suite die Produktion hochziehen können**.

#### `docs/INSTALL.md`

Übernimmt Spec §22 "Initial-Deploy". Inhalte:
- Prerequisites (brew-Pakete aus §4 + Python 3.12 + Cloudflare
  Account + WhatsApp Cloud API Access + eigene SIM).
- `brew install tmux ffmpeg python@3.12 cloudflared whisper-cpp`
  — ganzer Block.
- Claude-Code-Installer + `claude /login` + `claude /status`
  Verifikation.
- `make install` + `make setup-secrets` (interaktiv für alle 7
  Keychain-Einträge) + `make deploy-launchd` in genau dieser
  Reihenfolge.
- Cloudflare Tunnel: `cloudflared tunnel login` + `tunnel create
  whatsbot` + DNS-Route + Config-Datei.
- Meta-App-Setup in der Developer-Konsole (Webhook-URL +
  Verify-Token setzen, Test-Recipients).
- Whisper-Modell herunterladen: `~/Library/whisper-cpp/models/
  ggml-small.bin`-Pfad und `bash download-ggml-model.sh small`.
- SIM-Port-Lock beim Carrier aktivieren (§24 Threat Model).
- Abschluss-Test: vom Handy `/ping` schicken → `pong`-Response.

#### `docs/RUNBOOK.md`

Übernimmt Spec §23 komplett — alle 9 Recovery-Playbooks als H2-
Sektionen:
1. Mac-Crash während Pending Confirmation
2. Claude-Code-Update hat `--resume` gebrochen
3. Meta-API-Outage
4. tmux-Server OOM-killed
5. Hook-Script Syntax-Error nach Update
6. PIN vergessen
7. Meta-App-Secret geleakt
8. DB corrupt
9. Laptop-Sleep mitten in Session

Plus `## Secret-Rotation` mit den 7 Keychain-Einträgen (`security
add-generic-password -U -s whatsbot -a <name> -w`) und dem
LaunchAgent-Reload (`launchctl kickstart -k gui/$UID/com.DOMAIN.whatsbot`).

Plus `## Updates` — manueller Claude-Code-Update, Bot-Update
(`git pull` + `make install` + `launchctl kickstart`), Datenbank-
Migration via `whatsbot.adapters.sqlite_repo` + Schema-Version.

#### `docs/SECURITY.md`

Kondensierte Spec §12 + §24 + §26:
- Die vier Defense-Layer-Tabelle pro Modus.
- Die 17 Deny-Patterns.
- 4-Stage-Redaction-Pipeline.
- STRIDE-Threat-Model tabularisch.
- Die drei bewusst akzeptierten Schwächen (§26) **einschließlich**
  Worst-Case-Szenarien — damit der User in 3 Monaten nicht
  überrascht wird.

#### `docs/MODES.md`

Kurze Referenz für die drei Modi. Tabelle (wie Spec §6), plus
Antworten auf häufige Fragen: "Warum kein PIN auf `/mode yolo`?",
"Warum YOLO-Reset bei Reboot?", "Wie escape ich aus Strict?".

#### `docs/TROUBLESHOOTING.md`

Log-Grepping-Cheatsheet:
- `/log <msg_id>` für einen bestimmten Call.
- `~/Library/Logs/whatsbot/app.jsonl` mit `jq` filtern.
- `/status` vom Handy aus.
- Heartbeat-File checken (`stat /tmp/whatsbot-heartbeat`).
- tmux-Sessions (`tmux ls | grep wb-`).
- DB-Integrity (`sqlite3 state.db 'PRAGMA integrity_check;'`).

Plus die häufigen Symptome:
- "Bot antwortet nicht" → Tunnel down? Launchd zeigt Bot als
  running? Heartbeat frisch?
- "Prompt läuft nicht durch" → Lock-Owner prüfen (`/ps`), Max-Limit
  aktiv (`/status`)?
- "Output wird in Stücken gesendet" → Size-Pipeline (>10KB)
  erwartbar — `/cat <timestamp>`.

#### `docs/CHEAT-SHEET.md`

Eine Seite. Alle WhatsApp-Commands aus Spec §11 tabellarisch. Die
Lock-Owner-Badges + Mode-Badges. PIN-gated Commands markiert.

#### `README.md`

Eine Seite. One-Liner-Beschreibung + "Was ist das?"-Paragraph +
Link auf `docs/INSTALL.md` für den Setup-Teil. Status-Badge
("Phase 1-9 ✅ — produktiv auf einem Mac") + Tech-Stack-Liste
(Python 3.12, FastAPI, SQLite WAL, Cloudflare Tunnel, macOS
launchd). Links auf Spec + CHANGELOG.

### 3. Edge-Case-Härtung

- **Leerer Prompt** (`` an aktives Projekt): `CommandHandler.
  _handle_bare_prompt` ignoriert leeren Strings bereits — wir
  ergänzen einen Unit-Test für Whitespace-only (`"   \n  "`).
- **Unicode in Projektnamen**: `domain/projects.validate_project_name`
  lässt nur `[a-z0-9_-]{2,32}` durch — Test mit Unicode, Emoji,
  Leerzeichen assertet friendly Rejection.
- **Überlange Commands** (>1000 Zeichen): der CommandHandler
  verträgt das, aber der Footer könnte den Gesamt-Body über 10KB
  drücken → OutputService greift. Unit-Test: 15 KB-Prompt via
  `/p name <long prompt>` → Usage-Dialog triggert (was die C3.5-
  Pipeline schon macht, wir wollen nur einen Regression-Guard).
- **Kontroll-Zeichen in inbound Text**: `\x00`, `\x1b`-ESC,
  `\x7f`. Aktuell gehen die durch — die Frage ist, ob das gewollt
  ist oder gestript werden sollte. Spec §9 nennt das nicht
  explizit. **Entscheidung**: Wir strippen `\x00` (NULL) und alle
  `\x01-\x08` + `\x0b` + `\x0e-\x1f` + `\x7f`-Kontrollzeichen
  *außer* `\t` / `\n` / `\r` vor dem Command-Routing. Domain-
  Modul `whatsbot/domain/text_sanitize.py` (pure) + Webhook-
  Ebene-Call vor `iter_text_messages`-Loop.

### 4. Pre-existing E731 fix

`whatsbot/application/delete_service.py:48` hat seit Phase 2 ein
`_DEFAULT_CLOCK = lambda ...`-Assignment statt `def`. Ruff zeigt
das als E731-Warning an. Phase-9-Polish behebt es.

### 5. `tests/smoke_phase2.py` aufräumen

Existierender Phase-2-Smoke-Test wird in `tests/smoke.py`
aufgegangen — `smoke_phase2.py` wird entfernt oder als
`@pytest.mark.smoke` in die neue Datei gemerged.

## Checkpoints

### C9.1 — `tests/smoke.py` grün

- Neuer smoke-Test deckt die 9-Step-Journey aus §1 oben ab.
- `make smoke` läuft grün.
- `@pytest.mark.smoke`-Marker in `pyproject.toml` registriert (falls
  nicht schon).

### C9.2 — Dokumentations-Suite komplett

- Alle 7 Dateien (`docs/INSTALL.md`, `docs/RUNBOOK.md`,
  `docs/SECURITY.md`, `docs/MODES.md`, `docs/TROUBLESHOOTING.md`,
  `docs/CHEAT-SHEET.md`, `README.md`) existieren.
- INSTALL.md ist von einem Dritten nachvollziehbar — manueller
  "Pair-Read"-Check (ich lese sie noch einmal durch und prüfe, ob
  die Reihenfolge funktioniert).
- Keine internen-nur-URLs, keine TODO-Marker, keine "wird später
  erklärt"-Platzhalter.

### C9.3 — Edge-Cases + Polish

- `domain/text_sanitize.py` mit 10+ Unit-Tests für Kontroll-
  Zeichen.
- Unit-Tests für leeren + whitespace-only Prompt.
- Unit-Tests für Unicode-Projektnamen-Rejection (5 Varianten).
- `delete_service.py` E731 behoben (lambda → `def`).
- `ruff check whatsbot/` komplett clean.
- 10-Tage-Stabilitäts-Fenster: Smoke-Test wird 5x in Folge
  ausgeführt ohne Flakes (CI-Ersatz).

## Success Criteria

- [ ] `make smoke` grün (C9.1).
- [ ] `make test` grün (1501+ neue Tests dabei).
- [ ] `make lint` clean (ruff + mypy).
- [ ] Alle 7 Docs-Dateien final, keine Platzhalter.
- [ ] Ein Dritter kann aus `docs/INSTALL.md` installieren
      (manueller Review).
- [ ] Edge-Cases aus §3 oben als Unit-Tests grün.
- [ ] E731-Erbe aus Phase 2 behoben.
- [ ] `CHANGELOG.md` C9.x-Einträge pro Checkpoint.
- [ ] Sammel-Commit `feat(phase-9): complete phase 9`.

## Abbruch-Kriterien

- **Smoke-Test braucht einen echten Claude-Subprozess**: Stop.
  Die Phase-4-Integration-Tests decken das schon ab; der
  Smoke-Test soll auf Command-Router-Ebene arbeiten, nicht
  Claude-Subprocess-Ebene. Wenn der Scope auseinanderläuft,
  teilen wir in `tests/smoke.py` (command-level) + optional
  `tests/smoke_claude.py` (subprocess-level, @skipif ohne
  Claude-Subscription).
- **Docs brauchen externe Review-Runde**: Stop. Phase 9 liefert
  die erste Fassung; eine polierte Doc-Iteration kann als
  Phase-9.1 nachkommen wenn nötig.
- **INSTALL.md scheitert am Pair-Read**: Stop. Konkrete Lücke
  dokumentieren, korrigieren, Pair-Read wiederholen bevor C9.2
  als erfüllt gilt.

## Was in Phase 9 NICHT gebaut wird

- **Live-WhatsApp-Test vom echten Handy** — das ist der
  Abschluss-Schritt in `docs/INSTALL.md`, nicht Teil von Phase 9
  selbst.
- **Grafana-Dashboards** — Phase-8-Metrics liefern die Series,
  Dashboard-Bau ist optional und bleibt Follow-up.
- **CI-Pipeline** — Spec §17 explizit: "nicht in CI". Bleibt
  manuell via `make test` / `make smoke`.
- **External-Drain für Audit-Log** — Spec §26 Schwäche #2
  dokumentiert: bewusst akzeptiert.
- **Log-Retention-Cleanup** pro Projekt (`history.jsonl`) —
  Spec §20 sagt "kein Auto-Delete, >90 Tage manuell via
  `/clean-transcripts`"; das `/clean-transcripts`-Command ist
  in Spec §11 gelistet aber nicht priorisiert. Bleibt offen.

## Architektur-Hinweise

- Die `docs/`-Dateien dürfen ruhig Spec-Zitate enthalten — Ziel
  ist nicht Redundanz-Freiheit, sondern dass der User bei einem
  Problem genau EINE Quelle lesen muss ("RUNBOOK bei Crash",
  nicht "SPEC §23 bei Crash").
- `text_sanitize.py` ist pure Domain — kein I/O, kein Regex-
  Compile pro Call. Konstanten auf Module-Scope.
- Smoke-Test benutzt den schon bestehenden `StubSecrets`-Pattern
  aus `test_metrics_e2e.py` / `test_limit_guard_e2e.py`. Wenn
  der Copy-Paste-Overhead nervt, extrahieren wir einen
  `tests/fixtures/stub_secrets.py`-Helper — aber erst wenn er zum
  dritten Mal gebraucht würde.

## Nach Phase 9

- Update `.claude/rules/current-phase.md` auf "Phase 9 **komplett**
  ✅ — wartet auf Freigabe für produktive Aufnahme".
- CLAUDE.md informieren, dass der Build zu Ende ist (die Spec
  sagt "20-25 Claude-Code-Sessions" — wir können einen
  Sessions-Counter als Retrospektive ergänzen, muss nicht).
- User schickt ersten echten WhatsApp-Prompt vom Handy an den Bot.
- Falls Live-Probleme auftauchen: neue Phase 10 (Hotfix-Round)
  gegen die spezifischen Symptome, nicht Phase 9 erweitern.
