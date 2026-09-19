"""Prüft ein erzeugtes Spiegelpostfach über das Netz (F-ING-4, ADR-089, D7).

Vertrag: docs/SPEC-CLI.md §4 (`maildigest selfhost-mail --check`), Entwurf:
docs/PLAN-SELFHOST-MAIL.md §3. Dieses Modul ist das Gegenstück zu :mod:`maildigest.selfhost`:
Dort entstehen Dateien, hier wird nur beobachtet.

Was dieses Modul **nicht** tut, und zwar mit Absicht:

* **Keine Systemdatei lesen** (D7). Weder `/etc/postfix` noch `/etc/dovecot` noch die
  Zertifikatsdateien. Alles Beobachtete kommt aus `state.json` und vom Netz — geprüft wird
  damit das Verhalten, das die Welt sieht, nicht der Inhalt einer Konfigurationsdatei.
  Eine Prüfung, die `postconf -n` liest, bräuchte außerdem Rechte, die MailDigest nicht hat.
* **Keine Mail zustellen.** Der SMTP-Dialog endet nach den beiden `RCPT TO` mit `RSET` und
  `QUIT`; ein `DATA` wird nie gesendet. Eine Prüfung darf keine Post hinterlassen.
* **Keine zweite IMAP-Implementierung.** Der IMAPS-Teil läuft durch
  :class:`~maildigest.ingest.imap_client.ImapClient` mit demselben
  `ssl.create_default_context()` wie der Dienst (docs/SECURITY.md §6). Ein Zertifikat, das
  hier durchgeht, geht auch im Betrieb durch.
* **Nichts ausgeben und nichts beenden.** Jede Prüfung liefert ein :class:`CheckResult`;
  Zeilenformat, Warnungen und Exit-Code stehen in `cli.py` (PLAN §5 D8, SPEC-CLI.md §2).
* **Nirgends hängen bleiben** — mit einer benannten Ausnahme. Jede Netzoperation hat ein
  Socket-Timeout (SMTP/IMAPS 10 s, die Port-143-Probe 5 s, die MX-Abfrage 5 s);
  `--wait-for-mail` hat zusätzlich die Gesamtfrist aus `--timeout`. Die Ausnahme ist
  :func:`resolve_addresses`: `socket.getaddrinfo` kennt kein Zeitlimit, und ein eigener
  Thread mit Frist ließe die Abfrage im Hintergrund weiterlaufen, ohne etwas zu gewinnen.
  Bei einem schwarzlöchernden Resolver dauert die erste Zeile so lange, wie der Resolver
  der Bibliothek es vorgibt (SPEC-CLI.md §4 sagt dasselbe).

Sicherheits-Design: Das Postfach-Passwort kommt als Argument herein, steht in keiner
Meldung und wird nirgends abgelegt (I5). Jeder Text einer Gegenstelle — SMTP-Banner,
Absender und Betreff der Wartemail — läuft vor der Rückgabe durch
:func:`~maildigest.foreign_text.sanitize_foreign_text` (ADR-055), damit keine
Steuersequenz auf das Terminal gelangt.

Testbarkeit: Alles, was ein Testlauf nicht haben kann (Auflösung, MX-Antwort, Zertifikat,
Ports, Uhr, Schlaf), steckt in :class:`Probes`. Die Vorgaben sind genau die Werte des
Vertrags, sodass der reale Pfad der ist, den die Tests konfigurieren.
"""

from __future__ import annotations

import contextlib
import imaplib
import importlib
import ipaddress
import smtplib
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from maildigest.config import ImapConfig
from maildigest.foreign_text import sanitize_foreign_text
from maildigest.ingest.imap_client import ImapClient, IngestError
from maildigest.selfhost import MirrorState

__all__ = [
    "CHECK_NAMES",
    "STATUS_FAIL",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "CheckResult",
    "Probes",
    "iter_checks",
    "run_checks",
]


# --- Konstanten ------------------------------------------------------------------------

#: Die drei Zustände einer Prüfzeile (SPEC-CLI.md §4, D8). `skipped` ändert nie den
#: Exit-Code — eine fehlende optionale Bibliothek ist kein Fehler des Postfachs.
STATUS_OK = "ok"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "skipped"

