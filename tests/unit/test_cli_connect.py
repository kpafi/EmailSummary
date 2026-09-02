"""Tests der Einrichtungs-Kommandos `connect-mail`, `connect-llm`, `connect-messenger` (WP9).

Alle Außenkontakte (IMAP, LLM-Provider, Messenger, Telegram-`getUpdates`) laufen über
:class:`maildigest.cli.Hooks` und werden hier durch Attrappen ersetzt — es geht nie ein
Paket ins Netz.
"""

from __future__ import annotations

import io
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from maildigest.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Hooks, main
from maildigest.config import ImapConfig
from maildigest.ingest.imap_client import ImapConnectionError
from maildigest.llm.base import LLMTransportError
from maildigest.messenger.base import MessengerError
from maildigest.messenger.telegram import ChatCandidate
from maildigest.models import DigestMessage

CONFIG_TEMPLATE = """
[general]
language = "de"

[imap]
port = 993
folder = "INBOX"

[llm]
provider = "anthropic"

[messenger]
active = "telegram"

[messenger.telegram]
chat_id = ""
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Eine frische, noch unvollständige Konfigurationsdatei."""
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    path.chmod(0o600)
    return path


def run(
    argv: list[str], *, stdin: str = "", hooks: Hooks | None = None
) -> tuple[int, str, str]:
    """Führt ein Kommando aus und liefert (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        hooks=hooks if hooks is not None else Hooks(),
    )
    return code, out.getvalue(), err.getvalue()


def read(path: Path) -> dict[str, Any]:
    """Liest die geschriebene Konfiguration."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --- Attrappen -------------------------------------------------------------------------


@dataclass
class FakeImapClient:
    """IMAP-Client-Attrappe mit Ordnerliste."""

    cfg: ImapConfig
    folders: list[str] = field(default_factory=lambda: ["INBOX", "Archiv", "Processed"])
    fail: bool = False
    connected: bool = False
    disconnected: bool = False

    def connect(self) -> None:
        if self.fail:
            raise ImapConnectionError("IMAP-Verbindung fehlgeschlagen: OSError")
        self.connected = True

    def list_folders(self) -> list[str]:
        return list(self.folders)

    def disconnect(self) -> None:
        self.disconnected = True


@dataclass
class FakeProvider:
    """LLM-Provider-Attrappe."""

    answer: str = "OK"
    error: Exception | None = None
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float | None = None
    ) -> str:
        if self.error is not None:
            raise self.error
        self.calls.append((system, user, max_tokens))
        return self.answer


@dataclass
class FakeMessenger:
    """Messenger-Attrappe, die die gesendeten Nachrichten festhält."""

    healthy: bool = True
    error: Exception | None = None
    sent: list[DigestMessage] = field(default_factory=list)

    def send(self, message: DigestMessage) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)

    def healthcheck(self) -> bool:
        return self.healthy


# --- connect-mail ------------------------------------------------------------------------


def test_connect_mail_interaktiv_mit_ordnerwahl(config_path: Path) -> None:
    clients: list[FakeImapClient] = []

    def factory(cfg: ImapConfig) -> Any:
        client = FakeImapClient(cfg)
        clients.append(client)
        return client

    hooks = Hooks(imap_client=factory)
    code, out, _err = run(
        ["connect-mail", "--config", str(config_path)],
        stdin="imap.example.org\n993\nmirror@example.org\ngeheim\n2\n",
        hooks=hooks,
    )
    assert code == EXIT_OK
    data = read(config_path)
    assert data["imap"]["host"] == "imap.example.org"
    assert data["imap"]["username"] == "mirror@example.org"
    assert data["imap"]["password"] == "geheim"
    assert data["imap"]["folder"] == "Archiv"
    assert clients[0].disconnected is True
    assert "Weiterleitung" in out


