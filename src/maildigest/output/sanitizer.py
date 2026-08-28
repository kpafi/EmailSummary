"""Output-Sanitizer: baut aus Summary + CriticVerdict die DigestMessage und säubert jedes Feld
(URL-/Markdown-/HTML-Strip, Escaping, Längen-Split je Messenger) — letzte Code-Schicht vor dem
Nutzer (I3/I4).

Format: docs/ARCHITECTURE.md §7. Umsetzung in WP7.
Platzhalter (WP0): noch keine Logik.
"""
