"""Unit-Tests der Messenger-Factory (WP7): Auswahl und Prüfung aus `[messenger]`."""

from __future__ import annotations

from typing import Any

import pytest

from maildigest.config import Config, ConfigError, load_config_from_dict
from maildigest.messenger.discord import DiscordMessenger
from maildigest.messenger.factory import build_messenger
from maildigest.messenger.signal import SignalMessenger
from maildigest.messenger.telegram import TelegramMessenger


def config_with(messenger: dict[str, Any]) -> Config:
    """Baut eine gültige Config mit der gegebenen `[messenger]`-Sektion."""
    return load_config_from_dict(
        {
            "imap": {"host": "imap.example.org", "username": "mirror@example.org"},
            "llm": {"model": "modell", "api_key": "sk-test"},
            "messenger": messenger,
        },
        env={},
    )


def test_builds_telegram_adapter() -> None:
    """`active = telegram` mit Token und Chat-ID ergibt den Telegram-Adapter."""
    config = config_with(
        {"active": "telegram", "telegram": {"token": "123:abc", "chat_id": "4711"}}
    )
    assert isinstance(build_messenger(config), TelegramMessenger)


def test_telegram_without_token_names_the_env_variable() -> None:
    """Ohne Token nennt die Fehlermeldung den Weg zur Lösung, nie einen Wert (I5)."""
    config = config_with({"active": "telegram", "telegram": {"chat_id": "4711"}})
    with pytest.raises(ConfigError) as excinfo:
        build_messenger(config)
    assert "MAILDIGEST_TELEGRAM_TOKEN" in str(excinfo.value)


def test_telegram_without_chat_id_is_rejected() -> None:
    """Ohne Chat-ID gäbe es kein Ziel — das ist ein Konfigurationsfehler."""
    config = config_with({"active": "telegram", "telegram": {"token": "123:abc"}})
    with pytest.raises(ConfigError, match="chat_id"):
        build_messenger(config)


def test_builds_discord_adapter() -> None:
    """`active = discord` mit Webhook-URL ergibt den Discord-Adapter."""
    config = config_with(
        {
            "active": "discord",
            "discord": {"webhook_url": "https://discord.example/api/webhooks/1/abc"},
        }
    )
    assert isinstance(build_messenger(config), DiscordMessenger)


def test_discord_without_webhook_is_rejected() -> None:
    """Ohne Webhook-URL ist der Adapter nicht baubar."""
    with pytest.raises(ConfigError, match="webhook_url"):
        build_messenger(config_with({"active": "discord"}))


def test_discord_with_invalid_webhook_is_rejected() -> None:
    """Eine URL ohne Schema wird als Konfigurationsfehler gemeldet, nicht als ValueError."""
    config = config_with({"active": "discord", "discord": {"webhook_url": "discord.example"}})
    with pytest.raises(ConfigError, match="webhook_url"):
        build_messenger(config)


def test_signal_requires_feature_flag() -> None:
    """Der Signal-Adapter ist optional und muss ausdrücklich freigeschaltet werden."""
    config = config_with({"active": "signal", "signal": {"signal_cli_socket": "/tmp/s.sock"}})
    with pytest.raises(ConfigError, match="enabled"):
        build_messenger(config)


def test_signal_requires_socket_path() -> None:
    """Freigeschaltet, aber ohne Socket-Pfad: klare Fehlermeldung."""
    config = config_with({"active": "signal", "signal": {"enabled": True}})
    with pytest.raises(ConfigError, match="signal_cli_socket"):
        build_messenger(config)


def test_builds_signal_adapter_when_enabled() -> None:
    """Mit Flag und Socket-Pfad entsteht der signal-cli-Adapter."""
    config = config_with(
        {
            "active": "signal",
            "signal": {"enabled": True, "signal_cli_socket": "/tmp/signal.sock"},
        }
    )
    assert isinstance(build_messenger(config), SignalMessenger)
