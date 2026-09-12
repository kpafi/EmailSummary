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
* **Abruf je UID (O-1, zweite Iteration):** Erst `UID SEARCH UNSEEN`, dann je UID ein
  eigenes `UID FETCH`. imap-tools parst jede Mail eifrig im Konstruktor von
  `MailMessage` — innerhalb seines `fetch`-Generators. Scheitert dieser Parse (eine
  31-KB-Mail mit 984 `message/rfc822`-Ebenen reicht für `RecursionError`), war früher der
  ganze Zyklus hin und die Mail blieb für immer ungesehen. Jetzt ist der Parse je Mail
  isoliert: Die unparsbare Mail wird über einen zweiten, **nur Kopfzeilen** holenden
  `UID FETCH` als :class:`UnparsableMailMessage` vertreten und läuft wie jede andere
  unlesbare Mail in den Fail-closed-Pfad (Notiz, `failed`, Seen-Flag).

IMAP IDLE wird bewusst nicht genutzt: ADR-007, in WP2 nach Prüfung der imap-tools-API
bestätigt (siehe WP2-ADRs in docs/DECISIONS.md).
"""

from __future__ import annotations

import hashlib
import imaplib
import logging
import re
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.headerregistry import Address, AddressHeader, HeaderRegistry
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from types import TracebackType
from typing import Final, Protocol

from imap_tools import AND, BaseMailBox, ImapToolsError, MailBox, MailMessage
from imap_tools.errors import MailboxLoginError
from imap_tools.utils import decode_value, encode_folder

from maildigest.config import ImapConfig
from maildigest.logging_setup import traceback_enabled
from maildigest.models import RawMail
from maildigest.pipeline import PipelineResult
from maildigest.state.db import ClaimResult, MailState, StateDB, dedupe_hash

__all__ = [
    "MAX_BACKOFF_SECONDS",
    "MAX_MIME_DEPTH",
    "MAX_RECIPIENTS",
    "ImapAuthError",
    "ImapClient",
    "ImapConnectionError",
    "IngestError",
    "IngestService",
    "IngestStats",
    "MailProcessor",
    "MailboxFactory",
    "MailboxPostProcessError",
    "UnparsableMailMessage",
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

#: Höchste Verschachtelungstiefe des geparsten MIME-Baums (S-1/O-1). Alles darunter wird
#: **vor** jeder Serialisierung abgeschnitten (:func:`_cap_message_depth`). Grund: Jede
#: Ebene kostet in `Message.as_bytes()`, in `Message.walk()` und im Parser der
#: Standardbibliothek mehrere Stackframes — eine 16-KB-Mail mit 250 Ebenen reichte, um
#: `as_bytes()` mit `RecursionError` scheitern zu lassen. 32 Ebenen sind weit jenseits
#: alles Realen (der Sanitizer wertet ab Werk 10 aus, `limits.max_mime_depth`) und weit
#: unterhalb der Rekursionsgrenze. Bewusst kein Config-Feld: eine Schranke gegen einen
#: Angriff, kein Geschmacksparameter.
MAX_MIME_DEPTH: Final = 32

#: Ersatzinhalt, wenn selbst der Rückfall in :func:`_raw_bytes` scheitert. Nur Text über
#: die Mail, nie Mail-Inhalt.
_UNSERIALIZABLE_BODY: Final = b"[maildigest: message body could not be serialized]\r\n"


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
    """Alle Werte eines Headers **unverwandelt**, aber gedeckelt; wirft nie.

    Das ist die **einzige** Stelle, an der ein Headerwert aus der Nachricht gelesen wird
    (NF-1, fünfte Iteration, S-1): Jeder Wert wird hier — vor jeder Verarbeitung — auf
    :data:`_MAX_HEADER_CHARS` geschnitten, und es werden höchstens
    :data:`_MAX_HEADER_VALUES` Werte je Headername herausgegeben. Die vierte Iteration
    deckelte nur `From`, `Reply-To` und `Subject`; ein 20-MB-`To:` lief ungedeckelt durch
    `getaddresses` (18,5 s CPU). Eine Schranke gilt für die Klasse, nicht für die
    gemeldete Instanz — deshalb sitzt sie hier und nicht beim Aufrufer.

    Wichtig für HC-23: Enthält ein Header roh-8-bittige Bytes (RFC-Verstoß, in freier
    Wildbahn häufig), liefert `email` dafür ein :class:`email.header.Header`-Objekt. Dessen
    `str()` ersetzt jedes solche Byte durch U+FFFD — die Originalbytes stehen danach
    nirgends mehr. `decode_header` auf dem **Objekt** bekommt sie dagegen noch; unterhalb
    der Obergrenze bleibt das Objekt deshalb unangetastet.
    """
    try:
        values = msg.obj.get_all(name)
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        return []
    return [_cap_header_value(value) for value in list(values or [])[:_MAX_HEADER_VALUES]]


def _cap_header_value(value: object) -> object:
    """Schneidet einen einzelnen Rohwert auf :data:`_MAX_HEADER_CHARS` (S-1).

    Unterhalb der Obergrenze kommt der Wert unverändert zurück (auch ein
    `Header`-Objekt); darüber der Anfang als String. Der Schnitt liegt vor
    `decode_header`, `getaddresses` und jeder anderen Verarbeitung.
    """
    return _cap_header_value_with_length(value)[0]


def _cap_header_value_with_length(value: object) -> tuple[object, int]:
    """Wie :func:`_cap_header_value`, gibt die Zeichenlänge des Ergebnisses mit zurück."""
    text = str(value)
    if len(text) > _MAX_HEADER_CHARS:
        return text[:_MAX_HEADER_CHARS], _MAX_HEADER_CHARS
    return value, len(text)


def _cap_message_depth(message: Message) -> int:
    """Deckelt die Verschachtelungstiefe des geparsten MIME-Baums auf :data:`MAX_MIME_DEPTH`.

    Läuft **vor** jeder Serialisierung und vor jedem anderen Baumdurchlauf (O-1): `as_bytes()`,
    `walk()`, `msg.text`/`msg.html` von imap-tools und der Parser der Standardbibliothek sind
    allesamt rekursiv, und eine 16 KB grosse Mail mit 250 `multipart`-Ebenen genügt, um sie mit
    `RecursionError` scheitern zu lassen. Ein Teilbaum jenseits der Schranke wird durch einen
    **leeren** Payload ersetzt: Der Teil bleibt mit seinen Kopfzeilen stehen (sichtbar für
    Sanitizer und Nutzer), sein Inhalt ist weg. Alles Nachgelagerte (Serialisierung, `content_hash`,
    Sanitizer) sieht denselben, konsistenten Baum.

    Bewusst iterativ — die Schranke gegen Rekursion darf nicht selbst rekursiv sein
    (Vorbild: :func:`_cap_message_headers`). Für gewöhnliche Mails ist die Funktion ein
    reiner Lesedurchlauf: Sie ändert nichts, die Serialisierung bleibt byteidentisch.

    Returns:
        Zahl der abgeschnittenen Teilbäume (0 = nichts geändert).
    """
    capped = 0
    stack: list[tuple[Message, int]] = [(message, 0)]
    while stack:
        part, depth = stack.pop()
        try:
            if not part.is_multipart():
                continue
            payload = part.get_payload()
        except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
            continue
        if not isinstance(payload, list):
            continue
        if depth >= MAX_MIME_DEPTH:
            if payload:
                part.set_payload([])
                capped += 1
            continue
        stack.extend((child, depth + 1) for child in payload if isinstance(child, Message))
    return capped


def _cap_message_headers(message: Message) -> bool:
    """Deckelt **alle** Kopfzeilen des geparsten MIME-Baums im Objekt selbst (S-1).

    Der Deckel je gelesenem Wert (:func:`_raw_header_values`) schützt die Auswertung;
    dieser hier schützt die Rück-Serialisierung in :func:`_raw_bytes`, die jede Kopfzeile
    jedes Teils durch den Falter der Standardbibliothek schickt. Drei Schranken in
    Dokumentreihenfolge: jeder Wert auf :data:`_MAX_HEADER_CHARS`, die Summe der Zeichen
    auf :data:`_MAX_HEADER_CHARS_PER_MAIL`, die Zahl auf :data:`_MAX_HEADERS_PER_MAIL`.
    Ist ein Gesamtbudget erschöpft, behält der laufende Teil die bis dahin gezählten
    Kopfzeilen, und alle **weiteren Teile werden aus dem Baum entfernt** — die Mail ist
    ab dort abgeschnitten, sichtbar über das Log, und alles Nachgelagerte (Hash,
    Sanitizer) sieht denselben, konsistenten Baum. Für gewöhnliche Mails greift keine
    Schranke: Das Objekt bleibt unberührt, die Serialisierung byteidentisch, der
    `content_hash` stabil.

    Bewusst iterativ (kein Rekursionsfehler bei tiefer Schachtelung — `build_raw_mail`
    wirft nie, ADR-020 (e)) und linear in der Zahl der Kopfzeilen: Die Kopfzeilenliste
    eines Teils wird als Ganzes ersetzt, nicht Eintrag für Eintrag gelöscht (`del
    part[name]` läuft je Aufruf über alle Kopfzeilen und wäre bei vielen verschiedenen
    Namen quadratisch).

    Returns:
        True, wenn irgendetwas gekürzt oder entfernt wurde.
    """
    chars_left = _MAX_HEADER_CHARS_PER_MAIL
    headers_left = _MAX_HEADERS_PER_MAIL
    changed = False
    exhausted = False
    kept_ids: set[int] = set()
    multiparts: list[Message] = []
    stack: list[Message] = [message]
    while stack and not exhausted:
        part = stack.pop()
        kept_ids.add(id(part))
        # `raw_items()` statt `items()`: die gespeicherten Werte selbst (kein Header-Objekt
        # als Kopie), damit eine Ersetzung die Rohform behält.
        items = list(part.raw_items())
        kept: list[tuple[str, object]] = []
        for name, value in items:
            if headers_left <= 0 or chars_left <= 0:
                exhausted = True
                break
            capped, length = _cap_header_value_with_length(value)
            headers_left -= 1
            chars_left -= length
            kept.append((name, capped))
        if len(kept) != len(items) or any(
            new is not old for (_n, new), (_o, old) in zip(kept, items, strict=False)
        ):
            _replace_headers(part, kept)
            changed = True
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                multiparts.append(part)
                stack.extend(child for child in reversed(payload) if isinstance(child, Message))
    if exhausted or stack:
        for part in multiparts:
            payload = part.get_payload()
            if not isinstance(payload, list):
                continue
            remaining = [child for child in payload if id(child) in kept_ids]
            if len(remaining) != len(payload):
                part.set_payload(remaining)
                changed = True
    return changed


def _replace_headers(part: Message, headers: list[tuple[str, object]]) -> None:
    """Ersetzt die komplette Kopfzeilenliste eines Teils in einem Zug (S-1).

    `email.message.Message` hat keine öffentliche Operation dafür; `_headers` ist seit
    Python 2 die Liste von ``(name, wert)``-Paaren, die `raw_items()`, `items()` und
    der Generator lesen. `test_s1_kopfzeilen_ersetzen_ist_sichtbar` prüft, dass der
    Generator und `get_all` das Ergebnis sehen — ändert sich das in einer künftigen
    Standardbibliothek, fällt der Test.
    """
    part._headers = list(headers)  # type: ignore[attr-defined]


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


def _decode_subject(value: object) -> str:
    """Betreff dekodieren wie `imap_tools.MailMessage.subject`, aber auf dem gedeckelten Wert.

    Formgleich mit imap-tools (`decode_header` je Teil, `decode_value` mit dem Charset des
    Teils, `errors="ignore"`), damit sich für gewöhnliche Betreffs nichts ändert (S-1).
    """
    return "".join(
        decode_value(text, charset)
        for text, charset in decode_header(value)  # type: ignore[arg-type]
    )


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


#: Abschluss eines RFC-2047-Wortes.

#: Obergrenze für den **rohen** Wert eines anzeigenamentragenden Headers, angewandt vor
#: jeder Verarbeitung (R-8, Schicht a). RFC 5322 §2.1.1 erlaubt 998 Zeichen je Zeile;
#: gefaltete Adress-Header realer Mail liegen weit darunter, 4096 Zeichen sind also
#: grosszügig. Der Deckel ist die Schicht, die **jede** Kopfzeilen-Verarbeitung deckt —
#: auch `decode_header` selbst, das bei kaputten kodierten Wörtern quadratisch ist
#: (gemessen: 160-KiB-`Subject` = 8,6 s CPU). Er ist bewusst kein Config-Feld: Ein Header
#: jenseits davon ist kein Betriebsfall, sondern ein defekter oder bösartiger.
_MAX_HEADER_CHARS = 4096

#: Höchstzahl der Werte, die von **einem** Headernamen gelesen werden (S-1). Ein
#: mehrfach vorkommender Header (`Authentication-Results` je Hop, versehentlich doppelte
#: `To:`) bleibt damit vollständig; eine Mail aus einer Million winziger `To:`-Zeilen
#: nicht. Wie `_MAX_HEADER_CHARS` bewusst kein Config-Feld.
_MAX_HEADER_VALUES = 32

#: Höchstzahl der Empfänger in `RawMail.to_addrs` (S-1). Weitere werden verworfen; das
#: Feld dient der Anzeige und dem Vergleich, nicht der Zustellung.
MAX_RECIPIENTS = 200

#: Gesamtbudget an Kopfzeilen-**Zeichen** je Mail über alle MIME-Teile (S-1). Grund ist
#: die Rück-Serialisierung in :func:`_raw_bytes`: `Message.as_bytes()` faltet jede
#: Kopfzeile neu (`Header.encode`, rund 4 µs je Whitespace-Stück) — ein 20-MB-`name`-
#: Parameter eines Teils kostete 13,5 s, und der Deckel je Wert allein hilft nicht, weil
#: ein Angreifer ihn mit der Zahl der Kopfzeilen und Teile multipliziert. Gemessen:
#: 256 KiB der dichtesten Form (`a a a …`) kosten 0,15 s. Reale Mails liegen weit
#: darunter (Top-Level 2 bis 20 KB, ein Teil einige hundert Byte).
_MAX_HEADER_CHARS_PER_MAIL = 256 * 1024

#: Gesamtbudget an Kopfzeilen-**Zahl** je Mail über alle MIME-Teile (S-1): rund 50 µs
#: Serialisierung je Kopfzeile, 250 000 Kopfzeilen à 80 Byte kosteten 13 s. 4096 sind
#: acht je Teil bei voller Teilezahl (`sanitizer.MAX_MIME_PARTS` = 500); gemessen 0,42 s.
_MAX_HEADERS_PER_MAIL = 4096

#: FETCH-Teile für den Kopfzeilen-Abruf einer unparsbaren Mail (O-1, zweite Iteration):
#: nur die Kopfzeilen, per Teilabruf (RFC 3501 `<offset.count>`) server-seitig auf das
#: Kopfzeilenbudget begrenzt, dazu Grösse und Eingangszeit für den Ersatz-Dedupe-Key.
#: `PEEK`, damit der Abruf das `\Seen`-Flag nicht setzt (erst nach der Buchung, ADR-019).
_HEADER_FETCH_PARTS = (
    f"(BODY.PEEK[HEADER]<0.{_MAX_HEADER_CHARS_PER_MAIL}> RFC822.SIZE INTERNALDATE)"
)

#: `<lokalteil@domain>`-Klammer eines Headers (R-9, zweiter Griff). Beide Teile
#: schliessen `@`, `<`, `>` und Whitespace aus — das Muster ist damit eindeutig und
#: linear, es kann nicht zurücksetzen. Seit S-2 wird es nur noch mit `match` an der
#: **ersten** spitzen Klammer des kommentar- und quote-freien Texts angesetzt.

#: Ein schlichtes Wort (Buchstaben, Ziffern, Unterstrich, Bindestrich) — nur so ein
#: Lokalteil ohne Domain darf in der Outlook-Form als Anzeigename gelten (O-3).
_PLAIN_WORD_RE = re.compile(r"[\w\-]+")

#: Anzeigeform eines vorhandenen, aber unlesbaren Adress-Headers (O-3). Ein Kommentar,
#: damit `parseaddr` daraus nie eine Adresse liest.
_UNREADABLE_HEADER = "(unreadable)"

def _safe_parseaddr(text: str) -> tuple[str, str] | None:
    """`parseaddr` ohne Ausnahme: ``None``, wenn der Legacy-Parser scheitert.

    `email.utils.parseaddr`/`getaddresses` sind rekursiv (`g:` mal 1000 in 2 KB reicht für
    einen `RecursionError`) — auf angreiferkontrollierten Rohwerten laufen sie deshalb nur
    noch hier abgesichert (O-3, dritter Griff; ADR-020 (e): `build_raw_mail` wirft nie).
    """
    try:
        return parseaddr(text)
    except Exception:
        return None


def _safe_getaddresses(values: list[str]) -> list[tuple[str, str]]:
    """`getaddresses` ohne Ausnahme: leere Liste, wenn der Legacy-Parser scheitert."""
    try:
        return getaddresses(values)
    except Exception:
        return []

#: Der RFC-5322-Parser der Standardbibliothek — derselbe, den `email.policy.default` und
#: damit die Mailprogramme benutzen. Er ist seit O-3 die **einzige** Instanz, die aus einem
#: Adress-Header Anzeigename und Adresse liest: Jede Nachbildung (Maske für kodierte
#: Wörter, Klammer-Rückfall, Kommentar-Scanner — NF-1, Iterationen 2 bis 5) war an einer
#: Stelle enger oder weiter als er, und genau dort zeigte das Werkzeug eine andere
#: Absender-Domain als das Mailprogramm. Wer den Parser selbst befragt, sieht, was der
#: Nutzer sieht — und kann davon höchstens in die sichere Richtung abweichen.
_HEADER_REGISTRY = HeaderRegistry()


def _parse_address_header(raw_text: str) -> tuple[str, str, str, list[str], bool]:
    """Erste Angabe eines Adress-Headers: ``(anzeigename, adresse, domain, alle, lesbar)``.

    `alle` sind die Adressen (`addr_spec`) sämtlicher brauchbarer Angaben — bei `Reply-To`
    antworten Mailprogramme an alle, der Sanitizer vergleicht deshalb jede mit dem Absender.

    `lesbar = False`, wenn der Parser die Kopfzeile nicht verarbeiten kann (auch ein
    `RecursionError` bei tief verschachtelten Kommentaren) — der Aufrufer führt den Header
    dann als `(unreadable)`. Adresse und Domain sind nur gesetzt, wenn die Angabe einen
    Lokalteil **und** eine hostname-förmige Domain trägt (:data:`_HOSTNAME_RE`); alles
    andere ist „unbekannt", und der Sanitizer warnt (R-9). Gruppen zählen über ihre
    Mitglieder, wie bei `email.policy.default`.

    Die Adresse ist `addr_spec` des Parsers — **quotiert**, wo der Lokalteil es verlangt
    (`"x@bank.example,"@evil.example`); die Domain kommt direkt vom Parser, nie aus einem
    zweiten Parse der Adresszeichenkette (dort trennte `getaddresses` am Komma im Lokalteil
    und las `bank.example`, Skeptiker O-3, zweiter Durchgang).

    Outlook-Form: `Mueller, Hans <h@firma.example>` liest der Parser als zwei Angaben, die
    erste ohne Adresse — Mailprogramme zeigen trotzdem die eine Adresse, eine Warnung wäre
    ein Fehlalarm. Deshalb zählt die einzige Angabe mit Adresse, wenn der **Rohwert** genau
    ein `@` und höchstens ein `<` trägt: Dann kann keine zweite Adresse und kein
    Adressfragment (`<@evil.example>, <x@bank.example>`) im Spiel sein.
    """
    try:
        header = _HEADER_REGISTRY("from", raw_text)
        if not isinstance(header, AddressHeader):  # pragma: no cover - Registry-Vertrag
            return "", "", "", [], False
        addresses = list(header.addresses)
    except Exception:  # defekte Kopfzeile: fail-closed, nie werfen (ADR-020 (e))
        return "", "", "", [], False
    if not addresses:
        return " ".join(raw_text.split()), "", "", [], True

    def usable(entry: Address) -> bool:
        return bool(entry.username) and bool(
            _HOSTNAME_RE.fullmatch((entry.domain or "").lower())
        )

    chosen = addresses[0]
    display = chosen.display_name or ""
    all_usable = [entry.addr_spec for entry in addresses if usable(entry)]
    if not usable(chosen):
        outlook_form = raw_text.count("@") == 1 and raw_text.count("<") <= 1
        if len(all_usable) == 1 and outlook_form:
            chosen = next(entry for entry in addresses if usable(entry))
            # Die Wörter vor der Adresse legt der Parser als Lokalteile ohne Domain ab
            # (`Mueller` in `Mueller, Hans <h@…>`). Sie zählen nur als Name, wenn sie ein
            # schlichtes Wort sind — `https://evil.example/x, Hans <…>` oder
            # `bank.example, Hans <…>` dürfen keinen Anzeigenamen liefern (Skeptiker O-3).
            upto = addresses[: addresses.index(chosen) + 1]
            parts = []
            for entry in upto:
                word = entry.username or ""
                if entry.display_name:
                    parts.append(entry.display_name)
                elif _PLAIN_WORD_RE.fullmatch(word):
                    parts.append(word)
            display = " ".join(part for part in parts if part)
        else:
            return display, "", "", all_usable, True
    return display, chosen.addr_spec, chosen.domain, all_usable, True


def _compose_display_address(name: str, address: str) -> str:
    """Baut ``Name <adresse>`` so, dass `parseaddr` genau diese beiden Teile zurückgibt.

    Der Name kommt aus dem Parser und ist damit angreiferkontrolliert; ohne Selbstprüfung
    könnte er die Adresse erneut verschieben (HC2-2). Geprüft wird deshalb gegen
    `parseaddr` selbst: erst unquotiert (das ist die Form, die heutige Tests und der
    Korpus sehen), sonst quotiert, sonst bleibt nur die nackte Adresse.
    """
    if not address:
        # O-3: Ohne Adresse darf die Anzeigeform nicht selbst wie eine aussehen —
        # `parseaddr` (Sanitizer, `_reply_to_mismatch`) läse sonst `x@bank.example` aus
        # dem dekodierten Namen `=?utf-8?Q?<x@bank.example>?=` als Adresse.
        parsed = _safe_parseaddr(name)
        return name if parsed is not None and not parsed[1] else ""
    if not name:
        return address
    plain = f"{name} <{address}>"
    if _safe_parseaddr(plain) == (name, address):
        return plain
    quoted = f'"{name}" <{address}>'
    if _safe_parseaddr(quoted) == (name, address):
        return quoted
    return address


def _address_header(msg: MailMessage, name: str) -> tuple[str, str, str, list[str]]:
    """Anzeigenamentragender Header (`From`, `Reply-To`) → ``(anzeigeform, adresse)``.

    Seit O-3 liest der RFC-5322-Parser der Standardbibliothek die erste Angabe
    (:func:`_parse_address_header`); er dekodiert kodierte Wörter im Anzeigenamen selbst
    (HC-23) und entscheidet über Kommentare, Quoted Strings, Gruppen und Routen genau wie
    das Mailprogramm des Nutzers (HC2-2: „ersetzen" und „löschen" der Absender-Domain).
    Trägt der Rohwert keine Kodierung und liest `parseaddr` dieselbe Adresse, bleibt er
    unverändert — es gibt dann nichts zu reparieren.
    """
    values = _raw_header_values(msg, name)
    if not values:
        return "", "", "", []
    if len(str(values[0])) >= _MAX_HEADER_CHARS:
        # O-3: Ein Adress-Header am 4096-Zeichen-Deckel ist abgeschnitten (die Werte im
        # Baum sind bereits gedeckelt, S-1). Aus dem Rest liest der Parser sonst eine
        # Scheindomain wie `bank.examp`; kein echter `From` ist 4 KB lang. Unlesbar
        # heisst: Domain unbekannt, Warnung feuert. Unterhalb des Deckels kostet der
        # RFC-5322-Parser im ungünstigsten Fall (4 KB aus Kommas oder Punkten) rund 80 ms
        # je Header — deterministisch begrenzt, deshalb hingenommen (Skeptiker O-3).
        return _UNREADABLE_HEADER, "", "", []
    raw_text = _capped_raw_header(values[0])
    if not raw_text:
        return "", "", "", []
    display, address, domain, all_addresses, readable = _parse_address_header(raw_text)
    if not readable:
        return _UNREADABLE_HEADER, "", "", []
    parsed_raw = _safe_parseaddr(raw_text)
    if "=?" not in raw_text and parsed_raw is not None and parsed_raw[1] == address:
        return raw_text, address, domain, all_addresses
    final = _compose_display_address(_clean_display_name(display), address)
    if not address and not final:
        # O-3: Der Header ist da, liefert aber weder Adresse noch einen Namen, der nicht
        # selbst wie eine Adresse aussieht. Ein leerer Rückgabewert hiesse für den
        # Sanitizer „kein Header" — und ein fehlender Reply-To löst keine Warnung aus.
        return _UNREADABLE_HEADER, "", "", []
    return final, address, domain, all_addresses


def _capped_raw_header(value: object) -> str:
    """Rohwert eines anzeigenamentragenden Headers, entzerrt und gefaltet (R-8, Schicht a).

    Der Deckel auf :data:`_MAX_HEADER_CHARS` liegt seit S-1 in :func:`_raw_header_values`
    — also **vor** jeder Verarbeitung, auch vor der 8-Bit-Vorentzerrung, die selbst
    `decode_header` ruft (bei kaputten kodierten Wörtern quadratisch). Hier kommt nur
    noch ein bereits gedeckelter Wert an; ein Header jenseits der Obergrenze ist ohnehin
    defekt oder bösartig, sein Anfang genügt, um Adresse und Anzeigename zu bestimmen.
    """
    return _collapse(_predecode_eight_bit(_cap_header_value(value)))


#: Hostname-Form einer Domain (ASCII-Marken, Punkte, Bindestriche; Punycode inklusive).
_HOSTNAME_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def _domain_of(address: str) -> str:
    """Extrahiert die Domain aus einer Adresse (oder einem `Name <adr>`-Header), lowercase.

    Leerstring, wenn keine Domain erkennbar ist — der Sanitizer/Kritiker behandelt das als
    "Absender-Domain unbekannt" (docs/ARCHITECTURE.md §3: `from_domain` ist Pflichtfeld).
    """
    candidates = _safe_getaddresses([address.replace("\r", " ").replace("\n", " ")])
    for _name, email_address in candidates:
        local, at_sign, domain = email_address.rpartition("@")
        if not at_sign or not local:
            # Kein "lokalteil@domain" — z. B. ein blosser Anzeigename wie "Postmaster".
            continue
        cleaned = domain.strip().strip("<>[]").rstrip(".").lower()
        # O-3: Nur eine hostname-förmige Domain zählt. Reste ungültiger Kodierungssyntax
        # (`bank.example?,?=`) oder Klammerbruchstücke ergäben sonst eine „Domain", die
        # kein Mailprogramm zeigt — unbekannt ist dann die sichere Richtung.
        if cleaned and _HOSTNAME_RE.fullmatch(cleaned):
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

    Drei Stufen, jede abgesichert (O-1): `as_bytes()`, dann `str()`, dann die Kopfzeilen
    des obersten Teils plus Hinweiszeile. Der Rückfall `str()` läuft über **denselben**
    rekursiven Generator wie `as_bytes()` — bei tiefer Schachtelung scheiterte er deshalb
    genauso, und zwar ungefangen. Hier wirft jetzt keine Stufe mehr nach aussen; die letzte
    liefert einen minimalen, aber gültigen Rohwert. ADR-020 (e): `build_raw_mail` wirft nie.
    """
    try:
        return msg.obj.as_bytes()
    except Exception:  # defektes MIME ist der Normalfall, nicht die Ausnahme
        pass
    try:
        return str(msg.obj).encode("utf-8", "replace")
    except Exception:  # z. B. RecursionError: derselbe rekursive Generator wie oben
        pass
    return _headers_only_bytes(msg)


