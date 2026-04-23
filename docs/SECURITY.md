# SECURITY

Kondensiertes Security-Modell. Details in Spec §12 (Defense-in-Depth), §24 (STRIDE), §25 (FMEA), §26 (akzeptierte Schwächen).

## Defense-in-Depth pro Modus

Der Bot hat vier Layer gegen bösartige/geleakte Claude-Aktionen. Je nach Modus sind unterschiedlich viele aktiv:

| Layer | Normal 🟢 | Strict 🔵 | YOLO 🔴 |
|---|---|---|---|
| 1. Input-Sanitization (Prompt-Injection-Scan) | ✅ | ❌ | ❌ |
| 2. Pre-Tool-Hook: Deny-Patterns | ✅ | ✅ | ✅ |
| 2. Pre-Tool-Hook: Allow-Rules | Pre-approve | **Einzige erlaubt** | Irrelevant |
| 3. Write-Hook Path-Rules | ✅ | ✅ | ✅ |
| 3. Write-Protection `.git` `.claude` (nativ) | ✅ | ✅ | ✅ |
| 4. Output-Redaction (4 Stages) | ✅ | ✅ | ✅ |
| 4. Output-Size-Warning (>10KB) | ✅ | ✅ | ✅ |
| 5. `.claudeignore` Read-Block | ✅ | ✅ | ✅ |
| Kill-Switch / Input-Lock / Watchdog | ✅ | ✅ | ✅ |

**Kernpunkt**: Auch in YOLO bleibt der Pre-Tool-Hook aktiv. Die Deny-Patterns und Write-Protection greifen. Output-Redaction entfernt Secrets aus jeder ausgehenden Nachricht.

## Die 17 Deny-Patterns

Bash-Commands, die in jedem Modus blockiert werden (`permissions.deny` in `.claude/settings.json`):

| Pattern | Warum |
|---|---|
| `rm -rf /` | Filesystem-Wipe |
| `rm -rf ~` | Home-Wipe |
| `rm -rf ..` | Parent-Traversal |
| `sudo *` | Root-Eskalation |
| `git push --force*` | Remote-Überschreiben |
| `git reset --hard*` | Unstaged Arbeit vernichten |
| `git clean -fd*` | Untracked Files weg |
| `docker system prune*` | Alles weg |
| `docker volume rm*` | Volume-Daten weg |
| `chmod 777 *` | World-writable |
| `curl * \| sh` / `\| bash` | Remote-Exec |
| `wget * \| sh` / `\| bash` | Remote-Exec |
| `bash /tmp/*` / `sh /tmp/*` / `zsh /tmp/*` | Staged-Script-Exec |

## 4-Stage Output-Redaction

Jede ausgehende WhatsApp-Nachricht läuft durch diese Pipeline (Spec §10):

