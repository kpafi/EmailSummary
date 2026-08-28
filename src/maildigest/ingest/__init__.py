"""Ingest-Subpaket: Mail-Abruf aus dem Mirror-Postfach (WP2).

Öffentliche API: :mod:`maildigest.ingest.imap_client`.
"""

from maildigest.ingest.imap_client import (
    ImapClient,
    ImapConnectionError,
    IngestError,
    IngestService,
    IngestStats,
    backoff_delay,
    build_raw_mail,
    poll_once,
)

__all__ = [
    "ImapClient",
    "ImapConnectionError",
    "IngestError",
    "IngestService",
    "IngestStats",
    "backoff_delay",
    "build_raw_mail",
    "poll_once",
]
