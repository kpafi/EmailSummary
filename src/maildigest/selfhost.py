"""Erzeugt die Dateien für ein selbst gehostetes Spiegelpostfach (F-ING-4, ADR-089).

Vertrag: docs/SPEC-CLI.md §4 (`maildigest selfhost-mail`), Entwurf und Begründung:
docs/PLAN-SELFHOST-MAIL.md §3 und §5.

Dieses Modul ist **rein**: Es erzeugt die zufällige Adresse, prüft die Domain, füllt die
Vorlagen aus `data/selfhost/` und schreibt das Ausgabeverzeichnis samt `state.json`. Es
öffnet keine Netzverbindung und liest keine Systemdatei — die Prüfungen von `--check`
stehen in `selfhost_check.py`, und die beiden lokalen IP-Adressen für `dns.txt` bestimmt
die CLI und reicht sie hier herein (SPEC-CLI.md §4, PLAN §6).

Sicherheits-Design:

* **I5:** Hier entsteht kein Geheimnis. Das erzeugte Verzeichnis darf aufgehoben,
  eingecheckt oder verschickt werden; das Postfach-Passwort fragt erst `apply.sh` ab und
  legt nur dessen BLF-CRYPT-Hash in `/etc/dovecot/users` ab.
* **Shell-Sicherheit:** Domain, Adresse und Zertifikatsverzeichnis laufen durch eine
  strenge Zeichen-Allowlist, **bevor** sie eine Vorlage erreichen (:func:`validate_domain`,
  :func:`validate_cert_dir`); die Adresse entsteht ohnehin nur aus einer geprüften Domain
  und acht Hex-Ziffern. Zusätzlich steht jeder eingesetzte Wert in `apply.sh` und
  `postfix.sh` in einfachen Anführungszeichen — zwei Schranken, weil eine davon still
  wegfallen könnte.
* **Determinismus:** Gleiche Eingaben (Domain, Adresse, Zertifikatsverzeichnis,
  Ausgabeverzeichnis, IP-Adressen) ergeben byteweise dieselben Dateien. Nur
  :func:`generate_address` und der Zeitstempel in `state.json` sind nicht deterministisch,
  und beide sind Argumente, keine versteckten Aufrufe.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

__all__ = [
    "DIR_MODE",
    "FILE_MODES",
    "FILE_ORDER",
    "IPV4_PLACEHOLDER",
    "STATE_FILENAME",
    "STATE_VERSION",
    "DomainError",
    "MirrorState",
    "SelfhostError",
    "default_cert_dir",
    "default_output_dir",
    "dns_records",
    "generate_address",
    "next_steps",
    "read_state",
    "render_files",
    "validate_cert_dir",
    "validate_domain",
    "write_files",
    "write_state",
]


# --- Konstanten ------------------------------------------------------------------------

#: Name des Ausgabeverzeichnisses neben der Konfigurationsdatei (SPEC-CLI.md §4, D5).
DEFAULT_DIR_NAME = "selfhost-mail"

#: Dateiname des Zustands; seine Anwesenheit bedeutet „hier liegt schon ein Postfach".
STATE_FILENAME = "state.json"

#: Schema-Version von `state.json`. Eine andere Version ist ein Konfigurationsfehler.
STATE_VERSION = 1

#: Die fünf erzeugten Dateien in der Reihenfolge, in der sie dokumentiert sind.
FILE_ORDER = ("dns.txt", "postfix.sh", "dovecot.conf", "apply.sh", "checklist.txt")

#: Dateirechte je erzeugter Datei: ausführbar nur, was ausgeführt wird.
FILE_MODES: dict[str, int] = {
    "dns.txt": 0o600,
    "postfix.sh": 0o700,
    "dovecot.conf": 0o600,
    "apply.sh": 0o700,
    "checklist.txt": 0o600,
    STATE_FILENAME: 0o600,
}

#: Rechte des Ausgabeverzeichnisses.
DIR_MODE = 0o700

#: Vorgabe für Postfix' `message_size_limit`, wenn keine Konfiguration etwas anderes sagt —
#: derselbe Wert wie `[limits] max_mail_bytes` in `config.py` (25 MiB).
DEFAULT_MAX_MAIL_BYTES = 25 * 1024 * 1024

#: Platzhalter in `dns.txt`, wenn der Host keine globale IPv4-Adresse hat.
IPV4_PLACEHOLDER = "<the server's public IPv4 address>"

#: Kommentarzeile in `dns.txt`, wenn der Host keine globale IPv6-Adresse hat.
NO_IPV6_COMMENT = "# no global IPv6 address on this host — leave the AAAA record out"

#: Höchstlänge eines Hostnamens und einer Marke (RFC 1035).
_MAX_NAME_CHARS = 253
_MAX_LABEL_CHARS = 63

#: Erlaubte Marke: ASCII, keine führenden/abschließenden Bindestriche.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

#: Erlaubte Zeichen eines Zertifikatsverzeichnisses — bewusst eng, weil der Wert in
#: erzeugte Shell-Skripte und in die Dovecot-Konfiguration eingesetzt wird.
_CERT_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")

#: Zufälliger lokaler Teil: `mirror-` plus acht Hex-Ziffern (ADR-089). Der lokale Teil ist
#: die **ganze** Erlaubnis: Was `read_state` annimmt, geht später unverändert in den
#: SMTP-Dialog, in den IMAP-`LOGIN` und auf das Terminal.
_LOCAL_PART_RE = re.compile(r"^mirror-[0-9a-f]{8}$")

#: Platzhalter einer Vorlage: `{{name}}`.
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")

#: Erlaubte Schlüssel in `state.json`; ein unbekannter Schlüssel ist ein Fehler.
_STATE_KEYS = ("address", "cert_dir", "domain", "generated_at", "version")


class SelfhostError(Exception):
    """Das Spiegelpostfach lässt sich nicht erzeugen oder sein Zustand nicht lesen."""


class DomainError(SelfhostError):
    """Eine Domain verletzt die Regeln aus SPEC-CLI.md §4.

    Die Meldung ist **wörtlich** die des Vertrags, inklusive des Optionsnamens: Die CLI
    setzt nur `Error: ` davor. `reason` erlaubt einem Aufrufer (dem Zustandsleser), eine
    eigene Einbettung zu wählen, ohne die Meldung zu zerlegen.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        """Args: message: Vertragstext; reason: `idn` | `apex` | `syntax`."""
        super().__init__(message)
        self.reason = reason


