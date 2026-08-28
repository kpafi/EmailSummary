"""Unit-Tests für `sanitize/links.py` (I3, T3/T12)."""

from __future__ import annotations

import re

from maildigest.sanitize.links import LinkCollector

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
        assert "Punycode" in result
        assert collector.punycode_domains
        assert "xn--pypal-4ve[.]com" in collector.punycode_domains[0]
        assert "Unicode:" in collector.punycode_domains[0]

    def test_mixed_script_wird_gekennzeichnet(self) -> None:
        result, collector = scrubbed("http://pаypal.com/login")  # kyrillisches а
        assert "gemischte Schriftsysteme" in result
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
