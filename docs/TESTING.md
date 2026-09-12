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
      festgehalten (24 Tests, seit der Fixrunde 27 — §7, HC-38; AST-basiert statt `grep` — die ausführlichsten Fundstellen für
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

### HT-13: Ketten von Zeilen-Markdown überlebten den Scrub
- Severity: **low**
- Modul: `output/sanitizer.py` (`scrub_plain`, Rundenzahl)
- Beschreibung: Gefunden vom Property-Test mit `⚠️# # #`. Jede Runde entfernt nur **einen**
  Marker je Zeile, und ein vorangestelltes Struktur-Emoji verschiebt den Zeilenanfang um
  eine weitere Runde — bei drei Runden blieb ein `#` stehen. Ein alleinstehendes `#`
  rendert zwar keine Überschrift, die zugesicherte Eigenschaft („kein Zeilenanfangs-Markdown
  überlebt") galt aber nicht mehr.
- Fix: eigene, großzügige Rundenzahl für diesen Schritt (`_MAX_LINE_MARKUP_ROUNDS = 12`)
  statt der gemeinsamen 3. Regression: `test_ht13_ketten_von_zeilen_markdown_ueberleben_nicht`.
- Herkunft: Der Fall tauchte erst Monate nach WP10 auf — die Property zieht bei jedem Lauf
  neue Beispiele. Genau dafür ist sie da.

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

## 7. Findings-Log (Abschluss-Testrunde, HC-1 … HC-38)

Durchlauf 2026-09-09 bis 2026-09-11 auf Stand `13cb859`; der vollständige Bericht mit Repro,
Ursache und Skeptikerprüfung steht in **`docs/TESTRUNDE-HOT-COLD.md`** und wird nicht
nachträglich geändert. Diese Tabelle führt den Nachlauf: Zuständigkeit, Status, Regressionstest.
Der Fixplan mit Paketschnitt, Entscheidungen und Akzeptanzkriterien ist
[docs/PLAN-FIXRUNDE.md](PLAN-FIXRUNDE.md).

Severity-Maßstab wie in §5 und §6 (Wirkung beim Nutzer). Die Fixes liefen in neun Paketen über
vier Wellen (`4697231`, `4b6f709`, `a9a4d87`, `64c0ae4`); die Testzahl stieg dabei von 1354 auf
1565.

| HC | Sev. | Titel (Kurzform) | Status | Tests | ADR |
|----|------|------------------|--------|-------|-----|
| HC-1 | high | Betreff > 100 Zeichen bricht den Betrieb ohne Modell fail-closed ab | **gefixt** | `test_offline.py::test_hc1_langer_betreff_wird_gekuerzt_statt_fail_closed`, `::test_hc1_betreff_von_genau_101_zeichen_geht_durch` | ADR-076 (N) |
| HC-2 | medium | Verarbeiteter Anhang verschwindet ohne Modell stumm | **gefixt** | `test_offline.py::test_hc2_*` (5), `test_cli.py::test_hc38_selbsttest_im_werkszustand_zeigt_den_anhang` | ADR-076 (N), ADR-034 (N) |
| HC-3 | medium | `connect-llm` setzt fremde `base_url`, zeigt falsche Anleitung | **gefixt** | `test_cli_connect.py::test_hc3_*` (4), `test_providers.py::test_hc3_*` (2) | ADR-075 (N), ADR-076 (N) |
| HC-4 | medium | Anbieter-Fehlertext erreicht das Terminal ungefiltert (ANSI) | **gefixt** | `test_llm_providers.py::test_hc4_*` (2), `test_cli_connect.py::test_hc4_*` (2), `test_hot_cli_robustness.py::test_hc4_*` (6) | ADR-055 (N) |
| HC-5 | medium | Gefälschte Datenblock-Marker werden still entfernt statt geflaggt | **gefixt** | `test_summarizer.py::test_hc5_*`, `test_sanitize_mail.py::TestHc5GefaelschteDatenblockMarker`, `test_cold_suite.py::test_hc5_hc21_*` | ADR-061 (N) |
| HC-6 | medium | Split kann eine gefälschte Programmzeile am Teilanfang erzeugen | **gefixt** | `test_output_sanitizer.py::test_hc6_*`, `test_hot_properties.py::test_hc6_*`, `test_cold_suite.py::test_hc6_*` | ADR-062 (N), ADR-040 (N) |
| HC-7 | medium | Markdown in der Link-Fußnote wird nicht neutralisiert | **gefixt** | `test_output_composer.py::test_hc7_*`, `test_sanitize_links.py::…::test_hc7_*` | ADR-062 (N) |
| HC-8 | medium | Steuerzeichen U+0000 erreicht den zugestellten Nachrichtenteil | **gefixt** | `test_hot_properties.py::test_hc8_*`, `test_sanitize_links.py::…::test_hc8_*` (2), `test_output_sanitizer.py::test_hc8_*` | — |
| HC-9 | medium | Nackte IPv4 bleibt mit Nachbarzeichen ungebrochen | **gefixt** | `test_output_sanitizer.py::test_hc9_*` (2), `test_hot_properties.py::test_scrub_field_output_is_never_clickable` | ADR-036 (N) |
| HC-10 | medium | Dedupe-Key ist der vom Angreifer gesetzte `Message-ID`-Header | **gefixt** | `test_ingest_poll.py::test_hc10_*` (5), `test_state_db.py::test_hc10_*` (6), `test_ingest_rawmail.py::test_hc10_*` (3), `test_output_composer.py::test_hc10_*` (2) | **ADR-079**, ADR-018/019/048 (N) |
| HC-11 | medium | Mailstämmiger Text landet über `extra_forbidden` im Log | **gefixt** | `test_llm_schema.py::test_hc11_*` (2), `test_hot_fault_injection.py::test_hc11_*` | ADR-024 (N) |
| HC-12 | medium | `/digest` verkürzt die Wartezeit nicht | **gefixt** | `test_runner.py::test_hc12_*` (2) | **ADR-080**, ADR-077 (N) |
| HC-13 | medium | Nach `/digest` werden alle weiteren Befehle verworfen | **gefixt** | `test_runner.py::test_hc13_*` (2) | ADR-077 (N) |
| HC-14 | low | Oberfläche englisch, Vertrag deutsch | **gefixt** | `test_hc14_spec_literals.py` (19 Fälle) | **ADR-083** |
| HC-15 | medium | `connect-mail` lässt gesperrte Anbieter als Mailadresse durch | **gefixt** | `test_cli_connect.py::test_hc15_*` (4), `test_providers.py::test_hc15_*` | ADR-075 (N) |
| HC-16 | low | Testnachricht nennt ungültige Config-Sektion, geht an alle Messenger | **erledigt in `14ad9ed`** (ADR-078) | `test_cli.py::test_testnachricht_nennt_keine_ungueltige_config_sektion` | ADR-078 |
| HC-17 | low | Selbsttest-Vorspann behauptet bei `--eml` die Beispielmail | **gefixt** | `test_cli_e2e.py::test_hc17_*` (2) | — |
| HC-18 | low | `init` schreibt nicht den vollständigen Feldsatz aus §5 | **gefixt** (Rest; Feldsatz war in `14ad9ed` vorweggenommen) | `test_cli.py::test_hc18_*`, `test_spec_cli.py::test_hc18_*`, `test_cli_connect.py::test_hc18_*` | ADR-076 (N) |
| HC-19 | low | Befehls-Hinweis entfällt bei `connect-messenger --chat-id` | **gefixt** | `test_cli_connect.py::test_hc19_*` (2) | — |
| HC-20 | medium | Konfiguration wird nicht atomar geschrieben | **gefixt** | `test_hot_cli_robustness.py::test_hc20_*` (3) | **ADR-081** |
| HC-21 | medium | Phrasenliste verpasst die gängigsten Formulierungen | **gefixt** | `test_summarizer.py::test_hc21_*` (3, 13 Fälle), `test_cold_suite.py::test_hc5_hc21_*` | ADR-061 (N), ADR-076 (N) |
| HC-22 | low | Zu langer Dateiname wird ohne das zugesagte `…` gekürzt | **gefixt** | `test_sanitize_attachments.py::…::test_hc22_*` (4), `test_output_composer.py::test_hc22_*` | ADR-040 (N) |
| HC-23 | low | Absender-Anzeigename wird nie RFC-2047-dekodiert | **gefixt** | `test_ingest_rawmail.py::test_hc23_*` (5) | ADR-020 (N) |
| HC-24 | low | Marken > 63 Zeichen hebeln `_RE_DOMAINISH` aus; Orakel spiegelt die Schranke | **gefixt** | `test_output_sanitizer.py::test_hc24_*` (2), `test_hot_properties.py::test_scrub_*_output_is_never_clickable` | ADR-036 (N) |
| HC-25 | low | Outbox-Fristen hängen an der Wanduhr | **gefixt** | `test_delivery.py::test_hc25_*` (5) | ADR-048 (N) |
| HC-26 | low | Sammel-Digest wird von einem IMAP-Ausfall mitblockiert | **gefixt** | `test_runner.py::test_hc26_*` (3) | ADR-049 (N) |
| HC-27 | low | `run --once` fragt nie Befehle ab | **gefixt** | `test_runner.py::test_hc27_*` (3) | **ADR-080** |
| HC-28 | low | `/status`-Antwort umgeht `scrub_field` | **gefixt** | `test_runner.py::test_hc28_status_antwort_scrubbt_den_ordnernamen` | ADR-077 (N) |
| HC-29 | low | Quadratische Laufzeit in `_redact_tokens` | **gefixt** | `test_summarizer.py::test_hc29_*` (2, 3 Fälle) | — |
| HC-30 | low | `Retry-After: nan` verlässt die Fehler-Taxonomie | **gefixt** | `test_llm_providers.py::test_hc30_*` (2), `test_messenger_adapters.py::test_hc30_*` (2) | — |
| HC-31 | low | README-„Grenzen" und CHANGELOG kannten die Fernauslösung nicht | **erledigt in `14ad9ed`** (der Commit-Text nennt es fälschlich „HC-19") | — | ADR-078 |
| HC-32 | low | `connect-mail`: unpassender Anbieter-Hinweis, keine Serverantwort | **gefixt** (vorher selbst nachgestellt) | `test_ingest_client.py::test_hc32_*` (2), `test_cli_connect.py::test_hc32_*` (3) | — |
| HC-33 | low | PGP/S-MIME-Mail sieht aus wie eine inhaltsleere Mail | **gefixt** (vorher selbst nachgestellt) | `test_sanitize_mail.py::TestHc33VerschluesselteMail` (4), `test_output_composer.py::test_hc33_*` (2), `test_critic_signals.py::test_hc33_*` (3) | **ADR-082** |
| HC-34 | low | Fehlendes IMAP-Passwort als „Postfach nicht erreichbar" | **gefixt** (vorher selbst nachgestellt) | `test_cli.py::test_hc34_run_meldet_fehlendes_passwort_als_konfigurationsfehler` | — |
| HC-35 | low | Bei nicht bestätigter Zustellung fehlt die Zeile `5/5 …` | **gefixt** (vorher selbst nachgestellt) | `test_cli_e2e.py::test_hc35_nicht_bestaetigte_zustellung_hat_eine_fuenfte_zeile` | — |
| HC-36 | low | `connect-llm --provider` zeigt immer die Groq-Anleitung | **in HC-3 aufgegangen** (gleiche Ursache, dort gefixt) | `test_cli_connect.py::test_hc3_provider_anthropic_zeigt_die_anthropic_anleitung` | ADR-075 (N) |
| HC-37 | info | Zwei Schichtgrenzen der Befehlserkennung | **gefixt (Doku)** — (a) README/SPEC beschreiben die tolerante Erkennung jetzt korrekt, das Verhalten war richtig; (b) als Schichtgrenze unten festgehalten | `test_commands.py::test_bekannte_befehle_werden_erkannt`, `::test_botname_anhang_wird_abgetrennt`, `::test_argumente_werden_ignoriert_nicht_gelesen`, `::test_freier_text_wird_verworfen` | — |
| HC-38 | low | Testabdeckung Standardmodus, `/digest`-Zweig, mechanische Zusagen | **gefixt** (drei Teile in FP-1/FP-6/FP-9) | `test_runner.py::test_hc38_*` (2), `test_cli_connect.py::test_hc38_*`, `test_cli.py::test_hc38_*` (2), `test_invarianten.py::test_hc38_*` (3) | — |

| HC2-1-Rest (a) Schranke greift erst nach dem vollständigen lxml-Parse (24 MB = 47,4 s), (b) Schranke gilt je Teil statt je Mail (34 Teile = 29,6 s ohne Ablehnung) — **zweite Iteration** | medium | **gefixt** | `test_sanitize_html.py::TestHc21Schranken::test_hc2_1_byte_deckel_*`/`*_budget_*` (5), `test_sanitize_mail.py::TestHc21SchrankenDerHtmlKonvertierung::test_hc2_1_riesiger_html_teil_*`, `*_viele_html_teile_*`, `*_teuerste_mail_*`, `*_byte_budget_*`, `*_gewoehnliche_html_mail_*` (5) | ADR-084 (N, 2026-09-12) |
| HC2-2-Rest RFC-2047-Q-Wort mit rohem `@`/`,` verfälscht `from_domain` weiter — **zweite Iteration** | medium | **gefixt** | `test_ingest_rawmail.py::test_hc2_2_q_wort_*`, `*_b_wort_*`, `*_kodiertes_wort_direkt_*`, `*_zwei_kodierte_woerter`, `*_gewoehnlicher_kodierter_name_*` (8) | ADR-020 (N, 2026-09-12) |

„(N)" = Nachtrag zu einem bestehenden ADR, datiert **2026-09-11** (zweite Iteration:
**2026-09-12**). Fett gesetzte ADRs sind neu.

