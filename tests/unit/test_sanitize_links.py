"""Unit-Tests für `sanitize/links.py` (I3, T3/T12)."""

from __future__ import annotations

import re
import time

from maildigest.sanitize.links import (
    CAPPED_MARKER,
    MAX_LINKS_PER_MAIL,
    LinkCollector,
)

#: Muster, die nach dem Scrub in keinem Text mehr vorkommen dürfen.
_FORBIDDEN = (
    re.compile(r"https?\s*:", re.IGNORECASE),
    re.compile(r"hxxp", re.IGNORECASE),
    re.compile(r"://"),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"mailto\s*:", re.IGNORECASE),
    re.compile(r"\(\s*\.\s*\)"),
    re.compile(r"\[\s*\.\s*\]"),
)


def scrubbed(text: str) -> tuple[str, LinkCollector]:
    collector = LinkCollector()
    return collector.scrub(text), collector


def assert_no_url(text: str) -> None:
    for pattern in _FORBIDDEN:
        assert pattern.search(text) is None, f"Muster {pattern.pattern!r} überlebt: {text!r}"


class TestErkennung:
    def test_normale_http_url(self) -> None:
        result, collector = scrubbed("Siehe https://example.com/pfad?x=1 bitte.")
        assert result == "Siehe [Link #1: example.com] bitte."
        assert collector.links_found == ["#1: hxxps[:]//example[.]com/pfad?x=1"]
        assert_no_url(result)

    def test_hxxp_defang_wird_erkannt(self) -> None:
        result, collector = scrubbed("hxxps://boese(.)seite(.)example/login")
        assert "[Link #1: boese.seite.example]" in result
        assert collector.links_found == ["#1: hxxps[:]//boese[.]seite[.]example/login"]
        assert_no_url(result)

    def test_leerzeichen_einschub(self) -> None:
        result, _ = scrubbed("h t t p s : / / evil . example . org / a")
        assert "[Link #1: evil.example.org]" in result
        assert_no_url(result)

    def test_url_encoding_im_schema(self) -> None:
        result, collector = scrubbed("%68ttp://encoded.example/x")
        assert "[Link #1: encoded.example]" in result
        assert collector.links_found[0].startswith("#1: hxxp[:]//encoded[.]example")
        assert_no_url(result)

    def test_eckige_klammer_punkte(self) -> None:
        result, _ = scrubbed("kontakt[.]beispiel[dot]example")
        assert "[Link #1: kontakt.beispiel.example]" in result
        assert_no_url(result)

    def test_www_ohne_schema(self) -> None:
        result, collector = scrubbed("Besuche www.portal.example/start jetzt")
        assert "[Link #1: portal.example]" in result  # www. würde autoverlinkt → gestrippt
        assert collector.links_found == ["#1: www[.]portal[.]example/start"]
        assert_no_url(result)

    def test_nackte_domain(self) -> None:
        result, _ = scrubbed("Melde dich auf phishing-seite.example an.")
        assert "[Link #1: phishing-seite.example]" in result
        assert result.endswith(" an.")

    def test_mailto_und_tel(self) -> None:
        result, collector = scrubbed("mailto:chef@firma.example oder tel:+49 30 555 01 00")
        assert "[Mail #1: firma.example]" in result
        assert "[Tel #2]" in result
        assert collector.links_found[0] == "#1: mailto[:]chef@firma[.]example"
        assert collector.links_found[1].startswith("#2: tel[:]+49")
        assert_no_url(result)

    def test_userinfo_trick_liefert_echte_domain(self) -> None:
        # `http://vertrauenswuerdig@evil/` — die Domain im Marker muss die echte sein.
        result, _ = scrubbed("https://paypal.com@evil.example/login")
        assert "[Link #1: evil.example]" in result
        assert_no_url(result)

    def test_ftp_url(self) -> None:
        result, _ = scrubbed("ftp://dateien.example/download")
        assert "[Link #1: dateien.example]" in result
        assert_no_url(result)


