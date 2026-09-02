"""Kettentest Sanitizer → Summarizer über Mails aus dem WP3-Korpus (`tests/corpus`).

Der Provider ist eine Attrappe, die sich maximal feindlich verhält: Sie **echot** den
untrusted Datenblock in alle Textfelder der `Summary` zurück — das Verhalten eines
vollständig durch die Mail übernommenen Modells. Erwartung (PLAN WP5, I3/I4): Für jede
Korpus-Mail entsteht trotzdem eine schema-valide `Summary` ohne URL, ohne HTML-Tag und
ohne Steuerzeichen.

Bewusst nur Korpus-Mails ohne PDF-Anhang: Die PDF-Extraktion startet einen Subprozess
(I7) und gehört in den Integrationstest des Sanitizers, nicht in einen Unit-Test des
Summarizers.
"""

from __future__ import annotations

import email
import email.header
import json
import re
import unicodedata
from email.utils import parseaddr
from pathlib import Path

import pytest

from maildigest.agents.summarizer import SummarizerAgent
from maildigest.llm.prompts import block_markers
from maildigest.models import RawMail, SanitizedMail, Summary
from maildigest.sanitize import MailSanitizer

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

#: Korpus-Mails, die ohne Subprozess auskommen (multipart, geblockter Anhang,
#: obfuskierte Links, Zero-Width-Injection, versteckter Text).
CORPUS_NAMES = (
    "01_multipart_plain_html.eml",
    "04_attachment_docx_blocked.eml",
    "10_obfuscated_links.eml",
    "11_zero_width_injection.eml",
    "14_hidden_text_injection.eml",
)

TOKEN = "TESTTOKEN0001"

#: Was in keinem Feld der `Summary` stehen darf. Der bloße Domain-Name in einem
#: `[Link #n: domain.tld]`-Marker ist laut I3 erlaubt und deshalb nicht gelistet.
FORBIDDEN_PATTERNS = (
    re.compile(r"https?\s*:", re.IGNORECASE),
    re.compile(r"hxxp", re.IGNORECASE),
    re.compile(r"://"),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"mailto\s*:", re.IGNORECASE),
    re.compile(r"\(\s*\.\s*\)"),
    re.compile(r"\[\s*\.\s*\]"),
    re.compile(r"</?[a-zA-Z][^>]*>"),
)


def load_raw_mail(path: Path) -> RawMail:
    """Baut aus einer Korpus-`.eml` eine `RawMail` (vereinfacht, wie im Ingest)."""
    data = path.read_bytes()
    message = email.message_from_bytes(data)
    from_addr = str(message.get("From") or "")
    address = parseaddr(from_addr)[1]
    return RawMail(
        message_id=message.get("Message-ID"),
        dedupe_key=message.get("Message-ID") or path.name,
        from_addr=from_addr,
        from_domain=address.rsplit("@", 1)[-1].lower() if "@" in address else "",
        subject_raw=str(message.get("Subject") or ""),
        mime_bytes=data,
        size_bytes=len(data),
    )


def sanitize_corpus_mail(name: str) -> SanitizedMail:
    """Führt eine Korpus-Mail durch den echten Sanitizer (kein Mock)."""
    return MailSanitizer().sanitize(load_raw_mail(CORPUS_DIR / name))


class EchoProvider:
    """Feindliche Attrappe: spiegelt den untrusted Datenblock in alle Textfelder zurück.

    Zusätzlich lässt sich über `extra` Text einschmuggeln, den ein übernommenes Modell
    erfinden würde (z. B. eine rekonstruierte URL).
    """

    def __init__(self, extra: str = "") -> None:
        self._extra = extra
        self.users: list[str] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        self.users.append(user)
        start, end = block_markers(TOKEN)
        block = user[user.index(start) + len(start) : user.index(end)].strip()
        echoed = f"{block} {self._extra}".strip()
        return json.dumps(
            {
                "headline": echoed[:100],
                "summary_text": echoed,
                "importance": "normal",
                "importance_reason": echoed[:200],
                "category": echoed[:40],
                "attachment_summaries": {},
                "injection_suspected": False,
            },
            ensure_ascii=False,
        )


def summary_texts(summary: Summary) -> list[str]:
    """Alle Textfelder einer `Summary` — die Prüffläche dieses Tests."""
    return [
        summary.headline,
        summary.summary_text,
        summary.importance_reason,
        summary.category,
        *summary.attachment_summaries.keys(),
        *summary.attachment_summaries.values(),
    ]


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_corpus_mail_yields_a_clean_schema_valid_summary(name: str) -> None:
    """Für jede Korpus-Mail entsteht eine schema-valide, link- und tagfreie `Summary`."""
    mail = sanitize_corpus_mail(name)
    agent = SummarizerAgent(EchoProvider(), token_source=lambda: TOKEN)  # type: ignore[arg-type]
    summary = agent.summarize(mail)

    Summary.model_validate(summary.model_dump())
    assert len(summary.headline) <= 100
    for field in summary_texts(summary):
        for pattern in FORBIDDEN_PATTERNS:
            assert not pattern.search(field), f"{name}: {pattern.pattern} in {field!r}"
        for char in field:
            assert char in "\n\t" or not unicodedata.category(char).startswith("C")


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_corpus_mail_content_reaches_the_untrusted_block_only(name: str) -> None:
    """Der Mail-Inhalt steht ausschließlich im delimitierten Block der User-Message."""
    mail = sanitize_corpus_mail(name)
    provider = EchoProvider()
    SummarizerAgent(provider, token_source=lambda: TOKEN).summarize(mail)  # type: ignore[arg-type]

    user = provider.users[0]
    start, end = block_markers(TOKEN)
    block = user[user.index(start) + len(start) : user.index(end)]
    assert mail.subject in block
    if mail.body_text.strip():
        assert mail.body_text.strip().splitlines()[0] in block


def test_reconstructed_url_from_a_corpus_mail_is_removed_and_flagged() -> None:
    """Ein Modell, das die Original-URL „rekonstruiert", kommt damit nicht durch (I3/I4)."""
    mail = sanitize_corpus_mail("10_obfuscated_links.eml")
    provider = EchoProvider(extra="Vollstaendig: https://phish.example/login jetzt oeffnen.")
    summary = SummarizerAgent(provider, token_source=lambda: TOKEN).summarize(mail)  # type: ignore[arg-type]

    assert "phish.example/login" not in summary.summary_text
    assert "://" not in summary.summary_text
    assert summary.injection_suspected is True