def _headers_only_bytes(msg: MailMessage) -> bytes:
    """Letzter Rückfall für :func:`_raw_bytes`: Kopfzeilen des obersten Teils + Hinweiszeile.

    Bewusst ohne Generator, ohne `walk()` und ohne Rekursion — genau die Mechanik, die in
    den Stufen davor scheitert. Die Werte sind bereits gedeckelt (:func:`_cap_message_headers`);
    Zeilenumbrüche werden entfernt, damit ein Headerwert keine zusätzliche Kopfzeile
    vortäuschen kann. Das Ergebnis ist parsbares MIME: Der Sanitizer sieht eine Mail mit
    Kopfzeilen und einem kurzen Klartext-Body, der nichts aus der Mail enthält.
    """
    lines: list[bytes] = []
    try:
        items = list(msg.obj.raw_items())
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        items = []
    for name, value in items[:_MAX_HEADERS_PER_MAIL]:
        try:
            header = _collapse(f"{name}: {value}")
        except Exception:  # pragma: no cover - kaputte Header-Objekte
            continue
        lines.append(header.encode("utf-8", "replace"))
    lines.append(b"")
    return b"\r\n".join(lines) + b"\r\n" + _UNSERIALIZABLE_BODY


def _content_hash(mime_bytes: bytes) -> str:
    """SHA-256 (hex) über die MIME-Bytes — das zweite Dedupe-Merkmal (ADR-079, HC-10).

    Anders als `Message-ID` kann ein Absender diesen Wert nicht auf eine fremde Mail legen:
    Er ist genau dann gleich, wenn die Mail dieselbe ist.
    """
    return hashlib.sha256(mime_bytes).hexdigest()


