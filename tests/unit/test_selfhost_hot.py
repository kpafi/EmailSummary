"""Hot-Tester-Regressionen für `maildigest selfhost-mail` (PLAN.md §2, docs/TESTING.md §2).

Weißkasten-Runde gegen `selfhost.py`, `selfhost_check.py`, die Vorlagen unter
`data/selfhost/` und `cmd_selfhost_mail`. Jeder Test hier belegt **einen** Befund und
scheitert, solange er nicht behoben ist; keiner ist `xfail`.

Vertrag: docs/SPEC-CLI.md §4, docs/PLAN-SELFHOST-MAIL.md §3/§4, ADR-089, ADR-055 (HC-4),
Invariante I5.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from test_selfhost_check import DOMAIN, ScriptedSmtp, make_state, probes

from maildigest import cli, selfhost, selfhost_check
from maildigest.ingest.imap_client import IngestError
from maildigest.selfhost import MirrorState, SelfhostError

ADDRESS = f"mirror-aabbccdd@{DOMAIN}"

#: Steuerzeichen, mit denen ein manipulierter Zustand aus dem Feld gespielt wird.
CONTROL_CHARS = "\x1b\r\n\x00\x07"


def write_state_json(directory: Path, **overrides: Any) -> Path:
    """Schreibt eine `state.json` von Hand — auch eine, die der Generator nie erzeugt."""
    payload: dict[str, Any] = {
        "address": ADDRESS,
        "cert_dir": f"/etc/letsencrypt/live/{DOMAIN}",
        "domain": DOMAIN,
        "generated_at": "2026-09-19T12:04:57Z",
        "version": 1,
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / selfhost.STATE_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def rendered(**overrides: Any) -> dict[str, str]:
    """Die fünf Dateien mit den Vorgabewerten dieses Moduls."""
    values: dict[str, Any] = {
        "domain": DOMAIN,
        "address": ADDRESS,
        "cert_dir": f"/etc/letsencrypt/live/{DOMAIN}",
        "out_dir": Path("/tmp/selfhost-mail"),
    }
    values.update(overrides)
    return selfhost.render_files(**values)


# --- Befund 1: `state.json` nimmt jede Adresse an, die vorn und hinten passt ------------


@pytest.mark.parametrize(
    "local_junk",
    [
        "x\r\nRCPT TO:<evil@elsewhere.test>\r\n",  # SMTP-Kommandoeinschleusung
        'x"\r\n1 LOGOUT\r\n',  # IMAP-Kommandoeinschleusung (imaplib quotet, escapt aber nicht)
        "x\x1b[2JAll checks passed.",  # ANSI-Sequenz auf das Terminal
        "x y",  # Leerzeichen mitten in der Adresse
        pytest.param("x@" + "a" * 5000, id="absurd-length"),  # 5 kB lokaler Teil
    ],
)
def test_read_state_refuses_an_address_that_is_not_the_mirror_address(
    tmp_path: Path, local_junk: str
) -> None:
    """`address` wird nur vorn (`mirror-<hex>@`) und hinten (`@domain`) geprüft.

    Zwischen beiden Ankern steht, was der Angreifer will: CR/LF, Leerzeichen, ESC oder
    fünf Kilobyte. Der Wert geht danach unverändert in den SMTP-Dialog, in den
    IMAP-`LOGIN` und auf das Terminal. `read_state` muss ihn zurückweisen.
    """
    write_state_json(tmp_path, address=f"mirror-aabbccdd@{local_junk}@{DOMAIN}")
    with pytest.raises(SelfhostError):
        selfhost.read_state(tmp_path)


# --- Befund 2: Zustandstexte erreichen das Terminal ungefiltert -------------------------


class _FailingClient:
    """Ein IMAP-Client, dessen `connect()` scheitert — so entsteht die `FAIL`-Zeile."""

    def __init__(self, cfg: Any) -> None:
        del cfg

    def connect(self) -> None:
        raise IngestError("login failed")

    def disconnect(self) -> None:  # pragma: no cover - wird nie erreicht
        return None


def test_check_lines_carry_no_control_characters_from_state_json() -> None:
    """Jede Zeile von `--check` ist Ausgabe auf dem Terminal des Betreibers.

    Adresse und Domain stammen aus `state.json` und sind damit Fremdtext im Sinne von
    ADR-055/HC-4 — das Modul filtert Banner und Betreff, die Ratschläge aber nicht.
    """
    state = MirrorState(
        domain=DOMAIN,
        address=f"mirror-aabbccdd@\x1b[2J\x07{DOMAIN}",
        cert_dir=f"/etc/letsencrypt/live/{DOMAIN}",
        generated_at="2026-09-19T12:04:57Z",
    )
    results = selfhost_check.run_checks(
        state,
        password="geheim",
        wait_for_mail=True,
        timeout=10,
        probes=probes(imap_client=_FailingClient),
    )
    # Verbunden wird mit einem Leerzeichen, nicht mit `\n`: Die Behauptung deckt den
    # Zeilenumbruch selbst mit ab, ein Trenner mit Umbruch widerlegte sich also selbst.
    printed = " ".join(f"{result.text} {' '.join(result.warnings)}" for result in results)
    assert not any(char in printed for char in CONTROL_CHARS), repr(printed)


def test_mirror_ready_block_carries_no_control_characters_from_state_json() -> None:
    """Auch der Schlussblock druckt Adresse und Domain aus `state.json` wörtlich."""
    stdout = io.StringIO()
    console = cli.Console(io.StringIO(), stdout, io.StringIO(), interactive=False)
    state = MirrorState(
        domain=DOMAIN,
        address=f"mirror-aabbccdd@\x1b[2J{DOMAIN}",
        cert_dir=f"/etc/letsencrypt/live/{DOMAIN}",
        generated_at="2026-09-19T12:04:57Z",
    )
    cli._print_mirror_ready(console, state, wait_for_mail=True)
    assert "\x1b" not in stdout.getvalue()


# --- Befund 3: eine unerwartete Ausnahmeklasse verlässt den Prüflauf -------------------


def test_check_smtp_reports_fail_instead_of_raising_on_an_address_smtplib_rejects() -> None:
    """`check_smtp` fängt nur `OSError` und `SMTPException`.

    `smtplib.SMTP.putcmd` wirft für eine Adresse mit Zeilenumbruch jedoch `ValueError`;
    die Ausnahme verlässt `iter_checks` und die CLI hat keinen Fänger dafür — statt
    Exit-Code 1 bekommt der Betreiber einen Traceback. Eine Prüfung darf nie werfen.
    """
    state = make_state(address=f"mirror-aabbccdd@x\r\nDATA\r\n@{DOMAIN}")
    with ScriptedSmtp() as server:
        active = probes(smtp_host="127.0.0.1", smtp_port=server.port)
        results = selfhost_check.check_smtp(state, active)
    assert [result.name for result in results] == [
        selfhost_check.CHECK_SMTP_BANNER,
        selfhost_check.CHECK_SMTP_RELAY,
        selfhost_check.CHECK_SMTP_RECIPIENT,
    ]


# --- Befund 4: `write_files` folgt einem Symlink aus dem Ausgabeverzeichnis heraus ------


def test_write_files_does_not_write_through_a_symlink(tmp_path: Path) -> None:
    """Ein untergeschobener Symlink im `--out`-Verzeichnis lenkt den Schreibvorgang um.

    `os.open(..., O_CREAT)` ohne `O_NOFOLLOW` schreibt in das Ziel des Symlinks und das
    anschließende `os.chmod` setzt dort 0700 — eine fremde Datei wird überschrieben und
    ihre Rechte geändert.
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n", encoding="utf-8")
    victim.chmod(0o644)
    out = tmp_path / "out"
    out.mkdir()
    (out / "apply.sh").symlink_to(victim)

    with contextlib.suppress(SelfhostError):
        selfhost.write_files(out, rendered(out_dir=out))

    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    assert victim.stat().st_mode & 0o777 == 0o644


