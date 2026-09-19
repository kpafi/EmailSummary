"""Tests des Generators für das selbst gehostete Spiegelpostfach (S2, F-ING-4, ADR-089).

Vertrag: docs/SPEC-CLI.md §4 `selfhost-mail`, Entwurf: docs/PLAN-SELFHOST-MAIL.md §3/§6.
Geprüft wird, was ohne laufenden Mailserver prüfbar ist — also genau das, was die
Zusicherungen des Vertrags behaupten: Domainregeln, Determinismus, die Form der erzeugten
Dateien, `state.json` und die Exit-Codes der CLI. Dass die Vorlagen einen echten Postfix
und Dovecot zufriedenstellen, beweist erst die Container-Probe aus PLAN §7 (S4).
"""

from __future__ import annotations

import io
import json
import re
import stat
from pathlib import Path

import pytest

from maildigest import selfhost
from maildigest.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Hooks, main

DOMAIN = "mirror.example.org"
ADDRESS = "mirror-7f3a9c1d@mirror.example.org"
CERT_DIR = "/etc/letsencrypt/live/mirror.example.org"


def run(argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    """Führt ein Kommando über den echten Einstieg aus (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, hooks=Hooks())
    return code, out.getvalue(), err.getvalue()


def rendered(out_dir: Path, **overrides: object) -> dict[str, str]:
    """Die fünf Dateien mit den Standardwerten dieses Tests."""
    values: dict[str, object] = {
        "domain": DOMAIN,
        "address": ADDRESS,
        "cert_dir": CERT_DIR,
        "out_dir": out_dir,
        "ipv4": "203.0.113.7",
        "ipv6": "2001:db8::7",
    }
    values.update(overrides)
    return selfhost.render_files(**values)  # type: ignore[arg-type]


# --- Domainregeln (SPEC-CLI.md §4 „Domain rules") ---------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mirror.example.org", "mirror.example.org"),
        ("Mirror.Example.ORG", "mirror.example.org"),
        ("mirror.example.org.", "mirror.example.org"),
        ("  mirror.example.org  ", "mirror.example.org"),
        ("a.b.c.d.example.org", "a.b.c.d.example.org"),
        ("mirror-1.example.org", "mirror-1.example.org"),
    ],
)
def test_gueltige_domains_werden_normalisiert(value: str, expected: str) -> None:
    assert selfhost.validate_domain(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("spiegel.beispiel.de.ü", "idn"),
        ("xn--mller-kva.example.org", "idn"),
        ("mirror.münchen.de", "idn"),
        ("-mirror.example.org", "syntax"),
        ("mirror-.example.org", "syntax"),
        ("mirror..example.org", "syntax"),
        ("mirror.example.org/x", "syntax"),
        ("mirror example.org", "syntax"),
        ("localhost", "syntax"),
        ("example.org", "apex"),
    ],
)
def test_unerlaubte_domains_werden_abgelehnt(value: str, reason: str) -> None:
    with pytest.raises(selfhost.DomainError) as info:
        selfhost.validate_domain(value)
    assert info.value.reason == reason


def test_lange_marken_und_namen_sind_unzulaessig() -> None:
    with pytest.raises(selfhost.DomainError):
        selfhost.validate_domain(f"{'a' * 64}.example.org")
    with pytest.raises(selfhost.DomainError):
        selfhost.validate_domain(".".join(["abcdefghij"] * 25) + ".org")


def test_allow_apex_erlaubt_zwei_marken_aber_nicht_eine() -> None:
    assert selfhost.validate_domain("example.org", allow_apex=True) == "example.org"
    with pytest.raises(selfhost.DomainError):
        selfhost.validate_domain("org", allow_apex=True)


def test_die_drei_meldungen_stehen_woertlich_in_der_spezifikation() -> None:
    """Die Fehlertexte sind Vertrag (SPEC-CLI.md §4) und werden dort nachgeschlagen."""
    spec = (Path(__file__).resolve().parents[2] / "docs" / "SPEC-CLI.md").read_text(
        encoding="utf-8"
    )
    for value, allow_apex in (("mü.example.org", False), ("example.org", False), ("x..y", False)):
        with pytest.raises(selfhost.DomainError) as info:
            selfhost.validate_domain(value, allow_apex=allow_apex)
        skeleton = re.sub(r'"[^"]*"', '"<value>"', str(info.value))
        skeleton = skeleton.replace("mirror.example.org,", "mirror.<value>,")
        assert f"Error: {skeleton}" in spec, skeleton


# --- Adresse ----------------------------------------------------------------------------


def test_adresse_ist_mirror_plus_acht_hex() -> None:
    address = selfhost.generate_address(DOMAIN)
    assert re.fullmatch(r"mirror-[0-9a-f]{8}@mirror\.example\.org", address)


def test_zwei_adressen_sind_verschieden() -> None:
    """Der zufällige lokale Teil ist die Spam-Abwehr — er darf nicht wiederkehren."""
    assert len({selfhost.generate_address(DOMAIN) for _ in range(20)}) == 20


# --- Zertifikatsverzeichnis -------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["/etc/letsencrypt/live/mirror.example.org/", "/tmp/certs", "/a_b-c.d/e"]
)
def test_gueltiges_zertifikatsverzeichnis(value: str) -> None:
    assert selfhost.validate_cert_dir(value).startswith("/")
    assert not selfhost.validate_cert_dir(value).endswith("/")


@pytest.mark.parametrize(
    "value",
    [
        "relativ/pfad",
        "/etc/certs; rm -rf /",
        "/etc/$(whoami)",
        "/etc/certs'",
        "/etc/../root",
        "/etc/certs\n/evil",
    ],
)
def test_unerlaubtes_zertifikatsverzeichnis(value: str) -> None:
    with pytest.raises(selfhost.SelfhostError):
        selfhost.validate_cert_dir(value)


# --- Rendern ----------------------------------------------------------------------------


def test_fuenf_dateien_in_fester_reihenfolge(tmp_path: Path) -> None:
    files = rendered(tmp_path)
    assert list(files) == list(selfhost.FILE_ORDER)


def test_rendern_ist_deterministisch(tmp_path: Path) -> None:
    assert rendered(tmp_path) == rendered(tmp_path)


def test_kein_platzhalter_bleibt_stehen(tmp_path: Path) -> None:
    for name, text in rendered(tmp_path).items():
        assert "{{" not in text, name
        assert "}}" not in text, name


def test_domain_adresse_und_zertifikat_stehen_in_den_dateien(tmp_path: Path) -> None:
    files = rendered(tmp_path)
    assert ADDRESS in files["postfix.sh"]
    assert f"virtual_mailbox_domains = {DOMAIN}" in files["postfix.sh"]
    assert f"{CERT_DIR}/fullchain.pem" in files["dovecot.conf"]
    assert ADDRESS in files["checklist.txt"]


def test_keine_datei_enthaelt_ein_geheimnis(tmp_path: Path) -> None:
    """I5/D6: Das erzeugte Verzeichnis darf aufgehoben und verschickt werden."""
    literal = re.compile(r"(?i)(password|passwd|secret|token)\s*=\s*[\"']?[^\"'\s]+")
    for name, text in rendered(tmp_path).items():
        assert "{BLF-CRYPT}$" not in text, name
        hit = literal.search(text)
        assert hit is None, f"{name}: {hit.group(0) if hit else ''}"
    # Das Passwort taucht in `apply.sh` nur als Shell-Variable auf, und es wird nirgends
    # in eine Datei geschrieben: geschrieben wird `$hash`.
    apply = rendered(tmp_path)["apply.sh"]
    assert "doveadm pw -s BLF-CRYPT" in apply
    for line in apply.splitlines():
        if ">" in line and "mailbox_password" in line:
            raise AssertionError(f"apply.sh schreibt das Passwort in eine Datei: {line}")


def test_postfix_script_enthaelt_nur_postconf_zeilen(tmp_path: Path) -> None:
    """Zusicherung aus SPEC-CLI.md §4: keine andere Zeile als `postconf -e …`."""
    for line in rendered(tmp_path)["postfix.sh"].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert stripped.startswith("postconf -e '"), stripped
        assert stripped.endswith("'"), stripped


def test_postfix_script_setzt_die_schluessel_aus_dem_plan(tmp_path: Path) -> None:
    text = rendered(tmp_path)["postfix.sh"]
    for key in (
        "myhostname",
        "mydestination",
        "mynetworks_style",
        "virtual_mailbox_domains",
        "virtual_mailbox_maps",
        "virtual_transport",
        "smtpd_relay_restrictions",
        "smtpd_recipient_restrictions",
        "smtpd_helo_required",
        "disable_vrfy_command",
        "smtpd_tls_cert_file",
        "smtpd_tls_key_file",
        "smtpd_tls_security_level",
        "message_size_limit",
        "inet_interfaces",
    ):
        assert f"postconf -e '{key} " in text or f"postconf -e '{key}='" in text, key
    settings = [line for line in text.splitlines() if line.startswith("postconf ")]
    assert not any("permit_mynetworks" in line for line in settings), (
        "ohne permit_mynetworks lässt sich der offene Relay prüfen"
    )
    assert "message_size_limit = 26214400" in text, "= [limits] max_mail_bytes"


def test_dovecot_conf_erzwingt_tls_und_schliesst_port_143(tmp_path: Path) -> None:
    text = rendered(tmp_path)["dovecot.conf"]
    assert "ssl = required" in text
    assert "ssl = no" not in text
    assert "ssl = yes" not in text
    # Der einzige `imap`-Listener steht auf `port = 0` — nur Kommentarzeilen dürfen 143
    # überhaupt erwähnen.
    assert "port = 0" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "143" not in stripped, stripped


def test_dovecot_conf_behaelt_die_domain_im_lmtp_nutzernamen(tmp_path: Path) -> None:
    """Regression aus der VM-Probe (S4, PLAN §7): sonst bounct jede zugestellte Mail.

    Debians `20-lmtp.conf` setzt `auth_username_format = %{user | username | lower}` und
    wirft damit die Domain weg; die Suche nach `mirror-…` findet den passwd-file-Nutzer
    `mirror-…@Domain` nicht, und Postfix bekommt `550 5.1.1 User doesn't exist`. Der
    Drop-in wird nach 20-lmtp.conf gelesen und muss den Wert zurücksetzen.
    """
    text = rendered(tmp_path)["dovecot.conf"]
    lines = [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]
    assert "protocol lmtp {" in lines
    formats = [line for line in lines if line.startswith("auth_username_format")]
    assert formats == ["auth_username_format = %{user | lower}"], (
        "kein Format, das die Domain abschneidet"
    )


def test_apply_script_macht_die_passwd_datei_fuer_dovecot_lesbar(tmp_path: Path) -> None:
    """Regression aus der VM-Probe (S4): Dovecot 2.4 authentifiziert unprivilegiert.

    Mit `0600 root:root` scheitert jeder Login an
    `passwd-file: open(/etc/dovecot/users) failed: Permission denied`. Die Datei bleibt
    für die Welt unlesbar und für die Gruppe unbeschreibbar.
    """
    apply = rendered(tmp_path)["apply.sh"]
    assert "getent group dovecot" in apply
    assert "chown root:dovecot /etc/dovecot/users" in apply
    assert "chmod 0640 /etc/dovecot/users" in apply
    # Ohne dovecot-Gruppe bleibt es beim strengeren Wert.
    assert "chmod 0600 /etc/dovecot/users" in apply
    for line in apply.splitlines():
        if line.strip().startswith("chmod") and "/etc/dovecot/users" in line:
            mode = line.split()[1]
            assert mode.endswith("0"), f"/etc/dovecot/users darf nicht weltlesbar sein: {line}"


def test_apply_script_prueft_die_dovecot_konfiguration_vor_dem_reload(tmp_path: Path) -> None:
    """Regression aus der VM-Probe (S4): ein `reload` meldet auch dann Erfolg, wenn

    Dovecot den Drop-in gar nicht lesen konnte und die alte Konfiguration weiterläuft.
    `doveconf -n` sagt es laut, bevor `apply.sh` „Done" schreibt.
    """
    apply = rendered(tmp_path)["apply.sh"]
    lines = apply.splitlines()
    check = next(i for i, line in enumerate(lines) if line.startswith("doveconf -n"))
    # Nicht die Zeile im certbot-Hook, sondern die des Schritts 6/6.
    reload_at = next(
        i for i, line in enumerate(lines) if "systemctl restart dovecot" in line
    )
    assert check < reload_at, "die Prüfung muss vor dem Neuladen stehen"
    assert "for tool in postconf doveadm doveconf" in apply


def test_dns_records_mit_und_ohne_ipv6(tmp_path: Path) -> None:
    mit = selfhost.dns_records(DOMAIN, ipv4="203.0.113.7", ipv6="2001:db8::7")
    assert mit == [
        "mirror.example.org.   A     203.0.113.7",
        "mirror.example.org.   AAAA  2001:db8::7",
        "mirror.example.org.   MX 10 mirror.example.org.",
    ]
    ohne = selfhost.dns_records(DOMAIN, ipv4=None, ipv6=None)
    assert ohne[0].endswith(selfhost.IPV4_PLACEHOLDER)
    assert not any("AAAA" in line for line in ohne)
    assert selfhost.NO_IPV6_COMMENT in rendered(tmp_path, ipv6=None)["dns.txt"]


@pytest.mark.parametrize(
    ("family", "raw", "erwartet"),
    [
        ("v4", "93.184.216.34", "93.184.216.34"),
        ("v4", "192.168.1.5", None),
        ("v4", "127.0.0.1", None),
        ("v6", "2606:2800:220:1::1946", "2606:2800:220:1::1946"),
        ("v6", "fe80::1", None),
        # Regression aus der VM-Probe (S4): `ipaddress` hält fec0::/10 für global.
        ("v6", "fec0::5054:ff:fe12:3456", None),
    ],
)
def test_eigene_adresse_nur_wenn_die_welt_sie_erreicht(
    family: str, raw: str, erwartet: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dns.txt` darf keine Adresse empfehlen, an die von außen nichts zustellt."""
    import socket as socket_module

    from maildigest import cli

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def settimeout(self, _seconds: float) -> None:
            return None

        def connect(self, _target: tuple[str, int]) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return (raw, 9)

    monkeypatch.setattr(cli.socket, "socket", lambda *_a, **_k: FakeSocket())
    net = socket_module.AF_INET if family == "v4" else socket_module.AF_INET6
    assert cli._local_address(net, "203.0.113.1") == erwartet


def test_apply_script_ist_gueltige_posix_shell(tmp_path: Path) -> None:
    """`sh -n` ist die billigste Wahrheit über ein erzeugtes Skript."""
    import shutil
    import subprocess

    shell = shutil.which("sh")
    if shell is None:  # pragma: no cover - auf jedem Zielsystem vorhanden
        pytest.skip("keine sh im PATH")
    for name in ("apply.sh", "postfix.sh"):
        script = tmp_path / name
        script.write_text(rendered(tmp_path)[name], encoding="utf-8")
        assert subprocess.run([shell, "-n", str(script)], check=False).returncode == 0, name


def test_apply_script_setzt_jeden_wert_in_einfache_anfuehrungszeichen(tmp_path: Path) -> None:
    """Zweite Schranke neben der Zeichenprüfung (Shell-Sicherheit, S2)."""
    apply = rendered(tmp_path)["apply.sh"]
    assert f"domain='{DOMAIN}'" in apply
    assert f"address='{ADDRESS}'" in apply
    assert f"cert_dir='{CERT_DIR}'" in apply


# --- state.json -------------------------------------------------------------------------


def test_state_round_trip(tmp_path: Path) -> None:
    state = selfhost.MirrorState(
        domain=DOMAIN, address=ADDRESS, cert_dir=CERT_DIR, generated_at="2026-09-19T12:04:57Z"
    )
    selfhost.write_files(tmp_path, {})
    path = selfhost.write_state(tmp_path, state)
    assert selfhost.read_state(tmp_path) == state
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert '  "address"' in text, "zwei Leerzeichen Einrückung"
    assert json.loads(text)["version"] == selfhost.STATE_VERSION
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_state_json_kennt_kein_passwortfeld(tmp_path: Path) -> None:
    state = selfhost.MirrorState(
        domain=DOMAIN, address=ADDRESS, cert_dir=CERT_DIR, generated_at="2026-09-19T12:04:57Z"
    )
    assert "password" not in state.to_json().lower()


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        '["a"]',
        '{"address": "a", "cert_dir": "/x", "domain": "mirror.example.org", '
        '"generated_at": "t", "version": 2}',
        '{"address": "mirror-7f3a9c1d@mirror.example.org", "cert_dir": "/x", '
        '"domain": "mirror.example.org", "generated_at": "t", "version": 1, "extra": 1}',
        '{"address": "mirror-7f3a9c1d@mirror.example.org", "cert_dir": "/x", '
        '"domain": "mirror.example.org", "version": 1}',
        '{"address": "mirror-7f3a9c1d@nicht.die.domain", "cert_dir": "/x", '
        '"domain": "mirror.example.org", "generated_at": "t", "version": 1}',
        '{"address": "beliebig@mirror.example.org", "cert_dir": "/x", '
        '"domain": "mirror.example.org", "generated_at": "t", "version": 1}',
        '{"address": "mirror-7f3a9c1d@mü.example.org", "cert_dir": "/x", '
        '"domain": "mü.example.org", "generated_at": "t", "version": 1}',
    ],
)
def test_unbrauchbares_state_json_ist_ein_fehler(tmp_path: Path, payload: str) -> None:
    (tmp_path / selfhost.STATE_FILENAME).write_text(payload, encoding="utf-8")
    with pytest.raises(selfhost.SelfhostError):
        selfhost.read_state(tmp_path)


