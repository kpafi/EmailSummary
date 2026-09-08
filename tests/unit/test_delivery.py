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
            "<a@x>", kind="mail", parts=["x"], importance="normal", is_warning=False, now=clock()
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


# --- CT-13: Fortsetzung mitten in einer mehrteiligen Nachricht --------------------------------


class PartCountingMessenger:
    """Messenger-Attrappe, die nach `die_after` Teilen abbricht (Sink-Modus `die_after_1`)."""

    def __init__(self, die_after: int | None = None) -> None:
        self.parts: list[str] = []
        self._die_after = die_after

    def send(self, message: DigestMessage) -> None:
        for part in message.parts:
            if self._die_after is not None and len(self.parts) >= self._die_after:
                raise MessengerError("Verbindung abgebrochen (Attrappe)")
            self.parts.append(part)

    def heal(self) -> None:
        self._die_after = None


def test_ct13_retry_setzt_bei_dem_abgebrochenen_teil_fort() -> None:
    """CT-13: Bereits bestätigte Teile werden beim Retry nicht erneut zugestellt.

    Ohne den Fix sieht der Nutzer für **eine** Mail die Folge 1, 2, 1, 2, 3.
    """
    clock = Clock()
    parts = ["Teil 1", "Teil 2", "Teil 3"]
    with StateDB(":memory:") as db:
        adapter = PartCountingMessenger(die_after=2)
        outbox = OutboxMessenger(db, adapter, now=clock)
        outbox.send(
            DigestMessage(
                parts=parts, importance="normal", is_warning=False, dedupe_key="<a@x>"
            )
        )
        assert adapter.parts == ["Teil 1", "Teil 2"]
        assert outbox.pending == 1

        adapter.heal()
        clock.advance(DELIVERY_BACKOFF_SECONDS[0])
        stats = outbox.flush()

        assert stats.delivered == 1
        assert adapter.parts == ["Teil 1", "Teil 2", "Teil 3"]
        assert outbox.pending == 0


def test_ct13_gekuerzte_warteschlangenzeile_behaelt_wichtigkeit_und_warnflag() -> None:
    """Die Nutzlast wird nur eingekürzt — Metadaten und Text bleiben unverändert."""
    clock = Clock()
    with StateDB(":memory:") as db:
        outbox = OutboxMessenger(db, PartCountingMessenger(die_after=1), now=clock)
        outbox.send(
            DigestMessage(
                parts=["A", "B", "C"],
                importance="high",
                is_warning=True,
                dedupe_key="<a@x>",
            )
        )
        item = db.outbox_due(now=START + timedelta(hours=1))[0]
        assert item.parts == ["B", "C"]
        assert item.importance == "high"
        assert item.is_warning is True


def test_ct13_fehlschlag_beim_ersten_teil_laesst_die_nachricht_vollstaendig() -> None:
    """Ohne bestätigten Teil bleibt die Zeile unangetastet (kein stiller Teilverlust)."""
    clock = Clock()
    with StateDB(":memory:") as db:
        outbox = OutboxMessenger(db, PartCountingMessenger(die_after=0), now=clock)
        outbox.send(
            DigestMessage(
                parts=["A", "B"], importance="normal", is_warning=False, dedupe_key="<a@x>"
            )
        )
        item = db.outbox_due(now=START + timedelta(hours=1))[0]
        assert item.parts == ["A", "B"]
