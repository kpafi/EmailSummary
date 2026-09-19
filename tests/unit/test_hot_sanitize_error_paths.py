"""Fehler- und Notpfade der Sanitize-Schicht — Hot-Testing WP10 (NF-6).

Der Sanitizer ist die sicherheitskritischste Komponente (docs/PLAN.md WP3), und gerade seine
defensiven Zweige („wirft nie", „best effort", „fail-safe") liefen bisher nicht unter Test:
Sie werden nur von kaputten Mails erreicht, die kein Korpus-Fall erzeugt. Genau dort wäre
ein Fehler aber teuer — ein Absturz in `_payload_bytes` würde eine Mail unverarbeitbar
machen, ein durchgereichter Rohtext die Invariante I1 brechen.

Getestet wird deshalb mit absichtlich defekten `email.message.Message`-Objekten, die bei
jedem Zugriff werfen, mit Bomben an den Obergrenzen und mit dem PDF-Kindprozess, den der
Elternprozess sonst nur über `subprocess` sieht.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from email.message import Message
from typing import Any

import pytest

from maildigest.config import LimitsConfig
from maildigest.models import RawMail
from maildigest.sanitize import extract_pdf
from maildigest.sanitize.html_to_text import html_to_text
from maildigest.sanitize.links import LinkCollector, build_footnote
from maildigest.sanitize.sanitizer import (
    MailSanitizer,
    SanitizeError,
    _content_disposition,
    _content_type,
    _decode_text_part,
    _filename,
    _payload_bytes,
)
from maildigest.sanitize.unicode_clean import is_mixed_script_domain

# --- Ein Message-Objekt, das bei jedem Zugriff explodiert ------------------------------


def _mail(mime: bytes, **overrides: Any) -> RawMail:
    data: dict[str, Any] = {
        "dedupe_key": "<k@example>",
        "from_addr": "a@x.example",
        "from_domain": "x.example",
        "mime_bytes": mime,
        "size_bytes": len(mime),
    }
    data.update(overrides)
    return RawMail(**data)



class HostileMessage(Message):  # type: ignore[misc]
    """MIME-Teil, dessen Zugriffsmethoden alle werfen.

    Kein Kunstprodukt: `email` aus der stdlib wirft bei kaputten Headern tatsächlich
    (`HeaderParseError`, `UnicodeError`, `AttributeError`), je nach Defekt an
    unterschiedlichen Stellen. Der Sanitizer fängt jede davon einzeln ab — dieser Stub
    prüft, dass keine Stelle vergessen wurde.
    """

    def get_content_type(self) -> str:
        raise ValueError("Header kaputt")

    def get_content_disposition(self) -> str | None:
        raise ValueError("Header kaputt")

    def get_filename(self, failobj: Any = None) -> Any:
        raise ValueError("Header kaputt")

    def get_payload(self, i: Any = None, decode: bool = False) -> Any:
        raise ValueError("Payload kaputt")

    def get_content_charset(self, failobj: Any = None) -> Any:
        raise ValueError("Charset kaputt")


def test_content_type_falls_back_to_octet_stream() -> None:
    """Ein unlesbarer `Content-Type` gilt als „unbekannt", nicht als Text (Allowlist)."""
    assert _content_type(HostileMessage()) == "application/octet-stream"


def test_content_disposition_falls_back_to_none() -> None:
    """Unlesbare Disposition ⇒ `None`; der Teil wird dann nicht als Inline-Body behandelt."""
    assert _content_disposition(HostileMessage()) is None


def test_filename_falls_back_to_none() -> None:
    """Unlesbarer Dateiname ⇒ `None` ⇒ der Sanitizer vergibt einen Ersatznamen."""
    assert _filename(HostileMessage()) is None


def test_payload_bytes_never_raises() -> None:
    """`_payload_bytes` liefert im Zweifel leere Bytes statt zu werfen."""
    assert _payload_bytes(HostileMessage()) == b""


def test_decode_text_part_never_raises() -> None:
    """`_decode_text_part` liefert im Zweifel den leeren String."""
    assert _decode_text_part(HostileMessage()) == ""


def test_string_payload_is_encoded_not_dropped() -> None:
    """Liefert `get_payload(decode=True)` einen `str`, wird er kodiert, nicht verworfen."""
    part = Message()
    part["Content-Type"] = "text/plain"
    part.set_payload("Text mit Umlaut ä")
    assert b"Text mit Umlaut" in _payload_bytes(part)