#: Die Namen der Prüfungen in der Reihenfolge des Vertrags. `Mail` läuft nur mit
#: `--wait-for-mail`.
CHECK_DNS = "DNS A/AAAA"
CHECK_MX = "DNS MX"
CHECK_SMTP_BANNER = "SMTP banner"
CHECK_SMTP_RELAY = "SMTP relay"
CHECK_SMTP_RECIPIENT = "SMTP recipient"
CHECK_CERT = "IMAPS cert"
CHECK_LOGIN = "IMAPS login"
CHECK_PLAINTEXT = "IMAPS 143"
CHECK_MAIL = "Mail"

CHECK_NAMES = (
    CHECK_DNS,
    CHECK_MX,
    CHECK_SMTP_BANNER,
    CHECK_SMTP_RELAY,
    CHECK_SMTP_RECIPIENT,
    CHECK_CERT,
    CHECK_LOGIN,
    CHECK_PLAINTEXT,
    CHECK_MAIL,
)

#: Socket-Timeouts des Vertrags.
SMTP_TIMEOUT_SECONDS = 10.0
IMAPS_TIMEOUT_SECONDS = 10.0
PLAINTEXT_TIMEOUT_SECONDS = 5.0
DNS_TIMEOUT_SECONDS = 5.0

#: Ab hier gilt ein Zertifikat als „läuft bald ab" — die Warnung ist der Frühwarner für
#: eine still gescheiterte certbot-Erneuerung (PLAN §4).
CERT_WARN_DAYS = 14

#: Abstand zweier INBOX-Abfragen von `--wait-for-mail`.
POLL_INTERVAL_SECONDS = 5.0

#: So viele Zeichen des Betreffs erscheinen in der `Mail`-Zeile.
SUBJECT_MAX_CHARS = 60

#: Deckel für Fremdtexte, die nicht der Betreff sind (Banner, Absender).
_FOREIGN_MAX_CHARS = 120

#: Deckel für Werte aus `state.json`, die in eine Prüfzeile geraten (Adresse, Domain,
#: Verzeichnis). Eine Adresse ist höchstens 254 Zeichen lang, ein Pfad darf länger sein.
_STATE_MAX_CHARS = 300

#: Empfänger der Relay-Probe: eine Adresse außerhalb der Domain. `example.com` ist nach
#: RFC 2606 für genau solche Zwecke reserviert und nimmt nie wirklich Post an.
RELAY_TEST_ADDRESS = "relay-test@example.com"

#: Die SMTP-Probe fragt **localhost**: Ob die Welt Port 25 erreicht, beweist erst eine
#: echte Weiterleitung (`--wait-for-mail`), nichts sonst (PLAN §4).
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25

#: IMAPS und der Klartext-Port, den die erzeugte Dovecot-Konfiguration schließt.
IMAPS_PORT = 993
PLAINTEXT_PORT = 143

#: Der Ordner, in dem MailDigest später liest — und den `--check` deshalb prüft.
INBOX = "INBOX"

#: Was ein SMTP-Dialog an Fehlern werfen darf, ohne dass eine Prüfung abbricht. `ValueError`
#: gehört dazu, weil `smtplib.putcmd` genau die für ein Argument mit Zeilenumbruch wirft —
#: eine Prüfung darf nie werfen, sonst bekommt der Betreiber statt Exit-Code 1 einen
#: Traceback (SPEC-CLI.md §4: „Every check runs and is printed").
_DIALOGUE_ERRORS = (OSError, smtplib.SMTPException, ValueError)

#: Dasselbe für die IMAPS-Anmeldung: `ImapClient.connect()` wandelt nur
#: `ImapToolsError`, `ssl.SSLError` und `OSError` in `IngestError` um; ein aus dem Takt
#: geratener Dialog meldet sich als `imaplib.IMAP4.error` (Oberklasse von `abort`).
_LOGIN_ERRORS = (IngestError, imaplib.IMAP4.error, ssl.SSLError, ValueError, OSError)


