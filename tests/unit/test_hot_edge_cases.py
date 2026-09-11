"""Grenzfall-Tests aus Code-Kenntnis — Hot-Testing WP10 (PLAN.md WP10, docs/TESTING.md §2).

Die Liste aus PLAN.md WP10 wird hier abgearbeitet und erweitert. Getestet wird gezielt an
den Stellen, von denen der Whitebox-Blick weiß, dass sie fragil sind: die Ränder der Limits
aus docs/SECURITY.md §4 (genau darauf, eins darunter, eins darüber), degenerierte
MIME-Bäume, kaputte Kodierungen und die Zusammenbau-Stufe.

Durchgehende Erwartung: **keine Exception nach außen** (jeder Fehler ist fail-closed, I6)
und **keine URL/kein Tag/kein Steuerzeichen** im Ergebnis (I3/F-SEC-3).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from maildigest.config import LimitsConfig
from maildigest.models import (
    AttachmentInfo,
    CriticVerdict,
    RawMail,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.output.composer import DigestComposer, LowDigestItem
from maildigest.output.sanitizer import (
    CONTINUATION_PREFIX,
    final_guard,
    scrub_field,
    scrub_plain,
    split_parts,
)
from maildigest.sanitize.sanitizer import MailSanitizer, SanitizeError


def _strip_continuation(parts: list[str]) -> list[str]:
    """Zieht das Fortsetzungspräfix eines harten Zeilenschnitts ab (HC-6)."""
    return [
        part[len(CONTINUATION_PREFIX) :] if part.startswith(CONTINUATION_PREFIX) else part
        for part in parts
    ]

# --- Hilfen ---------------------------------------------------------------------------

#: Kleinstes PDF, das die Magic-Bytes-Prüfung besteht (Inhalt egal — die Extraktion läuft
#: im Subprozess und darf scheitern, der Anhang gilt dann als „nicht verarbeitet").
MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def raw_mail(mime: bytes, **overrides: Any) -> RawMail:
    """Baut eine `RawMail` um fertige MIME-Bytes herum."""
    data: dict[str, Any] = {
        "dedupe_key": "<hot@example>",
        "from_addr": "Absender <a@sender.example>",
        "from_domain": "sender.example",
        "mime_bytes": mime,
        "size_bytes": len(mime),
    }
    data.update(overrides)
    return RawMail(**data)


def multipart(*parts: bytes, boundary: bytes = b"grenze") -> bytes:
    """Setzt eine multipart/mixed-Mail aus fertigen Teilen zusammen."""
    body = b"".join(b"--" + boundary + b"\r\n" + part + b"\r\n" for part in parts)
    return (
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n\r\n'
        + body + b"--" + boundary + b"--\r\n"
    )


def attachment_part(
    ctype: bytes, filename: bytes, payload: bytes, *, disposition: bytes = b"attachment"
) -> bytes:
    """Ein Anhang-Teil mit rohem (nicht kodiertem) Payload."""
    return (
        b"Content-Type: " + ctype + b"\r\n"
        b"Content-Disposition: " + disposition + b'; filename="' + filename + b'"\r\n\r\n'
        + payload
    )


def nested_multipart(depth: int) -> bytes:
    """Baut `depth` ineinander geschachtelte multipart-Ebenen um einen Textteil (T10)."""
    inner = b"Content-Type: text/plain\r\n\r\nGANZ-TIEF\r\n"
    for level in range(depth):
        boundary = f"b{level}".encode()
        inner = (
            b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n\r\n'
            b"--" + boundary + b"\r\n" + inner + b"\r\n--" + boundary + b"--\r\n"
        )
    return inner


#: Was in keinem Sanitizer-Ergebnis stehen darf (WP3-Akzeptanzkriterium).
_FORBIDDEN_IN_BODY = re.compile(r"(?i)(?:https?|ftps?)\s*:\s*/\s*/|</?[a-z][^<>]{0,80}>")


def assert_body_clean(mail: SanitizedMail) -> None:
    """Kein lebendes Schema, kein Tag, kein Steuerzeichen im sanitisierten Text."""
    for text in [mail.body_text, mail.subject, mail.from_display, *mail.attachment_texts.values()]:
        assert not _FORBIDDEN_IN_BODY.search(text), f"unsicherer Rest in {text!r}"
        assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text)


# --- Degenerierte Mails ----------------------------------------------------------------


def test_completely_empty_mail_is_processed() -> None:
    """Leere Mail: kein Body, keine Anhänge, keine Exception (PLAN WP10)."""
    mail = MailSanitizer().sanitize(raw_mail(b""))
    assert mail.body_text == ""
    assert mail.attachments == []
    assert mail.sanitization_report.blocked_attachments == 0
    assert mail.from_display  # Fallback aus der Absenderadresse
    assert_body_clean(mail)


def test_headers_without_body() -> None:
    """Nur Kopfzeilen, kein Rumpf — der häufige „leere Weiterleitung"-Fall."""
    mail = MailSanitizer().sanitize(
        raw_mail(b"Subject: Nur Header\r\nFrom: a@sender.example\r\n\r\n")
    )
    assert mail.body_text == ""
    assert_body_clean(mail)


