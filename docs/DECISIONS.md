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
- Nachtrag (WP9, 2026-09-02): `README.md` existiert jetzt; `readme = "README.md"` ist in
  `[project]` ergänzt. Dazu kam `[project.scripts] maildigest = "maildigest.cli:run_cli"`
  (ADR-052). Die mitgelieferte Beispielmail liegt unter `src/maildigest/data/selftest.eml`
  und ist damit Teil des Wheels — hatchling nimmt alle Dateien des Paketverzeichnisses auf.

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

## ADR-026: Eigene Magic-Bytes-Tabelle, Offset 0, „Deklaration UND Inhalt"-Regel
- Status: accepted
- WP / Datum: WP3, 2026-08-28
- Kontext: F-SEC-4/T6 verlangen, dass dem deklarierten MIME-Typ nie geglaubt wird, ohne
  eine neue Dependency (`python-magic`/`filetype`) einzuführen. Offen war, wie streng die
  Prüfung ist und ob erkannter Inhalt eine falsche Deklaration „korrigieren" darf.
- Entscheidung: Kleine eigene Signaturtabelle in `sanitize/attachments.py` (~30 Präfixe:
  Executables, Archive, OLE2, Bilder, RTF, `#!`-Skripte, SQLite, WASM, PDF), geprüft
  **strikt an Offset 0**. Verarbeitet wird nur bei Deklaration ∈ Allowlist **und**
  passendem Inhalt: PDF braucht `%PDF-` an Offset 0; text/plain und text/html dürfen
  keine bekannte Binärsignatur tragen und müssen eine Text-Heuristik bestehen (kein
  NUL-Byte, < 5 % Steuerbytes in 8-KB-Stichprobe). Jeder Widerspruch ⇒
  `detected_kind="mismatch"` ⇒ nie verarbeitet. Inhalt, der wie ein Allowlist-Format
  aussieht, aber anders deklariert ist, wird nie hochgestuft (`unknown`).
- Alternativen: `%PDF-` in den ersten 1024 Bytes suchen (tolerant wie manche Viewer,
  aber ein Polyglott-Einfallstor); Erkennung rein nach Inhalt statt Deklaration
  (ein als `octet-stream` deklarierter PDF würde dann geöffnet — mehr Angriffsfläche
  ohne Nutzerwert); `python-magic` (libmagic-Bindung: neue Dependency, riesige, extern
  gepflegte Signaturbasis, NF-1/NF-2).
