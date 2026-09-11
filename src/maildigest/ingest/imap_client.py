"""IMAPS-Client (imap-tools): Verbindung, Abruf ungesehener Mails, Umwandlung in `RawMail`,
Dedupe, Gelesen-Markierung/optionales Verschieben — und der Polling-Loop mit Backoff.

Verhalten: docs/ARCHITECTURE.md §2 (Ingest) und §6 (Fehler-/Retry-Politik). Umsetzung in WP2.

Sicherheits-Design dieses Moduls:

* **Nur IMAPS (docs/SECURITY.md §6):** Es wird ausschließlich :class:`imap_tools.MailBox`
  (implizites TLS) verwendet, mit einem `ssl.create_default_context()` — Zertifikats- und
  Hostname-Prüfung sind damit aktiv. Es gibt keinen Codepfad zu `MailBoxUnencrypted` oder
  `MailBoxStartTls` und keinen Schalter, der die Prüfung abschaltet. Port 143 wird
  abgelehnt.
* **Niemals löschen (F-ING-1, ADR-064):** Die einzigen schreibenden Operationen sind
  `UID STORE +FLAGS (\\Seen)` und — falls konfiguriert — ein **server-seitiges**
  `UID MOVE` in den Ordner `imap.move_processed_to`. Beide werden bewusst als rohe
  UID-Kommandos abgesetzt (`mailbox.client.uid(...)`) statt über die Komfort-Methoden
  `MailBox.flag()`/`MailBox.move()`/`MailBox.delete()`: Diese hängen an **jedes** STORE
  ein unbedingtes `EXPUNGE`, und ein EXPUNGE löscht *alle* in der Mailbox als `\\Deleted`
  markierten Nachrichten endgültig — auch fremde, die ein anderer Mailclient oder eine
  Serverregel markiert hat. Kann der Server kein `MOVE`, wird **nicht** auf
  copy+delete+expunge ausgewichen: Die Mail bleibt liegen (nur als gelesen markiert) und
  der Vorgang meldet sich als :class:`MailboxPostProcessError`.
* **Genau einmal verarbeiten (F-ING-2):** Vor der Verarbeitung wird der Dedupe-Key in der
  State-DB reserviert (:meth:`~maildigest.state.db.StateDB.claim`). Gelesen-Flag und
  Verschieben passieren erst **nach** der Verarbeitung; ein Absturz davor führt beim
  Wiederanlauf zu einem erkannten Duplikat, nicht zu einer Doppelverarbeitung.
* **Kein Unterdrücken per Header (ADR-079):** Der Dedupe-Key ist im Normalfall die
  `Message-ID` — ein Header, den der Absender frei wählt. Neben ihm wird deshalb ein
  inhaltsabgeleitetes Merkmal geführt (`RawMail.content_hash`). Gleicher Key bei anderem
  Inhalt ist keine Wiederholung, sondern eine **Kollision**: Die Mail wird unter einem
  abgeleiteten Schlüssel regulär verarbeitet und im Hinweisblock kenntlich gemacht.
* **I5/NF-5:** Geloggt werden nur der gekürzte Dedupe-Hash, die Absender-Domain und der
  Status — nie Betreff, Body, Message-ID im Klartext oder Zugangsdaten.
* **I6 fail-closed:** Eine einzelne kaputte Mail beendet den Loop nicht. Der Fehler wird als
  Status `failed` festgehalten; die Metadaten-Notiz erzeugt die Pipeline
  (`process_mail` liefert `FailedNotice`), die hier als Verarbeitungs-Callback steckt.

IMAP IDLE wird bewusst nicht genutzt: ADR-007, in WP2 nach Prüfung der imap-tools-API
bestätigt (siehe WP2-ADRs in docs/DECISIONS.md).
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from types import TracebackType
from typing import Final, Protocol

from imap_tools import AND, BaseMailBox, ImapToolsError, MailBox, MailMessage
from imap_tools.errors import MailboxLoginError
from imap_tools.utils import encode_folder

from maildigest.config import ImapConfig
from maildigest.logging_setup import traceback_enabled
from maildigest.models import RawMail
from maildigest.pipeline import PipelineResult
from maildigest.state.db import ClaimResult, MailState, StateDB, dedupe_hash

__all__ = [
    "MAX_BACKOFF_SECONDS",
    "ImapAuthError",
    "ImapClient",
    "ImapConnectionError",
    "IngestError",
    "IngestService",
    "IngestStats",
    "MailProcessor",
    "MailboxFactory",
    "MailboxPostProcessError",
    "backoff_delay",
    "build_raw_mail",
    "poll_once",
]

logger = logging.getLogger("maildigest.ingest")

#: Obergrenze des Reconnect-Backoffs (docs/ARCHITECTURE.md §6: max. 10 Minuten).
MAX_BACKOFF_SECONDS: Final = 600.0

#: Erster Backoff-Schritt nach einem Verbindungsabbruch.
START_BACKOFF_SECONDS: Final = 5.0

#: Netzwerk-Timeout der IMAP-Verbindung in Sekunden.
DEFAULT_TIMEOUT_SECONDS: Final = 60.0

#: So viele Zeichen des Bodys fließen in den Fallback-Dedupe-Hash ein.
_BODY_PREFIX_CHARS: Final = 512

#: Präfix des Fallback-Dedupe-Keys, damit er nie mit einer echten Message-ID kollidiert.
_FALLBACK_PREFIX: Final = "sha256:"

#: Der Klartext-IMAP-Port; eine Konfiguration darauf ist immer ein Fehler (docs/SECURITY.md §6).
_PLAINTEXT_IMAP_PORT: Final = 143


class IngestError(Exception):
    """Ingest ist nicht durchführbar (Fehlkonfiguration, Login-Fehler, IMAP-Protokollfehler)."""


class ImapConnectionError(IngestError):
    """Verbindung/Login zum Mirror-Postfach fehlgeschlagen oder abgerissen.

    Löst im Polling-Loop einen Reconnect mit Exponential Backoff aus.
    """


class ImapAuthError(ImapConnectionError):
    """Der Server hat die Anmeldung abgelehnt (falscher Nutzer, falsches Passwort, IMAP aus).

    Eigene Klasse, aber **Unterklasse** von :class:`ImapConnectionError`: Für den
    Polling-Loop bleibt es ein Verbindungsfehler mit Reconnect-Backoff, alle bestehenden
    `except ImapConnectionError` gelten unverändert. Getrennt wird sie nur, damit die
    Einrichtung den anbieterspezifischen Hinweis („App-Passwort nötig") genau dann
    anhängt, wenn er passt — bei einem abgelehnten TCP-Verbindungsversuch ist er
    irreführend (HC-32).
    """


class MailboxPostProcessError(IngestError):
    """Die Nachbehandlung **einer** Mail (Gelesen-Flag/Verschieben) ist fehlgeschlagen.

    Abgegrenzt von :class:`ImapConnectionError`: Die Verbindung steht, der Server hat das
    Kommando nur mit ``NO``/``BAD`` beantwortet (typisch: `imap.move_processed_to` zeigt
    auf einen Ordner, den es auf dem Server nicht gibt) oder kann `MOVE` nicht. Der
    Poll-Durchlauf läuft danach weiter — ein einzelner Nachbehandlungsfehler darf nicht
    das ganze Postfach lahmlegen (CT-12, ADR-065).
    """


class MailProcessor(Protocol):
    """Verarbeitungs-Callback des Ingest — in der Praxis `pipeline.process_mail` (teilweise
    gebunden an die `PipelineDeps`).

    Der Callback darf laut I6 keine Exception nach außen geben; tut er es doch, fängt der
    Ingest sie und markiert die Mail als `failed`.
    """

    def __call__(self, raw: RawMail) -> PipelineResult: ...


@dataclass
class IngestStats:
    """Zählwerk eines Poll-Durchlaufs (nur Zahlen — keine Mail-Metadaten)."""

    fetched: int = 0
    processed: int = 0
    duplicates: int = 0
    failed: int = 0

    def __add__(self, other: IngestStats) -> IngestStats:
        return IngestStats(
            fetched=self.fetched + other.fetched,
            processed=self.processed + other.processed,
            duplicates=self.duplicates + other.duplicates,
            failed=self.failed + other.failed,
        )


# --- RawMail-Aufbau -------------------------------------------------------------------------


def _raw_header_values(msg: MailMessage, name: str) -> list[object]:
    """Alle Werte eines Headers **unverwandelt**; defensiv gegen kaputte Header-Objekte.

    Wichtig für HC-23: Enthält ein Header roh-8-bittige Bytes (RFC-Verstoß, in freier
    Wildbahn häufig), liefert `email` dafür ein :class:`email.header.Header`-Objekt. Dessen
    `str()` ersetzt jedes solche Byte durch U+FFFD — die Originalbytes stehen danach
    nirgends mehr. `decode_header` auf dem **Objekt** bekommt sie dagegen noch.
    """
    try:
        values = msg.obj.get_all(name)
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        return []
    return list(values or [])


def _header_values(msg: MailMessage, name: str) -> list[str]:
    """Alle Werte eines Headers als Strings; defensiv gegen kaputte Header-Objekte."""
    return [str(value) for value in _raw_header_values(msg, name)]


def _collapse(value: str) -> str:
    """Faltet Header-Umbrüche und Mehrfach-Whitespace zu einfachen Leerzeichen zusammen.

    Gefaltete Header (RFC 5322 §2.2.3) enthalten CRLF; die dürfen nicht bis in Notizen oder
    Logs durchschlagen. Das ist keine Sanitisierung (die macht WP3), sondern das Auflösen
    der Transport-Kodierung.
    """
    return " ".join(value.split())


def _decode_unknown_8bit(data: bytes) -> str:
    """Rät die Kodierung roh-8-bittiger Headerbytes (HC-23).

    `email` markiert sie als ``unknown-8bit`` und gibt sie sonst als U+FFFD aus. UTF-8 ist
    heute der Normalfall; Latin-1 ist der Auffangkorb, der nie scheitert. Das ist Raten —
    aber lesbares Raten schlägt garantierte Unlesbarkeit, und sicherheitsrelevant ist es
    nicht: Was hier herauskommt, geht anschliessend durch den Sanitizer.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _decode_mime_words(value: str) -> str:
    """Dekodiert RFC-2047-Wörter (``=?utf-8?Q?…?=``) in einem Headerwert (HC-23).

    Anzeigenamen tragen dieselbe Transport-Kodierung wie der Betreff. Ohne diesen Schritt
    sieht der Sanitizer nur ASCII-Hülsen: Die in docs/SECURITY.md §4 zugesagten NFKC-,
    Steuerzeichen- und Mixed-Script-Prüfungen liefen auf der Kodierung statt auf dem
    Namen, und der
    Nutzer bekäme `=?utf-8?Q?J=C3=B6rg?=` statt `Jörg` zu lesen.

    Bei kaputter Kodierung wird der Rohwert zurückgegeben — `build_raw_mail` wirft nie
    (ADR-020 (e)); eine hier scheiternde Mail käme nie in den Fail-closed-Pfad.
    """
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # jede kaputte RFC-2047-Kodierung: Rohwert behalten
        return value


def _header(msg: MailMessage, name: str) -> str | None:
    """Erster Wert eines Headers, whitespace-normalisiert; ``None`` wenn leer/fehlend."""
    values = _header_values(msg, name)
    if not values:
        return None
    return _collapse(values[0]) or None


def _predecode_eight_bit(value: object) -> str:
    """Wandelt roh-8-bittige Headerbytes in Zeichen, lässt RFC-2047 unangetastet (HC2-2).

    Reine Transport-Entzerrung **vor** dem Adress-Parsen: Bytes ≥ 0x80 können in einer
    Adresse nicht vorkommen, und ihre Dekodierung erzeugt nie ein ASCII-Struktursymbol
    (`<`, `>`, `,`) — die Adressgrenzen bleiben exakt die des Rohwerts. Anders als die
    RFC-2047-Dekodierung ist dieser Schritt deshalb harmlos.

    Trägt der Header zugleich echte kodierte Wörter, wird nichts angefasst: `decode_header`
    liefert deren Inhalt bereits dekodiert, eine Zusammenführung hier würde genau die
    Reihenfolge herstellen, die HC2-2 ausmacht.
    """
    try:
        parts = decode_header(value)  # type: ignore[arg-type]
    except Exception:  # kaputte Kodierung: Rohwert behalten (ADR-020 (e))
        return str(value)
    has_eight_bit = any(
        isinstance(text, bytes) and charset == "unknown-8bit" for text, charset in parts
    )
    only_eight_bit = all(charset in (None, "unknown-8bit") for _text, charset in parts)
    if has_eight_bit and only_eight_bit:
        return "".join(
            _decode_unknown_8bit(text) if isinstance(text, bytes) else text
            for text, _charset in parts
        )
    return str(value)


#: Zeichen, die ein Anzeigename nicht tragen darf, ohne die Adressstruktur zu verändern.
_DISPLAY_NAME_SPECIALS = str.maketrans(dict.fromkeys('<>,;:"\\', " "))


def _clean_display_name(name: str) -> str:
    """Entfernt aus einem dekodierten Anzeigenamen alles Strukturgebende (HC2-2)."""
    return " ".join(name.translate(_DISPLAY_NAME_SPECIALS).split())


def _first_address(text: str) -> tuple[str, str]:
    """Erste Angabe eines Adress-Headers als ``(name, adresse)`` (Rohwert-Parse)."""
    pairs = getaddresses([text])
    if not pairs:
        return "", ""
    for name, address in pairs:
        local, at_sign, domain = address.rpartition("@")
        if at_sign and local and domain.strip().strip("<>[]").rstrip("."):
            return name, address
    return pairs[0]


def _compose_display_address(name: str, address: str) -> str:
    """Baut ``Name <adresse>`` so, dass `parseaddr` genau diese beiden Teile zurückgibt.

    Der Name kommt aus der RFC-2047-Dekodierung und ist damit angreiferkontrolliert; ohne
    Selbstprüfung könnte er die Adresse erneut verschieben (HC2-2). Geprüft wird deshalb
    gegen den Parser selbst: erst unquotiert (das ist die Form, die heutige Tests und der
    Korpus sehen), sonst quotiert, sonst bleibt nur die nackte Adresse.
    """
    if not address:
        return name
    if not name:
        return address
    plain = f"{name} <{address}>"
    if parseaddr(plain) == (name, address):
        return plain
    quoted = f'"{name}" <{address}>'
    if parseaddr(quoted) == (name, address):
        return quoted
    return address


def _address_header(msg: MailMessage, name: str) -> tuple[str, str]:
    """Anzeigenamentragender Header (`From`, `Reply-To`) → ``(anzeigeform, adresse)``.

    Reihenfolge ist hier sicherheitsrelevant (HC2-2, Regression aus HC-23): Zuerst wird die
    Adresse aus dem **rohen** Headerwert gelesen, erst danach der Namensteil dekodiert. Ein
    kodierter Anzeigename wie ``=?utf-8?B?<'Bank <info@bank.example>,'>?=`` schleust sonst
    eine zweite Adresse in die Liste, die als erste steht und die Absender-Domain bestimmt.

    Enthält der Name keine Kodierung, wird der Rohwert unverändert übernommen — es gibt
    dann nichts zu reparieren, und jede Normalisierung wäre nur eine weitere Fehlerquelle.
    """
    values = _raw_header_values(msg, name)
    if not values:
        return "", ""
    raw_text = _collapse(_predecode_eight_bit(values[0]))
    if not raw_text:
        return "", ""
    display, address = _first_address(raw_text)
    decoded = _decode_mime_words(display) if display else ""
    if decoded == display:
        return raw_text, address
    return _compose_display_address(_clean_display_name(decoded), address), address


def _domain_of(address: str) -> str:
    """Extrahiert die Domain aus einer Adresse (oder einem `Name <adr>`-Header), lowercase.

    Leerstring, wenn keine Domain erkennbar ist — der Sanitizer/Kritiker behandelt das als
    "Absender-Domain unbekannt" (docs/ARCHITECTURE.md §3: `from_domain` ist Pflichtfeld).
    """
    candidates = getaddresses([address.replace("\r", " ").replace("\n", " ")])
    for _name, email_address in candidates:
        local, at_sign, domain = email_address.rpartition("@")
        if not at_sign or not local:
            # Kein "lokalteil@domain" — z. B. ein blosser Anzeigename wie "Postmaster".
            continue
        cleaned = domain.strip().strip("<>[]").rstrip(".").lower()
        if cleaned:
            return cleaned
    return ""


def _parse_date(raw_date: str | None) -> datetime | None:
    """RFC-2822-Datum → `datetime`; ``None`` bei fehlendem oder unparsbarem Header.

    Bewusst nicht `MailMessage.date`: imap-tools liefert dort für unparsbare Datumsangaben
    den Sentinel 1900-01-01, das Datenmodell verlangt aber ein ehrliches ``None``.
    """
    if not raw_date:
        return None
    try:
        return parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None


def _return_path_domain(return_path: str | None) -> str | None:
    """Domain des `Return-Path`-Headers; ``None``, wenn Header fehlt oder leer ist.

    Der Kritiker vergleicht sie in WP6 mit `from_domain` (F-CRIT-3) — ein leerer String wäre
    dort ein irreführender "Treffer", deshalb ist "unbekannt" hier explizit ``None``.
    """
    if not return_path:
        return None
    return _domain_of(return_path) or None


def _body_prefix(msg: MailMessage) -> str:
    """Erste Zeichen des Klartext- bzw. HTML-Bodys — nur als Hash-Eingabe, nie zur Ausgabe."""
    try:
        body = msg.text or msg.html
    except Exception:  # kaputtes MIME darf hier nichts kosten
        return ""
    return body[:_BODY_PREFIX_CHARS]


def _fallback_dedupe_key(from_addr: str, date_str: str, subject: str, body_prefix: str) -> str:
    """Deterministischer Ersatz-Key für Mails ohne Message-ID (docs/ARCHITECTURE.md §3).

    Hash über From + Date + Subject + Body-Präfix, Felder mit ``\\x00`` getrennt, damit
    Feldgrenzen nicht durch Verschiebung fälschbar sind.
    """
    digest = hashlib.sha256()
    for part in (from_addr, date_str, subject, body_prefix):
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x00")
    return f"{_FALLBACK_PREFIX}{digest.hexdigest()}"


def _raw_bytes(msg: MailMessage) -> bytes:
    """Serialisiert die Mail zurück nach Bytes (`RawMail.mime_bytes`).

    imap-tools hält nach dem Parsen nur das `email.message.Message`-Objekt; die Bytes werden
    daraus rekonstruiert. Für defekte Nachrichten, bei denen `as_bytes()` scheitert, wird auf
    die String-Repräsentation zurückgefallen, damit eine kaputte Mail trotzdem als `RawMail`
    in die Pipeline (und damit in den Fail-closed-Pfad) gelangt statt still zu verschwinden.
    """
    try:
        return msg.obj.as_bytes()
    except Exception:  # defektes MIME ist der Normalfall, nicht die Ausnahme
        return str(msg.obj).encode("utf-8", "replace")


def _content_hash(mime_bytes: bytes) -> str:
    """SHA-256 (hex) über die MIME-Bytes — das zweite Dedupe-Merkmal (ADR-079, HC-10).

    Anders als `Message-ID` kann ein Absender diesen Wert nicht auf eine fremde Mail legen:
    Er ist genau dann gleich, wenn die Mail dieselbe ist.
    """
    return hashlib.sha256(mime_bytes).hexdigest()


def build_raw_mail(msg: MailMessage) -> RawMail:
    """Wandelt eine `imap_tools.MailMessage` in das Domänenmodell `RawMail` um.

    Befüllt alle Felder aus docs/ARCHITECTURE.md §3. Die Funktion ist bewusst so defensiv
    gebaut, dass sie auch bei fehlenden oder kaputten Headern nicht wirft: Eine Mail, die
    hier scheitert, käme nie in den Fail-closed-Pfad und ginge damit still verloren (I6,
    F-OPS-3).

    Args:
        msg: Vom IMAP-Server geholte Nachricht.

    Returns:
        Die `RawMail` mit `dedupe_key` = Message-ID, sonst Fallback-Hash aus
        From + Date + Subject + Body-Präfix.
    """
    message_id = _header(msg, "Message-ID")
    from_addr, from_address = _address_header(msg, "From")
    date_str = _header(msg, "Date") or ""

    try:
        subject_raw = _collapse(msg.subject)
    except Exception:  # kaputte RFC-2047-Kodierung im Betreff
        subject_raw = _header(msg, "Subject") or ""

    dedupe_key = message_id or _fallback_dedupe_key(
        from_addr, date_str, subject_raw, _body_prefix(msg)
    )

    to_addrs = [
        email_address
        for _name, email_address in getaddresses(_header_values(msg, "To"))
        if email_address
    ]

    return_path = _header(msg, "Return-Path")
    auth_results = _header_values(msg, "Authentication-Results")

    mime_bytes = _raw_bytes(msg)
    try:
        size_bytes = msg.size_rfc822 or len(mime_bytes)
    except Exception:  # Server ohne RFC822.SIZE in der FETCH-Antwort
        size_bytes = len(mime_bytes)

    return RawMail(
        message_id=message_id,
        dedupe_key=dedupe_key,
        from_addr=from_addr,
        # HC2-2: aus der **roh** geparsten Adresse, nie aus dem (dekodierten) Anzeigenamen.
        from_domain=_domain_of(from_address),
        reply_to=_address_header(msg, "Reply-To")[0] or None,
        return_path_domain=_return_path_domain(return_path),
        to_addrs=to_addrs,
        subject_raw=subject_raw,
        date=_parse_date(date_str),
        auth_results_header="\n".join(auth_results) if auth_results else None,
        mime_bytes=mime_bytes,
        size_bytes=size_bytes,
        content_hash=_content_hash(mime_bytes),
    )


# --- Backoff ---------------------------------------------------------------------------------


def backoff_delay(
    attempt: int,
    *,
    start: float = START_BACKOFF_SECONDS,
    maximum: float = MAX_BACKOFF_SECONDS,
) -> float:
    """Exponentieller Backoff nach `attempt` fehlgeschlagenen Verbindungsversuchen.

    Args:
        attempt: 1 für den ersten Fehlversuch, dann aufsteigend.
        start: Wartezeit nach dem ersten Fehlversuch.
        maximum: Obergrenze (docs/ARCHITECTURE.md §6: 10 Minuten).

    Returns:
        Wartezeit in Sekunden: `start * 2**(attempt-1)`, gedeckelt auf `maximum`.
        Für `attempt <= 0` das Ergebnis von `attempt == 1`.
    """
    steps = max(attempt, 1) - 1
    if steps >= 64:  # Überlauf-/Performance-Schutz; längst über dem Deckel
        return maximum
    return min(start * float(2**steps), maximum)


# --- IMAP-Client -----------------------------------------------------------------------------

#: Fabrik für die Mailbox-Verbindung — in Tests durch einen Fake ersetzbar.
MailboxFactory = Callable[[], BaseMailBox]


def _default_mailbox_factory(cfg: ImapConfig, timeout: float) -> MailboxFactory:
    """Erzeugt die Fabrik für eine echte IMAPS-Verbindung mit aktiver Zertifikatsprüfung."""

    def factory() -> BaseMailBox:
        # create_default_context(): verify_mode=CERT_REQUIRED, check_hostname=True.
        # Es gibt hier bewusst keinen Schalter, das abzuschalten (docs/SECURITY.md §6).
        context = ssl.create_default_context()
        return MailBox(host=cfg.host, port=cfg.port, ssl_context=context, timeout=timeout)

    return factory


class ImapClient:
    """Dünner, testbarer Wrapper um `imap_tools.MailBox` für das Mirror-Postfach.

    Kann als Kontextmanager benutzt werden::

        with ImapClient(cfg) as client:
            for msg in client.fetch_unseen():
                ...
    """

    def __init__(
        self,
        cfg: ImapConfig,
        *,
        mailbox_factory: MailboxFactory | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Konfiguriert den Client (verbindet noch nicht).

        Raises:
            IngestError: Port 143 (Klartext-IMAP) konfiguriert oder kein Passwort gesetzt.
        """
        if cfg.port == _PLAINTEXT_IMAP_PORT:
            raise IngestError(
                "[imap] port = 143 is plaintext IMAP and is not supported. "
                "MailDigest connects over IMAPS only (usually port 993)."
            )
        if cfg.password is None:
            raise IngestError(
                "No IMAP password set: use [imap] password in the configuration or the "
                "environment variable MAILDIGEST_IMAP_PASSWORD."
            )
        self.cfg = cfg
        self._password = cfg.password.get_secret_value()
        self._factory = mailbox_factory or _default_mailbox_factory(cfg, timeout)
        self._mailbox: BaseMailBox | None = None

    # --- Verbindung -----------------------------------------------------------------------

    @property
    def mailbox(self) -> BaseMailBox:
        """Die offene Mailbox.

        Raises:
            ImapConnectionError: Es besteht keine Verbindung (`connect()` fehlt/abgerissen).
        """
        if self._mailbox is None:
            raise ImapConnectionError("No open IMAP connection.")
        return self._mailbox

    def connect(self) -> None:
        """Baut die IMAPS-Verbindung auf und meldet sich im konfigurierten Ordner an.

        Raises:
            ImapConnectionError: TLS-, Netzwerk- oder Login-Fehler. Die Meldung nennt nie
                das Passwort (I5).
        """
        password = self._password
        try:
            mailbox = self._factory()
            mailbox.login(self.cfg.username, password, initial_folder=self.cfg.folder)
        except (ImapToolsError, ssl.SSLError, OSError) as exc:
            # Der Servertext wird bewusst nicht übernommen (I5) — er kann den gesendeten
            # Nutzernamen zitieren. Übrig bleibt die Fehlerklasse; ob es ein Transport-
            # oder ein Anmeldefehler war, entscheidet der Typ der Ausnahme (HC-32).
            failure = (
                ImapAuthError if isinstance(exc, MailboxLoginError) else ImapConnectionError
            )
            raise failure(
                f"IMAP connection to {self.cfg.host}:{self.cfg.port} "
                f"(folder {self.cfg.folder}) failed: {type(exc).__name__}"
            ) from exc
        self._mailbox = mailbox
        logger.info(
            "imap_connected", extra={"host": self.cfg.host, "folder": self.cfg.folder}
        )

    def disconnect(self) -> None:
        """Meldet ab und verwirft die Verbindung; Fehler werden bewusst geschluckt."""
        mailbox, self._mailbox = self._mailbox, None
        if mailbox is None:
            return
        try:
            mailbox.logout()
        except (ImapToolsError, OSError) as exc:
            logger.debug("imap_logout_failed", extra={"error": type(exc).__name__})

    def __enter__(self) -> ImapClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.disconnect()

    # --- Abruf und Nachbehandlung ----------------------------------------------------------

    def fetch_unseen(self) -> Iterator[MailMessage]:
        """Liefert alle ungesehenen Mails des konfigurierten Ordners.

        ``mark_seen=False`` ist entscheidend: Das `\\Seen`-Flag wird erst nach erfolgreicher
        Verarbeitung gesetzt (:meth:`mark_processed`). Bricht der Prozess vorher ab, taucht
        die Mail beim nächsten Poll wieder auf — und wird dank State-DB als Duplikat erkannt
        (F-ING-2).

        Raises:
            ImapConnectionError: Verbindung weg oder FETCH fehlgeschlagen.
        """
        try:
            yield from self.mailbox.fetch(AND(seen=False), mark_seen=False, bulk=False)
        except (ImapToolsError, OSError) as exc:
            raise ImapConnectionError(
                f"Fetching unseen mail failed: {type(exc).__name__}"
            ) from exc

    def list_folders(self) -> list[str]:
        """Namen aller Ordner des Postfachs — nur lesend (WP9, `connect-mail`).

        Returns:
            Die Ordnernamen in Server-Reihenfolge. Nicht dekodierbare Einträge werden
            übersprungen statt zu raten.

        Raises:
            ImapConnectionError: Verbindung weg oder LIST fehlgeschlagen.
        """
        try:
            return [folder.name for folder in self.mailbox.folder.list() if folder.name]
        except (ImapToolsError, OSError, UnicodeError) as exc:
            raise ImapConnectionError(
                f"The folder list could not be retrieved: {type(exc).__name__}"
            ) from exc

    # --- Rohe UID-Kommandos (nie über MailBox.flag/move/delete — die expungen, ADR-064) ---

    def _uid_command(self, command: str, uid: str, *args: bytes | str) -> None:
        """Setzt ein rohes ``UID <command>`` ab und prüft den Status.

        Raises:
            ImapConnectionError: Socket-/Protokollfehler — die Verbindung ist hin.
            MailboxPostProcessError: Der Server hat mit ``NO``/``BAD`` geantwortet.
        """
        try:
            status, data = self.mailbox.client.uid(command, uid, *args)  # type: ignore[arg-type]
        except (ImapToolsError, OSError) as exc:
            raise ImapConnectionError(
                f"IMAP command {command} failed: {type(exc).__name__}"
            ) from exc
        except Exception as exc:  # imaplib wirft bei Protokollfehlern eigene Typen
            raise ImapConnectionError(
                f"IMAP command {command} failed: {type(exc).__name__}"
            ) from exc
        if str(status).upper() != "OK":
            raise MailboxPostProcessError(
                f"The server answered the {command} command with “{status}”."
            )
        del data  # Die Serverantwort enthält Mail-Metadaten und wird nicht geloggt (I5).

    def _server_supports_move(self) -> bool:
        """True, wenn der Server die MOVE-Erweiterung (RFC 6851) ankündigt."""
        capabilities = getattr(self.mailbox.client, "capabilities", ()) or ()
        return any(str(item).upper() == "MOVE" for item in capabilities)

    def mark_processed(self, msg: MailMessage) -> None:
        """Markiert eine verarbeitete Mail als gelesen und verschiebt sie ggf. (F-ING-1).

        Es wird **nie** gelöscht und **nie** expunged (ADR-064): Gesetzt wird nur
        ``UID STORE <uid> +FLAGS (\\Seen)``; ist `imap.move_processed_to` gesetzt, folgt ein
        server-seitiges ``UID MOVE``. Kann der Server kein `MOVE`, bleibt die Mail liegen —
        der client-seitige Ersatz (COPY + `\\Deleted` + EXPUNGE) ist ein Löschpfad und
        existiert hier bewusst nicht.

        Fehlt die UID (Server ohne UID in der FETCH-Antwort), passiert nichts — das ist
        unschön, aber ungefährlich: Die Mail wird beim nächsten Poll als Duplikat erkannt.

        Raises:
            ImapConnectionError: Die Verbindung ist abgerissen.
            MailboxPostProcessError: Der Server hat ein Kommando abgelehnt (z. B. weil
                `imap.move_processed_to` auf einen nicht existierenden Ordner zeigt) oder
                kann kein server-seitiges MOVE.
        """
        uid = msg.uid
        if not uid:
            logger.warning("imap_missing_uid")
            return
        try:
            self._uid_command("STORE", uid, "+FLAGS", r"(\Seen)")
        except MailboxPostProcessError as exc:
            raise MailboxPostProcessError(
                f"The mail could not be marked as read. {exc}"
            ) from exc

        folder = self.cfg.move_processed_to
        if not folder:
            return
        if not self._server_supports_move():
            raise MailboxPostProcessError(
                f"The server does not support server-side MOVE; the mail stays in "
                f'"{self.cfg.folder}" (it is marked as read). MailDigest deliberately '
                f"does not fall back to copy+delete — leave [imap] move_processed_to "
                f"empty or use a server that supports MOVE."
            )
        try:
            self._uid_command("MOVE", uid, encode_folder(folder))
        except MailboxPostProcessError as exc:
            raise MailboxPostProcessError(
                f'Moving to "{folder}" failed. {exc} Does the folder exist on the server? '
                f"Check [imap] move_processed_to."
            ) from exc


# --- Poll-Durchlauf und Loop ------------------------------------------------------------------


def poll_once(
    client: ImapClient,
    db: StateDB,
    process: MailProcessor,
    *,
    write_result_status: bool = True,
) -> IngestStats:
    """Holt alle ungesehenen Mails einmal ab und verarbeitet sie.

    Ablauf je Mail: `RawMail` bauen → in der State-DB reservieren (`claim`) → bei Erfolg
    verarbeiten und Endstatus schreiben → als gelesen markieren/verschieben. Ein bereits
    bekannter Dedupe-Key wird übersprungen, aber trotzdem als gelesen markiert, damit er
    nicht bei jedem Poll erneut auftaucht (F-ING-2).

    Meldet `claim` eine **Kollision** (gleicher Key, anderer Inhalt — ADR-079), wird die
    Mail nicht verworfen: Sie bekommt einen abgeleiteten Dedupe-Key, `id_collision = True`
    und läuft regulär durch; das Ereignis steht als `mail_id_collision` (WARNING, nur
    Hashes) im Log.

    Ein Fehler in der Verarbeitung **einer** Mail beendet den Durchlauf nicht (I6): Die Mail
    bekommt Status `failed`. Ebenso wenig beendet ihn ein abgelehntes Nachbehandlungs-
    Kommando (:class:`MailboxPostProcessError`, z. B. fehlender `move_processed_to`-Ordner):
    Er wird als `imap_postprocess_failed` protokolliert, der Zyklus läuft weiter (CT-12).
    Verbindungsfehler dagegen werden nach oben gereicht, damit der Loop einen Reconnect mit
    Backoff macht.

    Args:
        client: Verbundener :class:`ImapClient`.
        db: State-Datenbank für Dedupe und Status.
        process: Verarbeitungs-Callback (Pipeline).
        write_result_status: Wenn ``True`` (Default), schreibt dieser Durchlauf den
            Endstatus aus dem Pipeline-Ergebnis. Der Runner aus WP8 setzt ``False``: Er
            führt den Status selbst (inkl. Zwischenständen und Zustell-Warteschlange,
            ADR-050) und würde hier sonst überschrieben. Der Fehlerpfad schreibt
            ``failed`` unabhängig davon — eine abgestürzte Verarbeitung darf nie ohne
            Status bleiben (F-OPS-3).

    Returns:
        Zählwerk des Durchlaufs.

    Raises:
        ImapConnectionError: Verbindungsabbruch während Abruf oder Nachbehandlung.
    """
    stats = IngestStats()
    for msg in client.fetch_unseen():
        stats.fetched += 1
        raw = build_raw_mail(msg)
        key_short = dedupe_hash(raw.dedupe_key)[:12]

        outcome = db.claim(raw.dedupe_key, content_hash=raw.content_hash)
        if outcome is ClaimResult.COLLISION:
            # ADR-079/HC-10: Derselbe Dedupe-Key, nachweislich anderer Inhalt. Der Key kommt
            # aus einem frei wählbaren Header — ihn hier als Duplikat zu verwerfen, hiesse
            # eine echte Mail auf Zuruf des Angreifers zu unterdrücken. Die Mail läuft
            # deshalb unter einem abgeleiteten, inhaltsgebundenen Key regulär durch und wird
            # dem Nutzer als Kollision kenntlich gemacht.
            raw = _as_collision(raw)
            collision_short = dedupe_hash(raw.dedupe_key)[:12]
            logger.warning(
                "mail_id_collision",
                extra={"mail": key_short, "collision_mail": collision_short},
            )
            key_short = collision_short
            outcome = db.claim(raw.dedupe_key, content_hash=raw.content_hash)
        if outcome is not ClaimResult.CLAIMED:
            stats.duplicates += 1
            logger.info("mail_duplicate", extra={"mail": key_short})
            _mark_processed_best_effort(client, msg, key_short)
            continue

        try:
            result = process(raw)
        except Exception as exc:  # I6: eine kaputte Mail darf den Loop nie stoppen
            stats.failed += 1
            db.mark_status(raw.dedupe_key, MailState.FAILED, error_class="ingest_error")
            # ADR-047: nur der Exception-Klassenname; der Traceback (der Mail-Inhalt aus
            # Fehlertexten transportieren kann) erscheint ausschließlich bei log_level=DEBUG.
            logger.error(
                "mail_processing_crashed",
                extra={"mail": key_short, "error": type(exc).__name__},
                exc_info=traceback_enabled(logger),
            )
        else:
            stats.processed += 1
            if write_result_status:
                db.mark_status(raw.dedupe_key, MailState(result.status))
                logged_status: str = result.status
            else:
                # Der Runner führt den Status selbst (ADR-050): Eine Zustellung, die noch in
                # der Warteschlange liegt, bleibt `checked` und wird erst nach der
                # Bestätigung `delivered`. `result.status` wäre hier trotzdem schon
                # „delivered" — direkt nach einem `delivery_deferred` eine irreführende
                # Zeile (Nebenbefund aus CT-13). Geloggt wird deshalb der Stand, der
                # tatsächlich in der Datenbank steht.
                record = db.get(raw.dedupe_key)
                logged_status = result.status if record is None else record.status.value
            logger.info(
                "mail_processed",
                extra={
                    "mail": key_short,
                    "from_domain": raw.from_domain,
                    "status": logged_status,
                },
            )
        _mark_processed_best_effort(client, msg, key_short)
    return stats


def _as_collision(raw: RawMail) -> RawMail:
    """Kopie der Mail unter dem abgeleiteten Kollisions-Key, mit gesetztem Hinweis-Flag.

    `RawMail` ist frozen (I1-nahes Datenmodell) — die Kopie ist der einzige Weg. Der
    abgeleitete Key ist deterministisch, damit dieselbe Mail beim nächsten Poll wieder als
    Duplikat erkannt wird (F-ING-2 bleibt gültig).
    """
    return raw.model_copy(
        update={
            "dedupe_key": StateDB.derived_collision_key(raw.dedupe_key, raw.content_hash),
            "id_collision": True,
        }
    )


def _mark_processed_best_effort(client: ImapClient, msg: MailMessage, key_short: str) -> None:
    """Nachbehandlung einer Mail; ein abgelehntes Kommando stoppt den Zyklus nicht (CT-12).

    :class:`MailboxPostProcessError` bedeutet: Die Verbindung steht, der Server hat das
    Kommando abgelehnt — fast immer, weil `imap.move_processed_to` auf einen nicht
    existierenden Ordner zeigt. Die Mail ist bereits verarbeitet und in der State-DB
    vermerkt; sie beim nächsten Poll erneut zu sehen kostet nur einen Dedupe-Treffer. Der
    Rest des Postfachs wird weiter abgearbeitet. Verbindungsfehler
    (:class:`ImapConnectionError`) gehen dagegen weiter nach oben — dort gehört der
    Reconnect hin.
    """
    try:
        client.mark_processed(msg)
    except MailboxPostProcessError as exc:
        logger.warning(
            "imap_postprocess_failed",
            extra={"mail": key_short, "reason": str(exc)},
        )


@dataclass
class IngestService:
    """Polling-Loop über dem :class:`ImapClient` mit Reconnect und Exponential Backoff.

    Der Loop ist bewusst synchron und ohne Threads: `run_forever` blockiert, bis
    :meth:`stop` gerufen wird (aus einem Signal-Handler in WP8) oder ein nicht
    behebbarer Fehler auftritt. Verbindungsfehler führen zu Reconnect mit Backoff
    (docs/ARCHITECTURE.md §6), nie zum Abbruch.

    Attributes:
        cfg: `[imap]`-Sektion der Config (Ordner, Intervall, `move_processed_to`).
        db: State-Datenbank.
        process: Verarbeitungs-Callback (Pipeline).
        client_factory: Erzeugt den Client; Default ist ein echter :class:`ImapClient`.
        sleep: Warte-Funktion; Default ist eine unterbrechbare Wartefunktion auf dem
            Stop-Event. Tests reichen hier eine protokollierende Funktion herein.
        write_result_status: An `poll_once` durchgereicht (siehe dort); der Runner aus WP8
            setzt ``False``, weil er den Status selbst führt.
    """

    cfg: ImapConfig
    db: StateDB
    process: MailProcessor
    client_factory: Callable[[], ImapClient] | None = None
    sleep: Callable[[float], None] | None = None
    write_result_status: bool = True
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def stop(self) -> None:
        """Fordert den Loop zum Beenden auf (signal-handler-tauglich)."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        """True, sobald :meth:`stop` gerufen wurde."""
        return self._stop.is_set()

    def _new_client(self) -> ImapClient:
        if self.client_factory is not None:
            return self.client_factory()
        return ImapClient(self.cfg)

    def _wait(self, seconds: float) -> None:
        """Wartet unterbrechbar; ein `stop()` beendet die Wartezeit sofort."""
        if self.sleep is not None:
            self.sleep(seconds)
            return
        self._stop.wait(seconds)

    def run_once(self) -> IngestStats:
        """Verbindet, holt einmal alle ungesehenen Mails ab und trennt wieder.

        Grundlage von `maildigest run --once` (F-OPS-1, Cron-tauglich).

        Raises:
            IngestError: Verbindung/Login/Abruf fehlgeschlagen — bei `--once` gibt es
                bewusst keinen internen Retry, der Aufrufer (Cron) wiederholt.
        """
        client = self._new_client()
        client.connect()
        try:
            return poll_once(
                client, self.db, self.process, write_result_status=self.write_result_status
            )
        finally:
            client.disconnect()

    def run_forever(self) -> IngestStats:
        """Dauer-Loop: pollen, warten, bei Verbindungsfehlern reconnecten.

        Returns:
            Summe der Zählwerke aller Durchläufe (für Tests und Betriebsausgabe).
        """
        total = IngestStats()
        client: ImapClient | None = None
        failures = 0
        while not self.stopped:
            try:
                if client is None:
                    client = self._new_client()
                    client.connect()
                total = total + poll_once(
                    client, self.db, self.process,
                    write_result_status=self.write_result_status,
                )
            except IngestError as exc:
                failures += 1
                delay = backoff_delay(failures)
                logger.warning(
                    "imap_reconnect_scheduled",
                    extra={
                        "error": type(exc).__name__,
                        "attempt": failures,
                        "delay_seconds": delay,
                    },
                )
                if client is not None:
                    client.disconnect()
                    client = None
                self._wait(delay)
                continue
            failures = 0
            self._wait(float(self.cfg.poll_interval_seconds))
        if client is not None:
            client.disconnect()
        return total
