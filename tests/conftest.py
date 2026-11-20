"""Общие фикстуры pytest.

Qt-тесты работают без дисплея (offscreen) — в том числе в CI.
Анимации по умолчанию выключены (логика видимости проверяется мгновенно);
тесты анимаций включают их явно: ``NowPlayingWidget(..., animations=True)``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NOW_PLAYING_REDUCE_MOTION", "1")


@pytest.fixture(scope="session")
def qapp():
    """Единственный QApplication на всю сессию тестов."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def pump(qapp):
    """pump(condition, timeout) — крутить цикл событий, пока условие не станет True."""
    import time

    def _pump(condition=lambda: False, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qapp.processEvents()
            if condition():
                return True
            time.sleep(0.005)
        qapp.processEvents()
        return condition()

    return _pump
