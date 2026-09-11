"""Unit-Tests für die deterministische Hälfte des Kritikers (WP6) — **ohne LLM**.

Zwei Schwerpunkte:

* :func:`collect_signals` (F-CRIT-3): Aus dem `SanitizationReport` müssen genau die
  Fakten entstehen, die der Prompt als vertrauenswürdig ausweist. Kein Aufruf, kein
  Netz, keine Attrappe — reiner Code.
* :func:`enforce_verdict_policy` (I4): Die Nachkontrolle der Verdict-Textfelder säubert
  URLs/Markup, kappt Listen und Längen, hebt die Risikostufe an, senkt sie aber nie.
"""

from __future__ import annotations

import pytest

from maildigest.agents.critic import (
    MAX_NOTES_CHARS,
    MAX_REASON_CHARS,
    MAX_REASONS,
    Signal,
    collect_signals,
    enforce_verdict_policy,
    signal_lines,
)
from maildigest.models import (
    AttachmentInfo,
    CriticVerdict,
    SanitizationReport,
    SanitizedMail,
)


def make_mail(
    *, report: SanitizationReport | None = None, attachments: list[AttachmentInfo] | None = None
) -> SanitizedMail:
    """Minimale `SanitizedMail` mit steuerbarem Sanitizer-Protokoll."""
    return SanitizedMail(
        dedupe_key="key-1",
        from_display="Absender Test",
        from_domain="absender.example",
        subject="Betreff",
        body_text="Text der Mail.",
        attachments=attachments or [],
        sanitization_report=report or SanitizationReport(),
    )


def keys(mail: SanitizedMail) -> list[str]:
    """Die Signal-Schlüssel einer Mail in Reihenfolge."""
    return [signal.key for signal in collect_signals(mail)]


# --- collect_signals -------------------------------------------------------------------


def test_unauffaellige_mail_liefert_nur_basisfakten() -> None:
    """Ohne Auffälligkeit bleiben Auth-Status und Link-Zähler — nie ein leerer Block."""
    assert keys(make_mail()) == ["auth_missing", "links_removed"]


def test_reply_to_und_return_path_mismatch_werden_gemeldet() -> None:
    report = SanitizationReport(reply_to_mismatch=True, return_path_mismatch=True)
    assert keys(make_mail(report=report))[:2] == ["reply_to_mismatch", "return_path_mismatch"]


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ({"spf": "pass", "dkim": "pass", "dmarc": "pass"}, "auth_ok"),
        ({"spf": "pass", "dkim": "none", "dmarc": "none"}, "auth_ok"),
        ({"spf": "fail"}, "auth_failed"),
        ({"spf": "pass", "dmarc": "fail"}, "auth_failed"),
        ({"spf": "softfail"}, "auth_failed"),
        ({}, "auth_missing"),
    ],
)
def test_auth_results_werden_klassifiziert(results: dict[str, str], expected: str) -> None:
    """`pass/none/neutral/policy` gelten als bestanden, alles andere wird gemeldet."""
    mail = make_mail(report=SanitizationReport(auth_results=results))
    assert expected in keys(mail)


def test_auth_zeile_nennt_alle_werte_sortiert() -> None:
    mail = make_mail(
        report=SanitizationReport(auth_results={"spf": "fail", "dkim": "pass", "dmarc": "fail"})
    )
    line = next(s.text for s in collect_signals(mail) if s.key == "auth_failed")
    assert "DKIM=pass, DMARC=fail, SPF=fail" in line
    assert "failed: DMARC, SPF" in line


def test_spf_fail_ist_kein_hartes_signal() -> None:
    """Die Weiterleitung ins Spiegelpostfach bricht SPF/DKIM systematisch (ADR-043)."""
    mail = make_mail(report=SanitizationReport(auth_results={"spf": "fail", "dkim": "fail"}))
    assert not any(signal.hard for signal in collect_signals(mail))


def test_ct11_punycode_und_mixed_script_sind_hart() -> None:
    """Beides ist durch eine Weiterleitung nicht erklärbar (CT-11).

    Vorher galt Punycode nur als Faktum: `hard=False`. Eine Weiterleitung ins
    Spiegelpostfach bricht aber SPF/DKIM — sie schreibt keine Absender-Domain in
    IDN-Schreibweise um. Genau darin unterscheidet sich das Signal von `auth_failed`.
    """
    mail = make_mail(
        report=SanitizationReport(
            punycode_domains=["xn--test-3ya[.]example"],
            mixed_script_domains=["раypal[.]example"],
        )
    )
    signals = {signal.key: signal for signal in collect_signals(mail)}
    assert signals["punycode"].hard is True
    assert signals["mixed_script"].hard is True


