# MailDigest — Fixrunde zur Abschluss-Testrunde (HC-1 … HC-38)

> **Einstiegspunkt für alle Agenten dieser Runde.** Fix-Agenten lesen §0–§3 und ihr
> Fixpaket in §4. Der Doku-Agent liest zusätzlich §5, die Tester §6, die Abschlussprüfung §7.
> Die Orchestrierung (Wellen, Modelle, Skript) steht in §8 und Anhang B.
>
> Grundlage: [docs/TESTRUNDE-HOT-COLD.md](TESTRUNDE-HOT-COLD.md) (38 Befunde, Stand `13cb859`).
> Dieser Plan ändert den Bericht nicht — er ist Protokoll. Jeder Befund wird hier mit
> Zuständigkeit, Fix-Weg, Akzeptanzkriterium und Doku-Pflicht versehen.

---

## 0. Leserichtung und Reihenfolge

1. Fix-Agent: PLAN.md §1–§2 (Leitprinzipien, Invarianten I1–I8), dann **dieses Dokument
   §2 (Regeln) und §3 (getroffene Entscheidungen)**, dann das eigene Fixpaket in §4.
2. Zu jedem Befund des Pakets den vollständigen Eintrag in TESTRUNDE-HOT-COLD.md lesen —
   dort stehen Repro, Ursache und die vom Skeptiker geprüfte Fix-Richtung. Dieser Plan
   wiederholt das nicht, er präzisiert und entscheidet.
3. Bei Widerspruch gilt: SECURITY.md > REQUIREMENTS.md > dieses Dokument > TESTRUNDE-Bericht.
4. Die im Bericht genannten Zeilennummern beziehen sich auf `13cb859`; seither hat sich
   `cli.py` verschoben. Verlass dich auf Funktions- und Symbolnamen, nicht auf Zeilen.

## 1. Ausgangslage

- Stand: `14ad9ed` (main). `pytest`: **1354 Tests grün** (66 s). `ruff check src tests`
  und `mypy src` ohne Befund. Version bleibt `0.1.0`; CHANGELOG hat einen Abschnitt
  „Unveröffentlicht".
- **Bereits erledigt** durch `14ad9ed` (ADR-078, Befehlskanal ab Werk an):
  - **HC-16** (Testnachricht nannte `[messenger telegram]`, ging an alle Messenger) — erledigt.
  - **HC-31** (README §Grenzen und CHANGELOG kannten die Fernauslösung nicht) — erledigt;
    der Commit-Text nennt es fälschlich „HC-19".
  - **HC-18** nur **teilweise**: `init` schreibt `accept_commands` jetzt aus. Offen bleiben
    die Reihenfolge der Ausgabe (endet nicht mit der Schrittliste), der Spec-Nebenpunkt zu
    `[llm] model` und der Regressionstest gegen den vollständigen Feldsatz.
  - **HC-19** ist **nicht** erledigt: `_setup_telegram` kehrt im `--chat-id`-Zweig weiterhin
    vor dem Hinweisblock zurück.
- **HC-36** ist in HC-3 aufgegangen (gleiche Ursache) und wird dort erledigt.
- **Nachwirkung von ADR-078** auf offene Befunde: `accept_commands` ist jetzt ab Werk `true`.
  Damit ist die im Bericht vorgeschlagene Warnung bei `run --once` (HC-27) keine Lösung mehr —
  sie käme bei jedem Cron-Lauf. Entscheidung dazu in §3.
- Sechs Befunde hat kein Skeptiker geprüft (HC-32 bis HC-35, HC-37, Teile von HC-36). Der
  zuständige Agent stellt sie **vor** dem Fix selbst nach; ein nicht reproduzierbarer Befund
  wird als `nicht reproduzierbar` mit Nachweis gemeldet, nicht „gefixt".

Übersicht der verbleibenden Arbeit (35 Befunde; Severity aus dem Bericht):

| Severity | Befunde |
|---|---|
| high | HC-1 |
| medium | HC-2, HC-3, HC-4, HC-5, HC-6, HC-7, HC-8, HC-9, HC-10, HC-11, HC-12, HC-13, HC-15, HC-20, HC-21 |
| low | HC-14, HC-17, HC-18 (Rest), HC-19, HC-22 … HC-30, HC-32 … HC-35, HC-38 |
| info | HC-37 |

## 2. Verbindliche Regeln für jeden Fix-Agenten

1. **Eigentum an Dateien.** Jedes Fixpaket nennt seine Dateien. Nur diese werden geändert.
   Braucht ein Fix eine Änderung außerhalb, wird sie **nicht** gemacht, sondern im Ergebnis
   als `Fremdbedarf` mit exaktem Patchvorschlag gemeldet (Vorbild: CT-2/CT-14 in TESTING §6).
   Grund: Pakete derselben Welle laufen parallel im selben Arbeitsbaum.
2. **Kein `git commit`, kein `git add`, kein `git stash`.** Der Orchestrator committet je
   Welle. Auch keine `Write`-Überschreibung ganzer Quelldateien — nur gezielte Edits.
3. **Reservierte Dateien** (nur der Doku-Agent, §5): `docs/TESTING.md`, `CHANGELOG.md`,
   `docs/SECURITY.md` §7, `docs/TESTRUNDE-HOT-COLD.md` (nie). Was dort hinein muss, liefert
   der Fix-Agent im strukturierten Ergebnis (Anhang A). Alle anderen Doku-Stellen
   (SPEC-CLI, README, ARCHITECTURE, REQUIREMENTS, SECURITY §1–§6, DECISIONS) pflegt der
   Fix-Agent für **seine** Befunde selbst — Abschnitte stehen je Paket.
4. **ADRs.** Neue ADR-Nummern sind je Paket **vorab vergeben** (§4) und werden ans Ende von
   `docs/DECISIONS.md` angehängt; bestehende ADRs bekommen bei Bedarf einen Absatz
   „**Nachtrag (Fixrunde, 2026-09-11):** …" (Vorbild: Nachtrag zu ADR-058). Wird der
   Edit wegen paralleler Änderung abgelehnt: Datei neu lesen, erneut anhängen.
5. **Regressionstests** heißen `test_hc<N>_<was_gesichert_wird>` (Vorbild `test_ct1_*`,
   `test_ht4_*`). Jeder Befund mit Code-Fix bekommt mindestens einen; ein Test, der den
   Fehler vor dem Fix **nicht** zeigt, zählt nicht (Befund HC-5: der alte Test umging den
   Sanitizer). Wo der Bericht ein „Orakel" nennt, muss das Orakel **strikt großzügiger** sein
   als die Implementierung (HC-24).
