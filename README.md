# whatsbot Build-Kit

Dieses Kit enthält alles, was du brauchst, um den whatsbot mit Claude Code zu implementieren.

## Inhalt

```
whatsbot-build-kit/
├── README.md                          ← du bist hier
├── BOOTSTRAP-PROMPT.md                ← der Prompt für die erste Claude-Code-Session
├── CLAUDE.md                          ← persistente Meta-Instruktion (lädt Claude Code bei jeder Session)
├── SPEC.md                            ← die vollständige Spec (1788 Zeilen, §1-28)
└── .claude/
    └── rules/
        ├── current-phase.md           ← Live-Tracker: welche Phase/welcher Checkpoint
        ├── phase-1.md                 ← detaillierte Rules für Phase 1
        ├── phase-2.md                 ← detaillierte Rules für Phase 2
        └── phases-3-to-9.md           ← Referenz für restliche Phasen
```

## So benutzt du das Kit

### Schritt 1: Repo anlegen

```bash
mkdir -p ~/whatsbot
cd ~/whatsbot
git init
```

### Schritt 2: Build-Kit ins Repo kopieren

Den gesamten Inhalt dieses `whatsbot-build-kit/`-Ordners ins Repo legen:

```bash
cp -r /pfad/zum/whatsbot-build-kit/* ~/whatsbot/
cp -r /pfad/zum/whatsbot-build-kit/.claude ~/whatsbot/
```

Dann:

```bash
cd ~/whatsbot
git add -A
git commit -m "docs: initial spec and build kit"
```

### Schritt 3: Claude Code starten

```bash
cd ~/whatsbot
claude
```

### Schritt 4: Bootstrap-Prompt geben

Lies `BOOTSTRAP-PROMPT.md` und kopiere den Prompt in deine Claude-Code-Session. Dann folge den Anweisungen dort.

## Was dieses Kit nicht enthält

- Claude Code selbst (separat installieren)
- WhatsApp-Business-Account (manuell bei Meta anlegen)
- Cloudflare-Tunnel (während Phase 1 einrichten)
- macOS Keychain-Secrets (interaktiv in Phase 1 setzen via `make setup-secrets`)

All das ist in SPEC.md §22 (Deploy) dokumentiert.

## Größenordnung

- 9 Phasen, ca. 20-25 Claude-Code-Sessions
- Finale Codebase: ca. 8-12.000 Zeilen Python, Tests inklusive
- Timeline: mehrere Wochen bei realistischer Nebenbei-Arbeit

## Wenn etwas unklar ist

Lies §27 (Entscheidungs-Log) in SPEC.md – dort ist jede wichtige Design-Entscheidung mit Begründung dokumentiert. Wenn du dich später fragst "warum haben wir das so gemacht", steht die Antwort dort.

Die drei bewusst akzeptierten Schwächen (§26) sind auch klar benannt. Falls sich deine Risiko-Einschätzung ändert, kannst du sie später schließen.

## Support

Diese Spec und das Kit sind bewusst so geschrieben, dass sie selbsterklärend sind. Alles, was ein Implementierer wissen muss, steht in den Files. Wenn du trotzdem etwas brauchst: die Claude-Code-Docs unter https://code.claude.com/docs/en/overview.

Viel Erfolg.
