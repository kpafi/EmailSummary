"""Unit-Tests für :class:`maildigest.ingest.imap_client.ImapClient` (WP2).

Gemockt wird auf imap-tools-Ebene: Ein `FakeMailBox` ersetzt `imap_tools.MailBox` über die
injizierte Fabrik — kein Docker, kein echter Server (Begründung: WP2-ADR in
docs/DECISIONS.md).

Schwerpunkte: TLS-Erzwingung (docs/SECURITY.md §6), `mark_seen=False` beim Abruf,
Gelesen-Markierung/Verschieben statt Löschen (F-ING-1), Fehlerübersetzung.
"""

from __future__ import annotations

import ssl
from typing import Any

import pytest
from imap_tools import MailboxFetchError, MailboxLoginError, MailMessage, MailMessageFlags
from pydantic import SecretStr

from maildigest.config import ImapConfig
from maildigest.ingest.imap_client import (
    ImapClient,
    ImapConnectionError,
    IngestError,
    MailboxPostProcessError,
)

MAIL = b"""\
Message-ID: <one@example.org>
From: Absender <a@example.org>
Subject: Test
Date: Fri, 28 Aug 2026 14:12:03 +0200

Hallo.
"""


def make_message(raw: bytes = MAIL, *, uid: str | None = "42") -> MailMessage:
    """Baut eine `MailMessage` wie sie aus einem FETCH käme."""
    if uid is None:
        return MailMessage.from_bytes(raw)
    return MailMessage([(f"1 (UID {uid} FLAGS ())".encode(), raw), b")"])


class FakeFolderInfo:
    """Ein Eintrag der Ordnerliste, wie `imap_tools` ihn liefert."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeFolderManager:
    """Ersatz für `MailBox.folder` — nur `list()` wird von MailDigest benutzt."""

    def __init__(self, folders: list[str], error: Exception | None) -> None:
        self._folders = folders
        self._error = error

    def list(self) -> list[FakeFolderInfo]:
        if self._error is not None:
            raise self._error
        return [FakeFolderInfo(name) for name in self._folders]


def _as_text(value: Any) -> str:
    """Argument eines UID-Kommandos als Text (imaplib bekommt Ordnernamen als Bytes)."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


class FakeRawClient:
    """Ersatz für `imaplib.IMAP4_SSL` — protokolliert **jedes** rohe Kommando.

    Existiert wegen CT-9: `MailBox.flag()`/`move()`/`delete()` hängen intern ein
    unbedingtes `EXPUNGE` an. MailDigest setzt deshalb rohe UID-Kommandos ab, und dieser
    Fake ist die Stelle, an der ein EXPUNGE sichtbar würde.
    """

    def __init__(self, box: FakeMailBox) -> None:
        self._box = box

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self._box.capabilities

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self._box.commands.append((command.upper(), *(_as_text(arg) for arg in args)))
        if self._box.uid_error is not None:
            raise self._box.uid_error
        return self._box.uid_status.get(command.upper(), "OK"), [b""]

    def expunge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf niemals expunge aufrufen (F-ING-1, CT-9)")


