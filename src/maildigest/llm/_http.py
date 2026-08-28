"""Gemeinsame HTTP-Mechanik beider Provider: Retry mit Backoff, Fehler-Mapping.

Interne Hilfsschicht (nicht Teil der öffentlichen API des Pakets). Existiert, damit
`anthropic.py` und `openai.py` exakt dieselbe Retry- und Fehlersemantik haben — ohne
sie zweimal (und irgendwann unterschiedlich) zu implementieren.

Politik (PLAN.md WP4, docs/ARCHITECTURE.md §6):
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

import time
from collections.abc import Callable
from typing import Any

import httpx

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
    """429 (Rate-Limit) und jeder 5xx sind wiederholbar — sonst nichts (PLAN.md WP4).

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
    if seconds < 0:
        return None
    return min(seconds, _BACKOFF_MAX_SECONDS)


def _backoff_seconds(attempt: int) -> float:
    """Exponentielles Backoff für den `attempt`-ten Versuch (1-basiert)."""
    return min(_BACKOFF_BASE_SECONDS * 2.0 ** (attempt - 1), _BACKOFF_MAX_SECONDS)


def _provider_error_type(response: httpx.Response) -> str:
    """Extrahiert den Fehler-*Typ* (nicht den Text) aus einer Fehlerantwort.

    Anthropic und OpenAI liefern beide `{"error": {"type": ..., "message": ...}}`. Nur
    `type` wird übernommen: Es ist eine kurze, herstellerdefinierte Konstante wie
    `rate_limit_error`. Der `message`-Text bleibt außen vor, weil er im Zweifel Teile der
    Anfrage zitiert (I5).
    """
    try:
        payload = response.json()
    except ValueError:
        return "unbekannt"
    if not isinstance(payload, dict):
        return "unbekannt"
    error = payload.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        if isinstance(error_type, str) and error_type:
            return error_type
    return "unbekannt"


def post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
    timeout: float,
    max_attempts: int = MAX_ATTEMPTS,
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
    last_error_type = "unbekannt"

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(
                f"{provider}: Zeitlimit von {timeout:.0f} s überschritten."
            ) from exc
        except httpx.RequestError as exc:
            raise LLMTransportError(
                f"{provider}: Verbindung fehlgeschlagen ({type(exc).__name__})."
            ) from exc

        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError as exc:
                raise LLMTransportError(
                    f"{provider}: Antwort (HTTP {status}) ist kein gültiges JSON."
                ) from exc
            if not isinstance(data, dict):
                raise LLMTransportError(
                    f"{provider}: Antwort (HTTP {status}) ist kein JSON-Objekt."
                )
            return data

        last_status = status
        last_error_type = _provider_error_type(response)

        if not _is_retryable(status) or attempt == max_attempts:
            break

        wait = _retry_after_seconds(response)
        sleep(_backoff_seconds(attempt) if wait is None else wait)

    if last_status == 429:
        raise LLMRateLimited(
            f"{provider}: Rate-Limit erreicht (HTTP 429, Typ {last_error_type}); "
            f"nach {max_attempts} Versuchen aufgegeben."
        )
    raise LLMTransportError(
        f"{provider}: Anfrage fehlgeschlagen (HTTP {last_status}, Typ {last_error_type})."
    )