# --- Domain, Adresse, Zertifikatsverzeichnis -------------------------------------------


def validate_domain(value: str, *, allow_apex: bool = False) -> str:
    """Normalisiert eine Domain und prüft sie (SPEC-CLI.md §4 „Domain rules").

    Normalisiert wird ein abschließender Punkt weg und ASCII in Kleinschreibung; geprüft
    werden Zeichensatz (nur ASCII, kein Punycode), Markenlänge und Markenzahl.

    Args:
        value: Der eingegebene Wert.
        allow_apex: Zwei Marken genügen (sonst sind drei nötig).

    Returns:
        Die normalisierte Domain.

    Raises:
        DomainError: Der Wert verletzt eine der Regeln; die Meldung ist der Vertragstext.
    """
    raw = value.strip()
    normalized = raw.removesuffix(".").lower()
    labels = normalized.split(".")

    # `raw` **und** `normalized`: `str.lower()` faltet Nicht-ASCII nach ASCII (U+212A
    # KELVIN SIGN wird zu `k`), eine Prüfung erst danach ließe genau das durch.
    if (
        not raw.isascii()
        or not normalized.isascii()
        or any(label.startswith("xn--") for label in labels)
    ):
        raise DomainError(
            f'--domain: only ASCII host names, no IDN (got "{raw}").', reason="idn"
        )
    if (
        not normalized
        or len(normalized) > _MAX_NAME_CHARS
        or any(len(label) > _MAX_LABEL_CHARS for label in labels)
        or not all(_LABEL_RE.match(label) for label in labels)
    ):
        raise DomainError(
            f'--domain: not a valid host name (got "{raw}").', reason="syntax"
        )
    minimum = 2 if allow_apex else 3
    if len(labels) < minimum:
        if len(labels) == 2:
            raise DomainError(
                f'--domain: "{normalized}" is an apex domain — use a subdomain such as '
                f"mirror.{normalized}, or pass --allow-apex.",
                reason="apex",
            )
        raise DomainError(
            f'--domain: not a valid host name (got "{raw}").', reason="syntax"
        )
    return normalized


def generate_address(domain: str) -> str:
    """Erzeugt die Postfachadresse `mirror-<8 Hex>@<domain>` (ADR-089).

    Der zufällige lokale Teil ist die Spam-Abwehr: Die Adresse steht in keinem Verzeichnis
    und lässt sich nicht raten. `secrets` statt `random`, weil genau das der Zweck ist.
    """
    return f"mirror-{secrets.token_hex(4)}@{domain}"


