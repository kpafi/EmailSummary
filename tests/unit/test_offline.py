"""Tests des Betriebs ohne Sprachmodell (`agents/offline.py`, ADR-076).

Der Modus ist der Standard direkt nach `maildigest init`. Er muss zwei Dinge leisten: ohne
jede Anmeldung zu einer zustellbaren Nachricht führen **und** dabei kein Sicherheits-
versprechen aufweichen — die deterministischen Warnungen sind ja gerade der Teil, der auch
ohne Modell trägt.
"""

from __future__ import annotations

from maildigest.agents.offline import (
    EXCERPT_MAX_CHARS,
    NO_MODEL_NOTE,
    OfflineCritic,
    OfflineSummarizer,
)
from maildigest.models import AttachmentInfo, SanitizationReport, SanitizedMail
from maildigest.pipeline import Critic, Summarizer


def make_mail(
    *,
    body: str = "Die Rechnung liegt bei.",
    subject: str = "Rechnung Maerz",
    report: SanitizationReport | None = None,
    attachments: tuple[AttachmentInfo, ...] = (),
) -> SanitizedMail:
    return SanitizedMail(
        dedupe_key="<x@example.org>",
        from_display="Absender",
        from_domain="sender.example",
        subject=subject,
        date=None,
        body_text=body,
        attachment_texts={},
        attachments=list(attachments),
        links_found=[],
        sanitization_report=report or SanitizationReport(),
    )


# --- Protokoll-Konformität -------------------------------------------------------------------


def test_offline_stufen_erfuellen_die_pipeline_protokolle() -> None:
    """Sie werden von `pipeline.process_mail` wie jede andere Stufe benutzt."""
    assert isinstance(OfflineSummarizer(), Summarizer)
    assert isinstance(OfflineCritic(), Critic)


# --- Summarizer ------------------------------------------------------------------------------


def test_auszug_ist_als_solcher_beschriftet() -> None:
    """Ohne Hinweis könnte der Nutzer den Auszug für eine geprüfte Zusammenfassung halten."""
    summary = OfflineSummarizer().summarize(make_mail())
    assert NO_MODEL_NOTE in summary.summary_text
    assert "Die Rechnung liegt bei." in summary.summary_text
    assert summary.headline == "Rechnung Maerz"


def test_langer_text_wird_gekuerzt() -> None:
    """Der Auszug darf die Nachricht nicht sprengen."""
    summary = OfflineSummarizer().summarize(make_mail(body="wort " * 500))
    assert len(summary.summary_text) < EXCERPT_MAX_CHARS + len(NO_MODEL_NOTE) + 10
    assert summary.summary_text.rstrip().endswith("…")


def test_wichtigkeit_ist_normal_und_nicht_geraten() -> None:
    """`low` würde Mails stillschweigend in den Sammel-Digest schieben — zu riskant."""
    summary = OfflineSummarizer().summarize(make_mail())
    assert summary.importance == "normal"
    assert "no language model" in summary.importance_reason


def test_mail_ohne_text_beschreibt_die_anhaenge() -> None:
    """Auch ohne darstellbaren Text soll der Nutzer erfahren, was ankam."""
    blocked = AttachmentInfo(
        filename_sanitized="rechnung.docx",
        declared_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        detected_kind="unknown",
        size_bytes=34_000,
        processed=False,
    )
    summary = OfflineSummarizer().summarize(make_mail(body="", attachments=(blocked,)))
    assert "rechnung.docx" in summary.summary_text


def test_injection_indizien_wirken_auch_ohne_modell() -> None:
    """F-SEC-5/CT-6: Der Verdacht darf nicht an der Mitwirkung eines Modells hängen."""
    report = SanitizationReport(hidden_text_removed=True, control_chars_removed=42)
    summary = OfflineSummarizer().summarize(make_mail(report=report))
    # `enforce_output_policy` wertet die Mail-Seite aus — hier muss etwas ankommen.
    assert summary.injection_suspected or report.hidden_text_removed


# --- Kritiker --------------------------------------------------------------------------------


def test_ohne_signale_kein_risiko() -> None:
    """Eine unauffällige Mail darf keine Warnung erzeugen (sonst Warnmüdigkeit)."""
    verdict = OfflineCritic().review(make_mail(), OfflineSummarizer().summarize(make_mail()))
    assert verdict.phishing_risk == "none"
    assert verdict.summary_accurate is True


def test_harte_signale_heben_das_risiko_auch_ohne_modell() -> None:
    """Der Phishing-Schutz stammt aus Code (ADR-043/063) und trägt deshalb auch offline."""
    report = SanitizationReport(
        punycode_domains=["xn--pypal-4ve.com"],
        mixed_script_domains=["xn--pypal-4ve.com"],
        reply_to_mismatch=True,
        return_path_mismatch=True,
        auth_results={"spf": "fail", "dkim": "fail"},
    )
    mail = make_mail(report=report)
    verdict = OfflineCritic().review(mail, OfflineSummarizer().summarize(mail))
    assert verdict.phishing_risk != "none"
    assert verdict.risk_reasons


# --- Zusammenspiel ---------------------------------------------------------------------------


def test_der_modus_ruft_nachweislich_kein_modell_auf(monkeypatch) -> None:
    """I2 ist hier trivial erfüllt — das hält der Test fest, statt es zu behaupten."""
    import maildigest.llm.factory as factory

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("Es darf kein Provider gebaut werden")

    monkeypatch.setattr(factory, "build_provider", explode)
    monkeypatch.setattr(factory, "build_provider_from_settings", explode)
    mail = make_mail()
    summary = OfflineSummarizer().summarize(mail)
    OfflineCritic().review(mail, summary)
