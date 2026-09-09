"""Laden und Validieren der `config.toml` (pydantic), inkl. Secret-Override via
Umgebungsvariablen (`MAILDIGEST_*`).

Schema: docs/ARCHITECTURE.md §5, Limit-Defaults: docs/SECURITY.md §4 (umgesetzt in WP1).

Ablauf von :func:`load_config`:
1. TOML-Datei mit `tomllib` (stdlib) lesen,
2. Secrets aus den Umgebungsvariablen in das Roh-Dict einspiegeln (Env schlägt Datei, I5),
3. gegen das pydantic-Schema validieren,
4. Fehler in eine :class:`ConfigError` mit deutscher, feldbezogener Meldung übersetzen.

Secrets werden nie geloggt und nie in `__repr__`/`__str__` ausgegeben (I5): Die betroffenen
Felder sind `pydantic.SecretStr`.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from maildigest.models import Importance

__all__ = [
    "ENV_IMAP_PASSWORD",
    "ENV_LLM_API_KEY",
    "ENV_TELEGRAM_TOKEN",
    "Config",
    "ConfigError",
    "CriticLLMConfig",
    "DiscordConfig",
    "GeneralConfig",
    "ImapConfig",
    "LimitsConfig",
    "LinksConfig",
    "LlmConfig",
    "MessengerConfig",
    "SignalConfig",
    "SummarizerConfig",
    "TelegramConfig",
    "load_config",
    "load_config_from_dict",
    "resolve_state_db_path",
    "validate_section",
]

#: Dateiname der State-Datenbank, wenn `[general] state_db` leer ist (ADR-005/ADR-045).
DEFAULT_STATE_DB_NAME = "state.db"

#: Umgebungsvariablen, die Secrets aus der Config-Datei überschreiben (docs/SECURITY.md §6).
ENV_IMAP_PASSWORD = "MAILDIGEST_IMAP_PASSWORD"
ENV_LLM_API_KEY = "MAILDIGEST_LLM_API_KEY"
ENV_TELEGRAM_TOKEN = "MAILDIGEST_TELEGRAM_TOKEN"


class ConfigError(Exception):
    """Konfiguration fehlt, ist kein gültiges TOML oder verletzt das Schema.

    Die Meldung ist bewusst für Endnutzer formuliert (deutsch, feldbezogen) und enthält
    niemals Secret-Werte.
    """


class _Section(BaseModel):
    """Basis aller Config-Sektionen: unbekannte Schlüssel sind ein Fehler (Tippfehler-Schutz)."""

    model_config = ConfigDict(extra="forbid")


class GeneralConfig(_Section):
    """`[general]` — Sprache, Länge, Wichtigkeits-Schwelle, Sammel-Digest, Betrieb.

    `state_db` und `log_level` sind Betriebsfelder aus WP8 (ADR-045/ADR-046):
    `state_db = ""` bedeutet „`state.db` neben der Konfigurationsdatei" (aufgelöst von
    :func:`resolve_state_db_path`), `log_level` steuert den Schwellwert des strukturierten
    Loggings auf stdout.
    """

    language: str = "de"
    summary_length: Literal["short", "medium", "long"] = "medium"
    deliver_min_importance: Importance = "normal"
    low_digest_time: str = Field(default="18:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    state_db: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class ImapConfig(_Section):
    """`[imap]` — Zugang zum Mirror-Postfach (nur IMAPS, F-ING-1)."""

    host: str = Field(min_length=1)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(min_length=1)
    password: SecretStr | None = None
    folder: str = "INBOX"
    poll_interval_seconds: int = Field(default=120, ge=5)
    move_processed_to: str = ""


class CriticLLMConfig(_Section):
    """`[llm.critic]` — optionaler Override für den Kritiker; leere Felder erben von `[llm]`."""

    provider: Literal["anthropic", "openai_compatible"] | None = None
    model: str | None = None
    base_url: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)


class LlmConfig(_Section):
    """`[llm]` — Provider-Auswahl und Modellparameter (F-LLM-1).

    `provider = "none"` ist der Standard: MailDigest läuft dann ohne jedes Sprachmodell und
    stellt statt einer Zusammenfassung einen beschrifteten Auszug samt aller
    deterministischen Warnsignale zu (siehe :mod:`maildigest.agents.offline`). Das ist die
    einzige Betriebsart, die ohne Anmeldung bei irgendjemandem funktioniert — ein
    mitgelieferter Schlüssel scheidet bei quelloffener Software aus (ADR-076).

    `model` hat bewusst keinen Default: Modell-IDs veralten, ein hartkodierter Default würde
    stillschweigend ein falsches Modell verwenden (docs/ARCHITECTURE.md §5). Bei
    `provider = "none"` bleibt das Feld leer.
    """

    provider: Literal["none", "anthropic", "openai_compatible"] = "none"
    model: str = ""
    api_key: SecretStr | None = None
    base_url: str = ""
    max_tokens: int = Field(default=1024, ge=1)
    critic: CriticLLMConfig = Field(default_factory=CriticLLMConfig)

    @model_validator(mode="after")
    def _model_required_unless_offline(self) -> LlmConfig:
        """Erzwingt `model`, sobald ein echter Provider gewählt ist.

        Ohne diese Prüfung wäre ein leeres `model` bei `provider = "anthropic"` erst beim
        ersten Mail-Eingang aufgefallen — also im Dauerbetrieb statt bei der Einrichtung.

        Raises:
            ValueError: Provider gesetzt, aber kein Modellname.
        """
        if self.provider != "none" and not self.model.strip():
            raise ValueError(
                f'model is required for provider "{self.provider}" — or set '
                'provider = "none" to run without a language model at all'
            )
        return self


class SummarizerConfig(_Section):
    """`[summarizer]` — Custom-Instructions des Nutzers (semi-trusted, I8)."""

    instructions: str = ""


class LinksConfig(_Section):
    """`[links]` — Steuerung der defangten Link-Fußnote (Default aus, I3)."""

    footnote: bool = False


class TelegramConfig(_Section):
    """`[messenger.telegram]` — Bot-Token (bevorzugt via Env) und Chat-ID."""

    #: Ob MailDigest Befehle aus dem Chat annimmt (`/digest`, `/status`). Ab Werk **aus**:
    #: Die Zustellung ist sonst eine Einbahnstraße, und das bleibt die sichere Vorgabe
    #: (ADR-077). Eingeschaltet gilt weiterhin: nur die feste Befehlsliste, nur aus
    #: `chat_id`, und kein fremder Text erreicht je ein Sprachmodell.
    accept_commands: bool = False
    token: SecretStr | None = None
    chat_id: str = ""


class DiscordConfig(_Section):
    """`[messenger.discord]` — Webhook-URL; enthält ein Secret, daher `SecretStr`."""

    webhook_url: SecretStr | None = None


class SignalConfig(_Section):
    """`[messenger.signal]` — optionaler signal-cli-Adapter hinter Feature-Flag."""

    enabled: bool = False
    signal_cli_socket: str = ""


class MessengerConfig(_Section):
    """`[messenger]` — aktiver Adapter plus dessen Unter-Sektionen (F-MSG-1)."""

    active: Literal["telegram", "discord", "signal"] = "telegram"
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)


class LimitsConfig(_Section):
    """`[limits]` — Ressourcen- und Größenlimits; Defaults aus docs/SECURITY.md §4."""

    max_mail_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_text_chars: int = Field(default=30_000, ge=1)
    pdf_max_input_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    pdf_max_output_chars: int = Field(default=50_000, ge=1)
    pdf_timeout_seconds: int = Field(default=20, ge=1)
    max_mime_depth: int = Field(default=10, ge=1)
    max_attachments_processed: int = Field(default=20, ge=0)


class Config(_Section):
    """Gesamt-Konfiguration von MailDigest (docs/ARCHITECTURE.md §5)."""

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    imap: ImapConfig
    llm: LlmConfig
    summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)
    links: LinksConfig = Field(default_factory=LinksConfig)
    messenger: MessengerConfig = Field(default_factory=MessengerConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    def critic_model(self) -> str:
        """Modell des Kritikers: Override aus `[llm.critic]`, sonst das Modell aus `[llm]`."""
        return self.llm.critic.model or self.llm.model

    def critic_provider(self) -> str:
        """Provider des Kritikers: Override aus `[llm.critic]`, sonst der aus `[llm]`."""
        return self.llm.critic.provider or self.llm.provider

    def critic_base_url(self) -> str:
        """Base-URL des Kritikers: Override aus `[llm.critic]`, sonst die aus `[llm]`."""
        base_url = self.llm.critic.base_url
        return base_url if base_url is not None else self.llm.base_url

    def critic_max_tokens(self) -> int:
        """Token-Limit des Kritikers: Override aus `[llm.critic]`, sonst das aus `[llm]`."""
        return self.llm.critic.max_tokens or self.llm.max_tokens


# --- Env-Overrides -----------------------------------------------------------------------

#: (Env-Variable, Pfad in der Config-Struktur) — Env schlägt immer die Datei.
_ENV_OVERRIDES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ENV_IMAP_PASSWORD, ("imap", "password")),
    (ENV_LLM_API_KEY, ("llm", "api_key")),
    (ENV_TELEGRAM_TOKEN, ("messenger", "telegram", "token")),
)


def _apply_env_overrides(
    data: dict[str, Any], env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Spiegelt gesetzte `MAILDIGEST_*`-Secrets in das Roh-Dict (vor der Validierung).

    Vor der Validierung, damit Fehlermeldungen die echten Feldnamen nennen und ein Secret,
    das nur in der Umgebung steht, kein „Pflichtfeld fehlt" auslöst. Leere Env-Werte werden
    ignoriert (ein leerer Export soll nichts kaputt machen).

    Fehlende Zwischen-Sektionen werden angelegt. Ist eine Zwischen-Sektion in der Datei
    vorhanden, aber keine TOML-Tabelle (z. B. ``imap = "x"``), wird der Override
    übersprungen: Der Typfehler soll unverfälscht in der Validierung gemeldet werden statt
    von einem stillen Überschreiben verdeckt zu werden.
    """
    environment = os.environ if env is None else env
    for env_name, path in _ENV_OVERRIDES:
        value = environment.get(env_name)
        if not value:
            continue
        section = _ensure_section(data, path[:-1])
        if section is None:
            continue
        section[path[-1]] = value
    return data