def _best_effort(read: Callable[[], str]) -> str:
    """Liest einen Wert, der auch scheitern darf — für den Notpfad :func:`_unreadable_raw_mail`."""
    try:
        return read() or ""
    except Exception:
        return ""


class UnparsableMailMessage(MailMessage):
    """Platzhalter für eine Mail, die imap-tools nicht parsen konnte (O-1, zweite Iteration).

    `MailMessage.__init__` ruft `email.message_from_bytes` — rekursiv und ausserhalb jeder
    Schranke dieses Projekts. Scheitert es, ist von der Mail nur bekannt, was der Server
    ausserhalb des Parsers hergibt: die UID, `RFC822.SIZE`, `INTERNALDATE` und die
    Kopfzeilen des obersten Teils aus einem zweiten, gedeckelten `UID FETCH`. Die Kopfzeilen
    werden mit :class:`email.parser.BytesHeaderParser` gelesen — der liest nur bis zur
    ersten Leerzeile und steigt in keinen Teil hinab, kann also nicht rekursieren.

    Die Klasse ist absichtlich eine `MailMessage`: `poll_once` und `mark_processed` sehen
    dieselbe Schnittstelle (`uid`, `obj`, `_raw_flag_data`), :func:`_unreadable_raw_mail`
    baut daraus die Ersatz-`RawMail`. Der Konstruktor der Oberklasse wird bewusst **nicht**
    gerufen — er wäre genau der Parse, der gescheitert ist.

    Attributes:
        parse_error: Klassenname der Ausnahme aus dem Parse (für das Log, I5: kein Inhalt).
        headers_fetched: True, wenn der Kopfzeilen-Abruf geklappt hat; sonst ist `obj`
            leer und der Ersatz-Dedupe-Key hängt allein an der UID.
    """

    def __init__(self, uid: str, fetch_data: list[object] | None, error: BaseException) -> None:
        self._raw_uid_data = f"(UID {uid})".encode()
        self._raw_flag_data: list[bytes] = []
        header_bytes = b""
        for item in fetch_data or []:
            if isinstance(item, bytes):
                self._raw_flag_data.append(item)
            elif isinstance(item, tuple) and len(item) >= 2:
                if isinstance(item[0], bytes):
                    self._raw_flag_data.append(item[0])
                if isinstance(item[1], bytes):
                    header_bytes = item[1]
        # Der Server hat auf `<0.N>` geschnitten; die Schranke gilt auch für Server, die
        # den Teilabruf ignorieren.
        self.obj = BytesHeaderParser().parsebytes(header_bytes[:_MAX_HEADER_CHARS_PER_MAIL])
        self.parse_error = type(error).__name__
        self.headers_fetched = fetch_data is not None


