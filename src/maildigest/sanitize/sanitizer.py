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
from maildigest.sanitize.html_to_text import HtmlTooComplexError, html_to_text
from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.unicode_clean import clean_text, is_mixed_script_domain

__all__ = [
    "ENCRYPTED_CONTENT_TYPES",
    "FORGED_MARKER_TOKEN",
    "MailSanitizer",
    "SanitizeError",
]

#: MIME-Typen, die eine Ende-zu-Ende-verschlüsselte Mail auszeichnen (HC-33, ADR-082).
#: MailDigest entschlüsselt nicht — es hält nur fest, *warum* kein Inhalt da ist. Der
#: Sanitizer behandelt die Teile im Übrigen wie jeden anderen Anhang: Sie stehen nicht auf
#: der Allowlist und werden deshalb ohnehin nur als Metadatum geführt (SECURITY §4).
ENCRYPTED_CONTENT_TYPES = frozenset(
    {
        "multipart/encrypted",
        "application/pgp-encrypted",
        "application/pkcs7-mime",
        "application/x-pkcs7-mime",
    }
)

#: Ersatztext für einen nachgebauten Datenblock-Marker (HC-5). Er muss die Tag-Löschung
#: überleben (keine Winkelklammern) und darf selbst kein Marker-Wortlaut sein.
FORGED_MARKER_TOKEN = "[forged data-block marker removed]"

#: Ein in Winkelklammern gefasstes Konstrukt beliebiger Klammertiefe (`<…>`, `<<<…>>>`).
#: Ob es ein Marker-Nachbau ist, entscheidet :func:`neutralize_forged_markers` an den
#: Wörtern im Inneren — nicht die Regex. Das hält die Suche linear und die Regel lesbar.
_RE_ANGLE_CHUNK = re.compile(r"<+[^<>\n]{0,300}>+")

#: Beide Wörter müssen (case-insensitiv) im Inneren stehen, damit aus `<…>` ein Nachbau
#: des Prompt-Datenblocks wird. Die Reihenfolge ist egal, die Nonce beliebig.
_FORGED_MARKER_WORDS = ("MAILDIGEST", "UNTRUSTED")

#: Marker, der ans Ende gekürzter Texte gesetzt wird (SECURITY §4).
_TRUNCATION_MARKER = "[truncated]"

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

#: Marker, die nur auf der HTML-Seite entstehen (sichtbar gemachte Ziele, Alt-Texte) und
#: den Divergenz-Vergleich sonst systematisch verfälschen würden (CT-15).
_RE_DIVERGENCE_NOISE = re.compile(r"\[(?:Link|Mail|Tel|Bild)\b[^\]]*\]")

#: Wort-Token des Divergenz-Vergleichs: mindestens drei Buchstaben/Ziffern.
_RE_CONTENT_WORD = re.compile(r"[0-9a-zà-öø-ÿ]{3,}")

#: So viele im Klartext fehlende Wörter müssen im HTML-Teil mindestens stehen (CT-15).
_DIVERGENCE_MIN_NEW_WORDS = 5

#: … und zugleich diesen Anteil der HTML-Wörter ausmachen.
_DIVERGENCE_RATIO = 0.5


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
    html_rejected: bool = False
    truncated: bool = False
    html_divergent: bool = False
    forged_markers: int = 0
    encrypted: bool = False