def _ensure_section(
    data: dict[str, Any], path: tuple[str, ...]
) -> dict[str, Any] | None:
    """Navigiert zu `path` und legt fehlende Zwischen-Tabellen an.

    Gibt `None` zurück, wenn ein Zwischenknoten existiert, aber keine TOML-Tabelle ist —
    dann bleibt der Wert unangetastet, damit die Validierung den Typfehler meldet.
    """
    cursor = data
    for key in path:
        child = cursor.get(key)
        if child is None:
            child = {}
            cursor[key] = child
        elif not isinstance(child, dict):
            return None
        cursor = child
    return cursor


# --- Fehlerübersetzung -------------------------------------------------------------------


def _format_location(location: tuple[int | str, ...]) -> str:
    """Baut aus einem pydantic-`loc` eine TOML-nahe Pfadangabe wie `[imap] host`."""
    parts = [str(item) for item in location]
    if not parts:
        return "(root)"
    if len(parts) == 1:
        return parts[0]
    return f"[{'.'.join(parts[:-1])}] {parts[-1]}"


def _translate_error(error: dict[str, Any]) -> str:
    """Übersetzt genau einen pydantic-Fehler in eine deutsche, nutzbare Zeile."""
    error_type = str(error.get("type", ""))
    context = error.get("ctx") or {}
    if error_type == "missing":
        return "required value missing."
    if error_type == "extra_forbidden":
        return "unknown field — typo? (remove the field or check the spelling)"
    if error_type == "literal_error":
        return f"invalid value; allowed is {context.get('expected', 'a different value')}."
    if error_type == "string_pattern_mismatch":
        return f"invalid format (expected pattern: {context.get('pattern', '')})."
    if error_type in {"string_too_short", "string_type"}:
        return "value must be a non-empty string."
    if error_type in {"int_parsing", "int_type"}:
        return "value must be a whole number."
    if error_type in {"bool_parsing", "bool_type"}:
        return "value must be true or false."
    if error_type in {"greater_than_equal", "greater_than"}:
        return f"value is too small (minimum: {context.get('ge', context.get('gt', '?'))})."
    if error_type in {"less_than_equal", "less_than"}:
        return f"value is too large (maximum: {context.get('le', context.get('lt', '?'))})."
    if error_type in {"dict_type", "model_type"}:
        return "a TOML section (table) is expected here."
    if error_type == "list_type":
        return "a list is expected here."
    message = str(error.get("msg", "invalid value."))
    # pydantic stellt eigenen Validatoren „Value error, " voran — das ist Innenleben und
    # verwirrt in einer Meldung, die auf eine Konfigurationsdatei zeigt.
    return message.removeprefix("Value error, ")


