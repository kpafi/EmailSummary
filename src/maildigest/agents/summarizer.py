"""Summarizer-Agent (Stufe 3): `SanitizedMail` → `Summary` über ein rechteloses LLM.

Vertrag: `pipeline.Summarizer` (`summarize(mail) -> Summary`). Härtung: docs/SECURITY.md §5,
Invarianten I2/I4/I8. Umsetzung: WP5.

Ablauf eines Aufrufs:

1. Zufällige Blockkennung ziehen (Delimiter-Spoofing-Schutz, SECURITY §5 Punkt 1).
2. System-Prompt (Rolle → gelabelte Custom-Instructions → unüberschreibbare
   Sicherheitsregeln) und User-Message (Programm-Fakten + delimitierter Datenblock) aus
   `llm/prompts.py` bauen — die Wortlaute stehen ausschließlich dort.
3. `llm.schema.complete_json` erzwingt das `Summary`-Schema; scheitert auch der eine
   Reparaturversuch, fliegt `LLMInvalidResponse` nach oben und die Pipeline macht daraus
   eine Metadaten-Notiz (I6, fail-closed).
4. **Deterministische Nachkontrolle** (:func:`enforce_output_policy`): Die LLM-Ausgabe ist
   untrusted (I4). Jedes Textfeld wird auf URLs, Markdown-Links, HTML und Steuerzeichen
   gescannt; ein Fund wird entfernt **und** setzt `injection_suspected = true`. Zusätzlich
   werden Längen erzwungen, leere Felder normalisiert und erfundene Anhang-Schlüssel
   verworfen.

Bewusst **nicht** hier: die Zustell-Schwelle (`deliver_min_importance`). Sie wertet
`pipeline.process_mail` aus; eine zweite Auswertung wäre eine zweite Wahrheit.
"""

from __future__ import annotations

import re
import unicodedata

from maildigest.config import Config
from maildigest.llm import prompts
from maildigest.llm.base import LLMProvider
from maildigest.llm.factory import build_provider, max_tokens_for
from maildigest.llm.schema import complete_json
from maildigest.models import SanitizedMail, Summary

__all__ = [
    "HEADLINE_MAX_CHARS",
    "REDACTION_MARKER",
    "SummarizerAgent",
    "describe_without_body",
    "enforce_output_policy",
    "scrub_text",
]

#: Ersatz für entfernte Fundstellen — sichtbar, damit der Nutzer die Lücke bemerkt.
REDACTION_MARKER = "[entfernt]"

#: Harte Obergrenze der Headline (identisch zu `Summary.headline`, ARCHITECTURE §3).
HEADLINE_MAX_CHARS = 100

#: Wie viele nicht verarbeitete Anhänge der deterministische Ersatztext aufzählt.
_MAX_LISTED_BLOCKED = 5

#: Markdown-/HTML-Konstrukte, die als Ganzes verschwinden (vor dem Token-Scan).
_STRUCTURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Markdown-Link/-Bild: [text](ziel) bzw. ![alt](ziel)
    re.compile(r"!?\[[^\]\n]{0,200}\]\([^)\n]{0,500}\)"),
    # Markdown-Referenzdefinition am Zeilenanfang: [1]: ziel
    re.compile(r"(?m)^\s*\[[^\]\n]{0,80}\]:\s*\S+"),
    # HTML-/XML-Tag
    re.compile(r"</?[A-Za-z][^>\n]{0,200}>"),
    # Numerische HTML-Entity (Obfuskationsvektor)
    re.compile(r"&#x?[0-9A-Fa-f]{1,8};?"),
)

#: Muster, deren Fund das ganze umgebende Wort ersetzt (URLs und ihre Obfuskationen).
#: Bewusst **nicht** enthalten: nackte Domains ohne Pfad — die Sanitizer-Marker
#: `[Link #n: domain.tld]` sollen den Summarizer passieren dürfen (I3 erlaubt den bloßen
#: Domain-Namen als Text), und der Output-Sanitizer (WP7) prüft ohnehin noch einmal.
_URL_TOKEN_RE = re.compile(
    r"""(?xi)
      [a-z][a-z0-9+.\-]{0,15}://          # irgendein Schema mit :// (auch einbuchstabig)
    | h\W{0,2}x\W{0,2}x\W{0,2}p           # hxxp / h.x.x.p
    | \bwww\s*\.                          # www.
    | \bmailto\s*:                        # mailto:
    | \btel\s*:                           # tel:
    | \(\s*\.\s*\)                        # example(.)com
    | \[\s*\.\s*\]                        # example[.]com
    | \(\s*dot\s*\)                       # example(dot)com
    | [A-Za-z0-9\-]{1,63}\s*\.\s*[A-Za-z]{2,24}\s*/   # domain.tld/pfad
    """
)


