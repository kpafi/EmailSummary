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
