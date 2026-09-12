"""`maildigest instructions` — Custom-Instructions anzeigen und ändern (ADR-086)."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path
from typing import Any

import pytest

from maildigest.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Hooks, main

CONFIG = """
[general]
language = "de"

[imap]
port = 993

[llm]
provider = "none"

[summarizer]
instructions = "Rechnungen sind wichtig."

[messenger]
active = "telegram"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    path.chmod(0o600)
    return path


def run(
    argv: list[str], *, hooks: Hooks | None = None, interactive: bool = True
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    args = [*argv] if interactive else ["--non-interactive", *argv]
    code = main(args, stdin=io.StringIO(""), stdout=out, stderr=err, hooks=hooks or Hooks())
    return code, out.getvalue(), err.getvalue()


def instructions_in(path: Path) -> str:
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["summarizer"]["instructions"])


def test_anzeigen_ohne_option(config_path: Path) -> None:
    code, out, _err = run(["instructions", "--config", str(config_path)])
    assert code == EXIT_OK
    assert "Custom instructions (24 characters):" in out
    assert "  Rechnungen sind wichtig." in out
    assert "the critic never sees them" in out
    assert "--set" in out and "--edit" in out


def test_anzeigen_ohne_text(config_path: Path) -> None:
    without = CONFIG.replace('instructions = "Rechnungen sind wichtig."', "")
    config_path.write_text(without, encoding="utf-8")
    code, out, _err = run(["instructions", "--config", str(config_path)])
    assert code == EXIT_OK
    assert "Custom instructions: (none)" in out


def test_set_ersetzt(config_path: Path) -> None:
    code, out, _err = run(["instructions", "--config", str(config_path), "--set", "Nur Termine.  "])
    assert code == EXIT_OK
    assert "Saved to" in out
    assert instructions_in(config_path) == "Nur Termine."
    assert "the critic never sees them" not in out  # kein Hinweis im Änderungsmodus


def test_add_haengt_eine_zeile_an(config_path: Path) -> None:
    code, _out, _err = run(
        ["instructions", "--config", str(config_path), "--add", "Werbung ist nie wichtig."]
    )
    assert code == EXIT_OK
    assert instructions_in(config_path) == "Rechnungen sind wichtig.\nWerbung ist nie wichtig."
    # Auf leeren Text wird ohne führenden Zeilenumbruch begonnen.
    run(["instructions", "--config", str(config_path), "--clear"])
    run(["instructions", "--config", str(config_path), "--add", "Erste Zeile"])
    assert instructions_in(config_path) == "Erste Zeile"


def test_clear_entfernt(config_path: Path) -> None:
    code, out, _err = run(["instructions", "--config", str(config_path), "--clear"])
    assert code == EXIT_OK
    assert instructions_in(config_path) == ""
    assert "Custom instructions: (none)" in out


def test_unveraendert_schreibt_nicht(config_path: Path) -> None:
    before = config_path.stat().st_mtime_ns
    code, out, _err = run(
        ["instructions", "--config", str(config_path), "--set", "Rechnungen sind wichtig."]
    )
    assert code == EXIT_OK
    assert out.strip() == "Custom instructions unchanged."
    assert config_path.stat().st_mtime_ns == before


def test_zu_lang_und_steuerzeichen_sind_bedienfehler(config_path: Path) -> None:
    code, _out, err = run(["instructions", "--config", str(config_path), "--set", "x" * 2001])
    assert code == EXIT_USAGE and "too long" in err
    code, _out, err = run(["instructions", "--config", str(config_path), "--set", "a\x1bb"])
    assert code == EXIT_USAGE and "U+001B" in err
    assert instructions_in(config_path) == "Rechnungen sind wichtig."  # nichts gespeichert


def test_crlf_und_tab_werden_normalisiert(config_path: Path) -> None:
    code, _out, _err = run(["instructions", "--config", str(config_path), "--set", "a \r\n\tb\r\n"])
    assert code == EXIT_OK
    assert instructions_in(config_path) == "a\n\tb"


def test_optionen_schliessen_sich_aus(config_path: Path) -> None:
    code, _out, err = run(["instructions", "--config", str(config_path), "--set", "x", "--clear"])
    assert code == EXIT_USAGE
    assert "not allowed with" in err


def test_edit_uebernimmt_den_text_ohne_kommentarzeilen(
    config_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("VISUAL", "fake-editor --wait")
    seen: dict[str, Any] = {}

    def fake_editor(command: list[str]) -> int:
        seen["command"] = command
        scratch = Path(command[-1])
        seen["initial"] = scratch.read_text(encoding="utf-8")
        seen["mode"] = scratch.stat().st_mode & 0o777
        scratch.write_text("# my note\nTermine zuerst.\n\nNewsletter nie.\n", encoding="utf-8")
        return 0

    code, out, _err = run(
        ["instructions", "--config", str(config_path), "--edit"],
        hooks=Hooks(run_editor=fake_editor),
    )
    assert code == EXIT_OK
    assert seen["command"][:2] == ["fake-editor", "--wait"]
    assert seen["initial"].startswith("# Custom instructions for the summarizer")
    assert "Rechnungen sind wichtig." in seen["initial"]
    assert seen["mode"] == 0o600
    assert instructions_in(config_path) == "Termine zuerst.\n\nNewsletter nie."
    assert "Opening fake-editor" in out
    assert not list(config_path.parent.glob(".maildigest-instructions-*"))  # aufgeräumt


def test_edit_mit_editorfehler_speichert_nichts(config_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "broken")
    code, _out, err = run(
        ["instructions", "--config", str(config_path), "--edit"],
        hooks=Hooks(run_editor=lambda cmd: 3),
    )
    assert code == EXIT_ERROR
    assert "status 3" in err
    assert instructions_in(config_path) == "Rechnungen sind wichtig."
    assert not list(config_path.parent.glob(".maildigest-instructions-*"))


def test_edit_braucht_terminal_und_editor(config_path: Path, monkeypatch: Any) -> None:
    code, _out, err = run(
        ["instructions", "--config", str(config_path), "--edit"], interactive=False
    )
    assert code == EXIT_USAGE and "--set" in err
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "")
    code, _out, err = run(["instructions", "--config", str(config_path), "--edit"])
    assert code == EXIT_USAGE and "No editor found" in err


def test_text_erreicht_den_summarizer_prompt(config_path: Path) -> None:
    """Der gespeicherte Text landet im gelabelten Block des System-Prompts (I8)."""
    from maildigest.llm.prompts import summarizer_system_prompt

    run(["instructions", "--config", str(config_path), "--set", "Alles von der Uni ist wichtig."])
    prompt = summarizer_system_prompt(token="T", custom_instructions=instructions_in(config_path))
    assert "Alles von der Uni ist wichtig." in prompt
