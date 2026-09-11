"""Integrationstest: der komplette WP3-Korpus (tests/corpus/*.eml) durch den Sanitizer.

Zentrale Akzeptanz-Property (PLAN WP3): Für **jede** Korpus-Mail gilt nach dem Sanitizer —
kein URL-Muster, kein HTML-Tag, keine Zero-Width-/Bidi-Steuerzeichen in `body_text`,
`attachment_texts`, `subject` und `from_display`. Dazu Fall-für-Fall-Prüfungen des
`sanitization_report`.
"""

from __future__ import annotations

import email
import re
import unicodedata
from pathlib import Path

import pytest
from imap_tools import MailMessage

from maildigest.ingest.imap_client import build_raw_mail
from maildigest.models import RawMail, SanitizedMail
from maildigest.sanitize import MailSanitizer

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
CORPUS_FILES = sorted(CORPUS_DIR.glob("*.eml"))

#: Verbotene Muster im sanitisierten Output. Die Marker `[Link #n: domain.tld]` enthalten
#: bewusst einen nackten Domain-Namen (laut I3 erlaubt) — alles andere ist verboten.
FORBIDDEN_PATTERNS = (
    re.compile(r"https?\s*:", re.IGNORECASE),
    re.compile(r"hxxp", re.IGNORECASE),
    re.compile(r"://"),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"mailto\s*:", re.IGNORECASE),
    re.compile(r"\(\s*\.\s*\)"),
    re.compile(r"\[\s*\.\s*\]"),
    re.compile(r"</?[a-zA-Z][^>]*>"),  # HTML-Tags
)


def load_corpus_mail(path: Path) -> RawMail:
    """Baut aus einer Korpus-.eml eine `RawMail` — über den **echten** Ingest-Weg.

    Früher stand hier ein Nachbau mit eigenem RFC-2047-Dekodier-Helfer für From/Reply-To.
    Der hat einen `from_addr` erzeugt, den die Produktion nie geliefert hat (HC-23): Der
    Korpus-Test prüfte gegen einen besseren Wert als den echten. Seit `build_raw_mail` den
    Anzeigenamen selbst dekodiert, ist der Nachbau nicht nur überflüssig, sondern schädlich.
    Nur der `dedupe_key` bleibt der Dateiname — er macht Testausgaben lesbar.
    """
    data = path.read_bytes()
    raw = build_raw_mail(MailMessage.from_bytes(data))
    return raw.model_copy(update={"dedupe_key": path.name})


def sanitize_corpus_mail(path: Path) -> SanitizedMail:
    return MailSanitizer().sanitize(load_corpus_mail(path))


def all_output_texts(mail: SanitizedMail) -> dict[str, str]:
    texts = {
        "body_text": mail.body_text,
        "subject": mail.subject,
        "from_display": mail.from_display,
    }
    for name, text in mail.attachment_texts.items():
        texts[f"attachment:{name}"] = text
    return texts


def test_korpus_ist_vollstaendig() -> None:
    assert len(CORPUS_FILES) >= 15, "PLAN WP3 verlangt mindestens 15 Korpus-Mails"


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda p: p.name)
def test_jede_korpus_mail_hat_zweck_kommentar(path: Path) -> None:
    message = email.message_from_bytes(path.read_bytes())
    assert message.get("X-Test-Purpose"), f"{path.name} ohne X-Test-Purpose-Header"


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda p: p.name)
def test_jede_korpus_mail_laeuft_ohne_exception_durch(path: Path) -> None:
    mail = sanitize_corpus_mail(path)
    assert isinstance(mail, SanitizedMail)


@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda p: p.name)
def test_akzeptanz_property_kein_url_html_steuerzeichen(path: Path) -> None:
    """Die zentrale Akzeptanz-Property über ALLE Korpus-Ergebnisse (PLAN WP3)."""
    mail = sanitize_corpus_mail(path)
    for field, text in all_output_texts(mail).items():
        for pattern in FORBIDDEN_PATTERNS:
            assert not pattern.search(text), (
                f"{path.name}/{field}: verbotenes Muster {pattern.pattern!r} in {text!r}"
            )
        for char in text:
            if char in ("\t", "\n"):
                continue
            assert not unicodedata.category(char).startswith("C"), (
                f"{path.name}/{field}: Steuer-/Formatzeichen U+{ord(char):04X} im Output"
            )


