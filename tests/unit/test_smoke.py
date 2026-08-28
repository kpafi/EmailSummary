"""Smoke-Test (WP0): das Paket maildigest ist importierbar und exponiert eine Version."""

from __future__ import annotations

import importlib


def test_package_importable() -> None:
    """maildigest lässt sich importieren (Basis für alle weiteren WPs)."""
    module = importlib.import_module("maildigest")
    assert module is not None


def test_package_has_version() -> None:
    """Das Paket stellt eine __version__-Kennung bereit."""
    module = importlib.import_module("maildigest")
    assert isinstance(module.__version__, str)
    assert module.__version__
