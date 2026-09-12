"""Unit-Tests für `build_raw_mail` und `backoff_delay` (WP2).

Getestet wird gegen echte `imap_tools.MailMessage`-Objekte, die aus rohen `.eml`-Bytes
gebaut werden — also gegen genau das Objekt, das der Server liefert, ohne Netzwerk
(Begründung des Mock-Ansatzes: WP2-ADR in docs/DECISIONS.md).

Schwerpunkte: vollständige Feldbefüllung nach docs/ARCHITECTURE.md §3, Fallback-Dedupe-Key
bei fehlender Message-ID, Grenzfälle mit kaputten Headern, Backoff-Kennlinie.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime

import pytest
from imap_tools import MailMessage

from maildigest.ingest.imap_client import (
    MAX_BACKOFF_SECONDS,
    backoff_delay,
    build_raw_mail,
)
from maildigest.models import CriticVerdict, Summary
from maildigest.sanitize import MailSanitizer

FULL_MAIL = b"""\
Return-Path: <bounce@Mailer.Example.NET>
Authentication-Results: mx.mirror.example; spf=pass smtp.mailfrom=stadtwerke-x.de
Authentication-Results: mx.mirror.example; dkim=fail header.d=stadtwerke-x.de
Message-ID: <2026-invoice-42@Stadtwerke-X.DE>
From: "Stadtwerke X" <rechnung@Stadtwerke-X.DE>
Reply-To: antwort@anderes-ziel.example
To: mirror@example.org, zweite@example.org
Subject: Rechnung Maerz
Date: Fri, 28 Aug 2026 14:12:03 +0200
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Guten Tag, anbei Ihre Rechnung.
"""


def make_message(raw: bytes, *, uid: str | None = "42", size: int | None = None) -> MailMessage:
    """Baut eine `MailMessage` wie sie aus einem FETCH käme (inkl. UID/Größe)."""
    if uid is None:
        return MailMessage.from_bytes(raw)
    size_part = f" RFC822.SIZE {size}" if size is not None else ""
    return MailMessage([(f"1 (UID {uid}{size_part} FLAGS ())".encode(), raw), b")"])


# --- Vollständige Feldbefüllung -------------------------------------------------------------


def test_all_fields_are_populated() -> None:
    raw = build_raw_mail(make_message(FULL_MAIL, size=len(FULL_MAIL)))

    assert raw.message_id == "<2026-invoice-42@Stadtwerke-X.DE>"
    assert raw.dedupe_key == raw.message_id
    assert raw.from_addr == '"Stadtwerke X" <rechnung@Stadtwerke-X.DE>'
    assert raw.from_domain == "stadtwerke-x.de"
    assert raw.reply_to == "antwort@anderes-ziel.example"
    assert raw.return_path_domain == "mailer.example.net"
    assert raw.to_addrs == ["mirror@example.org", "zweite@example.org"]
    assert raw.subject_raw == "Rechnung Maerz"
    assert raw.date == datetime.fromisoformat("2026-08-28T14:12:03+02:00")
    assert raw.size_bytes == len(FULL_MAIL)
    assert raw.mime_bytes.startswith(b"Return-Path:")


def test_from_domain_is_lowercase() -> None:
    """docs/ARCHITECTURE.md §3: `from_domain` ist immer lowercase."""
    raw = build_raw_mail(make_message(FULL_MAIL))
    assert raw.from_domain == raw.from_domain.lower()


def test_all_auth_results_headers_are_kept() -> None:
    """Mehrere `Authentication-Results` werden gesammelt (Faktenbasis für F-CRIT-3)."""
    raw = build_raw_mail(make_message(FULL_MAIL))
    assert raw.auth_results_header is not None
    assert "spf=pass" in raw.auth_results_header
    assert "dkim=fail" in raw.auth_results_header
    assert raw.auth_results_header.count("\n") == 1


def test_size_falls_back_to_byte_length_without_server_size() -> None:
    raw = build_raw_mail(make_message(FULL_MAIL, uid=None))
    assert raw.size_bytes == len(raw.mime_bytes)
    assert raw.size_bytes > 0


def test_subject_is_rfc2047_decoded() -> None:
    mail = FULL_MAIL.replace(
        b"Subject: Rechnung Maerz", b"Subject: =?utf-8?q?Rechnung_M=C3=A4rz?="
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.subject_raw == "Rechnung März"


def test_folded_header_is_normalised() -> None:
    mail = FULL_MAIL.replace(
        b"Subject: Rechnung Maerz", b"Subject: Rechnung\r\n  fuer den Monat Maerz"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.subject_raw == "Rechnung fuer den Monat Maerz"


# --- HC-23: Anzeigename wird RFC-2047-dekodiert ----------------------------------------------


def test_hc23_from_display_name_is_rfc2047_decoded() -> None:
    """Der Anzeigename trägt dieselbe Transport-Kodierung wie der Betreff (HC-23)."""
    mail = FULL_MAIL.replace(
        b'From: "Stadtwerke X" <rechnung@Stadtwerke-X.DE>',
        b"From: =?utf-8?Q?J=C3=B6rg_M=C3=BCller?= <j@b.example>",
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_addr == "Jörg Müller <j@b.example>"
    assert raw.from_domain == "b.example"


def test_hc23_reply_to_display_name_is_decoded_too() -> None:
    mail = FULL_MAIL.replace(
        b"Reply-To: antwort@anderes-ziel.example",
        b"Reply-To: =?utf-8?Q?B=C3=BCro?= <antwort@anderes-ziel.example>",
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.reply_to == "Büro <antwort@anderes-ziel.example>"


def test_hc23_eight_bit_display_name_does_not_become_mojibake() -> None:
    """Roh-8-bittige Namen (RFC-Verstoß, in freier Wildbahn häufig) bleiben lesbar."""
    mail = FULL_MAIL.replace(
        b'From: "Stadtwerke X" <rechnung@Stadtwerke-X.DE>',
        "From: Jörg Müller <j@b.example>".encode(),
    )
    raw = build_raw_mail(make_message(mail))
    assert "Jörg Müller" in raw.from_addr
    assert "Ã" not in raw.from_addr


def test_hc23_control_chars_in_the_name_reach_the_sanitizer() -> None:
    """VS16/Cf im kodierten Namen erreichen den Sanitizer und werden dort gezählt.

    Vor dem Fix sah der Sanitizer nur die ASCII-Hülse `=?utf-8?…?=`; die in
    docs/SECURITY.md §4 zugesagte Steuerzeichen-Prüfung lief damit ins Leere.
    """
    encoded = base64.b64encode("Bank️​X".encode()).decode()
    mail = FULL_MAIL.replace(
        b'From: "Stadtwerke X" <rechnung@Stadtwerke-X.DE>',
        f"From: =?utf-8?B?{encoded}?= <b@b.example>".encode(),
    )
    raw = build_raw_mail(make_message(mail))
    assert "️" in raw.from_addr

    sanitized = MailSanitizer().sanitize(raw)
    assert sanitized.sanitization_report.control_chars_removed >= 1
    assert "​" not in sanitized.from_display  # Zero-Width-Zeichen ist raus


def test_hc23_broken_encoding_falls_back_to_the_raw_value() -> None:
    """ADR-020 (e): `build_raw_mail` wirft nie — auch nicht bei kaputter Kodierung."""
    mail = FULL_MAIL.replace(
        b'From: "Stadtwerke X" <rechnung@Stadtwerke-X.DE>',
        b"From: =?utf-8?B?%%%nicht-base64%%%?= <k@b.example>",
    )
    raw = build_raw_mail(make_message(mail))
    assert isinstance(raw.from_addr, str)
    assert raw.from_domain == "b.example"


# --- HC-10: content_hash ----------------------------------------------------------------------


def test_hc10_content_hash_is_the_sha256_of_the_mime_bytes() -> None:
    raw = build_raw_mail(make_message(FULL_MAIL))
    assert raw.content_hash == hashlib.sha256(raw.mime_bytes).hexdigest()
    assert raw.id_collision is False


def test_hc10_collision_flag_reaches_the_sanitization_report() -> None:
    """Der Hinweis überlebt die Sanitize-Stufe — sonst sähe der Nutzer nie etwas (ADR-079)."""
    raw = build_raw_mail(make_message(FULL_MAIL)).model_copy(update={"id_collision": True})
    assert MailSanitizer().sanitize(raw).sanitization_report.id_collision is True


def test_hc10_same_message_id_different_body_yields_different_content_hash() -> None:
    """Das zweite Merkmal ist genau dann gleich, wenn die Mail dieselbe ist (ADR-079)."""
    other = FULL_MAIL.replace(b"Guten Tag, anbei Ihre Rechnung.", b"Etwas ganz anderes.")
    first = build_raw_mail(make_message(FULL_MAIL))
    second = build_raw_mail(make_message(other))
    assert first.dedupe_key == second.dedupe_key
    assert first.content_hash != second.content_hash


# --- Dedupe-Key ------------------------------------------------------------------------------


def test_message_id_is_used_as_dedupe_key() -> None:
    raw = build_raw_mail(make_message(FULL_MAIL))
    assert raw.dedupe_key == "<2026-invoice-42@Stadtwerke-X.DE>"


def test_missing_message_id_yields_stable_fallback_hash() -> None:
    """Ohne Message-ID: Hash aus From + Date + Subject + Body-Präfix, deterministisch."""
    mail = FULL_MAIL.replace(b"Message-ID: <2026-invoice-42@Stadtwerke-X.DE>\n", b"")
    first = build_raw_mail(make_message(mail))
    second = build_raw_mail(make_message(mail))

    assert first.message_id is None
    assert first.dedupe_key.startswith("sha256:")
    assert first.dedupe_key == second.dedupe_key


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"From: \"Stadtwerke X\" <rechnung@Stadtwerke-X.DE>", b"From: <boese@example.invalid>"),
        (b"Date: Fri, 28 Aug 2026 14:12:03 +0200", b"Date: Sat, 29 Aug 2026 09:00:00 +0200"),
        (b"Subject: Rechnung Maerz", b"Subject: Mahnung"),
        (b"Guten Tag, anbei Ihre Rechnung.", b"Ganz anderer Text im Body."),
    ],
)
def test_fallback_hash_changes_with_each_input_field(old: bytes, new: bytes) -> None:
    base = FULL_MAIL.replace(b"Message-ID: <2026-invoice-42@Stadtwerke-X.DE>\n", b"")
    variant = base.replace(old, new)
    assert variant != base
    assert build_raw_mail(make_message(base)).dedupe_key != (
        build_raw_mail(make_message(variant)).dedupe_key
    )


def test_empty_message_id_header_counts_as_missing() -> None:
    mail = FULL_MAIL.replace(b"Message-ID: <2026-invoice-42@Stadtwerke-X.DE>", b"Message-ID:   ")
    raw = build_raw_mail(make_message(mail))
    assert raw.message_id is None
    assert raw.dedupe_key.startswith("sha256:")


def test_fallback_key_never_looks_like_a_message_id() -> None:
    """Der Fallback ist per Präfix von echten Message-IDs unterscheidbar (keine Kollision)."""
    mail = FULL_MAIL.replace(b"Message-ID: <2026-invoice-42@Stadtwerke-X.DE>\n", b"")
    key = build_raw_mail(make_message(mail)).dedupe_key
    assert not key.startswith("<")
    assert len(key) == len("sha256:") + 64


# --- Grenzfälle: fehlende und kaputte Header --------------------------------------------------


def test_minimal_mail_without_any_header() -> None:
    """Eine Mail ganz ohne Header darf nicht werfen (sonst ginge sie still verloren, I6)."""
    raw = build_raw_mail(make_message(b"\r\nnur ein body\r\n"))
    assert raw.message_id is None
    assert raw.from_addr == ""
    assert raw.from_domain == ""
    assert raw.reply_to is None
    assert raw.return_path_domain is None
    assert raw.to_addrs == []
    assert raw.subject_raw == ""
    assert raw.date is None
    assert raw.auth_results_header is None
    assert raw.dedupe_key.startswith("sha256:")


def test_unparsable_date_becomes_none() -> None:
    """`None` statt des imap-tools-Sentinels 1900-01-01."""
    mail = FULL_MAIL.replace(b"Date: Fri, 28 Aug 2026 14:12:03 +0200", b"Date: gestern abend")
    raw = build_raw_mail(make_message(mail))
    assert raw.date is None


def test_from_without_domain() -> None:
    mail = FULL_MAIL.replace(
        b'From: "Stadtwerke X" <rechnung@Stadtwerke-X.DE>', b"From: Postmaster"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == ""


def test_broken_rfc2047_subject_does_not_raise() -> None:
    mail = FULL_MAIL.replace(b"Subject: Rechnung Maerz", b"Subject: =?utf-8?B?%%%nicht-base64%%%?=")
    raw = build_raw_mail(make_message(mail))
    assert isinstance(raw.subject_raw, str)


def test_eight_bit_header_bytes_do_not_raise() -> None:
    mail = FULL_MAIL.replace(b"Subject: Rechnung Maerz", b"Subject: Rechnung M\xe4rz")
    raw = build_raw_mail(make_message(mail))
    assert isinstance(raw.subject_raw, str)
    assert raw.mime_bytes


def test_html_only_body_is_usable_for_the_fallback_hash() -> None:
    mail = (
        b"From: <a@example.org>\r\n"
        b"Subject: nur html\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b"\r\n"
        b"<html><body>Hallo</body></html>\r\n"
    )
    other = mail.replace(b"Hallo", b"Tschuess")
    assert build_raw_mail(make_message(mail)).dedupe_key != (
        build_raw_mail(make_message(other)).dedupe_key
    )


def test_empty_message_is_survivable() -> None:
    raw = build_raw_mail(make_message(b""))
    assert raw.dedupe_key.startswith("sha256:")
    assert raw.size_bytes >= 0


# --- HC2-2: Adresse aus dem Rohheader, nur der Name wird dekodiert ---------------------


def _address_in_raw_header(mail: bytes, header: str) -> str:
    """Orakel: die Adresse, wie sie **im Rohheader** steht — ohne Produktionscode.

    Bewusst strikt grosszügiger als die Implementierung (Regel 5 aus PLAN-FIXRUNDE §2):
    RFC-2047-kodierte Wörter sind per Definition Anzeigename und werden zuerst entfernt;
    was danach an Adressen übrig bleibt, ist die Adresse des Headers.
    """
    text = mail.decode("latin-1")
    line = next(
        row for row in text.splitlines() if row.lower().startswith(header.lower() + ":")
    )
    value = re.sub(r"=\?[^?]*\?[bBqQ]\?[^?]*\?=", "", line.split(":", 1)[1])
    return re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", value)[-1].lower()


def _encoded(name: str) -> bytes:
    """`name` als RFC-2047-Base64-Wort (die Form aus dem Befund)."""
    return b"=?utf-8?B?" + base64.b64encode(name.encode()) + b"?="


def _attack_mail(from_header: bytes, *, reply_to: bytes = b"") -> bytes:
    """Mail mit `Return-Path: billing@bank.example` und wählbarem From/Reply-To."""
    return (
        b"Return-Path: <billing@bank.example>\r\n"
        + from_header
        + b"\r\n"
        + reply_to
        + b"Subject: Ihre Rechnung\r\n"
        b"Date: Tue, 01 Sep 2026 10:00:00 +0200\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        b"Guten Tag.\r\n"
    )


def test_hc2_2_kodierter_name_mit_adresse_bestimmt_die_domain_nicht() -> None:
    """Der Anzeigename darf die Absender-Domain nicht ersetzen (Regression aus HC-23).

    Vor dem Fix dekodierte `_display_header` den **ganzen** Headerwert; die im Namen
    versteckte Adresse stand danach als erste in der Adressliste und gewann.
    """
    mail = _attack_mail(
        b"From: " + _encoded("Bank <info@bank.example>,") + b" <attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))

    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"
    report = MailSanitizer().sanitize(raw).sanitization_report
    assert report.return_path_mismatch is True


def test_hc2_2_zustellzeile_zeigt_die_echte_domain() -> None:
    """Die Absenderzeile ist das zentrale Anti-Phishing-Signal — sie zeigt `evil`."""
    from maildigest.output.composer import DigestComposer

    mail = _attack_mail(
        b"From: " + _encoded("Bank <info@bank.example>,") + b" <attacker@evil.example>"
    )
    sanitized = MailSanitizer().sanitize(build_raw_mail(make_message(mail)))
    text = "\n".join(
        DigestComposer()
        .compose(
            sanitized,
            Summary(
                headline="Rechnung",
                summary_text="Eine Rechnung.",
                importance="normal",
                category="other",
            ),
            CriticVerdict(phishing_risk="low"),
        )
        .parts
    )
    from_line = next(row for row in text.split("\n") if row.startswith("From: "))
    assert "evil[.]example" in from_line
    assert "(bank[.]example)" not in from_line


def test_hc2_2_nackte_adresse_als_name_loescht_die_domain_nicht() -> None:
    """Variante des Befunds: `from_domain` wurde leer, die Domain verschwand ganz."""
    mail = _attack_mail(
        b"From: " + _encoded("info@bank.example") + b" <attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == "evil.example"


def test_hc2_2_pseudotag_im_namen_laesst_die_domain_stehen() -> None:
    """Variante des Befunds: `Support <b>x</b>` liess `from_domain` leer werden."""
    mail = _attack_mail(
        b"From: " + _encoded("Support <b>x</b>") + b" <attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == "evil.example"
    # Der Name ist von Struktursymbolen befreit; die Winkelklammern im Ergebnis sind die
    # der echten Adresse.
    assert raw.from_addr == "Support b x /b <attacker@evil.example>"


def test_hc2_2_reply_to_indikator_feuert_wieder() -> None:
    """Auch `Reply-To` trägt einen Anzeigenamen — derselbe Angriff, dasselbe Signal."""
    mail = _attack_mail(
        b"From: " + _encoded("Bank") + b" <attacker@evil.example>",
        reply_to=b"Reply-To: "
        + _encoded("Bank <info@bank.example>,")
        + b" <collect@evil2.example>\r\n",
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == "evil.example"
    assert _address_in_raw_header(mail, "Reply-To") == "collect@evil2.example"
    assert "collect@evil2.example" in (raw.reply_to or "")
    assert MailSanitizer().sanitize(raw).sanitization_report.reply_to_mismatch is True


def test_hc2_2_kontrollmail_ohne_kodierung_bleibt_unveraendert() -> None:
    """Gegenprobe: Ohne Kodierung wird der Rohwert unverändert übernommen."""
    mail = _attack_mail(b"From: Bank <info@bank.example>")
    raw = build_raw_mail(make_message(mail))
    assert raw.from_addr == "Bank <info@bank.example>"
    assert raw.from_domain == "bank.example"
    report = MailSanitizer().sanitize(raw).sanitization_report
    assert report.return_path_mismatch is False
    assert report.reply_to_mismatch is False


def test_hc2_2_hc23_bleibt_behoben() -> None:
    """Der HC-23-Fall selbst bleibt korrekt dekodiert — kein Rückbau auf die ASCII-Hülse."""
    mail = _attack_mail(
        b"From: =?utf-8?Q?J=C3=B6rg_M=C3=BCller?= <j@b.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_addr == "Jörg Müller <j@b.example>"
    assert raw.from_domain == "b.example"


# --- HC2-2 (zweite Iteration): kodierte Wörter werden vor dem Adress-Parse maskiert ----
#
# Die Q-Kodierung darf `@`, `<`, `>` und `,` literal führen; `getaddresses` liest das auf
# dem Rohwert als Adresssyntax. Das Base64-Alphabet kann diese Zeichen nicht enthalten —
# die Tests oben sehen den Fall deshalb nicht.


def test_hc2_2_q_wort_mit_at_und_komma_bestimmt_die_domain_nicht() -> None:
    """`=?utf-8?Q?info@bank.example,?=` lieferte `from_domain='bank.example'`."""
    mail = _attack_mail(
        b"From: =?utf-8?Q?info@bank.example,?= <attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))

    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"
    report = MailSanitizer().sanitize(raw).sanitization_report
    assert report.return_path_mismatch is True


def test_hc2_2_q_wort_ohne_komma_loescht_die_domain_nicht() -> None:
    """Zweite Wirkung desselben Befunds: ohne Komma verschwand die Domain ganz."""
    mail = _attack_mail(b"From: =?utf-8?Q?info@bank.example?= <attacker@evil.example>")
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"


def test_hc2_2_q_wort_mit_winkelklammern() -> None:
    """Auch `<` und `>` darf ein Q-Wort literal tragen."""
    mail = _attack_mail(
        b"From: =?utf-8?Q?<info@bank.example>?= <attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"


def test_hc2_2_b_wort_mit_adresse_als_klartext() -> None:
    """Gegenprobe im neuen Weg: Das Base64-Wort bleibt genauso wirkungslos."""
    mail = _attack_mail(
        b"From: " + _encoded("Bank <info@bank.example>,") + b" <attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"
    assert "Bank" in raw.from_addr  # der Name bleibt lesbar


def test_hc2_2_kodiertes_wort_direkt_vor_der_adresse() -> None:
    """Ohne Leerzeichen vor `<` — der Platzhalter darf die Adressgrenze nicht verschieben."""
    mail = _attack_mail(
        b"From: =?utf-8?Q?info@bank.example,?=<attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"


def test_hc2_2_zwei_kodierte_woerter() -> None:
    """Zwei Wörter, das zweite trägt die eingeschleuste Adresse (`=3C`/`=3E`)."""
    mail = _attack_mail(
        b"From: =?utf-8?Q?Bank=20Support?= =?utf-8?Q?_=3Cinfo@bank.example=3E,?= "
        b"<attacker@evil.example>"
    )
    raw = build_raw_mail(make_message(mail))
    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"
    # Vor dem Fix stand hier die syntaktisch unmögliche Domain `bank.example=3e`.
    assert "=3e" not in raw.from_domain
    assert "Bank Support" in raw.from_addr


def test_hc2_2_q_wort_im_reply_to() -> None:
    """Derselbe Weg über `Reply-To`: der Indikator muss weiter feuern."""
    mail = _attack_mail(
        b"From: " + _encoded("Bank") + b" <attacker@evil.example>",
        reply_to=b"Reply-To: =?utf-8?Q?info@bank.example,?= <collect@evil2.example>\r\n",
    )
    raw = build_raw_mail(make_message(mail))
    assert _address_in_raw_header(mail, "Reply-To") == "collect@evil2.example"
    assert "collect@evil2.example" in (raw.reply_to or "")
    assert MailSanitizer().sanitize(raw).sanitization_report.reply_to_mismatch is True


def test_hc2_2_gewoehnlicher_kodierter_name_bleibt_lesbar() -> None:
    """Gegenprobe: Der Platzhalterschritt darf den normalen Fall nicht verändern."""
    mail = _attack_mail(b"From: " + _encoded("Jörg Müller") + b" <j@b.example>")
    raw = build_raw_mail(make_message(mail))
    assert raw.from_addr == "Jörg Müller <j@b.example>"
    assert raw.from_domain == "b.example"


# --- HC2-2 (dritte Iteration, R-4): die Maske darf nicht enger sein als der Dekoder -----
#
# `_ENCODED_WORD_RE` verlangte einen nicht-leeren Charset (`[^?]+`). `decode_header` — der
# Dekoder, der im selben Pfad danach läuft — kennt diese Einschränkung nicht: Ein einziges
# fehlendes Zeichen (`=??Q?…?=`) genügte, um an der Maske vorbei wieder Adresssyntax in den
# Anzeigenamen zu bringen. Die Maske folgt jetzt der Form von `email.header.ecre`.

#: Kodierte Formen, die `decode_header` als kodiert liest — die Maske muss sie alle sehen.
_KODIERTE_FORMEN = [
    b"=??Q?info@bank.example,?=",  # leerer Charset (der Befund)
    b"=??B?aW5mb0BiYW5rLmV4YW1wbGUsIDxhdD4=?=",  # leerer Charset, Base64
    b"=?utf-8*de?Q?info@bank.example,?=",  # Sprach-Tag am Charset
    b"=?UTF-8?q?info@bank.example,?=",  # Kleinschreibung der Kodierung
    b"=?utf-8?B?aW5mb0BiYW5rLmV4YW1wbGU=?=",  # grosses B
    b"=?us-ascii?Q?=3Cinfo@bank.example=3E,?=",  # Winkelklammern quoted-printable
    b"=?utf-8?Q?Bank?=\r\n =?utf-8?Q?_info@bank.example,?=",  # gefaltet, zwei Wörter
    b"=?utf-8?Q?info@bank.example,",  # kaputt: kein schliessendes ?=
]


@pytest.mark.parametrize("form", _KODIERTE_FORMEN)
def test_hc2_2_maskierung_ist_nicht_enger_als_der_dekoder(form: bytes) -> None:
    """Jedes Segment, das `decode_header` als kodiert liefert, ist maskiert unschädlich.

    Orakel ist der Dekoder selbst (strikt grosszügiger als die Implementierung, Regel 5):
    Was er als kodiertes Wort ausgibt, darf im maskierten Rohwert keine Adressgrenze mehr
    setzen — kein `@`, `,`, `<`, `>`, `;`, `:` aus einem solchen Segment bleibt stehen.
    """
    from email.header import decode_header

    from maildigest.ingest.imap_client import _mask_encoded_words

    rohwert = form.decode("latin-1") + " <attacker@evil.example>"
    kodiert = [
        text.decode("latin-1") if isinstance(text, bytes) else text
        for text, charset in decode_header(rohwert)
        if charset is not None
    ]
    maskiert, woerter, _stem = _mask_encoded_words(" ".join(rohwert.split()))

    if not kodiert:  # kaputte Form: der Dekoder sieht nichts Kodiertes, die Maske darf ruhen
        return
    assert woerter, f"kein kodiertes Wort maskiert, obwohl decode_header {kodiert} meldet"
    kopf = maskiert.split("<attacker@evil.example>")[0]
    assert not any(zeichen in kopf for zeichen in "@,<>;:"), kopf


@pytest.mark.parametrize(
    "from_header",
    [
        b"From: =??Q?info@bank.example,?= <attacker@evil.example>",
        b"From: =??Q?info@bank.example?= <attacker@evil.example>",
        b"From: =?utf-8*de?Q?info@bank.example,?= <attacker@evil.example>",
    ],
)
def test_hc2_2_leerer_charset_bestimmt_die_domain_nicht(from_header: bytes) -> None:
    """R-4: `=??Q?…?=` (und das Sprach-Tag) lieferten wieder `bank.example` bzw. `''`."""
    mail = _attack_mail(from_header)
    raw = build_raw_mail(make_message(mail))

    assert raw.from_domain == _address_in_raw_header(mail, "From").split("@")[1]
    assert raw.from_domain == "evil.example"
    assert MailSanitizer().sanitize(raw).sanitization_report.return_path_mismatch is True


def test_hc2_2_leerer_charset_im_reply_to() -> None:
    """R-4 über `Reply-To`: der Indikator verstummte mit."""
    mail = _attack_mail(
        b"From: =??Q?info@bank.example,?= <attacker@evil.example>",
        reply_to=b"Reply-To: =??Q?info@bank.example,?= <collect@evil2.example>\r\n",
    )
    raw = build_raw_mail(make_message(mail))
    assert _address_in_raw_header(mail, "Reply-To") == "collect@evil2.example"
    assert "collect@evil2.example" in (raw.reply_to or "")
    report = MailSanitizer().sanitize(raw).sanitization_report
    assert report.reply_to_mismatch is True
    assert report.return_path_mismatch is True


# --- Backoff ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(0, 5.0), (1, 5.0), (2, 10.0), (3, 20.0), (4, 40.0), (5, 80.0), (6, 160.0), (7, 320.0)],
)
def test_backoff_is_exponential(attempt: int, expected: float) -> None:
    assert backoff_delay(attempt) == expected


@pytest.mark.parametrize("attempt", [8, 20, 64, 1000])
def test_backoff_is_capped_at_ten_minutes(attempt: int) -> None:
    """docs/ARCHITECTURE.md §6: Reconnect-Backoff maximal 10 Minuten."""
    assert backoff_delay(attempt) == MAX_BACKOFF_SECONDS
    assert MAX_BACKOFF_SECONDS == 600.0


def test_backoff_is_monotonic() -> None:
    delays = [backoff_delay(n) for n in range(1, 30)]
    assert delays == sorted(delays)
    assert max(delays) <= MAX_BACKOFF_SECONDS


# --- NF-1, vierte Iteration: R-8 (Kopfzeilen-Obergrenze, lineare Maske) und R-9 ----------
#
# R-8 war eine Regression aus der dritten Iteration: das zu `email.header.ecre` formgleiche
# `.*?` darf über `?` hinweglaufen. Fehlt das schliessende `?=`, scannte jede der n
# Startstellen den ganzen Resttext — ein 156-KiB-`From` kostete 18,2 s CPU (vorher 0,002 s).
# Zwei Schichten: eine Obergrenze für den Rohwert **vor** jeder Verarbeitung und ein
# Scanner, der das Ende mit `str.find` sucht statt mit `.*?`.


def _kaputte_kodierte_woerter(n: int) -> str:
    """`n` angefangene kodierte Wörter ohne jedes schliessende `?=` (der Repro)."""
    return "=?a?Q?xxxx" * n


def test_r8_maskierung_ist_linear() -> None:
    """Messreihe n/2n/4n/8n: die Maskierung wächst linear, nicht quadratisch.

    Orakel ist das Wachstum selbst (strikt großzügiger als die Implementierung): Mit dem
    alten Muster kostete jede Verdopplung den vierfachen Aufwand (0,077 / 0,266 / 0,491 /
    1,940 s bei n = 1000 / 2000 / 4000 / 8000). Linear heißt hier: der Achtfache Umfang
    kostet weniger als das Achtfache plus großzügige Reserve.
    """
    import time

    from maildigest.ingest.imap_client import _mask_encoded_words

    zeiten: dict[int, float] = {}
    for n in (1000, 2000, 4000, 8000):
        text = _kaputte_kodierte_woerter(n)
        beginn = time.process_time()
        maskiert, woerter, _stem = _mask_encoded_words(text)
        zeiten[n] = time.process_time() - beginn
        assert not woerter  # kein schliessendes ?= ⇒ nichts zu maskieren
        assert maskiert == text

    # Absolut: 8000 kaputte Wörter (80 KB) dürfen keine messbare Zeit kosten.
    assert zeiten[8000] < 0.1, zeiten
    # Relativ: quadratisch wäre Faktor 64 zwischen n und 8n. Faktor 16 lässt der
    # Messungenauigkeit bei so kleinen Zeiten Luft und schliesst O(n²) sicher aus.
    if zeiten[1000] > 0.001:
        assert zeiten[8000] / zeiten[1000] < 16, zeiten


def test_r8_riesiger_from_header_kostet_keine_zeit() -> None:
    """Der Repro des Skeptikers: 156-KiB-`From` im Ingest (vorher 18,2 s CPU)."""
    import time

    kopf = _kaputte_kodierte_woerter(16_000)
    mail = _attack_mail(f"From: {kopf} <attacker@evil.example>".encode())

    beginn = time.process_time()
    raw = build_raw_mail(make_message(mail))
    dauer = time.process_time() - beginn

    assert dauer < 0.5, dauer
    # Der Deckel schneidet den Rohwert; eine Domain bleibt danach nicht übrig, und genau
    # das ist die fail-safe Richtung: unbekannte Domain neben `Return-Path` ⇒ Warnung.
    assert raw.from_domain == ""
    assert MailSanitizer().sanitize(raw).sanitization_report.return_path_mismatch is True


def test_r8_riesiger_betreff_kostet_keine_zeit() -> None:
    """Dieselbe Obergrenze für den Betreff: `decode_header` ist dort ebenso quadratisch."""
    import time

    kopf = _kaputte_kodierte_woerter(16_000)
    mail = (
        b"From: <a@b.example>\r\nSubject: "
        + kopf.encode()
        + b"\r\nDate: Tue, 01 Sep 2026 10:00:00 +0200\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\nHallo.\r\n'
    )

    beginn = time.process_time()
    raw = build_raw_mail(make_message(mail))
    dauer = time.process_time() - beginn

    assert dauer < 0.5, dauer  # vor dem Deckel: 8,6 s
    assert len(raw.subject_raw) <= 4096


def test_r8_gewoehnlicher_header_bleibt_unveraendert() -> None:
    """Gegenprobe: Unter der Obergrenze ändert sich nichts — auch nicht an der Maske."""
    raw = build_raw_mail(make_message(FULL_MAIL, size=len(FULL_MAIL)))

    assert raw.from_addr == '"Stadtwerke X" <rechnung@Stadtwerke-X.DE>'
    assert raw.from_domain == "stadtwerke-x.de"
    assert raw.subject_raw == "Rechnung Maerz"


#: R-9: ein kodiertes Wort **hinter** der Adresse liess `getaddresses` den Adressteil ganz
#: verwerfen (`[('', '')]`) — Absenderadresse und Domain verschwanden, `(unknown sender)`,
#: und beide Rückweg-Warnungen verstummten.
_LOESCH_FORMEN = [
    b'=??Q?a?= <attacker@evil.example> =??Q?info@bank.example?=',
    b"<attacker@evil.example> =??Q?info@bank.example?=",
    b"<attacker@evil.example> =?utf-8?Q?info@bank.example?=",
    b"=?utf-8?Q?Bank?= <attacker@evil.example> =?utf-8?Q?x?=",
]


@pytest.mark.parametrize("from_rest", _LOESCH_FORMEN)
def test_r9_angehaengtes_kodiertes_wort_loescht_die_domain_nicht(from_rest: bytes) -> None:
    """Das Schutzziel nennt „löschen" neben „ersetzen" — beides muss scheitern."""
    mail = _attack_mail(
        b"From: " + from_rest,
        reply_to=b"Reply-To: <collect@evil2.example>\r\n",
    )
    raw = build_raw_mail(make_message(mail))

    assert raw.from_domain == "evil.example"
    assert "attacker@evil.example" in raw.from_addr
    report = MailSanitizer().sanitize(raw).sanitization_report
    assert report.return_path_mismatch is True
    assert report.reply_to_mismatch is True


def test_r9_zweiter_griff_nimmt_die_erste_klammer() -> None:
    """Bewusst die erste `<…@…>`-Klammer — die letzte liesse die Domain doch fälschen."""
    mail = _attack_mail(
        b"From: <attacker@evil.example> <info@bank.example> =?utf-8?Q?x?="
    )
    raw = build_raw_mail(make_message(mail))

    assert raw.from_domain == "evil.example"


# --- NF-1, fünfte Iteration: S-1 (Deckel für JEDE Kopfzeile) und S-2 (Kommentare) --------
#
# S-1: Der R-8-Deckel sass nur in `_address_header` und im Betreff-Zweig; `To` lief
# ungedeckelt durch `getaddresses` (20-MB-`To` = 18,5 s CPU), und die Rück-Serialisierung
# in `_raw_bytes` faltete jede Kopfzeile jedes Teils neu (250 000 Kopfzeilen = 13,8 s).
# S-2: Der Klammer-Rückfall aus R-9 las die erste `<…>` auch aus einem Kommentar oder
# Quoted String — und der 4096-Schnitt aus R-8 zerschnitt beides mitten durch.


def _mail_mit_header(name: bytes, value: bytes) -> bytes:
    """Sonst gewöhnliche Mail mit einem beliebig grossen Header `name`."""
    return (
        b"Message-ID: <x@y.example>\r\nFrom: <a@b.example>\r\n"
        + name
        + b": "
        + value
        + b"\r\nSubject: Hi\r\nDate: Mon, 01 Sep 2026 10:00:00 +0200\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo.\r\n"
    )


def _from_domain_oracle(mail: bytes, header: str = "From") -> str:
    """Orakel: `email.policy.default` — das, was ein modernes Mailprogramm anzeigt."""
    import email
    import email.policy

    message = email.message_from_bytes(mail, policy=email.policy.default)
    try:
        addresses = message[header].addresses
    except Exception:
        return ""
    if not addresses or not addresses[0].domain:
        return ""
    return addresses[0].domain.lower()


def test_s1_riesiger_to_header_kostet_keine_zeit() -> None:
    """Der Repro des Skeptikers: 8 MB `To` (vorher 3,7 bis 4,1 s CPU je Form)."""
    import time

    from maildigest.ingest.imap_client import MAX_RECIPIENTS

    for value in (b"a@b.example, " * (8 * 1024 * 1024 // 13), b"<a@b" * (2 * 1024 * 1024)):
        message = make_message(_mail_mit_header(b"To", value))
        beginn = time.process_time()
        raw = build_raw_mail(message)
        dauer = time.process_time() - beginn

        assert dauer < 0.5, dauer
        assert len(raw.to_addrs) <= MAX_RECIPIENTS
        assert raw.from_domain == "b.example"


def test_s1_messreihe_to_header() -> None:
    """n/2n/4n/8n = 1/2/4/8 MB `To`: die Zeit hängt nicht mehr an der Grösse."""
    import time

    zeiten: dict[int, float] = {}
    for mb in (1, 2, 4, 8):
        value = b"a@b.example, " * (mb * 1024 * 1024 // 13)
        message = make_message(_mail_mit_header(b"To", value))
        beginn = time.process_time()
        build_raw_mail(message)
        zeiten[mb] = time.process_time() - beginn

    # Vorher (Stand b7d093d): 0,57 / 1,36 / 2,00 / 4,11 s — linear und ungedeckelt.
    assert all(dauer < 0.3 for dauer in zeiten.values()), zeiten


@pytest.mark.parametrize(
    "name",
    [
        b"To",
        b"Cc",
        b"Message-ID",
        b"Date",
        b"Return-Path",
        b"Authentication-Results",
        b"Subject",
        b"From",
        b"Reply-To",
        b"List-Unsubscribe",
    ],
)
def test_s1_jeder_gelesene_header_ist_gedeckelt(name: bytes) -> None:
    """Die Schranke gilt für die Klasse (jede Kopfzeile), nicht für die gemeldete Instanz."""
    import time

    from maildigest.ingest.imap_client import _MAX_HEADER_CHARS

    value = b"a@b.example, " * (2 * 1024 * 1024 // 13)
    message = make_message(_mail_mit_header(name, value))
    beginn = time.process_time()
    raw = build_raw_mail(message)
    dauer = time.process_time() - beginn

    assert dauer < 0.5, (name, dauer)
    # Nichts, was aus einem Header stammt, ist länger als der Deckel.
    for feld in (raw.message_id, raw.subject_raw, raw.from_addr, raw.reply_to):
        assert feld is None or len(feld) <= _MAX_HEADER_CHARS
    assert raw.auth_results_header is None or len(raw.auth_results_header) <= 32 * _MAX_HEADER_CHARS
    assert len(raw.dedupe_key) <= _MAX_HEADER_CHARS
    # Auch die Roh-Mail trägt den Riesen-Header nicht mehr (Serialisierung, Hash, Sanitizer).
    assert len(raw.mime_bytes) < 64 * 1024


def test_s1_empfaengerzahl_ist_gedeckelt() -> None:
    """Höchstens MAX_RECIPIENTS Adressen — auch über mehrere `To:`-Zeilen hinweg."""
    from maildigest.ingest.imap_client import MAX_RECIPIENTS

    zeile = b", ".join(b"u%d@b.example" % n for n in range(150))
    mail = (
        b"Message-ID: <x@y>\r\nFrom: <a@b.example>\r\nTo: "
        + zeile
        + b"\r\nTo: "
        + zeile
        + b"\r\nSubject: Hi\r\n\r\nHallo.\r\n"
    )
    raw = build_raw_mail(make_message(mail))

    assert len(raw.to_addrs) == MAX_RECIPIENTS == 200
    assert raw.to_addrs[0] == "u0@b.example"


def test_s1_viele_kopfzeilen_kosten_keine_zeit() -> None:
    """250 000 Kopfzeilen à 80 Byte: vorher 13,8 s in `as_bytes()`, jetzt gedeckelt."""
    import time

    kopf = (b"X-A: " + b"a " * 37 + b"\r\n") * 250_000
    mail = b"Message-ID: <x@y>\r\nFrom: <a@b.example>\r\n" + kopf + b"Subject: Hi\r\n\r\nHallo.\r\n"
    message = make_message(mail)
    beginn = time.process_time()
    raw = build_raw_mail(message)
    dauer = time.process_time() - beginn

    assert dauer < 1.0, dauer
    assert raw.from_domain == "b.example"
    assert len(raw.mime_bytes) < 512 * 1024


def test_s1_kopfzeilen_gesamtbudget_schneidet_den_baum() -> None:
    """5000 Teile à 4 KB Kopfzeilen (20 MB): vorher 13,0 s; der Baum wird sichtbar gekürzt."""
    import time

    teile = b"".join(
        b"--BB\r\nContent-Type: text/plain\r\nX-A: " + b"a " * 2045 + b"\r\n\r\nTeil %d\r\n" % n
        for n in range(5000)
    )
    mail = (
        b"Message-ID: <x@y>\r\nFrom: <a@b.example>\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=BB\r\n\r\n" + teile + b"--BB--\r\n"
    )
    message = make_message(mail)
    beginn = time.process_time()
    raw = build_raw_mail(message)
    dauer = time.process_time() - beginn

    assert dauer < 1.0, dauer
    assert len(raw.mime_bytes) < 512 * 1024
    # Die gekürzte Mail bleibt eine verarbeitbare Mail: der erste Teil ist da.
    sanitized = MailSanitizer().sanitize(raw)
    assert "Teil 0" in sanitized.body_text
    assert "Teil 4999" not in sanitized.body_text


def test_s1_kopfzeilen_ersetzen_ist_sichtbar() -> None:
    """`_cap_message_headers` wirkt auf `get_all` UND auf den Generator (privates `_headers`)."""
    import email

    from maildigest.ingest.imap_client import _MAX_HEADER_CHARS, _cap_message_headers

    message = email.message_from_bytes(_mail_mit_header(b"X-Gross", b"a" * 10_000))
    assert _cap_message_headers(message) is True
    werte = message.get_all("X-Gross")
    assert werte is not None and len(str(werte[0])) == _MAX_HEADER_CHARS
    assert len(message.as_bytes()) < 6000
    assert message["Message-ID"] == "<x@y.example>"


def test_s1_gewoehnliche_mail_bleibt_byteidentisch() -> None:
    """Gegenprobe: Ohne Riesen-Header greift nichts — Serialisierung und Hash unverändert."""
    import email

    from maildigest.ingest.imap_client import _cap_message_headers

    unberuehrt = email.message_from_bytes(FULL_MAIL).as_bytes()
    message = email.message_from_bytes(FULL_MAIL)
    assert _cap_message_headers(message) is False
    assert message.as_bytes() == unberuehrt

    raw = build_raw_mail(make_message(FULL_MAIL, size=len(FULL_MAIL)))
    assert raw.mime_bytes == unberuehrt
    assert raw.content_hash == hashlib.sha256(unberuehrt).hexdigest()
    assert raw.to_addrs == ["mirror@example.org", "zweite@example.org"]
    assert raw.subject_raw == "Rechnung Maerz"


#: S-2: Formen, in denen die Bank-Klammer in einem Kommentar oder Quoted String steht —
#: teils erst durch den 4096-Zeichen-Schnitt geöffnet, teils von vornherein offen.
_PAD = b"A" * 4200
_KOMMENTAR_FORMEN = [
    b"(<x@bank.example> " + _PAD + b") <real@evil.example>",
    b"(<x@bank.example> <real@evil.example>",
    b"(Bank Support <x@bank.example> " + _PAD + b") <real@evil.example>",
    b'"<x@bank.example> ' + _PAD + b'" <real@evil.example>',
    b'"<x@bank.example> <real@evil.example>',
    b"<real@evil.example> (<x@bank.example> TOKEN",
    b"(a (<x@bank.example>) b <real@evil.example> TOKEN",
    b'"' + _PAD + b'<x@bank.example>" <real@evil.example>',
    b"<<x@bank.example>real@evil.example>",
]


@pytest.mark.parametrize("from_rest", _KOMMENTAR_FORMEN)
def test_s2_klammer_in_kommentar_oder_quote_bestimmt_die_domain_nicht(from_rest: bytes) -> None:
    """Rückfälle lesen nie Kommentar- oder Quoted-String-Inhalt; unbalanciert ⇒ unbekannt.

    Orakel ist `email.policy.default` auf demselben Rohheader. Das Werkzeug darf davon
    nur in die ungefährliche Richtung abweichen: echte Angreiferadresse oder unbekannt
    **mit** Warnung — nie die fremde Domain aus dem Kommentar. Auf dem Stand b7d093d
    lieferten die ersten drei Formen `bank.example` mit beiden Warnungen False.
    """
    mail = _attack_mail(b"From: " + from_rest)
    raw = build_raw_mail(make_message(mail))
    orakel = _from_domain_oracle(mail)

    assert raw.from_domain != "bank.example"
    assert raw.from_domain in {"", orakel, "evil.example"}
    report = MailSanitizer().sanitize(raw).sanitization_report
    if not raw.from_domain:
        assert report.return_path_mismatch is True


@pytest.mark.parametrize("from_rest", _KOMMENTAR_FORMEN)
def test_s2_dasselbe_im_reply_to(from_rest: bytes) -> None:
    """Dieselben Formen im `Reply-To` schalten die Antwortadress-Warnung nicht ab."""
    mail = _attack_mail(
        b"From: <sender@evil.example>", reply_to=b"Reply-To: " + from_rest + b"\r\n"
    )
    raw = build_raw_mail(make_message(mail))
    report = MailSanitizer().sanitize(raw).sanitization_report

    assert raw.reply_to is not None
    # Was der Sanitizer als Antwortadresse liest, ist nie die Bank-Adresse aus dem
    # Kommentar/Quoted String (bei unbalancierten Formen bleibt der Rohtext stehen und
    # `parseaddr` liest daraus höchstens ein unbrauchbares Bruchstück, nie eine Adresse).
    assert parseaddr_domain(raw.reply_to) != "bank.example"
    assert parseaddr_address(raw.reply_to) != "x@bank.example"
    # Die Antwortadresse ist entweder die echte (real@evil.example ≠ sender@evil.example)
    # oder unbekannt — in beiden Fällen ist die Warnung an.
    assert report.reply_to_mismatch is True


def parseaddr_address(value: str) -> str:
    from email.utils import parseaddr

    return parseaddr(value)[1].lower()


def parseaddr_domain(value: str) -> str:
    return parseaddr_address(value).rpartition("@")[2]


@pytest.mark.parametrize(
    "from_rest",
    [
        b"Real Name (Firma) <real@evil.example>",
        b'"Real, Name" <real@evil.example>',
        b"(a (<x@bank.example>) b) <real@evil.example> TOKEN",
        b"(\\) <x@bank.example>) <real@evil.example> TOKEN",
        b'"\\" <x@bank.example>" <real@evil.example> TOKEN',
        b"<real@evil.example> (<x@bank.example>) TOKEN",
        b"(<x@bank.example>) <real@evil.example>",
        b"Real :) <real@evil.example>",
    ],
)
def test_s2_balancierte_kommentare_und_quotes_bleiben_lesbar(from_rest: bytes) -> None:
    """Gegenprobe: verschachtelt, escaped, Kommentar nach der Adresse — die Domain steht."""
    mail = _attack_mail(b"From: " + from_rest)
    raw = build_raw_mail(make_message(mail))

    assert raw.from_domain == "evil.example"
    assert MailSanitizer().sanitize(raw).sanitization_report.return_path_mismatch is True


def test_s2_scanner_kennt_verschachtelung_escapes_und_offene_enden() -> None:
    """Der lineare Scanner nach RFC 5322 §3.2.2/§3.2.4, direkt geprüft."""
    from maildigest.ingest.imap_client import _outside_comments_and_quotes

    assert _outside_comments_and_quotes("a (b (c) d) e")[0] == "a  e"
    assert _outside_comments_and_quotes('a "b (c" d')[0] == "a  d"
    assert _outside_comments_and_quotes("a (b \\) c) d")[0] == "a  d"
    assert _outside_comments_and_quotes('a "b \\" c" d')[0] == "a  d"
    assert _outside_comments_and_quotes("a ) b")[0] == "a ) b"  # Streuklammer: Text
    assert _outside_comments_and_quotes("a (b")[2] is False
    assert _outside_comments_and_quotes('a "b')[2] is False
    assert _outside_comments_and_quotes("a (b (c) d")[2] is False
    text, positionen, balanciert = _outside_comments_and_quotes("(x) <a@b>")
    assert (text, balanciert) == (" <a@b>", True)
    assert text[positionen.index(4)] == "<"


def test_s2_token_hinter_der_klammer_loescht_die_antwortadresse_nicht() -> None:
    """R-9-Form im `Reply-To`: `parseaddr` liest sie nicht, die Warnung muss trotzdem an sein."""
    mail = _attack_mail(
        b"From: <sender@evil.example>",
        reply_to=b"Reply-To: <collect@evil2.example> (x) TOKEN\r\n",
    )
    raw = build_raw_mail(make_message(mail))
    report = MailSanitizer().sanitize(raw).sanitization_report

    assert parseaddr_domain(raw.reply_to or "") == "evil2.example"
    assert report.reply_to_mismatch is True


def test_s2_reply_to_gleich_absender_mit_kommentar_ist_kein_mismatch() -> None:
    """Gegenprobe: Antwortadresse = Absender, nur mit Kommentar und Token — keine Warnung."""
    mail = _attack_mail(
        b"From: <sender@evil.example>",
        reply_to=b"Reply-To: <sender@evil.example> (Support) TOKEN\r\n",
    )
    raw = build_raw_mail(make_message(mail))

    assert MailSanitizer().sanitize(raw).sanitization_report.reply_to_mismatch is False


def test_s2_scanner_ist_linear() -> None:
    """Messreihe n/2n/4n/8n für den neuen Scanner im Hot Path (Regel der vierten Iteration)."""
    import time

    from maildigest.ingest.imap_client import _outside_comments_and_quotes

    zeiten: dict[int, float] = {}
    for n in (4096, 8192, 16384, 32768):
        text = "(a" * (n // 2)  # lauter offene Kommentare: die tiefste Verschachtelung
        beginn = time.process_time()
        _, _, balanciert = _outside_comments_and_quotes(text)
        zeiten[n] = time.process_time() - beginn
        assert balanciert is False

    assert zeiten[32768] < 0.1, zeiten
    if zeiten[4096] > 0.001:
        assert zeiten[32768] / zeiten[4096] < 16, zeiten
