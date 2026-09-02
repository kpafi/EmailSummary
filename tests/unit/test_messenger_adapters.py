"""Unit-Tests der Messenger-Adapter (WP7) gegen `httpx.MockTransport` — kein Netzzugriff.

Geprüft werden: Happy-Path von Telegram und Discord, das Fehlen von `parse_mode`/Embeds
(ADR-006, T7), Retry-/Fehlersemantik, Healthchecks, der signal-cli-Adapter gegen eine
Socket-Attrappe und durchgehend die Secret-Freiheit von `repr`/`str`/Fehlertexten (I5).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from maildigest.messenger.base import Messenger, MessengerError
from maildigest.messenger.discord import DiscordMessenger
from maildigest.messenger.signal import SignalMessenger
from maildigest.messenger.telegram import TelegramMessenger
from maildigest.models import DigestMessage

TELEGRAM_TOKEN = "123456:GEHEIMES-BOT-TOKEN-NICHT-LOGGEN"
WEBHOOK_URL = "https://discord.example/api/webhooks/42/GEHEIMES-WEBHOOK-SECRET"


class SleepSpy:
    """Attrappe für `time.sleep`, die Wartezeiten aufzeichnet statt zu warten."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def message(*parts: str) -> DigestMessage:
    """Baut eine fertige DigestMessage aus den gegebenen Teilen."""
    return DigestMessage(parts=list(parts), importance="normal", dedupe_key="k")


def client_for(handler: Any) -> httpx.Client:
    """httpx-Client auf einem MockTransport mit dem gegebenen Handler."""
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Telegram --------------------------------------------------------------------------


def test_telegram_sends_every_part_as_plain_text() -> None:
    """Jeder Teil wird einzeln gesendet — ohne `parse_mode` und ohne Link-Vorschau."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    adapter = TelegramMessenger(
        token=SecretStr(TELEGRAM_TOKEN), chat_id="4711", client=client_for(handler)
    )
    adapter.send(message("Teil eins", "Teil zwei"))

    assert [payload["text"] for payload in seen] == ["Teil eins", "Teil zwei"]
    for payload in seen:
        assert payload["chat_id"] == "4711"
        assert "parse_mode" not in payload  # ADR-006 / T7
        assert payload["disable_web_page_preview"] is True


def test_telegram_skips_empty_parts() -> None:
    """Leere Teile werden nicht gesendet (die Bot-API würde sie ablehnen)."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    adapter = TelegramMessenger(
        token=SecretStr(TELEGRAM_TOKEN), chat_id="4711", client=client_for(handler)
    )
    adapter.send(message("", "Inhalt", ""))
    assert calls == 1


def test_telegram_retries_on_rate_limit_then_succeeds() -> None:
    """429 wird mit `Retry-After` wiederholt (analog LLM-Schicht, ADR-023)."""
    responses = [
        httpx.Response(429, headers={"retry-after": "2"}, json={"ok": False}),
        httpx.Response(200, json={"ok": True}),
    ]
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    adapter = TelegramMessenger(
        token=SecretStr(TELEGRAM_TOKEN),
        chat_id="4711",
        client=client_for(handler),
        sleep=sleep,
    )
    adapter.send(message("Inhalt"))
    assert sleep.calls == [2.0]


def test_telegram_http_error_raises_messenger_error_without_secret() -> None:
    """Ein endgültiger Fehler wird zu `MessengerError` — ohne Token im Text (I5)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    adapter = TelegramMessenger(
        token=SecretStr(TELEGRAM_TOKEN), chat_id="4711", client=client_for(handler)
    )
    with pytest.raises(MessengerError) as excinfo:
        adapter.send(message("Inhalt"))
    text = str(excinfo.value)
    assert "401" in text
    assert TELEGRAM_TOKEN not in text
    assert "/bot" not in text  # keine URL in der Meldung


def test_telegram_api_level_error_is_reported() -> None:
    """Ein HTTP-200 mit `ok: false` ist trotzdem ein Fehlschlag."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 400})

    adapter = TelegramMessenger(
        token=SecretStr(TELEGRAM_TOKEN), chat_id="4711", client=client_for(handler)
    )
    with pytest.raises(MessengerError):
        adapter.send(message("Inhalt"))