def test_attachment_only_without_any_body() -> None:
    """Nur Anhang, kein Textteil: der Anhang muss trotzdem als Metadatum erscheinen."""
    mime = multipart(
        attachment_part(
            b"application/vnd.ms-excel", b"tabelle.xls",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        )
    )
    mail = MailSanitizer().sanitize(raw_mail(mime))
    assert mail.body_text == ""
    assert len(mail.attachments) == 1
    assert mail.attachments[0].processed is False
    assert mail.sanitization_report.blocked_attachments == 1


def test_mail_with_thousand_recipients() -> None:
    """1000 Empfänger (PLAN WP10): kein Empfänger landet je in der Ausgabe."""
    recipients = [f"nutzer{index}@ziel.example" for index in range(1000)]
    header = b"To: " + b", ".join(item.encode() for item in recipients) + b"\r\n"
    mail = MailSanitizer().sanitize(
        raw_mail(header + b"\r\nKurzer Text.\r\n", to_addrs=recipients)
    )
    assert mail.body_text.strip() == "Kurzer Text."
    assert "nutzer999" not in mail.body_text
    assert_body_clean(mail)


def test_eight_bit_headers_and_broken_utf8() -> None:
    """8-bit-Header und kaputtes UTF-8 (PLAN WP10): Ersatzzeichen statt Absturz."""
    mime = (
        b"Subject: Rechnung \xfc\xdf\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Kaputt: \xff\xfe\xc3 Ende\r\n"
    )
    mail = MailSanitizer().sanitize(
        raw_mail(mime, subject_raw="Rechnung \udcfc\udcdf mit Surrogaten")
    )
    # Surrogate sind Kategorie „Cs" und fallen dem Unicode-Cleaning zum Opfer (F-SEC-10).
    assert "\udcfc" not in mail.subject
    assert "Ende" in mail.body_text
    assert_body_clean(mail)


def test_unparseable_charset_falls_back() -> None:
    """Unbekanntes Charset: `_decode_text_part` fällt zurück statt zu werfen."""
    mime = (
        b"Content-Type: text/plain; charset=x-voellig-erfunden\r\n\r\n"
        b"Text mit Sonderzeichen \xe4\xf6\xfc\r\n"
    )
    mail = MailSanitizer().sanitize(raw_mail(mime))
    assert "Text mit Sonderzeichen" in mail.body_text


def test_mail_without_any_valid_mime_structure() -> None:
    """Reiner Bytesalat ohne Kopfzeilen wird als Body gelesen, nicht als Fehler."""
    mail = MailSanitizer().sanitize(raw_mail(b"\x00\x01\x02 kein MIME \xff\xfe"))
    assert_body_clean(mail)


# --- MIME-Rekursion --------------------------------------------------------------------


@pytest.mark.parametrize("depth", [0, 1, 9, 10])
def test_mime_depth_within_limit_is_read(depth: int) -> None:
    """Bis zur konfigurierten Tiefe (Default 10) wird der Inhalt gelesen."""
    mail = MailSanitizer().sanitize(raw_mail(nested_multipart(depth)))
    assert "GANZ-TIEF" in mail.body_text


@pytest.mark.parametrize("depth", [11, 12, 40])
def test_mime_depth_beyond_limit_becomes_metadata(depth: int) -> None:
    """Jenseits der Tiefe (T10): Metadatum statt Inhalt, Pipeline läuft weiter."""
    mail = MailSanitizer().sanitize(raw_mail(nested_multipart(depth)))
    assert "GANZ-TIEF" not in mail.body_text
    assert any("tiefe" in item.filename_sanitized for item in mail.attachments)
    assert mail.sanitization_report.blocked_attachments >= 1


