# Changelog

Alle nennenswerten Änderungen an MailDigest. Format angelehnt an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/); die Versionsnummern folgen
[Semantic Versioning](https://semver.org/lang/de/).

## [Unveröffentlicht]

### Funktionen
- **Betrieb ohne Sprachmodell** als Standard (`[llm] provider = "none"`, ADR-076):
  MailDigest läuft ohne Anmeldung bei irgendeinem Anbieter und stellt einen beschrifteten
  Auszug samt aller deterministischen Warnungen zu. `connect-llm` bietet die Betriebsarten
  als Auswahlliste an, darunter drei Anbieter mit Gratis-Kontingent und die lokale Variante.
- **Fernauslösung per Telegram** (ADR-077, Vorgabe geändert durch ADR-078): `maildigest run`
  reagiert auf `/digest` (sofortiger Abruf) und `/status` (Kurzbericht) — **nur** auf diese
  beiden Wörter und **nur** aus dem konfigurierten Chat. Jeder andere Text wird verworfen und
  erreicht nie ein Sprachmodell. Schalter `[messenger.telegram] accept_commands`, seit
  ADR-078 ab Werk **an**; für Gruppen-Chats auf `false` setzen.
- **Anbieter-Wissensbasis** für die Einrichtung (ADR-075): `connect-mail` erklärt den Begriff
  IMAP-Host, übersetzt eine eingetippte Mailadresse in den Host und bricht bei Anbietern ohne
  Passwort-Anmeldung (Outlook.com, Proton) sofort mit Begründung ab.

### Behoben

Aus der Abschluss-Testrunde (38 Befunde, docs/TESTING.md §7; Bericht in
docs/TESTRUNDE-HOT-COLD.md):

- **Ohne Sprachmodell kam bei einem Betreff über 100 Zeichen keine Zusammenfassung mehr an,
  sondern nur die Metadaten-Notiz** — für eine alltägliche Mailklasse war der
  Auslieferungszustand damit funktionslos. Der Betreff wird jetzt mit `…` gekürzt (HC-1).
- **Ohne Sprachmodell verschwand gelesener Anhangstext spurlos**, und die Nachricht behauptete
  „Mail ohne darstellbaren Inhalt". Jeder Anhang, aus dem Text gelesen werden konnte, erscheint
  jetzt als eigene Zeile mit beschriftetem Auszug (HC-2).
- **Eine Mail konnte eine andere still unterdrücken**, indem sie deren `Message-ID` kopierte.
  Zwei inhaltlich verschiedene Mails mit demselben Header werden jetzt beide zugestellt; die
  zweite trägt den Hinweis „Message-ID collides with an earlier mail" (HC-10).
- **Beim Speichern der Konfiguration konnte die alte Datei abgeschnitten zurückbleiben**
  (volle Platte, Quota, Stromausfall). Sie wird jetzt atomar geschrieben und bleibt bei einem
  Abbruch byteidentisch erhalten (HC-20).
- **Ein Sprung der Systemuhr kostete eine wartende Nachricht ihre Zustellversuche** oder parkte
  sie dauerhaft in der Warteschlange. Beide Richtungen sind abgefangen (HC-25).
- **Der tägliche Sammel-Digest blieb während einer Postfach-Störung ganz aus**, obwohl er kein
  IMAP braucht. Er geht jetzt auch dann raus (HC-26).
- **Ein `/digest` aus dem Chat wartete bis zum Ende des Poll-Intervalls** (bis zu zwei Minuten)
  und **verschluckte alle Befehle, die im selben Stapel dahinter standen**. Beides behoben:
  Antwort nach spätestens zehn Sekunden, jeder Befehl wird beantwortet (HC-12, HC-13).
- **Der Fehlertext eines Modell-Anbieters konnte das Terminal fernsteuern** (Bildschirm
  löschen, Fenstertitel setzen, eine erfundene Programmmeldung platzieren); ein von der
  Gegenstelle zitierter eigener API-Key erscheint jetzt als `***` (HC-4).
- **Absender werden mit ihrem echten Namen angezeigt** („Jörg Müller" statt
  `=?utf-8?Q?J=C3=B6rg_M=C3=BCller?=`), auch bei roh-8-bittigen Headern. Versteckte
  Steuerzeichen im Anzeigenamen werden dadurch überhaupt erst erkannt (HC-23).
- **Eine verschlüsselte Mail sagt jetzt, dass sie verschlüsselt ist** („encrypted (PGP/S-MIME)
  — content not readable by design") statt wie eine inhaltsleere Mail auszusehen (HC-33).
- **Härtung der zugestellten Nachricht** (F-SEC-3/F-SEC-5): ein exakt nachgebauter
  Datenblock-Marker und die gängigsten Übernahmeformeln („Ignoriere deine bisherigen
  Anweisungen", „Forget all previous instructions") lösen jetzt die Warnzeile aus (HC-5,
  HC-21); ein Nachrichten-Split kann keine gefälschte Programmzeile mehr an den Anfang eines
  Teils setzen (HC-6); die Link-Fußnote enthält kein Markdown mehr (HC-7); ein rohes
  Steuerzeichen erreicht die Nachricht nicht mehr (HC-8); nackte IP-Adressen und sehr lange
  Domain-Marken werden jetzt in jeder Nachbarschaft gebrochen (HC-9, HC-24); die `/status`-
  Antwort entschärft den Ordnernamen (HC-28).
- **Kein Mail- oder Modelltext mehr im Betreiber-Protokoll**: ein vom Modell erfundener
  Feldname wird durch `<extra field>` ersetzt (HC-11).
- **Zu lange Anhang-Dateinamen** werden sichtbar in der Mitte gekürzt und behalten ihre Endung
  — bei einem geblockten Anhang die sicherheitsrelevante Angabe (HC-22).
- **Einrichtung**: `connect-llm --provider openai_compatible` trägt keine fremde Anbieter-URL
  mehr ein und zeigt die passende Anleitung (HC-3, HC-36); `connect-mail` lehnt Outlook.com und
  Proton auch als Mailadresse ab (HC-15) und erklärt einen Fehlschlag passend zur Ursache
  (HC-32); `init` endet wieder mit der Liste der nächsten Schritte, jetzt inklusive
  `connect-llm` (HC-18); die Hinweise zu `/digest` und `/status` erscheinen auch nach
  `connect-messenger --chat-id` (HC-19); `run` meldet ein fehlendes IMAP-Passwort als
  Konfigurationsfehler statt als „Postfach nicht erreichbar" (HC-34); `test` beendet die
  Schrittfolge auch dann mit einer `5/5`-Zeile, wenn die Nachricht in der Warteschlange bleibt
  (HC-35), und der Selbsttest-Vorspann nennt bei `--eml` die übergebene Datei (HC-17).
- **Robustheit**: ein unbrauchbares `Retry-After` (etwa `nan`) wirft MailDigest nicht mehr aus
  der Fehler-Taxonomie (HC-30); ein whitespace-freies Modellfeld lief quadratisch und braucht
  jetzt Millisekunden statt Sekunden (HC-29).

Aus der zweiten Testrunde (docs/TESTING.md §7 „Nachfixrunde NF-1"; Bericht in
docs/TESTRUNDE-2.md):

- **Eine einzige Mail konnte MailDigest minutenlang anhalten.** Ein HTML-Teil mit sehr tief
  verschachtelten Elementen liess die Umwandlung in Text quadratisch wachsen — 68 KB
  Angriffs-HTML kosteten fünf Sekunden, ein Megabyte rechnerisch Minuten, und in dieser Zeit
  wurden weder Mails abgeholt noch `/digest` oder `/status` beantwortet. Die Umwandlung ist
  jetzt linear (16 000 Ebenen: 28,6 s → unter 0,1 s) und hat zusätzlich eine harte Grenze:
  Ein HTML-Teil mit mehr als `[limits] max_html_elements` Elementen (Standard 50 000) oder
  mehr als 2000 Verschachtelungsebenen wird **nicht** umgewandelt. Die Mail geht trotzdem
  raus — mit dem Klartext-Teil, falls vorhanden, und dem Hinweis `HTML part too complex, not
  converted`, damit niemand eine unvollständige Zusammenfassung für eine vollständige hält
  (HC2-1, ADR-084).
- **Ein gefälschter Absender-Anzeigename konnte die angezeigte Absender-Domain bestimmen.**
  Wer seinen Namen kodiert als `Bank <info@bank.example>,` schickte, erschien in der
  Zustellzeile als `bank.example`, obwohl die Mail von `attacker@evil.example` kam — und die
  beiden Warnungen zu abweichender Antwortadresse und abweichendem Rückweg verstummten dabei.
  Absenderadresse und Domain werden jetzt aus dem unveränderten Kopfzeilenwert gelesen;
  dekodiert wird nur noch der Name (HC2-2). Der seit HC-23 lesbare Klarname bleibt erhalten.
- **Nachgezogen (zweite Iteration):** Die Grenze für HTML-Teile griff erst, nachdem der
  Parser die ganze Eingabe gelesen hatte — ein 24-MB-HTML-Teil hielt den Dienst 47 Sekunden
  an, obwohl er anschliessend verworfen wurde; und sie galt je Teil, sodass 34 unauffällige
  HTML-Teile zusammen 29 Sekunden kosteten, ohne dass irgendetwas angezeigt wurde. Neu ist
  ein Byte-Deckel **vor** dem Umwandeln (`[limits] max_html_bytes`, Standard 1 MB), der
  zusammen mit der Elementgrenze als Budget für die **ganze Mail** gilt; höchstens vier
  HTML-Teile je Mail werden überhaupt umgewandelt. Dieselben Angriffsmails kosten jetzt 0,1 s
  bzw. 0,7 s, und keine Mail kann die Umwandlung länger als rund zwei Sekunden beschäftigen.
  Echte Newsletter sind nicht betroffen (HC2-1, ADR-084).
- **Nachgezogen (zweite Iteration):** Ein kodierter Anzeigename konnte die Absender-Domain
  weiterhin fälschen, wenn er `@` und `,` unkodiert trug (`=?utf-8?Q?info@bank.example,?=`).
  Kodierte Wörter werden jetzt vor dem Lesen der Adresse durch einen neutralen Platzhalter
  ersetzt und erst danach als Name wieder eingesetzt — ein Name kann die Absender-Domain
  damit weder ersetzen noch löschen (HC2-2, ADR-020).
- **Nachgezogen (dritte Iteration):** Eine einzige Mail konnte den Abruf weiterhin minutenlang
  blockieren — nicht über das HTML, sondern über die Zahl der Links: Das Entfernen von Links
  wurde mit jeder weiteren Adresse überproportional teurer (eine Mail mit 41 000 Links kostete
  29 Sekunden, obwohl sie alle Grenzen einhielt), und für Klartext gab es überhaupt keine
  Grenze (21 MB Text = 4,5 Sekunden). Das Entfernen läuft jetzt in einem Durchgang, je Mail
  werden höchstens 2000 Links einzeln aufgeführt (weitere werden trotzdem entfernt und als
  `[Link removed]` angezeigt; die Nachricht sagt das mit „too many links, further links
  removed unlisted"), und roher Mail- und Anhangstext wird vor der Prüfung auf ein
  Vielfaches des Textbudgets vorgeschnitten. Dieselben Mails kosten jetzt 1,1 s bzw. 0,4 s;
  die teuerste überhaupt mögliche Mail liegt bei etwa zwei bis drei Sekunden (HC2-1,
  ADR-028/ADR-084).
- **Nachgezogen (dritte Iteration):** Die Absender-Fälschung über einen kodierten
  Anzeigenamen war mit einem einzigen fehlenden Zeichen wieder möglich (`=??Q?…?=` ohne
  Zeichensatz). Die Erkennung kodierter Wörter folgt jetzt genau der Form, die auch der
  Dekodierer der Standardbibliothek verwendet (HC2-2, ADR-020).

### Geändert
- Alle nutzersichtbaren Texte sind englisch; Docstrings und `docs/` bleiben deutsch.
  **Seit ADR-083 ist das eine Festlegung**: Programmoberfläche und Nachrichtenrahmen sind
  sprachunabhängig englisch, `[general] language` steuert nur noch die vom Sprachmodell
  erzeugten Textfelder. SPEC-CLI §2/§4/§6 ist der wörtliche Vertrag dieser Ausgabe und wird
  von `tests/unit/test_hc14_spec_literals.py` maschinell dagegen geprüft.
- Die von `maildigest test` zugestellte Nachricht ist als Selbsttest gekennzeichnet.
- **Schema-Version 3 der Zustandsdatenbank** (ADR-079). Eine bestehende Datei wird beim ersten
  Öffnen still und ohne Datenverlust gehoben — kein Eingriff nötig, kein Migrationswerkzeug.
  Es gibt keinen Rückweg: Eine gehobene Datei lässt sich mit einer älteren MailDigest-Version
  nicht mehr öffnen.
- **Die Konfigurationsdatei wird atomar geschrieben** (ADR-081). Das Konfigurationsverzeichnis
  muss dafür schreibbar sein; während des Schreibens liegt dort kurz eine Datei
  `.<name>.<pid>.tmp` mit Modus `0600`.
- **Befehle werden im Dauerbetrieb alle ≤ 10 s abgefragt** statt einmal je Poll-Zyklus
  (ADR-080). MailDigest ruft dadurch bis zu `poll_interval_seconds / 10` mal `getUpdates` je
  Zyklus auf.
- **Auch `maildigest run --once` (Cron) bedient den Befehlskanal**, einmal am Ende des Laufs:
  `/status` wird beantwortet, `/digest` ist dort wirkungslos und wird nur konsumiert (ADR-080).
- Fortsetzungen eines harten Zeilenschnitts beginnen sichtbar mit `… `; sie zählen zum
  Teil-Limit des Messengers (ADR-062/ADR-040, Nachträge).
- Neue Logereignisse: `mail_id_collision`, `outbox_clock_skew_corrected`, `low_digest_failed`,
  `command_ignored_once`, `command_handling_failed` (docs/BETRIEB.md §5).

### Bekannte Grenzen
- Die Aussage „keine Antworten aus dem Messenger heraus" aus 0.1.0 gilt eingeschränkt
  weiter: kein Dialog, keine Aktionen — außer der festen Befehlsliste oben.

## [0.1.0] — 2026-09-08

Erstes vollständiges Release. MailDigest liest ein Spiegel-Postfach, fasst jede Mail mit
einem Sprachmodell zusammen, lässt eine zweite Instanz auf Phishing prüfen und stellt
reinen Text an Telegram, Discord oder Signal zu.

**Diese Version ist noch nie gegen echte Gegenstellen gelaufen** — kein reales Postfach,
keine reale LLM-API, kein realer Messenger. Siehe „Bekannte Grenzen".

### Funktionen

- **Postfach.** IMAPS-Abruf ungelesener Mails aus einem dedizierten Mirror-Postfach,
  Dedupe über die Message-ID mit persistentem Zustand in SQLite. Geschrieben wird nur das
  Gelesen-Flag und — falls konfiguriert — ein server-seitiges `UID MOVE`.
- **Zusammenfassung.** Ein Summarizer-Modell erzeugt Kopfzeile, Text, Kategorie und eine
  Wichtigkeit (`high`/`normal`/`low`) in konfigurierbarer Sprache und Länge.
  Custom-Instructions steuern Stil, Fokus und Wichtigkeitsbegriff.
- **Kritiker.** Eine zweite, unabhängige Modell-Instanz mit eigenem Prompt und optional
  eigenem Provider bewertet Phishing-Risiko und die Korrektheit der Zusammenfassung. Sie
  bekommt die Custom-Instructions bewusst nicht zu sehen.
- **Anhänge.** Text aus `text/plain` und PDF wird mitzusammengefasst; alles andere
  erscheint als Zeile „Nicht verarbeitet" mit Name und Größe. Die Datei selbst wird nie
  zugestellt.
- **Zustellung.** Adapter für Telegram, Discord und Signal (`signal-cli`, Notiz an mich),
  persistente Zustell-Warteschlange mit Wiederholversuchen, Split langer Nachrichten an
  Zeilengrenzen auf das Limit des Zielsystems.
- **Sammel-Digest.** Mails unterhalb der Zustellschwelle kommen einmal täglich gesammelt,
  nach Kategorie gruppiert.
- **Betrieb.** `maildigest run` als Dauerprozess mit sauberem SIGINT/SIGTERM-Shutdown,
  `run --once` für Cron. Strukturierte JSON-Logzeilen, systemd-Unit in docs/BETRIEB.md.
- **Einrichtung.** `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test` —
  jedes Kommando mit Verbindungstest; `connect-mail` druckt die Anleitung für die
  Weiterleitung bei Gmail, posteo, mailbox.org. `maildigest test --dry-run` fährt eine
  `.eml`-Datei durch die echte Pipeline, ohne zu senden.

### Sicherheit

- **Rechte-Nullsummen-Prinzip.** Die beiden Modell-Stufen sind die einzigen, die fremden
  Text interpretieren, und die einzigen ohne jede Fähigkeit: keine Tools, kein
  Function-Calling, kein Netz- oder Dateizugriff. Der Request-Körper beider Provider
  besteht aus einem geschlossenen Feldsatz.
- **Sanitizer vor dem Modell.** Kein Modell sieht rohes HTML, rohe MIME-Teile oder
  Anhangs-Binärdaten. Zero-Width- und Bidi-Steuerzeichen werden entfernt, Punycode- und
  Homoglyphen-Domains gekennzeichnet, Links durch `[Link #n: domain]` ersetzt.
- **Sanitizer nach dem Modell.** Die zugestellte Nachricht enthält nie einen klickbaren
  Link, nie einen Anhang, nie Markup. Markdown wird neutralisiert — auch die nur am
  Zeilenanfang wirkenden Formen —, Domains und Dateinamen erscheinen mit gebrochenem Punkt,
  `@everyone`/`@here` entschärft. Telegram ohne `parse_mode`, Discord ohne Embeds.
- **Modellfreie Erkennung.** Gefälschte Datenblock-Marker, Ballungen unsichtbarer Zeichen
  und wörtliche Anweisungen an ein Sprachmodell setzen den Injection-Verdacht im Code,
  bevor ein Modell befragt wird. Mehrere unabhängige Fälschungssignale heben das
  Phishing-Risiko auch gegen ein schweigendes Modell auf `high`.
- **Fail-closed.** Jeder Fehler in Sanitizer, Modell, Kritiker, Zustellung oder Zustand
  führt zur fünfzeiligen Metadaten-Notiz statt zu ungeprüftem Inhalt. Nichts verschwindet
  still.
- **Anhangs-Extraktion im Subprozess** mit Zeit-, Speicher-, Input- und Output-Limit;
  `pdfminer` wird im Elternprozess nie geladen.
- **Kein Löschpfad auf dem Postfach.** Weder `\Deleted` noch `EXPUNGE` existieren im Code.
  Ohne MOVE-Capability des Servers bleibt die Mail liegen — auf Kopieren+Löschen wird
  bewusst nicht ausgewichen.
- **Secrets** als `SecretStr`, Config-Datei mit `0600` bei jedem Schreiben, keine
  Kommandozeilen-Optionen für Passwörter und Tokens, keine Secrets in Prompts, Logs oder
  Datenbank.
- **Invarianten-Review I1–I8** über die gesamte Codebasis, dokumentiert in
  docs/SECURITY.md §7 und maschinell festgehalten in `tests/unit/test_invarianten.py`.

### Qualitätssicherung

- 1200+ Tests, darunter Property-Based-Tests über zufällige Modellausgaben, Fehlerinjektion
  an jeder Pipeline-Stufe und ein Angriffskorpus.
- Ein Whitebox-Durchlauf (12 Befunde, docs/TESTING.md §5) und ein Blackbox-Durchlauf durch
  einen Agenten ohne Code-Zugriff (16 Befunde, §6). Alle Befunde ab `medium` sind behoben
  und mit Regressionstest belegt.
- Coverage: `sanitize/` und `output/` über 98 %, gesamt über 95 %.

### Bekannte Grenzen

- **Kein Lauf gegen echte Gegenstellen.** Alle Nachweise stammen aus Attrappen.
- **Die zweite Blackbox-Runde fehlt.** Das eigene Testprotokoll verlangt sie nach den
  beiden `high`-Befunden des ersten Durchlaufs; sie hat nicht stattgefunden.
- **Kein OCR, keine Bildanalyse** — Phishing im Screenshot wird nur als unverarbeiteter
  Anhang gemeldet.
- **Keine Entschlüsselung von PGP/S-MIME.**
- **Signal nur als „Notiz an mich"**, mit laufendem `signal-cli --daemon`.
- **Neue Warnheuristiken sind ungeeicht** (Phrasenliste der Injection-Erkennung,
  HTML-Divergenz-Schwelle, Kombinationsregel für `high`) — Fehlalarme sind wahrscheinlicher
  als übersehene Fälle.
- **`connect-mail` warnt nicht vorab**, wenn der Server kein `MOVE` kann oder
  `move_processed_to` nicht existiert; der Fehler fällt erst im Betrieb auf, ohne
  Datenverlust.
- **Verarbeitungslatenz nie gemessen** (NF-4 bleibt offen).
- Ein Postfach pro Installation, keine Antworten aus dem Messenger heraus, kein Zugriff auf
  das echte Postfach.

### Doku

- README mit Sicherheitsmodell-Diagramm, Quickstart, FAQ und einer ehrlichen Liste der
  Grenzen; CHANGELOG angelegt.
- In WP12 gegen den Code geprüft und korrigiert: das Nachrichtenbeispiel im README zeigte
  eine Hinweiszeile („1 Link entfernt"), die das Programm nie erzeugt; SPEC-CLI §2 sagt
  jetzt, wohin die Protokollzeilen der inneren Schichten gehen; docs/BETRIEB.md §5 kennt
  `imap_postprocess_failed` und beschreibt `mail_processed` korrekt.