def _validation_error_message(exc: ValidationError, source: str) -> str:
    """Baut die vollständige, mehrzeilige deutsche Fehlermeldung für alle Einzelfehler."""
    lines = [f"Invalid configuration ({source}):"]
    lines += [
        f"  - {_format_location(tuple(error['loc']))}: {_translate_error(dict(error))}"
        for error in exc.errors()
    ]
    lines.append(
        # Die Feldreferenz steht in SPEC-CLI.md §5 — dorthin verweist auch der Kopf der
        # von `init` erzeugten config.toml. ARCHITECTURE §5 war schlicht falsch (CT-2).
        "Reference for all fields: docs/SPEC-CLI.md §5. "
        "Secrets can alternatively be set through the environment variables "
        f"{ENV_IMAP_PASSWORD}, {ENV_LLM_API_KEY}, {ENV_TELEGRAM_TOKEN}."
    )
    return "\n".join(lines)


# --- Öffentliche API ---------------------------------------------------------------------


def load_config(path: str | Path, *, env: dict[str, str] | None = None) -> Config:
    """Lädt und validiert die Konfigurationsdatei.

    Args:
        path: Pfad zur `config.toml`.
        env: Umgebung für die Secret-Overrides; Default ist `os.environ` (Tests reichen
            hier ein eigenes Dict herein).

    Returns:
        Die validierte :class:`Config`.

    Raises:
        ConfigError: Datei fehlt, ist kein lesbares/gültiges TOML oder verletzt das Schema.
            Die Meldung ist deutsch, feldbezogen und secret-frei.
    """
    config_path = Path(path)
    try:
        raw_bytes = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Konfigurationsdatei nicht gefunden: {config_path}. "
            "Mit `maildigest init` anlegen oder Pfad korrigieren."
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Configuration file {config_path} cannot be read: {exc.strerror}."
        ) from exc

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"Configuration file {config_path} is not UTF-8 encoded."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Configuration file {config_path} is not valid TOML: {exc}"
        ) from exc

    return load_config_from_dict(data, env=env, source=str(config_path))


