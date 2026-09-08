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

from maildigest.agents.critic import CriticAgent
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
    load_config,
    validate_section,
)
from maildigest.ingest.imap_client import ImapClient, IngestError, build_raw_mail
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
_PROVIDERS = ("anthropic", "openai_compatible")
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

#: Text der Testnachricht (`connect-messenger`). Bewusst ohne Punkte, Domains und Markup —
#: der Nachbrenner des Output-Sanitizers würde sie sonst sichtbar entschärfen.
_TEST_MESSAGE = (
    "✅ MailDigest Testnachricht\n"
    "Die Zustellung funktioniert — ab jetzt landen hier deine Mail-Zusammenfassungen"
)


class CliError(Exception):
    """Abbruch mit definiertem Exit-Code und einer Meldung für `stderr`.

    Die Meldung ist deutsch, nennt nach Möglichkeit den nächsten Schritt und enthält
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
                "Eingabe abgebrochen (Ende der Eingabe erreicht). Für Läufe ohne "
                "Terminal --non-interactive verwenden und die Werte als Optionen setzen.",
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
                    f"{prompt}: Pflichtangabe fehlt. Im nicht-interaktiven Modus über "
                    f"{flag or 'die passende Option'} setzen.",
                    EXIT_USAGE,
                )
            return value

        suffix = f" [{default}]" if default else ""
        options = f" ({'/'.join(allowed)})" if allowed else ""
        for _attempt in range(3):
            value = self._readline(f"{prompt}{options}{suffix}: ") or default
            if allowed and value not in allowed:
                self.err(f"Ungültiger Wert. Erlaubt: {', '.join(allowed)}.")
                continue
            if required and not value:
                self.err("Pflichtangabe — bitte einen Wert eingeben.")
                continue
            return value
        raise CliError("Zu viele ungültige Eingaben — abgebrochen.", EXIT_USAGE)

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
                self.err("Bitte eine ganze Zahl eingeben.")
                continue
            if not minimum <= value <= maximum:
                self.err(f"Bitte einen Wert zwischen {minimum} und {maximum} eingeben.")
                continue
            return value
        raise CliError("Zu viele ungültige Eingaben — abgebrochen.", EXIT_USAGE)

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
        hint = "J/n" if default else "j/N"
        answer = self._readline(f"{prompt} [{hint}]: ").lower()
        if not answer:
            return default
        return answer in {"j", "ja", "y", "yes"}

    def choose(self, prompt: str, options: Sequence[str], *, default_index: int = 0) -> int:
        """Lässt aus einer nummerierten Liste wählen und liefert den Index."""
        if not options:
            raise CliError("Keine Auswahlmöglichkeiten vorhanden.", EXIT_ERROR)
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
                self.err("Bitte die Nummer der gewünschten Zeile eingeben.")
                continue
            if 1 <= number <= len(options):
                return number - 1
            self.err(f"Bitte eine Nummer zwischen 1 und {len(options)} eingeben.")
        raise CliError("Zu viele ungültige Eingaben — abgebrochen.", EXIT_USAGE)


def _isatty(stream: TextIO) -> bool:
    """True, wenn der Strom ein Terminal ist (defensiv gegen Attrappen ohne `isatty`)."""
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - nur für exotische Ströme
        return False


def _safe_name(value: str, *, max_chars: int = 80) -> str:
    """Reduziert einen vom Server gelieferten Namen auf eine Zeichen-Allowlist (ADR-055)."""
    cleaned = _SAFE_NAME_RE.sub("·", value.replace("\n", " ").replace("\r", " ")).strip()
    return cleaned[:max_chars] if cleaned else "(namenlos)"


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
    "messenger.telegram": ("token", "chat_id"),
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
    ),
}

#: Reihenfolge der Unter-Sektionen einer Sektion.
_TABLE_ORDER: dict[str, tuple[str, ...]] = {
    "llm": ("critic",),
    "messenger": ("telegram", "discord", "signal"),
}

#: Erklärender Kommentar über jeder Sektion.
_SECTION_COMMENTS: dict[str, str] = {
    "general": "Sprache, Länge, Zustellschwelle und Betrieb",
    "imap": "Mirror-Postfach (nur IMAPS) — `maildigest connect-mail`",
    "llm": "LLM-Provider — `maildigest connect-llm`",
    "llm.critic": "optionaler Override für den Kritiker; leere Felder erben von [llm]",
    "summarizer": "Custom-Instructions: was ist wichtig, worauf achten",
    "links": "Links werden immer entfernt; hier nur die defangte Fußnote",
    "messenger": "Zustellung — `maildigest connect-messenger`",
    "limits": "Ressourcengrenzen (Defaults siehe docs/SPEC-CLI.md)",
}

#: Kommentar hinter einzelnen Schlüsseln.
_KEY_COMMENTS: dict[str, str] = {
    "general.summary_length": "short | medium | long",
    "general.deliver_min_importance": "low | normal | high",
    "general.low_digest_time": "tägliche Sammelzustellung, lokale Uhrzeit",
    "general.state_db": "leer = state.db neben dieser Datei",
    "general.log_level": "DEBUG | INFO | WARNING | ERROR",
    "imap.port": "993 = IMAPS; Port 143 wird abgelehnt",
    "imap.move_processed_to": "leer = nur als gelesen markieren",
    "llm.provider": "anthropic | openai_compatible",
    "llm.base_url": "nur für openai_compatible / lokale Server",
    "messenger.active": "telegram | discord | signal",
}

#: Auskommentierte Platzhalter für Felder ohne Default (Pflichtfelder) und für Secrets.
_PLACEHOLDERS: dict[str, tuple[tuple[str, str], ...]] = {
    "imap": (
        ("host", '"imap.example.org"'),
        ("username", '"mirror@example.org"'),
        ("password", f'"…"   # oder Umgebungsvariable {ENV_IMAP_PASSWORD}'),
    ),
    "llm": (
        ("model", '"…"   # Pflichtfeld, es gibt bewusst keinen Default'),
        ("api_key", f'"…"   # oder Umgebungsvariable {ENV_LLM_API_KEY}'),
    ),
    # Der Kritiker erbt alles von `[llm]`; die Datei zeigt trotzdem den vollständigen
    # Feldsatz aus SPEC-CLI.md §5, damit ein Override nicht nachgeschlagen werden muss.
    "llm.critic": (
        ("provider", '"openai_compatible"   # leer/fehlend = erbt von [llm]'),
        ("model", '"…"   # leer/fehlend = erbt von [llm]'),
        ("base_url", '"http://localhost:11434/v1"   # leer/fehlend = erbt von [llm]'),
        ("max_tokens", "1024   # leer/fehlend = erbt von [llm]"),
    ),
    "messenger.telegram": (
        ("token", f'"…"   # oder Umgebungsvariable {ENV_TELEGRAM_TOKEN}'),
    ),
    "messenger.discord": (("webhook_url", '"https://discord.com/api/webhooks/…"'),),
}

#: Kopf der erzeugten Datei.
_FILE_HEADER = (
    "# MailDigest — Konfiguration",
    "# Erzeugt von `maildigest init`. Dateirechte: 0600 (enthält ggf. Secrets).",
    "# Vollständige Feldreferenz: docs/SPEC-CLI.md",
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
        f"Wert vom Typ {type(value).__name__} kann nicht in die Konfiguration geschrieben "
        "werden. Bitte den Eintrag von Hand korrigieren."
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
                f"Konfigurationsdatei nicht gefunden: {path}\n"
                "Lege sie mit `maildigest init` an oder gib den Pfad mit --config an.",
                EXIT_ERROR,
            ) from exc
        except OSError as exc:
            raise CliError(
                f"Konfigurationsdatei {path} kann nicht gelesen werden: {exc.strerror}."
            ) from exc
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise CliError(
                f"Konfigurationsdatei {path} ist kein gültiges UTF-8-TOML: {exc}"
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
                    f"In {self.path} ist `{part}` kein Abschnitt. Bitte die Datei von Hand "
                    "korrigieren."
                )
            cursor = child
        return cursor

    def save(self) -> None:
        """Schreibt die Datei mit Dateirechten `0600` (I5, F-SEC-8).

        Der gerenderte Text wird vorher selbst geparst — eine unlesbare Datei würde jedes
        weitere Kommando blockieren.
        """
        text = render_toml(self.data)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - Schutz gegen Regression
            raise CliError(f"Interner Fehler beim Schreiben der Konfiguration: {exc}") from exc
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            # `O_CREAT` wirkt nur bei neuen Dateien — eine bestehende bekommt hier ihre
            # Rechte, falls sie zu offen war.
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise CliError(
                f"Konfigurationsdatei {self.path} kann nicht geschrieben werden: "
                f"{exc.strerror}."
            ) from exc


def _set_or_clear(section: dict[str, Any], key: str, value: str) -> None:
    """Setzt einen Wert oder entfernt den Schlüssel, wenn der Wert leer ist."""
    if value:
        section[key] = value
    else:
        section.pop(key, None)


# --- Injizierbare Bausteine -----------------------------------------------------------------


@dataclass
class Hooks:
    """Die Außenwelt der CLI, gebündelt und ersetzbar (Tests reichen Attrappen herein)."""

    build_runner: Callable[..., Runner] = build_runner
    build_messenger: Callable[..., Messenger] = build_messenger_from_section
    build_provider: Callable[..., Any] = build_provider_from_settings
    build_summarizer: Callable[[Config], Any] = SummarizerAgent.from_config
    build_critic: Callable[[Config], Any] = CriticAgent.from_config
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
            f"{path} existiert bereits. Mit --force überschreiben (der bisherige Inhalt "
            "geht dabei verloren) oder einen anderen Pfad mit --config wählen.",
            EXIT_ERROR,
        )

    console.out(f"MailDigest einrichten — Konfiguration: {path}")
    language = args.language or console.ask(
        "Sprache der Zusammenfassungen", default="de", allowed=_LANGUAGES, flag="--language"
    )
    summary_length = args.summary_length or console.ask(
        "Länge der Zusammenfassungen",
        default="medium",
        allowed=_SUMMARY_LENGTHS,
        flag="--summary-length",
    )
    min_importance = args.min_importance or console.ask(
        "Einzeln zustellen ab Wichtigkeit",
        default="normal",
        allowed=_IMPORTANCES,
        flag="--min-importance",
    )
    digest_time = args.low_digest_time or console.ask(
        "Uhrzeit des täglichen Sammel-Digests (HH:MM)",
        default="18:00",
        flag="--low-digest-time",
    )
    if args.instructions is not None:
        instructions = args.instructions
    else:
        if console.interactive:
            # Nur als Erläuterung der Frage — ohne Frage keine Erläuterung (CT-3).
            console.out(
                "Custom-Instructions: eine Zeile dazu, was für dich wichtig ist "
                "(leer lassen = keine)."
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
        "llm": {"provider": "anthropic", "base_url": "", "max_tokens": 1024, "critic": {}},
        "summarizer": {"instructions": instructions},
        "links": {"footnote": False},
        "messenger": {
            "active": "telegram",
            "telegram": {"chat_id": ""},
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
        },
    }
    # Die Werte aus den Abfragen müssen das Schema erfüllen, bevor die Datei entsteht.
    _validate(GeneralConfig, data["general"], f"[general] in {path}")

    config_file = ConfigFile(path=path, data=data)
    config_file.save()
    console.out(f"Konfiguration angelegt: {path} (Dateirechte 0600)")
    console.out("")
    console.out("Nächste Schritte:")
    console.out("  1) maildigest connect-mail        (Mirror-Postfach)")
    console.out("  2) maildigest connect-llm         (Sprachmodell)")
    console.out("  3) maildigest connect-messenger   (Telegram/Discord/Signal)")
    console.out("  4) maildigest test                (Selbsttest)")
    console.out("  5) maildigest run                 (Dauerbetrieb)")
    return EXIT_OK


# --- Kommando: connect-mail ------------------------------------------------------------------

_FORWARDING_GUIDE = """
So richtest du die Weiterleitung in deinem echten Postfach ein
--------------------------------------------------------------
MailDigest liest NIE dein echtes Postfach. Es liest nur das Mirror-Postfach, in das
du deine Mails weiterleitest.

