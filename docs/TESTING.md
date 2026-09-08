# MailDigest — Test-Protokoll (Hot & Cold)

> Zwei unabhängige Testrollen (NF-8): der **Hot-Tester** kennt den Code (Whitebox), der
> **Cold-Tester** kennt ihn nicht (Blackbox gegen die Spezifikation). Beide Rollen werden
> von getrennten Agenten-Läufen ausgefüllt.

## 1. Laufende Tests (jedes WP)

- Jedes WP liefert Unit-Tests für seine Funktionalität mit; `pytest` muss am Ende jedes
  WP grün sein.
- Der Angriffs-Korpus `tests/corpus/` (ab WP3) ist die gemeinsame Testbasis: echte-Welt-nahe
  `.eml`-Dateien inkl. Angriffsfälle. Jede Datei dokumentiert im Kommentar-Header ihren Zweck
  (`X-Test-Purpose`). Erzeugt wird er ausschließlich vom deterministischen Generator
  `tests/corpus/_make_corpus.py` — Dateien nicht von Hand ändern, sondern den Generator.
  Stand WP6: 01–20 Sanitizer-Fälle, 21–25 Phishing-Fälle für den Kritiker (CEO-Fraud,
  Paketdienst, Bank-Verifikation, Passwort-Reset, Rechnungs-Scam) mit den jeweils passenden
  Kopfzeilen-Signalen. Alle Payloads sind inert, alle Domains liegen unter `.example`.

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
- Test-Infrastruktur bereitstellen: lokaler Test-IMAP-Account (oder das in SPEC-CLI.md §4
  beschriebene `.eml`-Einspeise-Verfahren `maildigest test --eml <datei>` — es benutzt eine
  temporäre State-Datenbank, ist also beliebig oft wiederholbar und verändert den Betrieb
  nicht, ADR-057), Mock-Messenger-Endpoint (z. B. lokaler HTTP-Sink, dessen URL als
  Discord-Webhook konfiguriert wird), Mock- oder echter LLM-Key. Für Testläufe ohne
  Messenger gibt es `maildigest test --dry-run`, das die fertige Nachricht auf stdout
  schreibt.
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

- [x] Hot: Coverage-Ziele erreicht, Property-Tests grün, Findings-Log geführt (WP10, §5).
- [x] Cold: Report liegt vor (`tests/cold/REPORT.md`), alle Findings ≥ medium gefixt +
      Regressionstest (WP11, §6). Offen sind ausschließlich `info`-Befunde und ein bewusst
      anders gelöster Teilaspekt — beides in §6 begründet.
- [x] Adversarial-Suite Teil der CI (`pytest` gesamt): `tests/cold/test_cold_suite.py`
      fährt den Angriffs-Korpus des Cold-Tests über `maildigest.cli.main()` (73 Tests).
- [x] Invarianten-Review (SECURITY.md §7) dokumentiert (WP12): Befund je I1–I8, Methode und
      Datum; die mechanisch prüfbaren Aussagen sind als `tests/unit/test_invarianten.py`
      festgehalten (24 Tests, AST-basiert statt `grep` — die ausführlichsten Fundstellen für
      `expunge`/`parse_mode` sind die Begründungen, warum es sie nicht gibt). Darin auch der
      in SECURITY §6 angekündigte Lint-Check „kein `verify=False` irgendwo".
- [ ] **Zweite Cold-Runde mit frischem Agenten** (§3 Nachlauf verlangt sie bei
      Sicherheits-Findings ≥ high: CT-6 und CT-9). **Nicht erfüllt.** Sie hat nicht
      stattgefunden; der WP12-Agent hat den Code gelesen und kann eine Blackbox-Runde nicht
      ersetzen. Die Skripte in `tests/cold/scripts/` sind lauffähig und brauchen die
      Arbeitsumgebung aus dem Kopf von `tests/cold/REPORT.md`. Bis dahin bleibt NF-8
      `in-progress`; die Lücke steht als bekannte Grenze in README, CHANGELOG und
      SECURITY §7.2.

