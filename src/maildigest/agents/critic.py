"""Kritiker-Agent (Stufe 4): `SanitizedMail` + `Summary` → `CriticVerdict`.

Vertrag: `pipeline.Critic` (`review(mail, summary) -> CriticVerdict`). Härtung:
docs/SECURITY.md §3 (T8/T9) und §5 Punkt 5, Invarianten I1/I2/I4/I8. Umsetzung: WP6.

Ablauf eines Aufrufs:

1. **Deterministische Signale** aus dem `sanitization_report` sammeln
   (:func:`collect_signals`, F-CRIT-3): Reply-To ≠ From, From-Domain ≠ Return-Path-Domain,
   `Authentication-Results`, Punycode-/Mixed-Script-Domains, geblockte Anhänge, entfernte
   Links, versteckter Text, Kürzung. Reiner Code — durch den Mail-Inhalt nicht
   beeinflussbar (T9).
2. Zufällige Blockkennung ziehen; System- und User-Prompt aus `llm/prompts.py` bauen.
   Mail **und** Zusammenfassung stehen in je eigenen, delimitierten Untrusted-Blöcken
   (I8); die Signale stehen davor als vertrauenswürdige Programm-Fakten.
3. `llm.schema.complete_json` erzwingt das `CriticVerdict`-Schema; scheitert auch der eine
   Reparaturversuch, fliegt `LLMInvalidResponse` nach oben und die Pipeline macht daraus
   eine Metadaten-Notiz (I6, fail-closed).
4. **Deterministische Nachkontrolle** (:func:`enforce_verdict_policy`): Die LLM-Ausgabe ist
   untrusted (I4). Jedes Textfeld wird gesäubert (URLs, Markup, Steuerzeichen), Listen und
   Längen werden gekappt, und harte Code-Signale heben die Risikostufe an, falls das Modell
   sie ignoriert hat (ADR-043). Die Stufe wird nie gesenkt.

Bewusst **nicht** hier: die Wirkung des Verdicts. Warn-Banner, Mindest-Wichtigkeit bei
`high` und der Fail-closed-Pfad bei `summary_accurate = false` liegen in
`pipeline.process_mail` bzw. `output/composer.py`; eine zweite Auswertung wäre eine zweite
Wahrheit.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from maildigest.agents.summarizer import scrub_text
from maildigest.config import Config
from maildigest.llm import prompts
from maildigest.llm.base import LLMProvider
from maildigest.llm.factory import build_provider, max_tokens_for
from maildigest.llm.schema import complete_json
from maildigest.models import CriticVerdict, PhishingRisk, SanitizedMail, Summary

__all__ = [
    "MAX_NOTES_CHARS",
    "MAX_REASONS",
    "MAX_REASON_CHARS",
    "CriticAgent",
    "Signal",
    "collect_signals",
    "enforce_verdict_policy",
    "signal_lines",
]

#: Wie viele `risk_reasons` den Agenten verlassen (der Prompt nennt dieselbe Zahl).
MAX_REASONS = 5

#: Zeichenlimit je Grund (der Output-Sanitizer kürzt später noch einmal auf 200).
MAX_REASON_CHARS = 200

#: Zeichenlimit für `notes`.
MAX_NOTES_CHARS = 500

#: Wie viele defangte Domains eine Signalzeile höchstens aufzählt (Prompt-Budget).
_MAX_LISTED_DOMAINS = 3

#: Auth-Ergebnisse, die als „in Ordnung" gelten. Alles andere wird als Fakt gemeldet.
#: Identisch zur Liste in `output/composer.py` — dort für die Hinweiszeile, hier für den
#: Prompt.
_AUTH_OK = frozenset({"pass", "none", "neutral", "policy"})

#: Rangfolge der Risikostufen für den „nie senken"-Vergleich.
_RISK_RANK: dict[str, int] = {"none": 0, "low": 1, "high": 2}

#: Grund, den der Code selbst anhängt, wenn die Nachkontrolle etwas entfernen musste.
_SCRUBBED_REASON = "critic output contained disallowed content (removed)"

#: Signale, die auf eine **Absender-Fälschung** deuten — die einzigen, die zusammen die
#: Stufe `high` erzwingen dürfen (CT-11). Anhangs-, Link- und Kürzungssignale sagen über
#: die Echtheit des Absenders nichts aus und bleiben deshalb außen vor.
_SPOOFING_KEYS = frozenset(
    {
        "auth_failed",
        "reply_to_mismatch",
        "return_path_mismatch",
        "punycode",
        "mixed_script",
    }
)

#: Kurzbezeichnungen für den vom Code formulierten Kombinations-Grund.
_SIGNAL_LABELS: dict[str, str] = {
    "auth_failed": "sender authentication failed",
    "reply_to_mismatch": "differing reply address",
    "return_path_mismatch": "differing return path",
    "punycode": "punycode domain",
    "mixed_script": "mixed writing systems",
}

#: Ab so vielen **unabhängigen** Fälschungssignalen greift die Anhebung auf `high`.
_HIGH_SIGNAL_COUNT = 3


@dataclass(frozen=True)
class Signal:
    """Ein deterministisches Faktum über die Mail (F-CRIT-3).

    Attributes:
        key: Stabiler Bezeichner (Tests und spätere Auswertung hängen daran, nicht am Text).
        text: Der Wortlaut, wie er als Programm-Fakt in den Prompt geht.
        hard: True für Indizien, die ein Angreifer nicht versehentlich auslöst und die
            deshalb eine Mindest-Risikostufe erzwingen (ADR-043).
        label: Kurzfassung für den Nutzer (Warn-Banner). `text` ist für den Prompt
            formuliert — mit defangten Domains und Erläuterung — und wäre im Banner
            unlesbar. Leer ⇒ `text` wird verwendet.
    """

    key: str
    text: str
    hard: bool = False
    label: str = ""


def collect_signals(mail: SanitizedMail) -> tuple[Signal, ...]:
    """Berechnet die deterministischen Kritiker-Fakten aus dem Sanitizer-Protokoll.

    Reiner Code, kein LLM, keine Heuristik über den Mail-Text: Alle Werte stammen aus
    `SanitizationReport` bzw. `AttachmentInfo`, sind also vom Mail-Inhalt nicht
    manipulierbar (T9). Die Funktion ist die vollständige Umsetzung von F-CRIT-3.

    Args:
        mail: Die sanitisierte Mail.

    Returns:
        Die Signale in stabiler Reihenfolge. Immer mindestens eines (die Auth-Zeile),
        damit der Faktenblock nie leer ist.
    """
    report = mail.sanitization_report
    signals: list[Signal] = []

    if report.reply_to_mismatch:
        signals.append(
            Signal(
                "reply_to_mismatch",
                "reply address (Reply-To) differs from the sender address",
            )
        )
    if report.return_path_mismatch:
        signals.append(
            Signal(
                "return_path_mismatch",
                "return-path domain differs from the sender domain",
            )
        )

    if report.auth_results:
        rendered = ", ".join(
            f"{key.upper()}={value}" for key, value in sorted(report.auth_results.items())
        )
        failed = sorted(
            key.upper()
            for key, value in report.auth_results.items()
            if value.lower() not in _AUTH_OK
        )
        if failed:
            signals.append(
                Signal(
                    "auth_failed",
                    f"sender authentication: {rendered} — failed: "
                    f"{', '.join(failed)}. Note: forwarding into the mirror mailbox can "
                    "legitimately break SPF/DKIM; on its own this is not proof",
                )
            )
        else:
            signals.append(Signal("auth_ok", f"sender authentication: {rendered}"))
    else:
        signals.append(
            Signal("auth_missing", "sender authentication: no information in the headers")
        )

    if report.punycode_domains:
        listed = ", ".join(report.punycode_domains[:_MAX_LISTED_DOMAINS])
        signals.append(
            Signal(
                "punycode",
                f"punycode domains ({len(report.punycode_domains)}): {listed} — "
                "IDN spelling; can be legitimate or a spoofed name",
                # Hart im Sinne von ADR-043: Eine Weiterleitung ins Spiegelpostfach kann
                # SPF/DKIM brechen, aber sie schreibt keine Absender-Domain in Punycode um.
                # Das Signal ist deshalb — anders als `auth_failed` — nicht wegzuerklären
                # und hebt für sich allein auf `low` an (CT-11).
                hard=True,
                label=_SIGNAL_LABELS["punycode"],
            )
        )
    if report.mixed_script_domains:
        listed = ", ".join(report.mixed_script_domains[:_MAX_LISTED_DOMAINS])
        signals.append(
            Signal(
                "mixed_script",
                f"domains with mixed writing systems ({len(report.mixed_script_domains)}): "
                f"{listed} — typical homoglyph spoofing",
                hard=True,
                label=_SIGNAL_LABELS["mixed_script"],
            )
        )

    if report.blocked_attachments:
        kinds = sorted(
            {item.declared_mime for item in mail.attachments if not item.processed}
        )
        listed = ", ".join(kinds[:_MAX_LISTED_DOMAINS]) or "unknown"
        signals.append(
            Signal(
                "blocked_attachments",
                f"unprocessed attachments: {report.blocked_attachments} "
                f"(types declared in the mail: {listed})",
            )
        )
    if report.encrypted:
        # Weich im Sinne von ADR-043: Verschlüsselung ist kein Fälschungsindiz und darf die
        # Risikostufe nicht anheben. Der Kritiker braucht das Faktum trotzdem, sonst hält er
        # den leeren Text für eine verunglückte Zusammenfassung (F-CRIT-3, ADR-082).
        signals.append(
            Signal(
                "encrypted",
                "the mail is end-to-end encrypted (PGP/S-MIME) and was not decrypted — "
                "an empty body is expected here and is not a sign of manipulation",
            )
        )
    signals.append(
        Signal("links_removed", f"links removed/replaced: {report.links_removed}")
    )
    if report.hidden_text_removed:
        signals.append(
            Signal("hidden_text", "the HTML contained hidden text (removed)")
        )
    if report.html_divergent:
        # Weich im Sinne von ADR-043: Die Divergenz sagt über die Echtheit des Absenders
        # nichts, wohl aber über die Verlässlichkeit der Zusammenfassung — der Kritiker
        # soll sie kennen (F-CRIT-3, ADR-067).
        signals.append(
            Signal(
                "html_divergent",
                "the HTML version of the mail differs in content from the plain-text part "
                "that was analysed — the recipient sees the HTML part in their mail "
                "program, while the summary is based on the plain text",
                label="HTML part differs from the text part",
            )
        )
    if report.control_chars_removed:
        signals.append(
            Signal(
                "control_chars",
                f"control/invisible characters removed: {report.control_chars_removed}",
            )
        )
    if report.truncated:
        signals.append(Signal("truncated", "the mail text was truncated (length limit)"))
    return tuple(signals)


def signal_lines(signals: Sequence[Signal]) -> tuple[str, ...]:
    """Die Wortlaute der Signale — genau das, was in den Prompt geht."""
    return tuple(signal.text for signal in signals)


def _scrub_verdict_text(text: str) -> tuple[str, bool]:
    """Säubert genau ein Textfeld des Verdicts deterministisch (I4).

    Nutzt dieselbe Politik wie die Summarizer-Nachkontrolle
    (:func:`agents.summarizer.scrub_text`: Markdown, HTML, Entities, URL-Muster,
    Unicode-`C*`), normalisiert aber **vorher** nach NFKC. Damit fallen auch
    Fullwidth-Schreibweisen (U+FF48 U+FF54 … statt `https`) bereits hier auf (ADR-044); der
    Output-Sanitizer (WP7) prüft anschließend ohnehin noch einmal.

    Returns:
        `(gesäuberter Text, verdächtig)`.
    """
    normalized = unicodedata.normalize("NFKC", text)
    cleaned, suspicious = scrub_text(normalized)
    return cleaned, suspicious or normalized != text


def _one_line(text: str, limit: int) -> str:
    """Presst ein Feld auf eine Zeile und kürzt hart auf `limit` Zeichen."""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _raise_risk(current: PhishingRisk, floor: PhishingRisk) -> PhishingRisk:
    """Hebt die Risikostufe auf mindestens `floor` an — senken ist nie möglich."""
    return current if _RISK_RANK[current] >= _RISK_RANK[floor] else floor


def enforce_verdict_policy(
    verdict: CriticVerdict, signals: Sequence[Signal] = ()
) -> CriticVerdict:
    """Deterministische Nachkontrolle des Verdicts (I4, SECURITY §5 Punkt 4).

    Schritte:

    1. Jeden `risk_reasons`-Eintrag und `notes` säubern, auf eine Zeile pressen, kürzen;
       leere Einträge und Duplikate entfallen, die Liste wird auf :data:`MAX_REASONS`
       gekappt.
    2. Musste etwas entfernt werden, wird das als eigener Grund sichtbar gemacht und die
       Risikostufe auf mindestens `low` angehoben — das Gegenstück zum
       `injection_suspected`-Flag der `Summary`, für das `CriticVerdict` kein Feld hat
       (ADR-043).
    3. Harte Code-Signale (:attr:`Signal.hard`) heben die Stufe ebenfalls auf mindestens
       `low` an und werden als Grund ergänzt, falls das Modell sie übergangen hat (T9).
    4. Treffen mindestens :data:`_HIGH_SIGNAL_COUNT` unabhängige Fälschungssignale
       (:data:`_SPOOFING_KEYS`) zusammen und ist darunter mindestens ein hartes, geht die
       Stufe auf `high` — dann trägt die Nachricht das Warn-Banner (F-CRIT-2/F-CRIT-3).
       Ein einzelnes weiterleitungs-erklärbares Signal tut das weiterhin nicht (ADR-043).
    5. `phishing_risk != "none"` ohne jeden Grund bekommt einen neutralen Platzhalter,
       damit das Warn-Banner nie leer bleibt.

    `summary_accurate` bleibt unangetastet: Der Code kann inhaltliche Richtigkeit nicht
    beurteilen, und ein Herabsetzen würde den Fail-closed-Pfad (T8) aushebeln.

    Args:
        verdict: Die schema-validierte, inhaltlich ungeprüfte Modellausgabe. Wird
            in-place verändert (`CriticVerdict` ist dafür bewusst nicht frozen).
        signals: Die Signale aus :func:`collect_signals` desselben Aufrufs.

    Returns:
        Dasselbe, nun geprüfte `CriticVerdict`-Objekt.
    """
    suspicious = False
    model_reasons: list[str] = []
    for raw_reason in verdict.risk_reasons:
        cleaned, hit = _scrub_verdict_text(raw_reason)
        suspicious = suspicious or hit
        reason = _one_line(cleaned, MAX_REASON_CHARS)
        if reason and reason not in model_reasons:
            model_reasons.append(reason)

    notes, hit = _scrub_verdict_text(verdict.notes)
    suspicious = suspicious or hit
    verdict.notes = _one_line(notes, MAX_NOTES_CHARS)

    # Code-Gründe zuerst: Sie dürfen nicht aus der Liste fallen, wenn das Modell sie mit
    # fünf eigenen Einträgen verdrängt — sie sind die einzigen, die garantiert stimmen.
    risk = verdict.phishing_risk
    code_reasons: list[str] = []
    if suspicious:
        code_reasons.append(_SCRUBBED_REASON)
        risk = _raise_risk(risk, "low")
    for signal in signals:
        if not signal.hard:
            continue
        risk = _raise_risk(risk, "low")
        # Für den Nutzer zählt das Kurzlabel: `signal.text` ist für den Prompt formuliert
        # (mit defangten Domains und Erläuterung) und wäre im Warn-Banner unlesbar. Die
        # Domains selbst stehen ohnehin in der Hinweiszeile der Nachricht.
        hint = _one_line(signal.label or signal.text, MAX_REASON_CHARS)
        if hint and hint not in code_reasons:
            code_reasons.append(hint)

    # Kombination unabhängiger Fälschungssignale (CT-11). Ein einzelnes Signal bleibt
    # bewusst harmlos — eine Weiterleitung ins Spiegelpostfach bricht SPF/DKIM und
    # verändert den Return-Path, das ist der Normalfall dieses Produkts (ADR-043). Treffen
    # aber mehrere unabhängige Signale zusammen **und** ist mindestens eines davon durch
    # Weiterleitung nicht erklärbar (`hard`), ist das kein Nebeneffekt mehr.
    spoofing = [signal for signal in signals if signal.key in _SPOOFING_KEYS]
    if len(spoofing) >= _HIGH_SIGNAL_COUNT and any(signal.hard for signal in spoofing):
        risk = _raise_risk(risk, "high")
        listed = ", ".join(
            _SIGNAL_LABELS.get(signal.key, signal.key) for signal in spoofing
        )
        code_reasons.insert(
            0, _one_line(f"several independent spoofing signals: {listed}", MAX_REASON_CHARS)
        )

    reasons = code_reasons + [reason for reason in model_reasons if reason not in code_reasons]
    if risk != "none" and not reasons:
        reasons.append("the critic reports a risk without giving a reason")

    verdict.phishing_risk = risk
    verdict.risk_reasons = reasons[:MAX_REASONS]
    return verdict


class CriticAgent:
    """Implementiert `pipeline.Critic` gegen einen beliebigen :class:`LLMProvider`.

    Der Provider wird injiziert; der Agent kennt weder HTTP noch Provider-Details (I2).
    Er nimmt **keine** Custom-Instructions entgegen — der Kritiker ist die unabhängige
    zweite Instanz (ADR-042).
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        language: str = "de",
        max_tokens: int | None = None,
        token_source: prompts.TokenSource = prompts.default_token_source,
    ) -> None:
        """
        Args:
            provider: Text-in/Text-out-Provider (WP4).
            language: `[general] language` — Sprache der Verdict-Textfelder.
            max_tokens: Antwort-Budget des Providers; `None` = kein Limit (ADR-085).
            token_source: Zufallsquelle der Datenblock-Kennung. Default ist ein CSPRNG;
                Tests injizieren eine deterministische Quelle.
        """
        self._provider = provider
        self._language = language
        self._max_tokens = max_tokens
        self._token_source = token_source

    @classmethod
    def from_config(cls, config: Config, *, provider: LLMProvider | None = None) -> CriticAgent:
        """Baut den Agenten aus der Config.

        Ohne `provider` wird er über `llm.factory.build_provider` mit der Rolle `critic`
        gebaut — also inklusive der Overrides aus `[llm.critic]`, damit Summarizer und
        Kritiker unterschiedliche Modelle benutzen können (ADR-025).
        """
        return cls(
            provider if provider is not None else build_provider(config, "critic"),
            language=config.general.language,
            max_tokens=max_tokens_for(config, "critic"),
        )

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        """Prüft Mail und Zusammenfassung.

        Args:
            mail: Ausgabe des Sanitizers — der einzige Mail-Input, den ein LLM sieht (I1).
            summary: Die zu prüfende Ausgabe des Summarizers (untrusted, I4).

        Returns:
            Ein schema-valides, deterministisch nachkontrolliertes :class:`CriticVerdict`.

        Raises:
            LLMInvalidResponse: Auch der Reparaturversuch lieferte kein schema-valides JSON.
            LLMTimeout, LLMRateLimited, LLMTransportError: durchgereicht vom Provider.
                Alle Fehler enden in der Pipeline fail-closed als Metadaten-Notiz (I6).
        """
        signals = collect_signals(mail)
        token = self._token_source()
        system = prompts.critic_system_prompt(
            token=token, language=self._language, max_reasons=MAX_REASONS
        )
        user = prompts.critic_user_prompt(
            mail, summary, signal_lines(signals), token=token
        )
        verdict = complete_json(
            self._provider, system, user, CriticVerdict, max_tokens=self._max_tokens
        )
        return enforce_verdict_policy(verdict, signals)
