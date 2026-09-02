"""Telegram-Adapter über die Bot-API (`sendMessage`) mit `httpx`.

**Kein `parse_mode`** (ADR-006, T7): Die Nachricht geht als reiner Text raus. Damit kann
kein durchgerutschtes `*`, `_`, `[…](…)` oder `<b>` im Messenger zu Formatierung oder
gar zu einem klickbaren Ziel werden — die Entscheidung liegt bei uns, nicht beim Inhalt.
Zusätzlich wird die Link-Vorschau deaktiviert (`disable_web_page_preview`), damit eine
theoretisch übersehene Adresse nicht auch noch aufgelöst und als Vorschaukarte gerendert
wird (Defense in Depth zu I3).

Das Bot-Token steckt bei Telegram im **Pfad** der URL. Deshalb: Token als
:class:`~pydantic.SecretStr`, keine URL in Exceptions, kein Logging von Requests (I5).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from maildigest.messenger._http import request_json
from maildigest.messenger.base import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MessengerError,
)
from maildigest.models import DigestMessage

__all__ = ["TELEGRAM_DEFAULT_BASE_URL", "TelegramMessenger"]

#: Standard-Host der Bot-API (überschreibbar für Proxys/Tests).
TELEGRAM_DEFAULT_BASE_URL = "https://api.telegram.org"


class TelegramMessenger:
    """Zustellung an einen Telegram-Chat über einen Bot (F-MSG-1)."""

    def __init__(
        self,
        *,
        token: SecretStr,
        chat_id: str,
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Baut den Adapter.

        Args:
            token: Bot-Token aus `[messenger.telegram] token` oder
                `MAILDIGEST_TELEGRAM_TOKEN`.
            chat_id: Ziel-Chat (`connect-messenger` ermittelt ihn in WP9).
            base_url: Abweichender API-Host; leer = :data:`TELEGRAM_DEFAULT_BASE_URL`.
            timeout: Zeitlimit je Request in Sekunden.
            max_attempts: Versuche bei 429/5xx.
            client: Vorhandener httpx-Client (Tests: `MockTransport`).
            sleep: Wartefunktion für das Backoff.

        Raises:
            ValueError: Token oder Chat-ID fehlen.
        """
        if not token.get_secret_value():
            raise ValueError("Telegram-Token fehlt.")
        if not chat_id:
            raise ValueError("Telegram-Chat-ID fehlt.")
        self._token = token
        self._chat_id = chat_id
        self._base_url = (base_url or TELEGRAM_DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def __repr__(self) -> str:
        """Repräsentation ohne Token (I5)."""
        return f"TelegramMessenger(chat_id={self._chat_id!r}, base_url={self._base_url!r})"

    __str__ = __repr__

    def _url(self, method: str) -> str:
        """Endpunkt-URL inkl. Token — darf nie in Logs oder Exceptions landen (I5)."""
        return f"{self._base_url}/bot{self._token.get_secret_value()}/{method}"

    # --- Messenger ----------------------------------------------------------------

    def send(self, message: DigestMessage) -> None:
        """Stellt jeden Teil als eigene Nachricht zu (Reihenfolge bleibt erhalten)."""
        for part in message.parts:
            if not part:
                continue
            payload: dict[str, Any] = {
                "chat_id": self._chat_id,
                "text": part,
                # Kein `parse_mode`: reiner Text (ADR-006, T7).
                "disable_web_page_preview": True,
            }
            data = request_json(
                self._client,
                "POST",
                self._url("sendMessage"),
                payload=payload,
                adapter="telegram",
                timeout=self._timeout,
                max_attempts=self._max_attempts,
                sleep=self._sleep,
            )
            if data.get("ok") is False:
                raise MessengerError(
                    "telegram: API meldet Fehler "
                    f"(error_code={data.get('error_code')!r})."
                )

    def healthcheck(self) -> bool:
        """Prüft Token und Erreichbarkeit über `getMe` (wirft nicht)."""
        try:
            data = request_json(
                self._client,
                "POST",
                self._url("getMe"),
                payload={},
                adapter="telegram",
                timeout=self._timeout,
                max_attempts=1,
                sleep=self._sleep,
            )
        except MessengerError:
            return False
        return bool(data.get("ok", False))

    def close(self) -> None:
        """Schließt den intern erzeugten httpx-Client; injizierte bleiben offen."""
        if self._owns_client:
            self._client.close()