def test_broken_part_inside_a_real_mail_is_survived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ein kaputter Teil macht die ganze Mail nicht unverarbeitbar.

    Der Baum wird direkt eingesetzt statt serialisiert: Ein Teil, der bei *jedem*
    Header-Zugriff wirft, lässt sich gar nicht erst zu Bytes schreiben — genau deshalb
    muss der Sanitizer ihn im Baum aushalten.
    """
    container = Message()
    container["Content-Type"] = 'multipart/mixed; boundary="grenze"'
    good = Message()
    good["Content-Type"] = "text/plain"
    good.set_payload("Guter Teil.")
    container.attach(good)
    container.attach(HostileMessage())

    monkeypatch.setattr(
        "maildigest.sanitize.sanitizer.email.message_from_bytes",
        lambda *args, **kwargs: container,
    )
    mail = MailSanitizer().sanitize(_mail(b"platzhalter"))
    assert "Guter Teil." in mail.body_text
    # Der kaputte Teil erscheint als nicht verarbeitetes Anhang-Metadatum (fail-safe).
    assert mail.sanitization_report.blocked_attachments == 1
    assert mail.attachments[0].declared_mime == "application/octet-stream"


# --- Not-Ausgang: MIME überhaupt nicht parsbar ------------------------------------------


def test_unparseable_mime_raises_sanitize_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wirft schon `message_from_bytes`, ist die Mail fail-closed (I6, `mime_unparsbar`)."""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError("MIME-Baum zu tief für den Parser")

    monkeypatch.setattr("maildigest.sanitize.sanitizer.email.message_from_bytes", explode)
    with pytest.raises(SanitizeError, match="mime_unparsbar"):
        MailSanitizer().sanitize(
            RawMail(
                dedupe_key="<k@example>",
                from_addr="a@x.example",
                from_domain="x.example",
                mime_bytes=b"egal",
                size_bytes=5,
            )
        )


def test_sanitize_error_message_carries_no_mail_content() -> None:
    """Die Fehlermeldung ist ein konstantes Label — nie Mail-Inhalt (I5)."""
    sanitizer = MailSanitizer(LimitsConfig(max_mail_bytes=10))
    with pytest.raises(SanitizeError) as excinfo:
        sanitizer.sanitize(
            RawMail(
                dedupe_key="<k@example>",
                from_addr="a@x.example",
                from_domain="x.example",
                mime_bytes=b"GEHEIMER MAILINHALT der nicht in den Fehler darf",
                size_bytes=48,
            )
        )
    assert str(excinfo.value) == "mail_zu_gross"
    assert "GEHEIM" not in str(excinfo.value)


# --- Obergrenzen der Berichts-Felder ----------------------------------------------------


def test_overlong_subject_is_truncated_with_ellipsis() -> None:
    """Ein 10 000 Zeichen langer Betreff wird auf 300 gekürzt (Formatschutz, T10)."""
    mail = MailSanitizer().sanitize(
        _mail(b"\r\nBody.\r\n", subject_raw="B" * 10_000)
    )
    assert len(mail.subject) == 300
    assert mail.subject.endswith("…")


def test_overlong_display_name_is_truncated() -> None:
    """Auch der Anzeigename hat eine harte Obergrenze."""
    mail = MailSanitizer().sanitize(
        _mail(b"\r\nBody.\r\n", from_addr='"' + "N" * 500 + '" <a@x.example>')
    )
    assert len(mail.from_display) <= 120


def test_sender_domain_punycode_is_reported_even_without_links() -> None:
    """Die **Absender**-Domain wird eigenständig auf Punycode geprüft (T12, F-CRIT-3)."""
    mail = MailSanitizer().sanitize(
        _mail(b"\r\nOhne Links.\r\n", from_domain="xn--80ak6aa92e.example")
    )
    assert mail.sanitization_report.punycode_domains == ["xn--80ak6aa92e[.]example"]


def test_sender_domain_mixed_script_is_reported() -> None:
    """Homoglyphen in der Absender-Domain landen im Bericht, auch ohne Link im Text."""
    mail = MailSanitizer().sanitize(
        _mail(b"\r\nOhne Links.\r\n", from_domain="pаypal.example")  # kyrillisches а
    )
    assert mail.sanitization_report.mixed_script_domains == ["pаypal[.]example"]


