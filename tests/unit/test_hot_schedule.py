"""Zeit-Grenzfälle: Digest-Uhrzeit und Wiederanlauf-Ränder der Outbox — Hot-Testing WP10.

Zwei Zeitachsen mit je eigenen Rändern (PLAN.md WP10):

* **Sammel-Digest** (`Runner.maybe_send_low_digest`, F-SUM-5): läuft auf **lokaler** Zeit
  (`datetime.now`, wie `[general] low_digest_time`). Interessant sind Mitternacht,
  die Minute exakt auf der Schranke, der Tageswechsel und die Zeitumstellung — bei
  Sommerzeit-Ende gibt es 02:30 zweimal, bei Sommerzeit-Beginn gar nicht.
* **Zustell-Warteschlange** (`delivery.OutboxMessenger`, ADR-048): 5 Versuche über
  höchstens eine Stunde. Geprüft werden beide Abbruchbedingungen exakt auf ihrer Schranke
  (`attempts >= 5`, `age >= 3600 s`) und eine darunter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from maildigest.config import Config, load_config_from_dict
from maildigest.delivery import (
    DELIVERY_DEADLINE_SECONDS,
    DELIVERY_MAX_ATTEMPTS,
    OutboxMessenger,
    next_delivery_delay,
)
from maildigest.models import DigestMessage
from maildigest.output.composer import LOW_DIGEST_DEDUPE_KEY, DigestComposer
from maildigest.pipeline import PipelineDeps
from maildigest.runner import Runner
from maildigest.state.db import MailState, StateDB, dedupe_hash

BERLIN = ZoneInfo("Europe/Berlin")


# --- Attrappen ------------------------------------------------------------------------


class CollectingMessenger:
    def __init__(self) -> None:
        self.sent: list[DigestMessage] = []

    def send(self, message: DigestMessage) -> None:
        self.sent.append(message)


class AlwaysFailingMessenger:
    def __init__(self) -> None:
        self.attempts = 0

    def send(self, message: DigestMessage) -> None:
        self.attempts += 1
        raise RuntimeError("Messenger nicht erreichbar")


class DeadIngest:
    """Ingest-Attrappe: der Runner ruft sie im Digest-Test nie auf."""

    def run_once(self) -> Any:  # pragma: no cover - wird im Test nicht gebraucht
        raise AssertionError("Ingest wird hier nicht benutzt")

    def stop(self) -> None:
        return None


def config(**general: Any) -> Config:
    return load_config_from_dict(
        {
            "general": general,
            "imap": {"host": "imap.example.org", "username": "mirror@example.org"},
            "llm": {"model": "modell", "api_key": "sk-test"},
            "messenger": {
                "active": "telegram",
                "telegram": {"token": "1:abc", "chat_id": "42"},
            },
        },
        env={},
    )


class Clock:
    """Steuerbare Uhr — der Test schiebt die Zeit von Hand weiter."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def make_runner(
    tmp_path: Path, clock: Clock, *, digest_time: str = "18:00"
) -> tuple[Runner, CollectingMessenger, StateDB]:
    database = StateDB(tmp_path / "state.db")
    messenger = CollectingMessenger()
    composer = DigestComposer()
    outbox = OutboxMessenger(database, messenger, now=lambda: datetime.now(UTC))
    runner = Runner(
        config=config(low_digest_time=digest_time),
        db=database,
        deps=PipelineDeps(  # type: ignore[arg-type]
            sanitizer=None, summarizer=None, critic=None,
            composer=composer, messenger=outbox,
        ),
        outbox=outbox,
        composer=composer,
        ingest=DeadIngest(),  # type: ignore[arg-type]
        now=clock,
    )
    return runner, messenger, database


def queue_one_low(database: StateDB) -> None:
    database.queue_low(
        "<leise@example>", headline="Newsletter", category="newsletter",
        from_domain="x[.]example",
    )


# --- Digest-Uhrzeit ---------------------------------------------------------------------


