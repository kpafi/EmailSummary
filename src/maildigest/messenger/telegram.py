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
from dataclasses import dataclass
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

__all__ = [
    "TELEGRAM_DEFAULT_BASE_URL",
    "ChatCandidate",
    "TelegramMessenger",
    "discover_chat_ids",
]

#: Standard-Host der Bot-API (überschreibbar für Proxys/Tests).
TELEGRAM_DEFAULT_BASE_URL = "https://api.telegram.org"

#: Höchstzahl der je `getUpdates`-Abfrage betrachteten Updates.
_GET_UPDATES_LIMIT = 20

#: Chat-Typen, die Telegram kennt — alles andere wird als „unbekannt" angezeigt, damit
#: kein vom Absender gewählter Text auf dem Terminal des Nutzers landet (WP9, ADR-055).
_KNOWN_CHAT_TYPES = frozenset({"private", "group", "supergroup", "channel"})


@dataclass(frozen=True)
class ChatCandidate:
    """Ein von `getUpdates` gemeldeter Chat (WP9, `maildigest connect-messenger`).

    Enthält bewusst **nur** die numerische Chat-ID und den Chat-Typ aus einer festen
    Werteliste — kein Anzeigename, kein Gruppentitel: Wer den Bot anschreibt, bestimmt
    diese Texte, und sie würden ungeprüft auf dem Terminal des Nutzers landen (ADR-055).
    """

    chat_id: str
    chat_type: str


