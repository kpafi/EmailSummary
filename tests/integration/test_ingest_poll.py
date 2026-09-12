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

import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from imap_tools import BaseMailBox, MailMessage, MailMessageFlags
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
from maildigest.state.db import SCHEMA_VERSION, MailState, StateDB, dedupe_hash


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


def header_fetch_answer(uid: str, raw: bytes, parts: str) -> tuple[str, list[Any]]:
    """Serverantwort auf ``UID FETCH <uid> (BODY.PEEK[HEADER]<0.N> RFC822.SIZE INTERNALDATE)``
    in der Form von `imaplib`; der Teilabruf `<0.N>` wird wie beim Server angewandt."""
    limit = re.search(r"<0\.(\d+)>", parts)
    headers = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    if limit:
        headers = headers[: int(limit.group(1))]
    meta = (
        f"1 (UID {uid} RFC822.SIZE {len(raw)} INTERNALDATE "
        f'"01-Sep-2026 12:00:00 +0000" BODY[HEADER]<0> {{{len(headers)}}}'
    ).encode()
    return "OK", [(meta, headers), b")"]


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
        if command.upper() == "FETCH":
            # O-1 (zweite Iteration): der Kopfzeilen-Abruf einer unparsbaren Mail.
            if self._box.header_fetch_error is not None:
                raise self._box.header_fetch_error
            entry = self._box.unparsable.get(str(args[0]))
            raw = entry[1] if isinstance(entry, tuple) else entry
            if isinstance(raw, bytes):
                return header_fetch_answer(str(args[0]), raw, str(args[1]))
            return "NO", [None]
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
        unparsable: dict[str, Any] | None = None,
        header_fetch_error: Exception | None = None,
    ) -> None:
        """`unparsable`: UID → Rohbytes, an denen der Parse von imap-tools scheitert, oder
        eine Ausnahme, die der Konstruktor werfen soll, oder (Ausnahme, Rohbytes für den
        Kopfzeilen-Abruf)."""
        self.messages = messages if messages is not None else []
        self.unparsable = unparsable if unparsable is not None else {}
        self.header_fetch_error = header_fetch_error
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

    def uids(self, criteria: Any = "ALL", charset: Any = "US-ASCII", sort: Any = None) -> list[Any]:
        """Wie `MailBox.uids`: ein `UID SEARCH`; die UIDs aller ungesehenen Mails."""
        uids = [msg.uid for msg in self.messages] + list(self.unparsable)
        return sorted(uids, key=lambda uid: str(uid or "").zfill(9))  # Server-Reihenfolge

    def fetch(self, criteria: Any = "ALL", **kwargs: Any) -> list[MailMessage]:
        """Wie `MailBox.fetch(uid_list=...)`: parst je UID im Aufruf (eifrig, wie imap-tools)."""
        uid_list = kwargs.get("uid_list")
        if uid_list is None:
            return list(self.messages)
        found = [msg for msg in self.messages if msg.uid in uid_list]
        for uid in uid_list:
            entry = self.unparsable.get(uid)
            if isinstance(entry, tuple):
                raise entry[0]
            if isinstance(entry, BaseException):
                raise entry
            if isinstance(entry, bytes):  # der echte Parse von imap-tools — er wirft selbst
                found.append(MailMessage([(f"1 (UID {uid} FLAGS ())".encode(), entry), b")"]))
        return found

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


