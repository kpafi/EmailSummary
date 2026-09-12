"""Adapter für OpenAI-kompatible `chat/completions`-Endpunkte über `httpx`.

Umsetzung von WP4. Vertrag: :class:`maildigest.llm.base.LLMProvider`.

Die `base_url` ist konfigurierbar und deckt damit auch lokale Server ab (Ollama, vLLM,
llama.cpp, LM Studio …), die dasselbe Schema sprechen — F-LLM-1 verlangt genau das.

Fehler-, Retry- und Timeout-Semantik sind identisch zum Anthropic-Adapter (gemeinsame
Mechanik in `llm/_http.py`). Der Request-Körper enthält **nie** `tools`, `functions` oder
`tool_choice` (I2).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from maildigest.llm._http import body_error_suffix, post_json
from maildigest.llm.base import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    LLMInvalidResponse,
)

__all__ = ["OPENAI_DEFAULT_BASE_URL", "OpenAICompatibleProvider"]

#: Default-Basis-URL, falls die Config keine nennt (öffentliche OpenAI-API).
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleProvider:
    """Text-in/Text-out-Adapter für `POST {base_url}/chat/completions`.

    Der System-Prompt wird als erste Nachricht mit `role="system"` gesendet, der
    Datenblock als `role="user"` — die Trennung aus I8 bleibt damit auch auf der
    Transportebene erhalten.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr | None = None,
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        reveal_error_details: bool = False,
    ) -> None:
        """Baut den Adapter.

        Args:
            model: Modell-ID aus der Config (kein Default im Code).
            api_key: Bearer-Token. Optional, weil lokale Server (Ollama/vLLM) häufig
                keinen Key verlangen; ohne Key wird kein `Authorization`-Header gesendet.
            base_url: Endpunkt-Basis inkl. Versionspfad, z. B.
                `http://localhost:11434/v1`; leer = :data:`OPENAI_DEFAULT_BASE_URL`.
            timeout: Zeitlimit je Aufruf in Sekunden.
            max_attempts: Gesamtzahl der Versuche bei 429/5xx.
            client: Vorhandener httpx-Client (Tests: `MockTransport`).
            sleep: Wartefunktion für das Backoff.
        """
        if not model:
            raise ValueError("The model name must not be empty.")
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or OPENAI_DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        # Nur der Verbindungstest von `connect-llm` schaltet das ein (I5).
        self._reveal_error_details = reveal_error_details
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None

    # --- Introspektion ------------------------------------------------------------

    @property
    def model(self) -> str:
        """Die konfigurierte Modell-ID (kein Secret)."""
        return self._model

    def __repr__(self) -> str:
        """Repräsentation ohne API-Key (I5)."""
        return (
            f"OpenAICompatibleProvider(model={self._model!r}, "
            f"base_url={self._base_url!r}, timeout={self._timeout!r})"
        )

    __str__ = __repr__

    # --- LLMProvider --------------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None,
        temperature: float | None = None,
    ) -> str:
        """Siehe :meth:`maildigest.llm.base.LLMProvider.complete`."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # `max_tokens` nur senden, wenn ein Limit gesetzt ist (ADR-085): ohne das Feld
        # gilt die Obergrenze des Anbieters bzw. Modells — für Reasoning-Modelle, die ihre
        # Denk-Tokens vom Budget abziehen, ist das der einzige Wert, der nie abschneidet.
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        headers = {"content-type": "application/json"}
        if self._api_key is not None:
            headers["authorization"] = f"Bearer {self._api_key.get_secret_value()}"

        data = post_json(
            self._client,
            f"{self._base_url}/chat/completions",
            headers=headers,
            payload=payload,
            provider="openai_compatible",
            timeout=self._timeout,
            reveal_message=self._reveal_error_details,
            max_attempts=self._max_attempts,
            sleep=self._sleep,
        )
        return _extract_text(data, reveal=self._reveal_error_details)

    def close(self) -> None:
        """Schließt den intern erzeugten httpx-Client; injizierte Clients bleiben offen."""
        if self._owns_client:
            self._client.close()


def _extract_text(data: dict[str, Any], *, reveal: bool = False) -> str:
    """Liest `choices[0].message.content` und behandelt jede Abweichung als ungültig."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMInvalidResponse(
            "openai_compatible: the response has no `choices`."
            + body_error_suffix(data, reveal=reveal)
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
        raise LLMInvalidResponse(
            "openai_compatible: the response has no text content "
            f"(finish_reason={finish_reason!r})."
        )
    return content
