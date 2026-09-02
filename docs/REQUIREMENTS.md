# MailDigest — Anforderungen

> Verbindliche Anforderungsliste. Jede Anforderung hat eine ID (F = funktional,
> NF = nicht-funktional, SEC = sicherheitsfunktional). Agenten referenzieren diese IDs in
> Commits, Tests und ADRs. Änderungen an diesem Dokument nur mit ADR in DECISIONS.md.
>
> Status: `open` → `in-progress (WPx)` → `done (WPx)`.

## 1. Funktionale Anforderungen

| ID | Anforderung | Quelle | Status |
|----|-------------|--------|--------|
| F-ING-1 | Das Tool liest E-Mails aus einem dedizierten Mirror-Postfach per IMAPS. Es schreibt/löscht dort nichts außer Gelesen-Flag und optionalem Verschieben in einen `Processed`-Ordner. | Nutzer | done (WP2) |
| F-ING-2 | Jede Mail wird genau einmal verarbeitet (Dedupe über Message-ID, persistenter State). | abgeleitet | done (WP2) |
| F-ING-3 | Einrichtung des Mirror-Postfachs erfolgt über ein CLI-Kommando (`connect-mail`) mit Verbindungstest und Anleitung zur Weiterleitungs-Einrichtung. | Nutzer | open |
| F-SUM-1 | Eine LLM-Instanz („Summarizer") erzeugt pro Mail eine strukturierte Zusammenfassung (Headline, Text, Kategorie) in konfigurierbarer Sprache und Länge. | Nutzer | done (WP5) |
| F-SUM-2 | Der Summarizer klassifiziert jede Mail als `high`/`normal`/`low` wichtig, mit Begründung. | Nutzer | done (WP5) — `importance` + `importance_reason` kommen aus dem Summarizer; die Zustell-Schwelle wertet `pipeline.process_mail` aus |
| F-SUM-3 | Der Nutzer kann das Verhalten per Custom-Instructions anpassen (was zusammenfassen, wie ausführlich, was ist wichtig). | Nutzer | done (WP5) — `[summarizer] instructions` als gelabelter semi-trusted Block (I8, ADR-031) |
| F-SUM-4 | Inhalte verarbeitbarer Anhänge (v0.1: nur PDF-Text) werden mitzusammengefasst; die Datei selbst wird nie zugestellt. | Nutzer | done (WP5) — Anhangs-Texte stehen im Datenblock, `attachment_summaries` ist auf tatsächlich extrahierte Anhänge beschränkt |
| F-SUM-5 | Mails unterhalb der konfigurierten Wichtigkeits-Schwelle werden nicht einzeln zugestellt, sondern in einem täglichen Sammel-Digest zusammengefasst. | abgeleitet | open |
| F-CRIT-1 | Eine zweite, unabhängige LLM-Instanz („Kritiker") bewertet Mail + Zusammenfassung auf Phishing/Scam-Risiko (`none`/`low`/`high`) und auf inhaltliche Korrektheit der Zusammenfassung. | Nutzer | open |
| F-CRIT-2 | Bei `high`-Risiko wird die Zustellung mit deutlichem Warn-Banner versehen und nie in den Low-Digest verschoben. | abgeleitet | open |
| F-CRIT-3 | Deterministische Signale (Reply-To≠From, Domain-Diskrepanzen, Punycode, Auth-Results, geblockte Anhänge) werden per Code berechnet und dem Kritiker als Fakten mitgegeben. | abgeleitet | open |
| F-MSG-1 | Zustellung an mindestens Telegram und Discord; Signal optional über signal-cli. Adapter-Architektur für weitere Messenger. | Nutzer | open |
| F-MSG-2 | Einrichtung des Messengers über CLI (`connect-messenger`) inkl. Testnachricht. | Nutzer | open |
| F-LLM-1 | LLM-Provider ist austauschbar: mindestens Anthropic-API und OpenAI-kompatible Endpoints (deckt lokale Modelle ab). Auswahl + Modellname per Config. | Nutzer | done (WP4) |
| F-LLM-2 | Einrichtung des Providers über CLI (`connect-llm`) inkl. Testaufruf. | Nutzer | open |
| F-OPS-1 | `maildigest run` läuft als Dauer-Prozess (Polling); `--once` verarbeitet einmalig und beendet sich (Cron-tauglich). | abgeleitet | in-progress (WP2) |
| F-OPS-2 | `maildigest test` führt einen Ende-zu-Ende-Selbsttest mit einer Beispielmail aus. | abgeleitet | open |
| F-OPS-3 | Nicht verarbeitbare Mails/Anhänge erzeugen eine Metadaten-Notiz an den Messenger (fail-closed), gehen also nie stumm verloren. | abgeleitet | in-progress (WP1) |

## 2. Sicherheitsanforderungen (testbar, Grundlage für Cold-Testing)

| ID | Anforderung | Status |
|----|-------------|--------|
| F-SEC-1 | Kein LLM erhält jemals rohes HTML, rohe MIME-Teile oder Anhangs-Binärdaten — ausschließlich sanitisierten Klartext (Invariante I1). | in-progress (WP3 + WP5: der Summarizer sieht ausschließlich `SanitizedMail`; „done" erst mit dem Kritiker in WP6) |
| F-SEC-2 | LLM-Aufrufe sind Text-in/Text-out ohne Tools/Function-Calling/Netzzugriff im Modellkontext (I2). | in-progress (WP4 + WP5: der Summarizer ruft nur `complete_json`; „done" erst mit WP6) |
| F-SEC-3 | Zugestellte Nachrichten enthalten niemals klickbare URLs, Markdown-/HTML-Links, Dateianhänge oder ausführbare Inhalte. URLs höchstens defanged/als Domain-Text (I3). | open |
| F-SEC-4 | Anhänge werden per Allowlist behandelt: nur `text/plain`, `text/html`, `application/pdf` werden inhaltlich verarbeitet; alles andere wird nur als Metadatum gemeldet. MIME-Typ wird per Magic-Bytes verifiziert. | done (WP3) |
| F-SEC-5 | Instruktionen im Mail-Inhalt („ignore previous instructions", versteckter Text, etc.) dürfen das Verhalten nicht ändern; Verdacht wird geflaggt und dem Nutzer angezeigt. | open |
| F-SEC-6 | LLM-Ausgaben werden schema-validiert und durchlaufen vor Versand einen deterministischen Output-Sanitizer (I4). | open |
| F-SEC-7 | Fehler in Sanitizer/LLM/Kritiker führen zu fail-closed-Verhalten: Metadaten-Notiz statt ungeprüftem Inhalt (I6). | in-progress (WP1) |
| F-SEC-8 | Secrets erscheinen nie in Prompts, Logs oder der Datenbank; Config-Datei wird mit Mode 0600 angelegt (I5). | open |
| F-SEC-9 | Anhangs-Text-Extraktion läuft in einem ressourcenbegrenzten Subprozess (Timeout, Speicher, Input-/Output-Größe) (I7). | done (WP3) |
| F-SEC-10 | Zero-Width-/Bidi-Steuerzeichen werden entfernt; Punycode-/Homoglyphen-Domains werden gekennzeichnet. | done (WP3) |

## 3. Nicht-funktionale Anforderungen

| ID | Anforderung | Status |
|----|-------------|--------|
| NF-1 | Lightweight: Python ≥ 3.11, Laufzeit-Dependencies ≤ 8 Pakete, SQLite als einziger Store, lauffähig auf einem kleinen VPS/Raspberry Pi. | open |
| NF-2 | Neue Laufzeit-Dependency nur mit ADR. | open |
| NF-3 | Konfiguration vollständig über eine `config.toml` + Env-Vars; keine Datenbank-Migrationstools. | in-progress (WP1) |
| NF-4 | Verarbeitungslatenz pro Mail < 60 s unter Normalbedingungen (exkl. LLM-Ausreißer). | open |
| NF-5 | Logs strukturiert, ohne Mail-Inhalte und ohne PII über Absender-Domain + gehashte Message-ID hinaus. | in-progress (WP2) |
| NF-6 | Testabdeckung: ≥ 90 % `sanitize/` und `output/`, ≥ 80 % gesamt (Stand WP10). | open |
| NF-7 | Doku-Pflicht: REQUIREMENTS/ARCHITECTURE/SECURITY/DECISIONS werden in jedem WP mitgepflegt; SPEC-CLI.md ist vollständiger CLI-Vertrag. | open |
| NF-8 | Zwei unabhängige Testdurchläufe: Hot (Whitebox) und Cold (Blackbox durch Agent ohne Code-Zugriff) gemäß TESTING.md. | open |

## 4. Explizit außerhalb des Scopes (v0.1)

- Kein Zugriff auf das echte Postfach (nur Mirror).
- Keine Antwort-/Aktions-Funktionen aus dem Messenger heraus.
- Kein OCR / keine Bildinhalts-Analyse (Known Limitation: Bild-Phishing wird nur als
  unverarbeiteter Anhang gemeldet).
- Keine Entschlüsselung von PGP/S-MIME.
- Kein Multi-User-/Multi-Postfach-Betrieb.
