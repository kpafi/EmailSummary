"""Automatisierter Invarianten-Check I1-I8 über die Codebasis (WP12, SECURITY.md §7).

Dieser Test ist die maschinelle Hälfte des Invarianten-Reviews: Er hält die Aussagen aus
docs/SECURITY.md §7 fest, damit eine spätere Änderung sie nicht unbemerkt aufhebt. Die
inhaltliche Prüfung (Lesen der betroffenen Stellen) steht im Review-Text selbst — hier
stehen nur die Bedingungen, die sich mechanisch nachweisen lassen.

Bewusst quellcodebasiert und nicht über Importe: Eine Regel wie „das Wort `expunge` kommt
in keinem ausführbaren Codepfad vor" lässt sich nur am Text prüfen. Kommentarzeilen werden
deshalb vor der Suche entfernt — die Begründungen *nennen* die verbotenen Konstrukte.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "maildigest"

#: Alle Produktionsmodule.
PY_FILES = sorted(SRC.rglob("*.py"))


def code_of(path: Path) -> str:
    """Quelltext ohne Kommentare und ohne Docstrings.

    Die Modul-Dokumentation von `ingest/imap_client.py` erklärt ausführlich, warum
    `MailBox.delete()` nicht benutzt wird — eine reine Textsuche würde daran hängen
    bleiben. Übrig bleibt, was der Interpreter tatsächlich ausführt.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0] = ast.Expr(value=ast.Constant(value=""))
    return ast.unparse(tree)


ALL_CODE = {path: code_of(path) for path in PY_FILES}


def offenders(pattern: str) -> list[str]:
    """Alle `datei:zeile` mit einem Treffer des Musters im ausführbaren Code."""
    regex = re.compile(pattern)
    hits: list[str] = []
    for path, code in ALL_CODE.items():
        for number, line in enumerate(code.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    return hits


# --- I2: Text-in/Text-out, keine Werkzeuge ----------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        r"\btools\b",
        r"\btool_choice\b",
        r"\bfunctions\b",
        r"\bfunction_call\b",
        r"\bmcp_servers\b",
        r"\bparse_mode\b",
        r"\bembeds\b",
    ],
)
def test_i2_i3_keine_werkzeuge_und_kein_formatierungsmodus(pattern: str) -> None:
    """Weder LLM-Werkzeuge (I2) noch Messenger-Formatierungsmodi (I3) im Code."""
    assert offenders(pattern) == []


def test_i2_llm_payload_hat_nur_erlaubte_felder() -> None:
    """Die Request-Körper beider Provider bestehen aus einem geschlossenen Feldsatz."""
    erlaubt = {"model", "max_tokens", "system", "messages", "temperature"}
    for name in ("anthropic.py", "openai.py"):
        code = ALL_CODE[SRC / "llm" / name]
        tree = ast.parse(code)
        payloads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            and all(isinstance(key, ast.Constant) for key in node.keys)
            and {key.value for key in node.keys if isinstance(key, ast.Constant)} & {"model"}
        ]
        assert payloads, f"Kein Request-Körper in {name} gefunden."
        for payload in payloads:
            keys = {key.value for key in payload.keys if isinstance(key, ast.Constant)}
            assert keys <= erlaubt, f"{name}: unerwartete Felder {keys - erlaubt}"


# --- I5 / Betriebssicherheit: TLS immer geprüft -----------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        r"verify\s*=\s*False",
        r"CERT_NONE",
        r"check_hostname\s*=\s*False",
        r"_create_unverified_context",
    ],
)
def test_tls_pruefung_wird_nirgends_abgeschaltet(pattern: str) -> None:
    """SECURITY §6: kein `verify=False` irgendwo — der in WP12 angekündigte Lint-Check."""
    assert offenders(pattern) == []


# --- F-ING-1 / ADR-064: kein Löschpfad auf dem Postfach ---------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        r"\bexpunge\b",
        r"\bEXPUNGE\b",
        r"\\\\Deleted",
        r"\.delete\s*\(",
        r"\.flag\s*\(",
        r"\.move\s*\(",
    ],
)
def test_kein_loeschpfad_im_postfach(pattern: str) -> None:
    """Weder `EXPUNGE` noch die expungenden `imap_tools`-Bequemlichkeiten (ADR-064)."""
    assert offenders(pattern) == []


def test_nur_zwei_schreibende_imap_kommandos() -> None:
    """Genau `UID STORE +FLAGS (\\Seen)` und `UID MOVE` (SPEC-CLI §7.1)."""
    code = ALL_CODE[SRC / "ingest" / "imap_client.py"]
    kommandos = set(re.findall(r"""_uid_command\(\s*['"]([A-Z]+)['"]""", code))
    assert kommandos == {"STORE", "MOVE"}
    assert re.search(r"""_uid_command\([^)]*['"]\+FLAGS['"][^)]*\\\\Seen""", code)


# --- I1: `mime_bytes` erreicht keine Stufe nach dem Sanitizer ---------------------------


def test_i1_mime_bytes_nur_in_ingest_sanitize_und_modell() -> None:
    """Nach der Sanitize-Stufe gibt es keinen Zugriff mehr auf die Rohbytes."""
    erlaubt = {
        Path("models.py"),  # Felddefinition auf RawMail
        Path("ingest/imap_client.py"),  # erzeugt RawMail
        Path("sanitize/sanitizer.py"),  # einzige lesende Stelle
    }
    treffer = {
        Path(hit.split(":")[0]) for hit in offenders(r"\bmime_bytes\b")
    }
    assert treffer <= erlaubt, f"mime_bytes außerhalb der Sanitize-Grenze: {treffer - erlaubt}"


# --- I7: PDF-Extraktion nur im Subprozess mit Limits -------------------------------------


def test_i7_pdfminer_wird_nur_im_kindprozess_importiert() -> None:
    """`pdfminer` erscheint in keinem Modul, das im Elternprozess läuft."""
    treffer = {Path(hit.split(":")[0]) for hit in offenders(r"pdfminer")}
    assert treffer <= {Path("sanitize/extract_pdf.py")}


def test_i7_subprozess_hat_zeit_und_speicherlimit() -> None:
    """Timeout im Elternprozess, `RLIMIT_AS` im Kind — beides vorhanden."""
    code = ALL_CODE[SRC / "sanitize" / "extract_pdf.py"]
    assert "subprocess.run(" in code
    assert "timeout=timeout_seconds" in code
    assert "resource.setrlimit(resource.RLIMIT_AS" in code


# --- I8: Custom-Instructions stehen vor den Sicherheitsregeln ---------------------------


def test_i8_reihenfolge_im_system_prompt() -> None:
    """Rolle → Nutzer-Vorgaben → unüberschreibbare Sicherheitsregeln."""
    from maildigest.llm.prompts import summarizer_system_prompt

    system = summarizer_system_prompt(
        token="ABC123", custom_instructions="Rechnungen sind wichtig."
    )
    position_vorgaben = system.index("Rechnungen sind wichtig.")
    position_regeln = system.index("UNÜBERSCHREIBBARE SICHERHEITSREGELN")
    assert position_vorgaben < position_regeln


def test_i8_kritiker_sieht_die_nutzer_vorgaben_nicht() -> None:
    """Der Kritiker-Prompt trägt keine Custom-Instructions (ADR-042)."""
    from maildigest.llm.prompts import critic_system_prompt

    assert "custom_instructions" not in critic_system_prompt.__code__.co_varnames