def load_config_from_dict(
    data: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    source: str = "Konfiguration",
) -> Config:
    """Validiert ein bereits geparstes Config-Dict (inkl. Env-Overrides).

    Getrennt von :func:`load_config`, damit CLI (WP9) und Tests ohne Datei validieren können.
    """
    merged = _apply_env_overrides(copy.deepcopy(dict(data)), env=env)
    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_validation_error_message(exc, source)) from exc


#: Typvariable für :func:`validate_section` — jede Config-Sektion erbt von `_Section`.
_SectionT = TypeVar("_SectionT", bound=_Section)


def validate_section(model: type[_SectionT], data: Any, *, source: str) -> _SectionT:
    """Validiert **eine** Config-Sektion für sich (WP9, ADR-052).

    Die Einrichtungs-Kommandos der CLI (`connect-mail`, `connect-llm`,
    `connect-messenger`) arbeiten auf einer noch unvollständigen Konfiguration: Solange
    `[llm] model` fehlt, würde eine Gesamtvalidierung jeden `connect-mail`-Lauf mit einem
    themenfremden Fehler abbrechen. Diese Funktion prüft deshalb nur die gerade
    bearbeitete Sektion — mit derselben deutschen, feldbezogenen Fehlermeldung wie
    :func:`load_config`.

    Args:
        model: Sektions-Modell, z. B. :class:`ImapConfig`.
        data: Roh-Dict der Sektion (aus `tomllib`).
        source: Quellenangabe für die Fehlermeldung, z. B. ``"[imap] in config.toml"``.

    Returns:
        Die validierte Sektion.

    Raises:
        ConfigError: Die Sektion verletzt das Schema. Die Meldung ist deutsch und
            secret-frei (I5).
    """
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_validation_error_message(exc, source)) from exc


def resolve_state_db_path(config: Config, config_path: str | Path | None = None) -> Path:
    """Bestimmt den Pfad der State-Datenbank (ADR-045).

    Reihenfolge: `[general] state_db` (mit `~`-Auflösung; relative Pfade gelten relativ zum
    Verzeichnis der Konfigurationsdatei) → sonst ``state.db`` neben der Konfigurationsdatei
    → sonst ``state.db`` im aktuellen Verzeichnis (kein Config-Pfad bekannt, z. B. in Tests).

    Args:
        config: Validierte Gesamt-Config.
        config_path: Pfad der Konfigurationsdatei, falls bekannt.

    Returns:
        Absoluter oder relativer Dateipfad der SQLite-Datei (wird nicht angelegt).
    """
    base = Path(config_path).expanduser().parent if config_path is not None else Path()
    configured = config.general.state_db.strip()
    if not configured:
        return base / DEFAULT_STATE_DB_NAME
    path = Path(configured).expanduser()
    return path if path.is_absolute() else base / path
