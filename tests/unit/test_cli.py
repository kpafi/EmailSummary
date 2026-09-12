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
    assert "COMMAND" in out


def test_unbekanntes_kommando_ist_bedienfehler() -> None:
    code, _out, err = run(["gibtsnicht"])
    assert code == EXIT_USAGE
    assert "Error:" in err


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
    assert "Next steps" in out

    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["general"]["language"] == "de"
    assert data["summarizer"]["instructions"] == "Rechnungen sind wichtig"
    # Pflichtfelder bleiben bewusst als Kommentar-Platzhalter stehen.
    assert "host" not in data["imap"]
    assert data["llm"]["provider"] == "none"  # läuft sofort, ohne Anmeldung (ADR-076)
    assert data["llm"]["model"] == ""


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
    assert "not valid" in err


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
    assert "Invalid value" in err.getvalue()


def test_ask_bricht_nach_drei_fehlversuchen_ab() -> None:
    console, _err = _console("a\nb\nc\n")
    with pytest.raises(CliError) as excinfo:
        console.ask("Sprache", allowed=("de",))
    assert excinfo.value.code == EXIT_USAGE


def test_ask_meldet_eof_als_abbruch() -> None:
    console, _err = _console("")
    with pytest.raises(CliError) as excinfo:
        console.ask("Frage")
    assert "aborted" in str(excinfo.value).lower()


def test_ask_nicht_interaktiv_verlangt_pflichtangabe() -> None:
    console, _err = _console("", interactive=False)
    with pytest.raises(CliError) as excinfo:
        console.ask("Host", required=True, flag="--host")
    assert excinfo.value.code == EXIT_USAGE
    assert "--host" in str(excinfo.value)


def test_ask_int_prueft_bereich() -> None:
    console, err = _console("abc\n99999\n993\n")
    assert console.ask_int("Port", default=1) == 993
    assert "whole number" in err.getvalue()


def test_choose_liefert_index() -> None:
    console, _err = _console("2\n")
    assert console.choose("Ordner", ["INBOX", "Archiv"]) == 1


def test_choose_nimmt_default_bei_leerer_eingabe() -> None:
    console, _err = _console("\n")
    assert console.choose("Ordner", ["INBOX", "Archiv"], default_index=1) == 1


def test_confirm_versteht_yes() -> None:
    """Die Ja/Nein-Abfrage ist englisch (ADR-083): `yes`/`y` gelten, `ja` nicht mehr."""
    console, _err = _console("yes\n")
    assert console.confirm("Continue?") is True
    console, _err = _console("y\n")
    assert console.confirm("Continue?") is True
    console, _err = _console("ja\n")
    assert console.confirm("Continue?") is False


def test_ask_secret_liest_ohne_terminal_aus_stdin() -> None:
    console, _err = _console("geheim\n")
    assert console.ask_secret("Password") == "geheim"


def test_ask_secret_fragt_nicht_ohne_interaktivitaet() -> None:
    console, _err = _console("geheim\n", interactive=False)
    assert console.ask_secret("Password") == ""


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
        "instructions",
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
        assert "cannot be written" in err
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
    assert "Required" in err.getvalue()


def test_ask_int_nimmt_default_und_choose_meldet_leere_liste() -> None:
    console, _err = _console("\n")
    assert console.ask_int("Port", default=993) == 993
    with pytest.raises(CliError):
        console.choose("Nichts", [])


def test_choose_bricht_nach_drei_fehlversuchen_ab() -> None:
    console, err = _console("9\n0\n7\n")
    with pytest.raises(CliError):
        console.choose("Ordner", ["INBOX"])
    assert "between 1 and 1" in err.getvalue()


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
    assert "Error: Aborted." in err.getvalue()  # ADR-083: englisch, mit `Error: `-Präfix
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


# --- Regressionen aus dem Cold-Test (tests/cold/REPORT.md) -------------------------------


def test_ct1_config_vor_dem_kommandonamen_wirkt(tmp_path: Path, monkeypatch: Any) -> None:
    """CT-1: `maildigest --config X init` darf nicht still auf `config.toml` arbeiten."""
    arbeitsverzeichnis = tmp_path / "cwd"
    arbeitsverzeichnis.mkdir()
    monkeypatch.chdir(arbeitsverzeichnis)
    target = tmp_path / "c1.toml"
    code, out, _err = run(["--config", str(target), "--non-interactive", "init"])
    assert code == EXIT_OK
    assert target.exists()
    assert str(target) in out
    assert not (arbeitsverzeichnis / "config.toml").exists()


