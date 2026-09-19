"""Gemeinsame HTTP-Mechanik beider Provider: Retry mit Backoff, Fehler-Mapping.

Interne Hilfsschicht (nicht Teil der öffentlichen API des Pakets). Existiert, damit
`anthropic.py` und `openai.py` exakt dieselbe Retry- und Fehlersemantik haben — ohne
sie zweimal (und irgendwann unterschiedlich) zu implementieren.

Politik (docs/PLAN.md WP4, docs/ARCHITECTURE.md §6):
* Wiederholt wird nur bei HTTP 429 und 5xx, insgesamt max. :data:`base.MAX_ATTEMPTS`
  Versuche.
* Wartezeit: exponentielles Backoff (1 s, 2 s, 4 s …, gedeckelt), außer der Server nennt
  ein `Retry-After` in Sekunden — dann gilt dieser Wert (ebenfalls gedeckelt).
* Timeouts werden **nicht** wiederholt: Ein 60-s-Timeout dreimal zu wiederholen würde die
  Pipeline drei Minuten blockieren. Die Wiederholung ganzer Stufen ist Sache von WP8.
* Fehlermeldungen enthalten Statuscode und Provider-Fehlertyp, nie den Antwortkörper und
  nie Header (I5).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from maildigest.foreign_text import sanitize_foreign_text
from maildigest.llm.base import (
    MAX_ATTEMPTS,
    LLMRateLimited,
    LLMTimeout,
    LLMTransportError,
)

__all__ = ["post_json"]

#: Basiswert des exponentiellen Backoffs in Sekunden.
_BACKOFF_BASE_SECONDS = 1.0

#: Obergrenze einer einzelnen Wartezeit (auch für `Retry-After`).
_BACKOFF_MAX_SECONDS = 30.0


def _is_retryable(status: int) -> bool:
    """429 (Rate-Limit) und jeder 5xx sind wiederholbar — sonst nichts (docs/PLAN.md WP4).

    Bewusst eng: Ein 400/401/404 wird durch Wiederholung nicht besser, sondern verzögert
    nur die Fehlermeldung an den Nutzer.
    """
    return status == 429 or 500 <= status < 600


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Liest `Retry-After` als Sekundenangabe; ignoriert HTTP-Datumsformate.

    Ein HTTP-Datum wäre parsebar, brächte hier aber nichts: Der Wert würde ohnehin auf
    :data:`_BACKOFF_MAX_SECONDS` gedeckelt, und ein fehlerhaft geparstes Datum könnte zu
    einer Wartezeit von 0 führen. Ohne verwertbaren Wert gilt das normale Backoff.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    # `float()` akzeptiert `nan`, `inf` und `1e400`. NaN überlebt jeden Vergleich
    # (`nan < 0` ist falsch, `min(nan, 30.0)` ist NaN) und träte erst in `time.sleep`
    # als `ValueError` hervor — außerhalb der Fehler-Taxonomie (HC-30, ADR-012).
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, _BACKOFF_MAX_SECONDS)


def _backoff_seconds(attempt: int) -> float:
    """Exponentielles Backoff für den `attempt`-ten Versuch (1-basiert)."""
    return min(_BACKOFF_BASE_SECONDS * 2.0 ** (attempt - 1), _BACKOFF_MAX_SECONDS)


def _provider_error_message(response: httpx.Response) -> str:
    """Der Klartext aus `error.message`, gekürzt und einzeilig.

    Wird **nur** beim Verbindungstest von `connect-llm` ausgegeben (`reveal_message`).
    Dort besteht die Anfrage aus einem festen, inhaltsfreien Satz, der Antworttext kann
    also nichts aus einer Mail zitieren. Im Normalbetrieb bleibt er außen vor (I5).

    Der Nutzen ist erheblich: Anbieter erklären hier, *warum* abgelehnt wurde
    („No endpoints found matching your data policy" bei OpenRouter, „User not found."
    bei falschem Schlüssel). Ohne diesen Satz bleibt nur ein nackter Statuscode.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    return _describe_error(error)


def _foreign(value: str) -> str:
    """Ein Textstück aus der Antwort der Gegenstelle, terminalsicher gemacht (HC-4).

    Der Anbietertext ist Fremddaten (ADR-055): Er trägt im Zweifel ESC-Sequenzen, die den
    Bildschirm leeren, den Fenstertitel setzen oder eine eingefärbte Falschmeldung
    platzieren. Die Whitespace-Normalisierung allein reichte dafür nicht — sie kennt nur
    `str.split`, und ESC/BEL sind für Python kein Whitespace.
    """
    return sanitize_foreign_text(" ".join(value.split()))