class TestFehlalarmVermeidung:
    def test_dateinamen_bleiben_text(self) -> None:
        text = "Die Dateien backup.zip, skript.js und rechnung.pdf sind angehaengt."
        result, collector = scrubbed(text)
        assert result == text
        assert collector.links_removed == 0

    def test_versionsnummern_bleiben_text(self) -> None:
        text = "Version 1.2.3 ist da."
        result, _ = scrubbed(text)
        assert result == text

    def test_abkuerzungen_bleiben_text(self) -> None:
        text = "Das ist z.B. gut und i.d.R. korrekt."
        result, _ = scrubbed(text)
        assert result == text

    def test_email_adresse_im_text_wird_nicht_als_domain_markiert(self) -> None:
        # Bare-Domain-Pass darf nicht mitten in einer Adresse zuschlagen.
        result, _ = scrubbed("Antwort an support@firma.example bitte.")
        assert "@" in result


class TestKennzeichnung:
    def test_punycode_wird_gekennzeichnet(self) -> None:
        result, collector = scrubbed("https://xn--pypal-4ve.com/secure")
        assert "punycode" in result
        assert collector.punycode_domains
        assert "xn--pypal-4ve[.]com" in collector.punycode_domains[0]
        assert "Unicode:" in collector.punycode_domains[0]

    def test_mixed_script_wird_gekennzeichnet(self) -> None:
        result, collector = scrubbed("http://pаypal.com/login")  # kyrillisches а
        assert "mixed writing systems" in result
        assert collector.mixed_script_domains == ["pаypal[.]com"]

    def test_nummerierung_laeuft_ueber_aufrufe_weiter(self) -> None:
        collector = LinkCollector()
        first = collector.scrub("https://eins.example/a")
        second = collector.scrub("https://zwei.example/b")
        assert "[Link #1: eins.example]" in first
        assert "[Link #2: zwei.example]" in second
        assert collector.links_removed == 2

    def test_links_found_ist_begrenzt(self) -> None:
        collector = LinkCollector()
        text = "\n".join(f"https://host-{i}.example/x" for i in range(150))
        result = collector.scrub(text)
        assert collector.links_removed == 150
        assert len(collector.links_found) == 100
        assert_no_url(result)

    def test_marker_wird_nicht_erneut_ersetzt(self) -> None:
        # Der eingesetzte Marker enthält eine Domain — sie darf im selben Lauf nicht
        # noch einmal durch einen weiteren Marker ersetzt werden.
        result, collector = scrubbed("https://example.com/x")
        assert result == "[Link #1: example.com]"
        assert collector.links_removed == 1

    def test_hc8_ein_pass_schneidet_nie_in_einen_platzhalter(self) -> None:
        """HC-8: Kein U+0000 überlebt den Scrub, und jeder Marker bleibt zugeordnet.

        `_RE_MAILTO` deckelt bei 128 Zeichen und schnitt deshalb mitten in den
        ``\\x00<n>\\x00``-Platzhalter, den der `http`-Pass vorher gesetzt hatte. Die
        Rück-Ersetzung fand ihr Token nicht mehr: Ein rohes Steuerzeichen erreichte die
        zugestellte Nachricht, und der Marker zum `mailto`-Fund ging verloren.
        """
        collector = LinkCollector()
        result = collector.scrub("mailto:" + "a" * 127 + "http://boese.example")
        assert "\x00" not in result
        assert collector.links_removed == 2
        assert "[Link #1: boese.example]" in result
        assert "[Mail #2:" in result

    def test_hc8_lange_wiederholungslaeufe_lassen_kein_steuerzeichen_zurueck(self) -> None:
        """Dieselbe Zusage über eine Reihe langer Wiederholungsläufe (≥ 128 Zeichen)."""
        for length in (120, 127, 128, 129, 200, 400):
            collector = LinkCollector()
            payload = "mailto:" + "a" * length + "http://boese.example"
            assert "\x00" not in collector.scrub(payload), f"Länge {length}"

    def test_hc7_markup_faellt_aus_der_defangten_form(self) -> None:
        """HC-7: Die defangte Form trägt kein Messenger-Markup mehr — `[`/`]` bleiben."""
        collector = LinkCollector()
        collector.scrub("https://ok.example/?b=*fett*&c=||spoiler||&d=~~weg~~&e=`code`")
        entry = collector.links_found[0]
        assert not set("`*|~\\") & set(entry), entry
        assert entry.startswith("#1: hxxps[:]//ok[.]example")


