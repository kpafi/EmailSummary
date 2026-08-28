"""Unit-Tests für `maildigest.state.db` (WP2).

Schwerpunkte:
* Idempotenz-Primitive `claim` (F-ING-2),
* Statusübergänge und Fehlerklassen-Normalisierung (I5),
* keine Klartext-Identifikatoren und keine Mail-Inhalte in der Datei (docs/SECURITY.md §6),
* Schema-Version und Dateirechte.
"""

from __future__ import annotations

import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maildigest.state.db import (
    SCHEMA_VERSION,
    MailState,
    StateDB,
    StateError,
    dedupe_hash,
)

MESSAGE_ID = "<abc123@example.org>"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[StateDB]:
    with StateDB(tmp_path / "state.db") as database:
        yield database


# --- Dedupe / Idempotenz ------------------------------------------------------------------


def test_claim_is_idempotent(db: StateDB) -> None:
    """F-ING-2: Genau der erste Claim gewinnt, jeder weitere meldet 'schon bekannt'."""
    assert db.claim(MESSAGE_ID) is True
    assert db.claim(MESSAGE_ID) is False
    assert db.claim(MESSAGE_ID) is False


def test_claim_sets_pending_and_first_seen(db: StateDB) -> None:
    moment = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    db.claim(MESSAGE_ID, now=moment)
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.status is MailState.PENDING
    assert record.first_seen_at == moment
    assert record.retry_count == 0
    assert record.error_class is None


def test_claim_does_not_overwrite_final_status(db: StateDB) -> None:
    """Ein zweiter Abruf derselben Mail darf einen Endstatus nicht zurücksetzen."""
    db.claim(MESSAGE_ID)
    db.mark_status(MESSAGE_ID, MailState.DELIVERED)
    assert db.claim(MESSAGE_ID) is False
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.status is MailState.DELIVERED


def test_was_seen(db: StateDB) -> None:
    assert db.was_seen(MESSAGE_ID) is False
    db.claim(MESSAGE_ID)
    assert db.was_seen(MESSAGE_ID) is True


def test_distinct_keys_are_independent(db: StateDB) -> None:
    assert db.claim("<a@example.org>") is True
    assert db.claim("<b@example.org>") is True


# --- Statusübergänge ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        MailState.SANITIZED,
        MailState.SUMMARIZED,
        MailState.CHECKED,
        MailState.DELIVERED,
        MailState.SKIPPED_LOW,
        MailState.FAILED,
    ],
)
def test_mark_status_writes_every_state(db: StateDB, status: MailState) -> None:
    db.claim(MESSAGE_ID)
    db.mark_status(MESSAGE_ID, status)
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.status is status


def test_mark_status_creates_missing_record(db: StateDB) -> None:
    """Auch ohne vorherigen Claim entsteht ein Datensatz (kein stiller Verlust)."""
    db.mark_status(MESSAGE_ID, MailState.FAILED, error_class="sanitize_error")
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.status is MailState.FAILED
    assert record.error_class == "sanitize_error"


def test_mark_status_clears_error_class(db: StateDB) -> None:
    db.mark_status(MESSAGE_ID, MailState.FAILED, error_class="llm_timeout")
    db.mark_status(MESSAGE_ID, MailState.DELIVERED)
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.error_class is None


def test_get_unknown_key_returns_none(db: StateDB) -> None:
    assert db.get("<nope@example.org>") is None


def test_increment_retry(db: StateDB) -> None:
    db.claim(MESSAGE_ID)
    assert db.increment_retry(MESSAGE_ID) == 1
    assert db.increment_retry(MESSAGE_ID) == 2


def test_increment_retry_unknown_key(db: StateDB) -> None:
    assert db.increment_retry("<nope@example.org>") == 0


def test_count_by_status(db: StateDB) -> None:
    db.mark_status("<a@example.org>", MailState.DELIVERED)
    db.mark_status("<b@example.org>", MailState.DELIVERED)
    db.mark_status("<c@example.org>", MailState.FAILED)
    assert db.count_by_status(MailState.DELIVERED) == 2
    assert db.count_by_status(MailState.FAILED) == 1
    assert db.count_by_status(MailState.PENDING) == 0


# --- I5: Fehlerklasse trägt nie Inhalte ----------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Sanitize_Error", "sanitize_error"),
        ("  llm timeout  ", "llm_timeout"),
        ("Betreff: Rechnung von müller@example.org!", "betreff_rechnung_von_m_ller_example_org"),
        ("!!!", None),
        ("", None),
        (None, None),
    ],
)
def test_error_class_is_normalised(db: StateDB, given: str | None, expected: str | None) -> None:
    """Selbst ein versehentlich durchgereichter Fehlertext kann keine Inhalte einschleppen."""
    db.mark_status(MESSAGE_ID, MailState.FAILED, error_class=given)
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.error_class == expected


def test_error_class_is_truncated(db: StateDB) -> None:
    db.mark_status(MESSAGE_ID, MailState.FAILED, error_class="a" * 500)
    record = db.get(MESSAGE_ID)
    assert record is not None
    assert record.error_class is not None
    assert len(record.error_class) == 64


# --- Keine Klartext-Identifikatoren, keine Inhalte -----------------------------------------


def test_message_id_is_stored_hashed_only(tmp_path: Path) -> None:
    """docs/SECURITY.md §6 / NF-5: In der Datei steht nur der Hash, nie die Message-ID."""
    path = tmp_path / "state.db"
    with StateDB(path) as db:
        db.claim(MESSAGE_ID)
        db.mark_status(MESSAGE_ID, MailState.DELIVERED)
    content = path.read_bytes()
    assert MESSAGE_ID.encode() not in content
    assert b"example.org" not in content
    assert dedupe_hash(MESSAGE_ID).encode() in content


def test_dedupe_hash_is_stable_and_distinct() -> None:
    assert dedupe_hash(MESSAGE_ID) == dedupe_hash(MESSAGE_ID)
    assert dedupe_hash(MESSAGE_ID) != dedupe_hash("<other@example.org>")
    assert len(dedupe_hash(MESSAGE_ID)) == 64


def test_database_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with StateDB(path):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "state.db"
    with StateDB(path):
        pass
    assert path.exists()


# --- meta / Schema ------------------------------------------------------------------------


def test_meta_roundtrip(db: StateDB) -> None:
    assert db.meta_get("last_digest_at") is None
    db.meta_set("last_digest_at", "2026-08-28T18:00:00+00:00")
    assert db.meta_get("last_digest_at") == "2026-08-28T18:00:00+00:00"
    db.meta_set("last_digest_at", "2026-08-29T18:00:00+00:00")
    assert db.meta_get("last_digest_at") == "2026-08-29T18:00:00+00:00"


def test_schema_version_is_written(tmp_path: Path) -> None:
    with StateDB(tmp_path / "state.db") as db:
        assert db.meta_get("schema_version") == str(SCHEMA_VERSION)


def test_reopening_keeps_state(tmp_path: Path) -> None:
    """Wiederanlauf nach Prozess-Ende erkennt bereits gesehene Mails (F-ING-2)."""
    path = tmp_path / "state.db"
    with StateDB(path) as first:
        assert first.claim(MESSAGE_ID) is True
    with StateDB(path) as second:
        assert second.claim(MESSAGE_ID) is False


def test_foreign_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with StateDB(path) as db:
        db.meta_set("schema_version", "99")
    with pytest.raises(StateError, match="Schema-Version"):
        StateDB(path)


def test_unusable_file_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"das ist keine sqlite-datenbank" * 10)
    with pytest.raises(StateError):
        StateDB(path)
