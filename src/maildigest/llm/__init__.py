"""LLM-Subpaket: schmale, austauschbare Provider-Abstraktion.

Text-in/Text-out, ohne Tool-Use (I2). Umgesetzt in WP4:

* :mod:`maildigest.llm.base` — Protokoll und Fehlerklassen,
* :mod:`maildigest.llm.anthropic` / :mod:`maildigest.llm.openai` — HTTP-Adapter,
* :mod:`maildigest.llm.schema` — `complete_json` mit Schema-Zwang (I4/I6),
* :mod:`maildigest.llm.factory` — Provider-Auswahl aus der Config,
* :mod:`maildigest.llm.prompts` — Prompt-Texte (WP5/WP6).
"""

from maildigest.llm.anthropic import AnthropicProvider
from maildigest.llm.base import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    LLMError,
    LLMInvalidResponse,
    LLMProvider,
    LLMRateLimited,
    LLMTimeout,
    LLMTransportError,
)
from maildigest.llm.factory import LLMRole, build_provider, max_tokens_for
from maildigest.llm.openai import OpenAICompatibleProvider
from maildigest.llm.schema import complete_json, extract_json_object

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "AnthropicProvider",
    "LLMError",
    "LLMInvalidResponse",
    "LLMProvider",
    "LLMRateLimited",
    "LLMRole",
    "LLMTimeout",
    "LLMTransportError",
    "OpenAICompatibleProvider",
    "build_provider",
    "complete_json",
    "extract_json_object",
    "max_tokens_for",
]