def discover_chat_ids(
    *,
    token: SecretStr,
    base_url: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> list[ChatCandidate]:
    """Fragt `getUpdates` ab und liefert die darin vorkommenden Chats (F-MSG-2).

    Der Nutzer schreibt seinem Bot eine Nachricht, MailDigest liest die Chat-ID aus dem
    Update — damit entfällt die fehlerträchtige Handeingabe. Ein falsches Token führt zu
    einem HTTP-Fehler und damit zu :class:`MessengerError`; der Aufruf ist also zugleich
    der Token-Test.

    Args:
        token: Bot-Token.
        base_url: Abweichender API-Host; leer = :data:`TELEGRAM_DEFAULT_BASE_URL`.
        timeout: Zeitlimit des Requests in Sekunden.
        client: Vorhandener httpx-Client (Tests: `MockTransport`); sonst wird einer
            angelegt und wieder geschlossen.

    Returns:
        Die gefundenen Chats in Reihenfolge des ersten Auftretens, ohne Duplikate.
        Leere Liste heißt: Es liegt (noch) keine Nachricht vor.

    Raises:
        MessengerError: Telegram nicht erreichbar oder Token abgelehnt.
    """
    host = (base_url or TELEGRAM_DEFAULT_BASE_URL).rstrip("/")
    url = f"{host}/bot{token.get_secret_value()}/getUpdates"
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        data = request_json(
            http,
            "POST",
            url,
            payload={"limit": _GET_UPDATES_LIMIT, "timeout": 0},
            adapter="telegram",
            timeout=timeout,
            max_attempts=1,
            sleep=lambda _seconds: None,
        )
    finally:
        if client is None:
            http.close()

    if data.get("ok") is False:
        raise MessengerError(
            f"telegram: the API reports an error (error_code={data.get('error_code')!r})."
        )

    updates = data.get("result")
    if not isinstance(updates, list):
        return []

    found: list[ChatCandidate] = []
    seen: set[str] = set()
    for update in updates:
        candidate = _chat_of(update)
        if candidate is None or candidate.chat_id in seen:
            continue
        seen.add(candidate.chat_id)
        found.append(candidate)
    return found


def _chat_of(update: object) -> ChatCandidate | None:
    """Zieht Chat-ID und -Typ aus einem Update; `None`, wenn nichts Brauchbares drinsteht."""
    if not isinstance(update, dict):
        return None
    for key in ("message", "edited_message", "channel_post", "my_chat_member"):
        payload = update.get(key)
        if not isinstance(payload, dict):
            continue
        chat = payload.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            continue
        chat_type = chat.get("type")
        kind = chat_type if chat_type in _KNOWN_CHAT_TYPES else "unknown"
        return ChatCandidate(chat_id=str(chat_id), chat_type=str(kind))
    return None


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
                    "telegram: the API reports an error "
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


# --- Befehle vom Nutzer (opt-in, ADR-077) ----------------------------------------------------
#
# Bewusst schmal gehalten: MailDigest nimmt aus dem Messenger AUSSCHLIESSLICH die feste
# Wortliste unten entgegen, und nur aus dem konfigurierten Chat. Freier Text wird
# verworfen, ohne ihn zu lesen, zu beantworten oder gar an ein Sprachmodell zu geben.
# Damit bleibt die neue Befugnis auf „jetzt abrufen" beschränkt; die Invarianten I1-I8
# sind unberührt, weil Mails weiterhin denselben Weg nehmen.

#: Die einzigen akzeptierten Befehle. Alles andere wird ignoriert.
COMMANDS: frozenset[str] = frozenset({"/digest", "/status"})


def poll_commands(
    *,
    token: SecretStr,
    chat_id: str,
    offset: int = 0,
    base_url: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> tuple[tuple[str, ...], int]:
    """Holt neue Befehle aus dem konfigurierten Chat.

    Args:
        token: Bot-Token.
        chat_id: Der einzige Chat, aus dem Befehle angenommen werden. Nachrichten aus
            jedem anderen Chat werden verworfen — der Bot könnte sonst von einem
            beliebigen Fremden ausgelöst werden, der seinen Namen kennt.
        offset: `update_id`, ab der gelesen wird (Telegram bestätigt damit zugleich die
            älteren). Verhindert, dass ein Neustart alte Befehle erneut ausführt.
        base_url: Abweichender API-Host; leer = :data:`TELEGRAM_DEFAULT_BASE_URL`.
        timeout: Zeitlimit des Requests in Sekunden.
        client: Vorhandener httpx-Client (Tests: `MockTransport`).

    Returns:
        `(befehle, neuer_offset)`. `befehle` enthält nur Werte aus :data:`COMMANDS`, in
        der Reihenfolge des Eingangs. `neuer_offset` ist die höchste gesehene
        `update_id` + 1 — auch dann, wenn nichts Verwertbares dabei war, damit
        unbrauchbare Nachrichten nicht dauerhaft erneut geholt werden.

    Raises:
        MessengerError: Telegram nicht erreichbar oder Token abgelehnt.
    """
    host = (base_url or TELEGRAM_DEFAULT_BASE_URL).rstrip("/")
    url = f"{host}/bot{token.get_secret_value()}/getUpdates"
    payload: dict[str, Any] = {"limit": _GET_UPDATES_LIMIT, "timeout": 0}
    if offset > 0:
        payload["offset"] = offset
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        data = request_json(
            http,
            "POST",
            url,
            payload=payload,
            adapter="telegram",
            timeout=timeout,
            max_attempts=1,
            sleep=lambda _seconds: None,
        )
    finally:
        if client is None:
            http.close()

    if data.get("ok") is False:
        raise MessengerError(
            f"telegram: the API reports an error (error_code={data.get('error_code')!r})."
        )

    updates = data.get("result")
    if not isinstance(updates, list):
        return (), offset

    found: list[str] = []
    highest = offset - 1 if offset > 0 else -1
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            highest = max(highest, update_id)
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        seen_chat = chat.get("id") if isinstance(chat, dict) else None
        if str(seen_chat) != str(chat_id):
            continue
        text = message.get("text")
        if not isinstance(text, str):
            continue
        # Erstes Wort, kleingeschrieben, ohne @botname-Anhang (Telegram hängt den in
        # Gruppen an). Der Rest der Nachricht wird nicht einmal angesehen.
        word = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
        word = word.split("@", 1)[0]
        if word in COMMANDS:
            found.append(word)

    return tuple(found), (highest + 1 if highest >= 0 else offset)
