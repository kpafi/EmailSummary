# MailDigest — Design-Entscheidungen (ADR-Log)

> Jede nicht-triviale Entscheidung wird hier als kurzer ADR festgehalten — von jedem WP,
> in dem sie fällt. Bestehende ADRs werden nie umgeschrieben, nur durch neue ersetzt
> (Status `superseded by ADR-xxx`).

**Format:**
```markdown
## ADR-NNN: <Titel>
- Status: accepted | superseded by ADR-XXX
- WP / Datum: <WP-Nr>, <YYYY-MM-DD>
- Kontext: <Problem, 1–3 Sätze>
- Entscheidung: <was und wie>
- Alternativen: <was verworfen wurde und warum>
- Konsequenzen: <Trade-offs, Folgekosten>
```

---

## ADR-001: Python als Implementierungssprache
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Lightweight-Tool, ein Prozess, viel Text-/MIME-Verarbeitung, soll auf kleinem
  Server/Pi laufen und von Agenten gut wartbar sein.
- Entscheidung: Python ≥ 3.11; Paketname `maildigest`.
- Alternativen: Go (statisches Binary, aber schwächeres MIME-/HTML-Ökosystem für diesen
  Zweck und langsamere Agent-Iteration); Node (ok, aber Python-Stdlib `email` + pdfminer
  ist der kürzeste Weg).
- Konsequenzen: Deployment braucht Python-Runtime; dafür minimale Eigenentwicklung bei
  MIME/PDF/HTML.

## ADR-002: Eigene schmale LLM-Abstraktion statt Framework
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Provider müssen austauschbar sein (Anthropic, OpenAI-kompatibel/lokal), aber
  das Tool soll leichtgewichtig bleiben und I2 (kein Tool-Use) strukturell erzwingen.
- Entscheidung: Eigenes Protokoll `complete(system, user) -> str` + `complete_json` mit
  pydantic-Validierung; HTTP direkt via httpx.
- Alternativen: LangChain/LiteLLM (Dependency-Gewicht, Tool-Calling-Oberfläche wäre
  vorhanden und müsste aktiv vermieden werden).
- Konsequenzen: Neue Provider erfordern ~100 Zeilen Adapter; akzeptiert.

## ADR-003: Allowlist-Sanitizing, fail-closed
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Anhänge/Links sind der primäre Angriffsvektor; Blocklisten veralten.
- Entscheidung: Nur `text/plain`, `text/html`, `application/pdf` werden inhaltlich
  verarbeitet (nach Magic-Byte-Check); alles andere nur als Metadatum. Jeder
  Verarbeitungsfehler ⇒ Metadaten-Notiz statt Inhalt.
- Alternativen: Blocklist gefährlicher Endungen (unvollständig per Konstruktion);
  Virenscanner-Anbindung (Gewicht, falsches Sicherheitsgefühl).
- Konsequenzen: Manche legitime Inhalte (docx-Rechnung) werden nicht zusammengefasst —
  bewusster Trade-off, im README als FAQ erklärt.

## ADR-004: Zwei rechtelose LLM-Instanzen + deterministische Klammer
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Prompt-Injection ist nicht zuverlässig verhinderbar.
- Entscheidung: Injection wird wirkungslos gemacht statt „verhindert": LLMs ohne Tools
  (I2), Ein-/Ausgang jeweils deterministisch sanitisiert (I1/I4), unabhängiger Kritiker,
  letzte Instanz vor dem Nutzer ist immer Code.
- Alternativen: Ein einzelnes LLM mit „gutem Prompt" (keine Verteidigungstiefe);
  Injection-Detektor-Modelle (zusätzliche Fehlerquelle, ersetzt die Klammer nicht).
- Konsequenzen: Zwei LLM-Calls pro Mail (Kosten); akzeptiert, Kritiker darf kleineres
  Modell nutzen (`[llm.critic]`).

## ADR-005: SQLite als einziger Store
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Dedupe + Status + Low-Digest-Queue brauchen Persistenz; NF-1 verbietet Server-DB.
- Entscheidung: SQLite (stdlib), eine Datei neben der Config; kein Mail-Volltext in der DB.
- Alternativen: JSON-Statedatei (Locking/Atomicity selbst bauen); Postgres (Overkill).
- Konsequenzen: Single-Instance-Betrieb; für v0.1 gewollt.

## ADR-006: Telegram zuerst, reiner Text ohne parse_mode
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Erster Messenger soll einfach & robust sein; Markup ist ein Injection-Vektor (T7).
- Entscheidung: Telegram-Bot-API als Referenz-Adapter; Nachrichten als reiner Text
  (kein parse_mode). Discord via Webhook als zweiter. Signal optional via signal-cli.
- Alternativen: Formatierte Nachrichten mit Escaping (schöner, aber Escaping-Fehler wären
  sicherheitsrelevant — ggf. später per ADR).
- Konsequenzen: Keine Fettschrift in v0.1; Emojis/Unicode-Struktur übernehmen die Optik.

