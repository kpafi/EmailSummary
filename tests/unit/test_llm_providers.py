"""Unit-Tests der LLM-Provider (WP4) gegen `httpx.MockTransport` — kein Netzzugriff.

Geprüft werden: Happy-Path beider Provider, Retry mit Backoff bei 429 und 5xx, Timeout,
das Fehlen jeglicher Tool-Parameter im Request (I2) und die Secret-Freiheit von
`repr`/`str`/Fehlertexten (I5).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from maildigest.llm.anthropic import ANTHROPIC_API_VERSION, AnthropicProvider
from maildigest.llm.base import (
    LLMInvalidResponse,
    LLMProvider,
    LLMRateLimited,
    LLMTimeout,
    LLMTransportError,
)
from maildigest.llm.openai import OpenAICompatibleProvider

SECRET = "sk-geheim-4711-nicht-loggen"


class SleepSpy:
    """Attrappe für `time.sleep`, die die Wartezeiten aufzeichnet statt zu warten."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def anthropic_body(text: str) -> dict[str, Any]:
    """Minimale, realistisch geformte Antwort der Anthropic-Messages-API."""
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "test-model",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def openai_body(text: str) -> dict[str, Any]:
    """Minimale, realistisch geformte Antwort eines chat/completions-Endpunkts."""
    return {
        "id": "chatcmpl-01",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


def make_anthropic(
    handler: Any, sleep: SleepSpy | None = None, **kwargs: Any
) -> AnthropicProvider:
    """Baut einen Anthropic-Provider auf einem MockTransport."""
    return AnthropicProvider(
        model="test-model",
        api_key=SecretStr(SECRET),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep or SleepSpy(),
        **kwargs,
    )


def make_openai(
    handler: Any, sleep: SleepSpy | None = None, **kwargs: Any
) -> OpenAICompatibleProvider:
    """Baut einen OpenAI-kompatiblen Provider auf einem MockTransport."""
    return OpenAICompatibleProvider(
        model="test-model",
        api_key=SecretStr(SECRET),
        base_url="http://localhost:11434/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep or SleepSpy(),
        **kwargs,
    )


# --- Happy-Path --------------------------------------------------------------------


def test_anthropic_happy_path_sends_expected_request() -> None:
    """Erfolgsfall: korrekte URL, Header und Prompt-Trennung; Text kommt zurück."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=anthropic_body("Antwort"))

    provider = make_anthropic(handler)
    result = provider.complete("SYSTEM", "USER", max_tokens=256)

    assert result == "Antwort"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == SECRET
    assert seen["headers"]["anthropic-version"] == ANTHROPIC_API_VERSION
    assert seen["payload"]["model"] == "test-model"
    assert seen["payload"]["max_tokens"] == 256
    assert seen["payload"]["system"] == "SYSTEM"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "USER"}]


def test_anthropic_joins_multiple_text_blocks_and_ignores_others() -> None:
    """Mehrere Textblöcke werden verkettet, Nicht-Text-Blöcke ignoriert."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "Teil 1 "},
                    {"type": "text", "text": "Teil 2"},
                ]
            },
        )

    assert make_anthropic(handler).complete("s", "u", max_tokens=10) == "Teil 1 Teil 2"


def test_openai_happy_path_sends_system_and_user_separately() -> None:
    """Erfolgsfall OpenAI-kompatibel: base_url wird genutzt, I8-Trennung bleibt erhalten."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=openai_body("Hallo"))

    result = make_openai(handler).complete("SYSTEM", "USER", max_tokens=64)

    assert result == "Hallo"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["headers"]["authorization"] == f"Bearer {SECRET}"
    assert seen["payload"]["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]


def test_openai_without_api_key_sends_no_authorization_header() -> None:
    """Lokale Server ohne Key: kein `Authorization`-Header."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=openai_body("ok"))

    provider = OpenAICompatibleProvider(
        model="m",
        api_key=None,
        base_url="http://localhost:8000/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=SleepSpy(),
    )
    provider.complete("s", "u", max_tokens=8)
    assert "authorization" not in seen["headers"]


