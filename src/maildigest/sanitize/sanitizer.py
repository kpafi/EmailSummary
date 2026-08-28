"""Orchestrierung der Sanitize-Stufe: ``RawMail`` → ``SanitizedMail`` (reiner Code, kein LLM).

Politik und Limits: docs/SECURITY.md §4 (verbindlich); Vertrag: docs/ARCHITECTURE.md §2/§4.
Umsetzung in WP3 (ADR-026 bis ADR-030).

Ablauf je Mail:
1. Größenlimit (`limits.max_mail_bytes`) — drüber ⇒ :class:`SanitizeError` (fail-closed, T10).
2. MIME parsen (stdlib `email`), Baum manuell mit Tiefenlimit laufen; `message/rfc822`
   wird **nie** betreten (T13).
3. Body: alle Inline-`text/plain`-Teile, sonst alle Inline-`text/html`-Teile (konvertiert).
4. Anhänge: Allowlist + Magic-Bytes (`attachments.py`), PDF im Subprozess (`extract_pdf.py`),
   alles andere nur Metadatum.
5. Jeder Text: `unicode_clean` → (HTML→Text) → `unicode_clean` → Link-Scrub → Budget.
6. `SanitizationReport` vollständig befüllen (deterministische Fakten für den Kritiker,
   F-CRIT-3).
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parseaddr

from maildigest.config import Config, LimitsConfig
from maildigest.models import (
    AttachmentInfo,
    AttachmentKind,
    RawMail,
    SanitizationReport,
    SanitizedMail,
)
from maildigest.sanitize.attachments import detect_kind, sanitize_filename
from maildigest.sanitize.extract_pdf import extract_pdf_text
from maildigest.sanitize.html_to_text import html_to_text
from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.unicode_clean import clean_text, is_mixed_script_domain

__all__ = ["MailSanitizer", "SanitizeError"]

#: Marker, der ans Ende gekürzter Texte gesetzt wird (SECURITY §4).
_TRUNCATION_MARKER = "[gekürzt]"

#: HTML-Tag-artige Sequenzen, die auch in *Klartext*-Teilen neutralisiert werden.
#: Akzeptanzkriterium WP3: kein Output-Feld enthält ein HTML-Tag — auch nicht, wenn ein
#: Angreifer `<script>` wörtlich in einen text/plain-Body schreibt. Über-Entfernung
#: (z. B. `Name <adresse@example>`) ist der bewusst fail-safe Trade-off (ADR-027).
_RE_TAG_LIKE = re.compile(r"</?[a-zA-Z][^<>]{0,200}>")

#: Obergrenze der `attachments`-Metadatenliste (Schutz vor Teile-Bomben, T10).
#: `blocked_attachments` im Report zählt unabhängig davon alle.
_MAX_ATTACHMENT_ENTRIES = 100

#: Obergrenze für Betreff und Anzeigename nach Sanitisierung.
_MAX_SUBJECT_CHARS = 300
_MAX_DISPLAY_CHARS = 120

#: Obergrenze der Link-Fußnote (nur bei `links.footnote = true`).
_MAX_FOOTNOTE_CHARS = 5000


class SanitizeError(Exception):
    """Sanitize-Stufe kann die Mail nicht sicher verarbeiten ⇒ fail-closed (I6).

    Der Meldungstext ist immer ein konstantes Label ohne Mail-Inhalt (I5); die Pipeline
    bildet den Klassennamen auf `reason_class = "sanitize_error"` ab (ADR-012).
    """


@dataclass
class _WalkState:
    """Veränderlicher Zustand eines Sanitize-Durchlaufs (ein Objekt pro Mail)."""

    body_plain: list[str] = field(default_factory=list)
    body_html: list[str] = field(default_factory=list)
    attachments: list[AttachmentInfo] = field(default_factory=list)
    attachment_texts: dict[str, str] = field(default_factory=dict)
    attachment_count: int = 0
    processed_count: int = 0
    hidden_removed: int = 0
    control_chars_removed: int = 0
    truncated: bool = False


class MailSanitizer:
    """Deterministische Sanitize-Stufe (implementiert das `Sanitizer`-Protokoll der Pipeline)."""

    def __init__(self, limits: LimitsConfig | None = None, *, link_footnote: bool = False) -> None:
        self._limits = limits if limits is not None else LimitsConfig()
        self._link_footnote = link_footnote

    @classmethod
    def from_config(cls, config: Config) -> MailSanitizer:
        """Baut den Sanitizer aus der validierten Gesamt-Konfiguration."""
        return cls(config.limits, link_footnote=config.links.footnote)

    # --- Öffentliche API -------------------------------------------------------------------

    def sanitize(self, raw: RawMail) -> SanitizedMail:
        """Wandelt eine rohe Mail in sicheren Klartext + Metadaten um.

        Raises:
            SanitizeError: Mail überschreitet `limits.max_mail_bytes` oder das MIME ist
                nicht einmal ansatzweise parsbar. Die Pipeline macht daraus die
                Metadaten-Notiz (I6).
        """
        limit = self._limits.max_mail_bytes
        if raw.size_bytes > limit or len(raw.mime_bytes) > limit:
            raise SanitizeError("mail_zu_gross")

        try:
            message = email.message_from_bytes(raw.mime_bytes, policy=email.policy.compat32)
        except Exception as exc:
            raise SanitizeError("mime_unparsbar") from exc

        links = LinkCollector()
        state = _WalkState()
        self._walk(message, 0, state)

        # Body: text/plain bevorzugt; sonst text/html → Text (SECURITY §4).
        if state.body_plain:
            body_raw = "\n\n".join(state.body_plain)
        elif state.body_html:
            converted: list[str] = []
            for html in state.body_html:
                cleaned_html, removed = clean_text(html)
                state.control_chars_removed += removed
                text, hidden = html_to_text(cleaned_html)
                state.hidden_removed += hidden
                converted.append(text)
            body_raw = "\n\n".join(converted)
        else:
            body_raw = ""

        body_clean, removed = clean_text(body_raw)
        state.control_chars_removed += removed
        body_scrubbed = _strip_tag_like(links.scrub(body_clean))

        budget = self._limits.max_text_chars
        body_text, budget, truncated = _take_budget(body_scrubbed, budget)
        state.truncated = state.truncated or truncated

        attachment_texts: dict[str, str] = {}
        attachments: list[AttachmentInfo] = []
        for info in state.attachments:
            raw_text = state.attachment_texts.get(info.filename_sanitized)
            if info.processed and raw_text is not None:
                cleaned, removed = clean_text(raw_text)
                state.control_chars_removed += removed
                scrubbed = _strip_tag_like(links.scrub(cleaned))
                final, budget, truncated = _take_budget(scrubbed, budget)
                state.truncated = state.truncated or truncated
                attachment_texts[info.filename_sanitized] = final
                attachments.append(
                    info.model_copy(update={"extracted_chars": len(final)})
                )
            else:
                attachments.append(info)

        subject = self._sanitize_header(raw.subject_raw, links, state, _MAX_SUBJECT_CHARS)
        from_display = self._from_display(raw, links, state)

        if self._link_footnote and links.links_found:
            body_text = body_text + _footnote(links.links_found)

        report = self._build_report(raw, links, state, attachments)
        return SanitizedMail(
            dedupe_key=raw.dedupe_key,
            from_display=from_display,
            from_domain=raw.from_domain,
            subject=subject,
            date=raw.date,
            body_text=body_text,
            attachment_texts=attachment_texts,
            attachments=attachments,
            links_found=list(links.links_found),
            sanitization_report=report,
        )

    # --- MIME-Baum -------------------------------------------------------------------------

    def _walk(self, part: Message, depth: int, state: _WalkState) -> None:
        """Läuft den MIME-Baum manuell (Tiefenlimit, message/rfc822 wird nie betreten)."""
        if depth > self._limits.max_mime_depth:
            self._add_attachment_meta(
                state,
                filename="(mime-tiefe ueberschritten)",
                declared=_content_type(part),
                kind="unknown",
                size=0,
            )
            return

        ctype = _content_type(part)
        if ctype.startswith("message/"):
            # T13: eingebettete Mails sind Anhänge, ihr Inhalt wird nie geöffnet.
            self._handle_attachment(part, state, force_metadata_only=True)
            return

        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    if isinstance(sub, Message):
                        self._walk(sub, depth + 1, state)
            return

        disposition = _content_disposition(part)
        filename = _filename(part)
        is_inline_body = disposition != "attachment" and filename is None
        if ctype == "text/plain" and is_inline_body:
            state.body_plain.append(_decode_text_part(part))
            return
        if ctype == "text/html" and is_inline_body:
            state.body_html.append(_decode_text_part(part))
            return
        self._handle_attachment(part, state)

    def _handle_attachment(
        self, part: Message, state: _WalkState, *, force_metadata_only: bool = False
    ) -> None:
        """Klassifiziert einen Anhang; verarbeitet ihn nur bei Allowlist + Magic-Match."""
        state.attachment_count += 1
        declared = _content_type(part)
        data = _payload_bytes(part)
        fallback = f"anhang-{state.attachment_count}"
        filename = sanitize_filename(_filename(part), fallback=fallback)
        filename = _unique_name(filename, state)

        if force_metadata_only:
            self._add_attachment_meta(
                state, filename=filename, declared=declared, kind="unknown", size=len(data)
            )
            return

        kind = detect_kind(declared, data)
        disposition = _content_disposition(part)
        is_file = disposition == "attachment" or _filename(part) is not None
        # HTML nur als Inline-Body verarbeiten; .html-*Dateien* sind Smuggling-Vektor
        # und bleiben Metadatum (SECURITY §4). Als Datei markierte text/plain und PDF
        # sind die einzigen inhaltlich verarbeiteten Anhänge.
        processable = kind == "text" or kind == "pdf"
        if not processable or (kind == "html" and is_file):
            self._add_attachment_meta(
                state, filename=filename, declared=declared, kind=kind, size=len(data)
            )
            return

        if state.processed_count >= self._limits.max_attachments_processed:
            self._add_attachment_meta(
                state, filename=filename, declared=declared, kind=kind, size=len(data)
            )
            return

        text: str | None = None
        if kind == "text":
            text = _decode_text_part(part)
        elif kind == "pdf":
            text = extract_pdf_text(
                data,
                timeout_seconds=float(self._limits.pdf_timeout_seconds),
                max_input_bytes=self._limits.pdf_max_input_bytes,
                max_output_chars=self._limits.pdf_max_output_chars,
            )

        if text is None:
            self._add_attachment_meta(
                state, filename=filename, declared=declared, kind=kind, size=len(data)
            )
            return

        state.processed_count += 1
        state.attachment_texts[filename] = text
        # Verarbeitete Anhänge bekommen immer einen Metadaten-Eintrag (auch jenseits der
        # Listen-Obergrenze): ihre Zahl ist ohnehin durch `max_attachments_processed` begrenzt.
        state.attachments.append(
            AttachmentInfo(
                filename_sanitized=filename,
                declared_mime=declared,
                detected_kind=kind,
                size_bytes=len(data),
                processed=True,
                extracted_chars=len(text),
            )
        )

    def _add_attachment_meta(
        self,
        state: _WalkState,
        *,
        filename: str,
        declared: str,
        kind: AttachmentKind,
        size: int,
    ) -> None:
        if len(state.attachments) < _MAX_ATTACHMENT_ENTRIES:
            state.attachments.append(
                AttachmentInfo(
                    filename_sanitized=filename,
                    declared_mime=declared,
                    detected_kind=kind,
                    size_bytes=size,
                    processed=False,
                    extracted_chars=0,
                )
            )

    # --- Header & Report -------------------------------------------------------------------

    def _sanitize_header(
        self, value: str, links: LinkCollector, state: _WalkState, max_chars: int
    ) -> str:
        cleaned, removed = clean_text(value)
        state.control_chars_removed += removed
        scrubbed = _strip_tag_like(links.scrub(cleaned))
        collapsed = " ".join(scrubbed.split())
        if len(collapsed) > max_chars:
            collapsed = collapsed[: max_chars - 1] + "…"
        return collapsed

    def _from_display(
        self, raw: RawMail, links: LinkCollector, state: _WalkState
    ) -> str:
        display, address = parseaddr(raw.from_addr)
        if display:
            return self._sanitize_header(display, links, state, _MAX_DISPLAY_CHARS)
        cleaned, removed = clean_text(address or raw.from_addr)
        state.control_chars_removed += removed
        collapsed = " ".join(cleaned.split())
        return collapsed[:_MAX_DISPLAY_CHARS] or "(unbekannter Absender)"

    def _build_report(
        self,
        raw: RawMail,
        links: LinkCollector,
        state: _WalkState,
        attachments: list[AttachmentInfo],
    ) -> SanitizationReport:
        punycode = list(links.punycode_domains)
        mixed = list(links.mixed_script_domains)
        if raw.from_domain:
            if "xn--" in raw.from_domain and raw.from_domain not in punycode:
                punycode.append(raw.from_domain.replace(".", "[.]"))
            if is_mixed_script_domain(raw.from_domain):
                entry = raw.from_domain.replace(".", "[.]")
                if entry not in mixed:
                    mixed.append(entry)
        return SanitizationReport(
            links_removed=links.links_removed,
            hidden_text_removed=state.hidden_removed > 0,
            control_chars_removed=state.control_chars_removed,
            punycode_domains=punycode,
            mixed_script_domains=mixed,
            truncated=state.truncated,
            blocked_attachments=sum(1 for info in attachments if not info.processed),
            reply_to_mismatch=_reply_to_mismatch(raw),
            return_path_mismatch=_return_path_mismatch(raw),
            auth_results=_parse_auth_results(raw.auth_results_header),
        )


# --- Modul-Helfer (bewusst ohne Objekt-Zustand) -----------------------------------------------


def _strip_tag_like(text: str) -> str:
    """Neutralisiert HTML-Tag-artige Sequenzen in bereits konvertiertem Klartext."""
    return _RE_TAG_LIKE.sub(" ", text)


def _take_budget(text: str, budget: int) -> tuple[str, int, bool]:
    """Wendet das verbleibende Gesamt-Klartext-Budget an (SECURITY §4, T10)."""
    if len(text) <= budget:
        return text, budget - len(text), False
    cut = text[: max(budget, 0)].rstrip()
    result = f"{cut}\n{_TRUNCATION_MARKER}" if cut else _TRUNCATION_MARKER
    return result, 0, True


def _footnote(links_found: list[str]) -> str:
    """Baut die optionale defangte Link-Fußnote (Config `links.footnote`, I3-konform)."""
    lines = ["", "", "Link-Fußnote (defanged):"]
    total = 0
    for entry in links_found:
        total += len(entry) + 1
        if total > _MAX_FOOTNOTE_CHARS:
            lines.append("[weitere Links unterdrückt]")
            break
        lines.append(entry)
    return "\n".join(lines)


def _unique_name(filename: str, state: _WalkState) -> str:
    """Macht Anhang-Namen eindeutig, damit `attachment_texts`-Einträge nicht kollidieren."""
    existing = {info.filename_sanitized for info in state.attachments}
    existing.update(state.attachment_texts)
    if filename not in existing:
        return filename
    for counter in range(2, 1000):
        candidate = f"{filename} ({counter})"
        if candidate not in existing:
            return candidate
    return f"{filename} ({state.attachment_count})"


def _content_type(part: Message) -> str:
    try:
        return part.get_content_type().lower()
    except Exception:
        return "application/octet-stream"


def _content_disposition(part: Message) -> str | None:
    try:
        value = part.get_content_disposition()
    except Exception:
        return None
    return value


def _filename(part: Message) -> str | None:
    try:
        return part.get_filename()
    except Exception:
        return None


def _payload_bytes(part: Message) -> bytes:
    """Dekodierte Rohbytes eines Leaf-Parts; bei kaputtem Encoding best effort."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    if payload is None and _content_type(part).startswith("message/"):
        try:
            return part.as_bytes()
        except Exception:
            return b""
    return b""


