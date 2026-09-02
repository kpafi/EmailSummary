"""SQLite-State: Tabellen ``seen_mails``, ``low_digest_queue``, ``outbox`` und ``meta``.

Datenmodell: docs/ARCHITECTURE.md §2 (State). WP2 hat ``seen_mails`` und ``meta`` angelegt;
WP8 ergänzt die Sammel-Digest-Warteschlange (F-SUM-5) und die Zustell-Warteschlange
(``outbox``, at-least-once nach ADR-008/ADR-048).

Sicherheits-Design dieses Moduls:

* **Kein Mail-Volltext, keine PII** (docs/SECURITY.md §6, NF-5): In ``seen_mails`` steht
  ausschließlich der SHA-256-Hash des Dedupe-Keys, ein Zeitstempel, der Status, eine grobe
  Fehlerklasse und ein Retry-Zähler. Weder Betreff noch Absender noch Message-ID im Klartext.
* **Zwei bewusste, in docs/SECURITY.md §6 benannte Ausnahmen:** ``low_digest_queue`` und
  ``outbox`` enthalten bereits **fertig sanitisierten** Ausgabetext (Kritiker-geprüft und
  durch den Output-Sanitizer gelaufen) — nie Mail-Rohtext, nie Links, nie Anhänge. Ohne
  diese Persistenz ließe sich weder der tägliche Sammel-Digest noch die
  „nie stiller Verlust"-Zusage aus F-OPS-3 über einen Prozessneustart hinweg halten.
  Beide Tabellen werden nach Zustellung geleert.
* **I5:** Secrets landen nie in der DB. Die Fehlerklasse wird zusätzlich hart auf
  ``[a-z0-9_]`` und 64 Zeichen normalisiert, damit ein durchgereichter Fehlertext (der
  Inhalte enthalten könnte) hier strukturell nicht ankommen kann.
* Die Datenbankdatei wird mit Modus ``0600`` angelegt.

Idempotenz (F-ING-2) beruht auf :meth:`StateDB.claim`: ``INSERT OR IGNORE`` auf den
Primärschlüssel ``message_id_hash`` — genau der erste Aufruf gewinnt, jeder weitere meldet
"schon gesehen".
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, ParamSpec, TypeVar

__all__ = [
    "SCHEMA_VERSION",
    "LowDigestEntry",
    "MailState",
    "OutboxItem",
    "SeenMail",
    "StateDB",
    "StateError",
    "dedupe_hash",
]

#: Version des DB-Schemas; steht in `meta` und wird beim Öffnen geprüft.
#: 1 = WP2 (`seen_mails`, `meta`), 2 = WP8 (zusätzlich `low_digest_queue`, `outbox`).
SCHEMA_VERSION: Final = 2

#: Schema-Versionen, die rein additiv (nur neue Tabellen) auf SCHEMA_VERSION gehoben
#: werden können — ohne Migrationswerkzeug (NF-3, ADR-048).
_UPGRADABLE_FROM: Final = frozenset({"1"})

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

CREATE TABLE IF NOT EXISTS low_digest_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id_hash TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    headline        TEXT NOT NULL,
    category        TEXT NOT NULL,
    from_domain     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id_hash TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    first_queued_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_seen_mails_status ON seen_mails(status);
CREATE INDEX IF NOT EXISTS idx_outbox_next ON outbox(next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_mail ON outbox(message_id_hash);
"""