def test_fehlendes_state_json_nennt_das_kommando(tmp_path: Path) -> None:
    with pytest.raises(selfhost.SelfhostError) as info:
        selfhost.read_state(tmp_path)
    assert "maildigest selfhost-mail --domain" in str(info.value)


def test_zeitstempel_ist_utc_mit_z() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", selfhost.now_stamp())


# --- CLI: Erzeugen ----------------------------------------------------------------------


def test_cli_schreibt_sechs_dateien_mit_den_richtigen_rechten(tmp_path: Path) -> None:
    target = tmp_path / "sh"
    code, out, _err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"]
    )
    assert code == EXIT_OK
    assert sorted(item.name for item in target.iterdir()) == sorted(
        [*selfhost.FILE_ORDER, selfhost.STATE_FILENAME]
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    for name in [*selfhost.FILE_ORDER, selfhost.STATE_FILENAME]:
        assert stat.S_IMODE((target / name).stat().st_mode) == selfhost.FILE_MODES[name]
    assert "6 files, directory mode 0700" in out
    assert "Next steps:" in out
    assert "Port 25 must be reachable from the internet" in out


def test_cli_ausgabe_nennt_domain_adresse_und_zertifikat(tmp_path: Path) -> None:
    target = tmp_path / "sh"
    _code, out, _err = run(["selfhost-mail", "--domain", DOMAIN, "--out", str(target)])
    address = selfhost.read_state(target).address
    assert f"  Domain:      {DOMAIN}" in out
    assert f"  Address:     {address}" in out
    assert "  Certificate: /etc/letsencrypt/live/mirror.example.org" in out
    assert address in out.split("Next steps:")[1]


def test_cli_ohne_domain_ist_bedienfehler(tmp_path: Path) -> None:
    code, _out, err = run(["selfhost-mail", "--out", str(tmp_path / "sh")])
    assert code == EXIT_USAGE
    assert "--domain" in err


def test_cli_ungueltige_domain_ist_bedienfehler(tmp_path: Path) -> None:
    code, _out, err = run(["selfhost-mail", "--domain", "example.org", "--out", str(tmp_path)])
    assert code == EXIT_USAGE
    assert "apex domain" in err
    assert not (tmp_path / selfhost.STATE_FILENAME).exists(), "nichts geschrieben"


def test_cli_allow_apex(tmp_path: Path) -> None:
    target = tmp_path / "sh"
    code, _out, _err = run(
        ["selfhost-mail", "--domain", "example.org", "--allow-apex", "--out", str(target)]
    )
    assert code == EXIT_OK
    assert selfhost.read_state(target).domain == "example.org"


def test_cli_eigenes_zertifikatsverzeichnis(tmp_path: Path) -> None:
    target = tmp_path / "sh"
    code, out, _err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--cert-dir", "/tmp/certs"]
    )
    assert code == EXIT_OK
    assert "  Certificate: /tmp/certs" in out
    assert "/tmp/certs/fullchain.pem" in (target / "dovecot.conf").read_text(encoding="utf-8")
    assert selfhost.read_state(target).cert_dir == "/tmp/certs"