## ADR-007: Polling statt IMAP IDLE in v0.1
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: E-Mail-Zusammenfassungen sind nicht latenzkritisch; IDLE-Verbindungen sind
  fehleranfällig (Timeouts, Server-Eigenheiten).
- Entscheidung: Polling (Default 120 s). IDLE nur, falls `imap-tools` es trivial hergibt
  (Entscheidung in WP2 dokumentieren).
- Konsequenzen: Bis zu ~2 min Verzögerung; akzeptiert.

## ADR-008: Fail-closed-Zustellsemantik „at-least-once"
- Status: accepted
- WP / Datum: Planung, 2026-08-28
- Kontext: Crash zwischen Versand und State-Commit ist unvermeidbar möglich.
- Entscheidung: Status-Commit **vor** Versand auf `checked`, nach Versand auf `delivered`;
  Wiederanlauf versendet `checked`-Nachrichten erneut. Schlimmstenfalls Doppelzustellung,
  nie stiller Verlust (F-OPS-3).
- Konsequenzen: Seltene Duplikate im Messenger; akzeptiert.

## ADR-009: Build-Backend hatchling + src-Layout
- Status: accepted
- WP / Datum: WP0, 2026-08-28
- Kontext: WP0 braucht ein PEP-621-`pyproject.toml` mit Build-Backend. Das Paket liegt
  bewusst unter `src/maildigest/` (src-Layout), damit Tests gegen die installierte
  Distribution laufen und nicht versehentlich gegen das Quellverzeichnis importieren.
- Entscheidung: `hatchling` als Build-Backend; Wheel-Ziel explizit auf `src/maildigest`
  gesetzt (`[tool.hatch.build.targets.wheel] packages`).
- Alternativen: `setuptools` (funktioniert ebenso, braucht für src-Layout aber mehr
  Boilerplate bzw. `package-dir`-Konfiguration; historisch mehr Altlasten). Poetry/PDM
  (eigene, nicht-PEP-621-nahe Metadaten bzw. zusätzliches Tooling — widerspricht dem
  Lightweight-Prinzip).
- Konsequenzen: Reine PEP-621-Metadaten, minimale Konfiguration, keine `setup.py`/
  `setup.cfg`. Kein `readme`-Feld in `[project]`, da `README.md` erst in WP9/WP12
  entsteht (ein fehlendes Readme-File würde den Build brechen).

## ADR-010: Tooling-Regelsatz (ruff-Auswahl, mypy strict)
- Status: accepted
- WP / Datum: WP0, 2026-08-28
- Kontext: Einheitliche Lint-/Typ-Basis ab Projektbeginn, ohne die Agenten mit
  Rauschen auszubremsen. Sicherheitskritischer Code (Sanitizer, Output) profitiert von
  strenger statischer Prüfung.
- Entscheidung: `ruff` mit Regelgruppen `E,W,F,I,N,UP,B,C4,SIM,RUF` (Fehler,
  Pyflakes, Import-Sortierung, Naming, Modernisierung, Bugbear, Comprehensions,
  Simplify, Ruff-eigene), `line-length = 100`, `target-version = py311`.
  `mypy` im `strict`-Modus für `src/` (`mypy_path = "src"`, `explicit_package_bases`,
  `namespace_packages`), damit `mypy src/` mit src-Layout sauber auflöst.
- Alternativen: Minimaler ruff-Default (nur `E,F`) — zu wenig für sicherheitsnahen
  Code; zusätzlich `PL`/`ANN`/`D` — für ein Gerüst zu streng/geräuschig, kann später
  per ADR nachgezogen werden. Getrennte Tools (flake8+isort+black) — mehr
  Dependencies, widerspricht Lightweight.
- Konsequenzen: Konsistenter Stil und früh greifende Typprüfung; Regelsatz ist bewusst
  erweiterbar, wenn spätere WPs es rechtfertigen.

## ADR-011: `MailRef` statt Weitergabe von `RawMail` (strukturelle I1-Absicherung)
- Status: accepted
- WP / Datum: WP1, 2026-08-28
- Kontext: I1 verlangt, dass nach der Sanitize-Stufe niemand mehr `mime_bytes` sieht. Der
  Fehlerpfad braucht aber weiterhin Absender-Domain, Dedupe-Key und Betreff, um eine
  `FailureNotice` bauen zu können — genau die Felder stecken im `RawMail`.
- Entscheidung: `process_mail` zieht vor dem Sanitizer einen frozen Metadaten-Abzug
  `MailRef(dedupe_key, from_domain, subject_sanitized)`; die `RawMail`-Referenz wird direkt
  nach dem Sanitize-Aufruf im `finally` per `del` freigegeben. Die Stufen 3–6 und der
  Fehlerpfad arbeiten ausschließlich mit `SanitizedMail` + `MailRef`. Der Betreff im
  `MailRef` läuft durch einen abhängigkeitsfreien „Not-Sanitizer" (nur druckbare
  ASCII-Zeichen, Whitespace normalisiert, hart auf 120 Zeichen gekürzt), weil er auch dann
  korrekt sein muss, wenn genau der reguläre Sanitizer gerade versagt hat (T2/T12).
