"""Unit-Tests des strukturierten Loggings (`logging_setup.py`, WP8).

Prüffläche NF-5/I5/ADR-047: eine JSON-Zeile je Ereignis, keine Inhalte über die
`extra`-Felder hinaus, Traceback ausschließlich bei Log-Level DEBUG.
"""

from __future__ import annotations

import io
import json
import logging

from maildigest.logging_setup import configure_logging, traceback_enabled


def read_lines(stream: io.StringIO) -> list[dict[str, object]]:
    """Parst die geschriebenen Zeilen als JSON."""
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_event_is_written_as_one_json_line() -> None:
    """Feste Felder plus die `extra`-Felder der Aufrufstelle."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("maildigest.test").info(
        "mail_processed", extra={"mail": "abc123", "status": "delivered"}
    )
    entries = read_lines(stream)
    assert len(entries) == 1
    assert entries[0]["event"] == "mail_processed"
    assert entries[0]["level"] == "INFO"
    assert entries[0]["logger"] == "maildigest.test"
    assert entries[0]["mail"] == "abc123"
    assert entries[0]["status"] == "delivered"


def test_level_from_config_filters_lower_events() -> None:
    """`log_level = "WARNING"` unterdrückt INFO-Zeilen."""
    stream = io.StringIO()
    configure_logging("WARNING", stream=stream)
    logger = logging.getLogger("maildigest.test")
    logger.info("leise")
    logger.warning("laut")
    assert [entry["event"] for entry in read_lines(stream)] == ["laut"]


def test_unknown_level_falls_back_to_info() -> None:
    """Ein unbekannter Wert schaltet nicht versehentlich DEBUG frei."""
    stream = io.StringIO()
    logger = configure_logging("TRACE", stream=stream)
    assert logger.level == logging.INFO


def test_non_serializable_extra_becomes_its_type_name() -> None:
    """Kein `repr()` fremder Objekte — das könnte Mail-Text oder Secrets sichtbar machen."""

    class Secretish:
        def __repr__(self) -> str:  # pragma: no cover - darf nie aufgerufen werden
            return "geheimes-token"

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("maildigest.test").info("ereignis", extra={"obj": Secretish()})
    entry = read_lines(stream)[0]
    assert entry["obj"] == "Secretish"
    assert "geheimes-token" not in stream.getvalue()


def test_traceback_only_at_debug_level() -> None:
    """ADR-047: Auf INFO nur der Klassenname, auf DEBUG der volle Traceback."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    child = logging.getLogger("maildigest.test")
    try:
        raise ValueError("Mail-Zeile mit Inhalt")
    except ValueError as exc:
        child.error(
            "mail_processing_crashed",
            extra={"error": type(exc).__name__},
            exc_info=traceback_enabled(child),
        )
    entry = read_lines(stream)[0]
    assert entry["error"] == "ValueError"
    assert "traceback" not in entry
    assert "Mail-Zeile mit Inhalt" not in stream.getvalue()

    debug_stream = io.StringIO()
    configure_logging("DEBUG", stream=debug_stream)
    try:
        raise ValueError("Mail-Zeile mit Inhalt")
    except ValueError as exc:
        child.error(
            "mail_processing_crashed",
            extra={"error": type(exc).__name__},
            exc_info=traceback_enabled(child),
        )
    assert "traceback" in read_lines(debug_stream)[0]


def test_configure_logging_replaces_previous_handlers() -> None:
    """Mehrfaches Einrichten dupliziert keine Zeilen."""
    first = io.StringIO()
    second = io.StringIO()
    configure_logging("INFO", stream=first)
    configure_logging("INFO", stream=second)
    logging.getLogger("maildigest.test").info("einmal")
    assert len(read_lines(second)) == 1
    assert first.getvalue() == ""