# --- R-5: Link-Budget je Mail und lineare Rück-Ersetzung (HC2-1-Rest) --------------------
#
# `scrub` war quadratisch in der Zahl der Funde (ein Voll-Scan je Platzhalter): 8 000 Links
# 1,2 s, 16 000 Links 4,4 s, 32 000 Links 17,6 s. Eine 1-MB-Mail innerhalb aller Schranken
# aus ADR-084 kostete damit 26,8 s — die 10-s-Zusage aus SPEC-CLI §5 war gebrochen.


class TestR5LinkBudget:
    """Budget je Mail (`MAX_LINKS_PER_MAIL`) und Laufzeit der Rück-Ersetzung."""

    def test_r5_jenseits_des_budgets_ueberlebt_keine_url(self) -> None:
        """Gekappt heisst entfernt, nicht durchgelassen (I3)."""
        anzahl = MAX_LINKS_PER_MAIL + 500
        text = " ".join(f"http://x{i}.example/a" for i in range(anzahl))
        result, collector = scrubbed(text)

        assert collector.links_removed == anzahl
        assert collector.links_capped is True
        assert result.count(CAPPED_MARKER) == 500
        assert len(collector.links_found) == 100  # Fußnote bleibt bei ihrer Obergrenze
        assert_no_url(result)
        assert "x2400.example" not in result  # kein Host jenseits des Budgets

    def test_r5_bis_zum_budget_bleibt_alles_wie_bisher(self) -> None:
        """Gegenprobe: Unterhalb des Budgets ändert sich kein Zeichen."""
        text = " ".join(f"http://x{i}.example/a" for i in range(50))
        result, collector = scrubbed(text)
        assert CAPPED_MARKER not in result
        assert collector.links_capped is False
        assert result.startswith("[Link #1: x0.example]")

    def test_r5_budget_zaehlt_ueber_alle_teile_einer_mail(self) -> None:
        """Ein Collector je Mail: Der zweite Text erbt das verbrauchte Budget."""
        collector = LinkCollector()
        collector.scrub(" ".join(f"http://x{i}.example" for i in range(MAX_LINKS_PER_MAIL)))
        zweiter = collector.scrub("http://spaet.example/a")
        assert zweiter == CAPPED_MARKER
        assert collector.links_capped is True

    def test_r5_rueck_ersetzung_ist_linear(self) -> None:
        """32 000 Funde in Bruchteilen einer Sekunde (vorher 17,6 s).

        Die Schranke ist bewusst grosszügig gegen Maschinenlast gesetzt; die Aussage des
        Befunds (quadratisch, zweistellige Sekunden) ist mit jedem Wert unter einer
        Sekunde erledigt.
        """
        text = " ".join(f"http://x{i}.example/a" for i in range(32_000))
        start = time.perf_counter()
        result, collector = scrubbed(text)
        dauer = time.perf_counter() - start
        assert collector.links_removed == 32_000
        assert dauer < 2.0, f"Scrub von 32 000 Links dauerte {dauer:.1f} s"
        assert_no_url(result)

    def test_r5_kein_platzhalter_ueberlebt_die_kappung(self) -> None:
        """Auch gekappte Funde durchlaufen die Rück-Ersetzung sauber (HC-8 bleibt gültig)."""
        text = " ".join(f"http://x{i}.example/a" for i in range(MAX_LINKS_PER_MAIL + 10))
        result, _collector = scrubbed(text)
        assert "\x00" not in result
