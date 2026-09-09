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

from maildigest.config import ENV_TELEGRAM_TOKEN, Config, ConfigError, MessengerConfig
from maildigest.messenger.base import DEFAULT_TIMEOUT_SECONDS, MAX_ATTEMPTS, Messenger
from maildigest.messenger.discord import DiscordMessenger
from maildigest.messenger.signal import SignalMessenger
from maildigest.messenger.telegram import TelegramMessenger

__all__ = ["build_messenger", "build_messenger_from_section"]


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
    return build_messenger_from_section(
        config.messenger,
        timeout=timeout,
        max_attempts=max_attempts,
        client=client,
        sleep=sleep,
    )


def build_messenger_from_section(
    messenger: MessengerConfig,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Messenger:
    """Baut den Adapter aus der `[messenger]`-Sektion allein.

    Existiert für `maildigest connect-messenger` (WP9): Beim Einrichten ist die
    Gesamt-Config typischerweise noch unvollständig, die Testnachricht muss trotzdem
    schon verschickt werden können. :func:`build_messenger` ist ein dünner Aufsatz
    darauf, damit die Abbildung Name → Adapter nur einmal existiert.

    Raises:
        ConfigError: Zugangsdaten des aktiven Adapters fehlen oder Signal ist nicht
            freigeschaltet.
    """
    active = messenger.active

    if active == "telegram":
        telegram = messenger.telegram
        token = telegram.token
        if token is None or not token.get_secret_value():
            raise ConfigError(
                "[messenger.telegram] token is missing: set the bot token in the "
                f"configuration file or via the environment variable {ENV_TELEGRAM_TOKEN}."
            )
        if not telegram.chat_id:
            raise ConfigError(
                "[messenger.telegram] chat_id is missing: `maildigest connect-messenger` "
                "discovers the chat ID automatically."
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
        webhook_url = messenger.discord.webhook_url
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

    signal_config = messenger.signal
    if not signal_config.enabled:
        raise ConfigError(
            "[messenger.signal] enabled = false: the Signal adapter is optional and has to "
            "be enabled explicitly."
        )
    if not signal_config.signal_cli_socket:
        raise ConfigError(
            "[messenger.signal] signal_cli_socket fehlt: Pfad des Sockets von "
            "`signal-cli --daemon --socket <pfad>` eintragen."
        )
    return SignalMessenger(socket_path=signal_config.signal_cli_socket, timeout=timeout)
