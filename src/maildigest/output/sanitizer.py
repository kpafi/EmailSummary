"""Output-Sanitizer: die letzte deterministische Code-Schicht vor dem Nutzer (I3/I4).

Grundhaltung: **Alles**, was hier ankommt, ist untrusted — die Summary und das
Kritiker-Verdict stammen von einem LLM, dessen Eingabe der Angreifer kontrolliert
(docs/SECURITY.md §2, §5 Schicht 6). Selbst wenn jede vorherige Schicht versagt hat, darf
aus diesem Modul keine klickbare URL, kein Markup und kein Steuerzeichen herauskommen.

Reihenfolge je Textfeld (:func:`scrub_field`):

1. **HTML-Entities auflösen** (mehrfach, bis Fixpunkt): ``&#104;ttp&#58;//x.example``
   wäre sonst eine Rekonstruktions-Lücke — die Erkennung liefe auf dem kodierten Text.
2. **Unicode-Reinigung** über :func:`maildigest.sanitize.unicode_clean.clean_text`
   (NFKC + Entfernen aller „C*"-Zeichen). NFKC macht Fullwidth-Tricks (Fullwidth-`http`)
   erkennbar, das Entfernen der Steuerzeichen schützt Marker und Platzhalter (T2/F-SEC-10).
3. **Feldkürzung** (optional) — vor der Link-Erkennung, damit ein Schnitt keine halbe
   URL erzeugt, die anschließend niemand mehr prüft.
4. **Tag-Strip**: tag-artige Sequenzen fallen weg, danach werden alle verbliebenen
   ``<``/``>`` gelöscht (fail-safe, analog ADR-027).
5. **Segmentierung**: Bereits sichere, vom WP3-Sanitizer erzeugte Formen
   (``[Link #n: domain]``, ``[Mail #n: …]``, ``[Tel #n]``, ``hxxps[:]//evil[.]com``)
   werden erkannt und *unverändert* durchgereicht — sie dürfen nicht ein zweites Mal
   nummeriert oder zerlegt werden. Alles dazwischen läuft durch Markup-Neutralisierung
   und :class:`maildigest.sanitize.links.LinkCollector`.

Die Zusammenbau-Stufe (:mod:`maildigest.output.composer`) legt darüber noch
:func:`final_guard` über die **fertige** Nachricht: ein enger, von der Segmentierung
unabhängiger Nachbrenner, der lebende Schemata, ``www.``-Präfixe und domainartige Token
defangt. Damit hängt die Invariante I3 nicht an der Korrektheit der Segmentierungs-Regex,
sondern an einer zweiten, sehr einfachen Regel (Defense in Depth).
"""

from __future__ import annotations

import html
import re

from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.unicode_clean import clean_text

__all__ = [
    "DISCORD_MAX_PART_CHARS",
    "SIGNAL_MAX_PART_CHARS",
    "TELEGRAM_MAX_PART_CHARS",
    "final_guard",
    "scrub_field",
    "scrub_plain",
    "split_parts",
]

#: Zeichenlimit einer Telegram-Nachricht (Bot-API `sendMessage`).
TELEGRAM_MAX_PART_CHARS = 4096

#: Zeichenlimit des `content`-Feldes eines Discord-Webhooks.
DISCORD_MAX_PART_CHARS = 2000

#: Konservatives Limit für signal-cli (Signal selbst ist großzügiger).
SIGNAL_MAX_PART_CHARS = 2000

#: Maximale Runden beim Auflösen von HTML-Entities bzw. beim Tag-Strip.
_MAX_ROUNDS = 3

#: Zeichen, die Struktur/Formatierung erzeugen können und in untrusted Text nichts zu
#: suchen haben. `[`/`]` fallen mit, damit niemand einen Sanitizer-Marker fälschen kann
#: und `[text](ziel)` nicht als Markdown-Link zusammenfindet. `_` bleibt erhalten:
#: Es erzeugt höchstens Kursivschrift, aber nie ein klickbares Ziel — und zerstörte
#: Dateinamen wären ein realer Lesbarkeitsverlust.
_MARKUP_CHARS = "`*|~\\[]"

_RE_MARKUP = re.compile("[" + re.escape(_MARKUP_CHARS) + "]")

