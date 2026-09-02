"""Kettentest Sanitizer → Kritiker über die Phishing-Mails des Korpus (`tests/corpus`).

Zwei Rollen für den gemockten Provider:

* :class:`EchoProvider` — maximal feindlich: Er spiegelt **beide** untrusted Blöcke
  (Mail und Zusammenfassung) in alle Textfelder des `CriticVerdict` zurück, also das
  Verhalten eines vollständig durch die Mail übernommenen Modells. Erwartung (I3/I4):
  Das Verdict bleibt schema-valide und enthält keine URL, kein Markup, kein Steuerzeichen.
* :class:`ScoringProvider` — plausibel: Er stuft anhand der Programm-Fakten ein, wie es
  ein funktionierendes Modell täte. Damit wird geprüft, dass die Kette bis zur fertigen
  Nachricht (echter `DigestComposer`) das Warn-Banner erzeugt (F-CRIT-2).

Bewusst nur Mails ohne PDF-Anhang: Die PDF-Extraktion startet einen Subprozess (I7) und
gehört in den Integrationstest des Sanitizers.
"""

from __future__ import annotations

import email
import json
import re
import unicodedata
from email.utils import parseaddr
from pathlib import Path

import pytest

from maildigest.agents.critic import CriticAgent, collect_signals
from maildigest.llm.prompts import block_markers, summary_markers
from maildigest.models import CriticVerdict, RawMail, SanitizedMail, Summary
from maildigest.output.composer import DigestComposer
from maildigest.sanitize import MailSanitizer

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

#: Die in WP6 ergänzten Phishing-Mails (PLAN WP6: ≥ 5 Fälle).
PHISHING_NAMES = (
    "21_phishing_ceo_fraud.eml",
    "22_phishing_parcel.eml",
    "23_phishing_bank_verification.eml",
    "24_phishing_password_reset.eml",
    "25_phishing_invoice_scam.eml",
)

TOKEN = "TESTTOKEN0001"

#: Was in keinem Verdict-Feld stehen darf (analog zum WP5-Korpustest).
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

#: Für die **fertige** Nachricht: defangte Punkte sind dort Politik (ADR-036), lebende
#: Schemata, `www.`, Markdown-Links und Tags bleiben verboten (I3).
MESSAGE_FORBIDDEN_PATTERNS = (
    re.compile(r"https?\s*:", re.IGNORECASE),
    re.compile(r"hxxp", re.IGNORECASE),
    re.compile(r"://"),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"mailto\s*:", re.IGNORECASE),
    re.compile(r"\]\("),
    re.compile(r"</?[a-zA-Z][^>]*>"),
    re.compile(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}"),  # keine Domain mit lebendem Punkt
)


def fixed_token() -> str:
    return TOKEN


def load_raw_mail(path: Path) -> RawMail:
    """Baut aus einer Korpus-`.eml` eine `RawMail` — inklusive der Kritiker-Kopfzeilen.

    Anders als der WP5-Korpustest füllt dieser Loader `reply_to`, `return_path_domain` und
    `auth_results_header`: Genau daraus entstehen die deterministischen Signale (F-CRIT-3).
    """
    data = path.read_bytes()
    message = email.message_from_bytes(data)
    from_addr = str(message.get("From") or "")
    address = parseaddr(from_addr)[1]
    return_path = str(message.get("Return-Path") or "")
    return_domain = parseaddr(return_path)[1]
    auth = message.get_all("Authentication-Results")
    return RawMail(
        message_id=message.get("Message-ID"),
        dedupe_key=message.get("Message-ID") or path.name,
        from_addr=from_addr,
        from_domain=address.rsplit("@", 1)[-1].lower() if "@" in address else "",
        reply_to=message.get("Reply-To"),
        return_path_domain=(
            return_domain.rsplit("@", 1)[-1].lower() if "@" in return_domain else None
        ),
        subject_raw=str(message.get("Subject") or ""),
        auth_results_header="\n".join(str(value) for value in auth) if auth else None,
        mime_bytes=data,
        size_bytes=len(data),
    )


def sanitize_corpus_mail(name: str) -> SanitizedMail:
    """Führt eine Korpus-Mail durch den echten Sanitizer (kein Mock)."""
    return MailSanitizer().sanitize(load_raw_mail(CORPUS_DIR / name))


def plain_summary(mail: SanitizedMail) -> Summary:
    """Eine harmlose, vom Summarizer erzeugbare Zusammenfassung der Mail."""
    return Summary(
        headline=mail.subject[:100] or "Ohne Betreff",
        summary_text=mail.body_text[:400] or "Kein darstellbarer Text.",
        importance="normal",
        importance_reason="Zahlungs- oder Kontoanliegen",
        category="benachrichtigung",
    )


class EchoProvider:
    """Feindliche Attrappe: spiegelt beide untrusted Blöcke in alle Textfelder zurück."""

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
        summary_start, summary_end = summary_markers(TOKEN)
        mail_block = user[user.index(start) + len(start) : user.index(end)].strip()
        summary_block = user[
            user.index(summary_start) + len(summary_start) : user.index(summary_end)
        ].strip()
        echoed = f"{mail_block} {summary_block} {self._extra}".strip()
        return json.dumps(
            {
                "phishing_risk": "none",
                "risk_reasons": [echoed[:600], echoed[600:1200]],
                "summary_accurate": True,
                "notes": echoed,
            },
            ensure_ascii=False,
        )


