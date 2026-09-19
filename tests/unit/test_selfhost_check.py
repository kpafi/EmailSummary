"""Tests der Netzprüfungen des selbst gehosteten Spiegelpostfachs (S3, F-ING-4, ADR-089).

Vertrag: docs/SPEC-CLI.md §4 `selfhost-mail --check`, Entwurf: docs/PLAN-SELFHOST-MAIL.md
§3/§8. Jede der neun Prüfungen hat hier einen bestandenen **und** einen gescheiterten Fall.

Wogegen geprüft wird — bewusst so echt wie ohne Mailserver möglich:

* **SMTP** gegen einen geskripteten Socket auf einem flüchtigen Port von 127.0.0.1. Der
  echte `smtplib`-Dialog läuft also wirklich; der Gegenüber protokolliert jedes Kommando,
  sodass „kein `DATA`, immer `RSET`/`QUIT`" beweisbar ist statt behauptet.
* **Port 143** gegen einen echten Listener (mit Banner, ohne Banner) und gegen einen
  geschlossenen Port.
* **IMAPS** über den `FakeMailBox` aus `test_ingest_client.py` — dieselbe Attrappe wie der
  Ingest, damit hier keine zweite IMAP-Welt entsteht (PLAN §8, Abnahme S3).
* **DNS** und **Zertifikat** über :class:`~maildigest.selfhost_check.Probes`; für das
  Zertifikat zusätzlich die echte Funktion gegen einen Socket, der sofort zumacht.
* **dnspython** fehlt in der Testumgebung ohnehin; beide Fälle (fehlend → `skipped`,
  vorhanden → Antwort eines Resolvers) werden über `sys.modules` gestellt.
"""

from __future__ import annotations

import imaplib
import io
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from imap_tools import MailboxLoginError
from test_ingest_client import FakeFolderManager, FakeMailBox, make_message

from maildigest import cli, selfhost, selfhost_check
from maildigest.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Hooks, main
from maildigest.config import ENV_IMAP_PASSWORD, ImapConfig
from maildigest.ingest.imap_client import ImapClient
from maildigest.selfhost import MirrorState
from maildigest.selfhost_check import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIPPED,
    CheckResult,
    Probes,
)

DOMAIN = "mirror.example.org"
ADDRESS = "mirror-7f3a9c1d@mirror.example.org"
PASSWORD = "geheim"


def make_state(**overrides: str) -> MirrorState:
    """Der Zustand, den `--check` aus `state.json` liest."""
    values: dict[str, str] = {
        "domain": DOMAIN,
        "address": ADDRESS,
        "cert_dir": f"/etc/letsencrypt/live/{DOMAIN}",
        "generated_at": "2026-09-19T12:04:57Z",
    }
    values.update(overrides)
    return MirrorState(**values)  # type: ignore[arg-type]


# --- Attrappen ---------------------------------------------------------------------------


class Clock:
    """Uhr und Schlaf in einem: `sleep()` lässt `monotonic()` springen.

    Ohne das würde jeder `--wait-for-mail`-Test wirklich Minuten warten.
    """

    def __init__(self, *, on_sleep: Any = None) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.sleeps))


class FolderWithStatus(FakeFolderManager):
    """`FakeFolderManager` plus `status()` — MailDigest liest daraus nur `UIDNEXT`."""

    def __init__(self, box: MailboxWithStatus) -> None:
        super().__init__(box.folders, box.folder_error)
        self._box = box

    def status(self, folder: Any = None, options: Any = None) -> dict[str, int]:
        del folder, options
        if self._box.status_error is not None:
            raise self._box.status_error
        return {"UIDNEXT": self._box.uidnext}


class MailboxWithStatus(FakeMailBox):
    """Die Ingest-Attrappe, ergänzt um die einzige Zusatzabfrage von `--wait-for-mail`."""

    def __init__(
        self, messages: list[Any] | None = None, *, uidnext: int = 1, **kwargs: Any
    ) -> None:
        self.uidnext = uidnext
        self.status_error: Exception | None = kwargs.pop("status_error", None)
        super().__init__(messages, **kwargs)

    @property
    def folder(self) -> Any:
        return FolderWithStatus(self)

    def deliver(self, message: Any) -> None:
        """Eine Mail trifft ein: Sie liegt in der INBOX und `UIDNEXT` steigt."""
        self.messages.append(message)
        self.uidnext = int(str(message.uid)) + 1


def client_factory(box: FakeMailBox) -> Any:
    """Eine `imap_client`-Probe, die den echten `ImapClient` über die Attrappe legt."""

    def factory(cfg: ImapConfig) -> ImapClient:
        return ImapClient(cfg, mailbox_factory=lambda: box)  # type: ignore[arg-type]

    return factory


