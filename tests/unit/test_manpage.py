"""Handbuchseite aus dem Parser (ADR-087): keine Drift zwischen `man/maildigest.1` und Code."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from maildigest import __version__
from maildigest.cli import EXIT_OK, build_parser, main
from maildigest.manpage import render_manpage

MAN_FILE = Path(__file__).resolve().parents[2] / "man" / "maildigest.1"


def test_datei_im_repo_entspricht_dem_parser() -> None:
    """Regenerieren mit: maildigest --man > man/maildigest.1"""
    expected = render_manpage(build_parser(), version=__version__)
    assert MAN_FILE.read_text(encoding="utf-8") == expected, (
        "man/maildigest.1 weicht vom Parser ab — mit `maildigest --man > man/maildigest.1` "
        "neu erzeugen."
    )


def test_jedes_kommando_und_jede_option_steht_im_handbuch() -> None:
    text = render_manpage(build_parser())
    parser = build_parser()
    for action in parser._actions:
        for name in action.option_strings:
            assert name.replace("-", "\\-") in text, name
        if hasattr(action, "choices") and action.dest == "command":
            for command, sub in action.choices.items():
                heading = command.replace("-", "\\-")
                assert f'.SS "maildigest {heading}"' in text, command
                for opt in sub._actions:
                    for name in opt.option_strings:
                        assert name.replace("-", "\\-") in text, f"{command} {name}"


def test_option_man_gibt_die_seite_aus() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--man"], stdin=io.StringIO(""), stdout=out, stderr=err)
    assert code == EXIT_OK
    assert out.getvalue().startswith('.\\" Generated from the argparse definition')
    assert ".TH MAILDIGEST 1" in out.getvalue()
    assert err.getvalue() == ""


def test_troff_sonderzeichen_sind_maskiert() -> None:
    text = render_manpage(build_parser())
    for line in text.splitlines():
        # Eine Zeile, die mit `.` beginnt, ist ein Makro — nur bekannte Makros erlaubt.
        if line.startswith("."):
            assert line.split(" ")[0] in {
                ".TH", ".SH", ".SS", ".B", ".I", ".TP", ".PP", ".br", ".nf", ".fi",
                ".\\\"",
            }, line
        assert "\t" not in line


@pytest.mark.skipif(shutil.which("man") is None, reason="kein `man` installiert")
def test_man_rendert_die_seite_ohne_fehler() -> None:
    env = dict(os.environ, MANWIDTH="80", LC_ALL="C.UTF-8")
    result = subprocess.run(
        ["man", "-l", str(MAN_FILE)], capture_output=True, text=True, env=env, check=False,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    assert "MAILDIGEST(1)" in result.stdout
    assert "maildigest instructions" in result.stdout
