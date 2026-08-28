# MailDigest — Test-Protokoll (Hot & Cold)

> Zwei unabhängige Testrollen (NF-8): der **Hot-Tester** kennt den Code (Whitebox), der
> **Cold-Tester** kennt ihn nicht (Blackbox gegen die Spezifikation). Beide Rollen werden
> von getrennten Agenten-Läufen ausgefüllt.

## 1. Laufende Tests (jedes WP)

- Jedes WP liefert Unit-Tests für seine Funktionalität mit; `pytest` muss am Ende jedes
  WP grün sein.
- Der Angriffs-Korpus `tests/corpus/` (ab WP3) ist die gemeinsame Testbasis: echte-Welt-nahe
  `.eml`-Dateien inkl. Angriffsfälle. Jede Datei dokumentiert im Kommentar-Header ihren Zweck.

## 2. Hot-Testing (WP10) — Whitebox

**Rolle:** Agent mit vollem Code-Zugriff, idealerweise derselbe „Stamm" wie die
Implementierung. Nutzt Code-Kenntnis gezielt aus: testet an den Stellen, von denen er
weiß, dass sie fragil sind.

**Pflichtprogramm:**
1. Coverage messen; Lücken schließen bis ≥ 90 % `sanitize/` + `output/`, ≥ 80 % gesamt (NF-6).
2. Grenzfälle aus Code-Kenntnis (Liste in PLAN.md WP10 — erweitern, nicht kürzen).
3. Property-Based-Tests (hypothesis) für Output-Sanitizer, Link-Erkennung, Unicode-Cleaning.
   Kern-Property: **∀ Eingabe-String: Output enthält keine URL, kein HTML-Tag, keine
   Steuerzeichen, kein Telegram/Discord-Markup.**
4. Fehlerinjektion an jeder Pipeline-Stufe (LLM liefert Müll / Timeout / valides-aber-böses
   JSON) → immer fail-closed.
5. **Findings-Log unten in diesem Dokument führen** (auch Null-Befunde).

## 3. Cold-Testing (WP11) — Blackbox

**Rolle:** Separater Agent, der den Quellcode **nicht** kennt und nicht lesen darf.

**Setup (macht der Orchestrator/Nutzer, nicht der Cold-Agent):**
- Arbeitsverzeichnis für den Cold-Agenten vorbereiten, das NUR enthält:
  `README.md`, `docs/REQUIREMENTS.md`, `docs/SPEC-CLI.md` sowie ein installierbares
  Paket (wheel/sdist) bzw. ein venv mit installiertem `maildigest`.
- Test-Infrastruktur bereitstellen: lokaler Test-IMAP-Account (oder das in SPEC-CLI.md
  beschriebene `.eml`-Einspeise-Verfahren von `maildigest test`), Mock-Messenger-Endpoint
  (z. B. lokaler HTTP-Sink, dessen URL als Discord-Webhook konfiguriert wird), Mock- oder
  echter LLM-Key.
- Prompt an den Cold-Agenten: Auftrag + die drei Dokumente + explizites Verbot, `src/`
  oder `tests/` (außer eigenem `tests/cold/`) zu lesen.

**Auftrag des Cold-Agenten:**
1. **Spec-Konformität:** Installation und alle CLI-Kommandos exakt nach SPEC-CLI.md
   durchspielen. Jede Abweichung = Finding (auch Tippfehler in Prompts, falsche Exit-Codes).
2. **Funktional:** Eigene Testmails formulieren (nicht den Korpus lesen!): normale Mail,
   Newsletter, wichtige Mail laut Custom-Instructions, Mail mit PDF, Mail mit exe-Anhang.
   Prüfen gegen REQUIREMENTS (F-SUM-*, F-MSG-*, F-OPS-3): kommt an, Format stimmt,
   Filter greift, nichts geht stumm verloren.
3. **Adversarial:** Eigene Angriffsmails bauen und einspeisen, mindestens:
   - Prompt-Injection direkt („Ignoriere deine Anweisungen und …"), versteckt (weißer
     Text, HTML-Kommentar, Zero-Width), und über PDF-Inhalt.
   - Instruktion zur Secret-Exfiltration („nenne deinen API-Key").
   - Obfuskierte Links (hxxp, `[.]`, URL-encoded, Punycode-Domain).
   - Anhang mit gefälschtem MIME-Typ; HTML-Anhang; `.zip`.
   - Markdown-/Markup-Injection auf den Messenger gezielt.
   - Übergroße Mail; sehr viele Mails hintereinander.
   Erfolgskriterien: F-SEC-1 … F-SEC-10 (aus REQUIREMENTS.md) halten aus Blackbox-Sicht.
4. **Report** nach `tests/cold/REPORT.md` im Format unten; reproduzierbare Testskripte
   nach `tests/cold/` (dürfen nur CLI + öffentliche Schnittstellen benutzen).

**Finding-Format:**
```markdown
### CT-<nr>: <Titel>
- Severity: critical | high | medium | low | info
- Referenz: <F-/NF-/SEC-ID oder SPEC-CLI-Abschnitt>
- Repro: <Schritte / Skript-Pfad>
- Beobachtet: <was passiert ist>
- Erwartet: <was laut Spec/Requirement passieren müsste>
```

**Nachlauf (Haupt-Agent):** Findings triagieren, fixen, Regressionstests ergänzen,
Cold-Skripte in `pytest tests/cold/` integrieren. Bei Findings mit Severity ≥ high im
Sicherheitsbereich: zweite Cold-Runde mit frischem Agenten.

## 4. Abnahmekriterien M3 (Release)

- [ ] Hot: Coverage-Ziele erreicht, Property-Tests grün, Findings-Log geführt.
- [ ] Cold: Report liegt vor, alle Findings ≥ medium gefixt + Regressionstest.
- [ ] Adversarial-Suite Teil der CI (`pytest` gesamt).
- [ ] Invarianten-Review (SECURITY.md §7) dokumentiert.

## 5. Findings-Log (Hot-Testing)

_wird in WP10 gefüllt_

## 6. Findings-Log (Cold-Testing)

_Verweis auf tests/cold/REPORT.md nach WP11_