**Bilanz:** 35 offene Befunde, davon 33 mit Code-Fix und Regressionstest, einer rein
dokumentarisch (HC-37 a), einer in einem anderen aufgegangen (HC-36); dazu zwei bereits in
`14ad9ed` erledigte (HC-16, HC-31). **Kein Befund bleibt bewusst offen, keiner war nicht
reproduzierbar.** Die sechs von keinem Skeptiker geprüften Befunde (HC-32 … HC-35, HC-37,
Teile von HC-36) wurden vom jeweiligen Fix-Agenten vor dem Fix selbst nachgestellt und haben
sich alle bestätigt.

### Korrekturen an §5: drei Begründungen trugen nicht mehr

Diese Runde hat drei Einträge des Hot-Logs widerlegt. Sie bleiben oben unverändert stehen —
§5 ist Historie —, sind aber ab hier überholt:

- **HT-14 (neu): Erkennungsregex und Test-Orakel teilten dieselbe Schranke.**
  `tests/unit/test_hot_properties.py:_RE_LIVE_DOMAIN` hatte mit `[a-z0-9\-]{0,62}`/`{1,63}`
  die DNS-Längenschranke und mit `(?![\w\-.])` die Unterstrich-Wortgrenze der Implementierung
  übernommen — es konnte die Lücke, die es prüfen soll, prinzipiell nicht finden (HC-24). Das
  ist dieselbe Fehlerart wie HT-1/2/4/6, nur eine Ebene höher. Behoben durch: Deckel in Orakel
  **und** Implementierung ersatzlos entfernt, IPv4-Verbotsmuster (`_RE_LIVE_IPV4`) ergänzt, das
  dort ganz fehlte, Struktur-Zeilen des Orakels um die englischen Beschriftungen erweitert
  (es prüfte nur die deutschen, obwohl die Ausgabe englisch ist), und der Kommentar
  „das Orakel muss strikt großzügiger sein als die Implementierung" aufgenommen. Als Regel für
  künftige Runden: **Ein Orakel, das eine Konstante der Implementierung wiederholt, prüft nichts.**
  Zur selben Klasse gehört ein zweiter Fund aus HC-23: `tests/integration/test_sanitize_corpus.py`
  baute die `RawMail` von Hand nach, statt `build_raw_mail` zu rufen, und umging damit genau die
  Stufe, in der der Fehler saß; der Helfer ist entfallen. Ebenso HC-5: der CT-6-Regressionstest
  baute seine `SanitizedMail` mit `make_mail()` und ließ den Sanitizer aus.
