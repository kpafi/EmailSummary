"""Domänen-Datenklassen (pydantic): RawMail, AttachmentInfo, SanitizedMail, SanitizationReport,
Summary, CriticVerdict, DigestMessage, FailureNotice.

Verbindliche Definition: docs/ARCHITECTURE.md §3 (umgesetzt in WP1).

Konventionen:
- `frozen=True` überall dort, wo ARCHITECTURE.md §3 es vorgibt. `Summary` und `CriticVerdict`
  bleiben bewusst veränderlich: Die deterministische Nachkontrolle (WP5/WP6) säubert Felder
  der LLM-Ausgabe in-place.
- `extra="forbid"`: Unbekannte Felder (z. B. aus LLM-JSON) werden abgelehnt statt still
  übernommen — Teil der Schema-Härtung (I4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AttachmentInfo",
    "AttachmentKind",
    "CriticVerdict",
    "DigestMessage",
    "FailureNotice",
    "Importance",
    "PhishingRisk",
    "RawMail",
    "SanitizationReport",
    "SanitizedMail",
    "Summary",
]

#: Wichtigkeitsstufen einer Mail (Summarizer, F-SUM-2).
Importance = Literal["high", "normal", "low"]

#: Phishing-Risikostufen des Kritikers (F-CRIT-1).
PhishingRisk = Literal["none", "low", "high"]

#: Ergebnis der Magic-Bytes-Prüfung eines Anhangs (F-SEC-4).
AttachmentKind = Literal["pdf", "text", "html", "unknown", "mismatch"]


class RawMail(BaseModel):
    """Rohe Mail aus dem Mirror-Postfach (Stufe 1, Ingest).

    Enthält als einziges Modell die vollständigen MIME-Bytes. Dieses Objekt verlässt die
    Sanitize-Stufe nicht (I1) — siehe `pipeline.process_mail`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str | None = None
    dedupe_key: str
    from_addr: str
    from_domain: str
    reply_to: str | None = None
    return_path_domain: str | None = None
    to_addrs: list[str] = Field(default_factory=list)
    subject_raw: str = ""
    date: datetime | None = None
    auth_results_header: str | None = None
    mime_bytes: bytes
    size_bytes: int = Field(ge=0)


class AttachmentInfo(BaseModel):
    """Metadaten genau eines Anhangs — auch für nicht verarbeitete Anhänge (Allowlist, F-SEC-4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename_sanitized: str
    declared_mime: str
    detected_kind: AttachmentKind
    size_bytes: int = Field(ge=0)
    processed: bool = False
    extracted_chars: int = Field(default=0, ge=0)


class SanitizationReport(BaseModel):
    """Protokoll dessen, was der Sanitizer entfernt/ersetzt/erkannt hat (Stufe 2).

    Dient dem Nutzer als Hinweisblock und dem Kritiker als deterministische Faktenbasis
    (F-CRIT-3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    links_removed: int = Field(default=0, ge=0)
    hidden_text_removed: bool = False
    control_chars_removed: int = Field(default=0, ge=0)
    punycode_domains: list[str] = Field(default_factory=list)
    mixed_script_domains: list[str] = Field(default_factory=list)
    truncated: bool = False
    blocked_attachments: int = Field(default=0, ge=0)
    reply_to_mismatch: bool = False
    return_path_mismatch: bool = False
    auth_results: dict[str, str] = Field(default_factory=dict)


class SanitizedMail(BaseModel):
    """Ausgabe des Sanitizers: ausschließlich Klartext + Metadaten.

    Einziger Mail-Input, den ein LLM je zu sehen bekommt (I1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dedupe_key: str
    from_display: str
    from_domain: str
    subject: str
    date: datetime | None = None
    body_text: str
    attachment_texts: dict[str, str] = Field(default_factory=dict)
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    links_found: list[str] = Field(default_factory=list)
    sanitization_report: SanitizationReport


class Summary(BaseModel):
    """Strukturierte Ausgabe des Summarizers (Stufe 3) — untrusted (I4).

    Bewusst nicht frozen: Die deterministische Nachkontrolle in WP5 säubert Felder und setzt
    `injection_suspected` nach.
    """

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=100)
    summary_text: str
    importance: Importance
    importance_reason: str = ""
    category: str = ""
    attachment_summaries: dict[str, str] = Field(default_factory=dict)
    injection_suspected: bool = False


class CriticVerdict(BaseModel):
    """Ausgabe des Kritikers (Stufe 4) — untrusted (I4).

    Bewusst nicht frozen: analog zu `Summary` (deterministische Nachkontrolle in WP6).
    """

    model_config = ConfigDict(extra="forbid")

    phishing_risk: PhishingRisk
    risk_reasons: list[str] = Field(default_factory=list)
    summary_accurate: bool = True
    notes: str = ""


class DigestMessage(BaseModel):
    """Versandfertige, bereits output-sanitisierte Nachricht (Stufe 5).

    `parts` ist bereits auf die Längenbegrenzung des Ziel-Messengers gesplittet (WP7).
    Ab hier darf kein Text mehr verändert werden.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parts: list[str]
    importance: Importance
    is_warning: bool = False
    dedupe_key: str


class FailureNotice(BaseModel):
    """Metadaten-Notiz für den Fail-closed-Pfad (I6, F-OPS-3).

    Enthält bewusst **keine** Mail-Inhalte und keine Fehlerdetails — nur Absender-Domain,
    not-sanitisierten Betreff, Stufe und grobe Fehlerklasse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dedupe_key: str
    from_domain: str
    subject_sanitized: str
    stage: str
    reason_class: str