# --- Ergebnis --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Das Ergebnis **einer** Prüfung (PLAN §3, SPEC-CLI.md §4).

    `detail` ist die Beobachtung und erscheint bei `ok`; `fix` ist der eine Satz, der bei
    `FAIL` und `skipped` sagt, was zu tun ist. Beides getrennt zu führen statt einen
    fertigen Text zu übergeben, hält die Entscheidung „was zeige ich wann" an einer Stelle
    (:attr:`text`) und macht beide Hälften einzeln prüfbar.

    `warnings` sind Hinweise, die den Status **nicht** ändern (private Adresse, bald
    ablaufendes Zertifikat); die CLI schreibt sie nach stderr.
    """

    name: str
    status: str
    detail: str = ""
    fix: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """Der Text der Prüfzeile: die Beobachtung bei `ok`, sonst der Rat."""
        return self.detail if self.status == STATUS_OK else self.fix

    @property
    def failed(self) -> bool:
        """True, wenn diese Prüfung den Exit-Code auf 1 zieht (D8)."""
        return self.status == STATUS_FAIL


# --- Einzelne Netzoperationen (in :class:`Probes` ersetzbar) ----------------------------


def resolve_addresses(domain: str) -> list[str]:
    """Alle A-/AAAA-Adressen der Domain, Reihenfolge des Resolvers, ohne Duplikate.

    Raises:
        OSError: Der Name löst nicht auf (`socket.gaierror` ist eine Unterklasse).
    """
    found: list[str] = []
    for info in socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM):
        address = str(info[4][0]).split("%", 1)[0]
        if address not in found:
            found.append(address)
    return found


def mx_records(domain: str) -> list[tuple[int, str]] | None:
    """Die MX-Einträge der Domain als (Präferenz, Ziel ohne Schlusspunkt).

    `dnspython` ist **optional** (D4): Die Standardbibliothek kann keine MX-Abfrage, und
    ein selbstgeschriebener DNS-Parser wäre neue Angriffsfläche für einen einzigen
    optionalen Schritt. Der Import geschieht deshalb hier und nicht am Dateianfang —
    fehlt die Bibliothek, wird die Prüfung übersprungen statt zu scheitern.

    Returns:
        Die Einträge, `[]` wenn die Abfrage nichts ergab, und `None`, wenn `dnspython`
        fehlt — nur dieser dritte Fall bedeutet `skipped`.
    """
    try:
        resolver = importlib.import_module("dns.resolver")
    except ImportError:
        return None
    try:
        answer: Any = resolver.resolve(domain, "MX", lifetime=DNS_TIMEOUT_SECONDS)
    except Exception:
        # Jede Fehlerklasse von dnspython (NXDOMAIN, NoAnswer, Timeout, …) bedeutet
        # dasselbe: kein brauchbarer MX. Die Klassen einzeln zu importieren hieße, die
        # optionale Abhängigkeit doch wieder fest zu verdrahten.
        return []
    records: list[tuple[int, str]] = []
    for item in answer:
        exchange = str(getattr(item, "exchange", "")).rstrip(".").lower()
        if exchange:
            records.append((int(getattr(item, "preference", 0)), exchange))
    return records


def peer_certificate(host: str, port: int) -> dict[str, Any]:
    """Das Zertifikat, das `host:port` für **diesen Namen** vorzeigt.

    Derselbe Kontext wie im Betrieb (`create_default_context()`: Kettenprüfung und
    Hostname-Prüfung an, docs/SECURITY.md §6) — ein Zertifikat, das hier durchkommt,
    kommt auch beim Abruf durch.

    Raises:
        ssl.SSLError: Kette, Hostname oder Gültigkeit passen nicht.
        OSError: Der Port ist nicht erreichbar.
    """
    context = ssl.create_default_context()
    with (
        socket.create_connection((host, port), timeout=IMAPS_TIMEOUT_SECONDS) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        certificate = tls.getpeercert()
    if not certificate:
        raise ssl.SSLError(f"{host}:{port} presented no certificate")
    return dict(certificate)


def plaintext_banner(host: str, port: int, timeout: float) -> bytes:
    """Was `host:port` unaufgefordert sendet — leer, wenn nichts kommt oder niemand da ist.

    Genau das ist die Prüfung „143 ist zu": Ein abgelehnter Verbindungsversuch und ein
    stummer Port sind beide in Ordnung, ein IMAP-Banner ist es nicht.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                return bytes(sock.recv(128))
            except OSError:
                return b""
    except OSError:
        return b""