def test_cli_lehnt_gefaehrliches_zertifikatsverzeichnis_ab(tmp_path: Path) -> None:
    code, _out, err = run(
        [
            "selfhost-mail",
            "--domain",
            DOMAIN,
            "--out",
            str(tmp_path / "sh"),
            "--cert-dir",
            "/etc/certs'; rm -rf /",
        ]
    )
    assert code == EXIT_USAGE
    assert "--cert-dir" in err


def test_cli_erzeugt_nicht_zweimal_in_dasselbe_verzeichnis(tmp_path: Path) -> None:
    target = tmp_path / "sh"
    assert run(["selfhost-mail", "--domain", DOMAIN, "--out", str(target)])[0] == EXIT_OK
    address = selfhost.read_state(target).address
    code, _out, err = run(["selfhost-mail", "--domain", DOMAIN, "--out", str(target)])
    assert code == EXIT_ERROR
    assert "already holds a mirror mailbox" in err
    assert address in err
    assert selfhost.read_state(target).address == address, "die Adresse bleibt unverändert"


def test_cli_ohne_konfiguration_und_ohne_out_verweist_auf_init(tmp_path: Path) -> None:
    code, _out, err = run(
        ["--config", str(tmp_path / "config.toml"), "selfhost-mail", "--domain", DOMAIN]
    )
    assert code == EXIT_ERROR
    assert "maildigest init" in err


