"""Gemeinsame HTTP-Mechanik der Messenger-Adapter: Retry mit Backoff, Fehler-Mapping.

Interne Hilfsschicht (Telegram + Discord). Bewusst eine **eigene** Implementierung neben
`llm/_http.py` (ADR zu WP7): Die Politik ist zwar gleich, die Fehlerdomäne aber nicht —
`llm/_http` wirft `LLMTransportError`/`LLMRateLimited`, was die Pipeline als LLM-Fehler
klassifizieren würde (`pipeline._ERROR_CLASSES`). Ein Zustellfehler muss `MessengerError`
sein, sonst zeigt die Metadaten-Notiz dem Nutzer die falsche Ursache.

Politik (identisch zu docs/ARCHITECTURE.md §6 für die LLM-Schicht):

* Wiederholt wird nur bei HTTP 429 und 5xx, insgesamt max. :data:`base.MAX_ATTEMPTS`.
* Wartezeit: exponentiell (1 s, 2 s, …, gedeckelt bei 30 s), `Retry-After` in Sekunden
  schlägt vor.
* Timeouts werden nicht wiederholt.
* Fehlermeldungen enthalten Adapternamen und Statuscode — **nie** die URL (sie trägt bei
  Telegram das Bot-Token und bei Discord das Webhook-Secret), nie Header, nie den
  Antwortkörper (I5).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from maildigest.messenger.base import MAX_ATTEMPTS, MessengerError

__all__ = ["request_json"]

#: Basiswert des exponentiellen Backoffs in Sekunden.
_BACKOFF_BASE_SECONDS = 1.0

#: Obergrenze einer einzelnen Wartezeit (auch für `Retry-After`).
_BACKOFF_MAX_SECONDS = 30.0


def _is_retryable(status: int) -> bool:
    """429 (Rate-Limit) und jeder 5xx sind wiederholbar — sonst nichts."""
    return status == 429 or 500 <= status < 600


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Liest `Retry-After` als Sekundenangabe; ignoriert HTTP-Datumsformate."""
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


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    adapter: str,
    timeout: float,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Führt einen Request mit Retry-Politik aus und liefert die geparste Antwort.

    Args:
        client: Bereits konfigurierter httpx-Client (Tests: `MockTransport`).
        method: HTTP-Methode, z. B. `"POST"`.
        url: Vollständige Endpunkt-URL. Enthält ggf. ein Secret und wird deshalb niemals
            in eine Exception übernommen.
        payload: JSON-Körper (bei GET weglassen).
        adapter: Anzeigename für Fehlermeldungen (`"telegram"`, `"discord"`).
        timeout: Zeitlimit dieses Requests in Sekunden.
        max_attempts: Gesamtzahl der Versuche (>= 1).
        sleep: Wartefunktion; in Tests injizierbar.

    Returns:
        Den Antwortkörper als Dict. Antworten ohne Körper (z. B. Discords `204`) ergeben
        ein leeres Dict — für die Adapter zählt nur der Status.

    Raises:
        MessengerError: Timeout, Verbindungsfehler oder endgültiger Fehlerstatus.
    """
    last_status: int | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.request(
                method, url, json=payload, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise MessengerError(
                f"{adapter}: exceeded the time limit of {timeout:.0f} s."
            ) from exc
        except httpx.RequestError as exc:
            raise MessengerError(
                f"{adapter}: connection failed ({type(exc).__name__})."
            ) from exc

        status = response.status_code
        if 200 <= status < 300:
            if not response.content:
                return {}
            try:
                data = response.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        last_status = status
        if not _is_retryable(status) or attempt == max_attempts:
            break

        wait = _retry_after_seconds(response)
        sleep(_backoff_seconds(attempt) if wait is None else wait)

    raise MessengerError(f"{adapter}: delivery failed (HTTP {last_status}).")
