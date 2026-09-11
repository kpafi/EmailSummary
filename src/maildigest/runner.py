"""Verdrahtung und Betrieb: aus der Config den lauffähigen Daemon bauen (WP8).

Dieses Modul steckt die in WP2-WP7 gebauten Stufen zusammen und führt den State
(docs/ARCHITECTURE.md §2, §6):

* :func:`build_runner` baut Sanitizer, Summarizer, Kritiker, Composer, Messenger und
  State-DB aus der :class:`~maildigest.config.Config` und übergibt sie an
  :func:`maildigest.pipeline.process_mail`.
* :class:`Runner` bietet :meth:`Runner.run_once` (Cron/`run --once`) und
  :meth:`Runner.run_forever` (Daemon mit sauberem Shutdown auf SIGINT/SIGTERM) — die CLI
  aus WP9 ruft nur noch diese beiden Methoden auf.
* Statusführung: `pending` (Ingest-Claim) → `sanitized` → `summarized` → `checked` →
  `delivered` | `skipped_low` | `failed`. `checked` wird **vor** dem Versand committet
  (ADR-008); die Bestätigung auf `delivered` gibt erst die Zustell-Warteschlange
  (:mod:`maildigest.delivery`).
* Retry-Politik (docs/ARCHITECTURE.md §6, ADR-050): LLM-Stufen 3 Versuche mit Backoff,
  dann fail-closed-Notiz; Zustellung 5 Versuche über höchstens eine Stunde aus der DB.

Sicherheits-Design dieses Moduls:

* **I5/NF-5:** Geloggt werden nur gekürzter Dedupe-Hash, Absender-Domain, Status, Zähler
  und Exception-Klassennamen — nie Betreff, Body oder Zugangsdaten. Tracebacks nur bei
  `log_level = "DEBUG"` (ADR-047).
* **I3/I4:** In die Sammel-Digest-Warteschlange wandert ausschließlich Text, der den
  Kritiker **und** den Output-Sanitizer passiert hat (ADR-049).
* **I6:** `process_mail` wirft nicht; jeder Stufenfehler endet als Metadaten-Notiz. Der
  Runner ergänzt nur die Buchführung.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Final, TypeVar

from maildigest.agents.critic import CriticAgent
from maildigest.agents.offline import OfflineCritic, OfflineSummarizer
from maildigest.agents.summarizer import SummarizerAgent
from maildigest.config import Config, resolve_state_db_path
from maildigest.delivery import DeliveryStats, OutboxMessenger
from maildigest.ingest.imap_client import (
    ImapClient,
    IngestError,
    IngestService,
    IngestStats,
    backoff_delay,
)
from maildigest.llm.base import LLMRateLimited, LLMTimeout, LLMTransportError
from maildigest.logging_setup import traceback_enabled
from maildigest.messenger.base import MessengerError
from maildigest.messenger.factory import build_messenger
from maildigest.messenger.telegram import poll_commands
from maildigest.models import CriticVerdict, RawMail, SanitizedMail, Summary
from maildigest.output.composer import DigestComposer, LowDigestItem
from maildigest.output.sanitizer import scrub_field, scrub_plain
from maildigest.pipeline import (
    Critic,
    Delivered,
    PipelineDeps,
    PipelineResult,
    ProgressState,
    QueuedLow,
    Sanitizer,
    Summarizer,
    process_mail,
)
from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.sanitizer import MailSanitizer
from maildigest.state.db import MailState, StateDB, dedupe_hash

__all__ = [
    "COMMAND_POLL_SECONDS",
    "LLM_MAX_ATTEMPTS",
    "RetryingCritic",
    "RetryingSummarizer",
    "RunStats",
    "Runner",
    "StatusRecorder",
    "build_runner",
]

logger = logging.getLogger("maildigest.runner")

#: `meta`-Schlüssel für die zuletzt gesehene Telegram-`update_id` + 1 (ADR-077).
_META_COMMAND_OFFSET = "telegram_command_offset"

#: Versuche je LLM-Stufe (docs/ARCHITECTURE.md §6: „3 Versuche, dann fail-closed").
LLM_MAX_ATTEMPTS: Final = 3

#: Wartezeit vor Versuch 2 und 3 einer LLM-Stufe.
LLM_RETRY_BACKOFF_SECONDS: Final = (2.0, 4.0)

#: LLM-Fehler, die einen weiteren Versuch rechtfertigen. `LLMInvalidResponse` gehört
#: bewusst **nicht** dazu: Der Reparaturversuch ist in `llm/schema.py` bereits erfolgt,
#: ein weiterer Aufruf würde nur Zeit und Tokens kosten (ADR-050).
_RETRYABLE_LLM_ERRORS: Final = (LLMTimeout, LLMRateLimited, LLMTransportError)

#: Längster Abschnitt, den der Dauerbetrieb am Stück wartet (ADR-080). Nach jedem
#: Abschnitt wird der Befehlskanal abgefragt — die Latenz von `/digest` ist damit nach
#: oben durch diesen Wert begrenzt, statt durch `[imap] poll_interval_seconds` (HC-12).
#: Bewusst keine Konfigurationsoption: Der Wert ist ein Kompromiss zwischen Reaktionszeit
#: und der Zahl der `getUpdates`-Aufrufe, den niemand sinnvoll selbst wählen muss.
COMMAND_POLL_SECONDS: Final = 10.0

#: `meta`-Schlüssel des Tages, an dem zuletzt ein Sammel-Digest zugestellt wurde.
_META_LAST_LOW_DIGEST: Final = "last_low_digest_date"

#: Feldlimits beim Einreihen in die Sammel-Digest-Warteschlange (wie im Composer).
_LOW_HEADLINE_CHARS: Final = 120
_LOW_CATEGORY_CHARS: Final = 40
_LOW_DOMAIN_CHARS: Final = 100

#: Feldlimit des Ordnernamens in der `/status`-Antwort (HC-28).
_STATUS_FOLDER_CHARS: Final = 80


# --- Retry-Wrapper der LLM-Stufen -------------------------------------------------------


_T = TypeVar("_T")


def _with_llm_retry(
    call: Callable[[], _T],
    *,
    stage: str,
    attempts: int,
    sleep: Callable[[float], None],
) -> _T:
    """Führt einen LLM-Aufruf mit begrenzten Wiederholungen aus (ADR-050).

    Wiederholt ausschließlich Timeout-, Rate-Limit- und Transportfehler. Nach dem letzten
    Versuch fliegt die ursprüngliche Exception weiter — die Pipeline macht daraus die
    Metadaten-Notiz (I6).
    """
    last_delay_index = 0
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return call()
        except _RETRYABLE_LLM_ERRORS as exc:
            if attempt >= attempts:
                logger.warning(
                    "llm_giving_up",
                    extra={"stage": stage, "attempts": attempt, "error": type(exc).__name__},
                )
                raise
            delay = LLM_RETRY_BACKOFF_SECONDS[
                min(last_delay_index, len(LLM_RETRY_BACKOFF_SECONDS) - 1)
            ]
            last_delay_index += 1
            logger.warning(
                "llm_retry",
                extra={
                    "stage": stage,
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "error": type(exc).__name__,
                },
            )
            sleep(delay)
    raise AssertionError("unerreichbar: Schleife endet immer mit return oder raise")


@dataclass
class RetryingSummarizer:
    """Summarizer-Stufe mit Wiederholung bei vorübergehenden LLM-Fehlern."""

    inner: Summarizer
    attempts: int = LLM_MAX_ATTEMPTS
    sleep: Callable[[float], None] = time.sleep

    def summarize(self, mail: SanitizedMail) -> Summary:
        """Ruft den echten Summarizer, bei Bedarf mehrfach."""
        return _with_llm_retry(
            lambda: self.inner.summarize(mail),
            stage="summarize",
            attempts=self.attempts,
            sleep=self.sleep,
        )


@dataclass
class RetryingCritic:
    """Kritiker-Stufe mit Wiederholung bei vorübergehenden LLM-Fehlern."""

    inner: Critic
    attempts: int = LLM_MAX_ATTEMPTS
    sleep: Callable[[float], None] = time.sleep

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        """Ruft den echten Kritiker, bei Bedarf mehrfach."""
        return _with_llm_retry(
            lambda: self.inner.review(mail, summary),
            stage="critic",
            attempts=self.attempts,
            sleep=self.sleep,
        )


# --- Statusführung ----------------------------------------------------------------------


#: Zwischenstände der Pipeline → Statuswerte der State-DB.
_PROGRESS_TO_STATE: Final[dict[str, MailState]] = {
    "sanitized": MailState.SANITIZED,
    "summarized": MailState.SUMMARIZED,
    "checked": MailState.CHECKED,
}


@dataclass
class StatusRecorder:
    """Schreibt die Zwischenstände der Pipeline in die State-DB (`pipeline.ProgressSink`)."""

    db: StateDB

    def record(self, dedupe_key: str, state: ProgressState) -> None:
        """Committet den Zwischenstand — bei `checked` ist das die Zusage aus ADR-008."""
        self.db.mark_status(dedupe_key, _PROGRESS_TO_STATE[state])


# --- Laufstatistik ----------------------------------------------------------------------


@dataclass
class RunStats:
    """Zählwerk eines Laufs (nur Zahlen — keine Mail-Metadaten)."""

    ingest: IngestStats = field(default_factory=IngestStats)
    delivery: DeliveryStats = field(default_factory=DeliveryStats)
    low_digests: int = 0
    cycles: int = 0


# --- Runner -----------------------------------------------------------------------------


@dataclass
class Runner:
    """Betriebsschleife: Zustell-Warteschlange, Ingest, Sammel-Digest.

    Attributes:
        config: Validierte Gesamt-Config.
        db: State-Datenbank (Eigentum des Aufrufers; wird hier nicht geschlossen).
        deps: Die verdrahteten Pipeline-Stufen.
        outbox: Zustell-Warteschlange über dem echten Messenger.
        composer: Nachrichtenbau — hier für den Sammel-Digest.
        ingest: Poll-Dienst über dem Mirror-Postfach.
        now: Zeitquelle für die Digest-Planung (lokale Zeit, wie `low_digest_time`).
        sleep: Wartefunktion; Default wartet unterbrechbar auf das Stop-Event.
    """

    config: Config
    db: StateDB
    deps: PipelineDeps
    outbox: OutboxMessenger
    composer: DigestComposer
    ingest: IngestService
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] | None = None
    #: Befehlsabfrage (ADR-077); injizierbar, damit Tests ohne Netz auskommen.
    commands: Callable[..., tuple[tuple[str, ...], int]] = poll_commands
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    #: Zustellungen, die schon beim ersten Versuch durchgingen und deshalb nie in
    #: `outbox.flush()` auftauchen (siehe :meth:`take_direct_delivery_stats`).
    _direct: DeliveryStats = field(
        default_factory=DeliveryStats, init=False, repr=False
    )

    # --- Shutdown -------------------------------------------------------------------

    def stop(self) -> None:
        """Fordert den Loop zum Beenden auf (signal-handler-tauglich, reentrant)."""
        self._stop.set()
        self.ingest.stop()

    @property
    def stopped(self) -> bool:
        """True, sobald :meth:`stop` gerufen wurde."""
        return self._stop.is_set()

    def _wait(self, seconds: float) -> None:
        """Wartet unterbrechbar; ein `stop()` beendet die Wartezeit sofort."""
        if self.sleep is not None:
            self.sleep(seconds)
            return
        self._stop.wait(seconds)

    # --- Verarbeitung einer Mail ----------------------------------------------------

    def process(self, raw: RawMail) -> PipelineResult:
        """Pipeline-Callback des Ingest: verarbeiten und Endstatus buchen."""
        result = process_mail(raw, self.deps)
        self._record_result(result)
        return result

    def take_direct_delivery_stats(self) -> DeliveryStats:
        """Liefert die seit dem letzten Aufruf **sofort** zugestellten Nachrichten.

        `OutboxMessenger.send` reiht jede Nachricht ein und versucht sie unmittelbar
        zuzustellen; klappt das, ist der Eintrag weg, bevor ein `flush()` ihn sehen
        könnte. Die Bilanzzeile von `run --once` zählte deshalb nur Nachrichten aus der
        Warteschlange — im Normalfall also dauerhaft null (CT-10).

        Der Zähler wird beim Lesen zurückgesetzt, damit ein Lauf nur seine eigenen
        Zustellungen meldet.
        """
        stats = self._direct
        self._direct = DeliveryStats()
        return stats

    def _note_direct_delivery(self, dedupe_key: str) -> bool:
        """Bucht eine Zustellung, die die Warteschlange nicht mehr enthält.

        Returns:
            ``True``, wenn zu `dedupe_key` noch etwas aussteht (also **nicht** gezählt
            wurde). Deferrals bleiben Sache von :meth:`OutboxMessenger.flush` — sonst
            zählte derselbe Fehlversuch zweimal.
        """
        if self.db.outbox_pending(dedupe_key):
            return True
        self._direct.delivered += 1
        return False

    def _record_result(self, result: PipelineResult) -> None:
        """Schreibt den Endstatus einer Mail (ADR-050)."""
        key_short = dedupe_hash(result.dedupe_key)[:12]
        if isinstance(result, Delivered):
            if self._note_direct_delivery(result.dedupe_key):
                # Zustellung liegt in der Warteschlange: Status bleibt `checked`, bis die
                # Bestätigung da ist (ADR-008/ADR-048).
                logger.warning("mail_delivery_queued", extra={"mail": key_short})
                return
            self.db.mark_status(result.dedupe_key, MailState.DELIVERED)
            return
        if isinstance(result, QueuedLow):
            self._queue_low(result)
            self.db.mark_status(result.dedupe_key, MailState.SKIPPED_LOW)
            return
        if result.notice_delivered:
            # Auch die Metadaten-Notiz ist eine zugestellte Nachricht (F-OPS-3).
            self._note_direct_delivery(result.dedupe_key)
        self.db.mark_status(
            result.dedupe_key, MailState.FAILED, error_class=result.notice.reason_class
        )
        logger.info(
            "mail_failed_notice",
            extra={
                "mail": key_short,
                "stage": result.notice.stage,
                "reason": result.notice.reason_class,
                "notice_delivered": result.notice_delivered,
                # Nur gesetzt, wenn die Ursache nachweislich keinen Mail-Inhalt trägt.
                "detail": result.detail,
            },
        )

    def _queue_low(self, result: QueuedLow) -> None:
        """Reiht eine `low`-Mail in die Sammel-Digest-Warteschlange ein (F-SUM-5).

        Gespeichert wird ausschließlich bereits **output-sanitisierter** Text (ADR-049) —
        die Warteschlange ist damit keine Hintertür an Schicht 6 vorbei (I3/I4).
        """
        collector = LinkCollector()
        self.db.queue_low(
            result.dedupe_key,
            headline=_one_line(
                scrub_field(
                    result.summary.headline, collector=collector, max_chars=_LOW_HEADLINE_CHARS
                )
            ),
            category=_one_line(
                scrub_plain(result.summary.category, max_chars=_LOW_CATEGORY_CHARS)
            ),
            from_domain=_one_line(
                scrub_plain(result.from_domain, max_chars=_LOW_DOMAIN_CHARS)
            ),
        )

    # --- Sammel-Digest ---------------------------------------------------------------

    def maybe_send_low_digest(self) -> bool:
        """Stellt einmal täglich ab `[general] low_digest_time` den Sammel-Digest zu.

        Returns:
            ``True``, wenn in diesem Aufruf ein Digest erzeugt und eingereiht wurde.
        """
        now = self.now()
        today = now.date().isoformat()
        if self.db.meta_get(_META_LAST_LOW_DIGEST) == today:
            return False
        hour, _, minute = self.config.general.low_digest_time.partition(":")
        if (now.hour, now.minute) < (int(hour), int(minute)):
            return False

        entries = self.db.low_digest_entries()
        if not entries:
            # Nichts gesammelt: Tag als erledigt vermerken, keine leere Nachricht senden.
            self.db.meta_set(_META_LAST_LOW_DIGEST, today)
            return False

        message = self.composer.compose_low_digest(
            [
                LowDigestItem(
                    headline=entry.headline,
                    category=entry.category,
                    from_domain=entry.from_domain,
                )
                for entry in entries
            ]
        )
        self.outbox.send(message)
        self._note_direct_delivery(message.dedupe_key)
        # Die Nachricht liegt jetzt (mindestens) in der Zustell-Warteschlange und trägt
        # den Inhalt weiter; die Quell-Einträge dürfen weg, sonst entstünde morgen ein
        # Duplikat.
        self.db.clear_low_digest([entry.id for entry in entries])
        self.db.meta_set(_META_LAST_LOW_DIGEST, today)
        logger.info("low_digest_sent", extra={"mails": len(entries)})
        return True

    def _low_digest_guarded(self) -> bool:
        """Wie :meth:`maybe_send_low_digest`, aber ohne eigene Ausnahme nach außen.

        Gebraucht an den Stellen, an denen der Digest in einer ausnahmefesten Zone läuft
        (HC-26): Dort würde ein Fehler des Digests den eigentlichen Grund des Abbruchs —
        den `IngestError` — verdecken.
        """
        try:
            return self.maybe_send_low_digest()
        except Exception as exc:  # der Digest darf keinen Lauf kippen
            logger.warning(
                "low_digest_failed",
                extra={"error": type(exc).__name__},
                exc_info=traceback_enabled(logger),
            )
            return False

    # --- Läufe ------------------------------------------------------------------------

    def run_once(self) -> RunStats:
        """Ein vollständiger Durchlauf: Warteschlange, Postfach, Sammel-Digest.

        Grundlage von `maildigest run --once` (F-OPS-1, Cron-tauglich).

        Raises:
            IngestError: Verbindung/Login/Abruf fehlgeschlagen. Der Aufrufer (Cron)
                wiederholt; die Zustell-Warteschlange wurde vorher bereits abgearbeitet.
        """
        stats = RunStats(cycles=1)
        stats.delivery = stats.delivery + self.outbox.flush()
        try:
            stats.ingest = stats.ingest + self.ingest.run_once()
        finally:
            # Auch bei IMAP-Ausfall dürfen weder eine fertige Nachricht noch der fällige
            # Sammel-Digest liegen bleiben (HC-26): Der Digest liest nur aus der eigenen
            # Warteschlange und schreibt in die Outbox — er braucht kein Postfach.
            stats.delivery = stats.delivery + self.outbox.flush()
            if self._low_digest_guarded():
                stats.low_digests += 1
            # Der Befehlskanal wird im Cron-Betrieb einmal am Ende bedient (E2/ADR-080 —
            # verzögert, aber nicht tot); eine `/status`-Antwort zählt danach mit.
            self._serve_commands_once()
            stats.delivery = stats.delivery + self.take_direct_delivery_stats()
        return stats

    # --- Befehle aus dem Messenger (opt-in, ADR-077) --------------------------------

    def _commands_enabled(self) -> bool:
        """Ob Befehle angenommen werden — dreifach abgesichert."""
        telegram = self.config.messenger.telegram
        return (
            self.config.messenger.active == "telegram"
            and telegram.accept_commands
            and telegram.token is not None
            and bool(telegram.chat_id)
        )

    def poll_commands_once(self) -> tuple[str, ...]:
        """Holt neue Befehle und merkt sich den Telegram-Offset über Neustarts hinweg.

        Ein Fehler beim Abfragen darf den Betrieb nie stoppen: Die Zustellung ist die
        Hauptaufgabe, die Fernauslösung nur Bequemlichkeit.
        """
        if not self._commands_enabled():
            return ()
        telegram = self.config.messenger.telegram
        assert telegram.token is not None  # von `_commands_enabled` sichergestellt
        stored = self.db.meta_get(_META_COMMAND_OFFSET)
        offset = int(stored) if stored and stored.isdigit() else 0
        try:
            found, new_offset = self.commands(
                token=telegram.token, chat_id=telegram.chat_id, offset=offset
            )
        except MessengerError as exc:
            logger.warning("command_poll_failed", extra={"error": type(exc).__name__})
            return ()
        if new_offset != offset:
            self.db.meta_set(_META_COMMAND_OFFSET, str(new_offset))
        if found:
            logger.info("commands_received", extra={"count": len(found)})
        return found

    def handle_command(self, command: str) -> bool:
        """Führt einen Befehl aus der festen Liste aus.

        Returns:
            True, wenn danach ein Abrufzyklus fällig ist (`/digest`).
        """
        if command == "/status":
            pending = self.outbox.pending
            queued = len(self.db.low_digest_entries())
            # Der einzige variable Anteil ist der Ordnername aus der eigenen
            # Konfiguration. Er wird **vor** der Interpolation gescrubbt (HC-28): So
            # bleibt `compose_plain` eine reine Code-Nachricht, und der CT-8-Schutz
            # (Struktur-Emoji, Messenger-Markup, Zeilenumbrüche) greift dort, wo
            # ADR-062 ihn vorsieht.
            folder = scrub_plain(self.config.imap.folder, max_chars=_STATUS_FOLDER_CHARS)
            text = (
                f"MailDigest is running. Folder: {folder}. "
                f"{pending} message(s) waiting to be delivered, "
                f"{queued} mail(s) collected for the daily digest."
            )
            self.outbox.send(self.composer.compose_plain(text))
            return False
        return command == "/digest"

    def _serve_commands(self) -> bool:
        """Arbeitet einen Stapel Befehle **vollständig** ab (Dauerbetrieb).

        Bewusst eine Liste statt `any(…)` über einen Generator: `any` bricht beim ersten
        `True` ab und verwarf damit jeden Befehl hinter dem ersten `/digest` — still,
        endgültig und ohne Logzeile (HC-13).

        Returns:
            True, wenn mindestens ein `/digest` dabei war und sofort ein weiterer
            Abrufzyklus folgen soll. Mehrere `/digest` ergeben genau einen (ADR-077).
        """
        triggered = [self.handle_command(command) for command in self.poll_commands_once()]
        return any(triggered)

    def _serve_commands_once(self) -> None:
        """Bedient den Befehlskanal am Ende eines `run --once` (E2, ADR-080).

        `/status` wird beantwortet. `/digest` ist hier wirkungslos — der Abruf lief
        gerade — und wird nur konsumiert, damit er sich nicht bis zum nächsten Cron-Lauf
        staut; das hält den Offset in Bewegung und ist als `command_ignored_once`
        sichtbar. Ein Fehler darf den Lauf nie kippen: Diese Methode läuft in der
        ausnahmefesten Zone von :meth:`run_once`.
        """
        try:
            for command in self.poll_commands_once():
                if self.handle_command(command):
                    logger.info("command_ignored_once", extra={"command": command})
        except Exception as exc:  # Bequemlichkeit darf die Hauptaufgabe nie kippen
            logger.warning(
                "command_handling_failed",
                extra={"error": type(exc).__name__},
                exc_info=traceback_enabled(logger),
            )

    def _wait_for_next_cycle(self) -> bool:
        """Wartet das Poll-Intervall ab und fragt dabei regelmäßig Befehle ab (HC-12).

        Die Wartezeit zerfällt in Abschnitte von höchstens :data:`COMMAND_POLL_SECONDS`;
        nach jedem Abschnitt wird der Befehlskanal bedient. Damit wartet ein `/digest`
        höchstens einen Abschnitt statt eines vollen Poll-Intervalls, ohne dass ein
        blockierendes Long-Polling `_stop.wait` aushebelt — `stop()` beendet jeden
        Abschnitt sofort.

        Returns:
            True, wenn ein `/digest` den nächsten Zyklus sofort verlangt.
        """
        remaining = float(self.config.imap.poll_interval_seconds)
        while remaining > 0 and not self.stopped:
            chunk = min(COMMAND_POLL_SECONDS, remaining)
            self._wait(chunk)
            remaining -= chunk
            if self.stopped:
                break
            if self._serve_commands():
                return True
        return False

    def run_forever(self, *, handle_signals: bool = True) -> RunStats:
        """Dauerbetrieb bis SIGINT/SIGTERM oder :meth:`stop`.

        Der Loop ruft je Zyklus :meth:`IngestService.run_once` (statt dessen eigenem
        Dauer-Loop), damit zwischen zwei Polls die Zustell-Warteschlange und der
        Sammel-Digest drankommen (ADR-051). Verbindungsfehler führen zu Reconnect mit
        Exponential Backoff (docs/ARCHITECTURE.md §6), nie zum Abbruch.

        Args:
            handle_signals: SIGINT/SIGTERM auf :meth:`stop` legen (nur im Haupt-Thread;
                die vorherigen Handler werden am Ende wiederhergestellt).
        """
        total = RunStats()
        with _signal_handlers(self.stop, enabled=handle_signals):
            failures = 0
            logger.info("runner_started", extra={"folder": self.config.imap.folder})
            while not self.stopped:
                total.cycles += 1
                total.delivery = total.delivery + self.outbox.flush()
                try:
                    total.ingest = total.ingest + self.ingest.run_once()
                except IngestError as exc:
                    failures += 1
                    delay = backoff_delay(failures)
                    logger.warning(
                        "ingest_failed",
                        extra={
                            "error": type(exc).__name__,
                            "attempt": failures,
                            "delay_seconds": delay,
                        },
                        exc_info=traceback_enabled(logger),
                    )
                    # Der Sammel-Digest hängt nicht am Postfach (HC-26): Er liest die
                    # eigene Warteschlange und schreibt in die Outbox. Ein IMAP-Ausfall
                    # über den Digest-Zeitpunkt hinweg darf ihn nicht mitreißen.
                    if self._low_digest_guarded():
                        total.low_digests += 1
                    total.delivery = total.delivery + self.take_direct_delivery_stats()
                    self._wait(delay)
                    continue
                failures = 0
                if self.maybe_send_low_digest():
                    total.low_digests += 1
                total.delivery = total.delivery + self.take_direct_delivery_stats()

                # Auf Zuruf sofort noch einmal abrufen, statt das Poll-Intervall
                # abzuwarten. Mehrere `/digest` in einem Zyklus lösen genau einen
                # zusätzlichen Durchlauf aus — sonst könnte ein Tastendruck-Gewitter
                # das LLM-Kontingent verbrennen.
                if self._serve_commands():
                    continue
                # Die Wartezeit wird in Abschnitte zerlegt, nach jedem wird der
                # Befehlskanal bedient (ADR-080): `/digest` wartet höchstens
                # `COMMAND_POLL_SECONDS`, nicht ein volles Poll-Intervall.
                if self._wait_for_next_cycle():
                    continue
            logger.info(
                "runner_stopped",
                extra={
                    "cycles": total.cycles,
                    "processed": total.ingest.processed,
                    "queued_deliveries": self.outbox.pending,
                },
            )
        return total


# --- Verdrahtung ------------------------------------------------------------------------


def build_runner(
    config: Config,
    *,
    config_path: str | Path | None = None,
    db: StateDB | None = None,
    sanitizer: Sanitizer | None = None,
    summarizer: Summarizer | None = None,
    critic: Critic | None = None,
    composer: DigestComposer | None = None,
    messenger: object | None = None,
    client_factory: Callable[[], ImapClient] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Runner:
    """Baut den kompletten Daemon aus der Config (M1: Mail rein → Nachricht raus).

    Alle Stufen sind optional überschreibbar, damit Tests (und `maildigest test` in WP9)
    einzelne Teile durch Attrappen ersetzen können, ohne die Verdrahtung nachzubauen.

    Args:
        config: Validierte Gesamt-Config.
        config_path: Pfad der Konfigurationsdatei — bestimmt bei leerem
            `[general] state_db` den Ort der State-Datenbank (ADR-045).
        db: Bereits geöffnete State-DB; sonst wird sie aus der Config aufgelöst.
        sanitizer, summarizer, critic, composer, messenger: Ersatz-Stufen für Tests.
        client_factory: Ersatz-Fabrik für die IMAP-Verbindung (Tests, `maildigest test`).
        sleep: Wartefunktion der LLM-Retries.

    Returns:
        Einen einsatzbereiten :class:`Runner`.

    Raises:
        ConfigError: Zugangsdaten fehlen (LLM-Key, Messenger-Token …).
        StateError: State-Datenbank nicht benutzbar.
    """
    state = db if db is not None else StateDB(resolve_state_db_path(config, config_path))
    real_messenger = messenger if messenger is not None else build_messenger(config)
    outbox = OutboxMessenger(state, real_messenger)  # type: ignore[arg-type]
    digest_composer = composer if composer is not None else DigestComposer.from_config(config)
    # `provider = "none"`: Betrieb ohne Sprachmodell (ADR-076). Nur die *selbst gebauten*
    # Offline-Stufen laufen ohne Retry-Wrapper — es gibt keinen Netzaufruf, der scheitern
    # könnte. Eine von außen injizierte Stufe steht dagegen für ein echtes LLM (Tests,
    # Sonderfälle) und behält ihre Wiederholungen.
    offline = config.llm.provider == "none"
    stage_summarizer: Summarizer
    if summarizer is not None:
        stage_summarizer = RetryingSummarizer(summarizer, sleep=sleep)
    elif offline:
        stage_summarizer = OfflineSummarizer()
    else:
        stage_summarizer = RetryingSummarizer(SummarizerAgent.from_config(config), sleep=sleep)

    stage_critic: Critic
    if critic is not None:
        stage_critic = RetryingCritic(critic, sleep=sleep)
    elif offline:
        stage_critic = OfflineCritic()
    else:
        stage_critic = RetryingCritic(CriticAgent.from_config(config), sleep=sleep)

    deps = PipelineDeps(
        sanitizer=sanitizer if sanitizer is not None else MailSanitizer.from_config(config),
        summarizer=stage_summarizer,
        critic=stage_critic,
        composer=digest_composer,
        messenger=outbox,
        deliver_min_importance=config.general.deliver_min_importance,
        progress=StatusRecorder(state),
    )
    ingest = IngestService(
        cfg=config.imap,
        db=state,
        process=_unwired,
        client_factory=client_factory,
        write_result_status=False,
    )
    runner = Runner(
        config=config,
        db=state,
        deps=deps,
        outbox=outbox,
        composer=digest_composer,
        ingest=ingest,
    )
    # Henne-Ei: Der Ingest gehört dem Runner, der Verarbeitungs-Callback dem Ingest.
    ingest.process = runner.process
    return runner


def _unwired(raw: RawMail) -> PipelineResult:
    """Platzhalter-Callback; `build_runner` ersetzt ihn unmittelbar nach der Konstruktion."""
    raise AssertionError("The ingest callback was not wired up.")


# --- Hilfen -----------------------------------------------------------------------------


def _one_line(text: str) -> str:
    """Presst einen Wert auf eine Zeile (Warteschlangen-Felder sind einzeilig)."""
    return " ".join(text.split())


class _signal_handlers:  # noqa: N801 - Kontextmanager, bewusst klein geschrieben
    """Legt SIGINT/SIGTERM auf einen Callback und stellt die alten Handler wieder her."""

    def __init__(self, on_signal: Callable[[], None], *, enabled: bool = True) -> None:
        self._on_signal = on_signal
        self._enabled = enabled and threading.current_thread() is threading.main_thread()
        self._previous: list[tuple[int, object]] = []

    def __enter__(self) -> _signal_handlers:
        if not self._enabled:
            return self
        for number in (signal.SIGINT, signal.SIGTERM):
            try:
                previous = signal.signal(number, self._handle)
            except (ValueError, OSError):  # pragma: no cover - Plattform ohne SIGTERM
                continue
            self._previous.append((number, previous))
        return self

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        """Bittet den Loop um Beendigung; der aktuelle Zyklus läuft noch zu Ende."""
        logger.info("shutdown_requested", extra={"signal": signum})
        self._on_signal()

    def __exit__(self, *exc: object) -> None:
        for number, previous in self._previous:
            try:
                signal.signal(number, previous)  # type: ignore[arg-type]
            except (ValueError, OSError):  # pragma: no cover
                continue
        self._previous.clear()