def test_hc2_1_kill_waehrend_der_verarbeitung_ist_kein_dauer_dos(tmp_path: Path) -> None:
    """HC2-1 (offene Frage): Greift der Runner eine teure Mail nach einem Absturz erneut auf?

    Nein. `poll_once` reserviert den Dedupe-Key **vor** der Verarbeitung (`claim`, ADR-019),
    und `claim` committet sofort. Ein harter Abbruch mitten in der Sanitize-Stufe — hier
    als `BaseException` nachgestellt, die der I6-Handler bewusst nicht fängt — hinterlässt
    die Zeile in der Datenbank; der nächste Prozess sieht ein Duplikat. Eine einzelne
    Angriffsmail kostet damit höchstens *einen* Zyklus, nicht jeden folgenden.
    """
    path = tmp_path / "state.db"
    box = FakeMailBox([make_message(make_mail("a"), "1")])
    attempts: list[str] = []

    def killed(raw: RawMail) -> PipelineResult:
        attempts.append(raw.dedupe_key)
        raise KeyboardInterrupt("Prozess wird abgeschossen")

    with StateDB(path) as first, pytest.raises(KeyboardInterrupt):
        poll_once(make_client(box), first, killed)

    processor = RecordingProcessor()
    with StateDB(path) as second:
        stats = poll_once(make_client(box), second, processor)

    assert attempts == ["<a@example.org>"]
    assert processor.seen == []
    assert (stats.fetched, stats.duplicates) == (1, 1)


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


# --- HC-10 / ADR-079: kollidierende Message-ID --------------------------------------------------


def test_hc10_forged_message_id_does_not_suppress_the_real_mail(db: StateDB) -> None:
    """Angreifer-Mail zuerst, echte Mail mit derselben Message-ID danach ⇒ beide kommen an.

    Der Dedupe-Key ist ein frei wählbarer Header. Vor ADR-079 hat die zuerst eingetroffene
    Mail jede spätere mit gleicher Message-ID still verschluckt — genau das Angriffsziel.
    """
    attacker = make_mail("rechnung-4711@bank", subject="Ihre Rechnung")
    genuine = make_mail("rechnung-4711@bank", subject="Ihre echte Rechnung")
    box = FakeMailBox([make_message(attacker, "1"), make_message(genuine, "2")])
    processor = RecordingProcessor()

    stats = poll_once(make_client(box), db, processor)

    assert (stats.processed, stats.duplicates) == (2, 0)
    assert [raw.id_collision for raw in processor.seen] == [False, True]
    assert processor.seen[1].dedupe_key.startswith("collision:")
    assert processor.seen[1].dedupe_key != processor.seen[0].dedupe_key