- Alternativen: `RawMail` einfach weiterreichen und sich auf Disziplin verlassen (I1 wäre
  nur eine Konvention, in späteren WPs leicht zu verletzen). Ein zweites Modell ohne
  `mime_bytes` erzeugen (mehr Duplikation, gleicher Effekt).
- Konsequenzen: Ein zusätzliches Modell; dafür kann keine spätere Stufe versehentlich auf
  Rohbytes zugreifen. `del raw` ist ein bewusst sichtbarer Marker, kein Performance-Trick.

## ADR-012: Fehlerklassifikation über Exception-Klassennamen, ohne Fehlertext
- Status: accepted
- WP / Datum: WP1, 2026-08-28
- Kontext: `FailureNotice.reason_class` landet beim Nutzer und im State. Exception-Texte
  können Mail-Inhalte oder Secrets enthalten (I5). Gleichzeitig soll `pipeline.py` nicht von
  Modulen späterer WPs (`llm/`, `sanitize/`, `messenger/`) importieren.
- Entscheidung: `classify_failure(stage, exc)` bildet den Exception-**Klassennamen** über
  eine Tabelle (`SanitizeError`, `LLMTimeout`, `LLMRateLimited`, `LLMInvalidResponse`,
  `ValidationError`, `MessengerError`, intern `_InaccurateSummaryError`) auf eine stabile,
  grobe Klasse ab; Fallback ist `"<stage>_error"`. Der Exception-Text wird nie übernommen.
  Der Kritiker-Einspruch `summary_accurate = false` wird intern als Exception
  (`_InaccurateSummaryError`) modelliert, damit er exakt denselben Fail-closed-Pfad nimmt
  wie ein Stufenfehler.
- Alternativen: `isinstance`-Prüfungen mit echten Imports (Zirkel-/Kopplungsproblem, WP1
  könnte gar nicht importieren, was noch nicht existiert); Fehlertext mitgeben (I5-Verstoß).
- Konsequenzen: Namensgleiche Fremd-Exceptions könnten falsch klassifiziert werden — der
  Effekt ist auf ein ungenaues Label begrenzt, das Verhalten bleibt fail-closed. Spätere WPs
  müssen ihre Fehlerklassen so benennen wie in der Tabelle.

## ADR-013: Ergebnis-Typen als frozen dataclasses mit Endstatus; Notiz-Versand ohne Retry
- Status: accepted
- WP / Datum: WP1, 2026-08-28
- Kontext: `process_mail` muss WP8 sagen, welcher `mail_status` zu schreiben ist, und darf
  laut I6 keine Stufen-Exception nach außen lassen.
- Entscheidung: `PipelineResult = Delivered | QueuedLow | FailedNotice` als frozen
  dataclasses (nicht pydantic — reine Prozess-Rückgaben, keine externen Daten, keine
  Validierung nötig), jeweils mit einem `status`-Feld aus
  `MailStatus = "delivered" | "skipped_low" | "failed"`. `FailedNotice` trägt zusätzlich
  `notice_delivered: bool`: Die Pipeline versucht **genau einmal**, die Metadaten-Notiz
  zuzustellen; scheitert auch das (kaputter Composer/Messenger), wird das gemeldet statt
  eskaliert. Retry-/Backoff-Politik ist ausdrücklich WP8 (ARCHITECTURE §6).
- Alternativen: Exceptions als Kontrollfluss nach außen (widerspricht I6, jede Aufrufstelle
  müsste es erneut richtig machen); ein einzelner Result-Typ mit Optional-Feldern (kein
  Typ-Nutzen bei der Auswertung).
- Konsequenzen: WP8 kann per `match` erschöpfend auswerten; ein doppelter Notiz-Versand
  kann in WP1 nicht entstehen.

## ADR-014: Modell-Härtung `extra="forbid"`, Typ-Aliase, gezielte Veränderlichkeit
- Status: accepted
- WP / Datum: WP1, 2026-08-28
- Kontext: `models.py` setzt ARCHITECTURE §3 um. Zwei Punkte sind dort nicht ausbuchstabiert:
  Umgang mit unbekannten Feldern und welche Modelle veränderlich sein dürfen.
- Entscheidung: (a) Alle Modelle mit `extra="forbid"` — unbekannte Schlüssel aus LLM-JSON
  werden abgelehnt statt still übernommen (Teil der Schema-Härtung, I4). (b) `Summary` und
  `CriticVerdict` bleiben veränderlich, weil die deterministische Nachkontrolle in WP5/WP6
  Felder säubern muss; alle übrigen Modelle sind frozen. (c) Die wiederkehrenden
  `Literal`-Aufzählungen werden als Aliase `Importance`, `PhishingRisk`, `AttachmentKind`
  exportiert und von `config.py`/`pipeline.py` mitbenutzt — eine Definitionsstelle statt
  vier Kopien. (d) Größen-/Zählfelder bekommen `ge=0`, `headline` `max_length=100`; Felder
  mit natürlichem Leerwert bekommen Defaults, identifizierende Felder bleiben Pflicht.
  ARCHITECTURE §3 wurde um diese Umsetzungshinweise ergänzt.