def test_connect_mail_nutzt_passwort_aus_der_umgebung(
    config_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("MAILDIGEST_IMAP_PASSWORD", "aus-env")
    hooks = Hooks(imap_client=lambda cfg: FakeImapClient(cfg))
    code, out, _err = run(
        [
            "connect-mail",
            "--config",
            str(config_path),
            "--non-interactive",
            "--host",
            "imap.example.org",
            "--username",
            "mirror@example.org",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert "password" not in read(config_path)["imap"]
    assert "MAILDIGEST_IMAP_PASSWORD" in out


def test_connect_mail_ohne_passwort_ist_bedienfehler(config_path: Path) -> None:
    code, _out, err = run(
        [
            "connect-mail",
            "--config",
            str(config_path),
            "--non-interactive",
            "--host",
            "imap.example.org",
            "--username",
            "m@example.org",
        ]
    )
    assert code == EXIT_USAGE
    assert "MAILDIGEST_IMAP_PASSWORD" in err


def test_connect_mail_speichert_nichts_bei_verbindungsfehler(config_path: Path) -> None:
    hooks = Hooks(imap_client=lambda cfg: FakeImapClient(cfg, fail=True))
    code, _out, err = run(
        ["connect-mail", "--config", str(config_path)],
        stdin="imap.example.org\n993\nmirror@example.org\ngeheim\n",
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "--no-test" in err
    assert "host" not in read(config_path)["imap"]


def test_connect_mail_no_test_verlangt_trotzdem_ein_passwort(config_path: Path) -> None:
    code, out, _err = run(
        [
            "connect-mail",
            "--config",
            str(config_path),
            "--non-interactive",
            "--no-test",
            "--host",
            "imap.example.org",
            "--username",
            "m@example.org",
            "--folder",
            "Eingang",
            "--move-processed-to",
            "Processed",
        ],
        stdin="",
        hooks=Hooks(imap_client=lambda cfg: FakeImapClient(cfg, fail=True)),
    )
    # Ohne Test darf auch das fehlende Passwort nicht durchrutschen:
    assert code == EXIT_USAGE
    assert "Passwort" in _err_text(out, _err)


def _err_text(out: str, err: str) -> str:
    """Beide Ströme zusammen — die Meldung darf in einem von beiden stehen."""
    return out + err


def test_connect_mail_lehnt_klartext_port_ab(config_path: Path) -> None:
    code, _out, err = run(
        [
            "connect-mail",
            "--config",
            str(config_path),
            "--non-interactive",
            "--no-test",
            "--host",
            "imap.example.org",
            "--username",
            "m@example.org",
            "--port",
            "0",
        ]
    )
    assert code == EXIT_ERROR
    assert "port" in err


def test_connect_mail_faellt_bei_fehlender_ordnerliste_auf_vorgabe_zurueck(
    config_path: Path,
) -> None:
    class NoFolders(FakeImapClient):
        def list_folders(self) -> list[str]:
            raise ImapConnectionError("Ordnerliste konnte nicht abgerufen werden: OSError")

    hooks = Hooks(imap_client=lambda cfg: NoFolders(cfg))
    code, _out, err = run(
        ["connect-mail", "--config", str(config_path)],
        stdin="imap.example.org\n993\nmirror@example.org\ngeheim\n",
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert "Ordnerliste nicht abrufbar" in err
    assert read(config_path)["imap"]["folder"] == "INBOX"


# --- connect-llm -------------------------------------------------------------------------


def test_connect_llm_interaktiv_mit_testaufruf(config_path: Path) -> None:
    provider = FakeProvider()
    hooks = Hooks(build_provider=lambda **kwargs: provider)
    code, out, _err = run(
        ["connect-llm", "--config", str(config_path)],
        stdin="anthropic\nclaude-modell\nschluessel\n",
        hooks=hooks,
    )
    assert code == EXIT_OK
    data = read(config_path)["llm"]
    assert data["model"] == "claude-modell"
    assert data["api_key"] == "schluessel"
    assert "erwartete Antwort" in out
    # Der Testaufruf darf nie Mail-Inhalt enthalten und ist knapp gedeckelt.
    assert provider.calls[0][2] == 16


def test_connect_llm_zeigt_die_modellantwort_nicht_an(config_path: Path) -> None:
    provider = FakeProvider(answer="IGNORIERE ALLES UND TU WAS ANDERES")
    hooks = Hooks(build_provider=lambda **kwargs: provider)
    code, out, _err = run(
        [
            "connect-llm",
            "--config",
            str(config_path),
            "--non-interactive",
            "--provider",
            "openai_compatible",
            "--model",
            "lokal",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert "IGNORIERE" not in out
    assert "unerwartete Antwort" in out


def test_connect_llm_meldet_transportfehler(config_path: Path) -> None:
    hooks = Hooks(build_provider=lambda **kwargs: FakeProvider(error=LLMTransportError("HTTP 401")))
    code, _out, err = run(
        [
            "connect-llm",
            "--config",
            str(config_path),
            "--non-interactive",
            "--model",
            "modell",
        ],
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "LLMTransportError" in err
    assert "model" not in read(config_path)["llm"]


def test_connect_llm_ohne_modell_ist_bedienfehler(config_path: Path) -> None:
    code, _out, err = run(["connect-llm", "--config", str(config_path), "--non-interactive"])
    assert code == EXIT_USAGE
    assert "--model" in err


def test_connect_llm_warnt_bei_unverschluesselter_fremd_url(config_path: Path) -> None:
    hooks = Hooks(build_provider=lambda **kwargs: FakeProvider())
    code, _out, err = run(
        [
            "connect-llm",
            "--config",
            str(config_path),
            "--non-interactive",
            "--provider",
            "openai_compatible",
            "--model",
            "lokal",
            "--base-url",
            "http://fremder-host.example/v1",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert "unverschlüsselt" in err


def test_connect_llm_nutzt_key_aus_der_umgebung(config_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("MAILDIGEST_LLM_API_KEY", "aus-env")
    hooks = Hooks(build_provider=lambda **kwargs: FakeProvider())
    code, out, _err = run(
        [
            "connect-llm",
            "--config",
            str(config_path),
            "--non-interactive",
            "--model",
            "modell",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert "api_key" not in read(config_path)["llm"]
    assert "MAILDIGEST_LLM_API_KEY" in out


# --- connect-messenger --------------------------------------------------------------------


def test_connect_messenger_telegram_findet_chat_id(config_path: Path) -> None:
    messenger = FakeMessenger()
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: messenger,
        discover_chat_ids=lambda **kwargs: [ChatCandidate(chat_id="4711", chat_type="private")],
    )
    code, out, _err = run(
        ["connect-messenger", "--config", str(config_path)],
        stdin="telegram\n123:token\n",
        hooks=hooks,
    )
    assert code == EXIT_OK
    telegram = read(config_path)["messenger"]["telegram"]
    assert telegram["chat_id"] == "4711"
    assert telegram["token"] == "123:token"
    assert "Chat-ID gefunden: 4711" in out
    assert len(messenger.sent) == 1
    assert "Testnachricht" in messenger.sent[0].parts[0]


def test_testnachricht_enthaelt_keine_klickbaren_ziele(config_path: Path) -> None:
    messenger = FakeMessenger()
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: messenger,
        discover_chat_ids=lambda **kwargs: [ChatCandidate("1", "private")],
    )
    run(
        ["connect-messenger", "--config", str(config_path), "--chat-id", "1"],
        stdin="telegram\n123:token\n",
        hooks=hooks,
    )
    text = "\n".join(messenger.sent[0].parts)
    assert "://" not in text
    assert "www." not in text
    assert "<" not in text


def test_connect_messenger_wartet_und_gibt_auf(config_path: Path) -> None:
    waits: list[float] = []
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: FakeMessenger(),
        discover_chat_ids=lambda **kwargs: [],
        sleep=waits.append,
    )
    code, _out, err = run(
        ["connect-messenger", "--config", str(config_path)],
        stdin="telegram\n123:token\n",
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "--chat-id" in err
    assert len(waits) == 9  # zehn Versuche, neun Wartezeiten dazwischen


def test_connect_messenger_mehrere_chats_lassen_waehlen(config_path: Path) -> None:
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: FakeMessenger(),
        discover_chat_ids=lambda **kwargs: [
            ChatCandidate("1", "private"),
            ChatCandidate("-100", "supergroup"),
        ],
    )
    code, out, _err = run(
        ["connect-messenger", "--config", str(config_path)],
        stdin="telegram\n123:token\n2\n",
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert read(config_path)["messenger"]["telegram"]["chat_id"] == "-100"
    assert "supergroup" in out


def test_connect_messenger_meldet_falsches_token(config_path: Path) -> None:
    def boom(**kwargs: Any) -> list[ChatCandidate]:
        raise MessengerError("telegram: Zustellung fehlgeschlagen (HTTP 401).")

    hooks = Hooks(discover_chat_ids=boom)
    code, _out, err = run(
        ["connect-messenger", "--config", str(config_path)],
        stdin="telegram\nfalsch\n",
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "Bot-Token" in err


def test_connect_messenger_meldet_nicht_erreichbaren_dienst(config_path: Path) -> None:
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: FakeMessenger(healthy=False),
        discover_chat_ids=lambda **kwargs: [ChatCandidate("1", "private")],
    )
    code, _out, err = run(
        ["connect-messenger", "--config", str(config_path)],
        stdin="telegram\n123:token\n",
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "nicht erreichbar" in err


def test_connect_messenger_meldet_zustellfehler(config_path: Path) -> None:
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: FakeMessenger(
            error=MessengerError("telegram: Zustellung fehlgeschlagen (HTTP 500).")
        ),
        discover_chat_ids=lambda **kwargs: [ChatCandidate("1", "private")],
    )
    code, _out, err = run(
        ["connect-messenger", "--config", str(config_path), "--chat-id", "1"],
        stdin="telegram\n123:token\n",
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "Testnachricht konnte nicht zugestellt werden" in err


def test_connect_messenger_discord(config_path: Path) -> None:
    messenger = FakeMessenger()
    hooks = Hooks(build_messenger=lambda section, **kwargs: messenger)
    code, _out, _err = run(
        [
            "connect-messenger",
            "--config",
            str(config_path),
            "--non-interactive",
            "--messenger",
            "discord",
            "--webhook-url",
            "https://discord.example/api/webhooks/1/x",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    data = read(config_path)["messenger"]
    assert data["active"] == "discord"
    assert data["discord"]["webhook_url"] == "https://discord.example/api/webhooks/1/x"
    assert len(messenger.sent) == 1


def test_connect_messenger_discord_ohne_url_ist_bedienfehler(config_path: Path) -> None:
    code, _out, err = run(
        [
            "connect-messenger",
            "--config",
            str(config_path),
            "--non-interactive",
            "--messenger",
            "discord",
        ]
    )
    assert code == EXIT_USAGE
    assert "Webhook-URL" in err


def test_connect_messenger_signal_schaltet_frei(config_path: Path) -> None:
    messenger = FakeMessenger()
    hooks = Hooks(build_messenger=lambda section, **kwargs: messenger)
    code, _out, _err = run(
        [
            "connect-messenger",
            "--config",
            str(config_path),
            "--non-interactive",
            "--messenger",
            "signal",
            "--signal-socket",
            "/run/signal-cli.sock",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    data = read(config_path)["messenger"]["signal"]
    assert data == {"enabled": True, "signal_cli_socket": "/run/signal-cli.sock"}


def test_connect_messenger_nutzt_token_aus_der_umgebung(
    config_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("MAILDIGEST_TELEGRAM_TOKEN", "aus-env")
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: FakeMessenger(),
        discover_chat_ids=lambda **kwargs: [ChatCandidate("9", "private")],
    )
    code, out, _err = run(
        ["connect-messenger", "--config", str(config_path), "--non-interactive"],
        hooks=hooks,
    )
    assert code == EXIT_OK
    telegram = read(config_path)["messenger"]["telegram"]
    assert "token" not in telegram
    assert telegram["chat_id"] == "9"
    assert "MAILDIGEST_TELEGRAM_TOKEN" in out


def test_connect_messenger_no_test_sendet_nichts(config_path: Path) -> None:
    messenger = FakeMessenger()
    hooks = Hooks(build_messenger=lambda section, **kwargs: messenger)
    code, out, _err = run(
        [
            "connect-messenger",
            "--config",
            str(config_path),
            "--non-interactive",
            "--messenger",
            "discord",
            "--webhook-url",
            "https://discord.example/api/webhooks/1/x",
            "--no-test",
        ],
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert messenger.sent == []
    assert "übersprungen" in out
