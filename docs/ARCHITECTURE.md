# MailDigest — Architektur

> Komponenten, Datenmodell und Verträge zwischen den Pipeline-Stufen. Wird in jedem WP
> mitgepflegt; Abweichungen zwischen Code und diesem Dokument sind Bugs (in einem von beiden).

## 1. Überblick

Ein einzelner Python-Prozess führt eine lineare Pipeline aus. Jede Stufe hat genau eine
Eingabe- und eine Ausgabe-Datenklasse; Stufen kennen einander nicht (Dependency Injection
in `pipeline.py`).

```
[1] Ingest        IMAP → RawMail                       (ingest/)
[2] Sanitize      RawMail → SanitizedMail              (sanitize/)   ← nur Code
[3] Summarize     SanitizedMail → Summary              (agents/summarizer, llm/)
[4] Critic        SanitizedMail + Summary → Verdict    (agents/critic, llm/)
[5] Compose+Guard Summary + Verdict → DigestMessage    (output/)     ← nur Code
[6] Deliver       DigestMessage → Messenger            (messenger/)
```

Fehler in Stufe 2–5 ⇒ `FailureNotice` (Metadaten-Notiz) statt Zusammenfassung; Fehler in
1/6 ⇒ Retry mit Backoff, State bleibt konsistent.

## 2. Komponenten

### Ingest (`ingest/imap_client.py`)

**Stand WP2 (ADR-016 bis ADR-020):**

- **Transport:** ausschließlich IMAPS über `imap_tools.MailBox` mit
  `ssl.create_default_context()` (Zertifikats- und Hostname-Prüfung aktiv). Kein Codepfad zu
  `MailBoxUnencrypted`/`MailBoxStartTls`, kein Schalter zum Abschalten der Prüfung. Port aus
  `[imap] port`; Port 143 wird mit einer verständlichen Fehlermeldung abgelehnt.
- **Abruf:** `fetch(AND(seen=False), mark_seen=False)` im Ordner `[imap] folder`. Das
  `mark_seen=False` ist sicherheitsrelevant: Das Gelesen-Flag ist die letzte, nicht die erste
  Aktion (s. u.).
- **Reihenfolge je Mail (F-ING-2, ADR-019):**
  1. `RawMail` bauen, 2. `StateDB.claim(dedupe_key, content_hash=…)` (`INSERT OR IGNORE`,
  committet **vor** der Verarbeitung), 3. Pipeline-Callback, 4. Endstatus schreiben,
  5. `UID STORE +FLAGS (\Seen)` setzen und ggf. `UID MOVE` nach `[imap] move_processed_to`.
  Ein bereits bekannter Key wird übersprungen, aber trotzdem als gelesen markiert/verschoben,
  damit er die Unseen-Menge verlässt. `claim()` ist dreiwertig (ADR-079): `claimed` ⇒
  verarbeiten, `duplicate` ⇒ überspringen, `collision` ⇒ unter dem abgeleiteten Schlüssel
  `sha256(message_id_hash + content_hash)` verarbeiten, `RawMail.id_collision = True` setzen
  und `mail_id_collision` (WARNING) loggen.
- **Löschen: nie** (F-ING-1, ADR-064). Die Nachbehandlung setzt **rohe UID-Kommandos** über
  `mailbox.client.uid(...)` ab, nicht die Komfort-Methoden von imap-tools: `MailBox.flag()`
  und `MailBox.delete()` hängen an jedes STORE ein unbedingtes `EXPUNGE`, und
  `MailBox.move()` weicht ohne MOVE-Capability auf `copy()` + `delete()` aus. `\Deleted` und
  `EXPUNGE` kommen im Modul in keinem Pfad vor. Verschoben wird ausschließlich server-seitig
  (`UID MOVE`, RFC 6851); kann der Server das nicht, bleibt die Mail als gelesen markiert
  liegen und der Vorgang meldet `MailboxPostProcessError`.
- **Nachbehandlungsfehler (ADR-065):** `MailboxPostProcessError` (Verbindung steht, Server
  antwortet `NO` — typisch: `move_processed_to` zeigt auf einen nicht existierenden Ordner)
  wird in `poll_once` je Mail abgefangen, als `imap_postprocess_failed` protokolliert und
  stoppt den Zyklus nicht. Nur `ImapConnectionError` löst Reconnect/Backoff aus.
- **Kopfzeilen-Deckel (ADR-020-Nachtrag, NF-1 vierte und fünfte Iteration):** Jeder
  Headerwert wird an **einer** Stelle (`_raw_header_values`) gelesen und dort vor jeder
  Verarbeitung auf 4096 Zeichen und 32 Werte je Name geschnitten; `to_addrs` trägt höchstens
  `MAX_RECIPIENTS` (200) Adressen. Vor der Rück-Serialisierung (`mime_bytes`) deckelt
  `_cap_message_headers` zusätzlich den ganzen geparsten Baum (256 KiB und 4096 Kopfzeilen
  je Mail; weitere Teile werden entfernt, Log `mail_headers_capped`), weil der Falter der
  Standardbibliothek sonst je Kopfzeile und Whitespace-Stück zahlt. Gewöhnliche Mails
  bleiben byteidentisch. Der Adress-Rückfall (R-9) liest nie Kommentar- oder
  Quoted-String-Inhalt (linearer Scanner nach RFC 5322); ein unbalancierter Header liefert
  „Adresse unbekannt".
- **Dedupe-Key:** `Message-ID`, sonst `sha256:` + Hash über From + Date + Subject +
  Body-Präfix (512 Zeichen), Felder `\x00`-getrennt. Das Präfix macht Fallback-Keys von
  echten Message-IDs unterscheidbar.
- **Zweites Dedupe-Merkmal (ADR-079):** `RawMail.content_hash` = `sha256(mime_bytes)`, in
  `seen_mails.content_hash` gespeichert. Der Key allein stammt aus einem frei wählbaren
  Header; erst das zweite Merkmal unterscheidet „dieselbe Mail nochmal" von „fremde Mail
  unter gleichem Namen". Zeilen ohne Wert (Datenbank vor Schema-Version 3) gelten als
  „Inhalt unbekannt" und lösen nie eine Kollision aus.
