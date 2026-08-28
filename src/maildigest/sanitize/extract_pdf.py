"""PDF-Textextraktion (pdfminer.six) im ressourcenbegrenzten Subprozess (Timeout/RSS-/
Größen-Limits) — Invariante I7 (F-SEC-9, T5).

Limits: docs/SECURITY.md §4; Subprozess-Mechanik: ADR-029.

Mechanik: Der Elternprozess startet ``python -m maildigest.sanitize.extract_pdf`` (denselben
Interpreter, also das venv), schreibt die PDF-Bytes auf stdin und liest den extrahierten
Text (UTF-8, bereits im Kind auf `max_output_chars` gekürzt) von stdout. Das Kind setzt
**vor** dem pdfminer-Import ein `RLIMIT_AS`-Speicherlimit. Timeout überwacht der
Elternprozess über `subprocess.run(timeout=…)`; danach wird das Kind gekillt.

Jeder Fehler (Timeout, Absturz, Nicht-Null-Exit, Größenlimit) führt zu ``None`` —
der Anhang gilt dann als „nicht verarbeitet", die Pipeline läuft weiter (fail-closed, I6).
stderr des Kindes wird verworfen und nie geloggt (pdfminer-Meldungen können Mail-Inhalt
enthalten, I5).
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["DEFAULT_RSS_LIMIT_BYTES", "extract_pdf_text"]

#: Adressraum-Limit des Extraktions-Subprozesses. Bewusst eine Code-Konstante und kein
#: Config-Feld: Der Wert schützt den Host, nicht den Nutzerkomfort (ADR-029).
DEFAULT_RSS_LIMIT_BYTES = 512 * 1024 * 1024


def extract_pdf_text(
    data: bytes,
    *,
    timeout_seconds: float,
    max_input_bytes: int,
    max_output_chars: int,
    rss_limit_bytes: int = DEFAULT_RSS_LIMIT_BYTES,
) -> str | None:
    """Extrahiert Text aus PDF-Bytes im Subprozess.

    Returns:
        Den extrahierten Text (≤ `max_output_chars` Zeichen) oder ``None``, wenn der
        Anhang als „nicht verarbeitet" gelten muss (Limit überschritten, Timeout,
        Parser-Absturz). Es wird nie eine Exception nach außen gereicht.
    """
    if not data or len(data) > max_input_bytes:
        return None
    command = [
        sys.executable,
        "-m",
        "maildigest.sanitize.extract_pdf",
        str(max_output_chars),
        str(rss_limit_bytes),
    ]
    try:
        completed = subprocess.run(
            command,
            input=data,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.decode("utf-8", errors="replace")
    if len(text) > max_output_chars:
        text = text[:max_output_chars]
    return text


def _worker_main(argv: list[str]) -> int:
    """Kind-Prozess: stdin = PDF-Bytes, stdout = extrahierter Text (gekürzt), UTF-8."""
    max_output_chars = int(argv[0])
    rss_limit_bytes = int(argv[1])
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (rss_limit_bytes, rss_limit_bytes))
    except (ImportError, ValueError, OSError):  # pragma: no cover — Nicht-POSIX/Container
        pass

    import io

    from pdfminer.high_level import extract_text

    pdf_bytes = sys.stdin.buffer.read()
    text = extract_text(io.BytesIO(pdf_bytes))
    sys.stdout.write(text[:max_output_chars])
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — läuft nur im Subprozess
    try:
        _exit_code = _worker_main(sys.argv[1:])
    except Exception:  # jeder Fehler = Exit 1, nie ein Traceback mit Mail-Inhalt (I5)
        _exit_code = 1
    sys.exit(_exit_code)
