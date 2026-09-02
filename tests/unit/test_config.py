"""Unit-Tests (WP1) für das Config-Laden/-Validieren aus `maildigest.config`.

Abgedeckt: gültige Minimal-/Vollkonfiguration, fehlende Pflichtfelder, ungültige Werte,
unbekannte Felder, kaputtes TOML, fehlende Datei und die Secret-Overrides via Env (I5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maildigest.config import (
    ENV_IMAP_PASSWORD,
    ENV_LLM_API_KEY,
    ENV_TELEGRAM_TOKEN,
    Config,
    ConfigError,
    load_config,
    load_config_from_dict,
)

MINIMAL_TOML = """
[imap]
host = "imap.example.org"
username = "mirror@example.org"

[llm]
model = "test-model-1"
"""

FULL_TOML = """
[general]
language = "en"
summary_length = "long"
deliver_min_importance = "high"
low_digest_time = "07:05"

[imap]
host = "imap.example.org"
port = 993
username = "mirror@example.org"
password = "aus-der-datei"
folder = "Mirror"
poll_interval_seconds = 60
move_processed_to = "Processed"

[llm]
provider = "openai_compatible"
model = "lokales-modell"
api_key = "key-aus-datei"
base_url = "http://localhost:11434/v1"
max_tokens = 2048

[llm.critic]
model = "kleines-kritiker-modell"
max_tokens = 512

[summarizer]
instructions = "Newsletter nur einzeilig."

[links]
footnote = true

[messenger]
active = "discord"

[messenger.telegram]
chat_id = "12345"

[messenger.discord]
webhook_url = "https://example.invalid/webhook"

[messenger.signal]
enabled = false

[limits]
max_text_chars = 12345
"""


def _write(tmp_path: Path, content: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_minimal_config_uses_documented_defaults(tmp_path: Path) -> None:
    """Eine Minimalkonfiguration ist gültig; alle Defaults entsprechen ARCHITECTURE §5."""
    config = load_config(_write(tmp_path, MINIMAL_TOML), env={})

    assert isinstance(config, Config)
    assert config.general.language == "de"
    assert config.general.summary_length == "medium"
    assert config.general.deliver_min_importance == "normal"
    assert config.general.low_digest_time == "18:00"
    assert config.imap.port == 993
    assert config.imap.folder == "INBOX"
    assert config.imap.poll_interval_seconds == 120
    assert config.imap.password is None
    assert config.llm.provider == "anthropic"
    assert config.llm.max_tokens == 1024
    assert config.links.footnote is False
    assert config.messenger.active == "telegram"


def test_limit_defaults_match_security_doc(tmp_path: Path) -> None:
    """Die Limit-Defaults entsprechen docs/SECURITY.md §4."""
    limits = load_config(_write(tmp_path, MINIMAL_TOML), env={}).limits

    assert limits.max_mail_bytes == 25 * 1024 * 1024
    assert limits.max_text_chars == 30_000
    assert limits.pdf_max_input_bytes == 10 * 1024 * 1024
    assert limits.pdf_max_output_chars == 50_000
    assert limits.pdf_timeout_seconds == 20
    assert limits.max_mime_depth == 10
    assert limits.max_attachments_processed == 20


def test_full_config_is_parsed(tmp_path: Path) -> None:
    """Eine vollständige Konfiguration wird inklusive aller Sektionen übernommen."""
    config = load_config(_write(tmp_path, FULL_TOML), env={})

    assert config.general.language == "en"
    assert config.general.deliver_min_importance == "high"
    assert config.imap.move_processed_to == "Processed"
    assert config.llm.provider == "openai_compatible"
    assert config.summarizer.instructions == "Newsletter nur einzeilig."
    assert config.links.footnote is True
    assert config.messenger.active == "discord"
    assert config.messenger.telegram.chat_id == "12345"
    assert config.limits.max_text_chars == 12345


def test_critic_overrides_fall_back_to_llm_section(tmp_path: Path) -> None:
    """`[llm.critic]` überschreibt nur gesetzte Felder, der Rest erbt von `[llm]`."""
    config = load_config(_write(tmp_path, FULL_TOML), env={})

    assert config.critic_model() == "kleines-kritiker-modell"
    assert config.critic_max_tokens() == 512
    assert config.critic_provider() == "openai_compatible"
    assert config.critic_base_url() == "http://localhost:11434/v1"


def test_critic_without_override_inherits_everything(tmp_path: Path) -> None:
    """Ohne `[llm.critic]`-Sektion nutzt der Kritiker exakt die `[llm]`-Werte."""
    config = load_config(_write(tmp_path, MINIMAL_TOML), env={})

    assert config.critic_model() == "test-model-1"
    assert config.critic_provider() == "anthropic"
    assert config.critic_max_tokens() == 1024
    assert config.critic_base_url() == ""


def test_secrets_are_masked_in_repr(tmp_path: Path) -> None:
    """Secrets tauchen in Repr/Str nicht im Klartext auf (I5)."""
    config = load_config(_write(tmp_path, FULL_TOML), env={})

    assert config.imap.password is not None
    assert config.imap.password.get_secret_value() == "aus-der-datei"
    assert "aus-der-datei" not in repr(config)
    assert "key-aus-datei" not in repr(config)


# --- Fehlerfälle ---------------------------------------------------------------------------


def test_missing_file_raises_german_error(tmp_path: Path) -> None:
    """Fehlende Datei ⇒ verständliche deutsche Meldung mit Pfad."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "gibtsnicht.toml", env={})

    message = str(excinfo.value)
    assert "nicht gefunden" in message
    assert "gibtsnicht.toml" in message