def test_cli_ohne_out_schreibt_neben_die_konfiguration(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    assert run(["--config", str(config), "--non-interactive", "init"])[0] == EXIT_OK
    code, _out, _err = run(["--config", str(config), "selfhost-mail", "--domain", DOMAIN])
    assert code == EXIT_OK
    assert (tmp_path / "selfhost-mail" / selfhost.STATE_FILENAME).is_file()


# --- CLI: Modus-Ausschlüsse (SPEC-CLI.md §4, Exit-Code 2) --------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["selfhost-mail", "--check", "--domain", DOMAIN],
        ["selfhost-mail", "--check", "--allow-apex"],
        ["selfhost-mail", "--check", "--cert-dir", "/tmp/certs"],
        ["selfhost-mail", "--domain", DOMAIN, "--wait-for-mail"],
        ["selfhost-mail", "--check", "--timeout", "60"],
        ["selfhost-mail", "--check", "--timeout", "5", "--wait-for-mail"],
        ["selfhost-mail", "--check", "--timeout", "3601", "--wait-for-mail"],
        ["selfhost-mail", "--check", "--timeout", "viel", "--wait-for-mail"],
    ],
)
def test_optionen_des_anderen_modus_sind_bedienfehler(tmp_path: Path, argv: list[str]) -> None:
    code, _out, err = run([*argv, "--out", str(tmp_path / "sh")])
    assert code == EXIT_USAGE, err
    assert err.startswith("Error: ")