def test_message_rfc822_is_never_entered_even_when_nested() -> None:
    """T13: Mail-in-Mail-in-Mail wird nie betreten, auf keiner Ebene."""
    innermost = b"Subject: Geheim\r\n\r\nhttp://versteckt.example/payload\r\n"
    level_one = multipart(
        b"Content-Type: message/rfc822\r\n\r\n" + innermost, boundary=b"innen"
    )
    mime = multipart(
        b"Content-Type: text/plain\r\n\r\nAussen.\r\n",
        b"Content-Type: message/rfc822\r\n\r\n" + level_one,
    )
    mail = MailSanitizer().sanitize(raw_mail(mime))
    assert "Aussen." in mail.body_text
    assert "versteckt" not in mail.body_text
    assert "Geheim" not in mail.body_text
    assert_body_clean(mail)


# --- Anhänge an den Limit-Rändern ------------------------------------------------------


def test_zero_byte_attachment_is_metadata_only() -> None:
    """0-Byte-Anhang (PLAN WP10): kein `%PDF-`-Präfix ⇒ `mismatch`, nie verarbeitet."""
    mime = multipart(
        b"Content-Type: text/plain\r\n\r\nBody.\r\n",
        attachment_part(b"application/pdf", b"leer.pdf", b""),
    )
    mail = MailSanitizer().sanitize(raw_mail(mime))
    blocked = [item for item in mail.attachments if not item.processed]
    assert len(blocked) == 1
    assert blocked[0].size_bytes == 0
    assert blocked[0].detected_kind == "mismatch"
    assert blocked[0].extracted_chars == 0


@pytest.mark.parametrize(
    ("count", "expected_processed"),
    [(19, 19), (20, 20), (21, 20), (25, 20)],
)
def test_attachment_count_limit_is_exact(count: int, expected_processed: int) -> None:
    """Genau an der Anzahl-Schranke (Default 20): 20 verarbeitet, ab 21 nur Metadaten."""
    parts = [b"Content-Type: text/plain\r\n\r\nBody.\r\n"]
    parts += [
        attachment_part(b"text/plain", f"datei{index}.txt".encode(), b"Inhalt")
        for index in range(count)
    ]
    mail = MailSanitizer().sanitize(raw_mail(multipart(*parts)))
    processed = [item for item in mail.attachments if item.processed]
    assert len(processed) == expected_processed
    assert len(mail.attachment_texts) == expected_processed
    assert mail.sanitization_report.blocked_attachments == count - expected_processed


def test_mail_exactly_at_the_size_limit_is_accepted() -> None:
    """Genau `max_mail_bytes` ist erlaubt — erst ein Byte mehr ist zu groß (T10)."""
    limits = LimitsConfig(max_mail_bytes=4096)
    sanitizer = MailSanitizer(limits)
    header = b"Content-Type: text/plain\r\n\r\n"
    exact = header + b"a" * (4096 - len(header))
    assert len(exact) == 4096

    mail = sanitizer.sanitize(raw_mail(exact))
    assert mail.body_text

    with pytest.raises(SanitizeError):
        sanitizer.sanitize(raw_mail(exact + b"a"))


def test_declared_size_alone_triggers_the_limit() -> None:
    """Auch eine gelogene `size_bytes` (kleine Bytes, große Angabe) blockt fail-closed."""
    sanitizer = MailSanitizer(LimitsConfig(max_mail_bytes=100))
    with pytest.raises(SanitizeError):
        sanitizer.sanitize(raw_mail(b"kurz", size_bytes=10_000))


@pytest.mark.parametrize("budget", [1, 2, 50, 200])
def test_text_budget_boundaries(budget: int) -> None:
    """Zeichenbudget an seinen Rändern: exakt passend, eins zu viel, absurd klein."""
    sanitizer = MailSanitizer(LimitsConfig(max_text_chars=budget))
    body = "x" * budget
    exact = sanitizer.sanitize(
        raw_mail(b"Content-Type: text/plain\r\n\r\n" + body.encode())
    )
    assert exact.sanitization_report.truncated is False
    assert len(exact.body_text) <= budget

    over = sanitizer.sanitize(
        raw_mail(b"Content-Type: text/plain\r\n\r\n" + (body + "y").encode())
    )
    assert over.sanitization_report.truncated is True
    assert "[gekürzt]" in over.body_text


