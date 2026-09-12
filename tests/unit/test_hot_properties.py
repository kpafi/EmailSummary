"""Property-Based-Tests (hypothesis) — Hot-Testing WP10, docs/TESTING.md §2 Punkt 3.

Die übrigen Testdateien prüfen konkrete Payloads. Hier werden stattdessen die
**Eigenschaften** geprüft, die für *beliebige* Eingaben gelten müssen. Vier Kern-Properties
aus docs/TESTING.md §2:

(a) **Output-Sanitizer** (`final_guard(scrub_field(x))`, genau die Kette des Composers):
    kein lebendes Schema (auch `tg://`, `javascript:`, `data:` — ADR-036), kein HTML-Tag,
    keine Steuerzeichen, kein Telegram-/Discord-Markup, keine lebenden Domain-Punkte
    (inkl. U+3002/U+FF61).
(b) **Link-Erkennung**: bekannte Obfuskationen einer Domain werden vom WP3-`LinkCollector`
    erkannt und durch einen Marker ersetzt.
(c) **Unicode-Cleaning**: das Ergebnis enthält kein „C*"-Zeichen außer Tab/Newline.
(d) **Summarizer-Nachkontrolle**: für beliebige Modellantworten enthält die `Summary`
    keine URL **oder** `injection_suspected` ist gesetzt.

Bewusst **kein** Bug: Die in ADR-033 dokumentierte Schichtgrenze von WP5 (keine
NFKC-Normalisierung, nackte Domains/IPs passieren) wird hier nicht als Verletzung gewertet,
sondern als Eigenschaft der **WP7**-Schicht geprüft — dort muss sie halten
(:func:`test_wp7_closes_the_wp5_layer_gap`).

`hypothesis` ist Dev-Dependency (ADR-058); `src/maildigest/` importiert sie nie.
"""

from __future__ import annotations

