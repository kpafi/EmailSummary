"""HTML → Klartext (beautifulsoup4/lxml): entfernt script/style/Kommentare, unsichtbaren
Text und Tracking-Pixel; übernimmt Alt-Texte markiert und macht href-Ziele sichtbar.

Politik: docs/SECURITY.md §4 (T2). Umsetzung in WP3 (Hidden-Text-Heuristik: ADR-027).

Wichtig für Aufrufer: Das Ergebnis ist noch **nicht** fertig sanitisiert — es muss danach
durch `unicode_clean.clean_text` (HTML-Entities wie ``&zwnj;`` erzeugen beim Parsen neue
Zero-Width-Zeichen) und durch `links.LinkCollector.scrub` laufen.

Schranken (ADR-084, HC2-1): Ein HTML-Teil mit mehr als `max_elements` Elementen oder mehr
als :data:`MAX_HTML_DEPTH` Schachtelungsebenen gilt als **nicht verarbeitbar** —
:class:`HtmlTooComplexError`. Der Aufrufer behandelt den Teil dann wie einen geblockten
Anhang (Metadatum statt Inhalt), nie als Fehler der ganzen Mail.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString

__all__ = ["MAX_HTML_DEPTH", "HtmlTooComplexError", "html_to_text"]

#: Harte Obergrenze der Schachtelungstiefe eines HTML-Teils (ADR-084, HC2-1).
#:
#: Bewusst eine Modulkonstante und kein Config-Feld: Die Zahl ist keine Betriebsgrösse,
#: sondern eine Struktur-Plausibilität — echtes Mail-HTML (auch generiertes Tabellen-HTML)
#: bleibt um Grössenordnungen darunter, und lxml selbst kennt keine Tiefengrenze.
MAX_HTML_DEPTH = 2_000

#: Default für `max_elements`; der Betrieb setzt `[limits] max_html_elements` (ADR-084).
DEFAULT_MAX_HTML_ELEMENTS = 50_000


class HtmlTooComplexError(Exception):
    """Der HTML-Teil überschreitet Element- oder Tiefenschranke (ADR-084, T10).

    Kein Fail-closed für die ganze Mail: Der Aufrufer verwirft **nur diesen Teil** und
    läuft weiter (fail-safe). Der Meldungstext ist ein konstantes Label ohne Mail-Inhalt
    (I5), wie bei :class:`~maildigest.sanitize.sanitizer.SanitizeError`.
    """


#: Elemente, deren Inhalt nie Text werden darf (Skripte, Styles, eingebettete Objekte …).
_DROP_TAGS = (
    "script",
    "style",
    "head",
    "title",
    "template",
    "noscript",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "applet",
    "svg",
    "math",
)

#: CSS-Farbwerte, die als „weiße Schrift" gewertet werden (best effort, ADR-027).
_WHITE_COLORS = frozenset(
    {"#fff", "#ffffff", "white", "rgb(255,255,255)", "rgba(255,255,255,1)"}
)


def _style_declarations(tag: Tag) -> dict[str, str]:
    """Parst das style-Attribut naiv in ``{eigenschaft: wert}`` (lowercase, ohne Spaces)."""
    style = tag.get("style")
    if not isinstance(style, str):
        return {}
    declarations: dict[str, str] = {}
    for chunk in style.lower().split(";"):
        prop, _, value = chunk.partition(":")
        prop = prop.strip()
        if prop:
            declarations[prop] = re.sub(r"\s+", "", value)
    return declarations


def _is_zero_size(value: str) -> bool:
    """True für Größenangaben, die effektiv 0 sind (0, 0px, 0pt, 0em, 0%, .0px …)."""
    match = re.match(r"^(\d*\.?\d+)(px|pt|em|rem|%|)$", value)
    if match is None:
        return False
    return float(match.group(1)) == 0.0


def _is_hidden(tag: Tag) -> bool:
    """Best-effort-Erkennung von unsichtbarem Text (T2, ADR-027)."""
    if tag.has_attr("hidden"):
        return True
    decl = _style_declarations(tag)
    if decl.get("display") == "none":
        return True
    if decl.get("visibility") in ("hidden", "collapse"):
        return True
    opacity = decl.get("opacity", "")
    if re.match(r"^0(\.0*)?$", opacity):
        return True
    if _is_zero_size(decl.get("font-size", "")):
        return True
    color = decl.get("color", "")
    if color in _WHITE_COLORS:
        # Weiße Schrift gilt als versteckt (Mail-Hintergrund ist praktisch immer weiß),
        # außer das Element bringt selbst einen erkennbar nicht-weißen Hintergrund mit.
        background = decl.get("background-color", decl.get("background", ""))
        if not background or background in _WHITE_COLORS:
            return True
    return False


def _dimension(tag: Tag, attribute: str) -> int | None:
    """Liest width/height als ganze Pixelzahl aus Attribut oder style (best effort)."""
    value = tag.get(attribute)
    if isinstance(value, str):
        match = re.match(r"^\s*(\d+)", value)
        if match is not None:
            return int(match.group(1))
    decl = _style_declarations(tag)
    style_value = decl.get(attribute, "")
    match = re.match(r"^(\d+)(px|)$", style_value)
    if match is not None:
        return int(match.group(1))
    return None


def _is_tracking_pixel(img: Tag) -> bool:
    """1x1-/Kleinstbilder sind Tracking-Pixel und werden ersatzlos entfernt."""
    width = _dimension(img, "width")
    height = _dimension(img, "height")
    return width is not None and height is not None and width <= 2 and height <= 2


def _check_complexity(soup: BeautifulSoup, max_elements: int) -> None:
    """Zählt Elemente und Tiefe in **einem** iterativen Durchlauf und bricht früh ab.

    Läuft absichtlich vor jeder weiteren Arbeit (ADR-084): Parsen ist linear und billig,
    alles danach (Hidden-Heuristik, `get_text`) skaliert mit der Baumgrösse. Die Tiefe
    wird beim Absteigen mitgeführt statt je Element über `parents` ermittelt — letzteres
    wäre selbst wieder quadratisch. Kein Rekursionsabstieg: 2000 Ebenen sprengen den
    Python-Stack.
    """
    stack: list[tuple[Tag, int]] = [(soup, 0)]
    elements = 0
    while stack:
        node, depth = stack.pop()
        child_depth = depth + 1
        for child in node.contents:
            if not isinstance(child, Tag):
                continue
            if child_depth > MAX_HTML_DEPTH:
                raise HtmlTooComplexError("html_zu_tief")
            elements += 1
            if elements > max_elements:
                raise HtmlTooComplexError("html_zu_viele_elemente")
            stack.append((child, child_depth))


def html_to_text(
    html: str, *, max_elements: int = DEFAULT_MAX_HTML_ELEMENTS
) -> tuple[str, int]:
    """Konvertiert HTML in Klartext.

    Args:
        html: Der HTML-Teil (bereits durch `unicode_clean.clean_text` gelaufen).
        max_elements: Obergrenze der Elementzahl (`[limits] max_html_elements`).

    Returns:
        ``(text, hidden_removed)`` — `hidden_removed` ist die Anzahl entfernter
        unsichtbarer Elemente, die nicht-leeren Text enthielten (für den
        `sanitization_report`).

    Raises:
        HtmlTooComplexError: Element- oder Tiefenschranke überschritten (ADR-084).
    """
    soup = BeautifulSoup(html, "lxml")
    _check_complexity(soup, max_elements)
    hidden_removed = 0

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    for element in list(soup.find_all(True)):
        # `element.parent is None` statt `element.decomposed` (HC2-1): `Tag.decomposed`
        # liest `_decomposed`, das auf einem *lebenden* Tag nicht existiert — damit greift
        # `Tag.__getattr__` und sucht den Namen als Tag im ganzen Teilbaum ab (quadratisch,
        # 28 s bei 16 000 Ebenen). `decompose()` ruft intern `extract()`, das `parent` auf
        # `None` setzt; Nachfahren eines entfernten Elements haben ein geleertes `__dict__`
        # und liefern ebenfalls `None`. Ein Element aus `find_all` hat sonst immer einen
        # Elternknoten — die Prüfung ist bedeutungsgleich und O(1).
        if not isinstance(element, Tag) or element.parent is None:
            continue
        if _is_hidden(element):
            if element.get_text(strip=True):
                hidden_removed += 1
            element.decompose()

    for img in list(soup.find_all("img")):
        if not isinstance(img, Tag) or img.parent is None:  # HC2-1: O(1), s. o.
            continue
        if _is_tracking_pixel(img):
            img.decompose()
            continue
        alt = img.get("alt")
        if isinstance(alt, str) and alt.strip():
            img.replace_with(NavigableString(f"[Bild: {alt.strip()}]"))
        else:
            img.decompose()

    for anchor in list(soup.find_all("a")):
        if not isinstance(anchor, Tag) or anchor.parent is None:  # HC2-1: O(1), s. o.
            continue
        href = anchor.get("href")
        if isinstance(href, str):
            href = href.strip()
            if href and not href.startswith("#") and href not in anchor.get_text():
                # Ziel sichtbar machen: „Text (url)" — die URL wird anschließend vom
                # Link-Pass durch einen Marker ersetzt. Ohne diesen Schritt verschwände
                # ein Phishing-Ziel hinter harmlosem Ankertext (T3).
                anchor.append(NavigableString(f" ({href})"))

    text = soup.get_text(separator="\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if line:
            collapsed.append(line)
        elif collapsed and collapsed[-1] != "":
            collapsed.append("")
    return "\n".join(collapsed).strip(), hidden_removed
