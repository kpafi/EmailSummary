"""State-Subpaket: SQLite-Persistenz (Dedupe, Status).

WP2 implementiert `db.py` mit den Tabellen `seen_mails` und `meta`; die `low_digest_queue`
und die vollständige Retry-Buchführung folgen in WP8.
"""

from maildigest.state.db import (
    SCHEMA_VERSION,
    MailState,
    SeenMail,
    StateDB,
    StateError,
    dedupe_hash,
)

__all__ = [
    "SCHEMA_VERSION",
    "MailState",
    "SeenMail",
    "StateDB",
    "StateError",
    "dedupe_hash",
]
