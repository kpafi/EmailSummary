"""Kommandozeilen-Einstieg: init, connect-mail, connect-llm, connect-messenger, test, run.

Vollständiger Vertrag (Prompts, Exit-Codes, Fehlermeldungen, Config-Referenz):
docs/SPEC-CLI.md. Dieses Modul ist die Umsetzung davon (WP9) und enthält **keine**
Verarbeitungslogik: Es liest Eingaben, schreibt die Konfigurationsdatei und ruft die in
WP2-WP8 gebauten Bausteine auf.

Sicherheits-Design dieses Moduls:

* **I5:** Die Konfigurationsdatei wird ausschließlich mit `O_CREAT|0o600` angelegt und bei
  jedem Schreiben erneut auf `0600` gesetzt. Secrets werden nie ausgegeben, nie geloggt
  und nie in Fehlermeldungen übernommen; Eingaben laufen über :func:`getpass.getpass`,
  sobald ein Terminal vorhanden ist.
* **I3/I4:** Die Testnachricht von `connect-messenger` entsteht über
  :meth:`~maildigest.output.composer.DigestComposer.compose_plain`, also über denselben
  `_finalize()`-Pfad wie jede andere Nachricht. Vom Netz gelesene Fremddaten
  (Telegram-`getUpdates`, IMAP-Ordnernamen, LLM-Antworten) werden nie roh ausgegeben —
  gezeigt werden nur Zahlen, Aufzählungswerte und auf eine Zeichen-Allowlist reduzierte
  Ordnernamen (ADR-055).
* **I6:** `maildigest test` benutzt den echten Runner; ein Fehler in der Pipeline endet
  wie im Betrieb als Metadaten-Notiz — die CLI meldet ihn zusätzlich als Exit-Code 1.

Testbarkeit: :func:`main` bekommt `argv`, die drei Ströme und ein :class:`Hooks`-Objekt
injiziert; kein Kommando ruft `sys.exit` selbst. Jede interaktive Abfrage hat entweder
eine Kommandozeilen-Option oder einen Default, sodass `--non-interactive` alle Pfade
erreicht.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import json
import os
import re
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, TextIO, TypeVar

import httpx
from imap_tools import MailMessage
from pydantic import BaseModel, SecretStr

from maildigest import providers
from maildigest.agents.critic import CriticAgent
from maildigest.agents.offline import OfflineCritic, OfflineSummarizer
from maildigest.agents.summarizer import SummarizerAgent
from maildigest.config import (
    ENV_IMAP_PASSWORD,
    ENV_LLM_API_KEY,
    ENV_TELEGRAM_TOKEN,
    Config,
    ConfigError,
    GeneralConfig,
    ImapConfig,
    LlmConfig,
    MessengerConfig,
    TelegramConfig,
    load_config,
    validate_section,
)
from maildigest.foreign_text import mask_secrets, sanitize_foreign_text
from maildigest.ingest.imap_client import (
    ImapAuthError,
    ImapClient,
    IngestError,
    build_raw_mail,
)
from maildigest.llm.base import LLMError
from maildigest.llm.factory import build_provider_from_settings
from maildigest.logging_setup import configure_logging
from maildigest.messenger.base import Messenger, MessengerError
from maildigest.messenger.factory import build_messenger_from_section
from maildigest.messenger.telegram import ChatCandidate, discover_chat_ids
from maildigest.models import CriticVerdict, DigestMessage, RawMail, SanitizedMail, Summary
from maildigest.output.composer import DigestComposer, part_limit_for
from maildigest.pipeline import Critic, Delivered, FailedNotice, QueuedLow, Sanitizer, Summarizer
from maildigest.runner import Runner, build_runner
from maildigest.sanitize.sanitizer import MailSanitizer
from maildigest.state.db import StateDB, StateError

__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_USAGE", "Hooks", "build_parser", "main", "run_cli"]

# --- Konstanten ---------------------------------------------------------------------------

#: Exit-Code: alles in Ordnung.
EXIT_OK = 0

#: Exit-Code: Laufzeit-/Konfigurationsfehler (Verbindung, Zustellung, ungültige Config).
EXIT_ERROR = 1

#: Exit-Code: Bedienfehler (unbekanntes Kommando/Option, fehlende Pflichtangabe).
EXIT_USAGE = 2

#: Umgebungsvariable, die den Pfad der Konfigurationsdatei vorgibt.
ENV_CONFIG = "MAILDIGEST_CONFIG"

#: Dateiname der Konfiguration, wenn weder `--config` noch `MAILDIGEST_CONFIG` gesetzt ist.
DEFAULT_CONFIG_NAME = "config.toml"

#: Wahlmöglichkeiten der interaktiven Abfragen.
_LANGUAGES = ("de", "en")
_SUMMARY_LENGTHS = ("short", "medium", "long")
_IMPORTANCES = ("low", "normal", "high")
_PROVIDERS = ("none", "anthropic", "openai_compatible")
_MESSENGERS = ("telegram", "discord", "signal")

#: Obergrenze der Custom-Instructions bei der interaktiven Eingabe (der Prompt-Builder
#: deckelt sie ohnehin bei 2000 Zeichen, ADR-031).
_MAX_INSTRUCTIONS_CHARS = 2000

#: Versuche und Wartezeit des `getUpdates`-Flows von `connect-messenger`.
_CHAT_ID_ATTEMPTS = 10
_CHAT_ID_WAIT_SECONDS = 3.0

#: Klartext-IMAP-Port; eine Konfiguration darauf ist immer ein Fehler (SECURITY §6).
_PLAINTEXT_IMAP_PORT = 143

#: Zeichen, die ein vom Server gelieferter Ordnername auf dem Terminal haben darf.
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-zÄÖÜäöüß _./-]")

#: Deckel für eine Fehlerzeile auf `stderr`. Großzügig: Die längsten eigenen Meldungen
#: (Anbieter-Hinweise beim Einrichten) sind mehrzeilig und sollen vollständig erscheinen.
_ERROR_TEXT_MAX_CHARS = 4000

#: Text der Testnachricht (`connect-messenger`). Bewusst ohne Punkte, Domains und Markup —
#: der Nachbrenner des Output-Sanitizers würde sie sonst sichtbar entschärfen.
_TEST_MESSAGE = (
    "✅ MailDigest test message\n"
    "Delivery works — your mail summaries will arrive here from now on"
)

#: Nur für Telegram angehängt: `accept_commands` wirkt ausschließlich dort (HC-16). Der
#: Text nennt bewusst keinen punktierten Sektionsnamen — der Output-Sanitizer entschärft
#: Punkte, und eine falsch abgetippte TOML-Zeile ist schlimmer als gar keine.
_TELEGRAM_COMMAND_HINT = (
    "\n"
    "\n"
    "This chat can also trigger MailDigest:\n"
    "/digest — fetch and summarise right now\n"
    "/status — short report on what is waiting"
)

#: Terminal-Hinweis nach jeder erfolgreichen Telegram-Einrichtung (HC-19). Er geht auf
#: stdout, nicht in eine Nachricht — hier darf der punktierte Sektionsname stehen.
_TELEGRAM_SETUP_HINT = (
    "While `maildigest run` is running, this chat can also trigger it:\n"
    "  /digest   fetch and summarise right now\n"
    "  /status   short report on what is waiting\n"
    "With `maildigest run --once` (cron) both are answered at the end of the next run;\n"
    "an immediate fetch has no effect there — that run just fetched everything anyway.\n"
    "Anything else you write is discarded — there is no chat function.\n"
    "If the chat is a group where not everyone should be able to trigger runs, set\n"
    "accept_commands = false under [messenger.telegram] in your configuration."
)

#: Vorgabe für `[messenger.telegram] accept_commands` (ADR-078) — aus dem Schema, damit
#: Datei und Validierung nicht auseinanderlaufen.
_TELEGRAM_ACCEPT_COMMANDS_DEFAULT: bool = bool(
    TelegramConfig.model_fields["accept_commands"].default
)


class CliError(Exception):
    """Abbruch mit definiertem Exit-Code und einer Meldung für `stderr`.

    Die Meldung ist englisch (ADR-083), nennt nach Möglichkeit den nächsten Schritt und enthält
    niemals ein Secret (I5).
    """

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# --- Konsole (testbare Ein-/Ausgabe) -------------------------------------------------------


class Console:
    """Ein-/Ausgabe der CLI — vollständig injizierbar, damit Tests stdin füttern können."""

    def __init__(
        self,
        stdin: TextIO,
        stdout: TextIO,
        stderr: TextIO,
        *,
        interactive: bool = True,
    ) -> None:
        """Args: interactive: False = `--non-interactive` (nur Defaults und Optionen)."""
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.interactive = interactive

    # --- Ausgabe -------------------------------------------------------------------

    def out(self, text: str = "") -> None:
        """Schreibt eine Zeile auf stdout."""
        self.stdout.write(f"{text}\n")

    def err(self, text: str) -> None:
        """Schreibt eine Zeile auf stderr (Warnungen und Fehler)."""
        self.stderr.write(f"{text}\n")

    def step(self, text: str) -> None:
        """Fortschrittsmeldung eines mehrstufigen Kommandos."""
        self.out(f"  {text}")

    # --- Eingabe -------------------------------------------------------------------

    def _readline(self, prompt: str) -> str:
        """Liest genau eine Zeile; EOF ist ein Abbruch, kein leerer Wert.

        EOF bedeutet: Es wurde eine Eingabe erwartet und keine geliefert — also ein
        Bedienfehler (Exit-Code 2, SPEC-CLI.md §2 „fehlende Pflichtangabe"), nicht ein
        Laufzeitfehler. Wer nicht interaktiv arbeiten kann, nimmt `--non-interactive`.
        """
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise CliError(
                "Input aborted (end of input reached). For runs without a terminal, use "
                "--non-interactive and pass the values as options.",
                EXIT_USAGE,
            )
        return line.strip()

    def ask(
        self,
        prompt: str,
        *,
        default: str = "",
        allowed: Sequence[str] | None = None,
        flag: str = "",
        required: bool = False,
    ) -> str:
        """Fragt einen Textwert ab.

        Args:
            prompt: Fragetext ohne Doppelpunkt.
            default: Vorbelegung (wird angezeigt und bei leerer Eingabe übernommen).
            allowed: Erlaubte Werte; eine andere Eingabe wird abgelehnt und neu gefragt.
            flag: Zugehörige Kommandozeilen-Option — erscheint in der Fehlermeldung des
                nicht-interaktiven Modus.
            required: Ein leerer Endwert ist ein Fehler.

        Raises:
            CliError: Pflichtangabe fehlt (Exit-Code 2) oder Eingabe abgebrochen.
        """
        if not self.interactive:
            value = default
            if required and not value:
                raise CliError(
                    f"{prompt}: required value missing. In non-interactive mode, set it via "
                    f"{flag or 'the matching option'}.",
                    EXIT_USAGE,
                )
            return value

        suffix = f" [{default}]" if default else ""
        options = f" ({'/'.join(allowed)})" if allowed else ""
        for _attempt in range(3):
            value = self._readline(f"{prompt}{options}{suffix}: ") or default
            if allowed and value not in allowed:
                self.err(f"Invalid value. Allowed: {', '.join(allowed)}.")
                continue
            if required and not value:
                self.err("Required — please enter a value.")
                continue
            return value
        raise CliError("Too many invalid entries — aborted.", EXIT_USAGE)

    def ask_int(
        self, prompt: str, *, default: int, minimum: int = 1, maximum: int = 65535
    ) -> int:
        """Fragt eine ganze Zahl im angegebenen Bereich ab."""
        if not self.interactive:
            return default
        for _attempt in range(3):
            raw = self._readline(f"{prompt} [{default}]: ")
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                self.err("Please enter a whole number.")
                continue
            if not minimum <= value <= maximum:
                self.err(f"Please enter a value between {minimum} and {maximum}.")
                continue
            return value
        raise CliError("Too many invalid entries — aborted.", EXIT_USAGE)

    def ask_secret(self, prompt: str) -> str:
        """Fragt ein Secret ab — ohne Echo, sobald ein Terminal vorhanden ist (I5).

        Im nicht-interaktiven Modus wird nichts gefragt; das Secret muss dann aus einer
        Umgebungsvariablen kommen.
        """
        if not self.interactive:
            return ""
        if _isatty(self.stdin):
            return getpass.getpass(f"{prompt}: ").strip()
        return self._readline(f"{prompt}: ")

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        """Ja/Nein-Frage; im nicht-interaktiven Modus gilt der Default."""
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        answer = self._readline(f"{prompt} [{hint}]: ").lower()
        if not answer:
            return default
        return answer in {"y", "yes"}

    def choose(self, prompt: str, options: Sequence[str], *, default_index: int = 0) -> int:
        """Lässt aus einer nummerierten Liste wählen und liefert den Index."""
        if not options:
            raise CliError("No options available.", EXIT_ERROR)
        for index, option in enumerate(options, start=1):
            self.out(f"  {index:>2}) {option}")
        if not self.interactive:
            return default_index
        for _attempt in range(3):
            raw = self._readline(f"{prompt} [{default_index + 1}]: ")
            if not raw:
                return default_index
            try:
                number = int(raw)
            except ValueError:
                self.err("Please enter the number of the line you want.")
                continue
            if 1 <= number <= len(options):
                return number - 1
            self.err(f"Please enter a number between 1 and {len(options)}.")
        raise CliError("Too many invalid entries — aborted.", EXIT_USAGE)


def _isatty(stream: TextIO) -> bool:
    """True, wenn der Strom ein Terminal ist (defensiv gegen Attrappen ohne `isatty`)."""
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - nur für exotische Ströme
        return False


def _safe_name(value: str, *, max_chars: int = 80) -> str:
    """Reduziert einen vom Server gelieferten Namen auf eine Zeichen-Allowlist (ADR-055)."""
    cleaned = _SAFE_NAME_RE.sub("·", value.replace("\n", " ").replace("\r", " ")).strip()
    return cleaned[:max_chars] if cleaned else "(unnamed)"


def _terminal_text(text: str) -> str:
    """Letzter Filter vor `stderr`: Allowlist über die fertige Fehlermeldung (HC-4).

    Die Meldung ist zusammengesetzt — eigener Rahmen plus oft ein Textstück einer
    Gegenstelle (Anbieterfehler, Serverantwort). Statt jeden künftigen Pfad einzeln zu
    härten, läuft die ganze Zeile durch dieselbe Zeichen-Allowlist wie Ordnernamen
    (ADR-055). Eigene Zeilenumbrüche bleiben erhalten, weil mehrere Meldungen bewusst
    mehrzeilig sind.
    """
    return sanitize_foreign_text(text, max_chars=_ERROR_TEXT_MAX_CHARS, keep_newlines=True)


# --- Konfigurationsdatei -------------------------------------------------------------------

#: Reihenfolge der Sektionen in der erzeugten Datei.
_SECTION_ORDER = ("general", "imap", "llm", "summarizer", "links", "messenger", "limits")

#: Reihenfolge der Schlüssel je Sektion; unbekannte Schlüssel folgen in Dateireihenfolge.
_KEY_ORDER: dict[str, tuple[str, ...]] = {
    "general": (
        "language",
        "summary_length",
        "deliver_min_importance",
        "low_digest_time",
        "state_db",
        "log_level",
    ),
    "imap": (
        "host",
        "port",
        "username",
        "password",
        "folder",
        "poll_interval_seconds",
        "move_processed_to",
    ),
    "llm": ("provider", "model", "api_key", "base_url", "max_tokens"),
    "llm.critic": ("provider", "model", "base_url", "max_tokens"),
    "summarizer": ("instructions",),
    "links": ("footnote",),
    "messenger": ("active",),
    "messenger.telegram": ("token", "chat_id", "accept_commands"),
    "messenger.discord": ("webhook_url",),
    "messenger.signal": ("enabled", "signal_cli_socket"),
    "limits": (
        "max_mail_bytes",
        "max_text_chars",
        "pdf_max_input_bytes",
        "pdf_max_output_chars",
        "pdf_timeout_seconds",
        "max_mime_depth",
        "max_attachments_processed",
        "max_html_elements",
        "max_html_bytes",
    ),
}

#: Reihenfolge der Unter-Sektionen einer Sektion.
_TABLE_ORDER: dict[str, tuple[str, ...]] = {
    "llm": ("critic",),
    "messenger": ("telegram", "discord", "signal"),
}

#: Erklärender Kommentar über jeder Sektion.
_SECTION_COMMENTS: dict[str, str] = {
    "general": "language, length, delivery threshold and operation",
    "imap": "mirror mailbox (IMAPS only) — `maildigest connect-mail`",
    "llm": "language model provider — `maildigest connect-llm`",
    "llm.critic": "optional override for the critic; empty fields inherit from [llm]",
    "summarizer": "custom instructions: what matters, what to watch for",
    "links": "links are always removed; this only controls the defanged footnote",
    "messenger": "delivery — `maildigest connect-messenger`",
    "limits": "resource limits (defaults: see docs/SPEC-CLI.md)",
}

#: Kommentar hinter einzelnen Schlüsseln.
_KEY_COMMENTS: dict[str, str] = {
    "general.summary_length": "short | medium | long",
    "general.deliver_min_importance": "low | normal | high",
    "general.low_digest_time": "daily digest, local wall-clock time",
    "general.state_db": "empty = state.db next to this file",
    "general.log_level": "DEBUG | INFO | WARNING | ERROR",
    "imap.port": "993 = IMAPS; port 143 is rejected",
    "imap.move_processed_to": "empty = only mark as read",
    "llm.provider": "none (no model) | anthropic | openai_compatible",
    "llm.base_url": "only for openai_compatible / local servers",
    "messenger.active": "telegram | discord | signal",
    "messenger.telegram.accept_commands": "accept /digest and /status from your chat",
    "limits.max_html_elements": "HTML parts above this are not converted",
    "limits.max_html_bytes": "per mail, across all HTML parts (at most 4)",
}

#: Auskommentierte Platzhalter für Felder ohne Default (Pflichtfelder) und für Secrets.
_PLACEHOLDERS: dict[str, tuple[tuple[str, str], ...]] = {
    "imap": (
        ("host", '"imap.example.org"'),
        ("username", '"mirror@example.org"'),
        ("password", f'"..."   # or environment variable {ENV_IMAP_PASSWORD}'),
    ),
    "llm": (
        ("model", '"..."   # required unless provider = "none"; no default on purpose'),
        ("api_key", f'"..."   # or environment variable {ENV_LLM_API_KEY}'),
    ),
    # Der Kritiker erbt alles von `[llm]`; die Datei zeigt trotzdem den vollständigen
    # Feldsatz aus SPEC-CLI.md §5, damit ein Override nicht nachgeschlagen werden muss.
    "llm.critic": (
        ("provider", '"openai_compatible"   # empty/absent = inherits from [llm]'),
        ("model", '"..."   # empty/absent = inherits from [llm]'),
        ("base_url", '"http://localhost:11434/v1"   # empty/absent = inherits from [llm]'),
        ("max_tokens", "1024   # empty/absent = inherits from [llm]"),
    ),
    "messenger.telegram": (
        ("token", f'"..."   # or environment variable {ENV_TELEGRAM_TOKEN}'),
    ),
    "messenger.discord": (("webhook_url", '"https://discord.com/api/webhooks/…"'),),
}

#: Kopf der erzeugten Datei.
_FILE_HEADER = (
    "# MailDigest — configuration",
    "# Created by `maildigest init`. File mode: 0600 (may contain secrets).",
    "# Full field reference: docs/SPEC-CLI.md",
)


def _toml_value(value: Any) -> str:
    """Serialisiert einen Wert als TOML-Literal.

    Für Strings wird `json.dumps` benutzt: TOML-Basic-Strings verwenden dieselben
    Escapes (`\\"`, `\\\\`, `\\n`, `\\uXXXX`), sodass auch Steuerzeichen sicher landen.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise CliError(
        f"A value of type {type(value).__name__} cannot be written to the configuration. "
        "Please correct the entry by hand."
    )


def _ordered_keys(table: Mapping[str, Any], order: Sequence[str]) -> list[str]:
    """Bekannte Schlüssel in fester Reihenfolge, unbekannte danach in Dateireihenfolge."""
    known = [key for key in order if key in table]
    rest = [key for key in table if key not in known]
    return known + rest


def _render_table(path: str, table: Mapping[str, Any], lines: list[str]) -> None:
    """Rendert eine Sektion inkl. Unter-Sektionen (rekursiv)."""
    scalars = {key: value for key, value in table.items() if not isinstance(value, dict)}
    tables = {key: value for key, value in table.items() if isinstance(value, dict)}

    lines.append("")
    comment = _SECTION_COMMENTS.get(path)
    if comment:
        lines.append(f"# {comment}")
    lines.append(f"[{path}]")
    for key in _ordered_keys(scalars, _KEY_ORDER.get(path, ())):
        rendered = f"{key} = {_toml_value(scalars[key])}"
        key_comment = _KEY_COMMENTS.get(f"{path}.{key}")
        lines.append(f"{rendered}   # {key_comment}" if key_comment else rendered)
    for key, example in _PLACEHOLDERS.get(path, ()):
        if key not in scalars:
            lines.append(f"# {key} = {example}")
    for key in _ordered_keys(tables, _TABLE_ORDER.get(path, ())):
        _render_table(f"{path}.{key}", tables[key], lines)


def render_toml(data: Mapping[str, Any]) -> str:
    """Serialisiert die Konfiguration als kommentierte `config.toml`.

    Bewusst ein eigener, winziger Serialisierer statt einer neuen Abhängigkeit (`tomli-w`):
    Das Schema ist bekannt und flach, und die Datei soll Kommentare tragen (ADR-053).
    """
    lines: list[str] = list(_FILE_HEADER)
    root_scalars = {key: value for key, value in data.items() if not isinstance(value, dict)}
    for key, value in root_scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    tables = {key: value for key, value in data.items() if isinstance(value, dict)}
    for name in _ordered_keys(tables, _SECTION_ORDER):
        _render_table(name, tables[name], lines)
    return "\n".join(lines) + "\n"


@dataclass
class ConfigFile:
    """Die Konfigurationsdatei als veränderbares Roh-Dict plus sicheres Schreiben."""

    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> ConfigFile:
        """Lädt eine vorhandene Datei.

        Raises:
            CliError: Datei fehlt (mit Hinweis auf `maildigest init`) oder ist kein TOML.
        """
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise CliError(
                f"Configuration file not found: {path}\n"
                "Create it with `maildigest init`, or pass the path with --config.",
                EXIT_ERROR,
            ) from exc
        except OSError as exc:
            raise CliError(
                f"Configuration file {path} cannot be read: {exc.strerror}."
            ) from exc
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise CliError(
                f"Configuration file {path} is not valid UTF-8 TOML: {exc}"
            ) from exc
        return cls(path=path, data=data)

    def section(self, name: str) -> dict[str, Any]:
        """Liefert (und erzeugt bei Bedarf) eine Sektion — auch punktierte wie `llm.critic`.

        Raises:
            CliError: Ein Zwischenknoten existiert, ist aber keine TOML-Tabelle.
        """
        cursor = self.data
        for part in name.split("."):
            child = cursor.get(part)
            if child is None:
                child = {}
                cursor[part] = child
            elif not isinstance(child, dict):
                raise CliError(
                    f"In {self.path}, `{part}` is not a section. Please correct the file by "
                    "hand."
                )
            cursor = child
        return cursor

    def save(self) -> None:
        """Schreibt die Datei atomar mit Dateirechten `0600` (I5, F-SEC-8, ADR-081).

        Der gerenderte Text wird vorher selbst geparst — eine unlesbare Datei würde jedes
        weitere Kommando blockieren.

        Geschrieben wird in eine temporäre Datei **im Zielverzeichnis** (damit `os.replace`
        nicht über eine Dateisystemgrenze muss), erst danach wird umbenannt. Ein Abbruch
        mitten im Schreiben (volle Platte, Quota, `RLIMIT_FSIZE`, EIO, SIGKILL) lässt die
        bisherige Konfiguration damit unverändert stehen; ohne das Verfahren blieb eine
        halb geschriebene Datei zurück, in der Zugangsdaten fehlten (HC-20).
        """
        text = render_toml(self.data)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - Schutz gegen Regression
            raise CliError(f"Internal error while writing the configuration: {exc}") from exc
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            # `O_EXCL`: Eine bereits existierende Temp-Datei (Symlink eines anderen
            # Nutzers im selben Verzeichnis, Rest eines abgestürzten Laufs) wird nicht
            # beschrieben, sondern führt zum Fehler.
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            # `os.replace` übernimmt die Rechte der Temp-Datei (0600). Der `chmod` bleibt
            # als Absicherung — er kostet nichts und deckt exotische Dateisysteme ab.
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise CliError(
                f"Configuration file {self.path} cannot be written: "
                f"{exc.strerror}."
            ) from exc
        finally:
            if temporary is not None:
                # Der Schreibvorgang ist gescheitert: kein halber Rest im Verzeichnis.
                with contextlib.suppress(OSError):  # z. B. nie angelegt
                    temporary.unlink()


def _set_or_clear(section: dict[str, Any], key: str, value: str) -> None:
    """Setzt einen Wert oder entfernt den Schlüssel, wenn der Wert leer ist."""
    if value:
        section[key] = value
    else:
        section.pop(key, None)


# --- Injizierbare Bausteine -----------------------------------------------------------------


def _summarizer_for(config: Config) -> Any:
    """Summarizer passend zur Config — ohne Modell die Offline-Stufe (ADR-076)."""
    if config.llm.provider == "none":
        return OfflineSummarizer()
    return SummarizerAgent.from_config(config)


def _critic_for(config: Config) -> Any:
    """Kritiker passend zur Config — ohne Modell die Offline-Stufe (ADR-076)."""
    if config.llm.provider == "none":
        return OfflineCritic()
    return CriticAgent.from_config(config)


@dataclass
class Hooks:
    """Die Außenwelt der CLI, gebündelt und ersetzbar (Tests reichen Attrappen herein)."""

    build_runner: Callable[..., Runner] = build_runner
    build_messenger: Callable[..., Messenger] = build_messenger_from_section
    build_provider: Callable[..., Any] = build_provider_from_settings
    build_summarizer: Callable[[Config], Any] = _summarizer_for
    build_critic: Callable[[Config], Any] = _critic_for
    imap_client: Callable[[ImapConfig], ImapClient] = ImapClient
    discover_chat_ids: Callable[..., list[ChatCandidate]] = discover_chat_ids
    configure_logging: Callable[..., Any] = configure_logging
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Context:
    """Alles, was jedes Kommando braucht."""

    console: Console
    hooks: Hooks
    config_path: Path
    args: argparse.Namespace = field(default_factory=argparse.Namespace)


# --- Kommando: init --------------------------------------------------------------------------


def cmd_init(ctx: Context) -> int:
    """Legt eine neue `config.toml` an (F-OPS-2-Vorstufe, NF-3)."""
    console, args = ctx.console, ctx.args
    path = ctx.config_path
    if path.exists() and not args.force:
        raise CliError(
            f"{path} already exists. Overwrite it with --force (the current content is "
            "lost) or choose another path with --config.",
            EXIT_ERROR,
        )

    console.out(f"Setting up MailDigest — configuration: {path}")
    language = args.language or console.ask(
        "Language of the summaries", default="de", allowed=_LANGUAGES, flag="--language"
    )
    summary_length = args.summary_length or console.ask(
        "Length of the summaries",
        default="medium",
        allowed=_SUMMARY_LENGTHS,
        flag="--summary-length",
    )
    min_importance = args.min_importance or console.ask(
        "Deliver individually from importance",
        default="normal",
        allowed=_IMPORTANCES,
        flag="--min-importance",
    )
    digest_time = args.low_digest_time or console.ask(
        "Time of the daily digest (HH:MM)",
        default="18:00",
        flag="--low-digest-time",
    )
    if args.instructions is not None:
        instructions = args.instructions
    else:
        if console.interactive:
            # Nur als Erläuterung der Frage — ohne Frage keine Erläuterung (CT-3).
            console.out(
                "Custom instructions: one line about what matters to you "
                "(leave empty for none)."
            )
        instructions = console.ask("Custom-Instructions", flag="--instructions")
    instructions = instructions[:_MAX_INSTRUCTIONS_CHARS]

    data: dict[str, Any] = {
        "general": {
            "language": language,
            "summary_length": summary_length,
            "deliver_min_importance": min_importance,
            "low_digest_time": digest_time,
            "state_db": "",
            "log_level": "INFO",
        },
        "imap": {
            "port": 993,
            "folder": "INBOX",
            "poll_interval_seconds": 120,
            "move_processed_to": "",
        },
        "llm": {"provider": "none", "model": "", "base_url": "", "max_tokens": 1024, "critic": {}},
        "summarizer": {"instructions": instructions},
        "links": {"footnote": False},
        "messenger": {
            "active": "telegram",
            "telegram": {
                "chat_id": "",
                "accept_commands": _TELEGRAM_ACCEPT_COMMANDS_DEFAULT,
            },
            "discord": {},
            "signal": {"enabled": False, "signal_cli_socket": ""},
        },
        "limits": {
            "max_mail_bytes": 26214400,
            "max_text_chars": 30000,
            "pdf_max_input_bytes": 10485760,
            "pdf_max_output_chars": 50000,
            "pdf_timeout_seconds": 20,
            "max_mime_depth": 10,
            "max_attachments_processed": 20,
            "max_html_elements": 50000,
            "max_html_bytes": 1048576,
        },
    }
    # Die Werte aus den Abfragen müssen das Schema erfüllen, bevor die Datei entsteht.
    _validate(GeneralConfig, data["general"], f"[general] in {path}")

    config_file = ConfigFile(path=path, data=data)
    config_file.save()
    console.out(f"Configuration created: {path} (file mode 0600)")
    console.out("")
    # Erst die Einordnung, dann die Schrittliste: Die Ausgabe endet mit dem, was der
    # Nutzer als Nächstes tippt (SPEC-CLI §4 `init`, HC-18).
    console.out(
        "No language model is configured, so MailDigest starts out delivering a labelled "
        "excerpt of each mail plus all the warnings it works out in code. That needs no "
        "account anywhere. `maildigest connect-llm` adds real summaries later — it also "
        "lists the providers that are free of charge."
    )
    console.out("")
    console.out("Next steps:")
    console.out("  1) maildigest connect-mail        (mirror mailbox)")
    console.out("  2) maildigest connect-messenger   (Telegram/Discord/Signal)")
    console.out("  3) maildigest connect-llm         (optional: real summaries)")
    console.out("  4) maildigest test                (self-test)")
    console.out("  5) maildigest run                 (continuous operation)")
    return EXIT_OK


# --- Kommando: connect-mail ------------------------------------------------------------------

_FORWARDING_GUIDE = """
How to set up forwarding in your real mailbox
---------------------------------------------
MailDigest NEVER reads your real mailbox. It only reads the mirror mailbox that you
forward your mail into.

Gmail:       Settings > "Forwarding and POP/IMAP" > "Add a forwarding address" >
             address of the mirror mailbox > enter the confirmation code from the mail
             that arrives there > enable "Forward a copy of incoming mail" and choose
             "keep Gmail's copy in the Inbox".
Posteo:      Settings > "Email" > "Filter rules" > new rule > action "Forward to" plus
             "keep a copy in the mailbox".
mailbox.org: Settings > "Email" > "Filter" > new rule > "Redirect to" plus the action
             "Keep".
otherwise:   You are looking for a server-side forward or filter rule. A rule in your
             mail program (Outlook/Thunderbird) is not enough — it only runs while your
             computer is on.

Important: do not forward from the mirror mailbox back to your main mailbox — that
creates a loop.
""".strip()


_HOST_EXPLANATION = """
The IMAP host is the server address where your provider makes mail available for
retrieval — it is not your mail address. For the large providers it is:

{examples}

You can also simply type the mail address of the mirror mailbox; the matching host is
derived from it.
""".strip()


@dataclass(frozen=True)
class _ResolvedHost:
    """Ergebnis von :func:`_resolve_host`: Host **und** erkannter Anbieter (HC-15).

    Der erkannte Anbieter darf nicht verlorengehen: Eine zweite Suche über die
    Eingabezeichenkette findet ihn bei einer Mailadresse nicht wieder, und die Sperre für
    Anbieter ohne Passwort-Anmeldung griffe dann nicht.
    """

    host: str
    provider: providers.Provider | None


def _resolve_host(console: Console, entered: str) -> _ResolvedHost:
    """Macht aus einer Mailadresse oder blanken Domain den richtigen IMAP-Host.

    Eine Mailadresse statt des Hosts ist an dieser Stelle die häufigste Fehleingabe. Ist
    der Anbieter bekannt, wird sie in den Host übersetzt und die Übersetzung angezeigt;
    sonst bleibt die Eingabe unverändert — geraten wird nicht. Ein nicht unterstützter
    Anbieter behält seine Eingabe, wird aber mitgeliefert, damit der Aufrufer abbrechen
    kann.
    """
    provider = providers.find_by_host(entered)
    if provider is None and "@" in entered:
        provider = providers.find_by_address(entered)
    if provider is None or not provider.supported:
        return _ResolvedHost(entered, provider)
    host = provider.imap_host
    if entered.strip().lower() != host.lower():
        console.out(f"  -> IMAP host for {provider.name}: {host}")
    return _ResolvedHost(host, provider)


def _username_example(provider: providers.Provider | None) -> str:
    """Beispiel-Benutzername — macht sichtbar, dass die volle Mailadresse gemeint ist."""
    domain = provider.domains[0] if provider and provider.domains else "example.org"
    return f"the full mail address of the mirror mailbox, e.g. mirror@{domain}"


def _reject_unsupported(provider: providers.Provider) -> None:
    """Bricht ab, wenn der Anbieter Passwort-Anmeldung serverseitig verweigert.

    Raises:
        CliError: Immer — der Aufruf erfolgt nur für nicht unterstützte Anbieter.
    """
    text = f"MailDigest cannot read {provider.name}. {provider.unsupported_reason}"
    if provider.note:
        text = f"{text}\n\n{provider.note}"
    raise CliError(text, EXIT_USAGE)


def cmd_connect_mail(ctx: Context) -> int:
    """Fragt die IMAP-Zugangsdaten ab, testet sie und wählt den Ordner (F-ING-3)."""
    console, args = ctx.console, ctx.args
    config_file = ConfigFile.load(ctx.config_path)
    imap = config_file.section("imap")

    console.out("Connecting the mirror mailbox (IMAPS only, certificate check always on)")
    configured_host = str(imap.get("host", ""))
    if console.interactive and not configured_host and not args.host:
        console.out("")
        console.out(providers.MIRROR_RECOMMENDATION)
    console.out("")
    console.out(_HOST_EXPLANATION.format(examples=providers.host_examples()))
    console.out("")
    resolved = _resolve_host(
        console,
        args.host
        or console.ask("IMAP host", default=configured_host, flag="--host", required=True),
    )
    host, provider = resolved.host, resolved.provider
    if provider is not None and not provider.supported:
        _reject_unsupported(provider)
    if provider is not None:
        console.out("")
        console.out(providers.setup_guide(provider))
        console.out("")

    fallback_port = provider.imap_port if provider is not None else 993
    port = (
        args.port
        if args.port is not None
        else console.ask_int("Port", default=int(imap.get("port", fallback_port)))
    )
    console.out(f"Username = {_username_example(provider)}")
    username = args.username or console.ask(
        "Username",
        default=str(imap.get("username", "")),
        flag="--username",
        required=True,
    )

    imap["host"] = host
    imap["port"] = port
    imap["username"] = username
    folder = args.folder or str(imap.get("folder", "INBOX")) or "INBOX"
    imap["folder"] = folder
    if args.move_processed_to is not None:
        imap["move_processed_to"] = args.move_processed_to
    # Struktur zuerst prüfen: Ein falscher Port soll nicht erst nach der Passwortfrage
    # auffallen.
    _validate(ImapConfig, dict(imap), f"[imap] in {ctx.config_path}")
    if port == _PLAINTEXT_IMAP_PORT:
        raise CliError(
            "Port 143 is plaintext IMAP and is not supported. MailDigest connects over "
            "IMAPS only (usually port 993).",
            EXIT_ERROR,
        )

    env_password = os.environ.get(ENV_IMAP_PASSWORD, "")
    if env_password:
        console.out(f"Password: from {ENV_IMAP_PASSWORD} (not written to the file)")
        imap.pop("password", None)
        password = env_password
    else:
        if provider is not None:
            console.out(f"Password = {provider.password_kind}.")
        entered = console.ask_secret(
            f"Password (leave empty to use {ENV_IMAP_PASSWORD} instead)"
        )
        if entered:
            imap["password"] = entered
        password = entered or str(imap.get("password", ""))
        if not password:
            raise CliError(
                "No IMAP password given. Either enter it here or set the environment "
                f"variable {ENV_IMAP_PASSWORD}.",
                EXIT_USAGE,
            )

    probe = dict(imap)
    probe["password"] = password
    section = _validate(ImapConfig, probe, f"[imap] in {ctx.config_path}")

    if args.no_test:
        console.out("Connection test skipped (--no-test).")
    else:
        folder = _test_imap_and_choose_folder(ctx, section)
        imap["folder"] = folder

    config_file.save()
    console.out(f"Saved to {config_file.path} (file mode 0600).")
    console.out("")
    console.out(_FORWARDING_GUIDE)
    return EXIT_OK


def _test_imap_and_choose_folder(ctx: Context, section: ImapConfig) -> str:
    """Verbindet, listet die Ordner und lässt den Nutzer einen wählen.

    Returns:
        Den gewählten Ordnernamen.

    Raises:
        CliError: Verbindung oder Login fehlgeschlagen.
    """
    console = ctx.console
    console.out("Connecting ...")
    try:
        client = ctx.hooks.imap_client(section)
        client.connect()
    except IngestError as exc:
        # Der anbieterspezifische Hinweis („App-Passwort nötig") gehört nur an einen
        # Anmeldefehler. Bei einem abgelehnten oder unerreichbaren Port führt er in die
        # Irre — dort ist nicht das Passwort das Problem, sondern Host, Port oder Netz
        # (HC-32).
        if isinstance(exc, ImapAuthError):
            advice = providers.auth_failure_hint(section.host)
        else:
            advice = (
                "The server could not be reached at all — no login was attempted. Check "
                "host, port (IMAPS is usually 993) and your network connection."
            )
        raise CliError(
            f"{exc}\n\n{advice}\n\nWith --no-test the values can also be saved without "
            "testing them.",
            EXIT_ERROR,
        ) from exc
    try:
        console.step("Connection established.")
        try:
            folders = client.list_folders()
        except IngestError as exc:
            console.err(f"Folder list unavailable ({exc}); keeping the current setting.")
            return section.folder
    finally:
        client.disconnect()

    if not folders:
        return section.folder

    if section.folder not in folders:
        # Der eingestellte Ordner existiert dort nicht. Ohne Rückfrage einfach den ersten
        # der Liste zu nehmen wäre gefährlich: Server sortieren alphabetisch, der erste
        # Eintrag ist häufig „Drafts"/„Entwurf". MailDigest würde dann Entwürfe lesen und
        # als gelesen markieren, statt den Posteingang.
        console.err(
            f'The configured folder "{section.folder}" does not exist on the server.'
        )
        if not console.interactive:
            # Erst die Liste, dann die Meldung: Sie verwies auf eine Liste „above", die
            # noch gar nicht gedruckt war — und sie steht auf stderr, die Liste auf
            # stdout, wo die Reihenfolge beim Umleiten ohnehin nicht verlässlich ist.
            # Deshalb nennt die Meldung die Liste ohne Ortsangabe (HC-32).
            for number, name in enumerate(folders, start=1):
                console.out(f"  {number:>2}) {_safe_name(name)}")
            console.err(
                "Keeping it unchanged — pick one of the folders listed on stdout with "
                "--folder, otherwise the next run will fail."
            )
            return section.folder

    names = [_safe_name(name) for name in folders]
    if section.folder in folders:
        default_index = folders.index(section.folder)
    else:
        # Kein Treffer: INBOX ist die einzige vertretbare Vorgabe, sonst der erste Eintrag.
        default_index = folders.index("INBOX") if "INBOX" in folders else 0
    console.out("Which folder should MailDigest read?")
    index = console.choose("Folder", names, default_index=default_index)
    return folders[index]


# --- Kommando: connect-llm --------------------------------------------------------------------


def _choose_llm_preset(
    ctx: Context, *, current: str, current_base_url: str = ""
) -> providers.LlmPreset:
    """Lässt die Betriebsart wählen und liefert die passende Vorlage.

    Im nicht-interaktiven Modus (und wenn `--provider` gesetzt ist) wird nicht gefragt: Dann
    entscheidet die Option, und die Vorlage dient nur noch als Quelle für Erklärtext und
    Basis-URL-Vorbelegung.

    Args:
        current: Bisheriger Wert von `[llm] provider`.
        current_base_url: Bisheriger Wert von `[llm] base_url`; wählt in der interaktiven
            Liste den passenden Eintrag vor, wenn `provider` mehrdeutig ist (HC-3).
    """
    console, args = ctx.console, ctx.args
    wanted = args.provider or current
    if args.provider or not console.interactive:
        # Ohne `--base-url` wird hier **keine** anbieterspezifische URL vorbelegt: Die
        # generische Vorlage trägt keine (HC-3/E5).
        return providers.find_preset(wanted)

    console.out("")
    console.out(providers.LLM_CHOICE_INTRO)
    console.out("")
    labels = [preset.label for preset in providers.LLM_PRESETS]
    preselected = providers.find_preset(wanted, base_url=current_base_url)
    default_index = providers.LLM_PRESETS.index(preselected)
    return providers.LLM_PRESETS[console.choose("Option", labels, default_index=default_index)]


def cmd_connect_llm(ctx: Context) -> int:
    """Wählt Provider und Modell, speichert den Key und macht einen Testaufruf (F-LLM-2)."""
    console, args = ctx.console, ctx.args
    config_file = ConfigFile.load(ctx.config_path)
    llm = config_file.section("llm")

    console.out("Connecting the language model")
    preset = _choose_llm_preset(
        ctx,
        current=str(llm.get("provider", "none")),
        current_base_url=str(llm.get("base_url", "")),
    )
    provider = args.provider or preset.provider
    llm["provider"] = provider

    if preset.detail:
        console.out("")
        console.out(preset.detail)
        console.out("")

    if provider == "none":
        # Ohne Modell gibt es weder Modellname noch Schlüssel noch Testaufruf.
        llm["model"] = ""
        _set_or_clear(llm, "base_url", "")
        llm.pop("api_key", None)
        config_file.save()
        console.out(f"Saved to {config_file.path} (file mode 0600).")
        console.out(
            "MailDigest now runs without a language model. Run this command again at any "
            "time to connect one."
        )
        return EXIT_OK

    model = args.model or console.ask(
        "Model name (exact model ID used by the provider)",
        default=str(llm.get("model", "")),
        flag="--model",
        required=True,
    )
    llm["model"] = model

    if provider == "openai_compatible":
        base_url = args.base_url if args.base_url is not None else console.ask(
            "Base URL of the endpoint (e.g. http://localhost:11434/v1)",
            default=str(llm.get("base_url", "")) or preset.base_url,
            flag="--base-url",
        )
        llm["base_url"] = base_url
        if base_url and not base_url.startswith(("https://", "http://localhost", "http://127.")):
            console.err(
                "Note: this base URL is unencrypted and not local — mail content would "
                "travel over the network in the clear."
            )
    elif args.base_url is not None:
        llm["base_url"] = args.base_url

    env_key = os.environ.get(ENV_LLM_API_KEY, "")
    if env_key:
        console.out(f"API key: from {ENV_LLM_API_KEY} (not written to the file)")
        llm.pop("api_key", None)
        api_key = env_key
    else:
        entered = console.ask_secret(
            f"API key (leave empty to use {ENV_LLM_API_KEY}, or if the endpoint needs no "
            "key)"
        )
        if entered:
            llm["api_key"] = entered
        api_key = entered or str(llm.get("api_key", ""))

    probe = dict(llm)
    if api_key:
        probe["api_key"] = api_key
    section = _validate(LlmConfig, probe, f"[llm] in {ctx.config_path}")

    if args.no_test:
        console.out("Test call skipped (--no-test).")
    else:
        _test_llm(ctx, section)

    config_file.save()
    console.out(f"Saved to {config_file.path} (file mode 0600).")
    return EXIT_OK


def _known_secrets(section: LlmConfig) -> list[str]:
    """Die Secrets, die bei einem LLM-Testaufruf im Spiel sind (Config **und** Umgebung)."""
    values = [os.environ.get(ENV_LLM_API_KEY, "")]
    key = section.api_key
    if key is not None:
        values.append(key.get_secret_value())
    return [value for value in values if value]


def _test_llm(ctx: Context, section: LlmConfig) -> None:
    """Schickt einen minimalen Testaufruf an den Provider.

    Die Modellantwort ist untrusted (I4) und wird deshalb **nicht** ausgegeben — gemeldet
    werden nur Länge und ob das erwartete Wort vorkommt.

    Raises:
        CliError: Provider nicht erreichbar, Key abgelehnt oder leere Antwort.
    """
    console = ctx.console
    console.out("Test call ...")
    try:
        provider = ctx.hooks.build_provider(
            provider=section.provider,
            model=section.model,
            base_url=section.base_url,
            api_key=section.api_key,
            # Der Testaufruf schickt einen festen, inhaltsfreien Satz — der Antworttext des
            # Anbieters kann hier also nichts aus einer Mail zitieren und ist genau die
            # Auskunft, die bei einer abgelehnten Verbindung weiterhilft.
            reveal_error_details=True,
            # Genau ein Versuch: Ein Ratenlimit beim Einrichten wiederholt sich nicht
            # binnen Sekunden, und bei Anbietern mit Tageskontingent (OpenRouter: 50
            # Anfragen/Tag) würde jede Wiederholung davon eine weitere aufbrauchen. Der
            # Test soll melden, was der Anbieter sagt — nicht ihn überreden.
            max_attempts=1,
        )
        answer = provider.complete(
            "You are answering a connection test.",
            "Reply with the single word OK and nothing else.",
            max_tokens=16,
        )
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc
    except LLMError as exc:
        # Der Anbietertext ist bereits zeichengefiltert (`llm/_http._foreign`). Hier kommt
        # die zweite Zusage dazu: Zitiert die Gegenstelle den gesendeten Schlüssel, steht
        # er nicht auf dem Terminal (I5, SPEC-CLI §2).
        detail = mask_secrets(str(exc), _known_secrets(section))
        raise CliError(
            f"Test call failed ({type(exc).__name__}): {detail}\n"
            "Check the model name, API key and base URL.",
            EXIT_ERROR,
        ) from exc
    if not answer.strip():
        raise CliError("The provider returned an empty response.", EXIT_ERROR)
    marker = "expected reply" if "OK" in answer.upper() else "unexpected reply"
    console.step(f"Response received ({len(answer)} characters, {marker}).")


# --- Kommando: connect-messenger ---------------------------------------------------------------


def cmd_connect_messenger(ctx: Context) -> int:
    """Richtet den Zustellweg ein und schickt eine Testnachricht (F-MSG-2)."""
    console, args = ctx.console, ctx.args
    config_file = ConfigFile.load(ctx.config_path)
    messenger_section = config_file.section("messenger")

    console.out("Connecting the messenger")
    active = args.messenger or console.ask(
        "Messenger",
        default=str(messenger_section.get("active", "telegram")),
        allowed=_MESSENGERS,
        flag="--messenger",
    )
    messenger_section["active"] = active

    if active == "telegram":
        _setup_telegram(ctx, config_file)
    elif active == "discord":
        _setup_discord(ctx, config_file)
    else:
        _setup_signal(ctx, config_file)

    probe = _messenger_probe(config_file)
    section = _validate(MessengerConfig, probe, f"[messenger] in {ctx.config_path}")

    if args.no_test:
        console.out("Test message skipped (--no-test).")
    else:
        _send_test_message(ctx, section)

    config_file.save()
    console.out(f"Saved to {config_file.path} (file mode 0600).")
    return EXIT_OK


def _messenger_probe(config_file: ConfigFile) -> dict[str, Any]:
    """Baut die zu validierende `[messenger]`-Sektion inkl. Secret aus der Umgebung."""
    probe: dict[str, Any] = copy.deepcopy(config_file.section("messenger"))
    token = os.environ.get(ENV_TELEGRAM_TOKEN, "")
    if token:
        telegram = probe.setdefault("telegram", {})
        if isinstance(telegram, dict):
            telegram["token"] = token
    return probe


def _setup_telegram(ctx: Context, config_file: ConfigFile) -> None:
    """Token abfragen und die Chat-ID über `getUpdates` ermitteln."""
    console, args = ctx.console, ctx.args
    telegram = config_file.section("messenger.telegram")

    env_token = os.environ.get(ENV_TELEGRAM_TOKEN, "")
    if env_token:
        console.out(f"Bot token: from {ENV_TELEGRAM_TOKEN} (not written to the file)")
        telegram.pop("token", None)
        token = env_token
    else:
        console.out(providers.TELEGRAM_GUIDE)
        console.out("")
        entered = console.ask_secret(
            f"Bot token (leave empty to use {ENV_TELEGRAM_TOKEN} instead)"
        )
        if entered:
            telegram["token"] = entered
        token = entered or str(telegram.get("token", ""))
    if not token:
        raise CliError(
            "No bot token given. Either enter it here or set the environment variable "
            f"{ENV_TELEGRAM_TOKEN}.",
            EXIT_USAGE,
        )

    if args.chat_id:
        telegram["chat_id"] = args.chat_id
    else:
        telegram["chat_id"] = _discover_chat_id(ctx, SecretStr(token), telegram)
    # Alte Dateien kennen das Feld nicht; `connect-messenger` vervollständigt den Feldsatz
    # aus SPEC-CLI §5 mit dem Default (HC-18 (d)).
    telegram.setdefault("accept_commands", _TELEGRAM_ACCEPT_COMMANDS_DEFAULT)
    # Genau einmal, in beiden Zweigen: Mit `--chat-id` und `--no-test` erfuhr der Nutzer
    # sonst nichts vom Befehlskanal (HC-19).
    console.out("")
    console.out(_TELEGRAM_SETUP_HINT)


def _discover_chat_id(
    ctx: Context, token: SecretStr, telegram: dict[str, Any]
) -> str:
    """Wartet auf eine Nachricht an den Bot und liest die Chat-ID daraus (F-MSG-2)."""
    console = ctx.console
    console.out("Now send your bot a message in Telegram (e.g. /start).")
    attempts = _CHAT_ID_ATTEMPTS if console.interactive else 1
    candidates: list[ChatCandidate] = []
    for attempt in range(1, attempts + 1):
        try:
            candidates = ctx.hooks.discover_chat_ids(token=token)
        except MessengerError as exc:
            raise CliError(
                f"Telegram request failed: {exc}\nIs the bot token correct?",
                EXIT_ERROR,
            ) from exc
        if candidates:
            break
        if attempt < attempts:
            console.step(f"No message received yet — waiting ({attempt}/{attempts}) ...")
            ctx.hooks.sleep(_CHAT_ID_WAIT_SECONDS)

    if not candidates:
        existing = str(telegram.get("chat_id", ""))
        if existing:
            console.err("No new message found — keeping the existing chat ID.")
            return existing
        raise CliError(
            "No message to the bot found. Send the bot a message and run "
            "`maildigest connect-messenger` again — or pass the chat ID with "
            "--chat-id.",
            EXIT_ERROR,
        )
    if len(candidates) == 1:
        console.step(f"Chat ID found: {candidates[0].chat_id}")
        return candidates[0].chat_id
    console.out("Several chats found — which one should it be?")
    labels = [f"{item.chat_id} ({item.chat_type})" for item in candidates]
    return candidates[console.choose("Chat", labels)].chat_id


def _setup_discord(ctx: Context, config_file: ConfigFile) -> None:
    """Webhook-URL abfragen (sie ist selbst das Secret)."""
    console, args = ctx.console, ctx.args
    discord = config_file.section("messenger.discord")
    console.out(providers.DISCORD_GUIDE)
    console.out("")
    url = args.webhook_url or console.ask_secret("Webhook URL")
    if url:
        discord["webhook_url"] = url
    if not str(discord.get("webhook_url", "")):
        raise CliError(
            "No webhook URL given. The URL is itself the secret; it is stored in the "
            "configuration file with file mode 0600.",
            EXIT_USAGE,
        )


def _setup_signal(ctx: Context, config_file: ConfigFile) -> None:
    """signal-cli-Socket abfragen und den Adapter freischalten."""
    console, args = ctx.console, ctx.args
    signal_section = config_file.section("messenger.signal")
    console.out(providers.SIGNAL_GUIDE)
    console.out("")
    console.out("Prerequisite: `signal-cli --daemon --socket <path>` is already running.")
    socket_path = args.signal_socket or console.ask(
        "Path of the signal-cli socket",
        default=str(signal_section.get("signal_cli_socket", "")),
        flag="--signal-socket",
        required=True,
    )
    signal_section["signal_cli_socket"] = socket_path
    signal_section["enabled"] = True


def _send_test_message(ctx: Context, section: MessengerConfig) -> None:
    """Healthcheck + eine im Code formulierte Testnachricht (I3: über `compose_plain`)."""
    console = ctx.console
    console.out("Sending test message ...")
    try:
        messenger = ctx.hooks.build_messenger(section)
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc
    if not messenger.healthcheck():
        raise CliError(
            f'The service "{section.active}" is unreachable or the credentials are '
            "rejected.",
            EXIT_ERROR,
        )
    composer = DigestComposer(part_limit=part_limit_for(section.active))
    try:
        text = _TEST_MESSAGE
        if section.active == "telegram" and section.telegram.accept_commands:
            text += _TELEGRAM_COMMAND_HINT
        messenger.send(composer.compose_plain(text))
    except MessengerError as exc:
        raise CliError(f"The test message could not be delivered: {exc}", EXIT_ERROR) from exc
    console.step("Test message delivered — check your messenger.")


# --- Kommando: test ------------------------------------------------------------------------


@dataclass
class _ProbeSanitizer:
    """Sanitizer-Stufe, die ihr Ergebnis für die Ausgabe von `maildigest test` festhält."""

    inner: Sanitizer
    result: SanitizedMail | None = None

    def sanitize(self, raw: RawMail) -> SanitizedMail:
        """Sanitisiert und merkt sich das Ergebnis."""
        self.result = self.inner.sanitize(raw)
        return self.result


@dataclass
class _ProbeSummarizer:
    """Summarizer-Stufe mit Mitschnitt (nur Metadaten werden später angezeigt)."""

    inner: Summarizer
    result: Summary | None = None

    def summarize(self, mail: SanitizedMail) -> Summary:
        """Fasst zusammen und merkt sich das Ergebnis."""
        self.result = self.inner.summarize(mail)
        return self.result


@dataclass
class _ProbeCritic:
    """Kritiker-Stufe mit Mitschnitt."""

    inner: Critic
    result: CriticVerdict | None = None

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        """Prüft und merkt sich das Verdikt."""
        self.result = self.inner.review(mail, summary)
        return self.result


@dataclass
class _CollectingMessenger:
    """Zustell-Attrappe für `maildigest test --dry-run`."""

    messages: list[DigestMessage] = field(default_factory=list)

    def send(self, message: DigestMessage) -> None:
        """Nimmt die Nachricht entgegen, statt sie zu verschicken."""
        self.messages.append(message)

    def healthcheck(self) -> bool:
        """Immer erreichbar."""
        return True


def _selftest_notice(*, from_file: bool) -> str:
    """Vorspann der Selbsttest-Zustellung, passend zur Herkunft der Mail (HC-17).

    Ohne Punkte und Domains, damit der Nachbrenner des Output-Sanitizers den Text nicht
    sichtbar entschärft (wie bei :data:`_TEST_MESSAGE`). Der **Dateipfad** steht bewusst
    nicht darin: Er trüge Punkte, käme also entstellt an — die Kategorie genügt.

    Args:
        from_file: Ob die Mail aus einer mit `--eml` übergebenen Datei stammt.
    """
    origin = "the file you supplied" if from_file else "the bundled example mail"
    return (
        "🧪 MailDigest self-test\n"
        f"The next message is built from {origin}, not from your mailbox — "
        "there is no such mail to look for"
    )


def _announce_selftest(ctx: Context, config: Config, *, from_file: bool) -> None:
    """Kündigt die Selbsttest-Zustellung an, damit sie erkennbar ist.

    Ein Fehlschlag ist hier kein Grund abzubrechen: Der eigentliche Test ist die
    Zustellung danach, und die meldet ihre Probleme selbst.
    """
    try:
        messenger = ctx.hooks.build_messenger(config.messenger)
        composer = DigestComposer(part_limit=part_limit_for(config.messenger.active))
        messenger.send(composer.compose_plain(_selftest_notice(from_file=from_file)))
    except (ConfigError, MessengerError):
        ctx.console.err("Note: the self-test marker could not be delivered.")


def cmd_test(ctx: Context) -> int:
    """Ende-zu-Ende-Selbsttest mit einer `.eml`-Datei statt aus dem Postfach (F-OPS-2)."""
    console, args = ctx.console, ctx.args
    config = _load_full_config(ctx.config_path)
    console.out(f"1/5 Configuration loaded: {ctx.config_path}")

    raw_bytes, source = _read_test_mail(args.eml)
    console.out(f"2/5 Test mail read: {source} ({len(raw_bytes)} bytes)")
    raw = _build_raw_mail(raw_bytes)

    # Für den Selbsttest zählt „kommt etwas an“, nicht die Wichtigkeits-Schwelle des
    # Nutzers: Sonst landete eine als `low` eingestufte Testmail lautlos im Sammel-Digest.
    test_config = config.model_copy(
        update={"general": config.general.model_copy(update={"deliver_min_importance": "low"})}
    )

    sanitizer = _ProbeSanitizer(MailSanitizer.from_config(test_config))
    try:
        summarizer = _ProbeSummarizer(ctx.hooks.build_summarizer(test_config))
        critic = _ProbeCritic(ctx.hooks.build_critic(test_config))
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc

    collector = _CollectingMessenger() if args.dry_run else None
    if collector is not None:
        console.out("    Dry run: nothing is sent to the messenger (--dry-run).")
    else:
        # Ohne diesen Vorspann ist die zugestellte Nachricht von einer echten
        # Zusammenfassung nicht zu unterscheiden — und der Nutzer sucht im Postfach nach
        # einer Mail, die es nie gab (Feldbericht 2026-09-09).
        _announce_selftest(ctx, test_config, from_file=args.eml is not None)

    with tempfile.TemporaryDirectory(prefix="maildigest-test-") as tmp:
        # Eigene State-Datei: Der Selbsttest darf weder den Dedupe-Stand noch die
        # Zustell-Warteschlange des Betriebs verändern.
        try:
            db = StateDB(Path(tmp) / "selftest.db")
        except StateError as exc:
            raise CliError(str(exc), EXIT_ERROR) from exc
        try:
            try:
                runner = ctx.hooks.build_runner(
                    test_config,
                    config_path=ctx.config_path,
                    db=db,
                    sanitizer=sanitizer,
                    summarizer=summarizer,
                    critic=critic,
                    messenger=collector,
                )
            except (ConfigError, StateError) as exc:
                raise CliError(str(exc), EXIT_ERROR) from exc
            db.claim(raw.dedupe_key)
            console.out("3/5 Pipeline running (sanitizer -> summarizer -> critic -> delivery) ...")
            result = runner.process(raw)
            runner.outbox.flush()
            pending = runner.outbox.pending
        finally:
            db.close()

    _report_stages(console, sanitizer, summarizer, critic)
    return _report_test_result(ctx, result, collector, pending)


def _report_stages(
    console: Console,
    sanitizer: _ProbeSanitizer,
    summarizer: _ProbeSummarizer,
    critic: _ProbeCritic,
) -> None:
    """Meldet je Stufe **nur Zahlen und Aufzählungswerte** — nie Mail- oder Modelltext."""
    mail = sanitizer.result
    if mail is None:
        console.out("4/5 Sanitizer: failed.")
        return
    report = mail.sanitization_report
    processed = sum(1 for item in mail.attachments if item.processed)
    console.out(
        f"4/5 Sanitizer: {_count(len(mail.body_text), 'character', 'characters')} of text, "
        f"{_count(len(mail.attachments), 'attachment', 'attachments')} "
        f"({processed} processed), "
        f"{_count(report.links_removed, 'link', 'links')} removed, "
        f"{_count(report.control_chars_removed, 'control character', 'control characters')} "
        "removed"
    )
    summary = summarizer.result
    if summary is None:
        console.step("Summarizer: failed.")
        return
    console.step(
        f"Summarizer: importance={summary.importance}, "
        f"injection suspected={'yes' if summary.injection_suspected else 'no'}"
    )
    verdict = critic.result
    if verdict is None:
        console.step("Critic: failed.")
        return
    console.step(
        f"Critic: phishing risk={verdict.phishing_risk}, "
        f"summary accurate={'yes' if verdict.summary_accurate else 'no'}"
    )


def _report_test_result(
    ctx: Context,
    result: Delivered | QueuedLow | FailedNotice,
    collector: _CollectingMessenger | None,
    pending: int,
) -> int:
    """Bewertet das Pipeline-Ergebnis und liefert den Exit-Code."""
    console = ctx.console
    if isinstance(result, FailedNotice):
        console.out(
            f"5/5 Fail-closed: stage {result.notice.stage}, reason {result.notice.reason_class}."
        )
        if collector is not None:
            # Trockenlauf: Es ging nichts an den Messenger — und die Notiz ist genau
            # das, was der Nutzer hier sehen will (SPEC-CLI.md §4 `test --dry-run`).
            console.out("    Metadata notice created — dry run, not sent:")
            console.out("")
            for message in collector.messages:
                for part in message.parts:
                    console.out(part)
            console.out("")
            console.err(
                "Self-test failed — only the metadata notice was created "
                "(delivered: no — dry run)."
            )
            return EXIT_ERROR
        # `notice_delivered` sagt nur, dass die Warteschlange die Notiz angenommen hat.
        # Zugestellt ist sie erst, wenn danach nichts mehr wartet (CT-4).
        delivered = result.notice_delivered and not pending
        console.err(
            "Self-test failed — only the metadata notice was created "
            f"(delivered: {'yes' if delivered else 'no'})."
        )
        return EXIT_ERROR
    if isinstance(result, QueuedLow):  # pragma: no cover - Schwelle ist auf `low` gesetzt
        console.err("Self-test: the mail went into the daily digest instead of a message.")
        return EXIT_ERROR

    parts = result.message.parts
    if collector is not None:
        console.out(f"5/5 Message created ({_parts_label(parts)}) — dry run, not sent:")
        console.out("")
        for part in parts:
            console.out(part)
        console.out("")
        return EXIT_OK
    if pending:
        # Die Schrittfolge aus SPEC-CLI §4 endet immer mit einer 5/5-Zeile — auch wenn die
        # Zustellung nicht bestätigt wurde (HC-35). Die Erklärung bleibt auf stderr.
        console.out(f"5/5 Not delivered ({_parts_label(parts)}) — queued for retry.")
        console.err(
            "Self-test: the message was created but not delivered — it is sitting in the "
            "queue. Check the messenger credentials "
            "(`maildigest connect-messenger`)."
        )
        return EXIT_ERROR
    console.out(f"5/5 Delivered ({_parts_label(parts)}). Check your messenger.")
    return EXIT_OK


def _parts_label(parts: Sequence[str]) -> str:
    """`1 part` / `3 parts` — die Nachricht wird auf das Messenger-Limit gesplittet."""
    return "1 part" if len(parts) == 1 else f"{len(parts)} parts"


def _count(number: int, singular: str, plural: str) -> str:
    """`1 attachment` / `2 attachments` — Zählform für die Zeile 4/5 (SPEC-CLI §4)."""
    return f"{number} {singular}" if number == 1 else f"{number} {plural}"


def _read_test_mail(eml: str | None) -> tuple[bytes, str]:
    """Liest die Testmail: eigene Datei oder die mitgelieferte Beispielmail."""
    if eml is None:
        data = resources.files("maildigest").joinpath("data/selftest.eml").read_bytes()
        return data, "bundled example mail"
    path = Path(eml)
    try:
        return path.read_bytes(), str(path)
    except OSError as exc:
        raise CliError(
            f"Test mail {path} cannot be read: {exc.strerror}.", EXIT_ERROR
        ) from exc


def _build_raw_mail(raw_bytes: bytes) -> RawMail:
    """Baut aus `.eml`-Bytes dasselbe :class:`RawMail`, das der Ingest liefern würde."""
    try:
        return build_raw_mail(MailMessage.from_bytes(raw_bytes))
    except Exception as exc:  # jede Parser-Panne ist hier ein Bedienfehler, kein Absturz
        raise CliError(
            f"The file is not a readable e-mail ({type(exc).__name__}). An RFC 822 file "
            "(`.eml`) with headers and a body is expected.",
            EXIT_USAGE,
        ) from exc


# --- Kommando: run --------------------------------------------------------------------------


def cmd_run(ctx: Context) -> int:
    """Startet den Daemon bzw. einen Einzellauf (F-OPS-1)."""
    console, args = ctx.console, ctx.args
    config = _load_full_config(ctx.config_path)
    if config.imap.password is None:
        # Ein fehlendes Passwort ist ein Konfigurationsfehler, kein Erreichbarkeitsproblem:
        # Sonst sucht der Nutzer beim Server statt in seiner Datei (HC-34).
        raise CliError(
            f"Invalid configuration ({ctx.config_path}):\n"
            "  - [imap] password: required value missing. Set it in the configuration "
            f"or through the environment variable {ENV_IMAP_PASSWORD}.\n"
            "Reference for all fields: docs/SPEC-CLI.md §5.",
            EXIT_ERROR,
        )
    ctx.hooks.configure_logging(config.general.log_level)
    try:
        runner = ctx.hooks.build_runner(config, config_path=ctx.config_path)
    except (ConfigError, StateError) as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc

    try:
        if args.once:
            try:
                stats = runner.run_once()
            except IngestError as exc:
                raise CliError(f"Mailbox unreachable: {exc}", EXIT_ERROR) from exc
            console.err(
                f"Run finished: {stats.ingest.fetched} mails fetched, "
                f"{stats.ingest.processed} processed, {stats.ingest.duplicates} duplicates, "
                f"{stats.ingest.failed} errors, {stats.delivery.delivered} messages "
                f"delivered, {runner.outbox.pending} queued."
            )
            return EXIT_OK
        runner.run_forever()
        return EXIT_OK
    finally:
        runner.db.close()


# --- Gemeinsame Helfer -----------------------------------------------------------------------


_SectionT = TypeVar("_SectionT", bound=BaseModel)


def _validate(model: type[_SectionT], data: dict[str, Any], source: str) -> _SectionT:
    """Validiert eine Sektion und macht aus :class:`ConfigError` einen CLI-Abbruch."""
    try:
        return validate_section(model, data, source=source)  # type: ignore[type-var]
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc


def _load_full_config(path: Path) -> Config:
    """Lädt und validiert die vollständige Konfiguration (inkl. Env-Overrides)."""
    try:
        return load_config(path)
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc


def _resolve_config_path(args: argparse.Namespace) -> Path:
    """Bestimmt den Pfad der Konfigurationsdatei: `--config` → Env → `./config.toml`.

    `--config` darf vor oder nach dem Kommandonamen stehen (SPEC-CLI.md §3); beide
    Schreibweisen landen im selben Namespace-Schlüssel (siehe `build_parser`).
    """
    explicit = getattr(args, "config", None)
    if explicit:
        return Path(str(explicit)).expanduser()
    from_env = os.environ.get(ENV_CONFIG, "")
    if from_env:
        return Path(from_env).expanduser()
    return Path(DEFAULT_CONFIG_NAME)


# --- Argumentparser ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """Argparse-Parser, der bei Bedienfehlern eine Exception statt `SystemExit` wirft."""

    def error(self, message: str) -> Any:
        """Wandelt jeden argparse-Fehler in einen CLI-Abbruch mit Exit-Code 2."""
        raise CliError(f"{self.prog}: {message}", EXIT_USAGE)


#: Erlaubter Bereich eines TCP-Ports (SPEC-CLI.md §4 `connect-mail`, §5 `[imap] port`).
_PORT_MIN = 1
_PORT_MAX = 65535


def _port_value(raw: str) -> int:
    """Prüft `--port` schon im Parser, damit ein Bereichsfehler Exit-Code 2 ergibt.

    Ohne diese Prüfung fiele `--port 0` erst der Schema-Validierung zur Last und
    endete als Konfigurationsfehler (Exit 1). Ein unerlaubter **Optionswert** ist
    laut SPEC-CLI.md §2 aber ein Bedienfehler — genauso wie `--language klingon`.

    Raises:
        argparse.ArgumentTypeError: Keine Zahl oder außerhalb von 1..65535.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{raw}' is not a whole number (allowed: {_PORT_MIN} to {_PORT_MAX})"
        ) from None
    if not _PORT_MIN <= value <= _PORT_MAX:
        raise argparse.ArgumentTypeError(
            f"{value} is outside the allowed range {_PORT_MIN} to {_PORT_MAX}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    """Baut den vollständigen Argumentparser (Vertrag: docs/SPEC-CLI.md, Abschnitte 3 und 4)."""
    # `default=argparse.SUPPRESS` ist hier der ganze Trick (SPEC-CLI.md §3): Der
    # Subparser schreibt seine Ergebnisse in **dieselbe** Namespace-Instanz wie der
    # Hauptparser. Mit einem gewöhnlichen Default (None/False) überschriebe er damit
    # jeden Wert, der vor dem Kommandonamen stand — `maildigest --config x init`
    # arbeitete dann still auf `config.toml`. Mit SUPPRESS taucht der Schlüssel nur
    # auf, wenn die Option tatsächlich angegeben wurde; beide Schreibweisen sind
    # dadurch gleichwertig, und eine doppelte Angabe gewinnt hinten.
    common = _ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="path of the configuration file",
    )
    common.add_argument(
        "--non-interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="ask nothing; use defaults and options",
    )

    parser = _ArgumentParser(
        prog="maildigest",
        description=(
            "Summarises mail from a mirror mailbox, checks it for phishing and sends "
            "plain text to a messenger."
        ),
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = subparsers.add_parser(
        "init", parents=[common], help="create the configuration file (0600)"
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.add_argument("--language", choices=list(_LANGUAGES), help="language of the summaries")
    init.add_argument(
        "--summary-length", choices=list(_SUMMARY_LENGTHS), help="length of the summaries"
    )
    init.add_argument(
        "--min-importance", choices=list(_IMPORTANCES), help="threshold for individual delivery"
    )
    init.add_argument("--low-digest-time", metavar="HH:MM", help="time of the daily digest")
    init.add_argument("--instructions", metavar="TEXT", help="custom instructions")
    init.set_defaults(func=cmd_init)

    mail = subparsers.add_parser(
        "connect-mail", parents=[common], help="connect and test the mirror mailbox"
    )
    mail.add_argument("--host", metavar="HOST", help="IMAP host")
    mail.add_argument(
        "--port", type=_port_value, metavar="PORT", help="IMAP port (default 993)"
    )
    mail.add_argument("--username", metavar="NAME", help="IMAP username")
    mail.add_argument("--folder", metavar="FOLDER", help="folder to read")
    mail.add_argument(
        "--move-processed-to", metavar="FOLDER", help="move processed mail there"
    )
    mail.add_argument("--no-test", action="store_true", help="save without a connection test")
    mail.set_defaults(func=cmd_connect_mail)

    llm = subparsers.add_parser(
        "connect-llm", parents=[common], help="connect and test the language model"
    )
    llm.add_argument("--provider", choices=list(_PROVIDERS), help="provider")
    llm.add_argument("--model", metavar="ID", help="model ID of the provider")
    llm.add_argument("--base-url", metavar="URL", help="endpoint for openai_compatible")
    llm.add_argument("--no-test", action="store_true", help="save without a test call")
    llm.set_defaults(func=cmd_connect_llm)

    messenger = subparsers.add_parser(
        "connect-messenger", parents=[common], help="connect and test the messenger"
    )
    messenger.add_argument("--messenger", choices=list(_MESSENGERS), help="target system")
    messenger.add_argument(
        "--chat-id", metavar="ID", help="Telegram chat ID (instead of getUpdates)"
    )
    messenger.add_argument("--webhook-url", metavar="URL", help="Discord webhook URL")
    messenger.add_argument("--signal-socket", metavar="PATH", help="path of the signal-cli socket")
    messenger.add_argument("--no-test", action="store_true", help="save without a test message")
    messenger.set_defaults(func=cmd_connect_messenger)

    test = subparsers.add_parser(
        "test", parents=[common], help="end-to-end self-test with an example mail"
    )
    test.add_argument("--eml", metavar="PATH", help="your own .eml file instead of the example")
    test.add_argument(
        "--dry-run", action="store_true", help="only show the message, do not deliver it"
    )
    test.set_defaults(func=cmd_test)

    run = subparsers.add_parser("run", parents=[common], help="continuous operation (polling)")
    run.add_argument("--once", action="store_true", help="process once and exit")
    run.set_defaults(func=cmd_run)

    return parser


# --- Einstiegspunkt ----------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    hooks: Hooks | None = None,
) -> int:
    """Führt ein Kommando aus und liefert den Exit-Code (0 ok, 1 Fehler, 2 Bedienfehler)."""
    streams_in = stdin if stdin is not None else sys.stdin
    streams_out = stdout if stdout is not None else sys.stdout
    streams_err = stderr if stderr is not None else sys.stderr
    parser = build_parser()

    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if getattr(args, "command", None) is None:
            parser.print_help(streams_out)
            return EXIT_USAGE
        # `--non-interactive` ist mit `SUPPRESS` belegt (siehe `build_parser`) und fehlt
        # im Namespace, wenn es nicht angegeben wurde.
        console = Console(
            streams_in,
            streams_out,
            streams_err,
            interactive=not getattr(args, "non_interactive", False),
        )
        context = Context(
            console=console,
            hooks=hooks if hooks is not None else Hooks(),
            config_path=_resolve_config_path(args),
            args=args,
        )
        command: Callable[[Context], int] = args.func
        if command is not cmd_run:
            # Ohne konfigurierten Handler landen Bibliotheks-Logs (z. B. `llm_retry` aus
            # dem Provider) über `logging.lastResort` als **unformatierter** Text auf
            # stderr — an JsonLogFormatter vorbei und damit an der Zusicherung
            # „strukturierte JSON-Zeilen" (SECURITY §6) vorbei. Für alle Kommandos außer
            # `run` geht das Protokoll deshalb als JSON auf **stderr**: stdout gehört bei
            # `test`/`connect-*` allein der in SPEC-CLI §4 vertraglich festgelegten
            # Schritt-Ausgabe. `cmd_run` konfiguriert danach selbst neu (stdout, Level
            # aus der Config).
            context.hooks.configure_logging("WARNING", stream=streams_err)
        return command(context)
    except CliError as exc:
        streams_err.write(f"Error: {_terminal_text(str(exc))}\n")
        return exc.code
    except KeyboardInterrupt:
        streams_err.write("Error: Aborted.\n")
        return EXIT_ERROR
    except (ConfigError, StateError, IngestError, MessengerError, LLMError) as exc:
        # Sicherheitsnetz: Eine hier durchgerutschte Ausnahme darf keinen Traceback mit
        # möglichen Inhalten auf das Terminal schreiben (I5) — und keine Steuersequenz
        # einer Gegenstelle, die sich in den Text gerettet hat (HC-4).
        streams_err.write(f"Error: {_terminal_text(str(exc))}\n")
        return EXIT_ERROR
    except httpx.HTTPError as exc:
        streams_err.write(f"Error: network problem ({type(exc).__name__}).\n")
        return EXIT_ERROR


def run_cli() -> None:  # pragma: no cover - dünner Wrapper für den Konsolen-Einstiegspunkt
    """Konsolen-Einstiegspunkt (`[project.scripts] maildigest`)."""
    sys.exit(main())
