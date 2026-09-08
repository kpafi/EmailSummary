"""Ende-zu-Ende-Integrationstest des Daemons (WP8, Akzeptanzkriterium PLAN WP8).

Aufbau: **echter** Ingest-Ablauf (`IngestService` + `poll_once`) über einer
Postfach-Attrappe, die den vollständigen WP3-Korpus (25 `.eml`) liefert; **echter**
Sanitizer, **echte** Pipeline, **echter** Composer/Output-Sanitizer, **echte** State-DB.
Attrappen gibt es nur dort, wo das Netz begänne: LLM (Summarizer/Kritiker) und Messenger.

Geprüft werden:

* Einmal-Lauf verarbeitet das Korpus-Postfach Ende-zu-Ende (Zustellung + Low-Queue),
* F-SUM-5: `low`-Mails werden gesammelt und einmal täglich als eine Nachricht zugestellt,
* F-CRIT-2/T14: Kritiker-`high` landet nie im Sammel-Digest,
* I6/F-OPS-3: LLM-Dauerfehler ⇒ Metadaten-Notiz statt Inhalt,
* ADR-008: Absturz mitten in der Pipeline ⇒ Wiederanlauf verliert nichts und verarbeitet
  nichts doppelt (außer der ausdrücklich erlaubten Wiederholung eines `checked`-Versands),
* I3: keine zugestellte Nachricht enthält eine klickbare URL.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from imap_tools import MailMessage

from maildigest.config import Config, load_config_from_dict
from maildigest.ingest.imap_client import ImapClient
from maildigest.llm.base import LLMTimeout
from maildigest.models import CriticVerdict, DigestMessage, SanitizedMail, Summary
from maildigest.runner import Runner, build_runner
from maildigest.state.db import MailState, StateDB

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

#: Muster, die in keiner zugestellten Nachricht vorkommen dürfen (I3).
FORBIDDEN = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"</?[a-zA-Z][^>]*>"),
)


# --- Postfach-Attrappe ---------------------------------------------------------------------


class FakeRawClient:
    """Ersatz für `imaplib.IMAP4_SSL` — nur `UID STORE`/`UID MOVE` werden benutzt (CT-9)."""

    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self, box: FakeMailBox) -> None:
        self._box = box

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command.upper() == "STORE":
            self._box.flagged.append(str(args[0]))
            self._box.seen.add(str(args[0]))
        elif command.upper() != "MOVE":  # pragma: no cover - kein weiteres Kommando erwartet
            raise AssertionError(f"unerwartetes IMAP-Kommando: {command}")
        return "OK", [b""]


class FakeMailBox:
    """Postfach-Attrappe: liefert Nachrichten, merkt Flags/Moves, kann nicht löschen."""

    def __init__(self, messages: list[MailMessage]) -> None:
        self.messages = messages
        self.seen: set[str] = set()
        self.flagged: list[str] = []
        # CT-9: MailDigest setzt rohe UID-Kommandos ab, weil `MailBox.flag()`/`move()`
        # intern expungen. Der Fake bildet deshalb `imaplib`-Ebene nach.
        self.client = FakeRawClient(self)

    def login(self, username: str, password: str, initial_folder: str | None = "INBOX") -> None:
        return None

    def logout(self) -> None:
        return None

    def fetch(self, criteria: Any = "ALL", **kwargs: Any) -> list[MailMessage]:
        return [msg for msg in self.messages if msg.uid not in self.seen]

    def flag(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("MailBox.flag() expunged — verboten (F-ING-1, CT-9)")

    def move(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("move ist in diesem Test nicht konfiguriert")

    def delete(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("MailDigest darf Mails niemals löschen (F-ING-1)")


def corpus_messages() -> list[MailMessage]:
    """Baut aus allen Korpus-`.eml` IMAP-Nachrichten mit fortlaufender UID."""
    messages = []
    for index, path in enumerate(sorted(CORPUS_DIR.glob("*.eml")), start=1):
        raw = path.read_bytes().replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n")
        messages.append(MailMessage([(f"1 (UID {index} FLAGS ())".encode(), raw), b")"]))
    return messages


# --- LLM-/Messenger-Attrappen ---------------------------------------------------------------


class ScriptedSummarizer:
    """Summarizer-Attrappe: Wichtigkeit nach Betreff, optional mit Dauerfehler."""

    def __init__(self, *, fail_for: str = "", low_marker: str = "Newsletter") -> None:
        self.calls: list[str] = []
        self._fail_for = fail_for
        self._low_marker = low_marker

    def summarize(self, mail: SanitizedMail) -> Summary:
        self.calls.append(mail.dedupe_key)
        if self._fail_for and self._fail_for in mail.subject:
            raise LLMTimeout("Attrappe: Provider antwortet nicht")
        low = self._low_marker.lower() in mail.subject.lower()
        return Summary(
            headline=mail.subject[:100] or "(kein Betreff)",
            summary_text="Zusammenfassung der Mail.",
            importance="low" if low else "normal",
            importance_reason="Attrappe",
            category="newsletter" if low else "sonstiges",
            attachment_summaries={},
            injection_suspected=False,
        )


class ScriptedCritic:
    """Kritiker-Attrappe: `high` für Mails, deren Betreff das Stichwort enthält."""

    def __init__(self, *, high_marker: str = "") -> None:
        self._high_marker = high_marker
        self.calls = 0

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        self.calls += 1
        risky = bool(self._high_marker) and self._high_marker.lower() in mail.subject.lower()
        return CriticVerdict(
            phishing_risk="high" if risky else "none",
            risk_reasons=["Zahlungsaufforderung", "Absender-Domain weicht ab"] if risky else [],
            summary_accurate=True,
            notes="",
        )


class CollectingMessenger:
    """Messenger-Attrappe mit gezielt steuerbaren Ausfällen."""

    def __init__(self, *, fail_on: set[int] | None = None, crash_on: set[int] | None = None):
        self.sent: list[DigestMessage] = []
        self.attempts = 0
        self._fail_on = fail_on or set()
        self._crash_on = crash_on or set()

    def send(self, message: DigestMessage) -> None:
        self.attempts += 1
        if self.attempts in self._crash_on:
            # Simuliert einen harten Prozessabbruch mitten in der Pipeline: BaseException
            # wird von keiner Fail-closed-Schicht gefangen.
            raise KeyboardInterrupt("Absturz (Attrappe)")
        if self.attempts in self._fail_on:
            raise RuntimeError("Messenger nicht erreichbar (Attrappe)")
        self.sent.append(message)

    @property
    def texts(self) -> list[str]:
        return ["\n".join(message.parts) for message in self.sent]


# --- Aufbau -------------------------------------------------------------------------------


def make_config(**general: Any) -> Config:
    return load_config_from_dict(
        {
            "general": general,
            "imap": {
                "host": "imap.example.org",
                "username": "mirror@example.org",
                "password": "geheim",
            },
            "llm": {"model": "modell", "api_key": "sk-test"},
            "messenger": {
                "active": "telegram",
                "telegram": {"token": "1:abc", "chat_id": "42"},
            },
        },
        env={},
    )


def make_runner(
    db: StateDB,
    mailbox: FakeMailBox,
    *,
    summarizer: Any,
    critic: Any,
    messenger: Any,
    now: Any = None,
    **general: Any,
) -> Runner:
    """Verdrahtet den echten Runner über der Postfach-Attrappe."""
    config = make_config(**general)
    runner = build_runner(
        config,
        db=db,
        summarizer=summarizer,
        critic=critic,
        messenger=messenger,
        client_factory=lambda: ImapClient(config.imap, mailbox_factory=lambda: mailbox),
        sleep=lambda _seconds: None,
    )
    if now is not None:
        runner.now = now
    return runner


@pytest.fixture
def mailbox() -> FakeMailBox:
    return FakeMailBox(corpus_messages())


# --- Tests --------------------------------------------------------------------------------


def test_single_run_processes_the_whole_corpus(mailbox: FakeMailBox) -> None:
    """Ein `run --once` verarbeitet das Korpus-Postfach Ende-zu-Ende."""
    with StateDB(":memory:") as db:
        summarizer = ScriptedSummarizer()
        messenger = CollectingMessenger()
        runner = make_runner(
            db,
            mailbox,
            summarizer=summarizer,
            critic=ScriptedCritic(),
            messenger=messenger,
            now=lambda: datetime(2026, 9, 2, 12, 0),
        )
        stats = runner.run_once()

        assert stats.ingest.fetched == len(mailbox.messages)
        assert stats.ingest.failed == 0
        # Jede Mail hat einen Endstatus; nichts bleibt in einem Zwischenstand hängen.
        endstates = sum(
            db.count_by_status(state)
            for state in (MailState.DELIVERED, MailState.SKIPPED_LOW, MailState.FAILED)
        )
        assert endstates == len(mailbox.messages)
        assert db.count_by_status(MailState.PENDING) == 0
        assert db.count_by_status(MailState.CHECKED) == 0
        assert db.outbox_size() == 0
        # Zugestellte Nachrichten + Low-Queue decken zusammen alle Mails ab.
        assert len(messenger.sent) + len(db.low_digest_entries()) == len(mailbox.messages)
        # Alle Mails wurden als gelesen markiert, keine gelöscht (F-ING-1).
        assert len(mailbox.flagged) == len(mailbox.messages)


def test_no_delivered_message_contains_a_clickable_link(mailbox: FakeMailBox) -> None:
    """I3: Auch über den Angriffs-Korpus überlebt keine klickbare URL bis zur Zustellung."""
    with StateDB(":memory:") as db:
        messenger = CollectingMessenger()
        runner = make_runner(
            db,
            mailbox,
            summarizer=ScriptedSummarizer(low_marker="unmöglich"),  # alles zustellen
            critic=ScriptedCritic(),
            messenger=messenger,
        )
        runner.run_once()
        assert messenger.texts
        for text in messenger.texts:
            for pattern in FORBIDDEN:
                assert pattern.search(text) is None, text


def test_second_run_processes_nothing_twice(mailbox: FakeMailBox) -> None:
    """F-ING-2: Ein zweiter Lauf über dasselbe Postfach verarbeitet nichts erneut."""
    with StateDB(":memory:") as db:
        summarizer = ScriptedSummarizer()
        messenger = CollectingMessenger()
        runner = make_runner(
            db, mailbox, summarizer=summarizer, critic=ScriptedCritic(), messenger=messenger
        )
        runner.run_once()
        calls_after_first = len(summarizer.calls)
        sent_after_first = len(messenger.sent)

        mailbox.seen.clear()  # Nutzer markiert alles wieder als ungelesen
        stats = runner.run_once()

        assert stats.ingest.duplicates == len(mailbox.messages)
        assert len(summarizer.calls) == calls_after_first
        assert len(messenger.sent) == sent_after_first


def test_low_mails_are_collected_and_delivered_once_a_day(mailbox: FakeMailBox) -> None:
    """F-SUM-5: `low` sammeln, ab `low_digest_time` als eine Nachricht zustellen."""
    clock = [datetime(2026, 9, 2, 12, 0)]
    with StateDB(":memory:") as db:
        messenger = CollectingMessenger()
        runner = make_runner(
            db,
            mailbox,
            summarizer=ScriptedSummarizer(low_marker="Newsletter"),
            critic=ScriptedCritic(),
            messenger=messenger,
            now=lambda: clock[0],
            low_digest_time="18:00",
        )
        runner.run_once()
        queued = db.low_digest_entries()
        assert queued, "Der Korpus enthält Newsletter-artige Mails"
        assert db.count_by_status(MailState.SKIPPED_LOW) == len(queued)
        einzelzustellungen = len(messenger.sent)

        clock[0] = datetime(2026, 9, 2, 18, 30)
        runner.run_once()
        assert len(messenger.sent) == einzelzustellungen + 1
        digest = messenger.texts[-1]
        assert f"{len(queued)} unwichtige Mails" in digest
        assert db.low_digest_entries() == []
        for pattern in FORBIDDEN:
            assert pattern.search(digest) is None


def test_high_risk_mail_never_lands_in_the_low_digest(mailbox: FakeMailBox) -> None:
    """F-CRIT-2/T14: Kritiker-`high` erzwingt Einzelzustellung mit Banner."""
    with StateDB(":memory:") as db:
        messenger = CollectingMessenger()
        runner = make_runner(
            db,
            mailbox,
            # Alle Mails wären `low` — nur der Kritiker rettet die Phishing-Mail heraus.
            summarizer=ScriptedSummarizer(low_marker=""),
            critic=ScriptedCritic(high_marker="Rechnung"),
            messenger=messenger,
            low_digest_time="18:00",
        )
        runner.run_once()

        banner_messages = [text for text in messenger.texts if "PHISHING-VERDACHT" in text]
        assert banner_messages, "mindestens eine Rechnungs-Mail im Korpus"
        for text in banner_messages:
            assert "Rechnung" in text
        # Keine der gewarnten Mails steht in der Sammel-Warteschlange.
        queued_headlines = [entry.headline for entry in db.low_digest_entries()]
        assert not [line for line in queued_headlines if "Rechnung" in line]


def test_permanent_llm_failure_ends_as_metadata_notice(mailbox: FakeMailBox) -> None:
    """I6/F-OPS-3: Drei Fehlversuche ⇒ Notiz statt Inhalt, Status `failed`."""
    with StateDB(":memory:") as db:
        summarizer = ScriptedSummarizer(fail_for="Rechnung")
        messenger = CollectingMessenger()
        runner = make_runner(
            db, mailbox, summarizer=summarizer, critic=ScriptedCritic(), messenger=messenger
        )
        runner.run_once()

        notices = [
            text
            for text in messenger.texts
            if "konnte nicht sicher verarbeitet werden" in text
        ]
        assert notices
        assert all("llm_timeout" in text for text in notices)
        assert db.count_by_status(MailState.FAILED) == len(notices)
        # Jede betroffene Mail wurde dreimal versucht (ARCHITECTURE §6).
        failed_keys = [key for key in summarizer.calls if summarizer.calls.count(key) > 1]
        assert failed_keys and summarizer.calls.count(failed_keys[0]) == 3


def test_crash_between_commit_and_delivery_loses_nothing(mailbox: FakeMailBox) -> None:
    """ADR-008: Absturz nach dem `checked`-Commit ⇒ Wiederanlauf stellt zu, nichts doppelt."""
    with StateDB(":memory:") as db:
        summarizer = ScriptedSummarizer(low_marker="unmöglich")  # alles zustellen
        crashing = CollectingMessenger(crash_on={3})
        runner = make_runner(
            db, mailbox, summarizer=summarizer, critic=ScriptedCritic(), messenger=crashing
        )
        # Direkt auf dem Ingest, damit kein `finally` mehr zum Zuge kommt — ein echter
        # Prozessabbruch räumt auch nicht auf.
        with pytest.raises(KeyboardInterrupt):
            runner.ingest.run_once()

        # Zustand nach dem Absturz: die betroffene Mail ist `checked` und liegt committet
        # in der Zustell-Warteschlange — sie kann nicht mehr verloren gehen.
        assert db.count_by_status(MailState.CHECKED) == 1
        assert db.outbox_size() == 1
        crashed_calls = len(summarizer.calls)

        # Wiederanlauf mit frischem Prozess (neuer Runner, dieselbe DB und dasselbe Postfach).
        healthy = CollectingMessenger()
        restarted = make_runner(
            db, mailbox, summarizer=summarizer, critic=ScriptedCritic(), messenger=healthy
        )
        stats = restarted.run_once()

        assert stats.delivery.delivered == 1  # genau die abgestürzte Nachricht
        assert db.count_by_status(MailState.CHECKED) == 0
        assert db.outbox_size() == 0
        # Die bereits verarbeiteten Mails werden nicht erneut durch die LLM-Stufen geschickt:
        # die abgeschlossenen sind im Postfach als gelesen markiert, die abgestürzte taucht
        # wieder auf und wird über die State-DB als Duplikat erkannt (F-ING-2).
        assert stats.ingest.duplicates == 1
        assert len(summarizer.calls) == crashed_calls + stats.ingest.processed
        # Nichts geht verloren: Jede Mail hat am Ende einen Endstatus.
        assert (
            db.count_by_status(MailState.DELIVERED)
            + db.count_by_status(MailState.SKIPPED_LOW)
            + db.count_by_status(MailState.FAILED)
            == len(mailbox.messages)
        )


def test_delivery_failure_keeps_the_mail_in_checked_and_retries(mailbox: FakeMailBox) -> None:
    """Messenger-Ausfall: Status bleibt `checked`, die Warteschlange stellt später zu."""
    with StateDB(":memory:") as db:
        messenger = CollectingMessenger(fail_on={1})
        runner = make_runner(
            db,
            mailbox,
            summarizer=ScriptedSummarizer(low_marker="unmöglich"),
            critic=ScriptedCritic(),
            messenger=messenger,
        )
        runner.run_once()
        assert db.count_by_status(MailState.CHECKED) == 1
        assert db.outbox_size() == 1

        # Nach Ablauf des Backoffs wird erneut zugestellt (Zeit vorspulen).
        db._conn.execute("UPDATE outbox SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()
        stats = runner.outbox.flush()
        assert stats.delivered == 1
        assert db.count_by_status(MailState.CHECKED) == 0
        assert db.outbox_size() == 0
