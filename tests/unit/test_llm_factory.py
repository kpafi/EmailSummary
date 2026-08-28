"""Unit-Tests der Provider-Factory (WP4): Auswahl aus `[llm]` und `[llm.critic]`."""

from __future__ import annotations

from typing import Any

import pytest

from maildigest.config import Config, ConfigError, load_config_from_dict
from maildigest.llm.anthropic import AnthropicProvider
from maildigest.llm.factory import build_provider, max_tokens_for
from maildigest.llm.openai import OpenAICompatibleProvider

BASE_CONFIG: dict[str, Any] = {
    "imap": {"host": "imap.example.org", "username": "mirror@example.org"},
    "llm": {"model": "haupt-modell", "api_key": "sk-test", "max_tokens": 900},
}


def config_with(**llm_overrides: Any) -> Config:
    """Baut eine gültige Config mit angepasster `[llm]`-Sektion (ohne Env-Overrides)."""
    data: dict[str, Any] = {
        "imap": dict(BASE_CONFIG["imap"]),
        "llm": {**BASE_CONFIG["llm"], **llm_overrides},
    }
    return load_config_from_dict(data, env={})


def test_default_role_uses_llm_section() -> None:
    """Ohne Override baut die Factory den Anthropic-Provider aus `[llm]`."""
    provider = build_provider(config_with())
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "haupt-modell"


def test_critic_inherits_when_no_override_is_set() -> None:
    """Ohne `[llm.critic]` bekommt der Kritiker exakt die Werte aus `[llm]`."""
    config = config_with()
    provider = build_provider(config, "critic")
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "haupt-modell"
    assert max_tokens_for(config, "critic") == 900


def test_critic_override_switches_model_and_provider() -> None:
    """`[llm.critic]` kann Modell, Provider, base_url und max_tokens überschreiben."""
    config = config_with(
        critic={
            "provider": "openai_compatible",
            "model": "kritiker-modell",
            "base_url": "http://localhost:11434/v1",
            "max_tokens": 512,
        }
    )
    summarizer = build_provider(config, "summarizer")
    critic = build_provider(config, "critic")

    assert isinstance(summarizer, AnthropicProvider)
    assert isinstance(critic, OpenAICompatibleProvider)
    assert critic.model == "kritiker-modell"
    assert max_tokens_for(config, "summarizer") == 900
    assert max_tokens_for(config, "critic") == 512


def test_openai_compatible_without_api_key_is_allowed() -> None:
    """Lokale Server brauchen keinen Key — das darf die Factory nicht blockieren."""
    config = load_config_from_dict(
        {
            "imap": dict(BASE_CONFIG["imap"]),
            "llm": {
                "provider": "openai_compatible",
                "model": "llama",
                "base_url": "http://localhost:11434/v1",
            },
        },
        env={},
    )
    assert isinstance(build_provider(config), OpenAICompatibleProvider)


def test_anthropic_without_api_key_raises_config_error() -> None:
    """Fehlender Key ⇒ verständliche ConfigError mit Hinweis auf die Env-Variable."""
    config = load_config_from_dict(
        {"imap": dict(BASE_CONFIG["imap"]), "llm": {"model": "m"}}, env={}
    )
    with pytest.raises(ConfigError) as excinfo:
        build_provider(config)

    assert "MAILDIGEST_LLM_API_KEY" in str(excinfo.value)


def test_factory_output_repr_has_no_secret() -> None:
    """I5: Auch der von der Factory gebaute Provider zeigt den Key nirgends."""
    provider = build_provider(config_with(api_key="sk-super-geheim"))
    assert "sk-super-geheim" not in repr(provider)