def _utc_now() -> datetime:
    """Die aktuelle Zeit in UTC (ersetzbar, damit Restlaufzeiten prüfbar sind)."""
    return datetime.now(UTC)


def _imap_client(cfg: ImapConfig) -> ImapClient:
    """Der IMAPS-Client der Prüfung — mit dem Socket-Timeout des Vertrags (10 s)."""
    return ImapClient(cfg, timeout=IMAPS_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class Probes:
    """Die Außenwelt der Prüfungen, gebündelt und ersetzbar.

    Die Vorgaben sind die Werte des Vertrags; ein Test setzt einzelne Felder um und läuft
    damit durch denselben Code wie ein echter Lauf. `smtp_host`/`plaintext_host` sind
    getrennt, weil beide Proben gegen einen geskripteten Socket auf einem flüchtigen Port
    laufen müssen — `plaintext_host = None` heißt „die Domain aus `state.json`".

    Hinweis zu den Callable-Feldern: Sie werden im `__init__` als *Instanz*-Attribute
    gesetzt, deshalb bindet Python sie nicht als Methoden.
    """

    smtp_host: str = SMTP_HOST
    smtp_port: int = SMTP_PORT
    imaps_port: int = IMAPS_PORT
    plaintext_host: str | None = None
    plaintext_port: int = PLAINTEXT_PORT
    plaintext_timeout: float = PLAINTEXT_TIMEOUT_SECONDS
    resolve: Callable[[str], list[str]] = resolve_addresses
    mx: Callable[[str], list[tuple[int, str]] | None] = mx_records
    certificate: Callable[[str, int], dict[str, Any]] = peer_certificate
    banner: Callable[[str, int, float], bytes] = plaintext_banner
    imap_client: Callable[[ImapConfig], ImapClient] = _imap_client
    now: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


def _from_state(value: str) -> str:
    """Ein Wert aus `state.json`, bevor er in eine Prüfzeile geht (ADR-055, HC-4).

    `read_state` lässt nur die Adresse `mirror-<hex>@<domain>` und geprüfte Pfade durch —
    aber jede Prüfzeile landet auf einem Terminal, und die zweite Schranke ist genau die,
    die stehen bleibt, wenn die erste einmal still wegfällt. Dieselbe Allowlist wie für
    Banner und Betreff.
    """
    return sanitize_foreign_text(value, max_chars=_STATE_MAX_CHARS)


def _file(state: MirrorState, name: str) -> str:
    """Eine erzeugte Datei so benannt, wie sie wirklich liegt (CT-S4).

    Der Ratschlag schickt den Betreiber zu einer Datei; hieße sie hier immer
    `selfhost-mail/postfix.sh`, zeigte er bei `--out DIR` auf etwas, das es nicht gibt.
    """
    return _from_state(f"{state.directory.rstrip('/')}/{name}")


# --- DNS -------------------------------------------------------------------------------


#: Netze, aus denen das Internet nicht zustellen kann. Bewusst **nicht** `is_global`:
#: Das meldet auch die Dokumentationsbereiche (192.0.2.0/24, 203.0.113.0/24, 2001:db8::/32)
#: als nicht erreichbar, und genau die stehen in jedem Beispiel des Vertrags. Der Vertrag
#: nennt drei Fälle — Loopback, Link-Local, privat —, und die stehen hier.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-Grade-NAT: dasselbe Problem
    ipaddress.ip_network("fc00::/7"),
)