def test_check_ohne_state_json_ist_ein_konfigurationsfehler(tmp_path: Path) -> None:
    code, _out, err = run(["selfhost-mail", "--check", "--out", str(tmp_path / "sh")])
    assert code == EXIT_ERROR
    assert "state.json" in err


# --- Cold-Tester-Regressionen (tests/cold/REPORT-SELFHOST.md) ----------------------------


def test_schritte_4_und_5_nennen_das_out_verzeichnis(tmp_path: Path) -> None:
    """CT-S1: Die abgetippte Prüfzeile muss dasselbe Verzeichnis meinen wie der Lauf.

    Ohne `--out DIR` in den Schritten 4 und 5 sucht `--check` neben der Konfiguration —
    also entweder nirgends (Fehlermeldung) oder in einem älteren, fremden Postfach.
    """
    target = tmp_path / "elsewhere"
    code, out, _err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"]
    )
    assert code == EXIT_OK
    expected = f"maildigest selfhost-mail --check --out {target}"
    assert expected in out
    assert f"{expected} --wait-for-mail" in out
    assert expected in (target / "checklist.txt").read_text(encoding="utf-8")


def test_ohne_out_bleiben_die_schritte_bei_dem_blanken_kommando(tmp_path: Path) -> None:
    """Ohne `--out` findet `--check` das Verzeichnis selbst — die Option wäre Ballast."""
    config = tmp_path / "config.toml"
    assert run(["--config", str(config), "--non-interactive", "init"])[0] == EXIT_OK
    _code, out, _err = run(["--config", str(config), "selfhost-mail", "--domain", DOMAIN])
    assert "maildigest selfhost-mail --check\n" in out
    assert "--check --out" not in out