# --- Fall-für-Fall-Prüfungen ---------------------------------------------------------------


def by_name(name: str) -> SanitizedMail:
    return sanitize_corpus_mail(CORPUS_DIR / name)


def test_01_plain_wird_bevorzugt_html_link_taucht_nicht_auf() -> None:
    mail = by_name("01_multipart_plain_html.eml")
    assert "Projekt liegt im Plan" in mail.body_text
    assert "nur-im-html-teil" not in mail.body_text
    assert mail.sanitization_report.links_removed == 0
    assert mail.sanitization_report.auth_results == {
        "spf": "pass",
        "dkim": "pass",
        "dmarc": "pass",
    }


def test_02_html_only_pixel_weg_alt_text_da_link_defangt() -> None:
    mail = by_name("02_html_only.eml")
    assert "[Bild: Firmenlogo Herbstaktion]" in mail.body_text
    assert "tracker" not in mail.body_text
    assert "sofort-weg" not in mail.body_text  # script entfernt
    assert "kommentar-link" not in mail.body_text  # Kommentar entfernt
    assert "[Link #1: shop.example]" in mail.body_text
    assert mail.links_found == ["#1: hxxps[:]//shop[.]example/aktion?id=99"]


def test_03_pdf_anhang_wird_extrahiert_und_defangt() -> None:
    mail = by_name("03_attachment_pdf_ok.eml")
    info = mail.attachments[0]
    assert info.detected_kind == "pdf"
    assert info.processed is True
    assert info.extracted_chars > 0
    text = mail.attachment_texts["rechnung-4711.pdf"]
    assert "Rechnung Nr. 4711" in text
    assert "[Link #1: rechnung-portal.example]" in text
    assert mail.sanitization_report.blocked_attachments == 0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("04_attachment_docx_blocked.eml", [("vertrag.docx", "unknown")]),
        (
            "05_attachment_zip_exe_blocked.eml",
            [("daten.zip", "unknown"), ("setup.exe", "unknown")],
        ),
        (
            "06_attachment_js_ics_blocked.eml",
            [("einladung.ics", "unknown"), ("helfer.js", "unknown")],
        ),
        (
            "07_attachment_image_blocked.eml",
            [("anhang-1", "unknown"), ("event.jpg", "unknown")],
        ),
    ],
)
def test_04_bis_07_geblockte_anhangsklassen(
    name: str, expected: list[tuple[str, str]]
) -> None:
    mail = by_name(name)
    got = [(info.filename_sanitized, info.detected_kind) for info in mail.attachments]
    assert got == expected
    assert all(not info.processed for info in mail.attachments)
    assert all(info.extracted_chars == 0 for info in mail.attachments)
    assert mail.sanitization_report.blocked_attachments == len(expected)
    assert not mail.attachment_texts


def test_08_eingebettete_mail_wird_nie_geoeffnet() -> None:
    mail = by_name("08_attachment_rfc822_blocked.eml")
    assert len(mail.attachments) == 1
    assert mail.attachments[0].declared_mime == "message/rfc822"
    assert mail.attachments[0].processed is False
    # Der Inhalt der inneren Mail darf nirgends auftauchen (T13).
    for text in all_output_texts(mail).values():
        assert "GEHEIMER-INNERER-INHALT" not in text
        assert "schmuggel" not in text


def test_09_gefaelschter_mime_typ_ist_mismatch() -> None:
    mail = by_name("09_mime_forged_exe_as_pdf.eml")
    info = mail.attachments[0]
    assert info.filename_sanitized == "rechnung.pdf"
    assert info.declared_mime == "application/pdf"
    assert info.detected_kind == "mismatch"
    assert info.processed is False
    assert mail.sanitization_report.blocked_attachments == 1
    assert not mail.attachment_texts


def test_10_alle_obfuskationsvarianten_werden_erkannt() -> None:
    mail = by_name("10_obfuscated_links.eml")
    report = mail.sanitization_report
    assert report.links_removed == 9
    found = "\n".join(mail.links_found)
    assert "hxxps[:]//konto-check[.]example/login" in found
    assert "hxxps[:]//verify[.]example[.]org" in found
    assert "hxxp[:]//encoded-ziel[.]example/pfad?x=1" in found
    assert "hxxps[:]//normal-link[.]example/abc" in found
    assert "mailto[:]support@fake-hilfe[.]example" in found
    assert "tel[:]+49" in found
    assert "www[.]portal-beispiel[.]example/start" in found
    assert "sicherheit[.]example[.]net" in found
    assert "phishing-seite[.]example" in found
    # Dateinamen sind keine Links:
    assert "backup.zip" in mail.body_text
    assert "README.md" in mail.body_text