- Alternativen: pydantic-Default `extra="ignore"` (unbekannte LLM-Felder verschwinden
  stumm — schlechte Diagnose, schwächere Härtung); alles frozen und in WP5/WP6 mit
  `model_copy` arbeiten (mehr Zeremonie ohne Sicherheitsgewinn, da beide Objekte ohnehin
  untrusted sind und erst der Output-Sanitizer verbindlich ist).
- Konsequenzen: Ein Provider, der Zusatzfelder liefert, erzeugt einen Validierungsfehler ⇒
  fail-closed (gewollt). Änderungen an den Aufzählungen erfolgen an genau einer Stelle.

## ADR-015: Config — SecretStr, Env-Overrides vor der Validierung, deutsche Fehlermeldungen
- Status: accepted
- WP / Datum: WP1, 2026-08-28
- Kontext: Secrets dürfen weder in Logs noch in Fehlermeldungen auftauchen (I5), sollen
  bevorzugt aus der Umgebung kommen (SECURITY §6) — und ein nur in der Umgebung gesetztes
  Secret darf kein „Pflichtfeld fehlt" auslösen.
- Entscheidung: Secret-Felder sind `pydantic.SecretStr`. Die drei `MAILDIGEST_*`-Variablen
  werden **vor** der pydantic-Validierung in das (tiefenkopierte) Roh-Dict gespiegelt; Env
  schlägt Datei, leere Werte werden ignoriert, fehlende Zwischen-Sektionen werden angelegt.
  Ist eine Zwischen-Sektion vorhanden, aber keine TOML-Tabelle, wird der Override
  übersprungen, damit der echte Typfehler unverfälscht gemeldet wird. `ValidationError`
  wird in eine `ConfigError` mit deutscher, mehrzeiliger, feldbezogener Meldung
  (`[sektion] feld: <Grund>`) plus Hinweis auf die Env-Variablen übersetzt. Alle Sektionen
  sind `extra="forbid"` (Tippfehler-Schutz). ARCHITECTURE §5 wurde um die konkreten
  `[limits]`-Feldnamen und diese Umsetzungshinweise ergänzt.
- Alternativen: `pydantic-settings` als Env-Quelle (neue Laufzeit-Dependency, NF-2/ADR
  nötig, und die Env-Abbildung ist hier auf drei Felder beschränkt); Overrides nach der
  Validierung setzen (dann schlägt die Validierung fehl, bevor das Secret ankommt);
  englische pydantic-Rohmeldungen durchreichen (unbrauchbar für die Setup-UX in WP9).
- Konsequenzen: Die Fehlerübersetzung deckt die relevanten pydantic-Fehlertypen ab; für
  unbekannte Typen wird die Original-`msg` durchgereicht (englisch, aber secret-frei).
  Neue Secrets brauchen einen Eintrag in `_ENV_OVERRIDES`.

## ADR-016: Kein IMAP IDLE in v0.1 — Polling bleibt der einzige Abruf-Pfad
- Status: accepted
- WP / Datum: WP2, 2026-08-28
- Kontext: ADR-007 hat Polling festgelegt und IDLE unter den Vorbehalt gestellt, dass
  `imap-tools` es „trivial hergibt". WP2 hat die Bibliothek geprüft: `imap_tools` 1.15.0
  bietet einen `IdleManager` mit `start()`/`poll(timeout)`/`wait(timeout)`/`stop()`.
- Entscheidung: IDLE wird nicht implementiert. Der Ingest pollt ausschließlich mit dem
  konfigurierten Intervall (`[imap] poll_interval_seconds`, Default 120 s).
- Alternativen: IDLE nutzen. Verworfen, weil „trivial" nicht zutrifft: Ein korrekter
  IDLE-Betrieb braucht (a) einen erzwungenen Neustart des IDLE-Kommandos vor dem
  RFC-2177-Limit von 29 Minuten, (b) ein zweites, paralleles Fehler- und Reconnect-Regime
  neben dem Poll-Pfad (halb offene Sockets fallen bei IDLE erst nach Minuten auf), (c) einen
  Fallback-Poll für Server, die IDLE nicht ankündigen oder stillschweigend nicht liefern,
  und (d) eine eigene Behandlung für unterbrechbaren Shutdown. Das ist ungefähr die doppelte
  Menge an sicherheitsrelevantem Loop-Code für einen Latenzgewinn, der bei einem
  Mail-Zusammenfassungs-Digest keinen Nutzwert hat.
- Konsequenzen: Bis zu ~2 Minuten Verzögerung (wie in ADR-007 akzeptiert). Der Loop hat
  genau einen Fehlerpfad (Reconnect mit Exponential Backoff), was Tests und Review
  vereinfacht. IDLE kann später additiv per neuem ADR nachgerüstet werden, ohne dass sich
  die Ingest-Schnittstelle ändert.

