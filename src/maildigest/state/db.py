"""SQLite-State: Tabellen ``seen_mails`` und ``meta``, Statuswerte und Dedupe-Abfragen.

Datenmodell: docs/ARCHITECTURE.md §2 (State). Umsetzung des für WP2 Nötigen; die
Low-Digest-Queue und die vollständige Retry-Buchführung folgen in WP8.

Sicherheits-Design dieses Moduls:

* **Kein Mail-Volltext, keine PII** (docs/SECURITY.md §6, NF-5): Gespeichert wird
  ausschließlich der SHA-256-Hash des Dedupe-Keys, ein Zeitstempel, der Status, eine grobe
  Fehlerklasse und ein Retry-Zähler. Weder Betreff noch Absender noch Message-ID im Klartext.
* **I5:** Secrets landen nie in der DB. Die Fehlerklasse wird zusätzlich hart auf
  ``[a-z0-9_]`` und 64 Zeichen normalisiert, damit ein durchgereichter Fehlertext (der
  Inhalte enthalten könnte) hier strukturell nicht ankommen kann.
* Die Datenbankdatei wird mit Modus ``0600`` angelegt.

Idempotenz (F-ING-2) beruht auf :meth:`StateDB.claim`: ``INSERT OR IGNORE`` auf den
Primärschlüssel ``message_id_hash`` — genau der erste Aufruf gewinnt, jeder weitere meldet
"schon gesehen".
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final

__all__ = [
    "SCHEMA_VERSION",
    "MailState",
    "SeenMail",
    "StateDB",
    "StateError",
    "dedupe_hash",
]

#: Version des DB-Schemas; steht in `meta` und wird beim Öffnen geprüft.
SCHEMA_VERSION: Final = 1

#: Schlüssel der Schema-Version in der `meta`-Tabelle.
_META_SCHEMA_VERSION: Final = "schema_version"

#: Erlaubte Zeichen einer Fehlerklasse (alles andere wird verworfen, I5).
_ERROR_CLASS_RE: Final = re.compile(r"[^a-z0-9_]+")

#: Maximale Länge einer gespeicherten Fehlerklasse.
_ERROR_CLASS_MAX_CHARS: Final = 64

_SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS seen_mails (
    message_id_hash TEXT PRIMARY KEY,
    first_seen_at   TEXT NOT NULL,
    status          TEXT NOT NULL,
    error_class     TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_mails_status ON seen_mails(status);
"""


class StateError(Exception):
    """Die State-Datenbank ist nicht benutzbar (I/O-Fehler, fremdes Schema, Schema-Version)."""


class MailState(StrEnum):
    """Statuswerte einer Mail (docs/ARCHITECTURE.md §2).

    Übergänge: ``pending → sanitized → summarized → checked → delivered``; Abzweige
    ``failed`` (fail-closed, I6) und ``skipped_low`` (Sammel-Digest, F-SUM-5).
    WP2 nutzt ``pending`` beim Claim und schreibt danach den Endstatus, den die Pipeline
    zurückliefert; die Zwischenstufen bespielt WP8.
    """

    PENDING = "pending"
    SANITIZED = "sanitized"
    SUMMARIZED = "summarized"
    CHECKED = "checked"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED_LOW = "skipped_low"


@dataclass(frozen=True)
class SeenMail:
    """Ein Datensatz aus `seen_mails` — reine Metadaten, kein Mail-Inhalt."""

    message_id_hash: str
    first_seen_at: datetime
    status: MailState
    error_class: str | None
    retry_count: int


def dedupe_hash(dedupe_key: str) -> str:
    """Bildet den Dedupe-Key auf seinen SHA-256-Hex-Hash ab.

    Der Klartext-Key (Message-ID bzw. Fallback-Hash) ist ein Identifikator der Mail und wird
    deshalb weder gespeichert noch geloggt (NF-5); gespeichert wird nur dieser Hash. Er ist
    zugleich der Primärschlüssel von `seen_mails` und die einzige ID, die in Logs auftauchen
    darf (dort gekürzt).
    """
    return hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()


def _clean_error_class(error_class: str | None) -> str | None:
    """Normalisiert eine Fehlerklasse auf ein kurzes, inhaltsfreies Label (I5).

    Bewusst rigoros: Selbst wenn eine spätere Aufrufstelle versehentlich einen Fehlertext
    übergibt, kann daraus kein Mail-Inhalt und kein Secret in der DB landen.
    """
    if error_class is None:
        return None
    cleaned = _ERROR_CLASS_RE.sub("_", error_class.strip().lower()).strip("_")
    if not cleaned:
        return None
    return cleaned[:_ERROR_CLASS_MAX_CHARS]