def _decode_text_part(part: Message) -> str:
    """Text eines text/*-Parts mit Charset-Fallbacks (wirft nie)."""
    data = _payload_bytes(part)
    charset = None
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    for encoding in (charset, "utf-8", "latin-1"):
        if not encoding:
            continue
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _reply_to_mismatch(raw: RawMail) -> bool:
    """Reply-To weicht vom From ab (deterministisches Kritiker-Signal, F-CRIT-3)."""
    if not raw.reply_to:
        return False
    reply = parseaddr(raw.reply_to)[1].strip().lower()
    sender = parseaddr(raw.from_addr)[1].strip().lower()
    return bool(reply) and bool(sender) and reply != sender


def _return_path_mismatch(raw: RawMail) -> bool:
    """Return-Path-Domain weicht von der From-Domain ab (F-CRIT-3).

    Leere/unbekannte Werte sind nie ein Treffer (ADR-020: zwei Unbekannte sind keine
    Übereinstimmung, aber auch kein Mismatch-Beweis).
    """
    if not raw.return_path_domain or not raw.from_domain:
        return False
    return raw.return_path_domain.lower() != raw.from_domain.lower()


#: Auth-Ergebnis: `spf=pass`, `dkim=fail`, `dmarc=none` … (Werte-Allowlist per Regex).
_RE_AUTH_RESULT = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([A-Za-z0-9._-]{1,32})")


def _parse_auth_results(header: str | None) -> dict[str, str]:
    """Parst `Authentication-Results` best effort (F-CRIT-3); erste Nennung gewinnt."""
    if not header:
        return {}
    results: dict[str, str] = {}
    for match in _RE_AUTH_RESULT.finditer(header):
        results.setdefault(match.group(1).lower(), match.group(2).lower())
    return results