## ADR-017: Ingest-Tests mocken auf imap-tools-Ebene statt gegen einen echten IMAP-Server
- Status: accepted
- WP / Datum: WP2, 2026-08-28
- Kontext: PLAN WP2 nennt als Akzeptanzkriterium einen Integrationstest gegen einen lokalen
  Test-IMAP-Server (greenmail via Docker o. ä.) und erlaubt ausdrücklich den Mock auf
  `imap-tools`-Ebene, sofern das als ADR dokumentiert wird.
- Entscheidung: Es wird gemockt, und zwar an zwei präzise gewählten Schnittstellen:
  (a) `MailMessage` wird aus echten `.eml`-Rohbytes gebaut — die Nachrichten-Parselogik ist
  also **nicht** gemockt, sondern läuft im Test genau so wie im Betrieb; (b) nur die
  `MailBox`-Verbindung wird über die injizierte `mailbox_factory` durch einen `FakeMailBox`
  ersetzt, der `login/logout/fetch/flag/move` protokolliert und bei `delete`/`expunge`
  sofort mit einem `AssertionError` auffliegt.
- Alternativen: greenmail via Docker — verletzt „kein Docker-Zwang" (Leitprinzip 1), macht
  die Testsuite netzwerk- und imageabhängig und testet im Wesentlichen fremden Code
  (imaplib/imap-tools), nicht die MailDigest-Logik. Dovecot lokal — noch schwerer
  reproduzierbar. Ein In-Process-IMAP-Fake auf Protokollebene — hoher Eigenbau-Aufwand für
  denselben Erkenntnisgewinn.
- Konsequenzen: Die Testsuite läuft offline in Millisekunden und deckt genau die
  MailDigest-Logik ab (RawMail-Aufbau, Dedupe, Statusführung, Backoff, Seen/Move). Nicht
  abgedeckt bleiben serverspezifische Eigenheiten (UID-Formate, MOVE-Capability,
  Folder-Encoding) und der TLS-Handshake selbst — die werden über den Cold-Test (WP11) und
  `maildigest connect-mail` (WP9) gegen ein echtes Postfach abgedeckt. Diese Lücke ist
  bewusst und hier festgehalten.

## ADR-018: Dedupe-State speichert nur den Hash; `claim()` ist das Idempotenz-Primitiv
- Status: accepted
- WP / Datum: WP2, 2026-08-28
- Kontext: F-ING-2 verlangt „genau einmal verarbeiten". ARCHITECTURE §2 gibt die Spalte
  `message_id_hash` vor, SECURITY §6/NF-5 verbieten Mail-Inhalte und PII in der DB. Offen
  war, welcher Wert dort steht und wie Idempotenz konkret erzwungen wird.
- Entscheidung: (a) Gespeichert wird `sha256(dedupe_key)` als Hex — nie die Message-ID oder
  der Fallback-Key im Klartext; die Message-ID ist ein Identifikator der Mail und in
  manchen Fällen sprechend (Absender-Domain, Ticketnummern). Derselbe Hash, auf 12 Zeichen
  gekürzt, ist die einzige Mail-ID, die in Logs erscheinen darf (NF-5). (b) Idempotenz läuft
  über `StateDB.claim()` = `INSERT OR IGNORE` auf den Primärschlüssel: Genau der erste
  Aufruf bekommt `True`, jeder weitere `False`. Kein Read-then-Write, also auch bei
  parallelen Aufrufen kein Race. (c) `error_class` wird beim Schreiben hart auf
  `[a-z0-9_]`, 64 Zeichen normalisiert — eine strukturelle Schranke gegen versehentlich
  durchgereichte Exception-Texte (I5), ergänzend zu ADR-012, das dasselbe auf der
  Pipeline-Seite regelt. (d) Die DB-Datei wird mit Modus `0600` angelegt.
- Alternativen: Message-ID im Klartext speichern (bequemer beim Debuggen, aber PII in der
  DB und in Logs); `was_seen()` + separates `INSERT` (Race-Fenster); Fehlertexte speichern
  (I5-Verstoß).
- Konsequenzen: Aus der DB lässt sich nicht mehr ablesen, *welche* Mail ein Eintrag ist —
  Debugging geht nur über den Hash, den auch das Log ausgibt. Das ist der gewollte
  Trade-off. `was_seen()` bleibt als reine Abfrage erhalten, ist aber für den
  Verarbeitungspfad nicht die maßgebliche Prüfung. Die Normalisierung von `error_class`
  vereinheitlicht Zeichenvorrat und Länge; sie ist keine inhaltliche Filterung — es bleibt
  Pflicht der Aufrufstellen, ausschließlich konstante Klassenlabels zu übergeben.

## ADR-019: Reihenfolge im Ingest — erst reservieren, dann verarbeiten, zuletzt Seen/Move
- Status: accepted
- WP / Datum: WP2, 2026-08-28
- Kontext: Zwischen „Mail geholt", „Mail verarbeitet" und „Mail im Postfach als gelesen
  markiert" kann der Prozess jederzeit abstürzen. ADR-008 legt die Semantik
  „at-least-once, nie stiller Verlust" fest; WP2 muss sie im Postfach-Umgang konkret
  umsetzen.