def _is_global(address: str) -> bool:
    """True, wenn das Internet an diese Adresse zustellen könnte."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - getaddrinfo liefert immer gültige Adressen
        return False
    if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
        return False
    return not any(
        parsed in network
        for network in _PRIVATE_NETWORKS
        if network.version == parsed.version
    )


def check_dns(state: MirrorState, probes: Probes) -> CheckResult:
    """`DNS A/AAAA`: Die Domain löst überhaupt auf (SPEC-CLI.md §4).

    Eine private, Loopback- oder Link-Local-Adresse bleibt `ok` mit einer Warnung: Genau
    so bildet die Container-Probe aus PLAN §7 die Domain auf 127.0.0.1 ab. Wer wirklich
    Post aus dem Internet erwartet, liest die Warnung.
    """
    domain = _from_state(state.domain)
    try:
        addresses = probes.resolve(state.domain)
    except OSError:
        addresses = []
    if not addresses:
        return CheckResult(
            CHECK_DNS,
            STATUS_FAIL,
            fix=(
                f"{domain} does not resolve — create the A (and AAAA) record from "
                f"{_file(state, 'dns.txt')}."
            ),
        )
    warnings = tuple(
        f"{domain} resolves to a private address ({address}) — the internet cannot "
        "deliver there."
        for address in addresses
        if not _is_global(address)
    )
    return CheckResult(
        CHECK_DNS,
        STATUS_OK,
        detail=f"{domain} -> {', '.join(addresses)}",
        warnings=warnings,
    )


def check_mx(state: MirrorState, probes: Probes) -> CheckResult:
    """`DNS MX`: Ein MX der Domain nennt die Domain selbst — oder `skipped` ohne dnspython."""
    domain = _from_state(state.domain)
    records = probes.mx(state.domain)
    if records is None:
        return CheckResult(
            CHECK_MX,
            STATUS_SKIPPED,
            fix=(
                "install python3-dnspython (Debian) / python3-dns (Fedora) for the MX check"
            ),
        )
    match = next((record for record in records if record[1] == state.domain), None)
    if match is None:
        return CheckResult(
            CHECK_MX,
            STATUS_FAIL,
            fix=(
                f"the MX of {domain} does not point at {domain} — create the MX record "
                f"from {_file(state, 'dns.txt')}."
            ),
        )
    return CheckResult(CHECK_MX, STATUS_OK, detail=f"MX {match[0]} {domain}.")


# --- SMTP ------------------------------------------------------------------------------


def _relay_fix(state: MirrorState) -> str:
    """Der eine Satz zur Relay-Zeile — er nennt die Datei, die wirklich erzeugt wurde."""
    return (
        "the server accepted mail for an outside address — that is an open relay; reapply "
        f"{_file(state, 'postfix.sh')}, which sets smtpd_relay_restrictions."
    )


def _recipient_fix(state: MirrorState) -> str:
    """Der eine Satz zur Empfängerzeile."""
    return (
        "the server refuses its own mirror address — check virtual_mailbox_maps in "
        f"{_file(state, 'postfix.sh')} and reload Postfix."
    )


def _smtp_failures(state: MirrorState, banner: CheckResult) -> list[CheckResult]:
    """Alle drei Zeilen, wenn der Dialog nicht zustande kam oder mittendrin abriss."""
    return [
        banner,
        CheckResult(CHECK_SMTP_RELAY, STATUS_FAIL, fix=_relay_fix(state)),
        CheckResult(CHECK_SMTP_RECIPIENT, STATUS_FAIL, fix=_recipient_fix(state)),
    ]


def check_smtp(state: MirrorState, probes: Probes) -> list[CheckResult]:
    """Die drei SMTP-Prüfungen in **einem** Dialog (SPEC-CLI.md §4).

    Ablauf: Banner lesen, `EHLO <domain>`, `MAIL FROM:<probe@domain>`, die beiden
    `RCPT TO`, `RSET`, `QUIT`. Ein `DATA` gibt es nicht — die Prüfung stellt nichts zu und
    hinterlässt nichts in der Warteschlange. `RSET` und `QUIT` laufen auch dann, wenn
    zwischendrin etwas schiefgeht; ein halb offener Dialog wäre eine wartende Mail.
    """
    domain, address = state.domain, state.address
    shown = _from_state(domain)
    banner_fix = (
        f"nothing answers on {probes.smtp_host}:{probes.smtp_port} with a banner for "
        f"{shown} — check myhostname in {_file(state, 'postfix.sh')} and that Postfix is "
        "running."
    )
    session = smtplib.SMTP(timeout=SMTP_TIMEOUT_SECONDS, local_hostname=domain)
    try:
        code, raw = session.connect(probes.smtp_host, probes.smtp_port)
    except _DIALOGUE_ERRORS:
        return _smtp_failures(
            state, CheckResult(CHECK_SMTP_BANNER, STATUS_FAIL, fix=banner_fix)
        )

    greeting = sanitize_foreign_text(
        raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw),
        max_chars=_FOREIGN_MAX_CHARS,
    )
    if code == 220 and domain in greeting.lower():
        banner = CheckResult(CHECK_SMTP_BANNER, STATUS_OK, detail=f"{code} {greeting}")
    else:
        banner = CheckResult(CHECK_SMTP_BANNER, STATUS_FAIL, fix=banner_fix)

    try:
        return [banner, *_smtp_recipients(session, state, domain=domain, address=address)]
    except _DIALOGUE_ERRORS:
        return _smtp_failures(state, banner)
    finally:
        _smtp_close(session)


def _smtp_recipients(
    session: smtplib.SMTP, state: MirrorState, *, domain: str, address: str
) -> list[CheckResult]:
    """`MAIL FROM` und die beiden `RCPT TO` — der eigentliche Beweis (kein `DATA`)."""
    session.ehlo(domain)
    mail_code, _ = session.mail(f"probe@{domain}")
    if mail_code != 250:
        # Ohne angenommenen Absender sagt jede folgende Antwort nichts über die
        # Empfängerregeln aus (ein `503 bad sequence` sähe wie eine Relay-Verweigerung
        # aus). Beide Prüfungen scheitern hier ehrlich statt falsch zu bestehen.
        return [
            CheckResult(CHECK_SMTP_RELAY, STATUS_FAIL, fix=_relay_fix(state)),
            CheckResult(CHECK_SMTP_RECIPIENT, STATUS_FAIL, fix=_recipient_fix(state)),
        ]
    relay_code, _ = session.rcpt(RELAY_TEST_ADDRESS)
    rcpt_code, _ = session.rcpt(address)
    relay = (
        CheckResult(
            CHECK_SMTP_RELAY,
            STATUS_OK,
            detail=f"RCPT TO an outside address refused ({relay_code})",
        )
        if 500 <= relay_code <= 599
        else CheckResult(CHECK_SMTP_RELAY, STATUS_FAIL, fix=_relay_fix(state))
    )
    recipient = (
        CheckResult(
            CHECK_SMTP_RECIPIENT,
            STATUS_OK,
            detail=f"RCPT TO {_from_state(address)} accepted ({rcpt_code})",
        )
        if rcpt_code == 250
        else CheckResult(CHECK_SMTP_RECIPIENT, STATUS_FAIL, fix=_recipient_fix(state))
    )
    return [relay, recipient]


def _smtp_close(session: smtplib.SMTP) -> None:
    """`RSET` und `QUIT`, komme was wolle — danach wartet garantiert keine Mail."""
    for step in (session.rset, session.quit):
        try:
            step()
        except _DIALOGUE_ERRORS:
            continue
    with contextlib.suppress(OSError):  # close() wirft praktisch nie
        session.close()


# --- IMAPS -----------------------------------------------------------------------------


def check_certificate(state: MirrorState, probes: Probes) -> CheckResult:
    """`IMAPS cert`: Kette, Hostname und Restlaufzeit des Zertifikats auf 993."""
    domain = _from_state(state.domain)
    fix = (
        f"the certificate of {domain}:{probes.imaps_port} is not valid for this host — "
        f"run certbot certonly --standalone -d {domain} and reload Dovecot."
    )
    try:
        certificate = probes.certificate(state.domain, probes.imaps_port)
        expires_at = ssl.cert_time_to_seconds(str(certificate.get("notAfter", "")))
    except (ssl.SSLError, OSError, ValueError):
        return CheckResult(CHECK_CERT, STATUS_FAIL, fix=fix)
    days_left = int((expires_at - probes.now().timestamp()) // 86400)
    if days_left < 0:
        return CheckResult(CHECK_CERT, STATUS_FAIL, fix=fix)
    warnings = (
        (
            f"Certificate for {domain} expires in {days_left} days — check the certbot "
            "timer.",
        )
        if days_left < CERT_WARN_DAYS
        else ()
    )
    return CheckResult(
        CHECK_CERT,
        STATUS_OK,
        detail=f"chain and host name valid, {days_left} days left",
        warnings=warnings,
    )


def _open_mailbox(
    state: MirrorState, probes: Probes, *, password: str
) -> tuple[CheckResult, ImapClient | None]:
    """`IMAPS login`: anmelden und INBOX auswählen — beides in einem Schritt.

    `ImapClient.connect()` meldet sich mit `initial_folder=INBOX` an; gelingt das, sind
    Anmeldung **und** Auswählbarkeit des Ordners bewiesen. Der offene Client wird
    zurückgegeben, weil `--wait-for-mail` ihn gleich weiterbenutzt.
    """
    fix = (
        f"the login for {_from_state(state.address)} failed — read the IMAPS cert line "
        "first; if that one is ok, rerun apply.sh to set the mailbox password."
    )
    cfg = ImapConfig(
        host=state.domain,
        port=probes.imaps_port,
        username=state.address,
        password=SecretStr(password),
        folder=INBOX,
    )
    try:
        client = probes.imap_client(cfg)
        client.connect()
    except _LOGIN_ERRORS:
        # Die Meldung des Clients nennt nie das Passwort (I5); sie nennt aber Host und
        # Port und hilft hier nicht weiter — der eine Satz des Vertrags reicht.
        return CheckResult(CHECK_LOGIN, STATUS_FAIL, fix=fix), None
    return (
        CheckResult(CHECK_LOGIN, STATUS_OK, detail="login succeeded, INBOX selectable"),
        client,
    )


def check_plaintext_port(state: MirrorState, probes: Probes) -> CheckResult:
    """`IMAPS 143`: Auf dem Klartext-Port darf kein IMAP-Server antworten."""
    host = probes.plaintext_host or state.domain
    data = probes.banner(host, probes.plaintext_port, probes.plaintext_timeout)
    if not data:
        return CheckResult(CHECK_PLAINTEXT, STATUS_OK, detail="cleartext IMAP is closed")
    return CheckResult(
        CHECK_PLAINTEXT,
        STATUS_FAIL,
        fix=(
            f"cleartext IMAP on {_from_state(state.domain)}:{probes.plaintext_port} is "
            f"open — the port = 0 line from {_file(state, 'dovecot.conf')} is missing; "
            "reapply it and reload Dovecot."
        ),
    )


# --- Wartemail -------------------------------------------------------------------------


def _uidnext(client: ImapClient) -> int:
    """Die nächste zu vergebende UID der INBOX — die Grenze zwischen alt und neu.

    `UIDNEXT` statt „Zahl der Mails": Eine Mail, die während des Wartens gelöscht oder
    verschoben wird, würde einen Zähler wieder sinken lassen; UIDNEXT steigt nur.

    Raises:
        OSError: Der Server beantwortet das `STATUS`-Kommando nicht.
    """
    try:
        status = client.mailbox.folder.status(INBOX, ["UIDNEXT"])
        return int(status["UIDNEXT"])
    except Exception as exc:  # imap-tools und imaplib melden eigene Fehlerklassen
        raise OSError(f"INBOX status failed: {type(exc).__name__}") from exc


def _new_mail_line(client: ImapClient, baseline: int) -> str | None:
    """Absender und Betreff der neuesten Mail ab `baseline` — `None`, wenn keine da ist.

    Gelesen wird ohne jede Veränderung: `mark_seen=False`, kein Flag, kein Verschieben.
    """
    try:
        uids = sorted(
            uid
            for uid in (_as_uid(raw) for raw in client.mailbox.uids())
            if uid is not None and uid >= baseline
        )
    except Exception:  # Verbindungs- oder Protokollfehler: gleich noch einmal versuchen
        return None
    if not uids:
        return None
    try:
        messages = list(
            client.mailbox.fetch(uid_list=[str(uids[-1])], mark_seen=False, bulk=False)
        )
    except Exception:
        return None
    if not messages:
        return None
    message = messages[0]
    sender = sanitize_foreign_text(
        str(message.from_ or "(unknown sender)"), max_chars=_FOREIGN_MAX_CHARS
    )
    subject = sanitize_foreign_text(str(message.subject or ""), max_chars=SUBJECT_MAX_CHARS)
    return f'from {sender} — "{subject}"'


def _as_uid(raw: object) -> int | None:
    """Eine UID als Zahl; alles Unerwartete zählt als „keine UID"."""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def check_mail(
    state: MirrorState, probes: Probes, client: ImapClient | None, *, timeout: int
) -> CheckResult:
    """`Mail`: Wartet auf eine wirklich weitergeleitete Mail (der End-zu-End-Beweis).

    Vor dem Warten wird `UIDNEXT` festgehalten; was schon im Postfach liegt, zählt nicht.
    Danach wird alle fünf Sekunden nachgesehen, bis die Frist aus `--timeout` abläuft.
    """
    fix = (
        f"no mail arrived within {timeout} s — forward one mail to "
        f"{_from_state(state.address)} and make sure port 25 is reachable from the "
        "internet (openssl s_client -starttls smtp -connect "
        f"{_from_state(state.domain)}:25 from another machine)."
    )
    if client is None:
        return CheckResult(CHECK_MAIL, STATUS_FAIL, fix=fix)
    try:
        baseline = _uidnext(client)
    except OSError:
        return CheckResult(CHECK_MAIL, STATUS_FAIL, fix=fix)
    deadline = probes.monotonic() + timeout
    while True:
        remaining = deadline - probes.monotonic()
        if remaining <= 0:
            return CheckResult(CHECK_MAIL, STATUS_FAIL, fix=fix)
        probes.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        line = _new_mail_line(client, baseline)
        if line is not None:
            return CheckResult(CHECK_MAIL, STATUS_OK, detail=line)


