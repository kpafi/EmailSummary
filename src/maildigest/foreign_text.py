"""Zeichen-Allowlist für Fremdtext, der auf dem Terminal landet (ADR-055, HC-4).

Fremdtext ist jede Zeichenkette, die MailDigest nicht selbst erzeugt hat: Ordnernamen
vom IMAP-Server, Chat-Angaben aus `getUpdates`, der Fehlertext eines LLM-Anbieters. Ein
Terminal interpretiert darin enthaltene Steuersequenzen — ESC leert den Bildschirm, setzt
Farben und Fenstertitel, BEL piept. Damit lässt sich eine frei erfundene „Programmmeldung"
platzieren, die von einer echten nicht zu unterscheiden ist.

Dieses Modul liegt bewusst oben im Paket und importiert nur die Standardbibliothek: Die
Filter werden sowohl in der CLI (`cli.py`) als auch in den HTTP-Hilfsschichten
(`llm/_http.py`, `messenger/_http.py`) gebraucht, und `cli` importiert diese Schichten —
ein Import in die Gegenrichtung wäre ein Zyklus.

Abgrenzung: Der Output-Sanitizer (`output/sanitizer.py`) härtet **Nachrichten** an den
Messenger. Hier geht es um das **Terminal** des Betreibers; die Regeln sind enger
(kein Emoji, kein Markup nötig) und die Schutzrichtung eine andere.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "FOREIGN_TEXT_MAX_CHARS",
    "REPLACEMENT",
    "mask_secrets",
    "sanitize_foreign_text",
]

#: Ersatzzeichen für alles, was die Allowlist nicht kennt (wie in `cli._safe_name`).
REPLACEMENT = "·"

#: Vorgabe-Deckel für einen einzelnen Fremdtext.
FOREIGN_TEXT_MAX_CHARS = 400

#: So viele Zeichen eines Secrets genügen, damit ein Präfix als Fund gilt (HC-4).
_SECRET_PREFIX_CHARS = 8

#: Was ein Secret ersetzt.
_SECRET_MASK = "***"

#: Druckbares ASCII, deutsche Umlaute/ß und die im Projekt üblichen Satzzeichen
#: (Geviertstrich, Halbgeviertstrich, Auslassungspunkte, typografische Anführungszeichen,
#: Paragraphzeichen, das Ersatzzeichen selbst). Alles andere — insbesondere jedes
#: C0-/C1-Steuerzeichen außer `\n` — wird ersetzt.
_ALLOWED_RE = re.compile(
    "[^\\n\\x20-\\x7E"
    "\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df"  # Umlaute und ß
    "\u2014\u2013\u2026"  # Geviert-/Halbgeviertstrich, Auslassungspunkte
    "\u201e\u201c\u201d\u2018\u2019"  # typografische Anführungszeichen
    "\u00a7\u00b7"  # Paragraphzeichen, Ersatzzeichen
    "]"
)


def sanitize_foreign_text(
    value: str, *, max_chars: int = FOREIGN_TEXT_MAX_CHARS, keep_newlines: bool = False
) -> str:
    """Reduziert Fremdtext auf die Allowlist und kürzt ihn.

    Args:
        value: Der rohe Fremdtext.
        max_chars: Obergrenze der Länge; 0 oder kleiner bedeutet „nicht kürzen".
        keep_newlines: Zeilenumbrüche erhalten. Vorgabe ist `False` — ein Fremdtext soll
            die Zeilenstruktur der eigenen Ausgabe nicht zerreißen. `True` gilt für
            zusammengesetzte Meldungen, die eigene Zeilenumbrüche enthalten (`cli.main`).

    Returns:
        Den gefilterten Text; unbekannte Zeichen stehen als :data:`REPLACEMENT`.
    """
    if keep_newlines:
        text = value.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = value.replace("\r", " ").replace("\n", " ")
    cleaned = _ALLOWED_RE.sub(REPLACEMENT, text)
    if max_chars > 0:
        cleaned = cleaned[:max_chars]
    return cleaned


def mask_secrets(
    value: str, secrets: str | Iterable[str], *, mask: str = _SECRET_MASK
) -> str:
    """Ersetzt jedes bekannte Secret (und lange Präfixe davon) durch `***` (I5).

    Ein Anbieter, der den gesendeten Schlüssel in seine Fehlermeldung zitiert — manche
    tun das gekürzt —, darf die Zusage aus SPEC-CLI §2 („Fehlermeldungen enthalten
    niemals … API-Keys") nicht aushebeln. Deshalb wird nicht nur der volle Wert gesucht,
    sondern auch sein Präfix ab :data:`_SECRET_PREFIX_CHARS` Zeichen.

    Args:
        value: Der auszugebende Text.
        secrets: Eine Zeichenkette oder eine Folge davon; leere Werte werden übergangen.
    """
    items = [secrets] if isinstance(secrets, str) else list(secrets)

    text = value
    for secret in items:
        candidate = secret.strip()
        if len(candidate) < _SECRET_PREFIX_CHARS:
            continue
        # Längste Übereinstimmung zuerst: Erst der volle Wert, dann immer kürzere
        # Präfixe. Sonst bliebe hinter der Maske ein verwertbarer Rest stehen. Es wird
        # **jede** Länge versucht, nicht nur die erste passende: Eine Meldung kann den
        # Schlüssel voll und zusätzlich gekürzt zitieren.
        for length in range(len(candidate), _SECRET_PREFIX_CHARS - 1, -1):
            prefix = candidate[:length]
            if prefix in text:
                text = text.replace(prefix, mask)
    return text
