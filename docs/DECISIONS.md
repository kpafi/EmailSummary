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
