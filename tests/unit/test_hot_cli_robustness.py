"""CLI-Robustheit: kaputte Config, fehlende Rechte, feindliche Dateien — Hot-Testing WP10.

Die CLI ist die einzige Stelle, an der ein Fehler den **Nutzer** erreicht. Zwei Zusagen
werden hier geprüft:

1. **Kein Traceback, kein Absturz.** Jeder erwartbare Betriebsfehler (fehlende Datei,
   kaputtes TOML, Schemafehler, unlesbare/unschreibbare Pfade, unbrauchbare State-DB)
   endet in einer deutschen Meldung auf stderr und einem definierten Exit-Code
   (docs/SPEC-CLI.md). Ein Traceback könnte Mail-Inhalte oder Pfade transportieren (I5).
2. **Keine Secrets in der Ausgabe** (I5/F-SEC-8): Auch wenn die Config Passwörter und
   Tokens enthält, taucht keiner ihrer Werte in einer Fehlermeldung auf.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from maildigest.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Hooks, main

#: Vollständige, gültige Config mit erkennbaren „Secrets".
VALID_CONFIG = """
[general]
language = "de"

[imap]
host = "imap.example.org"
username = "mirror@example.org"
password = "GEHEIMES-IMAP-PASSWORT"

[llm]
model = "modell"
api_key = "sk-GEHEIMER-API-KEY"

[messenger]
active = "telegram"

[messenger.telegram]
token = "12345:GEHEIMES-BOT-TOKEN"
chat_id = "42"
"""

SECRETS = ["GEHEIMES-IMAP-PASSWORT", "sk-GEHEIMER-API-KEY", "GEHEIMES-BOT-TOKEN"]


def run(argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    """Führt ein Kommando aus und liefert (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, hooks=Hooks()
    )
    return code, out.getvalue(), err.getvalue()


def assert_clean_error(code: int, out: str, err: str) -> None:
    """Definierter Fehler-Exit, deutsche Meldung, kein Traceback, keine Secrets."""
    assert code in (EXIT_ERROR, EXIT_USAGE), f"unerwarteter Exit-Code {code}"
    assert "Error:" in err, f"keine Fehlermeldung: {err!r}"
    assert "Traceback" not in err and "Traceback" not in out
    for secret in SECRETS:
        assert secret not in err and secret not in out, "Secret in der Ausgabe (I5)"


# --- Kaputte Konfigurationsdateien -------------------------------------------------------


def test_missing_config_file(tmp_path: Path) -> None:
    """Fehlende Datei: klare Meldung, kein Absturz."""
    code, out, err = run(["run", "--once", "--config", str(tmp_path / "gibtsnicht.toml")])
    assert_clean_error(code, out, err)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("kein_toml", "das hier ist [kein gueltiges TOML = = ="),
        ("leer", ""),
        ("nur_kommentar", "# alles auskommentiert\n"),
        ("falscher_typ", '[imap]\nhost = 42\nusername = "x"\n'),
        ("fehlendes_pflichtfeld", '[imap]\nusername = "x"\n'),
        ("unbekannte_sektion", '[gibtsnicht]\nfoo = "bar"\n'),
        ("unbekanntes_feld", '[general]\nsprache_falsch_geschrieben = "de"\n'),
        ("ungueltige_uhrzeit", '[general]\nlow_digest_time = "25:99"\n'),
        ("ungueltiger_port", '[imap]\nhost = "x"\nusername = "y"\nport = 70000\n'),
        (
            "ungueltiges_intervall",
            '[imap]\nhost = "x"\nusername = "y"\npoll_interval_seconds = 0\n',
        ),
        ("ungueltiger_messenger", '[messenger]\nactive = "brieftaube"\n'),
        ("ungueltige_wichtigkeit", '[general]\ndeliver_min_importance = "sehr wichtig"\n'),
    ],
)
def test_broken_config_produces_a_clean_message(
    tmp_path: Path, name: str, content: str
) -> None:
    """Jede Art kaputter Config endet in einer Meldung, nie in einem Traceback."""
    path = tmp_path / f"{name}.toml"
    path.write_text(content, encoding="utf-8")
    code, out, err = run(["run", "--once", "--config", str(path)])
    assert_clean_error(code, out, err)


