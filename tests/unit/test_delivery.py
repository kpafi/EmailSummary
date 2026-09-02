"""Unit-Tests der Zustell-Warteschlange (`delivery.py`, WP8).

Prüffläche: at-least-once nach ADR-008/ADR-048 — Commit vor Versand, Wiederholung mit
Backoff, Aufgabe nach 5 Versuchen bzw. einer Stunde, unveränderter Nachrichtentext (I3/I4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maildigest.delivery import (
    DELIVERY_BACKOFF_SECONDS,
    DELIVERY_MAX_ATTEMPTS,
    OutboxMessenger,
    next_delivery_delay,
)
from maildigest.messenger.base import MessengerError
from maildigest.models import DigestMessage
from maildigest.output.composer import LOW_DIGEST_DEDUPE_KEY
from maildigest.state.db import MailState, StateDB, dedupe_hash

START = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class Clock:
    """Steuerbare Uhr für die Backoff-Fälligkeiten."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class RecordingMessenger:
    """Messenger-Attrappe: merkt sich Nachrichten, kann gezielt scheitern."""

    def __init__(self, failures: int = 0) -> None:
        self.sent: list[list[str]] = []
        self.failures = failures

    def send(self, message: DigestMessage) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise MessengerError("Zustellung fehlgeschlagen (Attrappe)")
        self.sent.append(list(message.parts))


def message(text: str = "Hallo", key: str = "<a@x>") -> DigestMessage:
    return DigestMessage(parts=[text], importance="normal", is_warning=False, dedupe_key=key)


def test_successful_send_leaves_no_queue_entry() -> None:
    """Happy Path: einreihen, senden, Zeile weg."""
    with StateDB(":memory:") as db:
        adapter = RecordingMessenger()
        OutboxMessenger(db, adapter, now=Clock()).send(message())
        assert adapter.sent == [["Hallo"]]
        assert db.outbox_size() == 0


def test_failed_send_keeps_the_message_in_the_queue() -> None:
    """Ein Fehlversuch wirft nicht, sondern hinterlässt die Nachricht zum Wiederholen."""
    clock = Clock()
    with StateDB(":memory:") as db:
        adapter = RecordingMessenger(failures=1)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(message())  # wirft nicht
        assert adapter.sent == []
        assert outbox.pending == 1

        clock.advance(DELIVERY_BACKOFF_SECONDS[0])
        stats = outbox.flush()
        assert stats.delivered == 1
        assert adapter.sent == [["Hallo"]]
        assert outbox.pending == 0


def test_queue_entry_is_not_due_before_the_backoff_has_passed() -> None:
    """Vor Ablauf des Backoffs wird nichts erneut versucht."""
    clock = Clock()
    with StateDB(":memory:") as db:
        adapter = RecordingMessenger(failures=1)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(message())
        assert outbox.flush().delivered == 0
        assert adapter.sent == []


def test_five_attempts_then_abandon() -> None:
    """Nach `DELIVERY_MAX_ATTEMPTS` Versuchen wird aufgegeben und `failed` gebucht."""
    clock = Clock()
    with StateDB(":memory:") as db:
        db.claim("<a@x>")
        db.mark_status("<a@x>", MailState.CHECKED)
        adapter = RecordingMessenger(failures=99)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(message())
        for attempt in range(1, DELIVERY_MAX_ATTEMPTS):
            clock.advance(next_delivery_delay(attempt))
            outbox.flush()
        assert outbox.pending == 0
        record = db.get("<a@x>")
        assert record is not None
        assert record.status is MailState.FAILED
        assert record.error_class == "delivery_failed"


def test_deadline_ends_the_retries_even_with_attempts_left() -> None:
    """Auch mit Restversuchen ist nach einer Stunde Schluss (ARCHITECTURE §6)."""
    clock = Clock()
    with StateDB(":memory:") as db:
        adapter = RecordingMessenger(failures=99)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(message())
        clock.advance(3600)
        stats = outbox.flush()
        assert stats.abandoned == 1
        assert outbox.pending == 0


