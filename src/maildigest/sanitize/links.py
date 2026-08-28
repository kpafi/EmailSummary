"""Link-Erkennung und -Defanging: URLs (auch obfuskiert) durch ``[Link #n: domain.tld]``
ersetzen; ``mailto:``/``tel:`` analog; Punycode-Domains kennzeichnen.

Politik: docs/SECURITY.md §4 (I3, T3, T12). Umsetzung in WP3 (Details: ADR-028).

Erkennungs-Pässe (in dieser Reihenfolge, jeweils Ersetzung durch einen Platzhalter,
damit spätere Pässe nicht auf die eingesetzten Marker anspringen):

1. URLs mit (auch obfuskiertem) Schema: ``http``, ``https``, ``hxxp``, ``h t t p``,
   ``%68ttp`` …, plus ``ftp``/``ftps``.
2. ``mailto:``-Adressen.
3. ``tel:``-Nummern.
4. ``www.``-Domains ohne Schema.
5. Domains mit obfuskierten Punkten: ``example(.)com``, ``example[.]com``, ``(dot)``.
6. Nackte Klartext-Domains (``example.com``) — nur mit eng gesetzten Punkten und
   alphabetischer TLD; bekannte Datei-Endungen werden übersprungen (ADR-028).

Der aufrufende Code muss den Text vorher durch
:func:`maildigest.sanitize.unicode_clean.clean_text` schicken: Zero-Width-Zeichen im
Wort ``http`` würden die Erkennung sonst aushebeln, und die intern verwendeten
``\\x00``-Platzhalter setzen voraus, dass der Eingabetext keine NUL-Bytes enthält.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from maildigest.sanitize.unicode_clean import clean_text, is_mixed_script_domain

__all__ = ["LinkCollector"]

#: Obergrenze der in `links_found` gesammelten Einträge (Schutz vor Link-Bomben, T10).
_MAX_LINKS_LISTED = 100

#: Obergrenze der Länge einer defangten URL in `links_found`.
_MAX_DEFANGED_CHARS = 300

# --- Regex-Bausteine -----------------------------------------------------------------------

#: Obfuskierter Punkt: (.) [.] {.} (dot) [dot] {dot} — mit optionalen Leerzeichen.
_OBF_DOT = r"(?:\(\s*(?:\.|dot)\s*\)|\[\s*(?:\.|dot)\s*\]|\{\s*(?:\.|dot)\s*\})"

#: http/https-Schema inkl. hxxp, Leerzeichen-Einschub und URL-Encoding (%68ttp, htt%70 …).
_SCHEME_HTTP = (
    r"(?:%68|h)\s{0,3}(?:%74|t|x)\s{0,3}(?:%74|t|x)\s{0,3}(?:%70|p)\s{0,3}(?:%73|s)?"
)
_SCHEME = rf"(?:{_SCHEME_HTTP}|f\s{{0,3}}t\s{{0,3}}p\s{{0,3}}s?)"
_SEP = r"\s{0,3}:\s{0,3}/\s{0,3}/\s{0,3}"

#: Ein Domain-Label; ``\w`` ist Unicode-fähig, damit Homoglyphen-Domains erkannt werden.
_LABEL = r"[\w%-]{1,63}"

#: Punkt-Varianten zwischen Labels. Leerzeichen-Varianten verlangen ein
#: Kleinbuchstaben-/Ziffern-Lookahead, damit nach „example.com. Nächster Satz" nicht der
#: folgende Satz verschluckt wird (bewusster Trade-off, ADR-028).
_HOST_SEP = (
    rf"(?:{_OBF_DOT}|\.|(?:\s{{1,3}}(?:{_OBF_DOT}|\.)\s{{0,3}}|(?:{_OBF_DOT}|\.)\s{{1,3}})"
    r"(?=[a-z0-9]))"
)
_HOSTISH = rf"{_LABEL}(?:{_HOST_SEP}{_LABEL})*"
_PATH = r"(?:[/?#][^\s<>\"'()]*)?"

_RE_URL = re.compile(
    rf"{_SCHEME}{_SEP}(?:[^\s<>\"'@/]{{1,64}}@)?{_HOSTISH}(?::\d{{1,5}})?{_PATH}",
    re.IGNORECASE,
)
_RE_MAILTO = re.compile(r"mailto\s{0,3}:\s{0,3}[^\s<>\"'`,;:]{1,128}", re.IGNORECASE)
_RE_TEL = re.compile(r"tel\s{0,3}:\s{0,3}\+?[0-9][0-9 ()./-]{2,24}[0-9]", re.IGNORECASE)
_RE_WWW = re.compile(
    rf"\bwww\s{{0,3}}(?:{_OBF_DOT}|\.)\s{{0,3}}{_HOSTISH}{_PATH}", re.IGNORECASE
)
_RE_OBF_DOMAIN = re.compile(
    rf"\b{_LABEL}(?:\s{{0,3}}{_OBF_DOT}\s{{0,3}}{_LABEL})+{_PATH}", re.IGNORECASE
)
_RE_BARE_DOMAIN = re.compile(
    rf"(?<![\w@.-]){_LABEL}(?:\.{_LABEL}){{1,10}}(?:/[^\s<>\"'()]*)?", re.IGNORECASE
)

#: Satzzeichen, die am Ende eines Funds abgetrennt werden (gehören zum Satz, nicht zur URL).
_TRAILING_PUNCTUATION = ".,;:!?"

#: Kanonisches http-Schema auf bereits „kondensiertem" Text (ohne Leerzeichen).
_RE_CANON_HTTP = re.compile(
    r"^(?:%68|h)(?:%74|t|x)(?:%74|t|x)(?:%70|p)(?P<s>%73|s)?", re.IGNORECASE
)

#: Datei-Endungen, die im Nackte-Domain-Pass **nicht** als TLD gewertet werden
#: („rechnung.pdf" ist keine Domain). Bewusste Lücke: `.zip`/`.js` existieren auch als TLD —
#: ohne Schema sind solche Nennungen aber ohnehin nicht klickbar (I3), siehe ADR-028.
_FILE_EXTENSIONS = frozenset(
    {
        "exe", "dll", "msi", "bat", "cmd", "ps1", "sh", "js", "mjs", "py", "jar", "apk",
        "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "iso", "img", "bin", "dat",
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf",
        "txt", "csv", "md", "html", "htm", "eml", "ics", "vcf", "log", "ini", "cfg",
        "toml", "json", "xml", "yml", "yaml", "png", "jpg", "jpeg", "gif", "svg", "webp",
        "bmp", "tif", "tiff", "mp3", "mp4", "mov", "avi", "db", "sqlite", "bak", "tmp",
    }
)


def _split_trailing(raw: str) -> tuple[str, str]:
    """Trennt Satz-Interpunktion am Ende eines Funds ab („… http://x.example." → Punkt bleibt)."""
    core = raw.rstrip(_TRAILING_PUNCTUATION)
    return core, raw[len(core) :]


def _condense(raw: str) -> str:
    """Entfernt Leerzeichen und ersetzt obfuskierte Punkte — Basis für die Analyse."""
    no_space = re.sub(r"\s+", "", raw)
    return re.sub(_OBF_DOT, ".", no_space, flags=re.IGNORECASE)


def _canonical_url(raw: str) -> str:
    """Baut aus einem (obfuskierten) Fund die kanonische URL-Form (http…://…)."""
    condensed = _condense(raw)
    match = _RE_CANON_HTTP.match(condensed)
    if match is not None:
        scheme = "https" if match.group("s") else "http"
        return scheme + condensed[match.end() :].lower()
    return condensed.lower()


def _host_of(url: str) -> str:
    """Extrahiert den Host: nach ``://``, hinter Userinfo (`@`-Trick), ohne Port."""
    after = url.split("://", 1)[1] if "://" in url else url
    authority = re.split(r"[/?#]", after, maxsplit=1)[0]
    host = authority.rsplit("@", 1)[-1]
    host = re.sub(r":\d+$", "", host)
    host = unquote(host).strip(".").lower()
    cleaned, _ = clean_text(host)
    # „www." im Marker würde von Messenger-Clients autoverlinkt — nur für die Anzeige
    # strippen (die defangte Vollform in `links_found` behält das Präfix).
    if cleaned.startswith("www.") and cleaned.count(".") >= 2:
        cleaned = cleaned[4:]
    return cleaned or "unbekannt"


def _defang(url: str) -> str:
    """Defangte Darstellung: ``hxxp``-Schema, ``[:]//``-Trenner, ``[.]``-Punkte, begrenzt.

    Auch der ``://``-Trenner und der ``mailto:``-Doppelpunkt werden gebrochen, damit die
    defangte Form nirgends (auch nicht in der optionalen Fußnote) einem Auto-Linkifier
    zum Opfer fallen kann (I3).
    """
    defanged = re.sub(r"^https", "hxxps", url)
    defanged = re.sub(r"^http\b", "hxxp", defanged)
    defanged = re.sub(r"^ftp", "fxp", defanged)
    defanged = re.sub(r"^mailto:", "mailto[:]", defanged)
    defanged = defanged.replace("://", "[:]//")
    defanged = defanged.replace(".", "[.]")
    if len(defanged) > _MAX_DEFANGED_CHARS:
        defanged = defanged[:_MAX_DEFANGED_CHARS] + "…"
    return defanged


def _punycode_unicode(host: str) -> str | None:
    """Unicode-Darstellung einer ``xn--``-Domain, oder None wenn nicht dekodierbar."""
    if "xn--" not in host:
        return None
    try:
        decoded = host.encode("ascii").decode("idna")
    except (UnicodeError, UnicodeDecodeError):
        return None
    cleaned, _ = clean_text(decoded)
    return cleaned


@dataclass
class LinkCollector:
    """Sammelt Link-Funde über mehrere Textteile einer Mail (Body, Anhänge, Betreff).

    Ein Objekt pro Mail: Die Nummerierung `#n` läuft über alle Teile durch, damit ein
    Fußnoten-Eintrag eindeutig einem Marker zuordenbar ist.
    """

    links_removed: int = 0
    links_found: list[str] = field(default_factory=list)
    punycode_domains: list[str] = field(default_factory=list)
    mixed_script_domains: list[str] = field(default_factory=list)

    def scrub(self, text: str) -> str:
        """Ersetzt alle erkannten URLs/Adressen in `text` durch Marker und protokolliert sie."""
        placeholders: dict[str, str] = {}

        def stash(marker: str) -> str:
            token = f"\x00{len(placeholders)}\x00"
            placeholders[token] = marker
            return token

        def replace_url(match: re.Match[str]) -> str:
            core, tail = _split_trailing(match.group(0))
            url = _canonical_url(core)
            host = _host_of(url)
            return stash(self._record(host, _defang(url))) + tail

        def replace_mailto(match: re.Match[str]) -> str:
            core, tail = _split_trailing(match.group(0))
            address = _condense(core).split(":", 1)[-1].lower()
            host = address.rsplit("@", 1)[-1] if "@" in address else "unbekannt"
            cleaned_host, _ = clean_text(host)
            marker = self._record(
                cleaned_host or "unbekannt", _defang(f"mailto:{address}"), kind="Mail"
            )
            return stash(marker) + tail

        def replace_tel(match: re.Match[str]) -> str:
            number = _condense(match.group(0)).split(":", 1)[-1]
            self.links_removed += 1
            index = self.links_removed
            if len(self.links_found) < _MAX_LINKS_LISTED:
                self.links_found.append(f"#{index}: tel[:]{number}")
            return stash(f"[Tel #{index}]")

        def replace_domain(match: re.Match[str]) -> str:
            core, tail = _split_trailing(match.group(0))
            condensed = _condense(core).lower()
            host = _host_of(condensed)
            return stash(self._record(host, _defang(condensed))) + tail

        def replace_bare(match: re.Match[str]) -> str:
            core, tail = _split_trailing(match.group(0))
            condensed = _condense(core).lower()
            host = _host_of(condensed)
            labels = host.split(".")
            tld = labels[-1] if labels else ""
            has_path = any(sep in condensed for sep in "/?#")
            if len(tld) < 2 or not tld.isalpha():
                return match.group(0)
            if not has_path and tld in _FILE_EXTENSIONS:
                return match.group(0)
            return stash(self._record(host, _defang(condensed))) + tail

        text = _RE_URL.sub(replace_url, text)
        text = _RE_MAILTO.sub(replace_mailto, text)
        text = _RE_TEL.sub(replace_tel, text)
        text = _RE_WWW.sub(replace_domain, text)
        text = _RE_OBF_DOMAIN.sub(replace_domain, text)
        text = _RE_BARE_DOMAIN.sub(replace_bare, text)

        for token, marker in placeholders.items():
            text = text.replace(token, marker)
        return text

    def _record(self, host: str, defanged: str, *, kind: str = "Link") -> str:
        """Zählt einen Fund, prüft Punycode/Mixed-Script und liefert den Text-Marker."""
        self.links_removed += 1
        index = self.links_removed
        if len(self.links_found) < _MAX_LINKS_LISTED:
            self.links_found.append(f"#{index}: {defanged}")

        suffix = ""
        unicode_form = _punycode_unicode(host)
        if "xn--" in host:
            suffix = " (Achtung: Punycode)"
            entry = host.replace(".", "[.]")
            if unicode_form:
                entry += f" (Unicode: {unicode_form.replace('.', '[.]')})"
            if entry not in self.punycode_domains:
                self.punycode_domains.append(entry)

        checked = unicode_form or host
        if is_mixed_script_domain(checked):
            entry = checked.replace(".", "[.]")
            if entry not in self.mixed_script_domains:
                self.mixed_script_domains.append(entry)
            suffix += " (Achtung: gemischte Schriftsysteme)"

        return f"[{kind} #{index}: {host}{suffix}]"