# --- Invariante I2: kein Tool-Use ---------------------------------------------------


@pytest.mark.parametrize("factory", [make_anthropic, make_openai])
def test_request_never_contains_tool_parameters(factory: Any) -> None:
    """I2: Kein Codepfad reicht Tools/Funktionen ans Modell."""
    seen: dict[str, Any] = {}
    forbidden = {
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "mcp_servers",
        "container",
        "skills",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        body = anthropic_body("x") if factory is make_anthropic else openai_body("x")
        return httpx.Response(200, json=body)

    factory(handler).complete("s", "u", max_tokens=16)
    assert forbidden.isdisjoint(seen["payload"].keys())


@pytest.mark.parametrize("factory", [make_anthropic, make_openai])
def test_temperature_is_only_sent_when_explicitly_given(factory: Any) -> None:
    """`temperature=None` (Default) sendet das Feld nicht — moderne Modelle lehnen es ab."""
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        body = anthropic_body("x") if factory is make_anthropic else openai_body("x")
        return httpx.Response(200, json=body)

    provider = factory(handler)
    provider.complete("s", "u", max_tokens=16)
    provider.complete("s", "u", max_tokens=16, temperature=0.3)

    assert "temperature" not in payloads[0]
    assert payloads[1]["temperature"] == 0.3


# --- Retry-Verhalten ----------------------------------------------------------------


def test_rate_limit_is_retried_with_backoff_then_succeeds() -> None:
    """429 → Backoff → Erfolg: der zweite Versuch zählt, gewartet wird exponentiell."""
    attempts: list[int] = []
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": {"type": "rate_limit_error"}})
        return httpx.Response(200, json=anthropic_body("endlich"))

    provider = make_anthropic(handler, sleep=sleep)
    assert provider.complete("s", "u", max_tokens=16) == "endlich"
    assert len(attempts) == 2
    assert sleep.calls == [1.0]


def test_rate_limit_honours_retry_after_header() -> None:
    """Nennt der Server `Retry-After`, gilt dieser Wert statt des Backoffs."""
    sleep = SleepSpy()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={})
        return httpx.Response(200, json=anthropic_body("ok"))

    make_anthropic(handler, sleep=sleep).complete("s", "u", max_tokens=16)
    assert sleep.calls == [7.0]


def test_rate_limit_gives_up_after_three_attempts() -> None:
    """Dauerhaftes 429: genau drei Versuche, zwei Wartezeiten, dann LLMRateLimited."""
    calls: list[int] = []
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"error": {"type": "rate_limit_error"}})

    provider = make_anthropic(handler, sleep=sleep)
    with pytest.raises(LLMRateLimited) as excinfo:
        provider.complete("s", "u", max_tokens=16)

    assert len(calls) == 3
    assert sleep.calls == [1.0, 2.0]
    assert "429" in str(excinfo.value)


def test_server_error_is_retried_then_raises_transport_error() -> None:
    """5xx wird wiederholt; bleibt es dabei, ist es ein Transportfehler."""
    calls: list[int] = []
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": {"type": "overloaded_error"}})

    with pytest.raises(LLMTransportError) as excinfo:
        make_openai(handler, sleep=sleep).complete("s", "u", max_tokens=16)

    assert len(calls) == 3
    assert sleep.calls == [1.0, 2.0]
    assert "503" in str(excinfo.value)