class StateDB:
    """SQLite-Persistenz für Dedupe und Mail-Status.

    Bewusst synchron und ohne ORM (ADR-005). Eine Instanz gehört genau einem Thread; der
    Betrieb ist Single-Instance (v0.1).

    Beispiel:
        >>> with StateDB(":memory:") as db:
        ...     db.claim("<a@b>")
        ...     db.claim("<a@b>")
        True
        False
    """

    def __init__(self, path: str | Path) -> None:
        """Öffnet (und erzeugt bei Bedarf) die State-Datenbank unter `path`.

        Args:
            path: Dateipfad oder ``":memory:"`` für Tests.

        Raises:
            StateError: Die Datei kann nicht geöffnet/angelegt werden oder enthält ein
                Schema einer anderen, inkompatiblen Version.
        """
        self.path = Path(path) if path != ":memory:" else None
        try:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                existed = self.path.exists()
            else:
                existed = True
            self._conn = sqlite3.connect(
                str(self.path) if self.path is not None else ":memory:", timeout=10.0
            )
            if self.path is not None and not existed:
                # Metadaten über verarbeitete Mails gehen niemanden sonst etwas an.
                self.path.chmod(0o600)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            with self._conn:
                self._conn.executescript(_SCHEMA_SQL)
            self._check_schema_version()
        except sqlite3.Error as exc:
            self._close_quietly()
            raise StateError(f"State-Datenbank {path} nicht benutzbar: {exc}") from exc
        except OSError as exc:
            self._close_quietly()
            raise StateError(
                f"State-Datenbank {path} kann nicht angelegt werden: {exc.strerror}"
            ) from exc
        except StateError:
            self._close_quietly()
            raise

    def _close_quietly(self) -> None:
        """Gibt eine halb aufgebaute Verbindung frei (nur für den Fehlerpfad in `__init__`)."""
        connection = getattr(self, "_conn", None)
        if connection is not None:
            connection.close()

    # --- Lebenszyklus ---------------------------------------------------------------------

    def close(self) -> None:
        """Schließt die Verbindung; mehrfacher Aufruf ist erlaubt."""
        self._conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _check_schema_version(self) -> None:
        """Legt die Schema-Version an oder prüft sie (NF-3: keine Migrationstools)."""
        stored = self.meta_get(_META_SCHEMA_VERSION)
        if stored is None:
            self.meta_set(_META_SCHEMA_VERSION, str(SCHEMA_VERSION))
            return
        if stored != str(SCHEMA_VERSION):
            raise StateError(
                f"State-Datenbank hat Schema-Version {stored}, erwartet wird "
                f"{SCHEMA_VERSION}. Datei entfernen oder Version prüfen."
            )

    # --- seen_mails -----------------------------------------------------------------------

    def claim(self, dedupe_key: str, *, now: datetime | None = None) -> bool:
        """Reserviert eine Mail zur Verarbeitung — der Kern der Idempotenz (F-ING-2).

        Args:
            dedupe_key: Message-ID oder Fallback-Hash aus dem Ingest.
            now: Zeitstempel für `first_seen_at` (Default: jetzt, UTC).

        Returns:
            ``True``, wenn die Mail neu ist und der Aufrufer sie verarbeiten darf;
            ``False``, wenn sie bereits bekannt ist (Duplikat ⇒ überspringen).
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO seen_mails "
                "(message_id_hash, first_seen_at, status, error_class, retry_count) "
                "VALUES (?, ?, ?, NULL, 0)",
                (dedupe_hash(dedupe_key), timestamp, MailState.PENDING.value),
            )
        return cursor.rowcount == 1

    def was_seen(self, dedupe_key: str) -> bool:
        """True, wenn zu diesem Dedupe-Key bereits ein Datensatz existiert."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_mails WHERE message_id_hash = ?", (dedupe_hash(dedupe_key),)
        ).fetchone()
        return row is not None

    def mark_status(
        self,
        dedupe_key: str,
        status: MailState,
        *,
        error_class: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Schreibt den Status einer Mail (legt den Datensatz an, falls er fehlt).

        Args:
            dedupe_key: Dedupe-Key der Mail.
            status: Neuer Status.
            error_class: Grobe Fehlerklasse (nur bei ``failed`` sinnvoll). Wird auf ein
                inhaltsfreies Label normalisiert; ``None`` löscht eine frühere Klasse.
            now: Zeitstempel für `first_seen_at`, falls der Datensatz neu angelegt wird.
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        cleaned = _clean_error_class(error_class)
        with self._conn:
            self._conn.execute(
                "INSERT INTO seen_mails "
                "(message_id_hash, first_seen_at, status, error_class, retry_count) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(message_id_hash) DO UPDATE SET status = excluded.status, "
                "error_class = excluded.error_class",
                (dedupe_hash(dedupe_key), timestamp, status.value, cleaned),
            )

    def get(self, dedupe_key: str) -> SeenMail | None:
        """Liest den Datensatz zu einem Dedupe-Key; ``None``, wenn unbekannt."""
        row = self._conn.execute(
            "SELECT message_id_hash, first_seen_at, status, error_class, retry_count "
            "FROM seen_mails WHERE message_id_hash = ?",
            (dedupe_hash(dedupe_key),),
        ).fetchone()
        if row is None:
            return None
        return SeenMail(
            message_id_hash=str(row["message_id_hash"]),
            first_seen_at=datetime.fromisoformat(str(row["first_seen_at"])),
            status=MailState(str(row["status"])),
            error_class=None if row["error_class"] is None else str(row["error_class"]),
            retry_count=int(row["retry_count"]),
        )

    def increment_retry(self, dedupe_key: str) -> int:
        """Erhöht den Retry-Zähler und gibt den neuen Wert zurück (0, wenn unbekannt)."""
        with self._conn:
            self._conn.execute(
                "UPDATE seen_mails SET retry_count = retry_count + 1 WHERE message_id_hash = ?",
                (dedupe_hash(dedupe_key),),
            )
        record = self.get(dedupe_key)
        return 0 if record is None else record.retry_count

    def count_by_status(self, status: MailState) -> int:
        """Anzahl der Mails in einem Status (für Betriebs-/Testauswertung)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM seen_mails WHERE status = ?", (status.value,)
        ).fetchone()
        return int(row["n"])

    # --- meta -----------------------------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        """Liest einen Wert aus der `meta`-Tabelle."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        """Setzt einen Wert in der `meta`-Tabelle (Upsert)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
