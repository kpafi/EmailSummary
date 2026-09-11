"""Zusammenbau der versandfertigen :class:`~maildigest.models.DigestMessage` (Stufe 5).

Implementiert das Protokoll `OutputComposer` aus :mod:`maildigest.pipeline`
(:meth:`DigestComposer.compose` **und** :meth:`DigestComposer.compose_failure`).
Format: docs/ARCHITECTURE.md §7.

Jedes Feld, das in die Nachricht wandert, ist untrusted (LLM-Ausgabe oder Mail-Metadatum)
und läuft durch :func:`maildigest.output.sanitizer.scrub_field`; die fertige Nachricht
läuft zusätzlich durch :func:`maildigest.output.sanitizer.final_guard` und wird erst
danach in messenger-taugliche Teile gesplittet (I3/I4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from maildigest.config import Config
from maildigest.models import (
    AttachmentInfo,
    CriticVerdict,
    DigestMessage,
    FailureNotice,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.output.sanitizer import (
    DISCORD_MAX_PART_CHARS,
    SIGNAL_MAX_PART_CHARS,
    TELEGRAM_MAX_PART_CHARS,
    final_guard,
    scrub_field,
    scrub_plain,
    split_parts,
)
from maildigest.sanitize.links import LinkCollector, build_footnote

__all__ = [
    "LOW_DIGEST_DEDUPE_KEY",
    "SELFTEST_DEDUPE_KEY",
    "DigestComposer",
    "LowDigestItem",
    "part_limit_for",
]

#: `DigestMessage.dedupe_key` des täglichen Sammel-Digests — er gehört zu keiner
#: einzelnen Mail; die Zustell-Warteschlange erkennt ihn daran (WP8).
LOW_DIGEST_DEDUPE_KEY = "low-digest"

#: `DigestMessage.dedupe_key` der Betriebsnachrichten der CLI (Testnachricht aus
#: `connect-messenger`, WP9). Gehört ebenfalls zu keiner Mail.
SELFTEST_DEDUPE_KEY = "cli-selftest"

#: Zeichenlimit je Messenger (docs/ARCHITECTURE.md §5, `[messenger] active`).
_PART_LIMITS: dict[str, int] = {
    "telegram": TELEGRAM_MAX_PART_CHARS,
    "discord": DISCORD_MAX_PART_CHARS,
    "signal": SIGNAL_MAX_PART_CHARS,
}

#: Einzellimits der untrusted Felder — verhindert, dass ein Feld die Nachricht sprengt.
_MAX_HEADLINE_CHARS = 120
_MAX_SUMMARY_CHARS = 3000
_MAX_ATTACHMENT_SUMMARY_CHARS = 400
_MAX_REASON_CHARS = 200
_MAX_DISPLAY_CHARS = 80
_MAX_DOMAIN_CHARS = 100
_MAX_FILENAME_CHARS = 80

#: Höchstzahl der im Banner genannten Kritiker-Gründe.
_MAX_RISK_REASONS = 5

#: Höchstzahl der namentlich genannten, nicht verarbeiteten Anhänge.
_MAX_LISTED_ATTACHMENTS = 10

#: Auth-Ergebnisse, die kein Warnsignal sind.
_AUTH_OK = frozenset({"pass", "none", "neutral", "policy"})

#: Höchstzahl namentlich genannter Mails im Sammel-Digest (Rest als „und N weitere").
_MAX_LOW_DIGEST_ITEMS = 60

#: Zeichenlimit einer Kategorie-Überschrift im Sammel-Digest.
_MAX_CATEGORY_CHARS = 40

#: Höchstzahl der Nachbrenner-/Split-Runden in :meth:`DigestComposer._finalize` (HT-4).
_MAX_GUARD_ROUNDS = 4


@dataclass(frozen=True)
class LowDigestItem:
    """Eine Zeile des täglichen Sammel-Digests (F-SUM-5).

    Bewusst kein Bezug auf `state/db.py`: Der Composer kennt die Persistenz nicht, der
    Runner (WP8) bildet die Warteschlangen-Einträge hierauf ab.
    """

    headline: str
    category: str
    from_domain: str


def part_limit_for(messenger: str) -> int:
    """Zeichenlimit eines Nachrichtenteils für den aktiven Messenger.

    Unbekannte Namen bekommen das kleinste bekannte Limit — lieber unnötig splitten als
    eine abgeschnittene Nachricht.
    """
    return _PART_LIMITS.get(messenger, min(_PART_LIMITS.values()))


def _format_size(size_bytes: int) -> str:
    """Deutsche Größenangabe: `812 B`, `34 KB`, `1,2 MB`."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{round(size_bytes / 1024)} KB"
    megabytes = size_bytes / (1024 * 1024)
    return f"{megabytes:.1f}".replace(".", ",") + " MB"