def test_schritt_1_nennt_die_datei_die_wirklich_geschrieben_wurde(tmp_path: Path) -> None:
    """CT-S4: Ein Lauf, zwei Namen für dieselbe Datei — Schritt 1 nannte `selfhost-mail/`."""
    target = tmp_path / "o1"
    _code, out, _err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"]
    )
    assert f"({target / 'dns.txt'})" in out
    assert "(selfhost-mail/dns.txt)" not in out


def test_out_auf_ein_nicht_leeres_verzeichnis_ist_ein_fehler(tmp_path: Path) -> None:
    """CT-S3: Ein bestehendes Verzeichnis wurde übernommen und stillschweigend auf 0700 gesetzt.

    `--out ~` schüttete die sechs Dateien in das Heimatverzeichnis und änderte dessen
    Rechte, ohne ein Wort auf stdout oder stderr.
    """
    target = tmp_path / "exdir"
    target.mkdir(mode=0o755)
    (target / "important.txt").write_text("nicht anfassen\n", encoding="utf-8")
    code, _out, err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"]
    )
    assert code == EXIT_ERROR
    assert "already exists and is not empty" in err
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert sorted(item.name for item in target.iterdir()) == ["important.txt"]


def test_out_auf_ein_leeres_verzeichnis_bleibt_erlaubt(tmp_path: Path) -> None:
    """Ein leeres Verzeichnis darf `--out` sein; seine Rechte werden auf 0700 gezogen."""
    target = tmp_path / "leer"
    target.mkdir(mode=0o755)
    code, _out, _err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"]
    )
    assert code == EXIT_OK
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ein_symlink_statt_state_json_taeuscht_kein_leeres_verzeichnis_vor(
    tmp_path: Path,
) -> None:
    """HOT-S4: `Path.exists()` folgt einem toten Symlink und meldete „kein Postfach hier"."""
    target = tmp_path / "sh"
    target.mkdir()
    (target / selfhost.STATE_FILENAME).symlink_to(tmp_path / "nirgendwo.json")
    code, _out, err = run(
        ["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"]
    )
    assert code == EXIT_ERROR
    assert "already holds a mirror mailbox" in err
    assert (target / selfhost.STATE_FILENAME).is_symlink()