def test_text_budget_is_shared_between_body_and_attachments() -> None:
    """Das Budget gilt über Body **und** Anhangs-Texte hinweg (SECURITY §4)."""
    sanitizer = MailSanitizer(LimitsConfig(max_text_chars=30))
    mime = multipart(
        b"Content-Type: text/plain\r\n\r\n" + b"b" * 25 + b"\r\n",
        attachment_part(b"text/plain", b"anhang.txt", b"a" * 100),
    )
    mail = sanitizer.sanitize(raw_mail(mime))
    assert mail.sanitization_report.truncated is True
    assert all("[gekürzt]" in text or len(text) <= 30 for text in mail.attachment_texts.values())


def test_pdf_over_input_limit_stays_unprocessed() -> None:
    """PDF über `pdf_max_input_bytes`: kein Subprozess-Start, Anhang bleibt Metadatum."""
    sanitizer = MailSanitizer(LimitsConfig(pdf_max_input_bytes=len(MINIMAL_PDF) - 1))
    mime = multipart(attachment_part(b"application/pdf", b"gross.pdf", MINIMAL_PDF))
    mail = sanitizer.sanitize(raw_mail(mime))
    assert mail.attachments[0].processed is False
    assert mail.attachment_texts == {}


def test_duplicate_attachment_names_do_not_collide() -> None:
    """Gleichnamige Anhänge bekommen eindeutige Schlüssel (`_unique_name`)."""
    parts = [
        attachment_part(b"text/plain", b"gleich.txt", f"Inhalt {index}".encode())
        for index in range(3)
    ]
    mail = MailSanitizer().sanitize(raw_mail(multipart(*parts)))
    names = [item.filename_sanitized for item in mail.attachments]
    assert len(set(names)) == 3
    assert len(mail.attachment_texts) == 3


def test_attachment_metadata_list_is_capped_but_counter_is_not() -> None:
    """Anhang-Bombe: Liste bei 100 gedeckelt, `blocked_attachments` zählt alle (T10)."""
    parts = [
        attachment_part(b"application/x-msdownload", f"v{index}.exe".encode(), b"MZ\x90")
        for index in range(150)
    ]
    mail = MailSanitizer().sanitize(raw_mail(multipart(*parts)))
    assert len(mail.attachments) == 100
    assert mail.sanitization_report.blocked_attachments == 100
    # Der Composer nennt höchstens 10 namentlich und zählt den Rest.
    composed = DigestComposer().compose(mail, _summary(), _verdict())
    assert "and 90 more" in "\n".join(composed.parts)


# --- Nachrichtenbau an den Rändern ------------------------------------------------------


def _summary(**overrides: Any) -> Summary:
    data: dict[str, Any] = {
        "headline": "Kopfzeile",
        "summary_text": "Inhalt.",
        "importance": "normal",
        "importance_reason": "Grund",
        "category": "other",
    }
    data.update(overrides)
    return Summary(**data)


def _verdict(**overrides: Any) -> CriticVerdict:
    data: dict[str, Any] = {
        "phishing_risk": "none",
        "risk_reasons": [],
        "summary_accurate": True,
        "notes": "",
    }
    data.update(overrides)
    return CriticVerdict(**data)


def _sanitized(**overrides: Any) -> SanitizedMail:
    data: dict[str, Any] = {
        "dedupe_key": "<hot@example>",
        "from_display": "Absender",
        "from_domain": "sender.example",
        "subject": "Betreff",
        "body_text": "Text",
        "sanitization_report": SanitizationReport(),
    }
    data.update(overrides)
    return SanitizedMail(**data)


@pytest.mark.parametrize("limit", [1, 2, 3, 10, 4095, 4096])
def test_split_never_exceeds_the_limit(limit: int) -> None:
    """Auch bei absurd kleinem Limit hält der Split das Limit ein.

    Seit HC-6 trägt jedes Stück eines harten Zeilenschnitts das Fortsetzungspräfix; es
    zählt zum Limit und wird für den Inhaltsvergleich wieder abgezogen.
    """
    text = "Zeile eins\nZeile zwei mit einem sehr langen ununterbrochenen Wort " + "w" * 200
    parts = split_parts(text, limit)
    assert all(len(part) <= limit for part in parts)
    joined = "".join("".join(part.split()) for part in _strip_continuation(parts))
    assert joined == "".join(text.split())


def test_split_rejects_a_limit_below_one() -> None:
    """`limit < 1` ist ein Programmierfehler, kein stiller Sonderfall."""
    with pytest.raises(ValueError, match="mindestens 1"):
        split_parts("Text", 0)


