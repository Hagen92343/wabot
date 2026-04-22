# whatsbot – Projekt-Instruktionen für Claude Code

Du baust einen persönlichen WhatsApp-Bot, der Claude Code auf macOS fernsteuert. Single-User, kein Enterprise-Scope.

## Was du zuerst lesen musst

Bevor du irgendetwas implementierst: **lies die vollständige Spezifikation**. Sie liegt im Root als `SPEC.md`.

- @SPEC.md

Die Spec ist nach 5 Review-Runden auf Weltklasse-Niveau. Sie enthält alle Entscheidungen inklusive ihrer Begründungen. Wenn dir etwas unklar oder widersprüchlich vorkommt: **frage, bevor du implementierst**. Interpretiere nicht.

## Deine Arbeitsweise

1. **Phasen-basiert**. Arbeite nur an der aktuell aktiven Phase. Die 9 Phasen stehen in §21 der Spec. Pro Phase: `.claude/rules/phase-X.md` lesen, bevor du anfängst.
2. **Checkpoints einhalten**. Jede Phase hat nummerierte Checkpoints (C1.1, C1.2, ...). Nach jedem Checkpoint: kurze Zusammenfassung was funktioniert, dann weiter.
3. **Success-Criteria sind bindend**. Eine Phase ist erst "done", wenn alle Success-Criteria erfüllt und mit echten Tests verifiziert sind. Keine "sollte funktionieren"-Abschlüsse.
4. **Abbruch-Kriterien ernst nehmen**. Wenn ein Abbruch-Kriterium greift: stop, melde es, frage nach Richtung. Nicht weiterbauen.

## Architektur-Prinzipien (nicht verhandelbar)

1. **Hexagonal Architecture**. Domain-Core in `whatsbot/domain/` enthält nur reine Logik, keine I/O. Ports in `whatsbot/ports/` sind Protocol/ABC-Interfaces. Adapters in `whatsbot/adapters/` sind konkrete I/O-Implementierungen. Tests auf Domain-Core sind pure Tests ohne Mocks für externe Services.

2. **Kein `claude-agent-sdk`**. Die Subscription-Lock-Prüfungen sind vierfach verriegelt (Spec §5). Füge diese Package niemals zu `requirements.txt` hinzu, egal wie plausibel ein Grund erscheint.

3. **Secrets nur in macOS Keychain**. Niemals in `.env`-Dateien oder Environment-Variablen hardcoden. Die 7 Keychain-Einträge sind in §4 aufgelistet.

4. **Fail-closed bei Security-Paths**. Pre-Tool-Hook für Bash/Write fällt bei Crash auf "blockieren", nicht auf "weitermachen". Auch in YOLO-Modus. Das ist nicht optional.

5. **Correlation-IDs von Anfang an**. Jeder eingehende Request bekommt eine ULID-`msg_id`. Diese fließt durch alle Log-Zeilen aller Layer. Nicht nachträglich einbauen.

## Dinge, die du NIEMALS tun darfst

- `claude-agent-sdk` installieren
- Secrets in Config-Files oder Code hardcoden
- Pre-Tool-Hook-Logik auf "fail-open" umstellen für Bash/Write
- In WhatsApp-Output raw Token-Counts, Session-IDs, oder Transcript-Pfade zurückgeben ohne Redaction zu checken
- `--dangerously-skip-permissions` als Default verwenden (nur für expliziten YOLO-Modus)
- Phase X+1 beginnen bevor Phase X Success-Criteria erfüllt
- Deine Arbeit abschließen ohne `make test` grün zu machen

## Coding-Konventionen

- **Python 3.12**, Type-Hints durchgängig, `mypy --strict` grün
- **structlog** für alle Logs, niemals `print()` oder `logging.basicConfig`
- **pytest** für Tests, Fixtures in `tests/fixtures/`, Unit-Tests nur für Domain-Core ohne I/O
- **async/await** für FastAPI-Handler und alle I/O
- **tenacity** für Retry-Logic (Decorator `@resilient(service_name)`)
- **Commit-Messages**: konventionell (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`)
- **Ein Commit pro Checkpoint**. Die Commit-History dokumentiert den Bau-Fortschritt.

## Fragen und Entscheidungen

Wenn du auf etwas stößt, das in der Spec nicht klar geregelt ist:

1. Prüfe zuerst, ob es in den phasenspezifischen Rules unter `.claude/rules/` steht
2. Wenn nicht: lies §27 Entscheidungs-Log – oft findest du dort den Kontext
3. Wenn immer noch unklar: **frage den User**, bevor du eine Annahme triffst

Die Spec enthält 50 dokumentierte Entscheidungen. Jede neue Richtungsentscheidung gehört mit Begründung ergänzt.

## Aktueller Stand

Steht in `.claude/rules/current-phase.md`. Prüfe diese Datei bei jedem Session-Start. Sie sagt dir, welche Phase aktiv ist und was der letzte abgeschlossene Checkpoint war.

## Test-Strategie pro Phase

Jede Phase muss drei Test-Ebenen grün haben, bevor sie "done" ist:

- **Unit** (pytest Domain-Core): `make test-unit`
- **Integration** (Adapters mit Mocks): `make test-integration`
- **Smoke** (wenn Phase genug Oberfläche hat): `tests/send_fixture.sh <scenario>`

Coverage-Ziel Domain-Core: >80%.

## Wenn du fertig mit einer Phase bist

1. Alle Success-Criteria verifiziert und mit Test-Output belegt
2. `make test` komplett grün
3. Commit mit Message `feat(phase-X): complete phase X`
4. Kurze Zusammenfassung ins CHANGELOG.md: was wurde gebaut, welche Rules wurden aktiv
5. Warte auf User-Freigabe bevor du Phase X+1 beginnst

Dieser Build dauert realistisch 20-25 Claude-Code-Sessions. Qualität schlägt Geschwindigkeit. Wenn du zweifelst: frag.