- **HT-7-Korrektur: „200 000-Zeichen-Felder abgedeckt" trug nicht.** Der Testwert der
  Fehlerinjektion ist `"S" * 200_000` und enthält **keinen einzigen** Treffer von
  `_URL_TOKEN_RE`; `_redact_tokens` steigt nach `finditer` sofort aus und misst nichts. Die
  Klasse, um die es geht — ein whitespace-freies Feld **mit** vielen Treffern —, war bis HC-29
  ungetestet und lief dort quadratisch (32 000 Zeichen: 9,26 s; nach dem Fix 0,006 s). Sie ist
  jetzt über `test_summarizer.py::test_hc29_redact_tokens_bleibt_im_zeitbudget` abgedeckt.
  Der Null-Befund von HT-7 im Übrigen bleibt gültig.
- **HT-12-Korrektur: `compose_plain` hat nicht einen, sondern drei Aufrufer.** Die alte
  Begründung („der einzige Aufrufer ist die CLI-Testnachricht, deren Text im Code steht")
  trägt seit ADR-054 nicht mehr: Aufrufer sind `cli.py:_send_test_message`,
  `cli.py:_announce_selftest` und `runner.py:handle_command` — und der dritte interpoliert
  `[imap] folder`, also einen variablen Anteil (HC-28). Die Schichtgrenze lautet ab jetzt:
  **`_finalize` ist Nachbrenner und Split, kein Feld-Scrub; variable Anteile scrubbt der
  Aufrufer.** Festgehalten im Docstring von `compose_plain`, in ADR-077 (Nachtrag b) und
  mechanisch in `test_invarianten.py::test_hc38_compose_plain_hat_nur_die_gelisteten_aufrufer`.

### Schichtgrenzen, die diese Runde bestätigt hat

- **HC-37 (b): Kein Rate-Limit für `/status`.** Sechs Befehle in einem Stapel ergeben sechs
  Antworten; nur `/digest`-Fluten werden zu genau **einem** zusätzlichen Zyklus zusammengefasst
  (ADR-077, seit HC-13 unverändert). Eine Befehlsflut treibt also die Modellkosten nicht linear
  hoch — sie kann aber den Messenger mit Statusantworten fluten. Wer in den Chat schreiben
  kann, ist laut SECURITY §1 der Betreiber selbst; für Gruppen-Chats ist `accept_commands =
  false` vorgesehen (README).
- **`run --once` läuft nicht unter den Signal-Handlern.** Der im Bericht unter „Geprüft und
  verworfen" Nr. 6 widerlegte Befund („Absturz zwischen `claim` und `checked` verliert die Mail")
  hinterlässt eine Klarstellung: Die Signal-Handler installiert nur `run_forever`. Ein Ctrl+C
  oder ein Cron-Timeout während `run --once` erzeugt den Zustand `sanitized` real — die Mail
  bleibt als Zeile abfragbar, wird beim Wiederanlauf aber als Duplikat erkannt und nicht erneut
  verarbeitet (ADR-019 hält das als akzeptiert fest). Steht auch in BETRIEB §3.
- **Über-Neutralisierung ist bei HC-5 der fail-safe Ausgang** (ADR-036): `<… MAILDIGEST …
  UNTRUSTED …>` innerhalb einer Zeile wird auch dann ersetzt, wenn ein harmloser Absender beide
  Wörter zufällig in spitzen Klammern schreibt. Über den gesamten Korpus tritt der Fall nicht auf.
- **Die Fehlalarmrate ist durch HC-5 und HC-21 nicht gestiegen.** Über die 49 sanitisierbaren
  Mails aus `tests/corpus/` und `tests/cold/mails/` setzten vorher vier den Injection-Verdacht,
  nachher ebenfalls vier — alle vier sind Angriffsmails. Neu ist nur, dass
  `10_injection_direkt.eml` zusätzlich das Indiz `forged_block_marker` trägt. Auch das neue
  `encrypted`-Flag (HC-33) setzt über den gesamten Korpus keine einzige Mail.

### Neue Testdateien und Korpusmails aus dieser Runde

| Datei | Inhalt |
|-------|--------|
| `tests/unit/test_hot_cli_robustness.py` | Terminal-Allowlist und Key-Maskierung (HC-4), atomares Schreiben der Konfiguration (HC-20) |
| `tests/unit/test_hc14_spec_literals.py` | Vertragstest: liest die Literale aus SPEC-CLI §2/§4/§6 und vergleicht sie mit einer echt komponierten Nachricht bzw. mit `inspect.getsource(cli)` (19 Fälle) |
| `tests/cold/mails/30_marker_nachbau.eml` | Exakt nachgebauter Datenblock-Marker (HC-5) |
| `tests/cold/mails/31_injection_variante.eml` | Naheliegende Variante der Übernahmeformel (HC-21) |

