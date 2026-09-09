"""Unit-Tests der WP8-Erweiterungen von `state/db.py`.

Prüffläche: Sammel-Digest-Warteschlange (F-SUM-5), Zustell-Warteschlange (`outbox`,
ADR-048), der additive Schema-Upgrade 1 → 2 (NF-3) und die eng gefasste
Statusbeförderung `checked → delivered|failed` (ADR-008).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maildigest.state.db import SCHEMA_VERSION, MailState, StateDB, StateError, dedupe_hash


def test_low_digest_queue_roundtrip() -> None:
    """Einreihen, lesen, leeren — in Eingangsreihenfolge."""
    with StateDB(":memory:") as db:
        db.queue_low("<a@x>", headline="Newsletter", category="newsletter", from_domain="x[.]de")
        db.queue_low("<b@x>", headline="Hinweis", category="system", from_domain="y[.]de")
        entries = db.low_digest_entries()
        assert [entry.headline for entry in entries] == ["Newsletter", "Hinweis"]
        assert entries[0].message_id_hash == dedupe_hash("<a@x>")
        assert entries[0].category == "newsletter"
        db.clear_low_digest([entry.id for entry in entries])
        assert db.low_digest_entries() == []


def test_clear_low_digest_without_ids_is_a_noop() -> None:
    """Ein leerer Aufruf löscht nichts (defensiv gegen `DELETE ohne WHERE`)."""
    with StateDB(":memory:") as db:
        db.queue_low("<a@x>", headline="H", category="c", from_domain="d")
        db.clear_low_digest([])
        assert len(db.low_digest_entries()) == 1


def test_low_digest_queue_stores_no_dedupe_key_in_clear_text() -> None:
    """In der Tabelle steht nur der Hash des Dedupe-Keys (NF-5)."""
    with StateDB(":memory:") as db:
        db.queue_low("<geheim@example.org>", headline="H", category="c", from_domain="d")
        row = db.low_digest_entries()[0]
        assert "geheim" not in row.message_id_hash


def test_outbox_enqueue_and_due() -> None:
    """Eingereihte Nachrichten sind sofort fällig und kommen unverändert zurück."""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    with StateDB(":memory:") as db:
        db.enqueue_outbox(
            "<a@x>", kind="mail", parts=["Teil 1", "Teil 2"], importance="high",
            is_warning=True, now=now,
        )
        items = db.outbox_due(now=now)
        assert len(items) == 1
        item = items[0]
        assert item.parts == ["Teil 1", "Teil 2"]
        assert item.importance == "high"
        assert item.is_warning is True
        assert item.attempts == 0
        assert db.outbox_pending("<a@x>") is True
        assert db.outbox_size() == 1


def test_outbox_defer_moves_the_next_attempt_into_the_future() -> None:
    """Ein Fehlversuch erhöht den Zähler und verschiebt die Fälligkeit."""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    with StateDB(":memory:") as db:
        item_id = db.enqueue_outbox(
            "<a@x>", kind="mail", parts=["x"], importance="normal", is_warning=False, now=now
        )
        db.outbox_defer(item_id, next_attempt_at=now + timedelta(minutes=5), error_class="Boom")
        assert db.outbox_due(now=now) == []
        later = db.outbox_due(now=now + timedelta(minutes=6))
        assert len(later) == 1
        assert later[0].attempts == 1


def test_outbox_done_removes_the_row() -> None:
    """Eine zugestellte Nachricht verschwindet aus der Warteschlange."""
    with StateDB(":memory:") as db:
        item_id = db.enqueue_outbox(
            "<a@x>", kind="mail", parts=["x"], importance="normal", is_warning=False
        )
        db.outbox_done(item_id)
        assert db.outbox_size() == 0
        assert db.outbox_pending("<a@x>") is False


def test_outbox_survives_a_corrupt_payload() -> None:
    """Eine kaputte Nutzlast liefert eine leere Teileliste statt einer Exception."""
    with StateDB(":memory:") as db:
        item_id = db.enqueue_outbox(
            "<a@x>", kind="mail", parts=["x"], importance="normal", is_warning=False
        )
        db._conn.execute("UPDATE outbox SET payload = ? WHERE id = ?", ("{kaputt", item_id))
        assert db.outbox_due()[0].parts == []


def test_promote_checked_to_delivered_only_touches_checked() -> None:
    """Die Warteschlange darf keine Statusübergänge erfinden (ADR-048)."""
    with StateDB(":memory:") as db:
        db.claim("<checked@x>")
        db.mark_status("<checked@x>", MailState.CHECKED)
        db.claim("<failed@x>")
        db.mark_status("<failed@x>", MailState.FAILED, error_class="sanitize_error")

        db.promote_checked_to_delivered(dedupe_hash("<checked@x>"), delivered=True)
        db.promote_checked_to_delivered(dedupe_hash("<failed@x>"), delivered=True)

        checked = db.get("<checked@x>")
        failed = db.get("<failed@x>")
        assert checked is not None and checked.status is MailState.DELIVERED
        assert failed is not None and failed.status is MailState.FAILED


def test_promote_checked_to_failed_sets_the_delivery_error_class() -> None:
    """Endgültig gescheiterte Zustellung landet als `failed`/`delivery_failed`."""
    with StateDB(":memory:") as db:
        db.claim("<a@x>")
        db.mark_status("<a@x>", MailState.CHECKED)
        db.promote_checked_to_delivered(dedupe_hash("<a@x>"), delivered=False)
        record = db.get("<a@x>")
        assert record is not None
        assert record.status is MailState.FAILED
        assert record.error_class == "delivery_failed"


def test_schema_version_one_is_upgraded_additively(tmp_path: Path) -> None:
    """Eine WP2-Datenbank (Version 1) wird beim Öffnen ohne Datenverlust hochgezogen."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    with connection:
        connection.executescript(
            "CREATE TABLE seen_mails (message_id_hash TEXT PRIMARY KEY, first_seen_at TEXT "
            "NOT NULL, status TEXT NOT NULL, error_class TEXT, retry_count INTEGER NOT NULL "
            "DEFAULT 0);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta (key, value) VALUES ('schema_version', '1');"
            "INSERT INTO seen_mails VALUES ('abc', '2026-09-01T10:00:00+00:00', "
            "'delivered', NULL, 0);"
        )
    connection.close()

    with StateDB(path) as db:
        assert db.meta_get("schema_version") == str(SCHEMA_VERSION)
        assert db.count_by_status(MailState.DELIVERED) == 1
        db.queue_low("<a@x>", headline="H", category="c", from_domain="d")
        assert len(db.low_digest_entries()) == 1


def test_unknown_schema_version_is_still_refused(tmp_path: Path) -> None:
    """Eine unbekannte (neuere) Version bleibt ein Fehler — kein Raten (NF-3)."""
    path = tmp_path / "state.db"
    with StateDB(path) as db:
        db.meta_set("schema_version", "99")
    with pytest.raises(StateError, match="schema version"):
        StateDB(path)