def test_ct1_non_interactive_vor_dem_kommandonamen_wirkt(tmp_path: Path) -> None:
    """CT-1: `--non-interactive` vor dem Kommando muss die Rückfragen abschalten."""
    target = tmp_path / "c4.toml"
    # Leeres stdin: Würde noch gefragt, endete das Kommando am EOF (Exit 2).
    code, _out, _err = run(["--non-interactive", "init", "--config", str(target)], stdin="")
    assert code == EXIT_OK
    assert target.exists()


def test_ct1_hintere_angabe_gewinnt_bei_doppeltem_config(tmp_path: Path) -> None:
    """CT-1: Beide Schreibweisen sind gleichwertig; die spätere Angabe gewinnt."""
    vorne, hinten = tmp_path / "vorne.toml", tmp_path / "hinten.toml"
    code, _out, _err = run(
        ["--config", str(vorne), "init", "--config", str(hinten), "--non-interactive"]
    )
    assert code == EXIT_OK
    assert hinten.exists()
    assert not vorne.exists()


def test_ct1_config_vor_dem_kommando_schlaegt_die_umgebungsvariable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """CT-1: Auch gegenüber MAILDIGEST_CONFIG muss die vordere Option greifen."""
    monkeypatch.setenv("MAILDIGEST_CONFIG", str(tmp_path / "aus-env.toml"))
    target = tmp_path / "explizit.toml"
    code, _out, _err = run(["--config", str(target), "--non-interactive", "init"])
    assert code == EXIT_OK
    assert target.exists()
    assert not (tmp_path / "aus-env.toml").exists()


def test_ct3_init_schreibt_den_vollstaendigen_critic_feldsatz(tmp_path: Path) -> None:
    """CT-3: `[llm.critic]` muss alle vier Felder aus SPEC-CLI §5 zeigen."""
    target = tmp_path / "config.toml"
    code, _out, _err = run(["init", "--config", str(target), "--non-interactive"])
    assert code == EXIT_OK
    text = target.read_text(encoding="utf-8")
    kopf = text.index("[llm.critic]")
    abschnitt = text[kopf : text.index("[summarizer]")]
    for feld in ("provider", "model", "base_url", "max_tokens"):
        assert f"# {feld} = " in abschnitt, f"{feld} fehlt in [llm.critic]"
    # Die Datei bleibt gültiges TOML und der Kritiker erbt weiterhin.
    assert tomllib.loads(text)["llm"]["critic"] == {}


def test_ct3_init_nicht_interaktiv_druckt_keinen_fragehinweis(tmp_path: Path) -> None:
    """CT-3: Ohne Frage kein Frage-Hinweis (die Spec kennt die Zeile nicht)."""
    code, out, _err = run(
        ["init", "--config", str(tmp_path / "config.toml"), "--non-interactive"]
    )
    assert code == EXIT_OK
    assert "leave empty for none" not in out


def test_ct3_init_interaktiv_erklaert_die_custom_instructions(tmp_path: Path) -> None:
    """Gegenprobe: Interaktiv bleibt die Erläuterung erhalten."""
    code, out, _err = run(
        ["init", "--config", str(tmp_path / "config.toml")],
        stdin="\n\n\n\n\n",
    )
    assert code == EXIT_OK
    assert "leave empty for none" in out


@pytest.mark.parametrize("port", ["0", "99999", "-1", "keinezahl"])
def test_ct5_unerlaubter_portwert_ist_bedienfehler(tmp_path: Path, port: str) -> None:
    """CT-5: Ein Wert außerhalb 1..65535 ist ein Bedienfehler (Exit 2), kein Config-Fehler."""
    target = tmp_path / "config.toml"
    run(["init", "--config", str(target), "--non-interactive"])
    code, _out, err = run(
        [
            "connect-mail",
            "--config",
            str(target),
            "--non-interactive",
            "--no-test",
            "--host",
            "imap.example.org",
            "--username",
            "m@example.org",
            "--port",
            port,
        ]
    )
    assert code == EXIT_USAGE
    assert "--port" in err