def _format_date(value: datetime | None) -> str:
    """`TT.MM. HH:MM` oder ein Platzhalter, wenn der Date-Header fehlte/kaputt war."""
    if value is None:
        return "date unknown"
    return f"{value:%d.%m. %H:%M}"


class DigestComposer:
    """Baut aus Summary + Verdict (bzw. aus einer FailureNotice) die fertige Nachricht.

    Zustandslos bis auf das Zeichenlimit — eine Instanz kann für alle Mails benutzt
    werden. Der :class:`LinkCollector` wird pro Nachricht neu erzeugt, damit die
    Marker-Nummerierung innerhalb einer Nachricht durchläuft und nicht über Mails hinweg
    weiterzählt.
    """

    def __init__(
        self,
        *,
        part_limit: int = TELEGRAM_MAX_PART_CHARS,
        link_footnote: bool = False,
    ) -> None:
        """Args:
            part_limit: Zeichenlimit eines Nachrichtenteils (Default: Telegram).
            link_footnote: Hängt die defangte Link-Liste an die Nachricht
                (`[links] footnote`). Sie gehört an die **Zustellung** und nicht in den
                Prompt — genau das war der Fehler hinter CT-14.
        """
        if part_limit < 1:
            raise ValueError("part_limit muss mindestens 1 sein.")
        self._part_limit = part_limit
        self._link_footnote = link_footnote

    @classmethod
    def from_config(cls, config: Config) -> DigestComposer:
        """Baut den Composer mit dem Limit des in `[messenger] active` gewählten Adapters."""
        return cls(
            part_limit=part_limit_for(config.messenger.active),
            link_footnote=config.links.footnote,
        )

    @property
    def part_limit(self) -> int:
        """Das konfigurierte Zeichenlimit eines Nachrichtenteils."""
        return self._part_limit

    def __repr__(self) -> str:
        """Repräsentation ohne jeden Inhalt (I5)."""
        return (
            f"DigestComposer(part_limit={self._part_limit}, "
            f"link_footnote={self._link_footnote})"
        )

    # --- OutputComposer -----------------------------------------------------------

    def compose(
        self, mail: SanitizedMail, summary: Summary, verdict: CriticVerdict
    ) -> DigestMessage:
        """Baut die Nachricht zu einer erfolgreich geprüften Mail (docs/ARCHITECTURE.md §7)."""
        collector = LinkCollector()
        lines: list[str] = []
        is_warning = verdict.phishing_risk == "high"

        if is_warning:
            lines.append(self._banner(verdict, collector))

        headline = scrub_field(
            summary.headline, collector=collector, max_chars=_MAX_HEADLINE_CHARS
        ) or "(no summary)"
        tag = " [important]" if summary.importance == "high" else ""
        lines.append(f"📧 {_one_line(headline)}{tag}")
        lines.append(self._sender_line(mail))

        body = scrub_field(
            summary.summary_text, collector=collector, max_chars=_MAX_SUMMARY_CHARS
        )
        if body:
            lines.append(body)

        lines.extend(self._attachment_summary_lines(summary, collector))

        unprocessed = self._unprocessed_line(mail.attachments)
        if unprocessed:
            lines.append(unprocessed)

        hints = self._hints_line(mail.sanitization_report, summary, verdict, collector)
        if hints:
            lines.append(hints)

        if self._link_footnote and mail.links_found:
            # Die Einträge sind bereits defanged (`hxxps[:]//…`); `_finalize` lässt diese
            # Formen als „bereits sicher" unangetastet (ADR-059), bricht aber alles auf,
            # was doch noch lebendig aussieht. I3 bleibt gewahrt (CT-14).
            lines.append(build_footnote(mail.links_found))

        return DigestMessage(
            parts=self._finalize("\n".join(lines)),
            importance=summary.importance,
            is_warning=is_warning,
            dedupe_key=mail.dedupe_key,
        )

    def compose_failure(self, notice: FailureNotice) -> DigestMessage:
        """Baut die Metadaten-Notiz des Fail-closed-Pfades (I6, F-OPS-3).

        Enthält bewusst keinen Mail-Inhalt: Der Betreff kommt aus dem Not-Sanitizer der
        Pipeline und wird hier trotzdem noch einmal gescrubbt (die Notiz kann entstehen,
        *weil* der reguläre Sanitizer versagt hat).
        """
        collector = LinkCollector()
        domain = _one_line(scrub_plain(notice.from_domain, max_chars=_MAX_DOMAIN_CHARS))
        subject = _one_line(
            scrub_field(
                notice.subject_sanitized, collector=collector, max_chars=_MAX_HEADLINE_CHARS
            )
        )
        lines = [
            "⚠️ This mail could not be processed safely — no content delivered.",
            f"From: {domain or 'unknown'}",
            f"Subject: {subject or '(no subject)'}",
            f"Stage: {_label(notice.stage)} · Reason: {_label(notice.reason_class)}",
            "Open your real mailbox to read it.",
        ]
        return DigestMessage(
            parts=self._finalize("\n".join(lines)),
            importance="normal",
            is_warning=False,
            dedupe_key=notice.dedupe_key,
        )

    def compose_low_digest(self, items: Sequence[LowDigestItem]) -> DigestMessage:
        """Baut den täglichen Sammel-Digest der `low`-Mails (F-SUM-5, ARCHITECTURE §7).

        Eine Nachricht, nach Kategorie gruppiert, je Mail eine Zeile
        ``• <headline> (<domain>)``. Die Felder sind bereits beim Einreihen sanitisiert
        worden; sie laufen hier trotzdem erneut durch `scrub_field`/`final_guard`
        (Defense in Depth, I3/I4).

        Raises:
            ValueError: Aufruf ohne Einträge — ein leerer Digest wird nie zugestellt.
        """
        if not items:
            raise ValueError("A digest without entries is not created.")
        collector = LinkCollector()
        grouped: dict[str, list[LowDigestItem]] = {}
        for item in items[:_MAX_LOW_DIGEST_ITEMS]:
            category = (
                _one_line(scrub_plain(item.category, max_chars=_MAX_CATEGORY_CHARS))
                or "other"
            )
            grouped.setdefault(category, []).append(item)

        total = len(items)
        overview = ", ".join(
            f"{len(entries)} {category}"
            for category, entries in sorted(
                grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])
            )
        )
        lines = [f"🗂 {total} low-priority mails: {overview}"]
        for category, entries in sorted(
            grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])
        ):
            lines.append(f"\n{category} ({len(entries)}):")
            for item in entries:
                headline = _one_line(
                    scrub_field(
                        item.headline, collector=collector, max_chars=_MAX_HEADLINE_CHARS
                    )
                )
                domain = _one_line(scrub_plain(item.from_domain, max_chars=_MAX_DOMAIN_CHARS))
                lines.append(f"• {headline or '(no subject)'} ({domain or 'unknown'})")
        rest = total - min(total, _MAX_LOW_DIGEST_ITEMS)
        if rest > 0:
            lines.append(f"... and {rest} more")

        return DigestMessage(
            parts=self._finalize("\n".join(lines)),
            importance="low",
            is_warning=False,
            dedupe_key=LOW_DIGEST_DEDUPE_KEY,
        )

    def compose_plain(self, text: str) -> DigestMessage:
        """Baut eine **im Code formulierte** Betriebsnachricht (WP9, ADR-054).

        Drei Aufrufer: die beiden Testnachrichten von `connect-messenger` und `test`
        (`cli.py`) sowie die Antwort auf `/status` (`runner.Runner.handle_command`). Der
        Text stammt nie aus einer Mail und nie aus einem Modell — er läuft trotzdem durch
        :meth:`_finalize`, damit `DigestMessage.parts` weiterhin auf genau einem Weg
        entsteht (docs/SECURITY.md §5, I3/I4). Es gibt bewusst keinen Parameter für
        Wichtigkeit oder Warn-Flag: Betriebsnachrichten sind immer `normal`/keine Warnung.

        **Regel für Aufrufer (HC-28):** `_finalize` ist Nachbrenner und Split, **kein**
        Feld-Scrub. Wer einen variablen Anteil einsetzt — `/status` interpoliert den
        Ordnernamen aus der Konfiguration —, schickt ihn vorher durch
        :func:`~maildigest.output.sanitizer.scrub_plain` bzw. `scrub_field`. Sonst
        überleben Struktur-Emoji am Zeilenanfang und Messenger-Markup (ADR-062, CT-8).
        """
        return DigestMessage(
            parts=self._finalize(text),
            importance="normal",
            is_warning=False,
            dedupe_key=SELFTEST_DEDUPE_KEY,
        )

    # --- Bausteine ----------------------------------------------------------------

    def _finalize(self, text: str) -> list[str]:
        """Nachbrenner + Split bis zum Fixpunkt — der einzige Weg von Text zu `parts`.

        Der Nachbrenner allein reicht **nicht**, wenn er nur über dem ungeteilten Text
        läuft (HT-4): :func:`split_parts` schneidet notfalls mitten in einem Wort, und
        genau dieser Schnitt kann aus einem unauffälligen Token ein Bruchstück machen, das
        für sich genommen wie eine Domain aussieht — links wie rechts. Aus
        ``-0000000.beispiel`` (führender Bindestrich, deshalb kein Domain-Token) wurde
        beim Schnitt ``0000000.beispiel``.

        Deshalb läuft :func:`final_guard` **nach** dem Split noch einmal über jeden Teil;
        wächst ein Teil dadurch über das Limit (Defangen fügt ``[.]`` hinzu), wird er
        erneut geteilt. Das terminiert: Auf bereits gebrochenem Text fügt der Nachbrenner
        nur noch Klammern hinzu und es gibt endlich viele Punkte. Die Rundenzahl ist
        trotzdem gedeckelt — eine Endlosschleife im Zustellpfad wäre schlimmer als ein
        theoretisch ungeprüftes Bruchstück nach vier Runden.
        """
        parts = split_parts(final_guard(text).strip(), self._part_limit)
        for _ in range(_MAX_GUARD_ROUNDS):
            guarded = [final_guard(part) for part in parts]
            if guarded == parts:
                break
            parts = [
                piece for part in guarded for piece in split_parts(part, self._part_limit)
            ]
        return parts

    def _banner(self, verdict: CriticVerdict, collector: LinkCollector) -> str:
        """Warn-Banner bei `phishing_risk == "high"` (F-CRIT-2)."""
        reasons = [
            _one_line(scrub_field(reason, collector=collector, max_chars=_MAX_REASON_CHARS))
            for reason in verdict.risk_reasons[:_MAX_RISK_REASONS]
        ]
        joined = ", ".join(reason for reason in reasons if reason)
        return f"⚠️ SUSPECTED PHISHING: {joined}" if joined else "⚠️ SUSPECTED PHISHING"

    def _sender_line(self, mail: SanitizedMail) -> str:
        """`From: <Anzeigename> (<domain>) · <TT.MM. HH:MM>`."""
        display = _one_line(scrub_plain(mail.from_display, max_chars=_MAX_DISPLAY_CHARS))
        domain = _one_line(scrub_plain(mail.from_domain, max_chars=_MAX_DOMAIN_CHARS))
        if display and domain and display.lower() != domain.lower():
            sender = f"{display} ({domain})"
        else:
            sender = display or domain or "unknown"
        return f"From: {sender} · {_format_date(mail.date)}"

    def _attachment_summary_lines(
        self, summary: Summary, collector: LinkCollector
    ) -> list[str]:
        """Je verarbeitetem Anhang eine Zeile `— <datei>: <1-2 Sätze>` (F-SUM-4)."""
        lines: list[str] = []
        for filename, text in list(summary.attachment_summaries.items())[
            :_MAX_LISTED_ATTACHMENTS
        ]:
            name = _one_line(scrub_plain(filename, max_chars=_MAX_FILENAME_CHARS))
            content = scrub_field(
                text, collector=collector, max_chars=_MAX_ATTACHMENT_SUMMARY_CHARS
            )
            if not name and not content:
                continue
            lines.append(f"— {name or '(file)'}: {content or '(no summary)'}")
        return lines

    def _unprocessed_line(self, attachments: list[AttachmentInfo]) -> str:
        """`📎 Not processed: rechnung.docx (34 KB), setup.exe (1,2 MB)` (F-SEC-4)."""
        blocked = [item for item in attachments if not item.processed]
        if not blocked:
            return ""
        shown: list[str] = []
        for item in blocked[:_MAX_LISTED_ATTACHMENTS]:
            name = _one_line(
                scrub_plain(item.filename_sanitized, max_chars=_MAX_FILENAME_CHARS)
            )
            shown.append(f"{name or '(unnamed)'} ({_format_size(item.size_bytes)})")
        rest = len(blocked) - len(shown)
        suffix = f" and {rest} more" if rest > 0 else ""
        return f"📎 Not processed: {', '.join(shown)}{suffix}"

    def _hints_line(
        self,
        report: SanitizationReport,
        summary: Summary,
        verdict: CriticVerdict,
        collector: LinkCollector,
    ) -> str:
        """`🔍 Notes: …` aus deterministischen Signalen + Injection-Flag (T1/T12)."""
        hints: list[str] = []
        if summary.injection_suspected:
            hints.append("the mail contained instructions aimed at the AI (ignored)")
        if report.encrypted:
            # ADR-082: kein Fehler, sondern die Erklärung für die fehlende Zusammenfassung.
            # Ohne diese Zeile sieht der Nutzer nur „Mail without displayable content"
            # und hält eine verschlüsselte Mail für eine kaputte (HC-33).
            hints.append("encrypted (PGP/S-MIME) — content not readable by design")
        if report.id_collision:
            # HC-10/ADR-079: Die Message-ID dieser Mail war bereits von einer inhaltlich
            # anderen belegt. Beide werden zugestellt; der Nutzer soll wissen, warum eine
            # Rechnungsnummer zweimal auftaucht.
            hints.append("Message-ID collides with an earlier mail")
        failed_auth = [
            f"{key.upper()}={value}"
            for key, value in sorted(report.auth_results.items())
            if value.lower() not in _AUTH_OK
        ]
        if failed_auth:
            hints.append("sender checks failed: " + ", ".join(failed_auth))
        if report.punycode_domains:
            hints.append("punycode domain(s): " + ", ".join(report.punycode_domains[:3]))
        if report.mixed_script_domains:
            hints.append(
                "mixed writing systems: " + ", ".join(report.mixed_script_domains[:3])
            )
        if report.html_divergent:
            # CT-15: Das Mailprogramm des Nutzers zeigt den HTML-Teil, zusammengefasst wurde
            # der Klartext-Teil. Ohne diesen Hinweis wäre eine „harmlos"-Meldung zu einem
            # Text möglich, den der Nutzer nie zu Gesicht bekommt (ADR-067).
            hints.append("HTML part differs from the text part")
        if report.hidden_text_removed:
            # Deterministisch, ohne jedes Modell (CT-6): Der Sanitizer *weiß*, dass im HTML
            # unsichtbarer Text stand. Bewusst als eigener Hinweis und nicht über
            # `injection_suspected`: Unsichtbarer Text ist auch der legitime
            # Newsletter-Preheader, „Anweisungen an die KI" wäre dann schlicht falsch.
            hints.append("hidden text removed from the HTML")
        if report.reply_to_mismatch:
            hints.append("reply address differs from the sender")
        if report.return_path_mismatch:
            hints.append("return-path domain differs")
        if report.truncated:
            hints.append("text truncated")
        if verdict.phishing_risk == "low" and verdict.risk_reasons:
            reasons = [
                _one_line(
                    scrub_field(reason, collector=collector, max_chars=_MAX_REASON_CHARS)
                )
                for reason in verdict.risk_reasons[:_MAX_RISK_REASONS]
            ]
            joined = ", ".join(reason for reason in reasons if reason)
            if joined:
                hints.append(f"critic: {joined}")
        if not hints:
            return ""
        return "🔍 Notes: " + "; ".join(
            _one_line(scrub_field(hint, collector=collector)) for hint in hints
        )


def _one_line(text: str) -> str:
    """Presst ein Feld auf eine Zeile — Zeilenumbrüche würden das Format zerschießen."""
    return " ".join(text.split())


def _label(value: str) -> str:
    """Normalisiert eine Stufen-/Fehlerklassen-Kennung auf ein knappes ASCII-Label (I5)."""
    kept = [char if char.isalnum() or char in "_-" else " " for char in value[:64]]
    return " ".join("".join(kept).split()) or "unknown"
