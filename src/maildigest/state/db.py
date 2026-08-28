"""SQLite-State: Tabellen seen_mails, low_digest_queue, meta; Statusübergänge
pending->sanitized->summarized->checked->delivered|failed|skipped_low. Kein Mail-Volltext in der
DB (docs/SECURITY.md §6).

Datenmodell: docs/ARCHITECTURE.md §2 (State). Umsetzung in WP2 (Nötigstes), Finalisierung in
WP8.
Platzhalter (WP0): noch keine Logik.
"""
