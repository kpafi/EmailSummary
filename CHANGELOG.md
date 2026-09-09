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

### Geändert
- Alle nutzersichtbaren Texte sind englisch; Docstrings und `docs/` bleiben deutsch.
- Die von `maildigest test` zugestellte Nachricht ist als Selbsttest gekennzeichnet.

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