def test_link_footnote_is_capped() -> None:
    """Die optionale Fußnote hat ein Zeichenbudget — eine Link-Bombe sprengt sie nicht."""
    body = " ".join(f"http://ziel{index}.example/{'p' * 200}" for index in range(100))
    mail = MailSanitizer().sanitize(
        _mail(b"Content-Type: text/plain\r\n\r\n" + body.encode())
    )
    footnote = build_footnote(mail.links_found)
    assert "[further links suppressed]" in footnote
    assert "://" not in footnote


def test_defanged_url_entry_is_length_capped() -> None:
    """Ein einzelner Fund in `links_found` wird bei 300 Zeichen abgeschnitten."""
    collector = LinkCollector()
    collector.scrub("http://ziel.example/" + "p" * 5000)
    assert len(collector.links_found[0]) <= 320
    assert collector.links_found[0].endswith("…")


def test_many_identical_attachment_names_stay_unique() -> None:
    """Auch 30 gleichnamige Anhänge bekommen eindeutige Schlüssel (`_unique_name`)."""
    boundary = b"grenze"
    parts = b"".join(
        b"--" + boundary + b"\r\nContent-Type: text/plain\r\n"
        b'Content-Disposition: attachment; filename="gleich.txt"\r\n\r\nInhalt\r\n'
        for _ in range(30)
    )
    mime = (
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n\r\n'
        + parts + b"--" + boundary + b"--\r\n"
    )
    mail = MailSanitizer().sanitize(_mail(mime))
    names = [item.filename_sanitized for item in mail.attachments]
    assert len(names) == len(set(names))


# --- Unicode-Hilfsfunktionen an ihren Rändern -------------------------------------------


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("pаypal.example", True),  # kyrillisches а in lateinischem Wort
        ("paypal.example", False),
        ("kyrillisch.example", False),  # je Label einheitlich
        ("日本語.example", False),  # CJK-Label, in sich einheitlich
        ("ελλην.example", False),
        ("ab日本.example", True),  # Latein + CJK im selben Label
        ("123.456", False),  # keine Buchstaben ⇒ kein Mix
        ("", False),
        ("☃.example", False),  # Zeichen ohne Schriftsystem werden ignoriert
    ],
)
def test_mixed_script_detection_edges(domain: str, expected: bool) -> None:
    """Die Homoglyphen-Erkennung an ihren Rändern (T12) — inkl. schriftloser Zeichen."""
    assert is_mixed_script_domain(domain) is expected


# --- HTML-Konvertierung: Notpfade --------------------------------------------------------


def test_html_without_any_tags_is_passed_through() -> None:
    """Reiner Text, der als HTML deklariert war, geht nicht verloren."""
    text, hidden = html_to_text("Nur Text, kein Markup.")
    assert "Nur Text" in text
    assert hidden == 0


def test_html_size_attributes_without_unit_are_understood() -> None:
    """Ein Tracking-Pixel wird auch ohne `px`-Einheit erkannt (`width="1"`)."""
    text, _hidden = html_to_text('<p>Text</p><img src="x" width="1" height="1">')
    assert "Text" in text
    assert "img" not in text


def test_html_anchor_without_href_is_kept_as_text() -> None:
    """Ein `<a>` ohne `href` verliert nur das Tag, nicht seinen Text."""
    text, _hidden = html_to_text("<a>Nur Text ohne Ziel</a>")
    assert "Nur Text ohne Ziel" in text


def test_deeply_nested_html_does_not_recurse_to_death() -> None:
    """Tief verschachteltes HTML (T10) wird verarbeitet, ohne den Stack zu sprengen."""
    text, _hidden = html_to_text("<div>" * 500 + "Kern" + "</div>" * 500)
    assert "Kern" in text


# --- PDF-Extraktion: Eltern- und Kindprozess ---------------------------------------------

#: Ein minimales, tatsächlich parsbares PDF mit einer Textzeile.
REAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 46>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (HALLO-PDF-TEXT) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)