Gmail:       Einstellungen > "Weiterleitung und POP/IMAP" > "Weiterleitungsadresse
             hinzufügen" > Adresse des Mirror-Postfachs > Bestätigungscode aus der
             dort eintreffenden Mail eintragen > "Eingehende Nachrichten weiterleiten"
             aktivieren und "Gmail-Kopie im Posteingang behalten" wählen.
posteo:      Einstellungen > "E-Mail" > "Filterregeln" > neue Regel > Aktion
             "Weiterleiten an" + "Nachricht zusätzlich im Postfach behalten".
mailbox.org: Einstellungen > "E-Mail" > "Filter" > neue Regel > "Umleiten nach"
             plus Aktion "Behalten".
sonst:       Gesucht ist eine serverseitige Weiterleitung oder Filterregel. Eine
             Regel im Mailprogramm (Outlook/Thunderbird) reicht nicht — die greift
             nur, wenn dein Rechner läuft.

Wichtig: Keine Weiterleitung vom Mirror-Postfach zurück ins Hauptpostfach — das
ergibt eine Schleife.
""".strip()


def cmd_connect_mail(ctx: Context) -> int:
    """Fragt die IMAP-Zugangsdaten ab, testet sie und wählt den Ordner (F-ING-3)."""
    console, args = ctx.console, ctx.args
    config_file = ConfigFile.load(ctx.config_path)
    imap = config_file.section("imap")

    console.out("Mirror-Postfach verbinden (nur IMAPS, Zertifikatsprüfung immer aktiv)")
    host = args.host or console.ask(
        "IMAP-Host", default=str(imap.get("host", "")), flag="--host", required=True
    )
    port = (
        args.port
        if args.port is not None
        else console.ask_int("Port", default=int(imap.get("port", 993)))
    )
    username = args.username or console.ask(
        "Benutzername",
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
            "Port 143 ist Klartext-IMAP und wird nicht unterstützt. MailDigest verbindet "
            "ausschließlich per IMAPS (üblich: Port 993).",
            EXIT_ERROR,
        )

    env_password = os.environ.get(ENV_IMAP_PASSWORD, "")
    if env_password:
        console.out(f"Passwort: aus {ENV_IMAP_PASSWORD} (wird nicht in die Datei geschrieben)")
        imap.pop("password", None)
        password = env_password
    else:
        entered = console.ask_secret(
            f"Passwort (leer lassen, wenn {ENV_IMAP_PASSWORD} gesetzt werden soll)"
        )
        if entered:
            imap["password"] = entered
        password = entered or str(imap.get("password", ""))
        if not password:
            raise CliError(
                "Kein IMAP-Passwort angegeben. Entweder hier eingeben oder die "
                f"Umgebungsvariable {ENV_IMAP_PASSWORD} setzen.",
                EXIT_USAGE,
            )

    probe = dict(imap)
    probe["password"] = password
    section = _validate(ImapConfig, probe, f"[imap] in {ctx.config_path}")

    if args.no_test:
        console.out("Verbindungstest übersprungen (--no-test).")
    else:
        folder = _test_imap_and_choose_folder(ctx, section)
        imap["folder"] = folder

    config_file.save()
    console.out(f"Gespeichert in {config_file.path} (Dateirechte 0600).")
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
    console.out("Verbinde …")
    try:
        client = ctx.hooks.imap_client(section)
        client.connect()
    except IngestError as exc:
        raise CliError(
            f"{exc}\nZugangsdaten prüfen; mit --no-test lassen sich die Angaben auch "
            "ungetestet speichern.",
            EXIT_ERROR,
        ) from exc
    try:
        console.step("Verbindung steht.")
        try:
            folders = client.list_folders()
        except IngestError as exc:
            console.err(f"Ordnerliste nicht abrufbar ({exc}); es bleibt bei der Vorgabe.")
            return section.folder
    finally:
        client.disconnect()

    if not folders:
        return section.folder
    names = [_safe_name(name) for name in folders]
    default_index = folders.index(section.folder) if section.folder in folders else 0
    console.out("Welchen Ordner soll MailDigest lesen?")
    index = console.choose("Ordner", names, default_index=default_index)
    return folders[index]


# --- Kommando: connect-llm --------------------------------------------------------------------


def cmd_connect_llm(ctx: Context) -> int:
    """Wählt Provider und Modell, speichert den Key und macht einen Testaufruf (F-LLM-2)."""
    console, args = ctx.console, ctx.args
    config_file = ConfigFile.load(ctx.config_path)
    llm = config_file.section("llm")

    console.out("Sprachmodell verbinden")
    provider = args.provider or console.ask(
        "Provider",
        default=str(llm.get("provider", "anthropic")),
        allowed=_PROVIDERS,
        flag="--provider",
    )
    llm["provider"] = provider

    model = args.model or console.ask(
        "Modellname (exakte Modell-ID des Anbieters)",
        default=str(llm.get("model", "")),
        flag="--model",
        required=True,
    )
    llm["model"] = model

    if provider == "openai_compatible":
        base_url = args.base_url if args.base_url is not None else console.ask(
            "Basis-URL des Endpunkts (z. B. http://localhost:11434/v1)",
            default=str(llm.get("base_url", "")),
            flag="--base-url",
        )
        llm["base_url"] = base_url
        if base_url and not base_url.startswith(("https://", "http://localhost", "http://127.")):
            console.err(
                "Hinweis: Diese Basis-URL ist unverschlüsselt und nicht lokal — "
                "Mail-Inhalte gingen im Klartext über das Netz."
            )
    elif args.base_url is not None:
        llm["base_url"] = args.base_url

    env_key = os.environ.get(ENV_LLM_API_KEY, "")
    if env_key:
        console.out(f"API-Key: aus {ENV_LLM_API_KEY} (wird nicht in die Datei geschrieben)")
        llm.pop("api_key", None)
        api_key = env_key
    else:
        entered = console.ask_secret(
            f"API-Key (leer lassen, wenn {ENV_LLM_API_KEY} gesetzt werden soll oder der "
            "Endpunkt keinen Key braucht)"
        )
        if entered:
            llm["api_key"] = entered
        api_key = entered or str(llm.get("api_key", ""))

    probe = dict(llm)
    if api_key:
        probe["api_key"] = api_key
    section = _validate(LlmConfig, probe, f"[llm] in {ctx.config_path}")

    if args.no_test:
        console.out("Testaufruf übersprungen (--no-test).")
    else:
        _test_llm(ctx, section)

    config_file.save()
    console.out(f"Gespeichert in {config_file.path} (Dateirechte 0600).")
    return EXIT_OK


def _test_llm(ctx: Context, section: LlmConfig) -> None:
    """Schickt einen minimalen Testaufruf an den Provider.

    Die Modellantwort ist untrusted (I4) und wird deshalb **nicht** ausgegeben — gemeldet
    werden nur Länge und ob das erwartete Wort vorkommt.

    Raises:
        CliError: Provider nicht erreichbar, Key abgelehnt oder leere Antwort.
    """
    console = ctx.console
    console.out("Testaufruf …")
    try:
        provider = ctx.hooks.build_provider(
            provider=section.provider,
            model=section.model,
            base_url=section.base_url,
            api_key=section.api_key,
        )
        answer = provider.complete(
            "Du beantwortest einen Verbindungstest.",
            "Antworte ausschliesslich mit dem Wort OK.",
            max_tokens=16,
        )
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc
    except LLMError as exc:
        raise CliError(
            f"Testaufruf fehlgeschlagen ({type(exc).__name__}): {exc}\n"
            "Modellname, API-Key und Basis-URL prüfen.",
            EXIT_ERROR,
        ) from exc
    if not answer.strip():
        raise CliError("Der Provider hat eine leere Antwort geliefert.", EXIT_ERROR)
    marker = "erwartete Antwort" if "OK" in answer.upper() else "unerwartete Antwort"
    console.step(f"Antwort erhalten ({len(answer)} Zeichen, {marker}).")


# --- Kommando: connect-messenger ---------------------------------------------------------------


def cmd_connect_messenger(ctx: Context) -> int:
    """Richtet den Zustellweg ein und schickt eine Testnachricht (F-MSG-2)."""
    console, args = ctx.console, ctx.args
    config_file = ConfigFile.load(ctx.config_path)
    messenger_section = config_file.section("messenger")

    console.out("Messenger verbinden")
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
        console.out("Testnachricht übersprungen (--no-test).")
    else:
        _send_test_message(ctx, section)

    config_file.save()
    console.out(f"Gespeichert in {config_file.path} (Dateirechte 0600).")
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
        console.out(f"Bot-Token: aus {ENV_TELEGRAM_TOKEN} (wird nicht in die Datei geschrieben)")
        telegram.pop("token", None)
        token = env_token
    else:
        console.out(
            "Bot anlegen: In Telegram @BotFather anschreiben, /newbot senden, Namen "
            "vergeben — BotFather antwortet mit dem Token."
        )
        entered = console.ask_secret(
            f"Bot-Token (leer lassen, wenn {ENV_TELEGRAM_TOKEN} gesetzt werden soll)"
        )
        if entered:
            telegram["token"] = entered
        token = entered or str(telegram.get("token", ""))
    if not token:
        raise CliError(
            "Kein Bot-Token angegeben. Entweder hier eingeben oder die Umgebungsvariable "
            f"{ENV_TELEGRAM_TOKEN} setzen.",
            EXIT_USAGE,
        )

    if args.chat_id:
        telegram["chat_id"] = args.chat_id
        return
    telegram["chat_id"] = _discover_chat_id(ctx, SecretStr(token), telegram)


def _discover_chat_id(
    ctx: Context, token: SecretStr, telegram: dict[str, Any]
) -> str:
    """Wartet auf eine Nachricht an den Bot und liest die Chat-ID daraus (F-MSG-2)."""
    console = ctx.console
    console.out("Schreib deinem Bot jetzt in Telegram eine Nachricht (z. B. /start).")
    attempts = _CHAT_ID_ATTEMPTS if console.interactive else 1
    candidates: list[ChatCandidate] = []
    for attempt in range(1, attempts + 1):
        try:
            candidates = ctx.hooks.discover_chat_ids(token=token)
        except MessengerError as exc:
            raise CliError(
                f"Telegram-Abfrage fehlgeschlagen: {exc}\nIst das Bot-Token richtig?",
                EXIT_ERROR,
            ) from exc
        if candidates:
            break
        if attempt < attempts:
            console.step(f"Noch keine Nachricht empfangen — warte ({attempt}/{attempts}) …")
            ctx.hooks.sleep(_CHAT_ID_WAIT_SECONDS)

    if not candidates:
        existing = str(telegram.get("chat_id", ""))
        if existing:
            console.err("Keine neue Nachricht gefunden — die bisherige Chat-ID bleibt stehen.")
            return existing
        raise CliError(
            "Keine Nachricht an den Bot gefunden. Schreib dem Bot eine Nachricht und "
            "starte `maildigest connect-messenger` erneut — oder gib die Chat-ID mit "
            "--chat-id an.",
            EXIT_ERROR,
        )
    if len(candidates) == 1:
        console.step(f"Chat-ID gefunden: {candidates[0].chat_id}")
        return candidates[0].chat_id
    console.out("Mehrere Chats gefunden — welcher soll es sein?")
    labels = [f"{item.chat_id} ({item.chat_type})" for item in candidates]
    return candidates[console.choose("Chat", labels)].chat_id


def _setup_discord(ctx: Context, config_file: ConfigFile) -> None:
    """Webhook-URL abfragen (sie ist selbst das Secret)."""
    console, args = ctx.console, ctx.args
    discord = config_file.section("messenger.discord")
    console.out(
        "Webhook anlegen: Discord > Kanal > Bearbeiten > Integrationen > Webhooks > "
        "Neuer Webhook > Webhook-URL kopieren."
    )
    url = args.webhook_url or console.ask_secret("Webhook-URL")
    if url:
        discord["webhook_url"] = url
    if not str(discord.get("webhook_url", "")):
        raise CliError(
            "Keine Webhook-URL angegeben. Die URL ist zugleich das Secret; sie wird in "
            "der Konfigurationsdatei mit Dateirechten 0600 abgelegt.",
            EXIT_USAGE,
        )


def _setup_signal(ctx: Context, config_file: ConfigFile) -> None:
    """signal-cli-Socket abfragen und den Adapter freischalten."""
    console, args = ctx.console, ctx.args
    signal_section = config_file.section("messenger.signal")
    console.out(
        "Voraussetzung: `signal-cli --daemon --socket <pfad>` läuft bereits und die "
        "Nummer ist dort registriert. Zugestellt wird an „Notiz an mich“."
    )
    socket_path = args.signal_socket or console.ask(
        "Pfad des signal-cli-Sockets",
        default=str(signal_section.get("signal_cli_socket", "")),
        flag="--signal-socket",
        required=True,
    )
    signal_section["signal_cli_socket"] = socket_path
    signal_section["enabled"] = True


def _send_test_message(ctx: Context, section: MessengerConfig) -> None:
    """Healthcheck + eine im Code formulierte Testnachricht (I3: über `compose_plain`)."""
    console = ctx.console
    console.out("Testnachricht senden …")
    try:
        messenger = ctx.hooks.build_messenger(section)
    except ConfigError as exc:
        raise CliError(str(exc), EXIT_ERROR) from exc
    if not messenger.healthcheck():
        raise CliError(
            f"Der Dienst „{section.active}“ ist nicht erreichbar oder die Zugangsdaten "
            "werden abgelehnt.",
            EXIT_ERROR,
        )
    composer = DigestComposer(part_limit=part_limit_for(section.active))
    try:
        messenger.send(composer.compose_plain(_TEST_MESSAGE))
    except MessengerError as exc:
        raise CliError(f"Testnachricht konnte nicht zugestellt werden: {exc}", EXIT_ERROR) from exc
    console.step("Testnachricht zugestellt — schau in deinen Messenger.")


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


def cmd_test(ctx: Context) -> int:
    """Ende-zu-Ende-Selbsttest mit einer `.eml`-Datei statt aus dem Postfach (F-OPS-2)."""
    console, args = ctx.console, ctx.args
    config = _load_full_config(ctx.config_path)
    console.out(f"1/5 Konfiguration geladen: {ctx.config_path}")

    raw_bytes, source = _read_test_mail(args.eml)
    console.out(f"2/5 Testmail gelesen: {source} ({len(raw_bytes)} Bytes)")
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
        console.out("    Trockenlauf: es wird nichts an den Messenger geschickt (--dry-run).")

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
            console.out("3/5 Pipeline läuft (Sanitizer → Summarizer → Kritiker → Zustellung) …")
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
        console.out("4/5 Sanitizer: fehlgeschlagen.")
        return
    report = mail.sanitization_report
    processed = sum(1 for item in mail.attachments if item.processed)
    console.out(
        f"4/5 Sanitizer: {_count(len(mail.body_text), 'Zeichen', 'Zeichen')} Klartext, "
        f"{_count(len(mail.attachments), 'Anhang', 'Anhänge')} ({processed} verarbeitet), "
        f"{_count(report.links_removed, 'Link', 'Links')} entfernt, "
        f"{_count(report.control_chars_removed, 'Steuerzeichen', 'Steuerzeichen')} entfernt"
    )
    summary = summarizer.result
    if summary is None:
        console.step("Summarizer: fehlgeschlagen.")
        return
    console.step(
        f"Summarizer: Wichtigkeit={summary.importance}, "
        f"Injection-Verdacht={'ja' if summary.injection_suspected else 'nein'}"
    )
    verdict = critic.result
    if verdict is None:
        console.step("Kritiker: fehlgeschlagen.")
        return
    console.step(
        f"Kritiker: Phishing-Risiko={verdict.phishing_risk}, "
        f"Zusammenfassung korrekt={'ja' if verdict.summary_accurate else 'nein'}"
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
            f"5/5 Fail-closed: Stufe {result.notice.stage}, Grund {result.notice.reason_class}."
        )
        if collector is not None:
            # Trockenlauf: Es ging nichts an den Messenger — und die Notiz ist genau
            # das, was der Nutzer hier sehen will (SPEC-CLI.md §4 `test --dry-run`).
            console.out("    Metadaten-Notiz erzeugt — Trockenlauf, nicht gesendet:")
            console.out("")
            for message in collector.messages:
                for part in message.parts:
                    console.out(part)
            console.out("")
            console.err(
                "Selbsttest fehlgeschlagen — es wurde nur die Metadaten-Notiz erzeugt "
                "(zugestellt: nein — Trockenlauf)."
            )
            return EXIT_ERROR
        # `notice_delivered` sagt nur, dass die Warteschlange die Notiz angenommen hat.
        # Zugestellt ist sie erst, wenn danach nichts mehr wartet (CT-4).
        delivered = result.notice_delivered and not pending
        console.err(
            "Selbsttest fehlgeschlagen — es wurde nur die Metadaten-Notiz erzeugt "
            f"(zugestellt: {'ja' if delivered else 'nein'})."
        )
        return EXIT_ERROR
    if isinstance(result, QueuedLow):  # pragma: no cover - Schwelle ist auf `low` gesetzt
        console.err("Selbsttest: Mail landete im Sammel-Digest statt in einer Nachricht.")
        return EXIT_ERROR

    parts = result.message.parts
    if collector is not None:
        console.out(f"5/5 Nachricht erzeugt ({_parts_label(parts)}) — Trockenlauf, nicht gesendet:")
        console.out("")
        for part in parts:
            console.out(part)
        console.out("")
        return EXIT_OK
    if pending:
        console.err(
            "Selbsttest: Die Nachricht wurde erzeugt, aber nicht zugestellt — sie liegt in "
            "der Warteschlange. Messenger-Zugangsdaten prüfen "
            "(`maildigest connect-messenger`)."
        )
        return EXIT_ERROR
    console.out(f"5/5 Zugestellt ({_parts_label(parts)}). Schau in deinen Messenger.")
    return EXIT_OK


def _parts_label(parts: Sequence[str]) -> str:
    """`1 Teil` / `3 Teile` — die Nachricht wird auf das Messenger-Limit gesplittet."""
    return "1 Teil" if len(parts) == 1 else f"{len(parts)} Teile"


def _count(number: int, singular: str, plural: str) -> str:
    """`1 Anhang` / `2 Anhänge` — deutsche Zählform für die Zeile 4/5 (SPEC-CLI §4)."""
    return f"{number} {singular}" if number == 1 else f"{number} {plural}"


def _read_test_mail(eml: str | None) -> tuple[bytes, str]:
    """Liest die Testmail: eigene Datei oder die mitgelieferte Beispielmail."""
    if eml is None:
        data = resources.files("maildigest").joinpath("data/selftest.eml").read_bytes()
        return data, "mitgelieferte Beispielmail"
    path = Path(eml)
    try:
        return path.read_bytes(), str(path)
    except OSError as exc:
        raise CliError(
            f"Testmail {path} kann nicht gelesen werden: {exc.strerror}.", EXIT_ERROR
        ) from exc


def _build_raw_mail(raw_bytes: bytes) -> RawMail:
    """Baut aus `.eml`-Bytes dasselbe :class:`RawMail`, das der Ingest liefern würde."""
    try:
        return build_raw_mail(MailMessage.from_bytes(raw_bytes))
    except Exception as exc:  # jede Parser-Panne ist hier ein Bedienfehler, kein Absturz
        raise CliError(
            f"Die Datei ist keine lesbare E-Mail ({type(exc).__name__}). Erwartet wird eine "
            "RFC-822-Datei (`.eml`) mit Headern und Body.",
            EXIT_USAGE,
        ) from exc


# --- Kommando: run --------------------------------------------------------------------------


def cmd_run(ctx: Context) -> int:
    """Startet den Daemon bzw. einen Einzellauf (F-OPS-1)."""
    console, args = ctx.console, ctx.args
    config = _load_full_config(ctx.config_path)
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
                raise CliError(f"Postfach nicht erreichbar: {exc}", EXIT_ERROR) from exc
            console.err(
                f"Lauf beendet: {stats.ingest.fetched} Mails geholt, "
                f"{stats.ingest.processed} verarbeitet, {stats.ingest.duplicates} Duplikate, "
                f"{stats.ingest.failed} Fehler, {stats.delivery.delivered} Nachrichten "
                f"zugestellt, {runner.outbox.pending} in der Warteschlange."
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
            f"'{raw}' ist keine ganze Zahl (erlaubt: {_PORT_MIN} bis {_PORT_MAX})"
        ) from None
    if not _PORT_MIN <= value <= _PORT_MAX:
        raise argparse.ArgumentTypeError(
            f"{value} liegt außerhalb des erlaubten Bereichs {_PORT_MIN} bis {_PORT_MAX}"
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
        metavar="PFAD",
        default=argparse.SUPPRESS,
        help="Pfad der Konfigurationsdatei",
    )
    common.add_argument(
        "--non-interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Keine Rückfragen stellen; Defaults und Optionen verwenden",
    )

    parser = _ArgumentParser(
        prog="maildigest",
        description=(
            "Fasst Mails aus einem Mirror-Postfach zusammen, prüft sie auf Phishing und "
            "schickt reinen Text an einen Messenger."
        ),
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", metavar="KOMMANDO")

    init = subparsers.add_parser(
        "init", parents=[common], help="Konfigurationsdatei anlegen (0600)"
    )
    init.add_argument("--force", action="store_true", help="Vorhandene Datei überschreiben")
    init.add_argument("--language", choices=list(_LANGUAGES), help="Sprache der Zusammenfassungen")
    init.add_argument(
        "--summary-length", choices=list(_SUMMARY_LENGTHS), help="Länge der Zusammenfassungen"
    )
    init.add_argument(
        "--min-importance", choices=list(_IMPORTANCES), help="Schwelle für Einzelzustellung"
    )
    init.add_argument("--low-digest-time", metavar="HH:MM", help="Uhrzeit des Sammel-Digests")
    init.add_argument("--instructions", metavar="TEXT", help="Custom-Instructions")
    init.set_defaults(func=cmd_init)

    mail = subparsers.add_parser(
        "connect-mail", parents=[common], help="Mirror-Postfach verbinden und testen"
    )
    mail.add_argument("--host", metavar="HOST", help="IMAP-Host")
    mail.add_argument(
        "--port", type=_port_value, metavar="PORT", help="IMAP-Port (Default 993)"
    )
    mail.add_argument("--username", metavar="NAME", help="IMAP-Benutzername")
    mail.add_argument("--folder", metavar="ORDNER", help="Zu lesender Ordner")
    mail.add_argument(
        "--move-processed-to", metavar="ORDNER", help="Verarbeitete Mails dorthin verschieben"
    )
    mail.add_argument("--no-test", action="store_true", help="Ohne Verbindungstest speichern")
    mail.set_defaults(func=cmd_connect_mail)

    llm = subparsers.add_parser(
        "connect-llm", parents=[common], help="Sprachmodell verbinden und testen"
    )
    llm.add_argument("--provider", choices=list(_PROVIDERS), help="Provider")
    llm.add_argument("--model", metavar="ID", help="Modell-ID des Anbieters")
    llm.add_argument("--base-url", metavar="URL", help="Endpunkt für openai_compatible")
    llm.add_argument("--no-test", action="store_true", help="Ohne Testaufruf speichern")
    llm.set_defaults(func=cmd_connect_llm)

    messenger = subparsers.add_parser(
        "connect-messenger", parents=[common], help="Messenger verbinden und testen"
    )
    messenger.add_argument("--messenger", choices=list(_MESSENGERS), help="Zielsystem")
    messenger.add_argument("--chat-id", metavar="ID", help="Telegram-Chat-ID (statt getUpdates)")
    messenger.add_argument("--webhook-url", metavar="URL", help="Discord-Webhook-URL")
    messenger.add_argument("--signal-socket", metavar="PFAD", help="Pfad des signal-cli-Sockets")
    messenger.add_argument("--no-test", action="store_true", help="Ohne Testnachricht speichern")
    messenger.set_defaults(func=cmd_connect_messenger)

    test = subparsers.add_parser(
        "test", parents=[common], help="Ende-zu-Ende-Selbsttest mit einer Beispielmail"
    )
    test.add_argument("--eml", metavar="PFAD", help="Eigene .eml-Datei statt der Beispielmail")
    test.add_argument(
        "--dry-run", action="store_true", help="Nachricht nur anzeigen, nicht zustellen"
    )
    test.set_defaults(func=cmd_test)

    run = subparsers.add_parser("run", parents=[common], help="Dauerbetrieb (Polling)")
    run.add_argument("--once", action="store_true", help="Einmal verarbeiten und beenden")
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
        streams_err.write(f"Fehler: {exc}\n")
        return exc.code
    except KeyboardInterrupt:
        streams_err.write("Abgebrochen.\n")
        return EXIT_ERROR
    except (ConfigError, StateError, IngestError, MessengerError, LLMError) as exc:
        # Sicherheitsnetz: Eine hier durchgerutschte Ausnahme darf keinen Traceback mit
        # möglichen Inhalten auf das Terminal schreiben (I5).
        streams_err.write(f"Fehler: {exc}\n")
        return EXIT_ERROR
    except httpx.HTTPError as exc:
        streams_err.write(f"Fehler: Netzwerkproblem ({type(exc).__name__}).\n")
        return EXIT_ERROR


def run_cli() -> None:  # pragma: no cover - dünner Wrapper für den Konsolen-Einstiegspunkt
    """Konsolen-Einstiegspunkt (`[project.scripts] maildigest`)."""
    sys.exit(main())
