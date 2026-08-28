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
- IMAPS-Verbindung, Polling (Default 120 s), Abruf ungesehener Mails.
- Markiert verarbeitete Mails als gelesen; optional Verschieben nach `Processed`. Löscht nie.
- Dedupe gegen `state/db.py` (Message-ID, Fallback-Hash).

### Sanitizer (`sanitize/`)
- Reiner Code, kein LLM, keine Netzwerkzugriffe.
- Module: `attachments.py` (Allowlist + Magic Bytes), `html_to_text.py`, `links.py`,
  `unicode_clean.py`, `extract_pdf.py` (Subprozess mit Limits).
- Politik und Limits: SECURITY.md §4 (dort verbindlich).

### LLM-Schicht (`llm/`)
- `base.py`: Protokoll `LLMProvider.complete(system, user, *, max_tokens, temperature) -> str`.
  Kein Tool-Use im Interface (I2).
- `anthropic.py`, `openai.py`: HTTP-Implementierungen (httpx), Retry/Timeout.
- `schema.py`: `complete_json(...)` erzwingt pydantic-Schema (1 Reparatur-Retry).
- `prompts.py`: alle Prompt-Texte zentral, mit Versions-Kommentar.

### Agenten (`agents/`)
- `summarizer.py`: baut Prompt (System + gelabelte Custom-Instructions + delimitierter
  Datenblock), ruft `complete_json`, führt deterministische Nachkontrolle aus.
- `critic.py`: berechnet erst deterministische Signale (Code!), ruft dann das LLM mit
  Mail-Klartext + Summary + Signalen, liefert `CriticVerdict`.

### Output (`output/sanitizer.py`)
- Baut aus Summary + Verdict die `DigestMessage` (Format §7) und sanitisiert jedes Feld
  (URL-/Markdown-/HTML-Strip, Escaping, Längen-Split je Messenger).

### Messenger (`messenger/`)
- Protokoll `Messenger.send(DigestMessage)`, `healthcheck()`.
- `telegram.py` (Bot-API, reiner Text ohne parse_mode), `discord.py` (Webhook),
  `signal.py` (signal-cli JSON-RPC, Feature-Flag).

### State (`state/db.py`)
- SQLite, Tabellen:
  - `seen_mails(message_id_hash TEXT PK, first_seen_at, status, error_class, retry_count)`
  - `low_digest_queue(id, message_id_hash, received_at, headline, short_summary, category)`
  - `meta(key, value)` — z. B. Schema-Version, letzter Digest-Zeitpunkt.
- Statuswerte: `pending → sanitized → summarized → checked → delivered | failed | skipped_low`.
- Kein Mail-Volltext in der DB (SECURITY.md §6).

### CLI (`cli.py`)
- Kommandos: `init`, `connect-mail`, `connect-llm`, `connect-messenger`, `test`,
  `run [--once]`. Vollständiger Vertrag: SPEC-CLI.md (entsteht in WP9).

## 3. Datenmodell (verbindlich für WP1)

```python
class RawMail(BaseModel, frozen=True):
    message_id: str | None          # Header; None wenn fehlend
    dedupe_key: str                 # message_id oder Fallback-Hash
    from_addr: str                  # "Anzeigename <adresse>" roh
    from_domain: str                # extrahiert, lowercase
    reply_to: str | None
    return_path_domain: str | None
    to_addrs: list[str]
    subject_raw: str                # undekodiert/dekodiert roh
    date: datetime | None
    auth_results_header: str | None # Authentication-Results, roh
    mime_bytes: bytes               # komplette Roh-Mail (verlässt Ingest+Sanitizer nie!)
    size_bytes: int

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
5. `reason_class` wird aus dem Exception-**Klassennamen** abgeleitet (Tabelle in
   `pipeline._ERROR_CLASSES`), Fallback `"<stage>_error"`. Der Exception-Text wird nie
   übernommen (I5).

## 5. Konfiguration (`config.toml`, Schema in `config.py`)

```toml
[general]
language = "de"              # Sprache der Zusammenfassungen
summary_length = "medium"    # short | medium | long
deliver_min_importance = "normal"  # low | normal | high
low_digest_time = "18:00"    # tägliche Sammelzustellung

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
max_mime_depth = 10
max_attachments_processed = 20
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

## 6. Fehler- & Retry-Politik (Detail in WP8)

- IMAP-Fehler: Reconnect mit Exponential Backoff (max. 10 min), Loop läuft weiter.
- LLM-Fehler (Timeout/429/5xx): 3 Versuche mit Backoff, dann `FailureNotice`.
- Schema-Invalidität: 1 Reparatur-Retry (in `llm/schema.py`), dann `FailureNotice`.
- Messenger-Fehler: 5 Versuche über max. 1 h (Nachricht ist fertig sanitisiert und darf
  aus der DB-Queue erneut versendet werden), dann `failed` + Log.
- Prozess-Crash: State in SQLite so, dass Wiederanlauf idempotent ist (Status vor Versand
  committen ⇒ schlimmstenfalls eine Doppelzustellung, nie Verlust — als ADR festhalten).

## 7. Nachrichtenformat (Referenz für WP7, final dort festschreiben)

```
⚠️ PHISHING-VERDACHT: <risk_reasons, kommasepariert>          ← nur bei phishing_risk=high
📧 <headline> [wichtig]                                        ← Tag nur bei high
Von: <from_display> (<from_domain>) · <TT.MM. HH:MM>
<summary_text>
<attachment_summaries als „— <datei>: <1–2 Sätze>">
📎 Nicht verarbeitet: <datei (größe)>, …                       ← nur wenn vorhanden
🔍 Hinweise: <injection_suspected/auth-fails/punycode-Hinweise> ← nur wenn vorhanden
```

Sammel-Digest (täglich): eine Nachricht, gruppiert nach Kategorie, je Mail eine Zeile
`• <headline> (<from_domain>)`.