**M3 ist damit nicht vollständig erreicht.** Release 0.1.0 wird trotzdem getaggt: Alle
Befunde ab `medium` aus beiden Durchläufen sind behoben und mit Regressionstests belegt, und
die fehlende zweite Runde ist an vier Stellen offen benannt statt weggehakt.

## 5. Findings-Log (Hot-Testing)

Durchlauf WP10, 2026-09-02. Format: `HT-<nr>`, Severity, Modul, Beschreibung, Fix bzw.
Begründung. Severity-Maßstab ist die **Wirkung beim Nutzer**, nicht die Auffälligkeit im
Code: `high` = eine der Invarianten I1–I8 bricht in einer real erreichbaren Lage,
`medium` = eine Verteidigungsschicht hält nicht, die nächste fängt es auf,
`low` = kosmetisch/robustheitsseitig, `info` = bewusste Schichtgrenze, kein Fix.

**Herkunft:** HT-1 bis HT-6 wurden von den neuen Property-Tests
(`tests/unit/test_hot_properties.py`) gefunden, nicht durch Lesen — sie liegen alle in
Regex-Zeichenklassen und Reihenfolgen, die beim Durchsehen plausibel aussehen. HT-7 kam aus
der Fehlerinjektion, HT-8 bis HT-12 aus der gezielten Grenzfall-Suche.

