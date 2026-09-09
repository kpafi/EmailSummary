"""Provider-Factory: aus der Config den passenden :class:`LLMProvider` bauen.

Die Config kennt `[llm]` und den optionalen Override `[llm.critic]`
(docs/ARCHITECTURE.md §5). Damit können Summarizer und Kritiker verschiedene Modelle —
oder sogar verschiedene Provider — benutzen, ohne dass die Agenten (WP5/WP6) etwas über
HTTP wissen müssen.

Die Rolle wird als Literal übergeben statt als zwei getrennte Funktionen, damit WP5/WP6
denselben Aufruf mit unterschiedlichem Argument verwenden und eine spätere dritte Rolle
nur einen Literal-Wert kostet.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import SecretStr

from maildigest.config import Config, ConfigError
from maildigest.llm.anthropic import AnthropicProvider
from maildigest.llm.base import DEFAULT_TIMEOUT_SECONDS, MAX_ATTEMPTS, LLMProvider
from maildigest.llm.openai import OpenAICompatibleProvider

__all__ = ["LLMRole", "build_provider", "build_provider_from_settings", "max_tokens_for"]

#: Für welche Pipeline-Stufe der Provider gebaut wird.
LLMRole = Literal["summarizer", "critic"]


def max_tokens_for(config: Config, role: LLMRole = "summarizer") -> int:
    """Liefert das Token-Limit der Rolle (Override aus `[llm.critic]`, sonst `[llm]`)."""
    return config.critic_max_tokens() if role == "critic" else config.llm.max_tokens


def build_provider(
    config: Config,
    role: LLMRole = "summarizer",
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMProvider:
    """Baut den Provider für die angegebene Rolle.

    Args:
        config: Validierte Gesamt-Config.
        role: `"summarizer"` nutzt `[llm]`, `"critic"` zusätzlich die Overrides aus
            `[llm.critic]`.
        timeout: Zeitlimit je Aufruf in Sekunden.
        max_attempts: Gesamtzahl der Versuche bei 429/5xx.
        client: Vorhandener httpx-Client (Tests/Connection-Pooling).
        sleep: Wartefunktion für das Backoff.

    Returns:
        Einen einsatzbereiten :class:`LLMProvider`.

    Raises:
        ConfigError: Der Anthropic-Provider ist gewählt, aber es liegt kein API-Key vor.
            Die Meldung nennt die Umgebungsvariable, nie einen Wert (I5).
    """
    if role == "critic":
        provider_name = config.critic_provider()
        model = config.critic_model()
        base_url = config.critic_base_url()
    else:
        provider_name = config.llm.provider
        model = config.llm.model
        base_url = config.llm.base_url

    return build_provider_from_settings(
        provider=provider_name,
        model=model,
        base_url=base_url,
        api_key=config.llm.api_key,
        timeout=timeout,
        max_attempts=max_attempts,
        client=client,
        sleep=sleep,
        # Bewusst nicht durchgereicht: Im Normalbetrieb bleibt der Antworttext des
        # Anbieters außen vor (I5). Nur `connect-llm` schaltet ihn für seinen
        # inhaltsfreien Testaufruf ein.
    )


def build_provider_from_settings(
    *,
    provider: str,
    model: str,
    base_url: str = "",
    api_key: SecretStr | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    reveal_error_details: bool = False,
) -> LLMProvider:
    """Baut einen Provider aus Einzelwerten statt aus einer vollständigen :class:`Config`.

    Existiert für `maildigest connect-llm` (WP9): Beim Einrichten ist die Gesamt-Config
    typischerweise noch unvollständig (kein IMAP-Host), ein Testaufruf muss aber schon
    möglich sein. :func:`build_provider` ist ein dünner Aufsatz darauf, damit es nur eine
    Stelle gibt, an der Provider-Namen auf Klassen abgebildet werden.

    Raises:
        ConfigError: Anthropic ohne API-Key.
    """
    if provider == "anthropic":
        if api_key is None:
            raise ConfigError(
                "[llm] api_key fehlt: Der Anthropic-Provider braucht einen API-Key. "
                "Setze ihn in der Konfigurationsdatei oder über die Umgebungsvariable "
                "MAILDIGEST_LLM_API_KEY."
            )
        return AnthropicProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_attempts=max_attempts,
            client=client,
            sleep=sleep,
        )

    # `openai_compatible`: Key ist optional, weil lokale Server (Ollama/vLLM) keinen
    # verlangen. Die Config-Validierung stellt sicher, dass es keinen dritten Wert gibt.
    return OpenAICompatibleProvider(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_attempts=max_attempts,
        client=client,
        sleep=sleep,
        reveal_error_details=reveal_error_details,
    )
