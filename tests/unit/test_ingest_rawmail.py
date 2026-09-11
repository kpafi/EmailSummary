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
