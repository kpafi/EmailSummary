"""Sanitizer-Subpaket: deterministische MIME→Klartext-Aufbereitung, kein LLM.

Politik: docs/SECURITY.md §4 (verbindlich). Umgesetzt in WP3 (ADR-026 bis ADR-030).

Öffentliche API: :class:`MailSanitizer` (implementiert das `Sanitizer`-Protokoll aus
`maildigest.pipeline`) und :class:`SanitizeError` (fail-closed-Signal, I6).
"""

from maildigest.sanitize.sanitizer import MailSanitizer, SanitizeError

__all__ = ["MailSanitizer", "SanitizeError"]