def test_broken_toml_raises_german_error(tmp_path: Path) -> None:
    """Syntaktisch kaputtes TOML ⇒ Meldung, die auf ungültiges TOML hinweist."""
    path = _write(tmp_path, '[imap\nhost = "x"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "kein gültiges TOML" in str(excinfo.value)


def test_missing_required_fields_are_listed(tmp_path: Path) -> None:
    """Fehlende Pflichtfelder werden einzeln, mit Sektion und deutschem Text genannt."""
    path = _write(tmp_path, "[imap]\nport = 993\n\n[llm]\nprovider = \"anthropic\"\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value)
    assert "Konfiguration ungültig" in message
    assert "[imap] host: Pflichtfeld fehlt." in message
    assert "[imap] username: Pflichtfeld fehlt." in message
    assert "[llm] model: Pflichtfeld fehlt." in message


def test_unreadable_file_raises_german_error(tmp_path: Path) -> None:
    """Ein Verzeichnis statt einer Datei (oder fehlende Rechte) ⇒ deutsche Meldung."""
    directory = tmp_path / "config.toml"
    directory.mkdir()

    with pytest.raises(ConfigError) as excinfo:
        load_config(directory, env={})

    assert "kann nicht gelesen werden" in str(excinfo.value)


def test_non_utf8_file_raises_german_error(tmp_path: Path) -> None:
    """Eine nicht UTF-8-kodierte Datei wird klar benannt statt roh zu crashen."""
    path = tmp_path / "config.toml"
    path.write_bytes(b'[imap]\nhost = "\xff\xfe"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "nicht UTF-8-kodiert" in str(excinfo.value)


def test_error_message_never_contains_secret_values(tmp_path: Path) -> None:
    """I5: Auch im Fehlerfall taucht kein Secret in der Meldung auf."""
    broken = FULL_TOML.replace('summary_length = "long"', 'summary_length = "riesig"')

    with pytest.raises(ConfigError) as excinfo:
        load_config(
            _write(tmp_path, broken), env={ENV_LLM_API_KEY: "geheim-aus-env"}
        )

    message = str(excinfo.value)
    assert "[general] summary_length" in message
    assert "aus-der-datei" not in message
    assert "key-aus-datei" not in message
    assert "geheim-aus-env" not in message


def test_env_override_does_not_mask_wrong_section_type() -> None:
    """Ist `[imap]` keine Tabelle, meldet die Validierung den Typfehler (kein stiller Fix)."""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_dict(
            {"imap": "imap.example.org", "llm": {"model": "m"}},
            env={ENV_IMAP_PASSWORD: "geheim"},
        )

    message = str(excinfo.value)
    assert "imap: Erwartet wird hier eine TOML-Sektion" in message
    assert "geheim" not in message


def test_missing_whole_sections_are_reported() -> None:
    """Fehlen ganze Pflichtsektionen, wird das ebenfalls klar gemeldet."""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_dict({}, env={})

    message = str(excinfo.value)
    assert "imap: Pflichtfeld fehlt." in message
    assert "llm: Pflichtfeld fehlt." in message


def test_invalid_literal_value_lists_allowed_values(tmp_path: Path) -> None:
    """Ein ungültiger Aufzählungswert nennt die erlaubten Alternativen."""
    path = _write(tmp_path, MINIMAL_TOML + '\n[general]\nsummary_length = "riesig"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value)
    assert "[general] summary_length" in message
    assert "Ungültiger Wert" in message
    assert "short" in message and "medium" in message and "long" in message


def test_invalid_time_format_is_reported(tmp_path: Path) -> None:
    """`low_digest_time` muss HH:MM sein."""
    path = _write(tmp_path, MINIMAL_TOML + '\n[general]\nlow_digest_time = "25:99"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "[general] low_digest_time" in str(excinfo.value)
    assert "Ungültiges Format" in str(excinfo.value)


def test_out_of_range_number_is_reported() -> None:
    """Zahlenwerte außerhalb des erlaubten Bereichs werden abgelehnt."""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_dict(
            {
                "imap": {"host": "h", "username": "u", "port": 70000},
                "llm": {"model": "m"},
            },
            env={},
        )

    assert "[imap] port" in str(excinfo.value)
    assert "zu groß" in str(excinfo.value)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    """Tippfehler/unbekannte Felder führen zu einem klaren Fehler statt stiller Ignoranz."""
    path = _write(tmp_path, MINIMAL_TOML + "\n[general]\nsprache = \"de\"\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value)
    assert "[general] sprache" in message
    assert "Unbekanntes Feld" in message


def test_error_message_mentions_env_vars() -> None:
    """Die Fehlermeldung weist auf die Secret-Env-Variablen hin."""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_dict({"imap": {"host": "h", "username": "u"}}, env={})

    message = str(excinfo.value)
    assert ENV_IMAP_PASSWORD in message
    assert ENV_LLM_API_KEY in message
    assert ENV_TELEGRAM_TOKEN in message


# --- Env-Overrides -------------------------------------------------------------------------


def test_env_provides_secrets_missing_in_file(tmp_path: Path) -> None:
    """Secrets dürfen ausschließlich aus der Umgebung kommen (empfohlener Betrieb)."""
    env = {
        ENV_IMAP_PASSWORD: "geheim-imap",
        ENV_LLM_API_KEY: "geheim-llm",
        ENV_TELEGRAM_TOKEN: "geheim-telegram",
    }

    config = load_config(_write(tmp_path, MINIMAL_TOML), env=env)

    assert config.imap.password is not None
    assert config.imap.password.get_secret_value() == "geheim-imap"
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "geheim-llm"
    assert config.messenger.telegram.token is not None
    assert config.messenger.telegram.token.get_secret_value() == "geheim-telegram"


def test_env_overrides_file_values(tmp_path: Path) -> None:
    """Ist ein Secret in Datei und Umgebung gesetzt, gewinnt die Umgebung."""
    env = {ENV_IMAP_PASSWORD: "aus-der-umgebung", ENV_LLM_API_KEY: "env-key"}

    config = load_config(_write(tmp_path, FULL_TOML), env=env)

    assert config.imap.password is not None
    assert config.imap.password.get_secret_value() == "aus-der-umgebung"
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "env-key"


def test_empty_env_value_does_not_override(tmp_path: Path) -> None:
    """Ein leerer Export überschreibt einen gesetzten Wert aus der Datei nicht."""
    config = load_config(_write(tmp_path, FULL_TOML), env={ENV_IMAP_PASSWORD: ""})

    assert config.imap.password is not None
    assert config.imap.password.get_secret_value() == "aus-der-datei"


def test_env_override_creates_missing_sections(tmp_path: Path) -> None:
    """Fehlt `[messenger.telegram]` ganz, legt der Env-Override die Struktur an."""
    config = load_config(
        _write(tmp_path, MINIMAL_TOML), env={ENV_TELEGRAM_TOKEN: "nur-aus-env"}
    )

    assert config.messenger.telegram.token is not None
    assert config.messenger.telegram.token.get_secret_value() == "nur-aus-env"


def test_input_dict_is_not_mutated() -> None:
    """`load_config_from_dict` verändert das übergebene Dict nicht (auch nicht verschachtelt)."""
    data = {"imap": {"host": "h", "username": "u"}, "llm": {"model": "m"}}

    load_config_from_dict(data, env={ENV_IMAP_PASSWORD: "x"})

    assert data == {"imap": {"host": "h", "username": "u"}, "llm": {"model": "m"}}


def test_default_env_is_os_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ohne explizites `env`-Argument wird `os.environ` verwendet."""
    monkeypatch.setenv(ENV_LLM_API_KEY, "aus-os-environ")

    config = load_config(_write(tmp_path, MINIMAL_TOML))

    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "aus-os-environ"


# --- WP8: Betriebsfelder in [general] ------------------------------------------------------


def test_state_db_defaults_next_to_the_config_file(tmp_path: Path) -> None:
    """Leeres `[general] state_db` ⇒ `state.db` neben der Konfigurationsdatei (ADR-045)."""
    from maildigest.config import resolve_state_db_path

    path = _write(tmp_path, MINIMAL_TOML)
    config = load_config(path, env={})

    assert resolve_state_db_path(config, path) == tmp_path / "state.db"


def test_state_db_relative_path_is_relative_to_the_config_file(tmp_path: Path) -> None:
    """Ein relativer Pfad gilt relativ zum Verzeichnis der Config-Datei."""
    from maildigest.config import resolve_state_db_path

    path = _write(tmp_path, MINIMAL_TOML + '\n[general]\nstate_db = "daten/mail.db"\n')
    config = load_config(path, env={})

    assert resolve_state_db_path(config, path) == tmp_path / "daten" / "mail.db"


def test_state_db_absolute_path_wins() -> None:
    """Ein absoluter Pfad wird unverändert übernommen."""
    from maildigest.config import resolve_state_db_path

    config = load_config_from_dict(
        {
            "general": {"state_db": "/var/lib/maildigest/state.db"},
            "imap": {"host": "h", "username": "u"},
            "llm": {"model": "m"},
        },
        env={},
    )

    assert resolve_state_db_path(config, "/etc/maildigest/config.toml") == Path(
        "/var/lib/maildigest/state.db"
    )


def test_state_db_without_config_path_falls_back_to_cwd() -> None:
    """Ohne bekannten Config-Pfad (Tests, Einbettung) bleibt es beim Arbeitsverzeichnis."""
    from maildigest.config import resolve_state_db_path

    config = load_config_from_dict(
        {"imap": {"host": "h", "username": "u"}, "llm": {"model": "m"}}, env={}
    )

    assert resolve_state_db_path(config) == Path("state.db")


def test_log_level_is_validated() -> None:
    """`log_level` akzeptiert nur die vier bekannten Stufen (ADR-046)."""
    config = load_config_from_dict(
        {
            "general": {"log_level": "DEBUG"},
            "imap": {"host": "h", "username": "u"},
            "llm": {"model": "m"},
        },
        env={},
    )
    assert config.general.log_level == "DEBUG"

    with pytest.raises(ConfigError, match="log_level"):
        load_config_from_dict(
            {
                "general": {"log_level": "verbose"},
                "imap": {"host": "h", "username": "u"},
                "llm": {"model": "m"},
            },
            env={},
        )
