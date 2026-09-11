"""HC-14/ADR-083: SPEC-CLI ist der **wörtliche** Vertrag der englischen Ausgabe.

Der Befund HC-14 entstand, weil vier Commits die gesamte Oberfläche auf Englisch umgestellt
haben, ohne dass ein Dokument nachgezogen wurde — sechs Testspuren sind darüber gestolpert.
Damit das nicht erneut still passiert, liest dieser Test die strukturgebenden Zeilen
**maschinell** aus `docs/SPEC-CLI.md` und vergleicht sie mit dem, was Composer und CLI
tatsächlich erzeugen. Wer eine Zeile im Code ändert, ohne die Spec anzufassen (oder
umgekehrt), bekommt hier einen roten Test statt einer stillen Vertragslücke.

Bewusst **kein** Abgleich Wort für Wort über die ganze Datei: Geprüft werden die Anteile,
die ein Cold-Tester Zeile für Zeile vergleicht — die Zeilenanfänge des Nachrichtenformats
aus §6, die fünf Zeilen der Metadaten-Notiz und die `5/5`-Formen aus §4 `test`.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from pathlib import Path

import pytest

from maildigest import cli
from maildigest.models import (
    AttachmentInfo,
    CriticVerdict,
    FailureNotice,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.output.composer import DigestComposer

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "SPEC-CLI.md"


def _spec() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def _block_after(marker: str, *, index: int = 0) -> list[str]:
    """Die Zeilen des `index`-ten Codeblocks nach `marker` in der Spec."""
    text = _spec()
    start = text.index(marker)
    blocks = re.findall(r"```\n(.*?)```", text[start:], flags=re.DOTALL)
    assert len(blocks) > index, f"Kein Codeblock #{index} nach {marker!r} in SPEC-CLI §6/§4"
    return [line for line in blocks[index].splitlines() if line.strip()]


def _prefix(line: str) -> str:
    """Der feste Anteil einer Vertragszeile — alles vor dem ersten Platzhalter."""
    without_comment = re.split(r"\s+←", line)[0]
    return re.split(r"[<]", without_comment)[0]


# --- §6: Zeilenanfänge der normalen Zustellung ------------------------------------------


def _full_message() -> str:
    """Eine Nachricht, in der jede strukturgebende Zeile vorkommt."""
    mail = SanitizedMail(
        dedupe_key="<mail-1@example.org>",
        from_display="Stadtwerke Service",
        from_domain="stadtwerke-x.de",
        subject="Rechnung",
        date=datetime(2026, 8, 28, 14, 12),
        body_text="Bitte zahlen.",
        attachments=[
            AttachmentInfo(
                filename_sanitized="setup.exe",
                declared_mime="application/x-msdownload",
                detected_kind="unknown",
                size_bytes=1_260_000,
                processed=False,
            )
        ],
        sanitization_report=SanitizationReport(
            blocked_attachments=1, truncated=True, reply_to_mismatch=True
        ),
    )
    summary = Summary(
        headline="Rechnung Stadtwerke",
        summary_text="Es geht um eine Rechnung.",
        importance="high",
        attachment_summaries={"notiz.txt": "Zwei Sätze zum Inhalt."},
    )
    verdict = CriticVerdict(
        phishing_risk="high", risk_reasons=["fordert Zugangsdaten"], summary_accurate=True
    )
    return "\n".join(DigestComposer().compose(mail, summary, verdict).parts)


def _spec_line_prefixes() -> list[str]:
    """Die festen Zeilenanfänge des §6-Blocks — Fortsetzungszeilen (eingerückt) zählen nicht."""
    lines = [line for line in _block_after("**Normale Zustellung**") if not line[0].isspace()]
    assert len(lines) == 8, f"§6 beschreibt {len(lines)} Zeilen — Block verändert?"
    prefixes = [_prefix(line).rstrip() for line in lines]
    # Zusammenfassung, Anhangszeile und Fußnote haben keinen festen Anteil.
    return [prefix for prefix in prefixes if prefix and not prefix.startswith("—")]


@pytest.mark.parametrize("prefix", _spec_line_prefixes())
def test_hc14_zeilenanfaenge_der_zustellung_stehen_so_in_der_spec(prefix: str) -> None:
    """Jede feste Zeichenkette aus dem §6-Block steht wörtlich in der Nachricht."""
    assert prefix in _full_message(), (
        f"SPEC §6 verspricht {prefix!r}, die Nachricht hat es nicht"
    )


def test_hc14_wichtig_tag_und_banner_wortlaut() -> None:
    """`[important]` und `⚠️ SUSPECTED PHISHING:` stehen in §6 **und** in der Nachricht."""
    spec = _spec()
    message = _full_message()
    for literal in ("[important]", "⚠️ SUSPECTED PHISHING:", "📎 Not processed:", "🔍 Notes:"):
        assert literal in spec, f"SPEC §6 kennt {literal!r} nicht mehr"
        assert literal in message, f"Die Nachricht enthält {literal!r} nicht"


def test_hc14_hinweistexte_stehen_woertlich_in_der_spec() -> None:
    """Jeder `🔍 Notes:`-Hinweis des Composers ist in §6 wörtlich aufgeführt."""
    spec = _spec()
    source = inspect.getsource(DigestComposer._hints_line)
    for literal in re.findall(r'"([a-zA-Z][^"\\]{12,})"', source):
        if literal.endswith(": ") or "{" in literal:
            continue  # Präfixe mit variablem Anteil stehen separat in §6.
        assert literal in spec, f"Hinweis {literal!r} fehlt in SPEC-CLI §6"


# --- §6: Metadaten-Notiz ----------------------------------------------------------------


def test_hc14_metadaten_notiz_hat_genau_die_fuenf_zeilen_der_spec() -> None:
    """Die fünf Zeilen aus §6 passen Zeile für Zeile auf `compose_failure`."""
    spec_lines = _block_after("**Metadaten-Notiz (fail-closed)**")
    assert len(spec_lines) == 5, "Die Notiz ist als fünfzeilig zugesagt"
    notice = FailureNotice(
        dedupe_key="<mail-9@example.org>",
        from_domain="example.org",
        subject_sanitized="Rechnung",
        stage="sanitize",
        reason_class="sanitize_error",
    )
    parts = DigestComposer().compose_failure(notice).parts
    assert len(parts) == 1
    actual = parts[0].splitlines()
    assert len(actual) == 5
    for spec_line, real_line in zip(spec_lines, actual, strict=True):
        pattern = "^" + re.sub(r"<[^>]+>", "@@", re.escape(spec_line)).replace("@@", ".+")
        # Die Spec zeigt den lebenden Punkt, die Zustellung bricht ihn (ADR-036).
        assert re.match(pattern, real_line.replace("[.]", ".")), (
            f"SPEC §6 sagt {spec_line!r}, zugestellt wird {real_line!r}"
        )


# --- §4 `test`: die 5/5-Formen ----------------------------------------------------------

#: Jede in §4 `test` zugesagte Abschlusszeile mit ihrem festen Anteil.
_STEP_LITERALS = (
    "5/5 Delivered (",
    "5/5 Message created (",
    "5/5 Not delivered (",
    "5/5 Fail-closed: stage ",
    "4/5 Sanitizer: failed.",
    "1/5 Configuration loaded: ",
    "2/5 Test mail read: ",
    "3/5 Pipeline running (sanitizer -> summarizer -> critic -> delivery) ...",
)


@pytest.mark.parametrize("literal", _STEP_LITERALS)
def test_hc14_schrittzeilen_stehen_in_spec_und_code(literal: str) -> None:
    """Jede Schrittzeile kommt wörtlich in SPEC §4 **und** in `cli.py` vor."""
    assert literal in _spec(), f"SPEC-CLI §4 `test` kennt {literal!r} nicht"
    assert literal in inspect.getsource(cli), f"`cli.py` erzeugt {literal!r} nicht"


def test_hc14_fehlerpraefix_und_abbruch() -> None:
    """§2: Fehlerzeilen beginnen mit `Error: `, Strg-C meldet `Error: Aborted.`."""
    spec = _spec()
    assert "beginnt mit `Error: `" in spec
    assert "`Error: Aborted.`" in spec
    assert '"Error: Aborted.\\n"' in inspect.getsource(cli)


def test_hc14_bilanzzeile_und_testnachricht() -> None:
    """Bilanzzeile (§4 `run`) und Testnachricht (§4 `connect-messenger`) sind vertraglich."""
    spec = _spec()
    source = inspect.getsource(cli)
    for literal in (
        "Run finished: ",
        " mails fetched, ",
        "✅ MailDigest test message",
        "Delivery works — your mail summaries will arrive here from now on",
        "🧪 MailDigest self-test",
    ):
        assert literal in spec, f"SPEC-CLI kennt {literal!r} nicht"
        assert literal in source, f"`cli.py` erzeugt {literal!r} nicht"
    # Die Bilanzzeile ist im Code über mehrere f-String-Teile gebrochen; geprüft wird
    # deshalb die vollständige Zeile gegen die Spec und die Bausteine gegen den Code.
    assert (
        "Run finished: N mails fetched, N processed, N duplicates, N errors, "
        "N messages delivered, N queued." in spec
    )
    for fragment in (" processed, ", " duplicates, ", " errors, ", " messages ", " queued."):
        assert fragment in source, f"`cli.py` erzeugt {fragment!r} nicht"


def test_hc14_keine_deutschen_vertragszeilen_mehr() -> None:
    """Die alten deutschen Vertragsliterale stehen weder in der Spec noch im Code."""
    spec = _spec()
    haystack = spec + inspect.getsource(cli)
    for gone in (
        "Fehler: Postfach",
        "Lauf beendet:",
        "5/5 Zugestellt",
        "Trockenlauf, nicht gesendet",
        "🔍 Hinweise:",
        "📎 Nicht verarbeitet:",
        "MailDigest Testnachricht",
        "Abgebrochen.",
    ):
        assert gone not in haystack, f"{gone!r} ist ein Rest der deutschen Fassung (HC-14)"
    # `Von:`/`Betreff:` bleiben **als Erkennungsmuster** erlaubt (CT-8, ADR-062) — sie
    # stehen in `output/sanitizer._RE_STRUCTURE_LABEL` und in §6 als solche beschrieben.
    assert "in beiden Sprachen" in spec