def _strip_control_chars(text: str) -> tuple[str, bool]:
    """Entfernt alle Unicode-„C*"-Zeichen außer Tab und Zeilenumbruch.

    Gleiche Politik wie `sanitize/unicode_clean.py` (SECURITY §4, F-SEC-10) — hier auf der
    Ausgabeseite, weil das Modell Zero-Width-/Bidi-Zeichen selbst erzeugen oder aus dem
    Datenblock durchreichen kann.
    """
    kept: list[str] = []
    removed = False
    for char in text:
        if char in "\n\t" or not unicodedata.category(char).startswith("C"):
            kept.append(char)
        else:
            removed = True
    return "".join(kept), removed


def _redact_tokens(text: str) -> tuple[str, bool]:
    """Ersetzt jedes Wort, das ein URL-Muster enthält, vollständig durch den Marker.

    Auf das ganze Wort ausgedehnt, damit aus `https://boese.example/pfad` nicht der Rest
    `boese.example/pfad` übrig bleibt.
    """
    spans: list[tuple[int, int]] = []
    for match in _URL_TOKEN_RE.finditer(text):
        start, end = match.start(), match.end()
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        while end < len(text) and not text[end].isspace():
            end += 1
        spans.append((start, end))
    if not spans:
        return text, False

    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(REDACTION_MARKER)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), True


