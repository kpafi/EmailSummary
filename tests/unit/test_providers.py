"""Tests der Einrichtungs-Wissensbasis (`providers.py`) und ihrer Wirkung in der CLI.

Hintergrund: Die Einrichtung des Spiegel-Postfachs war die erste Stelle, an der ein echter
Nutzer hängen blieb — mit `imap.gmail.com` und dem Google-Kontopasswort, das Gmail für IMAP
grundsätzlich ablehnt. Diese Tests halten fest, dass das Werkzeug diese Lage erkennt und
benennt, statt den Nutzer in wiederholte Anmeldefehler laufen zu lassen.
"""

from __future__ import annotations

import io

import pytest

from maildigest import providers
from maildigest.cli import EXIT_USAGE, Hooks, main


def run(argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    """Führt ein Kommando aus und liefert (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, hooks=Hooks())
    return code, out.getvalue(), err.getvalue()


# --- Nachschlagen ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("imap.gmail.com", "gmail"),
        ("IMAP.GMAIL.COM", "gmail"),
        ("gmail.com", "gmail"),
        ("smtp.gmail.com", "gmail"),
        ("posteo.de", "posteo"),
        ("imap.gmx.net", "gmx"),
        ("outlook.office365.com", "outlook"),
        ("secureimap.t-online.de", "tonline"),
    ],
)
def test_host_wird_dem_anbieter_zugeordnet(eingabe: str, erwartet: str) -> None:
    """Auch Schreibvarianten und verwandte Hosts führen zum richtigen Anbieter."""
    provider = providers.find_by_host(eingabe)
    assert provider is not None
    assert provider.key == erwartet


@pytest.mark.parametrize(
    ("adresse", "erwartet"),
    [
        ("aispiegel2@gmail.com", "gmail"),
        ("jemand@hotmail.de", "outlook"),
        ("wer@mailbox.org", "mailboxorg"),
        ("x@t-online.de", "tonline"),
    ],
)
def test_mailadresse_wird_dem_anbieter_zugeordnet(adresse: str, erwartet: str) -> None:
    """Die Mailadresse allein genügt — sie ist die Eingabe, die Nutzer zur Hand haben."""
    provider = providers.find_by_address(adresse)
    assert provider is not None
    assert provider.key == erwartet


def test_unbekannter_host_wird_nicht_geraten() -> None:
    """Bei einem unbekannten Anbieter wird nichts erfunden."""
    assert providers.find_by_host("mail.firma-xy.example") is None
    assert providers.find_by_address("chef@firma-xy.example") is None
    assert providers.find_by_host("") is None


# --- Datenqualität ---------------------------------------------------------------------------


def test_jeder_unterstuetzte_anbieter_hat_eine_anleitung() -> None:
    """Ein Anbieter ohne Anleitung wäre eine leere Zusage."""
    for provider in providers.PROVIDERS:
        if not provider.supported:
            continue
        assert provider.steps, f"{provider.key} ohne Anleitung"
        assert provider.password_kind
        assert provider.imap_port == 993, f"{provider.key}: nur IMAPS ist vorgesehen"


def test_nicht_unterstuetzte_anbieter_nennen_den_grund_und_einen_ausweg() -> None:
    """„Geht nicht“ ohne Begründung wäre wertlos."""
    blocked = [p for p in providers.PROVIDERS if not p.supported]
    assert {p.key for p in blocked} == {"outlook", "proton"}
    for provider in blocked:
        assert provider.unsupported_reason
        assert provider.note, f"{provider.key} nennt keinen Ausweg"


def test_anleitung_wird_auf_terminalbreite_umbrochen() -> None:
    """Lange Anleitungen ohne Umbruch sind im Terminal unlesbar."""
    for provider in providers.PROVIDERS:
        for line in providers.setup_guide(provider).splitlines():
            assert len(line) <= 80, f"{provider.key}: Zeile zu lang: {line!r}"


def test_gmail_anleitung_nennt_die_tatsaechliche_ursache() -> None:
    """Der konkrete Fall, an dem ein Nutzer hängen blieb."""
    guide = providers.setup_guide(providers.PROVIDERS[0])
    assert "app password" in guide
    assert "2-Step Verification" in guide  # 2FA ist Voraussetzung
    assert "myaccount.google.com/apppasswords" in guide


def test_gmx_anleitung_nennt_die_freischaltung() -> None:
    """Bei GMX/WEB.DE ist IMAP ab Werk aus — ohne diesen Hinweis sucht man am Passwort."""
    guide = providers.setup_guide(providers.find_by_host("imap.gmx.net"))
    assert "Switch IMAP on first" in guide
    assert "POP3/IMAP" in guide


# --- Hinweise nach fehlgeschlagener Anmeldung ------------------------------------------------


def test_hinweis_bei_bekanntem_anbieter_enthaelt_die_anleitung() -> None:
    hint = providers.auth_failure_hint("imap.gmail.com")
    assert "Gmail" in hint
    assert "app password" in hint


def test_hinweis_bei_unbekanntem_anbieter_bleibt_allgemein() -> None:
    """Ohne Wissen wird nicht geraten, aber die häufigste Ursache genannt."""
    hint = providers.auth_failure_hint("mail.firma-xy.example")
    assert "app password" in hint
    assert "firma-xy" not in hint


# --- Wirkung in der CLI ----------------------------------------------------------------------


def test_connect_mail_lehnt_outlook_mit_begruendung_ab(tmp_path) -> None:
    """Outlook.com kann grundsätzlich nicht — das gehört vor die Passwortfrage."""
    cfg = tmp_path / "config.toml"
    run(["--config", str(cfg), "--non-interactive", "init"])
    code, _out, err = run(
        [
            "--config", str(cfg), "--non-interactive", "connect-mail",
            "--host", "outlook.com", "--username", "x@outlook.com", "--no-test",
        ]
    )
    assert code == EXIT_USAGE
    assert "LOGINDISABLED" in err
    assert "forward" in err  # der Ausweg wird genannt


def test_connect_mail_uebersetzt_die_mailadresse_in_den_host(tmp_path) -> None:
    """Die Mailadresse im Host-Feld ist die häufigste Fehleingabe — sie wird übersetzt."""
    cfg = tmp_path / "config.toml"
    run(["--config", str(cfg), "--non-interactive", "init"])
    code, out, _err = run(
        [
            "--config", str(cfg), "--non-interactive", "connect-mail",
            "--host", "aispiegel2@gmail.com", "--username", "aispiegel2@gmail.com",
            "--no-test",
        ]
    )
    assert "imap.gmail.com" in out
    assert "app password" in out
    assert code in {0, EXIT_USAGE}  # ohne gesetztes Env-Passwort ist 2 zulässig


def test_connect_mail_zeigt_host_beispiele(tmp_path) -> None:
    """Ohne Beispiele weiß niemand, was ein IMAP-Host ist."""
    cfg = tmp_path / "config.toml"
    run(["--config", str(cfg), "--non-interactive", "init"])
    _code, out, _err = run(
        [
            "--config", str(cfg), "--non-interactive", "connect-mail",
            "--host", "mail.firma-xy.example", "--username", "x@firma-xy.example", "--no-test",
        ]
    )
    assert "imap.gmail.com" in out
    assert "not your mail address" in out


# --- Auswahl des Sprachmodells (ADR-076) ------------------------------------------------------


def test_erste_option_ist_die_ohne_anmeldung() -> None:
    """Der Standard muss ohne Konto funktionieren — sonst tut das Werkzeug anfangs nichts."""
    first = providers.LLM_PRESETS[0]
    assert first.key == "none"
    assert first.provider == "none"
    assert not first.needs_key
    assert not first.needs_model


def test_es_gibt_kostenlose_optionen_mit_erklaerung() -> None:
    """„Gratis" ohne Anleitung, wo man den Schlüssel herbekommt, hilft niemandem."""
    free = [p for p in providers.LLM_PRESETS if p.key in {"groq", "openrouter", "cerebras"}]
    assert len(free) == 3
    for preset in free:
        assert preset.provider == "openai_compatible"
        assert preset.base_url.startswith("https://")
        assert preset.detail


def test_lokale_option_braucht_keinen_schluessel() -> None:
    """Ollama & Co. laufen ohne Konto — die Abfrage darf keinen Schlüssel verlangen."""
    local = next(p for p in providers.LLM_PRESETS if p.key == "ollama")
    assert not local.needs_key
    assert local.base_url.startswith("http://localhost")


def test_jede_option_traegt_einen_gueltigen_provider_wert() -> None:
    """Die Vorlage schreibt direkt in `[llm] provider` — ein Tippfehler wäre fatal."""
    for preset in providers.LLM_PRESETS:
        assert preset.provider in {"none", "anthropic", "openai_compatible"}
        assert preset.label


def test_intro_nennt_den_grund_fuer_den_fehlenden_mitgelieferten_schluessel() -> None:
    """Ehrlichkeit an der Stelle, an der Nutzer „warum nicht einfach gratis?" fragen."""
    assert "open source" in providers.LLM_CHOICE_INTRO
    assert "no key of its own" in providers.LLM_CHOICE_INTRO
