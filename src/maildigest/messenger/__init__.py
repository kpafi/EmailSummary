"""Messenger-Subpaket: austauschbare Zustell-Adapter (Telegram/Discord/Signal, F-MSG-1).

Öffentliche Fläche: das Protokoll :class:`Messenger`, die Fehlerklasse
:class:`MessengerError`, die drei Adapter und die Factory :func:`build_messenger`.
"""

from maildigest.messenger.base import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    Messenger,
    MessengerError,
)
from maildigest.messenger.discord import DiscordMessenger
from maildigest.messenger.factory import build_messenger
from maildigest.messenger.signal import SignalMessenger
from maildigest.messenger.telegram import TelegramMessenger

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "DiscordMessenger",
    "Messenger",
    "MessengerError",
    "SignalMessenger",
    "TelegramMessenger",
    "build_messenger",
]