class ScriptedSmtp:
    """Ein geskripteter SMTP-Gegenüber auf 127.0.0.1 und einem flüchtigen Port.

    Beantwortet genau die Kommandos, die die Prüfung sendet, und merkt sich jedes — so
    lässt sich belegen, dass nie `DATA` kommt und immer `RSET`/`QUIT` (keine wartende Mail).
    """

    def __init__(
        self,
        *,
        banner: str | None = f"220 {DOMAIN} ESMTP Postfix",
        mail_code: int = 250,
        relay_code: int = 550,
        rcpt_code: int = 250,
    ) -> None:
        self.banner = banner
        self.mail_code = mail_code
        self.relay_code = relay_code
        self.rcpt_code = rcpt_code
        self.commands: list[str] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = int(self._sock.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> ScriptedSmtp:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._sock.close()
        self._thread.join(timeout=5)

    def _reply(self, text: str) -> str:
        verb = text.split(" ", 1)[0].upper()
        if verb in {"EHLO", "HELO"}:
            return f"250 {DOMAIN}"
        if verb == "MAIL":
            return f"{self.mail_code} sender"
        if verb == "RCPT":
            code = (
                self.relay_code
                if selfhost_check.RELAY_TEST_ADDRESS in text
                else self.rcpt_code
            )
            return f"{code} recipient"
        if verb == "RSET":
            return "250 reset"
        return "500 unexpected command"

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:  # pragma: no cover - nur beim Abbau
            return
        with conn, conn.makefile("rwb", buffering=0) as stream:
            if self.banner is not None:
                stream.write(f"{self.banner}\r\n".encode())
            while True:
                line = stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                self.commands.append(text)
                if text.upper().startswith("QUIT"):
                    stream.write(b"221 bye\r\n")
                    return
                stream.write(f"{self._reply(text)}\r\n".encode())

    def verbs(self) -> list[str]:
        """Die Kommandowörter in der gesendeten Reihenfolge."""
        return [command.split(" ", 1)[0].upper() for command in self.commands]


class TcpListener:
    """Ein Port, der eine Verbindung annimmt und optional ein Banner schickt."""

    def __init__(self, banner: bytes | None) -> None:
        self.banner = banner
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = int(self._sock.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> TcpListener:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._sock.close()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:  # pragma: no cover - nur beim Abbau
            return
        with conn:
            if self.banner is not None:
                conn.sendall(self.banner)
            conn.recv(16)


def closed_port() -> int:
    """Ein Port, auf dem sicher niemand lauscht."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def cert_expiring_in(days: int) -> dict[str, Any]:
    """Ein Zertifikat, wie `getpeercert()` es liefert — nur das eine benutzte Feld."""
    moment = datetime.now(UTC) + timedelta(days=days, hours=1)
    return {"notAfter": moment.strftime("%b %d %H:%M:%S %Y GMT")}


def probes(**overrides: Any) -> Probes:
    """Probes, bei denen ab Werk **jede** Prüfung besteht; Tests ersetzen einzelne Felder."""
    values: dict[str, Any] = {
        "resolve": lambda domain: ["203.0.113.7", "2001:db8::7"],
        "mx": lambda domain: [(10, domain)],
        "certificate": lambda host, port: cert_expiring_in(84),
        "banner": lambda host, port, timeout: b"",
        "imap_client": client_factory(MailboxWithStatus()),
        "plaintext_host": "127.0.0.1",
        "plaintext_port": closed_port(),
        "smtp_port": closed_port(),
    }
    values.update(overrides)
    return Probes(**values)


# --- DNS ---------------------------------------------------------------------------------


def test_dns_ok_names_every_address() -> None:
    result = selfhost_check.check_dns(make_state(), probes())
    assert result.status == STATUS_OK
    assert result.text == f"{DOMAIN} -> 203.0.113.7, 2001:db8::7"
    assert result.warnings == ()


def test_dns_fails_when_the_name_does_not_resolve() -> None:
    def boom(domain: str) -> list[str]:
        raise socket.gaierror(-2, "Name or service not known")

    result = selfhost_check.check_dns(make_state(), probes(resolve=boom))
    assert result.status == STATUS_FAIL
    assert result.text == (
        f"{DOMAIN} does not resolve — create the A (and AAAA) record from "
        "selfhost-mail/dns.txt."
    )


def test_dns_private_address_stays_ok_but_warns() -> None:
    """PLAN §7: Die Container-Probe bildet die Domain auf 127.0.0.1 ab — das darf laufen."""
    result = selfhost_check.check_dns(make_state(), probes(resolve=lambda d: ["127.0.0.1"]))
    assert result.status == STATUS_OK
    assert result.warnings == (
        f"{DOMAIN} resolves to a private address (127.0.0.1) — the internet cannot "
        "deliver there.",
    )


def test_dns_empty_answer_is_a_failure() -> None:
    result = selfhost_check.check_dns(make_state(), probes(resolve=lambda d: []))
    assert result.status == STATUS_FAIL


# --- MX (dnspython optional, D4) -----------------------------------------------------------


def test_mx_ok_when_the_domain_points_at_itself() -> None:
    result = selfhost_check.check_mx(make_state(), probes())
    assert result.status == STATUS_OK
    assert result.text == f"MX 10 {DOMAIN}."


def test_mx_fails_when_it_points_elsewhere() -> None:
    result = selfhost_check.check_mx(
        make_state(), probes(mx=lambda d: [(10, "mail.example.net")])
    )
    assert result.status == STATUS_FAIL
    assert result.text == (
        f"the MX of {DOMAIN} does not point at {DOMAIN} — create the MX record from "
        "selfhost-mail/dns.txt."
    )


def test_mx_is_skipped_without_dnspython(monkeypatch: pytest.MonkeyPatch) -> None:
    """D4: Die fehlende optionale Bibliothek ist kein Fehler des Postfachs."""
    monkeypatch.setitem(sys.modules, "dns.resolver", None)
    result = selfhost_check.check_mx(make_state(), Probes())
    assert result.status == STATUS_SKIPPED
    assert result.text == (
        "install python3-dnspython (Debian) / python3-dns (Fedora) for the MX check"
    )


def test_mx_uses_dnspython_when_it_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def resolve(name: str, kind: str, lifetime: float | None = None) -> list[Any]:
        calls.append((name, kind))
        assert lifetime == selfhost_check.DNS_TIMEOUT_SECONDS
        return [SimpleNamespace(preference=10, exchange=f"{DOMAIN}.")]

    monkeypatch.setitem(sys.modules, "dns.resolver", SimpleNamespace(resolve=resolve))
    result = selfhost_check.check_mx(make_state(), Probes())
    assert calls == [(DOMAIN, "MX")]
    assert result.status == STATUS_OK
    assert result.text == f"MX 10 {DOMAIN}."


def test_mx_lookup_error_is_a_failure_not_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(name: str, kind: str, lifetime: float | None = None) -> list[Any]:
        raise RuntimeError("NXDOMAIN")

    monkeypatch.setitem(sys.modules, "dns.resolver", SimpleNamespace(resolve=resolve))
    assert selfhost_check.check_mx(make_state(), Probes()).status == STATUS_FAIL


# --- SMTP --------------------------------------------------------------------------------


def smtp_results(server: ScriptedSmtp) -> dict[str, CheckResult]:
    """Führt den echten SMTP-Dialog gegen den geskripteten Gegenüber."""
    results = selfhost_check.check_smtp(make_state(), probes(smtp_port=server.port))
    return {result.name: result for result in results}


def test_smtp_all_three_pass_against_a_well_behaved_server() -> None:
    with ScriptedSmtp() as server:
        results = smtp_results(server)
    assert results["SMTP banner"].status == STATUS_OK
    assert results["SMTP banner"].text == f"220 {DOMAIN} ESMTP Postfix"
    assert results["SMTP relay"].text == "RCPT TO an outside address refused (550)"
    assert results["SMTP recipient"].text == f"RCPT TO {ADDRESS} accepted (250)"
    assert all(result.status == STATUS_OK for result in results.values())


def test_smtp_never_sends_data_and_always_closes_cleanly() -> None:
    """Eine Prüfung darf keine Post hinterlassen (PLAN §3)."""
    with ScriptedSmtp() as server:
        smtp_results(server)
    assert server.verbs() == ["EHLO", "MAIL", "RCPT", "RCPT", "RSET", "QUIT"]
    assert "DATA" not in server.verbs()


def test_smtp_banner_fails_when_it_names_another_host() -> None:
    with ScriptedSmtp(banner="220 other.example ESMTP") as server:
        results = smtp_results(server)
    assert results["SMTP banner"].status == STATUS_FAIL
    assert results["SMTP banner"].text.startswith("nothing answers on 127.0.0.1:")
    # Der Rest des Dialogs läuft trotzdem — es gibt keinen frühen Abbruch.
    assert results["SMTP recipient"].status == STATUS_OK


def test_smtp_open_relay_is_a_failure() -> None:
    with ScriptedSmtp(relay_code=250) as server:
        results = smtp_results(server)
    assert results["SMTP relay"].status == STATUS_FAIL
    assert results["SMTP relay"].text == (
        "the server accepted mail for an outside address — that is an open relay; "
        "reapply selfhost-mail/postfix.sh, which sets smtpd_relay_restrictions."
    )
    assert results["SMTP recipient"].status == STATUS_OK


def test_smtp_refused_mirror_address_is_a_failure() -> None:
    with ScriptedSmtp(rcpt_code=550) as server:
        results = smtp_results(server)
    assert results["SMTP recipient"].status == STATUS_FAIL
    assert results["SMTP recipient"].text == (
        "the server refuses its own mirror address — check virtual_mailbox_maps in "
        "selfhost-mail/postfix.sh and reload Postfix."
    )


def test_smtp_refused_sender_fails_both_recipient_checks() -> None:
    """Ohne angenommenen Absender sagt eine 5xx auf `RCPT TO` nichts über Relay-Regeln."""
    with ScriptedSmtp(mail_code=550) as server:
        results = smtp_results(server)
    assert results["SMTP relay"].status == STATUS_FAIL
    assert results["SMTP recipient"].status == STATUS_FAIL


def test_smtp_without_a_server_fails_all_three() -> None:
    results = {
        result.name: result
        for result in selfhost_check.check_smtp(make_state(), probes(smtp_port=closed_port()))
    }
    assert [result.status for result in results.values()] == [STATUS_FAIL] * 3


def test_smtp_banner_from_the_server_is_sanitized() -> None:
    """ADR-055: Kein Fremdtext erreicht das Terminal ungefiltert."""
    with ScriptedSmtp(banner=f"220 {DOMAIN} \x1b[31mESMTP") as server:
        results = smtp_results(server)
    assert "\x1b" not in results["SMTP banner"].text
    assert "·" in results["SMTP banner"].text


# --- Zertifikat ----------------------------------------------------------------------------


def test_certificate_ok_reports_the_remaining_days() -> None:
    result = selfhost_check.check_certificate(make_state(), probes())
    assert result.status == STATUS_OK
    assert result.text == "chain and host name valid, 84 days left"
    assert result.warnings == ()


def test_certificate_near_expiry_stays_ok_but_warns() -> None:
    result = selfhost_check.check_certificate(
        make_state(), probes(certificate=lambda h, p: cert_expiring_in(5))
    )
    assert result.status == STATUS_OK
    assert result.warnings == (
        f"Certificate for {DOMAIN} expires in 5 days — check the certbot timer.",
    )


def test_certificate_that_does_not_verify_is_a_failure() -> None:
    def boom(host: str, port: int) -> dict[str, Any]:
        raise ssl.SSLCertVerificationError("hostname mismatch")

    result = selfhost_check.check_certificate(make_state(), probes(certificate=boom))
    assert result.status == STATUS_FAIL
    assert result.text == (
        f"the certificate of {DOMAIN}:993 is not valid for this host — run certbot "
        f"certonly --standalone -d {DOMAIN} and reload Dovecot."
    )


def test_certificate_without_a_usable_date_is_a_failure() -> None:
    result = selfhost_check.check_certificate(
        make_state(), probes(certificate=lambda h, p: {"notAfter": "irgendwann"})
    )
    assert result.status == STATUS_FAIL


def test_real_certificate_probe_fails_on_a_port_without_tls() -> None:
    """Die echte Funktion, nicht die Attrappe: ein Port ohne TLS ist ein Fehler."""
    with TcpListener(banner=None) as listener, pytest.raises((ssl.SSLError, OSError)):
        selfhost_check.peer_certificate("127.0.0.1", listener.port)


# --- Anmeldung und INBOX ---------------------------------------------------------------------


def login_result(box: FakeMailBox) -> CheckResult:
    results = selfhost_check.run_checks(
        make_state(), password=PASSWORD, probes=probes(imap_client=client_factory(box))
    )
    return next(result for result in results if result.name == "IMAPS login")


def test_login_ok_selects_the_inbox() -> None:
    box = MailboxWithStatus()
    result = login_result(box)
    assert result.status == STATUS_OK
    assert result.text == "login succeeded, INBOX selectable"
    assert box.logins == [(ADDRESS, PASSWORD, "INBOX")]
    assert box.logouts == 1


def test_login_failure_names_the_address_and_apply_sh() -> None:
    box = MailboxWithStatus(
        login_error=MailboxLoginError(("NO", [b"AUTHENTICATIONFAILED"]), "OK")
    )
    result = login_result(box)
    assert result.status == STATUS_FAIL
    # Der Satz nennt zuerst die Zertifikatszeile: Scheitert der TLS-Aufbau, kam die
    # Anmeldung gar nicht zustande, und ein neues Passwort änderte daran nichts (CT-S5).
    assert result.text == (
        f"the login for {ADDRESS} failed — read the IMAPS cert line first; if that one "
        "is ok, rerun apply.sh to set the mailbox password."
    )


def test_login_uses_imaps_and_the_address_as_user_name() -> None:
    """Der Nutzername *ist* die Adresse (ADR-089); Port 993, nie 143."""
    seen: list[ImapConfig] = []

    def factory(cfg: ImapConfig) -> ImapClient:
        seen.append(cfg)
        return ImapClient(cfg, mailbox_factory=lambda: MailboxWithStatus())  # type: ignore[arg-type]

    selfhost_check.run_checks(
        make_state(), password=PASSWORD, probes=probes(imap_client=factory)
    )
    assert seen[0].port == 993
    assert seen[0].username == ADDRESS
    assert seen[0].folder == "INBOX"
    assert seen[0].password is not None
    assert seen[0].password.get_secret_value() == PASSWORD


# --- Klartext-Port 143 -------------------------------------------------------------------


def plaintext_result(**overrides: Any) -> CheckResult:
    return selfhost_check.check_plaintext_port(make_state(), probes(**overrides))


def test_plaintext_port_closed_is_ok() -> None:
    result = plaintext_result(banner=selfhost_check.plaintext_banner, plaintext_port=closed_port())
    assert result.status == STATUS_OK
    assert result.text == "cleartext IMAP is closed"


def test_plaintext_port_with_an_imap_banner_is_a_failure() -> None:
    with TcpListener(banner=b"* OK [CAPABILITY IMAP4rev1] Dovecot ready.\r\n") as listener:
        result = plaintext_result(
            banner=selfhost_check.plaintext_banner,
            plaintext_port=listener.port,
            plaintext_timeout=2.0,
        )
    assert result.status == STATUS_FAIL
    assert result.text == (
        f"cleartext IMAP on {DOMAIN}:{listener.port} is open — the port = 0 line from "
        "selfhost-mail/dovecot.conf is missing; reapply it and reload Dovecot."
    )


def test_plaintext_port_that_answers_nothing_is_ok() -> None:
    """„Refused **oder** kein Banner innerhalb der Frist" — beides ist in Ordnung."""
    with TcpListener(banner=None) as listener:
        result = plaintext_result(
            banner=selfhost_check.plaintext_banner,
            plaintext_port=listener.port,
            plaintext_timeout=0.3,
        )
    assert result.status == STATUS_OK


# --- Wartemail -----------------------------------------------------------------------------


def wait_result(box: MailboxWithStatus, clock: Clock, *, timeout: int = 60) -> CheckResult:
    active = probes(
        imap_client=client_factory(box), sleep=clock.sleep, monotonic=clock.monotonic
    )
    results = selfhost_check.run_checks(
        make_state(), password=PASSWORD, wait_for_mail=True, timeout=timeout, probes=active
    )
    assert [result.name for result in results] == list(selfhost_check.CHECK_NAMES)
    return results[-1]


def test_wait_for_mail_reports_sender_and_subject() -> None:
    box = MailboxWithStatus([make_message(uid="5")], uidnext=6)

    def arrive(count: int) -> None:
        if count == 2:
            box.deliver(make_message(uid="6"))

    result = wait_result(box, Clock(on_sleep=arrive))
    assert result.status == STATUS_OK
    assert result.text == 'from a@example.org — "Test"'


def test_wait_for_mail_ignores_what_was_already_there() -> None:
    """Die alte Mail mit UID 5 liegt vor der Grenze — sie beweist keine Weiterleitung."""
    box = MailboxWithStatus([make_message(uid="5")], uidnext=6)
    clock = Clock()
    result = wait_result(box, clock, timeout=20)
    assert result.status == STATUS_FAIL
    assert result.text == (
        f"no mail arrived within 20 s — forward one mail to {ADDRESS} and make sure port "
        "25 is reachable from the internet (openssl s_client -starttls smtp -connect "
        f"{DOMAIN}:25 from another machine)."
    )
    assert clock.sleeps == [selfhost_check.POLL_INTERVAL_SECONDS] * 4


def test_wait_for_mail_sanitizes_subject_and_cuts_it_at_60_characters() -> None:
    raw = (
        b"From: \x1b[31mA <a@example.org>\r\n"
        b"Subject: " + b"x" * 90 + b"\r\n"
        b"\r\n"
        b"Body\r\n"
    )
    box = MailboxWithStatus(uidnext=1)

    def arrive(count: int) -> None:
        if count == 1:
            box.deliver(make_message(raw, uid="1"))

    result = wait_result(box, Clock(on_sleep=arrive))
    assert result.status == STATUS_OK
    assert "\x1b" not in result.text
    assert result.text.endswith(f'— "{"x" * 60}"')


def test_wait_for_mail_fails_when_the_login_failed() -> None:
    box = MailboxWithStatus(
        login_error=MailboxLoginError(("NO", [b"AUTHENTICATIONFAILED"]), "OK")
    )
    result = wait_result(box, Clock())
    assert result.status == STATUS_FAIL


def test_wait_for_mail_fails_when_the_inbox_status_is_refused() -> None:
    box = MailboxWithStatus(status_error=RuntimeError("STATUS refused"))
    result = wait_result(box, Clock())
    assert result.status == STATUS_FAIL


# --- Reihenfolge und Vollständigkeit ---------------------------------------------------------


def test_every_check_runs_even_after_a_failure() -> None:
    """SPEC-CLI.md §4: keine frühe Rückkehr — alle Befunde auf einmal."""
    results = selfhost_check.run_checks(
        make_state(),
        password=PASSWORD,
        probes=probes(resolve=lambda d: [], mx=lambda d: [], smtp_port=closed_port()),
    )
    assert [result.name for result in results] == list(selfhost_check.CHECK_NAMES[:-1])
    assert sum(result.failed for result in results) == 5


def test_without_wait_for_mail_there_is_no_mail_line() -> None:
    results = selfhost_check.run_checks(make_state(), password=PASSWORD, probes=probes())
    assert "Mail" not in [result.name for result in results]


# --- CLI: Zeilenformat, Ausgabe, Exit-Codes (D8, SPEC-CLI.md §2/§4) --------------------------


def run(argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    """Führt ein Kommando über den echten Einstieg aus (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, hooks=Hooks())
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def mailbox_dir(tmp_path: Path) -> Path:
    """Ein erzeugtes Postfach-Verzeichnis mit festen Werten in `state.json`."""
    directory = tmp_path / "selfhost-mail"
    files = selfhost.render_files(
        domain=DOMAIN,
        address=ADDRESS,
        cert_dir=f"/etc/letsencrypt/live/{DOMAIN}",
        out_dir=directory,
        ipv4="203.0.113.7",
        ipv6="2001:db8::7",
    )
    selfhost.write_files(directory, files)
    selfhost.write_state(directory, make_state())
    return directory


@pytest.fixture
def with_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_IMAP_PASSWORD, PASSWORD)


@pytest.fixture
def no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_IMAP_PASSWORD, raising=False)


def use_probes(monkeypatch: pytest.MonkeyPatch, active: Probes) -> None:
    """Hängt die Prüf-Attrappen in die einzige Naht der CLI."""
    monkeypatch.setattr(cli, "_selfhost_probes", lambda ctx: active)


def test_check_prints_the_contract_lines_and_the_ready_block(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    with ScriptedSmtp() as server:
        use_probes(monkeypatch, probes(smtp_port=server.port))
        code, out, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_OK
    assert out.splitlines()[:10] == [
        f"Checking {DOMAIN} (state.json and the network only — no system file is read)",
        f"  DNS A/AAAA     ok      {DOMAIN} -> 203.0.113.7, 2001:db8::7",
        f"  DNS MX         ok      MX 10 {DOMAIN}.",
        f"  SMTP banner    ok      220 {DOMAIN} ESMTP Postfix",
        "  SMTP relay     ok      RCPT TO an outside address refused (550)",
        f"  SMTP recipient ok      RCPT TO {ADDRESS} accepted (250)",
        "  IMAPS cert     ok      chain and host name valid, 84 days left",
        "  IMAPS login    ok      login succeeded, INBOX selectable",
        "  IMAPS 143      ok      cleartext IMAP is closed",
        "All checks passed.",
    ]
    assert "Mirror mailbox ready." in out
    assert f"  IMAP host:   {DOMAIN}" in out
    assert "  Port:        993" in out
    assert f"  Username:    {ADDRESS}" in out
    assert f"  Forward to:  {ADDRESS}" in out
    assert (
        f"Next: maildigest connect-mail --host {DOMAIN} --username {ADDRESS}" in out
    )
    assert "Not proved yet: that a forward from the internet really arrives." in out
    assert err == ""


def test_check_with_wait_for_mail_drops_the_not_proved_note(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    box = MailboxWithStatus(uidnext=1)
    clock = Clock(on_sleep=lambda count: box.deliver(make_message(uid="1")))
    with ScriptedSmtp() as server:
        use_probes(
            monkeypatch,
            probes(
                smtp_port=server.port,
                imap_client=client_factory(box),
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            ),
        )
        code, out, _ = run(
            [
                "selfhost-mail",
                "--check",
                "--wait-for-mail",
                "--timeout",
                "30",
                "--out",
                str(mailbox_dir),
            ]
        )
    assert code == EXIT_OK
    assert '  Mail           ok      from a@example.org — "Test"' in out.splitlines()
    assert "Not proved yet" not in out


def test_a_failed_check_is_exit_1_and_suppresses_the_ready_block(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    use_probes(monkeypatch, probes(resolve=lambda d: [], smtp_port=closed_port()))
    code, out, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_ERROR
    assert "Mirror mailbox ready." not in out
    assert "All checks passed." not in out
    assert err.strip().endswith("Error: 4 of 8 checks failed — see the lines above.")


def test_warnings_go_to_stderr_and_keep_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    with ScriptedSmtp() as server:
        use_probes(
            monkeypatch,
            probes(
                smtp_port=server.port,
                resolve=lambda d: ["127.0.0.1"],
                certificate=lambda h, p: cert_expiring_in(3),
            ),
        )
        code, out, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_OK
    assert "  DNS A/AAAA     ok      " in out
    assert f"{DOMAIN} resolves to a private address (127.0.0.1)" in err
    assert f"Certificate for {DOMAIN} expires in 3 days" in err


def test_skipped_alone_never_changes_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    with ScriptedSmtp() as server:
        use_probes(monkeypatch, probes(smtp_port=server.port, mx=lambda d: None))
        code, out, _ = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_OK
    assert (
        "  DNS MX         skipped install python3-dnspython (Debian) / python3-dns "
        "(Fedora) for the MX check" in out
    )


# --- CLI: das Passwort (dieselbe Regel wie `connect-mail`) ------------------------------------


def test_non_interactive_without_the_variable_is_a_usage_error(
    mailbox_dir: Path, no_password: None
) -> None:
    code, _, err = run(
        ["selfhost-mail", "--check", "--non-interactive", "--out", str(mailbox_dir)]
    )
    assert code == EXIT_USAGE
    assert err.strip() == (
        "Error: --check needs the mailbox password: set MAILDIGEST_IMAP_PASSWORD or run "
        "without --non-interactive."
    )


def test_the_password_is_asked_when_the_variable_is_unset(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, no_password: None
) -> None:
    box = MailboxWithStatus()
    with ScriptedSmtp() as server:
        use_probes(monkeypatch, probes(smtp_port=server.port, imap_client=client_factory(box)))
        code, out, _ = run(
            ["selfhost-mail", "--check", "--out", str(mailbox_dir)], stdin=f"{PASSWORD}\n"
        )
    assert code == EXIT_OK
    assert "Mailbox password (for the login test; not stored): " in out
    assert box.logins == [(ADDRESS, PASSWORD, "INBOX")]


def test_an_empty_password_is_a_usage_error(mailbox_dir: Path, no_password: None) -> None:
    code, _, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)], stdin="\n")
    assert code == EXIT_USAGE
    assert "needs the mailbox password" in err


def test_the_password_never_appears_in_the_output(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    """I5: Es steht weder auf dem Terminal noch in einer Datei des Verzeichnisses."""
    with ScriptedSmtp() as server:
        use_probes(monkeypatch, probes(smtp_port=server.port))
        _, out, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert PASSWORD not in out
    assert PASSWORD not in err
    for path in mailbox_dir.iterdir():
        assert PASSWORD not in path.read_text(encoding="utf-8")


# --- CLI: Zustand und Bedienfehler -----------------------------------------------------------


def test_check_without_a_state_file_is_a_configuration_error(
    tmp_path: Path, with_password: None
) -> None:
    code, _, err = run(["selfhost-mail", "--check", "--out", str(tmp_path / "leer")])
    assert code == EXIT_ERROR
    assert "no state.json in" in err
    assert "maildigest selfhost-mail --domain <your domain> first." in err


def test_check_with_a_broken_state_file_is_a_configuration_error(
    mailbox_dir: Path, with_password: None
) -> None:
    (mailbox_dir / "state.json").write_text('{"version": 2}\n', encoding="utf-8")
    code, _, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_ERROR
    assert "Error: " in err


def test_check_reads_no_system_file(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    """D7: weder `/etc/postfix` noch `/etc/dovecot` noch die Zertifikatsdateien."""
    opened: list[str] = []
    real_open = Path.open

    def watched_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched_open)
    with ScriptedSmtp() as server:
        use_probes(monkeypatch, probes(smtp_port=server.port))
        run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert not [path for path in opened if path.startswith("/etc")]


# --- Hot: was eine feindselige Gegenstelle versucht -------------------------------------------
#
# Der Mailserver auf der anderen Seite ist im Normalfall der eigene — aber `--check` liest
# vom Netz, und die Wartemail kommt aus dem Internet. Diese Fälle sind deshalb die des
# Angreifers: überlange Antworten, Steuerzeichen, ein Dialog, der mittendrin abbricht.


def test_hot_no_check_line_can_be_forged_by_the_counterpart() -> None:
    """Kein Fremdtext darf eine zweite Prüfzeile erfinden — also nie ein Zeilenumbruch."""
    raw = (
        b"From: A <a@example.org>\r\n"
        b'Subject: ok\r\n  IMAPS 143      ok      cleartext IMAP is closed\r\n'
        b"\r\n"
        b"Body\r\n"
    )
    box = MailboxWithStatus(uidnext=1)
    clock = Clock(on_sleep=lambda count: box.deliver(make_message(raw, uid="1")))
    with ScriptedSmtp(banner=f"220 {DOMAIN}\r\n  SMTP relay     ok      forged") as server:
        results = selfhost_check.run_checks(
            make_state(),
            password=PASSWORD,
            wait_for_mail=True,
            timeout=30,
            probes=probes(
                smtp_port=server.port,
                imap_client=client_factory(box),
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            ),
        )
    assert [result.name for result in results] == list(selfhost_check.CHECK_NAMES)
    for result in results:
        assert "\n" not in result.text
        assert "\r" not in result.text


def test_hot_an_endless_smtp_banner_fails_instead_of_hanging() -> None:
    """Eine Antwortzeile jenseits des `smtplib`-Limits ist ein Fehler, kein Hänger."""
    with ScriptedSmtp(banner="220 " + "a" * 9000) as server:
        results = smtp_results(server)
    assert [result.status for result in results.values()] == [STATUS_FAIL] * 3


def test_hot_a_dialogue_that_breaks_off_fails_relay_and_recipient() -> None:
    """Der Server macht nach dem Banner zu: kein Absturz, drei ehrliche Befunde."""

    class DroppingSmtp(ScriptedSmtp):
        def _serve(self) -> None:
            conn, _ = self._sock.accept()
            with conn:
                conn.sendall(f"220 {DOMAIN} ESMTP\r\n".encode())

    with DroppingSmtp() as server:
        results = smtp_results(server)
    assert results["SMTP banner"].status == STATUS_OK
    assert results["SMTP relay"].status == STATUS_FAIL
    assert results["SMTP recipient"].status == STATUS_FAIL


def test_hot_a_mail_without_sender_and_subject_is_still_one_line() -> None:
    box = MailboxWithStatus(uidnext=1)
    clock = Clock(on_sleep=lambda count: box.deliver(make_message(b"\r\nkein Kopf\r\n", uid="1")))
    result = wait_result(box, clock)
    assert result.status == STATUS_OK
    assert "\n" not in result.text


# --- Cold-Tester-Regressionen (tests/cold/REPORT-SELFHOST.md) ---------------------------


def test_ctrl_d_at_the_password_prompt_is_a_handled_abort(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, no_password: None
) -> None:
    """CT-S2: Strg-D am Terminal endete in einem Traceback samt internem Pfad.

    Der Weg ohne Terminal ist längst behandelt; auf einer pty läuft die Abfrage durch
    `getpass`, und dessen `EOFError` fing niemand.
    """
    monkeypatch.setattr(cli, "_isatty", lambda stream: True)
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt="": (_ for _ in ()).throw(EOFError())
    )
    code, _out, err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_USAGE
    assert err.startswith("Error: Input aborted (end of input reached).")
    assert "Traceback" not in err


def test_the_advice_names_the_directory_that_was_used(
    monkeypatch: pytest.MonkeyPatch, mailbox_dir: Path, with_password: None
) -> None:
    """CT-S4: Jede FAIL-Zeile schickte zu `selfhost-mail/…`, auch nach `--out DIR`."""
    use_probes(monkeypatch, probes(resolve=_no_address, mx=lambda domain: []))
    code, out, _err = run(["selfhost-mail", "--check", "--out", str(mailbox_dir)])
    assert code == EXIT_ERROR
    assert f"create the A (and AAAA) record from {mailbox_dir}/dns.txt." in out
    assert f"create the MX record from {mailbox_dir}/dns.txt." in out
    assert f"check myhostname in {mailbox_dir}/postfix.sh" in out


def _no_address(domain: str) -> list[str]:
    """Ein Resolver, der nichts findet."""
    raise OSError("no such host")


def test_an_imap_protocol_error_is_a_fail_line_and_not_a_traceback() -> None:
    """HOT-S3, IMAP-Hälfte: `ImapClient` wandelt nicht jede Fehlerklasse in `IngestError`.

    Ein aus dem Takt geratener Dialog meldet sich als `imaplib.IMAP4.error`; die verließ
    `iter_checks` bis hierher ungefangen.
    """

    class Desynchronised:
        def __init__(self, cfg: Any) -> None:
            del cfg

        def connect(self) -> None:
            raise imaplib.IMAP4.abort("unexpected response")

        def disconnect(self) -> None:  # pragma: no cover - wird nie erreicht
            return None

    results = selfhost_check.run_checks(
        make_state(),
        password=PASSWORD,
        probes=probes(imap_client=Desynchronised),
    )
    login = next(item for item in results if item.name == selfhost_check.CHECK_LOGIN)
    assert login.status == STATUS_FAIL
    assert [item.name for item in results] == list(selfhost_check.CHECK_NAMES[:-1])
