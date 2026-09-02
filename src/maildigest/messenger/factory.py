"""Messenger-Factory: aus `[messenger] active` den passenden Adapter bauen (F-MSG-1).

Analog zu :mod:`maildigest.llm.factory`: Die aufrufende Stelle (WP8/WP9) kennt nur die
Config und das Protokoll — nie einen konkreten Adapter. Fehlende Zugangsdaten sind ein
Konfigurationsfehler (:class:`~maildigest.config.ConfigError`) mit einer Meldung, die den
Weg zur Lösung nennt, aber nie einen Wert (I5).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from maildigest.config import ENV_TELEGRAM_TOKEN, Config, ConfigError
from maildigest.messenger.base import DEFAULT_TIMEOUT_SECONDS, MAX_ATTEMPTS, Messenger
from maildigest.messenger.discord import DiscordMessenger
from maildigest.messenger.signal import SignalMessenger
from maildigest.messenger.telegram import TelegramMessenger

__all__ = ["build_messenger"]


def build_messenger(
    config: Config,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Messenger:
    """Baut den in der Config aktiven Zustell-Adapter.

    Args:
        config: Validierte Gesamt-Config.
        timeout: Zeitlimit je Request in Sekunden.
        max_attempts: Versuche bei 429/5xx (HTTP-Adapter).
        client: Vorhandener httpx-Client (Tests/Connection-Pooling).
        sleep: Wartefunktion für das Backoff.

    Returns:
        Einen einsatzbereiten :class:`Messenger`.

    Raises:
        ConfigError: Zugangsdaten des aktiven Adapters fehlen oder Signal ist nicht
            freigeschaltet.
    """
    active = config.messenger.active

    if active == "telegram":
        telegram = config.messenger.telegram
        token = telegram.token
        if token is None or not token.get_secret_value():
            raise ConfigError(
                "[messenger.telegram] token fehlt: Setze das Bot-Token in der "
                f"Konfigurationsdatei oder über die Umgebungsvariable {ENV_TELEGRAM_TOKEN}."
            )
        if not telegram.chat_id:
            raise ConfigError(
                "[messenger.telegram] chat_id fehlt: `maildigest connect-messenger` "
                "ermittelt die Chat-ID automatisch."
            )
        return TelegramMessenger(
            token=token,
            chat_id=telegram.chat_id,
            timeout=timeout,
            max_attempts=max_attempts,
            client=client,
            sleep=sleep,
        )

    if active == "discord":
        webhook_url = config.messenger.discord.webhook_url
        if webhook_url is None or not webhook_url.get_secret_value().strip():
            raise ConfigError(
                "[messenger.discord] webhook_url fehlt: Lege im Kanal einen Webhook an "
                "und trage seine URL in die Konfigurationsdatei ein (Datei bleibt 0600)."
            )
        try:
            return DiscordMessenger(
                webhook_url=webhook_url,
                timeout=timeout,
                max_attempts=max_attempts,
                client=client,
                sleep=sleep,
            )
        except ValueError as exc:
            raise ConfigError(f"[messenger.discord] webhook_url: {exc}") from exc

    signal_config = config.messenger.signal
    if not signal_config.enabled:
        raise ConfigError(
            "[messenger.signal] enabled = false: Der Signal-Adapter ist optional und muss "
            "ausdrücklich freigeschaltet werden."
        )
    if not signal_config.signal_cli_socket:
        raise ConfigError(
            "[messenger.signal] signal_cli_socket fehlt: Pfad des Sockets von "
            "`signal-cli --daemon --socket <pfad>` eintragen."
        )
    return SignalMessenger(socket_path=signal_config.signal_cli_socket, timeout=timeout)
