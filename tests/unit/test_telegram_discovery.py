"""Tests der Chat-ID-Ermittlung `messenger.telegram.discover_chat_ids` (WP9, F-MSG-2).

Alles läuft gegen `httpx.MockTransport` — kein Netzzugriff. Wichtigster Punkt neben dem
Happy-Path: Aus einem Update werden **nur** die numerische Chat-ID und ein Chat-Typ aus
fester Werteliste übernommen, nie ein vom Absender gewählter Text (ADR-055).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from maildigest.messenger.base import MessengerError
from maildigest.messenger.telegram import ChatCandidate, discover_chat_ids

TOKEN = SecretStr("123456:GEHEIMES-BOT-TOKEN")


def client_for(handler: Any) -> httpx.Client:
    """httpx-Client auf einem MockTransport."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def responder(payload: dict[str, Any], status: int = 200) -> Any:
    """Handler, der immer dieselbe Antwort liefert."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getUpdates")
        return httpx.Response(status, json=payload)

    return handler


def update(chat: dict[str, Any], key: str = "message") -> dict[str, Any]:
    """Baut ein Update-Objekt, wie Telegram es liefert."""
    return {"update_id": 1, key: {"message_id": 7, "chat": chat, "text": "/start"}}


def test_findet_die_chat_id() -> None:
    payload = {"ok": True, "result": [update({"id": 4711, "type": "private"})]}
    found = discover_chat_ids(token=TOKEN, client=client_for(responder(payload)))
    assert found == [ChatCandidate(chat_id="4711", chat_type="private")]


def test_uebernimmt_keine_namen_aus_dem_chat() -> None:
    chat = {
        "id": -1001,
        "type": "supergroup",
        "title": "\x1b[31mBÖSE\x1b[0m",
        "first_name": "Angreifer",
        "username": "evil",
    }
    found = discover_chat_ids(token=TOKEN, client=client_for(responder({"result": [update(chat)]})))
    assert found == [ChatCandidate(chat_id="-1001", chat_type="supergroup")]


def test_unbekannter_chat_typ_wird_neutralisiert() -> None:
    payload = {"result": [update({"id": 5, "type": "<script>alert(1)</script>"})]}
    found = discover_chat_ids(token=TOKEN, client=client_for(responder(payload)))
    assert found == [ChatCandidate(chat_id="5", chat_type="unknown")]


def test_dedupliziert_und_haelt_die_reihenfolge() -> None:
    payload = {
        "result": [
            update({"id": 1, "type": "private"}),
            update({"id": 2, "type": "group"}),
            update({"id": 1, "type": "private"}),
        ]
    }
    found = discover_chat_ids(token=TOKEN, client=client_for(responder(payload)))
    assert [item.chat_id for item in found] == ["1", "2"]


def test_liest_auch_bearbeitete_nachrichten_und_kanalposts() -> None:
    payload = {
        "result": [
            update({"id": 8, "type": "private"}, key="edited_message"),
            update({"id": 9, "type": "channel"}, key="channel_post"),
        ]
    }
    found = discover_chat_ids(token=TOKEN, client=client_for(responder(payload)))
    assert [item.chat_id for item in found] == ["8", "9"]


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "result": []},
        {"ok": True, "result": "kein-array"},
        {"ok": True},
        {"result": [{"update_id": 1}]},
        {"result": [{"message": {"chat": {"type": "private"}}}]},
        {"result": [{"message": {"chat": {"id": "nicht-numerisch", "type": "private"}}}]},
        {"result": [{"message": {"chat": {"id": True, "type": "private"}}}]},
        {"result": ["kein-objekt"]},
        {"result": [{"message": "kein-objekt"}]},
    ],
)
def test_unbrauchbare_antworten_ergeben_eine_leere_liste(payload: dict[str, Any]) -> None:
    assert discover_chat_ids(token=TOKEN, client=client_for(responder(payload))) == []


def test_falsches_token_wirft_messenger_error() -> None:
    with pytest.raises(MessengerError) as excinfo:
        discover_chat_ids(token=TOKEN, client=client_for(responder({}, status=401)))
    # I5: Die URL trägt das Token — es darf nie in der Meldung stehen.
    assert TOKEN.get_secret_value() not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_api_fehlerobjekt_wirft_messenger_error() -> None:
    payload = {"ok": False, "error_code": 409, "description": "Conflict"}
    with pytest.raises(MessengerError) as excinfo:
        discover_chat_ids(token=TOKEN, client=client_for(responder(payload)))
    assert "409" in str(excinfo.value)


def test_eigene_basis_url_wird_benutzt() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"result": []})

    discover_chat_ids(
        token=TOKEN, base_url="http://localhost:9000/", client=client_for(handler)
    )
    assert seen[0].startswith("http://localhost:9000/bot")


def test_ohne_client_wird_einer_angelegt_und_geschlossen(monkeypatch: Any) -> None:
    erzeugt: list[httpx.Client] = []
    original = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        client = original(transport=httpx.MockTransport(responder({"result": []})))
        erzeugt.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", factory)
    assert discover_chat_ids(token=TOKEN) == []
    assert erzeugt and erzeugt[0].is_closed
