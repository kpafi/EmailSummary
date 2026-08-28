"""Unit-Tests (WP1) für das Pipeline-Skeleton aus `maildigest.pipeline`.

Alle Stufen sind Stubs (siehe unten). Abgedeckt sind der Happy-Path bis :class:`Delivered`,
der :class:`QueuedLow`-Pfad, die Sonderregeln für Phishing-`high` (F-CRIT-2) sowie **jeder**
Fehlerpfad (Sanitizer, Summarizer, Kritiker, Composer, Messenger) mit dem geforderten
Fail-closed-Verhalten (I6) — inklusive der Absicherung, dass `RawMail`/`mime_bytes` nach der
Sanitize-Stufe strukturell nicht mehr weitergereicht wird (I1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from maildigest.models import (
    CriticVerdict,
    DigestMessage,
    FailureNotice,
    Importance,
    PhishingRisk,
    RawMail,
    SanitizationReport,
    SanitizedMail,
    Summary,
)
from maildigest.pipeline import (
    Delivered,
    FailedNotice,
    PipelineDeps,
    PipelineResult,
    QueuedLow,
    classify_failure,
    process_mail,
)

# --- Fixtures / Bausteine -----------------------------------------------------------------

MIME_MARKER = b"From: a@example.org\r\n\r\nGEHEIMER-ROHINHALT"


def make_raw(subject: str = "Rechnung Stadtwerke") -> RawMail:
    """Eine RawMail mit erkennbarem `mime_bytes`-Marker (für die I1-Prüfung)."""
    return RawMail(
        message_id="<abc@example.org>",
        dedupe_key="key-1",
        from_addr="Stadtwerke <rechnung@stadtwerke-x.de>",
        from_domain="stadtwerke-x.de",
        to_addrs=["mirror@example.org"],
        subject_raw=subject,
        date=datetime(2026, 8, 28, 14, 12, tzinfo=UTC),
        mime_bytes=MIME_MARKER,
        size_bytes=len(MIME_MARKER),
    )


def make_sanitized() -> SanitizedMail:
    return SanitizedMail(
        dedupe_key="key-1",
        from_display="Stadtwerke",
        from_domain="stadtwerke-x.de",
        subject="Rechnung Stadtwerke",
        date=datetime(2026, 8, 28, 14, 12, tzinfo=UTC),
        body_text="Bitte zahlen Sie 84,30 EUR bis 15.09. [Link #1: stadtwerke-x.de]",
        sanitization_report=SanitizationReport(links_removed=1),
    )


def make_summary(importance: Importance = "normal") -> Summary:
    return Summary(
        headline="Rechnung Stadtwerke März",
        summary_text="Zahlung von 84,30 EUR bis 15.09. gefordert.",
        importance=importance,
        importance_reason="Zahlungsfrist",
        category="rechnung",
    )


def make_verdict(
    *, risk: PhishingRisk = "none", accurate: bool = True
) -> CriticVerdict:
    return CriticVerdict(
        phishing_risk=risk,
        risk_reasons=[] if risk == "none" else ["Zahlungsaufforderung"],
        summary_accurate=accurate,
    )


class StageError(Exception):
    """Generischer Stufenfehler ohne bekannte Fehlerklasse (Default-Klassifikation)."""


class SanitizeError(Exception):
    """Trägt denselben Namen wie die spätere WP3-Fehlerklasse (Klassifikations-Test)."""


# --- Stub-Stufen ---------------------------------------------------------------------------


@dataclass
class StubSanitizer:
    result: SanitizedMail = field(default_factory=make_sanitized)
    raises: Exception | None = None
    seen_mime_bytes: bytes | None = None

    def sanitize(self, raw: RawMail) -> SanitizedMail:
        self.seen_mime_bytes = raw.mime_bytes
        if self.raises is not None:
            raise self.raises
        return self.result


@dataclass
class StubSummarizer:
    result: Summary = field(default_factory=make_summary)
    raises: Exception | None = None
    calls: list[SanitizedMail] = field(default_factory=list)

    def summarize(self, mail: SanitizedMail) -> Summary:
        self.calls.append(mail)
        if self.raises is not None:
            raise self.raises
        return self.result


@dataclass
class StubCritic:
    result: CriticVerdict = field(default_factory=make_verdict)
    raises: Exception | None = None
    calls: list[tuple[SanitizedMail, Summary]] = field(default_factory=list)

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict:
        self.calls.append((mail, summary))
        if self.raises is not None:
            raise self.raises
        return self.result


@dataclass
class StubComposer:
    raises: Exception | None = None
    failure_raises: Exception | None = None
    compose_calls: list[tuple[SanitizedMail, Summary, CriticVerdict]] = field(
        default_factory=list
    )
    failure_calls: list[FailureNotice] = field(default_factory=list)

    def compose(
        self, mail: SanitizedMail, summary: Summary, verdict: CriticVerdict
    ) -> DigestMessage:
        self.compose_calls.append((mail, summary, verdict))
        if self.raises is not None:
            raise self.raises
        return DigestMessage(
            parts=[f"{summary.headline}\n{summary.summary_text}"],
            importance=summary.importance,
            is_warning=verdict.phishing_risk == "high",
            dedupe_key=mail.dedupe_key,
        )

    def compose_failure(self, notice: FailureNotice) -> DigestMessage:
        self.failure_calls.append(notice)
        if self.failure_raises is not None:
            raise self.failure_raises
        return DigestMessage(
            parts=[
                f"Mail von {notice.from_domain} mit Betreff "
                f"{notice.subject_sanitized} konnte nicht sicher verarbeitet werden "
                f"({notice.stage}/{notice.reason_class})."
            ],
            importance="normal",
            dedupe_key=notice.dedupe_key,
        )


@dataclass
class StubMessenger:
    raises: Exception | None = None
    sent: list[DigestMessage] = field(default_factory=list)

    def send(self, message: DigestMessage) -> None:
        self.sent.append(message)
        if self.raises is not None:
            raise self.raises


def make_deps(
    *,
    sanitizer: StubSanitizer | None = None,
    summarizer: StubSummarizer | None = None,
    critic: StubCritic | None = None,
    composer: StubComposer | None = None,
    messenger: StubMessenger | None = None,
    deliver_min_importance: Importance = "normal",
) -> PipelineDeps:
    return PipelineDeps(
        sanitizer=sanitizer or StubSanitizer(),
        summarizer=summarizer or StubSummarizer(),
        critic=critic or StubCritic(),
        composer=composer or StubComposer(),
        messenger=messenger or StubMessenger(),
        deliver_min_importance=deliver_min_importance,
    )


# --- Happy-Path ----------------------------------------------------------------------------


def test_happy_path_delivers() -> None:
    """Normale Mail über der Schwelle läuft bis zur Zustellung durch."""
    messenger = StubMessenger()
    composer = StubComposer()
    deps = make_deps(composer=composer, messenger=messenger)

    result = process_mail(make_raw(), deps)

    assert isinstance(result, Delivered)
    assert result.status == "delivered"
    assert result.dedupe_key == "key-1"
    assert messenger.sent == [result.message]
    assert composer.failure_calls == []
    assert result.message.parts[0].startswith("Rechnung Stadtwerke März")


def test_high_importance_is_delivered_even_with_high_threshold() -> None:
    """`high`-Mails werden auch bei Schwelle `high` zugestellt."""
    deps = make_deps(
        summarizer=StubSummarizer(result=make_summary("high")),
        deliver_min_importance="high",
    )

    assert isinstance(process_mail(make_raw(), deps), Delivered)


def test_stages_receive_sanitized_mail_not_raw_mail() -> None:
    """I1: Nach der Sanitize-Stufe sieht keine Stufe mehr `RawMail`/`mime_bytes`."""
    sanitized = make_sanitized()
    sanitizer = StubSanitizer(result=sanitized)
    summarizer = StubSummarizer()
    critic = StubCritic()
    composer = StubComposer()
    deps = make_deps(
        sanitizer=sanitizer, summarizer=summarizer, critic=critic, composer=composer
    )

    process_mail(make_raw(), deps)

    assert sanitizer.seen_mime_bytes == MIME_MARKER  # nur der Sanitizer sieht die Bytes
    assert summarizer.calls == [sanitized]
    assert critic.calls[0][0] is sanitized
    assert composer.compose_calls[0][0] is sanitized
    for stage_input in (*summarizer.calls, critic.calls[0][0], composer.compose_calls[0][0]):
        assert not isinstance(stage_input, RawMail)
        assert not hasattr(stage_input, "mime_bytes")


def test_delivered_message_contains_no_raw_mime_content() -> None:
    """I1/I3: Der Roh-MIME-Marker taucht in keiner zugestellten Nachricht auf."""
    messenger = StubMessenger()
    process_mail(make_raw(), make_deps(messenger=messenger))

    for message in messenger.sent:
        for part in message.parts:
            assert "GEHEIMER-ROHINHALT" not in part


# --- QueuedLow -----------------------------------------------------------------------------


def test_low_importance_is_queued_instead_of_delivered() -> None:
    """Mails unter der Schwelle landen im Sammel-Digest, nicht beim Messenger (F-SUM-5)."""
    messenger = StubMessenger()
    composer = StubComposer()
    deps = make_deps(
        summarizer=StubSummarizer(result=make_summary("low")),
        composer=composer,
        messenger=messenger,
    )

    result = process_mail(make_raw(), deps)

    assert isinstance(result, QueuedLow)
    assert result.status == "skipped_low"
    assert result.dedupe_key == "key-1"
    assert result.from_domain == "stadtwerke-x.de"
    assert result.summary.importance == "low"
    assert messenger.sent == []
    assert composer.compose_calls == []


def test_threshold_high_queues_normal_mails() -> None:
    """Bei Schwelle `high` wird auch `normal` nur gesammelt."""
    deps = make_deps(deliver_min_importance="high")

    assert isinstance(process_mail(make_raw(), deps), QueuedLow)


def test_threshold_low_delivers_everything() -> None:
    """Bei Schwelle `low` wird jede Mail einzeln zugestellt."""
    deps = make_deps(
        summarizer=StubSummarizer(result=make_summary("low")),
        deliver_min_importance="low",
    )

    assert isinstance(process_mail(make_raw(), deps), Delivered)


def test_high_phishing_risk_never_lands_in_low_digest() -> None:
    """F-CRIT-2: `phishing_risk = high` erzwingt Einzelzustellung mit Warn-Flag."""
    messenger = StubMessenger()
    deps = make_deps(
        summarizer=StubSummarizer(result=make_summary("low")),
        critic=StubCritic(result=make_verdict(risk="high")),
        messenger=messenger,
        deliver_min_importance="high",
    )

    result = process_mail(make_raw(), deps)

    assert isinstance(result, Delivered)
    assert result.message.is_warning is True
    assert result.message.importance == "normal"  # von `low` angehoben
    assert len(messenger.sent) == 1


# --- Fehlerpfade (I6, fail-closed) ---------------------------------------------------------


def _assert_failed_notice(
    result: PipelineResult, *, stage: str, reason_class: str
) -> FailedNotice:
    assert isinstance(result, FailedNotice)
    assert result.status == "failed"
    assert result.notice.stage == stage
    assert result.notice.reason_class == reason_class
    assert result.notice.dedupe_key == "key-1"
    assert result.notice.from_domain == "stadtwerke-x.de"
    return result


def test_sanitizer_failure_is_fail_closed() -> None:
    """Sanitizer wirft ⇒ Metadaten-Notiz, keine Zusammenfassung."""
    summarizer = StubSummarizer()
    composer = StubComposer()
    messenger = StubMessenger()
    deps = make_deps(
        sanitizer=StubSanitizer(raises=SanitizeError("kaputtes MIME")),
        summarizer=summarizer,
        composer=composer,
        messenger=messenger,
    )

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="sanitize", reason_class="sanitize_error"
    )

    assert result.notice_delivered is True
    assert summarizer.calls == []  # keine Folgestufe wurde betreten
    assert composer.compose_calls == []
    assert composer.failure_calls == [result.notice]
    assert len(messenger.sent) == 1


def test_summarizer_failure_is_fail_closed() -> None:
    """Summarizer wirft ⇒ Notiz; Kritiker und Composer werden nicht aufgerufen."""
    critic = StubCritic()
    composer = StubComposer()
    deps = make_deps(
        summarizer=StubSummarizer(raises=StageError("LLM kaputt")),
        critic=critic,
        composer=composer,
    )

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="summarize", reason_class="summarize_error"
    )

    assert result.notice_delivered is True
    assert critic.calls == []
    assert composer.compose_calls == []


def test_critic_failure_is_fail_closed() -> None:
    """Kritiker wirft ⇒ Notiz statt ungeprüfter Zusammenfassung."""
    composer = StubComposer()
    deps = make_deps(critic=StubCritic(raises=StageError("Timeout")), composer=composer)

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="critic", reason_class="critic_error"
    )

    assert result.notice_delivered is True
    assert composer.compose_calls == []


def test_inaccurate_summary_is_fail_closed() -> None:
    """T8: `summary_accurate = false` ⇒ Notiz statt (halluzinierter) Zusammenfassung."""
    composer = StubComposer()
    messenger = StubMessenger()
    deps = make_deps(
        critic=StubCritic(result=make_verdict(accurate=False)),
        composer=composer,
        messenger=messenger,
    )

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="critic", reason_class="summary_inaccurate"
    )

    assert result.notice_delivered is True
    assert composer.compose_calls == []
    assert len(messenger.sent) == 1


def test_inaccurate_summary_beats_low_threshold_shortcut() -> None:
    """Auch eine `low`-Mail wird bei ungenauer Summary zur Notiz, nicht zu QueuedLow."""
    deps = make_deps(
        summarizer=StubSummarizer(result=make_summary("low")),
        critic=StubCritic(result=make_verdict(accurate=False)),
    )

    _assert_failed_notice(
        process_mail(make_raw(), deps), stage="critic", reason_class="summary_inaccurate"
    )


def test_composer_failure_is_fail_closed() -> None:
    """Composer wirft ⇒ Notiz; es wird nichts Ungeprüftes gesendet."""
    composer = StubComposer(raises=StageError("Formatfehler"))
    messenger = StubMessenger()
    deps = make_deps(composer=composer, messenger=messenger)

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="compose", reason_class="compose_error"
    )

    assert result.notice_delivered is True
    assert len(messenger.sent) == 1
    assert composer.failure_calls == [result.notice]


def test_messenger_failure_is_fail_closed() -> None:
    """Messenger wirft ⇒ FailedNotice; auch der Notiz-Versand scheitert dann."""
    messenger = StubMessenger(raises=StageError("HTTP 500"))
    deps = make_deps(messenger=messenger)

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="deliver", reason_class="deliver_error"
    )

    assert result.notice_delivered is False
    assert len(messenger.sent) == 2  # Original + Notiz, beide gescheitert


def test_failure_notice_delivery_failure_is_reported() -> None:
    """Scheitert der Notiz-Versand selbst, ist `notice_delivered = False` (WP8 retryt)."""
    deps = make_deps(
        sanitizer=StubSanitizer(raises=StageError("boom")),
        composer=StubComposer(failure_raises=StageError("Composer auch kaputt")),
    )

    result = _assert_failed_notice(
        process_mail(make_raw(), deps), stage="sanitize", reason_class="sanitize_error"
    )

    assert result.notice_delivered is False


def test_pipeline_never_raises_on_stage_exceptions() -> None:
    """Keine Stufen-Exception verlässt `process_mail` — auch nicht in Kombination."""
    deps = make_deps(
        sanitizer=StubSanitizer(raises=StageError("a")),
        composer=StubComposer(failure_raises=StageError("b")),
        messenger=StubMessenger(raises=StageError("c")),
    )

    assert isinstance(process_mail(make_raw(), deps), FailedNotice)


# --- Notiz-Inhalt: keine Inhalte, keine Details (I5) ---------------------------------------


def test_failure_notice_contains_no_error_text_or_mail_content() -> None:
    """I5: Weder Exception-Text noch Mail-Inhalt landen in der Notiz."""
    deps = make_deps(sanitizer=StubSanitizer(raises=StageError("Passwort: hunter2")))
    messenger = deps.messenger
    assert isinstance(messenger, StubMessenger)

    result = process_mail(make_raw(), deps)

    assert isinstance(result, FailedNotice)
    serialized = result.notice.model_dump_json()
    assert "hunter2" not in serialized
    assert "GEHEIMER-ROHINHALT" not in serialized
    for part in messenger.sent[0].parts:
        assert "hunter2" not in part


@pytest.mark.parametrize(
    ("subject_raw", "expected"),
    [
        ("Normaler Betreff", "Normaler Betreff"),
        ("", "(kein Betreff)"),
        ("   \t \n ", "(kein Betreff)"),
        ("Rechnung‮gnudlehcaN", "Rechnung gnudlehcaN"),
        ("Zero​Width", "Zero Width"),
        ("Mehrere    Leerzeichen\nund Zeilen", "Mehrere Leerzeichen und Zeilen"),
        ("Umlaut äöü", "Umlaut"),
    ],
)
def test_notice_subject_is_reduced_to_printable_ascii(
    subject_raw: str, expected: str
) -> None:
    """Der Not-Sanitizer des Betreffs entfernt Steuer-/Bidi-/Nicht-ASCII-Zeichen (T2/T12)."""
    deps = make_deps(sanitizer=StubSanitizer(raises=StageError("x")))

    result = process_mail(make_raw(subject=subject_raw), deps)

    assert isinstance(result, FailedNotice)
    assert result.notice.subject_sanitized == expected


def test_notice_subject_is_truncated() -> None:
    """Überlange Betreffs werden hart gekürzt."""
    deps = make_deps(sanitizer=StubSanitizer(raises=StageError("x")))

    result = process_mail(make_raw(subject="A" * 500), deps)

    assert isinstance(result, FailedNotice)
    assert len(result.notice.subject_sanitized) == 120
    assert result.notice.subject_sanitized.endswith("…")


# --- Fehlerklassifikation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_name", "expected"),
    [
        ("SanitizeError", "sanitize_error"),
        ("LLMTimeout", "llm_timeout"),
        ("LLMRateLimited", "llm_rate_limited"),
        ("LLMInvalidResponse", "llm_invalid_response"),
        ("ValidationError", "schema_invalid"),
        ("MessengerError", "delivery_error"),
    ],
)
def test_classify_failure_maps_known_exception_names(exc_name: str, expected: str) -> None:
    """Bekannte (spätere) Fehlerklassen werden über den Klassennamen zugeordnet."""
    exc_type = type(exc_name, (Exception,), {})
    assert classify_failure("summarize", exc_type()) == expected


def test_classify_failure_falls_back_to_stage() -> None:
    """Unbekannte Exceptions ergeben eine stufenbezogene Sammelklasse."""
    assert classify_failure("compose", RuntimeError("irgendwas")) == "compose_error"


def test_classify_failure_ignores_exception_message() -> None:
    """I5: Der Exception-Text fließt nie in die Fehlerklasse ein."""
    assert "geheim" not in classify_failure("deliver", RuntimeError("geheim"))