def validate_cert_dir(value: str) -> str:
    """Prüft das Zertifikatsverzeichnis, bevor es in eine Vorlage gelangt.

    Ein absoluter Pfad aus Buchstaben, Ziffern, `.`, `_`, `-` und `/`; ein abschließender
    Schrägstrich fällt weg, weil die Vorlagen `{{cert_dir}}/fullchain.pem` schreiben.

    Raises:
        SelfhostError: Kein absoluter Pfad oder ein unerlaubtes Zeichen.
    """
    cleaned = value.strip().rstrip("/") or "/"
    if not _CERT_DIR_RE.match(cleaned) or ".." in cleaned.split("/"):
        raise SelfhostError(
            f'--cert-dir: needs an absolute path without unusual characters (got "{value}").'
        )
    return cleaned


def default_cert_dir(domain: str) -> str:
    """Der Ort, an den `certbot certonly` das Zertifikat der Domain legt."""
    return f"/etc/letsencrypt/live/{domain}"


def default_output_dir(config_path: Path) -> Path:
    """Das Ausgabeverzeichnis neben der Konfigurationsdatei (D5)."""
    return config_path.expanduser().resolve().parent / DEFAULT_DIR_NAME


# --- Zustand ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirrorState:
    """Der Inhalt von `state.json`: was `--check` über das Postfach wissen muss.

    Bewusst ohne Passwortfeld (I5, D6) — `--check` fragt das Passwort ab oder nimmt es aus
    `MAILDIGEST_IMAP_PASSWORD`.
    """

    domain: str
    address: str
    cert_dir: str
    generated_at: str
    #: Das Verzeichnis, aus dem dieser Zustand stammt — steht in keiner `state.json`, aber
    #: in jedem Ratschlag von `--check` („… from <dir>/dns.txt"). Ohne das Feld nennen die
    #: Ratschläge `selfhost-mail/…` auch dann, wenn `--out` woandershin zeigte (CT-S4).
    #: `compare=False`, weil es nicht zum Inhalt gehört: Derselbe Zustand aus zwei
    #: Verzeichnissen ist derselbe Zustand.
    directory: str = field(default=DEFAULT_DIR_NAME, compare=False)

    @property
    def local_part(self) -> str:
        """Der lokale Teil der Adresse (vor dem `@`)."""
        return self.address.split("@", 1)[0]

    def to_json(self) -> str:
        """Rendert den Zustand: Schlüssel sortiert, zwei Leerzeichen, Zeilenumbruch."""
        payload = {
            "address": self.address,
            "cert_dir": self.cert_dir,
            "domain": self.domain,
            "generated_at": self.generated_at,
            "version": STATE_VERSION,
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def now_stamp(moment: datetime | None = None) -> str:
    """UTC-Zeitstempel für `state.json`: ISO 8601, Sekundengenauigkeit, Suffix `Z`."""
    current = moment if moment is not None else datetime.now(UTC)
    return current.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_state(directory: Path, state: MirrorState) -> Path:
    """Schreibt `state.json` mit Dateirechten 0600 und liefert seinen Pfad."""
    path = directory / STATE_FILENAME
    _write_file(path, state.to_json(), FILE_MODES[STATE_FILENAME])
    return path


def read_state(directory: Path) -> MirrorState:
    """Liest und prüft `state.json` aus dem Ausgabeverzeichnis.

    Geprüft werden Lesbarkeit, JSON, der geschlossene Schlüsselsatz, die Version und die
    Domainregeln; jede Verletzung ist ein Konfigurationsfehler (Exit-Code 1).

    Raises:
        SelfhostError: Datei fehlt, ist unlesbar oder ihr Inhalt ist ungültig.
    """
    path = directory / STATE_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SelfhostError(
            f"no {STATE_FILENAME} in {directory} — run maildigest selfhost-mail "
            "--domain <your domain> first."
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise SelfhostError(f"{path} cannot be read: {exc}.") from exc

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SelfhostError(f"{path} is not valid JSON: {exc.msg}.") from exc
    if not isinstance(data, dict):
        raise SelfhostError(f"{path} does not hold a JSON object.")

    unknown = sorted(set(data) - set(_STATE_KEYS))
    if unknown:
        raise SelfhostError(f"{path} carries unknown keys: {', '.join(unknown)}.")
    missing = sorted(set(_STATE_KEYS) - set(data))
    if missing:
        raise SelfhostError(f"{path} is missing the keys: {', '.join(missing)}.")
    if data["version"] != STATE_VERSION:
        raise SelfhostError(
            f"{path} has version {data['version']!r}, but this MailDigest writes "
            f"version {STATE_VERSION}."
        )
    for key in ("address", "cert_dir", "domain", "generated_at"):
        if not isinstance(data[key], str) or not data[key]:
            raise SelfhostError(f"{path}: `{key}` has to be a non-empty string.")

    try:
        domain = validate_domain(str(data["domain"]), allow_apex=True)
    except DomainError as exc:
        raise SelfhostError(
            f"{path}: `domain` breaks the domain rules ({exc.reason}): {data['domain']!r}."
        ) from exc
    address = str(data["address"])
    # Ganz prüfen, nicht nur vorn und hinten: Zwischen `mirror-<hex>@` und `@<domain>`
    # stünde sonst beliebiger Text — CR/LF für einen zweiten SMTP-Befehl, ESC für das
    # Terminal, Kilobytes für den IMAP-`LOGIN`. Die Domain ist hier bereits geprüft.
    local_part, _, rest = address.partition("@")
    if not _LOCAL_PART_RE.match(local_part) or rest != domain:
        raise SelfhostError(
            f"{path}: `address` is not a mirror address of {domain} (got {address!r})."
        )
    return MirrorState(
        domain=domain,
        address=address,
        cert_dir=validate_cert_dir(str(data["cert_dir"])),
        generated_at=str(data["generated_at"]),
        directory=str(directory),
    )


# --- Vorlagen --------------------------------------------------------------------------


def _template(name: str) -> str:
    """Liest eine Vorlage aus den Paketdaten (`data/selfhost/<name>.tmpl`)."""
    return (
        resources.files("maildigest")
        .joinpath(f"data/selfhost/{name}.tmpl")
        .read_text(encoding="utf-8")
    )


def _fill(template: str, values: dict[str, str]) -> str:
    """Ersetzt jedes `{{schlüssel}}` und besteht darauf, dass keines übrig bleibt.

    Geprüft wird die **Vorlage**, nicht das Ergebnis: Ein eingesetzter Wert darf `{{`
    enthalten (ein Ausgabeverzeichnis `/srv/{{domain}}` ist ein zulässiger Name), und
    nachgeschaut wird ohnehin nie ein zweites Mal — Daten bleiben Daten.
    """
    unknown = sorted(set(_PLACEHOLDER_RE.findall(template)) - set(values))
    if unknown:
        raise SelfhostError(
            "internal error: unfilled placeholder in template: " + ", ".join(unknown)
        )
    text = template
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def dns_records(domain: str, *, ipv4: str | None, ipv6: str | None) -> list[str]:
    """Die Zonendatei-Zeilen für A, AAAA und MX (SPEC-CLI.md §4).

    Ohne globale IPv4-Adresse trägt die A-Zeile den Platzhalter; ohne globale
    IPv6-Adresse entfällt die AAAA-Zeile ganz — eine leere AAAA wäre schlimmer als keine.
    """
    lines = [f"{domain}.   {'A':<6}{ipv4 or IPV4_PLACEHOLDER}"]
    if ipv6:
        lines.append(f"{domain}.   {'AAAA':<6}{ipv6}")
    lines.append(f"{domain}.   {'MX 10':<6}{domain}.")
    return lines


def next_steps(
    *,
    domain: str,
    address: str,
    out_dir: Path,
    ipv4: str | None,
    ipv6: str | None,
    out_option: str | None = None,
) -> str:
    """Die fünf Schritte aus SPEC-CLI.md §4 — dieselben in der Ausgabe wie in der Liste.

    Eine zweite, von Hand gepflegte Fassung in `checklist.txt` würde still auseinander
    laufen; deshalb füllt dieser Text auch die Vorlage.

    Jeder Pfad steht so in der Zeile, wie eine Shell ihn wieder liest
    (:func:`shlex.quote`): Schritt 3 ist eine `sudo`-Zeile zum Abtippen, und ein
    Verzeichnisname mit Leerzeichen oder Semikolon ist zulässig. `out_option` ist der Wert,
    den der Aufrufer an `--out` gegeben hat — dann tragen die Schritte 4 und 5 dieselbe
    Option, sonst fände `--check` das Postfach nicht wieder (CT-S1).

    Args:
        domain: Die geprüfte Domain.
        address: Die Postfachadresse.
        out_dir: Das Ausgabeverzeichnis, wie es geschrieben wird.
        ipv4: Globale IPv4-Adresse des Hosts oder `None`.
        ipv6: Globale IPv6-Adresse des Hosts oder `None`.
        out_option: Der gegebene `--out`-Wert, falls es einen gab.
    """
    records = "\n".join(f"       {line}" for line in dns_records(domain, ipv4=ipv4, ipv6=ipv6))
    check = "maildigest selfhost-mail --check"
    if out_option:
        check = f"{check} --out {shlex.quote(out_option)}"
    return "\n".join(
        [
            f"  1) DNS — create these records at your DNS provider "
            f"({shlex.quote(str(out_dir / 'dns.txt'))}):",
            records,
            "  2) Packages and certificate, once, as root:",
            "       Debian/Ubuntu: sudo apt install postfix dovecot-imapd dovecot-lmtpd certbot",
            "       Fedora:        sudo dnf install postfix dovecot certbot",
            f"       sudo certbot certonly --standalone -d {domain}",
            "  3) Apply the generated configuration, as root (it asks for the mailbox password):",
            f"       sudo sh {shlex.quote(str(out_dir / 'apply.sh'))}",
            "  4) Check from this machine:",
            f"       {check}",
            "  5) Prove the whole chain: forward one mail from your real mailbox to",
            f"       {address}",
            "     and let the command wait for it:",
            f"       {check} --wait-for-mail",
        ]
    )


def render_files(
    *,
    domain: str,
    address: str,
    cert_dir: str,
    out_dir: Path,
    ipv4: str | None = None,
    ipv6: str | None = None,
    out_option: str | None = None,
    max_mail_bytes: int = DEFAULT_MAX_MAIL_BYTES,
) -> dict[str, str]:
    """Rendert die fünf Dateien; Schlüssel sind die Dateinamen aus :data:`FILE_ORDER`.

    Alle Werte sind zu diesem Zeitpunkt geprüft (:func:`validate_domain`,
    :func:`generate_address`, :func:`validate_cert_dir`); die Vorlagen setzen sie
    zusätzlich in einfache Anführungszeichen, wo eine Shell sie liest.

    `max_mail_bytes` ist die wirksame Grenze aus `[limits] max_mail_bytes` und wird zu
    Postfix' `message_size_limit`: Nur wenn beide übereinstimmen, prallt eine zu große Mail
    schon beim Weiterleiter ab, statt später still im Sanitizer zu verschwinden
    (PLAN-SELFHOST-MAIL §3).
    """
    records = "\n".join(dns_records(domain, ipv4=ipv4, ipv6=ipv6))
    if not ipv6:
        records = f"{records}\n{NO_IPV6_COMMENT}"
    steps = next_steps(
        domain=domain,
        address=address,
        out_dir=out_dir,
        ipv4=ipv4,
        ipv6=ipv6,
        out_option=out_option,
    )
    values = {
        "domain": domain,
        "address": address,
        "cert_dir": cert_dir,
        "records": records,
        "steps": steps,
        "max_mail_bytes": str(max_mail_bytes),
    }
    return {name: _fill(_template(name), values) for name in FILE_ORDER}


# --- Schreiben -------------------------------------------------------------------------


def _write_file(path: Path, text: str, mode: int) -> None:
    """Schreibt eine Datei mit festen Rechten — angelegt wird sie gleich mit `mode`.

    `os.open` mit den Rechten im Aufruf statt `write_text` plus `chmod`: Zwischen beiden
    läge sonst ein Moment, in dem die Datei mit den Rechten der `umask` existiert.

    `O_NOFOLLOW` und `os.fchmod` statt `os.chmod(path, …)`: Läge im Ausgabeverzeichnis ein
    untergeschobener Symlink namens `apply.sh`, schriebe die Erzeugung sonst in dessen Ziel
    und setzte dort 0700. Ein Symlink ist hier ein Fehler, keine Umleitung.
    """
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SelfhostError(
                f"{path} is a symbolic link — remove it; the generated files are never "
                "written through a link."
            ) from exc
        raise SelfhostError(f"{path} cannot be written: {exc.strerror}.") from exc


def write_files(directory: Path, files: dict[str, str]) -> None:
    """Legt das Verzeichnis (0700) an und schreibt die gerenderten Dateien hinein.

    Ein **bestehendes, nicht leeres** Verzeichnis wird abgelehnt (SPEC-CLI.md §4): Sonst
    landeten die sechs Dateien zwischen fremdem Inhalt — `--out ~` schüttete sie in das
    Heimatverzeichnis — und dessen Rechte würden nebenbei auf 0700 gesetzt.
    """
    try:
        existing = sorted(entry.name for entry in directory.iterdir())
    except FileNotFoundError:
        existing = []
    except OSError as exc:
        raise SelfhostError(f"{directory} cannot be read: {exc.strerror}.") from exc
    if existing:
        raise SelfhostError(
            f"{directory} already exists and is not empty ({len(existing)} entries) — "
            "choose an empty directory with --out, or remove this one."
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)
    except OSError as exc:
        raise SelfhostError(f"{directory} cannot be created: {exc.strerror}.") from exc
    for name, text in files.items():
        _write_file(directory / name, text, FILE_MODES[name])
