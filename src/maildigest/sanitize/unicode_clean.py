"""Unicode-Härtung: Entfernen von Zero-Width-/Bidi-Steuerzeichen, NFKC-Normalisierung,
Homoglyphen-/Mixed-Script-Kennzeichnung.

Politik: docs/SECURITY.md §4 (F-SEC-10, T2/T12). Umsetzung in WP3.

Regel (Allowlist-Prinzip): Nach NFKC-Normalisierung wird **jedes** Zeichen der
Unicode-Hauptkategorie „C" (Cc Steuerzeichen, Cf Formatzeichen, Co Private Use,
Cs Surrogate, Cn unzugewiesen) entfernt und gezählt — außer Tab und Zeilenumbruch.
Das deckt insbesondere die in SECURITY §4 genannten Bereiche ab:
U+200B..U+200F, U+202A..U+202E, U+2066..U+2069, U+FEFF, außerdem U+00AD (Soft Hyphen),
U+061C, U+180E, U+2060..U+2064 und U+206A..U+206F. Eine Blockliste einzelner Codepunkte
gibt es bewusst nicht — neue Formatzeichen wären sonst per Konstruktion durchgelassen.
"""

from __future__ import annotations

import unicodedata

__all__ = ["clean_text", "is_mixed_script_domain"]

#: Schrift-Systeme, die für die Mixed-Script-Erkennung unterschieden werden. Buchstaben,
#: deren Unicode-Name mit keinem dieser Präfixe beginnt, werden ignoriert (best effort).
_KNOWN_SCRIPTS = frozenset(
    {
        "LATIN",
        "CYRILLIC",
        "GREEK",
        "ARMENIAN",
        "HEBREW",
        "ARABIC",
        "DEVANAGARI",
        "BENGALI",
        "TAMIL",
        "THAI",
        "GEORGIAN",
        "ETHIOPIC",
        "CHEROKEE",
        "MYANMAR",
        "HANGUL",
        "HIRAGANA",
        "KATAKANA",
    }
)


def clean_text(text: str) -> tuple[str, int]:
    """Normalisiert Text (NFKC) und entfernt unsichtbare Steuer-/Formatzeichen.

    Zeilenenden werden vorab auf ``\\n`` vereinheitlicht. Erhalten bleiben nur Tab und
    Zeilenumbruch; jedes andere Zeichen der Kategorie „C*" wird entfernt und gezählt.

    Returns:
        ``(bereinigter Text, Anzahl entfernter Zeichen)``.
    """
    normalized = unicodedata.normalize(
        "NFKC", text.replace("\r\n", "\n").replace("\r", "\n")
    )
    kept: list[str] = []
    removed = 0
    for char in normalized:
        if char in ("\t", "\n"):
            kept.append(char)
            continue
        if unicodedata.category(char).startswith("C"):
            removed += 1
            continue
        kept.append(char)
    return "".join(kept), removed


def _script_of(char: str) -> str | None:
    """Grobes Schriftsystem eines Buchstabens über den Unicode-Namen (best effort)."""
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    if name.startswith("CJK"):
        return "CJK"
    first = name.split(" ", 1)[0]
    if first in _KNOWN_SCRIPTS:
        return first
    return None


def is_mixed_script_domain(domain: str) -> bool:
    """True, wenn ein Label der Domain Buchstaben aus mehreren Schriftsystemen mischt.

    Klassischer Homoglyphen-Angriff (T12): „paypal" mit kyrillischem a an zweiter Stelle.
    Geprüft wird pro Label — ``kyrillisch.example`` (je Label einheitlich) ist kein Treffer.
    """
    for label in domain.split("."):
        scripts = {script for char in label if (script := _script_of(char)) is not None}
        if len(scripts) > 1:
            return True
    return False
