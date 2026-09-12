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
6. **Markup- und Strukturneutralisierung** (:func:`neutralize_markup`) über das ganze Feld:
   Konstrukte, die kein einzelnes Zeichen sind — Unterstriche am Wortrand, Markdown am
   Zeilenanfang (Überschrift, Liste, Zitat, Discord-Subtext), Massen-Pings — und die
   Zeilen-Präfixe des Nachrichtenformats, die nur der Composer erzeugen darf (CT-7/CT-8).

Die Zusammenbau-Stufe (:mod:`maildigest.output.composer`) legt darüber noch
:func:`final_guard` über die **fertige** Nachricht: ein enger, von der Segmentierung
unabhängiger Nachbrenner, der lebende Schemata, ``www.``-Präfixe und domainartige Token
defangt. Damit hängt die Invariante I3 nicht an der Korrektheit der Segmentierungs-Regex,
sondern an einer zweiten, sehr einfachen Regel (Defense in Depth).
"""

from __future__ import annotations

import html
import re
import unicodedata

from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.unicode_clean import clean_text

__all__ = [
    "CONTINUATION_PREFIX",
    "DISCORD_MAX_PART_CHARS",
    "SIGNAL_MAX_PART_CHARS",
    "TELEGRAM_MAX_PART_CHARS",
    "final_guard",
    "neutralize_markup",
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

#: Runden für Zeilenanfangs-Markdown. Höher als :data:`_MAX_ROUNDS`, weil jede Runde nur
#: einen Marker je Zeile entfernt und Struktur-Emojis den Anfang zusätzlich verschieben.
_MAX_LINE_MARKUP_ROUNDS = 12

#: Zeichen, die Struktur/Formatierung erzeugen können und in untrusted Text nichts zu
#: suchen haben. `[`/`]` fallen mit, damit niemand einen Sanitizer-Marker fälschen kann
#: und `[text](ziel)` nicht als Markdown-Link zusammenfindet. `_` steht **nicht** in dieser
#: Liste, wird aber gezielt entschärft (:data:`_RE_UNDERSCORE_RUN`/:data:`_RE_UNDERSCORE_EDGE`):
#: Ein `_` mitten in einem Wort (`rechnung_2024.pdf`) rendert nirgends, ein `_` am Wortrand
#: (`__fett__`, `_kursiv_`) rendert in Discord sehr wohl (CT-7).
_MARKUP_CHARS = "`*|~\\[]"

_RE_MARKUP = re.compile("[" + re.escape(_MARKUP_CHARS) + "]")

_RE_TAG = re.compile(r"<[^<>]{0,400}>")
_RE_ANGLE = re.compile(r"[<>]")

#: Markup-Zeichen als Zeichenklassen-Inhalt (für die Ausschlüsse unten).
_MARKUP_CLASS = re.escape(_MARKUP_CHARS)

#: Ein bereits defangtes Fragment: normales Zeichen oder ein `[.]`/`[:]`-Token.
#: Markup-Zeichen sind hier **ausgeschlossen** (HT-1): Der WP3-Sanitizer erzeugt in
#: defangten Formen nie ein ``*``/``|``/``~``/Backtick — steht dort eines, ist der Span in
#: Wahrheit vom Modell gebaut und darf die Markup-Neutralisierung nicht überspringen.
_DEFANGED_ATOM = rf"(?:[^\s{_MARKUP_CLASS}]|\[[.:]\])"

#: Inhalt eines `[Link #n: …]`-Markers: wie oben, aber Leerzeichen sind erlaubt
#: (`(Achtung: Punycode)`), Zeilenumbrüche nicht.
_MARKER_ATOM = rf"(?:[^\n{_MARKUP_CLASS}]|\[[.:]\])"

#: Schema-Namen ohne `//`, die in Messengern als Aktion gelten (ADR-036). Einmal
#: definiert, zweimal gebraucht: zum Brechen im Nachbrenner und zum Wiedererkennen der
#: bereits gebrochenen Form als „sichere Form".
_BARE_SCHEME_NAMES = "javascript|vbscript|data|tg|intent|market|smb"

#: Vom WP3-Sanitizer erzeugte bzw. vom Nachbrenner hinterlassene, bereits sichere Formen
#: (ADR-028/ADR-036). Werden unverändert durchgereicht — erneutes Scrubben würde Marker
#: verschachteln und die Nummerierung zerstören.
#:
#: Die gebrochenen Aktions-Schemata (``javascript[:]``) gehören ausdrücklich dazu (HT-6):
#: Ohne sie riss ein **zweiter** Durchlauf die Klammern als Markup wieder heraus und machte
#: aus ``javascript[:]`` erneut ``javascript:``. Zweite Durchläufe sind real — der Composer
#: scrubbt Hinweiszeilen und die bereits sanitisierten Sammel-Digest-Kopfzeilen (ADR-049)
#: ein weiteres Mal.
_RE_SAFE_SPAN = re.compile(
    rf"\[(?:Link|Mail) #\d{{1,5}}: {_MARKER_ATOM}{{1,200}}\]"
    r"|\[Tel #\d{1,5}\]"
    rf"|(?:hxxps?|fxps?|mailto|tel|{_BARE_SCHEME_NAMES})\[:\]{_DEFANGED_ATOM}{{0,300}}"
    rf"|{_DEFANGED_ATOM}{{0,120}}\[\.\]{_DEFANGED_ATOM}{{0,120}}"
    # Einzelnes Defang-Token ohne Kontext. Es entsteht, wenn der Nachbrenner eine
    # `](`-Naht auftrennt und dabei ein `[:]`/`[.]` aus seinem Wort löst (HT-6). Ohne
    # diese Alternative fräste der nächste Durchlauf die Klammern wieder heraus.
    r"|\[[.:]\]"
)

#: Lebendes URL-Schema (auch mit eingeschobenen Leerzeichen) — wird im Nachbrenner
#: gebrochen statt entfernt, damit der Nutzer sieht, dass dort etwas stand.
_RE_LIVE_SCHEME = re.compile(r"(?i)\b(h\s*t\s*t\s*p\s*s?|f\s*t\s*p\s*s?)\s*:\s*/\s*/")

#: Jedes **andere** Schema mit `://` (`tg://`, `steam://`, `file://`, …). Telegram und
#: Discord verlinken Deep-Link-Schemata ebenfalls; die Regel ist deshalb bewusst generisch
#: (Allowlist-Haltung: nicht „welche Schemata sind gefährlich", sondern „kein lebendes
#: Schema überlebt"). Läuft **nach** :data:`_RE_LIVE_SCHEME`, dessen Treffer danach kein
#: `:` mehr tragen.
#:
#: Der Schema-Name ist **optional** und **ohne** ``\b`` verankert (HT-2): Gebrochen wird
#: die Sequenz ``://`` selbst, nicht der Name davor. Mit Pflicht-Name und Wortgrenze
#: überlebte ``einsehrlangeswort://ziel`` die Regel — der Name war länger als das
#: Zeichenlimit, und links davon stand keine Wortgrenze. In einer fertigen Nachricht ist
#: ``://`` nie legitim, also darf die Regel hier großzügig zuschlagen.
_RE_LIVE_SCHEME_ANY = re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,15})?\s*:\s*/\s*/")

#: Schemata **ohne** `//`, die in Messengern trotzdem als Aktion interpretiert werden
#: können. Hier ist eine Namensliste unvermeidlich: `wort:wort` ist im Fließtext normal,
#: eine generische Regel würde „Achtung:Bitte" zerlegen. Der Fund wird nur gebrochen,
#: wenn direkt ein Nicht-Leerzeichen folgt.
_RE_LIVE_SCHEME_BARE = re.compile(rf"(?i)\b({_BARE_SCHEME_NAMES})\s*:(?=\S)")

_RE_LIVE_WWW = re.compile(r"(?i)\bwww\s*\.")

#: Punkt-Varianten, die IDN-fähige Clients wie einen Label-Trenner behandeln, die aber
#: NFKC **nicht** auf `.` abbildet (U+3002 ideographischer Punkt, U+FF61 halbbreit).
#: Ohne diese Übersetzung sähe `boese。example` für jede Domain-Regel unauffällig aus.
_IDN_DOTS = str.maketrans({"。": ".", "｡": "."})

#: Domainartiges Token (mind. zwei Labels). Die Entscheidung, ob defangt wird, fällt in
#: :func:`_defang_domain_match` — nur so bleiben „3.14" oder „z.B." unangetastet.
#: Zwei bewusste Abweichungen vom naheliegenden `\b…\b` (beide HT-4):
#:
#: * **ASCII statt `\w`:** `\w` ist Unicode-fähig, weshalb `evil.comÄ` gar nicht erst als
#:   Token erkannt und damit nie defangt wurde — ein Nicht-ASCII-Buchstabe direkt hinter
#:   der TLD reichte, um den Nachbrenner auszuhebeln.
#: * **Bindestrich/Unterstrich links erlaubt:** Ein vorangestelltes `-` machte aus
#:   `evil.example` ein `-evil.example`, das die Regel komplett verfehlte. Messenger
#:   verlinken trotzdem, weil ein Label nicht mit `-` beginnen darf und ihr Linkifier dort
#:   neu ansetzt. Der Match darf deshalb hinter einem `-` beginnen; nur mitten in einem
#:   alphanumerischen Lauf neu anzusetzen wäre sinnlos (der Lauf ist schon konsumiert).
#: * **Kein DNS-Längendeckel (HC-24):** ``[a-z0-9]{0,62}`` deckelte die *Erkennung* auf
#:   63 Oktette. Eine 64 Zeichen lange erste Marke fiel damit komplett durch — der
#:   Nachbrenner sah gar kein Token mehr und ließ die Punkte leben. Die Erkennung ist
#:   jetzt **ohne jeden Längendeckel**; ob defangt wird, entscheidet ausschließlich
#:   :func:`_defang_domain_match`. Über-Defang ist hier der fail-safe Ausgang (ADR-036
#:   nimmt ihn ausdrücklich in Kauf). Auch ein großzügiger Deckel wäre falsch: Das
#:   Property-Orakel hatte denselben und konnte die Klasse deshalb nicht finden — eine
#:   Schranke, die Prüfling und Orakel teilen, ist keine Prüfung (Nachtrag zu ADR-058).
#:   Backtracking ist unkritisch, weil ``.`` in keiner der Zeichenklassen vorkommt: Die
#:   Zerlegung in Marken ist eindeutig.
_RE_DOMAINISH = re.compile(
    r"(?<![A-Za-z0-9])[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+(?![A-Za-z0-9_\-])",
    re.IGNORECASE,
)

#: Nackte IPv4-Adresse. Die Lookarounds sind — wie bei :data:`_RE_DOMAINISH` nach HT-4 —
#: **ASCII-Grenzen und keine Wortgrenzen** (HC-9): Mit ``(?<![\w.\-])``/``(?![\w.\-])``
#: überlebte jede Adresse mit einem Nachbarzeichen (``-192.0.2.1/login``, ``192.0.2.1x``,
#: ``192.0.2.1_neu``, ``a.192.0.2.1``) den Nachbrenner ungebrochen. Links darf der Treffer
#: deshalb hinter ``-``/``_``/``.`` beginnen; rechts wird nur der Fall ausgeschlossen, in
#: dem der Treffer Teil einer **längeren** Zahl/IP wäre: ``(?!\.?\d)`` — direkt folgende
#: Ziffer (``0.0.0.0000``) oder ein fünftes Oktett (``1.2.3.4.5``). Der Bericht schlug
#: ``(?![0-9.])`` vor; das ließ ``192.0.2.1.`` (abschließender Wurzelpunkt) ungebrochen
#: durch, obwohl ein Linkifier daraus sehr wohl ein Ziel macht. Die gewählte Form defangt
#: strikt mehr — die fail-safe Richtung (ADR-036). Dass ``3.14``, ``1.2.3`` und ``v2.10.1``
#: lesbar bleiben, kommt aus der Vier-Oktett-Form, nicht aus den Lookarounds.
#:
#: **Vier oder mehr** Oktette (S-4): Mit genau ``{3}`` fand die Regex in einer längeren
#: Punktkette (``1.1.1.1.1.1.1.``) nur das **letzte** Vier-Oktett-Fenster — der Treffer am
#: Anfang scheiterte am Lookahead (fünftes Oktett), das nächste Fenster begann hinter einem
#: Punkt und passte. Ergebnis ``1.1.1.1[.]1[.]1[.]1.``: Die ersten vier Oktette lebten
#: weiter, und genau diese Form (``1.1.1.1`` vor ``[``) autolinkt ein Client. Mit ``{3,}``
#: erfasst der Treffer die **ganze** Kette, und die Ersetzung bricht jeden Punkt darin.
#: ``0.0.0.0000`` und ``1.2.3.4.5678`` bleiben wie bisher unangetastet (kein Fenster endet
#: vor einer Ziffer); die Regressionsprobe steht neben dem Property-Test, der die Lücke
#: fand (`test_hot_properties.py::test_s4_punktkette_wird_ganz_gebrochen`).
_RE_IPV4 = re.compile(r"(?<![A-Za-z0-9])\d{1,3}(?:\.\d{1,3}){3,}(?!\.?\d)")

#: `](` unmittelbar hintereinander ist die Markdown-Link-Syntax. Nach dem Scrubbing steht
#: im Ziel zwar nie eine URL, aber die Form soll gar nicht erst entstehen (T7).
_RE_MARKDOWN_SEAM = re.compile(r"\]\(")

#: Unterstrich-Läufe (`__fett__`, `___`) auf **einen** Unterstrich zusammenziehen. Damit
#: kann aus `a__b__c` keine Unterstreichung mehr werden, ohne dass ein Dateiname zerfällt.
_RE_UNDERSCORE_RUN = re.compile(r"__+")

#: Ein Unterstrich am Wortrand ist in Discord eine Kursiv-Klammer (`_kursiv_`) — er fällt
#: weg. Mitten in einem alphanumerischen Lauf (`rechnung_2024`) bleibt er stehen: Dort
#: rendert kein Messenger etwas, und ein zerstörter Dateiname wäre ein realer Verlust.
_RE_UNDERSCORE_EDGE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")

#: Discord-Massen-Pings. `allowed_mentions` verhindert den Ping bereits im Adapter; die
#: Zeichenkette selbst soll trotzdem nicht wie ein echter Ping aussehen (CT-7/CT-7a).
_RE_MASS_MENTION = re.compile(r"(?i)@(everyone|here)\b")

#: Markdown-Konstrukte, die **nur am Zeilenanfang** rendern: Überschriften (`#`..`######`),
#: Discord-Subtext (`-#`), Zitate (`>`/`>>>`) und Listen (`-`/`+`/`1.`/`1)`). Sie werden
#: nicht durch Zeichenlöschung erfasst, weil dieselben Zeichen mitten im Satz harmlos sind.
_RE_LINE_MARKUP = re.compile(r"(?m)^[ \t]*(-#|#{1,6}|>{1,3}|[-+]|\d{1,3}[.)])(?=[ \t]|$)[ \t]*")

#: Zeilen-Präfixe des Nachrichtenformats (docs/ARCHITECTURE.md §7). Nur der Composer darf
#: sie erzeugen; in untrusted Text am Zeilenanfang wären sie eine gefälschte Programmzeile
#: (CT-8) — insbesondere die Hinweiszeile ist der einzige Warnkanal des Produkts.
_STRUCTURE_EMOJI = "⚠\U0001f4e7\U0001f4ce\U0001f50d\U0001f5c2"
_RE_STRUCTURE_EMOJI = re.compile(
    rf"(?m)^[ \t]*(?:[{_STRUCTURE_EMOJI}][\ufe0e\ufe0f]?[ \t]*)+"
)

#: Beschriftete Strukturzeilen (`From: …`, `Subject: …`, `🔍 Notes: …`, `Stage: …`).
#: Der Doppelpunkt wird zum Trennpunkt — die Zeile bleibt lesbar, sieht aber nicht mehr
#: wie eine vom Programm erzeugte Kopfzeile aus.
#:
#: Die deutschen Beschriftungen stehen weiterhin in der Liste: Die Ausgabe ist zwar seit
#: der Umstellung auf Englisch die einzige, die das Programm selbst erzeugt — ein Angreifer
#: darf aber auch keine *deutsch* aussehende Kopfzeile fälschen können, und ein Zurück-
#: übersetzen der Ausgabe darf diese Schutzschicht nicht still aushebeln (CT-8).
_RE_STRUCTURE_LABEL = re.compile(
    r"(?mi)^[ \t]*("
    r"From|Subject|Notes|Stage|Reason|SUSPECTED PHISHING"
    r"|Von|Betreff|Hinweise|Stufe|Grund|PHISHING-VERDACHT"
    r")[ \t]*:[ \t]*"
)

_RE_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_RE_MANY_NEWLINES = re.compile(r"\n{3,}")

#: Kürzungsmarker für Felder über ihrem Einzellimit (ohne Klammern: die fielen der
#: Markup-Neutralisierung zum Opfer).
_TRUNCATION_MARKER = " …"

#: Präfix jedes Fortsetzungsstücks eines harten Zeilenschnitts (HC-6, ADR-062-Nachtrag).
#: U+2026 + Leerzeichen: neutral, ohne Markup-Wirkung, und es verhindert, dass ein Schnitt
#: mitten in der Zeile ein Struktur-Emoji oder eine Kopfzeilen-Beschriftung an einen
#: **Zeilenanfang** schiebt, den keine Schicht mehr prüft (ARCHITECTURE §7).
CONTINUATION_PREFIX = "… "


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


def _neutralize_line_markup(match: re.Match[str]) -> str:
    """Ersetzt ein Markdown-Konstrukt am Zeilenanfang.

    Aufzählungs-Präfixe werden zu ``• `` (rendert nirgends, hält aber die Liste lesbar);
    bei nummerierten Listen bleibt die Zahl stehen und nur das Satzzeichen wird ersetzt —
    ``12. März …`` am Zeilenanfang ist meist ein Datum und kein Listenpunkt.
    Überschriften, Zitate und Discord-Subtext fallen ersatzlos weg.
    """
    marker = match.group(1)
    if marker in ("-", "+"):
        return "• "
    if marker[0].isdigit():
        return f"{marker[:-1]} · "
    return ""


def neutralize_markup(text: str) -> str:
    """Entschärft Formatierung und gefälschte Strukturzeilen in untrusted Text (F-SEC-3).

    Ergänzt die zeichenweise Neutralisierung (:data:`_RE_MARKUP`) um die Konstrukte, die
    sich nicht an einem einzelnen Zeichen festmachen lassen (CT-7/CT-7a/CT-8):

    * Unterstriche am Wortrand (`__fett__`, `_kursiv_`) und Unterstrich-Läufe,
    * Zeilenanfangs-Markdown: Überschriften, Zitate, Listen, Discord-Subtext ``-#``,
    * Discord-Massen-Pings (``@everyone``/``@here``),
    * die Zeilen-Präfixe des Nachrichtenformats (``⚠️``, ``📧``, ``📎``, ``🔍``, ``Von:`` …).

    Der letzte Punkt ist eine Struktur- und keine Formatierungsfrage: Vertrauenswürdige
    Zeilen erzeugt allein der Composer, deshalb darf modellgelieferter Text sie am
    Zeilenanfang nicht nachbauen (docs/ARCHITECTURE.md §7).
    """
    cleaned = _RE_UNDERSCORE_EDGE.sub("", _RE_UNDERSCORE_RUN.sub("_", text))
    cleaned = _RE_MASS_MENTION.sub(r"(at)\1", cleaned)
    # Bis zum Fixpunkt: Jede Runde entfernt genau einen Marker je Zeile, und ein
    # vorangestelltes Struktur-Emoji verschiebt den Zeilenanfang um eine weitere Runde.
    # `⚠️# # #` brauchte deshalb vier — bei drei blieb ein `#` stehen (HT-13). Die Schranke
    # ist großzügig statt knapp; sie begrenzt nur den Aufwand, nicht die Wirkung.
    for _ in range(_MAX_LINE_MARKUP_ROUNDS):
        stripped = _RE_LINE_MARKUP.sub(_neutralize_line_markup, cleaned)
        stripped = _RE_STRUCTURE_EMOJI.sub("", stripped)
        if stripped == cleaned:
            break
        cleaned = stripped
    return _RE_STRUCTURE_LABEL.sub(r"\1 · ", cleaned)


def _strip_control(text: str) -> str:
    """Entfernt jedes „C*"-Zeichen außer Tab/Zeilenumbruch — ohne NFKC (F-SEC-10).

    Zweiter Durchgang **nach** der Link-Erkennung (HC-8): Der
    :class:`~maildigest.sanitize.links.LinkCollector` arbeitet intern mit
    ``\x00``-Platzhaltern. Schneidet eine seiner Regexen in ein gesetztes Token, bleibt ein
    rohes U+0000 im Text stehen — genau das erreichte die Zustellung. Die Zusage „kein
    Steuerzeichen verlässt dieses Modul" hängt damit nicht mehr an der Korrektheit fremder
    Link-Regexe, sondern an dieser einen Zeile (Defense in Depth).
    """
    if not any(
        char not in "\t\n" and unicodedata.category(char).startswith("C")
        for char in text
    ):
        return text
    return "".join(
        char
        for char in text
        if char in "\t\n" or not unicodedata.category(char).startswith("C")
    )


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

    return _collapse_whitespace(neutralize_markup(_strip_control("".join(chunks))))


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
    return _collapse_whitespace(
        neutralize_markup(_RE_MARKUP.sub("", _strip_tags(cleaned)))
    )


def _break_scheme(match: re.Match[str]) -> str:
    """Bricht ein lebendes Schema in seine defangte Form (`https://` ⇒ `hxxps[:]//`)."""
    scheme = re.sub(r"\s+", "", match.group(1)).lower()
    broken = "fxp" if scheme.startswith("ftp") else "hxxp"
    if scheme.endswith("s"):
        broken += "s"
    return f"{broken}[:]//"


def _defang_domain_match(match: re.Match[str]) -> str:
    """Defangt ein domainartiges Token, sobald **irgendeine** Marke wie eine TLD anfängt.

    Hintergrund (T7): Telegram und Discord verlinken nackte Domains im Klartext
    automatisch. I3 erlaubt zwar „bloßer Domain-Name in Textform", aber genau diese
    Autolinker machen daraus wieder ein klickbares Ziel. Deshalb bekommt jede Domain —
    auch die in einem `[Link #n: …]`-Marker und in Dateinamen — gebrochene Punkte.

    Geprüft wird **jede** Marke ab der zweiten, nicht nur die letzte (HT-4). Grund ist die
    Reihenfolge im Composer: Der Nachbrenner läuft vor :func:`split_parts`, und ein harter
    Schnitt kann aus einem unauffälligen Token ein Bruchstück mit neuem Ende machen —
    aus ``evil.com.123abc`` (letzte Marke ziffernbeginnend, früher unangetastet) wurde beim
    Schnitt ``evil.com``, also wieder eine lebende Domain. Da jede Marke geprüft wird, kann
    kein Bruchstück mehr TLD-förmig enden, ohne dass das Token schon gebrochen war.

    „TLD-förmig" heißt: mindestens zwei Zeichen lang und mit zwei Buchstaben beginnend.
    Damit bleiben Zahlen unangetastet — ``3.14``, ``1.2.3`` und ``2.0rc1`` sind keine
    Domains und sollen lesbar bleiben.
    """
    token = match.group(0)
    labels = token.split(".")
    if any(len(label) >= 2 and label[:2].isalpha() for label in labels[1:]):
        return token.replace(".", "[.]")
    return token


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
    guarded = _RE_LIVE_SCHEME_ANY.sub(lambda m: f"{m.group(1) or ''}[:]//", guarded)
    guarded = _RE_LIVE_SCHEME_BARE.sub(lambda m: f"{m.group(1)}[:]", guarded)
    guarded = _RE_LIVE_WWW.sub("www[.]", guarded)
    guarded = _RE_ANGLE.sub("", guarded)
    guarded = _RE_MARKDOWN_SEAM.sub("] (", guarded)
    # Formatierung, die kein einzelnes Zeichen ist: Unterstriche am Wortrand und
    # Massen-Pings. Bewusst **ohne** die Zeilenanfangs-Regeln aus
    # :func:`neutralize_markup` — die dürfen nur auf Feldern laufen, nie auf der fertigen
    # Nachricht, deren eigene Struktur-Präfixe (`📧 `, `Von: `) genau so aussehen (CT-8).
    guarded = _RE_UNDERSCORE_EDGE.sub("", _RE_UNDERSCORE_RUN.sub("_", guarded))
    guarded = _RE_MASS_MENTION.sub(r"(at)\1", guarded)
    guarded = _RE_DOMAINISH.sub(_defang_domain_match, guarded)
    return _RE_IPV4.sub(lambda m: m.group(0).replace(".", "[.]"), guarded)


def _split_long_line(line: str, limit: int) -> list[str]:
    """Zerlegt eine einzelne, zu lange Zeile — bevorzugt an einem Leerzeichen.

    Jedes Stück **nach dem ersten** bekommt das Fortsetzungspräfix :data:`CONTINUATION_PREFIX`
    (HC-6). Grund: Nur der harte Schnitt *innerhalb* einer Zeile kann einen Zeilenanfang
    erzeugen, den keine Schicht geprüft hat — Schnitte an Zeilengrenzen liefern Anfänge,
    die :func:`neutralize_markup` bereits entschärft hat oder die vom Composer stammen.
    Ohne das Präfix begann Teil 2 einer Discord-Nachricht (Limit 2000) bei einem
    Summary-Limit von 3000 mit einer vollständig gefälschten Programmzeile
    (``⚠️ SUSPECTED PHISHING: …``) — im einzigen Warnkanal des Produkts.

    Das Präfix zählt zum Limit; :func:`split_parts` gibt deshalb nie ein Stück über
    `limit` zurück. Ist `limit` kleiner als das Präfix selbst (Testgrößen), entfällt es —
    dort ist ohnehin kein Struktur-Präfix mehr darstellbar.
    """
    pieces: list[str] = []
    rest = line
    prefix = ""
    continuation = CONTINUATION_PREFIX if limit > len(CONTINUATION_PREFIX) else ""
    while len(prefix) + len(rest) > limit:
        budget = limit - len(prefix)
        window = rest[:budget]
        cut = window.rfind(" ")
        if cut < budget // 2:  # kein brauchbarer Trennpunkt ⇒ hart schneiden
            cut = budget
        pieces.append(prefix + rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
        prefix = continuation
    if rest:
        pieces.append(prefix + rest)
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