- Entscheidung: Je Mail gilt strikt: (1) `RawMail` bauen, (2) `db.claim()` — dieser Commit
  passiert **vor** jeder Verarbeitung, (3) Pipeline-Callback, (4) Endstatus schreiben,
  (5) `\Seen` setzen und ggf. `move()`. Der Abruf läuft deshalb mit `mark_seen=False`.
  Ein bereits bekannter Dedupe-Key wird übersprungen, aber trotzdem als gelesen markiert
  bzw. verschoben, damit er die Unseen-Menge verlässt und nicht bei jedem Poll erneut
  auftaucht. Eine Mail, deren Verarbeitung mit einer Exception endet, bekommt Status
  `failed` und wird ebenfalls als gelesen markiert — sie wird im Poll-Loop nicht endlos
  wiederholt (Retries sind laut ARCHITECTURE §6 Sache von WP8 und laufen über den
  DB-Status, nicht über das IMAP-Flag).
- Alternativen: `mark_seen=True` beim Fetch (ein Absturz direkt nach dem Fetch würde die
  Mail stillschweigend verlieren — Verstoß gegen F-OPS-3); Seen-Flag als Dedupe-Speicher
  statt der DB (das Postfach ist kein verlässlicher State: Der Nutzer kann Mails im
  Mirror-Postfach selbst als ungelesen markieren, und ein Ordner-Move würde den Zustand
  verlieren).
- Konsequenzen: Ein Absturz zwischen Schritt 2 und 5 führt beim Wiederanlauf zu einer als
  Duplikat erkannten, nicht erneut verarbeiteten Mail — konsistent mit ADR-008.
  `move_processed_to` ist rein kosmetisch und darf nie die einzige Dedupe-Quelle sein.
  Löschen findet nirgends statt (F-ING-1): `delete()`/`expunge()` kommen im Ingest-Modul
  nicht vor, und die Test-Fakes brechen ab, falls das je jemand einführt.

## ADR-020: IMAPS wird erzwungen; Header-Auswertung mit expliziten Unbekannt-Werten
- Status: accepted
- WP / Datum: WP2, 2026-08-28
- Kontext: SECURITY §6 verlangt „IMAP nur über TLS (IMAPS 993), Zertifikatsprüfung an, kein
  `verify=False` irgendwo". Die Config erlaubt aber jeden Port 1–65535. Zusätzlich lässt
  ARCHITECTURE §3 offen, was in `from_domain`/`date`/`subject_raw` steht, wenn die Header
  fehlen oder kaputt sind — und genau das ist der Normalfall bei Angriffsmails.
- Entscheidung: (a) Es wird ausschließlich `imap_tools.MailBox` (implizites TLS) mit einem
  frisch erzeugten `ssl.create_default_context()` verwendet — `CERT_REQUIRED` und
  `check_hostname=True`. Es gibt keinen Konfigurationsschalter und keinen Codepfad, der das
  abschaltet, und keinen Import von `MailBoxUnencrypted`/`MailBoxStartTls`. Port 143
  (Klartext-IMAP) wird beim Anlegen des Clients mit einer verständlichen Meldung abgelehnt,
  statt in einen unklaren Handshake-Fehler zu laufen. Andere Ports bleiben erlaubt (manche
  Anbieter nutzen abweichende IMAPS-Ports). (b) `from_domain` ist der Leerstring, wenn keine
  `lokalteil@domain`-Adresse erkennbar ist; `return_path_domain` ist in diesem Fall `None`,
  damit der Domain-Vergleich in WP6 nicht zwei Unbekannte als Übereinstimmung wertet.
  (c) `date` ist `None` bei fehlendem oder unparsbarem Header — bewusst nicht der
  imap-tools-Sentinel 1900-01-01. (d) `subject_raw` ist der RFC-2047-dekodierte, aber
  ansonsten unsanitisierte Betreff mit aufgelöster Header-Faltung; die eigentliche
  Sanitisierung bleibt WP3, der Not-Sanitizer für Notizen bleibt `pipeline._notice_subject`.
  (e) `build_raw_mail` wirft grundsätzlich nicht: Jeder Header-Zugriff ist abgesichert, weil
  eine hier scheiternde Mail den Fail-closed-Pfad nie erreichen und damit still verloren
  gehen würde (I6/F-OPS-3).
- Alternativen: Port 143 zulassen und auf STARTTLS umschalten (zweiter Transportpfad, den
  ein MITM herabstufen kann — widerspricht SECURITY §6); die Header-Rohwerte ungeprüft
  durchreichen (verlagert das Problem in WP3/WP6 und macht `RawMail` unzuverlässig); bei
  kaputten Headern eine Exception werfen (stiller Mailverlust).
- Konsequenzen: Eine Fehlkonfiguration auf Klartext-IMAP scheitert früh und verständlich.
  Die „Unbekannt"-Konventionen sind ab jetzt Teil des `RawMail`-Vertrags und gehören nach
  ARCHITECTURE §3; nachfolgende WPs müssen leeres `from_domain` als „Domain unbekannt"
  behandeln, nicht als Domain.