def scrub_text(text: str) -> tuple[str, bool]:
    """Säubert genau ein Textfeld der LLM-Ausgabe deterministisch.

    Args:
        text: Rohwert aus der Modellantwort (untrusted, I4).

    Returns:
        `(gesäuberter Text, verdächtig)`. `verdächtig` ist True, sobald irgendetwas
        entfernt wurde — der Aufrufer setzt daraufhin `injection_suspected = true`
        (PLAN WP5, SECURITY §5 Punkt 4).
    """
    suspicious = False
    cleaned = text

    for pattern in _STRUCTURE_PATTERNS:
        cleaned, count = pattern.subn(REDACTION_MARKER, cleaned)
        if count:
            suspicious = True

    cleaned, hit = _redact_tokens(cleaned)
    suspicious = suspicious or hit

    cleaned, hit = _strip_control_chars(cleaned)
    suspicious = suspicious or hit

    # Whitespace vereinheitlichen: Zeilen einzeln entschlacken, Leerzeilenketten kürzen.
    lines = [" ".join(line.split()) for line in cleaned.split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip(), suspicious


def _one_line(text: str) -> str:
    """Presst einen Text auf eine Zeile (für die Headline)."""
    return " ".join(text.split())


def describe_without_body(mail: SanitizedMail) -> str:
    """Deterministischer Ersatztext, wenn keine Zusammenfassung übrig bleibt.

    Deckt insbesondere den Fall „Mail ohne darstellbaren Text, nur geblockte Anhänge" ab
    (WP3-Bericht (g)5): Der Nutzer soll trotzdem erfahren, dass und was angehängt war.
    Die verwendeten Werte stammen ausschließlich aus dem Sanitizer, nicht aus dem LLM.
    """
    blocked = [item for item in mail.attachments if not item.processed]
    if not blocked:
        return "Mail ohne darstellbaren Inhalt."
    listed = ", ".join(
        f"{item.filename_sanitized} ({prompts.format_size(item.size_bytes)})"
        for item in blocked[:_MAX_LISTED_BLOCKED]
    )
    rest = len(blocked) - len(blocked[:_MAX_LISTED_BLOCKED])
    suffix = f" und {rest} weitere" if rest > 0 else ""
    return (
        f"Mail ohne darstellbaren Inhalt, {len(blocked)} geblockte Anhänge: "
        f"{listed}{suffix}."
    )


def _fallback_headline(mail: SanitizedMail) -> str:
    """Headline-Ersatz aus dem (bereits sanitisierten) Betreff."""
    subject = _one_line(mail.subject)
    if not subject:
        return "Mail ohne Betreff"
    if len(subject) > HEADLINE_MAX_CHARS:
        return subject[: HEADLINE_MAX_CHARS - 1] + "…"
    return subject


def enforce_output_policy(summary: Summary, mail: SanitizedMail) -> Summary:
    """Deterministische Nachkontrolle der LLM-Ausgabe (I4, SECURITY §5 Punkt 4).

    Säubert jedes Textfeld, verwirft erfundene Anhang-Schlüssel, erzwingt die
    Headline-Länge und füllt leer gewordene Felder mit Werten aus dem Sanitizer. Jeder
    Fund setzt `injection_suspected = true`; ein bereits vom Modell gesetztes Flag bleibt
    gesetzt.

    Args:
        summary: Die schema-validierte, aber inhaltlich ungeprüfte Modellausgabe. Wird
            in-place verändert (`Summary` ist dafür bewusst nicht frozen, ADR-014).
        mail: Die zugehörige sanitisierte Mail — Quelle aller Ersatzwerte.

    Returns:
        Dasselbe, nun geprüfte `Summary`-Objekt.
    """
    suspicious = summary.injection_suspected

    headline, hit = scrub_text(summary.headline)
    suspicious = suspicious or hit
    headline = _one_line(headline)
    if not headline:
        headline = _fallback_headline(mail)
    if len(headline) > HEADLINE_MAX_CHARS:
        headline = headline[: HEADLINE_MAX_CHARS - 1].rstrip() + "…"
    summary.headline = headline

    summary_text, hit = scrub_text(summary.summary_text)
    suspicious = suspicious or hit
    if not summary_text:
        summary_text = describe_without_body(mail)
    summary.summary_text = summary_text

    reason, hit = scrub_text(summary.importance_reason)
    suspicious = suspicious or hit
    summary.importance_reason = _one_line(reason)

    category, hit = scrub_text(summary.category)
    suspicious = suspicious or hit
    category = _one_line(category).lower()
    summary.category = category if category else "sonstiges"

    # Nur Anhänge, deren Text das Modell tatsächlich gesehen hat, dürfen einen Eintrag
    # haben. Erfundene oder geblockte Dateinamen fliegen raus (kein Halluzinationskanal
    # in Richtung WP7-Nachrichtenformat).
    known = set(mail.attachment_texts)
    attachment_summaries: dict[str, str] = {}
    for filename, text in summary.attachment_summaries.items():
        if filename not in known:
            continue
        cleaned, hit = scrub_text(text)
        suspicious = suspicious or hit
        if cleaned:
            attachment_summaries[filename] = cleaned
    summary.attachment_summaries = attachment_summaries

    summary.injection_suspected = suspicious
    return summary


class SummarizerAgent:
    """Implementiert `pipeline.Summarizer` gegen einen beliebigen :class:`LLMProvider`.

    Der Provider wird injiziert; der Agent kennt weder HTTP noch Provider-Details (I2).
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        language: str = "de",
        summary_length: str = "medium",
        instructions: str = "",
        max_tokens: int = 1024,
        token_source: prompts.TokenSource = prompts.default_token_source,
    ) -> None:
        """
        Args:
            provider: Text-in/Text-out-Provider (WP4).
            language: `[general] language` — Sprache der Zusammenfassung.
            summary_length: `[general] summary_length` (`short`/`medium`/`long`).
            instructions: `[summarizer] instructions` — semi-trusted Custom-Instructions (I8).
            max_tokens: Antwort-Budget des Providers.
            token_source: Zufallsquelle der Datenblock-Kennung. Default ist ein CSPRNG;
                Tests injizieren eine deterministische Quelle.
        """
        self._provider = provider
        self._language = language
        self._summary_length = summary_length
        self._instructions = instructions
        self._max_tokens = max_tokens
        self._token_source = token_source

    @classmethod
    def from_config(
        cls, config: Config, *, provider: LLMProvider | None = None
    ) -> SummarizerAgent:
        """Baut den Agenten aus der Config.

        Ohne `provider` wird er über `llm.factory.build_provider` mit der Rolle
        `summarizer` gebaut (also aus `[llm]`, ohne die `[llm.critic]`-Overrides).
        """
        return cls(
            provider if provider is not None else build_provider(config, "summarizer"),
            language=config.general.language,
            summary_length=config.general.summary_length,
            instructions=config.summarizer.instructions,
            max_tokens=max_tokens_for(config, "summarizer"),
        )

    def summarize(self, mail: SanitizedMail) -> Summary:
        """Fasst eine sanitisierte Mail zusammen.

        Args:
            mail: Ausgabe des Sanitizers — der einzige Mail-Input, den ein LLM sieht (I1).

        Returns:
            Eine schema-valide, deterministisch nachkontrollierte :class:`Summary`.

        Raises:
            LLMInvalidResponse: Auch der Reparaturversuch lieferte kein schema-valides JSON.
            LLMTimeout, LLMRateLimited, LLMTransportError: durchgereicht vom Provider.
                Alle Fehler enden in der Pipeline fail-closed als Metadaten-Notiz (I6).
        """
        token = self._token_source()
        system = prompts.summarizer_system_prompt(
            token=token,
            language=self._language,
            summary_length=self._summary_length,
            custom_instructions=self._instructions,
        )
        user = prompts.summarizer_user_prompt(mail, token=token)
        summary = complete_json(
            self._provider, system, user, Summary, max_tokens=self._max_tokens
        )
        return enforce_output_policy(summary, mail)