def test_server_error_then_success_returns_text() -> None:
    """5xx → Erfolg: der Aufruf gelingt ohne Fehler nach außen."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=openai_body("nach 500"))

    assert make_openai(handler).complete("s", "u", max_tokens=16) == "nach 500"
    assert len(calls) == 2


def test_client_error_is_not_retried() -> None:
    """401 ist nicht wiederholbar: genau ein Versuch, keine Wartezeit."""
    calls: list[int] = []
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"type": "authentication_error"}})

    with pytest.raises(LLMTransportError):
        make_anthropic(handler, sleep=sleep).complete("s", "u", max_tokens=16)

    assert len(calls) == 1
    assert sleep.calls == []


def test_timeout_raises_llm_timeout_without_retry() -> None:
    """Timeouts werden nicht wiederholt (WP8 entscheidet über Stufen-Retries)."""
    calls: list[int] = []
    sleep = SleepSpy()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("zu langsam", request=request)

    with pytest.raises(LLMTimeout):
        make_anthropic(handler, sleep=sleep).complete("s", "u", max_tokens=16)

    assert len(calls) == 1
    assert sleep.calls == []


def test_connection_error_raises_transport_error() -> None:
    """Verbindungsfehler werden als Transportfehler gemeldet, nicht als Timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kein Server", request=request)

    with pytest.raises(LLMTransportError):
        make_openai(handler).complete("s", "u", max_tokens=16)


# --- Unbrauchbare Antworten ---------------------------------------------------------


def test_anthropic_without_text_block_raises_invalid_response() -> None:
    """Antwort ohne Textblock ⇒ LLMInvalidResponse (fail-closed, I6)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [], "stop_reason": "max_tokens"})

    with pytest.raises(LLMInvalidResponse):
        make_anthropic(handler).complete("s", "u", max_tokens=16)


def test_openai_without_choices_raises_invalid_response() -> None:
    """Antwort ohne `choices` ⇒ LLMInvalidResponse."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(LLMInvalidResponse):
        make_openai(handler).complete("s", "u", max_tokens=16)


def test_non_json_success_response_raises_transport_error() -> None:
    """HTTP 200 mit HTML-Körper (Proxy-Fehlerseite) ist ein Transportfehler."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy</html>")

    with pytest.raises(LLMTransportError):
        make_openai(handler).complete("s", "u", max_tokens=16)


# --- Invariante I5: keine Secrets nach außen ----------------------------------------


@pytest.mark.parametrize("factory", [make_anthropic, make_openai])
def test_secret_never_appears_in_repr_or_str(factory: Any) -> None:
    """I5: `repr`/`str` des Providers enthalten den API-Key nicht."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    provider = factory(handler)
    assert SECRET not in repr(provider)
    assert SECRET not in str(provider)


@pytest.mark.parametrize("status", [401, 429, 500])
@pytest.mark.parametrize("factory", [make_anthropic, make_openai])
def test_secret_never_appears_in_error_messages(factory: Any, status: int) -> None:
    """I5: Auch Fehlertexte (inkl. `repr` der Exception) sind secret-frei."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Bösartiger Server, der den Key im Fehlertext zurückspiegelt.
        return httpx.Response(
            status,
            json={"error": {"type": "boom", "message": f"key war {SECRET}"}},
        )

    with pytest.raises(Exception) as excinfo:
        factory(handler).complete("s", "u", max_tokens=16)

    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


# --- Protokoll-Konformität ----------------------------------------------------------


@pytest.mark.parametrize("factory", [make_anthropic, make_openai])
def test_providers_satisfy_the_protocol(factory: Any) -> None:
    """Beide Adapter erfüllen dasselbe Interface (Akzeptanzkriterium WP4)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    assert isinstance(factory(handler), LLMProvider)


# --- Diagnose-Auskunft des Anbieters (nur beim Verbindungstest) -------------------------------


