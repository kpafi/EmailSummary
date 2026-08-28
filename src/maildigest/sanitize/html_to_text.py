"""HTML → Klartext (beautifulsoup4/lxml): entfernt script/style/Kommentare, unsichtbaren
Text und Tracking-Pixel; übernimmt Alt-Texte markiert und macht href-Ziele sichtbar.

Politik: docs/SECURITY.md §4 (T2). Umsetzung in WP3 (Hidden-Text-Heuristik: ADR-027).

Wichtig für Aufrufer: Das Ergebnis ist noch **nicht** fertig sanitisiert — es muss danach
durch `unicode_clean.clean_text` (HTML-Entities wie ``&zwnj;`` erzeugen beim Parsen neue
Zero-Width-Zeichen) und durch `links.LinkCollector.scrub` laufen.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString

__all__ = ["html_to_text"]

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


def html_to_text(html: str) -> tuple[str, int]:
    """Konvertiert HTML in Klartext.

    Returns:
        ``(text, hidden_removed)`` — `hidden_removed` ist die Anzahl entfernter
        unsichtbarer Elemente, die nicht-leeren Text enthielten (für den
        `sanitization_report`).
    """
    soup = BeautifulSoup(html, "lxml")
    hidden_removed = 0

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    for element in list(soup.find_all(True)):
        if not isinstance(element, Tag) or element.decomposed:
            continue
        if _is_hidden(element):
            if element.get_text(strip=True):
                hidden_removed += 1
            element.decompose()

    for img in list(soup.find_all("img")):
        if not isinstance(img, Tag) or img.decomposed:
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
        if not isinstance(anchor, Tag) or anchor.decomposed:
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