def test_compose_survives_completely_empty_fields() -> None:
    """Leere Summary-Felder erzeugen Platzhalter statt einer kaputten Nachricht."""
    message = DigestComposer().compose(
        _sanitized(from_display="", from_domain="", subject=""),
        _summary(headline="", summary_text="", importance_reason="", category=""),
        _verdict(),
    )
    text = "\n".join(message.parts)
    assert "(keine Zusammenfassung)" in text
    assert "unbekannt" in text


def test_compose_of_a_message_longer_than_one_part() -> None:
    """Eine Nachricht über dem Messenger-Limit wird in mehrere Teile zerlegt."""
    composer = DigestComposer(part_limit=200)
    message = composer.compose(
        _sanitized(), _summary(summary_text="Sehr langer Satz. " * 80), _verdict()
    )
    assert len(message.parts) > 1
    assert all(len(part) <= 200 for part in message.parts)


def test_low_digest_without_entries_is_refused() -> None:
    """Ein leerer Sammel-Digest wird nie erzeugt (F-SUM-5)."""
    with pytest.raises(ValueError, match="without entries"):
        DigestComposer().compose_low_digest([])


def test_low_digest_caps_the_listed_items() -> None:
    """Mehr als 60 Einträge: der Rest wird gezählt, nicht aufgezählt."""
    items = [
        LowDigestItem(headline=f"Mail {index}", category="newsletter", from_domain="x.example")
        for index in range(75)
    ]
    text = "\n".join(DigestComposer().compose_low_digest(items).parts)
    assert "75 low-priority mails" in text
    assert "and 15 more" in text


def test_attachment_summary_lines_are_capped() -> None:
    """Auch erfundene Mengen an Anhangs-Zusammenfassungen sprengen das Format nicht."""
    summary = _summary(
        attachment_summaries={f"datei{index}.txt": "Inhalt" for index in range(50)}
    )
    message = DigestComposer().compose(_sanitized(), summary, _verdict())
    assert "\n".join(message.parts).count("— datei") <= 10


def test_failure_notice_needs_no_sanitizer_output() -> None:
    """Die Notiz entsteht auch dann, wenn der Sanitizer selbst versagt hat (I6)."""
    from maildigest.models import FailureNotice

    notice = FailureNotice(
        dedupe_key="<x@example>",
        from_domain="evil.example",
        subject_sanitized="Dringend http://evil.example/login",
        stage="sanitize",
        reason_class="sanitize_error",
    )
    text = "\n".join(DigestComposer().compose_failure(notice).parts)
    assert "://" not in text
    assert "evil[.]example" in text


def test_date_without_value_is_labelled() -> None:
    """Fehlender/kaputter `Date`-Header erzeugt einen Platzhalter, keinen Absturz."""
    text = "\n".join(DigestComposer().compose(_sanitized(date=None), _summary(), _verdict()).parts)
    assert "Datum unbekannt" in text


def test_date_at_midnight_is_formatted() -> None:
    """Mitternacht ist kein Sonderfall der Formatierung (`00:00`, nicht leer)."""
    midnight = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    text = "\n".join(
        DigestComposer().compose(_sanitized(date=midnight), _summary(), _verdict()).parts
    )
    assert "01.01. 00:00" in text


# --- Regressionstests zu den Hot-Findings (docs/TESTING.md §5) ---------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "x[.]y*fett*",
        "a[.]b`code`c",
        "evil[.]com||spoiler||",
        "hxxp[:]//x*fett*y",
        "a[.]b~~durchgestrichen~~",
        "[Link #1: a[.]b]*fett*",
        "a[.]b\\entkommen",
    ],
)
def test_ht1_markup_never_hides_inside_a_defanged_span(payload: str) -> None:
    """HT-1: Markup-Zeichen dürfen sich nicht in einer „sicheren Form" verstecken.

    Der Span-Durchgriff sollte bereits defangte WP3-Formen unangetastet lassen. Weil die
    Zeichenklasse Markup mit einschloss, reichte ein `[.]` im selben Wort, um `*fett*`,
    Backticks oder `||spoiler||` an der Markup-Neutralisierung vorbeizuschleusen — auf
    Discord, das Markdown im `content` rendert, ist das sichtbare Formatierung.
    """
    result = final_guard(scrub_field(payload))
    assert not set("`*|~\\") & set(result), f"Markup überlebt in {result!r}"