def test_telegram_timeout_is_not_retried() -> None:
    """Timeouts werden nicht wiederholt (ADR-023)."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("zeitlimit")

    adapter = TelegramMessenger(
        token=SecretStr(TELEGRAM_TOKEN), chat_id="4711", client=client_for(handler)
    )
    with pytest.raises(MessengerError):
        adapter.send(message("Inhalt"))
    assert calls == 1


def test_telegram_healthcheck() -> None:
    """`getMe` entscheidet über den Healthcheck; Fehler werden nicht geworfen."""

    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getMe")
        return httpx.Response(200, json={"ok": True, "result": {"username": "bot"}})

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False})

    assert (
        TelegramMessenger(
            token=SecretStr(TELEGRAM_TOKEN), chat_id="1", client=client_for(ok)
        ).healthcheck()
        is True
    )
    assert (
        TelegramMessenger(
            token=SecretStr(TELEGRAM_TOKEN), chat_id="1", client=client_for(broken)
        ).healthcheck()
        is False
    )


def test_telegram_repr_hides_token() -> None:
    """Weder `repr` noch `str` enthalten das Bot-Token (I5)."""
    adapter = TelegramMessenger(token=SecretStr(TELEGRAM_TOKEN), chat_id="4711")
    assert TELEGRAM_TOKEN not in repr(adapter)
    assert TELEGRAM_TOKEN not in str(adapter)
    adapter.close()


def test_telegram_requires_token_and_chat_id() -> None:
    """Fehlende Zugangsdaten sind ein Fehler beim Bauen, nicht erst beim Senden."""
    with pytest.raises(ValueError, match="Token"):
        TelegramMessenger(token=SecretStr(""), chat_id="1")
    with pytest.raises(ValueError, match="Chat-ID"):
        TelegramMessenger(token=SecretStr(TELEGRAM_TOKEN), chat_id="")


def test_telegram_satisfies_protocol() -> None:
    """Der Adapter erfüllt das `Messenger`-Protokoll aus `messenger/base.py`."""
    adapter = TelegramMessenger(token=SecretStr(TELEGRAM_TOKEN), chat_id="1")
    assert isinstance(adapter, Messenger)
    adapter.close()


# --- Discord ---------------------------------------------------------------------------


def test_discord_posts_content_only() -> None:
    """Der Webhook-Post enthält nur `content` — keine Embeds, keine Mentions."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    adapter = DiscordMessenger(
        webhook_url=SecretStr(WEBHOOK_URL), client=client_for(handler)
    )
    adapter.send(message("Teil eins", "Teil zwei"))

    assert [payload["content"] for payload in seen] == ["Teil eins", "Teil zwei"]
    for payload in seen:
        assert "embeds" not in payload
        assert payload["allowed_mentions"] == {"parse": []}


def test_discord_error_does_not_leak_webhook_url() -> None:
    """Die Webhook-URL ist ein Secret und darf nicht in der Fehlermeldung stehen (I5)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Unknown Webhook"})

    adapter = DiscordMessenger(
        webhook_url=SecretStr(WEBHOOK_URL), client=client_for(handler)
    )
    with pytest.raises(MessengerError) as excinfo:
        adapter.send(message("Inhalt"))
    assert "GEHEIMES-WEBHOOK-SECRET" not in str(excinfo.value)
    assert "404" in str(excinfo.value)


def test_discord_healthcheck_and_repr() -> None:
    """Healthcheck über GET; `repr` nennt die URL nicht."""

    def ok(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "42"})

    adapter = DiscordMessenger(webhook_url=SecretStr(WEBHOOK_URL), client=client_for(ok))
    assert adapter.healthcheck() is True
    assert "GEHEIMES-WEBHOOK-SECRET" not in repr(adapter)


def test_discord_rejects_invalid_webhook_url() -> None:
    """Eine leere oder schemalose Webhook-URL wird sofort abgelehnt."""
    with pytest.raises(ValueError):
        DiscordMessenger(webhook_url=SecretStr(""))
    with pytest.raises(ValueError):
        DiscordMessenger(webhook_url=SecretStr("discord.example/webhook"))


def test_discord_satisfies_protocol() -> None:
    """Der Adapter erfüllt das `Messenger`-Protokoll."""
    adapter = DiscordMessenger(webhook_url=SecretStr(WEBHOOK_URL))
    assert isinstance(adapter, Messenger)
    adapter.close()


# --- Signal ----------------------------------------------------------------------------


class FakeSocket:
    """Attrappe eines signal-cli-Sockets: sammelt Requests, liefert feste Antworten."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._buffer = b""

    def sendall(self, data: bytes) -> None:
        self.sent.append(json.loads(data.decode("utf-8")))
        self._buffer = (self.responses.pop(0) + "\n").encode("utf-8")

    def recv(self, bufsize: int) -> bytes:
        chunk, self._buffer = self._buffer[:bufsize], self._buffer[bufsize:]
        return chunk

    def close(self) -> None:
        self.closed = True


