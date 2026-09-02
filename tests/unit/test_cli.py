"""Tests der CLI-Grundlagen: Argumentparser, Exit-Codes, `init`, TOML-Rendering (WP9).

Die Kommandos werden über :func:`maildigest.cli.main` mit injizierten Strömen aufgerufen —
das ist derselbe Pfad wie der Konsolen-Einstiegspunkt, nur ohne `sys.exit`.
"""

from __future__ import annotations

import io
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from maildigest.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    ConfigFile,
    Console,
    Hooks,
    build_parser,
    main,
    render_toml,
)


def run(
    argv: list[str], *, stdin: str = "", hooks: Hooks | None = None
) -> tuple[int, str, str]:
    """Führt ein Kommando aus und liefert (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        hooks=hooks if hooks is not None else Hooks(),
    )
    return code, out.getvalue(), err.getvalue()


# --- Argumentparser -------------------------------------------------------------------


def test_ohne_kommando_zeigt_hilfe_und_exit_2() -> None:
    code, out, _err = run([])
    assert code == EXIT_USAGE
    assert "KOMMANDO" in out


def test_unbekanntes_kommando_ist_bedienfehler() -> None:
    code, _out, err = run(["gibtsnicht"])
    assert code == EXIT_USAGE
    assert "Fehler:" in err


def test_unbekannte_option_ist_bedienfehler() -> None:
    code, _out, err = run(["init", "--gibtsnicht"])
    assert code == EXIT_USAGE
    assert "unrecognized arguments" in err or "nicht erkannt" in err


def test_ungueltiger_wert_einer_choice_option_ist_bedienfehler() -> None:
    code, _out, _err = run(["init", "--language", "klingonisch"])
    assert code == EXIT_USAGE


def test_help_beendet_mit_null() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run(["--help"])
    assert excinfo.value.code == EXIT_OK


# --- init ------------------------------------------------------------------------------


def test_init_legt_datei_mit_0600_an(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    code, out, _err = run(
        ["init", "--config", str(target)],
        stdin="de\nmedium\nnormal\n18:00\nRechnungen sind wichtig\n",
    )
    assert code == EXIT_OK
    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "Nächste Schritte" in out

    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["general"]["language"] == "de"
    assert data["summarizer"]["instructions"] == "Rechnungen sind wichtig"
    # Pflichtfelder bleiben bewusst als Kommentar-Platzhalter stehen.
    assert "host" not in data["imap"]
    assert "model" not in data["llm"]


def test_init_uebernimmt_antworten(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    code, _out, _err = run(
        ["init", "--config", str(target)], stdin="en\nlong\nhigh\n07:30\n\n"
    )
    assert code == EXIT_OK
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["general"] == {
        "language": "en",
        "summary_length": "long",
        "deliver_min_importance": "high",
        "low_digest_time": "07:30",
        "state_db": "",
        "log_level": "INFO",
    }


def test_init_nicht_interaktiv_nutzt_defaults_und_optionen(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    code, _out, _err = run(
        [
            "init",
            "--config",
            str(target),
            "--non-interactive",
            "--summary-length",
            "short",
            "--instructions",
            "kurz halten",
        ]
    )
    assert code == EXIT_OK
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["general"]["summary_length"] == "short"
    assert data["general"]["language"] == "de"
    assert data["summarizer"]["instructions"] == "kurz halten"


def test_init_ueberschreibt_nicht_ohne_force(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("# alt\n", encoding="utf-8")
    code, _out, err = run(["init", "--config", str(target), "--non-interactive"])
    assert code == EXIT_ERROR
    assert "--force" in err
    assert target.read_text(encoding="utf-8") == "# alt\n"


def test_init_mit_force_ueberschreibt(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("# alt\n", encoding="utf-8")
    code, _out, _err = run(
        ["init", "--config", str(target), "--non-interactive", "--force"]
    )
    assert code == EXIT_OK
    assert "[general]" in target.read_text(encoding="utf-8")


def test_init_lehnt_ungueltige_uhrzeit_ab(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    code, _out, err = run(
        ["init", "--config", str(target), "--non-interactive", "--low-digest-time", "25:99"]
    )
    assert code == EXIT_ERROR
    assert "low_digest_time" in err
    assert not target.exists()


def test_init_ueber_umgebungsvariable(tmp_path: Path, monkeypatch: Any) -> None:
    target = tmp_path / "aus-env.toml"
    monkeypatch.setenv("MAILDIGEST_CONFIG", str(target))
    code, _out, _err = run(["init", "--non-interactive"])
    assert code == EXIT_OK
    assert target.exists()


def test_init_kuerzt_zu_lange_instructions(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    code, _out, _err = run(
        [
            "init",
            "--config",
            str(target),
            "--non-interactive",
            "--instructions",
            "x" * 5000,
        ]
    )
    assert code == EXIT_OK
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert len(data["summarizer"]["instructions"]) == 2000


# --- Konfigurationsdatei ----------------------------------------------------------------


def test_render_toml_ist_wieder_lesbar() -> None:
    data: dict[str, Any] = {
        "general": {"language": "de", "log_level": "INFO"},
        "summarizer": {"instructions": 'Zeile "eins"\nZeile\tzwei\\drei'},
        "links": {"footnote": True},
        "limits": {"max_text_chars": 30000},
        "messenger": {"active": "telegram", "telegram": {"chat_id": "-100"}},
    }
    parsed = tomllib.loads(render_toml(data))
    assert parsed["summarizer"]["instructions"] == 'Zeile "eins"\nZeile\tzwei\\drei'
    assert parsed["links"]["footnote"] is True
    assert parsed["messenger"]["telegram"]["chat_id"] == "-100"


def test_render_toml_behaelt_unbekannte_schluessel() -> None:
    text = render_toml({"general": {"language": "de", "zukunftsfeld": 7}})
    assert tomllib.loads(text)["general"]["zukunftsfeld"] == 7


def test_speichern_setzt_rechte_einer_offenen_datei_zurueck(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[general]\n", encoding="utf-8")
    target.chmod(0o644)
    ConfigFile.load(target).save()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_laden_einer_fehlenden_datei_nennt_init(tmp_path: Path) -> None:
    with pytest.raises(CliError) as excinfo:
        ConfigFile.load(tmp_path / "fehlt.toml")
    assert "maildigest init" in str(excinfo.value)


def test_laden_von_kaputtem_toml_meldet_klartext(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[general\n", encoding="utf-8")
    code, _out, err = run(["connect-mail", "--config", str(target), "--non-interactive"])
    assert code == EXIT_ERROR
    assert "kein gültiges" in err


def test_section_meldet_falschen_typ(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('imap = "kaputt"\n', encoding="utf-8")
    with pytest.raises(CliError):
        ConfigFile.load(target).section("imap")


# --- Konsole ----------------------------------------------------------------------------


def _console(stdin: str, *, interactive: bool = True) -> tuple[Console, io.StringIO]:
    err = io.StringIO()
    return (
        Console(io.StringIO(stdin), io.StringIO(), err, interactive=interactive),
        err,
    )


def test_ask_nimmt_default_bei_leerer_eingabe() -> None:
    console, _err = _console("\n")
    assert console.ask("Frage", default="wert") == "wert"


def test_ask_wiederholt_bei_unerlaubtem_wert() -> None:
    console, err = _console("falsch\nde\n")
    assert console.ask("Sprache", allowed=("de", "en")) == "de"
    assert "Ungültiger Wert" in err.getvalue()


def test_ask_bricht_nach_drei_fehlversuchen_ab() -> None:
    console, _err = _console("a\nb\nc\n")
    with pytest.raises(CliError) as excinfo:
        console.ask("Sprache", allowed=("de",))
    assert excinfo.value.code == EXIT_USAGE


def test_ask_meldet_eof_als_abbruch() -> None:
    console, _err = _console("")
    with pytest.raises(CliError) as excinfo:
        console.ask("Frage")
    assert "abgebrochen" in str(excinfo.value).lower()


def test_ask_nicht_interaktiv_verlangt_pflichtangabe() -> None:
    console, _err = _console("", interactive=False)
    with pytest.raises(CliError) as excinfo:
        console.ask("Host", required=True, flag="--host")
    assert excinfo.value.code == EXIT_USAGE
    assert "--host" in str(excinfo.value)


def test_ask_int_prueft_bereich() -> None:
    console, err = _console("abc\n99999\n993\n")
    assert console.ask_int("Port", default=1) == 993
    assert "ganze Zahl" in err.getvalue()


def test_choose_liefert_index() -> None:
    console, _err = _console("2\n")
    assert console.choose("Ordner", ["INBOX", "Archiv"]) == 1


def test_choose_nimmt_default_bei_leerer_eingabe() -> None:
    console, _err = _console("\n")
    assert console.choose("Ordner", ["INBOX", "Archiv"], default_index=1) == 1


def test_confirm_versteht_ja() -> None:
    console, _err = _console("ja\n")
    assert console.confirm("Weiter?") is True


def test_ask_secret_liest_ohne_terminal_aus_stdin() -> None:
    console, _err = _console("geheim\n")
    assert console.ask_secret("Passwort") == "geheim"


def test_ask_secret_fragt_nicht_ohne_interaktivitaet() -> None:
    console, _err = _console("geheim\n", interactive=False)
    assert console.ask_secret("Passwort") == ""


# --- Parser-Struktur ---------------------------------------------------------------------


def test_alle_kommandos_haben_eine_funktion() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if hasattr(action, "choices") and action.dest == "command"
    )
    assert set(subparsers.choices) == {
        "init",
        "connect-mail",
        "connect-llm",
        "connect-messenger",
        "test",
        "run",
    }
    for sub in subparsers.choices.values():
        assert sub.get_default("func") is not None


# --- Randfälle und Sicherheitsnetze --------------------------------------------------------


def test_default_pfad_ist_config_toml_im_arbeitsverzeichnis(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("MAILDIGEST_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    code, _out, _err = run(["init", "--non-interactive"])
    assert code == EXIT_OK
    assert (tmp_path / "config.toml").exists()


def test_nicht_schreibbares_verzeichnis_ist_ein_fehler(tmp_path: Path) -> None:
    gesperrt = tmp_path / "gesperrt"
    gesperrt.mkdir(mode=0o500)
    try:
        code, _out, err = run(
            ["init", "--config", str(gesperrt / "config.toml"), "--non-interactive"]
        )
        assert code == EXIT_ERROR
        assert "kann nicht geschrieben werden" in err
    finally:
        gesperrt.chmod(0o700)


def test_render_toml_lehnt_unbekannte_typen_ab() -> None:
    from datetime import date

    with pytest.raises(CliError):
        render_toml({"general": {"stichtag": date(2026, 9, 2)}})


def test_render_toml_kann_listen_und_zahlen() -> None:
    text = render_toml({"general": {"zahl": 3, "quote": 1.5, "liste": ["a", "b"]}})
    parsed = tomllib.loads(text)["general"]
    assert parsed == {"zahl": 3, "quote": 1.5, "liste": ["a", "b"]}


def test_ask_wiederholt_bei_leerer_pflichtangabe() -> None:
    console, err = _console("\nwert\n")
    assert console.ask("Host", required=True) == "wert"
    assert "Pflichtangabe" in err.getvalue()


def test_ask_int_nimmt_default_und_choose_meldet_leere_liste() -> None:
    console, _err = _console("\n")
    assert console.ask_int("Port", default=993) == 993
    with pytest.raises(CliError):
        console.choose("Nichts", [])


def test_choose_bricht_nach_drei_fehlversuchen_ab() -> None:
    console, err = _console("9\n0\n7\n")
    with pytest.raises(CliError):
        console.choose("Ordner", ["INBOX"])
    assert "zwischen 1 und 1" in err.getvalue()


def test_confirm_nimmt_default_bei_leerer_eingabe() -> None:
    console, _err = _console("\n")
    assert console.confirm("Weiter?", default=True) is True
    console, _err = _console("nein\n")
    assert console.confirm("Weiter?", default=True) is False


def test_confirm_und_choose_nicht_interaktiv() -> None:
    console, _err = _console("", interactive=False)
    assert console.confirm("Weiter?", default=True) is True
    assert console.choose("Ordner", ["a", "b"], default_index=1) == 1


class InterruptingStdin(io.StringIO):
    """stdin-Attrappe, die beim Lesen Strg-C auslöst."""

    def readline(self, size: int = -1) -> str:
        raise KeyboardInterrupt


def test_tastaturunterbrechung_wird_sauber_gemeldet(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["init", "--config", str(tmp_path / "config.toml")],
        stdin=InterruptingStdin(),
        stdout=out,
        stderr=err,
        hooks=Hooks(),
    )
    assert code == EXIT_ERROR
    assert "Abgebrochen" in err.getvalue()
    assert not (tmp_path / "config.toml").exists()


def test_netzwerkfehler_erzeugt_keinen_traceback(tmp_path: Path, monkeypatch: Any) -> None:
    import httpx

    def boom(*_args: Any, **_kwargs: Any) -> int:
        raise httpx.ConnectError("keine Verbindung")

    monkeypatch.setenv("MAILDIGEST_IMAP_PASSWORD", "geheim")
    target = tmp_path / "config.toml"
    run(["init", "--config", str(target), "--non-interactive"])
    hooks = Hooks(imap_client=boom)
    code, _out, err = run(
        [
            "connect-mail",
            "--config",
            str(target),
            "--non-interactive",
            "--host",
            "imap.example.org",
            "--username",
            "m@example.org",
        ],
        stdin="geheim\n",
        hooks=hooks,
    )
    assert code == EXIT_ERROR
    assert "Traceback" not in err