### HT-1: Messenger-Markup überlebt in einer „bereits sicheren Form"
- Severity: **medium**
- Modul: `output/sanitizer.py` (`_RE_SAFE_SPAN`, `_DEFANGED_ATOM`)
- Beschreibung: `scrub_field` reicht vom WP3-Sanitizer erzeugte Formen unverändert durch
  (ADR-028), damit Marker nicht verschachteln. Die Zeichenklasse eines solchen Spans schloss
  aber die Markup-Zeichen `` ` ``, `*`, `|`, `~`, `\` mit ein. Ein einziges `[.]` im selben
  Wort genügte deshalb, um Markup an der Neutralisierung vorbeizuschleusen:
  `evil[.]com||spoiler||` und `x[.]y*fett*` kamen unverändert beim Nutzer an. Discord
  rendert Markdown im `content`-Feld, das ist dort also sichtbare Formatierung — und es
  verletzt die Kern-Property aus §2 Punkt 3 („kein Telegram/Discord-Markup"). Kein Link
  entstand dabei (`](` bricht `final_guard` weiterhin auf), deshalb medium statt high.
- Fix: Zeichenklassen `_DEFANGED_ATOM`/`_MARKER_ATOM` schließen Markup jetzt aus; ein Span
  mit Markup ist damit kein Span mehr und läuft durch die normale Neutralisierung.
  Regression: `test_ht1_markup_never_hides_inside_a_defanged_span`.

### HT-2: Langer Schema-Name hebelt den generischen `://`-Bruch aus
- Severity: **medium**
- Modul: `output/sanitizer.py` (`_RE_LIVE_SCHEME_ANY`)
- Beschreibung: Die Regel für „jedes andere Schema mit `://`" (ADR-036) verlangte eine
  Wortgrenze und einen Schema-Namen von höchstens 16 Zeichen. Stand links vom Schema keine
  Wortgrenze und war der Name länger, griff sie nicht: `einsehrlangeswortalsschema://ziel`
  behielt sein `://`. Die Domain dahinter wurde von der nachfolgenden Regel weiterhin
  defangt, ein klickbares Ziel entstand also nicht — die Zusage „kein lebendes Schema
  überlebt" aus docs/SECURITY.md §5 galt aber nicht.
- Fix: Schema-Name optional, Wortgrenze entfällt — gebrochen wird die Sequenz `://` selbst.
  In einer fertigen Nachricht ist `://` nie legitim. Regression:
  `test_ht2_a_long_scheme_name_is_still_broken`.

### HT-3: Roher `sqlite3.Error` verlässt die State-Schicht
- Severity: **medium**
- Modul: `state/db.py`
- Beschreibung: `StateError` wurde nur beim Öffnen erzeugt. Ein schreibgeschütztes
  Dateisystem oder eine volle Platte ließ `sqlite3.OperationalError` roh aus `claim()`,
  `mark_status()` und `enqueue_outbox()` heraus. Folgen: (a)
  `pipeline.classify_failure` traf die Klasse `state_error` nicht und schrieb
  `<stufe>_error` in Notiz und DB; (b) `cli.main` fängt gezielte Fehlertypen — der
  sqlite3-Fehler kam als **Traceback** auf das Terminal, und Tracebacks können laut
  ADR-047 Inhalte transportieren (I5).
- Fix: Dekorator `_wrap_sqlite_errors` auf allen öffentlichen `StateDB`-Methoden (ADR-060).
  Regression: `test_readonly_database_raises_the_documented_error`,
  `test_disk_full_is_reported_as_state_error`, `test_state_error_maps_to_a_stable_reason_class`.

### HT-4: Der Nachrichten-Split kann eine lebende Domain erzeugen
- Severity: **medium**
- Modul: `output/composer.py` (`_finalize`), `output/sanitizer.py` (`_defang_domain_match`,
  `_RE_DOMAINISH`)
- Beschreibung: Drei ineinandergreifende Lücken derselben Regel.
  (a) `_defang_domain_match` sah nur die **letzte** Marke an: `evil.com.123abc` blieb
  ungebrochen, weil `123abc` nicht TLD-förmig ist.
  (b) Die Token-Grenzen von `_RE_DOMAINISH` benutzten `\w` (Unicode) und schlossen `-`/`_`
  links aus: `evil.comÄ` und `-evil.example` wurden gar nicht erst als Token erkannt und
  nie defangt — Messenger verlinken dort trotzdem, weil ein Label nicht mit `-` beginnen
  darf und ihr Linkifier dahinter neu ansetzt.
  (c) `final_guard` lief **vor** `split_parts`. Der harte Schnitt erzeugt aus einem
  ungebrochenen Token ein Bruchstück mit neuem Anfang **und** neuem Ende — aus
  `-0000000.beispiel` wurde `0000000.beispiel`. Zugestellt wird der Teil, geprüft war die
  ganze Nachricht.
- Fix: Regel pro Marke ab der zweiten statt nur der letzten; ASCII-Token-Grenzen, Match darf
  hinter `-`/`_` beginnen; `_finalize` lässt den Nachbrenner nach dem Split über jeden Teil
  erneut laufen und teilt bei Bedarf nach (ADR-059). Regressionen:
  `test_ht4_domain_tokens_are_defanged_even_in_odd_shapes`,
  `test_ht4_no_part_becomes_a_live_domain_through_the_cut`,
  `test_ht4_numbers_and_abbreviations_stay_readable` (Gegenprobe: `3.14` bleibt lesbar).

### HT-5: Einbuchstabiges Schema entgeht der Summarizer-Nachkontrolle
- Severity: **low**
- Modul: `agents/summarizer.py` (`_URL_TOKEN_RE`)
- Beschreibung: Das URL-Muster verlangte mindestens zwei Zeichen vor `://`
  (`[a-z][a-z0-9+.\-]{1,15}`). `a://ziel.example` passierte die Nachkontrolle also
  unverändert **und ohne** `injection_suspected` — der Nutzer bekam den Hinweis „Mail
  enthielt Anweisungen an die KI" nicht zu sehen. Die WP7-Schicht entschärft das Muster
  weiterhin, es ging also nichts Klickbares raus.
- Fix: `{1,15}` → `{0,15}`. Regression:
  `test_ht5_single_letter_scheme_is_flagged_by_the_summarizer`.

### HT-6: Zweiter Scrub-Durchlauf öffnet ein Defang-Token wieder
- Severity: **low**
- Modul: `output/sanitizer.py` (`_RE_SAFE_SPAN`)
- Beschreibung: `javascript[:]` und ein alleinstehendes `[.]`/`[:]` galten nicht als
  „sichere Form". Ein zweiter `scrub_field`-Lauf entfernte die Klammern als Markup und
  lieferte wieder `javascript:`. Zweite Läufe gibt es wirklich: Der Composer scrubbt seine
  Hinweiszeilen erneut, und Sammel-Digest-Kopfzeilen sind beim Einreihen bereits
  output-sanitisiert (ADR-049). Da `final_guard` als letzte Stufe erneut bricht, erreichte
  die geöffnete Form den Nutzer nicht — die Schicht war aber nicht mehr idempotent, und
  Defense in Depth lebt davon, dass jede Schicht für sich hält.
- Fix: Aktions-Schemata (`javascript`, `data`, `tg`, …) und das nackte Defang-Token in
  `_RE_SAFE_SPAN` aufgenommen. Regression:
  `test_ht6_defanged_forms_survive_a_second_scrub`.

### HT-7: Keine Befunde bei der Fehlerinjektion
- Severity: **info**
- Modul: `pipeline.py`, `agents/`, `llm/schema.py`, `delivery.py`
- Beschreibung: 50 Injektionsfälle (`tests/unit/test_hot_fault_injection.py`) an beiden
  LLM-Positionen — Timeout, Rate-Limit, Transportfehler, `RuntimeError`, `MemoryError`,
  leere Antwort, `{}`, `null`, `[]`, abgeschnittenes JSON, 50 000 Zeichen Müll,
  schema-valides Angriffs-JSON mit URLs in jedem Feld, 200 000-Zeichen-Felder, erfundene
  Zusatzfelder (`tool_calls`, `system_prompt_override`), Messenger-Absturz mitten in einer
  mehrteiligen Nachricht, kaputter Composer, kaputte State-DB. **Kein Fall** lieferte eine
  unsanitierte Zustellung, keiner ließ eine Exception aus `process_mail` heraus. Die
  Metadaten-Notiz enthielt in keinem Fall Mail-Inhalt.
- Fix: keiner nötig — Eintrag dokumentiert den Null-Befund (Vorgabe §2 Punkt 5).

### HT-8: Marker-Nummerierung folgt der Erkennungsreihenfolge, nicht dem Text
- Severity: **info**
- Modul: `sanitize/links.py`
- Beschreibung: Die Pässe laufen in fester Reihenfolge (URLs → `mailto:` → `tel:` → `www.`
  → obfuskierte → nackte Domains). Ein `mailto:` weiter hinten im Text bekommt deshalb eine
  kleinere Nummer als eine Domain weiter vorne; im Text steht dann `[Link #2] … [Mail #1]`.
- Fix: keiner. Die Nummer dient laut ADR-028 der Zuordnung Marker ↔ Fußnoteneintrag, und
  die Eindeutigkeit ist gegeben (durch `test_link_collector_records_every_marker` geprüft).
  Eine Umnummerierung nach Textposition würde die Sammel-Logik über mehrere Textteile
  hinweg verkomplizieren, ohne eine Anforderung zu erfüllen.

### HT-9: Modell kann einen `[Link #n: …]`-Marker fälschen
- Severity: **info**
- Modul: `output/sanitizer.py` (`_RE_SAFE_SPAN`)
- Beschreibung: Schreibt das Modell (oder über es der Angreifer) `[Link #1: sparkasse.example]`
  in ein Summary-Feld, passiert die Form als „sichere Form" unverändert; `final_guard`
  defangt nur die Domain. Der Nutzer sieht einen Marker, der von einem echten nicht zu
  unterscheiden ist, obwohl die Mail dort keinen Link hatte.
- Fix: keiner. Ein klickbares Ziel entsteht nicht (I3 hält), und die Form ist inhärent
  mehrdeutig: Der Summarizer sieht die echten Marker im Datenblock und darf sie zitieren —
  eine Unterscheidung „echt/erfunden" wäre nur mit einer pro Nachricht zufälligen
  Marker-Kennung möglich. Das ist eine Formatänderung und gehört, wenn überhaupt, in ein
  eigenes WP. Für den Nutzer ist die Falschaussage nicht schädlicher als eine erfundene
  Zusammenfassung, gegen die der Kritiker (`summary_accurate`) steht.

### HT-10: `max_text_chars` wird um die Länge des Kürzungsmarkers überschritten
- Severity: **info**
- Modul: `sanitize/sanitizer.py` (`_take_budget`)
- Beschreibung: Bei Budget 30 000 ist `body_text` 30 010 Zeichen lang — `\n[gekürzt]` kommt
  nach der Kürzung dazu. Bei ausgeschöpftem Budget bekommt zusätzlich jeder Anhangs-Text
  den Marker (max. 20 Stück, also ≤ 200 weitere Zeichen).
- Fix: keiner. Die Überschreitung ist konstant begrenzt und beabsichtigt (der Marker soll
  sichtbar sein); der Schutzzweck des Limits — kein unbegrenzter Text ins LLM — ist
  gewahrt. Festgehalten durch `test_text_budget_boundaries`.

### HT-11: `scrub_plain` entfernt Klammern bereits defangter Formen
- Severity: **info**
- Modul: `output/sanitizer.py`
- Beschreibung: `scrub_plain` (Domain, Anzeigename, Dateiname) läuft ohne Link-Erkennung und
  löscht `[`/`]` als Markup. Aus `evil[.]com` wird dabei kurzzeitig wieder `evil.com`.
- Fix: keiner. `final_guard` defangt unmittelbar danach erneut, und seit HT-4 greift die
  Regel auch für die Formen, die vorher durchrutschten. Property-Test
  `test_scrub_plain_output_is_never_clickable` deckt die Kette ab.

### HT-12: `compose_plain` scrubbt seine Felder nicht
- Severity: **info**
- Modul: `output/composer.py`
- Beschreibung: `compose_plain` ruft nur `_finalize` (Nachbrenner + Split), nicht
  `scrub_field`. Untrusted Text käme dort also ohne Feld-Scrub durch — Steuerzeichen und
  Markup blieben erhalten.
- Fix: keiner. Der einzige Aufrufer ist die CLI-Testnachricht, deren Text im Code steht
  (ADR-054); der Docstring sagt das ausdrücklich. Festgehalten ist es dadurch, dass der
  Property-Test `test_split_never_produces_an_unsafe_part` seine Eingabe **explizit** erst
  durch `scrub_field` schickt — wer das ändert, sieht den Grund im Test.

### Coverage-Endstand (NF-6)

Gemessen mit `.venv/bin/pytest --cov=src/maildigest --cov-report=term-missing`:

| Bereich | Ziel (NF-6) | vor WP10 | nach WP10 (1103 Tests) | Stand WP12 (1278 Tests) |
|---------|-------------|----------|------------------------|-------------------------|
| `sanitize/` | ≥ 90 % | 92,3 % (48 offen) | **98 %** (622 Anw., 10 offen) | **98 %** (645 Anw., 11 offen) |
| `output/` | ≥ 90 % | 98,0 % | **99 %** (308 Anw., 4 offen) | **99 %** (341 Anw., 2 offen) |
| gesamt | ≥ 80 % | 95,5 % | **97 %** (3824 Anw., 122 offen) | **97 %** (3986 Anw., 118 offen) |

Die verbliebenen Lücken sind Verzweigungen, die nur auf anderen Plattformen erreichbar sind
(`__main__.py`, `# pragma: no cover`-Zweige) oder HTTP-Fehlerpfade der Provider-/Messenger-
Adapter, die bereits über `httpx.MockTransport` in ihren eigenen Tests abgedeckt sind.

### Neue Testdateien aus WP10

| Datei | Inhalt |
|-------|--------|
| `tests/unit/test_hot_properties.py` | Property-Based-Tests (hypothesis, ADR-058) — die vier Kern-Properties aus §2 Punkt 3 |
| `tests/unit/test_hot_edge_cases.py` | Grenzfälle aus PLAN WP10 + Regressionstests zu HT-1/2/4/5/6 |
| `tests/unit/test_hot_fault_injection.py` | Fehlerinjektion an jeder Stufe, beide LLM-Positionen (§2 Punkt 4) |
| `tests/unit/test_hot_state_concurrency.py` | SQLite-Locking, zwei Prozesse/zwölf Threads, Wiederanlauf, Schema |
| `tests/unit/test_hot_schedule.py` | Digest-Uhrzeit (Mitternacht, Zeitumstellung) und die 1-Stunden-Schranke der Outbox |
| `tests/unit/test_hot_cli_robustness.py` | Kaputte Config, fehlende Rechte, keine Secrets in Fehlermeldungen |
| `tests/unit/test_hot_sanitize_error_paths.py` | Defensive Zweige des Sanitizers und der PDF-Kindprozess |

## 6. Findings-Log (Cold-Testing)

Durchlauf WP11, 2026-09-08. Der vollständige Blackbox-Report mit Repro-Schritten,
Abdeckungstabelle und Gesamturteil steht in **`tests/cold/REPORT.md`**; er wird nicht
nachträglich geändert — er ist das Protokoll dessen, was ein Agent ohne Code-Kenntnis
gemessen hat. Diese Tabelle führt den Nachlauf (§3): Triage, Fix und Regressionstest.

Severity-Maßstab wie in §5 (Wirkung beim Nutzer). Die Fixes liefen in drei parallelen
Läufen (Ausgabe/Kritiker, Ingest/Zustellung, CLI); die Finalisierung hat sie
zusammengeführt, gegengeprüft und die vier offen gebliebenen Punkte nachgezogen.

| CT | Severity | Titel (Kurzform) | Status |
|----|----------|------------------|--------|
| CT-1 | high | Globale Optionen vor dem Kommandonamen wirkungslos | **gefixt** (ADR-068) — `test_cli.py::test_ct1_*`, `test_cold_suite.py::test_ct1_*` |
| CT-2 | low | Fehlermeldung verweist auf ARCHITECTURE statt SPEC-CLI | **gefixt** — `test_config.py::test_ct2_*`, `test_cold_suite.py::test_ct2_*` |
| CT-3 | info | `[llm.critic]` unvollständig; Frage-Hinweis ohne Frage | **gefixt** — `test_cli.py::test_ct3_*`, `test_cold_suite.py::test_ct3_*` |
| CT-4 | medium | `test --dry-run` behauptet eine Zustellung, zeigt die Notiz nicht | **gefixt** (ADR-071) — `test_cli_e2e.py::test_ct4_*`, `test_cold_suite.py::test_ct4_*` |
| CT-5 | low | Out-of-range-Portwert endet mit Exit 1 statt 2 | **gefixt** (ADR-069) — `test_cli.py::test_ct5_*`, `test_cold_suite.py::test_ct5_*` |
| CT-6 | high | Injection-Verdacht hängt allein am LLM (F-SEC-5) | **gefixt** (ADR-061) — `test_summarizer.py::test_ct6_*`, `test_cold_suite.py::test_ct6_*` |
| CT-7 | medium | Markdown erreicht den Messenger (F-SEC-3) | **gefixt** (ADR-062) — `test_output_sanitizer.py::test_ct7_*`, Property-Tests, `test_cold_suite.py` |
| CT-7a | medium | Derselbe Leak ohne Modell, über die Betreffzeile der Notiz | **gefixt** (ADR-062) — `test_ct7a_*`, `test_cold_suite.py::test_ct7a_*` |
| CT-8 | medium | Nachrichtenstruktur ist fälschbar (Fake-Hinweiszeile) | **gefixt** (ADR-062) — `test_ct8_*`, `test_cold_suite.py::test_der_angreifer_kann_keine_programmzeile_faelschen` |
| CT-9 | high | Unbedingtes EXPUNGE nach jedem Gelesen-Flag (F-ING-1) | **gefixt** (ADR-064) — `test_ingest_client.py::test_ct9_*`, `test_ingest_poll.py::test_ct9_*` |
| CT-10 | medium | Bilanzzeile zählt zugestellte Nachrichten immer als 0 | **gefixt** (ADR-070) — `test_cli_e2e.py::test_ct10_*` |
| CT-11 | medium | Harte Signale heben nie auf `high`, kein Banner (F-CRIT-3) | **gefixt** (ADR-063) — `test_critic_signals.py::test_ct11_*`, `test_cold_suite.py::test_ct11_*` |
| CT-12 | low | „Postfach nicht erreichbar" bei fehlendem Zielordner | **gefixt** (ADR-065) — `test_ingest_client.py::test_ct12_*`, `test_ingest_poll.py::test_ct12_*` |
| CT-13 | medium | Retry stellt bereits gesendete Teile erneut zu | **gefixt** (ADR-066) — `test_delivery.py::test_ct13_*` |
| CT-14 | medium | `[links] footnote = true` ohne Wirkung | **gefixt** (ADR-072) — `test_output_composer.py::test_ct14_*`, `test_cold_suite.py::test_ct14_*` |
| CT-15 | medium | Divergierender HTML-Teil bei `multipart/alternative` | **gefixt** (ADR-067) — `test_sanitize_mail.py::TestCt15*`, `test_output_composer.py::test_ct15_*`, `test_cold_suite.py::test_ct15_*` |
| CT-16a | info | `(1 Teil)` statt des wörtlichen `(N Teile)` der Spec | **offen, bewusst** — der Code hat recht (deutsche Grammatik), der Vertrag war unpräzise. Klarstellung in SPEC-CLI §4 `test`. Kein Code-Fix. |
| CT-16b | info | EOF auf stdin endet mit Exit 1 statt 2 | **gefixt** (ADR-069) — `test_cli.py::test_ct16_*`, `test_cold_suite.py::test_ct16b_*` |
| CT-16c | info | LLM-Versuchszahlen schwanken (9/3/2) | **offen, bewusst** — drei multiplikative Retry-Ebenen, jede mit eigener Begründung. Kein Bug; aufgeschlüsselt in ARCHITECTURE §6, Verweis aus F-SEC-7. |
| CT-16d | info | REQUIREMENTS F-SEC-4 nennt fälschlich `text/html` | **gefixt (Doku)** — REQUIREMENTS F-SEC-4 korrigiert; das Verhalten war richtig, die Anforderung falsch. |
| CT-16e | info | Positivbefund: `log_level=INFO` unterdrückt Tracebacks | **kein Befund** — nichts zu tun. |

**Bewusste Abweichung bei CT-6.** Ein Teilaspekt des Befunds („entfernter versteckter Text
setzt `injection_suspected`") ist absichtlich anders gelöst: `hidden_text_removed` setzt das
Flag **nicht**. Unsichtbarer Text ist in Newslettern der Regelfall (Preheader mit
`display:none`); die Zeile „Mail enthielt Anweisungen an die KI (ignoriert)" wäre dort
schlicht falsch und würde die Warnung entwerten — Warnmüdigkeit statt Warnung. Das Signal
erreicht den Nutzer stattdessen als eigener, wörtlich zutreffender Hinweis „versteckter Text
im HTML entfernt" (ADR-061). Der Kern des Befunds — deterministische Signale erreichen den
Nutzer nie — ist damit behoben.

### Befunde der Finalisierung (Review der drei Fix-Läufe)

Die Zusammenführung hat vier Lücken gefunden, die keiner der drei Läufe schließen konnte
oder wollte; sie sind in dieser Fassung mit erledigt:

- **CT-2 und CT-14 lagen zwischen den Zuständigkeiten.** Beide Fixes liegen in Dateien, die
  einem jeweils anderen Parallel-Agenten zugewiesen waren; beide wurden mit exaktem
  Patchvorschlag als „nicht gefixt" gemeldet. Nachgezogen.
- **Das CT-15-Signal erreichte den Nutzer nicht.** Der Sanitizer berechnete
  `html_divergent`, aber weder `output/composer._hints_line` noch
  `agents/critic.collect_signals` werteten es aus — das Feld war folgenlos. Nachgezogen.
- **Ein Fremdtest kodierte den CT-10-Bug.**
  `test_runner_e2e.py::test_crash_between_commit_and_delivery_loses_nothing` erwartete
  `delivery.delivered == 1` und fixierte damit die alte, fehlerhafte Buchführung. Auf
  `1 + stats.ingest.processed` korrigiert (ADR-070).
- **Die CT-6-Phrasenliste war zu breit.** `du bist jetzt …` und `system prompt` allein
  hätten Alltagsdeutsch getroffen („du bist jetzt dran", „wir besprechen den System-Prompt
  im Meeting") — derselbe Fehlalarm-Mechanismus, wegen dessen `hidden_text_removed`
  ausgeschlossen wurde. Die Muster verlangen jetzt ein Objekt (`… ein Sprachmodell`,
  `nenne mir deinen Systemprompt`).

### Was automatisiert ist — und was Skript bleibt

`tests/cold/test_cold_suite.py` (73 Tests, Teil von `pytest`) fährt den Angriffs-Korpus
`tests/cold/mails/*.eml` über dieselbe Eintrittstür wie der Cold-Tester
(`maildigest.cli.main()`). Ersetzt sind nur die drei Außenkontakte, für die er Mocks
gestartet hatte: LLM-**Provider** (nicht der Agent — sonst wäre die deterministische
Nachkontrolle aus CT-6/CT-11 umgangen und der Test wertlos), Messenger und IMAP.
Automatisiert sind: die Kern-Property über den gesamten Korpus gegen ein vollständig
übernommenes Modell, die Struktur-Fälschung (CT-8), CT-4, CT-6, CT-7/7a, CT-11, CT-14,
CT-15 sowie die reinen CLI-Befunde CT-1, CT-2, CT-3, CT-5, CT-16b und eine F-SEC-8-Probe
mit den markierten Fake-Secrets des Cold-Tests.

**Nicht automatisiert, bleibt dokumentiertes manuelles Skript** (`tests/cold/scripts/`,
wörtlich so im Repo, wie der Cold-Tester sie laufen ließ; von pytest ausgenommen über
`tests/cold/conftest.py` und im Lint über `pyproject.toml`):

| Skript | Warum nicht in pytest |
|--------|------------------------|
| `imap_server.py` | Echter IMAP4rev1-Server über TLS-Socket. CT-9/CT-12 sind stattdessen über die Postfach-Attrappen in `test_ingest_client.py`/`test_ingest_poll.py` abgedeckt, die jedes rohe Kommando protokollieren und bei `flag()`/`move()`/`delete()`/`expunge()` hart auffliegen. |
| `sink_server.py` | HTTP-Sink mit Abbruchmodi (`die_after_1`, `http500`). Der Verbindungsabbruch mitten in einer mehrteiligen Nachricht (CT-13) ist in `test_delivery.py` ohne Socket nachgebaut. |
| `mock_llm.py` | OpenAI-kompatibler HTTP-Endpunkt. In der Suite als `FakeProvider` auf der Provider-Schnittstelle nachgebaut — dieselben Modi (`nice`, `raw`, `broken`), ohne Port und ohne Wartezeit. |
| `split_test.py` | 210 Läufe × Prozessstart über 28 Offsets (Minuten Laufzeit). Die Eigenschaft ist als Property-Test in `test_hot_properties.py` abgedeckt (ADR-059). |
| `check_sink.py`, `mocks.sh`, `run_mails.sh`, `make_mails.py` | Auswertungs- und Setup-Helfer der Blackbox-Umgebung; ohne diese Umgebung gegenstandslos. `make_mails.py` hat den Korpus unter `tests/cold/mails/` erzeugt. |

Für eine zweite Cold-Runde (§3 Nachlauf) sind die Skripte damit weiterhin lauffähig; sie
brauchen die Arbeitsumgebung aus dem Report-Kopf und absolute Pfade darin.