class ScoringProvider:
    """Plausible Attrappe: bewertet anhand der Programm-Fakten in der User-Message."""

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        facts = user[user.index("PROGRAMM-FAKTEN") : user.index("MAIL-DATEN")]
        reasons = []
        if "nicht bestanden" in facts:
            reasons.append("Absender-Authentifizierung nicht bestanden")
        if "Reply-To" in facts:
            reasons.append("Antwortadresse weicht ab")
        if "Punycode" in facts:
            reasons.append("Absender-Domain in IDN-Schreibweise")
        reasons.append("fordert Zahlung oder Zugangsdaten")
        return json.dumps(
            {
                "phishing_risk": "high",
                "risk_reasons": reasons,
                "summary_accurate": True,
                "notes": "Mehrere Betrugsmuster gleichzeitig.",
            },
            ensure_ascii=False,
        )


def verdict_texts(verdict: CriticVerdict) -> list[str]:
    """Alle Textfelder eines Verdicts — die Prüffläche dieses Tests."""
    return [verdict.notes, *verdict.risk_reasons]


@pytest.mark.parametrize("name", PHISHING_NAMES)
def test_uebernommenes_modell_bringt_nichts_nach_draussen(name: str) -> None:
    """I3/I4: Der Echo-Angriff überlebt die deterministische Nachkontrolle nicht."""
    mail = sanitize_corpus_mail(name)
    agent = CriticAgent(EchoProvider(), token_source=fixed_token)
    verdict = agent.review(mail, plain_summary(mail))

    assert isinstance(verdict, CriticVerdict)
    for text in verdict_texts(verdict):
        for pattern in FORBIDDEN_PATTERNS:
            assert not pattern.search(text), f"{pattern.pattern} in {name}: {text!r}"
        assert all(
            char in "\n\t" or not unicodedata.category(char).startswith("C") for char in text
        )
        assert "\n" not in text  # Verdict-Felder sind einzeilig


@pytest.mark.parametrize("name", PHISHING_NAMES)
def test_echo_angriff_wird_als_risiko_sichtbar(name: str) -> None:
    """Was gesäubert werden musste, wird gemeldet statt still verschwiegen (ADR-043)."""
    mail = sanitize_corpus_mail(name)
    provider = EchoProvider(extra="Klicke https://boese.example/login")
    agent = CriticAgent(provider, token_source=fixed_token)
    verdict = agent.review(mail, plain_summary(mail))
    assert verdict.phishing_risk != "none"
    assert any("entfernt" in reason for reason in verdict.risk_reasons)


@pytest.mark.parametrize(
    ("name", "expected_keys"),
    [
        ("21_phishing_ceo_fraud.eml", {"reply_to_mismatch"}),
        ("22_phishing_parcel.eml", {"return_path_mismatch", "auth_failed", "hidden_text"}),
        ("23_phishing_bank_verification.eml", {"punycode", "auth_failed"}),
        (
            "24_phishing_password_reset.eml",
            {"reply_to_mismatch", "return_path_mismatch", "auth_failed"},
        ),
        ("25_phishing_invoice_scam.eml", {"return_path_mismatch", "blocked_attachments"}),
    ],
)
def test_korpusmails_liefern_die_erwarteten_signale(name: str, expected_keys: set[str]) -> None:
    """F-CRIT-3 über die echte Kette: Sanitizer-Protokoll → Signale, ohne LLM."""
    mail = sanitize_corpus_mail(name)
    assert expected_keys <= {signal.key for signal in collect_signals(mail)}


def test_unauffaellige_korpusmail_hat_keine_alarmsignale() -> None:
    """Gegenprobe: Eine normale Mail erzeugt keine Mismatch-/Auth-Fehler-Fakten."""
    mail = sanitize_corpus_mail("01_multipart_plain_html.eml")
    keys = {signal.key for signal in collect_signals(mail)}
    assert "auth_ok" in keys
    assert not keys & {"reply_to_mismatch", "return_path_mismatch", "auth_failed", "punycode"}


def test_kette_bis_zur_fertigen_nachricht_zeigt_das_warn_banner() -> None:
    """F-CRIT-2 über die Changeset-Grenze: Kritiker-Verdict → echter `DigestComposer`."""
    mail = sanitize_corpus_mail("23_phishing_bank_verification.eml")
    summary = plain_summary(mail)
    verdict = CriticAgent(ScoringProvider(), token_source=fixed_token).review(mail, summary)

    assert verdict.phishing_risk == "high"
    message = DigestComposer().compose(mail, summary, verdict)
    assert message.is_warning is True
    assert message.parts[0].startswith("⚠️ PHISHING-VERDACHT:")
    # In der fertigen Nachricht sind defangte Formen (`beispiel[.]example`) ausdrücklich
    # erwünscht (ADR-036) — verboten bleibt alles, was ein Messenger verlinken könnte.
    for part in message.parts:
        for pattern in MESSAGE_FORBIDDEN_PATTERNS:
            assert not pattern.search(part), f"{pattern.pattern} in Nachricht: {part!r}"
