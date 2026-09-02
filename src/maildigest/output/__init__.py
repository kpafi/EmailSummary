"""Output-Subpaket: letzte deterministische Kontrolle vor dem Versand (I3/I4).

* :mod:`maildigest.output.sanitizer` — Feld-Scrubbing, Nachbrenner, Längen-Split.
* :mod:`maildigest.output.composer` — Zusammenbau der `DigestMessage` (Format §7).
"""

from maildigest.output.composer import DigestComposer, part_limit_for
from maildigest.output.sanitizer import (
    DISCORD_MAX_PART_CHARS,
    SIGNAL_MAX_PART_CHARS,
    TELEGRAM_MAX_PART_CHARS,
    final_guard,
    scrub_field,
    split_parts,
)

__all__ = [
    "DISCORD_MAX_PART_CHARS",
    "SIGNAL_MAX_PART_CHARS",
    "TELEGRAM_MAX_PART_CHARS",
    "DigestComposer",
    "final_guard",
    "part_limit_for",
    "scrub_field",
    "split_parts",
]
