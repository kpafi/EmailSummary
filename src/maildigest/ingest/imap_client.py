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
import re
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
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


#: **Kopf** eines RFC-2047-Wortes (`=?charset?B?` / `=?charset?Q?`) im rohen Headerwert.
#:
#: Formgleich mit dem Kopf von `email.header.ecre` — dem Muster des Dekoders, der **nach**
#: der Maskierung läuft (NF-1, dritte Iteration, R-4). Die Maske darf nie enger sein als
#: der Dekoder: Ein leerer Charset (`=??Q?info@bank.example,?=`) oder ein Sprach-Tag
#: (`=?utf-8*de?Q?…?=`) genügte sonst, um an der Maske vorbei wieder Adresssyntax in den
#: Anzeigenamen zu schmuggeln. Deshalb `[^?]*` für den Charset.
#:
#: Das **Ende** (`?=`) sucht :func:`_mask_encoded_words` mit `str.find`, nicht mit einem
#: `.*?` im selben Muster (NF-1, vierte Iteration, R-8): `.*?` darf über `?` hinweglaufen,
#: und in einem Header ohne schliessendes `?=` scannte damit jede der n Startstellen den
#: ganzen Resttext — O(n²) (gemessen: 156-KiB-`From` = 18,2 s CPU, vorher 0,002 s). Der
#: Scanner liefert dieselben Segmente wie `ecre` (erstes `?=` nach dem Kopf, non-greedy),
#: braucht aber nur einen Durchlauf über den Text.
_ENCODED_WORD_START_RE = re.compile(r"=\?[^?]*\?[BbQq]\?")

#: Abschluss eines RFC-2047-Wortes.
_ENCODED_WORD_END = "?="

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

#: `<lokalteil@domain>`-Klammer eines Headers (R-9, zweiter Griff). Beide Teile
#: schliessen `@`, `<`, `>` und Whitespace aus — das Muster ist damit eindeutig und
#: linear, es kann nicht zurücksetzen. Seit S-2 wird es nur noch mit `match` an der
#: **ersten** spitzen Klammer des kommentar- und quote-freien Texts angesetzt.
_BRACKET_ADDRESS_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")

#: Stamm der Platzhalter, die kodierte Wörter beim Adress-Parsen vertreten (HC2-2-Rest).
#: Nur `[A-Za-z0-9]` — damit ist ein Platzhalter für `getaddresses` reiner Text und kann
#: keine Adressgrenze (`@`, `<`, `>`, `,`, `;`, `:`) erzeugen.
_PLACEHOLDER_STEM = "MDENCWORD"


def _mask_encoded_words(raw_text: str) -> tuple[str, dict[str, str], str]:
    """Ersetzt jedes RFC-2047-Wort durch einen adressneutralen Platzhalter (HC2-2-Rest).

    Der Rohwert-Parse allein genügt nicht: Die Q-Kodierung darf `@`, `<`, `>` und `,`
    **literal** führen, und `getaddresses` liest die Kodierungssyntax dann als
    Adresssyntax — `=?utf-8?Q?info@bank.example,?= <attacker@evil.example>` zerfällt in
    zwei Angaben, von denen die erste (der Anzeigename!) die Absender-Domain bestimmt.
    Maskiert ist ein kodiertes Wort ein einzelnes Wort ohne Sonderzeichen; die
    Adressgrenzen sind danach genau die, die der Header wirklich setzt.

    Returns:
        ``(maskierter Text, {platzhalter: kodiertes Wort}, Platzhalter-Stamm)``.
    """
    stem = _PLACEHOLDER_STEM
    while stem in raw_text:  # der Header führt den Stamm selbst: eindeutig machen
        stem += "X"
    words: dict[str, str] = {}
    pieces: list[str] = []
    position = 0
    while True:
        head = _ENCODED_WORD_START_RE.search(raw_text, position)
        if head is None:
            break
        end = raw_text.find(_ENCODED_WORD_END, head.end())
        if end < 0:
            # Kein schliessendes `?=` mehr — und für jede spätere Startstelle erst recht
            # nicht (sie liegt weiter rechts). Abbrechen statt weitersuchen: genau das
            # macht den Durchlauf linear (R-8).
            break
        token = f"{stem}{len(words)}"
        words[token] = raw_text[head.start() : end + len(_ENCODED_WORD_END)]
        pieces.append(raw_text[position : head.start()])
        pieces.append(token)
        position = end + len(_ENCODED_WORD_END)
    pieces.append(raw_text[position:])
    return "".join(pieces), words, stem