_RE_TAG = re.compile(r"<[^<>]{0,400}>")
_RE_ANGLE = re.compile(r"[<>]")

#: Ein bereits defangtes Fragment: normales Zeichen oder ein `[.]`/`[:]`-Token.
_DEFANGED_ATOM = r"(?:[^\s\[\]]|\[[.:]\])"

#: Vom WP3-Sanitizer erzeugte, bereits sichere Formen (ADR-028). Werden unverändert
#: durchgereicht — erneutes Scrubben würde Marker verschachteln und die Nummerierung
#: zerstören.
_RE_SAFE_SPAN = re.compile(
    r"\[(?:Link|Mail) #\d{1,5}: (?:[^\[\]\n]|\[[.:]\]){1,200}\]"
    r"|\[Tel #\d{1,5}\]"
    rf"|(?:hxxps?|fxps?|mailto|tel)\[:\]{_DEFANGED_ATOM}{{0,300}}"
    rf"|{_DEFANGED_ATOM}{{0,120}}\[\.\]{_DEFANGED_ATOM}{{0,120}}"
)

#: Lebendes URL-Schema (auch mit eingeschobenen Leerzeichen) — wird im Nachbrenner
#: gebrochen statt entfernt, damit der Nutzer sieht, dass dort etwas stand.
_RE_LIVE_SCHEME = re.compile(r"(?i)\b(h\s*t\s*t\s*p\s*s?|f\s*t\s*p\s*s?)\s*:\s*/\s*/")

#: Jedes **andere** Schema mit `://` (`tg://`, `steam://`, `file://`, …). Telegram und
#: Discord verlinken Deep-Link-Schemata ebenfalls; die Regel ist deshalb bewusst generisch
#: (Allowlist-Haltung: nicht „welche Schemata sind gefährlich", sondern „kein lebendes
#: Schema überlebt"). Läuft **nach** :data:`_RE_LIVE_SCHEME`, dessen Treffer danach kein
#: `:` mehr tragen.
_RE_LIVE_SCHEME_ANY = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,15})\s*:\s*/\s*/")

#: Schemata **ohne** `//`, die in Messengern trotzdem als Aktion interpretiert werden
#: können. Hier ist eine Namensliste unvermeidlich: `wort:wort` ist im Fließtext normal,
#: eine generische Regel würde „Achtung:Bitte" zerlegen. Der Fund wird nur gebrochen,
#: wenn direkt ein Nicht-Leerzeichen folgt.
_RE_LIVE_SCHEME_BARE = re.compile(
    r"(?i)\b(javascript|vbscript|data|tg|intent|market|smb)\s*:(?=\S)"
)

_RE_LIVE_WWW = re.compile(r"(?i)\bwww\s*\.")

#: Punkt-Varianten, die IDN-fähige Clients wie einen Label-Trenner behandeln, die aber
#: NFKC **nicht** auf `.` abbildet (U+3002 ideographischer Punkt, U+FF61 halbbreit).
#: Ohne diese Übersetzung sähe `boese。example` für jede Domain-Regel unauffällig aus.
_IDN_DOTS = str.maketrans({"。": ".", "｡": "."})

#: Domainartiges Token (mind. zwei Labels). Die Entscheidung, ob defangt wird, fällt in
#: :func:`_defang_domain_match` — nur so bleiben „3.14" oder „z.B." unangetastet.
_RE_DOMAINISH = re.compile(
    r"(?<![\w\-])[a-z0-9](?:[a-z0-9\-]{0,62})(?:\.[a-z0-9\-]{1,63})+(?![\w\-])",
    re.IGNORECASE,
)

_RE_IPV4 = re.compile(r"(?<![\w.\-])\d{1,3}(?:\.\d{1,3}){3}(?![\w.\-])")

#: `](` unmittelbar hintereinander ist die Markdown-Link-Syntax. Nach dem Scrubbing steht
#: im Ziel zwar nie eine URL, aber die Form soll gar nicht erst entstehen (T7).
_RE_MARKDOWN_SEAM = re.compile(r"\]\(")

_RE_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_RE_MANY_NEWLINES = re.compile(r"\n{3,}")