def test_message_size_limit_folgt_der_konfiguration(tmp_path: Path) -> None:
    """HOT-S9: Postfix' Grenze ist `[limits] max_mail_bytes`, sonst verschwindet Post still."""
    config = tmp_path / "config.toml"
    # Nur die eine Sektion: Die Erzeugung liest sonst nichts aus der Konfiguration und
    # soll an einer unvollständigen Datei nicht scheitern.
    config.write_text("[limits]\nmax_mail_bytes = 10485760\n", encoding="utf-8")
    target = tmp_path / "sh"
    code, _out, _err = run(
        ["--config", str(config), "selfhost-mail", "--domain", DOMAIN, "--out", str(target)]
    )
    assert code == EXIT_OK
    postfix = (target / "postfix.sh").read_text(encoding="utf-8")
    assert "postconf -e 'message_size_limit = 10485760'" in postfix


def test_ohne_konfiguration_gilt_die_vorgabegrenze(tmp_path: Path) -> None:
    """Mit `--out` und ohne Konfiguration bleibt es bei den 25 MiB der Vorgabe."""
    target = tmp_path / "sh"
    assert (
        run(["selfhost-mail", "--domain", DOMAIN, "--out", str(target), "--non-interactive"])[0]
        == EXIT_OK
    )
    assert "postconf -e 'message_size_limit = 26214400'" in (
        target / "postfix.sh"
    ).read_text(encoding="utf-8")


def test_ein_symlink_im_ausgabeverzeichnis_ist_ein_fehler_mit_satz(tmp_path: Path) -> None:
    """HOT-S4: Der Abbruch sagt, was zu tun ist, statt durch den Link zu schreiben."""
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n", encoding="utf-8")
    victim.chmod(0o644)
    link = tmp_path / "out" / "dns.txt"
    link.parent.mkdir()
    link.symlink_to(victim)
    # Direkt auf `_write_file`: `write_files` lehnt das nicht leere Verzeichnis schon
    # vorher ab; die zweite Schranke ist die gegen einen Link, der erst danach entsteht.
    with pytest.raises(selfhost.SelfhostError, match="symbolic link"):
        selfhost._write_file(link, "neu\n", 0o600)
    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_eine_vorlage_mit_unbekanntem_platzhalter_faellt_auf_die_vorlage(
    tmp_path: Path,
) -> None:
    """HOT-S6: Der Wächter prüft die Vorlage, nicht das Ergebnis — Daten bleiben Daten."""
    with pytest.raises(selfhost.SelfhostError, match=r"unfilled placeholder.*unbekannt"):
        selfhost._fill("a {{unbekannt}} b", {"domain": DOMAIN})
    assert selfhost._fill("{{domain}}", {"domain": "{{address}}"}) == "{{address}}"