## ADR-021: Anthropic- und OpenAI-Zugriff direkt über httpx, kein Provider-SDK
- Status: accepted
- WP / Datum: WP4, 2026-08-28
- Kontext: WP4 braucht Zugriff auf die Anthropic-Messages-API und auf OpenAI-kompatible
  chat/completions-Endpunkte. Es gibt offizielle SDKs (`anthropic`, `openai`), die Auth,
  Retries und Typisierung mitbringen.
- Entscheidung: Beide Adapter sprechen direkt HTTP über das ohnehin vorhandene `httpx`. Der
  genutzte API-Ausschnitt ist ein einziger POST mit vier Feldern; die gemeinsame Retry-/
  Timeout-Mechanik liegt in `llm/_http.py` und ist damit für beide Provider identisch und
  testbar.
- Alternativen: Das Anthropic-SDK wurde geprüft und **verworfen**. Es wäre eine weitere
  Laufzeit-Dependency (NF-1: ≤ 8 Pakete, NF-2: ADR-Pflicht) und bringt eine vollständige
  Tool-Use-/Agent-Oberfläche mit (`tools`, `tool_runner`, `mcp_servers`, `container`,
  Managed Agents), die wegen I2 aktiv vermieden werden müsste — ein Sicherheitsmerkmal, das
  nur durch Disziplin statt durch Abwesenheit von Code gehalten würde. Ein zweites SDK
  (`openai`) hätte dieselben Nachteile verdoppelt. Ein weiterer Punkt: mit zwei SDKs hätten
  die Provider unterschiedliche Retry-Semantik (SDK-Default 2 Retries inkl. 408/409), was
  der Politik aus PLAN.md WP4 widerspricht.
- Konsequenzen: Wir tragen die Anpassung an API-Änderungen selbst (Header
  `anthropic-version` ist im Code fixiert und muss bei Bedarf bewusst gehoben werden). Dafür
  ist per grep prüfbar (WP12), dass kein Codepfad Tools ans Modell reicht, und beide
  Provider verhalten sich bei 429/5xx/Timeout garantiert gleich.

## ADR-022: `temperature` ist optional und wird nur bei explizitem Wert gesendet
- Status: accepted
- WP / Datum: WP4, 2026-08-28
- Kontext: PLAN.md WP4 spezifiziert `complete(system, user, *, max_tokens: int,
  temperature: float) -> str`. Aktuelle Anthropic-Modelle (Opus 4.7/4.8, Sonnet 5) sowie
  Claude Fable 5 lehnen die Sampling-Parameter `temperature`/`top_p`/`top_k` mit HTTP 400
  ab; das Feld ist dort entfernt.
- Entscheidung: Die Signatur lautet `temperature: float | None = None`. `None` bedeutet
  „Feld wird nicht in den Request-Körper aufgenommen", der Provider-Default gilt. Beide
  Adapter fügen `temperature` nur ein, wenn ein Wert übergeben wurde. Jeder plankonforme
  Aufruf mit einem float funktioniert unverändert; die Signatur ist eine Obermenge der
  geplanten.
