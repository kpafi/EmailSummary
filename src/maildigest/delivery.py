"""Zustellung über eine persistente Warteschlange (`outbox`) — at-least-once nach ADR-008.

Umsetzung der Messenger-Retry-Politik aus docs/ARCHITECTURE.md §6 (WP8, ADR-048):
5 Versuche über höchstens eine Stunde, danach `failed` + Log. Die Nachricht in der
Warteschlange ist **fertig sanitisiert** (sie hat Kritiker, Output-Sanitizer und
`final_guard` bereits durchlaufen) und wird beim erneuten Versand unverändert übernommen —
es gibt keinen Codepfad, der `parts` nachträglich verändert (I3/I4).

Ablauf eines Versands:

1. `enqueue_outbox()` — **committet**, bevor irgendetwas gesendet wird. Ein Absturz nach
   diesem Punkt kostet höchstens eine Doppelzustellung (ADR-008), nie den Inhalt.
2. Sofortiger Zustellversuch über den echten Adapter (der intern schon 429/5xx abfedert).
3. Erfolg ⇒ Zeile löschen und den Mail-Status von `checked` auf `delivered` heben.
   Misserfolg ⇒ Zeile bleibt mit erhöhtem Zähler und späterem `next_attempt_at` liegen;
   :meth:`OutboxMessenger.flush` nimmt sie im nächsten Zyklus wieder auf.

:meth:`OutboxMessenger.send` wirft deshalb **nicht** bei einem Zustellfehler: Die Zusage
„nichts geht verloren" trägt hier die Warteschlange, nicht der Aufrufer. Damit gilt für die
Pipeline eine zugestellte Mail als zugestellt, sobald sie sicher in der Queue liegt — der
Mail-Status bleibt bis zur echten Bestätigung auf `checked` (siehe `runner.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from maildigest.logging_setup import traceback_enabled
from maildigest.models import DigestMessage
from maildigest.output.composer import LOW_DIGEST_DEDUPE_KEY
from maildigest.pipeline import Messenger as PipelineMessenger
from maildigest.state.db import OutboxItem, StateDB

__all__ = [
    "DELIVERY_BACKOFF_SECONDS",
    "DELIVERY_DEADLINE_SECONDS",
    "DELIVERY_MAX_ATTEMPTS",
    "DeliveryStats",
    "OutboxMessenger",
    "next_delivery_delay",
]

logger = logging.getLogger("maildigest.delivery")

#: Versuche je Nachricht insgesamt (docs/ARCHITECTURE.md §6).
DELIVERY_MAX_ATTEMPTS: Final = 5

#: Wartezeit vor Versuch 2..5 in Sekunden — Summe 55 min, damit alle fünf Versuche
#: innerhalb der in §6 zugesagten Stunde stattfinden.
DELIVERY_BACKOFF_SECONDS: Final = (60.0, 300.0, 900.0, 2100.0)

#: Härtere Schranke: Nach einer Stunde ab dem ersten Einreihen wird aufgegeben.
DELIVERY_DEADLINE_SECONDS: Final = 3600.0

#: `kind`-Kennung einer Nachricht des täglichen Sammel-Digests.
LOW_DIGEST_KIND: Final = "low_digest"

#: `kind`-Kennung einer Mail-Nachricht (Zusammenfassung oder Fail-closed-Notiz).
MAIL_KIND: Final = "mail"

#: `dedupe_key`, unter dem der Sammel-Digest läuft (er gehört zu keiner einzelnen Mail);
#: die Definition liegt beim Erzeuger der Nachricht (`output/composer.py`).
LOW_DIGEST_KEY: Final = LOW_DIGEST_DEDUPE_KEY


@dataclass
class DeliveryStats:
    """Zählwerk eines Zustell-Durchlaufs (nur Zahlen, keine Inhalte)."""

    delivered: int = 0
    deferred: int = 0
    abandoned: int = 0

    def __add__(self, other: DeliveryStats) -> DeliveryStats:
        return DeliveryStats(
            delivered=self.delivered + other.delivered,
            deferred=self.deferred + other.deferred,
            abandoned=self.abandoned + other.abandoned,
        )


def _single_part(message: DigestMessage, part: str) -> DigestMessage:
    """Baut eine Ein-Teil-Nachricht aus einem Teil der Vorlage (CT-13).

    Der Text wird **übernommen, nicht verändert** — `parts` ist bereits durch
    `DigestComposer._finalize()` gelaufen (I3/I4). Der Adapter sieht damit dieselbe
    Zustellreihenfolge wie zuvor, nur weiß der Aufrufer jetzt, wie weit er gekommen ist.
    """
    return DigestMessage(
        parts=[part],
        importance=message.importance,
        is_warning=message.is_warning,
        dedupe_key=message.dedupe_key,
    )


def next_delivery_delay(attempts: int) -> float:
    """Wartezeit bis zum nächsten Versuch nach `attempts` Fehlversuchen (in Sekunden)."""
    index = max(attempts, 1) - 1
    if index >= len(DELIVERY_BACKOFF_SECONDS):
        return DELIVERY_BACKOFF_SECONDS[-1]
    return DELIVERY_BACKOFF_SECONDS[index]


class OutboxMessenger:
    """Erfüllt das schmale `pipeline.Messenger`-Protokoll über die DB-Warteschlange.

    Attributes:
        db: State-Datenbank mit der `outbox`-Tabelle.
        messenger: Der echte Adapter (Telegram/Discord/Signal) aus WP7.
    """

    def __init__(
        self,
        db: StateDB,
        messenger: PipelineMessenger,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Args: now: Zeitquelle (Tests reichen eine steuerbare Uhr herein)."""
        self._db = db
        self._messenger = messenger
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def __repr__(self) -> str:
        """Repräsentation ohne Inhalte und ohne Adapter-Interna (I5)."""
        return f"OutboxMessenger(messenger={type(self._messenger).__name__})"

    # --- pipeline.Messenger -----------------------------------------------------------

    def send(self, message: DigestMessage) -> None:
        """Reiht die Nachricht ein (Commit) und versucht sofort zuzustellen.

        Wirft nicht: Ein fehlgeschlagener Versuch bleibt als Warteschlangen-Eintrag liegen
        und wird von :meth:`flush` wiederholt (ADR-048). Ein Fehler der **Datenbank** wird
        dagegen durchgereicht — dann ist der State unbrauchbar und die Pipeline muss
        fail-closed reagieren (I6).
        """
        kind = LOW_DIGEST_KIND if message.dedupe_key == LOW_DIGEST_KEY else MAIL_KIND
        now = self._now()
        item_id = self._db.enqueue_outbox(
            message.dedupe_key,
            kind=kind,
            parts=message.parts,
            importance=message.importance,
            is_warning=message.is_warning,
            now=now,
        )
        self._attempt(
            item_id=item_id,
            message=message,
            message_id_hash=None,
            attempts=0,
            first_queued_at=now,
            kind=kind,
        )

    # --- Warteschlange ----------------------------------------------------------------

    def flush(self, *, limit: int = 50) -> DeliveryStats:
        """Arbeitet alle fälligen Warteschlangen-Einträge ab (Wiederanlauf + Retries).

        Wird zu Beginn jedes Laufs aufgerufen: Nach einem Absturz liegen hier genau die
        Nachrichten, die committet, aber nicht bestätigt zugestellt wurden.
        """
        stats = DeliveryStats()
        now = self._now()
        for item in self._db.outbox_due(now=now, limit=limit):
            if not item.parts:
                # Unlesbare oder leere Nutzlast: nichts zu senden, nichts zu raten.
                logger.error(
                    "delivery_payload_unusable",
                    extra={"mail": item.message_id_hash[:12], "kind": item.kind},
                )
                self._db.outbox_done(item.id)
                self._finish(item.message_id_hash, item.kind, delivered=False)
                stats.abandoned += 1
                continue
            outcome = self._attempt(
                item_id=item.id,
                message=self._to_message(item),
                message_id_hash=item.message_id_hash,
                attempts=item.attempts,
                first_queued_at=item.first_queued_at,
                kind=item.kind,
            )
            if outcome == "delivered":
                stats.delivered += 1
            elif outcome == "abandoned":
                stats.abandoned += 1
            else:
                stats.deferred += 1
        return stats

    @property
    def pending(self) -> int:
        """Anzahl wartender Zustellungen."""
        return self._db.outbox_size()

    # --- Interna ----------------------------------------------------------------------

    @staticmethod
    def _to_message(item: OutboxItem) -> DigestMessage:
        """Baut die Nachricht aus der Warteschlange zurück.

        `dedupe_key` bleibt leer: Gespeichert ist nur der Hash (NF-5), und kein Adapter
        wertet das Feld aus — der Text steht in `parts` und wird nicht mehr angefasst.
        """
        return DigestMessage(
            parts=item.parts,
            importance=item.importance,  # type: ignore[arg-type]  # DB-Wert ist geprüft
            is_warning=item.is_warning,
            dedupe_key="",
        )

    def _attempt(
        self,
        *,
        item_id: int,
        message: DigestMessage,
        message_id_hash: str | None,
        attempts: int,
        first_queued_at: datetime,
        kind: str,
    ) -> str:
        """Ein Zustellversuch; pflegt die Warteschlange und liefert das Ergebnis.

        Die Teile einer mehrteiligen Nachricht gehen **einzeln** an den Adapter, damit der
        Fortschritt bekannt ist: Bricht die Zustellung beim Teil *n* ab, wird die
        Warteschlangen-Zeile auf die Teile ab *n* eingekürzt und der Retry setzt dort fort
        (CT-13, ADR-066). ADR-008 (at-least-once) bleibt unberührt — genau der eine Teil,
        dessen Bestätigung ausblieb, kann weiterhin doppelt ankommen; die Teile davor nicht
        mehr.

        Returns:
            ``"delivered"``, ``"deferred"`` oder ``"abandoned"``.
        """
        confirmed = 0
        try:
            for part in message.parts:
                self._messenger.send(_single_part(message, part))
                confirmed += 1
        except Exception as exc:  # jeder Adapter-Fehler ist wiederholbar (ADR-048)
            return self._handle_failure(
                item_id=item_id,
                message_id_hash=message_id_hash,
                attempts=attempts,
                first_queued_at=first_queued_at,
                kind=kind,
                exc=exc,
                message=message,
                confirmed_parts=confirmed,
            )
        self._db.outbox_done(item_id)
        if message_id_hash is not None:
            self._finish(message_id_hash, kind, delivered=True)
        logger.info("delivery_ok", extra={"kind": kind, "attempt": attempts + 1})
        return "delivered"

    def _handle_failure(
        self,
        *,
        item_id: int,
        message_id_hash: str | None,
        attempts: int,
        first_queued_at: datetime,
        kind: str,
        exc: BaseException,
        message: DigestMessage | None = None,
        confirmed_parts: int = 0,
    ) -> str:
        """Zählt den Fehlversuch, plant den nächsten — oder gibt endgültig auf."""
        now = self._now()
        used = attempts + 1
        age = (now - first_queued_at).total_seconds()
        expired = used >= DELIVERY_MAX_ATTEMPTS or age >= DELIVERY_DEADLINE_SECONDS
        if expired:
            self._db.outbox_done(item_id)
            if message_id_hash is not None:
                self._finish(message_id_hash, kind, delivered=False)
            logger.error(
                "delivery_abandoned",
                extra={
                    "kind": kind,
                    "attempts": used,
                    "age_seconds": round(age),
                    "error": type(exc).__name__,
                },
                exc_info=traceback_enabled(logger),
            )
            return "abandoned"
        delay = next_delivery_delay(used)
        remaining: list[str] | None = None
        if message is not None and confirmed_parts > 0:
            remaining = list(message.parts[confirmed_parts:])
        self._db.outbox_defer(
            item_id,
            next_attempt_at=now + timedelta(seconds=delay),
            error_class=type(exc).__name__,
            remaining_parts=remaining,
            importance=message.importance if message is not None else None,
            is_warning=message.is_warning if message is not None else None,
        )
        logger.warning(
            "delivery_deferred",
            extra={
                "kind": kind,
                "attempts": used,
                "delay_seconds": delay,
                "error": type(exc).__name__,
                "confirmed_parts": confirmed_parts,
                "remaining_parts": len(remaining) if remaining is not None else None,
            },
        )
        return "deferred"

    def _finish(self, message_id_hash: str, kind: str, *, delivered: bool) -> None:
        """Schließt den Mail-Status ab (nur `checked` → `delivered`/`failed`)."""
        if kind == LOW_DIGEST_KIND:
            return  # Der Sammel-Digest gehört zu keiner einzelnen Mail.
        self._db.promote_checked_to_delivered(message_id_hash, delivered=delivered)