`tests/unit/test_invarianten.py` ist von 24 auf 27 Tests gewachsen: Die Menge der
`.send(`-Aufrufstellen (sieben), die Herkunft jedes Sende-Arguments und die Aufrufer von
`compose_plain` (drei) sind jetzt per AST gesperrt (HC-38). Beide Zusagen waren bis dahin nur
Prosa in SECURITY §7.1 — und beide hätten HC-28 beim Einbauen der `/status`-Antwort sofort
sichtbar gemacht.

### Abdeckung nach dieser Runde

| Modul | Stand |
|-------|-------|
| `agents/offline.py` | 100 % |
| `output/sanitizer.py` | 99 % (2 Zeilen: Fixpunkt-Rückgabe in `_unescape`, Schnellpfad von `_strip_control`) |
| `sanitize/links.py` | 99 % (1 Zeile: `build_footnote` ohne Einträge) |
| `sanitize/attachments.py` | 100 % |
| `runner.py` | 99 % (4 Zeilen: `AssertionError`-Wächter in `_with_llm_retry`, `_stop.wait`-Zweig von `_wait`, `total.low_digests += 1`, Platzhalter `_unwired`) |
| `cli.py` | 96 %, `providers.py` 89 % (Rückfallzweige von `find_preset`) |

In `runner.build_runner` sind die Offline-Zweige jetzt gedeckt; ungedeckt bleiben dort nur die
Zweige mit echtem LLM-Provider. Die Gesamtziele aus NF-6 sind unverändert erfüllt.

### §4 nach dieser Runde

Der Haken **„Zweite Cold-Runde mit frischem Agenten"** bleibt offen — er läuft als §6 der
Fixrunde ([docs/PLAN-FIXRUNDE.md](PLAN-FIXRUNDE.md)). Seine Voraussetzung ist seit HC-14
erfüllt: Die Spec beschreibt wieder wörtlich, was das Programm ausgibt, und ein Vertragstest
hält das fest. Bis die Runde stattgefunden hat, bleibt NF-8 `in-progress`.

### Nachfixrunde NF-1 (HC2-1, HC2-2)

Die zweite Testrunde (**`docs/TESTRUNDE-2.md`**, 45 Befunde) und die Abschlussprüfung
(**`docs/ABNAHME-FIXRUNDE.md`**) sind Protokoll und werden nicht geändert. §8 der
Abschlussprüfung macht zwei Befunde zur **Auflage vor dem Release**; sie sind hier
nachgezogen. Die übrigen Pakete NF-2 … NF-7 aus jener Restliste sind offen.

| Befund | Severity | Status | Tests | ADR |
|--------|----------|--------|-------|-----|
| HC2-1 `html_to_text` skaliert quadratisch mit der Schachtelungstiefe; keine Schranke | high | **gefixt** | `test_sanitize_html.py::TestHc21Schranken` (5), `test_sanitize_mail.py::TestHc21SchrankenDerHtmlKonvertierung` (6), `test_output_composer.py::test_hc2_1_*` (2), `test_ingest_poll.py::test_hc2_1_kill_waehrend_der_verarbeitung_ist_kein_dauer_dos` | **ADR-084**, ADR-067/029/019 (N-frei: nur zitiert) |
| HC2-2 RFC-2047-Dekodierung vor dem Adress-Parsen (Regression aus HC-23) | medium (blockierend) | **gefixt** | `test_ingest_rawmail.py::test_hc2_2_*` (7) | ADR-020 (N) |
| R-4 (dritte Iteration) Maske für kodierte Wörter war enger als der Dekoder (leerer Charset `=??Q?…?=`) | hoch | **gefixt** | `test_ingest_rawmail.py::test_hc2_2_maskierung_ist_nicht_enger_als_der_dekoder` (8 Formen), `::test_hc2_2_leerer_charset_bestimmt_die_domain_nicht` (3), `::test_hc2_2_leerer_charset_im_reply_to` | ADR-020 (N, dritte Iteration) |
| R-5 (dritte Iteration) `LinkCollector.scrub` quadratisch in der Zahl der Funde; kein Link-Budget | hoch (DoS) | **gefixt** | `test_sanitize_links.py::TestR5LinkBudget` (5), `test_output_composer.py::test_r5_*` (2) | ADR-028 (N), ADR-084 (N) |
| R-6 (dritte Iteration) Klartext-Pfad ohne Budget (bis 25 MB durch alle Pässe) | mittel | **gefixt** | `test_sanitize_mail.py::test_r6_*` (3) | ADR-084 (N) |
| R-7 (dritte Iteration) Messkorrektur der teuersten Mail; Zusage „rund 2 s" zu knapp | mittel | **gefixt** (Zusage korrigiert) | `test_sanitize_mail.py::test_hc2_1_teuerste_mail_unter_den_neuen_grenzen` (neu vermessen), `::test_r7_gesamt_worst_case_bleibt_weit_unter_der_zusage` | ADR-084 (N) |
| R-8 (vierte Iteration) Regression aus R-4: `_ENCODED_WORD_RE` mit `.*?` quadratisch; keine Kopfzeilen-Obergrenze | hoch (DoS im Ingest) | **gefixt** | `test_ingest_rawmail.py::test_r8_maskierung_ist_linear`, `::test_r8_riesiger_from_header_kostet_keine_zeit`, `::test_r8_riesiger_betreff_kostet_keine_zeit`, `::test_r8_gewoehnlicher_header_bleibt_unveraendert` | ADR-020 (N, vierte Iteration) |
| R-9 (vierte Iteration) Kodiertes Wort hinter der Adresse löscht Absenderadresse und Domain; beide Rückweg-Warnungen verstummen | mittel | **gefixt** | `test_ingest_rawmail.py::test_r9_angehaengtes_kodiertes_wort_loescht_die_domain_nicht` (4 Formen), `::test_r9_zweiter_griff_nimmt_die_erste_klammer` | ADR-020 (N, vierte Iteration) |
| R-10 (vierte Iteration) Klartext-Vorschnitt je Textstück statt je Mail; Teilezahl ohne Schranke | hoch (9,2–10,2 s CPU je Mail) | **gefixt** | `test_sanitize_mail.py::TestR10SchrankenDesAnhangsPfads` (6) | ADR-084 (N, dritter Nachtrag) |
| R-11 (vierte Iteration) `pdf_timeout_seconds` gilt je Anhang; 20 PDFs ≈ 400 s Wandzeit | hoch (DoS über die Poll-Periode) | **gefixt** | `test_sanitize_mail.py::TestR11PdfZeitbudget` (2), `test_spec_cli.py::test_alle_config_felder_stehen_in_der_referenz` (Feldsatz) | ADR-029 (N, vierte Iteration) |
| S-1 (fünfte Iteration) Kopfzeilen-Deckel nur für From/Reply-To/Subject; `To` ungedeckelt (20 MB = 18,5 s), Rück-Serialisierung faltet jede Kopfzeile jedes Teils (bis 14 s) | hoch (DoS im Ingest) | **gefixt** | `test_ingest_rawmail.py::test_s1_riesiger_to_header_kostet_keine_zeit`, `::test_s1_messreihe_to_header`, `::test_s1_jeder_gelesene_header_ist_gedeckelt` (10 Header), `::test_s1_empfaengerzahl_ist_gedeckelt`, `::test_s1_viele_kopfzeilen_kosten_keine_zeit`, `::test_s1_kopfzeilen_gesamtbudget_schneidet_den_baum`, `::test_s1_kopfzeilen_ersetzen_ist_sichtbar`, `::test_s1_gewoehnliche_mail_bleibt_byteidentisch` | ADR-020 (N, fünfte Iteration) |
| S-2 (fünfte Iteration) Regression aus R-9: Klammer-Rückfall liest Kommentar-/Quoted-String-Inhalt; 4096-Schnitt öffnet Kommentare; unlesbarer `Reply-To` schaltet Warnung ab | hoch | **gefixt** | `test_ingest_rawmail.py::test_s2_klammer_in_kommentar_oder_quote_bestimmt_die_domain_nicht` (9 Formen), `::test_s2_dasselbe_im_reply_to` (9), `::test_s2_balancierte_kommentare_und_quotes_bleiben_lesbar` (8), `::test_s2_scanner_kennt_verschachtelung_escapes_und_offene_enden`, `::test_s2_token_hinter_der_klammer_loescht_die_antwortadresse_nicht`, `::test_s2_reply_to_gleich_absender_mit_kommentar_ist_kein_mismatch`, `test_sanitize_mail.py::TestReport::test_s2_*` (2) | ADR-020 (N, fünfte Iteration) |
| S-3 (fünfte Iteration) Gesamt-Worst-Case zu günstig gemessen (2,70 s); real 3,6–5,1 s, davon ~2 s Standardbibliotheks-Parser | mittel | **gefixt** (Zahl korrigiert, Rohbyte-Budget begründet verworfen) | Messung `sk4_final.py`, Profil (`_payload_bytes`/`_decode_text_part` zusammen 0,01 s) | ADR-084 (N, vierter Nachtrag) |
| S-4 (Sanitizer, 2026-09-12) `_RE_IPV4` mit genau vier Oktetten bricht in Punktketten > 4 Gruppen nur das letzte Fenster: `1.1.1.1.1.1.1.` → `1.1.1.1[.]1[.]1[.]1.`, die ersten vier Oktette leben (I3/F-SEC-3); gefunden vom Property-Test CT-7 (hypothesis-Gegenbeispiel, im Ausgangsstand nur wegen des lokalen Beispiel-Caches grün) | mittel | **gefixt** (`{3,}`: ganze Kette, jeder Punkt gebrochen) | `test_hot_properties.py::test_s4_punktkette_wird_ganz_gebrochen` (4 Formen, neben dem Property-Test), `test_output_sanitizer.py::test_s4_punktkette_ueber_vier_oktette_wird_ganz_gebrochen` (3), `::test_s4_ziffernlauf_hinter_der_kette_bleibt_wie_bisher` (3, Gegenprobe) | ADR-036 (N, S-4) |

