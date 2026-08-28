"""Unit-Tests für `build_raw_mail` und `backoff_delay` (WP2).

Getestet wird gegen echte `imap_tools.MailMessage`-Objekte, die aus rohen `.eml`-Bytes
gebaut werden — also gegen genau das Objekt, das der Server liefert, ohne Netzwerk
(Begründung des Mock-Ansatzes: WP2-ADR in docs/DECISIONS.md).

Schwerpunkte: vollständige Feldbefüllung nach docs/ARCHITECTURE.md §3, Fallback-Dedupe-Key
bei fehlender Message-ID, Grenzfälle mit kaputten Headern, Backoff-Kennlinie.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from imap_tools import MailMessage

from maildigest.ingest.imap_client import (
    MAX_BACKOFF_SECONDS,
    backoff_delay,
    build_raw_mail,
)

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
