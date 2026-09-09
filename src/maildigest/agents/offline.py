"""Betrieb ohne Sprachmodell — der Standardmodus direkt nach `maildigest init`.

Warum es diesen Modus gibt
--------------------------
Ein Zugang zu einem Sprachmodell kostet Geld oder mindestens eine Anmeldung. Ein
Werkzeug, das ohne diesen Schritt gar nichts tut, ist für den ersten Versuch wertlos —
und ein mitgelieferter Schlüssel scheidet aus: MailDigest ist quelloffen, ein
eingebetteter Zugang wäre binnen Tagen abgegriffen und gesperrt.

Der Ausweg nutzt eine Eigenschaft der Architektur aus, die ohnehin gilt (PLAN §1
Leitprinzip 4): **Alles Sicherheitsrelevante entscheidet Code, nicht das Modell.** Der
Sanitizer entfernt Links und Anhänge, `critic.collect_signals` berechnet die
Fälschungssignale, `summarizer.detect_injection_evidence` erkennt Injection-Indizien —
alles ohne jedes Modell. Was ein Sprachmodell beisteuert, ist der *zusammenfassende Text*
und die Einschätzung der Wichtigkeit; alles andere bleibt auch ohne es bestehen.

Dieser Modus liefert deshalb keine Zusammenfassung, sondern einen ehrlich beschrifteten
Auszug: Betreff, Absender, ein Anfang des bereits sanitisierten Textes, die geblockten
Anhänge und sämtliche deterministischen Warnsignale. Für „ist da etwas Wichtiges
gekommen?" reicht das oft; für echte Zusammenfassungen verbindet man später ein Modell
(`maildigest connect-llm`).

Sicherheitslage
---------------
Der Modus ist **strenger** als der Modellbetrieb, nicht lockerer: Es gibt keine
untrusted Modellausgabe, die geprüft werden müsste. Der ausgegebene Text stammt
ausschließlich aus :class:`~maildigest.models.SanitizedMail` und läuft anschließend
durch dieselbe deterministische Nachkontrolle wie eine Modellausgabe
(:func:`~maildigest.agents.summarizer.enforce_output_policy`,
:func:`~maildigest.agents.critic.enforce_verdict_policy`) und durch den
Output-Sanitizer. I1 bis I8 bleiben unberührt; I2 ist trivial erfüllt, weil kein Modell
aufgerufen wird.
"""

from __future__ import annotations

from maildigest.agents.critic import collect_signals, enforce_verdict_policy
from maildigest.agents.summarizer import (
    describe_without_body,
    enforce_output_policy,
)
from maildigest.models import CriticVerdict, SanitizedMail, Summary

__all__ = ["EXCERPT_MAX_CHARS", "NO_MODEL_NOTE", "OfflineCritic", "OfflineSummarizer"]

#: Zeichen des Mail-Textes, die als Auszug übernommen werden. Bewusst kurz: Der Auszug
#: soll die Nachricht nicht sprengen, sondern die Frage „lohnt sich Hinschauen?" beantworten.
EXCERPT_MAX_CHARS = 400

#: Steht in jeder Zusammenfassung dieses Modus. Ohne diesen Hinweis müsste der Nutzer
#: raten, warum der Text abgehackt wirkt — und könnte einen Auszug für eine geprüfte
#: Zusammenfassung halten.
NO_MODEL_NOTE = "Excerpt, not a summary — no language model configured."

#: Kategorie ohne Modell: Eine Einordnung ist ohne Sprachverständnis nicht seriös möglich.
_CATEGORY = "unclassified"


def _excerpt(mail: SanitizedMail) -> str:
    """Der Anfang des bereits sanitisierten Mail-Textes, auf eine Länge gebracht.

    Der Text ist an dieser Stelle schon frei von Links, HTML und Steuerzeichen (WP3) —
    hier wird nur noch gekürzt.
    """
    text = " ".join(mail.body_text.split())
    if not text:
        return describe_without_body(mail)
    if len(text) > EXCERPT_MAX_CHARS:
        return text[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    return text


class OfflineSummarizer:
    """Erfüllt das `Summarizer`-Protokoll ohne jeden Modellaufruf.

    Die Wichtigkeit ist immer `normal`: Ohne Sprachverständnis lässt sich „wichtig" nicht
    beurteilen, und ein geratenes `low` würde Mails stillschweigend in den Sammel-Digest
    schieben. `normal` stellt einzeln zu — die vorsichtige Richtung.
    """

    def summarize(self, mail: SanitizedMail) -> Summary:
        """Baut eine `Summary` allein aus den Sanitizer-Daten."""
        summary = Summary(
            headline=mail.subject or "Mail without subject",
            summary_text=f"{NO_MODEL_NOTE} {_excerpt(mail)}",
            importance="normal",
            importance_reason="no language model configured — not assessed",
            category=_CATEGORY,
            attachment_summaries={},
            injection_suspected=False,
        )
        # Dieselbe Nachkontrolle wie bei einer Modellausgabe: Sie kürzt die Headline,
        # füllt leere Felder und setzt `injection_suspected` anhand der Mail-Seite (CT-6).
        return enforce_output_policy(summary, mail)


class OfflineCritic:
    """Erfüllt das `Critic`-Protokoll ohne jeden Modellaufruf.

    Das Urteil entsteht ausschließlich aus den deterministischen Signalen: Ausgangspunkt
    ist `none`, und :func:`enforce_verdict_policy` hebt die Stufe anhand der harten
    Signale an (ADR-043/ADR-063) — genau wie im Modellbetrieb, nur ohne die Modellgründe.
    `summary_accurate` ist immer wahr: Der Text stammt aus dem Sanitizer, es gibt nichts
    zu halluzinieren.
    """

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        """Bewertet die Mail allein anhand der Code-Signale."""
        del summary  # Es gibt keine Modellausgabe, die gegengeprüft werden müsste.
        verdict = CriticVerdict(
            phishing_risk="none",
            risk_reasons=[],
            summary_accurate=True,
            notes="",
        )
        return enforce_verdict_policy(verdict, collect_signals(mail))
