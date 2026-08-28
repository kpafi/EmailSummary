"""LLMProvider-Protokoll und Fehlerklassen der LLM-Schicht.

Vertrag: docs/ARCHITECTURE.md §2 (LLM-Schicht), umgesetzt in WP4.

Das Interface ist bewusst **reiner Text-in/Text-out** (Invariante I2): Es gibt keinen
Parameter für Tools, Function-Calls, MCP-Server, Datei- oder Netzzugriff im Namen des
Modells. Der einzige Effekt, den ein Modell erzielen kann, ist ein String — und der wird
stromabwärts schema-validiert (`llm/schema.py`) und deterministisch sanitisiert (WP7).
Diese Beschränkung ist strukturell: Ein späteres WP kann Tool-Use nicht "durchreichen",
ohne dieses Protokoll zu ändern.

Fehler-Taxonomie (die Namen sind Vertrag — `pipeline._ERROR_CLASSES` bildet sie auf
`reason_class`-Werte ab, siehe ADR-012):

* :class:`LLMTimeout` — Zeitlimit überschritten.
* :class:`LLMRateLimited` — HTTP 429, auch nach allen Retries.
* :class:`LLMTransportError` — sonstige HTTP-/Transportfehler (5xx nach Retries, 4xx wie
  401/403, Verbindungsabbruch).
* :class:`LLMInvalidResponse` — Antwort ist unbrauchbar (leer, kein Text, kein
  schema-valides JSON nach dem Reparaturversuch) ⇒ fail-closed upstream (I6).

Fehlermeldungen enthalten nie Secrets (I5) und nie Modell-/Mail-Inhalte: Ein Provider
darf Statuscode und Provider-Fehlertyp nennen, aber niemals den Antworttext.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "LLMError",
    "LLMInvalidResponse",
    "LLMProvider",
    "LLMRateLimited",
    "LLMTimeout",
    "LLMTransportError",
]

#: Zeitlimit eines einzelnen LLM-Aufrufs in Sekunden (PLAN.md WP4).
DEFAULT_TIMEOUT_SECONDS: float = 60.0

#: Maximale Anzahl Versuche je Aufruf (1 regulärer + 2 Retries) bei 429/5xx.
MAX_ATTEMPTS: int = 3


class LLMError(Exception):
    """Basisklasse aller Fehler der LLM-Schicht.

    Aufrufer, die nicht zwischen den Ursachen unterscheiden müssen, fangen diese Klasse;
    die Pipeline unterscheidet über die Unterklassen (ADR-012).
    """


class LLMTimeout(LLMError):  # noqa: N818 - Name ist Vertrag (PLAN WP4, pipeline._ERROR_CLASSES)
    """Der Provider hat nicht innerhalb des Zeitlimits geantwortet."""


class LLMRateLimited(LLMError):  # noqa: N818 - Name ist Vertrag (PLAN WP4, pipeline._ERROR_CLASSES)
    """Der Provider hat mit HTTP 429 geantwortet — auch nach allen Retries."""


class LLMTransportError(LLMError):
    """HTTP- oder Transportfehler, der kein Timeout und kein Rate-Limit ist.

    Umfasst 5xx nach erschöpften Retries, nicht wiederholbare 4xx (z. B. 401 bei
    falschem API-Key) und Verbindungsfehler. Die Meldung nennt den Statuscode, nie den
    Antwortkörper.
    """


class LLMInvalidResponse(LLMError):  # noqa: N818 - Name ist Vertrag (PLAN WP4, pipeline._ERROR_CLASSES)
    """Die Antwort ist unbrauchbar: leer, ohne Textblock oder nicht schema-valide.

    Führt upstream zur Metadaten-Notiz statt zur Zustellung ungeprüften Inhalts (I6).
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Schmales Provider-Interface: ein System-Prompt, ein User-Prompt, ein Text zurück.

    Implementierungen: :class:`maildigest.llm.anthropic.AnthropicProvider`,
    :class:`maildigest.llm.openai.OpenAICompatibleProvider`.
    """

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        """Erzeugt eine Textantwort.

        Args:
            system: System-/Developer-Anteil des Prompts (Code + gelabelte
                Custom-Instructions, I8). Nie Mail-Inhalt.
            user: Nutzer-Anteil, enthält den delimitierten, als untrusted markierten
                Datenblock (I8).
            max_tokens: Obergrenze der Antwortlänge (aus der Config, `[llm] max_tokens`).
            temperature: Sampling-Temperatur. `None` (Default) bedeutet: Feld wird nicht
                gesendet, der Provider-Default gilt. Das ist bewusst so, weil aktuelle
                Modelle das Feld ablehnen können (siehe ADR zu WP4).

        Returns:
            Den zusammengesetzten Text der Antwort (nie `None`, ggf. leerer String nur,
            wenn der Provider einen leeren Textblock liefert).

        Raises:
            LLMTimeout: Zeitlimit überschritten.
            LLMRateLimited: HTTP 429 auch nach allen Retries.
            LLMTransportError: sonstiger HTTP-/Transportfehler.
            LLMInvalidResponse: Antwort ohne verwertbaren Textblock.
        """
        ...