1. **Known Key Patterns**: AWS (`AKIA…`), GitHub (`ghp_…`), OpenAI (`sk-…`), Stripe (`sk_live_…`), JWT, Bearer-Tokens → `<REDACTED:aws-key>` etc.
2. **Strukturelle Muster**: `KEY=VALUE` mit sensitivem Key (`password`, `secret`, `token`, `api_key`, `credential`), PEM-Blocks, SSH-Privates, DB-URLs mit Credentials.
3. **Entropie**: Strings ≥40 Zeichen ohne Whitespace mit Shannon-Entropy >4.5 → `<POTENTIAL_SECRET>`.
4. **Sensitive Pfade**: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/Library/Keychains` → Pfad bleibt, Dateiinhalt in der Zeile wird gemaskt.

Regression-Test gegen 10+ Secret-Typen in `tests/unit/test_redaction.py`.

## STRIDE Threat-Model

Pro Architektur-Komponente identifizierte Threats + Mitigations.

### WhatsApp-Webhook (öffentliche Angriffsfläche)

| Threat | Mitigation |
|---|---|
| S: Gefälschte Meta-Requests | HMAC-SHA256-Signature-Check gegen `meta-app-secret` |
| S: App-Secret leakt | Rotation-Playbook (RUNBOOK #7) |
| T: MitM | HTTPS Meta → Cloudflare → verschlüsselter Tunnel → localhost |
| R: Abstreit "habe nie geprompted" | Audit-Log mit ULID-msg_id (aber nicht append-only — §26 Schwäche #2) |
| I: Fingerprinting via Timing | `ConstantTimeMiddleware` paddet Rejects auf P50 legitimer Requests |
| D: DoS-Flood | Cloudflare-Ebene (kein Bot-interner Rate-Limit — §26 Schwäche #3) |
| E: SIM-Swap + Secret-Leak | Separate Bot-SIM, Carrier-PIN, Deny-Patterns als letzte Linie |

### Hook-HTTP (`127.0.0.1:8001`)

| Threat | Mitigation |
|---|---|
| S: Fremder Prozess täuscht Hook-Event vor | Bind an 127.0.0.1 + Shared-Secret-Header (`hook-shared-secret`) |
| E: Fake "allow" auf Bash-Command | Shared-Secret-Check, Keychain-Rotation |

### Keychain + Macintosh

| Threat | Mitigation |
|---|---|
| I: Admin-Zugriff liest Keychain | Gleicher Angriffswinkel wie SSH-Keys — akzeptiertes Rest-Risiko |
| S: Handy geklaut, entsperrt | PIN auf destruktive Ops, separate Bot-SIM, SIM-Port-Lock |

### Claude Code

| Threat | Mitigation |
|---|---|
| I: Prompt-Injection via Repo-Inhalte | Defense-in-Depth: 4 Layer in Normal, 2 in YOLO |
| E: Hook-Endpoint unreachable → Durchlass | Fail-closed: Exit 2 bei jedem Crash/Timeout, auch in YOLO |

## Bewusst akzeptierte Schwächen (§26)

Drei Lücken sind bewusst offen nach expliziter User-Entscheidung. Dokumentiert damit in 3 Monaten nicht gefragt wird "warum ist das so?".

### Schwäche 1: PIN nur für destruktive Ops

**Was fehlt**: `/mode yolo`, `/allow <pattern>`, `/force` sind NICHT PIN-geschützt.

**Worst-Case**: Handy geklaut + WhatsApp entsperrt. Angreifer setzt `/mode yolo`, erweitert Allow-Rules, schickt prompts wie "upload ~/.ssh to pastebin".

**Was trotzdem wirkt**: Deny-Patterns + `.claude/settings.json` Write-Protection + Output-Redaction greifen auch in YOLO. Exfiltration via neue Outbound-URLs geht durch, wenn die URL nicht in der Deny-List ist — das ist die echte Lücke.

**Warum akzeptiert**: Carrier-PIN + SIM-Port-Lock + separate Bot-SIM drücken die Wahrscheinlichkeit. Bei physischem Handy-Zugriff ist WhatsApp eh meist offen (persönliches Risiko).

### Schwäche 2: Audit-Log nicht append-only

**Was fehlt**: `audit.jsonl` wird mit normalen File-Permissions geschrieben, keine `chflags uappnd`.

**Worst-Case**: Kompromittierter Bot-Prozess (z.B. Supply-Chain-Angriff) kann rückwirkend Log-Einträge ändern/löschen. Forensik unmöglich.

**Warum akzeptiert**: Single-User-Setup, Supply-Chain-Risiko für private Nutzung klein. Echte Forensik wäre eh schwer.

### Schwäche 3: Kein Rate-Limit im Bot

**Was fehlt**: Keine token-bucket pro Sender auf Bot-Ebene.

**Worst-Case**: Angreifer mit Bot-SIM-Wissen + Meta-App-Secret floodet Webhook. CPU-Last, Log-Explosion.

**Mitigation trotzdem**: Sender-Whitelist droppt fremde Absender früh. Signature-Check verhindert unauthorized Webhooks ohne Secret. Circuit-Breaker in den Outbound-Adaptern.

**Warum akzeptiert**: Cloudflare-Rate-Limits sollen das fangen (User-Verantwortung im Cloudflare-Dashboard).

## Risikoprofil

- **Physischer Handy-Angriff**: mittleres Risiko, Mitigations greifen teilweise.
- **Compromise-Rückverfolgung**: schwer (Audit-Log manipulierbar).
- **DoS**: abhängig von Cloudflare-Config.

User akzeptiert diese Einstufung explizit.