def test_domainliste_wird_gekappt_aber_die_anzahl_genannt() -> None:
    mail = make_mail(
        report=SanitizationReport(punycode_domains=[f"xn--d{i}[.]example" for i in range(9)])
    )
    line = next(s.text for s in collect_signals(mail) if s.key == "punycode")
    assert "(9)" in line
    assert line.count("xn--") == 3


def test_geblockte_anhaenge_mit_deklarierten_typen() -> None:
    attachments = [
        AttachmentInfo(
            filename_sanitized="zahlung.docx",
            declared_mime="application/vnd.ms-word",
            detected_kind="unknown",
            size_bytes=1024,
            processed=False,
        ),
        AttachmentInfo(
            filename_sanitized="ok.pdf",
            declared_mime="application/pdf",
            detected_kind="pdf",
            size_bytes=2048,
            processed=True,
        ),
    ]
    mail = make_mail(
        report=SanitizationReport(blocked_attachments=1), attachments=attachments
    )
    line = next(s.text for s in collect_signals(mail) if s.key == "blocked_attachments")
    assert "1" in line
    assert "application/vnd.ms-word" in line
    assert "application/pdf" not in line  # verarbeitete Anhänge gehören nicht in das Signal


def test_weitere_sanitizer_fakten_erscheinen() -> None:
    report = SanitizationReport(
        links_removed=4,
        hidden_text_removed=True,
        control_chars_removed=7,
        truncated=True,
    )
    found = keys(make_mail(report=report))
    assert {"links_removed", "hidden_text", "control_chars", "truncated"} <= set(found)


def test_signal_lines_liefert_genau_die_wortlaute() -> None:
    mail = make_mail(report=SanitizationReport(reply_to_mismatch=True))
    signals = collect_signals(mail)
    assert signal_lines(signals) == tuple(signal.text for signal in signals)


def test_signale_enthalten_keinen_mailtext() -> None:
    """Die Fakten sind Code-Fakten (T9): Sie dürfen keinen Mail-Inhalt transportieren."""
    mail = make_mail(report=SanitizationReport(links_removed=2))
    joined = " ".join(signal_lines(collect_signals(mail)))
    assert "Text der Mail" not in joined
    assert "Betreff" not in joined


# --- enforce_verdict_policy ------------------------------------------------------------


def verdict(**kwargs: object) -> CriticVerdict:
    """Verdict mit Standardwerten, wie es aus `complete_json` käme."""
    data: dict[str, object] = {
        "phishing_risk": "none",
        "risk_reasons": [],
        "summary_accurate": True,
        "notes": "",
    }
    data.update(kwargs)
    return CriticVerdict.model_validate(data)


def test_sauberes_verdict_bleibt_unveraendert() -> None:
    result = enforce_verdict_policy(
        verdict(phishing_risk="low", risk_reasons=["fordert Zugangsdaten"], notes="Vorsicht.")
    )
    assert result.phishing_risk == "low"
    assert result.risk_reasons == ["fordert Zugangsdaten"]
    assert result.notes == "Vorsicht."


@pytest.mark.parametrize(
    "payload",
    [
        "Klicke https://boese.example/login",
        "[Anmelden](https://boese.example)",
        "<a href='x'>Anmelden</a>",
        "www.boese.example",
        "Kontakt mailto:opfer@boese.example",
        "Zero​Width",
    ],
)
def test_nachkontrolle_saeubert_und_hebt_das_risiko(payload: str) -> None:
    """Ein Fund wird entfernt, sichtbar gemacht und hebt die Stufe auf mindestens `low`."""
    result = enforce_verdict_policy(verdict(risk_reasons=[payload]))
    joined = " ".join(result.risk_reasons) + result.notes
    assert "boese" not in joined
    assert "://" not in joined and "www." not in joined and "<a" not in joined
    assert "​" not in joined
    assert result.phishing_risk == "low"
    assert any("removed" in reason for reason in result.risk_reasons)


def test_fullwidth_schema_wird_ebenfalls_erkannt() -> None:
    """NFKC vor dem Scan schließt die Fullwidth-Lücke der WP5-Schicht (ADR-044)."""
    result = enforce_verdict_policy(verdict(notes="Siehe ｈｔｔｐｓ://x.example"))
    assert "://" not in result.notes
    assert result.phishing_risk == "low"