def test_config_with_invalid_utf8(tmp_path: Path) -> None:
    """Kaputte Kodierung: die Datei wird abgelehnt, nicht halb geraten."""
    path = tmp_path / "kaputt.toml"
    path.write_bytes(b'[general]\nlanguage = "de\xff\xfe"\n')
    code, out, err = run(["run", "--once", "--config", str(path)])
    assert_clean_error(code, out, err)


def test_config_that_is_a_directory(tmp_path: Path) -> None:
    """Ein Verzeichnis statt einer Datei ist ein Bedienfehler, kein Absturz."""
    directory = tmp_path / "config.toml"
    directory.mkdir()
    code, out, err = run(["run", "--once", "--config", str(directory)])
    assert_clean_error(code, out, err)


def test_enormous_config_file(tmp_path: Path) -> None:
    """Eine absurd große Config sprengt nichts (T10 gilt auch für lokale Eingaben)."""
    path = tmp_path / "riesig.toml"
    path.write_text(
        '[summarizer]\ninstructions = "' + "x" * 2_000_000 + '"\n', encoding="utf-8"
    )
    code, out, err = run(["run", "--once", "--config", str(path)])
    assert_clean_error(code, out, err)


# --- Fehlende Rechte ---------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="als root sind Dateirechte wirkungslos")
def test_unreadable_config_file(tmp_path: Path) -> None:
    """Config ohne Leserecht: verständliche Meldung statt `PermissionError`."""
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    path.chmod(0o000)
    try:
        code, out, err = run(["run", "--once", "--config", str(path)])
        assert_clean_error(code, out, err)
    finally:
        path.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="als root sind Dateirechte wirkungslos")
def test_state_db_directory_not_writable(tmp_path: Path) -> None:
    """State-DB nicht anlegbar: `StateError` wird von `main` abgefangen (I5)."""
    workdir = tmp_path / "nur-lesen"
    workdir.mkdir()
    path = workdir / "config.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    workdir.chmod(0o500)
    try:
        code, out, err = run(["run", "--once", "--config", str(path)])
        assert_clean_error(code, out, err)
    finally:
        workdir.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="als root sind Dateirechte wirkungslos")
def test_state_db_file_is_unreadable(tmp_path: Path) -> None:
    """Vorhandene, aber unlesbare State-DB: Meldung statt Traceback."""
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    database = tmp_path / "state.db"
    database.write_bytes(b"SQLite format 3\x00 kaputt")
    database.chmod(0o000)
    try:
        code, out, err = run(["run", "--once", "--config", str(path)])
        assert_clean_error(code, out, err)
    finally:
        database.chmod(0o600)


def test_state_db_is_a_foreign_file(tmp_path: Path) -> None:
    """Eine fremde Datei am DB-Pfad wird nicht überschrieben, sondern gemeldet."""
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    (tmp_path / "state.db").write_bytes(b"wichtige fremde Daten")
    code, out, err = run(["run", "--once", "--config", str(path)])
    assert_clean_error(code, out, err)
    assert (tmp_path / "state.db").read_bytes() == b"wichtige fremde Daten"


# --- Bedienfehler ------------------------------------------------------------------------


def test_unknown_command_is_a_usage_error() -> None:
    """Unbekanntes Kommando ⇒ Exit 2, kein Traceback."""
    code, _out, err = run(["voellig-erfunden"])
    assert code == EXIT_USAGE
    assert "Error:" in err


def test_test_command_with_missing_eml(tmp_path: Path) -> None:
    """`test --eml` mit fehlender Datei meldet sauber."""
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    code, out, err = run(
        ["test", "--eml", str(tmp_path / "fehlt.eml"), "--dry-run", "--config", str(path)]
    )
    assert_clean_error(code, out, err)


def test_test_command_with_a_directory_as_eml(tmp_path: Path) -> None:
    """`test --eml <verzeichnis>`: Bedienfehler statt `IsADirectoryError`."""
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    directory = tmp_path / "ordner"
    directory.mkdir()
    code, out, err = run(
        ["test", "--eml", str(directory), "--dry-run", "--config", str(path)]
    )
    assert_clean_error(code, out, err)


def test_help_always_works_without_a_config() -> None:
    """`--help` funktioniert immer — auch ohne jede Konfiguration."""
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"], stdin=io.StringIO(), stdout=out, stderr=err, hooks=Hooks())
    assert excinfo.value.code == EXIT_OK
