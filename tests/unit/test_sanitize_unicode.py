"""Unit-Tests für `sanitize/unicode_clean.py` (F-SEC-10, T2/T12)."""

from __future__ import annotations

from maildigest.sanitize.unicode_clean import clean_text, is_mixed_script_domain


class TestCleanText:
    def test_entfernt_zero_width_zeichen_und_zaehlt(self) -> None:
        text = "ig​nore‌ inst​ructions"
        cleaned, removed = clean_text(text)
        assert cleaned == "ignore instructions"
        assert removed == 3

    def test_entfernt_bidi_steuerzeichen(self) -> None:
        text = "Foto‮gpj.exe‬ und ⁦iso⁩ dazu ‎‏"
        cleaned, removed = clean_text(text)
        assert cleaned == "Fotogpj.exe und iso dazu "
        assert removed == 6

    def test_entfernt_bom_und_soft_hyphen(self) -> None:
        cleaned, removed = clean_text("﻿Tren­nung")
        assert cleaned == "Trennung"
        assert removed == 2

    def test_nfkc_normalisierung(self) -> None:
        # Ligatur ﬁ und Vollbreite-Ziffern werden auf ASCII normalisiert.
        cleaned, _ = clean_text("ﬁnden １２３")
        assert cleaned == "finden 123"

    def test_tab_und_newline_bleiben_erhalten(self) -> None:
        cleaned, removed = clean_text("a\tb\nc")
        assert cleaned == "a\tb\nc"
        assert removed == 0

    def test_crlf_wird_normalisiert(self) -> None:
        cleaned, removed = clean_text("a\r\nb\rc")
        assert cleaned == "a\nb\nc"
        assert removed == 0

    def test_nul_und_steuerbytes_werden_entfernt(self) -> None:
        cleaned, removed = clean_text("a\x00b\x1bc\x07d")
        assert cleaned == "abcd"
        assert removed == 3

    def test_leerer_text(self) -> None:
        assert clean_text("") == ("", 0)

    def test_ergebnis_enthaelt_nie_kategorie_c(self) -> None:
        hostile = "".join(chr(cp) for cp in range(0x2000, 0x2070)) + "​﻿"
        cleaned, _ = clean_text(hostile)
        import unicodedata

        assert not any(
            unicodedata.category(char).startswith("C")
            for char in cleaned
            if char not in ("\t", "\n")
        )


class TestMixedScript:
    def test_homoglyphen_domain_wird_erkannt(self) -> None:
        assert is_mixed_script_domain("pаypal.com") is True  # kyrillisches а

    def test_reines_latein_ist_kein_treffer(self) -> None:
        assert is_mixed_script_domain("paypal.com") is False

    def test_pro_label_einheitlich_ist_kein_treffer(self) -> None:
        # Ein rein kyrillisches Label neben einem lateinischen Label ist kein Mix
        # innerhalb eines Labels.
        assert is_mixed_script_domain("почта.example") is False

    def test_ziffern_und_bindestriche_sind_neutral(self) -> None:
        assert is_mixed_script_domain("shop-24.example") is False

    def test_leere_domain(self) -> None:
        assert is_mixed_script_domain("") is False
