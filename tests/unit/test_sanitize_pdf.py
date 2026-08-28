"""Unit-Tests für `sanitize/extract_pdf.py` — Subprozess mit Limits (I7, F-SEC-9, T5)."""

from __future__ import annotations

import sys
from pathlib import Path

from maildigest.sanitize.extract_pdf import extract_pdf_text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "corpus"))
from _make_corpus import build_pdf


def test_valides_pdf_liefert_text() -> None:
    pdf = build_pdf(["Hallo aus dem Test-PDF."])
    text = extract_pdf_text(
        pdf, timeout_seconds=20, max_input_bytes=10_000_000, max_output_chars=50_000
    )
    assert text is not None
    assert "Hallo aus dem Test-PDF." in text


def test_output_limit_kuerzt() -> None:
    pdf = build_pdf(["Zeile mit ordentlich Inhalt drin."] * 50)
    text = extract_pdf_text(
        pdf, timeout_seconds=20, max_input_bytes=10_000_000, max_output_chars=40
    )
    assert text is not None
    assert len(text) <= 40


def test_input_limit_lehnt_ab() -> None:
    pdf = build_pdf(["x"])
    assert (
        extract_pdf_text(
            pdf, timeout_seconds=20, max_input_bytes=10, max_output_chars=1000
        )
        is None
    )


def test_leere_daten_lehnt_ab() -> None:
    assert (
        extract_pdf_text(
            b"", timeout_seconds=20, max_input_bytes=1000, max_output_chars=1000
        )
        is None
    )


def test_kaputtes_pdf_faellt_kontrolliert_aus() -> None:
    garbage = b"%PDF-1.4\n" + b"\x00\x01kaputt" * 500
    assert (
        extract_pdf_text(
            garbage, timeout_seconds=20, max_input_bytes=10_000_000, max_output_chars=1000
        )
        is None
    )


def test_timeout_faellt_kontrolliert_aus() -> None:
    # Ein Timeout, das kürzer ist als der Python-Start des Subprozesses, erzwingt den
    # Timeout-Pfad deterministisch, ohne auf ein „langsames" PDF angewiesen zu sein.
    pdf = build_pdf(["x"])
    assert (
        extract_pdf_text(
            pdf, timeout_seconds=0.001, max_input_bytes=10_000_000, max_output_chars=1000
        )
        is None
    )


def test_ergebnis_ist_untrusted_text_mit_url_moeglich() -> None:
    # Der Subprozess sanitisiert bewusst NICHT — das übernimmt der Orchestrator
    # (unicode_clean + Link-Scrub). Hier wird nur belegt, dass URLs im Roh-Ergebnis
    # ankommen können und der Aufrufer sie behandeln muss.
    pdf = build_pdf(["Siehe http://im-pdf.example/pfad"])
    text = extract_pdf_text(
        pdf, timeout_seconds=20, max_input_bytes=10_000_000, max_output_chars=50_000
    )
    assert text is not None
    assert "http://im-pdf.example/pfad" in text