„(N)" = Nachtrag zu einem bestehenden ADR, datiert **2026-09-11**. Fett gesetzte ADRs sind neu.

**Was die beiden Tests beweisen sollen.** Für HC2-1 ist die Aussage des Befunds eine
Zeitaussage, also misst der Regressionstest die Wanduhr: 16 000 Ebenen vor dem Fix 28,6 s,
danach < 1 s (`test_hc2_1_deep_nesting_is_linear`; vor dem Fix gesehen und gemessen). Weil
ein Zeittest allein auch durch einen früheren Abbruch grün werden kann, prüft
`test_hc2_1_konversion_bleibt_linear` die Kennlinie **ohne** Schranke (vierfache Tiefe,
höchstens achtfache Zeit — quadratisch wäre sechzehnfach). Für HC2-2 ist das Orakel die
Adresse im **Rohheader**, unabhängig vom Produktionscode ermittelt (kodierte Wörter
entfernen, dann die Adresse lesen) — nicht `build_raw_mail` selbst. Fünf der sieben
HC2-2-Tests schlagen auf dem Stand vor dem Fix fehl, die beiden Kontrollfälle
(unkodierte Mail, HC-23-Fall) bleiben grün.

**Zweite Iteration (2026-09-12).** Der Skeptiker bestätigte die Original-Repros als behoben,
belegte aber drei Restlücken; sie sind oben als eigene Zeilen geführt. Für die beiden
HC2-1-Reste ist die Aussage wieder eine Zeitaussage, also messen die Tests die Wanduhr an
genau den Repro-Mails: 24-MB-Teil 47,4 s → < 0,5 s, 34 HTML-Teile 29,6 s → < 2 s. Damit ein
Zeittest nicht bloss die neuen Grenzen bestätigt, konstruiert
`test_hc2_1_teuerste_mail_unter_den_neuen_grenzen` zusätzlich die **ungünstigste** Mail, die
unter den neuen Grenzen überhaupt möglich ist (`MAX_HTML_PARTS` × `max_html_bytes` der
dichtesten Elementform): gemessen rund 2 s. Für den HC2-2-Rest bleibt das Orakel die Adresse
im Rohheader; die neuen Fälle tragen `@`, `,`, `<` und `>` **literal** in der Q-Kodierung —
das reine Base64-Orakel der ersten Iteration konnte sie nicht erzeugen. Alle acht neuen
HC2-2-Tests schlagen auf dem Stand vor dieser Iteration fehl, die Gegenprobe (gewöhnlicher
kodierter Name) bleibt grün.

**Dritte Iteration (2026-09-12).** Der Skeptiker der zweiten Iteration bestätigte alle
bisherigen Repros als tot und belegte vier neue Punkte (oben als R-4 bis R-7 geführt).
Zwei davon sind Zeitaussagen und werden wieder an der Wanduhr gemessen, jeweils vor dem Fix
auf denselben Skripten gesehen: `LinkCollector.scrub` mit 32 000 Funden 17,0 s → 0,3 s, die
1-MB-HTML-Mail mit 41 000 Links (innerhalb **aller** ADR-084-Schranken, `html_rejected` war
False) 28,9 s → 1,1 s, 21 MB Klartext 4,5 s → 0,4 s. Damit die Zeittests nicht bloss die
neuen Budgets bestätigen, prüfen zwei Tests die Wirkung inhaltlich: `test_r5_*` zeigt, dass
jenseits des Link-Budgets **keine** URL überlebt (der Fund wird `[Link removed]`, gezählt
bleibt er), und `test_r6_gewoehnliche_mail_wird_byteidentisch_verarbeitet` vergleicht eine
100-KB-Mail gegen denselben Sanitizer mit praktisch abgeschaltetem Vorschnitt — byteidentisch.
Für R-4 bleibt das Orakel die Adresse im Rohheader; zusätzlich ist jetzt der **Dekoder
selbst** das Orakel der Maskierung: Für acht kodierte Formen (leerer Charset, Sprach-Tag,
B/Q gross und klein, gefalteter Header, kaputte Form ohne `?=`) darf kein Segment, das
`email.header.decode_header` als kodiert liefert, im maskierten Rohwert noch `@`, `,`, `<`,
`>`, `;` oder `:` zeigen. Fünf der neuen R-4-Tests schlagen auf dem Stand vor dieser
Iteration fehl. R-7 ist eine Messkorrektur: Der bisherige Test gab jedem der vier HTML-Teile
den ganzen Byte-Deckel, worauf die Teile 2 bis 4 ungeparst verworfen wurden (1,84 s). Neu
trägt jeder Teil ein Viertel des Deckels, alle vier werden geparst — gemessen 1,9 bis 2,6 s;
die Zusage lautet jetzt „etwa 2–3 s je nach Maschinenlast" statt „rund 2 s". Die
Gesamt-Worst-Case-Mail (HTML-Budget, Klartext-Vorschnitt und Link-Budget gleichzeitig voll)
kostet 1,0 s.

