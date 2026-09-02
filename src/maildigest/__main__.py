"""Modul-Einstiegspunkt: `python -m maildigest …`.

Gleichwertig zum Konsolen-Kommando `maildigest` (`[project.scripts]`, ADR-052) und
gedacht für Umgebungen, in denen das Skript-Verzeichnis des venv nicht im `PATH` liegt.
"""

from __future__ import annotations

from maildigest.cli import run_cli

if __name__ == "__main__":
    run_cli()