def _unmask_encoded_words(text: str, words: dict[str, str]) -> str:
    """Setzt die kodierten Wörter in einem **Namensteil** wieder ein (HC2-2-Rest)."""
    for token, word in words.items():
        text = text.replace(token, word)
    return text


def _outside_comments_and_quotes(text: str) -> tuple[str, list[int], bool]:
    """Zeichen eines Headers **ausserhalb** von Kommentaren und Quoted Strings (S-2).

    Linearer Scanner nach RFC 5322 §3.2.2 (Kommentare, verschachtelt, mit Quoted-Pair)
    und §3.2.4 (Quoted Strings mit Quoted-Pair). Innerhalb eines Kommentars öffnet `(`
    eine weitere Ebene, `\\x` schützt jedes Zeichen; innerhalb eines Quoted Strings zählt
    nur `"` (und `\\x`). Ein schliessendes `)` ohne offenen Kommentar ist gewöhnlicher
    Text — es kann nichts verbergen.

    Returns:
        ``(text_aussen, positionen, balanciert)`` — `positionen[i]` ist der Index des
        `i`-ten Aussenzeichens im Original, `balanciert` ist False, wenn am Ende ein
        Kommentar oder Quoted String offen ist (etwa weil der 4096-Zeichen-Deckel ihn
        zerschnitten hat).
    """
    chars: list[str] = []
    positions: list[int] = []
    depth = 0
    in_quote = False
    escaped = False
    for index, char in enumerate(text):
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
            continue
        if depth:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            continue
        if char == '"':
            in_quote = True
            continue
        if char == "(":
            depth = 1
            continue
        chars.append(char)
        positions.append(index)
    return "".join(chars), positions, not in_quote and depth == 0