import re
import unicodedata

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from maildigest.agents.summarizer import enforce_output_policy, scrub_text
from maildigest.models import (
    CriticVerdict,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.output.composer import DigestComposer
from maildigest.output.sanitizer import (
    CONTINUATION_PREFIX,
    DISCORD_MAX_PART_CHARS,
    TELEGRAM_MAX_PART_CHARS,
    final_guard,
    scrub_field,
    scrub_plain,
    split_parts,
)
from maildigest.sanitize.links import LinkCollector
from maildigest.sanitize.unicode_clean import clean_text

# --- Strategien -----------------------------------------------------------------------

#: Vollständig freier Unicode-Text — der harte Fall für die Unicode-Eigenschaften.
ANY_TEXT = st.text(st.characters(), max_size=200)

#: Bausteine, aus denen sich Angriffe zusammensetzen. Rein zufälliger Text trifft die
#: interessanten Zustände (Marker-Grammatik, Entity-Ketten, IDN-Punkte) praktisch nie —
#: dieses Alphabet erzeugt sie systematisch.
_PIECES = [
    "[", "]", "(", ")", "<", ">", ".", ":", "/", "\\", "*", "`", "|", "~", "_", "#",
    "&", ";", "%", "@", "-", "+", " ", "\n", "\t", "a", "b", "x", "1", "9",
    "http", "https", "ftp", "hxxp", "www", "mailto", "tel", "javascript", "data",
    "tg", "vbscript", "intent", "market", "smb", "steam", "file",
    "&#104;", "&#58;", "&amp;", "&lt;", "&gt;", "&#x2F;",
    "。", "｡", "﻿", "​", "‮", "­",
    "ｈ", "ｔ", "０", "＠",
    "Link", "Mail", "Tel", "#1:", "example", "com", "de", "evil",
    "script", "img", "src=", "onerror=", "]#(", "://", "[.]", "[:]", "(.)", "(dot)",
    "xn--", "192.0.2.1", "а",  # kyrillisches a (Homoglyph)
]

#: Lange Wiederholungsläufe (≥ 128 gleiche Zeichen). Sie fehlten der Strategie komplett —
#: und genau dort lag HC-8: ``_RE_MAILTO`` deckelt bei 128 Zeichen, schnitt deshalb in
#: einen bereits gesetzten ``\x00``-Platzhalter und ließ ein rohes U+0000 zurück. Ein
#: zufälliges Alphabet erzeugt so einen Lauf praktisch nie.
_LONG_RUNS = st.builds(
    lambda char, count: char * count,
    st.sampled_from(["a", "x", "1", "-", ".", "_", "%", "@"]),
    st.integers(min_value=120, max_value=300),
)

ATTACK_TEXT = st.lists(
    st.one_of(st.sampled_from(_PIECES), _LONG_RUNS), min_size=1, max_size=25
).map("".join)

#: Beide Quellen gemischt: strukturierte Angriffe und wilder Unicode.
UNTRUSTED = st.one_of(ATTACK_TEXT, ANY_TEXT, st.tuples(ATTACK_TEXT, ANY_TEXT).map("".join))

#: Wie :data:`UNTRUSTED`, aber ohne einzelne Surrogate (U+D800..U+DFFF). Modellausgaben
#: entstehen aus `json.loads` über dekodiertem UTF-8 und können solche Codepunkte nicht
#: enthalten; das `Summary`-Schema lehnt sie zusätzlich schon in pydantic ab. Sie hier
#: mitzuerzeugen prüfte den Testaufbau, nicht die Nachkontrolle.
_NO_SURROGATES = st.characters(blacklist_categories=["Cs"])
MODEL_OUTPUT = st.one_of(
    ATTACK_TEXT,
    st.text(_NO_SURROGATES, max_size=200),
    st.tuples(ATTACK_TEXT, st.text(_NO_SURROGATES, max_size=200)).map("".join),
)

_SLOW = settings(
    max_examples=600,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# --- Verbotsmuster (die Eigenschaft, negativ formuliert) --------------------------------

#: Ein lebendes Schema jeder Art: `http://`, `tg://`, `steam://` — und die `//`-losen
#: Aktions-Schemata aus ADR-036.
_RE_LIVE_SCHEME_ANY = re.compile(r"(?i)[a-z][a-z0-9+.\-]{0,32}\s*:\s*/\s*/|://")
_RE_LIVE_BARE_SCHEME = re.compile(
    r"(?i)\b(javascript|vbscript|data|tg|intent|market|smb)\s*:(?=\S)"
)
_RE_TAG = re.compile(r"[<>]")
_RE_WWW = re.compile(r"(?i)\bwww\s*\.")

#: Ein vollständiges domainartiges Token, dessen Ende TLD-förmig ist — genau das, was
#: Telegram/Discord automatisch verlinken (T7). Nach dem Nachbrenner darf es das nur noch
#: mit gebrochenen Punkten (`[.]`) geben.
#:
#: Die Token-Grenzen (`.` im Lookaround) sind wesentlich: `a.http.a` endet auf die Marke
#: `a` und ist damit keine verlinkbare Domain — nur ein Teilstring davon sähe wie eine aus.
#: **Das Orakel muss strikt großzügiger sein als die Implementierung** (HC-24). Vorher
#: übernahm es mit ``{0,62}``/``{1,63}`` exakt die DNS-Längenschranke und mit ``[\w\-.]``
#: exakt die Wortgrenze von ``output.sanitizer._RE_DOMAINISH`` — es konnte die Lücke, die
#: es prüfen soll, prinzipiell nicht finden (dieselbe Fehlerart wie HT-1/2/4/6, Nachtrag
#: zu ADR-058). Jetzt ohne Längendeckel und ohne Unterstrich in der rechten Wortgrenze:
#: Das Orakel schlägt auch dort an, wo die Implementierung bewusst nicht defangt.
_RE_LIVE_DOMAIN = re.compile(
    r"(?<![\w\-.])[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.[a-z]{2}[a-z0-9\-]*"
    r"(?![\w\-.])",
    re.IGNORECASE,
)

#: Eine lebende Vier-Oktett-Folge — nach dem Nachbrenner darf es sie nur gebrochen geben
#: (HC-9). Das Orakel prüfte IPv4 bisher überhaupt nicht, die Klasse war testseitig blind.
#: Das Orakel beschreibt die **Eigenschaft**, nicht die Regex der Implementierung: ein
#: eigenständiges Token aus genau vier Oktetten. Ein Ziffernlauf, der links an einem
#: Buchstaben/einer Marke hängt (``a192.0.2.1``, ``a.192.0.2.1``) oder rechts weiterläuft
#: (``0.0.0.0000``, ``1.2.3.4.5``), ist keine Adresse — kein Linkifier macht daraus ein
#: Ziel. Ein abschließender Wurzelpunkt (``192.0.2.1.``) dagegen schon; er ist erlaubt und
#: wird gefunden. Die Implementierung defangt an mehreren Stellen **mehr** als dieses
#: Orakel verlangt (Über-Defang ist der fail-safe Ausgang, ADR-036).
_RE_LIVE_IPV4 = re.compile(
    r"(?<![A-Za-z0-9.])\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)"
)

#: Markup, das Telegram/Discord als Formatierung interpretieren (Discord rendert Markdown
#: im `content`-Feld). `_` steht nicht in der Menge, weil es im Wortinneren erlaubt bleibt
#: (`rechnung_2024`); am Wortrand prüft es :data:`_RE_UNDERSCORE_EDGE` (CT-7).
_MARKUP = set("`*|~\\")

#: Ein Unterstrich am Wortrand ist in Discord eine Kursiv-/Unterstreichungsklammer.
_RE_UNDERSCORE_EDGE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")

#: Discord-Massen-Pings — im gelieferten Text darf die Zeichenkette nicht stehen (CT-7).
_RE_MASS_MENTION = re.compile(r"(?i)@(everyone|here)\b")

#: Markdown, das **am Zeilenanfang** rendert: Überschrift, Zitat, Liste, Discord-Subtext.
_RE_LINE_MARKUP = re.compile(r"(?m)^[ \t]*(?:-#|#{1,6}|>{1,3}|[-+]|\d{1,3}[.)])(?=[ \t]|$)")

#: Zeilen-Präfixe des Nachrichtenformats (docs/ARCHITECTURE.md §7) — nur der Composer darf
#: sie erzeugen, modellgelieferter Text nie (CT-8).
#: Seit HC-6 stehen hier auch die **englischen** Beschriftungen und die Banner-Zeile
#: ``SUSPECTED PHISHING:`` — die Ausgabe ist englisch, das Orakel prüfte bis dahin nur die
#: deutschen Formen und hätte die gefälschte Warnzeile aus dem Split nie gesehen.
_RE_STRUCTURE_LINE = re.compile(
    r"(?mi)^[ \t]*(?:[\u26a0\U0001f4e7\U0001f4ce\U0001f50d\U0001f5c2]"
    r"|(?:From|Subject|Notes|Stage|Reason|SUSPECTED PHISHING"
    r"|Von|Betreff|Hinweise|Stufe|Grund|PHISHING-VERDACHT)[ \t]*:)"
)

#: IDN-Punktvarianten, die ein Client wie `.` behandelt (SECURITY §5, WP7).
_IDN_DOTS = "。｡"


def _without_whitespace(text: str) -> str:
    """Nur die Nicht-Leerraum-Zeichen — die Invariante des Nachrichten-Splits."""
    return "".join(text.split())


def assert_output_safe(text: str) -> None:
    """Die zentrale Eigenschaft (a) über einem fertigen Nachrichtentext (I3/F-SEC-3)."""
    assert not _RE_LIVE_SCHEME_ANY.search(text), f"lebendes Schema in {text!r}"
    assert not _RE_LIVE_BARE_SCHEME.search(text), f"Aktions-Schema in {text!r}"
    assert not _RE_TAG.search(text), f"Winkelklammer in {text!r}"
    assert not _RE_WWW.search(text), f"www-Präfix in {text!r}"
    assert "](" not in text, f"Markdown-Naht in {text!r}"
    assert not _RE_LIVE_DOMAIN.search(text), f"lebende Domain in {text!r}"
    assert not _RE_LIVE_IPV4.search(text), f"lebende IPv4 in {text!r}"
    assert not (_MARKUP & set(text)), f"Messenger-Markup in {text!r}"
    assert not _RE_UNDERSCORE_EDGE.search(text), f"Unterstrich am Wortrand in {text!r}"
    assert not _RE_MASS_MENTION.search(text), f"Massen-Ping in {text!r}"
    assert not (set(_IDN_DOTS) & set(text)), f"IDN-Punkt in {text!r}"
    for char in text:
        if char in "\t\n":
            continue
        assert not unicodedata.category(char).startswith("C"), (
            f"Steuerzeichen U+{ord(char):04X} in {text!r}"
        )


# --- (a) Output-Sanitizer ---------------------------------------------------------------


@_SLOW
@given(UNTRUSTED)
def test_scrub_field_output_is_never_clickable(payload: str) -> None:
    """∀ Eingabe: Feld-Scrub + Nachbrenner liefern keinen klickbaren, formatierten Text."""
    assert_output_safe(final_guard(scrub_field(payload)))


@pytest.mark.parametrize(
    "payload",
    [
        # HC-8, kanonische Form: `_RE_MAILTO` deckelt bei 128 Zeichen und schnitt in den
        # Platzhalter des vorher gefundenen `http`-Links hinein.
        "mailto:" + "a" * 127 + "http://boese.example",
        "mailto:" + "a" * 200 + "http://boese.example",
        "http://boese.example" + "a" * 130 + "mailto:x@y.example",
    ],
)
def test_hc8_no_placeholder_byte_survives_the_link_pass(payload: str) -> None:
    """Kein rohes U+0000 in der Zustellung — und jeder Marker bleibt zugeordnet (HC-8)."""
    result = final_guard(scrub_field(payload))
    assert "\x00" not in result
    assert_output_safe(result)

    collector = LinkCollector()
    scrubbed = collector.scrub(payload)
    assert "\x00" not in scrubbed
    for index in range(1, collector.links_removed + 1):
        assert f"#{index}" in scrubbed, f"Marker #{index} ohne Fundstelle: {scrubbed!r}"


@_SLOW
@given(UNTRUSTED)
def test_scrub_plain_output_is_never_clickable(payload: str) -> None:
    """Gleiches gilt für Nicht-Fließtext-Felder (Domain, Anzeigename, Dateiname)."""
    assert_output_safe(final_guard(scrub_plain(payload)))


@_SLOW
@given(st.lists(UNTRUSTED, min_size=2, max_size=5))
def test_fields_of_one_message_stay_safe_together(payloads: list[str]) -> None:
    """Auch die **zusammengesetzte** Nachricht bleibt sicher (Feldgrenzen als Nahtstelle).

    Deckt den Rekonstruktions-Angriff ab: `http` im einen, `://evil.example` im nächsten
    Feld. Der Nachbrenner läuft laut Composer über den fertigen Text, nicht je Feld.
    """
    collector = LinkCollector()
    joined = "\n".join(scrub_field(item, collector=collector) for item in payloads)
    for part in split_parts(final_guard(joined), TELEGRAM_MAX_PART_CHARS):
        assert_output_safe(part)


@_SLOW
@given(UNTRUSTED)
def test_repeated_scrubbing_stays_safe(payload: str) -> None:
    """Mehrfaches Scrubben bleibt sicher — die Kette kippt nicht bei der Wiederholung.

    Wiederholtes Scrubben ist real: Der Composer schickt Hinweiszeilen und die bereits
    output-sanitisierten Sammel-Digest-Kopfzeilen (ADR-049) ein weiteres Mal durch
    `scrub_field`. Die Zusage lautet bewusst **nicht** „Ergebnis identisch": Ein zweiter
    Durchlauf darf mehr finden (eine durch den Seam-Bruch freigelegte Domain wird dann erst
    zum Marker) und darf mehr entfernen (`&#1` ohne Semikolon löst Pythons `html.unescape`
    erst beim nächsten Mal auf). Verboten ist allein, dass er ein Ergebnis **unsicherer**
    macht. Dass konkrete Defang-Formen einen zweiten Durchlauf überleben, pinnen die
    Regressionstests in `test_hot_edge_cases.py` fest (HT-6).
    """
    once = final_guard(scrub_field(payload))
    twice = final_guard(scrub_field(once))
    assert_output_safe(twice)
    assert_output_safe(final_guard(scrub_field(twice)))


@_SLOW
@given(UNTRUSTED, st.integers(min_value=1, max_value=200))
def test_split_never_produces_an_unsafe_part(payload: str, limit: int) -> None:
    """**Jeder zugestellte Teil** ist für sich sicher — nicht nur die ganze Nachricht.

    Geprüft wird der echte Weg (`DigestComposer._finalize`), nicht `split_parts` allein:
    Nur dort läuft der Nachbrenner auch **nach** der Segmentierung, und genau darauf kommt
    es an — ein harter Schnitt kann links wie rechts ein Bruchstück erzeugen, das erst für
    sich genommen wie eine Domain aussieht (HT-4).

    Die Eingabe läuft vorher durch :func:`scrub_field` — genau wie im Composer, der jedes
    Feld einzeln scrubbt und erst danach zusammensetzt. `compose_plain` selbst scrubbt
    bewusst nicht (ADR-054: sein Text stammt aus dem Code, nicht aus einer Mail).
    """
    parts = DigestComposer(part_limit=limit).compose_plain(scrub_field(payload)).parts
    assert parts, "es entsteht immer mindestens ein Teil"
    for part in parts:
        assert len(part) <= limit
        assert_output_safe(part)


@_SLOW
@given(
    st.lists(UNTRUSTED, min_size=1, max_size=4),
    st.one_of(
        st.just(DISCORD_MAX_PART_CHARS),
        st.just(TELEGRAM_MAX_PART_CHARS),
        st.integers(min_value=200, max_value=6000),
    ),
)
def test_hc6_no_part_and_no_line_starts_with_a_program_prefix(
    payloads: list[str], limit: int
) -> None:
    """Der Split erzeugt nie einen gefälschten Zeilen-/Teilanfang (HC-6, ADR-062).

    Eingabe ist ausschließlich Feldtext (durch :func:`scrub_field`) — genau das, was der
    Composer zusammensetzt. Dessen eigene Präfixe sind hier bewusst nicht dabei: Geprüft
    wird die Naht, nicht das Format. Der Bericht-Repro ist der Fall Discord/Signal
    (Teil-Limit 2000) bei einem Summary-Limit von 3000.
    """
    collector = LinkCollector()
    text = "\n".join(scrub_field(item, collector=collector) for item in payloads)
    parts = DigestComposer(part_limit=limit).compose_plain(text).parts
    for part in parts:
        assert len(part) <= limit
        for line in part.split("\n"):
            assert not _RE_STRUCTURE_LINE.match(line), (
                f"gefälschte Programmzeile {line[:60]!r} in {part[:60]!r}"
            )


@_SLOW
@given(UNTRUSTED, st.integers(min_value=1, max_value=200))
def test_split_preserves_every_non_whitespace_character(payload: str, limit: int) -> None:
    """`split_parts` wirft nur Leerraum weg — kein Zeichen geht beim Teilen verloren."""
    text = final_guard(scrub_field(payload))
    parts = split_parts(text, limit)
    assert all(len(part) <= limit for part in parts)
    stripped = [
        part[len(CONTINUATION_PREFIX) :] if part.startswith(CONTINUATION_PREFIX) else part
        for part in parts
    ]
    assert _without_whitespace("".join(stripped)) == _without_whitespace(text)


# --- (b) Link-Erkennung -----------------------------------------------------------------

#: Obfuskationen, die laut docs/SECURITY.md §4 (ADR-028) erkannt werden müssen.
_OBFUSCATIONS: list[str] = [
    "http://{d}/pfad",
    "https://{d}",
    "hxxp://{d}",
    "hxxps://{d}",
    "HTTP://{d}",
    "h t t p://{d}",
    "%68ttp://{d}",
    "ftp://{d}",
    "www.{d}",
    "{d}/pfad",
    "http://gut@{d}/",
    "http://{d}:8080/x",
]

#: Domains, deren Punkt selbst obfuskiert ist (`example(.)com`).
_DOT_TRICKS = ["(.)", "[.]", "{.}", "(dot)", "[dot]"]


@given(
    st.sampled_from(_OBFUSCATIONS),
    st.sampled_from(["evil.example", "sub.evil.example", "xn--80ak6aa92e.example"]),
)
def test_known_obfuscations_are_detected(template: str, domain: str) -> None:
    """Property (b): Jede bekannte Obfuskation erzeugt einen Marker, kein Rest-URL-Text."""
    collector = LinkCollector()
    result = collector.scrub(f"Bitte {template.format(d=domain)} anklicken")
    assert collector.links_removed >= 1, f"nicht erkannt: {template!r}"
    assert "Link #1" in result
    assert "://" not in result


@given(st.sampled_from(_DOT_TRICKS))
def test_obfuscated_dots_are_detected(trick: str) -> None:
    """Auch Domains ohne Schema, deren Punkt verschleiert ist, werden erkannt."""
    collector = LinkCollector()
    result = collector.scrub(f"Schreib an evil{trick}example bitte")
    assert collector.links_removed == 1
    assert "evil.example" in result


@_SLOW
@given(UNTRUSTED)
def test_link_collector_never_leaves_a_live_url(payload: str) -> None:
    """Nach dem WP3-Scrub steht in keinem Fund noch eine vollständige `schema://host`-URL.

    Ein **hostloses** Fragment (`http://` ohne alles) bleibt bewusst stehen: Es ist keine
    URL, hat kein Ziel und wird von der WP7-Schicht ohnehin zu `hxxp[:]//` gebrochen
    (geprüft in :func:`test_wp7_closes_the_wp5_layer_gap`).
    """
    result = LinkCollector().scrub(clean_text(payload)[0])
    assert not re.search(r"(?i)(https?|ftps?)\s*:\s*/\s*/\s*\w", result)


@_SLOW
@given(UNTRUSTED)
def test_link_collector_records_every_marker(payload: str) -> None:
    """Buchführung: Jeder Marker trägt genau eine eigene Nummer aus `links_found` (ADR-028).

    Bewusst **nicht** geprüft: aufsteigende Reihenfolge im Text. Die Nummer entsteht in der
    Reihenfolge der Erkennungs-Pässe (erst URLs, dann `mailto:`, … zuletzt nackte Domains),
    nicht in Textreihenfolge — ein `mailto:` weiter hinten kann also `#1` tragen. Für die
    Zuordnung Marker ↔ Fußnoteneintrag zählt allein die Eindeutigkeit.
    """
    collector = LinkCollector()
    result = collector.scrub(clean_text(payload)[0])
    numbers = [int(match) for match in re.findall(r"\[(?:Link|Mail|Tel) #(\d+)", result)]
    assert len(numbers) == len(set(numbers)), f"doppelte Marker-Nummer in {result!r}"
    assert all(1 <= number <= collector.links_removed for number in numbers)
    assert len(numbers) <= collector.links_removed


# --- (c) Unicode-Cleaning ---------------------------------------------------------------


@_SLOW
@given(ANY_TEXT)
def test_clean_text_removes_every_control_category(payload: str) -> None:
    """Property (c): kein „C*"-Zeichen außer Tab/Newline überlebt (F-SEC-10)."""
    cleaned, removed = clean_text(payload)
    for char in cleaned:
        assert char in "\t\n" or not unicodedata.category(char).startswith("C")
    assert removed >= 0
    assert "\r" not in cleaned, "Zeilenenden werden auf \\n vereinheitlicht"


@_SLOW
@given(ANY_TEXT)
def test_clean_text_is_idempotent_and_nfkc_stable(payload: str) -> None:
    """Zweiter Durchlauf ändert nichts; das Ergebnis ist NFKC-normalisiert."""
    once, _ = clean_text(payload)
    twice, again = clean_text(once)
    assert twice == once
    assert again == 0
    assert unicodedata.normalize("NFKC", once) == once


# --- (d) Summarizer-Nachkontrolle -------------------------------------------------------


def _mail() -> SanitizedMail:
    """Sanitisierte Mail als Bezugspunkt der Nachkontrolle (Quelle der Ersatzwerte)."""
    return SanitizedMail(
        dedupe_key="<hot@example>",
        from_display="Absender",
        from_domain="example.org",
        subject="Betreff",
        body_text="Text",
        attachment_texts={"datei.txt": "Inhalt"},
        sanitization_report=SanitizationReport(),
    )


#: URL-Formen, die nach der WP5-Nachkontrolle in keinem Feld stehen dürfen — die
#: Eigenschaft ist wörtlich die aus docs/TESTING.md §2: „keine URLs in der Summary".
#:
#: Bewusst **nicht** enthalten (dokumentierte Schichtgrenzen, geprüft in
#: :func:`test_wp7_closes_the_wp5_layer_gap`, kein Bug):
#: * nackte Domains/IPs und Fullwidth-Formen — ADR-033: WP5 normalisiert nicht nach NFKC;
#: * einzelne Winkelklammern ohne schließendes ``>`` (``<http``) — WP5 entfernt HTML-*Tags*,
#:   WP7 entfernt danach jedes ``<``/``>`` bedingungslos (ADR-027/ADR-035).
_RE_SUMMARY_URL = re.compile(r"(?i)[a-z][a-z0-9+.\-]{0,15}://|\bwww\s*\.")


@_SLOW
@given(MODEL_OUTPUT, MODEL_OUTPUT, MODEL_OUTPUT, MODEL_OUTPUT)
def test_summary_has_no_url_or_is_flagged(
    headline: str, body: str, reason: str, category: str
) -> None:
    """Property (d): beliebige Modellantwort ⇒ keine URL **oder** `injection_suspected`.

    Formuliert genau als Disjunktion aus docs/TESTING.md §2: Der Agent darf säubern, muss
    den Fund dann aber sichtbar machen — stillschweigendes Durchreichen ist der Bug.
    """
    summary = Summary(
        headline=headline[:100],
        summary_text=body,
        importance="normal",
        importance_reason=reason,
        category=category,
        attachment_summaries={"datei.txt": body, "erfunden.txt": body},
    )
    checked = enforce_output_policy(summary, _mail())

    fields = [
        checked.headline,
        checked.summary_text,
        checked.importance_reason,
        checked.category,
        *checked.attachment_summaries.values(),
    ]
    for field in fields:
        assert not _RE_SUMMARY_URL.search(field) or checked.injection_suspected, (
            f"URL überlebt ungeflaggt in {field!r}"
        )
        for char in field:
            assert char in "\t\n" or not unicodedata.category(char).startswith("C")

    assert len(checked.headline) <= 100
    assert checked.headline, "Headline wird nie leer (Fallback aus dem Betreff)"
    assert checked.summary_text, "Summary-Text wird nie leer"
    assert set(checked.attachment_summaries) <= {"datei.txt"}, (
        "erfundene Anhang-Schlüssel werden verworfen"
    )


@_SLOW
@given(UNTRUSTED)
def test_scrub_text_flags_whatever_it_removes(payload: str) -> None:
    """`scrub_text` meldet genau dann „verdächtig", wenn es etwas verändert hat."""
    cleaned, suspicious = scrub_text(payload)
    if not suspicious:
        # Ohne Fund darf nur Leerraum normalisiert worden sein.
        assert cleaned.split() == payload.split()


@_SLOW
@given(UNTRUSTED)
def test_wp7_closes_the_wp5_layer_gap(payload: str) -> None:
    """Die ADR-033-Schichtgrenze ist kein Loch: WP7 fängt, was WP5 durchlässt.

    WP5 normalisiert bewusst nicht nach NFKC und lässt nackte Domains/IPs stehen. Genau
    diese Reste müssen den Output-Sanitizer passieren und dort entschärft werden — geprüft
    wird also die Kette, nicht die Einzelschicht.
    """
    from_model, _ = scrub_text(payload)
    assert_output_safe(final_guard(scrub_field(from_model)))


# --- (e) Markdown- und Strukturklassen aus dem Cold-Test (CT-7/CT-7a/CT-8) --------------

#: Bausteine, die genau die im Cold-Test übrig gebliebenen Konstrukte erzeugen. Sie fehlen
#: in :data:`_PIECES`, weil dort kein Zeilenanfangs-Kontext entsteht — und ohne
#: Zeilenanfang rendert Discord weder Überschrift noch Liste noch Subtext.
_MARKDOWN_PIECES = [
    "\n", " ", "\t", "#", "##", "###", "-#", "-", "+", ">", ">>>", "1.", "12)",
    "_", "__", "___", "@everyone", "@here", "⚠️", "📧", "📎", "🔍", "🗂",
    "Von:", "Betreff:", "Hinweise:", "Stufe:", "a", "Text", "boese.example",
]

MARKDOWN_ATTACK = st.lists(st.sampled_from(_MARKDOWN_PIECES), min_size=1, max_size=30).map(
    "".join
)


@_SLOW
@given(MARKDOWN_ATTACK)
def test_no_rendered_markdown_survives_the_field_scrub(payload: str) -> None:
    """CT-7: ∀ Eingabe — kein Zeilenanfangs-Markdown übersteht `scrub_field` (F-SEC-3)."""
    result = final_guard(scrub_field(payload))
    assert not _RE_LINE_MARKUP.search(result), f"Zeilen-Markdown in {result!r}"
    assert_output_safe(result)


@pytest.mark.parametrize(
    "payload",
    ["1.1.1.1.1.1.1.", "1.1.1.1.1", "1.2.3.4.5.6.7.8", "1.2.3.4.5."],
)
def test_s4_punktkette_wird_ganz_gebrochen(payload: str) -> None:
    """S-4: hypothesis-Gegenbeispiel ``1.1.1.1.1.1.1.`` aus dem Property-Test darüber.

    `_RE_IPV4` verlangte **genau** vier Oktette; in einer längeren Kette scheiterte das
    Fenster am Anfang am Lookahead (fünftes Oktett) und erst das letzte Fenster passte —
    ``1.1.1.1[.]1[.]1[.]1.`` ließ die ersten vier Oktette leben, und das Orakel fand
    ``1.1.1.1`` vor ``[``. Seit ``{3,}`` bricht die Ersetzung jeden Punkt der Kette. Festgehalten
    als explizites Beispiel, weil die Beispiel-Datenbank von hypothesis ein lokaler Cache ist
    (ADR-058) und der Fall sonst auf einer anderen Maschine wieder Zufall wäre.
    """
    result = final_guard(scrub_field(payload))
    assert "." not in result.replace("[.]", "").rstrip("."), result
    assert not _RE_LIVE_IPV4.search(result), f"lebende IPv4 in {result!r}"
    assert_output_safe(result)


@_SLOW
@given(MARKDOWN_ATTACK)
def test_no_rendered_markdown_survives_the_plain_scrub(payload: str) -> None:
    """Dasselbe für Nicht-Fließtext-Felder (Anzeigename, Domain, Dateiname)."""
    result = final_guard(scrub_plain(payload))
    assert not _RE_LINE_MARKUP.search(result), f"Zeilen-Markdown in {result!r}"
    assert_output_safe(result)


@_SLOW
@given(MARKDOWN_ATTACK)
def test_model_text_can_never_forge_a_structure_line(payload: str) -> None:
    """CT-8: ∀ Modelltext — keine Zeile beginnt wie eine Zeile des Nachrichtenformats.

    Vertrauenswürdige Zeilen (`📧`, `Von:`, `🔍 Hinweise:`, `⚠️`) erzeugt allein der
    Composer; sonst ist der einzige Warnkanal des Produkts vom Angreifer beschreibbar.
    """
    assert not _RE_STRUCTURE_LINE.search(scrub_field(payload))
    assert not _RE_STRUCTURE_LINE.search(scrub_plain(payload))


@_SLOW
@given(MARKDOWN_ATTACK, MARKDOWN_ATTACK, MARKDOWN_ATTACK)
def test_composed_message_keeps_exactly_the_composer_structure(
    headline: str, body: str, reason: str
) -> None:
    """Die **fertige** Nachricht trägt genau eine 📧- und eine Von-Zeile (CT-8).

    Geprüft wird der echte Weg über `DigestComposer.compose`, nicht nur der Feld-Scrub.
    """
    mail = SanitizedMail(
        dedupe_key="k",
        from_display=headline,
        from_domain="absender.example",
        subject=body,
        body_text=body,
        sanitization_report=SanitizationReport(),
    )
    summary = Summary(headline=headline[:100], summary_text=body, importance="normal")
    verdict = CriticVerdict(phishing_risk="low", risk_reasons=[reason], summary_accurate=True)
    text = "\n".join(DigestComposer().compose(mail, summary, verdict).parts)
    lines = text.split("\n")
    assert sum(1 for line in lines if line.startswith("📧 ")) == 1
    assert sum(1 for line in lines if line.startswith("From: ")) == 1
    assert sum(1 for line in lines if line.startswith("🔍 Notes: ")) <= 1
    assert not any(line.startswith("⚠️") for line in lines)


def test_ht13_ketten_von_zeilen_markdown_ueberleben_nicht() -> None:
    """HT-13: Jede Runde entfernt nur einen Marker — drei reichten nicht.

    Gefunden von der Property unten mit `⚠️# # #`: Das Struktur-Emoji verschiebt den
    Zeilenanfang um eine Runde, danach blieb bei drei Runden ein `#` stehen.
    """
    from maildigest.output.sanitizer import scrub_plain

    for payload in ("⚠️# # #", "# " * 10 + "x", "⚠️" * 5 + "# " * 5, "> > > > zitat"):
        result = scrub_plain(payload)
        for line in result.splitlines():
            assert not line.lstrip().startswith(("#", ">", "-#")), (
                f"{payload!r} hinterließ {line!r}"
            )
