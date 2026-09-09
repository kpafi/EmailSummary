"""Ende-zu-Ende-Tests von `maildigest test` und `maildigest run` (WP9).

Diese Tests fahren die **echte** Verdrahtung aus WP8 (`build_runner`, Pipeline, State-DB,
Zustell-Warteschlange) und ersetzen nur die drei Außenkontakte: LLM-Stufen, Messenger und
IMAP. Damit prüfen sie genau das, was der Cold-Tester später über die CLI sieht.
"""

from __future__ import annotations

import io
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from maildigest.cli import EXIT_ERROR, EXIT_OK, Hooks, main
from maildigest.config import Config
from maildigest.ingest.imap_client import ImapConnectionError
from maildigest.llm.base import LLMTimeout
from maildigest.models import CriticVerdict, DigestMessage, SanitizedMail, Summary
from maildigest.runner import build_runner

CORPUS = Path(__file__).resolve().parents[1] / "corpus"


def write_config(tmp_path: Path, **overrides: str) -> Path:
    """Schreibt eine vollständige, gültige Konfiguration."""
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[general]
language = "de"
deliver_min_importance = "{overrides.get('min_importance', 'high')}"
state_db = "{tmp_path / 'state.db'}"
log_level = "ERROR"

[imap]
host = "imap.example.org"
username = "mirror@example.org"
password = "geheim"
folder = "INBOX"

[llm]
provider = "openai_compatible"
model = "testmodell"
base_url = "http://127.0.0.1:1/v1"

[messenger]
active = "discord"

[messenger.discord]
webhook_url = "https://discord.example/api/webhooks/1/x"
""",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@dataclass
class FakeSummarizer:
    """Summarizer-Attrappe mit fester Ausgabe."""

    summary: Summary
    error: Exception | None = None

    def summarize(self, mail: SanitizedMail) -> Summary:
        if self.error is not None:
            raise self.error
        return self.summary.model_copy(deep=True)


@dataclass
class FakeCritic:
    """Kritiker-Attrappe mit festem Verdikt."""

    verdict: CriticVerdict

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        return self.verdict.model_copy(deep=True)


@dataclass
class FakeMessenger:
    """Messenger-Attrappe; `fail` lässt jede Zustellung scheitern."""

    sent: list[DigestMessage] = field(default_factory=list)
    fail: bool = False

    def send(self, message: DigestMessage) -> None:
        if self.fail:
            from maildigest.messenger.base import MessengerError

            raise MessengerError("discord: Zustellung fehlgeschlagen (HTTP 500).")
        self.sent.append(message)

    def healthcheck(self) -> bool:
        return not self.fail


def make_hooks(
    *,
    messenger: FakeMessenger,
    summary: Summary | None = None,
    verdict: CriticVerdict | None = None,
    summarizer_error: Exception | None = None,
    client_factory: Any = None,
) -> Hooks:
    """Baut Hooks, die die echte Verdrahtung mit Attrappen an den Rändern benutzen."""
    default_summary = summary or Summary(
        headline="Heizungsablesung am Donnerstag",
        summary_text="Die Hausverwaltung kuendigt eine Ablesung an.",
        importance="normal",
        importance_reason="Termin mit Handlungsbedarf",
        category="termin",
    )
    default_verdict = verdict or CriticVerdict(phishing_risk="none", summary_accurate=True)

    def runner_factory(config: Config, **kwargs: Any) -> Any:
        kwargs.setdefault("messenger", messenger)
        if kwargs.get("messenger") is None:
            kwargs["messenger"] = messenger
        if client_factory is not None:
            kwargs["client_factory"] = client_factory
        kwargs["sleep"] = lambda _seconds: None
        return build_runner(config, **kwargs)

    return Hooks(
        build_runner=runner_factory,
        build_summarizer=lambda config: FakeSummarizer(default_summary, summarizer_error),
        build_critic=lambda config: FakeCritic(default_verdict),
    )


def run(argv: list[str], *, hooks: Hooks, stdin: str = "") -> tuple[int, str, str]:
    """Führt ein Kommando aus und liefert (Exit-Code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, hooks=hooks)
    return code, out.getvalue(), err.getvalue()