- **Anzeigenamen (ADR-020 Nachträge, HC-23/HC2-2):** `From` und `Reply-To` werden wie der
  Betreff RFC-2047-dekodiert, bevor sie in `RawMail` landen; roh-8-bittige Namen werden als
  UTF-8 (Auffang Latin-1) gelesen. Der Sanitizer sieht damit den Namen, nicht seine
  Kodierung. **Reihenfolge ist dabei sicherheitsrelevant:** Adresse und `from_domain`
  stammen aus dem mit `getaddresses` geparsten **Rohwert**, dekodiert wird ausschliesslich
  der Namensteil. Sonst schleust ein kodierter Anzeigename eine zweite Adresse in die
  Adressliste und bestimmt die angezeigte Absender-Domain (HC2-2). Der dekodierte Name wird
  von `<`, `>`, `,`, `;`, `:`, `"`, `\` befreit; `from_addr` wird als `Name <adresse>` neu
  zusammengesetzt, und zwar nur in einer Form, die `parseaddr` wieder in genau diese zwei
  Teile zerlegt.
- **Polling-Loop (`IngestService`):** `run_once()` für `run --once`; `run_forever()` mit
  `[imap] poll_interval_seconds` (Default 120 s) und Shutdown über ein `threading.Event`
  (`stop()`). Verbindungsfehler ⇒ Reconnect mit Exponential Backoff 5 s, 10 s, 20 s …
  gedeckelt bei 600 s (§6); nach einem erfolgreichen Poll wird der Backoff zurückgesetzt.
- **Fehler einer einzelnen Mail (I6):** Wirft der Verarbeitungs-Callback, bekommt die Mail
  Status `failed` mit `error_class = "ingest_error"`, wird als gelesen markiert und der Loop
  läuft weiter. Die Metadaten-Notiz erzeugt die Pipeline selbst (`FailedNotice`).
- **IMAP IDLE:** bewusst nicht implementiert (ADR-007, in ADR-016 bestätigt).
- **Logging (NF-5/I5):** nur gekürzter Dedupe-Hash, Absender-Domain, Status,
  Exception-Klassenname, Backoff-Dauer. **Stand WP8 (ADR-047):** Der Absturzpfad loggt
  `mail_processing_crashed` mit dem Exception-*Klassennamen*; der Traceback erscheint nur
  bei `log_level = "DEBUG"` (`logging_setup.traceback_enabled`).
- **Statushoheit (WP8, ADR-050):** `poll_once`/`IngestService` kennen das Flag
  `write_result_status` (Default `True`). Der Runner setzt es auf `False` und führt den
  Status selbst; der `failed`-Fall des Absturzpfades wird unabhängig davon immer
  geschrieben.
- **Größenlimit:** `limits.max_mail_bytes` hat im Ingest bewusst keinen Durchsetzungspunkt —
  die Größenpolitik liegt laut SECURITY §4 beim Sanitizer (dort seit WP3 umgesetzt, geprüft
  gegen `size_bytes` **und** `len(mime_bytes)`); der Ingest befüllt lediglich
  `RawMail.size_bytes` korrekt aus `RFC822.SIZE`.

### Sanitizer (`sanitize/`)

- Reiner Code, kein LLM, keine Netzwerkzugriffe.
- Module: `attachments.py` (Allowlist + Magic Bytes), `html_to_text.py`, `links.py`,
  `unicode_clean.py`, `extract_pdf.py` (Subprozess mit Limits), `sanitizer.py`
  (Orchestrierung).
- Politik und Limits: SECURITY.md §4 (dort verbindlich).

**Stand WP3 (ADR-026 bis ADR-030):**

- **Einstieg:** `sanitize.MailSanitizer` implementiert das `Sanitizer`-Protokoll aus
  `pipeline.py` (`sanitize(raw: RawMail) -> SanitizedMail`). Konstruktor:
  `MailSanitizer(limits: LimitsConfig | None, *, link_footnote: bool = False)`;
  Komfort: `MailSanitizer.from_config(config)` (nutzt `[limits]` und `links.footnote`).
- **Fehlerpfad:** `sanitize.SanitizeError` (Meldung ist ein konstantes Label wie
  `mail_zu_gross`/`mime_unparsbar`, nie Mail-Inhalt — I5). Der Klassenname ist in
  `pipeline._ERROR_CLASSES` auf `reason_class = "sanitize_error"` abgebildet (ADR-012).
  Eine Mail über `limits.max_mail_bytes` (geprüft gegen `size_bytes` **und**
  `len(mime_bytes)`) wirft vor jedem Parsing — der in WP2 offene Durchsetzungspunkt für
  die Größenpolitik liegt damit hier.
- **Ablauf je Mail:** Größencheck → `email.message_from_bytes` (compat32, robust gegen
  kaputte Header) → manueller MIME-Walk mit Tiefenlimit (`message/rfc822` wird nie
  betreten, T13) → Body-Aggregation (alle Inline-`text/plain`, sonst alle
  Inline-`text/html` via `html_to_text`, mit Element- und Tiefenschranke, s. u.) → pro Text: `unicode_clean.clean_text` →
  Marker-Neutralisierung (`neutralize_forged_markers`: ein nachgebauter
  `<<<MAILDIGEST-…-UNTRUSTED-…>>>`-Block wird zum Token
  `[forged data-block marker removed]` und in `forged_markers` gezählt — **vor** dem
  Tag-Strip, der ihn sonst spurlos löscht, HC-5) → Link-Scrub (`links.LinkCollector`, eine
  Instanz pro Mail: `#n` läuft über Body, Anhänge, Betreff, Anzeigename durch) → Tag-Strip
  auch für Klartext → Gesamt-Klartext-Budget (`[truncated]`).
- **Anhänge:** `attachments.detect_kind(declared_mime, data)` liefert
  `pdf|text|html|mismatch|unknown`; verarbeitet werden nur `text` (Datei) und `pdf`
  (Subprozess, `extract_pdf.extract_pdf_text(...) -> str | None`, `None` ⇒ unverarbeitet).
  `attachment_texts`-Schlüssel sind die sanitisierten (kollisionsfrei gemachten)
  Dateinamen und stimmen mit `AttachmentInfo.filename_sanitized` überein.
  `blocked_attachments` = Anzahl der Einträge mit `processed=False`.
- **Report:** vollständig befüllt inkl. `reply_to_mismatch` (Adressvergleich via
  `parseaddr`, fehlendes Reply-To ⇒ False), `return_path_mismatch` (nur bei zwei
  bekannten Domains), `auth_results` (Regex-Parse `spf|dkim|dmarc=wert`, erste Nennung
  gewinnt), Punycode-/Mixed-Script-Kennzeichnung auch für die Absender-Domain.
- **Schranken der HTML-Konvertierung (ADR-084, HC2-1):** `html_to_text` zählt direkt nach
  dem Parsen Elemente und Schachtelungstiefe und bricht bei mehr als
  `[limits] max_html_elements` (Default 50 000) bzw. `html_to_text.MAX_HTML_DEPTH` (2000)
  mit `HtmlTooComplexError` ab — **vor** Hidden-Heuristik und `get_text`. Der Sanitizer
  verwirft dann genau diesen Teil, setzt `html_rejected` im Report (Hinweiszeile `HTML part
  too complex, not converted`) und läuft fail-safe weiter; ein vorhandener
  `text/plain`-Teil wird normal zugestellt, rohes HTML verlässt die Stufe nie (I1).
  Derselbe Deckel gilt im Divergenzcheck `_html_diverges` (ADR-067) — er ist der
  praktisch wichtigere Einstieg, weil er auch bei vorhandenem Klartext-Teil läuft.
  Davor liegt (ADR-084-Nachtrag, NF-1 zweite Iteration) ein reiner **Byte-Deckel**
  `[limits] max_html_bytes` (Default 1 MB): Element- und Tiefenschranke sehen den Baum
  erst, wenn der Parser ihn gebaut hat — die Bytelänge wird geprüft, bevor `clean_text`
  oder BeautifulSoup den Teil überhaupt anfassen. Byte- und Elementschranke laufen als
  **Restbudget einer ganzen Mail** (`HtmlBudget`, ein Objekt je `sanitize()`-Lauf, geteilt
  von Body-Pfad und Divergenzcheck), und höchstens `MAX_HTML_PARTS` (4) `text/html`-Teile
  werden überhaupt konvertiert; weitere gelten als nicht konvertiert (`html_rejected`).
- **Schranken des Klartext- und Link-Pfads (ADR-084-/ADR-028-Nachtrag, NF-1 dritte und
  vierte Iteration):** Roher Body- und Anhangstext läuft gegen ein **Restbudget der ganzen
  Mail** von `_RAW_TEXT_FACTOR` (16) mal `[limits] max_text_chars` Zeichen — ein Objekt je
  `sanitize()`-Lauf, geteilt von Body und allen Anhangstexten, Vorbild `HtmlBudget`. Das
  Budget wird abgebucht, **bevor** `clean_text`, die Marker-Neutralisierung und der
  Link-Scrub laufen (und vor dem Divergenzcheck); es setzt `truncated`, und ein Anhangstext,
  für den nichts mehr übrig ist, läuft gar nicht mehr durch die teuren Pässe und gilt als
  nicht verarbeitet. Daneben steht `sanitizer.MAX_MIME_PARTS` (500): Teile jenseits davon
  werden nicht betreten, sondern als ein Metadatum `(mime-teile ueberschritten)` gezählt —
  sonst multipliziert die blosse Teilezahl jede andere Schranke. Der Link-Scrub
  ist linear in der Zahl der Funde (eine einzige Rück-Ersetzung über das Platzhalter-Muster)
  und hat mit `links.MAX_LINKS_PER_MAIL` (2000) ein eigenes Budget je Mail: Weitere Funde
  werden entfernt (I3 gilt ausnahmslos), erscheinen aber nur noch als `[Link removed]` und
  setzen `links_capped` im Report (Hinweiszeile
  `too many links, further links removed unlisted`). Was keine dieser Schranken erreicht,
  ist `email.message_from_bytes` selbst: Der zeilenweise Parser der Standardbibliothek läuft
  **vor** jedem Budget, und eine 25-MB-Mail aus Dreibyte-Zeilen kostet dort allein rund 2 s
  (ADR-084-Nachtrag, fünfte Iteration).