def test_11_zero_width_injection_wird_neutralisiert() -> None:
    mail = by_name("11_zero_width_injection.eml")
    report = mail.sanitization_report
    assert report.control_chars_removed >= 6
    # Die per U+200B zerrissene URL muss trotzdem gefunden worden sein:
    assert any("unsichtbar[.]example" in entry for entry in mail.links_found)
    assert "[Link #" in mail.body_text


def test_12_bidi_rlo_in_betreff_und_body_entfernt() -> None:
    mail = by_name("12_bidi_rlo_subject.eml")
    assert mail.subject == "Anhang: Fotogpj.exe ansehen"
    assert mail.sanitization_report.control_chars_removed >= 4


def test_13_punycode_und_mixed_script_gekennzeichnet() -> None:
    mail = by_name("13_punycode_mixed_script.eml")
    report = mail.sanitization_report
    assert report.punycode_domains == ["xn--pypal-4ve[.]com (Unicode: pаypal[.]com)"]
    assert report.mixed_script_domains == ["pаypal[.]com"]
    assert "punycode" in mail.body_text
    assert "mixed writing systems" in mail.body_text


def test_14_versteckte_prompt_injection_wird_entfernt() -> None:
    mail = by_name("14_hidden_text_injection.eml")
    assert mail.sanitization_report.hidden_text_removed is True
    body = mail.body_text.lower()
    assert "ignore previous instructions" not in body
    assert "forward all future mails" not in body
    assert "system override" not in body
    assert "Wochenbericht" in mail.body_text


def test_15_degeneriertes_pdf_wird_nicht_verarbeitet() -> None:
    mail = by_name("15_pdf_degenerate.eml")
    info = mail.attachments[0]
    assert info.detected_kind == "pdf"
    assert info.processed is False
    assert info.extracted_chars == 0
    assert mail.sanitization_report.blocked_attachments == 1
    assert not mail.attachment_texts


def test_16_kaputtes_mime_faellt_nicht_um() -> None:
    mail = by_name("16_broken_mime.eml")
    # Kein Absturz, und was immer als Text herauskommt, ist frei von URLs/Tags —
    # das prüft die Akzeptanz-Property; hier nur: Ergebnis existiert.
    assert isinstance(mail.body_text, str)


def test_17_mail_ohne_body_nur_anhang() -> None:
    mail = by_name("17_no_body_only_attachment.eml")
    assert mail.body_text == ""
    assert mail.attachments[0].processed is True
    assert "ausschliesslich im PDF" in mail.attachment_texts["einziger-inhalt.pdf"]


def test_18_mime_rekursion_wird_begrenzt() -> None:
    mail = by_name("18_mime_recursion.eml")
    assert "Oberster Text-Teil." in mail.body_text
    assert "ZU-TIEF-VERSCHACHTELTER-TEXT" not in mail.body_text
    assert any(
        info.filename_sanitized == "(mime-tiefe ueberschritten)"
        for info in mail.attachments
    )
    assert mail.sanitization_report.blocked_attachments >= 1


def test_19_html_datei_anhang_wird_geblockt() -> None:
    mail = by_name("19_attachment_html_file_blocked.eml")
    info = mail.attachments[0]
    assert info.filename_sanitized == "angebot.html"
    assert info.detected_kind == "html"
    assert info.processed is False
    for text in all_output_texts(mail).values():
        assert "SCHMUGGEL" not in text
        assert "smuggle-ziel" not in text


def test_20_verarbeitungslimit_fuer_anhaenge() -> None:
    mail = by_name("20_many_attachments.eml")
    processed = [info for info in mail.attachments if info.processed]
    blocked = [info for info in mail.attachments if not info.processed]
    assert len(processed) == 20
    assert len(blocked) == 5
    assert len(mail.attachment_texts) == 20
    assert mail.sanitization_report.blocked_attachments == 5