class FakeMailBox:
    """Minimaler Ersatz für `imap_tools.MailBox` — protokolliert alle Kommandos."""

    def __init__(
        self,
        messages: list[MailMessage] | None = None,
        *,
        login_error: Exception | None = None,
        fetch_error: Exception | None = None,
        flag_error: Exception | None = None,
        folders: list[str] | None = None,
        folder_error: Exception | None = None,
        capabilities: tuple[str, ...] = ("IMAP4REV1", "MOVE", "UIDPLUS"),
        uid_status: dict[str, str] | None = None,
    ) -> None:
        self.messages = messages if messages is not None else []
        self.folders = folders if folders is not None else ["INBOX", "Archiv"]
        self.folder_error = folder_error
        self.login_error = login_error
        self.fetch_error = fetch_error
        self.uid_error = flag_error
        self.capabilities = capabilities
        self.uid_status = uid_status if uid_status is not None else {}
        self.logins: list[tuple[str, str, str | None]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self.commands: list[tuple[str, ...]] = []
        self.logouts = 0
        self.client = FakeRawClient(self)

    # Abgeleitete Sichten auf `commands` (die Tests von WP2 lesen sie weiter so).
    @property
    def flagged(self) -> list[tuple[str, str, bool]]:
        return [
            (cmd[1], MailMessageFlags.SEEN, True)
            for cmd in self.commands
            if cmd[0] == "STORE" and r"\Seen" in cmd[-1]
        ]

    @property
    def moved(self) -> list[tuple[str, str]]:
        return [(cmd[1], cmd[2].strip('"')) for cmd in self.commands if cmd[0] == "MOVE"]

    def login(self, username: str, password: str, initial_folder: str | None = "INBOX") -> None:
        if self.login_error is not None:
            raise self.login_error
        self.logins.append((username, password, initial_folder))

    def logout(self) -> None:
        self.logouts += 1

    def fetch(self, criteria: Any = "ALL", **kwargs: Any) -> list[MailMessage]:
        self.fetch_calls.append({"criteria": str(criteria), **kwargs})
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.messages)

    @property
    def folder(self) -> Any:
        return FakeFolderManager(self.folders, self.folder_error)

    # Die folgenden Kommandos darf MailDigest niemals aufrufen (F-ING-1, CT-9): Alle drei
    # Komfort-Methoden von imap-tools expungen intern nach jedem STORE.
    def flag(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailBox.flag() expunged — verboten (F-ING-1, CT-9)")

    def move(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailBox.move() kann client-seitig löschen — verboten (CT-9)")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf Mails niemals löschen (F-ING-1)")

    def expunge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("MailDigest darf niemals expunge aufrufen (F-ING-1)")


def make_config(**overrides: Any) -> ImapConfig:
    values: dict[str, Any] = {
        "host": "imap.example.org",
        "username": "mirror@example.org",
        "password": SecretStr("geheim"),
    }
    values.update(overrides)
    return ImapConfig(**values)


def make_client(box: FakeMailBox, **cfg_overrides: Any) -> ImapClient:
    return ImapClient(make_config(**cfg_overrides), mailbox_factory=lambda: box)  # type: ignore[arg-type]


# --- Fehlkonfiguration -----------------------------------------------------------------------


def test_plaintext_port_is_rejected() -> None:
    """docs/SECURITY.md §6: IMAP nur über TLS — Port 143 ist keine Option."""
    with pytest.raises(IngestError, match="143"):
        make_client(FakeMailBox(), port=143)


def test_missing_password_is_rejected_with_hint() -> None:
    with pytest.raises(IngestError, match="MAILDIGEST_IMAP_PASSWORD"):
        ImapClient(make_config(password=None), mailbox_factory=FakeMailBox)  # type: ignore[arg-type]


def test_default_factory_uses_tls_with_certificate_verification() -> None:
    """Der Produktiv-Pfad baut immer einen Default-SSL-Context (Zertifikatsprüfung an)."""
    context = ssl.create_default_context()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


# --- Verbindung ------------------------------------------------------------------------------


def test_connect_logs_in_to_configured_folder() -> None:
    box = FakeMailBox()
    client = make_client(box, folder="Mirror/Neu")
    client.connect()
    assert box.logins == [("mirror@example.org", "geheim", "Mirror/Neu")]


def test_login_failure_becomes_connection_error_without_secret() -> None:
    box = FakeMailBox(login_error=MailboxLoginError(("NO", [b"AUTHENTICATIONFAILED"]), "OK"))
    client = make_client(box)
    with pytest.raises(ImapConnectionError) as excinfo:
        client.connect()
    assert "geheim" not in str(excinfo.value)
    assert "imap.example.org" in str(excinfo.value)


def test_network_error_becomes_connection_error() -> None:
    box = FakeMailBox(login_error=OSError("connection refused"))
    with pytest.raises(ImapConnectionError):
        make_client(box).connect()


def test_access_without_connection_raises() -> None:
    with pytest.raises(ImapConnectionError):
        _ = make_client(FakeMailBox()).mailbox


def test_context_manager_connects_and_logs_out() -> None:
    box = FakeMailBox()
    with make_client(box):
        assert box.logins
    assert box.logouts == 1


def test_disconnect_swallows_logout_errors() -> None:
    box = FakeMailBox()
    client = make_client(box)
    client.connect()
    box.logout = _raising_logout  # type: ignore[method-assign]
    client.disconnect()
    with pytest.raises(ImapConnectionError):
        _ = client.mailbox


def _raising_logout() -> None:
    raise OSError("socket weg")


def test_disconnect_without_connection_is_a_noop() -> None:
    make_client(FakeMailBox()).disconnect()


# --- Abruf -----------------------------------------------------------------------------------


def test_fetch_unseen_does_not_mark_seen() -> None:
    """Entscheidend für F-ING-2: Gelesen-Flag erst nach der Verarbeitung."""
    box = FakeMailBox([make_message()])
    client = make_client(box)
    client.connect()
    messages = list(client.fetch_unseen())
    assert len(messages) == 1
    assert box.fetch_calls[0]["mark_seen"] is False
    assert "UNSEEN" in box.fetch_calls[0]["criteria"]


def test_fetch_error_becomes_connection_error() -> None:
    box = FakeMailBox(fetch_error=MailboxFetchError(("NO", [b"nope"]), "OK"))
    client = make_client(box)
    client.connect()
    with pytest.raises(ImapConnectionError):
        list(client.fetch_unseen())


# --- Nachbehandlung --------------------------------------------------------------------------


def test_mark_processed_sets_seen_flag_only() -> None:
    box = FakeMailBox()
    client = make_client(box)
    client.connect()
    client.mark_processed(make_message(uid="7"))
    assert box.flagged == [("7", MailMessageFlags.SEEN, True)]
    assert box.moved == []


def test_mark_processed_moves_when_configured() -> None:
    box = FakeMailBox()
    client = make_client(box, move_processed_to="Processed")
    client.connect()
    client.mark_processed(make_message(uid="7"))
    assert box.flagged == [("7", MailMessageFlags.SEEN, True)]
    assert box.moved == [("7", "Processed")]


def test_mark_processed_never_deletes() -> None:
    """F-ING-1: Der Fake wirft, sobald irgendein Löschkommando käme."""
    box = FakeMailBox()
    client = make_client(box, move_processed_to="Processed")
    client.connect()
    client.mark_processed(make_message(uid="7"))  # würde sonst AssertionError auslösen


def test_mark_processed_without_uid_is_skipped() -> None:
    box = FakeMailBox()
    client = make_client(box)
    client.connect()
    client.mark_processed(make_message(uid=None))
    assert box.flagged == []
    assert box.moved == []


def test_ct9_mark_processed_sendet_niemals_expunge_oder_deleted() -> None:
    """CT-9/F-ING-1: Kein EXPUNGE, kein `\\Deleted` — auch nicht beim Verschieben.

    Ohne den Fix läuft die Nachbehandlung über `MailBox.flag()`/`move()`; beide hängen
    intern ein unbedingtes `EXPUNGE` an und löschen damit fremde, von anderen Clients als
    `\\Deleted` markierte Mails im Spiegelpostfach endgültig.
    """
    box = FakeMailBox()
    client = make_client(box, move_processed_to="Processed")
    client.connect()
    client.mark_processed(make_message(uid="7"))

    verbs = [cmd[0] for cmd in box.commands]
    assert verbs == ["STORE", "MOVE"]
    assert "EXPUNGE" not in verbs
    assert not any(r"\Deleted" in " ".join(cmd) for cmd in box.commands)
    assert box.commands[0] == ("STORE", "7", "+FLAGS", r"(\Seen)")


def test_ct9_ohne_move_capability_wird_nicht_client_seitig_verschoben() -> None:
    """CT-9: Ohne MOVE-Capability kein COPY+`\\Deleted`+EXPUNGE, sondern eine Meldung."""
    box = FakeMailBox(capabilities=("IMAP4REV1",))
    client = make_client(box, move_processed_to="Processed")
    client.connect()
    with pytest.raises(MailboxPostProcessError, match="MOVE"):
        client.mark_processed(make_message(uid="7"))
    assert [cmd[0] for cmd in box.commands] == ["STORE"]  # nur das Gelesen-Flag


def test_ct12_abgelehntes_move_nennt_das_config_feld() -> None:
    """CT-12: Ein fehlender Zielordner ist kein „Postfach nicht erreichbar“."""
    box = FakeMailBox(uid_status={"MOVE": "NO"})
    client = make_client(box, move_processed_to="GibtEsNicht")
    client.connect()
    with pytest.raises(MailboxPostProcessError) as excinfo:
        client.mark_processed(make_message(uid="7"))
    message = str(excinfo.value)
    assert "move_processed_to" in message
    assert "GibtEsNicht" in message
    assert not isinstance(excinfo.value, ImapConnectionError)


def test_flag_error_becomes_connection_error() -> None:
    box = FakeMailBox(flag_error=OSError("socket weg"))
    client = make_client(box)
    client.connect()
    with pytest.raises(ImapConnectionError):
        client.mark_processed(make_message(uid="7"))


# --- Ordnerliste (WP9, `maildigest connect-mail`) ---------------------------------------------


def test_list_folders_liefert_die_namen() -> None:
    box = FakeMailBox(folders=["INBOX", "Archiv", "Processed"])
    client = make_client(box)
    client.connect()
    assert client.list_folders() == ["INBOX", "Archiv", "Processed"]


def test_list_folders_ueberspringt_namenlose_eintraege() -> None:
    box = FakeMailBox(folders=["INBOX", "", "Archiv"])
    client = make_client(box)
    client.connect()
    assert client.list_folders() == ["INBOX", "Archiv"]


def test_list_folders_ohne_verbindung_ist_ein_verbindungsfehler() -> None:
    client = make_client(FakeMailBox())
    with pytest.raises(ImapConnectionError):
        client.list_folders()


def test_list_folders_uebersetzt_imap_fehler() -> None:
    box = FakeMailBox(folder_error=MailboxFetchError(("NO", [b"LIST failed"]), "OK"))
    client = make_client(box)
    client.connect()
    with pytest.raises(ImapConnectionError, match="folder list"):
        client.list_folders()


def test_list_folders_loescht_nichts() -> None:
    """Die Ordnerliste ist rein lesend (F-ING-1)."""
    box = FakeMailBox()
    client = make_client(box)
    client.connect()
    client.list_folders()
    assert box.moved == [] and box.flagged == []