- Konsequenzen: Legitime PDFs mit Vorspann-Bytes werden abgelehnt (selten; erscheinen
  als „nicht verarbeitet" — fail-safe). Die Tabelle muss nicht vollständig sein: Sie
  falsifiziert nur Allowlist-Deklarationen, Unbekanntes ist ohnehin geblockt.

## ADR-027: Hidden-Text-Heuristik und Tag-Strip auch in Klartext — Über-Entfernung ist ok
- Status: accepted
- WP / Datum: WP3, 2026-08-28
- Kontext: T2 (versteckter Injection-Text) ist nur heuristisch erkennbar; zugleich
  verlangt das WP3-Akzeptanzkriterium „kein HTML-Tag im Output", obwohl ein Angreifer
  `<script>` auch wörtlich in einen text/plain-Body schreiben kann.
- Entscheidung: (a) Als versteckt gilt ein Element mit `display:none`,
  `visibility:hidden|collapse`, `opacity:0`, `font-size:0` (0/0px/0pt/0em/0%),
  `hidden`-Attribut oder weißer Schriftfarbe (#fff/#ffffff/white/rgb(255,255,255)) ohne
  eigenen nicht-weißen Hintergrund — Mail-Hintergründe sind praktisch immer weiß.
  Entfernte Elemente mit Textinhalt werden gezählt (`hidden_text_removed`).
  (b) Nach Link-Scrub werden HTML-Tag-artige Sequenzen (`</?[A-Za-z]…>`) auch in
  Klartext-Teilen durch Leerzeichen ersetzt. (c) Beides ist bewusst überschießend:
  Ein legitimes `Name <adresse@example>` im Fließtext oder weißer Text auf dunklem
  Inline-Bild geht verloren — Informationsverlust ist der akzeptierte Preis dafür, dass
  kein Tag und möglichst wenig unsichtbarer Text das LLM bzw. den Nutzer erreicht.
- Alternativen: CSS vollständig kaskadiert auswerten (praktisch ein Browser-Engine-
  Nachbau, riesige Angriffsfläche); Tags nur in HTML-Teilen entfernen (verfehlt das
  Akzeptanzkriterium für text/plain-Angriffe); gar keine Weiß-auf-Weiß-Heuristik
  (T2-Standardtrick bliebe unerkannt).
- Konsequenzen: `hidden_text_removed` ist ein Kritiker-Signal (F-CRIT-3). Bekannte
  Lücken (dokumentiert, best effort): CSS-Klassen aus `<style>`-Blöcken, `position:
  absolute; left:-9999px`, Farbwerte wie `#fffffe` werden nicht erkannt.

## ADR-028: Link-Erkennung in sechs Pässen mit Platzhaltern; enge Bare-Domain-Politik
- Status: accepted
- WP / Datum: WP3, 2026-08-28
- Kontext: I3/T3 verlangen, dass keine URL die Sanitize-Stufe übersteht, inklusive
  Obfuskationen (`hxxp`, `(.)`, `[.]`, Leerzeichen-Einschub, `%68ttp`). Naive einzelne
  Regexe übersehen Varianten oder springen auf die eigenen `[Link #n: domain]`-Marker an.
- Entscheidung: `links.LinkCollector.scrub` läuft in sechs Pässen (Schema-URLs inkl.
  obfuskiertem/percent-kodiertem Schema und ftp, `mailto:`, `tel:`, `www.`-Domains,
  Domains mit obfuskierten Punkten, nackte Domains). Jeder Fund wird sofort durch einen
  `\x00`-Platzhalter ersetzt (Eingabetext ist durch `unicode_clean` garantiert NUL-frei)
  und erst am Ende zum Marker aufgelöst — spätere Pässe können Marker-Domains nicht
  erneut treffen. Nackte Domains werden nur mit eng gesetzten Punkten und alphabetischer
  TLD erkannt; ~70 bekannte Datei-Endungen (`rechnung.pdf`) sind ausgenommen.
  Leerzeichen-Einschub wird nur in Schema-/www-/Obfuskations-Kontext akzeptiert, nie bei
  nackten Domains (sonst würde „usw. der" zur Domain). Defang-Format bricht Schema,
  Trenner und Punkte (`hxxps[:]//evil[.]com`), damit auch die optionale Fußnote keinem
  Auto-Linkifier zum Opfer fällt; `www.`-Präfixe werden im Marker gestrippt.
  Host-Extraktion nimmt den Teil hinter dem letzten `@` der Authority (Userinfo-Trick)
  und dekodiert Punycode für Kennzeichnung und Mixed-Script-Prüfung.
- Alternativen: Ein einziger „Super-Regex" (unwartbar, Marker-Reentranz ungelöst);
  tld-Liste der IANA einbetten für Bare-Domains (Pflegeaufwand, NF-1); Bare-Domains gar
  nicht erkennen (Phishing nennt Domains oft ohne Schema).
- Konsequenzen: Bekannte, dokumentierte Lücken: `.zip`/`.js` existieren auch als echte
  TLDs — ohne Schema werden solche Nennungen als Dateiname gewertet (mit Schema immer
  erkannt; ohne Schema nicht klickbar, I3 bleibt gewahrt). Nach „URL. kleinwort" kann
  der Spaced-Dot-Pass ein Folgewort verschlucken (Über-Entfernung, fail-safe).
  Die Marker-Nummerierung folgt der Pass-Reihenfolge, nicht zwingend der Textreihenfolge.

## ADR-029: PDF-Subprozess über `python -m`, stdin/stdout, RLIMIT_AS als Code-Konstante
- Status: accepted
- WP / Datum: WP3, 2026-08-28
- Kontext: I7 verlangt Zeit-, Speicher- und Größenlimits für die PDF-Extraktion. Offen
  war die Mechanik (multiprocessing vs. eigener Prozess) und wo das Speicherlimit lebt.
- Entscheidung: `extract_pdf.extract_pdf_text` startet `sys.executable -m
  maildigest.sanitize.extract_pdf` (derselbe Interpreter ⇒ dasselbe venv), schreibt die
  PDF-Bytes auf stdin und liest gekürzten UTF-8-Text von stdout. Das Kind setzt **vor**
  dem pdfminer-Import `RLIMIT_AS` (512 MB, Code-Konstante — der Wert schützt den Host,
  nicht den Nutzerkomfort, darum kein Config-Feld) und kürzt den Output selbst; der
  Elternprozess erzwingt den Timeout über `subprocess.run(timeout=…)` (Kill inklusive)
  und prüft das Input-Limit vor dem Start. Jeder Fehler (Nicht-Null-Exit, Timeout,
  OSError) ⇒ `None` ⇒ Anhang unverarbeitet. stderr wird verworfen und nie geloggt
  (pdfminer-Meldungen können Mail-Inhalt enthalten, I5); auch `SystemExit(0)` des Kindes
  wird nicht vom Fehler-Handler verschluckt (Exception-Filter ist `Exception`).
- Alternativen: `multiprocessing`/`fork` (erbt den kompletten Eltern-Speicher inkl.
  eventueller Secrets im Config-Objekt; RLIMIT-Setzen nach fork ist fehleranfällig);
  Extraktion in-process mit Signal-Timeout (kein Schutz gegen Parser-Crash/Memory, T5);
  konfigurierbares RSS-Limit (unnötige Config-Fläche).
- Konsequenzen: Pro PDF fallen ~100-300 ms Interpreter-Start an — bei einem
  Mail-Digest irrelevant. Auf Nicht-POSIX-Plattformen ohne `resource`-Modul bleibt nur
  Timeout+Größenlimit (best effort, im Code als solches markiert).

## ADR-030: MIME-Baum-Politik — rfc822 nie betreten, Tiefenlimit als Metadatum, Body-Aggregation
- Status: accepted
- WP / Datum: WP3, 2026-08-28
- Kontext: ARCHITECTURE §3 lässt offen, wie mehrere Body-Teile, eingebettete Mails und
  zu tiefe MIME-Bäume konkret behandelt werden; SECURITY §4 gibt nur die Grundsätze vor.
- Entscheidung: (a) Der MIME-Baum wird manuell gelaufen (compat32-Parser, jeder
  Header-Zugriff einzeln abgesichert); `message/*`-Teile werden **nie** betreten,
  sondern als ein Anhang-Metadatum erfasst (T13) — auch nicht zur Body-Suche.
  (b) Teile, die tiefer als `limits.max_mime_depth` verschachtelt sind, werden nicht
  geparst, sondern als ein
  synthetisches Metadatum `(mime-tiefe ueberschritten)` gezählt (T10) — sichtbar statt
  still verworfen. (c) Body = alle Inline-`text/plain`-Teile, sonst alle
  Inline-`text/html`-Teile; „Inline" = keine Attachment-Disposition und kein Dateiname.
  (d) `text/html` mit Dateiname/Attachment-Disposition ist Smuggling-Vektor und bleibt
  Metadatum (`processed=False`), obwohl der Typ auf der Allowlist steht.
  (e) Verarbeitete Anhänge erhalten immer einen `AttachmentInfo`-Eintrag; die
  Metadatenliste ist auf 100 Einträge gedeckelt (Teile-Bomben), `blocked_attachments`
  zählt unabhängig davon korrekt.
- Alternativen: `walk()` der stdlib (steigt in rfc822 hinein — genau der T13-Bypass);
  nur den ersten Text-Teil als Body (verliert legitime multipart/mixed-Textfolgen);
  policy.default-Parser (schöneres API, wirft aber bei bestimmten defekten Headern beim
  Zugriff — jede solche Exception wäre ein vermeidbarer Fail-closed-Fall).
- Konsequenzen: Eine Mail, die nur aus einer eingebetteten Mail besteht, hat leeren
  Body und ein Anhang-Metadatum — WP7 meldet das als „nicht verarbeiteter Anhang".
  Tiefe, legitime Newsletter-Verschachtelungen (> 10 Ebenen) verlieren Inhalt; das
  Limit ist per Config anhebbar.

## ADR-031: Prompt-Aufbau des Summarizers — Sicherheitsregeln stehen nach den Custom-Instructions
- Status: accepted
- WP / Datum: WP5, 2026-09-02
- Kontext: Der System-Prompt enthält zwei Quellen mit unterschiedlichem Vertrauen: fest im
  Code stehende Sicherheitsregeln und die semi-trusted Custom-Instructions aus
  `[summarizer] instructions` (I8). Modelle gewichten spätere Anweisungen im selben Prompt
  tendenziell stärker; die Reihenfolge ist damit sicherheitsrelevant, nicht kosmetisch.
- Entscheidung: `llm/prompts.py` baut den System-Prompt in fester Reihenfolge Rolle →
  Nutzer-Vorgaben (eigener Block zwischen `--- Anfang/Ende der Nutzer-Vorgaben ---`, als
  semi-vertrauenswürdig gelabelt, auf 2000 Zeichen gedeckelt) → unüberschreibbare
  Sicherheitsregeln inkl. Sprache/Länge/Wichtigkeit/Ausgabeformat. Der Regelblock ist
  explizit als „höchste Priorität, steht bewusst nach den Nutzer-Vorgaben" ausgezeichnet.
  Alle Wortlaute liegen ausschließlich in `llm/prompts.py` und tragen mit `PROMPT_VERSION`
  eine Version im Schema `wp<NR>/<YYYY-MM-DD>[.n]`, die bei jeder inhaltlichen Änderung
  erhöht wird.
- Alternativen: Custom-Instructions in die User-Message (verletzt I8 — sie sind
  Konfiguration, keine Daten); Custom-Instructions nach den Regeln (widerspricht
  docs/SECURITY.md §5 letzter Absatz); Instructions ungedeckelt übernehmen (ein
  versehentlich riesiger Config-Wert würde den Regelblock aus dem effektiven Kontext
  drängen).
- Konsequenzen: Nutzer können Stil, Fokus und Wichtigkeitspolitik steuern, aber die Regeln
  nicht lockern. Die Zeichengrenze kann sehr ausführliche Vorgaben kürzen (sichtbar über
  den Marker `[…gekürzt]`). Prompt-Änderungen sind reviewbar und über die Version
  referenzierbar.

## ADR-032: Zufällige Datenblock-Marker mit injizierbarer Zufallsquelle
- Status: accepted
- WP / Datum: WP5, 2026-09-02
- Kontext: docs/SECURITY.md §5 Punkt 1 verlangt pro Aufruf zufällige Delimiter, damit der
  Mail-Text den Datenblock nicht vorzeitig „schließen" und danach als Instruktion
  weiterschreiben kann. Gleichzeitig müssen Tests den Prompt-Aufbau exakt prüfen können.
- Entscheidung: `prompts.default_token_source()` zieht 12 Byte aus `secrets` (96 Bit,
  CSPRNG) und baut daraus `<<<MAILDIGEST-UNTRUSTED-DATA {token}>>>` /
  `<<<MAILDIGEST-END-UNTRUSTED-DATA {token}>>>`. `SummarizerAgent` nimmt die Quelle als
  Konstruktor-Parameter `token_source` entgegen (Default: der CSPRNG); Tests injizieren
  eine feste Kennung. Der System-Prompt nennt die konkreten Marker, damit das Modell weiß,
  was Daten sind. Zusätzlich wird eine im Mail-Text auftauchende Kennung vor dem Einbau
  durch `MARKER-ENTFERNT` ersetzt — Tiefenverteidigung für den Fall, dass die Kennung
  anderswo leckt.
- Alternativen: Fester Delimiter (im Angreifermodell aus SECURITY §1 kennt der Angreifer
  die Prompts und kann ihn nachbauen); Zufall aus `random` (nicht kryptografisch);
  Zufallsquelle als Modul-Global monkeypatchen (unsichtbare Kopplung, schlecht in
  parallelen Tests).
- Konsequenzen: Prompt-Caching über Mails hinweg ist für den System-Prompt nicht möglich,
  weil er die Kennung enthält — akzeptiert, Sicherheit vor Kosten. Eine falsch injizierte
  Quelle in Produktivcode wäre eine echte Schwächung; deshalb ist der Parameter
  keyword-only und nur in Tests gesetzt.

## ADR-033: Deterministische Nachkontrolle redigiert wortweise und flaggt jeden Fund
- Status: accepted
- WP / Datum: WP5, 2026-09-02
- Kontext: Die LLM-Ausgabe ist untrusted (I4). Ein reines Ersetzen der Fundstelle („://")
  ließe den gefährlichen Rest stehen (`https://boese.example/x` →
  `httpsboese.example/x`); ein zu breites Muster würde die legitimen Sanitizer-Marker
  `[Link #n: domain.tld]` zerstören, die laut I3 erlaubt sind.
- Entscheidung: `agents/summarizer.scrub_text` arbeitet in drei Pässen: (1) Struktur-Muster
  (Markdown-Link/-Bild, Markdown-Referenzdefinition, HTML-Tag, numerische HTML-Entity)
  werden als Ganzes durch `[entfernt]` ersetzt; (2) URL-Muster (`schema://`, `hxxp`,
  `www.`, `mailto:`, `tel:`, `(.)`/`[.]`/`(dot)`, `domain.tld/pfad`) ersetzen das komplette
  umgebende Nicht-Whitespace-Wort; (3) alle Unicode-`C*`-Zeichen außer Tab/Zeilenumbruch
  fallen weg. Nackte Domains **ohne** Pfad werden bewusst nicht angetastet. Jeder Fund in
  irgendeinem Feld setzt `injection_suspected = true`; ein vom Modell gesetztes Flag wird
  nie gelöscht. Anschließend werden Headline auf eine Zeile und ≤ 100 Zeichen gezwungen und
  leere Felder aus Sanitizer-Werten aufgefüllt.
- Alternativen: Feld bei Fund komplett verwerfen (Nutzer verliert die Information, obwohl
  der Rest brauchbar ist); nur flaggen ohne Säubern (widerspricht I3/PLAN WP5); auch nackte
  Domains redigieren (zerstört die Marker und damit die einzige verbliebene
  Herkunftsinformation).
- Konsequenzen: Der Nutzer sieht `[entfernt]`-Lücken statt stiller Kürzungen.
  Falsch-Positive sind möglich (ein Text über „www." als Wort), kosten aber nur ein Flag
  und ein Wort. **Bekannte Grenze (Finalisierungs-Review WP5/WP7, 2026-09-02):** Diese
  Schicht normalisiert **nicht** nach NFKC. Fullwidth-Schreibweisen (`ｈｔｔｐｓ://…`),
  nackte IPv4-Adressen und der ideographische Punkt (`boese。example`) passieren sie
  unverändert und **ohne** Flag. Das ist tolerierbar, weil der Output-Sanitizer (ADR-036)
  vor der Zustellung NFKC-normalisiert und diese Formen sicher entschärft — es ist aber
  ein realer Unterschied zwischen den beiden Schichten und darf nicht als „doppelte
  Absicherung derselben Muster" gelesen werden.

## ADR-034: Mails ohne darstellbaren Text gehen trotzdem ans LLM; Metadaten-Text nur als Fallback
- Status: accepted
- WP / Datum: WP5, 2026-09-02
- Kontext: Nach der Allowlist-Politik (SECURITY §4) kann eine Mail komplett ohne
  verwertbaren Text ankommen — etwa eine Rechnung, die nur als `.docx` anhängt. Der
  WP3-Bericht (g)5 verlangt trotzdem eine brauchbare Zusammenfassung aus Metadaten. Die
  Frage war, ob der Agent das LLM in diesem Fall überspringt.
- Entscheidung: Der Aufruf findet statt. Der Datenblock markiert den fehlenden Text
  explizit („(kein darstellbarer Text vorhanden)") und listet die geblockten Anhänge mit
  Name, deklariertem MIME-Typ und Größe. Betreff, Absender und Anhangsnamen bleiben damit
  als Signale für `importance` und `category` erhalten. Erst wenn `summary_text` nach der
  Nachkontrolle leer ist, greift der rein deterministische Ersatztext
  `describe_without_body()` („Mail ohne darstellbaren Inhalt, 2 geblockte Anhänge:
  rechnung.docx (34 KB), setup.exe (1,2 MB)."), der ausschließlich aus Sanitizer-Werten
  gebaut wird. Ergänzend werden Einträge in `attachment_summaries` verworfen, deren
  Schlüssel nicht in `SanitizedMail.attachment_texts` steht — über geblockte oder erfundene
  Anhänge kann das Modell keine Inhalte behaupten.
- Alternativen: LLM überspringen und rein deterministisch antworten (spart einen Aufruf,
  verschenkt aber die Wichtigkeitsbewertung aus dem Betreff — genau die Mail „Mahnung" mit
  nur einem `.docx` würde als `low` einsortiert); die Metadatenzeile immer zusätzlich
  anhängen (Dopplung zur Anhangs-Zeile des WP7-Nachrichtenformats).
- Konsequenzen: Ein LLM-Aufruf auch für inhaltsleere Mails (Kosten akzeptiert). Der
  Ersatztext ist der einzige Summary-Text, der nicht vom Modell stammt — er ist deshalb
  nicht scrub-pflichtig, weil alle Bestandteile bereits durch den Sanitizer gegangen sind.

## ADR-035: Output-Sanitizer in zwei Stufen — Feld-Scrub plus unabhängiger Nachbrenner
- Status: accepted
- WP / Datum: WP7, 2026-09-02
- Kontext: Der Output-Sanitizer ist laut SECURITY §5 die letzte Schicht vor dem Nutzer
  (I3/I4). Er muss (a) bereits vom WP3-Sanitizer erzeugte sichere Formen
  (`[Link #n: domain]`, `[Tel #n]`, `hxxps[:]//evil[.]com`) unverändert durchreichen und
  (b) in jedem anderen Textteil jede — auch rekonstruierte oder kodierte — Adresse
  entschärfen. Beides zugleich in einer Regex-Schicht zu lösen führt zu einem
  komplizierten Muster, dessen Lücken niemand mehr überblickt.
- Entscheidung: Zwei Stufen. (1) `scrub_field` arbeitet je Feld: HTML-Entities bis Fixpunkt
  auflösen (max. 3 Runden), `unicode_clean.clean_text` (NFKC + „C*"-Entfernung), optionale
  Feldkürzung **vor** der Link-Erkennung, Tag-Strip inkl. Entfernen verbliebener `<`/`>`,
  dann Segmentierung: sichere Formen werden per Regex erkannt und wörtlich übernommen, alle
  übrigen Segmente laufen durch Markup-Neutralisierung und
  `links.LinkCollector.scrub` (Wiederverwendung der WP3-Erkennung mit allen
  Obfuskationspässen, ein Collector pro Nachricht ⇒ durchlaufende Marker-Nummerierung).
  (2) `final_guard` läuft über die **fertige** Nachricht und ist bewusst trivial: lebende
  Schemata brechen, `www.` brechen, `<`/`>` entfernen, `](` auftrennen, domain-/IPv4-artige
  Token defangen. `DigestMessage.parts` entsteht ausschließlich über diesen Weg
  (`DigestComposer._finalize`).
- Alternativen: nur eine Regex-Schicht (Segmentierungslücken wären unmittelbar
  I3-Verletzungen — konkret rutschte in einem Zwischenstand `http://evil.com/x[.]y` durch,
  weil der `[.]`-Schutz tokenweit griff; der Nachbrenner fängt genau das ab); Markup
  escapen statt entfernen (setzt einen Rendering-Modus voraus, den wir bewusst nicht
  wählen); den WP3-Sanitizer erneut über die ganze Nachricht laufen lassen (würde eigene
  Marker verschachteln und neu nummerieren).
- Konsequenzen: Die Sicherheitsaussage hängt nicht mehr allein an der
  Segmentierungs-Regex. Preis ist eine gewisse Über-Entfernung (Winkelklammern in „5 < 7",
  ein Punkt in „usw.Dann" wird gebrochen) — fail-safe und dokumentiert. Die Property „kein
  Teil enthält eine klickbare Adresse oder Markup" ist als Testfunktion `assert_safe` einmal
  formuliert und wird über 25 Angriffs-Payloads sowie 300 zufällige Kombinationen geprüft.

## ADR-036: Domain-Punkte werden im Output gebrochen — auch in Markern und Dateinamen
- Status: accepted
- WP / Datum: WP7, 2026-09-02 (im Finalisierungs-Review am selben Tag erweitert)
- Kontext: I3 erlaubt ausdrücklich „bloßer Domain-Name in Textform", und ARCHITECTURE §7
  zeigte genau das (`Von: rechnung@stadtwerke-x.de`, `[Link #n: evil.com]`). Telegram und
  Discord verlinken jedoch nackte Domains im Klartext automatisch (Autolinker) — aus dem
  „bloßen Domain-Namen" wird im Client wieder ein klickbares Ziel (T7). Dasselbe gilt für
  Dateinamen mit TLD-kollidierender Endung (`rechnung.zip`, `bericht.app`).
- Entscheidung: `final_guard` bricht in der fertigen Nachricht jeden Punkt eines
  domainartigen Tokens, dessen letzte Marke 2–24 alphabetische Zeichen hat, sowie
  IPv4-Adressen: `stadtwerke-x[.]de`, `[Link #1: evil[.]com]`, `rechnung[.]pdf`,
  `192[.]168[.]0[.]1`. Struktur und Nummerierung der WP3-Marker bleiben unangetastet —
  verändert wird nur die Punkt-Darstellung innerhalb der Domain. Zahlen (`3.14`), Kürzel mit
  einbuchstabiger „TLD" (`z.B.`) und bereits defangte Formen bleiben unberührt.
  **Ergänzung aus dem Finalisierungs-Review (2026-09-02):** (a) Gebrochen wird **jedes**
  lebende Schema mit `://`, nicht nur `http(s)`/`ftp(s)` — `tg://` ist in Telegram selbst
  ein anklickbarer Deep-Link, `steam://`/`file://` in anderen Clients ebenso; (b) für
  Schemata ohne `//` (`javascript:`, `data:`, `vbscript:`, `tg:`, `intent:`, `market:`,
  `smb:`) bleibt eine Namensliste unvermeidlich, weil `wort:wort` im Fließtext normal ist;
  (c) die Punkt-Varianten U+3002/U+FF61 (`boese。example`) werden vor jeder Prüfung auf `.`
  abgebildet — NFKC tut das nicht, IDN-fähige Clients behandeln sie aber als Label-Trenner.
- Alternativen: Domains unverändert lassen (verlässt sich darauf, dass der Nutzer nicht auf
  den Autolink tippt — genau das Risiko, das I3 ausschließen soll); nur Dateinamen mit
  riskanter Endung defangen (Pflegeliste, dieselbe Klasse Fehler wie eine Blocklist);
  Marker-Domains durch Platzhalter ersetzen (der Nutzer verlöre die für Phishing-Erkennung
  wichtigste Information).
- Konsequenzen: Die Nachricht weicht optisch vom früheren Beispiel in ARCHITECTURE §7 ab
  (dort jetzt angepasst). Lesbarkeit leidet minimal, dafür ist keine Zeichenkette der
  Nachricht mehr autolinkbar. Die Regel ist eine einzige Funktion und damit in WP12
  grep-/testbar. Die Namensliste unter (b) ist die einzige Blocklist im Sanitizer-Pfad und
  muss bei WP12 erneut geprüft werden.

## ADR-037: Markup wird entfernt statt escaped; `_` bleibt erhalten
- Status: accepted
- WP / Datum: WP7, 2026-09-02
- Kontext: Der Output-Sanitizer muss Markdown-/HTML-/Telegram-/Discord-Formatierungstricks
  aus jedem Feld entfernen (T7). Escaping setzt voraus, dass man den Rendering-Modus des
  Ziels kennt — Telegram bekommt laut ADR-006 gar keinen `parse_mode`, Discord rendert im
  `content` immer Markdown. Ein Escape-Zeichen im falschen Modus ist selbst sichtbarer Müll.
- Entscheidung: In untrusted Segmenten werden die Zeichen `` ` ``, `*`, `|`, `~`, `\`, `[`,
  `]` ersatzlos gelöscht; tag-artige Sequenzen und alle `<`/`>` fallen ohnehin. `[`/`]`
  fallen mit, damit weder ein Sanitizer-Marker gefälscht noch die Markdown-Link-Syntax
  `[text](ziel)` zusammengesetzt werden kann; die *echten* Marker sind vor dieser Löschung
  durch die Segmentierung geschützt. `_` bleibt erhalten: Es kann höchstens Kursivschrift
  erzeugen, nie ein Ziel, und seine Löschung würde Dateinamen und Bezeichner zerstören. Der
  Kürzungsmarker ist deshalb `…` statt `[…]`.
- Alternativen: HTML-`parse_mode` mit hartem Escaping (PLAN nennt es als Option; es öffnet
  einen Rendering-Modus, den wir sonst gar nicht brauchen — mehr Angriffsfläche für null
  Nutzen); alle Sonderzeichen löschen inkl. `_` und Klammern (Lesbarkeitsverlust ohne
  Sicherheitsgewinn, da ohne URL kein Ziel existiert).
- Konsequenzen: Kursivschrift durch `_` ist in Discord weiterhin möglich (kosmetisch). Der
  Nutzer sieht bei Angriffs-Mails leicht zerpflückten Text — gewollt: Er soll erkennen,
  dass etwas entfernt wurde.

## ADR-038: Eigene HTTP-Mechanik für Messenger statt Wiederverwendung von `llm/_http.py`
- Status: accepted
- WP / Datum: WP7, 2026-09-02
- Kontext: Telegram und Discord brauchen dieselbe Retry-/Backoff-/Timeout-Politik wie die
  LLM-Provider (nur 429/5xx, max. 3 Versuche, Backoff 1 s/2 s bzw. `Retry-After`, Timeouts
  nicht wiederholt — ADR-023). `llm/_http.post_json` implementiert das bereits. Es wirft
  aber `LLMTimeout`/`LLMRateLimited`/`LLMTransportError`, und `pipeline._ERROR_CLASSES`
  bildet genau diese Namen auf `llm_*`-Fehlerklassen ab.
- Entscheidung: `messenger/_http.request_json` als eigenständige, kleine Kopie der Politik
  mit `MessengerError` als einziger Fehlerklasse (bereits als `delivery_error` in der
  Pipeline-Tabelle vorgesehen). Zusätzlich unterstützt sie GET (Discord-Healthcheck) und
  Antworten ohne Körper (Discord antwortet auf Webhook-Posts mit `204`).
- Alternativen: `llm/_http` mit einem Exception-Mapping-Parameter generalisieren (Umbau an
  fremdem, bereits abgenommenem WP4-Code; die gemeinsame Abstraktion müsste die
  Fehlerdomäne beider Seiten kennen); Zustellfehler als LLM-Fehler durchreichen (die
  Metadaten-Notiz nennte dem Nutzer und dem State die falsche Ursache — I6/NF-5 wären
  formal erfüllt, praktisch aber irreführend).
- Konsequenzen: ~60 Zeilen bewusste Duplikation. Ändert sich die Retry-Politik, müssen zwei
  Stellen angefasst werden; beide Module verweisen im Docstring aufeinander, und WP12 kann
  den Abgleich prüfen.

## ADR-039: Signal-Adapter minimal — JSON-RPC über Unix-Socket, Zustellung an „Note to Self"
- Status: accepted
- WP / Datum: WP7, 2026-09-02
- Kontext: PLAN.md WP7 verlangt den Signal-Adapter „optional, hinter Feature-Flag, Aufwand
  begrenzen". Die Config (`[messenger.signal]`) kennt nur `enabled` und
  `signal_cli_socket` — es gibt kein Feld für eine Empfängernummer, und das Config-Schema
  gehört zu einem anderen Arbeitspaket.
- Entscheidung: Der Adapter spricht mit `signal-cli --daemon --socket <pfad>` über
  zeilengetrenntes JSON-RPC auf einem Unix-Domain-Socket (stdlib `socket`, keine neue
  Dependency; Verbindungsfabrik injizierbar ⇒ testbar ohne Daemon). Gesendet wird `send`
  mit `noteToSelf: true`, geprüft wird mit `version`. Kein Prozess-Management: Fehlt der
  Daemon, gibt es eine deutsche Meldung samt Startbefehl statt eines Startversuchs.
  Antwortmenge auf 1 MB gedeckelt.
- Alternativen: Empfängernummer als neues Config-Feld (Scope-Verletzung in diesem WP;
  Vorschlag für WP9 dokumentiert); signal-cli per Subprozess je Nachricht aufrufen
  (Startkosten pro Nachricht, kein Fehlerkanal, und wir starten grundsätzlich keine fremden
  Prozesse); Signal ganz weglassen (F-MSG-1 nennt es ausdrücklich als optionalen dritten
  Adapter).
- Konsequenzen: v0.1 stellt Signal-Nachrichten nur an das eigene Konto zu — für ein
  persönliches Mail-Digest der Normalfall. Eine Empfängernummer
  (`[messenger.signal] recipient`) ist in WP9 nachrüstbar, ohne den Adapter umzubauen (nur
  `params` ändern sich).

## ADR-040: Zeichenlimits je Messenger, Einzellimits je Feld, Split ohne Teil-Zähler
- Status: accepted
- WP / Datum: WP7, 2026-09-02
- Kontext: PLAN.md nennt nur Telegrams 4096-Zeichen-Limit. Discord akzeptiert im `content`
  eines Webhooks aber nur 2000 Zeichen; ein einzelnes entartetes LLM-Feld (Modell dreht
  durch, Injection erzeugt Endlostext) könnte außerdem eine Nachricht aus hundert Teilen
  erzeugen (T10).
- Entscheidung: (a) Limit-Tabelle je Adapter (`telegram` 4096, `discord` 2000, `signal`
  2000); unbekannte Namen bekommen das kleinste bekannte Limit. `DigestComposer.from_config`
  zieht das Limit aus `[messenger] active`. (b) Zusätzlich Einzellimits je untrusted Feld
  (Headline 120, Summary 3000, Anhang-Zusammenfassung 400, Kritiker-Grund 200, Anzeigename
  80, Domain 100, Dateiname 80 Zeichen; höchstens 10 gelistete Anhänge, Rest als „und N
  weitere", höchstens 5 Banner-Gründe) — Kürzung mit `…`-Marker. (c) Gesplittet wird an
  Zeilengrenzen, überlange Einzelzeilen bevorzugt am Leerzeichen, notfalls hart; die Teile
  bekommen **keinen** „(1/3)"-Zähler.
- Alternativen: nur das Telegram-Limit verwenden (Discord-Zustellung würde bei langen Mails
  hart abgelehnt); Felder unbegrenzt lassen und allein splitten (eine Mail könnte Dutzende
  Nachrichten erzeugen); Teil-Zähler anhängen (verändert den bereits sanitisierten Text nach
  dem Nachbrenner und kostet Platz im ohnehin knappen Limit — die Reihenfolge der Zustellung
  ist im Messenger ohnehin sichtbar).
- Konsequenzen: Sehr lange Zusammenfassungen werden sichtbar gekürzt statt gesplittet; das
  ist gewollt (der Nutzer soll nicht 10 Nachrichten pro Mail bekommen). Ein hart
  geschnittener Teil kann einen Marker kosmetisch zerreißen — nie klickbar.

## ADR-041: Kritiker bekommt Mail und Zusammenfassung in zwei getrennten Untrusted-Blöcken
- Status: accepted
- WP / Datum: WP6, 2026-09-02
- Kontext: Der Kritiker muss zwei Dinge gleichzeitig lesen, die beide untrusted sind: den
  sanitisierten Mail-Text (fremder Absender) und die `Summary` (Ausgabe eines Modells, das
  die Mail gelesen hat und von ihr übernommen worden sein kann, I4). Er muss sie aber
  auseinanderhalten können — der Prüfauftrag „stimmt die Zusammenfassung zur Mail?" ergibt
  sonst keinen Sinn.
- Entscheidung: Die User-Message enthält beide Quellen in je eigenen, benannten Blöcken
  mit **derselben** Aufruf-Kennung: `<<<MAILDIGEST-UNTRUSTED-DATA {token}>>>` (Mail, geteilt
  mit dem Summarizer über `prompts.block_markers`) und `<<<MAILDIGEST-UNTRUSTED-SUMMARY
  {token}>>>` (`prompts.summary_markers`). Beide Markerpaare stehen im System-Prompt und
  gelten dort ausdrücklich als Daten. Der Marker-Neutralisierer aus WP5
  (`prompts._neutralize_markers`) läuft über **beide** Blockinhalte, also auch über die
  Modellausgabe. Der Mail-Block wird mit demselben `_data_block()` gebaut wie im
  Summarizer — eine Quelle, ein Wortlaut.
- Alternativen: beides in einen Block (das Modell könnte Mail-Text und Zusammenfassung nicht
  sicher trennen, und ein Mail-Text könnte eine „Zusammenfassung" vortäuschen); zwei
  verschiedene Zufallskennungen (kein Sicherheitsgewinn — eine Kennung ist bereits
  unbekannt — bei doppeltem Prompt-Aufwand); die Zusammenfassung als vertrauenswürdig in
  die Programm-Fakten legen (widerspricht I4).
- Konsequenzen: Der Kritiker-Prompt ist länger als der des Summarizers (Mail + Summary).
  `PROMPT_VERSION` steht auf `wp6/2026-09-02`.

## ADR-042: Der Kritiker bekommt keine Custom-Instructions
- Status: accepted
- WP / Datum: WP6, 2026-09-02
- Kontext: `[summarizer] instructions` sind semi-trusted und dürfen laut I8/ADR-031 Stil,
  Fokus und Wichtigkeitspolitik des Summarizers steuern. Für den Kritiker stellt sich die
  Frage neu: Er ist laut docs/SECURITY.md §5 Punkt 5 die **unabhängige** zweite Instanz.
- Entscheidung: `CriticAgent` nimmt keinen `instructions`-Parameter entgegen, und
  `critic_system_prompt()` hat keinen Nutzer-Vorgaben-Block. Der Kritiker kennt strukturell
  nur `language` und `max_tokens`. Konfigurierbar bleibt allein das Modell über
  `[llm.critic]`.
- Alternativen: dieselben Instructions auch an den Kritiker geben (eine harmlos gemeinte
  Vorgabe wie „fasse dich kurz, keine Warnungen" würde die Phishing-Erkennung entschärfen —
  und wer die Config-Datei ändern kann, könnte den Schutz stillschweigend abschalten); ein
  eigener `[critic] instructions`-Abschnitt (dasselbe Risiko, nur explizit; ohne
  erkennbaren Nutzen — der Prüfauftrag ist nicht Geschmackssache).
- Konsequenzen: Nutzer können die Phishing-Prüfung nicht an eigene Gepflogenheiten anpassen
  (z. B. „Rechnungen von X sind immer echt"); Fehlalarme müssen über die Ausgabe ertragen
  oder in einem späteren WP über eine explizite, eng definierte Allowlist gelöst werden.
  Die Sprache der Verdict-Texte folgt weiterhin `[general] language`.

## ADR-043: Harte Code-Signale heben die Risikostufe an, senken sie nie
- Status: accepted
- WP / Datum: WP6, 2026-09-02
- Kontext: T9 verlangt, dass die deterministischen Signale (F-CRIT-3) vom Mail-Inhalt nicht
  beeinflussbar sind. Sie landen aber im Prompt — ein übernommenes oder schlicht
  nachlässiges Modell kann sie ignorieren und `phishing_risk = none` liefern. Umgekehrt
  wäre eine Code-Regel „Signal ⇒ high" ein Fehlalarm-Generator: Die Weiterleitung ins
  Spiegelpostfach bricht SPF und DKIM systematisch, und Mailinglisten setzen Reply-To
  routinemäßig um.
- Entscheidung: Jedes Signal trägt ein Attribut `hard`. `enforce_verdict_policy` hebt die
  Stufe bei einem harten Signal auf mindestens `low` an und ergänzt es als `risk_reasons`-
  Eintrag; die Stufe wird **nie** gesenkt und **nie** per Code auf `high` gesetzt. Hart ist
  derzeit ausschließlich `mixed_script` (Domain mit gemischten Schriftsystemen — eine
  Technik ohne legitime Verwendung). Punycode allein ist weich (deutsche Umlaut-Domains
  sind legitimes IDN), Auth-Fails sind weich (Weiterleitungseffekt, im Faktentext
  ausdrücklich benannt), Reply-To/Return-Path-Abweichung ist weich. Dasselbe Anheben auf
  `low` löst die Nachkontrolle aus, wenn sie in den Verdict-Texten etwas entfernen musste —
  das ist das Gegenstück zum `injection_suspected`-Flag, für das `CriticVerdict` kein Feld
  hat (das Schema ist in ARCHITECTURE §3 festgeschrieben und wird hier nicht erweitert).
  `summary_accurate` bleibt unangetastet: Der Code kann inhaltliche Richtigkeit nicht
  beurteilen, und ein Herabsetzen würde den Fail-closed-Pfad aus T8 aushebeln.
- Alternativen: Signale nur in den Prompt geben (ein übernommenes Modell könnte sie
  folgenlos ignorieren — T9 wäre nur auf dem Papier erfüllt); Code setzt `high` (Banner bei
  jeder weitergeleiteten Mail mit SPF-Bruch — Warnmüdigkeit, der teuerste aller Fehler);
  eine Punktetabelle über alle Signale (mehr Mechanik, mehr Kalibrierbedarf, ohne Daten
  nicht begründbar).
- Konsequenzen: Ein Homoglyphen-Absender erzeugt garantiert mindestens die Hinweiszeile
  „Kritiker: …", auch wenn das Modell schweigt. Ein `low` ohne Modellgrund bekommt einen
  neutralen Platzhalter, damit kein leeres Banner entsteht. Die Liste der harten Signale ist
  bewusst kurz und in WP12 erneut zu prüfen.

## ADR-044: Verdict-Nachkontrolle nutzt die Summarizer-Politik plus NFKC
- Status: accepted
- WP / Datum: WP6, 2026-09-02
- Kontext: Die Textfelder des `CriticVerdict` (`risk_reasons`, `notes`) landen im Banner und
  in der Hinweiszeile der Nachricht und sind genauso untrusted wie die `Summary` (I4). Die
  WP5-Nachkontrolle (`agents.summarizer.scrub_text`) deckt Markdown, HTML, Entities,
  URL-Obfuskationen und Unicode-`C*` bereits ab, normalisiert aber ausdrücklich nicht nach
  NFKC (ADR-033) — Fullwidth-Schreibweisen passieren sie.
- Entscheidung: `agents/critic.py` importiert `scrub_text` und ruft es auf dem
  **NFKC-normalisierten** Feld auf; eine durch die Normalisierung veränderte Zeichenfolge
  gilt selbst schon als Fund. Danach: eine Zeile erzwingen, harte Zeichenlimits
  (Grund 200, `notes` 500), Duplikate und Leereinträge verwerfen, Liste auf 5 Einträge
  kappen — Code-erzeugte Gründe stehen dabei **vor** den Modellgründen, damit die Kappung
  sie nicht verdrängt. Die Zahl 5 entspricht dem, was der Composer ohnehin anzeigt (ADR-040), und steht
  auch im Prompt.
- Alternativen: `scrub_text` in ein gemeinsames Modul ziehen (Umbau an fremdem, bereits
  reviewtem WP5-Code — nicht der Auftrag von WP6); eigene Regex-Sammlung für den Kritiker
  (zweite Wahrheit, die auseinanderlaufen würde); NFKC auch in WP5 nachrüsten (ändert das
  Verhalten des Summarizers und dessen Testerwartungen; die Grenze ist dort bewusst
  dokumentiert).
- Konsequenzen: Der Kritiker-Pfad ist strenger als der Summarizer-Pfad; die Differenz aus
  ADR-033 besteht für die `Summary` weiter und wird weiterhin von Schicht 6 (WP7) gedeckt.
  NFKC kann Zeichen ersetzen, die ein Modell bewusst gesetzt hat (z. B. `²` → `2`) — für
  einen zweizeiligen Warnhinweis ist das folgenlos.

## ADR-045: `[general] state_db` — Ort der State-Datenbank
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: Der Pfad der SQLite-Datei war bislang ein Konstruktor-Argument von `StateDB`;
  ADR-005 sagt „eine Datei neben der Config", ein Config-Feld dafür fehlte (offener Befund
  aus WP2). Der Daemon muss den Pfad aber aus der Config ableiten können, ohne ihn zu raten.
- Entscheidung: Neues Feld `[general] state_db = ""` plus `config.resolve_state_db_path(
  config, config_path)`. Auflösung: gesetzter Wert (mit `~`-Auflösung; relative Pfade
  relativ zum Verzeichnis der Config-Datei) → sonst `state.db` neben der Config-Datei →
  sonst `state.db` im Arbeitsverzeichnis (kein Config-Pfad bekannt, z. B. in Tests).
  Das Feld ist bewusst ein Pfad und kein Verzeichnis: Ein Betreiber, der die DB auf ein
  anderes Volume legen will, will genau die Datei benennen.
- Alternativen: Pfad nur als CLI-Argument (dann muss jeder systemd-/Cron-Aufruf ihn
  mitschleppen und der Daemon hätte zwei Wahrheiten); fester Pfad unter `~/.local/state`
  (bricht die „alles steht in einer Datei"-Zusage aus NF-3 und überrascht bei
  Mehrfach-Instanzen).
- Konsequenzen: Das Config-Schema wächst um ein Feld; `extra="forbid"` bleibt unberührt.
  Die Datei wird weiterhin mit Modus 0600 angelegt (WP2).

## ADR-046: Strukturiertes JSON-Logging auf stdout, Level aus `[general] log_level`
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: NF-5 verlangt strukturierte Logs ohne Mail-Inhalte und ohne PII über
  Absender-Domain + gehashte Message-ID hinaus. Die Module loggen seit WP2 mit
  `extra`-Feldern, es gab aber keinen Formatter und keinen konfigurierbaren Schwellwert.
- Entscheidung: `logging_setup.configure_logging(level, stream)` richtet **nur** den Logger
  `maildigest` ein (kein `basicConfig`, `propagate = False`): Fremdbibliotheken wie
  `httpx`/`imap_tools` sollen nicht ungefragt in unser Format schreiben — deren Debug-Logs
  können URLs mit Token enthalten. Ausgabe ist eine JSON-Zeile je Ereignis
  (`ts`, `level`, `logger`, `event` + `extra`-Felder). Nicht JSON-fähige `extra`-Werte
  werden durch ihren **Typnamen** ersetzt statt via `repr()` ausgegeben (ein `repr()` könnte
  Mail-Text oder ein Secret-Objekt sichtbar machen). Neues Feld `[general] log_level`
  (`DEBUG|INFO|WARNING|ERROR`, Default `INFO`); unbekannte Werte fallen auf `INFO` zurück,
  schalten also nie versehentlich DEBUG frei.
- Alternativen: `logging.basicConfig` mit Textformat (nicht maschinenlesbar, und der
  Wurzel-Logger würde Fremdbibliotheken mitziehen); eine Logging-Bibliothek wie `structlog`
  (neue Laufzeit-Dependency ohne Notwendigkeit, NF-1/NF-2).
- Konsequenzen: `journalctl -o cat | jq` funktioniert direkt. Wer Fremd-Logs sehen will,
  muss sie bewusst selbst konfigurieren — genau die Hürde, die wir wollen.

## ADR-047: Tracebacks nur bei Log-Level DEBUG
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: Offener Befund aus WP2: `ingest/imap_client.poll_once` loggte unerwartete
  Abstürze mit `logger.exception`, also inklusive vollem Traceback. Exception-Texte
  transportieren regelmäßig Eingabedaten (`ValueError: unerwartetes Zeichen in '<Mail-Zeile>'`,
  Parser-Fehler mit Byte-Auszügen). Das verletzt NF-5/I5 im Regelbetrieb — gleichzeitig ist
  ein Traceback für die Fehlersuche unverzichtbar.
- Entscheidung: Regelbetrieb loggt ausschließlich den Exception-**Klassennamen**
  (`extra={"error": type(exc).__name__}`); der Traceback erscheint nur, wenn der Betreiber
  `log_level = "DEBUG"` bewusst einschaltet. Umgesetzt über
  `logging_setup.traceback_enabled(logger)` als `exc_info=`-Argument — eine Stelle, an der
  die Politik steht, statt einer Konvention pro Aufrufstelle. Betroffen sind
  `poll_once`, `delivery.OutboxMessenger` und `Runner.run_forever`. docs/BETRIEB.md weist
  darauf hin, dass DEBUG-Logs Mail-Inhalte enthalten können und entsprechend zu behandeln
  sind.
- Alternativen: Tracebacks generell (I5-Verstoß im Normalbetrieb); Tracebacks generell
  weglassen (Fehlersuche bei einem Crash im Sanitizer wäre Raten); Traceback filtern/
  redigieren (nicht verlässlich möglich — der Inhalt steckt in beliebigen Frames).
- Konsequenzen: Ein Bug-Report enthält zunächst nur Klassennamen; der Betreiber muss für
  Details DEBUG einschalten und weiß dann, dass das Log vertraulich ist.

## ADR-048: Zustell-Warteschlange (`outbox`) in SQLite statt Retry im Speicher
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: ARCHITECTURE §6 verlangt für Messenger-Fehler 5 Versuche über höchstens eine
  Stunde und hält ausdrücklich fest, dass die Nachricht „fertig sanitisiert ist und aus der
  DB-Queue erneut versendet werden darf". ADR-008 verlangt at-least-once: Status `checked`
  committen, dann senden. Ohne Persistenz wäre beides nicht haltbar — ein Neustart während
  der Stunde verlöre die Nachricht (Verstoß gegen F-OPS-3), und `checked` wäre eine Zusage
  ohne Gegenstück.
- Entscheidung: Neue Tabelle `outbox(id, message_id_hash, kind, payload, attempts,
  first_queued_at, next_attempt_at, last_error)`. `payload` ist das JSON der **fertigen**
  `DigestMessage`-Teile (`parts`, `importance`, `is_warning`) —
  Kritiker-geprüft, output-sanitisiert, durch `final_guard` gelaufen. `OutboxMessenger.send`
  committet vor dem ersten Versuch und wirft bei Zustellfehlern **nicht**: Die Zusage trägt
  die Warteschlange. Backoff 60 s/300 s/900 s/2100 s (5 Versuche, Summe 55 min), harte
  Schranke 1 h ab dem Einreihen; danach Zeile löschen, `failed`/`delivery_failed` buchen und
  `ERROR` loggen. Erfolgreiche bzw. endgültig gescheiterte Zustellung darf ausschließlich
  Datensätze im Status `checked` bewegen (`promote_checked_to_delivered`) — die
  Warteschlange kann damit keinen Statusübergang erfinden. Schema-Version 1 → 2; da der
  Schritt rein additiv ist (nur neue Tabellen), hebt `StateDB` eine Version-1-Datei beim
  Öffnen still an, statt ein Migrationswerkzeug zu verlangen (NF-3).
  **docs/SECURITY.md §6 wird entsprechend erweitert:** Neben der Low-Digest-Queue ist die
  Outbox die zweite benannte Ausnahme von „kein Klartext in der DB"; sie enthält
  ausschließlich Text, der bereits für den Nutzer freigegeben war, nie Mail-Rohtext, nie
  Links, nie Anhänge, und wird nach Zustellung gelöscht.
- Alternativen: Retry nur im Speicher (Neustart = Verlust, F-OPS-3 verletzt); Nachricht neu
  aufbauen statt speichern (bräuchte den Mail-Klartext in der DB — ungleich schlimmer, und
  ein zweiter LLM-Lauf könnte etwas anderes sagen); die Mail beim Wiederanlauf erneut durch
  die ganze Pipeline schicken (LLM-Kosten, andere Zusammenfassung, und die Mail ist im
  Postfach bereits als gelesen markiert).
- Konsequenzen: Der Status einer Mail bleibt `checked`, solange ihre Nachricht in der
  Warteschlange liegt — genau die von ADR-008 vorgesehene Lücke, in der eine Doppelzustellung
  möglich ist. Betreiber sehen wartende Zustellungen in den Log-Feldern
  `queued_deliveries`/`delivery_deferred`.

## ADR-049: Inhalt der Low-Digest-Warteschlange und Planung des Sammel-Digests
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: F-SUM-5 verlangt einen täglichen Sammel-Digest der `low`-Mails; ARCHITECTURE §2
  skizziert die Tabelle mit den Spalten `headline`, `short_summary`, `category`, §7 gibt das
  Format vor: eine Zeile `• <headline> (<from_domain>)`, gruppiert nach Kategorie.
- Entscheidung: (a) Gespeichert werden `headline`, `category`, `from_domain`, `received_at`
  und der Hash — die in §2 vorgesehene Spalte `short_summary` **entfällt** (Datenminimierung:
  Das Format zeigt sie nicht, und jedes gespeicherte Zeichen Mail-Ableitung ist eines zu
  viel); ARCHITECTURE §2 wird angeglichen. (b) Eingereiht wird ausschließlich Text, der
  `output/sanitizer.scrub_field`/`scrub_plain` passiert hat — die Warteschlange ist keine
  Hintertür an Schicht 6 vorbei. Das Brechen der Domain-Punkte macht wie gehabt erst
  `final_guard` beim Bau der Nachricht. (c) Planung: `meta.last_low_digest_date` hält den
  Tag der letzten Zustellung; gesendet wird beim ersten Zyklus ab `[general] low_digest_time`
  (lokale Zeit). Ein verpasster Zeitpunkt (Prozess stand still) wird beim nächsten Lauf
  desselben Tages nachgeholt, nicht übersprungen. Leere Warteschlange ⇒ keine Nachricht,
  der Tag gilt trotzdem als erledigt. (d) Die Einträge werden gelöscht, sobald die Nachricht
  in der Outbox liegt — dort trägt sie die Zustellgarantie weiter; sonst entstünde am
  Folgetag ein Duplikat. (e) `compose_low_digest` gehört in `output/composer.py`, damit
  `_finalize()` der einzige Weg zu `DigestMessage.parts` bleibt (SECURITY §5, WP7).
- Alternativen: Volltext-Kurzfassung speichern (mehr Inhalt in der DB ohne Nutzen für das
  Format); Digest aus `seen_mails` rekonstruieren (dort steht bewusst keine Headline);
  Zustellung strikt zur Minute (ein Cron-Lauf um 18:05 würde den Digest nie sehen).
- Konsequenzen: Der Digest zeigt Kopfzeile + Domain je Mail, keine Inhaltszeile — wer mehr
  will, öffnet das Postfach. Bei mehr als 60 gesammelten Mails werden die restlichen nur
  gezählt („… und N weitere"), damit eine Nacht voller Newsletter keine Nachrichtenflut wird.

## ADR-050: Stufen-Retries, Zwischenstände und Statushoheit des Runners
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: ARCHITECTURE §6 verlangt für LLM-Fehler „3 Versuche, dann fail-closed" oberhalb
  der providerinternen Retries (429/5xx, ADR-023) und die Statusfolge
  `pending → sanitized → summarized → checked → …` in SQLite. `pipeline.process_mail`
  kannte bisher weder State noch Retries, `poll_once` schrieb den Endstatus selbst.
- Entscheidung: (a) Die Retry-Politik sitzt in dünnen Dekoratoren
  (`RetryingSummarizer`/`RetryingCritic`) statt in den Agenten oder der Pipeline — die
  Stufen bleiben, wie WP5/WP6 sie gebaut haben, und die Politik ist an einer Stelle
  nachlesbar. Wiederholt werden nur `LLMTimeout`, `LLMRateLimited`, `LLMTransportError`;
  `LLMInvalidResponse` **nicht** (der Reparaturversuch ist laut ADR-024 bereits gelaufen —
  ein weiterer Aufruf kostet nur Zeit und Tokens). Backoff 2 s/4 s. (b) `PipelineDeps`
  bekommt einen optionalen `progress`-Sink (`ProgressSink.record(dedupe_key, state)`), den
  die Pipeline nach Sanitize, Summarize und — entscheidend — **vor** dem Versand mit
  `checked` aufruft (ADR-008). Wirft der Sink, ist das ein Stufenfehler und endet
  fail-closed (I6); `pipeline._ERROR_CLASSES` kennt dafür `StateError → state_error`.
  (c) Die Statushoheit liegt beim Runner: `poll_once` bekommt das Flag
  `write_result_status` (Default `True`, damit WP2-Aufrufer unverändert funktionieren) und
  schreibt bei `False` nur noch den `failed`-Fall des Absturzpfades — sonst würde es die
  feinere Buchführung des Runners (Warteschlange, `checked`) überschreiben.
- Alternativen: Retries in den Agenten (jeder Agent bekäme dieselbe Schleife, WP5/WP6-Code
  müsste umgebaut werden); Retries in der Pipeline (die Pipeline würde Fehlerklassen der
  LLM-Schicht kennen müssen und wäre nicht mehr provider-agnostisch); Status ausschließlich
  in `poll_once` (kein Platz für Zwischenstände und keine Kenntnis der Zustell-Warteschlange).
- Konsequenzen: Eine Mail kostet im schlechtesten Fall 3 Summarizer- **plus** 3
  Kritiker-Versuche, bevor die Notiz kommt; mit den Provider-Retries aus ADR-023 sind das
  bis zu 9 HTTP-Aufrufe je Stufe. Die Timeout-Politik (Timeouts werden providerintern nicht
  wiederholt) hält die Wartezeit im Rahmen.

## ADR-051: Runner-Loop ruft `IngestService.run_once` je Zyklus
- Status: accepted
- WP / Datum: WP8, 2026-09-02
- Kontext: WP2 liefert mit `IngestService.run_forever` bereits einen Dauer-Loop inklusive
  Reconnect-Backoff. WP8 braucht zwischen zwei Polls aber zwei weitere Arbeiten: die
  Zustell-Warteschlange abarbeiten und den Sammel-Digest prüfen.
- Entscheidung: `Runner.run_forever` hat den äußeren Loop und ruft je Zyklus
  `IngestService.run_once` (also Verbindung auf, pollen, Verbindung zu), dazwischen
  `outbox.flush()` und `maybe_send_low_digest()`. Der Backoff kommt aus derselben Funktion
  wie in WP2 (`ingest.backoff_delay`), damit es nur eine Backoff-Politik gibt.
  `IngestService.run_forever` bleibt bestehen (Tests, direkte Nutzung), wird vom Runner aber
  nicht verwendet. Shutdown: SIGINT/SIGTERM setzen ein `threading.Event`; der laufende
  Zyklus wird zu Ende geführt, danach beendet sich der Loop. Die vorherigen Signal-Handler
  werden beim Verlassen wiederhergestellt, und außerhalb des Haupt-Threads wird gar nicht
  erst installiert (Tests, spätere Einbettung).
- Alternativen: `run_forever` aus WP2 mit Callback-Haken erweitern (Umbau an fremdem Code
  für einen Fall, der sich außen sauber lösen lässt); zweiter Thread für Warteschlange und
  Digest (Nebenläufigkeit auf einer SQLite-Verbindung, die laut WP2 genau einem Thread
  gehört — unnötiges Risiko für ein Ein-Prozess-Werkzeug).
- Konsequenzen: Je Poll-Intervall entsteht eine neue IMAP-Verbindung (bei Default 120 s
  unkritisch, und ein Reconnect je Zyklus ist robuster als eine über Stunden gehaltene
  Verbindung). Ein Zustellversuch findet mindestens einmal je Zyklus statt — die
  Backoff-Zeiten der Outbox (60 s aufwärts) sind darauf abgestimmt.

## ADR-052: CLI mit `argparse` statt `click`, Einstiegspunkt `maildigest`
- Status: accepted
- WP / Datum: WP9, 2026-09-02
- Kontext: PLAN.md WP9 erlaubt `click` nur mit ADR. Die CLI braucht sechs Kommandos,
  gemeinsame Optionen, klare Exit-Codes und muss zu 100 % testbar sein (Prompts über
  injizierbare Ströme, keine echten Netzverbindungen).
- Entscheidung: `argparse` aus der Standardbibliothek. Gemeinsame Optionen (`--config`,
  `--non-interactive`) liegen in einem `parents=`-Parser, der sowohl am Hauptparser als
  auch an jedem Unterkommando hängt — damit funktionieren `maildigest --config x init`
  und `maildigest init --config x` gleichermaßen. `ArgumentParser.error` ist überschrieben
  und wirft eine `CliError` mit Exit-Code 2 statt `SystemExit`, sodass `main()` in Tests
  einen Rückgabewert liefert. `main(argv, stdin, stdout, stderr, hooks)` bekommt alle
  Außenkontakte (Runner, Messenger-Fabrik, Provider-Fabrik, IMAP-Client, `getUpdates`,
  `configure_logging`, `sleep`) über ein `Hooks`-Objekt injiziert. Installiert wird die CLI
  über `[project.scripts] maildigest = "maildigest.cli:run_cli"`, zusätzlich gibt es
  `python -m maildigest`.
- Alternativen: `click` (schöner für verschachtelte Gruppen, aber eine Laufzeit-Dependency
  mehr für ein Werkzeug, das laut NF-1 mit ≤ 8 Paketen auskommen soll — und `click.testing`
  hätte die Ströme ohnehin nur anders injiziert); `typer` (zieht `click` mit).
- Konsequenzen: Etwas mehr Handarbeit bei Prompts und Validierung; dafür null neue
  Abhängigkeiten und ein `main()`, das sich wie eine normale Funktion testen lässt.
  `--help` beendet weiterhin über `SystemExit(0)` — das ist argparse-Verhalten und in
  SPEC-CLI.md so dokumentiert.

## ADR-053: Eigener TOML-Renderer statt `tomli-w`; Sektionsweise Validierung
- Status: accepted
- WP / Datum: WP9, 2026-09-02
- Kontext: `tomllib` kann nur lesen. Die Einrichtungs-Kommandos müssen die
  `config.toml` schreiben — kommentiert, in stabiler Reihenfolge und ohne die Angaben
  anderer Kommandos zu verlieren. Gleichzeitig ist die Konfiguration während der
  Einrichtung **unvollständig**: Nach `init` fehlen `[imap] host`, `[imap] username` und
  `[llm] model`, sodass `load_config` zwangsläufig scheitert.
- Entscheidung: (a) Ein winziger Renderer in `cli.py` (rund 40 Zeilen) serialisiert das
  Roh-Dict; Strings gehen durch `json.dumps` (TOML-Basic-Strings benutzen dieselben
  Escapes). Reihenfolge, Sektions- und Feldkommentare sowie auskommentierte Platzhalter für
  Pflichtfelder und Secrets stehen in Tabellen im Modul; unbekannte Schlüssel bleiben
  erhalten und wandern ans Ende ihrer Sektion. Vor dem Schreiben wird das Ergebnis selbst
  geparst. (b) `config.validate_section(model, data, source=…)` validiert **eine** Sektion
  mit derselben deutschen Fehlerübersetzung wie `load_config`; die `connect-*`-Kommandos
  benutzen das, `test` und `run` verlangen weiterhin die vollständige Config.
- Alternativen: `tomli-w` als Laufzeit-Dependency (NF-2 verlangt dafür einen ADR — der
  Nutzen wäre eine Funktion, die wir in 40 Zeilen bekommen, und Kommentare kann `tomli-w`
  ohnehin nicht schreiben); Datei mit Textersetzung patchen (bricht bei jeder
  Handänderung); vollständige Validierung nach jedem `connect-*` (jeder Lauf würde an einer
  themenfremden fehlenden Angabe scheitern).
- Konsequenzen: Der Renderer kennt nur die Typen, die im Schema vorkommen (Text, Zahl,
  Wahrheitswert, Liste); ein handgeschriebener Datums- oder Tabellen-Array-Wert würde beim
  Speichern eine Fehlermeldung erzeugen statt still verloren zu gehen. Handgeschriebene
  Kommentare des Nutzers gehen beim Speichern verloren — die Datei wird neu gerendert.

## ADR-054: Betriebsnachrichten der CLI laufen über `DigestComposer.compose_plain`
- Status: accepted
- WP / Datum: WP9, 2026-09-02
- Kontext: `connect-messenger` schickt eine Testnachricht. Deren Text stammt aus dem
  Programm, ist also nicht untrusted — trotzdem sagt docs/SECURITY.md §5, dass
  `DigestMessage.parts` ausschließlich über `DigestComposer._finalize()` entsteht.
- Entscheidung: Neue Methode `compose_plain(text)` im Composer, die durch `_finalize()`
  geht (Nachbrenner + Split) und `importance="normal"`, `is_warning=False`,
  `dedupe_key="cli-selftest"` setzt. Die CLI baut keine `DigestMessage` selbst. Der Text
  der Testnachricht enthält bewusst keine Punkte, Domains oder Markup-Zeichen, damit der
  Nachbrenner nichts zu entschärfen hat und die Nachricht so ankommt, wie sie dasteht.
- Alternativen: `DigestMessage` in der CLI direkt konstruieren (zweiter Weg zu `parts` —
  genau das, was die Invariante ausschließt); `compose_failure` zweckentfremden
  (semantisch falsch, der Nutzer läse „Mail konnte nicht verarbeitet werden").
- Konsequenzen: Eine öffentliche Methode mehr im Composer, die nur die CLI benutzt. Dafür
  bleibt die Aussage „es gibt genau einen Weg zum Messenger" wörtlich wahr.

## ADR-055: Fremddaten der Einrichtung erreichen das Terminal nur gefiltert
- Status: accepted
- WP / Datum: WP9, 2026-09-02
- Kontext: Drei Einrichtungs-Kommandos zeigen Daten an, die von außen kommen:
  IMAP-Ordnernamen (Server), Telegram-Chats aus `getUpdates` (jeder, der den Bot
  anschreibt) und die Antwort des Sprachmodells auf den Testaufruf. Ein Terminal
  interpretiert ANSI-Sequenzen; ein Chat-Titel oder Ordnername ist ein Einfallstor für
  Steuerzeichen, und die Modellantwort ist laut I4 grundsätzlich untrusted.
- Entscheidung: (a) Ordnernamen werden vor der Anzeige auf eine Zeichen-Allowlist
  (alphanumerisch, deutsche Umlaute, Leerzeichen, `_ . / -`) reduziert und auf 80 Zeichen
  gekürzt; gewählt wird über die **Nummer**, gespeichert wird der Originalname.
  (b) `messenger/telegram.discover_chat_ids` liefert nur die numerische Chat-ID und den
  Chat-Typ aus einer festen Werteliste — Anzeigenamen und Gruppentitel werden gar nicht
  erst gelesen. (c) Die Antwort des Testaufrufs wird nie ausgegeben, gemeldet werden nur
  Länge und ob das erwartete Wort vorkommt.
- Alternativen: Namen und Titel roh anzeigen (bequemer, aber ein Terminal-Injection-Weg
  und bei Telegram eine vom Angreifer wählbare Zeichenkette); Ausgabe erst im
  Output-Sanitizer entschärfen (der arbeitet auf Nachrichten, nicht auf Terminalausgaben).
- Konsequenzen: Ein exotisch benannter Ordner erscheint mit `·` an den gefilterten
  Stellen; die Zuordnung bleibt über die Nummer eindeutig. Bei mehreren Telegram-Chats muss
  der Nutzer die Chat-ID anhand der Nummer erkennen — akzeptabel, weil der Normalfall genau
  ein Chat ist.

## ADR-056: Secrets haben keine Kommandozeilen-Optionen
- Status: accepted
- WP / Datum: WP9, 2026-09-02
- Kontext: Für `--non-interactive` braucht jedes Feld einen Weg ohne Rückfrage. Für
  IMAP-Passwort, API-Key und Bot-Token wäre die naheliegende Lösung je eine Option.
- Entscheidung: Es gibt **keine** Optionen `--password`, `--api-key`, `--token`. Diese drei
  Werte kommen aus der Abfrage (ohne Echo, über `getpass`, sobald ein Terminal vorhanden
  ist) oder aus `MAILDIGEST_IMAP_PASSWORD` / `MAILDIGEST_LLM_API_KEY` /
  `MAILDIGEST_TELEGRAM_TOKEN`. Die Discord-Webhook-URL ist die Ausnahme: Sie ist zwar
  ebenfalls ein Secret, hat aber keine Umgebungsvariable im Schema (ADR-015) — hier gibt es
  `--webhook-url`, und die Abfrage läuft trotzdem ohne Echo.
- Alternativen: Optionen für alle Secrets (landen in der Shell-History und in der
  Prozessliste jedes Nutzers auf dem Rechner — genau der Weg, den I5 vermeiden soll);
  Secrets nur über Dateien (unnötig umständlich für eine Einrichtung von Hand).
- Konsequenzen: Eine vollautomatische Einrichtung braucht die Umgebungsvariablen; das ist
  auch die Form, die docs/BETRIEB.md für den Dienstbetrieb empfiehlt. Für Discord bleibt
  eine Secret-Option bestehen — dokumentiert in SPEC-CLI.md, mitsamt dem Hinweis, dass die
  URL selbst das Secret ist.

## ADR-057: `maildigest test` läuft auf einer eigenen State-DB und ohne Zustellschwelle
- Status: accepted
- WP / Datum: WP9, 2026-09-02
- Kontext: Der Selbsttest (F-OPS-2) soll beliebig oft dieselbe `.eml` einspeisen können —
  auch der Cold-Tester (WP11) baut darauf seine Angriffsmails. Die Dedupe-Logik (F-ING-2)
  würde den zweiten Lauf überspringen, und eine als `low` eingestufte Testmail landete
  lautlos im Sammel-Digest statt im Messenger.
- Entscheidung: `cmd_test` öffnet eine State-Datenbank in einem temporären Verzeichnis, das
  nach dem Lauf verschwindet, und setzt für den Lauf `deliver_min_importance = "low"`
  (Kopie der Config, die Datei bleibt unberührt). Alles andere ist die echte Verdrahtung:
  `build_runner`, dieselbe Pipeline, dieselbe Zustell-Warteschlange, derselbe Messenger.
  Die Stufen werden für die Schrittausgabe in dünne Mitschnitt-Wrapper gepackt, die nur
  Zahlen und Aufzählungswerte anzeigen — nie Mail- oder Modelltext.
- Alternativen: Die Betriebs-DB benutzen (jeder zweite Testlauf wäre ein „Duplikat", und
  ein Testlauf hinterließe Spuren im Zustell-Zustand); die Testmail über IMAP einspeisen
  (verlangt Schreibrechte im Postfach und einen Server, der `APPEND` erlaubt — die
  `.eml`-Datei ist der einfachere und für den Cold-Tester besser reproduzierbare Weg).
- Konsequenzen: `maildigest test` sagt nichts über die IMAP-Verbindung aus — dafür ist
  `connect-mail` zuständig. Ein Zustellfehler im Selbsttest bleibt in der temporären
  Warteschlange liegen und wird nicht wiederholt; die CLI meldet das als Exit-Code 1.
