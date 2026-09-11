"""Anhang-Behandlung nach Allowlist (text/plain, text/html, application/pdf) mit
Magic-Bytes-Verifikation; alles andere nur als Metadatum.

Politik: docs/SECURITY.md §4 (F-SEC-4, T4/T6). Umsetzung in WP3 (Signaturtabelle: ADR-026).

Grundregeln:
- Dem deklarierten MIME-Typ wird **nie** geglaubt. Verarbeitet wird nur, wenn der
  deklarierte Typ auf der Allowlist steht **und** der Inhalt dazu passt.
- Die Signaturtabelle prüft strikt an Offset 0. Ein als PDF deklarierter Anhang ohne
  ``%PDF-``-Präfix ist ein `mismatch` — ebenso ein als Text deklarierter Anhang mit
  bekannter Binärsignatur oder Binär-Heuristik-Treffer.
- Ein Inhalt, der wie ein bekanntes Binärformat aussieht, aber als Allowlist-Typ
  deklariert wurde, wird nie „umklassifiziert und doch verarbeitet" (fail-closed).
"""

from __future__ import annotations

import unicodedata

from maildigest.models import AttachmentKind

__all__ = [
    "ALLOWLIST_MIMES",
    "detect_kind",
    "sanitize_filename",
    "sniff_signature",
]

#: Die einzigen MIME-Typen, deren Inhalt überhaupt verarbeitet werden darf (SECURITY §4).
ALLOWLIST_MIMES = frozenset({"text/plain", "text/html", "application/pdf"})

#: Bekannte Binär-/Format-Signaturen an Offset 0 → Label. Bewusst klein und eigenständig
#: (keine neue Dependency, ADR-026). Die Tabelle muss nicht vollständig sein: Sie dient
#: nur dazu, *deklarierte* Allowlist-Typen zu falsifizieren — Unbekanntes wird ohnehin
#: nie verarbeitet (Allowlist-Prinzip).
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"MZ", "exe"),
    (b"\x7fELF", "elf"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"MSCF", "cab"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"\xca\xfe\xba\xbe", "macho-fat/javaclass"),
    (b"\xfe\xed\xfa\xce", "macho"),
    (b"\xfe\xed\xfa\xcf", "macho"),
    (b"\xcf\xfa\xed\xfe", "macho"),
    (b"\xce\xfa\xed\xfe", "macho"),
    (b"{\\rtf", "rtf"),
    (b"#!", "script"),
    (b"SQLite format 3\x00", "sqlite"),
    (b"\x00asm", "wasm"),
)

#: Steuerbytes, die in echtem Text nicht vorkommen (Tab/LF/CR/FF/ESC ausgenommen).
_TEXT_CONTROL_BYTES = frozenset(range(0x20)) - {0x09, 0x0A, 0x0C, 0x0D, 0x1B}

#: Stichprobengröße für die Text-Heuristik.
_SNIFF_WINDOW = 8192

#: Maximale Länge eines sanitisierten Dateinamens.
_MAX_FILENAME_CHARS = 80

#: Kürzungsmarker in der Mitte zu langer Dateinamen (SPEC-CLI §6, ADR-040).
_ELLIPSIS = "…"

#: Zeichenkosten von :data:`_ELLIPSIS` **nach** NFKC. Der Composer schickt den Namen durch
#: `output.sanitizer.scrub_plain`, dessen `clean_text` NFKC anwendet — und NFKC bildet
#: U+2026 auf ``...`` ab. Ohne diese Reserve wäre der Name nach dem Normalisieren 82
#: Zeichen lang, der Composer würde bei 80 erneut abschneiden und genau die Endung
#: verlieren, die hier erhalten werden soll (HC-22).
_ELLIPSIS_COST = 3

#: Längste Endung, die beim Kürzen erhalten bleibt (``.xlsx``, ``.torrent`` …). Bei
#: geblockten Anhängen ist die Endung die sicherheitsrelevante Information (E7).
_MAX_KEPT_SUFFIX = 10

