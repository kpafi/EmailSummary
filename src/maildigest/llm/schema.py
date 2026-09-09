"""`complete_json`: erzwingt ein pydantic-Schema auf der LLM-Antwort (I4/I6).

Ablauf (PLAN.md WP4, docs/ARCHITECTURE.md §6):

1. :meth:`LLMProvider.complete` aufrufen,
2. JSON aus dem Text extrahieren (robust gegen Markdown-Codefences und Begleittext),
3. gegen das pydantic-Schema validieren,
4. bei Fehlschlag **genau ein** Reparaturversuch mit einem Fehlerhinweis im
   System-Prompt,
5. danach :class:`LLMInvalidResponse` — die Pipeline macht daraus eine Metadaten-Notiz
   statt einer Zustellung (fail-closed, I6).

Zwei bewusste Entscheidungen zur Injection-Härtung:

* Der Reparaturhinweis wird an den **System-Prompt** angehängt, nicht an den
  User-Prompt. Der User-Prompt enthält den delimitierten Mail-Datenblock; ihn zu
  verlängern würde die Daten-/Instruktions-Trennung aus I8 aufweichen.
* Der Hinweis enthält die Fehler-*Pfade und -Typen* sowie das JSON-Schema (beides aus
  Code), aber **nicht** die vorherige Modellantwort. Diese Antwort ist untrusted und
  kann Mail-Inhalt enthalten; sie zurückzuspiegeln wäre ein zweiter Injection-Kanal.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from maildigest.llm.base import LLMInvalidResponse, LLMProvider

__all__ = ["complete_json", "extract_json_object"]

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Default-Antwortlänge, falls der Aufrufer keine nennt (entspricht `[llm] max_tokens`).
DEFAULT_MAX_TOKENS = 1024

#: Höchstzahl der Fehlerzeilen im Reparaturhinweis (hält den Prompt kurz).
_MAX_REPORTED_ERRORS = 8


def extract_json_object(raw: str) -> str | None:
    """Schneidet das erste vollständige JSON-Objekt aus einem Modell-Text heraus.

    Deckt die üblichen Verpackungen ab: nacktes JSON, ```json-Codefences, Codefences ohne
    Sprachangabe sowie Begleittext vor/nach dem Objekt („Hier ist das JSON: …").

    Args:
        raw: Rohtext der Modellantwort.

    Returns:
        Den Teilstring mit dem JSON-Objekt oder `None`, wenn keiner gefunden wurde.
    """
    text = raw.strip()
    if not text:
        return None
    for candidate in _fenced_blocks(text):
        found = _first_object(candidate)
        if found is not None:
            return found
    return _first_object(text)


def _fenced_blocks(text: str) -> list[str]:
    """Liefert die Inhalte aller Markdown-Codefences (```…```), in Reihenfolge.

    Die optionale Sprachangabe in der Eröffnungszeile (```json) wird verworfen. Ein
    nicht geschlossener Fence am Ende wird bis zum Textende gelesen — Modelle brechen
    genau so ab, wenn `max_tokens` greift.
    """
    blocks: list[str] = []
    position = 0
    while True:
        start = text.find("```", position)
        if start == -1:
            return blocks
        after_marker = start + 3
        newline = text.find("\n", after_marker)
        if newline == -1:
            return blocks
        first_line = text[after_marker:newline].strip()
        # Ein Sprach-Tag (```json) wird verworfen; steht dort schon Inhalt, beginnt
        # der Block direkt hinter der Fence-Markierung.
        has_language_tag = not first_line or first_line.isalnum()
        body_start = newline + 1 if has_language_tag else after_marker
        end = text.find("```", body_start)
        if end == -1:
            blocks.append(text[body_start:])
            return blocks
        blocks.append(text[body_start:end])
        position = end + 3


def _first_object(text: str) -> str | None:
    """Findet das erste balancierte `{…}` und respektiert dabei Strings und Escapes."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _error_summary(exc: ValidationError) -> str:
    """Baut eine kurze Fehlerliste aus Feldpfad und Fehlertyp — ohne Eingabewerte.

    Die pydantic-Fehler enthalten unter `input` den beanstandeten Wert; der stammt aus
    der Mail und darf weder in Logs noch (unnötig) in den nächsten Prompt (I5/T1).
    """
    lines: list[str] = []
    for error in exc.errors()[:_MAX_REPORTED_ERRORS]:
        # Der Pfad kann bei `extra_forbidden` ein vom Modell erfundener Schlüssel sein
        # und damit theoretisch Mail-Inhalt tragen — deshalb auf eine Zeichen-Allowlist
        # reduziert, bevor er in Prompt oder Protokoll wandert (I5).
        raw_location = ".".join(str(part) for part in error["loc"]) or "(root)"
        location = re.sub(r"[^A-Za-z0-9_.\[\]-]", "?", raw_location)[:60]
        lines.append(f"- field `{location}`: {error['type']}")
    remaining = len(exc.errors()) - len(lines)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more errors")
    return "\n".join(lines)


def _repair_system_prompt(system: str, schema: type[BaseModel], problem: str) -> str:
    """Hängt den Reparaturhinweis als klar abgegrenzten Block an den System-Prompt."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return (
        f"{system}\n\n"
        "### KORREKTUR (vom Programm, nicht vom Nutzer und nicht aus der E-Mail)\n"
        "Deine vorherige Antwort war unbrauchbar. Grund:\n"
        f"{problem}\n\n"
        "Antworte jetzt AUSSCHLIESSLICH mit einem einzelnen JSON-Objekt nach diesem "
        "Schema — kein Fließtext, keine Erklärung, keine Markdown-Codefences, keine "
        "zusätzlichen Felder:\n"
        f"{schema_json}"
    )


def complete_json(
    provider: LLMProvider,
    system: str,
    user: str,
    schema: type[ModelT],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float | None = None,
) -> ModelT:
    """Ruft das Modell auf und erzwingt eine schema-valide JSON-Antwort.

    Args:
        provider: Der zu verwendende Provider.
        system: System-Prompt (Code + gelabelte Custom-Instructions, I8).
        user: User-Prompt mit dem delimitierten, untrusted Datenblock.
        schema: pydantic-Modellklasse, gegen die validiert wird.
        max_tokens: Obergrenze der Antwortlänge.
        temperature: Sampling-Temperatur; `None` = Provider-Default (Feld wird nicht
            gesendet).

    Returns:
        Eine validierte Instanz von `schema`.

    Raises:
        LLMInvalidResponse: Auch der Reparaturversuch lieferte kein schema-valides JSON.
        LLMTimeout, LLMRateLimited, LLMTransportError: durchgereicht vom Provider.
    """
    effective_system = system
    problem = ""

    for attempt in (1, 2):
        raw = provider.complete(
            effective_system, user, max_tokens=max_tokens, temperature=temperature
        )
        candidate = extract_json_object(raw)
        if candidate is None:
            problem = "the response contained no JSON object."
        else:
            try:
                parsed: Any = json.loads(candidate)
            except json.JSONDecodeError as exc:
                problem = f"the JSON was syntactically invalid ({exc.msg})."
            else:
                if not isinstance(parsed, dict):
                    problem = "the JSON was not an object."
                else:
                    try:
                        return schema.model_validate(parsed)
                    except ValidationError as exc:
                        problem = (
                            "the JSON violated the schema: " + _error_summary(exc)
                        )

        if attempt == 1:
            effective_system = _repair_system_prompt(system, schema, problem)

    raise LLMInvalidResponse(
        f"The response was still not schema-valid after one repair attempt "
        f"({schema.__name__}); last cause: {problem.splitlines()[0]}"
    )