def _error_transport(status: int, message: str) -> httpx.MockTransport:
    """Antwortet immer mit einer Fehlerantwort im OpenAI-/Anthropic-Format."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "code": status}})

    return httpx.MockTransport(handler)


def test_anbietertext_bleibt_im_normalbetrieb_aussen_vor() -> None:
    """I5: Der Antworttext könnte Teile der Anfrage zitieren — im Betrieb also nicht zeigen."""
    client = httpx.Client(transport=_error_transport(401, "User not found."))
    provider = OpenAICompatibleProvider(model="m", client=client, max_attempts=1)

    with pytest.raises(LLMTransportError) as excinfo:
        provider.complete("s", "u", max_tokens=8)

    assert "User not found" not in str(excinfo.value)
    assert "HTTP 401" in str(excinfo.value)


def test_anbietertext_erscheint_beim_verbindungstest() -> None:
    """Beim festen, inhaltsfreien Testaufruf ist der Text die eigentliche Auskunft.

    Ohne ihn stand bei einem abgelehnten OpenRouter-Zugang nur „HTTP 401" da — die
    Ursache („No endpoints found matching your data policy", „User not found.") blieb
    verborgen und war ohne fremde Hilfe nicht zu erraten.
    """
    client = httpx.Client(transport=_error_transport(401, "User not found."))
    provider = OpenAICompatibleProvider(
        model="m", client=client, max_attempts=1, reveal_error_details=True
    )

    with pytest.raises(LLMTransportError) as excinfo:
        provider.complete("s", "u", max_tokens=8)

    assert 'Provider says: "User not found."' in str(excinfo.value)


def test_anbietertext_wird_gekuerzt_und_einzeilig_gemacht() -> None:
    """Ein Anbieter darf die Fehlermeldung nicht zur Textwand machen."""
    client = httpx.Client(transport=_error_transport(400, "zeile1\nzeile2 " + "x" * 500))
    provider = OpenAICompatibleProvider(
        model="m", client=client, max_attempts=1, reveal_error_details=True
    )

    with pytest.raises(LLMTransportError) as excinfo:
        provider.complete("s", "u", max_tokens=8)

    message = str(excinfo.value)
    assert "\n" not in message
    assert len(message) < 450


def _body_error_transport(payload: dict[str, object]) -> httpx.MockTransport:
    """HTTP 200 mit einem Fehler-Objekt im Körper — die OpenRouter-Eigenart."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_fehler_im_koerper_wird_erkannt_statt_verschluckt() -> None:
    """OpenRouter meldet manche Fehler mit HTTP 200 im Antwortkörper.

    Ohne diese Auswertung blieb davon nur „die Antwort hat kein `choices`" übrig — formal
    richtig, aber für die Fehlersuche wertlos.
    """
    payload = {"error": {"message": "No endpoints found matching your data policy", "code": 404}}
    client = httpx.Client(transport=_body_error_transport(payload))
    provider = OpenAICompatibleProvider(model="m", client=client, max_attempts=1)

    with pytest.raises(LLMInvalidResponse) as excinfo:
        provider.complete("s", "u", max_tokens=8)

    message = str(excinfo.value)
    assert "error in the response body" in message
    assert "404" in message
    # Ohne Freigabe bleibt der Klartext des Anbieters außen vor (I5).
    assert "data policy" not in message


def test_fehler_im_koerper_wird_beim_verbindungstest_ausgeschrieben() -> None:
    """Beim inhaltsfreien Testaufruf ist genau dieser Satz die gesuchte Auskunft."""
    payload = {"error": {"message": "No endpoints found matching your data policy", "code": 404}}
    client = httpx.Client(transport=_body_error_transport(payload))
    provider = OpenAICompatibleProvider(
        model="m", client=client, max_attempts=1, reveal_error_details=True
    )

    with pytest.raises(LLMInvalidResponse) as excinfo:
        provider.complete("s", "u", max_tokens=8)

    assert "No endpoints found matching your data policy" in str(excinfo.value)


def test_gueltige_antwort_bleibt_unberuehrt() -> None:
    """Der neue Zweig darf den Normalfall nicht anfassen."""
    payload = {"choices": [{"message": {"content": "OK"}}]}
    client = httpx.Client(transport=_body_error_transport(payload))
    provider = OpenAICompatibleProvider(model="m", client=client, max_attempts=1)

    assert provider.complete("s", "u", max_tokens=8) == "OK"
