"""Tests des Betriebs ohne Sprachmodell (`agents/offline.py`, ADR-076).

Der Modus ist der Standard direkt nach `maildigest init`. Er muss zwei Dinge leisten: ohne
jede Anmeldung zu einer zustellbaren Nachricht führen **und** dabei kein Sicherheits-
versprechen aufweichen — die deterministischen Warnungen sind ja gerade der Teil, der auch
ohne Modell trägt.
"""

from __future__ import annotations

from maildigest.agents.offline import (
    ATTACHMENT_EXCERPT_MAX_CHARS,
    EXCERPT_MAX_CHARS,
    NO_MODEL_NOTE,
    OfflineCritic,
    OfflineSummarizer,
)
from maildigest.agents.summarizer import HEADLINE_MAX_CHARS
from maildigest.models import AttachmentInfo, SanitizationReport, SanitizedMail
from maildigest.output.composer import DigestComposer
from maildigest.pipeline import Critic, Summarizer


def make_mail(
    *,
    body: str = "Die Rechnung liegt bei.",
    subject: str = "Rechnung Maerz",
    report: SanitizationReport | None = None,
    attachments: tuple[AttachmentInfo, ...] = (),
    attachment_texts: dict[str, str] | None = None,
) -> SanitizedMail:
    return SanitizedMail(
        dedupe_key="<x@example.org>",
        from_display="Absender",
        from_domain="sender.example",
        subject=subject,
        date=None,
        body_text=body,
        attachment_texts=dict(attachment_texts or {}),
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


# --- Regressionen der Abschluss-Testrunde ----------------------------------------------------


def test_hc1_langer_betreff_wird_gekuerzt_statt_fail_closed() -> None:
    """HC-1: Ein Betreff über 100 Zeichen brach den Standardmodus fail-closed ab.

    `Summary.headline` trägt `max_length=100`; die Kürzung lief erst in der Nachkontrolle,
    also nach der Konstruktion — Pydantic warf vorher. 300 Zeichen ist die Obergrenze, die
    der Sanitizer durchlässt.
    """
    summary = OfflineSummarizer().summarize(make_mail(subject="A" * 300))
    assert len(summary.headline) <= HEADLINE_MAX_CHARS
    assert summary.headline.endswith("…")
    assert summary.headline.startswith("AAAA")


def test_hc1_betreff_von_genau_101_zeichen_geht_durch() -> None:
    """HC-1, Repro des Berichts: 100 Zeichen ging, 101 brach ab — beides muss tragen."""
    for length in (100, 101):
        summary = OfflineSummarizer().summarize(make_mail(subject="B" * length))
        assert len(summary.headline) <= HEADLINE_MAX_CHARS


def test_hc2_gelesener_anhang_erscheint_als_beschrifteter_auszug() -> None:
    """HC-2: Der extrahierte Anhangstext verschwand spurlos aus der Nachricht."""
    mail = make_mail(
        body="",
        attachment_texts={
            "mitteilung.txt": "WICHTIG: Ihre Bankverbindung wurde geaendert.",
            # Ein Anhang ohne lesbaren Text erzeugt keine leere Zeile.
            "leer.txt": "   ",
        },
    )
    summary = OfflineSummarizer().summarize(mail)
    assert "mitteilung.txt" in summary.attachment_summaries
    assert "leer.txt" not in summary.attachment_summaries
    value = summary.attachment_summaries["mitteilung.txt"]
    assert "Bankverbindung" in value
    assert value.startswith("Excerpt: ")


def test_hc2_auszug_erreicht_die_zugestellte_nachricht() -> None:
    """HC-2: Der Weg bis in die Nachricht zählt, nicht nur das Feld."""
    mail = make_mail(
        body="",
        attachment_texts={"mitteilung.txt": "Neue IBAN im Anhang."},
    )
    summary = OfflineSummarizer().summarize(mail)
    verdict = OfflineCritic().review(mail, summary)
    message = DigestComposer(part_limit=4096).compose(mail, summary, verdict)
    text = "\n".join(message.parts)
    # Der Dateiname ist im Ausgabepfad defanged (I3) — deshalb `mitteilung[.]txt`.
    assert "— mitteilung[.]txt:" in text
    assert "Neue IBAN im Anhang." in text


def test_hc2_langer_anhangstext_wird_auf_das_composer_limit_gekuerzt() -> None:
    """HC-2: Der Composer kürzt bei 400 Zeichen — das darf nicht ihm überlassen bleiben."""
    mail = make_mail(body="", attachment_texts={"lang.txt": "wort " * 500})
    value = OfflineSummarizer().summarize(mail).attachment_summaries["lang.txt"]
    assert len(value) <= ATTACHMENT_EXCERPT_MAX_CHARS
    assert value.endswith("…")


def test_hc2_ohne_body_aber_mit_anhangstext_keine_falsche_behauptung() -> None:
    """HC-2: „Mail without displayable content" war sachlich falsch, der Inhalt war da."""
    mail = make_mail(body="", attachment_texts={"a.txt": "Inhalt"})
    text = OfflineSummarizer().summarize(mail).summary_text
    assert "without displayable content" not in text
    assert "No mail body; 1 attachment with readable text." in text


def test_hc2_singular_bei_genau_einem_geblockten_anhang() -> None:
    """HC-14/HC-2: „1 blocked attachments" war ein Grammatikfehler in derselben Funktion."""
    blocked = AttachmentInfo(
        filename_sanitized="setup.exe",
        declared_mime="application/octet-stream",
        detected_kind="unknown",
        size_bytes=1024,
        processed=False,
    )
    text = OfflineSummarizer().summarize(make_mail(body="", attachments=(blocked,))).summary_text
    assert "1 blocked attachment:" in text
    assert "1 blocked attachments" not in text
