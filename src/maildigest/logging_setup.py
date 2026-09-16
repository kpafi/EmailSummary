"""Strukturiertes Logging auf stdout (NF-5/I5), Level aus `[general] log_level`.

Umsetzung in WP8 (ADR-046/ADR-047). Jede Zeile ist ein JSON-Objekt mit festen Feldern
(`ts`, `level`, `logger`, `event`) plus den `extra`-Feldern der Aufrufstelle — damit sind
Logs maschinenlesbar (`journalctl -o cat | jq`) und trotzdem im Terminal lesbar.

Sicherheits-Design dieses Moduls:

* **Keine Mail-Inhalte, keine Secrets:** Aufrufstellen loggen ausschließlich Metadaten
  (gekürzter Dedupe-Hash, Absender-Domain, Status, Exception-**Klassenname**, Zähler). Der
  Formatter zieht keine zusätzlichen Quellen heran und serialisiert nur JSON-fähige Werte;
  alles andere wird zu seinem Typnamen verdichtet statt via `repr()` ausgegeben (ein `repr()`
  könnte Mail-Text oder ein Secret-Objekt sichtbar machen).
* **Tracebacks nur bei DEBUG (ADR-047):** Exception-Texte und Stacktraces können
  Mail-Inhalte transportieren (`ValueError: unerwartetes Zeichen in '<Mail-Zeile>'`). Auf
  den Standard-Leveln wird deshalb nur der Klassenname geloggt; der volle Traceback
  erscheint ausschließlich, wenn der Betreiber `log_level = "DEBUG"` bewusst einschaltet.
  Für diesen Fall dokumentiert docs/OPERATIONS.md die Vertraulichkeit der Logs.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Final, TextIO

__all__ = ["JsonLogFormatter", "configure_logging", "traceback_enabled"]

#: Logger-Name der Anwendung; alle Module hängen darunter (`maildigest.ingest`, …).
ROOT_LOGGER_NAME: Final = "maildigest"

#: Attribute eines `LogRecord`, die nicht aus `extra` stammen und nicht mitgeloggt werden.
_STANDARD_FIELDS: Final = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info", "taskName",
        "thread", "threadName",
    }
)

#: Typen, die unverändert in die JSON-Zeile dürfen.
_SAFE_TYPES: Final = (str, int, float, bool, type(None))


def traceback_enabled(logger: logging.Logger) -> bool:
    """True, wenn für diesen Logger DEBUG aktiv ist — nur dann darf ein Traceback ins Log.

    Aufrufstellen benutzen das als `exc_info=`-Argument (ADR-047)::

        logger.error("mail_processing_crashed", extra={...},
                     exc_info=traceback_enabled(logger))
    """
    return logger.isEnabledFor(logging.DEBUG)


class JsonLogFormatter(logging.Formatter):
    """Formatiert einen `LogRecord` als einzeiliges JSON-Objekt."""

    def format(self, record: logging.LogRecord) -> str:
        """Baut die JSON-Zeile aus festen Feldern + den `extra`-Feldern der Aufrufstelle."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_FIELDS or key.startswith("_") or key in payload:
                continue
            payload[key] = value if isinstance(value, _SAFE_TYPES) else type(value).__name__
        # Bewusst `if record.exc_info:` statt `is not None`: `exc_info=False` (der
        # Normalfall aus `traceback_enabled`) landet als `False` im Record, nicht als None.
        if record.exc_info:
            # Nur erreichbar, wenn die Aufrufstelle exc_info gesetzt hat — bei DEBUG (ADR-047).
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO", *, stream: TextIO | None = None, force: bool = True
) -> logging.Logger:
    """Richtet das Anwendungs-Logging ein und liefert den Wurzel-Logger von MailDigest.

    Es wird bewusst **nur** der Logger `maildigest` konfiguriert (kein `basicConfig` auf dem
    Wurzel-Logger): Fremdbibliotheken sollen nicht ungefragt in unser Format schreiben, und
    `imap_tools`/`httpx` könnten in ihren Debug-Logs Inhalte oder URLs mit Token führen.

    Args:
        level: Schwellwert aus `[general] log_level` (`DEBUG`/`INFO`/`WARNING`/`ERROR`).
        stream: Zielstrom; Default `sys.stdout` (systemd/journald lesen dort mit).
        force: Vorhandene Handler dieses Loggers ersetzen (Default) statt zu ergänzen.

    Returns:
        Den konfigurierten Logger `maildigest`.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Nichts an den Wurzel-Logger weiterreichen: sonst dupliziert ein fremdes basicConfig
    # unsere Zeilen in einem anderen Format.
    logger.propagate = False
    return logger
