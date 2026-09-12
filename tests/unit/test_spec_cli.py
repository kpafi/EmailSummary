"""Gleicht `docs/SPEC-CLI.md` gegen die tatsächliche argparse-Struktur ab (WP9).

Die Spezifikation ist der Vertrag für den Cold-Tester (docs/TESTING.md §3): Was dort
steht, muss existieren — und was existiert, muss dort stehen. Beides prüft dieser Test
maschinell, damit die Dokumentation nicht still veraltet.

Verankerung im Dokument:

* Überschrift ``### `maildigest <kommando>` `` je Kommando, plus ``## 3. Globale Optionen``.
* Optionen stehen in Tabellenzeilen, die mit ``| `--option` |`` beginnen. Nur solche
  Zeilen werden ausgewertet — Fließtext darf Optionen also frei erwähnen.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from maildigest.cli import build_parser

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "SPEC-CLI.md"

#: Optionen, die jedes Kommando erbt und die nur im globalen Abschnitt stehen.
GLOBAL_OPTIONS = {"--config", "--non-interactive", "--help", "--man"}

_COMMAND_HEADING = re.compile(r"^### `maildigest ([a-z-]+)`\s*$")
_GLOBAL_HEADING = re.compile(r"^## 3\. Globale Optionen\s*$")
_ANY_HEADING = re.compile(r"^#{2,3} ")
_TABLE_OPTION = re.compile(r"^\|\s*`(--[a-z0-9-]+)`")


def _spec_sections() -> dict[str, set[str]]:
    """Liest je Abschnitt die in Optionstabellen dokumentierten Optionen."""
    sections: dict[str, set[str]] = {}
    current: str | None = None
    for line in SPEC_PATH.read_text(encoding="utf-8").splitlines():
        heading = _COMMAND_HEADING.match(line)
        if heading:
            current = heading.group(1)
            sections.setdefault(current, set())
            continue
        if _GLOBAL_HEADING.match(line):
            current = "__global__"
            sections.setdefault(current, set())
            continue
        if _ANY_HEADING.match(line):
            current = None
            continue
        option = _TABLE_OPTION.match(line)
        if option and current is not None:
            sections[current].add(option.group(1))
    return sections


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    """Alle Unterkommandos des echten Parsers."""
    parser = build_parser()
    action = next(item for item in parser._actions if item.dest == "command")
    return dict(action.choices)  # type: ignore[attr-defined]


def _options_of(parser: argparse.ArgumentParser) -> set[str]:
    """Alle langen Optionen eines Parsers."""
    return {
        name
        for action in parser._actions
        for name in action.option_strings
        if name.startswith("--")
    }


SPEC = _spec_sections()
COMMANDS = _subparsers()


def test_spezifikation_existiert() -> None:
    assert SPEC_PATH.is_file()


def test_alle_kommandos_sind_dokumentiert() -> None:
    dokumentiert = {name for name in SPEC if name != "__global__"}
    assert dokumentiert == set(COMMANDS), (
        "Kommandos in SPEC-CLI.md und im Parser stimmen nicht überein: "
        f"nur in der Spec {dokumentiert - set(COMMANDS)}, "
        f"nur im Code {set(COMMANDS) - dokumentiert}"
    )


def test_globale_optionen_sind_dokumentiert() -> None:
    assert SPEC["__global__"] == GLOBAL_OPTIONS


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_optionen_stimmen_mit_der_spezifikation_ueberein(command: str) -> None:
    im_code = _options_of(COMMANDS[command]) - GLOBAL_OPTIONS
    in_der_spec = SPEC[command]
    assert in_der_spec == im_code, (
        f"`maildigest {command}`: nur in der Spec {in_der_spec - im_code}, "
        f"nur im Code {im_code - in_der_spec}"
    )


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_kein_kommando_dokumentiert_globale_optionen_doppelt(command: str) -> None:
    assert not (SPEC[command] & GLOBAL_OPTIONS)


def test_alle_exit_codes_sind_beschrieben() -> None:
    text = SPEC_PATH.read_text(encoding="utf-8")
    for code in ("| 0 |", "| 1 |", "| 2 |"):
        assert code in text


def test_alle_config_felder_stehen_in_der_referenz() -> None:
    """Jedes Feld des Config-Schemas muss in der Feldtabelle auftauchen (NF-3/NF-7)."""
    from maildigest.config import Config

    text = SPEC_PATH.read_text(encoding="utf-8")
    fehlend: list[str] = []
    for section_name, section_field in Config.model_fields.items():
        model = section_field.annotation
        for field_name, sub in getattr(model, "model_fields", {}).items():
            sub_model = getattr(sub.annotation, "model_fields", None)
            if sub_model is not None:
                for leaf in sub_model:
                    if f"`[{section_name}.{field_name}] {leaf}`" not in text:
                        fehlend.append(f"[{section_name}.{field_name}] {leaf}")
                continue
            if f"`[{section_name}] {field_name}`" not in text:
                fehlend.append(f"[{section_name}] {field_name}")
    assert not fehlend, f"In SPEC-CLI.md fehlen: {fehlend}"


def test_umgebungsvariablen_sind_dokumentiert() -> None:
    from maildigest.cli import ENV_CONFIG
    from maildigest.config import ENV_IMAP_PASSWORD, ENV_LLM_API_KEY, ENV_TELEGRAM_TOKEN

    text = SPEC_PATH.read_text(encoding="utf-8")
    for name in (ENV_CONFIG, ENV_IMAP_PASSWORD, ENV_LLM_API_KEY, ENV_TELEGRAM_TOKEN):
        assert name in text


# --- HC-18: `init` schreibt den vollständigen Feldsatz aus §5 ---------------------------

_FIELD_ROW = re.compile(r"^\|\s*`\[([a-z.]+)\]\s+([a-z_]+)`\s*\|([^|]*)\|([^|]*)\|")

#: Default-Zellen, die kein Literal sind: „—" (kein Default) und „erbt" (Override).
_NO_DEFAULT = {"—", "erbt"}


def _spec_fields() -> list[tuple[str, str, str]]:
    """Liest die Feldtabelle aus §5: (Sektionspfad, Feld, Default-Zelle)."""
    rows = [
        (m.group(1), m.group(2), m.group(4).strip())
        for line in SPEC_PATH.read_text(encoding="utf-8").splitlines()
        if (m := _FIELD_ROW.match(line))
    ]
    assert len(rows) > 30, "Die Feldtabelle aus §5 wurde nicht gefunden"
    return rows


def _default_value(cell: str) -> object:
    """Macht aus der Default-Zelle (`` `993` ``) den TOML-Wert."""
    import tomllib as _tomllib

    return _tomllib.loads(f"x = {cell.strip('`')}")["x"]


def _lookup(data: dict[str, object], path: str, key: str) -> tuple[bool, object]:
    """Sucht `key` unter dem punktierten Sektionspfad; liefert (gefunden, Wert)."""
    node: object = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    if not isinstance(node, dict) or key not in node:
        return False, None
    return True, node[key]


def _commented_lines(text: str) -> set[str]:
    """Alle auskommentierten Feldzeilen der erzeugten Datei als `sektion.feld`."""
    section = ""
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        commented = re.match(r"^#\s*([a-z_]+)\s*=", stripped)
        if commented and section:
            found.add(f"{section}.{commented.group(1)}")
    return found


def test_hc18_init_schreibt_den_vollstaendigen_feldsatz(tmp_path: Path) -> None:
    """HC-18: Jedes Feld aus §5 steht in der Vorlage — mit Wert oder als Kommentar.

    Der Test liest die §5-Tabelle **maschinell**; eine zweite, von Hand gepflegte Liste
    würde genauso still veralten wie `_KEY_ORDER` es getan hat (`accept_commands` stand
    dort, wurde aber nie geschrieben).
    """
    import io
    import tomllib

    from maildigest.cli import EXIT_OK, main

    target = tmp_path / "config.toml"
    code = main(
        ["--config", str(target), "--non-interactive", "init"],
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == EXIT_OK

    text = target.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    commented = _commented_lines(text)

    fehlend: list[str] = []
    abweichend: list[str] = []
    for path, key, cell in _spec_fields():
        found, value = _lookup(data, path, key)
        if cell in _NO_DEFAULT:
            # Ohne Default: als auskommentierte Beispielzeile, nicht als echter Wert.
            if not found and f"{path}.{key}" not in commented:
                fehlend.append(f"[{path}] {key} (Kommentarzeile)")
            continue
        if not found:
            fehlend.append(f"[{path}] {key}")
            continue
        expected = _default_value(cell)
        if value != expected:
            abweichend.append(f"[{path}] {key}: Datei {value!r}, Spec {expected!r}")

    assert not fehlend, f"`init` schreibt diese Felder aus §5 nicht: {fehlend}"
    assert not abweichend, f"Defaults weichen von §5 ab: {abweichend}"