def _describe_error(error: dict[str, Any]) -> str:
    """Setzt `message` und — falls vorhanden — die Angaben aus `metadata` zusammen.

    OpenRouter reicht Fehler des tatsächlich bedienenden Anbieters als generisches
    „Provider returned error" weiter und legt den eigentlichen Text nach
    `error.metadata.raw`, den Namen nach `error.metadata.provider_name`. Ohne diese
    Auswertung bleibt eine Hülle ohne Inhalt stehen — genau die Lage, in der ein Nutzer
    nicht weiterkommt.
    """
    parts: list[str] = []
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(_foreign(message))

    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        name = metadata.get("provider_name")
        if isinstance(name, str) and name.strip():
            parts.append(f"upstream provider: {_foreign(name)}")
        raw = metadata.get("raw")
        if isinstance(raw, str) and raw.strip():
            parts.append(f"upstream says: {_foreign(raw)}")
        elif raw is not None and not isinstance(raw, str):
            parts.append(f"upstream says: {_foreign(repr(raw))}")

    return " | ".join(parts)[:400]


def _provider_error_type(response: httpx.Response) -> str:
    """Extrahiert den Fehler-*Typ* (nicht den Text) aus einer Fehlerantwort.

    Anthropic und OpenAI liefern beide `{"error": {"type": ..., "message": ...}}`. Nur
    `type` wird übernommen: Es ist eine kurze, herstellerdefinierte Konstante wie
    `rate_limit_error`. Der `message`-Text bleibt außen vor, weil er im Zweifel Teile der
    Anfrage zitiert (I5) — Ausnahme: :func:`_provider_error_message` beim Verbindungstest.
    """
    try:
        payload = response.json()
    except ValueError:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    error = payload.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        if isinstance(error_type, str) and error_type:
            # Auch dieser kurze Wert ist Fremdtext und steht später in der Meldung (HC-4).
            return _foreign(error_type)
    return "unknown"


def post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
    timeout: float,
    max_attempts: int = MAX_ATTEMPTS,
    reveal_message: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Sendet einen JSON-POST mit Retry-Politik und liefert die geparste Antwort.

    Args:
        client: Bereits konfigurierter httpx-Client (in Tests mit `MockTransport`).
        url: Vollständige Endpunkt-URL.
        headers: Request-Header inkl. Authentifizierung (werden nie geloggt).
        payload: Request-Körper.
        provider: Anzeigename für Fehlermeldungen (z. B. `"anthropic"`).
        timeout: Zeitlimit dieses Aufrufs in Sekunden.
        max_attempts: Gesamtzahl der Versuche (>= 1).
        sleep: Wartefunktion; in Tests injizierbar, damit Backoff prüfbar ist, ohne
            tatsächlich zu warten.

    Returns:
        Den als Dict geparsten Antwortkörper.

    Raises:
        LLMTimeout: Zeitlimit überschritten.
        LLMRateLimited: HTTP 429 auch nach dem letzten Versuch.
        LLMTransportError: sonstiger HTTP-Status != 2xx, Verbindungsfehler oder
            nicht-JSON-Antwort bei Status 2xx.
    """
    last_status: int | None = None
    last_error_type = "unknown"
    last_message = ""

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(
                f"{provider}: exceeded the time limit of {timeout:.0f} s."
            ) from exc
        except httpx.RequestError as exc:
            raise LLMTransportError(
                f"{provider}: connection failed ({type(exc).__name__})."
            ) from exc

        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError as exc:
                raise LLMTransportError(
                    f"{provider}: response (HTTP {status}) is not valid JSON."
                ) from exc
            if not isinstance(data, dict):
                raise LLMTransportError(
                    f"{provider}: response (HTTP {status}) is not a JSON object."
                )
            return data

        last_status = status
        last_error_type = _provider_error_type(response)
        if reveal_message:
            last_message = _provider_error_message(response)

        if not _is_retryable(status) or attempt == max_attempts:
            break

        wait = _retry_after_seconds(response)
        sleep(_backoff_seconds(attempt) if wait is None else wait)

    detail = f' Provider says: "{last_message}"' if last_message else ""
    if last_status == 429:
        raise LLMRateLimited(
            f"{provider}: rate limit reached (HTTP 429, type {last_error_type}); "
            f"gave up after {max_attempts} attempts.{detail}"
        )
    raise LLMTransportError(
        f"{provider}: request failed (HTTP {last_status}, type {last_error_type}).{detail}"
    )


def body_error_suffix(data: dict[str, Any], *, reveal: bool) -> str:
    """Beschreibt ein Fehler-Objekt, das mit HTTP 200 im Antwortkörper steckt.

    Nicht jeder Anbieter meldet Fehler über den Statuscode: OpenRouter antwortet in
    manchen Lagen mit `200` und `{"error": {"message": ..., "code": ...}}` im Körper. Ohne
    diese Auswertung bliebe davon nur „die Antwort hat kein `choices`" übrig — formal
    richtig und praktisch nutzlos.

    Wie bei :func:`_provider_error_message` wandert der Klartext nur in die Meldung, wenn
    `reveal` gesetzt ist (Verbindungstest, inhaltsfreie Anfrage — I5).

    Returns:
        Einen anhängbaren Satz, oder `""`, wenn kein Fehler-Objekt vorliegt.
    """
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    suffix = (
        " The provider reported an error in the response body "
        f"(code {_foreign(repr(code))})."
    )
    if not reveal:
        return suffix
    described = _describe_error(error)
    return f'{suffix} It says: "{described}"' if described else suffix
