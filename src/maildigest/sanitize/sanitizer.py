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
import time
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
from maildigest.sanitize.html_to_text import (
    HtmlBudget,
    HtmlTooComplexError,
    html_to_text,
)
from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.unicode_clean import clean_text, is_mixed_script_domain

__all__ = [
    "ENCRYPTED_CONTENT_TYPES",
    "FORGED_MARKER_TOKEN",
    "MAX_MIME_PARTS",
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

#: Vielfaches von `limits.max_text_chars`, auf das ROHER Klartext **vor** den teuren
#: Pässen (clean_text, Marker-Neutralisierung, Link-Scrub) vorgeschnitten wird (R-5/R-6).
#:
#: Kein eigenes Config-Feld: Das Endergebnis ist ohnehin auf `max_text_chars` gedeckelt,
#: der Vorschnitt ändert also nichts Sichtbares — er verhindert nur, dass eine 20-MB-Mail
#: (`max_mail_bytes` erlaubt 25 MB) Megabytes durch Pässe schickt, deren Ergebnis danach
#: auf 30 000 Zeichen fällt. Der Faktor ist mit Absicht grosszügig: Der Scrub kann Text
#: **verkürzen** (eine lange URL wird zu einem kurzen Marker), deshalb darf der Vorschnitt
#: nicht knapp am Endbudget liegen. 16 mal 30 000 = 480 000 Zeichen sind rund das Fünfzigfache
#: dessen, was eine reale Mail trägt (gemessen: 21-MB-Klartext 4,2 s → unter 0,2 s).
_RAW_TEXT_FACTOR = 16

#: Obergrenze der MIME-Teile, die überhaupt gelaufen werden (R-10).
#:
#: Modulkonstante wie `html_to_text.MAX_HTML_PARTS`: keine Betriebsgrösse, sondern eine
#: Struktur-Plausibilität. Eine reale Mail trägt eine Handvoll Teile; `max_attachments_
#: processed` (20) und `_MAX_ATTACHMENT_ENTRIES` (100) liegen weit darunter. Ohne diese
#: Schranke multipliziert die blosse *Zahl* der Teile jede andere Schranke — 200 000
#: winzige `text/plain`-Teile in einer 10,8-MB-Mail kosteten allein im Baumlauf 1,7 s.
#: Teile jenseits der Schranke werden nicht betreten; dass es sie gab, sagt ein einzelnes
#: Anhangs-Metadatum.
MAX_MIME_PARTS = 500

#: Harte Obergrenze für die Rekursionstiefe von :meth:`Sanitizer._walk` (O-1). Der
#: Baumlauf ist rekursiv; `limits.max_mime_depth` ist konfigurierbar und nach oben offen,
#: eine sehr grosse Einstellung würde aus einer tief verschachtelten Mail einen
#: `RecursionError` machen. Wirksam ist immer der kleinere der beiden Werte. Der Ingest
#: deckelt den Baum ohnehin früher (`ingest.imap_client.MAX_MIME_DEPTH`); diese Schranke
#: gilt für `RawMail`-Objekte aus anderen Quellen (Tests, künftige Aufrufer).
_MAX_MIME_DEPTH_HARD = 64

#: Name des Metadatums für die Teile jenseits von :data:`MAX_MIME_PARTS`.
_TOO_MANY_PARTS_NAME = "(mime-teile ueberschritten)"

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
class _RawTextBudget:
    """Restbudget an **rohen** Zeichen für die teuren Textpässe einer Mail (R-10).

    Vorbild ist :class:`~maildigest.sanitize.html_to_text.HtmlBudget`: ein Objekt je
    `sanitize()`-Lauf, geteilt von Body und **allen** Anhangstexten. Die dritte Iteration
    schnitt jedes Textstück einzeln auf `_RAW_TEXT_FACTOR * max_text_chars` — 20
    verarbeitete Anhänge multiplizierten das auf 9,6 Mio. Zeichen, obwohl `_take_budget`
    schon nach dem ersten Anhang nichts mehr durchliess. Verbraucht ist jetzt verbraucht.
    """

    remaining: int

    def take(self, text: str) -> tuple[str, bool]:
        """Gibt so viel rohen Text heraus, wie das Budget der Mail noch hergibt.

        Returns:
            ``(text, wurde_geschnitten)``. Ist das Budget erschöpft, ist der Text leer —
            der Aufrufer lässt ihn dann gar nicht erst durch `clean_text`, die
            Marker-Neutralisierung und den Link-Scrub laufen.
        """
        if len(text) <= self.remaining:
            self.remaining -= len(text)
            return text, False
        cut = text[: max(self.remaining, 0)]
        self.remaining = 0
        return cut, True


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
    #: Restbudget der HTML-Konvertierung für **diese** Mail (ADR-084-Nachtrag, HC2-1).
    #: Wird in `sanitize()` aus den Limits gesetzt; der Default hier ist nur Rückfall.
    html_budget: HtmlBudget = field(default_factory=HtmlBudget)
    #: Restbudget an rohem Klartext für **diese** Mail (R-10). Wird in `sanitize()` aus
    #: den Limits gesetzt; der Default hier ist nur Rückfall.
    raw_budget: _RawTextBudget = field(
        default_factory=lambda: _RawTextBudget(30_000 * _RAW_TEXT_FACTOR)
    )
    #: Zahl der betretenen MIME-Teile (Schranke :data:`MAX_MIME_PARTS`, R-10).
    parts_seen: int = 0
    #: Teile jenseits der Schranke — nur gezählt, nie betreten.
    parts_skipped: int = 0
    #: Bereits verbrauchte Wandzeit aller PDF-Extraktionen dieser Mail (R-11).
    pdf_seconds_used: float = 0.0


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
        state = _WalkState(
            html_budget=HtmlBudget(
                elements=self._limits.max_html_elements,
                byte_budget=self._limits.max_html_bytes,
            ),
            raw_budget=_RawTextBudget(self._limits.max_text_chars * _RAW_TEXT_FACTOR),
        )
        self._walk(message, 0, state)
        if state.parts_skipped:
            # R-10: sichtbar statt still — dieselbe Politik wie beim Tiefenlimit (ADR-030).
            self._add_attachment_meta(
                state,
                filename=_TOO_MANY_PARTS_NAME,
                declared="multipart/mixed",
                kind="unknown",
                size=0,
            )

        # Body: text/plain bevorzugt; sonst text/html → Text (SECURITY §4).
        if state.body_plain:
            body_raw = "\n\n".join(state.body_plain)
            # R-6: Der Vorschnitt liegt VOR dem Divergenzcheck — dessen Wortmengen-Vergleich
            # liefe sonst über die vollen 20 MB, die `max_mail_bytes` zulässt. Verglichen
            # wird damit genau der Klartext, der auch zusammengefasst wird; fehlt dem
            # Vergleich ein abgeschnittener Teil, meldet der Check eher Divergenz als
            # weniger — die fail-safe Richtung (ADR-036).
            body_raw, precut = state.raw_budget.take(body_raw)
            state.truncated = state.truncated or precut
            # CT-15: Der Nutzer sieht in seinem Mailprogramm den HTML-Teil. Weicht der
            # inhaltlich ab, wird das vermerkt — sonst beschreibt die Zusammenfassung
            # unbemerkt einen anderen Text als den angezeigten.
            state.html_divergent = self._html_diverges(body_raw, state)
        elif state.body_html:
            converted: list[str] = []
            for html in state.body_html:
                try:
                    text, hidden, removed = self._to_text(html, state)
                except HtmlTooComplexError:
                    # ADR-084/HC2-1: Der Teil ist zu gross/zu tief/zu viel, um ihn in der
                    # Poll-Periode sicher zu konvertieren. Er gilt als nicht verarbeitet —
                    # kein roher HTML-Text geht weiter (I1), die Mail läuft mit dem Rest
                    # (hier: ohne Body) fail-safe durch und der Nutzer sieht die Hinweiszeile.
                    state.html_rejected = True
                    continue
                state.control_chars_removed += removed
                state.hidden_removed += hidden
                converted.append(text)
            # R-6/R-10: Das Rohtext-Budget läuft über die GANZE Mail. Im Klartext-Zweig ist
            # es oben schon abgebucht (vor dem Divergenzcheck), hier für den HTML-Zweig.
            body_raw, precut = state.raw_budget.take("\n\n".join(converted))
            state.truncated = state.truncated or precut
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
                # Dasselbe Rohtext-Budget wie beim Body, als Restzähler über die ganze Mail
                # (R-10). Ein text/plain-Anhang unterliegt keiner eigenen Grössenschranke,
                # und `max_attachments_processed` (20) multiplizierte einen Vorschnitt je
                # Stück auf ein Vielfaches; PDF-Text ist über `pdf_max_output_chars`
                # ohnehin gedeckelt.
                had_text = bool(raw_text)
                raw_text, precut = state.raw_budget.take(raw_text)
                state.truncated = state.truncated or precut
                if had_text and not raw_text:
                    # Budget erschöpft: Dieser Anhang läuft gar nicht mehr durch die teuren
                    # Pässe und gilt — sichtbar — als nicht verarbeitet.
                    attachments.append(
                        info.model_copy(update={"processed": False, "extracted_chars": 0})
                    )
                    continue
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

    def _to_text(self, html: str, state: _WalkState) -> tuple[str, int, int]:
        """HTML→Text gegen das Restbudget **dieser Mail** (ADR-084-Nachtrag, HC2-1).

        Reihenfolge ist hier der ganze Punkt: `admit()` prüft Teilezahl und Bytelänge am
        **rohen** Teil, bevor irgendetwas ihn anfasst — weder `clean_text` noch der Parser
        sehen einen Teil, der ohnehin abgelehnt wird. Element- und Tiefenschranke können
        das nicht leisten, sie kennen den Baum erst nach dem Parsen.

        Returns:
            ``(text, hidden_removed, control_chars_removed)``.

        Raises:
            HtmlTooComplexError: Eine der Schranken ist erschöpft (ADR-084).
        """
        state.html_budget.admit(html)
        cleaned_html, removed = clean_text(html)
        text, hidden = html_to_text(cleaned_html, budget=state.html_budget)
        return text, hidden, removed

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
            try:
                text, _hidden, _removed = self._to_text(html, state)
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
        """Läuft den MIME-Baum manuell (Tiefen- und Teilezahl-Limit, message/rfc822 nie)."""
        state.parts_seen += 1
        if state.parts_seen > MAX_MIME_PARTS:
            # R-10: Die blosse Zahl der Teile darf keine Schranke multiplizieren. Jenseits
            # der Obergrenze wird nichts mehr betreten — auch kein Teilbaum; gezählt wird,
            # dass es sie gab (ein Metadatum am Ende von `sanitize()`).
            state.parts_skipped += 1
            return
        if depth > min(self._limits.max_mime_depth, _MAX_MIME_DEPTH_HARD):
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
            # R-11: `pdf_timeout_seconds` gilt je Anhang; 20 PDFs summierten sich damit auf
            # ~400 s Wandzeit je Mail — weit über `poll_interval_seconds`. Darüber liegt
            # jetzt ein Zeitbudget für die ganze Mail (ADR-029-Nachtrag).
            remaining = float(self._limits.pdf_time_budget_seconds) - state.pdf_seconds_used
            if remaining <= 0:
                # Budget aufgebraucht: Es startet gar kein Kindprozess mehr; der Anhang
                # gilt wie beim Timeout als nicht verarbeitet (fail-safe, I6).
                self._add_attachment_meta(
                    state, filename=filename, declared=declared, kind=kind, size=len(data)
                )
                return
            started = time.monotonic()
            text = extract_pdf_text(
                data,
                timeout_seconds=min(float(self._limits.pdf_timeout_seconds), remaining),
                max_input_bytes=self._limits.pdf_max_input_bytes,
                max_output_chars=self._limits.pdf_max_output_chars,
            )
            state.pdf_seconds_used += time.monotonic() - started

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
        display, address = _safe_parseaddr(raw.from_addr)
        # O-3: Anzeigename und Adress-Rückfall laufen durch denselben Pfad (Steuerzeichen,
        # gefälschte Marker, Link-Scrub, Tag-Reste) — sonst gelangte eine Roh-URL oder ein
        # gefälschter `[Link #n:]`-Marker aus einem quotierten Lokalteil in den Prompt.
        # Ein Anzeigename darf nicht mit `/` oder `:` beginnen: `From: //…` fräse sonst
        # das Strukturpräfix der Zustellzeile an (Skeptiker O-3, vierter Durchgang).
        text = display or address or raw.from_address or raw.from_addr
        cleaned = self._sanitize_header(text, links, state, _MAX_DISPLAY_CHARS)
        return cleaned.lstrip("/:. ") or "(unknown sender)"

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
            links_capped=links.links_capped,
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


def _safe_parseaddr(text: str) -> tuple[str, str]:
    """`parseaddr` ohne Ausnahme: ``("", "")``, wenn der Legacy-Parser scheitert (O-3)."""
    try:
        return parseaddr(text)
    except Exception:
        return "", ""


def _reply_to_mismatch(raw: RawMail) -> bool:
    """Reply-To weicht vom From ab (deterministisches Kritiker-Signal, F-CRIT-3).

    Verglichen werden die Adressen, die der RFC-5322-Parser beim Ingest gelesen hat
    (`from_address`, `reply_to_address`, O-3); `parseaddr` ist nur noch Rückfall für
    RawMails ohne diese Felder. Der strikte `parseaddr` liest aus `"a["@evil.example`
    nichts — zwei „Unbekannte" hätten die Warnung stumm geschaltet, obwohl das
    Mailprogramm die Antwort an evil.example richtet.
    """
    if not raw.reply_to:
        return False
    if raw.reply_to_address is None:  # RawMail ohne Parser-Felder: Rückfall
        replies = [_safe_parseaddr(raw.reply_to)[1].strip().lower()]
    else:
        # Mailprogramme antworten an ALLE Reply-To-Adressen (O-3, vierter Skeptiker):
        # `Reply-To: x@bank.example, y@evil.example` ist ein Mismatch, auch wenn die
        # erste Adresse der Absender ist.
        replies = [r.strip().lower() for r in (raw.reply_to_addresses or [raw.reply_to_address])]
    sender = (raw.from_address or _safe_parseaddr(raw.from_addr)[1]).strip().lower()
    if not any(replies) and not sender:
        # ADR-020 (b): zwei Unbekannte sind weder Übereinstimmung noch Mismatch-Beweis.
        return False
    if not sender or not all(replies):
        # R-9/S-2: **eine** Unbekannte neben einer Bekannten ist genau der Fall, für den
        # die Warnung gedacht ist — schweigen hiesse, dass ein Angreifer sie abschaltet,
        # indem er den `From` zerstört (R-9) oder den `Reply-To` so schreibt, dass er
        # vorhanden, aber für `parseaddr` unlesbar ist (S-2: offener Kommentar, Token
        # hinter der Klammer). Ein fehlender `Reply-To` bleibt oben kein Mismatch.
        return True
    return any(reply != sender for reply in replies)


def _return_path_mismatch(raw: RawMail) -> bool:
    """Return-Path-Domain weicht von der From-Domain ab (F-CRIT-3).

    Leere/unbekannte Werte sind nie ein Treffer (ADR-020: zwei Unbekannte sind keine
    Übereinstimmung, aber auch kein Mismatch-Beweis).
    """
    if not raw.return_path_domain:
        return False
    if not raw.from_domain:
        # R-9: unbekannte Absender-Domain + bekannter Rückweg ⇒ Warnfall. ADR-020 (b) bleibt
        # für den Fall gültig, dass **beide** Seiten unbekannt sind: dann kein Treffer.
        return True
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
