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
import signal
import tomllib
from pathlib import Path

import pytest

from maildigest.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Hooks, main
from maildigest.foreign_text import mask_secrets, sanitize_foreign_text
from maildigest.llm.base import LLMTransportError

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


# --- HC-20: die Konfiguration wird atomar geschrieben -------------------------------------


def _full_config(tmp_path: Path) -> Path:
    """Eine vollständige, gültige Konfiguration mit Secrets."""
    path = tmp_path / "full.toml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    path.chmod(0o600)
    return path


def _leftovers(directory: Path) -> list[str]:
    """Alle Dateien im Verzeichnis außer der Konfiguration selbst."""
    return sorted(entry.name for entry in directory.iterdir() if entry.name != "full.toml")


def test_hc20_abgebrochenes_schreiben_laesst_die_alte_datei_unveraendert(
    tmp_path: Path,
) -> None:
    """Volle Platte/Quota mitten im Schreiben: die bisherige Konfiguration überlebt.

    Repro des Berichts mit `RLIMIT_FSIZE`. Vor dem Fix öffnete `ConfigFile.save` das Ziel
    direkt mit `O_TRUNC`: Die Datei war danach abgeschnitten, `[imap] host`/`username`
    fehlten und jedes Folgekommando scheiterte an der Validierung. Jetzt wird in eine
    temporäre Datei im selben Verzeichnis geschrieben und erst am Ende umbenannt.
    """
    resource = pytest.importorskip("resource")
    path = _full_config(tmp_path)
    before = path.read_bytes()

    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (len(before) // 2, hard))
        code, out, err = run(
            ["connect-llm", "--config", str(path), "--non-interactive", "--provider", "none"]
        )
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, previous)

    assert_clean_error(code, out, err)
    assert path.read_bytes() == before, "die alte Konfiguration wurde beschädigt"
    assert _leftovers(tmp_path) == [], "temporäre Datei nicht aufgeräumt"


def test_hc20_fehler_beim_umbenennen_laesst_die_alte_datei_unveraendert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auch ein Fehler im letzten Schritt (`os.replace`) darf nichts hinterlassen."""
    path = _full_config(tmp_path)
    before = path.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    code, out, err = run(
        ["connect-llm", "--config", str(path), "--non-interactive", "--provider", "none"]
    )

    assert_clean_error(code, out, err)
    assert path.read_bytes() == before
    assert _leftovers(tmp_path) == []


def test_hc20_erfolgreiches_schreiben_ergibt_0600_und_vollstaendigen_inhalt(
    tmp_path: Path,
) -> None:
    """Der Normalfall bleibt, wie er war: vollständige Datei, Rechte 0600, kein Rest."""
    path = _full_config(tmp_path)
    code, _out, _err = run(
        ["connect-llm", "--config", str(path), "--non-interactive", "--provider", "none"]
    )
    assert code == EXIT_OK
    assert path.stat().st_mode & 0o777 == 0o600
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["imap"]["host"] == "imap.example.org"
    assert written["llm"]["provider"] == "none"
    assert _leftovers(tmp_path) == []


# --- HC-4: kein Fremdtext mit Steuerzeichen auf stderr ------------------------------------


def test_hc4_stderr_traegt_keine_steuerzeichen_einer_gegenstelle(tmp_path: Path) -> None:
    """Die Ausgabestelle in `main` filtert jede Fehlermeldung (HC-4, ADR-055).

    Der Schutz hängt damit nicht mehr daran, dass jeder einzelne Fremdtext-Pfad daran
    gedacht hat: Auch eine Ausnahme, die künftig irgendwo einen Servertext mitnimmt,
    erreicht das Terminal nur gefiltert.
    """
    path = _full_config(tmp_path)
    hostile = "\x1b[2J\x1b]0;PWNED\x07 boom"

    def provider(**kwargs: object) -> object:
        raise LLMTransportError(hostile)

    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "connect-llm",
            "--config",
            str(path),
            "--non-interactive",
            "--provider",
            "anthropic",
            "--model",
            "m",
        ],
        stdin=io.StringIO(""),
        stdout=out,
        stderr=err,
        hooks=Hooks(build_provider=provider),
    )
    text = err.getvalue()
    assert code == EXIT_ERROR
    assert "\x1b" not in text and "\x07" not in text
    assert all(ord(char) >= 0x20 or char == "\n" for char in text)
    assert "boom" in text


# --- HC-4: die Filterfunktionen selbst ----------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "\x1b[2J",  # Bildschirm löschen
        "\x1b]0;Fenstertitel\x07",  # Fenstertitel setzen
        "\x00\x01\x02",  # weitere C0-Zeichen
        "\x9b31m",  # C1-Steuerzeichen (CSI)
        "harmlos\ttabuliert",
    ],
)
def test_hc4_allowlist_laesst_kein_steuerzeichen_durch(hostile: str) -> None:
    """`sanitize_foreign_text` lässt außer `\\n` kein Steuerzeichen stehen."""
    result = sanitize_foreign_text(hostile)
    assert all(ord(char) >= 0x20 or char == "\n" for char in result)
    assert "\x1b" not in result and "\x07" not in result


def test_hc4_eigene_satzzeichen_und_umlaute_bleiben_lesbar() -> None:
    """Die eigenen Meldungen verwenden Gedankenstrich, Auslassungspunkte und Umlaute."""
    text = "Abbruch — zu viele ungültige Eingaben … „so nicht\" (Größe: 5 §3)"
    assert sanitize_foreign_text(text, keep_newlines=True) == text


def test_hc4_zeilenumbrueche_nur_auf_wunsch() -> None:
    """Ein Fremdtext darf die Zeilenstruktur nicht zerreißen; eigene Meldungen dürfen es."""
    assert sanitize_foreign_text("a\nb") == "a b"
    assert sanitize_foreign_text("a\r\nb", keep_newlines=True) == "a\nb"


def test_hc4_maske_greift_auch_bei_gekuerztem_schluessel() -> None:
    """Manche Anbieter zitieren den Schlüssel gekürzt — auch das ist ein Leck (I5)."""
    key = "sk-ABCDEF1234567890"
    text = f"unknown key {key}, sent as {key[:10]}"
    masked = mask_secrets(text, [key])
    assert key not in masked and key[:10] not in masked
    assert masked.count("***") == 2


def test_hc4_kurze_werte_werden_nicht_maskiert() -> None:
    """Ein zu kurzer „Schlüssel" würde sonst harmlosen Text zerstören."""
    assert mask_secrets("port 993 abgelehnt", ["993", ""]) == "port 993 abgelehnt"