def test_before_the_scheduled_minute_nothing_is_sent(tmp_path: Path) -> None:
    """Eine Minute vor der Schranke passiert nichts."""
    clock = Clock(datetime(2026, 9, 2, 17, 59))
    runner, messenger, database = make_runner(tmp_path, clock)
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is False
    assert messenger.sent == []
    database.close()


def test_exactly_on_the_scheduled_minute_the_digest_goes_out(tmp_path: Path) -> None:
    """Die Schranke ist inklusiv: exakt 18:00 löst aus."""
    clock = Clock(datetime(2026, 9, 2, 18, 0))
    runner, messenger, database = make_runner(tmp_path, clock)
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is True
    assert messenger.sent[0].dedupe_key == LOW_DIGEST_DEDUPE_KEY
    assert database.low_digest_entries() == [], "Quell-Einträge werden geleert"
    database.close()


def test_midnight_schedule_fires_at_zero_zero(tmp_path: Path) -> None:
    """`low_digest_time = "00:00"`: Mitternacht ist kein Sonderfall (Tupelvergleich)."""
    clock = Clock(datetime(2026, 9, 2, 0, 0))
    runner, messenger, database = make_runner(tmp_path, clock, digest_time="00:00")
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is True
    assert len(messenger.sent) == 1
    database.close()


def test_last_minute_of_the_day_still_fires(tmp_path: Path) -> None:
    """`23:59` — die letzte Minute des Tages ist erreichbar."""
    clock = Clock(datetime(2026, 9, 2, 23, 59))
    runner, messenger, database = make_runner(tmp_path, clock, digest_time="23:59")
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is True
    assert len(messenger.sent) == 1
    database.close()


def test_only_one_digest_per_calendar_day(tmp_path: Path) -> None:
    """Nach dem Versand blockt der Tagesstempel jeden weiteren Aufruf desselben Tages."""
    clock = Clock(datetime(2026, 9, 2, 18, 0))
    runner, messenger, database = make_runner(tmp_path, clock)
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is True

    queue_one_low(database)
    clock.now = datetime(2026, 9, 2, 23, 59)
    assert runner.maybe_send_low_digest() is False
    assert len(messenger.sent) == 1

    clock.now = datetime(2026, 9, 3, 18, 0)
    assert runner.maybe_send_low_digest() is True
    assert len(messenger.sent) == 2
    database.close()


def test_empty_queue_marks_the_day_without_sending(tmp_path: Path) -> None:
    """Ohne gesammelte Mails wird keine leere Nachricht erzeugt — der Tag gilt trotzdem."""
    clock = Clock(datetime(2026, 9, 2, 18, 0))
    runner, messenger, database = make_runner(tmp_path, clock)
    assert runner.maybe_send_low_digest() is False
    assert messenger.sent == []

    # Eine später am selben Tag eingereihte Mail wandert in den Digest von morgen.
    queue_one_low(database)
    clock.now = datetime(2026, 9, 2, 20, 0)
    assert runner.maybe_send_low_digest() is False
    clock.now = datetime(2026, 9, 3, 18, 0)
    assert runner.maybe_send_low_digest() is True
    database.close()


def test_missed_window_delays_but_never_drops_entries(tmp_path: Path) -> None:
    """Läuft der Prozess über die Schranke hinweg nicht, geht nichts verloren.

    Der Digest kommt dann am Folgetag — die Einträge bleiben bis dahin in der
    Warteschlange (F-SUM-5: „nichts geht stumm verloren").
    """
    clock = Clock(datetime(2026, 9, 2, 17, 0))
    runner, sent, database = make_runner(tmp_path, clock, digest_time="18:00")
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is False

    clock.now = datetime(2026, 9, 3, 2, 0)  # Prozess war über 18:00 hinweg aus
    assert runner.maybe_send_low_digest() is False
    assert len(database.low_digest_entries()) == 1

    clock.now = datetime(2026, 9, 3, 18, 0)
    assert runner.maybe_send_low_digest() is True
    assert len(sent.sent) == 1
    database.close()


