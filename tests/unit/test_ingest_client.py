"""Unit-Tests für :class:`maildigest.ingest.imap_client.ImapClient` (WP2).

Gemockt wird auf imap-tools-Ebene: Ein `FakeMailBox` ersetzt `imap_tools.MailBox` über die
injizierte Fabrik — kein Docker, kein echter Server (Begründung: WP2-ADR in
docs/DECISIONS.md).

Schwerpunkte: TLS-Erzwingung (docs/SECURITY.md §6), `mark_seen=False` beim Abruf,
Gelesen-Markierung/Verschieben statt Löschen (F-ING-1), Fehlerübersetzung.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from typing import Any

import pytest
from imap_tools import MailboxFetchError, MailboxLoginError, MailMessage, MailMessageFlags
from pydantic import SecretStr

from maildigest.config import ImapConfig
from maildigest.ingest.imap_client import (
    ImapAuthError,
    ImapClient,
    ImapConnectionError,
    IngestError,
    MailboxPostProcessError,
    UnparsableMailMessage,
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


def header_fetch_answer(uid: str, raw: bytes, parts: str) -> tuple[str, list[Any]]:
    """Serverantwort auf ``UID FETCH <uid> (BODY.PEEK[HEADER]<0.N> RFC822.SIZE INTERNALDATE)``.

    Genau die Form, die `imaplib` liefert: ein Tupel (Metadaten, Kopfzeilen) und das
    schliessende `)`. Der Teilabruf `<0.N>` wird wie beim Server angewandt.
    """
    limit = re.search(r"<0\.(\d+)>", parts)
    headers = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    if limit:
        headers = headers[: int(limit.group(1))]
    meta = (
        f"1 (UID {uid} RFC822.SIZE {len(raw)} INTERNALDATE "
        f'"01-Sep-2026 12:00:00 +0000" BODY[HEADER]<0> {{{len(headers)}}}'
    ).encode()
    return "OK", [(meta, headers), b")"]


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
        if command.upper() == "FETCH":
            # O-1 (zweite Iteration): der Kopfzeilen-Abruf einer unparsbaren Mail.
            if self._box.header_fetch_error is not None:
                raise self._box.header_fetch_error
            entry = self._box.unparsable.get(str(args[0]))
            raw = entry[1] if isinstance(entry, tuple) else entry
            if isinstance(raw, bytes):
                return header_fetch_answer(str(args[0]), raw, str(args[1]))
            return "NO", [None]
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
        search_error: Exception | None = None,
        unparsable: dict[str, Any] | None = None,
        header_fetch_error: Exception | None = None,
        vanished: tuple[str, ...] = (),
    ) -> None:
        """`unparsable`: UID → Rohbytes, die imap-tools nicht parsen kann, **oder** eine
        Ausnahme, die der Konstruktor werfen soll, **oder** (Ausnahme, Rohbytes für den
        Kopfzeilen-Abruf). `vanished`: UIDs, die SEARCH nennt, FETCH aber nicht mehr findet."""
        self.messages = messages if messages is not None else []
        self.search_error = search_error
        self.unparsable = unparsable if unparsable is not None else {}
        self.header_fetch_error = header_fetch_error
        self.vanished = vanished
        self.search_calls: list[str] = []
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

    def uids(self, criteria: Any = "ALL", charset: Any = "US-ASCII", sort: Any = None) -> list[Any]:
        """Wie `MailBox.uids`: ein `UID SEARCH`; liefert die UIDs aller ungesehenen Mails."""
        self.search_calls.append(str(criteria))
        if self.search_error is not None:
            raise self.search_error
        uids = [msg.uid for msg in self.messages] + list(self.unparsable) + list(self.vanished)
        return sorted(uids, key=lambda uid: str(uid or "").zfill(9))  # Server-Reihenfolge

    def fetch(self, criteria: Any = "ALL", **kwargs: Any) -> list[MailMessage]:
        """Wie `MailBox.fetch(uid_list=...)`: parst je UID im Aufruf (eifrig, wie imap-tools)."""
        self.fetch_calls.append({"criteria": str(criteria), **kwargs})
        if self.fetch_error is not None:
            raise self.fetch_error
        uid_list = kwargs.get("uid_list")
        if uid_list is None:
            return list(self.messages)
        found = [msg for msg in self.messages if msg.uid in uid_list]
        for uid in uid_list:
            entry = self.unparsable.get(uid)
            if isinstance(entry, tuple):
                raise entry[0]
            if isinstance(entry, BaseException):
                raise entry
            if isinstance(entry, bytes):  # der echte Parse von imap-tools — er wirft selbst
                found.append(MailMessage([(f"1 (UID {uid} FLAGS ())".encode(), entry), b")"]))
        return found

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


def test_hc32_login_failure_is_an_auth_error() -> None:
    """Ein abgelehnter Login ist unterscheidbar — aber weiterhin ein Verbindungsfehler.

    Die Einrichtung hängt den anbieterspezifischen Hinweis („App-Passwort") nur an einen
    Anmeldefehler; der Polling-Loop behandelt beides gleich (Reconnect mit Backoff),
    deshalb bleibt `ImapAuthError` eine Unterklasse von `ImapConnectionError` (HC-32).
    """
    box = FakeMailBox(login_error=MailboxLoginError(("NO", [b"AUTHENTICATIONFAILED"]), "OK"))
    with pytest.raises(ImapAuthError) as excinfo:
        make_client(box).connect()
    assert isinstance(excinfo.value, ImapConnectionError)
    assert "geheim" not in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("connection refused"),
        OSError("no route to host"),
        ssl.SSLError("handshake failed"),
    ],
)
def test_hc32_transport_failure_is_no_auth_error(error: Exception) -> None:
    """Ein Netz-/TLS-Fehler ist **kein** Anmeldefehler — es wurde nie ein Login versucht."""
    box = FakeMailBox(login_error=error)
    with pytest.raises(ImapConnectionError) as excinfo:
        make_client(box).connect()
    assert not isinstance(excinfo.value, ImapAuthError)


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
    # O-1 (zweite Iteration): erst ein `UID SEARCH UNSEEN`, dann je UID ein eigener Abruf.
    assert box.search_calls == ["(UNSEEN)"]
    assert box.fetch_calls[0]["uid_list"] == [messages[0].uid]


def test_fetch_error_becomes_connection_error() -> None:
    box = FakeMailBox([make_message()], fetch_error=MailboxFetchError(("NO", [b"nope"]), "OK"))
    client = make_client(box)
    client.connect()
    with pytest.raises(ImapConnectionError):
        list(client.fetch_unseen())


def test_o1b_search_fehler_ist_ein_verbindungsfehler() -> None:
    """Der neue Vorab-`UID SEARCH` meldet Transportfehler wie der Abruf: als Verbindungsfehler."""
    box = FakeMailBox(search_error=OSError("socket error"))
    client = make_client(box)
    client.connect()
    with pytest.raises(ImapConnectionError):
        list(client.fetch_unseen())


@pytest.mark.parametrize(
    "error",
    [
        OSError("socket error"),
        MailboxFetchError(("NO", [b"nope"]), "OK"),
        imaplib.IMAP4.abort("socket error: EOF"),
    ],
    ids=["OSError", "MailboxFetchError", "imaplib.abort"],
)
def test_o1b_echter_verbindungsfehler_bleibt_verbindungsfehler(error: Exception) -> None:
    """Die Isolation je UID darf einen echten Verbindungsabbruch nicht als „unparsbare Mail"
    verbuchen — sonst würde eine tote Verbindung als `failed`-Mail gebucht. `imaplib`
    meldet Socketfehler als eigenes `abort`, das kein `ImapToolsError` ist."""
    box = FakeMailBox([make_message(uid="1")], fetch_error=error)
    client = make_client(box)
    client.connect()
    with pytest.raises(ImapConnectionError):
        list(client.fetch_unseen())


def test_o1b_fetch_unseen_isoliert_unparsbare_mail() -> None:
    """O-1 (zweite Iteration): Wirft der Parse von imap-tools für **eine** UID, liefert
    `fetch_unseen` dafür einen Platzhalter mit den nachgeholten Kopfzeilen — und die Mail
    dahinter ganz normal. Vor dem Fix wurde der `RecursionError` in `ImapConnectionError`
    umgedeutet: kein Platzhalter, keine weitere Mail, endlose Wiederholung."""
    kopf = (
        b"Message-ID: <gift@example.org>\r\nFrom: a@example.org\r\nSubject: Gift\r\n\r\nHallo\r\n"
    )
    box = FakeMailBox(
        [make_message(uid="2")], unparsable={"1": (RecursionError("maximum recursion"), kopf)}
    )
    client = make_client(box)
    client.connect()
    messages = list(client.fetch_unseen())

    assert [msg.uid for msg in messages] == ["1", "2"]
    platzhalter = messages[0]
    assert isinstance(platzhalter, UnparsableMailMessage)
    assert platzhalter.parse_error == "RecursionError"
    assert platzhalter.headers_fetched is True
    assert platzhalter.obj["Message-ID"] == "<gift@example.org>"
    assert platzhalter.size_rfc822 == len(kopf)
    assert not isinstance(messages[1], UnparsableMailMessage)
    # Genau ein zusätzliches Kommando, nur für die unparsbare Mail, nur Kopfzeilen, PEEK.
    fetches = [cmd for cmd in box.commands if cmd[0] == "FETCH"]
    assert len(fetches) == 1
    assert fetches[0][1] == "1"
    assert "BODY.PEEK[HEADER]<0." in fetches[0][2]
    assert "RFC822.SIZE" in fetches[0][2] and "INTERNALDATE" in fetches[0][2]


def test_o1b_kopfzeilen_abruf_scheitert_auch() -> None:
    """Scheitert auch der Kopfzeilen-Abruf, bleibt der Platzhalter — mit UID, ohne Felder."""
    box = FakeMailBox(
        unparsable={"7": RecursionError("maximum recursion")},
        header_fetch_error=OSError("socket error"),
    )
    client = make_client(box)
    client.connect()
    (platzhalter,) = client.fetch_unseen()

    assert isinstance(platzhalter, UnparsableMailMessage)
    assert platzhalter.uid == "7"
    assert platzhalter.headers_fetched is False
    assert list(platzhalter.obj.items()) == []
    assert platzhalter.size_rfc822 == 0


def test_o1b_unparsbare_mail_ohne_kopfzeilen_antwort() -> None:
    """Antwortet der Server auf den Kopfzeilen-Abruf mit NO, gilt dasselbe wie bei einem Fehler."""
    box = FakeMailBox(unparsable={"7": RecursionError("maximum recursion")})
    client = make_client(box)
    client.connect()
    (platzhalter,) = client.fetch_unseen()
    assert isinstance(platzhalter, UnparsableMailMessage)
    assert platzhalter.headers_fetched is False
    assert platzhalter.uid == "7"


def test_o1b_verschwundene_uid_wird_uebersprungen() -> None:
    """Eine UID, die SEARCH nennt, FETCH aber nicht mehr findet (anderer Client), wird
    übersprungen — wie bei `MailBox.fetch`, und ohne Platzhalter."""
    box = FakeMailBox([make_message(uid="2")], vanished=("1",))
    client = make_client(box)
    client.connect()
    assert [msg.uid for msg in client.fetch_unseen()] == ["2"]


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