#: Zeichen, die in sanitisierten Dateinamen erlaubt sind (ASCII-Allowlist gegen
#: RLO-/Homoglyphen-Tricks in Dateinamen, T2/T12).
_FILENAME_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._ -"
)


def sniff_signature(data: bytes) -> str | None:
    """Liefert das Label der ersten passenden Signatur an Offset 0, sonst None."""
    for prefix, label in _SIGNATURES:
        if data.startswith(prefix):
            return label
    return None


def _looks_like_text(data: bytes) -> bool:
    """Heuristik: Stichprobe ohne NUL-Bytes und mit < 5 % Steuerbytes gilt als Text."""
    sample = data[:_SNIFF_WINDOW]
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    control = sum(1 for byte in sample if byte in _TEXT_CONTROL_BYTES)
    return control / len(sample) < 0.05


def detect_kind(declared_mime: str, data: bytes) -> AttachmentKind:
    """Verifiziert den deklarierten MIME-Typ gegen den Inhalt (F-SEC-4, T6).

    Returns:
        - ``"pdf"`` / ``"text"`` / ``"html"``: deklarierter Allowlist-Typ, Inhalt passt.
        - ``"mismatch"``: deklarierter Allowlist-Typ, Inhalt widerspricht (nie verarbeiten).
        - ``"unknown"``: Typ nicht auf der Allowlist (nie verarbeiten, nur Metadatum).
    """
    mime = declared_mime.split(";", 1)[0].strip().lower()
    if mime not in ALLOWLIST_MIMES:
        return "unknown"

    signature = sniff_signature(data)
    if mime == "application/pdf":
        return "pdf" if signature == "pdf" else "mismatch"
    # text/plain oder text/html: Jede bekannte Binärsignatur (auch "pdf" oder "script")
    # widerspricht der Deklaration; zusätzlich muss die Text-Heuristik bestehen.
    if signature is not None or not _looks_like_text(data):
        return "mismatch"
    return "html" if mime == "text/html" else "text"


def _shorten(name: str, limit: int) -> str:
    """Kürzt einen zu langen Dateinamen in der **Mitte** und behält die Endung (E7/HC-22).

    Der harte Schnitt auf `limit` verschwieg beides: dass gekürzt wurde und worauf die
    Datei endet. Gerade bei geblockten Anhängen ist die Endung die sicherheitsrelevante
    Information (``…rechnung.exe``). Das Ergebnis ist höchstens `limit` Zeichen lang —
    auch nach der NFKC-Auflösung des Auslassungszeichens (:data:`_ELLIPSIS_COST`).
    """
    if len(name) <= limit:
        return name
    stem, dot, extension = name.rpartition(".")
    suffix = ""
    if dot and stem and 1 <= len(extension) <= _MAX_KEPT_SUFFIX:
        suffix = f".{extension}"
    budget = limit - _ELLIPSIS_COST - len(suffix)
    if budget < 2:  # Endung selbst zu lang ⇒ ohne sie, aber mit sichtbarer Kürzung
        suffix = ""
        budget = limit - _ELLIPSIS_COST
    if budget < 2:
        return name[:limit]
    body = name[: len(name) - len(suffix)]
    head = (budget + 1) // 2
    tail = budget - head
    return body[:head] + _ELLIPSIS + (body[-tail:] if tail else "") + suffix


def sanitize_filename(name: str | None, *, fallback: str = "unbenannt") -> str:
    """Sanitisiert einen Anhang-Dateinamen zu einer harmlosen ASCII-Darstellung.

    Entfernt Pfadanteile, ersetzt alles außerhalb einer engen ASCII-Allowlist durch ``_``
    (deckt Bidi-/Zero-Width-/Homoglyphen-Tricks im Dateinamen ab) und kürzt zu lange Namen
    in der Mitte mit ``…`` (:func:`_shorten`).
    """
    if not name:
        return fallback
    normalized = unicodedata.normalize("NFKC", name)
    basename = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    kept = "".join(
        char if char in _FILENAME_ALLOWED else "_" for char in basename
    )
    collapsed = " ".join(kept.split()).strip("._ ")
    if not collapsed:
        return fallback
    return _shorten(collapsed, _MAX_FILENAME_CHARS)