def test_dst_end_repeats_a_wall_clock_hour_without_a_second_digest(tmp_path: Path) -> None:
    """Sommerzeit-Ende: 02:30 gibt es zweimal — der Digest darf nur einmal gehen.

    Am 25.10.2026 wird in Europa/Berlin um 03:00 auf 02:00 zurückgestellt. Der Tagesstempel
    (`meta`-Eintrag auf Datumsbasis) trägt genau deshalb, weil er nicht an der Uhrzeit hängt.
    """
    first = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)
    assert first.utcoffset() != second.utcoffset(), "Testdatum ist keine Umstellung"

    clock = Clock(first)
    runner, messenger, database = make_runner(tmp_path, clock, digest_time="02:00")
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is True

    queue_one_low(database)
    clock.now = second
    assert runner.maybe_send_low_digest() is False, "zweiter Durchlauf derselben Stunde"
    assert len(messenger.sent) == 1
    database.close()


def test_dst_start_skips_the_hour_and_fires_afterwards(tmp_path: Path) -> None:
    """Sommerzeit-Beginn: 02:30 existiert nicht — der Digest kommt trotzdem noch.

    Am 29.03.2026 springt Europa/Berlin von 02:00 auf 03:00. Eine auf `02:30` gestellte
    Zustellung darf nicht ausfallen: Der Vergleich ist `>=`, also löst 03:00 aus.
    """
    clock = Clock(datetime(2026, 3, 29, 3, 0, tzinfo=BERLIN))
    runner, messenger, database = make_runner(tmp_path, clock, digest_time="02:30")
    queue_one_low(database)
    assert runner.maybe_send_low_digest() is True
    assert len(messenger.sent) == 1
    database.close()


def test_digest_time_is_validated_by_the_config_schema() -> None:
    """Kaputte Uhrzeiten scheitern beim Laden, nicht erst im Betrieb (NF-3)."""
    for broken in ["18", "25:00", "18:60", "abc", "6:00 pm", "", "18:0"]:
        with pytest.raises(Exception):  # noqa: B017 - ConfigError/ValidationError
            config(low_digest_time=broken)
    for valid in ["00:00", "23:59", "09:05"]:
        assert config(low_digest_time=valid).general.low_digest_time == valid


# --- Wiederanlauf-Ränder der Outbox -----------------------------------------------------


def test_backoff_sums_to_less_than_an_hour() -> None:
    """Alle Wartezeiten zusammen bleiben unter der Deadline (ARCHITECTURE §6)."""
    total = sum(next_delivery_delay(attempt) for attempt in range(1, DELIVERY_MAX_ATTEMPTS))
    assert total < DELIVERY_DEADLINE_SECONDS


