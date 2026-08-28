"""Unit-Tests für `sanitize/attachments.py` — Magic-Bytes-Tabelle einzeln (F-SEC-4, T6)."""

from __future__ import annotations

import pytest

from maildigest.sanitize.attachments import (
    ALLOWLIST_MIMES,
    detect_kind,
    sanitize_filename,
    sniff_signature,
)

PDF_BYTES = b"%PDF-1.7\nirgendwas"


class TestSignaturTabelle:
    @pytest.mark.parametrize(
        ("data", "label"),
        [
            (b"%PDF-1.4 rest", "pdf"),
            (b"MZ\x90\x00rest", "exe"),
            (b"\x7fELF\x02\x01", "elf"),
            (b"PK\x03\x04rest", "zip"),
            (b"PK\x05\x06rest", "zip"),
            (b"PK\x07\x08rest", "zip"),
            (b"Rar!\x1a\x07\x00", "rar"),
            (b"7z\xbc\xaf\x27\x1crest", "7z"),
            (b"\x1f\x8b\x08rest", "gzip"),
            (b"BZh91AY", "bzip2"),
            (b"\xfd7zXZ\x00rest", "xz"),
            (b"MSCFrest", "cab"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "ole2"),
            (b"\x89PNG\r\n\x1a\nrest", "png"),
            (b"\xff\xd8\xff\xe0rest", "jpeg"),
            (b"GIF87arest", "gif"),
            (b"GIF89arest", "gif"),
            (b"BMrest", "bmp"),
            (b"II*\x00rest", "tiff"),
            (b"MM\x00*rest", "tiff"),
            (b"{\\rtf1\\ansi", "rtf"),
            (b"#!/bin/sh\necho x", "script"),
            (b"SQLite format 3\x00", "sqlite"),
            (b"\x00asm\x01\x00", "wasm"),
            (b"\xca\xfe\xba\xbe\x00", "macho-fat/javaclass"),
            (b"\xcf\xfa\xed\xfe\x07", "macho"),
        ],
    )
    def test_signatur_wird_erkannt(self, data: bytes, label: str) -> None:
        assert sniff_signature(data) == label

    def test_harmloser_text_hat_keine_signatur(self) -> None:
        assert sniff_signature(b"Hallo, das ist nur Text.") is None

    def test_signatur_nur_an_offset_null(self) -> None:
        # Eingebettete Magic Bytes mitten im Inhalt zählen nicht.
        assert sniff_signature(b"xxMZyy") is None
        assert sniff_signature(b" %PDF-1.4") is None

    def test_leere_daten(self) -> None:
        assert sniff_signature(b"") is None


class TestDetectKind:
    def test_allowlist_ist_exakt_drei_typen(self) -> None:
        assert {"text/plain", "text/html", "application/pdf"} == ALLOWLIST_MIMES

    def test_pdf_mit_korrekten_magic_bytes(self) -> None:
        assert detect_kind("application/pdf", PDF_BYTES) == "pdf"

    def test_exe_als_pdf_deklariert_ist_mismatch(self) -> None:
        assert detect_kind("application/pdf", b"MZ\x90\x00payload") == "mismatch"

    def test_text_als_pdf_deklariert_ist_mismatch(self) -> None:
        assert detect_kind("application/pdf", b"nur text") == "mismatch"

    def test_text_plain_mit_text(self) -> None:
        assert detect_kind("text/plain", b"Hallo Welt\n") == "text"

    def test_text_plain_mit_binaerdaten_ist_mismatch(self) -> None:
        assert detect_kind("text/plain", b"ab\x00cd" * 100) == "mismatch"

    def test_text_plain_mit_zip_magic_ist_mismatch(self) -> None:
        assert detect_kind("text/plain", b"PK\x03\x04inhalt") == "mismatch"

    def test_text_plain_mit_pdf_magic_ist_mismatch(self) -> None:
        # Auch ein „harmloseres" Format als deklariert ist ein Mismatch (nie umdeuten).
        assert detect_kind("text/plain", PDF_BYTES) == "mismatch"

    def test_html_deklariert_mit_html(self) -> None:
        assert detect_kind("text/html", b"<html><body>x</body></html>") == "html"

    def test_html_deklariert_mit_exe_ist_mismatch(self) -> None:
        assert detect_kind("text/html", b"MZ\x90\x00") == "mismatch"

    def test_office_mime_ist_unknown(self) -> None:
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert detect_kind(mime, b"PK\x03\x04docx") == "unknown"

    def test_octet_stream_ist_unknown_auch_wenn_inhalt_pdf_ist(self) -> None:
        # Allowlist gilt für Deklaration UND Inhalt: ein nicht als PDF deklarierter
        # PDF-Inhalt wird nicht „hochgestuft".
        assert detect_kind("application/octet-stream", PDF_BYTES) == "unknown"

    def test_mime_parameter_werden_ignoriert(self) -> None:
        assert detect_kind("text/plain; charset=utf-8", b"hallo") == "text"

    def test_gross_kleinschreibung_egal(self) -> None:
        assert detect_kind("Application/PDF", PDF_BYTES) == "pdf"

    def test_message_rfc822_ist_unknown(self) -> None:
        assert detect_kind("message/rfc822", b"From: x@y.example\r\n\r\nhi") == "unknown"

    def test_leerer_text_anhang_ist_text(self) -> None:
        assert detect_kind("text/plain", b"") == "text"

    def test_leerer_pdf_anhang_ist_mismatch(self) -> None:
        assert detect_kind("application/pdf", b"") == "mismatch"


class TestSanitizeFilename:
    def test_normaler_name_bleibt(self) -> None:
        assert sanitize_filename("rechnung-2026.pdf") == "rechnung-2026.pdf"

    def test_pfadanteile_werden_entfernt(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("C:\\Windows\\evil.exe") == "evil.exe"

    def test_bidi_und_unicode_tricks_werden_ersetzt(self) -> None:
        result = sanitize_filename("Foto‮gpj.exe")
        assert "‮" not in result
        assert result == "Foto_gpj.exe"

    def test_leerer_name_ergibt_fallback(self) -> None:
        assert sanitize_filename("") == "unbenannt"
        assert sanitize_filename(None) == "unbenannt"
        assert sanitize_filename("​​​") == "unbenannt"

    def test_laenge_wird_begrenzt(self) -> None:
        assert len(sanitize_filename("a" * 500 + ".pdf")) <= 80

    def test_fuehrende_punkte_werden_entfernt(self) -> None:
        assert not sanitize_filename(".hidden").startswith(".")

    def test_eigener_fallback(self) -> None:
        assert sanitize_filename(None, fallback="anhang-3") == "anhang-3"