def test_notes_werden_gesaeubert_und_gekappt() -> None:
    result = enforce_verdict_policy(verdict(notes="A" * (MAX_NOTES_CHARS + 200)))
    assert len(result.notes) == MAX_NOTES_CHARS


def test_gruende_werden_gekappt_entdoppelt_und_einzeilig() -> None:
    result = enforce_verdict_policy(
        verdict(
            phishing_risk="high",
            risk_reasons=["a" * 400, "doppelt", "doppelt", "  mehr\nzeilig  ", "x", "y", "z"],
        )
    )
    assert len(result.risk_reasons) == MAX_REASONS
    assert len(result.risk_reasons[0]) == MAX_REASON_CHARS
    assert result.risk_reasons.count("doppelt") == 1
    assert "mehr zeilig" in result.risk_reasons


def test_leere_gruende_fliegen_raus_und_risiko_bekommt_platzhalter() -> None:
    result = enforce_verdict_policy(verdict(phishing_risk="high", risk_reasons=["   ", ""]))
    assert result.risk_reasons == ["the critic reports a risk without giving a reason"]


def test_hartes_signal_hebt_none_auf_low() -> None:
    signals = (Signal("mixed_script", "Homoglyphen-Domain erkannt", hard=True),)
    result = enforce_verdict_policy(verdict(phishing_risk="none"), signals)
    assert result.phishing_risk == "low"
    assert "Homoglyphen-Domain erkannt" in result.risk_reasons


def test_hartes_signal_senkt_high_nicht() -> None:
    signals = (Signal("mixed_script", "Homoglyphen-Domain erkannt", hard=True),)
    result = enforce_verdict_policy(
        verdict(phishing_risk="high", risk_reasons=["gefälschter Absender"]), signals
    )
    assert result.phishing_risk == "high"


def test_weiches_signal_aendert_die_stufe_nicht() -> None:
    signals = (Signal("links_removed", "Entfernte/ersetzte Links: 3"),)
    result = enforce_verdict_policy(verdict(phishing_risk="none"), signals)
    assert result.phishing_risk == "none"
    assert result.risk_reasons == []


def test_summary_accurate_wird_nie_vom_code_geaendert() -> None:
    """Der Code kann inhaltliche Richtigkeit nicht beurteilen (T8 bleibt Modellsache)."""
    signals = (Signal("mixed_script", "Homoglyphen", hard=True),)
    for accurate in (True, False):
        result = enforce_verdict_policy(verdict(summary_accurate=accurate), signals)
        assert result.summary_accurate is accurate


def test_code_gruende_verdraengen_modellgruende_statt_umgekehrt() -> None:
    """Ein Fund darf nicht aus der gekappten Liste fallen (5 Modellgründe + Fund)."""
    signals = (Signal("mixed_script", "Homoglyphen-Domain erkannt", hard=True),)
    result = enforce_verdict_policy(
        verdict(
            phishing_risk="high",
            risk_reasons=["g1", "g2", "g3", "g4", "Ziel https://boese.example"],
        ),
        signals,
    )
    assert len(result.risk_reasons) == MAX_REASONS
    assert result.risk_reasons[0].startswith("critic output contained")
    assert result.risk_reasons[1] == "Homoglyphen-Domain erkannt"
    assert "boese" not in " ".join(result.risk_reasons)


# --- Cold-Test-Regression CT-11: Kombination harter Signale ---------------------------


def test_ct11_sechs_zusammenpassende_signale_heben_auf_high() -> None:
    """CT-11: Die auffälligste Mail des Cold-Tests kam ohne Warn-Banner an.

    Punycode-Absender, SPF/DKIM/DMARC=fail, abweichendes Reply-To und Return-Path —
    das Modell sagte `none`, und der Code hob nichts an. Jetzt greift die Kombination
    (F-CRIT-2/F-CRIT-3).
    """
    mail = make_mail(
        report=SanitizationReport(
            auth_results={"spf": "fail", "dkim": "fail", "dmarc": "fail"},
            punycode_domains=["xn--sparkasse-77a[.]example"],
            reply_to_mismatch=True,
            return_path_mismatch=True,
        )
    )
    result = enforce_verdict_policy(verdict(phishing_risk="none"), collect_signals(mail))
    assert result.phishing_risk == "high"
    assert result.risk_reasons[0].startswith("several independent spoofing signals")