class MailSanitizer:
    """Deterministische Sanitize-Stufe (implementiert das `Sanitizer`-Protokoll der Pipeline)."""

    def __init__(self, limits: LimitsConfig | None = None) -> None:
        self._limits = limits if limits is not None else LimitsConfig()

    @classmethod
    def from_config(cls, config: Config) -> MailSanitizer:
        """Baut den Sanitizer aus der validierten Gesamt-Konfiguration.

        `links.footnote` wird hier **nicht** ausgewertet: Die Fußnote gehört an die
        zugestellte Nachricht, nicht in den Prompt — sie hängt am Composer (CT-14).
        """
        return cls(config.limits)

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
            # CT-15: Der Nutzer sieht in seinem Mailprogramm den HTML-Teil. Weicht der
            # inhaltlich ab, wird das vermerkt — sonst beschreibt die Zusammenfassung
            # unbemerkt einen anderen Text als den angezeigten.
            state.html_divergent = self._html_diverges(body_raw, state)
        elif state.body_html:
            converted: list[str] = []
            for html in state.body_html:
                cleaned_html, removed = clean_text(html)
                state.control_chars_removed += removed
                try:
                    text, hidden = self._to_text(cleaned_html)
                except HtmlTooComplexError:
                    # ADR-084/HC2-1: Der Teil ist zu gross/zu tief, um ihn in der
                    # Poll-Periode sicher zu konvertieren. Er gilt als nicht verarbeitet —
                    # kein roher HTML-Text geht weiter (I1), die Mail läuft mit dem Rest
                    # (hier: ohne Body) fail-safe durch und der Nutzer sieht die Hinweiszeile.
                    state.html_rejected = True
                    continue
                state.hidden_removed += hidden
                converted.append(text)
            body_raw = "\n\n".join(converted)
        else:
            body_raw = ""

        body_clean, removed = clean_text(body_raw)
        state.control_chars_removed += removed
        body_unforged, forged = neutralize_forged_markers(body_clean)
        state.forged_markers += forged
        body_scrubbed = _strip_tag_like(links.scrub(body_unforged))

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
                unforged, forged = neutralize_forged_markers(cleaned)
                state.forged_markers += forged
                scrubbed = _strip_tag_like(links.scrub(unforged))
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

        # Die Link-Fußnote gehört **nicht** an den Body: Der geht in den Prompt, und der
        # Nutzer bekäme sie nie zu sehen (CT-14). Sie hängt jetzt der Composer an die
        # zugestellte Nachricht; die defangte Vollliste steht in `links_found`.
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

    def _to_text(self, cleaned_html: str) -> tuple[str, int]:
        """HTML→Text mit der konfigurierten Elementschranke (ADR-084, HC2-1)."""
        return html_to_text(cleaned_html, max_elements=self._limits.max_html_elements)

    def _html_diverges(self, body_plain: str, state: _WalkState) -> bool:
        """Weicht der (ignorierte) HTML-Teil inhaltlich vom Klartext-Teil ab? (CT-15)

        Verglichen werden Wort-Mengen, nicht Zeichen: Ein `multipart/alternative` ist
        legitim, wenn beide Teile dasselbe *sagen*; Formatierung, Reihenfolge und
        Link-Auszeichnung dürfen sich unterscheiden. Link-, Mail-, Tel- und Bild-Marker
        werden vorher aus beiden Seiten entfernt — sie entstehen nur auf der HTML-Seite
        (sichtbar gemachte `href`-Ziele, Alt-Texte) und wären sonst eine sichere Quelle
        für Falschmeldungen.

        Gemeldet wird nur ein **substanzieller** Überhang: mindestens
        :data:`_DIVERGENCE_MIN_NEW_WORDS` Wörter, die im Klartext gar nicht vorkommen, und
        zugleich mehr als :data:`_DIVERGENCE_RATIO` der HTML-Wörter. Der klassische
        Angriff (harmloser Klartext, bösartiges HTML) hat einen Überhang nahe 1,0.
        """
        if not state.body_html:
            return False
        converted: list[str] = []
        for html in state.body_html:
            cleaned_html, _ = clean_text(html)
            try:
                text, _ = self._to_text(cleaned_html)
            except HtmlTooComplexError:
                # Dieselbe Schranke wie im Body-Pfad (ADR-084): Der Divergenzcheck ist der
                # praktisch wichtigere Einstieg (er trifft auch Mails *mit* harmlosem
                # text/plain) und darf die Schranke deshalb nicht umgehen. Ohne Konversion
                # gibt es keine Vergleichsbasis — gemeldet wird die Ablehnung, nicht eine
                # Divergenz, die niemand geprüft hat.
                state.html_rejected = True
                return False
            converted.append(text)
        html_words = _content_words("\n".join(converted))
        if not html_words:
            return False
        plain_words = _content_words(body_plain)
        new_words = html_words - plain_words
        return (
            len(new_words) >= _DIVERGENCE_MIN_NEW_WORDS
            and len(new_words) / len(html_words) > _DIVERGENCE_RATIO
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
        if ctype in ENCRYPTED_CONTENT_TYPES:
            # Nur ein Faktum für Report und Hinweiszeile — der Teil läuft danach ganz
            # normal weiter (multipart/encrypted wird betreten, der Chiffretext landet
            # als geblockter Anhang in der Liste). Kein Fail-closed: Es ist nichts
            # schiefgegangen, es ist nur nichts zu lesen (ADR-082).
            state.encrypted = True
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
        unforged, forged = neutralize_forged_markers(cleaned)
        state.forged_markers += forged
        scrubbed = _strip_tag_like(links.scrub(unforged))
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
        return collapsed[:_MAX_DISPLAY_CHARS] or "(unknown sender)"

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
            html_divergent=state.html_divergent,
            html_rejected=state.html_rejected,
            blocked_attachments=sum(1 for info in attachments if not info.processed),
            reply_to_mismatch=_reply_to_mismatch(raw),
            return_path_mismatch=_return_path_mismatch(raw),
            id_collision=raw.id_collision,
            forged_markers=state.forged_markers,
            encrypted=state.encrypted,
            auth_results=_parse_auth_results(raw.auth_results_header),
        )