**Vierte Iteration (2026-09-12).** Der Skeptiker der dritten Iteration bestätigte wieder
alle bisherigen Repros als tot und belegte vier neue Punkte (oben als R-8 bis R-11). Neue
Regel dieser Iteration: **Jede neue oder geänderte Regex und jede Schleife im Hot Path
bekommt eine Messreihe (n, 2n, 4n)** — R-8 war genau der Fall, den die dritte Iteration ohne
Messreihe eingebaut hatte. R-8 ist eine Regression aus dem R-4-Fix: Das zu
`email.header.ecre` formgleiche `.*?` darf über `?` hinweglaufen, und ohne schliessendes
`?=` scannt jede Startstelle den ganzen Resttext. Messreihe `_mask_encoded_words` auf
kaputten Wörtern, n = 25 000 / 50 000 / 100 000 / 200 000: vorher 21,4 s / 80,7 s / Abbruch,
nachher 0,000 / 0,000 / 0,001 / 0,001 s; mit gültigen Wörtern 0,012 / 0,028 / 0,057 /
0,119 s (linear). Ende-zu-Ende: 156-KiB-`From` im Ingest 18,2 s → 0,00 s, 160-KiB-`Subject`
8,6 s → 0,00 s. Damit der Zeittest nicht bloss die neue Kopfzeilen-Obergrenze bestätigt,
prüft `test_r8_maskierung_ist_linear` die Kennlinie **ohne** Obergrenze (achtfacher Umfang,
höchstens sechzehnfache Zeit — quadratisch wäre vierundsechzigfach), und der Leitsatz „nie
enger als der Dekoder" bleibt mit dem unveränderten Orakeltest
`test_hc2_2_maskierung_ist_nicht_enger_als_der_dekoder` abgesichert. Für R-9 ist das Orakel
weiterhin die Adresse im Rohheader beziehungsweise `email.policy.default`; alle vier Formen
liefern vor dem Fix `from_domain=''`, `(unknown sender)` und beide Warnungen auf `False`.
R-10 und R-11 sind Zeitaussagen und wurden vorher wie nachher an denselben Skripten gemessen
(`sk3_max.py`, `sk3_parts.py`, `sk3_pdf.py`): Gesamt-Worst-Case-Mail 21,45 MB 9,22 s →
2,70 s, 200 000 MIME-Teile 3,69 s → 1,81 s, drei PDF-Anhänge 60,1 s → 30,1 s Wandzeit,
zwanzig PDF-Anhänge rechnerisch ~400 s → 30,3 s. Messreihen dazu: 20 Textanhänge à 480 000
Zeichen (n/2n/4n = 5/10/20) vorher 1,64 / 2,53 / 5,44 s, nachher 0,28 / 0,46 / 0,49 s;
MIME-Teile 2000/4000/8000 vorher 0,03 / 0,06 / 0,12 s, nachher 0,02 / 0,04 / 0,10 s (dort
dominiert das Parsen, das keine Schranke abwenden kann). Für R-11 misst der Unit-Test nicht
die Wanduhr, sondern die **vergebenen Zeitlimits** (`[20,0; 10,0]` statt dreimal 20,0) und
die Zahl der Aufrufe — deterministisch und in Millisekunden, mit einer Uhr-Attrappe statt
echter Kindprozesse.

**Fünfte Iteration (2026-09-12).** Der Skeptiker der vierten Iteration bestätigte alle
bisherigen Repros als tot und belegte drei neue Punkte (oben als S-1 bis S-3). S-1: Der
Deckel aus R-8 galt für die gemeldete Instanz, nicht für die Klasse — `To` lief ungedeckelt
durch `getaddresses`, und beim Nachmessen aller Lesestellen zeigte sich die eigentlich
teure: `as_bytes()` in `_raw_bytes` faltet jede Kopfzeile jedes Teils neu (250 000
Kopfzeilen 13,8 s, 20-MB-Teil-Header 13,9 s, 5000 Teile à 4 KB 13,0 s, 20-MB-`Return-Path`
11,5 s). Messreihe `To` 1/2/4/8 MB, `build_raw_mail` allein, alt (Stand `649a9b8`) → neu:
0,57 / 1,36 / 2,00 / 4,11 s → 0,01 s durchweg (Form `a@b.example, `); 0,80 / 1,60 / 3,22 /
6,51 s → 0,01 s (Form `<a@b`). Die übrigen Formen: 20-MB-`To`-Repro des Skeptikers
(`sk4_to2.py`) 2,41 / 5,38 / 9,58 / 18,57 s → 0,05 / 0,09 / 0,15 / 0,12 s (der Rest ist
Serialisierung und Hash des 20-MB-Bodys); Cc / Authentication-Results / Message-ID / Date /
Return-Path je 20 MB: 1,99 / 2,41 / 0,29 / 4,12 / 11,51 s → 0,00 s; viele Kopfzeilen bzw.
Teile 13,8 / 12,5 / 13,9 / 13,0 s → 0,22 / 0,35 / 0,01 / 0,16 s. S-2: Orakel ist
`email.policy.default` auf demselben Rohheader; neun unbalancierte Formen liefern vor dem
Fix in drei Fällen `bank.example` ohne jede Warnung, nach dem Fix in keinem Fall eine fremde
Domain (unbekannt + Warnung oder die echte Adresse), acht balancierte Gegenproben
(verschachtelt, escaped, Kommentar nach der Adresse) bleiben `evil.example`; der neue
Scanner ist linear (4096 … 262 144 Zeichen: 0,0005 … 0,030 s, `test_s2_scanner_ist_linear`).
S-3 ist eine Messkorrektur, per Profil begründet: Der Parser der Standardbibliothek trägt 1,9 bis 2,3 s
der 3,6 bis 5,1 s, die eigenen Pässe 1,7 bis 2,2 s, die Dekodierung der Anhänge 0,01 s.