6. **Definition of Done je Paket:** alle Befunde des Pakets `gefixt`, `bewusst offen`
   (mit Begründung) oder `nicht reproduzierbar` (mit Nachweis); `.venv/bin/pytest` gesamt
   grün; `.venv/bin/ruff check src tests` und `.venv/bin/mypy src` ohne Befund; Doku-Pflichten
   des Pakets erfüllt; Invarianten I1–I8 geprüft und im Ergebnis bestätigt („Invarianten
   geprüft: keine Verletzung" oder Abweichung begründet); keine neue Laufzeit-Abhängigkeit.
7. **Sprache.** Nutzersichtbare Texte (CLI, Nachricht, Fehler) **englisch**; Docstrings,
   Kommentare, `docs/` **deutsch** (CHANGELOG „Unveröffentlicht", ADR-083 in §3). Beim
   Ändern von `output/sanitizer.py` bleiben die deutschen Alternativen in
   `_RE_STRUCTURE_LABEL` stehen (CT-8).
8. **Schichtdisziplin.** Nicht die Feld-Neutralisierung verschärfen, wenn eine Naht das
   Problem ist (HC-6). `final_guard` bekommt kein `_RE_MARKUP` (HC-7). Über-Defang ist der
   fail-safe Ausgang (ADR-036), Unter-Defang nie.
9. **Ergebnis** ausschließlich als strukturiertes Objekt nach Anhang A (der Workflow
   erzwingt das Schema). Keine Prosa-Zusammenfassung daneben.

## 3. Entscheidungen, die dieser Plan trifft

Die Fix-Agenten diskutieren diese Punkte nicht neu; abweichen nur mit Begründung im Ergebnis.

- **E1 — Ausgabesprache (HC-14): englisch bleibt.** Die Umstellung war gewollt (vier Commits,
  CHANGELOG-Eintrag). Der Vertrag wird nachgezogen, nicht der Code: ADR-083 hält fest, dass
  `[general] language` **nur** die Zusammenfassung steuert und der Nachrichtenrahmen sowie die
  CLI sprachunabhängig englisch sind. SPEC-CLI §2/§4/§6 und README werden auf die tatsächlichen
  Literale gebracht. Restliche deutsche Literale im Ausgabepfad werden übersetzt (FP-8).
- **E2 — Fernauslösung im Cron-Betrieb (HC-27):** `run --once` bedient den Befehlskanal
  **einmal am Ende des Zyklus**: `/status` wird beantwortet, `/digest` wird konsumiert und
  ist dort wirkungslos (der Abruf lief gerade). Keine Warnung auf stderr — sie käme seit
  ADR-078 bei jedem Cron-Lauf. Doku: „im Cron-Betrieb beim nächsten Lauf, verzögert".
- **E3 — Latenz von `/digest` (HC-12): beheben, nicht wegdokumentieren.** Die Wartezeit wird
  in kurze Abschnitte zerlegt; nach jedem Abschnitt werden Befehle abgefragt. Zielwert:
  Latenz ≤ 10 s. SIGINT bricht weiterhin sofort ab (kein blockierendes Long-Polling, das
  `_stop.wait` aushebelt). Ein nach dem regulären Zyklus gelesenes `/digest` darf einen
  zweiten Zyklus auslösen — das ist ein IMAP-Poll, kein Modellaufruf; wird dokumentiert.
- **E4 — Dedupe-Kollision (HC-10):** zweites Merkmal `content_hash` (SHA-256 über
  `RawMail.mime_bytes`) neben `message_id_hash`. Gleicher Key, anderer Inhalt ⇒ Kollision:
  die Mail wird mit einem abgeleiteten Schlüssel (`sha256(message_id_hash + content_hash)`)
  regulär verarbeitet, bekommt eine `🔍`-Hinweiszeile und einen WARNING-Logeintrag. Schema
  additiv auf Version 3 heben (bestehende Datenbanken werden ohne Verlust migriert; Zeilen
  ohne `content_hash` gelten als „unbekannt" und lösen nie eine Kollision aus). UID/UIDVALIDITY
  bleiben draußen (ADR-019).
- **E5 — Vorlagenwahl in `connect-llm` (HC-3): keine neue Option.** `--provider` setzt nur
  den Config-Wert. Die Vorlage wird zuerst über `LlmPreset.key` gesucht; ist `--provider`
  mehrdeutig (`openai_compatible`), gilt die **generische** Vorlage (leerer Erklärtext,
  keine `base_url`-Vorbelegung). Nicht-interaktiv wird nie eine anbieterspezifische URL
  gesetzt, die nicht ausdrücklich per `--base-url` kam.
- **E6 — PGP/S-MIME (HC-33): Verhalten bleibt, Doku und Hinweis kommen.** Die fünfzeilige
  Metadaten-Notiz („could not be processed safely") wäre für verschlüsselte Mail sachlich
  falsch — nichts Unsicheres ist passiert. Der Sanitizer erkennt `multipart/encrypted` und
  `application/pkcs7-mime` als `encrypted = True` im `SanitizationReport`; der Composer
  ergänzt die `🔍`-Zeile „encrypted (PGP/S-MIME) — content not readable by design"; README
  §Grenzen beschreibt das tatsächliche Verhalten. ADR-082.
- **E7 — Dateinamen-Kürzung (HC-22):** Kürzung in der **Mitte** mit `…`, Endung bleibt
  erhalten (bei geblockten Anhängen ist die Endung die sicherheitsrelevante Information),
  Gesamtlänge ≤ 80. Gleiche Regel in `sanitize/attachments.py` und Composer.
- **E8 — Split-Naht (HC-6):** Nur der **harte Schnitt innerhalb einer Zeile**
  (`_split_long_line`) kann neue Zeilenanfänge erzeugen; Schnitte an Zeilengrenzen liefern
  Anfänge, die `scrub_field` bereits neutralisiert hat oder die vom Programm stammen. Deshalb
  bekommt jedes Fortsetzungsstück aus einem Zeilenschnitt ein neutrales Präfix `… `
  (U+2026 + Leerzeichen). Keine Zeilenanfangs-Regeln in `final_guard`.
- **E9 — Zusatzfelder im Log (HC-11):** Bei `extra_forbidden` wird der Feldpfad durch den
  festen Platzhalter `<extra field>` ersetzt — in **beiden** Fassungen (Log und
  Reparatur-Prompt); der erfundene Name hat auch für die Reparatur keinen Wert, das Modell
  sieht seine eigene Antwort ohnehin.
- **E10 — Terminal-Fremdtext (HC-4):** eine zentrale Allowlist-Funktion (Vorbild
  `cli._safe_name`) für **alle** Strings, die von einer Gegenstelle stammen, angewendet in
  `_describe_error` **und** an der stderr-Ausgabestelle in `cli.main`; zusätzlich wird ein
  im Text auftauchender eigener API-Key maskiert.

---

## 4. Fixpakete

Neun Pakete in vier Wellen. Innerhalb einer Welle sind die Dateimengen disjunkt, die Pakete
laufen parallel im selben Arbeitsbaum. Zwischen den Wellen: Gate (§8.2), Commit.

| Welle | Pakete | Warum diese Reihenfolge |
|---|---|---|
| A | FP-1 Standardmodus · FP-2 Ausgabeschicht · FP-4 Postfach/Zustand · FP-5 Einrichtung | HC-1/HC-2 zuerst (Release-Blocker); die vier Pakete teilen keine Datei |
| B | FP-3 Erkennung/LLM-Schema · FP-6 Fernauslösung/Runner | FP-3 wartet auf FP-1 (`agents/summarizer.py`), FP-6 auf FP-2 (`composer.py`-Docstring) |
| C | FP-7 CLI-Robustheit · FP-9 Verschlüsselte Mail/Testarbeit | beide brauchen `cli.py` bzw. `sanitizer.py` frei (Welle A/B) |
| D | FP-8 Sprache und Vertrag | berührt fast jede Datei mit Literalen — läuft allein, nach allen Code-Fixes |

Vorab vergebene neue ADR-Nummern: ADR-079 (FP-4, HC-10), ADR-080 (FP-6, HC-12/HC-27),
ADR-081 (FP-7, HC-20), ADR-082 (FP-9, HC-33), ADR-083 (FP-8, HC-14). Alles andere sind
Nachträge zu bestehenden ADRs.

---

### FP-1 — Standardmodus ohne Sprachmodell (Welle A)

**Befunde:** HC-1 (high), HC-2 (medium), HC-38 Teil 1 (Verdrahtung des Standardmodus).
**Modell/Effort:** `claude-opus-5`, medium.

**Eigentum:** `src/maildigest/agents/offline.py`; in `src/maildigest/agents/summarizer.py`
**nur** die Funktionen `_fallback_headline`, `describe_without_body` und eine neu
herauszuziehende Kürzungshilfe; `tests/unit/test_offline.py`, `tests/unit/test_runner.py`
(nur neue Tests für `provider = "none"`), `tests/unit/test_cli_connect.py` (nur neuer
`connect-llm --provider none`-Fall), `tests/unit/test_cli.py` (nur neuer `test`-Fall über
die echten Hooks). Doku: README §„Mit oder ohne Sprachmodell" (falls Verhalten beschrieben
wird), ADR-076 Nachtrag.

**Aufgaben:**

- **HC-1.** Kürzung der Kopfzeile aus `enforce_output_policy` als Hilfsfunktion
  (`clamp_headline(text, limit)` o. ä.) herausziehen und in `OfflineSummarizer.summarize`
  **vor** der `Summary`-Konstruktion anwenden; Kürzung mit `…`, Ergebnis ≤ `max_length` des
  Feldes. Danach **jede** Stelle im Repo prüfen, die `Summary(...)` direkt konstruiert
  (`grep -n "Summary(" src/`), und sicherstellen, dass keine einen ungekürzten Wert in ein
  längenbegrenztes Feld gibt (auch `summary_text`, `importance_reason`, `category`,
  `attachment_summaries`-Werte). Bericht-Repro: 100 Zeichen → Exit 0, 101 Zeichen → Exit 1;
  danach beides Exit 0.
- **HC-2.** `OfflineSummarizer.summarize` füllt `attachment_summaries` je Eintrag aus
  `mail.attachment_texts` mit einem beschrifteten, gekürzten Auszug (Composer-Limit 400
  Zeichen je Wert beachten, Kürzung mit `…`); `enforce_output_policy` behält diese Schlüssel.
  `describe_without_body` behauptet „Mail without displayable content" nicht mehr, wenn
  `mail.attachment_texts` nicht leer ist — stattdessen z. B. „No mail body; N attachment(s)
  with readable text". Singular/Plural korrekt (die Zeile „1 blocked attachments" ist Teil
  von HC-14, liegt aber in derselben Funktion — hier gleich mit erledigen und im Ergebnis
  vermerken).
- **HC-38 (1).** Drei Verdrahtungstests: `build_runner` mit `llm.provider = "none"` ohne
  injizierte Stufen ⇒ `isinstance(deps.summarizer, OfflineSummarizer)` und
  `isinstance(deps.critic, OfflineCritic)`; `connect-llm --provider none --non-interactive`
  ⇒ `model` geleert, `api_key` aus der Datei entfernt; `maildigest test --dry-run --eml …`
  über die echten Hooks (nicht die Attrappen) im Werkszustand nach `init --non-interactive`.

**Akzeptanz:** `test_hc1_offline_headline_is_clamped_at_the_sanitizer_ceiling` (Betreff mit
300 Zeichen = Sanitizer-Obergrenze, Exit 0 im Ende-zu-Ende-Dry-Run, Kopfzeile endet mit `…`);
`test_hc1_no_summary_constructor_receives_unclamped_fields`; `test_hc2_processed_attachment_
appears_in_offline_message` (Mail ohne Body + `text/plain`-Anhang ⇒ Zeile `— mitteilung.txt:
…` in der Nachricht, keine Behauptung „without displayable content"); `test_hc2_body_plus_
attachment`; die drei HC-38-Tests. Coverage-Lücken `runner.py`/`cli.py` für die
`provider == "none"`-Zweige geschlossen (im Ergebnis mit Prozent nennen).

**Ergebnis-Fakten für den Doku-Agenten:** Wortlaut der neuen Nachrichtenzeilen (für SPEC §6),
Coverage vorher/nachher.

---

### FP-2 — Ausgabeschicht: Nachbrenner, Split, Fußnote, Dateinamen (Welle A)

**Befunde:** HC-6, HC-7, HC-8, HC-9 (alle medium), HC-22, HC-24 (low).
**Modell/Effort:** `claude-opus-5`, medium.

**Eigentum:** `src/maildigest/output/sanitizer.py`, `src/maildigest/output/composer.py`,
`src/maildigest/sanitize/links.py`, `src/maildigest/sanitize/attachments.py`;
`tests/unit/test_output_sanitizer.py`, `tests/unit/test_output_composer.py`,
`tests/unit/test_sanitize_links.py`, `tests/unit/test_sanitize_attachments.py`,
`tests/unit/test_hot_properties.py`, `tests/unit/test_hot_edge_cases.py`,
`tests/cold/test_cold_suite.py` (nur neue Fälle). Doku: ADR-036, ADR-040, ADR-062 Nachträge;
SECURITY §5 (Schicht 6, falls Wortlaut betroffen); ARCHITECTURE §7 (Fortsetzungspräfix).

**Aufgaben:**

- **HC-6** nach E8: In `_split_long_line` erhält jedes Stück nach dem ersten das Präfix
  `… `; `_finalize` läuft danach wie bisher (Nachbrenner erneut, ggf. Nachteilen — das
  Präfix zählt zum Limit). Bestehenden Test `test_der_angreifer_kann_keine_programmzeile_
  faelschen` um den Split-Pfad erweitern. Property-Test: ∀ Text, ∀ Limit ∈ {2000, 4096,
  zufällig ≥ 200}: kein `parts[i]` und keine Zeile in irgendeinem Teil beginnt mit
  ⚠️/📧/📎/🔍/`From:`/`Von:`/`SUSPECTED PHISHING:`/`PHISHING-VERDACHT:`, wenn die Eingabe
  ausschließlich Feldtext (durch `scrub_field`) war. Discord-Limit 2000 mit Summary-Limit
  3000 explizit als Beispiel (Bericht-Repro).
- **HC-7.** In `sanitize/links.py` die defangte Form (`_defang` bzw. `build_footnote`) von
  den Markup-Zeichen befreien, die `output/sanitizer._RE_MARKUP` kennt — `` ` ``, `*`, `|`,
  `~`, `\`, ggf. `_` außerhalb von `[.]`/`[:]`; `[` und `]` bleiben. `final_guard`
  unverändert. Regressionstest mit den beiden Bericht-Zeilen (` ```diff `, `*fett*`,
  `||spoiler||`, `~~weg~~`, `` `code` ``) bei `[links] footnote = true`.
- **HC-8.** Beide Nähte: (1) `\x00` aus jeder begrenzenden Zeichenklasse in `links.py`
  ausschließen und Platzhalter-Tokens vorab atomar konsumieren; abschließende Zusicherung
  (`assert "\x00" not in result` bzw. defensiver Ersatz) im `LinkCollector`; (2) in
  `scrub_field` die C\*-Entfernung ein zweites Mal **nach** der Link-Erkennung. Den Payload
  `"mailto:" + "a"*127 + "http://boese.example"` als expliziten Fall festnageln; Hypothesis-
  Strategie in `test_hot_properties.py` um lange Wiederholungsläufe (≥ 128 gleiche Zeichen)
  erweitern. Marker-Zuordnung bleibt erhalten (`links_found` ↔ Marker im Text).
- **HC-9.** `_RE_IPV4`: links `(?<![A-Za-z0-9])`, rechts `(?![0-9.])`; Gegenproben
  `3.14`, `1.2.3`, `v2.10.1` bleiben lesbar (Schutz aus der Vier-Oktett-Form). Fälle aus dem
  Bericht: `-192.0.2.1/login`, `192.0.2.1x`, `192.0.2.1_neu`, `a.192.0.2.1`. **Orakel**
  `assert_output_safe` um ein IPv4-Verbotsmuster erweitern (lebende Vier-Oktett-Folge).
- **HC-24.** Längenobergrenzen aus der *Erkennung* in `_RE_DOMAINISH` nehmen (großzügiger
  Backtracking-Deckel erlaubt, z. B. 253 Zeichen gesamt), Entscheidung ausschließlich in
  `_defang_domain_match`; `_LABEL` in `links.py` analog prüfen. Orakel `_RE_LIVE_DOMAIN` in
  `test_hot_properties.py`: Deckel entfernen, Unterstrich-Wortgrenze lockern, Kommentar
  „Orakel muss strikt großzügiger sein als die Implementierung". Regressionsfall
  `'a'*64 + '.com/rechnung'`.
- **HC-22** nach E7: Mittelkürzung mit `…` und erhaltener Endung in
  `sanitize/attachments.py` (Ergebnis ≤ 80); Composer-Limit unverändert (dort greift
  `scrub_plain` nur noch bei fremden Überlängen). Test: 120×`a` + `.exe` ⇒ Name enthält `…`,
  endet auf `.exe`, Länge ≤ 80.

**Akzeptanz:** alle genannten Tests grün; Property-Tests laufen mit dem strengeren Orakel
mindestens 2 000 Beispiele je Property ohne Befund (`--hypothesis-seed` beliebig);
bestehende Gegenproben (`3.14`, `test_ht4_numbers_and_abbreviations_stay_readable`) bleiben
grün; kein Teil einer Nachricht überschreitet das Teil-Limit inklusive Präfix.

**Ergebnis-Fakten für den Doku-Agenten:** Text für einen neuen Eintrag **HT-14** in TESTING §5
(„Erkennungsregex und Test-Orakel teilen dieselbe Schranke" — Klasse, Fundstelle, was geändert
wurde); geänderte Zusicherungen für SECURITY §7.1 (I3).

---

### FP-3 — Modellfreie Erkennung und LLM-Schema (Welle B)

**Befunde:** HC-5, HC-21, HC-11 (medium), HC-29 (low).
**Modell/Effort:** `claude-opus-5`, medium. Startet erst nach FP-1 (gleiche Datei).

**Eigentum:** `src/maildigest/sanitize/sanitizer.py`, `src/maildigest/models.py` (nur
`SanitizationReport`-Felder), `src/maildigest/agents/summarizer.py` (alles außer den
FP-1-Funktionen), `src/maildigest/llm/schema.py`; `tests/unit/test_sanitize_mail.py`,
`tests/unit/test_summarizer.py`, `tests/unit/test_llm_schema.py`,
`tests/unit/test_hot_fault_injection.py`, `tests/cold/test_cold_suite.py` (neue Fälle).
Doku: ADR-061, ADR-024, ADR-076 Nachträge; SECURITY §5 (Prompt-Härtung, falls Wortlaut);
REQUIREMENTS F-SEC-5 (nur falls Formulierung präzisiert werden muss).

**Aufgaben:**

- **HC-5.** Das Faktum „gefälschter Marker" wird **im Sanitizer** erhoben, vor dem
  Tag-Stripper: jede Sequenz, die case-insensitiv `MAILDIGEST` und `UNTRUSTED` in einem
  spitz geklammerten Konstrukt (`<`/`<<<` … `>`/`>>>`, beliebiger Nonce) trägt, wird
  (a) im `SanitizationReport` gezählt (`forged_markers: int`, neues Feld, Default 0) und
  (b) im Text durch das feste Token `[forged data-block marker removed]` ersetzt. Der
  Detektor in `summarizer.py` setzt `injection_suspected`, wenn `report.forged_markers > 0`
  **oder** das Token bzw. `_FORGED_MARKER_RE` im Text vorkommt (Text-Pfad bleibt als zweite
  Schicht für von Hand gebaute `SanitizedMail`). Kommentare bei `_FORGED_MARKER_RE` und
  ADR-061 korrigieren (die Prämisse „der Sanitizer entfernt nur die Winkelklammern" war
  falsch). Regressionstest **über `MailSanitizer().sanitize(RawMail(...))`**, nicht über
  `make_mail()`, mit den vier Bericht-Varianten (vollständig, ohne `<<< >>>`, ohne `>>>`,
  ohne `<<<`) — alle vier setzen den Verdacht und erzeugen die `🔍`-Zeile.
- **HC-21.** `_INSTRUCTION_PHRASES_RE` erweitern: Determiner `your|the|all|any` bzw.
  `deine|deinen|die|alle|sämtliche|bisherigen|vorherigen`; Verben `ignore|forget|disregard`
  bzw. `ignoriere|vergiss|missachte`; Objekt `instruction(s)|rule(s)|prompt` bzw.
  `Anweisung(en)|Regel(n)`. Wörtlichkeit und Objektbindung bleiben — **nicht** zu
  `ignore .* instructions` verallgemeinern. Je Variante aus dem Bericht ein Positivtest
  (sieben Formulierungen), dazu Negativtests mit Alltagsdeutsch/-englisch („du bist jetzt
  dran", „please ignore the noise in the background", „wir besprechen den System-Prompt im
  Meeting", „ignore my previous mail, here is the corrected invoice" — Letzteres darf
  **nicht** feuern: Objekt ist „mail", nicht „instructions"). Ende-zu-Ende-Fall im Werkszustand
  (`provider = "none"`) mit dem Bericht-Body ⇒ `injection suspected=yes` und Hinweiszeile.
- **HC-11** nach E9: `_error_summary` ersetzt bei `error["type"] == "extra_forbidden"` den
  Pfad durch `<extra field>`; Test mit einem Schlüsselnamen, der Mailinhalt trägt ⇒ weder
  in `failure_detail` noch in der Logzeile. ADR-024 Nachtrag.
- **HC-29.** `_redact_tokens` amortisiert linear: `\S+`-Spans einmal vorberechnen und per
  `bisect` zuordnen, oder den letzten erreichten `end` mitführen. Zeitbudget-Test
  `_redact_tokens("a.co/" * 6400)` < 0,5 s (und ein 32 000-Zeichen-Fall < 2 s); HT-7-Vermerk
  korrigieren lassen (Fakt an den Doku-Agenten: der 200 000-Zeichen-Testwert enthielt keinen
  Treffer). Eine Obergrenze für `[llm] max_tokens` ist **Fremdbedarf** (`config.py`) — als
  solcher melden, nicht ändern.

**Akzeptanz:** `test_hc5_*` (4 Varianten, über den echten Sanitizer), `test_hc21_*`
(7 positiv, ≥ 4 negativ, 1 Ende-zu-Ende), `test_hc11_*`, `test_hc29_*`; Cold-Suite um je einen
Korpusfall für HC-5 und HC-21 ergänzt; `injection_suspected`-Fehlalarmrate über den bestehenden
Korpus (`tests/corpus/*.eml`, `tests/cold/mails/*.eml`) unverändert — im Ergebnis die Zahl der
Korpusmails mit gesetztem Flag vorher/nachher nennen.

**Ergebnis-Fakten für den Doku-Agenten:** SECURITY §7.1 (I5: Feld `detail` und der Platzhalter),
SECURITY §7.2 Punkt 3 („Miss-Richtung" der Phrasenliste; bei `provider = "none"` sind die
deterministischen Indizien die einzige Quelle), HT-7-Korrektur.

---

### FP-4 — Postfach, Zustand, Zustellfristen (Welle A)

**Befunde:** HC-10 (medium), HC-23, HC-25 (low).
**Modell/Effort:** `claude-opus-5`, medium.

**Eigentum:** `src/maildigest/ingest/imap_client.py`, `src/maildigest/ingest/` (übrige),
`src/maildigest/state/db.py`, `src/maildigest/delivery.py`, `src/maildigest/models.py`
(nur `RawMail.content_hash`), `src/maildigest/pipeline.py` (nur Kollisions-Hinweis, falls
dort verdrahtet); `tests/unit/test_ingest_client.py`, `tests/unit/test_ingest_rawmail.py`,
`tests/integration/test_ingest_poll.py`, `tests/unit/test_state_db.py`,
`tests/unit/test_state_wp8.py`, `tests/unit/test_hot_state_concurrency.py`,
`tests/unit/test_delivery.py`, `tests/unit/test_hot_schedule.py`,
`tests/integration/test_sanitize_corpus.py` (nur den From-Dekodier-Helfer entfernen).
Doku: ADR-079 (neu), ADR-018/ADR-019/ADR-020/ADR-048 Nachträge, SECURITY §3
(Bedrohungszeile „gefälschte Message-ID unterdrückt echte Mail"), REQUIREMENTS F-ING-2,
ARCHITECTURE §3 (Dedupe-Key, `content_hash`, `from_addr` dekodiert) und §6 (Fristen).

**Aufgaben:**

- **HC-10** nach E4. `build_raw_mail` berechnet `content_hash`. `StateDB`: Schema additiv auf
  Version 3 (`ALTER TABLE seen_mails ADD COLUMN content_hash TEXT` beim Upgrade; der
  bestehende additive Upgrade-Pfad in `db.py` ist zu benutzen, **nicht** ein neuer
  Mechanismus). `claim()` liefert statt `bool` ein dreiwertiges Ergebnis
  (`claimed | duplicate | collision`); Aufrufer anpassen. Im Kollisionsfall verarbeitet
  `poll_once` die Mail unter dem abgeleiteten Schlüssel und setzt `RawMail.id_collision =
  True`; der Sanitizer kopiert das Flag nach `SanitizationReport.id_collision` (beide Felder
  legt FP-4 an; `sanitize/sanitizer.py` darf FP-4 dafür in Welle A anfassen — nur diese
  Kopierzeile). Die zugehörige `🔍`-Zeile im Composer („Message-ID collides with an earlier
  mail") baut **FP-9** in Welle C (dort wird `_hints_line` ohnehin erweitert). Log:
  `mail_id_collision` (WARNING, nur Hashes). Tests: Angreifer-Mail zuerst, echte danach ⇒
  beide zugestellt, zweite mit Hinweis; identische Mail zweimal ⇒ weiterhin `duplicate`;
  Datenbank der Version 2 (Fixture) wird geöffnet, migriert, alte Zeilen bleiben Duplikate
  ohne Kollisionsalarm; Nebenläufigkeitstest aus `test_hot_state_concurrency.py` bleibt grün.
- **HC-23.** From (und Reply-To, Sender — alle anzeigenamentragenden Header) in
  `build_raw_mail` vor `_collapse` per `decode_header` + `make_header` dekodieren, in
  derselben try/except-Klammer wie der Betreff; bei kaputter Kodierung Rohwert (ADR-020 (e):
  `build_raw_mail` wirft nie). Korpus-Test-Helfer entfernen. Tests: `=?utf-8?Q?J=C3=B6rg_
  M=C3=BCller?=` ⇒ `Jörg Müller`; roh-8-bit ⇒ kein Mojibake; VS16/Cf im Namen ⇒ vom
  Sanitizer als Steuerzeichen gezählt; kaputte Kodierung ⇒ Rohwert, keine Exception.
- **HC-25.** `_handle_failure`: `age = max(0, …)`; die Stunden-Schranke gilt nur, wenn
  `age` plausibel ist (≤ 24 h), sonst entscheidet allein `used >= DELIVERY_MAX_ATTEMPTS`
  und `first_queued_at` wird beim Defer auf `now` gesetzt (`outbox_defer` bekommt einen
  optionalen Parameter). `outbox_due` sammelt Zeilen ein, deren `next_attempt_at` mehr als
  2 h in der Zukunft liegt (Uhr-Rücksprung), setzt sie auf `now` und loggt
  `outbox_clock_skew_corrected`. Je ein Test pro Richtung mit nicht-monotoner Fake-Uhr
  (+3 h, −2 Tage).

**Akzeptanz:** `test_hc10_*` (≥ 4 Fälle inkl. Migration), `test_hc23_*` (4 Fälle),
`test_hc25_*` (2 Fälle); `tests/integration/test_ingest_poll.py` grün; Idempotenz-Nachweis
(zweimal abrufen ⇒ einmal verarbeiten) bleibt grün.

**Ergebnis-Fakten für den Doku-Agenten:** Schema-Version 3 und Migrationsweg (für
BETRIEB §4 Wartung und CHANGELOG), neue Logereignisse (BETRIEB §5), Bedrohungszeile.

---

### FP-5 — Einrichtung: `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`, `run` (Welle A)

**Befunde:** HC-3, HC-15 (medium), HC-17, HC-18 (Rest), HC-19, HC-34, HC-35 (low).
**Modell/Effort:** `claude-opus-5`, medium.

**Eigentum:** `src/maildigest/cli.py`, `src/maildigest/providers.py`;
`tests/unit/test_cli.py`, `tests/unit/test_cli_connect.py`, `tests/unit/test_providers.py`,
`tests/unit/test_spec_cli.py`, `tests/integration/test_cli_e2e.py`. Doku: SPEC-CLI §4
(Abschnitte `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`, `run` — nur
die von diesen Befunden berührten Sätze; die Sprachangleichung macht FP-8), ADR-075/ADR-076
Nachträge. **Nicht** anfassen: `ConfigFile.save`, die stderr-Ausgabestelle in `main`,
`llm/_http.py` (FP-7).

**Aufgaben:**

- **HC-3** nach E5. `_choose_llm_preset`: Suche zuerst `preset.key == wanted`, dann bei
  mehrdeutigem `provider` die generische Vorlage (`key == "openai_compatible"`; falls es sie
  nicht gibt: anlegen, leerer Erklärtext, leere `base_url`). Nicht-interaktiv: `base_url`
  bleibt auf `--base-url` oder dem bisherigen Dateiwert, sonst leer. Interaktive Liste: der
  vorausgewählte Eintrag ist der, dessen `base_url` dem Dateiwert entspricht, sonst der
  generische. SPEC §4 `connect-llm` Optionstabelle (`--provider`) und den Satz zur
  Anleitung anpassen. Tests: Bericht-Repro (kein Groq-Text, `base_url` bleibt leer),
  `--base-url http://127.0.0.1:1/v1` bleibt erhalten, `--provider anthropic` zeigt die
  Anthropic-Anleitung; bestehender Dateiwert überlebt einen zweiten Lauf.
- **HC-15.** `_resolve_host` gibt Host **und** erkannten `Provider` zurück (kleines
  Ergebnisobjekt); `cmd_connect_mail` prüft `supported` auf diesem Ergebnis, kein zweites
  `find_by_host` über die Adresszeichenkette. Tests: `--host me@outlook.com`,
  `me@hotmail.de`, `me@proton.me` (je mit `--no-test`) ⇒ Exit 2, Datei unverändert, Meldung
  nennt Grund und Ausweg; Gegenprobe `me@gmail.com` ⇒ `imap.gmail.com`.
- **HC-17.** `_SELFTEST_NOTICE` wird zur Funktion mit Herkunfts-Argument; `_announce_selftest`
  bekommt `source` aus `cmd_test`. Zwei Fassungen: „bundled example mail" / „the file you
  supplied" — **kein** Dateipfad im Text. Test in `test_cli.py` erweitern; Integrationsfall
  in `test_cli_e2e.py` mit `build_messenger`-Hook, der beide Nachrichten mitschreibt.
- **HC-18 (Rest).** (a) `cmd_init`: der Absatz zum Sprachmodell wandert **vor** „Next
  steps:"; die Schrittliste bekommt einen optionalen Punkt `maildigest connect-llm
  (optional: real summaries)`; stdout endet mit der Liste. (b) Regressionstest, der die von
  `init --non-interactive` erzeugte Datei gegen den **vollständigen** Feldsatz aus SPEC §5
  prüft — die §5-Tabelle maschinell lesen (Vorbild `test_spec_cli.py`), nicht eine zweite
  Liste pflegen; Felder ohne Default (`token`, `api_key`, `password`) dürfen als Kommentar
  erscheinen. (c) SPEC §4 `init`: „Pflichtfelder ohne Default" um `[llm] model` bereinigen
  (Pflicht nur bei echtem Provider, ADR-076). (d) `connect-messenger` schreibt
  `accept_commands` mit, wenn es fehlt (Default-Wert), damit alte Dateien den Feldsatz
  vervollständigen.
- **HC-19.** Der Hinweisblock (`/digest`, `/status`, Gruppen-Hinweis) wird genau einmal am
  Ende von `_setup_telegram` gedruckt — in beiden Zweigen. Test mit und ohne `--chat-id`.
- **HC-34** (vorher selbst nachstellen). In `cmd_run` (beide Betriebsarten) vor dem
  Verbindungsaufbau prüfen, ob ein IMAP-Passwort gesetzt ist; fehlt es, dieselbe Meldung wie
  bei unvollständiger Konfiguration (Exit 1), nicht „Mailbox unreachable". Test: Config ohne
  Passwort und ohne Env ⇒ stderr beginnt mit `Error: Invalid configuration` (oder der in
  SPEC §4 `run` festgelegten Formulierung), Exit 1.
- **HC-35** (vorher selbst nachstellen). `cmd_test`: im Zustellfehlerpfad eine Zeile
  `5/5 Not delivered (N part(s)) — queued for retry.` auf stdout; stderr-Erklärung und Exit 1
  unverändert. SPEC §4 `test` um diese dritte Form der 5/5-Zeile ergänzen.

**Akzeptanz:** `test_hc3_*` (4), `test_hc15_*` (4), `test_hc17_*` (2), `test_hc18_*` (3),
`test_hc19_*` (2), `test_hc34_*` (1), `test_hc35_*` (1); `test_spec_cli.py` grün (jede neue
oder geänderte Option steht in der Tabelle); Bericht-Repros für HC-3 und HC-15 laufen mit
dem beschriebenen Ergebnis.

**Ergebnis-Fakten für den Doku-Agenten:** exakte neue stdout-Zeilen (für FP-8/SPEC), Liste der
geänderten SPEC-Sätze.

---

### FP-6 — Fernauslösung und Runner (Welle B)

**Befunde:** HC-12, HC-13 (medium), HC-26, HC-27, HC-28 (low), HC-37 (a) (info),
HC-38 Teil 2.
**Modell/Effort:** `claude-opus-5`, medium. Startet nach Welle A.

**Eigentum:** `src/maildigest/runner.py`, `src/maildigest/messenger/telegram.py`,
`src/maildigest/config.py` (nur falls eine Konstante konfigurierbar gemacht wird — bevorzugt
**nicht**), `src/maildigest/cli.py` (nur `cmd_run` und der Telegram-Hinweistext in
`_setup_telegram`), `src/maildigest/output/composer.py` (nur der Docstring von
`compose_plain`); `tests/unit/test_runner.py`, `tests/unit/test_commands.py`,
`tests/unit/test_telegram_discovery.py`, `tests/integration/test_runner_e2e.py`,
`tests/unit/test_hot_schedule.py`. Doku: ADR-080 (neu: Geltungsbereich und Latenz der
Fernauslösung), ADR-077 und ADR-049 Nachträge; SPEC-CLI §4 `run` und §5 `accept_commands`;
README §„Vom Handy aus anstoßen"; BETRIEB §3 (Cron) und §5 (neue Logereignisse);
ARCHITECTURE §2 (Runner-Schleife).

**Aufgaben:**

- **HC-13** zuerst (Einzeiler): `triggered = [self.handle_command(c) for c in
  self.poll_commands_once()]`, danach `if any(triggered): continue`. Test mit dem Stapel
  `["/status", "/digest", "/status", "/status"]` ⇒ drei Statusantworten, ein Zusatzzyklus;
  `["/digest", "/status"]` ⇒ eine Antwort.
- **HC-12** nach E3. `run_forever`: statt `_wait(poll_interval)` eine Schleife über
  Abschnitte von höchstens `COMMAND_POLL_SECONDS = 10` (Modulkonstante, `Final`), die nach
  jedem Abschnitt `poll_commands_once()` abfragt und bei `/digest` sofort den nächsten Zyklus
  beginnt; `/status` wird im Abschnitt beantwortet. Abbruch weiterhin über `self.stopped` /
  `_stop.wait(abschnitt)`; die injizierbare `sleep`-Attrappe und die Fake-Uhr der bestehenden
  Tests müssen weiter funktionieren. Fehler der Befehlsabfrage bleiben folgenlos
  (`command_poll_failed`). Test mit virtueller Uhr: `/digest` nach 5 s bei
  `poll_interval_seconds = 60` ⇒ Zyklus startet vor Sekunde 15; SIGINT-Test: `stop()` während
  eines Abschnitts beendet `run_forever` ohne weitere Abfrage.
- **HC-27** nach E2. `run_once` ruft am Ende `poll_commands_once()` und behandelt jeden
  Befehl: `/status` antworten, `/digest` konsumieren und als `command_ignored_once` (INFO)
  loggen. Doku: SPEC §4 `run` (`--once`: „Befehle werden beim nächsten Lauf bedient; `/digest`
  ist dort wirkungslos"), §5, README, BETRIEB §3 (Zeile in der Cron-Liste), ADR-080. Der
  Einrichtungs-Tipp in `_setup_telegram` nennt beide Betriebsarten korrekt.
- **HC-26.** `run_once`: `maybe_send_low_digest()` samt Flush in die ausnahmefeste Zone
  (`finally`) ziehen; Fehler des Digests abfangen und loggen, damit sie den `IngestError`
  nicht verdecken. `run_forever`: denselben Schritt im `except IngestError`-Zweig vor
  `continue`. Test: wartender Low-Eintrag, Uhr nach `low_digest_time`, Ingest wirft ⇒ Digest
  geht trotzdem raus. ADR-049 (c) Nachtrag.
- **HC-28.** `handle_command('/status')`: `scrub_plain(self.config.imap.folder, max_chars=80)`
  **vor** der Interpolation; `compose_plain` bleibt reine Code-Nachricht, Docstring nennt die
  drei Aufrufer und die Regel „variable Anteile scrubbt der Aufrufer". Test: Ordnername mit
  Zeilenumbruch, Struktur-Emoji und `*Markdown*` ⇒ nichts davon in der Antwort. Fakten für
  HT-12/ADR-077/SECURITY §5 an den Doku-Agenten.
- **HC-37 (a).** README und SPEC §5: Befehle werden tolerant erkannt (Groß/Klein,
  umgebende Leerzeichen, `@bot`-Suffix, Zusatztext hinter dem Befehl wird ignoriert); kein
  Zeichen des Zusatztextes erreicht je eine Nachricht. Kein Code-Fix.
- **HC-38 (2).** `run_forever` mit einer `commands`-Attrappe, die im ersten Zyklus
  `("/digest",)` liefert ⇒ zwei Zyklen ohne dazwischenliegendes Warten; Coverage des
  `continue`-Zweigs.

**Akzeptanz:** `test_hc12_*` (2), `test_hc13_*` (2), `test_hc26_*` (1), `test_hc27_*` (2:
`/status` beantwortet, `/digest` konsumiert und Offset gesetzt), `test_hc28_*` (1),
`test_hc38_digest_triggers_second_cycle_without_wait`; bestehende Tests zu Signal-Handling,
Backoff und Digest-Uhrzeit grün; `tests/integration/test_runner_e2e.py` grün.

**Ergebnis-Fakten für den Doku-Agenten:** HT-12-Korrektur (Begründung trägt nicht mehr; jetzt
gilt „Aufrufer scrubbt"), HC-37 (b) als Schichtgrenze (kein Rate-Limit für `/status`;
`/digest`-Fluten werden zu einem Zyklus zusammengefasst), neue Logereignisse.

---

### FP-7 — CLI-Robustheit: Fremdtext, atomares Schreiben, Fehlerklassen (Welle C)

**Befunde:** HC-4, HC-20 (medium), HC-30, HC-32 (low).
**Modell/Effort:** `claude-opus-5`, medium. Startet nach Welle B.

**Eigentum:** `src/maildigest/cli.py`, `src/maildigest/llm/_http.py`,
`src/maildigest/messenger/_http.py`, `src/maildigest/ingest/imap_client.py` (nur
Fehlerklassen für Transport- vs. Anmeldefehler); `tests/unit/test_cli.py`,
`tests/unit/test_cli_connect.py`, `tests/unit/test_hot_cli_robustness.py`,
`tests/unit/test_llm_providers.py`, `tests/unit/test_messenger_adapters.py`,
`tests/unit/test_ingest_client.py`. Doku: ADR-081 (neu: atomares Schreiben), ADR-055
Nachtrag (vierte Fremddatenquelle), SECURITY §6 (Terminal-Filter, atomares Schreiben),
SPEC-CLI §2 und §4 `connect-mail` (Serverantwort vs. I5-Verzicht).

**Aufgaben:**

- **HC-4** nach E10. Zentrale Funktion (z. B. `sanitize_foreign_text(value, max_chars)` in
  `cli.py` oder einem kleinen Hilfsmodul, das beide Schichten importieren dürfen — keine
  zirkulären Importe): druckbares ASCII plus Umlaute/ß und übliche Satzzeichen, Rest `·`,
  Länge gedeckelt. Anwenden in `llm/_http.py::_describe_error` auf `message`,
  `metadata.provider_name`, `metadata.raw` und `body_error_suffix`; zusätzlich an der
  stderr-Ausgabestelle in `cli.main` (oder `Console.err`) auf den Ausnahmetext. API-Key-
  Maskierung: kommt der konfigurierte Schlüssel (oder ein Präfix ≥ 8 Zeichen davon) im
  Text vor, wird er durch `***` ersetzt. Tests: Bericht-Payload mit ESC, BEL, `\x1b]0;…`,
  Fenstertitel, Schlüssel im `raw`-Feld ⇒ stderr enthält keine C0-Zeichen außer `\n` und
  nicht den Schlüssel; IMAP-Ordnernamen-Pfad (`_safe_name`) bleibt unverändert grün.
- **HC-20** nach ADR-081: `ConfigFile.save` schreibt in eine temporäre Datei im
  Zielverzeichnis (`O_WRONLY|O_CREAT|O_EXCL`, 0600), `flush` + `os.fsync`, dann
  `os.replace`; im Fehlerfall `finally`-Aufräumen der Temp-Datei, dann `CliError`. Das
  bestehende `os.chmod` bleibt als Absicherung. Test: `RLIMIT_FSIZE`-Repro aus dem Bericht
  (oder eine Attrappe, die beim Schreiben `OSError(ENOSPC)` wirft) ⇒ alte Datei byteidentisch
  erhalten, keine Temp-Datei übrig, Exit 1; Erfolgsfall ⇒ Rechte 0600, Inhalt vollständig.
- **HC-30.** `_retry_after_seconds` in **beiden** `_http.py`: nach `float()` auf
  `math.isfinite` prüfen; nicht endlich oder negativ ⇒ `None`. Tests mit `nan`, `inf`,
  `-nan`, `1e400`, `-5`, `abc`, HTTP-Datum ⇒ nie `ValueError` aus `time.sleep`, Ratenlimit
  landet als `LLMRateLimited` bzw. `MessengerError` in der Taxonomie.
- **HC-32** (vorher selbst nachstellen). Transport- und Anmeldefehler in
  `ingest/imap_client.py` unterscheidbar machen (`ImapAuthError(ImapConnectionError)` o. ä.;
  bestehende `except ImapConnectionError` bleiben gültig); `cmd_connect_mail` hängt den
  Anbieter-Hinweis („app password …") nur bei Anmeldefehlern an; Ordnerliste und Meldung
  „pick one of the folders listed above" in die richtige Reihenfolge (Liste zuerst, beide
  auf demselben Stream oder Meldung ohne „above"). SPEC §4 `connect-mail`: der Satz zur
  Serverantwort wird an das konservative Verhalten angepasst („nennt die Fehlerklasse; der
  Servertext wird wegen I5 nicht ausgegeben") — I5-Verzicht ausdrücklich in SECURITY §6
  festhalten. Tests: `ConnectionRefusedError` ⇒ kein App-Passwort-Hinweis; Login-Fehler ⇒
  Hinweis; Reihenfolge der Ausgaben.

**Akzeptanz:** `test_hc4_*` (≥ 3), `test_hc20_*` (2), `test_hc30_*` (≥ 6 Werte), `test_hc32_*`
(3); `.venv/bin/pytest tests/unit/test_hot_cli_robustness.py` grün (keine Secrets in
Fehlermeldungen — Property bleibt).

---

### FP-8 — Sprache und Vertrag (Welle D, allein)

**Befunde:** HC-14 (low, sechsfach gemeldet; Voraussetzung für die zweite Cold-Runde).
**Modell/Effort:** `claude-opus-5`, medium. Startet nach Welle C, läuft **allein**.

**Eigentum:** alle Quelldateien unter `src/maildigest/` **nur für Literale**, die der Nutzer
sieht; `tests/**` für angepasste Erwartungen und einen neuen Vertragstest; `docs/SPEC-CLI.md`
vollständig; `README.md` (Abschnitte mit zitierter Programmausgabe: „So sieht eine Nachricht
aus", „Quickstart", FAQ-Zitate); `docs/REQUIREMENTS.md` (nur zitierte Literale);
`docs/ARCHITECTURE.md` §7 (Nachrichtenformat); ADR-083 (neu).

**Aufgaben:**

1. **ADR-083 „Ausgabesprache":** englischer Rahmen und englische CLI, `[general] language`
   steuert nur die Zusammenfassung; `docs/` deutsch; Begründung (Reichweite, ein Wortlaut für
   den Vertrag); Konsequenz: SPEC-CLI ist wörtlicher Vertrag der englischen Ausgabe.
2. **Restliteral-Übersetzung** (Code): `sanitize/sanitizer.py` `_TRUNCATION_MARKER = "[gekürzt]"`
   → `"[truncated]"` und `"(unbekannter Absender)"` → `"(unknown sender)"`;
   `output/composer.py` `"Datum unbekannt"` → `"date unknown"`; `agents/summarizer.py`
   `"Mail ohne darstellbaren Inhalt."` und `"Mail ohne Betreff"` (falls FP-1 sie nicht
   schon ersetzt hat) → englisch; `cli.py` `"Abgebrochen."` → `"Error: Aborted."` mit
   Exit-Code nach SPEC §2 (130 oder 1 — festlegen und in §2 eintragen). Vorher
   `grep -rn "[äöüÄÖÜß]" src/maildigest --include=*.py | grep -v '#' | grep '"'` als
   Vollständigkeitsprüfung; Docstrings/Kommentare bleiben deutsch. Tests, die deutsche
   Literale erwarten, anpassen. `_RE_STRUCTURE_LABEL` behält die deutschen Alternativen.
3. **SPEC-CLI §2, §4, §6, §7** auf den tatsächlichen Wortlaut bringen: jede zitierte
   Abfrage, Schrittzeile, Fehlerzeile, Bilanzzeile und jede Zeile des Nachrichtenformats
   (inkl. Metadaten-Notiz, Selbsttest-Vorspann, Testnachricht, `/status`-Antwort, Sammel-
   Digest-Kopf, Fußnote). **Methode:** die CLI wirklich laufen lassen (`init
   --non-interactive`, `connect-* --non-interactive --no-test`, `test --dry-run --eml`,
   `run --once` gegen die Attrappen aus `tests/cold/scripts/`) und die Strings kopieren, nicht
   aus dem Code abschreiben. Wo FP-1 bis FP-7 neue Zeilen eingeführt haben (Ergebnisse der
   Wellen A–C liegen dem Agenten vor), diese aufnehmen.
4. **README** und **ARCHITECTURE §7** gleich behandeln; REQUIREMENTS nur, wo Literale zitiert
   werden.
5. **Vertragstest** `tests/unit/test_hc14_spec_literals.py`: liest aus SPEC §6 die
   strukturgebenden Zeilenanfänge (`From:`, `📎 Not processed:`, `🔍 Notes:`,
   `⚠️ SUSPECTED PHISHING:`, die fünf Zeilen der Metadaten-Notiz, die 5/5-Formen aus §4
   `test`) und prüft sie gegen die Konstanten/Ausgaben des Composers und der CLI —
   maschinell, damit der Vertrag nicht erneut still veraltet.

**Akzeptanz:** obiger `grep` liefert keine nutzersichtbare deutsche Zeichenkette mehr;
`test_hc14_spec_literals.py` grün; `test_spec_cli.py` grün; ein Cold-Tester, der SPEC §4
„Zeile für Zeile" gegen `init --non-interactive`, `test --dry-run` und `run --once`
vergleicht, findet keine Abweichung (der Agent macht diesen Vergleich selbst zum Schluss
und protokolliert ihn im Ergebnis).

---

### FP-9 — Verschlüsselte Mail, Hinweiszeilen, mechanische Zusagen (Welle C)

**Befunde:** HC-33 (low), HC-38 Teil 3; dazu die Composer-Zeile für HC-10 (Fremdbedarf aus FP-4).
**Modell/Effort:** `claude-opus-5`, medium. Startet nach Welle B.

**Eigentum:** `src/maildigest/sanitize/sanitizer.py` (Erkennung `multipart/encrypted`,
`application/pkcs7-mime`, `application/pgp-encrypted`), `src/maildigest/models.py`
(`SanitizationReport.encrypted`), `src/maildigest/output/composer.py` (`_hints_line`),
`src/maildigest/agents/critic.py` (nur `collect_signals`, falls das Signal dem Kritiker
mitgegeben wird); `tests/unit/test_sanitize_mail.py`, `tests/unit/test_output_composer.py`,
`tests/unit/test_invarianten.py`, `tests/unit/test_critic_signals.py`. Doku: ADR-082 (neu),
README §„Grenzen dieser Version" (Absatz verschlüsselte Mail), SECURITY §4 (Allowlist-Hinweis),
SPEC-CLI §6 (neue `🔍`-Hinweise).

**Aufgaben:**

- **HC-33** nach E6 (vorher selbst nachstellen). Sanitizer setzt `encrypted = True`;
  Composer-Hinweis `encrypted (PGP/S-MIME) — content not readable by design`; Kritiker
  bekommt das Signal als Fakt (kein Risiko-Aufschlag). README-Absatz: statt „nur die
  Metadaten-Notiz" das tatsächliche Verhalten (Kopfzeile, Absender, Hinweis, geblockte
  Teile). Tests: `multipart/encrypted` mit `protocol="application/pgp-encrypted"` und eine
  S/MIME-Mail ⇒ Hinweiszeile, Exit 0, kein entschlüsselter Inhalt (trivial), keine
  Metadaten-Notiz.
- **HC-10-Hinweis:** `_hints_line` zeigt bei `report.id_collision` die Zeile
  `Message-ID collides with an earlier mail`. Test über `compose()` mit gesetztem Flag.
- **HC-38 (3).** `test_invarianten.py`: (a) die Menge der `.send(`-Aufrufstellen in
  `src/maildigest/` per AST gegen eine gepflegte Positivliste (Datei + Funktion) sperren;
  (b) per AST prüfen, dass jedes Argument einer Sendestelle aus `compose_plain`,
  `compose_failure`, `compose` oder einem `DigestMessage`-Ausdruck stammt; (c) die Aufrufer
  von `compose_plain` gegen eine Positivliste sperren (Stand nach FP-6: `cli.py`
  Testnachricht, `cli.py` Selbsttest-Vorspann, `runner.py` `/status`). Kommentar im Test:
  „Wer hier eine Stelle ergänzt, muss den variablen Anteil scrubben (HC-28)".

**Akzeptanz:** `test_hc33_*` (2), `test_hc10_hint_line_*` (1), `test_hc38_send_sites_*` (3);
Cold-Suite grün; Korpus-Läufe ohne Änderung des Verhaltens für unverschlüsselte Mail.

---

## 5. Doku-Agent (Welle E, nach allen Fixes)

**Modell/Effort:** `claude-opus-5`, medium. Ein Agent, läuft allein. Eingabe: die neun
Ergebnisobjekte der Fixpakete (Anhang A), `git diff 14ad9ed..HEAD --stat`, dieses Dokument.

**Aufgaben (in dieser Reihenfolge):**

1. **`docs/TESTING.md`** — neuer Abschnitt **§7 „Findings-Log (Abschluss-Testrunde,
   HC-1 … HC-38)"** nach dem Muster von §6: Tabelle `HC | Severity | Titel (Kurzform) |
   Status | Tests | ADR`. Status aus den Ergebnisobjekten: `gefixt`, `bewusst offen`
   (mit Begründung in einer Zeile), `nicht reproduzierbar` (Nachweis), `erledigt in 14ad9ed`
   (HC-16, HC-31), `in HC-3 aufgegangen` (HC-36). Darunter die Absätze, die die Pakete
   geliefert haben: **HT-14** in §5 (FP-2), HT-7-Korrektur (FP-3), HT-12-Korrektur (FP-6),
   HC-37 (b) als Schichtgrenze (FP-6), die Doku-Klarstellung aus „Geprüft und verworfen"
   Nr. 6 (`run --once` läuft nicht unter den Signal-Handlern — Ctrl+C/Cron-Timeout erzeugt
   den Zustand `sanitized` real; gehört nach BETRIEB §3 und hierher). §4: der Haken „Zweite
   Cold-Runde" bleibt offen mit dem Vermerk „läuft als §6 dieser Fixrunde".
2. **`CHANGELOG.md`** §Unveröffentlicht: Unterabschnitt **„Behoben"** — je Befund die
   **Wirkung für den Nutzer**, nicht die Nummer allein (Nummern in Klammern); Unterabschnitt
   „Geändert": Schema-Version 3 der Zustandsdatenbank (automatische Migration, kein
   Eingriff nötig), atomares Schreiben der Konfiguration, Befehlsabfrage alle ≤ 10 s,
   Befehle auch bei `run --once`, Ausgabesprache (ADR-083). Der 0.1.0-Abschnitt bleibt
   unverändert.
3. **`docs/SECURITY.md` §7** — Momentaufnahme erneuern: Datum, Modulzahl, Zahl der
   `.send()`-Stellen und `compose_plain`-Aufrufer (jetzt mechanisch gesperrt, HC-38), I3
   (Fortsetzungspräfix, IPv4-Regel, NUL-Zusicherung), I5 (Feld `detail`, Platzhalter
   `<extra field>`, Terminal-Allowlist für Anbietertext, Servertext-Verzicht), I6
   (Kollisionspfad HC-10). §7.2: Phrasenliste (Miss-Richtung, bei `provider = "none"`
   einzige Quelle), zweite Cold-Runde „steht aus — §6 dieser Fixrunde".
4. **`docs/BETRIEB.md`** §3 (Cron-Zeile zu Befehlen und zum Signal-Verhalten von `--once`),
   §4 (Migration auf Schema 3, wie man sie erkennt), §5 (neue Logereignisse:
   `mail_id_collision`, `outbox_clock_skew_corrected`, `command_ignored_once`, …).
5. **Konsistenzlauf** über README, SPEC-CLI, ARCHITECTURE, REQUIREMENTS, DECISIONS: (a) jede
   in §7-Tabelle genannte Testfunktion existiert (`.venv/bin/pytest --collect-only -q |
   grep <name>`); (b) jeder genannte ADR existiert, ADR-079 … ADR-083 stehen in aufsteigender
   Reihenfolge am Ende, Nachträge tragen das Datum; (c) keine Aussage „ab Werk aus" zu
   `accept_commands` mehr (ADR-078); (d) README-Anker und Querverweise lösen auf;
   (e) REQUIREMENTS: NF-8 bleibt `in-progress`, F-ING-2 nennt das zweite Merkmal.
6. **Fremdbedarf** aus den Ergebnisobjekten: reine Doku-Punkte selbst erledigen; Code-Punkte
   im Ergebnis auflisten (der Orchestrator entscheidet).

**Nicht anfassen:** `src/`, `tests/` (außer nichts), `docs/TESTRUNDE-HOT-COLD.md`.
**Akzeptanz:** `pytest` weiterhin grün (Doku-Änderungen können `test_spec_cli.py` und
`test_hc14_spec_literals.py` treffen — dann ist die Doku falsch, nicht der Test);
Konsistenzlauf ohne offene Punkte oder mit benannter Restliste.

## 6. Testrunde 2 (Hot & Cold)

Zweck: (1) die 35 Fixes unabhängig bestätigen, (2) den seit `13cb859` geänderten Code neu
angreifen, (3) die in TESTRUNDE-HOT-COLD.md §„Abdeckung und Grenzen" benannten Lücken
schließen, (4) den Haken „zweite Cold-Runde" (TESTING §4) **methodisch sauber** erfüllen:
die kalten Spuren bleiben blackbox, und **die Skeptiker der kalten Befunde bekommen keinen
Code** — sie prüfen mit denselben Mitteln wie die Spur (Vertrag + CLI). Nur Skeptiker heißer
Befunde lesen Code.

**Rahmen (alle Tester):** kein Fix, keine Änderung im Repo, keine Schreiboperation über git;
Artefakte im Scratchpad; Befundformat aus TESTING §3 (`Severity`, `Referenz`, `Repro`,
`Beobachtet`, `Erwartet`, `Fix-Richtung`, `Herkunft`); Severity-Maßstab TESTING §5
(Wirkung beim Nutzer). Jeder Tester liefert zusätzlich eine **HC-Verifikationstabelle** für
die ihm zugewiesenen Befunde: `bestätigt behoben | weiterhin fehlerhaft | nicht prüfbar`
mit Nachweis (Ausgabe, Exit-Code, Test).

**Vorbereitung der kalten Umgebung** (Orchestrator, vor dem Start — §8.5): Wheel bauen,
Verzeichnis `~/maildigest-coldtest2/` mit venv + installiertem Wheel und **nur**
`README.md`, `docs/REQUIREMENTS.md`, `docs/SPEC-CLI.md`, `CHANGELOG.md`; Attrappen aus
`tests/cold/scripts/` (`sink_server.py`, `mock_llm.py`, `imap_server.py`) dorthin kopieren
(sie sind Werkzeug, kein Quellcode).

**Kalte Spuren** (5 Agenten, `claude-opus-5`, medium; Verbot: `src/`, `tests/`, alles außer
den vier Dokumenten und der kalten Umgebung):

| Spur | Auftrag | HC-Verifikation |
|---|---|---|
| cold/spec | SPEC-CLI §2–§7 Zeile für Zeile gegen alle sechs Kommandos, Exit-Codes, Optionen | HC-14, HC-15, HC-17, HC-18, HC-19, HC-32, HC-34, HC-35, HC-3 |
| cold/funktional | eigene Mails (normal, Newsletter, PDF, exe, nur Anhang, langer Betreff, RFC-2047-Absender, PGP) im Werkszustand und mit Mock-LLM | HC-1, HC-2, HC-22, HC-23, HC-33 |
| cold/injection | Marker-Nachbau in allen Varianten, Phrasen-Varianten, Fußnoten-Markdown, NUL/lange Läufe, Split-Fälschung bei Discord-Limit | HC-5, HC-21, HC-7, HC-6, HC-8, HC-9 (soweit blackbox sichtbar) |
| cold/kanal | Discord-Sink und Telegram-Attrappe: Rendering, Split, Fußnote, `/status`-Antwort | HC-7, HC-28, HC-16 |
| cold/fernauslösung | Dauerbetrieb gegen Telegram-Attrappe: Latenz, gemischte Stapel, `run --once`, tolerante Erkennung, Fluten | HC-12, HC-13, HC-27, HC-37 |

**Heiße Spuren** (6 Agenten, `claude-opus-5`, medium; voller Zugriff; Schwerpunkt: der Diff
`git diff 14ad9ed..HEAD` und die Fehlerklassen dieser Runde — Nähte zwischen Schichten,
Orakel = Implementierung, Split-/Regex-Grenzen, Zeit und Uhr):

| Spur | Schwerpunkt | HC-Verifikation | Zusätzlich (Lücken aus dem Bericht) |
|---|---|---|---|
| hot/sanitize-output | `output/`, `sanitize/links.py`, `attachments.py`, Property-Orakel | HC-6, HC-7, HC-8, HC-9, HC-22, HC-24 | `html_to_text.py`, `extract_pdf.py` (Limits, Kindprozess) |
| hot/llm-erkennung | `agents/`, `llm/`, Phrasenliste, Marker, Log-Felder | HC-4, HC-5, HC-11, HC-21, HC-29, HC-30 | `[llm.critic]`-Overrides, `max_tokens`-Grenzen |
| hot/zustand-zustellung | `state/`, `delivery.py`, `ingest/` | HC-10, HC-20, HC-23, HC-25, HC-26 | Migration v2→v3 mit echter alter Datei, `move_processed_to`/UID MOVE, `max_mail_bytes`, volle Platte |
| hot/cli | `cli.py`, `providers.py` | HC-3, HC-15, HC-17, HC-18, HC-19, HC-32, HC-34, HC-35 | getUpdates-Flow mit mehreren Chats, `[limits]`-Schwellen |
| hot/runner | `runner.py`, `messenger/telegram.py` | HC-12, HC-13, HC-27, HC-28, HC-38 | SIGTERM im Dauerbetrieb, Long-Poll-Fehler (Timeout, 429, doppelte Updates) |
| hot/invarianten | I1–I8 per AST/grep über die gesamte Codebasis, Doku-gegen-Code (SPEC, SECURITY §7, ARCHITECTURE) | HC-14, HC-38 (3), HC-2 (Doku) | Coverage-Bericht gegen NF-6, `test_invarianten.py` vollständig |

**Skeptiker:** je Rohbefund **ein** Agent (`claude-opus-5`, medium), Auftrag: widerlegen;
kalte Befunde ohne Code-Zugriff, heiße mit. Kein Prüf-Deckel je Spur (in der letzten Runde
blieben so 7 Befunde ungeprüft). Urteil: `bestätigt | widerlegt | nicht prüfbar` mit
Severity-Vorschlag und Begründung.

**Bericht-Agent** (`claude-opus-5`, medium): führt Dubletten zusammen, schreibt
`docs/TESTRUNDE-2.md` im Format von TESTRUNDE-HOT-COLD.md (Kurzfassung, Befunde `HC2-n`,
„Geprüft und verworfen", „Abdeckung und Grenzen", Empfehlung) und zusätzlich die
**konsolidierte HC-Verifikationstabelle** (je HC: kalt/heiß/beides bestätigt, Nachweis).
Er ändert keine andere Datei.

## 7. Abschlussprüfung (Fable 5.1, medium — ein Agent, nach Hot & Cold)

**Modell:** `claude-fable-5-1` (volle ID; Fallback-Alias `fable` nur, wenn die ID abgelehnt
wird — der Agent nennt im Bericht, welches Modell tatsächlich lief). Effort **medium**.
Voller Zugriff, **keine Code-Änderung**, keine git-Schreiboperation.

**Eingabe:** dieses Dokument, TESTRUNDE-HOT-COLD.md, TESTRUNDE-2.md, TESTING §7,
`git diff 14ad9ed..HEAD`, die Ergebnisobjekte der Pakete und des Doku-Agenten.

**Auftrag:**

1. **Eigene Nachprüfung jedes Befunds ≥ medium** (16) über den **Original-Repro** aus
   TESTRUNDE-HOT-COLD.md — nicht über die Regressionstests. Dazu alle sechs ursprünglich
   ungeprüften (HC-32 … HC-37) und mindestens fünf weitere low-Befunde nach Zufall.
2. **Vollständiger Lauf:** `pytest --cov=maildigest --cov-report=term-missing`, `ruff`, `mypy`;
   Coverage gegen NF-6 und gegen die in HC-38 genannten Zweige.
3. **Vertragsprobe nach HC-14:** `init --non-interactive`, `test --dry-run --eml`, `run --once`
   gegen Attrappen — Ausgabe Zeile für Zeile gegen SPEC §4/§6.
4. **Plausibilität der Testrunde 2:** Severities der `HC2-n`-Befunde nachvollziehen, bei
   Bedarf mit Begründung anders einstufen; die Skeptiker-Urteile zu kalten Befunden auf ihre
   Blackbox-Reinheit prüfen.
5. **Invarianten I1–I8** stichprobenartig selbst (AST-Test lesen, zwei Pfade von Hand).
6. **Urteil:** `release-fähig: ja | nein | ja mit Auflagen`, Restliste offener Punkte mit
   Severity und Zuordnung zu einem Nachfix-Paket, Einschätzung, ob der Haken „zweite
   Cold-Runde" (TESTING §4) jetzt erfüllt ist.

**Ausgabe:** `docs/ABNAHME-FIXRUNDE.md` (deutsch, gleiches Format wie die Testberichte:
Kurzfassung, Nachprüfung je HC als Tabelle, Befunde, Urteil) plus das strukturierte
Urteil (Anhang A.4). Eine Nachfixrunde ist **Nutzerentscheidung** und wird nicht automatisch
gestartet.

---

## 8. Orchestrierung

### 8.1 Modelle und Effort

| Rolle | Modell (volle ID) | Effort | Anzahl |
|---|---|---|---|
| Fix-Agent FP-1 … FP-9 | `claude-opus-5` | medium | 9 (+ Nachbesserung, max. 1 je Paket) |
| Gate je Welle | `claude-opus-5` | low | 4 |
| Doku-Agent | `claude-opus-5` | medium | 1 |
| Vorbereitung kalte Umgebung | `claude-opus-5` | low | 1 |
| Kalte/heiße Spuren | `claude-opus-5` | medium | 5 + 6 |
| Skeptiker | `claude-opus-5` | medium | 1 je Rohbefund (erwartet 20–60) |
| Bericht-Agent | `claude-opus-5` | medium | 1 |
| Abschlussprüfung | `claude-fable-5-1` | medium | 1 |

Volle Modell-IDs sind Pflicht: der Alias `opus` löst in dieser Umgebung auf Opus 4.8 auf
(PLAN.md §5, verifiziert 2026-08-28); für `fable` ist nicht belegt, welche Version der Alias
trifft. Effort ist nur über Workflow-`agent()` setzbar.

### 8.2 Gates und Commits

Nach jeder Welle ein Gate-Agent: `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests`,
`.venv/bin/mypy src`; Ergebnisobjekte auf `Fremdbedarf` prüfen und rein dokumentarische
Punkte selbst erledigen; bei Grün **committen** (`git add -A`, Nachricht
`Fixrunde Welle <X>: <HC-Liste> (<FP-Liste>)`, Rumpf mit einer Zeile je Befund, Abschluss
`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`). Bei Rot: Diagnose je
Paket, ein Nachbesserungs-Agent für das betroffene Paket (gleiches Modell, gleiche
Regeln, Eingabe: Gate-Diagnose), dann ein zweites Gate. Bleibt es rot, endet der Workflow
mit Bericht an den Nutzer — keine dritte Runde ohne Rückfrage. Code-Fremdbedarf, der nicht
in einem späteren Paket vorgesehen ist, wird vom Gate als offener Punkt gemeldet und vom
Orchestrator einem Paket der nächsten Welle zugewiesen (Prompt-Zusatz).

### 8.3 Ablauf

```
Welle A (FP-1 ∥ FP-2 ∥ FP-4 ∥ FP-5) → Gate A
Welle B (FP-3 ∥ FP-6)               → Gate B
Welle C (FP-7 ∥ FP-9)               → Gate C
Welle D (FP-8)                      → Gate D
Doku-Agent                          → Gate E (nur Konsistenz + pytest)
Vorbereitung kalte Umgebung
Testrunde: 5 kalte ∥ 6 heiße Spuren → je Spur sofort: Skeptiker je Befund (pipeline)
Bericht-Agent (docs/TESTRUNDE-2.md)
Abschlussprüfung (Fable 5.1, medium) → docs/ABNAHME-FIXRUNDE.md
```

Gesamtumfang 50–90 Agenten. Das überschreitet die Richtgröße „unter 15 Agenten" — der
Nutzer hat diesen Umfang mit dem Auftrag ausdrücklich gewollt. Anhang B ist ein Skript;
es lässt sich über `args.phase` an den Phasengrenzen (`fix`, `doku`, `test`, `abschluss`)
in mehrere Läufe teilen, wenn eine Unterbrechung nötig wird (Resume über `resumeFromRunId`).

### 8.4 Was jeder Agenten-Prompt enthält

Rolle und Paket/Spur; Pfad dieses Dokuments und die zu lesenden Abschnitte; Repo-Pfad
`/home/kpafi/emailzusammenfassung` und `.venv/bin/…`; die Ergebnisobjekte der vorherigen
Wellen (Fix-Agenten) bzw. der Fixpakete (Doku, Tester, Abschluss); die Regel „Ergebnis nur
als Objekt nach Anhang A". Kalte Spuren zusätzlich: Pfad der kalten Umgebung und das Verbot,
`src/`/`tests/` zu lesen.

### 8.5 Vorbereitung der kalten Umgebung (vor der Testrunde)

```bash
cd /home/kpafi/emailzusammenfassung && rm -rf dist && .venv/bin/pip wheel . --no-deps -w dist -q && rm -rf ~/maildigest-coldtest2 && mkdir -p ~/maildigest-coldtest2/docs ~/maildigest-coldtest2/tools && python3 -m venv ~/maildigest-coldtest2/venv && ~/maildigest-coldtest2/venv/bin/pip install -q dist/maildigest-0.1.0-py3-none-any.whl && cp README.md CHANGELOG.md ~/maildigest-coldtest2/ && cp docs/REQUIREMENTS.md docs/SPEC-CLI.md ~/maildigest-coldtest2/docs/ && cp tests/cold/scripts/sink_server.py tests/cold/scripts/mock_llm.py tests/cold/scripts/imap_server.py ~/maildigest-coldtest2/tools/ && ~/maildigest-coldtest2/venv/bin/maildigest --help | head -3
```

---

## Anhang A — Ergebnisschemata

### A.1 Fix-Agent (FP-1 … FP-9) und Nachbesserung

```json
{
  "package": "FP-n",
  "findings": [
    {
      "id": "HC-n",
      "status": "fixed | partial | deliberately_open | not_reproducible",
      "summary": "ein Satz: was geändert wurde bzw. warum nicht",
      "files": ["src/maildigest/…"],
      "tests": ["tests/unit/test_x.py::test_hcN_…"],
      "adrs": ["ADR-0xx (neu)", "ADR-0yy (Nachtrag)"],
      "doc_changes": ["docs/SPEC-CLI.md §4 connect-llm: …"],
      "facts_for_docs": ["Wortlaut neuer Zeilen, Zahlen, Korrekturen für TESTING/CHANGELOG/SECURITY §7"],
      "user_visible_change": "ein Satz für den CHANGELOG oder leer"
    }
  ],
  "foreign_needs": [{"file": "…", "reason": "…", "patch": "unified diff oder exakte Anweisung"}],
  "checks": {"pytest": "N passed", "ruff": "clean", "mypy": "clean", "coverage_note": "optional"},
  "invariants": "Invarianten geprüft: keine Verletzung | Abweichung: …",
  "open_questions": ["nur, wenn eine Entscheidung aus §3 nicht tragfähig war"]
}
```

### A.2 Gate

```json
{"wave": "A", "green": true, "pytest": "…", "ruff": "…", "mypy": "…", "committed": "sha oder null",
 "diagnosis": [{"package": "FP-n", "problem": "…", "instruction": "…"}], "doc_foreign_needs_done": ["…"], "code_foreign_needs_open": ["…"]}
```

### A.3 Tester-Spur, Skeptiker, Bericht

Spur: `{"track": "cold/spec", "findings": [{"id": "T-n", "severity": "…", "title": "…",
"reference": "…", "repro": "…", "observed": "…", "expected": "…", "fix_direction": "…"}],
"hc_verification": [{"hc": "HC-n", "result": "fixed | still_broken | not_testable",
"evidence": "…"}], "coverage_notes": "was nicht geprüft werden konnte"}`.
Skeptiker: `{"finding_id": "T-n", "verdict": "confirmed | refuted | unverifiable",
"severity_suggestion": "…", "reasoning": "…", "repro_result": "…"}`.
Bericht: `{"report_path": "docs/TESTRUNDE-2.md", "findings_total": n, "by_severity": {…},
"hc_fixed_confirmed": [..], "hc_still_broken": [..], "hc_not_testable": [..]}`.

### A.4 Abschlussprüfung

```json
{"verdict": "go | no-go | go_with_conditions", "model_actually_used": "…",
 "report_path": "docs/ABNAHME-FIXRUNDE.md",
 "hc_reverified": [{"hc": "HC-n", "result": "fixed | still_broken | not_reproducible", "evidence": "…"}],
 "open_items": [{"id": "…", "severity": "…", "summary": "…", "suggested_package": "…"}],
 "second_cold_round_satisfied": true, "notes": "…"}
```

---

## Anhang B — Workflow-Skript

Plain JavaScript für das Workflow-Tool (kein TypeScript). `args.phase` ∈ `{all, fix, doku,
test, abschluss}` (Default `all`); `args.start_wave` ∈ `{A, B, C, D}` (Default `A`).

```js
export const meta = {
  name: 'maildigest-fixrunde',
  description: 'Fixrunde HC-1..HC-38: vier Fix-Wellen (Opus 5 medium), Doku, Hot/Cold-Testrunde, Abschluss (Fable 5.1 medium)',
  phases: [
    { title: 'Welle A' }, { title: 'Welle B' }, { title: 'Welle C' }, { title: 'Welle D' },
    { title: 'Doku' }, { title: 'Testrunde' }, { title: 'Skeptiker' }, { title: 'Bericht' },
    { title: 'Abschluss', model: 'claude-fable-5-1' },
  ],
}
const REPO = '/home/kpafi/emailzusammenfassung'
const PLAN = 'docs/PLAN-FIXRUNDE.md'
const OPUS = { model: 'claude-opus-5', effort: 'medium' }
const OPUS_LOW = { model: 'claude-opus-5', effort: 'low' }
const FABLE = { model: 'claude-fable-5-1', effort: 'medium' }
const phaseWanted = (p) => !args?.phase || args.phase === 'all' || args.phase === p

const FINDING = { type: 'object', required: ['id', 'status', 'summary', 'files', 'tests', 'adrs', 'doc_changes', 'facts_for_docs', 'user_visible_change'],
  properties: { id: { type: 'string' }, status: { type: 'string', enum: ['fixed', 'partial', 'deliberately_open', 'not_reproducible'] },
    summary: { type: 'string' }, files: { type: 'array', items: { type: 'string' } }, tests: { type: 'array', items: { type: 'string' } },
    adrs: { type: 'array', items: { type: 'string' } }, doc_changes: { type: 'array', items: { type: 'string' } },
    facts_for_docs: { type: 'array', items: { type: 'string' } }, user_visible_change: { type: 'string' } } }
const FIX_SCHEMA = { type: 'object', required: ['package', 'findings', 'foreign_needs', 'checks', 'invariants'],
  properties: { package: { type: 'string' }, findings: { type: 'array', items: FINDING },
    foreign_needs: { type: 'array', items: { type: 'object', required: ['file', 'reason', 'patch'], properties: { file: { type: 'string' }, reason: { type: 'string' }, patch: { type: 'string' } } } },
    checks: { type: 'object', required: ['pytest', 'ruff', 'mypy'], properties: { pytest: { type: 'string' }, ruff: { type: 'string' }, mypy: { type: 'string' }, coverage_note: { type: 'string' } } },
    invariants: { type: 'string' }, open_questions: { type: 'array', items: { type: 'string' } } } }
const GATE_SCHEMA = { type: 'object', required: ['wave', 'green', 'pytest', 'ruff', 'mypy', 'diagnosis', 'code_foreign_needs_open'],
  properties: { wave: { type: 'string' }, green: { type: 'boolean' }, pytest: { type: 'string' }, ruff: { type: 'string' }, mypy: { type: 'string' },
    committed: { type: 'string' }, diagnosis: { type: 'array', items: { type: 'object', required: ['package', 'problem', 'instruction'], properties: { package: { type: 'string' }, problem: { type: 'string' }, instruction: { type: 'string' } } } },
    doc_foreign_needs_done: { type: 'array', items: { type: 'string' } }, code_foreign_needs_open: { type: 'array', items: { type: 'string' } } } }
const TRACK_SCHEMA = { type: 'object', required: ['track', 'findings', 'hc_verification', 'coverage_notes'],
  properties: { track: { type: 'string' },
    findings: { type: 'array', items: { type: 'object', required: ['id', 'severity', 'title', 'reference', 'repro', 'observed', 'expected', 'fix_direction'],
      properties: { id: { type: 'string' }, severity: { type: 'string' }, title: { type: 'string' }, reference: { type: 'string' }, repro: { type: 'string' }, observed: { type: 'string' }, expected: { type: 'string' }, fix_direction: { type: 'string' } } } },
    hc_verification: { type: 'array', items: { type: 'object', required: ['hc', 'result', 'evidence'], properties: { hc: { type: 'string' }, result: { type: 'string', enum: ['fixed', 'still_broken', 'not_testable'] }, evidence: { type: 'string' } } } },
    coverage_notes: { type: 'string' } } }
const VERDICT_SCHEMA = { type: 'object', required: ['finding_id', 'verdict', 'severity_suggestion', 'reasoning', 'repro_result'],
  properties: { finding_id: { type: 'string' }, verdict: { type: 'string', enum: ['confirmed', 'refuted', 'unverifiable'] }, severity_suggestion: { type: 'string' }, reasoning: { type: 'string' }, repro_result: { type: 'string' } } }
const REPORT_SCHEMA = { type: 'object', required: ['report_path', 'findings_total', 'hc_fixed_confirmed', 'hc_still_broken', 'hc_not_testable'],
  properties: { report_path: { type: 'string' }, findings_total: { type: 'number' }, hc_fixed_confirmed: { type: 'array', items: { type: 'string' } }, hc_still_broken: { type: 'array', items: { type: 'string' } }, hc_not_testable: { type: 'array', items: { type: 'string' } } } }
const FINAL_SCHEMA = { type: 'object', required: ['verdict', 'model_actually_used', 'report_path', 'hc_reverified', 'open_items', 'second_cold_round_satisfied', 'notes'],
  properties: { verdict: { type: 'string', enum: ['go', 'no-go', 'go_with_conditions'] }, model_actually_used: { type: 'string' }, report_path: { type: 'string' },
    hc_reverified: { type: 'array', items: { type: 'object', required: ['hc', 'result', 'evidence'], properties: { hc: { type: 'string' }, result: { type: 'string' }, evidence: { type: 'string' } } } },
    open_items: { type: 'array', items: { type: 'object', required: ['id', 'severity', 'summary', 'suggested_package'], properties: { id: { type: 'string' }, severity: { type: 'string' }, summary: { type: 'string' }, suggested_package: { type: 'string' } } } },
    second_cold_round_satisfied: { type: 'boolean' }, notes: { type: 'string' } } }

const WAVES = [
  { key: 'A', title: 'Welle A', packages: ['FP-1', 'FP-2', 'FP-4', 'FP-5'] },
  { key: 'B', title: 'Welle B', packages: ['FP-3', 'FP-6'] },
  { key: 'C', title: 'Welle C', packages: ['FP-7', 'FP-9'] },
  { key: 'D', title: 'Welle D', packages: ['FP-8'] },
]
const common = `Repo: ${REPO} (venv: ${REPO}/.venv/bin). Lies zuerst ${PLAN} §0–§3 vollständig. Liefere dein Ergebnis AUSSCHLIESSLICH als Objekt nach ${PLAN} Anhang A — keine Prosa daneben.`
const fixPrompt = (fp, wave, prior, extra) => `Du bist der Fix-Agent für ${fp} (${wave.title}). ${common} Dann dein Paket ${fp} in ${PLAN} §4, PLAN.md §1–§2, docs/SECURITY.md §2, und JEDEN Befundeintrag deines Pakets in docs/TESTRUNDE-HOT-COLD.md (Repro, Ursache, Fix-Richtung). Halte §2 strikt ein: nur eigene Dateien, kein git commit/add/stash, reservierte Dateien nicht anfassen, Fremdbedarf melden statt fixen. Arbeite bis zur Definition of Done (pytest gesamt, ruff, mypy). Ergebnisse früherer Wellen (Fakten, neue Zeilen, Fremdbedarf): ${JSON.stringify(prior)}. ${extra || ''}`
const gatePrompt = (wave, results) => `Du bist das Gate für ${wave.title}. ${common} Führe aus: .venv/bin/pytest -q; .venv/bin/ruff check src tests; .venv/bin/mypy src. Prüfe die Ergebnisobjekte: ${JSON.stringify(results)} — jeder Befund hat Status, Tests existieren (pytest --collect-only), Invarianten bestätigt. Erledige reine Doku-Fremdbedarfe selbst (nicht in reservierten Dateien aus §2). Bei Grün: git add -A && git commit mit Nachricht 'Fixrunde ${wave.title}: <HC-Liste> (<FP-Liste>)', Rumpf eine Zeile je Befund, Abschluss 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>'. Bei Rot: nicht committen, je Paket Diagnose und konkrete Anweisung. Ergebnis nach Anhang A.2.`

const results = {}
let prior = []
if (phaseWanted('fix')) {
  const startIdx = WAVES.findIndex(w => w.key === (args?.start_wave || 'A'))
  for (const wave of WAVES.slice(startIdx < 0 ? 0 : startIdx)) {
    phase(wave.title)
    // Barrier ist hier gewollt: das Gate braucht alle Pakete der Welle.
    const out = (await parallel(wave.packages.map(fp => () =>
      agent(fixPrompt(fp, wave, prior), { ...OPUS, label: fp, phase: wave.title, schema: FIX_SCHEMA })))).filter(Boolean)
    out.forEach(r => { results[r.package] = r })
    let gate = await agent(gatePrompt(wave, out), { ...OPUS_LOW, label: `Gate ${wave.key}`, phase: wave.title, schema: GATE_SCHEMA })
    if (!gate.green) {
      log(`${wave.title} rot — eine Nachbesserung je betroffenem Paket`)
      const redo = (await parallel(gate.diagnosis.map(d => () =>
        agent(fixPrompt(d.package, wave, prior, `NACHBESSERUNG. Das Gate meldet: ${d.problem}. Anweisung: ${d.instruction}. Dein früheres Ergebnis: ${JSON.stringify(results[d.package])}`),
          { ...OPUS, label: `${d.package} (Nachbesserung)`, phase: wave.title, schema: FIX_SCHEMA })))).filter(Boolean)
      redo.forEach(r => { results[r.package] = r })
      gate = await agent(gatePrompt(wave, Object.values(results)), { ...OPUS_LOW, label: `Gate ${wave.key} (2)`, phase: wave.title, schema: GATE_SCHEMA })
      if (!gate.green) return { stopped_at: wave.title, gate, results }
    }
    if (gate.code_foreign_needs_open.length) log(`Offener Code-Fremdbedarf nach ${wave.title}: ${gate.code_foreign_needs_open.join(' | ')}`)
    prior = Object.values(results).map(r => ({ package: r.package, findings: r.findings.map(f => ({ id: f.id, status: f.status, summary: f.summary, facts_for_docs: f.facts_for_docs, user_visible_change: f.user_visible_change })), foreign_needs: r.foreign_needs }))
  }
}

let doku = null
if (phaseWanted('doku')) {
  phase('Doku')
  doku = await agent(`Du bist der Doku-Agent (${PLAN} §5). ${common} Lies §5 vollständig. Ergebnisobjekte aller Pakete: ${JSON.stringify(Object.values(results))}. Nutze auch 'git log --oneline 14ad9ed..HEAD' und 'git diff 14ad9ed..HEAD --stat'. Am Ende: .venv/bin/pytest -q muss grün sein; dann git add -A && git commit 'Fixrunde: Dokumentation (TESTING §7, CHANGELOG, SECURITY §7, BETRIEB)' mit Co-Authored-By-Zeile. Ergebnis: Objekt mit files_changed, open_items, code_foreign_needs.`,
    { ...OPUS, label: 'Doku', phase: 'Doku' })
}

let bericht = null
if (phaseWanted('test')) {
  phase('Testrunde')
  await agent(`Bereite die kalte Testumgebung vor: führe exakt den Befehl aus ${PLAN} §8.5 aus und bestätige, dass ~/maildigest-coldtest2/venv/bin/maildigest --help läuft. Ergebnis: Pfad und Wheel-Name.`, { ...OPUS_LOW, label: 'Kalte Umgebung', phase: 'Testrunde' })
  const COLD = ['cold/spec', 'cold/funktional', 'cold/injection', 'cold/kanal', 'cold/fernauslösung']
  const HOT = ['hot/sanitize-output', 'hot/llm-erkennung', 'hot/zustand-zustellung', 'hot/cli', 'hot/runner', 'hot/invarianten']
  const coldPrompt = (t) => `Du bist die kalte Testspur ${t} (Blackbox). Arbeitsverzeichnis: ~/maildigest-coldtest2 (venv, CLI, README.md, CHANGELOG.md, docs/REQUIREMENTS.md, docs/SPEC-CLI.md, tools/ mit Attrappen). VERBOTEN: ${REPO}/src, ${REPO}/tests und jede andere Datei des Repos außer docs/PLAN-FIXRUNDE.md §6 (dein Auftrag, deine HC-Verifikationsliste) und docs/TESTRUNDE-HOT-COLD.md (nur die Einträge deiner HC-Liste: Repro/Beobachtet/Erwartet). Kein Fix, keine Repo-Änderung. Befundformat und Severity-Maßstab: ${PLAN} §6. Ergebnis nach Anhang A.3 (Spur).`
  const hotPrompt = (t) => `Du bist die heiße Testspur ${t} (Whitebox, voller Zugriff auf ${REPO}). Lies ${PLAN} §6 (deine Zeile: Schwerpunkt, HC-Verifikation, Lücken) und §3, docs/TESTING.md §2/§5, die HC-Einträge deiner Liste in docs/TESTRUNDE-HOT-COLD.md sowie git diff 14ad9ed..HEAD für deine Dateien. Kein Fix, keine Repo-Änderung, Artefakte ins Scratchpad. Ergebnis nach Anhang A.3 (Spur).`
  const skepticPrompt = (f, track) => `Du bist Skeptiker für den Befund ${JSON.stringify(f)} aus der Spur ${track}. Auftrag: WIDERLEGEN. ${track.startsWith('cold') ? `Blackbox-Regel: du arbeitest NUR in ~/maildigest-coldtest2 mit CLI und den vier Dokumenten — ${REPO}/src und ${REPO}/tests sind verboten.` : `Voller Code-Zugriff auf ${REPO}.`} Stelle den Repro nach, prüfe Referenz und Severity-Maßstab (${PLAN} §6, docs/TESTING.md §5). Keine Änderung im Repo. Ergebnis nach Anhang A.3 (Skeptiker).`
  const tracks = [...COLD.map(t => ({ track: t, cold: true })), ...HOT.map(t => ({ track: t, cold: false }))]
  // pipeline: die Skeptiker einer Spur starten, sobald diese Spur fertig ist.
  const verified = await pipeline(tracks,
    t => agent(t.cold ? coldPrompt(t.track) : hotPrompt(t.track), { ...OPUS, label: t.track, phase: 'Testrunde', schema: TRACK_SCHEMA }),
    async (res, t) => {
      if (!res) return null
      const verdicts = (await parallel(res.findings.map(f => () =>
        agent(skepticPrompt(f, t.track), { ...OPUS, label: `Skeptiker ${t.track} ${f.id}`, phase: 'Skeptiker', schema: VERDICT_SCHEMA })))).filter(Boolean)
      return { ...res, verdicts }
    })
  const tracksDone = verified.filter(Boolean)
  log(`Testrunde: ${tracksDone.length}/${tracks.length} Spuren, ${tracksDone.reduce((n, t) => n + t.findings.length, 0)} Rohbefunde`)
  phase('Bericht')
  bericht = await agent(`Du bist der Bericht-Agent (${PLAN} §6, letzter Absatz). ${common} Rohbefunde mit Skeptiker-Urteilen und HC-Verifikationen aller Spuren: ${JSON.stringify(tracksDone)}. Schreibe docs/TESTRUNDE-2.md im Format von docs/TESTRUNDE-HOT-COLD.md (Befunde HC2-n, Dubletten zusammengeführt, 'Geprüft und verworfen', 'Abdeckung und Grenzen', konsolidierte HC-Verifikationstabelle, Empfehlung). Keine andere Datei ändern; git add docs/TESTRUNDE-2.md && git commit 'Testrunde 2 (Hot & Cold): Bericht' mit Co-Authored-By-Zeile. Ergebnis nach Anhang A.3 (Bericht).`,
    { ...OPUS, label: 'Bericht', phase: 'Bericht', schema: REPORT_SCHEMA })
}

let abschluss = null
if (phaseWanted('abschluss')) {
  phase('Abschluss')
  abschluss = await agent(`Du bist die Abschlussprüfung (${PLAN} §7) — Modell Fable 5.1, medium. ${common} Lies §7 vollständig, dann docs/TESTRUNDE-HOT-COLD.md, docs/TESTRUNDE-2.md, docs/TESTING.md §7, git diff 14ad9ed..HEAD. Ergebnisobjekte: Pakete ${JSON.stringify(Object.values(results))}; Doku ${JSON.stringify(doku)}; Bericht ${JSON.stringify(bericht)}. KEINE Code-Änderung. Schreibe docs/ABNAHME-FIXRUNDE.md; git add docs/ABNAHME-FIXRUNDE.md && git commit 'Abschlussprüfung der Fixrunde' mit 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>'. Nenne im Bericht, welches Modell tatsächlich lief. Ergebnis nach Anhang A.4.`,
    { ...FABLE, label: 'Abschlussprüfung (Fable 5.1)', phase: 'Abschluss', schema: FINAL_SCHEMA })
}

return { results: Object.values(results).map(r => ({ package: r.package, findings: r.findings.map(f => `${f.id}: ${f.status}`) })), doku, bericht, abschluss }
```

Hinweise zum Skript: `parallel` je Welle ist eine bewusste Barriere (das Gate braucht alle
Pakete). Die Skeptiker laufen als `pipeline`-Stufe je Spur, damit langsame Spuren die
schnellen nicht blockieren. `Date.now()`/`Math.random()` kommen nicht vor (Resume-fähig).
Fällt `claude-fable-5-1` mit Modellfehler aus (`agent()` liefert `null`), den letzten
Aufruf mit `model: 'fable'` wiederholen und im Bericht vermerken.
