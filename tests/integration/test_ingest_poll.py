"""Integrationstests für den Ingest-Durchlauf: IMAP (gemockt) + State-DB + Pipeline-Callback.

Der IMAP-Server wird auf imap-tools-Ebene durch einen `FakeMailBox` ersetzt (kein Docker,
kein echter Server — Begründung: WP2-ADR in docs/DECISIONS.md). State-DB und Poll-/Loop-Logik
laufen echt.

Schwerpunkte:
* **F-ING-2:** zweimal abrufen ⇒ einmal verarbeiten (Idempotenz, auch über einen simulierten
  Prozess-Neustart hinweg),
* **F-ING-1:** verarbeitete Mails werden gelesen markiert/verschoben, nie gelöscht,
* **I6:** eine kaputte Mail stoppt den Durchlauf nicht,
* **ARCHITECTURE §6:** Verbindungsabbruch ⇒ Reconnect mit Exponential Backoff.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from imap_tools import MailMessage, MailMessageFlags
from pydantic import SecretStr

from maildigest.config import ImapConfig
from maildigest.ingest.imap_client import (
    ImapClient,
    ImapConnectionError,
    IngestService,
    poll_once,
)
from maildigest.models import DigestMessage, FailureNotice, RawMail, Summary
from maildigest.pipeline import Delivered, FailedNotice, PipelineResult, QueuedLow
from maildigest.state.db import MailState, StateDB


def make_mail(message_id: str, *, subject: str = "Test") -> bytes:
    return (
        f"Message-ID: <{message_id}@example.org>\r\n"
        f"From: Absender <a@Example.ORG>\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Fri, 28 Aug 2026 14:12:03 +0200\r\n"
        f"\r\n"
        f"Hallo.\r\n"
    ).encode()


def make_message(raw: bytes, uid: str) -> MailMessage:
    return MailMessage([(f"1 (UID {uid} FLAGS ())".encode(), raw), b")"])


class FakeRawClient:
    """Ersatz für `imaplib.IMAP4_SSL`: protokolliert jedes rohe UID-Kommando (CT-9)."""

    def __init__(self, box: FakeMailBox) -> None:
        self._box = box

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self._box.capabilities

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        decoded = [
            arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg)
            for arg in args
        ]
        self._box.commands.append((command.upper(), *decoded))
        if self._box.uid_error is not None:
            raise self._box.uid_error
        return self._box.uid_status.get(command.upper(), "OK"), [b""]


class FakeMailBox:
    """Postfach-Attrappe: hält Nachrichten, kennt gelesen/verschoben, kann nicht löschen."""

    def __init__(
        self,
        messages: list[MailMessage] | None = None,
        *,
        capabilities: tuple[str, ...] = ("IMAP4REV1", "MOVE"),
        uid_status: dict[str, str] | None = None,
        uid_error: Exception | None = None,
    ) -> None:
        self.messages = messages if messages is not None else []
        self.commands: list[tuple[str, ...]] = []
        self.capabilities = capabilities
        self.uid_status = uid_status if uid_status is not None else {}
        self.uid_error = uid_error
        self.logouts = 0
        self.client = FakeRawClient(self)

    @property
    def flagged(self) -> list[tuple[str, str, bool]]:
        return [
            (cmd[1], MailMessageFlags.SEEN, True)
            for cmd in self.commands
            if cmd[0] == "STORE" and r"\Seen" in cmd[-1]
        ]

    @property
    def moved(self) -> list[tuple[str, str]]:
        return [(cmd[1], cmd[2].strip('"')) for cmd in self.commands if cmd[0] == "MOVE"]

    def login(self, username: str, password: str, initial_folder: str | None = "INBOX") -> None:
        return None

    def logout(self) -> None:
        self.logouts += 1

    def fetch(self, criteria: Any = "ALL", **kwargs: Any) -> list[MailMessage]:
        return list(self.messages)

    # F-ING-1/CT-9: Alle Komfort-Methoden von imap-tools expungen intern.
    def flag(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailBox.flag() expunged — verboten (F-ING-1, CT-9)")

    def move(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailBox.move() kann client-seitig löschen — verboten (CT-9)")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf Mails niemals löschen (F-ING-1)")

    def expunge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf niemals expunge aufrufen (F-ING-1)")


class RecordingProcessor:
    """Pipeline-Ersatz: merkt sich jede verarbeitete Mail und liefert ein festes Ergebnis."""

    def __init__(self, result: str = "delivered") -> None:
        self.result = result
        self.seen: list[RawMail] = []

    def __call__(self, raw: RawMail) -> PipelineResult:
        self.seen.append(raw)
        if self.result == "skipped_low":
            return QueuedLow(
                dedupe_key=raw.dedupe_key,
                from_domain=raw.from_domain,
                summary=Summary(headline="h", summary_text="t", importance="low"),
            )
        if self.result == "failed":
            return FailedNotice(
                notice=FailureNotice(
                    dedupe_key=raw.dedupe_key,
                    from_domain=raw.from_domain,
                    subject_sanitized="Test",
                    stage="sanitize",
                    reason_class="sanitize_error",
                ),
                notice_delivered=True,
            )
        return Delivered(
            dedupe_key=raw.dedupe_key,
            message=DigestMessage(parts=["ok"], importance="normal", dedupe_key=raw.dedupe_key),
        )


def make_config(**overrides: Any) -> ImapConfig:
    values: dict[str, Any] = {
        "host": "imap.example.org",
        "username": "mirror@example.org",
        "password": SecretStr("geheim"),
        "poll_interval_seconds": 120,
    }
    values.update(overrides)
    return ImapConfig(**values)


def make_client(box: FakeMailBox, **cfg_overrides: Any) -> ImapClient:
    client = ImapClient(make_config(**cfg_overrides), mailbox_factory=lambda: box)  # type: ignore[arg-type]
    client.connect()
    return client


@pytest.fixture
def db(tmp_path: Path) -> Iterator[StateDB]:
    with StateDB(tmp_path / "state.db") as database:
        yield database


# --- Happy Path ------------------------------------------------------------------------------


def test_poll_processes_every_new_mail(db: StateDB) -> None:
    box = FakeMailBox([make_message(make_mail("a"), "1"), make_message(make_mail("b"), "2")])
    processor = RecordingProcessor()

    stats = poll_once(make_client(box), db, processor)

    assert (stats.fetched, stats.processed, stats.duplicates, stats.failed) == (2, 2, 0, 0)
    assert [raw.message_id for raw in processor.seen] == ["<a@example.org>", "<b@example.org>"]
    assert db.count_by_status(MailState.DELIVERED) == 2


def test_processed_mails_are_marked_seen_not_deleted(db: StateDB) -> None:
    """F-ING-1: Nur das Gelesen-Flag; der Fake würde bei delete/expunge sofort auffliegen."""
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    poll_once(make_client(box), db, RecordingProcessor())
    assert box.flagged == [("1", MailMessageFlags.SEEN, True)]
    assert box.moved == []


def test_processed_mails_are_moved_when_configured(db: StateDB) -> None:
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    poll_once(make_client(box, move_processed_to="Processed"), db, RecordingProcessor())
    assert box.moved == [("1", "Processed")]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("delivered", MailState.DELIVERED),
        ("skipped_low", MailState.SKIPPED_LOW),
        ("failed", MailState.FAILED),
    ],
)
def test_pipeline_result_is_persisted_as_status(
    db: StateDB, result: str, expected: MailState
) -> None:
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    poll_once(make_client(box), db, RecordingProcessor(result))
    record = db.get("<a@example.org>")
    assert record is not None
    assert record.status is expected


# --- F-ING-2: Idempotenz ----------------------------------------------------------------------


def test_second_poll_of_the_same_mail_processes_it_once(db: StateDB) -> None:
    """Kernnachweis F-ING-2: zweimal abrufen ⇒ einmal verarbeiten."""
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    processor = RecordingProcessor()
    client = make_client(box)

    first = poll_once(client, db, processor)
    second = poll_once(client, db, processor)

    assert len(processor.seen) == 1
    assert (first.processed, first.duplicates) == (1, 0)
    assert (second.processed, second.duplicates) == (0, 1)


def test_duplicate_is_still_marked_seen(db: StateDB) -> None:
    """Ein Duplikat wird übersprungen, aber aus der Unseen-Menge genommen."""
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    client = make_client(box)
    poll_once(client, db, RecordingProcessor())
    box.commands.clear()
    poll_once(client, db, RecordingProcessor())
    assert box.flagged == [("1", MailMessageFlags.SEEN, True)]


def test_idempotent_across_process_restart(tmp_path: Path) -> None:
    """Auch nach Prozessende (neue StateDB-Instanz) wird nicht doppelt verarbeitet."""
    path = tmp_path / "state.db"
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    processor = RecordingProcessor()

    with StateDB(path) as first:
        poll_once(make_client(box), first, processor)
    with StateDB(path) as second:
        poll_once(make_client(box), second, processor)

    assert len(processor.seen) == 1


def test_mails_without_message_id_dedupe_via_fallback_hash(db: StateDB) -> None:
    raw = make_mail("x").replace(b"Message-ID: <x@example.org>\r\n", b"")
    box = FakeMailBox([make_message(raw, "1")])
    processor = RecordingProcessor()
    client = make_client(box)

    poll_once(client, db, processor)
    poll_once(client, db, processor)

    assert len(processor.seen) == 1
    assert processor.seen[0].dedupe_key.startswith("sha256:")


def test_two_distinct_mails_without_message_id_are_not_confused(db: StateDB) -> None:
    first = make_mail("x", subject="Rechnung").replace(b"Message-ID: <x@example.org>\r\n", b"")
    second = make_mail("y", subject="Mahnung").replace(b"Message-ID: <y@example.org>\r\n", b"")
    box = FakeMailBox([make_message(first, "1"), make_message(second, "2")])
    processor = RecordingProcessor()

    poll_once(make_client(box), db, processor)

    assert len(processor.seen) == 2


# --- CT-9/CT-12: Nachbehandlung ----------------------------------------------------------------


def test_ct9_poll_once_setzt_nur_seen_und_expunged_nie(db: StateDB) -> None:
    """CT-9: Über fünf Mails hinweg kein einziges EXPUNGE und kein `\\Deleted`."""
    box = FakeMailBox(
        [make_message(make_mail(f"m{index}"), str(index)) for index in range(1, 6)]
    )
    poll_once(make_client(box), db, RecordingProcessor())

    assert [cmd[0] for cmd in box.commands] == ["STORE"] * 5
    assert not any(r"\Deleted" in " ".join(cmd) for cmd in box.commands)


def test_ct12_fehlender_zielordner_stoppt_den_zyklus_nicht(db: StateDB) -> None:
    """CT-12: Ein abgelehntes MOVE ist kein „Postfach nicht erreichbar“.

    Ohne den Fix wirft `mark_processed` bei der ersten Mail einen `ImapConnectionError`;
    die CLI macht daraus „Fehler: Postfach nicht erreichbar“ und der Rest des Postfachs
    bleibt unverarbeitet.
    """
    box = FakeMailBox(
        [make_message(make_mail(f"m{index}"), str(index)) for index in range(1, 4)],
        uid_status={"MOVE": "NO"},
    )
    processor = RecordingProcessor()

    stats = poll_once(make_client(box, move_processed_to="GibtEsNicht"), db, processor)

    assert len(processor.seen) == 3
    assert stats.processed == 3
    assert stats.failed == 0
    assert [cmd[0] for cmd in box.commands] == ["STORE", "MOVE"] * 3


def test_ct12_verbindungsabbruch_wird_weiterhin_hochgereicht(db: StateDB) -> None:
    """Abgrenzung zu CT-12: Ein echter Socket-Fehler bleibt ein Verbindungsfehler."""
    box = FakeMailBox([make_message(make_mail("a"), "1")], uid_error=OSError("socket weg"))
    with pytest.raises(ImapConnectionError):
        poll_once(make_client(box), db, RecordingProcessor())


# --- I6: eine kaputte Mail stoppt den Loop nicht ----------------------------------------------


def test_crashing_processor_marks_failed_and_continues(db: StateDB) -> None:
    box = FakeMailBox(
        [
            make_message(make_mail("a"), "1"),
            make_message(make_mail("b"), "2"),
            make_message(make_mail("c"), "3"),
        ]
    )
    processed: list[str] = []

    def process(raw: RawMail) -> PipelineResult:
        if raw.message_id == "<b@example.org>":
            raise RuntimeError("kaputt")
        processed.append(raw.dedupe_key)
        return Delivered(
            dedupe_key=raw.dedupe_key,
            message=DigestMessage(parts=["ok"], importance="normal", dedupe_key=raw.dedupe_key),
        )

    stats = poll_once(make_client(box), db, process)

    assert processed == ["<a@example.org>", "<c@example.org>"]
    assert (stats.processed, stats.failed) == (2, 1)
    failed = db.get("<b@example.org>")
    assert failed is not None
    assert failed.status is MailState.FAILED
    assert failed.error_class == "ingest_error"
    assert len(box.flagged) == 3  # auch die kaputte Mail verlässt die Unseen-Menge


def test_failed_mail_is_not_retried_on_the_next_poll(db: StateDB) -> None:
    """Eine gescheiterte Mail gilt als gesehen — kein Endlos-Retry im Poll-Loop."""
    box = FakeMailBox([make_message(make_mail("a"), "1")])

    def failing(raw: RawMail) -> PipelineResult:
        raise RuntimeError("kaputt")

    client = make_client(box)
    poll_once(client, db, failing)
    stats = poll_once(client, db, failing)
    assert (stats.processed, stats.duplicates, stats.failed) == (0, 1, 0)


def test_broken_mail_still_reaches_the_processor(db: StateDB) -> None:
    """Auch eine Mail ohne jeden Header geht in die Pipeline (und damit in den I6-Pfad)."""
    box = FakeMailBox([make_message(b"\r\nnur ein body\r\n", "1")])
    processor = RecordingProcessor()
    poll_once(make_client(box), db, processor)
    assert len(processor.seen) == 1
    assert processor.seen[0].from_domain == ""


# --- Loop, Reconnect, Backoff ------------------------------------------------------------------


def test_run_once_connects_polls_and_disconnects(db: StateDB) -> None:
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    processor = RecordingProcessor()
    service = IngestService(
        cfg=make_config(),
        db=db,
        process=processor,
        client_factory=lambda: ImapClient(make_config(), mailbox_factory=lambda: box),  # type: ignore[arg-type]
    )
    stats = service.run_once()
    assert stats.processed == 1
    assert box.logouts == 1


def test_run_forever_polls_and_waits_the_configured_interval(db: StateDB) -> None:
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    waits: list[float] = []
    service = IngestService(
        cfg=make_config(poll_interval_seconds=42),
        db=db,
        process=RecordingProcessor(),
        client_factory=lambda: ImapClient(make_config(), mailbox_factory=lambda: box),  # type: ignore[arg-type]
    )

    def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 3:
            service.stop()

    service.sleep = fake_sleep
    total = service.run_forever()

    assert waits == [42.0, 42.0, 42.0]
    assert total.fetched == 3
    assert total.processed == 1  # nur der erste Durchlauf verarbeitet, danach Duplikate


def test_run_forever_reconnects_with_exponential_backoff(db: StateDB) -> None:
    """ARCHITECTURE §6: Verbindungsabbruch ⇒ Reconnect mit wachsender Wartezeit."""
    waits: list[float] = []
    attempts = {"n": 0}

    class BrokenClient(ImapClient):
        def connect(self) -> None:
            attempts["n"] += 1
            raise ImapConnectionError("Verbindung weg")

    service = IngestService(
        cfg=make_config(),
        db=db,
        process=RecordingProcessor(),
        client_factory=lambda: BrokenClient(make_config(), mailbox_factory=FakeMailBox),  # type: ignore[arg-type]
    )

    def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 4:
            service.stop()

    service.sleep = fake_sleep
    service.run_forever()

    assert waits == [5.0, 10.0, 20.0, 40.0]
    assert attempts["n"] == 4


def test_run_forever_resumes_after_a_reconnect(db: StateDB) -> None:
    """Nach erfolgreichem Reconnect wird weitergearbeitet und der Backoff zurückgesetzt."""
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    waits: list[float] = []
    calls = {"n": 0}
    processor = RecordingProcessor()

    class FlakyClient(ImapClient):
        def connect(self) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ImapConnectionError("erster Versuch scheitert")
            super().connect()

    service = IngestService(
        cfg=make_config(poll_interval_seconds=7),
        db=db,
        process=processor,
        client_factory=lambda: FlakyClient(make_config(), mailbox_factory=lambda: box),  # type: ignore[arg-type]
    )

    def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 2:
            service.stop()

    service.sleep = fake_sleep
    service.run_forever()

    assert waits == [5.0, 7.0]  # erst Backoff, dann normales Poll-Intervall
    assert len(processor.seen) == 1


def test_stop_before_the_first_poll_does_nothing(db: StateDB) -> None:
    service = IngestService(
        cfg=make_config(),
        db=db,
        process=RecordingProcessor(),
        client_factory=lambda: ImapClient(make_config(), mailbox_factory=FakeMailBox),  # type: ignore[arg-type]
    )
    service.stop()
    stats = service.run_forever()
    assert (stats.fetched, stats.processed) == (0, 0)
    assert service.stopped is True
