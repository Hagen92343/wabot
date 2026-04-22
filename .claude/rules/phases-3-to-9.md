# Phasen 3-9: Referenzen

Die Details für jede der restlichen Phasen findest du in der Spec §21 unter dem entsprechenden Abschnitt. Alle Phasen haben dort:

- Scope (was wird gebaut)
- Checkpoints (konkrete Verifikations-Schritte)
- Success Criteria (was erfüllt sein muss)
- Abbruch-Kriterien (wann stoppen und fragen)

Wenn du eine dieser Phasen beginnst, schreibe **einen detaillierten phase-N.md wie phase-1.md und phase-2.md** als ersten Schritt, basierend auf Spec §21 + den Referenzen unten. User freigeben lassen. Dann erst bauen.

---

## Phase 3: Security-Core (Hook + Allow/Deny + Redaction)

**Aufwand**: 3-4 Sessions
**Abhängigkeiten**: Phase 1 (kann parallel zu Phase 2)
**Spec-Referenzen**: §7 (Hook-Verhalten), §10 (Redaction), §12 (Security-Layer)

**Wichtigste Gotchas**:
- Hook-Shared-Secret zwischen `hooks/pre_tool.py` und Bot zwingend
- Hook-Endpoint bindet nur an `127.0.0.1`
- Fail-closed für Bash bei Hook-Endpoint-Unreachable
- Die 17 Deny-Patterns aus Spec §12 exakt übernehmen

---

## Phase 4: Mode-System + Claude-Launch

**Aufwand**: 4-5 Sessions (größte Phase)
**Abhängigkeiten**: Phase 2 + Phase 3 beide komplett
**Spec-Referenzen**: §6 (Modi), §7 (tmux, Transcript), §8 (Context)

**Wichtigste Gotchas**:
- Mode-Switch braucht Session-Recycle mit `--resume`
- YOLO-Reset bei Reboot ist nicht optional (§6)
- Transcript-Watching event-basiert (watchdog lib), nicht polling
- Token-Count aus `message.usage`-Feldern im Transcript
- Bot-Prompts mit Zero-Width-Space-Prefix markieren

---

## Phase 5: Input-Lock + Multi-Session

**Aufwand**: 1-2 Sessions
**Abhängigkeiten**: Phase 4
**Parallelisierbar mit**: Phase 6
**Spec-Referenzen**: §7 (Input-Lock)

**Wichtigste Gotchas**:
- Soft-Preemption: lokales Terminal hat Vorrang
- Prefix-Technik muss Bot- von User-Input unterscheiden können

---

## Phase 6: Kill-Switch + Watchdog + Sleep-Handling

**Aufwand**: 1-2 Sessions
**Abhängigkeiten**: Phase 4
**Parallelisierbar mit**: Phase 5, 7
**Spec-Referenzen**: §7 (Kill-Switch), FMEA #12 (Sleep)

**Wichtigste Gotchas**:
- pmset-Integration: Sleep-Event pausiert Heartbeat-Check, Wake-Event setzt fort
- `/panic` setzt YOLO-Projekte sofort auf Normal
- Watchdog ist separater LaunchAgent

---

## Phase 7: Medien-Pipeline

**Aufwand**: 2-3 Sessions
**Abhängigkeiten**: Phase 4
**Parallelisierbar mit**: Phase 5, 6
**Spec-Referenzen**: §16

**Wichtigste Gotchas**:
- Whisper.cpp `small`-Model multilingual
- Secure-Delete nach TTL: Überschreiben mit Nullen vor Unlink
- Size-Validation vor Download

---

## Phase 8: Observability + Limits

**Aufwand**: 2 Sessions
**Abhängigkeiten**: Phase 4
**Parallelisierbar mit**: Phase 5, 6, 7
**Spec-Referenzen**: §14 (Limits), §15 (Observability)

**Wichtigste Gotchas**:
- Max-Limit-Parser primär aus Transcript-Events, Status-Line nur Fallback
- Circuit-Breaker für alle externen Adapters (Meta, Whisper)
- `/metrics` nur auf localhost binden, nicht über Tunnel exponieren

---

## Phase 9: Docs + Smoke-Tests + Polish

**Aufwand**: 1-2 Sessions
**Abhängigkeiten**: Alle vorigen Phasen
**Spec-Referenzen**: §22 (Deploy), §23 (Recovery-Playbooks)

**Wichtigste Gotchas**:
- INSTALL.md muss von einem Dritten nachvollziehbar sein
- RUNBOOK.md mit allen Recovery-Playbooks aus Spec §23
- Smoke-Test in `tests/smoke.py` – End-to-End mit Mock-Meta-Server

---

## Wie du diese Phasen-Files erzeugst

Wenn du z.B. Phase 3 beginnst:

1. Erstelle `.claude/rules/phase-3.md` mit der gleichen Struktur wie `phase-1.md` und `phase-2.md`
2. Befülle Scope basierend auf Spec §21 Phase 3 + den Gotchas oben
3. Liste alle Checkpoints mit konkreten Test-Commands
4. Success Criteria als Checkliste
5. Abbruch-Kriterien explizit
6. Was NICHT gebaut wird (Abgrenzung zu späteren Phasen)
7. **Zeige den User das File und frag nach Freigabe, bevor du implementierst**

Das verhindert, dass du Phase 3 mit einem ungenauen mentalen Modell beginnst.
