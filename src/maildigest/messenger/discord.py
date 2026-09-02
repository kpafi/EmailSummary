"""Discord-Adapter über einen Webhook — ausschließlich `content` als reiner Text.

**Keine Embeds** (PLAN.md WP7, T7): Embeds können Titel-/Beschreibungs-Links und
Bild-URLs rendern, also genau das, was I3 verbietet. Der Adapter sendet nur das
`content`-Feld. Zusätzlich unterdrückt `allowed_mentions` jede Erwähnung — ein
`@everyone` aus einer Angreifer-Mail soll keinen Server aufwecken.

Die Webhook-URL ist selbst das Secret (wer sie hat, darf posten): Sie wird als
:class:`~pydantic.SecretStr` gehalten und erscheint in keiner Fehlermeldung (I5).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from maildigest.messenger._http import request_json
from maildigest.messenger.base import DEFAULT_TIMEOUT_SECONDS, MAX_ATTEMPTS, MessengerError
from maildigest.models import DigestMessage

__all__ = ["DiscordMessenger"]


class DiscordMessenger:
    """Zustellung in einen Discord-Kanal über einen Webhook (F-MSG-1)."""

    def __init__(
        self,
        *,
        webhook_url: SecretStr,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Baut den Adapter.

        Args:
            webhook_url: Webhook-URL aus `[messenger.discord] webhook_url`.
            timeout: Zeitlimit je Request in Sekunden.
            max_attempts: Versuche bei 429/5xx.
            client: Vorhandener httpx-Client (Tests: `MockTransport`).
            sleep: Wartefunktion für das Backoff.

        Raises:
            ValueError: Webhook-URL fehlt oder ist kein HTTP(S)-Endpunkt.
        """
        url = webhook_url.get_secret_value().strip()
        if not url:
            raise ValueError("Discord-Webhook-URL fehlt.")
        if not url.startswith(("http://", "https://")):
            raise ValueError("Discord-Webhook-URL muss mit http:// oder https:// beginnen.")
        self._webhook_url = webhook_url
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def __repr__(self) -> str:
        """Repräsentation ohne Webhook-URL (I5)."""
        return "DiscordMessenger(webhook_url=<secret>)"

    __str__ = __repr__

    # --- Messenger ----------------------------------------------------------------

    def send(self, message: DigestMessage) -> None:
        """Stellt jeden Teil als eigenen Webhook-Post zu (nur `content`, keine Embeds)."""
        for part in message.parts:
            if not part:
                continue
            payload: dict[str, Any] = {
                "content": part,
                "allowed_mentions": {"parse": []},
            }
            request_json(
                self._client,
                "POST",
                self._webhook_url.get_secret_value(),
                payload=payload,
                adapter="discord",
                timeout=self._timeout,
                max_attempts=self._max_attempts,
                sleep=self._sleep,
            )

    def healthcheck(self) -> bool:
        """Fragt die Webhook-Metadaten ab (GET auf die Webhook-URL); wirft nicht."""
        try:
            request_json(
                self._client,
                "GET",
                self._webhook_url.get_secret_value(),
                adapter="discord",
                timeout=self._timeout,
                max_attempts=1,
                sleep=self._sleep,
            )
        except MessengerError:
            return False
        return True

    def close(self) -> None:
        """Schließt den intern erzeugten httpx-Client; injizierte bleiben offen."""
        if self._owns_client:
            self._client.close()
