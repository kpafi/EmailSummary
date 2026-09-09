"""Unit-Tests der Orchestrierung (`runner.py`, WP8).

Prüffläche: LLM-Retry-Politik (ARCHITECTURE §6), Statusführung inkl. `checked` vor dem
Versand (ADR-008), Einreihen in die Sammel-Digest-Warteschlange mit
Output-Sanitisierung (ADR-049) und die tägliche Digest-Planung (F-SUM-5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from maildigest.config import Config, load_config_from_dict
from maildigest.llm.base import LLMInvalidResponse, LLMTimeout
from maildigest.models import (
    CriticVerdict,
    DigestMessage,
    FailureNotice,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.pipeline import Delivered, FailedNotice, QueuedLow
from maildigest.runner import (
    LLM_MAX_ATTEMPTS,
    RetryingCritic,
    RetryingSummarizer,
    Runner,
    StatusRecorder,
    build_runner,
)
from maildigest.state.db import MailState, StateDB


def make_config(**general: Any) -> Config:
    """Minimal gültige Config mit optionalen `[general]`-Werten."""
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


def make_summary(**overrides: Any) -> Summary:
    data: dict[str, Any] = {
        "headline": "Newsletter der Woche",
        "summary_text": "Kurzer Text.",
        "importance": "low",
        "importance_reason": "Newsletter",
        "category": "newsletter",
        "attachment_summaries": {},
        "injection_suspected": False,
    }
    data.update(overrides)
    return Summary(**data)


def make_mail() -> SanitizedMail:
    return SanitizedMail(
        dedupe_key="<a@x>",
        from_display="Absender",
        from_domain="example.org",
        subject="Betreff",
        date=None,
        body_text="Text",
        sanitization_report=SanitizationReport(),
    )


class SendingMessenger:
    """Messenger-Attrappe, die alles annimmt."""

    def __init__(self) -> None:
        self.sent: list[DigestMessage] = []

    def send(self, message: DigestMessage) -> None:
        self.sent.append(message)


class FailingMessenger:
    """Messenger-Attrappe, die immer scheitert."""

    def send(self, message: DigestMessage) -> None:
        raise RuntimeError("kein Netz")


def build_test_runner(
    db: StateDB,
    *,
    messenger: Any,
    summarizer: Any = None,
    critic: Any = None,
    now: Any = None,
    **general: Any,
) -> Runner:
    """Baut einen Runner mit Attrappen für LLM-Stufen und Messenger."""
    runner = build_runner(
        make_config(**general),
        db=db,
        summarizer=summarizer if summarizer is not None else _StubSummarizer(),
        critic=critic if critic is not None else _StubCritic(),
        messenger=messenger,
        sleep=lambda _seconds: None,
    )
    if now is not None:
        runner.now = now
    return runner


class _StubSummarizer:
    def summarize(self, mail: SanitizedMail) -> Summary:
        return make_summary()


class _StubCritic:
    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        return CriticVerdict(phishing_risk="none", risk_reasons=[], summary_accurate=True)


# --- Retry-Politik -----------------------------------------------------------------------


class FlakySummarizer:
    """Scheitert die ersten `failures` Aufrufe mit einem wiederholbaren Fehler."""

    def __init__(self, failures: int, error: type[Exception] = LLMTimeout) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def summarize(self, mail: SanitizedMail) -> Summary:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise self.error("Attrappe")
        return make_summary()


def test_summarizer_retry_succeeds_on_the_third_attempt() -> None:
    """Zwei Timeouts, dann Erfolg — genau drei Versuche (ARCHITECTURE §6)."""
    inner = FlakySummarizer(failures=2)
    agent = RetryingSummarizer(inner, sleep=lambda _s: None)
    assert agent.summarize(make_mail()).importance == "low"
    assert inner.calls == LLM_MAX_ATTEMPTS


def test_summarizer_retry_gives_up_after_three_attempts() -> None:
    """Nach dem dritten Fehlversuch fliegt der Fehler weiter (⇒ fail-closed, I6)."""
    inner = FlakySummarizer(failures=99)
    agent = RetryingSummarizer(inner, sleep=lambda _s: None)
    with pytest.raises(LLMTimeout):
        agent.summarize(make_mail())
    assert inner.calls == LLM_MAX_ATTEMPTS


def test_invalid_response_is_not_retried() -> None:
    """Schema-Fehler sind in `llm/schema.py` bereits repariert worden (ADR-050)."""
    inner = FlakySummarizer(failures=99, error=LLMInvalidResponse)
    agent = RetryingSummarizer(inner, sleep=lambda _s: None)
    with pytest.raises(LLMInvalidResponse):
        agent.summarize(make_mail())
    assert inner.calls == 1


def test_critic_retry_uses_the_same_policy() -> None:
    """Der Kritiker wird nach derselben Politik wiederholt."""

    class FlakyCritic:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
            self.calls += 1
            if self.calls < 3:
                raise LLMTimeout("Attrappe")
            return CriticVerdict(phishing_risk="none")

    inner = FlakyCritic()
    verdict = RetryingCritic(inner, sleep=lambda _s: None).review(make_mail(), make_summary())
    assert verdict.phishing_risk == "none"
    assert inner.calls == 3


def test_delays_are_not_slept_away_in_tests() -> None:
    """Die Wartezeiten sind injizierbar — der Retry blockiert Tests nicht."""
    slept: list[float] = []
    inner = FlakySummarizer(failures=2)
    RetryingSummarizer(inner, sleep=slept.append).summarize(make_mail())
    assert len(slept) == 2
    assert all(delay > 0 for delay in slept)


# --- Statusführung -----------------------------------------------------------------------


def test_status_recorder_writes_the_intermediate_states() -> None:
    """`sanitized`/`summarized`/`checked` landen in der DB."""
    with StateDB(":memory:") as db:
        db.claim("<a@x>")
        recorder = StatusRecorder(db)
        for state, expected in (
            ("sanitized", MailState.SANITIZED),
            ("summarized", MailState.SUMMARIZED),
            ("checked", MailState.CHECKED),
        ):
            recorder.record("<a@x>", state)  # type: ignore[arg-type]
            record = db.get("<a@x>")
            assert record is not None and record.status is expected


def test_delivered_result_is_booked_as_delivered() -> None:
    """Zugestellte Mail ohne Rest in der Warteschlange ⇒ Status `delivered`."""
    with StateDB(":memory:") as db:
        messenger = SendingMessenger()
        runner = build_test_runner(db, messenger=messenger)
        db.claim("<a@x>")
        runner._record_result(
            Delivered(
                dedupe_key="<a@x>",
                message=DigestMessage(
                    parts=["x"], importance="normal", is_warning=False, dedupe_key="<a@x>"
                ),
            )
        )
        record = db.get("<a@x>")
        assert record is not None and record.status is MailState.DELIVERED


def test_queued_delivery_keeps_the_mail_in_checked() -> None:
    """Liegt die Nachricht noch in der Warteschlange, bleibt es bei `checked` (ADR-008)."""
    with StateDB(":memory:") as db:
        runner = build_test_runner(db, messenger=FailingMessenger())
        db.claim("<a@x>")
        db.mark_status("<a@x>", MailState.CHECKED)
        runner.deps.messenger.send(
            DigestMessage(parts=["x"], importance="normal", is_warning=False, dedupe_key="<a@x>")
        )
        runner._record_result(
            Delivered(
                dedupe_key="<a@x>",
                message=DigestMessage(
                    parts=["x"], importance="normal", is_warning=False, dedupe_key="<a@x>"
                ),
            )
        )
        record = db.get("<a@x>")
        assert record is not None and record.status is MailState.CHECKED
        assert db.outbox_size() == 1


def test_failed_notice_is_booked_with_its_reason_class() -> None:
    """Der Fail-closed-Pfad hinterlässt Status und grobe Fehlerklasse."""
    with StateDB(":memory:") as db:
        runner = build_test_runner(db, messenger=SendingMessenger())
        db.claim("<a@x>")
        runner._record_result(
            FailedNotice(
                notice=FailureNotice(
                    dedupe_key="<a@x>",
                    from_domain="example.org",
                    subject_sanitized="Betreff",
                    stage="summarize",
                    reason_class="llm_timeout",
                ),
                notice_delivered=True,
            )
        )
        record = db.get("<a@x>")
        assert record is not None
        assert record.status is MailState.FAILED
        assert record.error_class == "llm_timeout"


# --- Sammel-Digest -----------------------------------------------------------------------


def test_low_mail_is_queued_output_sanitized() -> None:
    """In die Warteschlange kommt nur Text, der Schicht 6 passiert hat (ADR-049)."""
    with StateDB(":memory:") as db:
        runner = build_test_runner(db, messenger=SendingMessenger())
        db.claim("<a@x>")
        runner._record_result(
            QueuedLow(
                dedupe_key="<a@x>",
                from_domain="newsletter.example.org",
                summary=make_summary(headline="Jetzt klicken: https://boese.example/gewinn"),
            )
        )
        entry = db.low_digest_entries()[0]
        assert "https://" not in entry.headline
        assert "boese.example/gewinn" not in entry.headline
        # Das Brechen der Punkte macht der Nachbrenner beim Bau der Nachricht (WP7);
        # in der Warteschlange steht die Domain unverändert, aber markup-frei.
        assert entry.from_domain == "newsletter.example.org"
        record = db.get("<a@x>")
        assert record is not None and record.status is MailState.SKIPPED_LOW


def test_low_digest_is_sent_after_the_configured_time() -> None:
    """Ab `low_digest_time` genau einmal am Tag (F-SUM-5)."""
    with StateDB(":memory:") as db:
        messenger = SendingMessenger()
        clock = [datetime(2026, 9, 2, 17, 59)]
        runner = build_test_runner(
            db, messenger=messenger, now=lambda: clock[0], low_digest_time="18:00"
        )
        db.queue_low("<a@x>", headline="Newsletter A", category="newsletter",
                     from_domain="a.de")
        db.queue_low("<b@x>", headline="Newsletter B", category="newsletter",
                     from_domain="b.de")

        assert runner.maybe_send_low_digest() is False
        assert messenger.sent == []

        clock[0] = datetime(2026, 9, 2, 18, 0)
        assert runner.maybe_send_low_digest() is True
        text = "\n".join(messenger.sent[0].parts)
        assert "2 low-priority mails" in text
        assert "Newsletter A" in text
        assert "a[.]de" in text
        assert db.low_digest_entries() == []

        # Kein zweiter Digest am selben Tag, auch nicht mit neuen Einträgen.
        db.queue_low("<c@x>", headline="C", category="newsletter", from_domain="c.de")
        assert runner.maybe_send_low_digest() is False
        assert len(messenger.sent) == 1

        # Am nächsten Tag wieder.
        clock[0] = datetime(2026, 9, 3, 18, 5)
        assert runner.maybe_send_low_digest() is True
        assert len(messenger.sent) == 2


def test_empty_queue_produces_no_message() -> None:
    """Ohne gesammelte Mails wird nichts zugestellt, der Tag gilt trotzdem als erledigt."""
    with StateDB(":memory:") as db:
        messenger = SendingMessenger()
        runner = build_test_runner(
            db,
            messenger=messenger,
            now=lambda: datetime(2026, 9, 2, 20, 0),
            low_digest_time="18:00",
        )
        assert runner.maybe_send_low_digest() is False
        assert messenger.sent == []
        assert db.meta_get("last_low_digest_date") == "2026-09-02"


def test_digest_message_is_not_lost_when_delivery_fails() -> None:
    """Scheitert die Zustellung, trägt die Warteschlange den Digest weiter."""
    with StateDB(":memory:") as db:
        runner = build_test_runner(
            db,
            messenger=FailingMessenger(),
            now=lambda: datetime(2026, 9, 2, 18, 0),
            low_digest_time="18:00",
        )
        db.queue_low("<a@x>", headline="A", category="newsletter", from_domain="a.de")
        assert runner.maybe_send_low_digest() is True
        assert db.outbox_size() == 1
        assert db.low_digest_entries() == []


# --- Dauerbetrieb ------------------------------------------------------------------------


def test_run_forever_stops_after_the_current_cycle() -> None:
    """`stop()` beendet den Loop; der laufende Zyklus wird zu Ende geführt."""
    with StateDB(":memory:") as db:
        runner = build_test_runner(db, messenger=SendingMessenger())
        cycles = {"n": 0}

        def fake_run_once() -> Any:
            cycles["n"] += 1
            if cycles["n"] == 2:
                runner.stop()
            from maildigest.ingest.imap_client import IngestStats

            return IngestStats(fetched=1, processed=1)

        runner.ingest.run_once = fake_run_once  # type: ignore[method-assign]
        runner.sleep = lambda _seconds: None
        stats = runner.run_forever(handle_signals=False)

        assert cycles["n"] == 2
        assert stats.cycles == 2
        assert stats.ingest.processed == 2
        assert runner.stopped is True


def test_run_forever_reconnects_after_an_ingest_error() -> None:
    """Ein IMAP-Fehler beendet den Loop nicht, sondern führt zu Backoff + Reconnect."""
    from maildigest.ingest.imap_client import ImapConnectionError, IngestStats

    with StateDB(":memory:") as db:
        runner = build_test_runner(db, messenger=SendingMessenger())
        attempts = {"n": 0}
        waits: list[float] = []

        def flaky_run_once() -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ImapConnectionError("kein Server (Attrappe)")
            runner.stop()
            return IngestStats(fetched=0)

        runner.ingest.run_once = flaky_run_once  # type: ignore[method-assign]
        runner.sleep = waits.append
        runner.run_forever(handle_signals=False)

        assert attempts["n"] == 2
        assert waits and waits[0] > 0  # Backoff nach dem Fehlversuch


def test_run_once_flushes_the_queue_even_when_imap_fails() -> None:
    """Ein IMAP-Ausfall darf eine fertige Nachricht nicht in der Warteschlange festhalten."""
    from maildigest.ingest.imap_client import ImapConnectionError

    with StateDB(":memory:") as db:
        messenger = SendingMessenger()
        runner = build_test_runner(db, messenger=messenger)
        db.enqueue_outbox(
            "<a@x>", kind="mail", parts=["Wartender Text"], importance="normal",
            is_warning=False,
        )

        def failing_run_once() -> Any:
            raise ImapConnectionError("kein Server (Attrappe)")

        runner.ingest.run_once = failing_run_once  # type: ignore[method-assign]
        with pytest.raises(ImapConnectionError):
            runner.run_once()

        assert [part for message in messenger.sent for part in message.parts] == [
            "Wartender Text"
        ]
        assert db.outbox_size() == 0


def test_signal_handlers_are_installed_and_restored() -> None:
    """SIGINT/SIGTERM stoppen den Loop und die vorherigen Handler kommen zurück (ADR-051)."""
    import signal as signal_module

    with StateDB(":memory:") as db:
        runner = build_test_runner(db, messenger=SendingMessenger())
        before = (
            signal_module.getsignal(signal_module.SIGINT),
            signal_module.getsignal(signal_module.SIGTERM),
        )
        installed: list[Any] = []

        def one_cycle() -> Any:
            from maildigest.ingest.imap_client import IngestStats

            installed.append(signal_module.getsignal(signal_module.SIGTERM))
            # Der Handler tut genau das, was ein echtes Signal auslösen würde.
            installed[-1](signal_module.SIGTERM, None)
            return IngestStats()

        runner.ingest.run_once = one_cycle  # type: ignore[method-assign]
        runner.sleep = lambda _seconds: None
        runner.run_forever(handle_signals=True)

        assert runner.stopped is True
        assert installed[0] not in before  # während des Laufs ein eigener Handler
        assert (
            signal_module.getsignal(signal_module.SIGINT),
            signal_module.getsignal(signal_module.SIGTERM),
        ) == before