@pytest.fixture
def recorded_rlimit(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, Any]]]:
    """Fängt `resource.setrlimit` ab und schreibt die Aufrufe mit.

    :func:`extract_pdf._worker_main` setzt das Adressraum-Limit des **eigenen** Prozesses.
    Im Betrieb ist das der Kindprozess; hier wäre es der pytest-Prozess — der danach auf
    512 MB gedeckelt bliebe und keine neuen Threads mehr starten könnte (Thread-Stacks
    brauchen Adressraum). Die Nebenläufigkeits-Tests fielen dann mit „can't start new
    thread" um: ein Testartefakt, das wie ein Produktfehler aussähe.

    Der Abfang ist kein Verlust, im Gegenteil — so lässt sich zusätzlich **prüfen**, dass
    das Limit überhaupt und mit dem richtigen Wert gesetzt wird (I7/F-SEC-9).
    """
    import resource

    calls: list[tuple[int, Any]] = []

    def record(which: int, limits: Any) -> None:
        calls.append((which, limits))

    monkeypatch.setattr(resource, "setrlimit", record)
    yield calls


def test_worker_extracts_text_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recorded_rlimit: list[tuple[int, Any]],
) -> None:
    """Der Kindprozess selbst (`_worker_main`) — sonst nur über `subprocess` sichtbar.

    Er liest die PDF-Bytes von stdin, setzt sein Speicherlimit und schreibt den gekürzten
    Text nach stdout. Ohne diesen Test liefe der Kern von I7/F-SEC-9 ungeprüft.
    """
    import resource

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(REAL_PDF)))
    exit_code = extract_pdf._worker_main(["1000", str(extract_pdf.DEFAULT_RSS_LIMIT_BYTES)])
    assert exit_code == 0
    assert "HALLO-PDF-TEXT" in capsys.readouterr().out
    # I7: Das Adressraum-Limit steht, **bevor** pdfminer importiert und angewandt wird.
    limit = extract_pdf.DEFAULT_RSS_LIMIT_BYTES
    assert recorded_rlimit == [(resource.RLIMIT_AS, (limit, limit))]


def test_worker_truncates_its_own_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recorded_rlimit: list[tuple[int, Any]],
) -> None:
    """Die Kürzung passiert schon im Kind — der Elternprozess liest nie mehr als nötig."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(REAL_PDF)))
    assert extract_pdf._worker_main(["5", str(extract_pdf.DEFAULT_RSS_LIMIT_BYTES)]) == 0
    assert len(capsys.readouterr().out) <= 5


def test_worker_runs_the_real_child_process() -> None:
    """Gegenprobe ohne Abfang: der echte Subprozess-Weg liefert denselben Text (ADR-029)."""
    text = extract_pdf.extract_pdf_text(
        REAL_PDF, timeout_seconds=20.0, max_input_bytes=1_000_000, max_output_chars=1000
    )
    assert text is not None
    assert "HALLO-PDF-TEXT" in text


def test_parent_truncates_an_oversized_child_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auch wenn das Kind zu viel liefert, kürzt der Elternprozess (doppelter Boden)."""

    class Completed:
        returncode = 0
        stdout = b"x" * 5000

    monkeypatch.setattr(
        extract_pdf.subprocess, "run", lambda *args, **kwargs: Completed()
    )
    text = extract_pdf.extract_pdf_text(
        REAL_PDF, timeout_seconds=1.0, max_input_bytes=10_000, max_output_chars=100
    )
    assert text is not None
    assert len(text) == 100


@pytest.mark.parametrize("data", [b"", b"%PDF-nur-muell", b"\x00" * 100])
def test_broken_pdf_never_raises(data: bytes) -> None:
    """Jeder kaputte PDF-Input endet in `None` — Anhang „nicht verarbeitet" (I7/T5)."""
    assert (
        extract_pdf.extract_pdf_text(
            data, timeout_seconds=20.0, max_input_bytes=10_000, max_output_chars=100
        )
        is None
    )


def test_pdf_timeout_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein Timeout des Kindprozesses ist kein Fehler nach oben, sondern `None`."""

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise extract_pdf.subprocess.TimeoutExpired(cmd="x", timeout=1.0)

    monkeypatch.setattr(extract_pdf.subprocess, "run", timeout)
    assert (
        extract_pdf.extract_pdf_text(
            REAL_PDF, timeout_seconds=1.0, max_input_bytes=10_000, max_output_chars=100
        )
        is None
    )


def test_pdf_subprocess_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startet der Subprozess gar nicht (kein Interpreter, kein Speicher), gilt dasselbe."""

    def no_process(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fork fehlgeschlagen")

    monkeypatch.setattr(extract_pdf.subprocess, "run", no_process)
    assert (
        extract_pdf.extract_pdf_text(
            REAL_PDF, timeout_seconds=1.0, max_input_bytes=10_000, max_output_chars=100
        )
        is None
    )