# --- maildigest test ----------------------------------------------------------------------


def test_selbsttest_stellt_die_beispielmail_zu(tmp_path: Path) -> None:
    messenger = FakeMessenger()
    code, out, _err = run(
        ["test", "--config", str(write_config(tmp_path))], hooks=make_hooks(messenger=messenger)
    )
    assert code == EXIT_OK
    assert len(messenger.sent) == 1
    text = "\n".join(messenger.sent[0].parts)
    assert "Heizungsablesung" in text
    # I3: Der Link der Beispielmail darf nirgends klickbar ankommen.
    assert "://" not in text
    assert "hausverwaltung-meier[.]example" in text
    for marker in ("1/5", "2/5", "3/5", "4/5", "5/5"):
        assert marker in out


def test_selbsttest_ignoriert_die_zustellschwelle(tmp_path: Path) -> None:
    # Der Nutzer stellt nur `high` zu, die Testmail ist `normal` — sie muss trotzdem kommen.
    messenger = FakeMessenger()
    code, _out, _err = run(
        ["test", "--config", str(write_config(tmp_path, min_importance="high"))],
        hooks=make_hooks(messenger=messenger),
    )
    assert code == EXIT_OK
    assert len(messenger.sent) == 1


def test_selbsttest_beruehrt_die_echte_state_datenbank_nicht(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    run(["test", "--config", str(config_path)], hooks=make_hooks(messenger=FakeMessenger()))
    assert not (tmp_path / "state.db").exists()


def test_selbsttest_trockenlauf_zeigt_die_nachricht(tmp_path: Path) -> None:
    messenger = FakeMessenger()
    code, out, _err = run(
        ["test", "--config", str(write_config(tmp_path)), "--dry-run"],
        hooks=make_hooks(messenger=messenger),
    )
    assert code == EXIT_OK
    assert messenger.sent == []
    assert "Heizungsablesung" in out
    assert "Dry run" in out


def test_selbsttest_mit_eigener_eml(tmp_path: Path) -> None:
    code, out, _err = run(
        [
            "test",
            "--config",
            str(write_config(tmp_path)),
            "--dry-run",
            "--eml",
            str(CORPUS / "03_attachment_pdf_ok.eml"),
        ],
        hooks=make_hooks(messenger=FakeMessenger()),
    )
    assert code == EXIT_OK
    # Genau ein Anhang ⇒ deutsche Einzahlform (SPEC-CLI §4, WP12).
    assert "1 attachment (" in out
    assert "1 attachments" not in out


def test_selbsttest_mit_phishing_mail_zeigt_banner(tmp_path: Path) -> None:
    messenger = FakeMessenger()
    verdict = CriticVerdict(
        phishing_risk="high",
        risk_reasons=["Absenderdomain passt nicht zum angeblichen Absender"],
        summary_accurate=True,
    )
    code, out, _err = run(
        [
            "test",
            "--config",
            str(write_config(tmp_path)),
            "--eml",
            str(CORPUS / "21_phishing_ceo_fraud.eml"),
        ],
        hooks=make_hooks(messenger=messenger, verdict=verdict),
    )
    assert code == EXIT_OK
    assert "phishing risk=high" in out
    assert "SUSPECTED PHISHING" in "\n".join(messenger.sent[0].parts)


def test_selbsttest_meldet_fail_closed_mit_exit_1(tmp_path: Path) -> None:
    messenger = FakeMessenger()
    code, out, err = run(
        ["test", "--config", str(write_config(tmp_path))],
        hooks=make_hooks(messenger=messenger, summarizer_error=LLMTimeout("Zeitlimit")),
    )
    assert code == EXIT_ERROR
    assert "Fail-closed" in out
    assert "llm_timeout" in out
    assert "failed" in err
    # F-OPS-3: Die Metadaten-Notiz geht trotzdem raus.
    assert len(messenger.sent) == 1
    assert "could not be processed safely" in messenger.sent[0].parts[0]


def test_selbsttest_meldet_gescheiterte_zustellung(tmp_path: Path) -> None:
    code, _out, err = run(
        ["test", "--config", str(write_config(tmp_path))],
        hooks=make_hooks(messenger=FakeMessenger(fail=True)),
    )
    assert code == EXIT_ERROR
    assert "queue" in err


def test_selbsttest_ohne_config_nennt_init(tmp_path: Path) -> None:
    code, _out, err = run(
        ["test", "--config", str(tmp_path / "fehlt.toml")],
        hooks=make_hooks(messenger=FakeMessenger()),
    )
    assert code == EXIT_ERROR
    assert "maildigest init" in err


def test_selbsttest_meldet_unvollstaendige_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[general]\nlanguage = "de"\n', encoding="utf-8")
    code, _out, err = run(
        ["test", "--config", str(path)], hooks=make_hooks(messenger=FakeMessenger())
    )
    assert code == EXIT_ERROR
    assert "required value missing" in err


def test_selbsttest_mit_muell_datei_stuerzt_nicht_ab(tmp_path: Path) -> None:
    """Der `email`-Parser ist bewusst tolerant: Müll ergibt eine leere Mail, keinen Absturz.

    Wichtig ist hier nur, dass die CLI einen definierten Exit-Code liefert und nichts
    Unsanitisiertes zustellt — nicht, dass sie die Datei ablehnt.
    """
    kaputt = tmp_path / "keine-mail.bin"
    kaputt.write_bytes(b"\x00\x01\x02")
    messenger = FakeMessenger()
    code, out, _err = run(
        ["test", "--config", str(write_config(tmp_path)), "--eml", str(kaputt)],
        hooks=make_hooks(messenger=messenger),
    )
    assert code in {EXIT_OK, EXIT_ERROR}
    assert "5/5" in out
    if messenger.sent:
        assert "://" not in "\n".join(messenger.sent[0].parts)


def test_selbsttest_meldet_fehlende_eml_datei(tmp_path: Path) -> None:
    code, _out, err = run(
        [
            "test",
            "--config",
            str(write_config(tmp_path)),
            "--eml",
            str(tmp_path / "gibtsnicht.eml"),
        ],
        hooks=make_hooks(messenger=FakeMessenger()),
    )
    assert code == EXIT_ERROR
    assert "cannot be read" in err


# --- maildigest run -----------------------------------------------------------------------


@dataclass
class FakeImapClient:
    """IMAP-Client-Attrappe für den `run --once`-Pfad (leeres Postfach)."""

    fail: bool = False
    polls: int = 0

    def connect(self) -> None:
        if self.fail:
            raise ImapConnectionError("IMAP-Verbindung fehlgeschlagen: OSError")

    def fetch_unseen(self) -> list[Any]:
        self.polls += 1
        return []

    def mark_processed(self, msg: Any) -> None:  # pragma: no cover - leeres Postfach
        raise AssertionError("unerreichbar")

    def disconnect(self) -> None:
        return None


def test_run_once_laeuft_und_meldet_die_bilanz(tmp_path: Path) -> None:
    client = FakeImapClient()
    code, _out, err = run(
        ["run", "--once", "--config", str(write_config(tmp_path))],
        hooks=make_hooks(messenger=FakeMessenger(), client_factory=lambda: client),
    )
    assert code == EXIT_OK
    assert client.polls == 1
    assert "Run finished" in err
    assert (tmp_path / "state.db").exists()


def test_run_once_meldet_unerreichbares_postfach(tmp_path: Path) -> None:
    code, _out, err = run(
        ["run", "--once", "--config", str(write_config(tmp_path))],
        hooks=make_hooks(
            messenger=FakeMessenger(), client_factory=lambda: FakeImapClient(fail=True)
        ),
    )
    assert code == EXIT_ERROR
    assert "Mailbox unreachable" in err


def test_run_konfiguriert_das_logging(tmp_path: Path) -> None:
    levels: list[str] = []
    hooks = make_hooks(messenger=FakeMessenger(), client_factory=lambda: FakeImapClient())
    hooks.configure_logging = lambda level, **kwargs: levels.append(level)
    code, _out, _err = run(["run", "--once", "--config", str(write_config(tmp_path))], hooks=hooks)
    assert code == EXIT_OK
    assert levels == ["ERROR"]


def test_run_ohne_config_nennt_init(tmp_path: Path) -> None:
    code, _out, err = run(
        ["run", "--once", "--config", str(tmp_path / "fehlt.toml")],
        hooks=make_hooks(messenger=FakeMessenger()),
    )
    assert code == EXIT_ERROR
    assert "maildigest init" in err


# --- Beispielmail --------------------------------------------------------------------------


def test_beispielmail_ist_mitgeliefert_und_lesbar() -> None:
    from importlib import resources

    data = resources.files("maildigest").joinpath("data/selftest.eml").read_bytes()
    assert b"Subject:" in data
    assert len(data) > 200


@pytest.mark.parametrize("name", ["01_multipart_plain_html.eml", "17_no_body_only_attachment.eml"])
def test_selbsttest_verarbeitet_korpus_faelle(tmp_path: Path, name: str) -> None:
    code, _out, _err = run(
        [
            "test",
            "--config",
            str(write_config(tmp_path)),
            "--dry-run",
            "--eml",
            str(CORPUS / name),
        ],
        hooks=make_hooks(messenger=FakeMessenger()),
    )
    assert code == EXIT_OK


def test_config_datei_bleibt_unveraendert_beim_selbsttest(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    before = path.read_bytes()
    run(["test", "--config", str(path)], hooks=make_hooks(messenger=FakeMessenger()))
    assert path.read_bytes() == before
    assert tomllib.loads(before.decode("utf-8"))["general"]["log_level"] == "ERROR"


def test_run_ohne_once_laeuft_bis_zum_stop_signal(tmp_path: Path) -> None:
    """`maildigest run` ohne `--once` geht in `run_forever` und endet mit Exit-Code 0."""
    aufrufe: list[str] = []

    class StoppingRunner:
        """Runner-Attrappe, die den Dauerbetrieb sofort beendet."""

        def __init__(self) -> None:
            self.db = _DummyDb()
            self.outbox = _DummyOutbox()

        def run_forever(self) -> None:
            aufrufe.append("forever")

    class _DummyDb:
        def close(self) -> None:
            aufrufe.append("closed")

    class _DummyOutbox:
        pending = 0

    hooks = make_hooks(messenger=FakeMessenger())
    hooks.build_runner = lambda config, **kwargs: StoppingRunner()
    code, _out, _err = run(["run", "--config", str(write_config(tmp_path))], hooks=hooks)
    assert code == EXIT_OK
    assert aufrufe == ["forever", "closed"]


def test_selbsttest_meldet_fehlende_llm_konfiguration(tmp_path: Path) -> None:
    """Ein Anthropic-Provider ohne Key ist ein Konfigurationsfehler, kein Absturz."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[imap]
host = "imap.example.org"
username = "m@example.org"
password = "geheim"

[llm]
provider = "anthropic"
model = "modell"

[messenger]
active = "discord"

[messenger.discord]
webhook_url = "https://discord.example/api/webhooks/1/x"
""",
        encoding="utf-8",
    )
    hooks = Hooks()  # echte Fabriken: SummarizerAgent.from_config muss den Key vermissen
    code, _out, err = run(["test", "--config", str(path)], hooks=hooks)
    assert code == EXIT_ERROR
    assert "api_key" in err


# --- Regressionen aus dem Cold-Test (tests/cold/REPORT.md) ---------------------------------


def test_ct4_trockenlauf_meldet_keine_zustellung_und_zeigt_die_notiz(tmp_path: Path) -> None:
    """CT-4: Im Trockenlauf wird nichts zugestellt — das muss die Ausgabe auch sagen.

    Vorher meldete `test --dry-run` bei fail-closed „zugestellt: ja", obwohl nichts
    hinausging, und die Metadaten-Notiz selbst war nirgends zu sehen.
    """
    messenger = FakeMessenger()
    code, out, err = run(
        ["test", "--config", str(write_config(tmp_path)), "--dry-run"],
        hooks=make_hooks(messenger=messenger, summarizer_error=LLMTimeout("Zeitlimit")),
    )
    assert code == EXIT_ERROR
    assert messenger.sent == []
    assert "Fail-closed" in out
    # Die Notiz steht auf stdout — genau dafür ist --dry-run da.
    assert "could not be processed safely" in out
    assert "Subject:" in out
    # Und die Bilanz behauptet keine Zustellung mehr.
    assert "delivered: yes" not in err
    assert "delivered: no" in err
    assert "dry run" in err


def test_ct4_ohne_trockenlauf_bleibt_die_zustellmeldung_ehrlich(tmp_path: Path) -> None:
    """Gegenprobe zu CT-4: Kommt die Notiz nicht durch, steht dort „nein"."""
    code, _out, err = run(
        ["test", "--config", str(write_config(tmp_path))],
        hooks=make_hooks(
            messenger=FakeMessenger(fail=True), summarizer_error=LLMTimeout("Zeitlimit")
        ),
    )
    assert code == EXIT_ERROR
    assert "delivered: no" in err


@dataclass
class DeliveringImapClient:
    """IMAP-Attrappe, die ein Postfach mit `count` identischen Mails vorspielt."""

    count: int = 3
    polls: int = 0

    def connect(self) -> None:
        return None

    def fetch_unseen(self) -> list[Any]:
        from imap_tools import MailMessage as _MailMessage

        self.polls += 1
        if self.polls > 1:
            return []
        messages = []
        for index in range(1, self.count + 1):
            raw = (
                f"From: Absender {index} <a{index}@beispiel-fuer-tests.example>\r\n"
                f"To: mirror@example.org\r\n"
                f"Subject: Testmail {index}\r\n"
                f"Message-ID: <ct10-{index}@beispiel-fuer-tests.example>\r\n"
                f"Date: Thu, 12 Mar 2026 09:14:00 +0100\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                f"Kurzer harmloser Text Nummer {index}.\r\n"
            ).encode()
            messages.append(
                _MailMessage([(f"1 (UID {index} FLAGS ())".encode(), raw), b")"])
            )
        return messages

    def mark_processed(self, msg: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None


def test_ct10_bilanz_zaehlt_direkt_zugestellte_nachrichten(tmp_path: Path) -> None:
    """CT-10: Sofort zugestellte Nachrichten tauchen nie in `flush()` auf — trotzdem zählen.

    Vorher meldete die Bilanzzeile im Normalbetrieb dauerhaft „0 Nachrichten zugestellt".
    """
    messenger = FakeMessenger()
    client = DeliveringImapClient(count=3)
    code, _out, err = run(
        ["run", "--once", "--config", str(write_config(tmp_path, min_importance="low"))],
        hooks=make_hooks(messenger=messenger, client_factory=lambda: client),
    )
    assert code == EXIT_OK
    assert len(messenger.sent) == 3
    assert "3 mails fetched, 3 processed" in err
    assert "3 messages delivered" in err
    assert "0 queued" in err


def test_ct10_leeres_postfach_meldet_weiterhin_null(tmp_path: Path) -> None:
    """Gegenprobe zu CT-10: Ohne Mail wird auch nichts gezählt."""
    code, _out, err = run(
        ["run", "--once", "--config", str(write_config(tmp_path))],
        hooks=make_hooks(messenger=FakeMessenger(), client_factory=lambda: FakeImapClient()),
    )
    assert code == EXIT_OK
    assert "0 messages delivered" in err


def test_ct10_gescheiterte_zustellung_wird_nicht_als_zugestellt_gezaehlt(
    tmp_path: Path,
) -> None:
    """Gegenprobe zu CT-10: Was in der Warteschlange landet, ist nicht zugestellt."""
    code, _out, err = run(
        ["run", "--once", "--config", str(write_config(tmp_path, min_importance="low"))],
        hooks=make_hooks(
            messenger=FakeMessenger(fail=True),
            client_factory=lambda: DeliveringImapClient(count=2),
        ),
    )
    assert code == EXIT_OK
    assert "0 messages delivered" in err
    assert "2 queued" in err