def test_ct16_eof_auf_stdin_ist_bedienfehler(tmp_path: Path) -> None:
    """CT-16b: Fehlende Eingabe ist Exit 2, nicht Exit 1 (SPEC-CLI §2)."""
    code, _out, err = run(["init", "--config", str(tmp_path / "config.toml")], stdin="")
    assert code == EXIT_USAGE
    assert "--non-interactive" in err


def test_testnachricht_nennt_die_befehle() -> None:
    """Nach dem Einrichten soll der Nutzer wissen, dass es /digest und /status gibt."""
    from maildigest.cli import _TELEGRAM_COMMAND_HINT

    assert "/digest" in _TELEGRAM_COMMAND_HINT
    assert "/status" in _TELEGRAM_COMMAND_HINT


def test_testnachricht_nennt_keine_ungueltige_config_sektion() -> None:
    """HC-16: Eine abgetippte Zeile `[messenger telegram]` bricht die Konfiguration.

    Der Sanitizer entschärft Punkte, ein punktierter Sektionsname käme also verstümmelt
    an. Statt ihn ohne Punkt zu schreiben (und damit falsch), nennt die Nachricht ihn gar
    nicht — die exakte Syntax steht im Terminal-Hinweis und im README.
    """
    from maildigest.cli import _TELEGRAM_COMMAND_HINT, _TEST_MESSAGE, _selftest_notice
    from maildigest.output.sanitizer import final_guard

    texts = (
        _TEST_MESSAGE,
        _TELEGRAM_COMMAND_HINT,
        _selftest_notice(from_file=False),
        _selftest_notice(from_file=True),
    )
    for text in texts:
        assert "messenger telegram" not in text
        # Der Text übersteht den Nachbrenner unverändert — sonst käme er entstellt an.
        assert final_guard(text) == text


def test_init_schreibt_accept_commands_aus(tmp_path: Path) -> None:
    """HC-18: Ein Feld mit Default gehört in die Vorlage, nicht nur ins Schema."""
    cfg = tmp_path / "config.toml"
    code, _out, _err = run(["--config", str(cfg), "--non-interactive", "init"])

    assert code == EXIT_OK
    data = tomllib.loads(cfg.read_text())
    assert data["messenger"]["telegram"]["accept_commands"] is True


def test_selbsttest_vorspann_sagt_dass_die_mail_nicht_echt_ist() -> None:
    """Sonst sucht man im Postfach nach einer Mail, die es nie gab (Feldbericht)."""
    from maildigest.cli import _selftest_notice

    notice = _selftest_notice(from_file=False)
    assert "self-test" in notice
    assert "not from your mailbox" in notice


# --- HC-38 (1): `test` im Werkszustand, über die echten Hooks ---------------------------


def _factory_config(tmp_path: Path) -> Path:
    """Der Zustand direkt nach `init --non-interactive`, um Postfach und Chat ergänzt."""
    path = tmp_path / "config.toml"
    code, _out, _err = run(["init", "--config", str(path), "--non-interactive"])
    assert code == EXIT_OK
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "[imap]\nport = 993",
        '[imap]\nhost = "imap.example.org"\nusername = "mirror@example.org"\nport = 993',
    )
    text = text.replace('chat_id = ""', 'chat_id = "42"\ntoken = "1:abc"')
    path.write_text(text, encoding="utf-8")
    return path


def _eml(subject: str, *, attachment: tuple[str, str] | None = None) -> str:
    """Eine minimale `.eml` — mit optionalem `text/plain`-Anhang."""
    head = (
        "From: Absender <a@sender.example>\n"
        f"Subject: {subject}\n"
        "Date: Mon, 1 Sep 2026 10:00:00 +0200\n"
        "Message-ID: <x@sender.example>\n"
        "MIME-Version: 1.0\n"
    )
    if attachment is None:
        return head + 'Content-Type: text/plain; charset="utf-8"\n\nHallo Welt.\n'
    name, content = attachment
    return (
        head + 'Content-Type: multipart/mixed; boundary="B"\n\n--B\n'
        'Content-Type: text/plain; charset="utf-8"\n\nHallo Welt.\n--B\n'
        f'Content-Type: text/plain; charset="utf-8"; name="{name}"\n'
        f'Content-Disposition: attachment; filename="{name}"\n\n{content}\n--B--\n'
    )