def test_ht2_a_long_scheme_name_is_still_broken() -> None:
    """HT-2: Ein Schema-Name über dem Zeichenlimit hebelte den Nachbrenner aus.

    `\\b` plus Pflicht-Name mit ≤ 16 Zeichen bedeutete: Steht links vom Schema keine
    Wortgrenze und ist der Name lang genug, greift die Regel nicht — `://` überlebte.
    """
    assert "://" not in final_guard(scrub_field("einsehrlangeswortalsschema://evil.example"))
    assert "://" not in final_guard(scrub_field("x" * 200 + "://ziel.example"))
    assert final_guard("tg://x") == "tg[:]//x"


@pytest.mark.parametrize(
    "payload",
    [
        "evil.com123456789012345678901234567890",  # letzte Marke nicht TLD-förmig
        "evil.com.123abc",  # nur die mittlere Marke ist TLD-förmig
        "evil.comÄ",  # Nicht-ASCII direkt hinter der TLD
        "-0000000.beispiel",  # führender Bindestrich unterdrückte das Token
    ],
)
def test_ht4_domain_tokens_are_defanged_even_in_odd_shapes(payload: str) -> None:
    """HT-4: Jede Marke ab der zweiten entscheidet über das Defangen, nicht nur die letzte.

    Sonst blieb im Token ein lebender Punkt stehen, den erst der **Schnitt** in
    `split_parts` zu einer verlinkbaren Domain machte.
    """
    guarded = final_guard(scrub_plain(payload))
    assert "[.]" in guarded, f"nicht defangt: {guarded!r}"


@pytest.mark.parametrize("limit", [5, 7, 12, 40])
def test_ht4_no_part_becomes_a_live_domain_through_the_cut(limit: int) -> None:
    """HT-4: Der Nachbrenner läuft nach dem Split, also ist auch jeder Teil sauber."""
    payload = "-0000000.beispiel " + "0" * 30 + ".AB0 evil.com.123abc"
    for part in DigestComposer(part_limit=limit).compose_plain(scrub_field(payload)).parts:
        assert not re.search(r"(?<![\w\-.])[a-z0-9-]+\.[a-z]{2}[a-z0-9-]*(?![\w\-.])", part), (
            f"lebende Domain im Teil {part!r}"
        )


@pytest.mark.parametrize("value", ["3.14", "1.2.3", "Version 2.0rc1", "z.B", "u.a"])
def test_ht4_numbers_and_abbreviations_stay_readable(value: str) -> None:
    """Gegenprobe zu HT-4: Zahlen und Abkürzungen werden **nicht** defangt."""
    assert final_guard(value) == value


@pytest.mark.parametrize(
    "payload",
    ["javascript:alert(1)", "data:text/html,x", "tg:x", "javascript&#58;(", "[.]([:]"],
)
def test_ht6_defanged_forms_survive_a_second_scrub(payload: str) -> None:
    """HT-6: Ein zweiter Durchlauf darf ein Defang-Token nicht wieder aufbrechen.

    `javascript[:]` war keine bekannte „sichere Form"; der nächste `scrub_field`-Lauf
    entfernte die Klammern als Markup und lieferte wieder `javascript:`. Zweite Durchläufe
    gibt es wirklich — Hinweiszeilen und Sammel-Digest-Kopfzeilen (ADR-049) laufen doppelt.
    """
    once = final_guard(scrub_field(payload))
    twice = final_guard(scrub_field(once))
    assert twice == once, f"nicht stabil: {once!r} ⇒ {twice!r}"


def test_ht5_single_letter_scheme_is_flagged_by_the_summarizer() -> None:
    """HT-5: Auch ein einbuchstabiges Schema (`a://`) gilt als URL-Fund (SECURITY §5)."""
    from maildigest.agents.summarizer import scrub_text

    cleaned, suspicious = scrub_text("Bitte a://ziel.example oeffnen")
    assert suspicious is True
    assert "://" not in cleaned


def test_blocked_attachment_without_name_is_labelled() -> None:
    """Anhang ohne Namen bekommt einen Platzhalter statt einer leeren Klammer."""
    mail = _sanitized(
        attachments=[
            AttachmentInfo(
                filename_sanitized="",
                declared_mime="application/octet-stream",
                detected_kind="unknown",
                size_bytes=0,
            )
        ]
    )
    text = "\n".join(DigestComposer().compose(mail, _summary(), _verdict()).parts)
    assert "(unnamed) (0 B)" in text
