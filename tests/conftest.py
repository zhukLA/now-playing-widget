"""Общие фикстуры pytest.

Qt-тесты работают без дисплея (offscreen) — в том числе в CI.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """Единственный QApplication на всю сессию тестов."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