- **Nicht Aufgabe des Sanitizers:** `RawMail.date`/`from_domain` werden unverändert
  übernommen (Vertrauensmodell aus ADR-020); die Nachrichten-Formatierung der
  Anhang-Hinweise („⚠ 2 nicht verarbeitete Anhänge …") ist WP7 (`output/`), auf Basis
  der `AttachmentInfo`-Liste.

### LLM-Schicht (`llm/`)

**Stand WP4 (ADR-021 bis ADR-025):**

- `base.py`: Protokoll
  `LLMProvider.complete(system, user, *, max_tokens: int, temperature: float | None = None) -> str`.
  Kein Tool-Use im Interface (I2). `temperature=None` (Default) bedeutet: Feld wird nicht
  gesendet — aktuelle Modelle lehnen es mit HTTP 400 ab (ADR-022). Fehlerklassen: `LLMError`
  (Basis), `LLMTimeout`, `LLMRateLimited`, `LLMInvalidResponse`, `LLMTransportError`
  (HTTP-/Verbindungsfehler, die weder Timeout noch 429 sind). Konstanten
  `DEFAULT_TIMEOUT_SECONDS = 60.0`, `MAX_ATTEMPTS = 3`.
- `_http.py`: gemeinsame HTTP-Mechanik beider Provider (POST, Retry, Fehler-Mapping) —
  garantiert identische Semantik. Wiederholt nur 429 und 5xx, max. 3 Versuche, Backoff
  1 s/2 s (Deckel 30 s) bzw. `Retry-After` in Sekunden. Timeouts werden nicht wiederholt.
  Fehlermeldungen enthalten Statuscode und `error.type`, nie den Antwortkörper (I5).
- `anthropic.py`: `POST {base_url}/v1/messages`, Header `x-api-key` +
  `anthropic-version: 2023-06-01`, Default-Host `https://api.anthropic.com`. Modell aus der
  Config (kein Code-Default). Antwort = Verkettung aller `text`-Blöcke; ohne Textblock
  `LLMInvalidResponse`.
- `openai.py`: `POST {base_url}/chat/completions` (Default `https://api.openai.com/v1`),
  Bearer-Auth nur wenn ein Key vorliegt — lokale Server (Ollama/vLLM) brauchen keinen.
  System-Prompt als `role="system"`, Datenblock als `role="user"` (I8).
- `schema.py`: `complete_json(provider, system, user, schema, *, max_tokens, temperature)`.
  Extrahiert JSON robust aus Markdown-Codefences und Begleittext (klammerbalancierter Scan,
  string-/escape-fest), validiert gegen pydantic, genau ein Reparatur-Retry, danach
  `LLMInvalidResponse` (I6). Der Reparaturhinweis steht im System-Prompt und enthält
  Feldpfade, Fehlertypen und das JSON-Schema — nie die verworfene Modellantwort (ADR-024).
- `factory.py`: `build_provider(config, role)` mit `role = "summarizer" | "critic"`
  (Override-Vererbung aus `[llm.critic]`), dazu `max_tokens_for(config, role)`. Fehlender
  API-Key bei Provider `anthropic` ⇒ `ConfigError` mit Hinweis auf `MAILDIGEST_LLM_API_KEY`.
- `prompts.py`: alle Prompt-Texte zentral, versioniert über `PROMPT_VERSION` (Schema
  `wp<NR>/<YYYY-MM-DD>[.n]`; jede inhaltliche Änderung erhöht sie). Stand WP5: die
  Summarizer-Bausteine `summarizer_system_prompt(*, token, language, summary_length,
  custom_instructions)` und `summarizer_user_prompt(mail, *, token)`, dazu
  `default_token_source()` / `block_markers(token)` für die pro Aufruf zufälligen
  Datenblock-Marker (ADR-032) und `format_size()` für deutsche Größenangaben. Stand WP6:
  zusätzlich `critic_system_prompt(*, token, language, max_reasons)`,
  `critic_user_prompt(mail, summary, signals, *, token)` und `summary_markers(token)` für
  den zweiten Untrusted-Block des Kritikers (ADR-041). `PROMPT_VERSION` steht auf
  `wp6/2026-09-02`.

### Betrieb ohne Sprachmodell (`agents/offline.py`)

**Stand 2026-09-09 (ADR-076).** `[llm] provider = "none"` ist der Standard nach
`maildigest init`. `build_runner` und `cli.Hooks` setzen dann statt `SummarizerAgent`/
`CriticAgent` die Stufen `OfflineSummarizer`/`OfflineCritic` ein:

- **OfflineSummarizer** baut die `Summary` allein aus `SanitizedMail`: Betreff als
  Headline — **vor** der Konstruktion mit `summarizer.clamp_headline` auf 100 Zeichen
  gekürzt, weil `Summary.headline` diese Grenze als `max_length` trägt und die
  Nachkontrolle zu spät käme (HC-1) —, ein auf 400 Zeichen gekürzter Auszug des bereits
  sanitisierten Textes mit fester Beschriftung (`Excerpt, not a summary …`),
  `importance = "normal"` (ein geratenes `low` würde Mails stillschweigend in den
  Sammel-Digest schieben). Danach läuft dieselbe `enforce_output_policy` wie bei einer
  Modellausgabe — inklusive `detect_injection_evidence` (F-SEC-5/CT-6).
- **Anhänge ohne Modell:** Je Eintrag in `attachment_texts` trägt der OfflineSummarizer
  einen beschrifteten, auf 400 Zeichen (Composer-Limit je Wert) gekürzten Auszug in
  `attachment_summaries` ein; der Composer rendert daraus `— <datei>: Excerpt: …`. Ohne
  diesen Schritt fiel ein Anhang, dessen Text erfolgreich extrahiert wurde, spurlos aus der
  Nachricht — `📎 Not processed` nennt nur die *geblockten* (HC-2).
- **OfflineCritic** startet bei `phishing_risk = "none"` und überlässt die Anhebung
  vollständig `enforce_verdict_policy(collect_signals(mail))` — denselben Code-Signalen wie
  im Modellbetrieb (ADR-043/ADR-063). `summary_accurate` ist immer wahr: Der Text stammt
  aus dem Sanitizer, es gibt nichts zu halluzinieren.
- **Retries:** Nur die selbst gebauten Offline-Stufen laufen ohne `RetryingSummarizer`/
  `RetryingCritic` — es gibt keinen Netzaufruf. Eine von außen injizierte Stufe (Tests)
  behält ihre Wiederholungen.
- **Sicherheitslage:** strenger als der Modellbetrieb, nicht lockerer — es existiert keine
  untrusted Modellausgabe. I2 ist trivial erfüllt (kein Modellaufruf), I1/I3/I4/I6
  unverändert.

### Agenten (`agents/`)
- `summarizer.py`: baut Prompt (System + gelabelte Custom-Instructions + delimitierter
  Datenblock), ruft `complete_json`, führt deterministische Nachkontrolle aus.
- `critic.py`: berechnet erst deterministische Signale (Code!), ruft dann das LLM mit
  Mail-Klartext + Summary + Signalen, liefert `CriticVerdict`. Details unter „Stand WP6".

**Stand WP5 (ADR-031 bis ADR-034):**

- **Einstieg:** `agents.summarizer.SummarizerAgent` implementiert das `Summarizer`-Protokoll
  aus `pipeline.py` (`summarize(mail) -> Summary`). Konstruktor:
  `SummarizerAgent(provider, *, language, summary_length, instructions, max_tokens,
  token_source)`; Komfort: `SummarizerAgent.from_config(config, provider=None)` (nutzt
  `[general] language/summary_length`, `[summarizer] instructions`,
  `llm.factory.build_provider(config, "summarizer")` und `max_tokens_for(config,
  "summarizer")`). Ein `[llm.summarizer]`-Abschnitt existiert nicht (ADR-025): Rolle
  `summarizer` = `[llm]`.
- **Prompt-Aufbau (I8, SECURITY §5):** System-Prompt = Rolle → Nutzer-Vorgaben (eigener,
  gelabelter, auf 2000 Zeichen gedeckelter Block) → unüberschreibbare Sicherheitsregeln +
  Sprache/Länge/Wichtigkeit/Ausgabeformat. User-Message = vertrauenswürdige Programm-Fakten
  aus dem `sanitization_report`, dann der Mail-Inhalt (Betreff, Anzeigename, Domain, Datum,
  geblockte Anhänge als Metadaten, `body_text`, `attachment_texts`) zwischen pro Aufruf
  zufälligen Markern mit Untrusted-Hinweis, dann eine Format-Erinnerung.
- **Nachkontrolle (`enforce_output_policy`, reiner Code, I4):** Scan jedes Textfelds auf
  Markdown-Links, HTML-Tags, numerische Entities, URL-Muster (`schema://`, `hxxp`, `www.`,
  `mailto:`, `tel:`, `(.)`/`[.]`/`(dot)`, `domain.tld/pfad`) und Unicode-`C*`-Zeichen.
  Fund ⇒ Ersetzung durch `[entfernt]` (URL-Muster wortweise) **und**
  `injection_suspected = true`; ein vom Modell gesetztes Flag bleibt gesetzt. Danach:
  Headline einzeilig und ≤ 100 Zeichen, leere Felder aus Sanitizer-Werten aufgefüllt
  (`headline` ← Betreff, `category` ← `sonstiges`, `summary_text` ← Metadaten-Ersatztext),
  `attachment_summaries` auf Schlüssel aus `attachment_texts` beschränkt. Nackte Domains
  ohne Pfad bleiben stehen — die Marker `[Link #n: domain.tld]` sind nach I3 erlaubt.
  Diese Schicht normalisiert **nicht** nach NFKC; Fullwidth-Formen fängt erst der
  Output-Sanitizer (ADR-033 „Konsequenzen", ADR-036).
- **Mail ohne darstellbaren Text:** Der LLM-Aufruf findet trotzdem statt
  (Betreff/Absender/Anhangsnamen sind Signale); bleibt `summary_text` leer, greift der
  deterministische Ersatztext `describe_without_body` („Mail without displayable content,
  N blocked attachments: …"). Wurde aus Anhängen Text gelesen, behauptet er keinen fehlenden
  Inhalt, sondern nennt ihn („No mail body; N attachments with readable text", HC-2).
- **Nicht Aufgabe des Summarizers:** die Zustell-Schwelle `deliver_min_importance` (wertet
  `pipeline.process_mail` aus, inkl. F-CRIT-2-Anhebung) und die Nachrichten-Formatierung
  (WP7).

**Stand WP6 (ADR-041 bis ADR-044):**

- **Einstieg:** `agents.critic.CriticAgent` implementiert das `Critic`-Protokoll aus
  `pipeline.py` (`review(mail, summary) -> CriticVerdict`). Konstruktor:
  `CriticAgent(provider, *, language, max_tokens, token_source)`; Komfort:
  `CriticAgent.from_config(config, provider=None)` (nutzt `[general] language`,
  `llm.factory.build_provider(config, "critic")` und `max_tokens_for(config, "critic")`,
  also die Overrides aus `[llm.critic]`). Es gibt **keinen** `instructions`-Parameter:
  Der Kritiker ist die unabhängige zweite Instanz (ADR-042).
- **Deterministische Signale (`collect_signals`, reiner Code, F-CRIT-3):** Aus
  `SanitizationReport` + `AttachmentInfo` entsteht eine geordnete Folge von `Signal(key,
  text, hard)` mit den Schlüsseln `reply_to_mismatch`, `return_path_mismatch`,
  `auth_ok` / `auth_failed` / `auth_missing` (Werte-Rendering `DKIM=…, DMARC=…, SPF=…`;
  „bestanden" = `pass|none|neutral|policy`, identisch zur Hinweiszeile in `output/`),
  `punycode`, `mixed_script`, `blocked_attachments` (mit deklarierten MIME-Typen),
  `links_removed`, `hidden_text`, `control_chars`, `truncated`. Die Folge ist nie leer
  (die Auth-Zeile erscheint immer) und enthält keinen Mail-Text — sie ist vom Absender
  nicht manipulierbar (T9).
- **Prompt-Aufbau (I8, SECURITY §5, ADR-041):** System-Prompt = Rolle → unüberschreibbare
  Sicherheitsregeln (nennen beide Markerpaare) → Prüfauftrag 1 (Phishing-Muster:
  Dringlichkeit, Zahlungsaufforderung, Credential-Anfrage, Absender-Diskrepanzen,
  untypische Sprache, Manipulation der Verarbeitung) → Prüfauftrag 2 (Halluzinations-Check
  inkl. Warnung vor der Fail-closed-Wirkung von `summary_accurate = false`) → Risikostufen
  → Sprache/Form → Ausgabeformat. User-Message = Signale als Programm-Fakten, dann der
  Mail-Block (`<<<MAILDIGEST-UNTRUSTED-DATA …>>>`, identisch gebaut wie beim Summarizer),
  dann der Summary-Block (`<<<MAILDIGEST-UNTRUSTED-SUMMARY …>>>`), beide mit derselben
  Zufallskennung und beide marker-neutralisiert.
- **Nachkontrolle (`enforce_verdict_policy`, reiner Code, I4, ADR-043/044):** Jedes
  Textfeld wird NFKC-normalisiert und mit derselben Politik wie beim Summarizer gescrubbt
  (`agents.summarizer.scrub_text`); danach einzeilig, `risk_reasons` ≤ 5 Einträge à
  200 Zeichen ohne Duplikate/Leereinträge, `notes` ≤ 500 Zeichen. Ein Fund macht sich als
  eigener Grund sichtbar und hebt `phishing_risk` auf mindestens `low`; dasselbe tun harte
  Signale (`hard=True`, derzeit nur `mixed_script`). Code-Gründe stehen vor den
  Modellgründen, damit die Kappung sie nicht verdrängt. Die Stufe wird nie gesenkt und nie per
  Code auf `high` gesetzt; `summary_accurate` bleibt unangetastet.
- **Nicht Aufgabe des Kritikers:** die Wirkung des Verdicts. Warn-Banner und
  Mindest-Wichtigkeit bei `high` (F-CRIT-2) sowie der Fail-closed-Pfad bei
  `summary_accurate = false` (T8) liegen in `pipeline.process_mail` und `output/composer.py`.

### Output (`output/`)
- Baut aus Summary + Verdict die `DigestMessage` (Format §7) und sanitisiert jedes Feld
  (URL-/Markdown-/HTML-Strip, Escaping, Längen-Split je Messenger).

**Stand WP7 (ADR-035 bis ADR-040):** Zwei Module. `output/sanitizer.py` liefert
`scrub_field` (Entity-Auflösung → `unicode_clean.clean_text` → optionale Feldkürzung →
Tag-Strip → Segmentierung: WP3-Marker und bereits defangte Formen unverändert, alles andere
Markup-Neutralisierung + `links.LinkCollector.scrub`), `scrub_plain` (dasselbe ohne
Link-Erkennung, für Domain/Anzeigename/Dateiname), `final_guard` (Nachbrenner über die
fertige Nachricht: jedes lebende Schema mit `://` sowie `javascript:`/`data:`-artige
Schemata brechen, `www.` brechen, `<`/`>` entfernen, `](` auftrennen, Domains/IPv4 defangen)
und `split_parts`. `output/composer.py` enthält `DigestComposer` (implementiert
`OutputComposer` aus `pipeline.py` mit `compose` **und** `compose_failure`; `from_config`
wählt das Messenger-Limit); `parts` entsteht ausschließlich über `_finalize()` =
`final_guard` + `split_parts`. Ein `LinkCollector` pro Nachricht ⇒ durchlaufende
Marker-Nummerierung über alle Felder.

### Messenger (`messenger/`)
- Protokoll `Messenger.send(DigestMessage)`, `healthcheck()`.
- `telegram.py` (Bot-API, reiner Text ohne parse_mode), `discord.py` (Webhook),
  `signal.py` (signal-cli JSON-RPC, Feature-Flag).

**Stand WP7:** `base.py` = Adapter-Protokoll `send(DigestMessage)` + `healthcheck() -> bool`
plus `MessengerError` (in `pipeline._ERROR_CLASSES` als `delivery_error` geführt) und die
Konstanten `DEFAULT_TIMEOUT_SECONDS = 30.0`, `MAX_ATTEMPTS = 3`. Das Protokoll ist bewusst
breiter als das gleichnamige in `pipeline.py` (die CLI braucht den Healthcheck).
`_http.py` = eigene Retry-Mechanik mit der Politik der LLM-Schicht (nur 429/5xx, max. 3
Versuche, Backoff 1 s/2 s bzw. `Retry-After`, Timeouts nicht wiederholt), Fehlermeldungen
ohne URL/Header/Body (I5 — Telegram trägt das Token im Pfad). `telegram.py`:
`POST {base_url}/bot<token>/sendMessage`, ein Request je Teil, **kein `parse_mode`**
(ADR-006) und `disable_web_page_preview: true`; `ok: false` gilt trotz HTTP 200 als Fehler;
Healthcheck über `getMe`. `discord.py`: Webhook-POST mit ausschließlich `content` und
`allowed_mentions: {"parse": []}`, keine Embeds; Healthcheck über GET auf die Webhook-URL.
`signal.py`: JSON-RPC (`send` mit `noteToSelf`, `version`) über Unix-Socket, hinter
`[messenger.signal] enabled`. `factory.py`: `build_messenger(config)` wählt nach
`[messenger] active` und wirft `ConfigError` bei fehlendem Token/Chat-ID/Webhook bzw. nicht
freigeschaltetem Signal.

### State (`state/db.py`)
- SQLite, Tabellen:
  - `seen_mails(message_id_hash TEXT PK, first_seen_at, status, error_class, retry_count,
    content_hash)`
  - `low_digest_queue(id, message_id_hash, received_at, headline, category, from_domain)`
  - `outbox(id, message_id_hash, kind, payload, attempts, first_queued_at, next_attempt_at,
    last_error)`
  - `meta(key, value)` — z. B. Schema-Version, letzter Digest-Zeitpunkt.
- Statuswerte: `pending → sanitized → summarized → checked → delivered | failed | skipped_low`.
- Kein Mail-Volltext in der DB (SECURITY.md §6).

**Stand WP2 (ADR-018):** Angelegt sind `seen_mails` und `meta`; `low_digest_queue` entsteht
erst in WP8. `message_id_hash` ist `sha256(dedupe_key)` als Hex — der Dedupe-Key selbst wird
nie gespeichert und nie ungekürzt geloggt (NF-5). `error_class` wird beim Schreiben auf
`[a-z0-9_]`, max. 64 Zeichen normalisiert (strukturelle I5-Schranke; sie vereinheitlicht
Zeichenvorrat und Länge und ersetzt nicht die Pflicht der Aufrufstellen, konstante Labels zu
übergeben). Die Schema-Version steht in `meta.schema_version` und wird beim Öffnen geprüft
(NF-3, keine Migrationstools); die DB-Datei wird mit Modus `0600` angelegt.
Idempotenz-Primitiv ist `StateDB.claim()` (`INSERT OR IGNORE` auf den Primärschlüssel),
nicht `was_seen()`.

**Stand WP8 (ADR-045, ADR-048, ADR-049):** Schema-Version 2. Neu sind `low_digest_queue`
(F-SUM-5) und `outbox` (Zustell-Warteschlange). Der Schritt 1 → 2 ist rein additiv und wird
beim Öffnen einer WP2-Datei still vollzogen (nur neue Tabellen, keine Migrationstools,
NF-3); jede andere Versionsabweichung bleibt ein `StateError`. Der Dateipfad kommt aus
`[general] state_db` bzw. `config.resolve_state_db_path()` (ADR-045). Beide neuen Tabellen
enthalten **nur bereits output-sanitisierten** Text (Kopfzeile/Kategorie/Domain bzw. die
fertigen Nachrichtenteile), nie Mail-Rohtext, und werden nach Zustellung geleert
(docs/SECURITY.md §6). Zustell-Ergebnisse dürfen ausschließlich Datensätze im Status
`checked` bewegen (`promote_checked_to_delivered`).

**Stand Fixrunde (ADR-079):** Schema-Version 3. Neu ist die nullbare Spalte
`seen_mails.content_hash` (`sha256(mime_bytes)` der Mail, HC-10). Der Schritt 2 → 3 ist
wieder rein additiv und wird beim Öffnen still vollzogen — neue Tabellen legt
`CREATE TABLE IF NOT EXISTS` an, neue Spalten `ALTER TABLE … ADD COLUMN`
(`StateDB._add_missing_columns`); bestehende Zeilen bleiben unangetastet und bekommen
`NULL` = „Inhalt unbekannt". `claim()` liefert seither `ClaimResult`
(`claimed`/`duplicate`/`collision`) statt `bool`.

### Orchestrierung & Betrieb (`runner.py`, `delivery.py`, `logging_setup.py`)

**Stand WP8 (ADR-045 bis ADR-051):**

- `runner.build_runner(config, *, config_path, …)` verdrahtet alle Stufen:
  `MailSanitizer.from_config` → `RetryingSummarizer(SummarizerAgent.from_config)` →
  `RetryingCritic(CriticAgent.from_config)` → `DigestComposer.from_config` →
  `OutboxMessenger(StateDB, messenger.factory.build_messenger)`, dazu
  `StatusRecorder` als `ProgressSink` und `deliver_min_importance` aus `[general]`.
  Jede Stufe ist per Argument ersetzbar (Tests, `maildigest test` in WP9).
- `Runner.run_once()`: Warteschlange leeren → einen IMAP-Poll (`IngestService.run_once`) →
  Warteschlange erneut leeren (auch im Fehlerfall, `finally`) → Sammel-Digest prüfen.
  `Runner.run_forever()`: derselbe Zyklus in einer Schleife mit Poll-Intervall,
  Reconnect-Backoff aus `ingest.backoff_delay` und Shutdown über SIGINT/SIGTERM (ADR-051).
- **Befehlskanal in der Schleife (ADR-077/ADR-080, Stand Fixrunde 2026-09-11):** Nach
  jedem Zyklus und danach nach **jedem** Warte-Abschnitt von höchstens
  `runner.COMMAND_POLL_SECONDS = 10` Sekunden ruft `run_forever` `_serve_commands()`.
  Das arbeitet den gelesenen Stapel vollständig ab (Liste statt `any(…)` über einen
  Generator — sonst fielen alle Befehle hinter dem ersten `/digest` weg) und meldet, ob
  sofort ein weiterer Zyklus folgen soll. `_wait_for_next_cycle()` ersetzt das frühere
  einmalige `_wait(poll_interval)`; `stop()` beendet jeden Abschnitt sofort, damit SIGINT
  nicht auf ein Long-Polling wartet. `run_once` bedient den Kanal einmal am Ende
  (`_serve_commands_once`): `/status` antwortet, `/digest` wird nur konsumiert.
- **Ausnahmefeste Zone (ADR-049 Nachtrag):** Zustell-Warteschlange **und** Sammel-Digest
  laufen in `run_once` im `finally` und in `run_forever` auch im `except IngestError`-
  Zweig — beide brauchen kein Postfach. Ein Fehler des Digests wird dort abgefangen
  (`low_digest_failed`), damit er den `IngestError` nicht verdeckt.
- Statusfolge: `pending` (Ingest-`claim`) → `sanitized` → `summarized` → `checked` →
  `delivered` | `skipped_low` | `failed`. `checked` wird **vor** dem Versand committet
  (ADR-008); solange die Nachricht in der `outbox` liegt, bleibt es dabei — erst die
  bestätigte Zustellung schreibt `delivered`, die endgültig gescheiterte `failed` mit
  `error_class = "delivery_failed"`.
- `delivery.OutboxMessenger` erfüllt das schmale `pipeline.Messenger`-Protokoll: einreihen
  (Commit) → sofort versuchen → Erfolg löscht die Zeile, Misserfolg verschiebt sie.
  `send()` wirft bei Zustellfehlern nicht; `flush()` arbeitet fällige Einträge ab
  (5 Versuche, Backoff 60/300/900/2100 s, harte Schranke 1 h).
- `logging_setup.configure_logging(level, stream)` richtet ausschließlich den Logger
  `maildigest` ein und schreibt eine JSON-Zeile je Ereignis auf stdout; `log_level` kommt
  aus `[general]`, Tracebacks nur bei `DEBUG` (ADR-046/ADR-047).
- Betriebsdoku (systemd-Unit, Cron-Variante, Wartung): docs/BETRIEB.md.

### CLI (`cli.py`)

**Stand WP9 (ADR-052 bis ADR-057).** Vollständiger, verbindlicher Vertrag:
[SPEC-CLI.md](SPEC-CLI.md) — dieser Abschnitt beschreibt nur den inneren Aufbau.

- Kommandos: `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`,
  `run [--once]`; Exit-Codes 0/1/2. Einstiegspunkte: `[project.scripts] maildigest` und
  `python -m maildigest`.
- `argparse` mit einem `parents=`-Parser für `--config`/`--non-interactive`, sodass beide
  Optionen vor und nach dem Kommandonamen stehen dürfen (ADR-052). `main(argv, stdin,
  stdout, stderr, hooks)` gibt den Exit-Code **zurück**; nur `run_cli()` ruft `sys.exit`.
- `Hooks` bündelt alle Außenkontakte (`build_runner`, `build_messenger_from_section`,
  `build_provider_from_settings`, `build_summarizer`/`build_critic`, `ImapClient`,
  `discover_chat_ids`, `configure_logging`, `sleep`) — Tests ersetzen sie einzeln.
- `Console` kapselt jede Ein-/Ausgabe: `ask`/`ask_int`/`ask_secret`/`confirm`/`choose`,
  Defaults, Wertelisten, drei Fehlversuche, EOF = Abbruch. `--non-interactive` schaltet
  auf „nur Defaults und Optionen" um; eine fehlende Pflichtangabe nennt dann die zuständige
  Option und endet mit Exit-Code 2.
- `ConfigFile` hält die Datei als Roh-Dict, `render_toml` schreibt sie kommentiert zurück,
  `save()` legt sie über `os.open(..., 0o600)` an und setzt die Rechte bei jedem Schreiben
  neu (ADR-053, F-SEC-8). Validiert wird sektionsweise über `config.validate_section`,
  weil die Konfiguration während der Einrichtung unvollständig ist.
- Erweiterungen an bestehenden Modulen für die CLI (jeweils additiv):
  `config.validate_section`, `llm.factory.build_provider_from_settings`,
  `messenger.factory.build_messenger_from_section`, `messenger.telegram.discover_chat_ids`,
  `ImapClient.list_folders`, `DigestComposer.compose_plain` (ADR-054).
- `maildigest test` speist eine `.eml`-Datei in die echte Verdrahtung ein — temporäre
  State-DB, Zustellschwelle für den Lauf auf `low` (ADR-057). Mitgeliefert:
  `src/maildigest/data/selftest.eml`.
- Fremddaten (Ordnernamen, Telegram-Chats, Modellantwort) erreichen das Terminal nur
  gefiltert bzw. gar nicht (ADR-055).

## 3. Datenmodell (verbindlich für WP1)

```python
class RawMail(BaseModel, frozen=True):
    message_id: str | None          # Header; None wenn fehlend
    dedupe_key: str                 # message_id oder Fallback-Hash
    from_addr: str                  # "Anzeigename <adresse>"; Name RFC-2047-dekodiert und
                                    # von Struktursymbolen befreit, Adresse aus dem Rohheader
    from_domain: str                # aus der roh geparsten Adresse, lowercase (nie aus dem
                                    # Anzeigenamen — HC2-2)
    reply_to: str | None
    return_path_domain: str | None
    to_addrs: list[str]           # höchstens MAX_RECIPIENTS (200)
    subject_raw: str                # undekodiert/dekodiert roh
    date: datetime | None
    auth_results_header: str | None # Authentication-Results, roh
    mime_bytes: bytes               # komplette Roh-Mail (verlässt Ingest+Sanitizer nie!)
    size_bytes: int
    content_hash: str               # sha256(mime_bytes), zweites Dedupe-Merkmal (ADR-079)
    id_collision: bool              # Key belegt, Inhalt anders (ADR-079)

class AttachmentInfo(BaseModel, frozen=True):
    filename_sanitized: str
    declared_mime: str
    detected_kind: Literal["pdf", "text", "html", "unknown", "mismatch"]
    size_bytes: int
    processed: bool                 # True nur für Allowlist-Typen unter Limits
    extracted_chars: int            # 0 wenn nicht verarbeitet

class SanitizedMail(BaseModel, frozen=True):
    dedupe_key: str
    from_display: str               # sanitisierter Anzeigename
    from_domain: str
    subject: str                    # sanitisiert
    date: datetime | None
    body_text: str                  # sanitisierter Klartext inkl. [Link #n: domain] Marker
    attachment_texts: dict[str, str]  # filename → extrahierter, sanitisierter Text
    attachments: list[AttachmentInfo]
    links_found: list[str]          # defangte Darstellungen, nur für Report/Fußnote
    sanitization_report: SanitizationReport

class SanitizationReport(BaseModel, frozen=True):
    links_removed: int
    hidden_text_removed: bool
    control_chars_removed: int
    punycode_domains: list[str]
    mixed_script_domains: list[str]
    truncated: bool
    blocked_attachments: int
    reply_to_mismatch: bool
    return_path_mismatch: bool
    id_collision: bool              # Kopie von RawMail.id_collision (ADR-079)
    forged_markers: int             # nachgebaute Datenblock-Marker (ADR-061, HC-5)
    auth_results: dict[str, str]    # z. B. {"spf": "pass", "dkim": "fail"} best effort

class Summary(BaseModel):
    headline: str                   # ≤ 100 Zeichen
    summary_text: str
    importance: Literal["high", "normal", "low"]
    importance_reason: str
    category: str
    attachment_summaries: dict[str, str]
    injection_suspected: bool

class CriticVerdict(BaseModel):
    phishing_risk: Literal["none", "low", "high"]
    risk_reasons: list[str]
    summary_accurate: bool
    notes: str

class DigestMessage(BaseModel, frozen=True):
    # fertig formatierter, bereits output-sanitisierter Text, ggf. gesplittet
    parts: list[str]
    importance: Literal["high", "normal", "low"]
    is_warning: bool                # Phishing-Banner enthalten
    dedupe_key: str

class FailureNotice(BaseModel, frozen=True):
    dedupe_key: str
    from_domain: str
    subject_sanitized: str          # durch Not-Sanitizer (nur ASCII-Printables, gekürzt)
    stage: str                      # wo es scheiterte
    reason_class: str               # Fehlerklasse, keine Details/Inhalte
```

**Umsetzungshinweise (WP1, `models.py` — Begründung in ADR-014):**

- Die wiederkehrenden `Literal`-Aufzählungen sind als benannte Typ-Aliase exportiert und
  werden von `config.py`/`pipeline.py` mitbenutzt (semantisch identisch zur Tabelle oben):
  `Importance = Literal["high","normal","low"]`,
  `PhishingRisk = Literal["none","low","high"]`,
  `AttachmentKind = Literal["pdf","text","html","unknown","mismatch"]`.
- Alle Modelle laufen mit `extra="forbid"`; unbekannte Felder (z. B. aus LLM-JSON) sind ein
  Validierungsfehler statt stiller Übernahme (I4).
- `Summary` und `CriticVerdict` sind bewusst **nicht** frozen: Die deterministische
  Nachkontrolle in WP5/WP6 säubert Felder der untrusted LLM-Ausgabe. Alle übrigen Modelle
  sind frozen.
- Zählfelder/Größen haben `ge=0`, `Summary.headline` erzwingt `max_length=100`. Felder mit
  natürlichem Leerwert (Listen, Dicts, `bool`, optionale Header) haben Defaults, damit
  Ingest/Sanitizer keine Pflicht-Boilerplate erzeugen; identifizierende Felder
  (`dedupe_key`, `from_domain`, `body_text`, `mime_bytes`, …) bleiben Pflichtfelder.
- **`RawMail`-Konventionen für Unbekanntes (WP2, ADR-020):** `from_domain` ist der
  Leerstring, wenn im `From`-Header keine `lokalteil@domain`-Adresse steht oder der Header
  unbalanciert ist (offener Kommentar/Quoted String, S-2);
  `return_path_domain` ist in diesem Fall `None` (damit der Domain-Vergleich in WP6 nicht
  zwei Unbekannte als Treffer wertet). `date` ist `None` bei fehlendem oder unparsbarem
  `Date`-Header. `subject_raw` ist der RFC-2047-dekodierte, aber unsanitisierte Betreff mit
  aufgelöster Header-Faltung. `auth_results_header` enthält **alle**
  `Authentication-Results`-Vorkommen, mit `\n` verbunden. `build_raw_mail` wirft
  grundsätzlich nicht — eine dort scheiternde Mail käme nie in den Fail-closed-Pfad und
  ginge still verloren (I6/F-OPS-3).

## 4. Pipeline-Vertrag (`pipeline.py`)

```python
def process_mail(raw: RawMail, deps: PipelineDeps) -> PipelineResult: ...
# PipelineResult = Delivered | QueuedLow | FailedNotice (jeweils mit Status für state/db)
```

- Stufen werden als Protokolle injiziert (`Sanitizer`, `Summarizer`, `Critic`,
  `OutputComposer`, `Messenger`) → jede Stufe einzeln mockbar.
- `mime_bytes` ist nach dem Sanitizer nicht mehr erreichbar (Objekt wird nicht
  weitergereicht) — strukturelle Absicherung von I1.
- Jede Exception einer Stufe wird gefangen, klassifiziert, gezählt (Retry-Politik WP8)
  und endet schlimmstenfalls als `FailureNotice`.

**Stand WP1 (`pipeline.py`) — Details in ADR-011 bis ADR-013:**

```python
Stage      = Literal["sanitize", "summarize", "critic", "compose", "deliver"]
MailStatus = Literal["delivered", "skipped_low", "failed"]

class MailRef(BaseModel, frozen=True):   # Metadaten-Abzug, der den Sanitizer überlebt (I1)
    dedupe_key: str
    from_domain: str
    subject_sanitized: str               # Not-Sanitizer, s. FailureNotice in §3

@dataclass(frozen=True)
class PipelineDeps:
    sanitizer: Sanitizer;  summarizer: Summarizer;  critic: Critic
    composer: OutputComposer;  messenger: Messenger
    deliver_min_importance: Importance = "normal"
    progress: ProgressSink | None = None          # WP8: Zwischenstände in die State-DB

@dataclass(frozen=True)
class Delivered:    dedupe_key: str; message: DigestMessage; status = "delivered"
@dataclass(frozen=True)
class QueuedLow:    dedupe_key: str; from_domain: str; summary: Summary; status = "skipped_low"
@dataclass(frozen=True)
class FailedNotice: notice: FailureNotice; notice_delivered: bool; status = "failed"
```

Stufen-Protokolle: `Sanitizer.sanitize(raw) -> SanitizedMail`,
`Summarizer.summarize(mail) -> Summary`, `Critic.review(mail, summary) -> CriticVerdict`,
`OutputComposer.compose(mail, summary, verdict) -> DigestMessage` **und**
`OutputComposer.compose_failure(notice) -> DigestMessage`, `Messenger.send(message) -> None`.
Das Pipeline-`Messenger`-Protokoll ist bewusst schmaler als `messenger/base.py` (kein
`healthcheck()`): Die Pipeline verlangt nur, was sie aufruft.

Ablauf und Entscheidungspunkte:

1. Vor dem Sanitizer wird `MailRef` gezogen; nach dem Sanitize-Aufruf wird die
   `RawMail`-Referenz per `del` freigegeben. Die Stufen 3–6 sehen `SanitizedMail` und
   `MailRef` — nie `RawMail`/`mime_bytes` (I1).
2. `verdict.summary_accurate == False` ⇒ fail-closed mit `stage="critic"`,
   `reason_class="summary_inaccurate"` (T8) — noch vor jedem Schwellwertvergleich.
3. `phishing_risk == "high"` erzwingt Einzelzustellung und hebt `importance` mindestens auf
   `normal` (F-CRIT-2); sonst entscheidet `deliver_min_importance` über
   Zustellung vs. `QueuedLow`.
4. Fehler in einer Stufe ⇒ `FailureNotice` + **ein** Zustellversuch der Notiz über
   `compose_failure`/`send`. Scheitert auch der, ist `notice_delivered=False`; Retries sind
   Sache von WP8. `process_mail` wirft nie eine Stufen-Exception nach außen.
5. **Stand WP8 (ADR-050):** Nach Sanitize, nach Summarize und — entscheidend — **vor**
   Compose/Versand meldet die Pipeline den Zwischenstand an `deps.progress`
   (`sanitized`/`summarized`/`checked`). Der Sink committet; wirft er, gilt das als
   Stufenfehler und endet fail-closed (`state_error`).
6. `reason_class` wird aus dem Exception-**Klassennamen** abgeleitet (Tabelle in
   `pipeline._ERROR_CLASSES`), Fallback `"<stage>_error"`. Der Exception-Text wird nie
   übernommen (I5). Die Tabelle wurde bei der WP4-Integration um
   `"LLMTransportError": "llm_transport_error"` ergänzt — ohne den Eintrag wäre jeder
   HTTP-/Verbindungsfehler der LLM-Schicht im unscharfen Fallback gelandet (Verhalten war
   auch vorher fail-closed, nur das Label war grob).

## 5. Konfiguration (`config.toml`, Schema in `config.py`)

```toml
[general]
language = "de"              # Sprache der Zusammenfassungen
summary_length = "medium"    # short | medium | long
deliver_min_importance = "normal"  # low | normal | high
low_digest_time = "18:00"    # tägliche Sammelzustellung (lokale Zeit)
state_db = ""                # leer = state.db neben der Konfigurationsdatei (ADR-045)
log_level = "INFO"           # DEBUG | INFO | WARNING | ERROR (ADR-046)

[imap]
host = "imap.example.org"
port = 993
username = "mirror@example.org"
# password via MAILDIGEST_IMAP_PASSWORD oder hier (Datei ist 0600)
folder = "INBOX"
poll_interval_seconds = 120
move_processed_to = ""       # leer = nur als gelesen markieren

[llm]
provider = "anthropic"       # anthropic | openai_compatible
model = "…"                  # Pflichtfeld, kein hartkodierter Default im Code
# api_key via MAILDIGEST_LLM_API_KEY
base_url = ""                # für openai_compatible / lokale Server
max_tokens = 1024

[llm.critic]                 # optionaler Override, sonst wie [llm]
# model = "…"

[summarizer]
instructions = ""            # Custom-Instructions des Nutzers (semi-trusted, I8)

[links]
footnote = false             # defangte Link-Liste als Fußnote anhängen

[messenger]
active = "telegram"          # telegram | discord | signal

[messenger.telegram]
# token via MAILDIGEST_TELEGRAM_TOKEN
chat_id = ""

[messenger.discord]
webhook_url = ""             # Achtung: enthält Secret → Datei 0600

[messenger.signal]
enabled = false
signal_cli_socket = ""

[limits]                     # Defaults siehe SECURITY.md §4
max_mail_bytes = 26214400          # 25 MB
max_text_chars = 30000
pdf_max_input_bytes = 10485760     # 10 MB
pdf_max_output_chars = 50000
pdf_timeout_seconds = 20
pdf_time_budget_seconds = 30      # Zeitbudget aller PDF-Extraktionen einer Mail (ADR-029)
max_mime_depth = 10
max_attachments_processed = 20
max_html_elements = 50000          # Elementbudget der HTML-Konvertierung je Mail (ADR-084)
max_html_bytes = 1048576           # Bytebudget je Mail, vor dem Parsen geprüft (ADR-084)
```

**Umsetzungshinweise (WP1, `config.py` — Begründung in ADR-015):**

- Secret-Felder sind `pydantic.SecretStr` (`imap.password`, `llm.api_key`,
  `messenger.telegram.token`, `messenger.discord.webhook_url`) und erscheinen damit weder in
  `repr()`/Logs noch in Fehlermeldungen (I5).
- Env-Overrides (`MAILDIGEST_IMAP_PASSWORD`, `MAILDIGEST_LLM_API_KEY`,
  `MAILDIGEST_TELEGRAM_TOKEN`) werden **vor** der Validierung in das Roh-Dict gespiegelt;
  Env schlägt Datei, leere Werte werden ignoriert.
- Alle Sektionen sind `extra="forbid"` — ein Tippfehler ist ein Fehler, keine stille
  Ignoranz. Fehler werden zu einer `ConfigError` mit deutscher, feldbezogener,
  mehrzeiliger Meldung (`[sektion] feld: <Grund>`) übersetzt.
- `[llm.critic]` ist ein Override mit Vererbung: `Config.critic_model()`,
  `critic_provider()`, `critic_base_url()`, `critic_max_tokens()` liefern den Override oder
  den Wert aus `[llm]`.
- `[llm] model` hat bewusst keinen Default (Pflichtfeld); die übrigen Werte oben sind die
  tatsächlichen Code-Defaults.
- Neben `load_config(path)` gibt es `load_config_from_dict(data)` für CLI (WP9) und Tests.

**Weiterhin offen (aus WP7; in WP9 bewusst nicht angefasst):** `[messenger.signal]` hat kein Empfängerfeld; der Adapter stellt
deshalb an „Note to Self" zu (ADR-039). Für die Zustellung an eine andere Nummer wäre
`[messenger.signal] recipient = ""` nötig — eigener ADR, weil es das Config-Schema
erweitert.

**Ergänzungen aus WP4 (ADR-025):**

- `[llm] api_key` gilt für **beide** Rollen. Nutzt der Kritiker einen anderen *Cloud*-Provider
  als der Summarizer, reicht ein gemeinsamer Key nicht; für den Regelfall (Kritiker auf
  lokalem Server ohne Key oder gleicher Provider) ist das unproblematisch. Ein
  `[llm.critic] api_key` ist bewusst noch nicht vorgesehen.
- Eine Sektion `[llm.summarizer]` gibt es bewusst **nicht**: Der Summarizer ist der Normalfall
  und nutzt `[llm]` direkt; nur der Kritiker ist per `[llm.critic]` überschreibbar. Die
  gegenteilige Erwähnung in PLAN.md WP4 ist stale — ARCHITECTURE ist hier maßgeblich, und
  `extra="forbid"` würde `[llm.summarizer]` als Config-Fehler zurückweisen.
- `base_url` wird nicht auf `https` eingeschränkt, weil lokale Server (Ollama/vLLM) über
  `http://localhost` angesprochen werden. Für Cloud-Provider ist `https` Sache der
  Konfiguration; `connect-llm` warnt seit WP9 auf stderr, wenn die Basis-URL weder
  `https` noch lokal ist.

## 6. Fehler- & Retry-Politik (umgesetzt in WP8)

- IMAP-Fehler: Reconnect mit Exponential Backoff (max. 10 min), Loop läuft weiter.
- LLM-Fehler: 429 und 5xx werden **innerhalb** der LLM-Schicht bis zu 3-mal mit Backoff
  (1 s/2 s, `Retry-After` schlägt vor) wiederholt; Timeouts nicht (sonst blockiert eine Mail
  bis zu 3 Minuten). Bleibt der Fehler, wirft die Schicht
  `LLMRateLimited`/`LLMTransportError`/`LLMTimeout`, und die Stufen-Retry-Politik von WP8
  entscheidet über weitere Versuche, am Ende `FailureNotice` (ADR-023).
- Schema-Invalidität: 1 Reparatur-Retry (in `llm/schema.py`), dann `FailureNotice`.
- **Drei Retry-Ebenen, multiplikativ** (Klarstellung WP11, CT-16c): (1) Transport
  (`llm/_http.py`, `MAX_ATTEMPTS = 3`) — aber **nur** bei HTTP 429 und 5xx. (2) Schema
  (`llm/schema.py`) — genau ein zusätzlicher Reparaturaufruf, wenn die Antwort kein
  schemakonformes JSON ist. (3) Stufe (`runner.py`, ADR-050) — 3 Versuche je LLM-Stufe, aber
  nur bei Timeout, Rate-Limit und Transportfehler; `LLMInvalidResponse` wird hier bewusst
  **nicht** wiederholt. Daraus ergeben sich die beobachtbaren Zahlen: dauerhafter HTTP 500 →
  3 × 3 = 9 Requests; gültige HTTP-Antwort mit Nicht-JSON-Inhalt → 2 Requests (Erstaufruf +
  Reparatur, danach kein Stufen-Retry); Transportfehler ohne HTTP-Status → 3 Requests. Der
  in F-SEC-7 genannte Wert „3 Versuche" meint die **Stufen**-Ebene.
- **Systemuhr (HC-25):** Alle Zustellfristen rechnen mit der Wanduhr. Springt sie, gilt
  weiterhin „fünf Versuche", aber nicht mehr „über höchstens eine Stunde": Ein Alter, das
  nicht zum Retry-Plan passt, wird ignoriert statt geglaubt. Details im ADR-048-Nachtrag.
- Messenger-Fehler: 5 Versuche über max. 1 h (Nachricht ist fertig sanitisiert und darf
  aus der DB-Queue erneut versendet werden), dann `failed` + Log. Die Teile einer
  mehrteiligen Nachricht gehen einzeln an den Adapter; bricht die Zustellung beim Teil *n*
  ab, wird die Warteschlangen-Zeile auf die Teile ab *n* eingekürzt und der Retry setzt dort
  fort (ADR-066). At-least-once nach ADR-008 bleibt: genau der Teil, dessen Bestätigung
  ausblieb, kann doppelt ankommen — die Teile davor nicht.
- Prozess-Crash: State in SQLite so, dass Wiederanlauf idempotent ist (Status vor Versand
  committen ⇒ schlimmstenfalls eine Doppelzustellung, nie Verlust — ADR-008).

**Konkretisierung WP8 (ADR-048, ADR-050):**

| Fehlerort | Politik | Ort im Code |
|-----------|---------|-------------|
| IMAP | Reconnect, Backoff 5 s → 600 s, Loop läuft weiter | `ingest.backoff_delay`, `Runner.run_forever` |
| LLM (Timeout/429/Transport) | providerintern max. 3 (429/5xx), darüber 3 Stufenversuche mit 2 s/4 s | `llm/_http.py`, `runner.RetryingSummarizer`/`RetryingCritic` |
| LLM-Schema | 1 Reparaturversuch, **kein** Stufen-Retry | `llm/schema.py` |
| Kritiker `summary_accurate = false` | sofort fail-closed, kein Retry | `pipeline.process_mail` |
| Zustellung | 5 Versuche, 60/300/900/2100 s, harte Schranke 1 h, dann `failed`/`delivery_failed` | `delivery.OutboxMessenger` |
| Uhrsprung während der Zustellung | Alter auf `>= 0` geklemmt; die Stundenfrist greift nur bei plausiblem Alter (`age_is_plausible`), sonst entscheidet der Versuchszähler und `first_queued_at` wird neu gesetzt. Eine Fälligkeit mehr als 2 h in der Zukunft wird auf `now` korrigiert (`outbox_clock_skew_corrected`) | `delivery._handle_failure`, `StateDB.outbox_due` (ADR-048 Nachtrag, HC-25) |
| State-DB nicht schreibbar | Stufenfehler ⇒ Metadaten-Notiz (`state_error`) | `pipeline._record` |

## 7. Nachrichtenformat (final festgeschrieben in WP7, `output/composer.py`)

```
⚠️ SUSPECTED PHISHING: <risk_reasons, kommasepariert, max. 5>   ← nur bei phishing_risk = high
📧 <headline> [important]                                       ← Tag nur bei importance = high
From: <from_display> (<from_domain>) · <TT.MM. HH:MM>           ← ohne Anzeigename nur Domain;
                                                                  ohne Date-Header `date unknown`
<summary_text>
— <datei>: <1–2 Sätze je verarbeitetem Anhang>
📎 Not processed: <datei (größe)>, … [and N more]               ← alle AttachmentInfo mit processed = false
🔍 Notes: <Injection-Flag; verschlüsselt; Message-ID-Kollision; Auth-Fails; Punycode;
           gemischte Schriftsysteme; HTML-Teil weicht vom Textteil ab; versteckter Text im
           HTML entfernt; Reply-To-/Return-Path-Abweichung; Text gekürzt;
           Kritiker-Gründe bei risk = low>
<Link-Fußnote (defanged)>                                       ← nur bei [links] footnote = true
```

Der **Rahmen ist englisch und sprachunabhängig** (ADR-083); `[general] language` steuert nur
die vom Modell gefüllten Felder. Der wörtliche Vertrag steht in docs/SPEC-CLI.md §6 und ist
über `tests/unit/test_hc14_spec_literals.py` maschinell an den Composer gebunden.

Die **Präfixe dieses Formats sind reserviert**: Der Composer fügt sie nach dem Feld-Scrub an,
und `output/sanitizer.neutralize_markup()` verhindert, dass modellgelieferter Text sie am
Zeilenanfang nachbaut (ADR-062, CT-8). Die Link-Fußnote hängt ebenfalls der Composer an — sie
gehört an die Zustellung und nicht in den Prompt (ADR-072, CT-14).

Verbindliche Zusatzregeln (WP7):

1. **Alle** Domains und Dateinamen erscheinen mit gebrochenen Punkten
   (`stadtwerke-x[.]de`, `rechnung[.]pdf`, auch innerhalb von `[Link #n: …]`-Markern) —
   Telegram und Discord verlinken nackte Domains sonst automatisch (T7, ADR-036). Das
   Beispiel oben ist entsprechend zu lesen.
2. Einzellimits je untrusted Feld (Headline 120, Summary 3000, Anhangs-Zusammenfassung 400,
   Kritiker-Grund 200, Anzeigename 80, Domain 100, Dateiname 80 Zeichen; max. 10 gelistete
   Anhänge, max. 5 Banner-Gründe), Kürzung mit `…` (ADR-040).
3. Split an Zeilengrenzen auf das Limit des aktiven Messengers (Telegram 4096, Discord 2000,
   Signal 2000), ohne Teil-Zähler. Muss eine **einzelne Zeile** hart geschnitten werden,
   trägt jedes Stück nach dem ersten das Fortsetzungspräfix `… ` (U+2026 + Leerzeichen).
   Es zählt zum Limit. Grund: Nur dieser Schnitt erzeugt einen Zeilenanfang, den keine
   Schicht geprüft hat — ohne das Präfix konnte ein Teil mit einer gefälschten
   Programmzeile beginnen (HC-6, Nachtrag zu ADR-062). Schnitte an Zeilengrenzen brauchen
   es nicht: Diese Anfänge hat `neutralize_markup` bereits gesehen oder der Composer
   selbst erzeugt.
4. Fail-closed-Notiz (`compose_failure`, F-OPS-3) — fünf Zeilen, ausschließlich Metadaten:

   ```
   ⚠️ This mail could not be processed safely — no content delivered.
   From: <from_domain>
   Subject: <not-sanitisierter Betreff>
   Stage: <stage> · Reason: <reason_class>
   Open your real mailbox to read it.
   ```

   `importance = normal`, `is_warning = false`; Stufe und Grund werden auf
   `[A-Za-z0-9_-]`-Labels normalisiert (I5).

Sammel-Digest (täglich, `DigestComposer.compose_low_digest`, F-SUM-5): **eine** Nachricht,
gruppiert nach Kategorie (größte Gruppe zuerst), je Mail eine Zeile
`• <headline> (<from_domain>)`:

```
🗂 12 low-priority mails: 8 newsletter, 3 benachrichtigung, 1 other

newsletter (8):
• Wochenrückblick KW 36 (news[.]example[.]org)
…
... and N more                                   ← ab 60 gelisteten Mails
```

Die Nachricht entsteht wie jede andere über `_finalize()` (Nachbrenner + Split),
`importance = low`, `is_warning = false`, `dedupe_key = "low-digest"`. Zugestellt wird beim
ersten Zyklus ab `[general] low_digest_time`; ein verpasster Zeitpunkt wird am selben Tag
nachgeholt, eine leere Warteschlange erzeugt keine Nachricht (ADR-049).
