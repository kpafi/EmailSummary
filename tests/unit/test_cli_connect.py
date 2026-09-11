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
    assert "forwarding" in out


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
    assert "password" in _err_text(out, _err).lower()


def _err_text(out: str, err: str) -> str:
    """Beide Ströme zusammen — die Meldung darf in einem von beiden stehen."""
    return out + err


def test_connect_mail_lehnt_klartext_port_ab(config_path: Path) -> None:
    """Port 143 ist Klartext-IMAP: Konfigurationsfehler, also Exit 1 (SPEC-CLI §4)."""
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
            "143",
        ]
    )
    assert code == EXIT_ERROR
    assert "143" in err


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
    assert "Folder list unavailable" in err
    assert read(config_path)["imap"]["folder"] == "INBOX"


# --- connect-llm -------------------------------------------------------------------------


def test_connect_llm_interaktiv_mit_testaufruf(config_path: Path) -> None:
    provider = FakeProvider()
    hooks = Hooks(build_provider=lambda **kwargs: provider)
    code, out, _err = run(
        ["connect-llm", "--config", str(config_path)],
        # 6 = Anthropic in der Auswahlliste (providers.LLM_PRESETS)
        stdin="6\nclaude-modell\nschluessel\n",
        hooks=hooks,
    )
    assert code == EXIT_OK
    data = read(config_path)["llm"]
    assert data["model"] == "claude-modell"
    assert data["api_key"] == "schluessel"
    assert "expected reply" in out
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
    assert "unexpected reply" in out


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
    assert "unencrypted" in err


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
    assert "Chat ID found: 4711" in out
    assert len(messenger.sent) == 1
    assert "test message" in messenger.sent[0].parts[0]


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
    assert "bot token" in err.lower()


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
    assert "unreachable" in err


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
    assert "The test message could not be delivered" in err


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
    assert "webhook URL" in err


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
    assert "skipped" in out


def test_connect_llm_testaufruf_wiederholt_ratenlimits_nicht(config_path: Path) -> None:
    """Ein Ratenlimit beim Einrichten darf kein Kontingent verbrennen.

    Anbieter mit Tageskontingent (OpenRouter: 50 Anfragen/Tag im Gratis-Tarif) zählen jede
    Wiederholung mit. Ein einziger Fehlversuch hätte sonst drei davon gekostet — und die
    Wartezeiten des Backoffs stünden interaktiv im Weg.
    """
    gesehen: dict[str, object] = {}

    def fake_build(**kwargs: object) -> object:
        gesehen.update(kwargs)
        # Kontrollierter Abbruch: Die CLI fängt LLMError und macht daraus einen
        # sauberen Exit — der Test kommt danach an die mitgeschriebenen Argumente.
        raise LLMTransportError("stop")

    run(
        ["connect-llm", "--config", str(config_path)],
        stdin="6\nclaude-modell\nschluessel\n",
        hooks=Hooks(build_provider=fake_build),
    )
    assert gesehen["max_attempts"] == 1
    assert gesehen["reveal_error_details"] is True


