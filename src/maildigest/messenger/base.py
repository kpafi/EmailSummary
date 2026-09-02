"""Adapter-Interface der Zustell-Stufe (Stufe 6): `send()` + `healthcheck()`.

Dieses Protokoll ist die Adapter-Sicht (F-MSG-1) und bewusst **breiter** als das
gleichnamige Protokoll in :mod:`maildigest.pipeline`: Die Pipeline verlangt nur `send()`,
die CLI (WP9, `connect-messenger`) braucht zusätzlich einen Erreichbarkeitstest. Jeder
Adapter hier erfüllt damit automatisch auch den schmalen Pipeline-Vertrag.

Sicherheitsregeln für **jeden** Adapter:

* Der Adapter verändert `message.parts` nicht mehr — der Text ist fertig sanitisiert
  (I3/I4). Er wählt keine Formatierungs-Modi (`parse_mode`, Embeds), die aus Text wieder
  Markup machen könnten (T7, ADR-006).
* Secrets (Bot-Token, Webhook-URL) werden als :class:`pydantic.SecretStr` gehalten und
  erscheinen weder in `repr()` noch in Exception-Texten (I5) — deshalb nennen die
  Fehlermeldungen nur den Adapternamen und den Statuscode, nie die URL.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from maildigest.models import DigestMessage

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "MAX_ATTEMPTS", "Messenger", "MessengerError"]

#: Zeitlimit eines einzelnen Zustell-Requests in Sekunden.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Versuche je Request bei wiederholbaren Fehlern (429/5xx).
MAX_ATTEMPTS = 3


class MessengerError(Exception):
    """Zustellung fehlgeschlagen.

    Der Klassenname ist in `pipeline._ERROR_CLASSES` auf `delivery_error` abgebildet —
    die Meldung selbst erreicht den Nutzer nie und enthält nie Secrets (I5).
    """


@runtime_checkable
class Messenger(Protocol):
    """Zustell-Adapter für eine fertige :class:`~maildigest.models.DigestMessage`."""

    def send(self, message: DigestMessage) -> None:
        """Stellt alle `parts` in Reihenfolge zu.

        Raises:
            MessengerError: Zustellung endgültig fehlgeschlagen.
        """
        ...

    def healthcheck(self) -> bool:
        """True, wenn der Dienst erreichbar und die Konfiguration plausibel ist.

        Wirft nicht: Ein Healthcheck ist eine Frage, keine Aktion.
        """
        ...