# --- Ablauf ----------------------------------------------------------------------------


def iter_checks(
    state: MirrorState,
    *,
    password: str,
    wait_for_mail: bool = False,
    timeout: int = 600,
    probes: Probes | None = None,
) -> Iterator[CheckResult]:
    """Führt alle Prüfungen in der Reihenfolge des Vertrags aus und liefert sie einzeln.

    Ein Generator, weil die Zeilen erscheinen sollen, sobald sie feststehen — `--check
    --wait-for-mail` steht sonst bis zu zehn Minuten stumm da. **Keine** Prüfung bricht
    die Folge ab: Wer eine kaputte Kette repariert, will alle Befunde auf einmal sehen,
    nicht einen pro Lauf (SPEC-CLI.md §4).

    Args:
        state: Der gelesene `state.json`-Inhalt.
        password: Postfach-Passwort für die Anmeldeprobe; wird nirgends abgelegt (I5).
        wait_for_mail: Zusätzlich auf eine echte Weiterleitung warten.
        timeout: Frist dafür in Sekunden.
        probes: Ersatz für die Netzoperationen (Tests, Container-Probe).
    """
    active = probes if probes is not None else Probes()
    yield check_dns(state, active)
    yield check_mx(state, active)
    yield from check_smtp(state, active)
    yield check_certificate(state, active)
    login, client = _open_mailbox(state, active, password=password)
    yield login
    try:
        yield check_plaintext_port(state, active)
        if wait_for_mail:
            yield check_mail(state, active, client, timeout=timeout)
    finally:
        if client is not None:
            client.disconnect()


def run_checks(
    state: MirrorState,
    *,
    password: str,
    wait_for_mail: bool = False,
    timeout: int = 600,
    probes: Probes | None = None,
) -> list[CheckResult]:
    """Wie :func:`iter_checks`, aber als fertige Liste (für Tests und Aufrufer ohne Ausgabe)."""
    return list(
        iter_checks(
            state,
            password=password,
            wait_for_mail=wait_for_mail,
            timeout=timeout,
            probes=probes,
        )
    )