# --- Befund 5: `inet_interfaces` wird gesetzt, aber nur ein `reload` ausgelöst ----------


def test_apply_sh_restarts_postfix_because_postfix_sh_changes_inet_interfaces() -> None:
    """Postfix übernimmt `inet_interfaces` erst nach einem vollständigen Neustart.

    `postfix.sh` setzt den Schlüssel, `apply.sh` begnügt sich im letzten Schritt mit
    `reload`. Auf einem Host, der vorher `inet_interfaces = loopback-only` hatte (die
    „Local only"-Auswahl des Debian-Postfix-Installers), lauscht Postfix danach weiter
    nur auf Loopback — und `--check` prüft ausgerechnet 127.0.0.1 und meldet `ok`.
    """
    files = rendered()
    assert "postconf -e 'inet_interfaces = all'" in files["postfix.sh"]
    last_step = files["apply.sh"].split("6/6")[-1]
    assert "reload postfix" not in last_step
    assert "postfix reload" not in last_step
    assert "restart postfix" in last_step or "postfix stop" in last_step


# --- Befund 6: ein Ausgabeverzeichnis mit `{{` bricht die Erzeugung ab ------------------


def test_render_files_accepts_an_output_directory_that_contains_braces() -> None:
    """`--out '/srv/{{domain}}'` ist ein zulässiger Verzeichnisname.

    Der Pfad landet über `next_steps` in `checklist.txt`; `_fill` ersetzt `steps` zuletzt
    und findet danach ein übriges `{{` — die Erzeugung endet mit „internal error".
    """
    files = rendered(out_dir=Path("/srv/{{domain}}"))
    assert "/srv/{{domain}}/apply.sh" in files["checklist.txt"]


