# MailDigest — Anforderungen

> Verbindliche Anforderungsliste. Jede Anforderung hat eine ID (F = funktional,
> NF = nicht-funktional, SEC = sicherheitsfunktional). Agenten referenzieren diese IDs in
> Commits, Tests und ADRs. Änderungen an diesem Dokument nur mit ADR in DECISIONS.md.
>
> Status: `open` → `in-progress (WPx)` → `done (WPx)`.

## 1. Funktionale Anforderungen

| ID | Anforderung | Quelle | Status |
|----|-------------|--------|--------|
| F-ING-1 | Das Tool liest E-Mails aus einem dedizierten Mirror-Postfach per IMAPS. Es schreibt/löscht dort nichts außer Gelesen-Flag und optionalem Verschieben in einen `Processed`-Ordner. | Nutzer | done (WP2, korrigiert WP11 — ADR-064: rohe UID-Kommandos, kein EXPUNGE; erstmals belegbar) |
| F-ING-2 | Jede Mail wird genau einmal verarbeitet (Dedupe über Message-ID, persistenter State). | abgeleitet | done (WP2) |
| F-ING-3 | Einrichtung des Mirror-Postfachs erfolgt über ein CLI-Kommando (`connect-mail`) mit Verbindungstest und Anleitung zur Weiterleitungs-Einrichtung. | Nutzer | done (WP9) — Abfrage, IMAPS-Verbindungstest, Ordnerwahl aus der Serverliste (`ImapClient.list_folders`), Weiterleitungs-Anleitung für Gmail/posteo/mailbox.org/allgemein; gespeichert wird erst nach bestandenem Test. Seit 2026-09-09 zusätzlich (ADR-075): Erklärung des Begriffs IMAP-Host mit Beispielen, Übersetzung einer eingetippten Mailadresse in den Host, anbieterspezifische Anleitung zur Passwortbeschaffung, sofortiger Abbruch bei Anbietern ohne Passwort-Anmeldung (Outlook.com, Proton) und anbieterspezifischer Hinweis nach fehlgeschlagener Anmeldung |
| F-SUM-1 | Eine LLM-Instanz („Summarizer") erzeugt pro Mail eine strukturierte Zusammenfassung (Headline, Text, Kategorie) in konfigurierbarer Sprache und Länge. | Nutzer | done (WP5) |
| F-SUM-2 | Der Summarizer klassifiziert jede Mail als `high`/`normal`/`low` wichtig, mit Begründung. | Nutzer | done (WP5) — `importance` + `importance_reason` kommen aus dem Summarizer; die Zustell-Schwelle wertet `pipeline.process_mail` aus |
| F-SUM-3 | Der Nutzer kann das Verhalten per Custom-Instructions anpassen (was zusammenfassen, wie ausführlich, was ist wichtig). | Nutzer | done (WP5) — `[summarizer] instructions` als gelabelter semi-trusted Block (I8, ADR-031) |
| F-SUM-4 | Inhalte verarbeitbarer Anhänge (v0.1: nur PDF-Text) werden mitzusammengefasst; die Datei selbst wird nie zugestellt. | Nutzer | done (WP5) — Anhangs-Texte stehen im Datenblock, `attachment_summaries` ist auf tatsächlich extrahierte Anhänge beschränkt |
| F-SUM-5 | Mails unterhalb der konfigurierten Wichtigkeits-Schwelle werden nicht einzeln zugestellt, sondern in einem täglichen Sammel-Digest zusammengefasst. | abgeleitet | done (WP8) — `low_digest_queue` + `DigestComposer.compose_low_digest`, Zustellung ab `[general] low_digest_time` (ADR-049); Kritiker-`high` bleibt davon ausgenommen (F-CRIT-2) |
| F-CRIT-1 | Eine zweite, unabhängige LLM-Instanz („Kritiker") bewertet Mail + Zusammenfassung auf Phishing/Scam-Risiko (`none`/`low`/`high`) und auf inhaltliche Korrektheit der Zusammenfassung. | Nutzer | done (WP6) — `agents.critic.CriticAgent` mit eigenem System-Prompt, eigenem Provider (`[llm.critic]`) und ohne Custom-Instructions (ADR-042) |
| F-CRIT-2 | Bei `high`-Risiko wird die Zustellung mit deutlichem Warn-Banner versehen und nie in den Low-Digest verschoben. | abgeleitet | done (WP6 + WP7) — Verdict aus WP6, Anhebung auf `importance = normal` in `pipeline.process_mail`, Banner in `output/composer.py`; Kettentest Sanitizer → Kritiker → Composer in `tests/unit/test_critic_corpus.py` |
| F-CRIT-3 | Deterministische Signale (Reply-To≠From, Domain-Diskrepanzen, Punycode, Auth-Results, geblockte Anhänge) werden per Code berechnet und dem Kritiker als Fakten mitgegeben. | abgeleitet | done (WP3 + WP6) — `agents.critic.collect_signals` aus dem `sanitization_report`, LLM-frei getestet; harte Signale heben die Risikostufe auch ohne Modell an (ADR-043); mehrere unabhängige Fälschungssignale mit mindestens einem harten heben auf `high` und lösen das Banner aus (WP11, ADR-063) |
| F-MSG-1 | Zustellung an mindestens Telegram und Discord; Signal optional über signal-cli. Adapter-Architektur für weitere Messenger. | Nutzer | done (WP7 + WP8 + WP9) — Adapter, Verdrahtung im Daemon über die Warteschlange (ADR-048) und Einrichtung per `connect-messenger` |
| F-MSG-2 | Einrichtung des Messengers über CLI (`connect-messenger`) inkl. Testnachricht. | Nutzer | done (WP9) — Telegram inkl. `getUpdates`-Chat-ID-Ermittlung, Discord-Webhook, Signal-Socket; Healthcheck + Testnachricht über `DigestComposer.compose_plain` (ADR-054) |
| F-LLM-1 | LLM-Provider ist austauschbar: mindestens Anthropic-API und OpenAI-kompatible Endpoints (deckt lokale Modelle ab). Auswahl + Modellname per Config. | Nutzer | done (WP4) Seit 2026-09-09 zusätzlich `provider = "none"` als Standard: Betrieb ganz ohne Sprachmodell (ADR-076), damit die Einrichtung ohne Anmeldung bei Dritten gelingt; `connect-llm` bietet die Betriebsarten als Auswahlliste an, inklusive dreier Anbieter mit Gratis-Kontingent und der lokalen Variante. |
| F-LLM-2 | Einrichtung des Providers über CLI (`connect-llm`) inkl. Testaufruf. | Nutzer | done (WP9) — Provider-/Modellwahl, Key aus Abfrage oder `MAILDIGEST_LLM_API_KEY`, Testaufruf mit `max_tokens = 16`; die Modellantwort wird nie angezeigt (ADR-055) |
| F-OPS-1 | `maildigest run` läuft als Dauer-Prozess (Polling); `--once` verarbeitet einmalig und beendet sich (Cron-tauglich). | abgeleitet | done (WP8 + WP9) — `run`/`run --once` über den WP8-Runner, JSON-Logs auf stdout, Bilanzzeile auf stderr, SIGINT/SIGTERM-Shutdown |
| F-OPS-2 | `maildigest test` führt einen Ende-zu-Ende-Selbsttest mit einer Beispielmail aus. | abgeleitet | done (WP9) — mitgelieferte `.eml` oder `--eml <pfad>`, echte Verdrahtung auf temporärer State-DB, `--dry-run` zeigt die Nachricht statt sie zu senden (ADR-057) |
| F-OPS-3 | Nicht verarbeitbare Mails/Anhänge erzeugen eine Metadaten-Notiz an den Messenger (fail-closed), gehen also nie stumm verloren. | abgeleitet | done (WP1 + WP7 + WP8) — Notiz in `output/composer.compose_failure`, Zustellung (auch der Notiz) über die persistente Warteschlange mit 5 Versuchen; Absturztest in `tests/integration/test_runner_e2e.py` |

## 2. Sicherheitsanforderungen (testbar, Grundlage für Cold-Testing)

| ID | Anforderung | Status |
|----|-------------|--------|
| F-SEC-1 | Kein LLM erhält jemals rohes HTML, rohe MIME-Teile oder Anhangs-Binärdaten — ausschließlich sanitisierten Klartext (Invariante I1). | done (WP3 + WP5 + WP6) — beide Agenten nehmen strukturell nur `SanitizedMail`/`Summary` entgegen |
| F-SEC-2 | LLM-Aufrufe sind Text-in/Text-out ohne Tools/Function-Calling/Netzzugriff im Modellkontext (I2). | done (WP4 + WP5 + WP6) — Summarizer und Kritiker rufen ausschließlich `complete_json` über `LLMProvider.complete` |
| F-SEC-3 | Zugestellte Nachrichten enthalten niemals klickbare URLs, Markdown-/HTML-Links, Dateianhänge oder ausführbare Inhalte. URLs höchstens defanged/als Domain-Text (I3). | done (WP7) — Feld-Scrub + Nachbrenner + Property-Tests; Telegram ohne `parse_mode`, Discord ohne Embeds; nachgeschärft WP11 um Zeilenanfangs-Markdown, Rand-Unterstriche und Massen-Pings (ADR-062) |
| F-SEC-4 | Anhänge werden per Allowlist behandelt: nur `text/plain` und `application/pdf` werden inhaltlich verarbeitet; alles andere — **einschließlich `text/html`** — wird nur als Metadatum gemeldet. MIME-Typ wird per Magic-Bytes verifiziert. | done (WP3; Wortlaut korrigiert WP11 — CT-16d: die Zeile nannte fälschlich `text/html`, im Widerspruch zu SECURITY §4, SPEC-CLI §7.3 und zum tatsächlichen Verhalten) |
| F-SEC-5 | Instruktionen im Mail-Inhalt („ignore previous instructions", versteckter Text, etc.) dürfen das Verhalten nicht ändern; Verdacht wird geflaggt und dem Nutzer angezeigt. | done (WP5 + WP7, nachgeschärft WP11) — Prompt-Härtung, deterministische Nachkontrolle der Modellausgabe **und** modellunabhängige Indizien aus der Mail (gefälschte Datenblock-Marker, Unsichtbarzeichen-Ballung, wörtliche Modell-Anweisungen) setzen `injection_suspected` (ADR-061); versteckter Text erscheint als eigener Hinweis. Cold-Nachweis in WP11 (CT-6) |
| F-SEC-6 | LLM-Ausgaben werden schema-validiert und durchlaufen vor Versand einen deterministischen Output-Sanitizer (I4). | done (WP5 + WP6 + WP7) — `Summary` und `CriticVerdict` werden schema-erzwungen, agentenseitig nachkontrolliert und im Composer erneut gescrubbt |
| F-SEC-7 | Fehler in Sanitizer/LLM/Kritiker führen zu fail-closed-Verhalten: Metadaten-Notiz statt ungeprüftem Inhalt (I6). | done (WP1 + WP8) — inkl. Stufen-Retries (3 LLM-Versuche auf **Stufen**-Ebene, ADR-050 — die drei multiplikativen Retry-Ebenen sind in ARCHITECTURE §6 aufgeschlüsselt, CT-16c) und State-Fehlern (`state_error`); Ende-zu-Ende belegt in `tests/integration/test_runner_e2e.py` |
| F-SEC-8 | Secrets erscheinen nie in Prompts, Logs oder der Datenbank; Config-Datei wird mit Mode 0600 angelegt (I5). | done (WP1 + WP4 + WP7 + WP9) — `SecretStr` in der Config, keine Secrets in Prompts/Logs/DB, `ConfigFile.save()` legt die Datei mit `0o600` an und setzt die Rechte bei jedem Schreiben neu; Secrets haben bewusst keine Kommandozeilen-Optionen (ADR-056). Blackbox-Nachweis in WP11 |
| F-SEC-9 | Anhangs-Text-Extraktion läuft in einem ressourcenbegrenzten Subprozess (Timeout, Speicher, Input-/Output-Größe) (I7). | done (WP3) |
| F-SEC-10 | Zero-Width-/Bidi-Steuerzeichen werden entfernt; Punycode-/Homoglyphen-Domains werden gekennzeichnet. | done (WP3) |

## 3. Nicht-funktionale Anforderungen

| ID | Anforderung | Status |
|----|-------------|--------|
| NF-1 | Lightweight: Python ≥ 3.11, Laufzeit-Dependencies ≤ 8 Pakete, SQLite als einziger Store, lauffähig auf einem kleinen VPS/Raspberry Pi. | done (WP12) — sechs Laufzeit-Pakete (`imap-tools`, `httpx`, `pydantic`, `beautifulsoup4`, `lxml`, `pdfminer.six`), SQLite aus der stdlib, `hypothesis` ist Dev-only. **Nicht** belegt: die Lauffähigkeit auf einem Raspberry Pi ist nie gemessen worden |
| NF-2 | Neue Laufzeit-Dependency nur mit ADR. | done (WP12) — die sechs Pakete stehen mit Begründung in PLAN §3 und in docs/DECISIONS.md; seit WP0 ist keines ohne ADR hinzugekommen |
| NF-3 | Konfiguration vollständig über eine `config.toml` + Env-Vars; keine Datenbank-Migrationstools. | done (WP1 + WP8 + WP9) — jedes Feld ist in docs/SPEC-CLI.md §5 mit Default dokumentiert und wird von einem Test gegen das Schema abgeglichen; Schema-Upgrade der DB additiv im Code (ADR-048) |
| NF-4 | Verarbeitungslatenz pro Mail < 60 s unter Normalbedingungen (exkl. LLM-Ausreißer). | **open** — nie gemessen. Ohne echte LLM-API gibt es keine belastbare Zahl; die eigenen Stufen (Sanitizer, Composer) liegen im Millisekundenbereich, die Latenz ist damit praktisch die Summe zweier Modell-Aufrufe. Bleibt offen bis zum ersten Produktivlauf |
| NF-5 | Logs strukturiert, ohne Mail-Inhalte und ohne PII über Absender-Domain + gehashte Message-ID hinaus. | done (WP2 + WP8) — JSON-Zeilen auf stdout, Level aus `[general] log_level`, nicht serialisierbare `extra`-Werte werden auf ihren Typnamen reduziert; Tracebacks nur bei DEBUG (ADR-046/047) |
| NF-6 | Testabdeckung: ≥ 90 % `sanitize/` und `output/`, ≥ 80 % gesamt (Stand WP10). | done (WP10, Stand WP12) — `sanitize/` 98 %, `output/` 99 %, gesamt 97 % bei 1278 Tests; Messwerte und Restlücken in docs/TESTING.md §5. Ergänzt um Property-Based-Tests (hypothesis, Dev-only, ADR-058), Fehlerinjektion an jeder Stufe und den Findings-Log HT-1…HT-12 |
| NF-7 | Doku-Pflicht: REQUIREMENTS/ARCHITECTURE/SECURITY/DECISIONS werden in jedem WP mitgepflegt; SPEC-CLI.md ist vollständiger CLI-Vertrag. | done (WP12) — SPEC-CLI.md wird von `tests/unit/test_spec_cli.py` maschinell gegen argparse und das Config-Schema abgeglichen; in WP12 wurde die gesamte Doku gegen den Ist-Stand geprüft und SECURITY §7 ausgefüllt |
| NF-8 | Zwei unabhängige Testdurchläufe: Hot (Whitebox) und Cold (Blackbox durch Agent ohne Code-Zugriff) gemäß TESTING.md. | **in-progress** — Hot abgeschlossen (WP10, §5), erster Cold-Durchlauf abgeschlossen (WP11, §6: 16 Befunde, alle ≥ medium gefixt und mit Regressionstest belegt). Die von TESTING §3 nach den `high`-Befunden CT-6/CT-9 verlangte **zweite** Cold-Runde steht aus — der einzige offene Punkt dieser Anforderung, benannt in README „Grenzen dieser Version" und SECURITY §7.2 |

## 4. Explizit außerhalb des Scopes (v0.1)

- Kein Zugriff auf das echte Postfach (nur Mirror).
- Kein Dialog mit dem Bot und keine Aktionen aus dem Messenger heraus. **Ausnahme seit
  2026-09-09 (ADR-077):** ein opt-in Befehlskanal mit der festen Wortliste `/digest`
  und `/status`, nur aus dem konfigurierten Chat. Freier Text wird verworfen und
  erreicht nie ein Sprachmodell; ab Werk ist der Kanal aus.
- Kein OCR / keine Bildinhalts-Analyse (Known Limitation: Bild-Phishing wird nur als
  unverarbeiteter Anhang gemeldet).
- Keine Entschlüsselung von PGP/S-MIME.
- Kein Multi-User-/Multi-Postfach-Betrieb.
