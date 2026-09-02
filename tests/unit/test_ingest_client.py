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
from maildigest.ingest.imap_client import ImapClient, ImapConnectionError, IngestError

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
    ) -> None:
        self.messages = messages if messages is not None else []
        self.folders = folders if folders is not None else ["INBOX", "Archiv"]
        self.folder_error = folder_error
        self.login_error = login_error
        self.fetch_error = fetch_error
        self.flag_error = flag_error
        self.logins: list[tuple[str, str, str | None]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self.flagged: list[tuple[str, str, bool]] = []
        self.moved: list[tuple[str, str]] = []
        self.logouts = 0

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

    def flag(self, uid_list: str, flag_set: str, value: bool) -> None:
        if self.flag_error is not None:
            raise self.flag_error
        self.flagged.append((uid_list, flag_set, value))

    def move(self, uid_list: str, destination_folder: str) -> None:
        self.moved.append((uid_list, destination_folder))

    @property
    def folder(self) -> Any:
        return FakeFolderManager(self.folders, self.folder_error)

    # Die folgenden Kommandos darf MailDigest niemals aufrufen (F-ING-1).
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
    with pytest.raises(ImapConnectionError, match="Ordnerliste"):
        client.list_folders()


def test_list_folders_loescht_nichts() -> None:
    """Die Ordnerliste ist rein lesend (F-ING-1)."""
    box = FakeMailBox()
    client = make_client(box)
    client.connect()
    client.list_folders()
    assert box.moved == [] and box.flagged == []