def _unreadable_raw_mail(msg: MailMessage) -> RawMail:
    """Notfall-`RawMail` für eine Mail, an der :func:`build_raw_mail` gescheitert ist (O-1).

    `build_raw_mail` ist so gebaut, dass es nicht wirft (ADR-020 (e)) — aber „gebaut" ist
    keine Garantie, und ein Fehler genau hier hielt den gesamten Dienst an: Die Mail bleibt
    ungelesen im Postfach und kippt jeden weiteren Poll, alle danach eintreffenden Mails
    werden nie verarbeitet. Deshalb gibt es diesen zweiten Boden: Jedes Feld wird einzeln
    und abgesichert gelesen, was nicht lesbar ist, bleibt leer („unbekannt", ADR-020 (b)/(c)).

    Der Dedupe-Key ist die `Message-ID`, wenn sie lesbar ist — dann trifft ein späterer,
    erfolgreicher Lauf denselben Key. Sonst ein Hash aus UID, den rohen FETCH-Flags (die
    `INTERNALDATE` und `RFC822.SIZE` tragen) und den lesbaren Kopfzeilen; dieser Wert ist
    über Neustarts hinweg stabil, solange die Mail im Postfach dieselbe ist (ADR-019).

    `mime_bytes` trägt bewusst keinen Mail-Inhalt: Die Mail läuft über
    `RawMail.ingest_failed` unmittelbar in den Fail-closed-Pfad der Pipeline.
    """
    # S-1: Auch die Kopfzeilen des Notpfads sind gedeckelt; ein Fehler dabei ist egal.
    _best_effort(lambda: str(_cap_message_headers(msg.obj)))
    message_id = _best_effort(lambda: _header(msg, "Message-ID") or "")
    digest = hashlib.sha256()
    digest.update(_best_effort(lambda: msg.uid or "").encode("utf-8", "replace"))
    digest.update(b"\x00")
    try:
        for flag_data in msg._raw_flag_data:  # INTERNALDATE/RFC822.SIZE aus der FETCH-Antwort
            digest.update(bytes(flag_data))
            digest.update(b"\x00")
    except Exception:  # pragma: no cover - andere imap-tools-Fassung
        pass
    digest.update(_headers_only_bytes(msg))
    return RawMail(
        message_id=message_id or None,
        dedupe_key=message_id or f"{_FALLBACK_PREFIX}{digest.hexdigest()}",
        from_addr=_best_effort(lambda: _header(msg, "From") or ""),
        from_domain="",
        subject_raw=_collapse(_best_effort(lambda: _header(msg, "Subject") or "")),
        mime_bytes=_UNSERIALIZABLE_BODY,
        size_bytes=0,
        # Leer heisst „unbekannt" (ADR-079): Der Platzhalter ist kein Inhaltsmerkmal — hätte
        # er einen Hash, wäre jede andere unlesbare Mail unter demselben Key eine
        # „Kollision" und ein späterer geglückter Lauf ebenfalls.
        content_hash="",
        ingest_failed=True,
    )


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
    try:
        # Zuerst die Tiefe (O-1): Jeder folgende Baumdurchlauf — Kopfzeilendeckel,
        # `_body_prefix`, Serialisierung — arbeitet danach auf einem flachen Baum.
        if _cap_message_depth(msg.obj):
            # Kein Mail-Inhalt im Log (I5): nur die Tatsache.
            logger.info("mail_mime_depth_capped")
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        pass
    try:
        if _cap_message_headers(msg.obj):
            # Kein Mail-Inhalt im Log (I5): nur die Tatsache.
            logger.info("mail_headers_capped")
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        pass
    message_id = _header(msg, "Message-ID")
    from_addr, from_address, from_domain, _from_all = _address_header(msg, "From")
    reply_to_display, reply_to_address, _rt_domain, reply_to_all = _address_header(
        msg, "Reply-To"
    )
    date_str = _header(msg, "Date") or ""

    try:
        subject_values = _raw_header_values(msg, "Subject")
        # R-8/S-1: nicht `msg.subject` — das liest den **vollen** Rohwert an der
        # Obergrenze vorbei und ruft `decode_header` darauf (bei kaputten kodierten
        # Wörtern quadratisch, 160 KiB Betreff = 8,6 s CPU). Hier läuft exakt der
        # Dekodierweg von imap-tools (`decode_header` + `decode_value`), aber auf dem
        # gedeckelten Wert. Sichtbar ändert der Deckel nichts: Der Sanitizer kürzt den
        # Betreff ohnehin auf 300 Zeichen.
        subject_raw = _collapse(_decode_subject(subject_values[0])) if subject_values else ""
    except Exception:  # kaputte RFC-2047-Kodierung im Betreff
        subject_raw = _header(msg, "Subject") or ""

    dedupe_key = message_id or _fallback_dedupe_key(
        from_addr, date_str, subject_raw, _body_prefix(msg)
    )

    # S-1: Die Werte sind in `_raw_header_values` bereits gedeckelt; die Empfängerzahl
    # zusätzlich, damit `to_addrs` nie mehr als eine Handvoll Kilobyte trägt.
    to_addrs = [
        email_address
        for _name, email_address in _safe_getaddresses(_header_values(msg, "To"))
        if email_address
    ][:MAX_RECIPIENTS]

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
        from_domain=from_domain.lower(),
        from_address=from_address,
        reply_to=reply_to_display or None,
        reply_to_address=reply_to_address if reply_to_display else None,
        reply_to_addresses=reply_to_all if reply_to_display else [],
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
        """Liefert alle ungesehenen Mails des konfigurierten Ordners — **je UID einzeln**.

        ``mark_seen=False`` ist entscheidend: Das `\\Seen`-Flag wird erst nach erfolgreicher
        Verarbeitung gesetzt (:meth:`mark_processed`). Bricht der Prozess vorher ab, taucht
        die Mail beim nächsten Poll wieder auf — und wird dank State-DB als Duplikat erkannt
        (F-ING-2).

        Ablauf (O-1, zweite Iteration): ein ``UID SEARCH UNSEEN`` (`MailBox.uids`), dann je
        UID ein eigenes ``UID FETCH`` über :meth:`_fetch_one`. Das sind exakt die Kommandos,
        die `MailBox.fetch(bulk=False)` ohnehin absetzt — nur dass der Parse jeder Mail jetzt
        in seinem eigenen Schutz läuft: Eine Mail, die imap-tools nicht parsen kann, wird als
        :class:`UnparsableMailMessage` geliefert statt den ganzen Zyklus zu kippen. Eine
        zwischen SEARCH und FETCH verschwundene Mail (anderer Client) wird übersprungen.

        Raises:
            ImapConnectionError: Verbindung weg, SEARCH oder FETCH fehlgeschlagen.
        """
        try:
            uids = list(self.mailbox.uids(AND(seen=False)))
        except Exception as exc:  # ImapToolsError, OSError oder imaplib-eigene Typen
            raise ImapConnectionError(
                f"Searching unseen mail failed: {type(exc).__name__}"
            ) from exc
        for uid in uids:
            msg = self._fetch_one(uid)
            if msg is not None:
                yield msg

    def _fetch_one(self, uid: str) -> MailMessage | None:
        """Holt **eine** Mail; ein gescheiterter Parse wird zum Platzhalter, kein Abbruch.

        Zwei Fehlerklassen, sauber getrennt: Transport- und Protokollfehler
        (`ImapToolsError`, `OSError`, `imaplib.IMAP4.error` — imaplib meldet Socketfehler
        als eigenes `abort`) sind Verbindungsfehler und gehen als
        :class:`ImapConnectionError` nach oben, damit der Runner reconnectet. **Alles
        andere** kommt aus `MailMessage.__init__` — dem eifrigen Parse in imap-tools —
        und ist eine Eigenschaft der Mail, nicht der Verbindung: `RecursionError` bei
        rund 1000 `message/rfc822`-Ebenen (31 KB), `MemoryError`, jede kaputte
        Parser-Ausnahme. Dafür holt :meth:`_unparsable_mail` die Kopfzeilen nach.

        Returns:
            Die Mail, der Platzhalter — oder ``None``, wenn die UID inzwischen weg ist.
        """
        try:
            messages = list(
                self.mailbox.fetch(uid_list=[uid], mark_seen=False, bulk=False)
            )
        except (ImapToolsError, OSError, imaplib.IMAP4.error) as exc:
            raise ImapConnectionError(
                f"Fetching unseen mail failed: {type(exc).__name__}"
            ) from exc
        except Exception as exc:  # der Parse der Mail selbst (RecursionError, …)
            return self._unparsable_mail(uid, exc)
        if not messages:
            return None
        return messages[0]

    def _unparsable_mail(self, uid: str, error: BaseException) -> UnparsableMailMessage:
        """Baut den Platzhalter einer unparsbaren Mail aus einem Kopfzeilen-Abruf.

        ``UID FETCH <uid> (BODY.PEEK[HEADER]<0.N> RFC822.SIZE INTERNALDATE)`` — nur
        Kopfzeilen, server-seitig auf das Kopfzeilenbudget geschnitten, `PEEK` ohne
        Seen-Flag. Der Abruf kostet **ein** zusätzliches Kommando, und zwar nur für diese
        eine Mail; gesunde Mails kosten keins mehr als vorher. Scheitert auch dieser Abruf
        (Ablehnung oder Verbindungsfehler), entsteht der Platzhalter mit leeren Feldern —
        der Ersatz-Dedupe-Key hängt dann allein an der UID. Ein echter Verbindungsabbruch
        fällt gleich danach beim `UID STORE` auf und geht dort als
        :class:`ImapConnectionError` nach oben.
        """
        fetch_data: list[object] | None = None
        try:
            status, data = self.mailbox.client.uid("FETCH", uid, _HEADER_FETCH_PARTS)
            if str(status).upper() == "OK" and data and data[0] is not None:
                fetch_data = list(data)
        except Exception as exc:  # jede Fehlerklasse: der Platzhalter kommt ohne Kopfzeilen
            logger.warning(
                "mail_header_fetch_failed", extra={"error": type(exc).__name__}
            )
        return UnparsableMailMessage(uid, fetch_data, error)

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

    Auch ein Fehler **vor** der Verarbeitung beendet den Durchlauf nicht (O-1): Scheitert
    `build_raw_mail` wider Erwarten (ADR-020 (e)) — oder schon der Parse in imap-tools, den
    :meth:`ImapClient.fetch_unseen` je UID isoliert und als :class:`UnparsableMailMessage`
    meldet (Log `mail_unparsable`) —, tritt :func:`_unreadable_raw_mail` an
    seine Stelle — die Mail wird über einen Ersatz-Dedupe-Key beansprucht, läuft als
    `ingest_failed` in den Fail-closed-Pfad (Metadaten-Notiz, Status `failed`,
    Fehlerklasse `ingest_error`) und wird als gelesen markiert. Sonst bliebe sie ungelesen
    im Postfach und kippte jeden weiteren Poll — mit ihr alle später eintreffenden Mails.

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
        if isinstance(msg, UnparsableMailMessage):
            # O-1, zweite Iteration: imap-tools konnte die Mail nicht parsen; der Client hat
            # sie über einen Kopfzeilen-Abruf isoliert. Sie geht denselben Weg wie eine Mail,
            # an der `build_raw_mail` scheitert — nur der Logeintrag benennt den Fall.
            raw = _unreadable_raw_mail(msg)
            logger.error(
                "mail_unparsable",
                extra={"mail": dedupe_hash(raw.dedupe_key)[:12], "error": msg.parse_error},
            )
        else:
            try:
                raw = build_raw_mail(msg)
            except Exception as exc:  # O-1: keine einzelne Mail darf den Zyklus anhalten
                raw = _unreadable_raw_mail(msg)
                # ADR-047/I5: nur die Exception-Klasse, kein Traceback ausserhalb von DEBUG.
                logger.error(
                    "mail_ingest_failed",
                    extra={
                        "mail": dedupe_hash(raw.dedupe_key)[:12],
                        "error": type(exc).__name__,
                    },
                    exc_info=traceback_enabled(logger),
                )
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
            if raw.ingest_failed:
                # O-1: Die Pipeline hat die Metadaten-Notiz zugestellt (fail-closed, I6);
                # verarbeitet ist hier nichts. Der Status ist `failed` mit der Fehlerklasse
                # der Stufe 1 — im Runner-Betrieb schreibt ihn `_record_result` aus der
                # Notiz, hier schreiben wir ihn selbst.
                stats.failed += 1
                if write_result_status:
                    db.mark_status(
                        raw.dedupe_key, MailState.FAILED, error_class="ingest_error"
                    )
                logger.info("mail_unreadable", extra={"mail": key_short})
                _mark_processed_best_effort(client, msg, key_short)
                continue
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