def test_hc10_collision_is_logged_with_hashes_only(
    db: StateDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Das Kollisions-Ereignis ist sichtbar (WARNING) und trägt nur gekürzte Hashes (I5)."""
    box = FakeMailBox(
        [
            make_message(make_mail("gleich@bank", subject="A"), "1"),
            make_message(make_mail("gleich@bank", subject="B"), "2"),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="maildigest.ingest"):
        poll_once(make_client(box), db, RecordingProcessor())

    records = [record for record in caplog.records if record.message == "mail_id_collision"]
    assert len(records) == 1
    assert "gleich@bank" not in caplog.text
    assert len(records[0].mail) == 12  # type: ignore[attr-defined]
    assert len(records[0].collision_mail) == 12  # type: ignore[attr-defined]


def test_hc10_identical_mail_twice_is_still_a_duplicate(db: StateDB) -> None:
    """Gleicher Key **und** gleicher Inhalt bleibt ein Duplikat — F-ING-2 ist unberührt."""
    raw = make_mail("a")
    box = FakeMailBox([make_message(raw, "1"), make_message(raw, "2")])
    processor = RecordingProcessor()

    stats = poll_once(make_client(box), db, processor)

    assert (stats.processed, stats.duplicates) == (1, 1)


def test_hc10_collision_key_is_stable_across_polls(db: StateDB) -> None:
    """Die kollidierende Mail wird beim zweiten Abruf wieder als Duplikat erkannt."""
    attacker = make_mail("gleich@bank", subject="A")
    genuine = make_mail("gleich@bank", subject="B")
    box = FakeMailBox([make_message(attacker, "1"), make_message(genuine, "2")])
    processor = RecordingProcessor()
    client = make_client(box)

    poll_once(client, db, processor)
    second = poll_once(client, db, processor)

    assert len(processor.seen) == 2
    assert (second.processed, second.duplicates) == (0, 2)


def test_hc10_state_of_schema_version_two_migrates_without_collision_alarm(
    tmp_path: Path,
) -> None:
    """Eine Datenbank der Version 2 wird additiv migriert; alte Zeilen bleiben Duplikate.

    Zeilen ohne `content_hash` heissen "Inhalt unbekannt" — ohne Vergleichswert ist eine
    Kollision nicht beweisbar, und ein Fehlalarm nach dem Update wäre die schlechtere
    Antwort als das bisherige Verhalten.
    """
    path = tmp_path / "state.db"
    legacy_hash = dedupe_hash("<alt@example.org>")
    with sqlite3.connect(path) as legacy:  # Version-2-Fixture von Hand, ohne content_hash
        legacy.executescript(
            "CREATE TABLE seen_mails ("
            "  message_id_hash TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL,"
            "  status TEXT NOT NULL, error_class TEXT,"
            "  retry_count INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta (key, value) VALUES ('schema_version', '2');"
            "INSERT INTO seen_mails (message_id_hash, first_seen_at, status)"
            f" VALUES ('{legacy_hash}', '2026-08-01T00:00:00+00:00',"
            "         'delivered');"
        )
    legacy.close()

    box = FakeMailBox([make_message(make_mail("alt", subject="neuer Inhalt"), "1")])
    processor = RecordingProcessor()
    with StateDB(path) as database:
        assert database.meta_get("schema_version") == str(SCHEMA_VERSION)
        stats = poll_once(make_client(box), database, processor)

    assert (stats.processed, stats.duplicates) == (0, 1)
    assert processor.seen == []


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


# --- O-1: eine einzelne Mail darf den Zyklus nie anhalten -----------------------------------


def poison_mail(depth: int = 250) -> bytes:
    """Die Gift-Mail des Befunds: 16 KB, `depth` verschachtelte `multipart`-Ebenen."""
    head = (
        b"Message-ID: <poison@example.org>\r\nFrom: Absender <a@Example.ORG>\r\n"
        b"Subject: tief\r\nMIME-Version: 1.0\r\n"
    )
    body = b"Content-Type: text/plain\r\n\r\nHallo\r\n"
    for index in range(depth):
        boundary = b"B%d" % index
        body = (
            b"Content-Type: multipart/mixed; boundary="
            + boundary
            + b"\r\n\r\n--"
            + boundary
            + b"\r\n"
            + body
            + b"--"
            + boundary
            + b"--\r\n"
        )
    return head + body


def test_o1_poll_once_ueberlebt_die_giftmail(tmp_path: Path) -> None:
    """Gift-Mail + normale Mail: beide gebucht, beide gelesen, kein Abbruch — auch nach Neustart.

    Vor dem Fix warf `build_raw_mail` an der Gift-Mail `RecursionError`; `poll_once` brach
    ohne Status ab, die Mail blieb ungelesen im Postfach und kippte jeden weiteren Poll —
    die normale Mail dahinter wurde nie verarbeitet.
    """
    path = tmp_path / "state.db"
    box = FakeMailBox(
        [make_message(poison_mail(), "1"), make_message(make_mail("normal"), "2")]
    )
    processor = RecordingProcessor()

    with StateDB(path) as db:
        stats = poll_once(make_client(box), db, processor)

        assert (stats.fetched, stats.processed, stats.failed) == (2, 2, 0)
        assert [raw.message_id for raw in processor.seen] == [
            "<poison@example.org>",
            "<normal@example.org>",
        ]
        assert db.count_by_status(MailState.DELIVERED) == 2

    assert [flag[0] for flag in box.flagged] == ["1", "2"]

    # Neustart auf derselben Datenbank: beide Mails sind Duplikate, der Prozessor schweigt.
    zweiter = RecordingProcessor()
    with StateDB(path) as db:
        stats = poll_once(make_client(box), db, zweiter)

    assert (stats.processed, stats.duplicates, stats.failed) == (0, 2, 0)
    assert zweiter.seen == []


def test_o1_beliebiger_fehler_vor_process_wird_zur_notiz(
    db: StateDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Scheitert `build_raw_mail` (Attrappe, beliebige Exception), läuft der Zyklus weiter.

    Die Mail wird über einen Ersatz-Dedupe-Key beansprucht, als `ingest_failed` in den
    Fail-closed-Pfad gereicht, mit Fehlerklasse `ingest_error` gebucht und als gelesen
    markiert — die Mail dahinter wird ganz normal verarbeitet (ADR-020 (e), I6).
    """
    from maildigest.ingest import imap_client as modul

    echt = modul.build_raw_mail

    def kaputt(msg: MailMessage) -> RawMail:
        if msg.uid == "1":
            raise ValueError("Attrappe: irgendetwas ging schief")
        return echt(msg)

    monkeypatch.setattr(modul, "build_raw_mail", kaputt)
    box = FakeMailBox([make_message(make_mail("a"), "1"), make_message(make_mail("b"), "2")])
    processor = RecordingProcessor()

    with caplog.at_level(logging.INFO, logger="maildigest.ingest"):
        stats = poll_once(make_client(box), db, processor)

    assert (stats.fetched, stats.processed, stats.failed) == (2, 1, 1)
    unlesbar = processor.seen[0]
    assert unlesbar.ingest_failed is True
    assert unlesbar.dedupe_key == "<a@example.org>"  # Message-ID war noch lesbar
    assert [raw.message_id for raw in processor.seen[1:]] == ["<b@example.org>"]

    record = db.get(unlesbar.dedupe_key)
    assert record is not None
    assert record.status is MailState.FAILED
    assert record.error_class == "ingest_error"
    assert [flag[0] for flag in box.flagged] == ["1", "2"]

    meldungen = [entry.message for entry in caplog.records]
    assert "mail_ingest_failed" in meldungen
    # I5: kein Mail-Inhalt im Protokoll.
    assert not any("Hallo" in entry.getMessage() for entry in caplog.records)

    # ADR-019: beim Wiederanlauf ist die Mail ein Duplikat, nicht ein zweiter Versuch.
    zweiter = RecordingProcessor()
    zweite_stats = poll_once(make_client(box), db, zweiter)
    assert (zweite_stats.duplicates, zweite_stats.failed) == (2, 0)
    assert zweiter.seen == []


def test_o1_ersatzkey_ohne_lesbare_header_ist_stabil() -> None:
    """Ohne lesbare `Message-ID` kommt der Ersatz-Key aus UID, FETCH-Daten und Kopfzeilen.

    Er muss über Neustarts hinweg derselbe sein (ADR-019) und sich von dem einer anderen
    Mail unterscheiden.
    """
    from maildigest.ingest.imap_client import _unreadable_raw_mail

    ohne_id = b"From: a@Example.ORG\r\nSubject: x\r\n\r\nHallo\r\n"
    erste = _unreadable_raw_mail(make_message(ohne_id, "1"))
    wieder = _unreadable_raw_mail(make_message(ohne_id, "1"))
    andere = _unreadable_raw_mail(make_message(ohne_id, "2"))

    assert erste.dedupe_key == wieder.dedupe_key
    assert erste.dedupe_key != andere.dedupe_key
    assert erste.ingest_failed is True
    assert erste.from_domain == ""  # „unbekannt", ADR-020 (b)


# --- O-1, zweite Iteration: unparsbare Mail wird je UID isoliert -----------------------------


def rfc822_poison(depth: int = 990) -> bytes:
    """Die Gift-Mail des Skeptikers (sk6_e2e_rfc.py): `depth` `message/rfc822`-Ebenen, 31 KB.

    Ab Tiefe 984 scheitert `email.message_from_bytes` — und damit der Konstruktor von
    `imap_tools.MailMessage` — mit `RecursionError` (Standard-Rekursionslimit 1000), bevor
    irgendein Code dieses Projekts die Mail sieht.
    """
    head = b"Message-ID: <poison-rfc822@example.org>\r\nFrom: a@example.org\r\nSubject: t\r\n"
    body = b"Content-Type: text/plain\r\n\r\nHallo\r\n"
    return head + b"Content-Type: message/rfc822\r\n\r\n" * depth + body


class RawLevelClient:
    """`imaplib`-Attrappe, gegen die die **echten** `uids()`/`fetch()` von imap-tools laufen.

    Zählt jedes Kommando (CT-9, O-1 (c)); antwortet auf SEARCH, FETCH (voll und
    Kopfzeilen) und STORE. Alles andere fliegt auf.
    """

    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self, box: RawLevelMailBox) -> None:
        self._box = box
        self.commands: list[tuple[str, ...]] = []

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        decoded = [
            arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg)
            for arg in args
        ]
        self.commands.append((command.upper(), *decoded))
        verb = command.upper()
        if verb == "SEARCH":
            return "OK", [" ".join(self._box.unseen()).encode()]
        if verb == "FETCH":
            uid, parts = decoded[0], decoded[1]
            raw = self._box.mails.get(uid)
            if raw is None:
                return "OK", [None]
            if "HEADER" in parts:
                return header_fetch_answer(uid, raw, parts)
            meta = f"1 (UID {uid} RFC822.SIZE {len(raw)} FLAGS () BODY[] {{{len(raw)}}}".encode()
            return "OK", [(meta, raw), b")"]
        if verb == "STORE":
            self._box.seen.add(decoded[0])
            return "OK", [b""]
        raise AssertionError(f"unerwartetes IMAP-Kommando: {command} (F-ING-1/CT-9)")

    def expunge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf niemals expunge aufrufen (F-ING-1, CT-9)")


