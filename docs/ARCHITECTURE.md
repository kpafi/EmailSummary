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
```

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
