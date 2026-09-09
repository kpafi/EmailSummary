"""Anthropic-Messages-API-Adapter über `httpx` (ohne SDK), mit Retry/Backoff/Timeout.

Umsetzung von WP4. Vertrag: :class:`maildigest.llm.base.LLMProvider`.

Bewusst **kein** Anthropic-SDK (ADR zu WP4): Das SDK brächte eine weitere
Laufzeit-Dependency (NF-1/NF-2) und eine vollständige Tool-Use-Oberfläche mit, die wir
wegen I2 aktiv vermeiden müssten. Der genutzte Ausschnitt der API ist ein einziger
POST — der Nutzen einer Abstraktion darüber ist gering, das Risiko (versehentlich
verfügbares Tool-Calling) real.

Der Request-Körper enthält **nie** `tools`, `tool_choice`, `mcp_servers` oder
`container` (I2). Das ist per grep prüfbar (WP12).
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

__all__ = ["ANTHROPIC_API_VERSION", "ANTHROPIC_DEFAULT_BASE_URL", "AnthropicProvider"]

#: Default-Endpunkt der Anthropic-API (per Config überschreibbar, z. B. für Proxys).
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Pflicht-Header `anthropic-version` der Messages-API.
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider:
    """Text-in/Text-out-Adapter für `POST {base_url}/v1/messages`.

    Der API-Key wird als :class:`pydantic.SecretStr` gehalten und nur beim Bauen der
    Header ausgepackt; `repr()`/`str()` der Instanz enthalten ihn nicht (I5).
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        reveal_error_details: bool = False,
    ) -> None:
        """Baut den Adapter.

        Args:
            model: Modell-ID aus der Config. Kein Default im Code — Modell-IDs veralten,
                ein hartkodierter Default würde stillschweigend falsch liegen
                (docs/ARCHITECTURE.md §5).
            api_key: API-Key aus Config oder `MAILDIGEST_LLM_API_KEY`.
            base_url: Abweichender Endpunkt-Host; leer = :data:`ANTHROPIC_DEFAULT_BASE_URL`.
            timeout: Zeitlimit je Aufruf in Sekunden.
            max_attempts: Gesamtzahl der Versuche bei 429/5xx.
            client: Vorhandener httpx-Client (Tests: `httpx.Client(transport=MockTransport(...))`).
            sleep: Wartefunktion für das Backoff (Tests injizieren eine Attrappe).
        """
        if not model:
            raise ValueError("The model name must not be empty.")
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
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
            f"AnthropicProvider(model={self._model!r}, base_url={self._base_url!r}, "
            f"timeout={self._timeout!r})"
        )

    __str__ = __repr__

    # --- LLMProvider --------------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        """Siehe :meth:`maildigest.llm.base.LLMProvider.complete`."""
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # `temperature` nur senden, wenn explizit gesetzt: aktuelle Modelle lehnen das
        # Feld mit HTTP 400 ab, ein stiller Default würde sie unbenutzbar machen.
        if temperature is not None:
            payload["temperature"] = temperature

        headers = {
            "x-api-key": self._api_key.get_secret_value(),
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        data = post_json(
            self._client,
            f"{self._base_url}/v1/messages",
            headers=headers,
            payload=payload,
            provider="anthropic",
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
    """Setzt die `text`-Blöcke der Antwort zusammen.

    Andere Blocktypen (z. B. `thinking`) werden ignoriert. Fehlt jeder Textblock, ist die
    Antwort für uns unbrauchbar ⇒ :class:`LLMInvalidResponse` (fail-closed, I6). Die
    Fehlermeldung nennt nur die Struktur, nie den Inhalt (I5).
    """
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise LLMInvalidResponse(
            "anthropic: the response has no `content` field with a block list."
            + body_error_suffix(data, reveal=reveal)
        )
    parts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    if not parts:
        raise LLMInvalidResponse(
            f"anthropic: the response has no text block ({len(blocks)} blocks, "
            f"stop_reason={data.get('stop_reason')!r})."
        )
    return "".join(parts)