# --- Modul-Helfer (bewusst ohne Objekt-Zustand) -----------------------------------------------


def _strip_tag_like(text: str) -> str:
    """Neutralisiert HTML-Tag-artige Sequenzen in bereits konvertiertem Klartext."""
    return _RE_TAG_LIKE.sub(" ", text)


def neutralize_forged_markers(text: str) -> tuple[str, int]:
    """Ersetzt nachgebaute Datenblock-Marker durch :data:`FORGED_MARKER_TOKEN` (HC-5).

    Muss **vor** :func:`_strip_tag_like` laufen: Der Tag-Stripper greift ab dem dritten
    `<` und löscht `<MAILDIGEST-END-UNTRUSTED-DATA nonce>` restlos — ausgerechnet der
    perfekte Nachbau verschwände damit spurlos, und der modellunabhängige Detektor in
    `agents/summarizer.py` fände nichts mehr (F-SEC-5). Die Zahl der Funde geht als
    `SanitizationReport.forged_markers` weiter; das Token ist die zweite, textliche Spur.

    Returns:
        `(Text mit ersetzten Markern, Anzahl der Funde)`.
    """
    found = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        inner = match.group(0).upper()
        if all(word in inner for word in _FORGED_MARKER_WORDS):
            found += 1
            return FORGED_MARKER_TOKEN
        return match.group(0)

    return _RE_ANGLE_CHUNK.sub(replace, text), found


def _content_words(text: str) -> set[str]:
    """Wortmenge eines Textes für den Divergenz-Vergleich (CT-15)."""
    without_markers = _RE_DIVERGENCE_NOISE.sub(" ", text)
    return set(_RE_CONTENT_WORD.findall(without_markers.casefold()))


def _take_budget(text: str, budget: int) -> tuple[str, int, bool]:
    """Wendet das verbleibende Gesamt-Klartext-Budget an (SECURITY §4, T10)."""
    if len(text) <= budget:
        return text, budget - len(text), False
    cut = text[: max(budget, 0)].rstrip()
    result = f"{cut}\n{_TRUNCATION_MARKER}" if cut else _TRUNCATION_MARKER
    return result, 0, True


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