def test_attempts_below_the_limit_are_deferred(tmp_path: Path) -> None:
    """Versuch 1..4 verschieben; die Nachricht bleibt in der Warteschlange."""
    clock = Clock(datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
    database = StateDB(tmp_path / "state.db")
    outbox = OutboxMessenger(database, AlwaysFailingMessenger(), now=clock)

    database.claim("<zaeh@example>")
    database.mark_status("<zaeh@example>", MailState.CHECKED)
    outbox.send(
        DigestMessage(parts=["Inhalt"], importance="normal", is_warning=False,
                      dedupe_key="<zaeh@example>")
    )
    assert outbox.pending == 1

    for attempt in range(2, DELIVERY_MAX_ATTEMPTS):
        clock.now += timedelta(seconds=next_delivery_delay(attempt - 1))
        stats = outbox.flush()
        assert stats.deferred == 1, f"Versuch {attempt} darf noch nicht aufgeben"
        assert outbox.pending == 1
    record = database.get("<zaeh@example>")
    assert record is not None and record.status is MailState.CHECKED
    database.close()


def test_the_fifth_attempt_gives_up(tmp_path: Path) -> None:
    """Auf `DELIVERY_MAX_ATTEMPTS` genau wird aufgegeben und die Mail `failed`."""
    clock = Clock(datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
    database = StateDB(tmp_path / "state.db")
    outbox = OutboxMessenger(database, AlwaysFailingMessenger(), now=clock)

    database.claim("<auf@example>")
    database.mark_status("<auf@example>", MailState.CHECKED)
    outbox.send(
        DigestMessage(parts=["Inhalt"], importance="normal", is_warning=False,
                      dedupe_key="<auf@example>")
    )
    for attempt in range(2, DELIVERY_MAX_ATTEMPTS + 1):
        clock.now += timedelta(seconds=next_delivery_delay(attempt - 1))
        stats = outbox.flush()
    assert stats.abandoned == 1
    assert outbox.pending == 0
    record = database.get("<auf@example>")
    assert record is not None
    assert record.status is MailState.FAILED
    assert record.error_class == "delivery_failed"
    database.close()


@pytest.mark.parametrize(
    ("age_seconds", "expect_abandoned"),
    [
        (DELIVERY_DEADLINE_SECONDS - 1, False),
        (DELIVERY_DEADLINE_SECONDS, True),
        (DELIVERY_DEADLINE_SECONDS + 1, True),
    ],
)
def test_one_hour_deadline_is_exact(
    tmp_path: Path, age_seconds: float, expect_abandoned: bool
) -> None:
    """Die Stunden-Schranke greift **auf** 3600 s, nicht erst danach (ADR-048)."""
    start = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    clock = Clock(start)
    database = StateDB(tmp_path / "state.db")
    outbox = OutboxMessenger(database, AlwaysFailingMessenger(), now=clock)

    database.claim("<frist@example>")
    database.mark_status("<frist@example>", MailState.CHECKED)
    outbox.send(
        DigestMessage(parts=["Inhalt"], importance="normal", is_warning=False,
                      dedupe_key="<frist@example>")
    )
    clock.now = start + timedelta(seconds=age_seconds)
    stats = outbox.flush()

    assert (stats.abandoned == 1) is expect_abandoned
    assert (outbox.pending == 0) is expect_abandoned
    database.close()


def test_low_digest_failure_never_marks_a_mail_failed(tmp_path: Path) -> None:
    """Der Sammel-Digest gehört zu keiner Mail — sein Scheitern ändert keinen Mail-Status."""
    clock = Clock(datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
    database = StateDB(tmp_path / "state.db")
    outbox = OutboxMessenger(database, AlwaysFailingMessenger(), now=clock)

    database.claim("<unberuehrt@example>")
    database.mark_status("<unberuehrt@example>", MailState.CHECKED)
    outbox.send(
        DigestMessage(parts=["Sammel"], importance="low", is_warning=False,
                      dedupe_key=LOW_DIGEST_DEDUPE_KEY)
    )
    clock.now += timedelta(seconds=DELIVERY_DEADLINE_SECONDS)
    assert outbox.flush().abandoned == 1
    record = database.get("<unberuehrt@example>")
    assert record is not None and record.status is MailState.CHECKED
    database.close()


def test_restart_resends_a_committed_but_unconfirmed_message(tmp_path: Path) -> None:
    """Absturz zwischen Commit und Bestätigung: der nächste Lauf stellt zu (F-OPS-3)."""
    path = tmp_path / "state.db"
    start = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    database = StateDB(path)
    database.claim("<absturz@example>")
    database.mark_status("<absturz@example>", MailState.CHECKED)
    failing = OutboxMessenger(database, AlwaysFailingMessenger(), now=lambda: start)
    failing.send(
        DigestMessage(parts=["Wichtiger Inhalt"], importance="high", is_warning=False,
                      dedupe_key="<absturz@example>")
    )
    database.close()  # „Absturz"

    database = StateDB(path)
    messenger = CollectingMessenger()
    outbox = OutboxMessenger(
        database, messenger, now=lambda: start + timedelta(seconds=120)
    )
    stats = outbox.flush()
    assert stats.delivered == 1
    assert messenger.sent[0].parts == ["Wichtiger Inhalt"]
    assert messenger.sent[0].importance == "high"
    record = database.get("<absturz@example>")
    assert record is not None and record.status is MailState.DELIVERED
    assert dedupe_hash("<absturz@example>") == record.message_id_hash
    database.close()
