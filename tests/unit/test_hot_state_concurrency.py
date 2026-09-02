"""Nebenläufigkeit und Locking der State-Datenbank — Hot-Testing WP10 (PLAN.md WP10).

Der Betrieb ist als Single-Instance ausgelegt (`state/db.py`, ADR-005), aber genau das ist
in der Praxis nicht garantiert: Ein Cron-Eintrag mit `run --once` kann sich mit einem noch
laufenden Daemon überschneiden, und `maildigest test` öffnet ohnehin eine zweite Verbindung
(auf einer eigenen DB, ADR-057). Getestet wird deshalb die Zusage, die wirklich zählt:

**F-ING-2 (Idempotenz) hält auch dann, wenn mehrere Prozesse/Threads dieselbe Datei
benutzen** — `claim()` gewinnt genau einmal, egal wie viele Aufrufer es gleichzeitig
versuchen. Zusätzlich: Der Wiederanlauf verliert nichts, und ein Schreib-Lock führt zu
einem sauberen `StateError` statt zu einem rohen `sqlite3`-Fehler (HT-3).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maildigest.state.db import (
    SCHEMA_VERSION,
    MailState,
    StateDB,
    StateError,
    dedupe_hash,
)

# --- Idempotenz unter Nebenläufigkeit ---------------------------------------------------


def test_two_connections_claim_the_same_mail_only_once(tmp_path: Path) -> None:
    """Zwei offene Verbindungen auf derselben Datei: genau ein `claim` gewinnt (F-ING-2)."""
    path = tmp_path / "state.db"
    with StateDB(path) as first, StateDB(path) as second:
        assert first.claim("<doppelt@example>") is True
        assert second.claim("<doppelt@example>") is False
        assert second.was_seen("<doppelt@example>") is True


def test_many_threads_claim_the_same_mail_only_once(tmp_path: Path) -> None:
    """12 Threads mit je eigener Verbindung — genau einer darf verarbeiten."""
    path = tmp_path / "state.db"
    StateDB(path).close()  # Schema einmal anlegen, dann gleichzeitig zugreifen
    results: list[bool] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(12)

    def worker() -> None:
        try:
            with StateDB(path) as database:
                barrier.wait(timeout=10)
                results.append(database.claim("<wettlauf@example>"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == [], f"Nebenläufigkeitsfehler: {errors!r}"
    assert results.count(True) == 1, f"genau ein Gewinner erwartet, war: {results!r}"
    assert len(results) == 12


def test_parallel_writers_do_not_lose_records(tmp_path: Path) -> None:
    """Acht Schreiber-Threads: kein „database is locked", kein verlorener Datensatz.

    Deckt die WAL-/Timeout-Einstellungen aus `StateDB.__init__` ab (10 s busy timeout).
    """
    path = tmp_path / "state.db"
    StateDB(path).close()
    errors: list[BaseException] = []

    def writer(worker_id: int) -> None:
        try:
            with StateDB(path) as database:
                for index in range(30):
                    key = f"<w{worker_id}-{index}@example>"
                    database.claim(key)
                    database.mark_status(key, MailState.DELIVERED)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], f"Schreibfehler unter Last: {errors!r}"
    with StateDB(path) as database:
        assert database.count_by_status(MailState.DELIVERED) == 8 * 30


def test_two_processes_claim_the_same_mail_only_once(tmp_path: Path) -> None:
    """Der echte Fall: zwei **Prozesse** (Daemon + Cron) auf derselben Datei.

    Threads teilen sich den Prozess-Zustand; erst zwei Interpreter zeigen, dass die
    Idempotenz wirklich am Primärschlüssel in der Datei hängt und nicht an einem Lock im
    Speicher.
    """
    path = tmp_path / "state.db"
    StateDB(path).close()
    script = (
        "import sys\n"
        "from maildigest.state.db import StateDB\n"
        "with StateDB(sys.argv[1]) as db:\n"
        "    print('1' if db.claim('<prozess@example>') else '0')\n"
    )
    outcomes = [
        subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        for _ in range(2)
    ]
    assert outcomes.count("1") == 1, f"genau ein Prozess darf gewinnen: {outcomes!r}"


def test_second_connection_sees_committed_progress(tmp_path: Path) -> None:
    """Zwischenstände sind committet, sobald sie geschrieben sind (ADR-008)."""
    path = tmp_path / "state.db"
    with StateDB(path) as writer, StateDB(path) as reader:
        writer.claim("<sichtbar@example>")
        writer.mark_status("<sichtbar@example>", MailState.CHECKED)
        record = reader.get("<sichtbar@example>")
        assert record is not None
        assert record.status is MailState.CHECKED


# --- Wiederanlauf und Schema -----------------------------------------------------------


def test_restart_keeps_outbox_and_low_queue(tmp_path: Path) -> None:
    """Nach einem Neustart liegen Warteschlangen unverändert vor (F-OPS-3, F-SUM-5)."""
    path = tmp_path / "state.db"
    with StateDB(path) as database:
        database.claim("<neustart@example>")
        database.mark_status("<neustart@example>", MailState.CHECKED)
        database.enqueue_outbox(
            "<neustart@example>",
            kind="mail",
            parts=["Teil eins", "Teil zwei"],
            importance="high",
            is_warning=True,
        )
        database.queue_low(
            "<leise@example>", headline="Newsletter", category="newsletter",
            from_domain="x[.]example",
        )

    with StateDB(path) as database:
        items = database.outbox_due(now=datetime.now(UTC))
        assert len(items) == 1
        assert items[0].parts == ["Teil eins", "Teil zwei"]
        assert items[0].importance == "high"
        assert items[0].is_warning is True
        assert items[0].message_id_hash == dedupe_hash("<neustart@example>")
        assert len(database.low_digest_entries()) == 1


def test_schema_version_one_is_upgraded_in_place(tmp_path: Path) -> None:
    """Additives Schema-Upgrade 1 → 2 ohne Migrationswerkzeug (NF-3, ADR-048)."""
    path = tmp_path / "state.db"
    with StateDB(path) as database:
        database.meta_set("schema_version", "1")
        database.claim("<alt@example>")

    with StateDB(path) as database:
        assert database.meta_get("schema_version") == str(SCHEMA_VERSION)
        assert database.was_seen("<alt@example>") is True


def test_unknown_schema_version_refuses_to_open(tmp_path: Path) -> None:
    """Eine fremde Zukunftsversion wird nicht geraten, sondern abgelehnt (fail-closed)."""
    path = tmp_path / "state.db"
    with StateDB(path) as database:
        database.meta_set("schema_version", "99")
    with pytest.raises(StateError, match="Schema-Version"):
        StateDB(path)


def test_foreign_file_is_refused(tmp_path: Path) -> None:
    """Eine Datei, die keine SQLite-DB ist, erzeugt `StateError` statt eines Absturzes."""
    path = tmp_path / "keine.db"
    path.write_bytes(b"das hier ist definitiv keine Datenbank")
    with pytest.raises(StateError):
        StateDB(path)


def test_directory_without_write_permission_is_refused(tmp_path: Path) -> None:
    """Fehlende Schreibrechte im Zielverzeichnis melden `StateError` (CLI zeigt Meldung)."""
    locked = tmp_path / "gesperrt"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(StateError):
            StateDB(locked / "state.db")
    finally:
        locked.chmod(0o700)


def test_database_file_is_created_private(tmp_path: Path) -> None:
    """Die neu angelegte Datei gehört nur dem Nutzer (SECURITY §6)."""
    path = tmp_path / "state.db"
    StateDB(path).close()
    assert path.stat().st_mode & 0o077 == 0


# --- Locking gegen einen fremden Schreiber ----------------------------------------------


def test_write_lock_of_another_connection_surfaces_as_state_error(tmp_path: Path) -> None:
    """Eine fremde, offene Schreib-Transaktion blockiert — sauber als `StateError` (HT-3).

    Das Timeout wird im Test auf einen Sekundenbruchteil gesetzt; im Betrieb sind es 10 s
    (`StateDB.__init__`), was für den Single-Instance-Fall reichlich ist.
    """
    path = tmp_path / "state.db"
    StateDB(path).close()

    blocker = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        database = StateDB(path)
        database._conn.execute("PRAGMA busy_timeout = 100")
        with pytest.raises(StateError, match="nicht benutzbar"):
            database.claim("<blockiert@example>")
        database.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_retry_counter_is_monotonic_under_repeated_use(tmp_path: Path) -> None:
    """Der Retry-Zähler zählt hoch statt zurückzuspringen (Grundlage der Retry-Politik)."""
    with StateDB(tmp_path / "state.db") as database:
        database.claim("<zaehler@example>")
        values = [database.increment_retry("<zaehler@example>") for _ in range(5)]
        assert values == [1, 2, 3, 4, 5]
        assert database.increment_retry("<unbekannt@example>") == 0


def test_promote_only_touches_checked_records(tmp_path: Path) -> None:
    """Die Zustell-Warteschlange erfindet keinen Statusübergang (ADR-048)."""
    with StateDB(tmp_path / "state.db") as database:
        for key, state in [
            ("<a@example>", MailState.CHECKED),
            ("<b@example>", MailState.FAILED),
            ("<c@example>", MailState.DELIVERED),
        ]:
            database.claim(key)
            database.mark_status(key, state)
            database.promote_checked_to_delivered(dedupe_hash(key), delivered=True)

        assert database.get("<a@example>").status is MailState.DELIVERED  # type: ignore[union-attr]
        assert database.get("<b@example>").status is MailState.FAILED  # type: ignore[union-attr]
        assert database.get("<c@example>").status is MailState.DELIVERED  # type: ignore[union-attr]


def test_outbox_due_boundary_is_inclusive(tmp_path: Path) -> None:
    """Ein Eintrag ist **genau** zu seinem `next_attempt_at` fällig, nicht erst danach."""
    moment = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    with StateDB(tmp_path / "state.db") as database:
        database.enqueue_outbox(
            "<faellig@example>", kind="mail", parts=["x"], importance="normal",
            is_warning=False, now=moment,
        )
        assert database.outbox_due(now=moment - timedelta(microseconds=1)) == []
        assert len(database.outbox_due(now=moment)) == 1