def test_hc38_selbsttest_im_werkszustand_laeuft_ueber_die_echten_hooks(tmp_path: Path) -> None:
    """HC-38/HC-1: Der Standardmodus war nur isoliert geprüft, nie auf dem Nutzerweg.

    `Hooks()` ohne Attrappen heißt: `_summarizer_for`/`_critic_for` entscheiden anhand
    `[llm] provider = "none"`, und ein Betreff über 100 Zeichen darf den Lauf nicht mehr
    fail-closed abbrechen (HC-1). `--dry-run` hält alles im Prozess.
    """
    path = _factory_config(tmp_path)
    eml = tmp_path / "lang.eml"
    eml.write_text(_eml("A" * 300), encoding="utf-8")

    code, out, _err = run(["--config", str(path), "test", "--dry-run", "--eml", str(eml)])
    assert code == EXIT_OK
    assert "Message created" in out
    assert "Fail-closed" not in out
    assert "Excerpt, not a summary" in out


def test_hc38_selbsttest_im_werkszustand_zeigt_den_anhang(tmp_path: Path) -> None:
    """HC-2 auf dem Nutzerweg: Der gelesene Anhang darf nicht stumm verschwinden."""
    path = _factory_config(tmp_path)
    eml = tmp_path / "anhang.eml"
    eml.write_text(
        _eml("Kurz", attachment=("mitteilung.txt", "Neue IBAN im Anhang.")), encoding="utf-8"
    )

    code, out, _err = run(["--config", str(path), "test", "--dry-run", "--eml", str(eml)])
    assert code == EXIT_OK
    assert "mitteilung[.]txt" in out
    assert "Neue IBAN im Anhang." in out


def test_hc18_ausgabe_endet_mit_der_schrittliste(tmp_path: Path) -> None:
    """HC-18 (a): SPEC §4 `init` sagt zu, dass die Ausgabe mit der Liste endet.

    Der Absatz zum Sprachmodell stand dahinter — das Letzte auf dem Bildschirm war damit
    eine Einordnung statt des nächsten Befehls.
    """
    target = tmp_path / "config.toml"
    code, out, _err = run(["--config", str(target), "--non-interactive", "init"])

    assert code == EXIT_OK
    zeilen = [line for line in out.splitlines() if line.strip()]
    assert zeilen[-1].strip().startswith("5) maildigest run")
    assert "Next steps:" in zeilen[-6]
    # Die optionale Stufe steht in der Liste, nicht nur im Fließtext.
    assert any("connect-llm" in line and "optional" in line for line in zeilen)
    # Die Einordnung kommt vor der Liste.
    assert out.index("No language model is configured") < out.index("Next steps:")


def test_hc34_run_meldet_fehlendes_passwort_als_konfigurationsfehler(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """HC-34: Ein fehlendes Passwort ist kein Erreichbarkeitsproblem (SPEC §4 `run`).

    Sonst sucht der Nutzer beim Server statt in seiner Datei.
    """
    monkeypatch.delenv("MAILDIGEST_IMAP_PASSWORD", raising=False)
    target = tmp_path / "run1.toml"
    # Eine im Übrigen vollständige, gültige Konfiguration — sonst scheitert schon das
    # Laden und der Test bewiese nichts.
    assert run(["--config", str(target), "--non-interactive", "init"])[0] == EXIT_OK
    text = target.read_text(encoding="utf-8")
    text = text.replace(
        "[imap]",
        '[imap]\nhost = "imap.example.org"\nusername = "mirror@example.org"',
    ).replace('active = "telegram"', 'active = "discord"').replace(
        "# webhook_url =", 'webhook_url = "https://discord.example/api/webhooks/1/x"\n#'
    )
    target.write_text(text, encoding="utf-8")

    code, _out, err = run(["--config", str(target), "run", "--once"])

    assert code == EXIT_ERROR
    assert err.startswith("Error: Invalid configuration")
    assert "MAILDIGEST_IMAP_PASSWORD" in err
    assert "unreachable" not in err