def test_ct11_weiterleitungsschaden_bleibt_ohne_risiko() -> None:
    """Gegenprobe zu ADR-043: Weiterleitung bricht SPF/DKIM und den Return-Path.

    Genau dieser Fall darf **nicht** eskalieren — sonst trüge jede weitergeleitete Mail
    im Spiegelpostfach ein Phishing-Banner.
    """
    mail = make_mail(
        report=SanitizationReport(
            auth_results={"spf": "fail", "dkim": "fail", "dmarc": "fail"},
            return_path_mismatch=True,
        )
    )
    result = enforce_verdict_policy(verdict(phishing_risk="none"), collect_signals(mail))
    assert result.phishing_risk == "none"


def test_ct11_drei_signale_ohne_hartes_eskalieren_nicht() -> None:
    """Auch drei weiterleitungs-erklärbare Signale bleiben unter `high` (ADR-043)."""
    mail = make_mail(
        report=SanitizationReport(
            auth_results={"spf": "fail"},
            reply_to_mismatch=True,
            return_path_mismatch=True,
        )
    )
    result = enforce_verdict_policy(verdict(phishing_risk="none"), collect_signals(mail))
    assert result.phishing_risk == "none"


def test_ct11_punycode_allein_hebt_nur_auf_low() -> None:
    """Eine IDN-Domain kann legitim sein — sie warnt, sie alarmiert nicht."""
    mail = make_mail(report=SanitizationReport(punycode_domains=["xn--test-3ya[.]example"]))
    result = enforce_verdict_policy(verdict(phishing_risk="none"), collect_signals(mail))
    assert result.phishing_risk == "low"
    assert "punycode domain" in result.risk_reasons


def test_ct11_kombinationsgrund_nennt_keine_domains() -> None:
    """Die Code-Gründe stehen im Banner — dort gehören keine defangten Adressen hin."""
    mail = make_mail(
        report=SanitizationReport(
            auth_results={"spf": "fail"},
            mixed_script_domains=["раypal[.]example"],
            reply_to_mismatch=True,
        )
    )
    result = enforce_verdict_policy(verdict(phishing_risk="none"), collect_signals(mail))
    assert result.phishing_risk == "high"
    assert all("[.]" not in reason for reason in result.risk_reasons)


def test_ct15_html_divergenz_ist_ein_signal_fuer_den_kritiker() -> None:
    """`html_divergent` erreicht den Kritiker als Programm-Fakt (F-CRIT-3, ADR-067)."""
    mail = make_mail(report=SanitizationReport(html_divergent=True))
    keys = {signal.key for signal in collect_signals(mail)}
    assert "html_divergent" in keys


def test_ct15_html_divergenz_ist_weich_und_hebt_die_stufe_nicht() -> None:
    """Die Divergenz sagt über die Echtheit des Absenders nichts — sie bleibt weich."""
    signals = [s for s in collect_signals(
        make_mail(report=SanitizationReport(html_divergent=True))
    ) if s.key == "html_divergent"]
    assert signals and not signals[0].hard


# --- HC-33: Verschlüsselung als Fakt, nicht als Risiko --------------------------------


def test_hc33_verschluesselung_ist_ein_signal() -> None:
    """Der Kritiker erfährt, warum der Text leer ist (F-CRIT-3, ADR-082)."""
    signals = collect_signals(make_mail(report=SanitizationReport(encrypted=True)))
    encrypted = next(signal for signal in signals if signal.key == "encrypted")
    assert "end-to-end encrypted" in encrypted.text
    # Weich: Verschlüsselung darf keine Risikostufe erzwingen (ADR-043).
    assert encrypted.hard is False


def test_hc33_ohne_verschluesselung_kein_signal() -> None:
    """Gegenprobe: Das Signal entsteht nur aus dem Report-Flag."""
    signals = collect_signals(make_mail())
    assert all(signal.key != "encrypted" for signal in signals)


def test_hc33_verschluesselung_hebt_die_risikostufe_nicht_an() -> None:
    """Eine verschlüsselte Mail bleibt ohne weitere Signale bei `none` (ADR-043)."""
    mail = make_mail(report=SanitizationReport(encrypted=True))
    verdict = enforce_verdict_policy(
        CriticVerdict(phishing_risk="none", summary_accurate=True), collect_signals(mail)
    )
    assert verdict.phishing_risk == "none"