**Das Muster der Nachfixrunde und die Regel daraus.** Jede der vier vorigen Iterationen hat
an der Naht, die sie neu gezogen hat, ein Loch geöffnet, das erst der Skeptiker fand: Die
Maske formgleich zum Dekoder (R-4) brachte das quadratische `.*?` (R-8); der Deckel gegen
R-8 sass nur bei der gemeldeten Kopfzeile (S-1) und zerschnitt Kommentare, die der neue
Rückfall aus R-9 dann las (S-2); das Roh-Budget je Textstück (R-6) wurde von der Anhangszahl
multipliziert (R-10), und die Worst-Case-Zahl wurde dreimal nach oben korrigiert (R-7,
R-10, S-3). Die Regel ab jetzt, zusätzlich zur Messreihen-Pflicht der vierten Iteration:
**(1) Jede Schranke wird für die Klasse gebaut, nicht für die Instanz** — wer einen Header
deckelt, deckelt an der einen Lesestelle alle Header und misst danach jede andere Stelle,
die dasselbe liest (hier: die Rück-Serialisierung). **(2) Ein Rückfall, der mehr Text liest
als der Hauptweg, ist verdächtig** — Rückfälle lesen nie Kommentar- oder
Quoted-String-Inhalt, und im Zweifel ist das Ergebnis „unbekannt + Warnung", nie eine
Domain. **(3) Der Worst Case wird mit der für den Parser dichtesten Form gemessen**, nicht
mit der Form, die das eigene Budget am frühesten abschneidet, und der Anteil der
Standardbibliothek wird getrennt ausgewiesen. **(4) Vor der Abgabe laufen alle
Repro-Skripte aller Iterationen gegen den alten und den neuen Stand.**

**Offene Frage aus HC2-1 beantwortet.** „Greift der Runner eine solche Mail nach einem
Neustart erneut auf (Dauer-DoS)?" — Nein. `poll_once` reserviert den Dedupe-Key mit
`StateDB.claim` **vor** der Verarbeitung (ADR-019), und `claim` committet sofort
(`with self._conn` um das `INSERT OR IGNORE`). Ein harter Abbruch mitten in der
Sanitize-Stufe hinterlässt die Zeile mit Status `pending`; der nächste Prozess bekommt für
dieselbe Mail `ClaimResult.DUPLICATE` und überspringt sie. Eine Angriffsmail kostet also
höchstens **einen** Zyklus. Belegt durch
`test_ingest_poll.py::test_hc2_1_kill_waehrend_der_verarbeitung_ist_kein_dauer_dos`. Ein
`failed`-Status vor der Sanitize-Stufe ist damit nicht nötig.

**Testzahl nach NF-1:** 1693 (von 1565; 1586 nach der ersten, 1604 nach der zweiten,
1627 nach der dritten, 1644 nach der vierten Iteration), Laufzeit rund 145 bis 180 s je
nach Maschinenlast — der Zuwachs kommt aus den Zeitmessungen der zweiten bis fünften
Iteration.

### Offen nach Ende der Nachfixrunde (Skeptiker der fünften Iteration, Fable 5.1, 2026-09-12)

Die Nachfixrunde wurde nach fünf Iterationen auf Nutzerentscheid beendet. Die beiden
Release-Blocker HC2-1 und HC2-2 sind in ihrer gemeldeten Form seit der ersten Iteration
behoben und über alle fünf Skeptiker-Läufe stabil geblieben (alle Repro-Skripte der
Iterationen 1–5 laufen auf `a2feda2` ohne Rückfall in die gefährliche Richtung). Der letzte
Skeptiker hat fünf Punkte belegt, die zunächst **nicht** bearbeitet wurden; sie stehen hier,
damit sie nicht nur im Workflow-Journal liegen. Vor Release 0.2.0 wurden O-1 (zwei
Iterationen mit Skeptiker) und O-3 (direkt, mit dem Skeptiker-Fuzz als Regressionstest)
geschlossen; O-6 bis O-8 sind Nebenbefunde des O-1-Skeptikers. Die Repro-Skripte
(`sk5_*.py` … `sk7_*.py`) liegen im Scratchpad der Sitzung, nicht im Repo.