class RawLevelMailBox(BaseMailBox):
    """Postfach auf `imaplib`-Ebene: `uids()`/`fetch()` sind die echten von imap-tools.

    Damit läuft der eifrige Parse in `MailMessage.__init__` genau dort, wo er im Betrieb
    läuft — innerhalb des `fetch`-Generators. `track_seen=False` liefert wie die Attrappe
    des Skeptikers jede Mail in jedem Zyklus erneut.
    """

    def __init__(self, mails: dict[str, bytes], *, track_seen: bool = True) -> None:
        self.mails = dict(mails)
        self.seen: set[str] = set()
        self.track_seen = track_seen
        super().__init__()

    def _get_mailbox_client(self) -> Any:
        return RawLevelClient(self)

    def unseen(self) -> list[str]:
        return [uid for uid in self.mails if not (self.track_seen and uid in self.seen)]

    @property
    def commands(self) -> list[tuple[str, ...]]:
        return self.client.commands  # type: ignore[no-any-return]

    def login(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def logout(self) -> None:
        return None

    def flag(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailBox.flag() expunged — verboten (F-ING-1, CT-9)")

    def move(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailBox.move() kann client-seitig löschen — verboten (CT-9)")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf Mails niemals löschen (F-ING-1)")

    def expunge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf niemals expunge aufrufen (F-ING-1)")


def make_raw_client(box: RawLevelMailBox) -> ImapClient:
    client = ImapClient(make_config(), mailbox_factory=lambda: box)
    client.connect()
    return client


def test_o1b_unparsbare_mail_blockiert_den_poll_nicht(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Der Parse von imap-tools wirft für UID 1 `RecursionError`; UID 2 ist gesund.

    Vor dem Fix: `fetch_unseen` deutete den Fehler in `ImapConnectionError` um — der ganze
    Zyklus brach ab, nichts wurde verarbeitet, nichts markiert, bei jedem Poll dasselbe.
    Jetzt: die gesunde Mail wird verarbeitet, die unparsbare als `failed`/`ingest_error` mit
    Notiz gebucht, beide als gelesen markiert; beim Wiederanlauf ist sie ein Duplikat.
    """
    path = tmp_path / "state.db"
    kopf = (
        b"Message-ID: <gift@example.org>\r\nFrom: Absender <a@Example.ORG>\r\n"
        b"Subject: Geheimer Betreff\r\n\r\nGeheimer Inhalt\r\n"
    )
    gift = {"1": (RecursionError("maximum recursion depth exceeded"), kopf)}
    box = FakeMailBox([make_message(make_mail("normal"), "2")], unparsable=gift)
    processor = RecordingProcessor()

    with StateDB(path) as db, caplog.at_level(logging.INFO, logger="maildigest.ingest"):
        stats = poll_once(make_client(box), db, processor)

        assert (stats.fetched, stats.processed, stats.failed) == (2, 1, 1)
        unlesbar, gesund = processor.seen
        assert unlesbar.ingest_failed is True
        assert unlesbar.dedupe_key == "<gift@example.org>"  # aus dem Kopfzeilen-Abruf
        assert "a@Example.ORG" in unlesbar.from_addr
        assert gesund.message_id == "<normal@example.org>"
        record = db.get(unlesbar.dedupe_key)
        assert record is not None
        assert record.status is MailState.FAILED
        assert record.error_class == "ingest_error"
        assert db.count_by_status(MailState.DELIVERED) == 1

    assert [flag[0] for flag in box.flagged] == ["1", "2"]
    meldungen = [entry.message for entry in caplog.records]
    assert "mail_unparsable" in meldungen
    assert "mail_ingest_failed" not in meldungen
    unparsable = next(entry for entry in caplog.records if entry.message == "mail_unparsable")
    assert unparsable.levelno == logging.ERROR
    assert unparsable.error == "RecursionError"  # type: ignore[attr-defined]
    assert len(unparsable.mail) == 12  # type: ignore[attr-defined]
    # I5: weder Betreff noch Inhalt noch Message-ID im Protokoll.
    for entry in caplog.records:
        zeile = entry.getMessage() + repr(entry.__dict__)
        assert "Geheimer" not in zeile and "gift@example.org" not in zeile

    # Wiederanlauf: nur die unparsbare Mail liegt noch da — Duplikat, kein zweiter Versuch.
    zweiter = RecordingProcessor()
    with StateDB(path) as db:
        stats = poll_once(make_client(FakeMailBox(unparsable=gift)), db, zweiter)
    assert (stats.fetched, stats.duplicates, stats.failed) == (1, 1, 0)
    assert zweiter.seen == []


def test_o1b_echte_giftmail_rfc822_tiefe_990(tmp_path: Path) -> None:
    """Die Bytes des Skeptikers durch den **echten** `ImapClient` und die echten
    imap-tools-Abrufpfade: drei Zyklen, kein `ImapConnectionError`, in jedem Zyklus werden
    beide Mails als gelesen markiert. Vor dem Fix: drei Zyklen `ImapConnectionError`,
    verarbeitete Mails [], STORE-Aufrufe 0."""
    poison = rfc822_poison(990)
    with pytest.raises(RecursionError):  # Vorbedingung: der Parse von imap-tools scheitert
        MailMessage.from_bytes(poison)
    box = RawLevelMailBox({"1": poison, "2": make_mail("normal")}, track_seen=False)
    processor = RecordingProcessor()

    with StateDB(tmp_path / "state.db") as db:
        client = make_raw_client(box)
        for zyklus in range(3):
            stats = poll_once(client, db, processor)  # wirft nicht
            assert stats.fetched == 2
            stores = [cmd for cmd in box.commands if cmd[0] == "STORE"]
            assert [cmd[1] for cmd in stores[-2:]] == ["1", "2"], zyklus
            assert len(stores) == 2 * (zyklus + 1)
        assert [(raw.message_id, raw.ingest_failed) for raw in processor.seen] == [
            ("<poison-rfc822@example.org>", True),
            ("<normal@example.org>", False),
        ]
        record = db.get("<poison-rfc822@example.org>")
        assert record is not None and record.status is MailState.FAILED
        assert record.error_class == "ingest_error"
        assert db.count_by_status(MailState.DELIVERED) == 1
    assert stats.duplicates == 2  # dritter Zyklus: beide bekannt


def test_o1b_header_abruf_scheitert_auch(tmp_path: Path) -> None:
    """Scheitert auch der Kopfzeilen-Abruf: leere Felder, Key allein aus der UID — und der
    Key ist über zwei Läufe stabil (ADR-019), sonst wäre die Mail beim Wiederanlauf neu."""
    path = tmp_path / "state.db"
    gift = {"5": RecursionError("maximum recursion depth exceeded")}
    keys: list[str] = []
    for _lauf in range(2):
        box = FakeMailBox(unparsable=gift, header_fetch_error=OSError("socket error"))
        processor = RecordingProcessor()
        with StateDB(path) as db:
            stats = poll_once(make_client(box), db, processor)
        assert [flag[0] for flag in box.flagged] == ["5"]
        if processor.seen:
            raw = processor.seen[0]
            assert raw.ingest_failed is True
            assert raw.message_id is None
            assert (raw.from_addr, raw.from_domain, raw.subject_raw) == ("", "", "")
            assert raw.dedupe_key.startswith("sha256:")
            keys.append(raw.dedupe_key)
            assert (stats.failed, stats.duplicates) == (1, 0)
        else:
            assert (stats.failed, stats.duplicates) == (0, 1)
    assert len(keys) == 1  # zweiter Lauf: Duplikat unter demselben Key
    andere = FakeMailBox(unparsable={"6": RecursionError("x")}, header_fetch_error=OSError())
    with StateDB(tmp_path / "andere.db") as db:
        processor = RecordingProcessor()
        poll_once(make_client(andere), db, processor)
    assert processor.seen[0].dedupe_key != keys[0]


def test_o1b_nur_uid_kommandos_kein_expunge(tmp_path: Path) -> None:
    """ADR-064: Auch der neue Kopfzeilen-Abruf ist ein rohes `UID FETCH` mit `PEEK`; im ganzen
    Zyklus fallen nur SEARCH, FETCH und STORE — nie EXPUNGE, nie `\\Deleted`."""
    box = RawLevelMailBox({"1": rfc822_poison(990), "2": make_mail("normal")})
    with StateDB(tmp_path / "state.db") as db:
        poll_once(make_raw_client(box), db, RecordingProcessor())
    verbs = [cmd[0] for cmd in box.commands]
    assert set(verbs) == {"SEARCH", "FETCH", "STORE"}
    assert not any("Deleted" in " ".join(cmd) for cmd in box.commands)
    header_fetches = [cmd for cmd in box.commands if cmd[0] == "FETCH" and "HEADER" in cmd[2]]
    assert len(header_fetches) == 1
    assert header_fetches[0][1] == "1"
    assert "BODY.PEEK[HEADER]<0." in header_fetches[0][2]
    assert "BODY[" not in header_fetches[0][2]  # kein Abruf ohne PEEK, kein Seen-Flag vorab


@pytest.mark.parametrize("n", [10, 20, 40])
def test_o1b_kommandozahl_je_zyklus(tmp_path: Path, n: int) -> None:
    """O-1 (c): Der Abruf je UID kostet für gesunde Mails **kein** zusätzliches Kommando —
    `MailBox.fetch(bulk=False)` setzte ohnehin ein SEARCH und je Mail ein FETCH ab
    (vorher wie nachher 1 + 2n mit den STOREs). Nur eine unparsbare Mail kostet genau
    ein weiteres FETCH (Kopfzeilen)."""
    mails = {str(i): make_mail(f"m{i}") for i in range(1, n + 1)}
    box = RawLevelMailBox(mails)
    with StateDB(tmp_path / "a.db") as db:
        stats = poll_once(make_raw_client(box), db, RecordingProcessor())
    assert stats.processed == n
    assert len(box.commands) == 1 + 2 * n
    assert sum(1 for cmd in box.commands if cmd[0] == "SEARCH") == 1

    mails[str(n + 1)] = rfc822_poison(990)
    box = RawLevelMailBox(mails)
    with StateDB(tmp_path / "b.db") as db:
        stats = poll_once(make_raw_client(box), db, RecordingProcessor())
    assert (stats.processed, stats.failed) == (n, 1)
    assert len(box.commands) == 1 + 2 * (n + 1) + 1
