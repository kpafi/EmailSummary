"""Orchestrierung der Pipeline-Stufen 2-6 (Sanitize, Summarize, Critic, Output-Sanitize,
Deliver) mit Fail-closed-Verhalten (I6).

Vertrag: docs/ARCHITECTURE.md §4. Skeleton in WP1, Finalisierung (State/Retries/Low-Digest)
in WP8.

Sicherheits-Design dieses Moduls:

* **I1 strukturell:** `process_mail` reicht das :class:`~maildigest.models.RawMail`-Objekt
  (und damit `mime_bytes`) niemals an eine Stufe nach dem Sanitizer weiter. Die Referenz
  wird direkt nach dem Sanitize-Aufruf per ``del`` aus dem Namensraum entfernt; ein späterer
  Zugriff wäre ein `NameError` und würde in Tests/Typprüfung sofort auffallen. Alles, was
  nachfolgende Stufen und der Fehlerpfad an Metadaten brauchen, steckt in :class:`MailRef`.
* **I6 fail-closed:** Jede Exception einer Stufe wird gefangen, zu einer groben Fehlerklasse
  verdichtet (keine Fehlertexte, keine Mail-Inhalte) und zu einer
  :class:`~maildigest.models.FailureNotice` — nie zur Zustellung ungeprüften Inhalts.
* **I5:** In Ergebnis- und Notiz-Objekten stehen ausschließlich Metadaten (Domain, not-
  sanitisierter Betreff, Stufe, Fehlerklasse), niemals Secrets oder Mail-Text.

Die Stufen selbst sind hier nur Protokolle (`typing.Protocol`) — implementiert in WP3, WP5,
WP6 und WP7 und in Tests durch Stubs ersetzbar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from maildigest.models import (
    CriticVerdict,
    DigestMessage,
    FailureNotice,
    Importance,
    RawMail,
    SanitizedMail,
    Summary,
)

__all__ = [
    "Critic",
    "Delivered",
    "FailedNotice",
    "MailRef",
    "MailStatus",
    "Messenger",
    "OutputComposer",
    "PipelineDeps",
    "PipelineResult",
    "ProgressSink",
    "ProgressState",
    "QueuedLow",
    "Sanitizer",
    "Stage",
    "Summarizer",
    "classify_failure",
    "process_mail",
]

#: Pipeline-Stufen, wie sie in `FailureNotice.stage` erscheinen.
Stage = Literal["sanitize", "summarize", "critic", "compose", "deliver"]

#: Endstatus einer Mail für `state/db.py` (docs/ARCHITECTURE.md §2, Statuswerte).
MailStatus = Literal["delivered", "skipped_low", "failed"]

#: Zwischenstände, die die Pipeline während des Durchlaufs meldet (WP8, ADR-050).
#: `checked` wird bewusst **vor** dem Versand gemeldet und vom Sink committet (ADR-008).
ProgressState = Literal["sanitized", "summarized", "checked"]

#: Rangfolge der Wichtigkeitsstufen für den Schwellwertvergleich.
_IMPORTANCE_RANK: dict[str, int] = {"low": 0, "normal": 1, "high": 2}

#: Maximale Länge des not-sanitisierten Betreffs in einer FailureNotice.
_NOTICE_SUBJECT_MAX_CHARS = 120


class _InaccurateSummaryError(Exception):
    """Interner Marker: Kritiker meldet `summary_accurate = false` (T8, fail-closed)."""


#: Bekannte Exception-Namen → grobe, stabile Fehlerklassen. Bewusst über den Klassennamen
#: statt über Imports, damit `pipeline.py` nicht von späteren WP-Modulen abhängt.
_ERROR_CLASSES: dict[str, str] = {
    "_InaccurateSummaryError": "summary_inaccurate",
    "SanitizeError": "sanitize_error",
    "LLMTimeout": "llm_timeout",
    "LLMRateLimited": "llm_rate_limited",
    "LLMInvalidResponse": "llm_invalid_response",
    "LLMTransportError": "llm_transport_error",
    "ValidationError": "schema_invalid",
    "MessengerError": "delivery_error",
    "StateError": "state_error",
}


class MailRef(BaseModel):
    """Minimaler Metadaten-Abzug einer Mail, der den Sanitizer überlebt.

    Existiert, damit der Fehlerpfad eine :class:`FailureNotice` bauen kann, ohne dass das
    `RawMail`-Objekt (mit `mime_bytes`) weitergereicht werden muss — strukturelle
    Absicherung von I1 (docs/ARCHITECTURE.md §4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dedupe_key: str
    from_domain: str
    subject_sanitized: str


# --- Stufen-Protokolle (Dependency Injection) ---------------------------------------------


@runtime_checkable
class Sanitizer(Protocol):
    """Stufe 2: `RawMail` → `SanitizedMail`, reiner Code, kein LLM (WP3)."""

    def sanitize(self, raw: RawMail) -> SanitizedMail: ...


@runtime_checkable
class Summarizer(Protocol):
    """Stufe 3: `SanitizedMail` → `Summary` (rechteloses LLM, WP5)."""

    def summarize(self, mail: SanitizedMail) -> Summary: ...


@runtime_checkable
class Critic(Protocol):
    """Stufe 4: `SanitizedMail` + `Summary` → `CriticVerdict` (rechteloses LLM, WP6)."""

    def review(self, mail: SanitizedMail, summary: Summary) -> CriticVerdict: ...


@runtime_checkable
class OutputComposer(Protocol):
    """Stufe 5: Zusammenbau + letzte deterministische Säuberung der Nachricht (WP7)."""

    def compose(
        self, mail: SanitizedMail, summary: Summary, verdict: CriticVerdict
    ) -> DigestMessage: ...

    def compose_failure(self, notice: FailureNotice) -> DigestMessage: ...


@runtime_checkable
class Messenger(Protocol):
    """Stufe 6: Zustellung (WP7).

    Absichtlich schmaler als das vollständige Protokoll in `messenger/base.py` (dort
    zusätzlich `healthcheck()`): Die Pipeline verlangt nur, was sie tatsächlich aufruft.
    """

    def send(self, message: DigestMessage) -> None: ...


@runtime_checkable
class ProgressSink(Protocol):
    """Meldeweg für die Zwischenstände einer Mail (WP8: `state/db.py`).

    Der Sink muss den Stand **committen**, bevor er zurückkehrt: `checked` ist laut ADR-008
    die Zusage „ab hier darf höchstens doppelt zugestellt werden, nie verloren gehen".
    Wirft der Sink, behandelt die Pipeline das wie einen Stufenfehler (fail-closed, I6).
    """

    def record(self, dedupe_key: str, state: ProgressState) -> None: ...


@dataclass(frozen=True)
class PipelineDeps:
    """Injizierte Stufen + die Policy-Parameter, die die Pipeline selbst auswertet."""

    sanitizer: Sanitizer
    summarizer: Summarizer
    critic: Critic
    composer: OutputComposer
    messenger: Messenger
    deliver_min_importance: Importance = "normal"
    progress: ProgressSink | None = None


# --- Ergebnis-Typen ------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivered:
    """Mail wurde als Einzelnachricht zugestellt."""

    dedupe_key: str
    message: DigestMessage
    status: MailStatus = "delivered"


@dataclass(frozen=True)
class QueuedLow:
    """Mail liegt unter der Zustellschwelle → Sammel-Digest (F-SUM-5, Versand in WP8)."""

    dedupe_key: str
    from_domain: str
    summary: Summary
    status: MailStatus = "skipped_low"


@dataclass(frozen=True)
class FailedNotice:
    """Fail-closed-Ergebnis: statt Inhalt gibt es die Metadaten-Notiz (I6, F-OPS-3).

    `notice_delivered` ist False, wenn auch der Versand der Notiz selbst scheiterte —
    dann muss WP8 erneut zustellen.
    """

    notice: FailureNotice
    notice_delivered: bool
    status: MailStatus = "failed"

    @property
    def dedupe_key(self) -> str:
        """Dedupe-Key der betroffenen Mail (einheitlicher Zugriff über alle Ergebnistypen)."""
        return self.notice.dedupe_key


#: Ergebnis eines Pipeline-Durchlaufs (docs/ARCHITECTURE.md §4).
PipelineResult = Delivered | QueuedLow | FailedNotice


# --- Hilfsfunktionen -----------------------------------------------------------------------


def classify_failure(stage: Stage, exc: BaseException) -> str:
    """Verdichtet eine Exception zu einer groben, stabilen Fehlerklasse.

    Bewusst **ohne** Exception-Text: Der könnte Mail-Inhalte oder Secrets enthalten und
    landet über die Notiz beim Nutzer bzw. im State (I5, docs/SECURITY.md §6).
    """
    return _ERROR_CLASSES.get(type(exc).__name__, f"{stage}_error")


def _notice_subject(subject_raw: str) -> str:
    """Not-Sanitizer für den Betreff einer FailureNotice (docs/ARCHITECTURE.md §3).

    Läuft auch dann, wenn der reguläre Sanitizer versagt hat — daher bewusst maximal
    simpel und ohne Abhängigkeiten: nur druckbare ASCII-Zeichen, Whitespace normalisiert,
    hart gekürzt. Damit können weder Steuerzeichen noch Unicode-Tricks (T2/T12) noch
    überlange Betreffs in die Zustellung gelangen.
    """
    kept = [char if 0x20 <= ord(char) <= 0x7E else " " for char in subject_raw]
    collapsed = " ".join("".join(kept).split())
    if not collapsed:
        return "(no subject)"
    if len(collapsed) > _NOTICE_SUBJECT_MAX_CHARS:
        return collapsed[: _NOTICE_SUBJECT_MAX_CHARS - 1] + "…"
    return collapsed


def _mail_ref(raw: RawMail) -> MailRef:
    """Zieht den Metadaten-Abzug, der das RawMail-Objekt überlebt (I1)."""
    return MailRef(
        dedupe_key=raw.dedupe_key,
        from_domain=raw.from_domain,
        subject_sanitized=_notice_subject(raw.subject_raw),
    )


def _fail_closed(
    ref: MailRef, stage: Stage, exc: BaseException, deps: PipelineDeps
) -> FailedNotice:
    """Baut die Metadaten-Notiz und versucht **einmal**, sie zuzustellen (I6).

    Scheitert auch das (kaputter Composer/Messenger), bleibt es beim Ergebnis
    `notice_delivered=False`; es wird nicht erneut versucht — Retries sind Sache von WP8.
    """
    notice = FailureNotice(
        dedupe_key=ref.dedupe_key,
        from_domain=ref.from_domain,
        subject_sanitized=ref.subject_sanitized,
        stage=stage,
        reason_class=classify_failure(stage, exc),
    )
    try:
        deps.messenger.send(deps.composer.compose_failure(notice))
    except Exception:  # Fail-closed: Der Versand der Notiz darf nie eskalieren.
        return FailedNotice(notice=notice, notice_delivered=False)
    return FailedNotice(notice=notice, notice_delivered=True)


def _record(deps: PipelineDeps, ref: MailRef, state: ProgressState) -> None:
    """Meldet einen Zwischenstand an den Sink (falls einer injiziert wurde)."""
    if deps.progress is not None:
        deps.progress.record(ref.dedupe_key, state)


def _meets_threshold(importance: Importance, threshold: Importance) -> bool:
    """True, wenn `importance` mindestens die konfigurierte Zustellschwelle erreicht."""
    return _IMPORTANCE_RANK[importance] >= _IMPORTANCE_RANK[threshold]


# --- Pipeline ------------------------------------------------------------------------------


def process_mail(raw: RawMail, deps: PipelineDeps) -> PipelineResult:
    """Führt eine Mail durch die Stufen 2-6 und liefert das Ergebnis für `state/db.py`.

    Args:
        raw: Rohe Mail aus dem Ingest. Verlässt diese Funktion nicht und wird nach der
            Sanitize-Stufe aktiv freigegeben (I1).
        deps: Injizierte Stufen und Policy-Parameter.

    Returns:
        :class:`Delivered`, :class:`QueuedLow` oder :class:`FailedNotice`. Es wird nie eine
        Exception aus einer Stufe nach außen gereicht — jeder Fehler endet fail-closed (I6).
    """
    ref = _mail_ref(raw)
    try:
        mail = deps.sanitizer.sanitize(raw)
        _record(deps, ref, "sanitized")
    except Exception as exc:  # I6: jede Stufen-Exception ist fail-closed.
        return _fail_closed(ref, "sanitize", exc, deps)
    finally:
        # Ab hier existiert keine Referenz auf mime_bytes mehr (I1, strukturell).
        del raw

    return _process_sanitized(mail, ref, deps)


def _process_sanitized(mail: SanitizedMail, ref: MailRef, deps: PipelineDeps) -> PipelineResult:
    """Stufen 3-6 auf der bereits sanitisierten Mail (kein Zugriff mehr auf `RawMail`)."""
    try:
        summary = deps.summarizer.summarize(mail)
        _record(deps, ref, "summarized")
    except Exception as exc:  # I6: fail-closed
        return _fail_closed(ref, "summarize", exc, deps)

    try:
        verdict = deps.critic.review(mail, summary)
    except Exception as exc:  # I6: fail-closed
        return _fail_closed(ref, "critic", exc, deps)

    if not verdict.summary_accurate:
        # T8: Der Kritiker hält die Zusammenfassung für falsch ⇒ kein Inhalt, nur Notiz.
        return _fail_closed(ref, "critic", _InaccurateSummaryError(), deps)

    try:
        # ADR-008: Der Stand `checked` ist committet, bevor irgendetwas versendet wird.
        _record(deps, ref, "checked")
    except Exception as exc:  # I6: fail-closed (State-DB nicht schreibbar)
        return _fail_closed(ref, "critic", exc, deps)

    high_risk = verdict.phishing_risk == "high"
    if high_risk and not _meets_threshold(summary.importance, "normal"):
        # F-CRIT-2: Warnungen dürfen nicht im Low-Digest untergehen.
        summary = summary.model_copy(update={"importance": "normal"})

    if not high_risk and not _meets_threshold(summary.importance, deps.deliver_min_importance):
        return QueuedLow(
            dedupe_key=ref.dedupe_key, from_domain=ref.from_domain, summary=summary
        )

    try:
        message = deps.composer.compose(mail, summary, verdict)
    except Exception as exc:  # I6: fail-closed
        return _fail_closed(ref, "compose", exc, deps)

    try:
        deps.messenger.send(message)
    except Exception as exc:  # I6: fail-closed
        return _fail_closed(ref, "deliver", exc, deps)

    return Delivered(dedupe_key=ref.dedupe_key, message=message)