#: Kürzungsmarker für Felder über ihrem Einzellimit (ohne Klammern: die fielen der
#: Markup-Neutralisierung zum Opfer).
_TRUNCATION_MARKER = " …"


def _unescape(text: str) -> str:
    """Löst HTML-Entities auf, bis sich nichts mehr ändert (max. :data:`_MAX_ROUNDS`).

    Mehrfach, weil `&amp;#104;ttp://…` erst nach zwei Runden als URL sichtbar wird. Die
    Rundenzahl ist gedeckelt: Eine Kette aus 10 000 `&amp;` soll keinen quadratischen
    Aufwand erzeugen (T10).
    """
    current = text
    for _ in range(_MAX_ROUNDS):
        decoded = html.unescape(current)
        if decoded == current:
            return current
        current = decoded
    return current


def _strip_tags(text: str) -> str:
    """Entfernt tag-artige Sequenzen und danach jedes verbliebene ``<``/``>``.

    Über-Entfernung ist beabsichtigt (ADR-027): Ein verlorenes „<" in „5 < 7" kostet
    nichts, ein durchgerutschtes Tag kann im Messenger Formatierung/Links erzeugen (T7).
    """
    current = text
    for _ in range(_MAX_ROUNDS):
        stripped = _RE_TAG.sub(" ", current)
        if stripped == current:
            break
        current = stripped
    return _RE_ANGLE.sub("", current)


def _scrub_segment(segment: str, collector: LinkCollector) -> str:
    """Neutralisiert Markup und ersetzt jede erkannte URL durch einen Marker."""
    if not segment:
        return ""
    return collector.scrub(_RE_MARKUP.sub("", segment))


def _collapse_whitespace(text: str) -> str:
    """Vereinheitlicht Leerraum: keine Zeilen-Endleerzeichen, höchstens eine Leerzeile."""
    return _RE_MANY_NEWLINES.sub("\n\n", _RE_TRAILING_SPACE.sub("", text)).strip()


def scrub_field(
    text: str, *, collector: LinkCollector | None = None, max_chars: int | None = None
) -> str:
    """Säubert genau ein untrusted Textfeld (Headline, Summary, Kritiker-Grund …).

    Args:
        text: Rohfeld aus der LLM-Ausgabe oder aus `SanitizedMail`.
        collector: Gemeinsamer :class:`LinkCollector` einer Nachricht, damit die
            Marker-Nummerierung über alle Felder hinweg durchläuft. Ohne Angabe wird ein
            eigener benutzt (Einzelfeld-Nutzung, Tests).
        max_chars: Optionales Einzellimit; überlange Felder werden mit „[…]" gekürzt.

    Returns:
        Klartext ohne Tags, ohne Markup-Steuerzeichen und ohne lebende URLs. Bereits
        defangte Formen aus WP3 bleiben unverändert erhalten.
    """
    if not text:
        return ""

    cleaned, _removed = clean_text(_unescape(text))
    cleaned = cleaned.translate(_IDN_DOTS)
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + _TRUNCATION_MARKER
    prepared = _strip_tags(cleaned)

    link_collector = collector if collector is not None else LinkCollector()
    chunks: list[str] = []
    position = 0
    for match in _RE_SAFE_SPAN.finditer(prepared):
        chunks.append(_scrub_segment(prepared[position : match.start()], link_collector))
        chunks.append(match.group(0))
        position = match.end()
    chunks.append(_scrub_segment(prepared[position:], link_collector))

    return _collapse_whitespace("".join(chunks))


def scrub_plain(text: str, *, max_chars: int | None = None) -> str:
    """Säubert ein Feld, das **kein** Fließtext ist: Domain, Anzeigename, Dateiname.

    Unterschied zu :func:`scrub_field`: Es läuft **keine** Link-Erkennung. Die
    Absender-Domain oder ein Dateiname sollen als das erscheinen, was sie sind — ein
    `[Link #n: …]`-Marker an dieser Stelle wäre irreführend und würde die
    Marker-Nummerierung der Nachricht verfälschen. Gegen Autolinking schützt hier
    :func:`final_guard`, der die Punkte bricht (`stadtwerke-x[.]de`).
    """
    if not text:
        return ""
    cleaned, _removed = clean_text(_unescape(text))
    cleaned = cleaned.translate(_IDN_DOTS)
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + _TRUNCATION_MARKER
    return _collapse_whitespace(_RE_MARKUP.sub("", _strip_tags(cleaned)))


