# Bootstrap-Prompt für Claude Code

Kopiere den folgenden Text **exakt so** in deine erste Claude-Code-Session im `~/whatsbot/` Repo.

---

## Prompt zum Kopieren

```
Ich baue einen persönlichen WhatsApp-Bot, der Claude Code auf meinem Mac fernsteuert. Du bist der Implementierer.

Bevor du irgendwelchen Code schreibst, mach diese drei Dinge in genau dieser Reihenfolge:

1. Lies CLAUDE.md im Repo-Root – das ist deine Arbeitsanweisung für alle Sessions
2. Lies SPEC.md komplett – das ist die Architektur-Referenz
3. Lies .claude/rules/current-phase.md – das sagt dir, wo wir gerade stehen
4. Lies die phasenspezifischen Rules für die aktive Phase

Danach: fasse in 10-15 Zeilen zusammen, was du verstanden hast. Ich prüfe deine Zusammenfassung und gebe dann grünes Licht für die eigentliche Arbeit.

Besonders wichtig: Frage mich bevor du Annahmen triffst. Die Spec ist detailliert, aber Edge-Cases gibt es immer. Lieber eine Frage zuviel als eine falsche Richtung.

Starte jetzt mit Schritt 1.
```

---

## Was du von Claude Code erwartest

Nach diesem Prompt sollte Claude Code:

1. Mehrere `Read`-Tool-Aufrufe für die genannten Files machen
2. Eine Zusammenfassung liefern, die ungefähr diese Punkte enthält:
   - Was das Projekt ist (persönlicher WhatsApp-Bot für Claude Code auf macOS)
   - Das 3-Modi-System (Normal / Strict / YOLO)
   - Die 9-Phasen-Struktur
   - Die wichtigsten Nicht-Verhandelbaren (kein `claude-agent-sdk`, Keychain only, fail-closed)
   - Dass Phase 1 aktiv ist und was dort gebaut wird
3. Dann auf deine Freigabe warten

Wenn die Zusammenfassung gut ist: `Grünes Licht, beginne mit C1.1` oder ähnlich.

Wenn die Zusammenfassung Lücken hat: weise darauf hin und lass Claude Code nochmal lesen.

## Bei jeder folgenden Session (gleiche Phase)

Du musst nicht jedes Mal den Bootstrap-Prompt wiederholen. CLAUDE.md wird bei jeder Session automatisch geladen. Ein kurzer Impuls reicht:

```
Weitermachen bei C1.3 – Health-Endpoint.
```

Oder bei Kontext-Verlust:

```
Lies current-phase.md, dann weiter.
```

## Bei Phasen-Wechsel

Wenn eine Phase fertig ist:

```
Phase 1 ist abgeschlossen. Alle Success-Criteria geprüft, Tests grün, committet.

Beginne mit Phase 2: lies phase-2.md, fasse zusammen was du bauen wirst, warte auf meine Freigabe.
```

## Wichtig: Wenn Claude Code etwas anders machen will

Claude Code wird irgendwann eine Abkürzung vorschlagen. Typische Varianten:
- "Ich könnte das auch einfacher lösen, indem ich..."
- "Das Phasen-Modell scheint hier nicht nötig, weil..."
- "Ich sehe einen Weg, das in einem Rutsch zu bauen..."

Diese Vorschläge können richtig sein oder auch nicht. Eine gute Heuristik:

- **Grüne Zonen** (meist OK, kannst grünes Licht geben):
  - Interne Refactorings, die Success-Criteria nicht ändern
  - Besserer Code innerhalb einer Komponente
  - Zusätzliche Tests über das geforderte Minimum hinaus

- **Rote Zonen** (immer ablehnen oder nachfragen):
  - Security-Layer weglassen ("brauchst du für Personal-MVP nicht wirklich")
  - Phasen-Grenzen überschreiten
  - `claude-agent-sdk` einfügen
  - Secrets in Config-Files
  - Fail-open statt fail-closed

Wenn unsicher: *"Warum ist das besser als der geplante Weg? Was verliere ich dadurch?"* – dann neu entscheiden.

## Session-Länge und Context

Claude-Code-Sessions haben ein Token-Limit. Typisch reichst du mit einer Session für einen Checkpoint, manchmal zwei. Wenn du merkst dass Claude Code langsamer wird oder den Faden verliert:

```
/compact

Fokussiere auf Phase 1, aktueller Stand ist C1.3 abgeschlossen. Weiter mit C1.4.
```

`/compact` reduziert den Context, behält aber wichtige Infos. CLAUDE.md wird nach Compact automatisch neu geladen.

## Wenn etwas schiefgeht

Wenn eine Phase festfährt (Abbruch-Kriterium greift):

1. Lass Claude Code das Problem präzise beschreiben
2. Lies die Beschreibung, entscheide ob: fixen, Scope ändern, oder zurück auf vorige Phase
3. Update entsprechend `current-phase.md` und die relevante `phase-N.md`
4. Dokumentiere im Spec §27 Entscheidungs-Log, dass X aus Grund Y geändert wurde

Die Spec ist ein lebendes Dokument. Anpassungen sind OK, aber dokumentieren ist Pflicht.

---

## Das war's

Damit hast du alles was du brauchst. Der Build dauert realistisch 20-25 Sessions, verteilt auf Wochen. Qualität vor Geschwindigkeit.

Viel Erfolg.
