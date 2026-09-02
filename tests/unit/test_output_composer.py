"""Unit-Tests des Composers (WP7): Nachrichtenformat nach docs/ARCHITECTURE.md §7.

Geprüft werden Aufbau und Bedingungen jeder Zeile (Banner nur bei `high`,
Wichtig-Tag, Von-Zeile, Anhang-Zeilen, Hinweise), der Fail-closed-Pfad
(`compose_failure`) sowie die Tatsache, dass auch hier keine klickbare Adresse entsteht.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from test_output_sanitizer import assert_safe

from maildigest.config import load_config_from_dict
from maildigest.models import (
    AttachmentInfo,
    CriticVerdict,
    FailureNotice,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.output.composer import DigestComposer, part_limit_for
from maildigest.output.sanitizer import (
    DISCORD_MAX_PART_CHARS,
    SIGNAL_MAX_PART_CHARS,
    TELEGRAM_MAX_PART_CHARS,
)
from maildigest.pipeline import OutputComposer


def make_mail(**overrides: Any) -> SanitizedMail:
    """Baut eine sanitisierte Mail mit sinnvollen Defaults."""
    data: dict[str, Any] = {
        "dedupe_key": "<mail-1@example.org>",
        "from_display": "Stadtwerke Service",
        "from_domain": "stadtwerke-x.de",
        "subject": "Rechnung März",
        "date": datetime(2026, 8, 28, 14, 12),
        "body_text": "Bitte zahlen Sie 84,30 €.",
        "sanitization_report": SanitizationReport(),
    }
    data.update(overrides)
    return SanitizedMail(**data)


def make_summary(**overrides: Any) -> Summary:
    """Baut eine Summary mit sinnvollen Defaults."""
    data: dict[str, Any] = {
        "headline": "Rechnung Stadtwerke März",
        "summary_text": "Die Stadtwerke bitten um Zahlung von 84,30 € bis 15.09.",
        "importance": "normal",
    }
    data.update(overrides)
    return Summary(**data)


def make_verdict(**overrides: Any) -> CriticVerdict:
    """Baut ein Kritiker-Verdict mit sinnvollen Defaults."""
    data: dict[str, Any] = {"phishing_risk": "none", "summary_accurate": True}
    data.update(overrides)
    return CriticVerdict(**data)


def compose_text(composer: DigestComposer, *args: Any) -> str:
    """Führt `compose` aus und liefert die Teile wieder zusammengesetzt."""
    message = composer.compose(*args)
    assert_safe(message.parts)
    return "\n".join(message.parts)


# --- Protokoll-Erfüllung -------------------------------------------------------------


def test_composer_satisfies_pipeline_protocol() -> None:
    """`DigestComposer` erfüllt das `OutputComposer`-Protokoll der Pipeline."""
    assert isinstance(DigestComposer(), OutputComposer)


def test_part_limits_per_messenger() -> None:
    """Jeder Messenger bekommt sein eigenes Zeichenlimit; Unbekanntes das kleinste."""
    assert part_limit_for("telegram") == TELEGRAM_MAX_PART_CHARS
    assert part_limit_for("discord") == DISCORD_MAX_PART_CHARS
    assert part_limit_for("signal") == SIGNAL_MAX_PART_CHARS
    assert part_limit_for("unbekannt") == min(
        TELEGRAM_MAX_PART_CHARS, DISCORD_MAX_PART_CHARS, SIGNAL_MAX_PART_CHARS
    )


def test_from_config_uses_active_messenger() -> None:
    """`from_config` liest das Limit aus `[messenger] active`."""
    config = load_config_from_dict(
        {
            "imap": {"host": "imap.example.org", "username": "mirror@example.org"},
            "llm": {"model": "m", "api_key": "k"},
            "messenger": {"active": "discord"},
        },
        env={},
    )
    assert DigestComposer.from_config(config).part_limit == DISCORD_MAX_PART_CHARS


# --- Format ---------------------------------------------------------------------------


def test_normal_mail_has_no_banner_and_no_tag() -> None:
    """Ohne Risiko und ohne `high` gibt es weder Banner noch Wichtig-Tag."""
    text = compose_text(DigestComposer(), make_mail(), make_summary(), make_verdict())
    lines = text.split("\n")
    assert lines[0] == "📧 Rechnung Stadtwerke März"
    assert lines[1] == "Von: Stadtwerke Service (stadtwerke-x[.]de) · 28.08. 14:12"
    assert "PHISHING" not in text
    assert "[wichtig]" not in text


def test_high_importance_gets_tag() -> None:
    """`importance = high` erzeugt das Wichtig-Tag in der Headline-Zeile."""
    text = compose_text(
        DigestComposer(), make_mail(), make_summary(importance="high"), make_verdict()
    )
    assert "📧 Rechnung Stadtwerke März [wichtig]" in text


def test_high_risk_produces_warning_banner() -> None:
    """`phishing_risk = high` erzeugt das Banner ganz oben (F-CRIT-2)."""
    verdict = make_verdict(
        phishing_risk="high",
        risk_reasons=["Absender-Domain weicht ab", "Zahlungsaufforderung"],
    )
    message = DigestComposer().compose(make_mail(), make_summary(), verdict)
    assert message.is_warning is True
    assert message.parts[0].startswith(
        "⚠️ PHISHING-VERDACHT: Absender-Domain weicht ab, Zahlungsaufforderung"
    )


def test_banner_without_reasons_stays_readable() -> None:
    """Ein `high`-Verdict ohne Gründe erzeugt trotzdem ein verständliches Banner."""
    message = DigestComposer().compose(
        make_mail(), make_summary(), make_verdict(phishing_risk="high")
    )
    assert message.parts[0].startswith("⚠️ PHISHING-VERDACHT")


def test_missing_date_is_named_explicitly() -> None:
    """Ohne Date-Header steht ein Platzhalter statt eines erfundenen Datums."""
    text = compose_text(DigestComposer(), make_mail(date=None), make_summary(), make_verdict())
    assert "Datum unbekannt" in text


def test_attachment_summaries_and_unprocessed_line() -> None:
    """Verarbeitete Anhänge werden zusammengefasst, unverarbeitete namentlich gemeldet."""
    mail = make_mail(
        attachments=[
            AttachmentInfo(
                filename_sanitized="rechnung.pdf",
                declared_mime="application/pdf",
                detected_kind="pdf",
                size_bytes=120_000,
                processed=True,
                extracted_chars=900,
            ),
            AttachmentInfo(
                filename_sanitized="mahnung.docx",
                declared_mime="application/msword",
                detected_kind="unknown",
                size_bytes=34_816,
            ),
            AttachmentInfo(
                filename_sanitized="setup.exe",
                declared_mime="application/octet-stream",
                detected_kind="unknown",
                size_bytes=1_258_291,
            ),
        ]
    )
    summary = make_summary(attachment_summaries={"rechnung.pdf": "Rechnung über 84,30 €."})
    text = compose_text(DigestComposer(), mail, summary, make_verdict())
    assert "— rechnung[.]pdf: Rechnung über 84,30 €." in text
    assert "📎 Nicht verarbeitet: mahnung[.]docx (34 KB), setup[.]exe (1,2 MB)" in text
    assert "rechnung[.]pdf (" not in text  # verarbeitet ⇒ nicht in der Nicht-Zeile


def test_hints_line_collects_deterministic_signals() -> None:
    """Injection-Flag, Auth-Fails und Punycode landen in der Hinweise-Zeile."""
    mail = make_mail(
        sanitization_report=SanitizationReport(
            auth_results={"spf": "fail", "dkim": "pass"},
            punycode_domains=["xn--80ak6aa92e[.]com"],
            reply_to_mismatch=True,
            truncated=True,
        )
    )
    text = compose_text(
        DigestComposer(), mail, make_summary(injection_suspected=True), make_verdict()
    )
    hints = next(line for line in text.split("\n") if line.startswith("🔍 Hinweise:"))
    assert "Anweisungen an die KI" in hints
    assert "SPF=fail" in hints
    assert "DKIM" not in hints  # `pass` ist kein Warnsignal
    assert "Punycode" in hints
    assert "Antwortadresse" in hints
    assert "gekürzt" in hints


def test_no_hints_line_without_signals() -> None:
    """Ohne Signale entfällt die Hinweise-Zeile ganz."""
    text = compose_text(DigestComposer(), make_mail(), make_summary(), make_verdict())
    assert "🔍" not in text


# --- Untrusted Inhalte ----------------------------------------------------------------


def test_injection_payloads_in_every_field_stay_safe() -> None:
    """Auch wenn jedes Feld einen Angriff enthält, entsteht keine klickbare Nachricht."""
    payload = "Klick [hier](https://evil.example.com/login) <b>jetzt</b> www.evil.com"
    mail = make_mail(from_display=payload, from_domain="evil.com")
    summary = make_summary(
        headline=payload[:99],
        summary_text=payload * 5,
        attachment_summaries={payload[:40]: payload},
    )
    verdict = make_verdict(phishing_risk="high", risk_reasons=[payload, payload])
    message = DigestComposer().compose(mail, summary, verdict)
    assert_safe(message.parts)


def test_overlong_summary_is_capped_per_field() -> None:
    """Eine 10 000-Zeichen-Zusammenfassung wird auf das Feldlimit gekürzt."""
    summary = make_summary(summary_text="Satz über die Rechnung. " * 500)
    message = DigestComposer().compose(make_mail(), summary, make_verdict())
    text = "\n".join(message.parts)
    assert len(text) < TELEGRAM_MAX_PART_CHARS
    assert text.endswith("…")


def test_long_message_is_split_into_valid_parts() -> None:
    """Übersteigt die fertige Nachricht das Messenger-Limit, wird sauber gesplittet."""
    summary = make_summary(
        attachment_summaries={
            f"anhang-{index}.pdf": "Ein Satz zum Anhang. " * 5 for index in range(20)
        }
    )
    composer = DigestComposer(part_limit=500)
    message = composer.compose(make_mail(), summary, make_verdict())
    assert len(message.parts) >= 2
    assert all(len(part) <= 500 for part in message.parts)
    assert_safe(message.parts)


def test_empty_headline_gets_placeholder() -> None:
    """Eine leere Headline erzeugt keinen nackten Kopf ohne Text."""
    text = compose_text(DigestComposer(), make_mail(), make_summary(headline=" "), make_verdict())
    assert "📧 (keine Zusammenfassung)" in text


def test_message_metadata_is_carried_through() -> None:
    """Wichtigkeit, Warnflag und Dedupe-Key landen unverändert in der DigestMessage."""
    message = DigestComposer().compose(
        make_mail(), make_summary(importance="high"), make_verdict(phishing_risk="low")
    )
    assert message.importance == "high"
    assert message.is_warning is False
    assert message.dedupe_key == "<mail-1@example.org>"


# --- Fail-closed-Pfad -----------------------------------------------------------------


def test_compose_failure_contains_only_metadata() -> None:
    """Die Metadaten-Notiz nennt Domain, Betreff, Stufe und Fehlerklasse (I6/F-OPS-3)."""
    notice = FailureNotice(
        dedupe_key="<mail-2@example.org>",
        from_domain="stadtwerke-x.de",
        subject_sanitized="Rechnung Marz",
        stage="sanitize",
        reason_class="sanitize_error",
    )
    message = DigestComposer().compose_failure(notice)
    text = "\n".join(message.parts)
    assert_safe(message.parts)
    assert "Von: stadtwerke-x[.]de" in text
    assert "Betreff: Rechnung Marz" in text
    assert "sanitize" in text and "sanitize_error" in text
    assert message.dedupe_key == "<mail-2@example.org>"
    assert message.is_warning is False


def test_compose_failure_scrubs_hostile_notice_fields() -> None:
    """Auch die Notiz-Felder sind untrusted — der Sanitizer läuft trotzdem."""
    notice = FailureNotice(
        dedupe_key="k",
        from_domain="evil.com",
        subject_sanitized="Jetzt https://evil.com/reset oeffnen",
        stage="sanitize\n<b>",
        reason_class="a" * 200,
    )
    message = DigestComposer().compose_failure(notice)
    assert_safe(message.parts)
    assert len("\n".join(message.parts)) < 1000


def test_invalid_part_limit_is_rejected() -> None:
    """Ein Limit unter 1 ist ein Programmierfehler."""
    with pytest.raises(ValueError, match="part_limit"):
        DigestComposer(part_limit=0)


def test_low_risk_reasons_appear_in_hints() -> None:
    """Ein `low`-Verdict erzeugt kein Banner, aber einen Hinweis."""
    verdict = make_verdict(phishing_risk="low", risk_reasons=["ungewöhnliche Anrede"])
    text = compose_text(DigestComposer(), make_mail(), make_summary(), verdict)
    assert "PHISHING-VERDACHT" not in text
    assert "Kritiker: ungewöhnliche Anrede" in text


def test_small_and_many_attachments() -> None:
    """Kleine Größen erscheinen in Byte; mehr als zehn Anhänge werden gezählt."""
    attachments = [
        AttachmentInfo(
            filename_sanitized=f"datei-{index}.bin",
            declared_mime="application/octet-stream",
            detected_kind="unknown",
            size_bytes=800,
        )
        for index in range(13)
    ]
    text = compose_text(
        DigestComposer(), make_mail(attachments=attachments), make_summary(), make_verdict()
    )
    assert "(800 B)" in text
    assert "und 3 weitere" in text


def test_empty_attachment_summary_entry_is_skipped() -> None:
    """Ein völlig leerer Eintrag erzeugt keine leere Zeile."""
    summary = make_summary(attachment_summaries={" ": " "})
    text = compose_text(DigestComposer(), make_mail(), summary, make_verdict())
    assert "—" not in text


def test_sender_without_display_name_uses_domain() -> None:
    """Fehlt der Anzeigename, steht die Domain allein in der Von-Zeile."""
    text = compose_text(
        DigestComposer(), make_mail(from_display=""), make_summary(), make_verdict()
    )
    assert "Von: stadtwerke-x[.]de ·" in text