# --- Befund 7: der `sudo`-Befehl der Prüfliste ist nicht quotiert -----------------------


def test_next_steps_quotes_an_output_directory_that_needs_quoting() -> None:
    """Schritt 3 ist eine Zeile zum Abtippen — mit `sudo` davor.

    `--out '/srv/a b'` ergibt `sudo sh /srv/a b/apply.sh`; die Shell führt `/srv/a` mit
    dem Argument `b/apply.sh` aus. Ein Verzeichnisname mit Leerzeichen oder Semikolon ist
    zulässig, die daraus erzeugte Befehlszeile muss ihn schützen.
    """
    steps = selfhost.next_steps(
        domain=DOMAIN,
        address=ADDRESS,
        out_dir=Path("/srv/a b"),
        ipv4="203.0.113.7",
        ipv6=None,
    )
    assert "sudo sh /srv/a b/apply.sh" not in steps
    assert "'/srv/a b/apply.sh'" in steps or '"/srv/a b/apply.sh"' in steps


# --- Befund 8: Nicht-ASCII, das zu ASCII kleingeschrieben wird ---------------------------


def test_validate_domain_refuses_non_ascii_that_lowercases_to_ascii() -> None:
    """`--domain` wird erst kleingeschrieben und dann auf ASCII geprüft.

    Das Kelvin-Zeichen U+212A wird dabei zu `k`: Eine Eingabe mit Nicht-ASCII kommt
    durch, obwohl SPEC-CLI.md §4 dafür Exit-Code 2 und die IDN-Meldung vorschreibt.
    """
    with pytest.raises(selfhost.DomainError) as caught:
        selfhost.validate_domain("mirror.example.orK")
    assert caught.value.reason == "idn"


# --- Befund 9: `message_size_limit` ignoriert die konfigurierte Grenze ------------------


def test_postfix_message_size_limit_follows_the_configured_limit() -> None:
    """Der Wert ist fest verdrahtet, obwohl er `[limits] max_mail_bytes` spiegeln soll.

    Wer `max_mail_bytes` kleiner setzt, bekommt eine Mail, die Postfix annimmt und der
    Sanitizer danach verwirft: Sie verschwindet still, statt beim Weiterleiter zu
    bounce'n (PLAN-SELFHOST-MAIL §3).
    """
    files = selfhost.render_files(
        domain=DOMAIN,
        address=ADDRESS,
        cert_dir=f"/etc/letsencrypt/live/{DOMAIN}",
        out_dir=Path("/tmp/selfhost-mail"),
        max_mail_bytes=10_485_760,
    )
    assert "postconf -e 'message_size_limit = 10485760'" in files["postfix.sh"]


# --- Befund 10: die Prüfliste kennt nur `apt`, nennt aber Fedora als getragen ------------


def test_checklist_names_a_package_command_for_fedora() -> None:
    """SPEC-CLI.md §4 „Reach" nennt Fedora 43+, Schritt 2 nennt nur `apt install`."""
    checklist = rendered()["checklist.txt"]
    assert "Fedora 43 and newer" in checklist
    assert "dnf install" in checklist