- Alternativen: Signatur wie geplant lassen und immer senden — MailDigest wäre mit genau den
  Modellen unbenutzbar, für die es gedacht ist (HTTP 400 auf jeden Aufruf). Ein
  Config-Schalter `send_temperature` — zusätzliche Config-Fläche für einen Wert, den wir
  ohnehin nie brauchen (Zusammenfassen und Phishing-Prüfung profitieren nicht von
  Sampling-Tuning). Provider-spezifische Sonderlogik („bei Modell X weglassen") — bräuchte
  eine pflegebedürftige Modell-Liste im Code, genau das, was ARCHITECTURE §5 mit „kein
  hartkodierter Default" vermeiden will.
- Konsequenzen: ARCHITECTURE §2 ist entsprechend präzisiert; PLAN.md WP4 bleibt an dieser
  Stelle stale (PLAN ist rangniedriger als ARCHITECTURE und wird nicht rückwirkend
  umgeschrieben). WP5/WP6 rufen ohne `temperature` auf, solange kein konkreter Bedarf
  entsteht.

## ADR-023: Retry-Politik der LLM-Schicht — nur 429/5xx, Timeouts nicht wiederholen
- Status: accepted
- WP / Datum: WP4, 2026-08-28
- Kontext: PLAN.md WP4 verlangt „Retries mit Backoff bei 429/5xx (max. 3), Timeout 60 s".
  Offen war, ob auch Timeouts und Verbindungsfehler wiederholt werden und wie `Retry-After`
  behandelt wird.
- Entscheidung: Wiederholt werden ausschließlich HTTP 429 und 5xx, insgesamt drei Versuche.
  Wartezeit ist exponentiell (1 s, 2 s, Deckel 30 s); nennt der Server ein `Retry-After` in
  Sekunden, gilt dieser Wert (ebenfalls gedeckelt) — HTTP-Datumsformate werden ignoriert
  statt riskant geparst. Timeouts werden **nicht** wiederholt: Drei 60-s-Timeouts
  blockierten die Pipeline drei Minuten pro Mail; die Wiederholung ganzer Stufen ist laut
  ARCHITECTURE §6 Sache von WP8. Nicht wiederholbare Antworten (400/401/404) brechen sofort
  ab. Die Wartefunktion ist injizierbar, damit Tests das Backoff prüfen können, ohne zu
  warten.
- Alternativen: SDK-typische Politik (auch 408/409 und Verbindungsfehler wiederholen) —
  weiter gefasst als der Plan und bei 409 sinnlos. Jitter im Backoff — bei einem
  Einzelprozess ohne Thundering-Herd-Problem unnötige Komplexität und schlechter testbar.
- Konsequenzen: Ein transienter Verbindungsabbruch kostet die Mail einen Pipeline-Durchlauf
  statt eines stillen Retrys — akzeptabel, weil WP8 ohnehin drei Stufen-Versuche vorsieht
  und das Ergebnis fail-closed bleibt.

## ADR-024: Schema-Reparatur ohne Echo der verworfenen Modellantwort
- Status: accepted
- WP / Datum: WP4, 2026-08-28
- Kontext: `complete_json` bekommt laut PLAN.md WP4 genau einen Reparatur-Retry „mit
  Fehlerhinweis". Üblich wäre, dem Modell seine ungültige Antwort samt Fehlermeldung
  zurückzuspiegeln. Die Antwort ist jedoch untrusted (SECURITY §2) und kann Mail-Inhalt
  inklusive Injection-Text enthalten.
- Entscheidung: Der Reparaturhinweis wird an den **System-Prompt** angehängt (klar
  gelabelter Block „KORREKTUR (vom Programm …)"), nicht an den User-Prompt — der enthält den
  delimitierten Mail-Datenblock, und ihn zu verlängern würde die Trennung aus I8 aufweichen.
  Der Hinweis enthält nur code-erzeugte Information: Feldpfade und pydantic-Fehler*typen*
  (nicht die beanstandeten Werte, die unter `error['input']` stünden) sowie das JSON-Schema
  aus `model_json_schema()`. Die verworfene Antwort wird nicht zurückgespiegelt. Auch die
  `LLMInvalidResponse`-Meldung nennt nur Schemanamen und Ursachenkategorie.
- Alternativen: Antwort + Fehlertext zurückspiegeln (Standardvorgehen, aber ein zweiter
  Injection-Kanal und ein Weg, Mail-Inhalt in Logs zu tragen — I5/T1). Structured-Output-/
  JSON-Mode-Parameter der Provider nutzen (nicht bei allen OpenAI-kompatiblen/lokalen
  Servern verfügbar, würde die Provider-Gleichwertigkeit brechen).
- Konsequenzen: Die Reparaturquote ist etwas niedriger als mit Echo, weil das Modell seinen
  konkreten Fehler nicht sieht — dafür ist der zweite Aufruf genauso hart abgeschottet wie
  der erste. Scheitert die Reparatur, greift ohnehin fail-closed (I6).

## ADR-025: Provider-Factory mit Rollen-Parameter statt zweier Bauwege
- Status: accepted
- WP / Datum: WP4, 2026-08-28
- Kontext: `[llm.critic]` ist laut ARCHITECTURE §5 ein Override mit Vererbung. WP5 und WP6
  brauchen je einen Provider, ohne HTTP-Details zu kennen.
- Entscheidung: Eine Funktion `build_provider(config, role)` mit
  `LLMRole = Literal["summarizer", "critic"]`; die Rolle wählt zwischen den `[llm]`-Werten
  und den WP1-Helpern `critic_provider()`/`critic_model()`/`critic_base_url()`. Ergänzend
  `max_tokens_for(config, role)`, weil das Token-Limit zum Aufruf gehört, nicht zum
  Provider-Objekt (ein Provider kann mit verschiedenen Limits benutzt werden). Fehlender
  API-Key bei Provider `anthropic` erzeugt eine `ConfigError` (die Klasse aus `config.py`,
  damit die Setup-UX in WP9 nur einen Fehlertyp behandeln muss) mit Nennung der
  Umgebungsvariable, nie eines Wertes.
- Alternativen: Zwei Funktionen `build_summarizer_provider`/`build_critic_provider`
  (Duplikat, eine dritte Rolle kostet eine dritte Funktion). Ein Dataclass-Rückgabewert
  `LLMSetup(provider, max_tokens)` (bindet Limit und Provider aneinander, obwohl sie
  unterschiedliche Lebensdauer haben).
- Konsequenzen: Der API-Key liegt weiterhin nur einmal unter `[llm]` und gilt für beide
  Rollen; bei gemischten Providern (Kritiker auf lokalem Server) ist das unproblematisch,
  bei zwei verschiedenen Cloud-Providern wäre ein `[llm.critic] api_key` nötig — als
  bekannte Grenze in ARCHITECTURE §5 dokumentiert, nicht in WP4 gelöst (config.py gehört
  WP1).