| Nr. | Severity | Befund | Herkunft | Fix-Richtung |
|-----|----------|--------|----------|--------------|
| O-1 | hoch | **gefixt (2026-09-12).** Gift-Mail mit ≥ 250 verschachtelten multipart-Ebenen (16 KB): `as_bytes()` scheiterte mit `RecursionError`, der Rückfall `str(msg.obj)` in `_raw_bytes` rekursierte erneut und wurde nicht gefangen; `build_raw_mail` warf entgegen ADR-020 (e), `poll_once` hatte kein try darum — der Dauerbetrieb starb, `run --once` scheiterte bei jedem Lauf, spätere Mails blieben liegen | vorbestehend (auch auf `649a9b8`) | **Erledigt:** Tiefendeckel `MAX_MIME_DEPTH` = 32 iterativ vor jeder Serialisierung (`_cap_message_depth`, Log `mail_mime_depth_capped`), dreistufiger und vollständig gefangener Rückfall in `_raw_bytes` (zuletzt nur Kopfzeilen + Hinweistext), Schutz um `build_raw_mail` in `poll_once` mit Ersatz-`RawMail` (`ingest_failed`) ⇒ Metadaten-Notiz, Status `failed`/`ingest_error`, Mail als gelesen markiert, Zyklus läuft weiter; Sanitizer-Baumlauf zusätzlich hart auf 64 Ebenen begrenzt. ADR-020-Nachtrag (O-1). Tests: `test_o1_tiefe_verschachtelung_wirft_nicht` (250/1000/5000), `test_o1_geparste_giftmail_wird_gedeckelt_und_bleibt_klein`, `test_o1_normale_mail_bleibt_byteidentisch`, `test_o1_raw_bytes_rueckfall_wirft_nie`, `test_o1_sanitizer_parse_der_giftmail_ist_gedeckelt` (unit/test_ingest_rawmail.py), `test_o1_unlesbare_mail_wird_zur_notiz` (unit/test_pipeline.py), `test_o1_poll_once_ueberlebt_die_giftmail`, `test_o1_beliebiger_fehler_vor_process_wird_zur_notiz`, `test_o1_ersatzkey_ohne_lesbare_header_ist_stabil` (integration/test_ingest_poll.py), `test_o1_run_forever_stirbt_nicht` (integration/test_runner_e2e.py). **Restfall (Skeptiker, zweite Iteration): geschlossen.** Ab 984 `message/rfc822`-Ebenen (31 569 B) scheiterte schon `MailMessage.__init__` in imap-tools innerhalb des `fetch`-Generators; die Umdeutung in `ImapConnectionError` machte daraus eine endlose Backoff-Schleife (Mail nie beansprucht, nie Seen; `run --once` „Mailbox unreachable"). Jetzt Abruf **je UID** (`uids()` + `fetch(uid_list=…)`, gleiche Kommandozahl 1 + 2n je Zyklus, gemessen n = 10/20/40), Parse je Mail isoliert, Kopfzeilen-Ersatz über gedeckeltes `UID FETCH (BODY.PEEK[HEADER]<0.262144> …)` ⇒ `UnparsableMailMessage` ⇒ Notiz, `failed`/`ingest_error`, Seen; Log `mail_unparsable`; echte Verbindungsfehler (inkl. `imaplib.IMAP4.error`) bleiben `ImapConnectionError`. ADR-020-Nachtrag (zweite Iteration), ADR-064-Nachtrag. Tests: `test_o1b_fetch_unseen_isoliert_unparsbare_mail`, `test_o1b_echter_verbindungsfehler_bleibt_verbindungsfehler` (3 Klassen), `test_o1b_search_fehler_ist_ein_verbindungsfehler`, `test_o1b_kopfzeilen_abruf_scheitert_auch`, `test_o1b_unparsbare_mail_ohne_kopfzeilen_antwort`, `test_o1b_verschwundene_uid_wird_uebersprungen` (unit/test_ingest_client.py), `test_o1b_unparsbare_mail_blockiert_den_poll_nicht`, `test_o1b_echte_giftmail_rfc822_tiefe_990`, `test_o1b_header_abruf_scheitert_auch`, `test_o1b_nur_uid_kommandos_kein_expunge`, `test_o1b_kommandozahl_je_zyklus` (10/20/40) (integration/test_ingest_poll.py), `test_o1b_run_forever_drei_zyklen`, `test_o1b_run_once_bilanz_und_exitcode` (integration/test_runner_e2e.py) |
| O-2 | hoch | Teile-Flut ohne Kopfzeilen: 2 Mio. leere MIME-Teile in 20 MB kosten 34 s CPU (Parser 12,6 s vor jedem Budget, `build_raw_mail` 8,4 s, Sanitizer-Parse 13,2 s), linear und ungedeckelt — der Kopfzeilen-Deckel greift nicht, weil Teile ohne Kopfzeile kein Budget verbrauchen | vorbestehend | Nur vor dem Parsen abwendbar: Boundary-Zählung auf den Rohbytes des Abrufs (Eingriff in den Fetch-Pfad, ADR nötig) oder kleinerer Default für `max_mail_bytes`; bis dahin als Grenze dokumentiert |
| O-3 | hoch | **gefixt (2026-09-12, vor Release 0.2.0, zwei Griffe).** Erster Griff (Maske spiegelt die Wortform des Parsers) vom Skeptiker widerlegt: Token-Grenzen enger als der Parser (nach `.`, `:`, `<`, `\\`), Q-Wort mit literalem `@` ersetzte die Domain wieder. Zweiter Griff: der RFC-5322-Parser der Standardbibliothek (`email.headerregistry`) liest Anzeigename und Adresse selbst — Maske, Klammer-Rückfall und Kommentar-Scanner sind entfernt; nur die erste Angabe zählt, Domains müssen hostname-förmig sein, ein unlesbarer Header bleibt als `(unreadable)` sichtbar (Warnung feuert). Regressionstest: die 45 Header-Formen des Skeptikers in `test_ingest_rawmail.py::test_o3_*` mit `email.policy.default` als Orakel — nie eine Domain, die das Mailprogramm nicht zeigt, nie „unbekannt“ ohne Warnung. ADR-020 (N, O-3). Ursprünglich: ein regelwidrig kodiertes Wort mit `?` oder `(` im encoded-text (`=?utf-8?Q?Support?(?=`) wird maskiert und versteckt so die Klammer vor Kommentar-Scanner und `getaddresses`; das Werkzeug zeigt `bank.example` ohne Warnung, das Orakel `real@evil.example` | seit `b7d093d` | Maske strikt nach RFC 2047 (encoded-text ohne `?` und ohne Leerzeichen, Wort durch Whitespace oder Anfang abgetrennt) — oder den Kommentar-Scanner vor der Maskierung auf dem Rohtext laufen lassen |
| O-4 | mittel | Gesamt-Worst-Case 3,6–4,1 s CPU im Sanitizer ist dokumentiert, aber die Ende-zu-Ende-Zusage „rund 8 s" (ADR-084, vierter Nachtrag) wird von O-2 um das Siebenfache überschritten | Doku | Nach O-2 neu messen und die Zusage in ADR-084 auf die teuerste zulässige Mail setzen |
| O-5 | niedrig | Demaskierung: `MDENCWORD1` ersetzt auch den Präfix von `MDENCWORD10…19`; Anzeigenamen mit mehr als zehn kodierten Wörtern werden verstümmelt (kein Sicherheitsbezug, die Adresse steht vorher fest) | seit `649a9b8` | Längste Tokens zuerst ersetzen oder ein Trennzeichen hinter der Nummer |
| O-6 | mittel | Nebenbefund der zweiten O-1-Iteration (Skeptiker Fable 5.1): Der Platzhalter einer unparsbaren Mail beansprucht die nackte Message-ID mit unbekanntem Inhalts-Hash — eine spätere echte Mail mit derselben Message-ID wird still als Duplikat unterdrückt (`mail_duplicate`, keine Zustellung); der Kollisionsschutz aus ADR-079 greift nicht, weil `content_hash` leer ist | seit `eef74d4` | Platzhalter nie mit der nackten Message-ID beanspruchen: immer der UID/INTERNALDATE-gebundene Ersatz-Key oder ein Sentinel-Inhaltshash über die abgerufenen Kopfzeilenbytes, damit die echte Mail als Kollision unter abgeleitetem Key verarbeitet wird; Test: Gift-Mail mit Message-ID X, danach echte Mail X ⇒ verarbeitet |
| O-7 | mittel | Antwortet der Server auf das `UID FETCH` einer einzelnen Mail dauerhaft mit `NO` (Server-seitig defekte Mail), blockiert diese Mail den Abruf weiterhin endlos: `MailboxFetchError` → `ImapConnectionError` → Backoff, `run --once` meldet „Mailbox unreachable", die Mails dahinter laufen nie | vorbestehend, durch O-1 sichtbar | Ein `NO` auf das FETCH einer UID bei funktionierendem SEARCH ist eine Eigenschaft der Mail, nicht der Verbindung: wie eine unparsbare Mail als Platzhalter buchen (Kopfzeilen-Abruf, sonst UID-Key), Notiz, Seen — mindestens überspringen und mit eigenem Logereignis (UID-Hash, Status) melden |
| O-8 | niedrig | Der Ersatz-Dedupe-Key einer unparsbaren Mail ohne Message-ID enthält die IMAP-Sequenznummer aus der rohen FETCH-Antwort; nach Verbindungsabbruch zwischen Claim und STORE und einem Expunge durch einen fremden Client rutscht sie, dieselbe Gift-Mail wird zweimal gebucht und zugestellt | seit `eef74d4` | Key nur aus mail-gebundenen Werten bilden: UID + aus der Antwort geparste INTERNALDATE und RFC822.SIZE + Kopfzeilenbytes, nicht die rohe Antwortzeile |

Bewertung: Mit dem Maßstab der Skeptiker („fixed nur, wenn kein Weg mehr existiert, der
das Schutzziel verletzt") fand jede Iteration an der neu gezogenen Naht oder in einer bisher
ungemessenen Klasse ein weiteres Loch. Die Runde wurde deshalb beendet, statt weiter zu
iterieren. **O-1 ist inzwischen behoben** (2026-09-12, ADR-020-Nachtrag; siehe die Zeile
oben — der vom Skeptiker nachgemessene Rest ab 984 `message/rfc822`-Ebenen ist in der
zweiten Iteration durch den Abruf je UID geschlossen); O-2 ist eine Designentscheidung am Abruf, O-3 und O-5 sind kleine
Korrekturen an der Maske.