def test_successful_flush_promotes_checked_to_delivered() -> None:
    """Erst die bestätigte Zustellung macht aus `checked` ein `delivered` (ADR-008)."""
    clock = Clock()
    with StateDB(":memory:") as db:
        db.claim("<a@x>")
        db.mark_status("<a@x>", MailState.CHECKED)
        adapter = RecordingMessenger(failures=1)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(message())
        record = db.get("<a@x>")
        assert record is not None and record.status is MailState.CHECKED

        clock.advance(DELIVERY_BACKOFF_SECONDS[0])
        outbox.flush()
        record = db.get("<a@x>")
        assert record is not None and record.status is MailState.DELIVERED


def test_low_digest_never_touches_a_mail_status() -> None:
    """Der Sammel-Digest gehört zu keiner Mail — sein Hash darf keinen Status bewegen."""
    clock = Clock()
    with StateDB(":memory:") as db:
        db.claim(LOW_DIGEST_DEDUPE_KEY)
        db.mark_status(LOW_DIGEST_DEDUPE_KEY, MailState.CHECKED)
        adapter = RecordingMessenger(failures=1)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(message(key=LOW_DIGEST_DEDUPE_KEY))
        clock.advance(DELIVERY_BACKOFF_SECONDS[0])
        outbox.flush()
        record = db.get(LOW_DIGEST_DEDUPE_KEY)
        assert record is not None and record.status is MailState.CHECKED


def test_restart_delivers_a_committed_message_again() -> None:
    """Wiederanlauf: Was in der Warteschlange liegt, wird zugestellt (nichts geht verloren)."""
    clock = Clock()
    with StateDB(":memory:") as db:
        # Zustand nach einem Absturz zwischen Commit und Versand.
        db.enqueue_outbox(
            "<a@x>",
            kind="mail",
            parts=["Bereits sanitisierter Text"],
            importance="normal",
            is_warning=False,
            now=clock(),
        )
        adapter = RecordingMessenger()
        stats = OutboxMessenger(db, adapter, now=clock).flush()
        assert stats.delivered == 1
        assert adapter.sent == [["Bereits sanitisierter Text"]]


def test_unusable_payload_is_dropped_and_booked_as_failed() -> None:
    """Eine unlesbare Nutzlast wird verworfen — es wird nichts geraten."""
    clock = Clock()
    with StateDB(":memory:") as db:
        db.claim("<a@x>")
        db.mark_status("<a@x>", MailState.CHECKED)
        item_id = db.enqueue_outbox(
            "<a@x>", kind="mail", parts=["x"], importance="normal", is_warning=False
        )
        db._conn.execute("UPDATE outbox SET payload = ? WHERE id = ?", ("[]", item_id))
        adapter = RecordingMessenger()
        stats = OutboxMessenger(db, adapter, now=clock).flush()
        assert stats.abandoned == 1
        assert adapter.sent == []
        record = db.get("<a@x>")
        assert record is not None and record.status is MailState.FAILED


def test_message_text_is_never_modified_by_the_queue() -> None:
    """Der Text kommt Zeichen für Zeichen so an, wie er eingereiht wurde (I3/I4)."""
    clock = Clock()
    text = "⚠️ PHISHING-VERDACHT: Absender-Domain weicht ab\n📧 Rechnung stadtwerke-x[.]de"
    with StateDB(":memory:") as db:
        adapter = RecordingMessenger(failures=1)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(DigestMessage(parts=[text], importance="high", is_warning=True,
                                  dedupe_key="<a@x>"))
        clock.advance(DELIVERY_BACKOFF_SECONDS[0])
        outbox.flush()
        assert adapter.sent == [[text]]


def test_outbox_stores_only_the_hash_of_the_dedupe_key() -> None:
    """NF-5: Der Klartext-Key steht auch in der Zustell-Warteschlange nicht."""
    with StateDB(":memory:") as db:
        adapter = RecordingMessenger(failures=1)
        OutboxMessenger(db, adapter, now=Clock()).send(message(key="<geheim@example.org>"))
        item = db.outbox_due(now=START + timedelta(hours=1))[0]
        assert item.message_id_hash == dedupe_hash("<geheim@example.org>")
        assert "geheim" not in item.message_id_hash


def test_next_delivery_delay_is_capped() -> None:
    """Der Backoff wächst monoton und bleibt bei der letzten Stufe stehen."""
    delays = [next_delivery_delay(attempt) for attempt in range(1, 8)]
    assert delays[: len(DELIVERY_BACKOFF_SECONDS)] == list(DELIVERY_BACKOFF_SECONDS)
    assert delays[-1] == DELIVERY_BACKOFF_SECONDS[-1]