def test_connect_mail_wechselt_nicht_stillschweigend_den_ordner(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein nicht vorhandener Ordner darf nicht kommentarlos durch den ersten ersetzt werden.

    Server liefern die Ordnerliste alphabetisch; der erste Eintrag ist häufig
    „Drafts"/„Entwurf". Würde der nicht-interaktive Modus den nehmen, läse MailDigest
    Entwürfe statt des Posteingangs — und markierte sie als gelesen.
    """

    monkeypatch.setenv("MAILDIGEST_IMAP_PASSWORD", "geheim")

    class Mailbox:
        def connect(self) -> None: ...
        def disconnect(self) -> None: ...
        def list_folders(self) -> list[str]:
            return ["Entwurf", "Gesendet", "INBOX", "Papierkorb"]

    code, out, err = run(
        [
            "connect-mail", "--config", str(config_path), "--non-interactive",
            "--host", "imap.web.de", "--username", "x@web.de", "--folder", "GibtsNicht",
        ],
        stdin="",
        hooks=Hooks(imap_client=lambda section: Mailbox()),
    )

    assert code == EXIT_OK
    assert "does not exist on the server" in err
    assert read(config_path)["imap"]["folder"] == "GibtsNicht"  # unverändert, nicht „Entwurf"
    assert "Entwurf" in out  # die Liste wird trotzdem gezeigt


# --- HC-3: Vorlagenwahl in `connect-llm` -----------------------------------------------


def _init(tmp_path: Path) -> Path:
    """Eine per `init --non-interactive` erzeugte Konfiguration (Werkszustand)."""
    target = tmp_path / "c.toml"
    code, _out, _err = run(["--config", str(target), "--non-interactive", "init"])
    assert code == EXIT_OK
    return target


def test_hc3_openai_compatible_setzt_keine_fremde_base_url(tmp_path: Path) -> None:
    """HC-3: `--provider openai_compatible` darf nicht bei Groq landen (E5).

    `provider` ist mehrdeutig — fünf Vorlagen tragen den Wert. Die Suche lieferte früher
    die erste (Groq) samt deren Erklärtext und deren URL.
    """
    target = _init(tmp_path)
    code, out, _err = run(
        [
            "connect-llm",
            "--config",
            str(target),
            "--non-interactive",
            "--provider",
            "openai_compatible",
            "--model",
            "foo",
            "--no-test",
        ]
    )
    assert code == EXIT_OK
    assert read(target)["llm"]["base_url"] == ""
    assert "groq" not in out.lower()
    assert "Local or OpenAI-compatible model" in out


def test_hc3_base_url_option_bleibt_erhalten(tmp_path: Path) -> None:
    """HC-3: Ausdrücklich gesetzte Endpunkte überschreibt keine Vorlage."""
    target = _init(tmp_path)
    code, _out, _err = run(
        [
            "connect-llm",
            "--config",
            str(target),
            "--non-interactive",
            "--provider",
            "openai_compatible",
            "--model",
            "foo",
            "--base-url",
            "http://127.0.0.1:1/v1",
            "--no-test",
        ]
    )
    assert code == EXIT_OK
    assert read(target)["llm"]["base_url"] == "http://127.0.0.1:1/v1"


def test_hc3_provider_anthropic_zeigt_die_anthropic_anleitung(tmp_path: Path) -> None:
    """HC-3: Die Anleitung gehört zur gewählten Betriebsart."""
    target = _init(tmp_path)
    code, out, _err = run(
        [
            "connect-llm",
            "--config",
            str(target),
            "--non-interactive",
            "--provider",
            "anthropic",
            "--model",
            "claude-haiku-4-5",
            "--no-test",
        ]
    )
    assert code == EXIT_OK
    assert "Getting an API key (Anthropic)" in out
    assert "groq" not in out.lower()


def test_hc3_bestehender_dateiwert_ueberlebt_den_zweiten_lauf(tmp_path: Path) -> None:
    """HC-3: Ohne `--base-url` gilt der bisherige Dateiwert, nie eine Vorlagen-URL."""
    target = _init(tmp_path)
    argv = [
        "connect-llm",
        "--config",
        str(target),
        "--non-interactive",
        "--provider",
        "openai_compatible",
        "--model",
        "foo",
        "--no-test",
    ]
    assert run([*argv, "--base-url", "http://localhost:8000/v1"])[0] == EXIT_OK
    assert run(argv)[0] == EXIT_OK
    assert read(target)["llm"]["base_url"] == "http://localhost:8000/v1"


# --- HC-15: Anbietersperre bei eingegebener Mailadresse --------------------------------


@pytest.mark.parametrize("address", ["me@outlook.com", "me@hotmail.de", "me@proton.me"])
def test_hc15_mailadresse_eines_gesperrten_anbieters_wird_abgelehnt(
    config_path: Path, address: str
) -> None:
    """HC-15: Adresse und Domain sind laut SPEC §4 gleichwertig — auch für die Sperre."""
    before = config_path.read_bytes()
    code, out, err = run(
        [
            "connect-mail",
            "--config",
            str(config_path),
            "--non-interactive",
            "--no-test",
            "--host",
            address,
            "--username",
            address,
        ]
    )
    assert code == EXIT_USAGE
    text = _err_text(out, err)
    assert "MailDigest cannot read" in text
    # Grund und Ausweg stehen in der Meldung, nicht nur die Absage.
    assert "forward" in text.lower() or "mirror" in text.lower()
    assert config_path.read_bytes() == before


def test_hc15_gmail_adresse_wird_weiterhin_uebersetzt(
    config_path: Path, monkeypatch: Any
) -> None:
    """Gegenprobe: Die Übersetzung einer Adresse in den Host bleibt erhalten."""
    monkeypatch.setenv("MAILDIGEST_IMAP_PASSWORD", "aus-env")
    code, out, _err = run(
        [
            "connect-mail",
            "--config",
            str(config_path),
            "--non-interactive",
            "--no-test",
            "--host",
            "me@gmail.com",
            "--username",
            "me@gmail.com",
        ]
    )
    assert code == EXIT_OK
    assert read(config_path)["imap"]["host"] == "imap.gmail.com"
    assert "imap.gmail.com" in out


# --- HC-19: Befehls-Hinweis nach jeder Telegram-Einrichtung ----------------------------


def test_hc19_hinweis_erscheint_auch_mit_chat_id(
    config_path: Path, monkeypatch: Any
) -> None:
    """HC-19: Der `--chat-id`-Zweig kehrte vor dem Hinweisblock zurück."""
    monkeypatch.setenv("MAILDIGEST_TELEGRAM_TOKEN", "aus-env")
    code, out, _err = run(
        [
            "connect-messenger",
            "--config",
            str(config_path),
            "--non-interactive",
            "--messenger",
            "telegram",
            "--chat-id",
            "555",
            "--no-test",
        ]
    )
    assert code == EXIT_OK
    assert read(config_path)["messenger"]["telegram"]["chat_id"] == "555"
    assert "/digest" in out
    assert "/status" in out
    assert out.count("/digest") == 1


def test_hc19_hinweis_erscheint_genau_einmal_ueber_getupdates(
    config_path: Path, monkeypatch: Any
) -> None:
    """Gegenprobe: Auf dem regulären Weg bleibt es bei genau einem Hinweis."""
    monkeypatch.setenv("MAILDIGEST_TELEGRAM_TOKEN", "aus-env")
    hooks = Hooks(
        build_messenger=lambda section, **kwargs: FakeMessenger(),
        discover_chat_ids=lambda **kwargs: [ChatCandidate("9", "private")],
    )
    code, out, _err = run(
        ["connect-messenger", "--config", str(config_path), "--non-interactive", "--no-test"],
        hooks=hooks,
    )
    assert code == EXIT_OK
    assert out.count("/status") == 1


def test_hc18_connect_messenger_ergaenzt_accept_commands(
    config_path: Path, monkeypatch: Any
) -> None:
    """HC-18 (d): Eine alte Datei ohne das Feld bekommt den Default geschrieben."""
    monkeypatch.setenv("MAILDIGEST_TELEGRAM_TOKEN", "aus-env")
    assert "accept_commands" not in config_path.read_text(encoding="utf-8")
    code, _out, _err = run(
        [
            "connect-messenger",
            "--config",
            str(config_path),
            "--non-interactive",
            "--messenger",
            "telegram",
            "--chat-id",
            "555",
            "--no-test",
        ]
    )
    assert code == EXIT_OK
    assert read(config_path)["messenger"]["telegram"]["accept_commands"] is True


# --- HC-38 (1): der Rückweg zum Betrieb ohne Sprachmodell --------------------------------


def test_hc38_connect_llm_provider_none_raeumt_modell_und_key(tmp_path: Path) -> None:
    """HC-38: Der `provider == "none"`-Zweig von `connect-llm` war ungetestet.

    Er ist der Rückweg in den Werkszustand (ADR-076): Modellname geleert, Schlüssel aus
    der Datei entfernt, kein Testaufruf. Bliebe der Schlüssel stehen, läge ein
    Zugangsgeheimnis ohne Grund weiter auf der Platte.
    """
    path = tmp_path / "config.toml"
    path.write_text(
        '[imap]\nport = 993\n\n[llm]\nprovider = "anthropic"\nmodel = "claude"\n'
        'api_key = "sk-geheim"\nbase_url = "https://api.example/v1"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    def explode(**kwargs: Any) -> Any:  # pragma: no cover - darf nie laufen
        raise AssertionError("ohne Modell darf kein Provider gebaut werden")

    code, out, _err = run(
        ["connect-llm", "--config", str(path), "--non-interactive", "--provider", "none"],
        hooks=Hooks(build_provider=explode),
    )
    assert code == EXIT_OK
    llm = read(path)["llm"]
    assert llm["provider"] == "none"
    assert llm["model"] == ""
    assert "api_key" not in llm
    assert "without a language model" in out