def _first_address(text: str, *, placeholder_stem: str = "") -> tuple[str, str]:
    """Erste Angabe eines Adress-Headers als ``(name, adresse)`` (Rohwert-Parse).

    Ein Platzhalter im Adressteil bedeutet, dass dort ein kodiertes Wort stand — also ein
    Name, keine Adresse (HC2-2-Rest). Solche Angaben werden übersprungen; sie dürfen die
    Absender-Domain weder liefern noch löschen.

    S-2: Ist der Header unbalanciert (offener Kommentar oder Quoted String — auch als
    Folge des 4096-Zeichen-Deckels), gilt die Adresse als **unbekannt**, egal was
    `getaddresses` daraus macht: Ein offener Kommentar kann eine beliebige
    `<adresse>` tragen, und `getaddresses` liest ihn bei fehlender Klammer als Adressteil.
    Unbekannt ist die sichere Richtung — der Sanitizer warnt dann (R-9-Regel), statt eine
    fremde Domain zu zeigen.
    """
    outside, positions, balanced = _outside_comments_and_quotes(text)
    if not balanced:
        return " ".join(text.split()), ""
    pairs = getaddresses([text])
    if not pairs:
        return "", ""
    candidates = [
        (name, address)
        for name, address in pairs
        if not (placeholder_stem and placeholder_stem in address)
    ]
    for name, address in candidates:
        local, at_sign, domain = address.rpartition("@")
        if at_sign and local and domain.strip().strip("<>[]").rstrip("."):
            return name, address
    # R-9: `getaddresses` verwirft den Adressteil komplett, sobald hinter der spitzen
    # Klammer noch ein Token steht — `<attacker@evil.example> MDENCWORD0` liefert
    # `[('', '')]`. Die Absender-Domain verschwände damit, und das Schutzziel von HC2-2
    # nennt „löschen" ausdrücklich neben „ersetzen". Zweiter, konservativer Griff: die
    # **erste** spitze Klammer des Texts **ausserhalb** von Kommentaren und Quoted Strings
    # (S-2: Rückfälle lesen nie Kommentar- oder Quoted-String-Inhalt), und nur, wenn genau
    # dort eine vollständige `<lokalteil@domain>`-Klammer steht — `<<x@bank.example>…>`
    # liefert damit nichts. Bewusst die erste und nicht die letzte — `email.policy.default`
    # nimmt ebenfalls die erste Angabe, und die letzte zu nehmen hiesse, dass ein
    # angehängtes `<info@bank.example>` die Domain doch wieder übernimmt.
    first_bracket = outside.find("<")
    bracketed = (
        _BRACKET_ADDRESS_RE.match(outside, first_bracket) if first_bracket >= 0 else None
    )
    if bracketed is not None:
        address = bracketed.group(1)
        local, at_sign, domain = address.rpartition("@")
        if at_sign and local and domain.strip().strip("<>[]").rstrip("."):
            name = text[: positions[first_bracket]]
            return " ".join(name.split()), address
    if candidates:
        return candidates[0]
    # Nur Platzhalter: Der Header trägt gar keine Adresse, alles davon ist Name.
    return " ".join(part for pair in pairs for part in pair if part), ""


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
    raw_text = _capped_raw_header(values[0])
    if not raw_text:
        return "", ""
    masked, encoded_words, stem = _mask_encoded_words(raw_text)
    display, address = _first_address(masked, placeholder_stem=stem)
    if not encoded_words:
        if not address or parseaddr(raw_text)[1] == address:
            # Kein kodiertes Wort: Es gibt nichts zu reparieren, und jede Normalisierung
            # des Rohwerts wäre nur eine weitere Fehlerquelle.
            return raw_text, address
        # S-2: Die Adresse steht fest, aber `parseaddr` liest sie aus dem Rohwert nicht
        # heraus (Token oder Kommentar hinter der Klammer, R-9-Form). Der Sanitizer
        # vergleicht `from_addr` und `reply_to` genau mit `parseaddr` — bliebe der Rohwert
        # stehen, hielte er die Antwortadresse für unbekannt und schwiege. Deshalb die
        # Anzeigeform so zusammensetzen, dass `parseaddr` nachweislich diese Adresse
        # zurückgibt (Selbstprüfung in `_compose_display_address`).
        return _compose_display_address(_clean_display_name(display), address), address
    display = _unmask_encoded_words(display, encoded_words)
    decoded = _decode_mime_words(display) if display else ""
    return _compose_display_address(_clean_display_name(decoded), address), address


def _capped_raw_header(value: object) -> str:
    """Rohwert eines anzeigenamentragenden Headers, entzerrt und gefaltet (R-8, Schicht a).

    Der Deckel auf :data:`_MAX_HEADER_CHARS` liegt seit S-1 in :func:`_raw_header_values`
    — also **vor** jeder Verarbeitung, auch vor der 8-Bit-Vorentzerrung, die selbst
    `decode_header` ruft (bei kaputten kodierten Wörtern quadratisch). Hier kommt nur
    noch ein bereits gedeckelter Wert an; ein Header jenseits der Obergrenze ist ohnehin
    defekt oder bösartig, sein Anfang genügt, um Adresse und Anzeigename zu bestimmen.
    """
    return _collapse(_predecode_eight_bit(_cap_header_value(value)))


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
    try:
        if _cap_message_headers(msg.obj):
            # Kein Mail-Inhalt im Log (I5): nur die Tatsache.
            logger.info("mail_headers_capped")
    except Exception:  # pragma: no cover - defekte email.Message-Implementierungen
        pass
    message_id = _header(msg, "Message-ID")
    from_addr, from_address = _address_header(msg, "From")
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
        for _name, email_address in getaddresses(_header_values(msg, "To"))
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