def test_signal_sends_note_to_self() -> None:
    """Jeder Teil geht als `send`-Kommando an „Note to Self"."""
    socket = FakeSocket([json.dumps({"jsonrpc": "2.0", "id": "1", "result": {}})] * 2)
    adapter = SignalMessenger(socket_path="/tmp/signal.sock", connect=lambda *_: socket)
    adapter.send(message("Teil eins", "Teil zwei"))

    assert [call["method"] for call in socket.sent] == ["send", "send"]
    assert socket.sent[0]["params"] == {"noteToSelf": True, "message": "Teil eins"}
    assert socket.closed is True


def test_signal_reports_missing_daemon_clearly() -> None:
    """Fehlt signal-cli, gibt es eine verständliche Meldung statt eines Tracebacks."""

    def refuse(path: str, timeout: float) -> Any:
        raise FileNotFoundError(path)

    adapter = SignalMessenger(socket_path="/tmp/fehlt.sock", connect=refuse)
    with pytest.raises(MessengerError) as excinfo:
        adapter.send(message("Inhalt"))
    assert "signal-cli" in str(excinfo.value)


def test_signal_rpc_error_is_reported() -> None:
    """Ein Fehlerobjekt in der Antwort ist ein Fehlschlag."""
    socket = FakeSocket([json.dumps({"jsonrpc": "2.0", "id": "1", "error": {"code": -1}})])
    adapter = SignalMessenger(socket_path="/tmp/signal.sock", connect=lambda *_: socket)
    with pytest.raises(MessengerError, match="Code"):
        adapter.send(message("Inhalt"))


def test_signal_healthcheck() -> None:
    """`version` beantwortet den Healthcheck; ohne Daemon ist er False."""
    socket = FakeSocket([json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"version": "0.13"}})])
    adapter = SignalMessenger(socket_path="/tmp/signal.sock", connect=lambda *_: socket)
    assert adapter.healthcheck() is True

    def refuse(path: str, timeout: float) -> Any:
        raise ConnectionRefusedError(path)

    assert SignalMessenger(socket_path="/tmp/x.sock", connect=refuse).healthcheck() is False


def test_signal_requires_socket_path() -> None:
    """Ohne Socket-Pfad lässt sich der Adapter nicht bauen."""
    with pytest.raises(ValueError, match="Socket"):
        SignalMessenger(socket_path="")


def test_signal_satisfies_protocol() -> None:
    """Der Adapter erfüllt das `Messenger`-Protokoll."""
    assert isinstance(SignalMessenger(socket_path="/tmp/signal.sock"), Messenger)


def test_discord_retries_with_exponential_backoff() -> None:
    """Ohne `Retry-After` gilt das exponentielle Backoff, danach der endgültige Fehler."""
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="kaputt")

    adapter = DiscordMessenger(
        webhook_url=SecretStr(WEBHOOK_URL), client=client_for(handler), sleep=sleep
    )
    with pytest.raises(MessengerError, match="503"):
        adapter.send(message("Inhalt"))
    assert sleep.calls == [1.0, 2.0]


def test_discord_accepts_non_json_success_body() -> None:
    """Ein 200 ohne JSON-Körper ist kein Fehler — es zählt der Status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    adapter = DiscordMessenger(webhook_url=SecretStr(WEBHOOK_URL), client=client_for(handler))
    adapter.send(message("Inhalt"))


def test_discord_connection_error_is_wrapped() -> None:
    """Verbindungsfehler werden zu `MessengerError` (die Pipeline bleibt fail-closed)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kein Netz")

    adapter = DiscordMessenger(webhook_url=SecretStr(WEBHOOK_URL), client=client_for(handler))
    with pytest.raises(MessengerError, match="Verbindung"):
        adapter.send(message("Inhalt"))


def test_signal_invalid_json_response() -> None:
    """Eine unlesbare Antwort ist ein Fehlschlag, kein stiller Erfolg."""
    socket = FakeSocket(["kein json"])
    adapter = SignalMessenger(socket_path="/tmp/signal.sock", connect=lambda *_: socket)
    with pytest.raises(MessengerError, match="JSON"):
        adapter.send(message("Inhalt"))


def test_signal_empty_response() -> None:
    """Bricht die Verbindung ohne Antwort ab, wird das gemeldet."""
    socket = FakeSocket([""])
    adapter = SignalMessenger(socket_path="/tmp/signal.sock", connect=lambda *_: socket)
    with pytest.raises(MessengerError, match="keine Antwort"):
        adapter.send(message("Inhalt"))


def test_signal_skips_empty_parts_and_repr_is_harmless() -> None:
    """Leere Teile werden übersprungen; `repr` nennt nur den Socket-Pfad."""
    socket = FakeSocket([json.dumps({"result": {}})])
    adapter = SignalMessenger(socket_path="/tmp/signal.sock", connect=lambda *_: socket)
    adapter.send(message("", "Inhalt"))
    assert len(socket.sent) == 1
    assert "signal.sock" in repr(adapter)
