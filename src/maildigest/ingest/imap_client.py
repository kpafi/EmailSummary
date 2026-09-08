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
from email.utils import getaddresses, parsedate_to_datetime
from types import TracebackType
from typing import Final, Protocol

from imap_tools import AND, BaseMailBox, ImapToolsError, MailBox, MailMessage
from imap_tools.utils import encode_folder

from maildigest.config import ImapConfig
from maildigest.logging_setup import traceback_enabled
from maildigest.models import RawMail
from maildigest.pipeline import PipelineResult
from maildigest.state.db import MailState, StateDB, dedupe_hash

__all__ = [
    "MAX_BACKOFF_SECONDS",
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


def _header_values(msg: MailMessage, name: str) -> list[str]:
    """Alle Werte eines Headers als Strings; defensiv gegen kaputte Header-Objekte."""
    try:
        values = msg.obj.get_all(name)
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        return []
    if not values:
        return []
    return [str(value) for value in values]


def _collapse(value: str) -> str:
    """Faltet Header-Umbrüche und Mehrfach-Whitespace zu einfachen Leerzeichen zusammen.

    Gefaltete Header (RFC 5322 §2.2.3) enthalten CRLF; die dürfen nicht bis in Notizen oder
    Logs durchschlagen. Das ist keine Sanitisierung (die macht WP3), sondern das Auflösen
    der Transport-Kodierung.
    """
    return " ".join(value.split())


def _header(msg: MailMessage, name: str) -> str | None:
    """Erster Wert eines Headers, whitespace-normalisiert; ``None`` wenn leer/fehlend."""
    values = _header_values(msg, name)
    if not values:
        return None
    return _collapse(values[0]) or None


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
    from_addr = _header(msg, "From") or ""
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
        from_domain=_domain_of(from_addr),
        reply_to=_header(msg, "Reply-To"),
        return_path_domain=_return_path_domain(return_path),
        to_addrs=to_addrs,
        subject_raw=subject_raw,
        date=_parse_date(date_str),
        auth_results_header="\n".join(auth_results) if auth_results else None,
        mime_bytes=mime_bytes,
        size_bytes=size_bytes,
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
                "[imap] port = 143 ist Klartext-IMAP und wird nicht unterstützt. "
                "MailDigest verbindet ausschließlich per IMAPS (üblich: Port 993)."
            )
        if cfg.password is None:
            raise IngestError(
                "Kein IMAP-Passwort gesetzt: [imap] password in der Config oder die "
                "Umgebungsvariable MAILDIGEST_IMAP_PASSWORD verwenden."
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
            raise ImapConnectionError("Keine offene IMAP-Verbindung.")
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
            raise ImapConnectionError(
                f"IMAP-Verbindung zu {self.cfg.host}:{self.cfg.port} "
                f"(Ordner {self.cfg.folder}) fehlgeschlagen: {type(exc).__name__}"
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
                f"Abruf ungesehener Mails fehlgeschlagen: {type(exc).__name__}"
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
                f"Ordnerliste konnte nicht abgerufen werden: {type(exc).__name__}"
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
                f"IMAP-Kommando {command} fehlgeschlagen: {type(exc).__name__}"
            ) from exc
        except Exception as exc:  # imaplib wirft bei Protokollfehlern eigene Typen
            raise ImapConnectionError(
                f"IMAP-Kommando {command} fehlgeschlagen: {type(exc).__name__}"
            ) from exc
        if str(status).upper() != "OK":
            raise MailboxPostProcessError(
                f"Der Server hat das Kommando {command} mit „{status}“ beantwortet."
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
                f"Die Mail konnte nicht als gelesen markiert werden. {exc}"
            ) from exc

        folder = self.cfg.move_processed_to
        if not folder:
            return
        if not self._server_supports_move():
            raise MailboxPostProcessError(
                f"Der Server kann kein server-seitiges MOVE; die Mail bleibt in "
                f"„{self.cfg.folder}“ liegen (sie ist als gelesen markiert). MailDigest "
                f"weicht bewusst nicht auf Kopieren+Löschen aus — [imap] "
                f"move_processed_to leer lassen oder einen Server mit MOVE verwenden."
            )
        try:
            self._uid_command("MOVE", uid, encode_folder(folder))
        except MailboxPostProcessError as exc:
            raise MailboxPostProcessError(
                f"Verschieben nach „{folder}“ fehlgeschlagen. {exc} Existiert der Ordner "
                f"auf dem Server? [imap] move_processed_to prüfen."
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

        if not db.claim(raw.dedupe_key):
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
            logger.info(
                "mail_processed",
                extra={
                    "mail": key_short,
                    "from_domain": raw.from_domain,
                    "status": result.status,
                },
            )
        _mark_processed_best_effort(client, msg, key_short)
    return stats


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
