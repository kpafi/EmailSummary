"""Signal-Adapter über ein lokal laufendes `signal-cli` im JSON-RPC-Modus (Feature-Flag).

Bewusst klein gehalten (PLAN.md WP7: „Aufwand begrenzen"): Der Adapter spricht über einen
Unix-Domain-Socket mit `signal-cli --daemon --socket <pfad>` und schickt pro
Nachrichtenteil ein `send`-Kommando. Es gibt keine Registrierungs-, Gruppen- oder
Anhang-Funktionen — MailDigest stellt nur Text zu.

Zwei bewusste Einschränkungen (ADR zu WP7):

* **Zustellung an sich selbst** („Note to Self"): Die Config kennt in v0.1 nur
  `[messenger.signal] enabled` und `signal_cli_socket`, keine Empfängernummer. Statt das
  Config-Schema aus diesem Arbeitspaket heraus zu erweitern, nutzt der Adapter
  `note-to-self` — für ein persönliches Mail-Digest der Normalfall.
* **Kein Prozess-Management:** Läuft kein signal-cli, gibt es eine klare, englische
  Fehlermeldung statt eines Startversuchs. MailDigest startet keine fremden Prozesse.
"""

from __future__ import annotations

import contextlib
import json
import socket
from collections.abc import Callable
from typing import Any, Protocol

from maildigest.messenger.base import DEFAULT_TIMEOUT_SECONDS, MessengerError
from maildigest.models import DigestMessage

__all__ = ["SignalMessenger"]

#: Obergrenze der gelesenen Antwortmenge je Aufruf (Schutz vor endlosem Datenstrom).
_MAX_RESPONSE_BYTES = 1_000_000


class _Connection(Protocol):
    """Minimale Sicht auf einen verbundenen Socket (in Tests durch eine Attrappe ersetzt)."""

    def sendall(self, data: bytes) -> None: ...

    def recv(self, bufsize: int) -> bytes: ...

    def close(self) -> None: ...


def _connect_unix(path: str, timeout: float) -> _Connection:
    """Öffnet den Unix-Domain-Socket von signal-cli."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(path)
    return connection


class SignalMessenger:
    """Zustellung über signal-cli (JSON-RPC über Unix-Socket).

    Steht hinter dem Feature-Flag `[messenger.signal] enabled`.
    """

    def __init__(
        self,
        *,
        socket_path: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        connect: Callable[[str, float], _Connection] | None = None,
    ) -> None:
        """Baut den Adapter.

        Args:
            socket_path: Pfad des signal-cli-Sockets (`[messenger.signal] signal_cli_socket`).
            timeout: Zeitlimit je Aufruf in Sekunden.
            connect: Verbindungsfabrik; in Tests injizierbar.

        Raises:
            ValueError: Socket-Pfad fehlt.
        """
        if not socket_path:
            raise ValueError("Pfad zum signal-cli-Socket fehlt.")
        self._socket_path = socket_path
        self._timeout = timeout
        self._connect = connect if connect is not None else _connect_unix
        self._next_id = 0

    def __repr__(self) -> str:
        """Repräsentation ohne Inhalte (der Socket-Pfad ist kein Secret)."""
        return f"SignalMessenger(socket_path={self._socket_path!r})"

    __str__ = __repr__

    # --- Messenger ----------------------------------------------------------------

    def send(self, message: DigestMessage) -> None:
        """Stellt jeden Teil als eigene Signal-Nachricht an „Note to Self" zu."""
        for part in message.parts:
            if not part:
                continue
            self._rpc("send", {"noteToSelf": True, "message": part})

    def healthcheck(self) -> bool:
        """Fragt die signal-cli-Version ab; False, wenn der Daemon nicht erreichbar ist."""
        try:
            self._rpc("version", {})
        except MessengerError:
            return False
        return True

    # --- JSON-RPC -----------------------------------------------------------------

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Sendet ein JSON-RPC-Kommando und liefert das `result`-Objekt.

        Raises:
            MessengerError: Socket nicht erreichbar, Antwort unlesbar oder Fehlerobjekt in
                der Antwort. Die Meldung nennt nie den Nachrichtentext (I5/NF-5).
        """
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": str(self._next_id),
            "method": method,
            "params": params,
        }
        try:
            connection = self._connect(self._socket_path, self._timeout)
        except OSError as exc:
            raise MessengerError(
                "signal: signal-cli is unreachable "
                f"(socket {self._socket_path!r}, {type(exc).__name__}). "
                "Is `signal-cli --daemon --socket <path>` running?"
            ) from exc

        try:
            connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
            raw = self._read_line(connection)
        except OSError as exc:
            raise MessengerError(
                f"signal: communication with signal-cli failed ({type(exc).__name__})."
            ) from exc
        finally:
            # Aufräumen darf den Fehlerpfad nicht überschreiben.
            with contextlib.suppress(OSError):
                connection.close()

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise MessengerError("signal: the reply from signal-cli is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise MessengerError("signal: the reply from signal-cli is not a JSON object.")

        error = payload.get("error")
        if isinstance(error, dict):
            raise MessengerError(
                f"signal: signal-cli reports an error (code {error.get('code')!r})."
            )
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def _read_line(self, connection: _Connection) -> str:
        """Liest eine zeilengetrennte JSON-Antwort (gedeckelt, siehe `_MAX_RESPONSE_BYTES`)."""
        buffer = b""
        while b"\n" not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > _MAX_RESPONSE_BYTES:
                raise MessengerError("signal: the reply from signal-cli is unexpectedly large.")
        line = buffer.split(b"\n", 1)[0]
        if not line:
            raise MessengerError("signal: signal-cli returned no reply.")
        return line.decode("utf-8", errors="replace")