def _break_scheme(match: re.Match[str]) -> str:
    """Bricht ein lebendes Schema in seine defangte Form (`https://` ⇒ `hxxps[:]//`)."""
    scheme = re.sub(r"\s+", "", match.group(1)).lower()
    broken = "fxp" if scheme.startswith("ftp") else "hxxp"
    if scheme.endswith("s"):
        broken += "s"
    return f"{broken}[:]//"


def _defang_domain_match(match: re.Match[str]) -> str:
    """Defangt ein domainartiges Token, wenn seine letzte Marke wie eine TLD aussieht.

    Hintergrund (T7): Telegram und Discord verlinken nackte Domains im Klartext
    automatisch. I3 erlaubt zwar „bloßer Domain-Name in Textform", aber genau diese
    Autolinker machen daraus wieder ein klickbares Ziel. Deshalb bekommt jede Domain —
    auch die in einem `[Link #n: …]`-Marker und in Dateinamen — gebrochene Punkte.
    """
    token = match.group(0)
    last_label = token.rsplit(".", 1)[-1]
    if not (2 <= len(last_label) <= 24 and last_label.isalpha()):
        return token
    return token.replace(".", "[.]")


def final_guard(text: str) -> str:
    """Letzter, von der Feld-Logik unabhängiger Nachbrenner über die fertige Nachricht.

    Bewusst simpel und ohne Zustand: **jedes** lebende Schema mit ``://`` brechen (nicht
    nur http/ftp — Telegram verlinkt auch ``tg://``), dazu die Schema-Namen ohne ``//``,
    die als Aktion gelten (``javascript:``, ``data:`` …), ``www.``-Präfixe brechen,
    Winkelklammern entfernen, domain- und IP-artige Token defangen. Selbst wenn
    :func:`scrub_field` durch eine unentdeckte Regex-Lücke etwas durchließe, kann die
    Nachricht danach keine anklickbare Adresse mehr enthalten (I3).
    """
    guarded = _RE_LIVE_SCHEME.sub(_break_scheme, text.translate(_IDN_DOTS))
    guarded = _RE_LIVE_SCHEME_ANY.sub(lambda m: f"{m.group(1)}[:]//", guarded)
    guarded = _RE_LIVE_SCHEME_BARE.sub(lambda m: f"{m.group(1)}[:]", guarded)
    guarded = _RE_LIVE_WWW.sub("www[.]", guarded)
    guarded = _RE_ANGLE.sub("", guarded)
    guarded = _RE_MARKDOWN_SEAM.sub("] (", guarded)
    guarded = _RE_DOMAINISH.sub(_defang_domain_match, guarded)
    return _RE_IPV4.sub(lambda m: m.group(0).replace(".", "[.]"), guarded)


def _split_long_line(line: str, limit: int) -> list[str]:
    """Zerlegt eine einzelne, zu lange Zeile — bevorzugt an einem Leerzeichen."""
    pieces: list[str] = []
    rest = line
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind(" ")
        if cut < limit // 2:  # kein brauchbarer Trennpunkt ⇒ hart schneiden
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces or [""]


def split_parts(text: str, limit: int) -> list[str]:
    """Teilt die Nachricht an Zeilengrenzen in Teile von höchstens `limit` Zeichen.

    Zeilen, die für sich genommen zu lang sind, werden zusätzlich an einem Leerzeichen
    (notfalls hart) getrennt. Es wird nie über Feldgrenzen hinweg umsortiert: Die
    Reihenfolge des Textes bleibt exakt erhalten.

    Raises:
        ValueError: `limit` ist kleiner als 1.
    """
    if limit < 1:
        raise ValueError("limit muss mindestens 1 sein.")
    if not text:
        return [""]

    parts: list[str] = []
    current = ""
    for raw_line in text.split("\n"):
        for line in _split_long_line(raw_line, limit):
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                parts.append(current)
            current = line
    if current or not parts:
        parts.append(current)
    return parts