class StateError(Exception):
    """Die State-Datenbank ist nicht benutzbar (I/O-Fehler, fremdes Schema, Schema-Version).

    Gilt für den gesamten Lebenszyklus, nicht nur fürs Öffnen: Jede öffentliche Methode von
    :class:`StateDB` verpackt `sqlite3.Error` in diesen Typ (HT-3). Ein schreibgeschütztes
    oder volles Dateisystem erreicht die Pipeline dadurch als dokumentierter Fehler mit der
    Fehlerklasse ``state_error`` (``pipeline._ERROR_CLASSES``) statt als roher
    `sqlite3.OperationalError`, den die CLI nur noch als Traceback zeigen könnte.
    """


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _wrap_sqlite_errors(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Verpackt `sqlite3.Error` einer Methode in :class:`StateError`.

    Die Meldung enthält nur den Methodennamen und den SQLite-Text (Dinge wie „attempt to
    write a readonly database") — nie Mail-Inhalt, nie Secrets (I5).
    """

    @functools.wraps(func)
    def inner(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except sqlite3.Error as exc:
            raise StateError(
                f"State-Datenbank nicht benutzbar ({func.__name__}): {exc}"
            ) from exc

    return inner


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


@dataclass(frozen=True)
class LowDigestEntry:
    """Eine Zeile der Sammel-Digest-Warteschlange (F-SUM-5).

    `headline` ist bereits durch Summarizer-Nachkontrolle **und** Output-Sanitizer gelaufen
    (ADR-049) — kein Mail-Rohtext, keine Links.
    """

    id: int
    message_id_hash: str
    received_at: datetime
    headline: str
    category: str
    from_domain: str


@dataclass(frozen=True)
class OutboxItem:
    """Eine wartende Zustellung (ADR-048).

    `parts` ist der fertig sanitisierte Nachrichtentext aus
    :meth:`maildigest.output.composer.DigestComposer._finalize` — er wird beim erneuten
    Versand unverändert übernommen (I3/I4).
    """

    id: int
    message_id_hash: str
    kind: str
    parts: list[str]
    importance: str
    is_warning: bool
    attempts: int
    first_queued_at: datetime
    next_attempt_at: datetime


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


def _clean_label(value: str) -> str:
    """Normalisiert eine Kennung (`kind`) auf ein kurzes, inhaltsfreies Label (I5)."""
    return _clean_error_class(value) or "unbekannt"


def _decode_payload(raw: str) -> tuple[list[str], str, bool]:
    """Liest eine Outbox-Nutzlast; defekte Einträge werden zu einer leeren Nachricht.

    Ein unlesbarer Eintrag darf den Zustell-Lauf nicht sprengen: Er wird als leere
    Teileliste zurückgegeben und vom Aufrufer verworfen (nichts wird geraten).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return [], "normal", False
    if not isinstance(data, dict):
        return [], "normal", False
    parts = data.get("parts")
    if not isinstance(parts, list) or not all(isinstance(part, str) for part in parts):
        return [], "normal", False
    importance = data.get("importance")
    return (
        parts,
        importance if importance in {"high", "normal", "low"} else "normal",
        bool(data.get("is_warning", False)),
    )


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
        """Legt die Schema-Version an oder prüft sie (NF-3: keine Migrationstools).

        Rein additive Vorgängerversionen (nur neue Tabellen, keine geänderte Spalte —
        derzeit Version 1 → 2, ADR-048) werden beim Öffnen stillschweigend hochgesetzt:
        Die Tabellen sind durch ``CREATE TABLE IF NOT EXISTS`` bereits angelegt, Daten
        müssen nicht angefasst werden. Alles andere ist ein Fehler.
        """
        stored = self.meta_get(_META_SCHEMA_VERSION)
        if stored is None:
            self.meta_set(_META_SCHEMA_VERSION, str(SCHEMA_VERSION))
            return
        if stored in _UPGRADABLE_FROM:
            self.meta_set(_META_SCHEMA_VERSION, str(SCHEMA_VERSION))
            return
        if stored != str(SCHEMA_VERSION):
            raise StateError(
                f"State-Datenbank hat Schema-Version {stored}, erwartet wird "
                f"{SCHEMA_VERSION}. Datei entfernen oder Version prüfen."
            )

    # --- seen_mails -----------------------------------------------------------------------

    @_wrap_sqlite_errors
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

    @_wrap_sqlite_errors
    def was_seen(self, dedupe_key: str) -> bool:
        """True, wenn zu diesem Dedupe-Key bereits ein Datensatz existiert."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_mails WHERE message_id_hash = ?", (dedupe_hash(dedupe_key),)
        ).fetchone()
        return row is not None

    @_wrap_sqlite_errors
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

    @_wrap_sqlite_errors
    def promote_checked_to_delivered(self, message_id_hash: str, *, delivered: bool) -> None:
        """Schließt eine Mail ab, deren Zustellung aus der Warteschlange kam (ADR-048).

        Wirkt **nur** auf Datensätze im Status ``checked`` — genau die Mails, deren Inhalt
        laut ADR-008 versendet werden durfte, aber noch nicht bestätigt war. Ein `failed`
        (Fail-closed-Notiz) oder `delivered` wird nie überschrieben; damit kann die
        Zustell-Warteschlange keinen Statusübergang erfinden, den die Pipeline nicht
        vorgesehen hat.

        Args:
            message_id_hash: Hash aus :class:`OutboxItem` (der Klartext-Key ist nicht
                gespeichert).
            delivered: ``True`` ⇒ `delivered`; ``False`` ⇒ `failed` mit
                `error_class = "delivery_failed"` (endgültig aufgegeben).
        """
        status = MailState.DELIVERED if delivered else MailState.FAILED
        error_class = None if delivered else "delivery_failed"
        with self._conn:
            self._conn.execute(
                "UPDATE seen_mails SET status = ?, error_class = ? "
                "WHERE message_id_hash = ? AND status = ?",
                (status.value, error_class, message_id_hash, MailState.CHECKED.value),
            )

    @_wrap_sqlite_errors
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

    @_wrap_sqlite_errors
    def increment_retry(self, dedupe_key: str) -> int:
        """Erhöht den Retry-Zähler und gibt den neuen Wert zurück (0, wenn unbekannt)."""
        with self._conn:
            self._conn.execute(
                "UPDATE seen_mails SET retry_count = retry_count + 1 WHERE message_id_hash = ?",
                (dedupe_hash(dedupe_key),),
            )
        record = self.get(dedupe_key)
        return 0 if record is None else record.retry_count

    @_wrap_sqlite_errors
    def count_by_status(self, status: MailState) -> int:
        """Anzahl der Mails in einem Status (für Betriebs-/Testauswertung)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM seen_mails WHERE status = ?", (status.value,)
        ).fetchone()
        return int(row["n"])

    # --- low_digest_queue -------------------------------------------------------------------

    @_wrap_sqlite_errors
    def queue_low(
        self,
        dedupe_key: str,
        *,
        headline: str,
        category: str,
        from_domain: str,
        now: datetime | None = None,
    ) -> int:
        """Stellt eine `low`-Mail in die Sammel-Digest-Warteschlange (F-SUM-5).

        Args:
            dedupe_key: Dedupe-Key der Mail (wird gehasht gespeichert).
            headline: Bereits output-sanitisierte Kopfzeile (ADR-049).
            category: Kategorie aus der Summary, ebenfalls sanitisiert.
            from_domain: Absender-Domain, sanitisiert/defangt.
            now: Zeitstempel (Default: jetzt, UTC).

        Returns:
            Die Zeilen-ID des Eintrags.
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO low_digest_queue "
                "(message_id_hash, received_at, headline, category, from_domain) "
                "VALUES (?, ?, ?, ?, ?)",
                (dedupe_hash(dedupe_key), timestamp, headline, category, from_domain),
            )
        return int(cursor.lastrowid or 0)

    @_wrap_sqlite_errors
    def low_digest_entries(self) -> list[LowDigestEntry]:
        """Alle wartenden Sammel-Digest-Einträge in Eingangsreihenfolge."""
        rows = self._conn.execute(
            "SELECT id, message_id_hash, received_at, headline, category, from_domain "
            "FROM low_digest_queue ORDER BY id"
        ).fetchall()
        return [
            LowDigestEntry(
                id=int(row["id"]),
                message_id_hash=str(row["message_id_hash"]),
                received_at=datetime.fromisoformat(str(row["received_at"])),
                headline=str(row["headline"]),
                category=str(row["category"]),
                from_domain=str(row["from_domain"]),
            )
            for row in rows
        ]

    @_wrap_sqlite_errors
    def clear_low_digest(self, ids: list[int]) -> None:
        """Entfernt zugestellte Sammel-Digest-Einträge (nach erfolgreicher Zustellung)."""
        if not ids:
            return
        with self._conn:
            self._conn.executemany(
                "DELETE FROM low_digest_queue WHERE id = ?", [(item,) for item in ids]
            )

    # --- outbox ------------------------------------------------------------------------------

    @_wrap_sqlite_errors
    def enqueue_outbox(
        self,
        dedupe_key: str,
        *,
        kind: str,
        parts: list[str],
        importance: str,
        is_warning: bool,
        now: datetime | None = None,
    ) -> int:
        """Legt eine fertig sanitisierte Nachricht in die Zustell-Warteschlange (ADR-048).

        Der Commit passiert **vor** dem Versandversuch — genau das macht die
        at-least-once-Zusage aus ADR-008 über einen Prozessabsturz hinweg haltbar.

        Returns:
            Die Zeilen-ID des Eintrags (für :meth:`outbox_done`/:meth:`outbox_defer`).
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        payload = json.dumps(
            {"parts": list(parts), "importance": importance, "is_warning": bool(is_warning)},
            ensure_ascii=False,
        )
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO outbox "
                "(message_id_hash, kind, payload, attempts, first_queued_at, next_attempt_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (dedupe_hash(dedupe_key), _clean_label(kind), payload, timestamp, timestamp),
            )
        return int(cursor.lastrowid or 0)

    @_wrap_sqlite_errors
    def outbox_due(self, *, now: datetime | None = None, limit: int = 50) -> list[OutboxItem]:
        """Alle fälligen Zustellungen (ältester Eintrag zuerst)."""
        timestamp = (now or datetime.now(UTC)).isoformat()
        rows = self._conn.execute(
            "SELECT id, message_id_hash, kind, payload, attempts, first_queued_at, "
            "next_attempt_at FROM outbox WHERE next_attempt_at <= ? ORDER BY id LIMIT ?",
            (timestamp, limit),
        ).fetchall()
        items: list[OutboxItem] = []
        for row in rows:
            payload = _decode_payload(str(row["payload"]))
            items.append(
                OutboxItem(
                    id=int(row["id"]),
                    message_id_hash=str(row["message_id_hash"]),
                    kind=str(row["kind"]),
                    parts=payload[0],
                    importance=payload[1],
                    is_warning=payload[2],
                    attempts=int(row["attempts"]),
                    first_queued_at=datetime.fromisoformat(str(row["first_queued_at"])),
                    next_attempt_at=datetime.fromisoformat(str(row["next_attempt_at"])),
                )
            )
        return items

    @_wrap_sqlite_errors
    def outbox_done(self, item_id: int) -> None:
        """Entfernt eine zugestellte (oder endgültig aufgegebene) Nachricht."""
        with self._conn:
            self._conn.execute("DELETE FROM outbox WHERE id = ?", (item_id,))

    @_wrap_sqlite_errors
    def outbox_defer(
        self, item_id: int, *, next_attempt_at: datetime, error_class: str | None = None
    ) -> None:
        """Zählt einen Fehlversuch und verschiebt den nächsten Versuch."""
        with self._conn:
            self._conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, "
                "last_error = ? WHERE id = ?",
                (next_attempt_at.isoformat(), _clean_error_class(error_class), item_id),
            )

    @_wrap_sqlite_errors
    def outbox_pending(self, dedupe_key: str) -> bool:
        """True, wenn zu dieser Mail noch eine Zustellung aussteht."""
        row = self._conn.execute(
            "SELECT 1 FROM outbox WHERE message_id_hash = ? LIMIT 1",
            (dedupe_hash(dedupe_key),),
        ).fetchone()
        return row is not None

    @_wrap_sqlite_errors
    def outbox_size(self) -> int:
        """Anzahl wartender Zustellungen (Betriebs-/Testauswertung)."""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
        return int(row["n"])

    # --- meta -----------------------------------------------------------------------------

    @_wrap_sqlite_errors
    def meta_get(self, key: str) -> str | None:
        """Liest einen Wert aus der `meta`-Tabelle."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @_wrap_sqlite_errors
    def meta_set(self, key: str, value: str) -> None:
        """Setzt einen Wert in der `meta`-Tabelle (Upsert)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
