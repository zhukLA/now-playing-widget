"""Мини-движок анимаций для виджета: кривые в духе macOS и «твины» на float.

Почему не QPropertyAnimation: состояние виджета рисуется вручную в
``paintEvent``, анимируются не Qt-свойства, а числа (прозрачность слоя,
масштаб кнопки, прогресс перехода). ``Tween`` держит одно такое число и
плавно ведёт его к цели по заданной кривой, вызывая колбэк на каждом кадре
(Qt-анимации тикают синхронно с ~60 Гц таймером приложения).

Кривые написаны на Python (а не ``QEasingCurve``), чтобы была пружина с
лёгким перелётом — именно она даёт «живое» ощущение macOS.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Callable, Optional

from PySide6.QtCore import QEasingCurve, QObject, QVariantAnimation

Ease = Callable[[float], float]

#: Переменная окружения: 1 — отключить анимации («уменьшить движение»).
REDUCE_MOTION_ENV_VAR = "NOW_PLAYING_REDUCE_MOTION"


# ---------------------------------------------------------------------------
# Кривые (t ∈ [0, 1] → прогресс; f(0) = 0, f(1) ≈ 1)
# ---------------------------------------------------------------------------


def linear(t: float) -> float:
    return t


def ease_out_cubic(t: float) -> float:
    """Быстрый старт, мягкая остановка — появление элементов."""
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    """Аналог системного ease-in-ease-out macOS — переходы и исчезновения."""
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - (-2.0 * t + 2.0) ** 3 / 2.0


def spring(t: float) -> float:
    """Затухающая пружина: быстрый разгон, перелёт ~6 % и успокоение.

    1 − e^(−6.5·t)·cos(2π·1.15·t): к t = 1 остаток < 0.2 %, а финальное
    значение ``Tween`` всё равно выставляет точно в цель.
    """
    if t >= 1.0:
        return 1.0
    return 1.0 - math.exp(-6.5 * t) * math.cos(2.0 * math.pi * 1.15 * t)


# ---------------------------------------------------------------------------
# Системная настройка анимаций
# ---------------------------------------------------------------------------


def system_animations_enabled(env: Optional[dict] = None) -> bool:
    """Включены ли анимации: учитывает NOW_PLAYING_REDUCE_MOTION и Windows.

    На Windows читается «Параметры → Специальные возможности → Эффекты
    анимации» (SPI_GETCLIENTAREAANIMATION) — как macOS уважает
    «Уменьшить движение».
    """
    source = os.environ if env is None else env
    raw = str(source.get(REDUCE_MOTION_ENV_VAR, "")).strip().lower()
    if raw not in ("", "0", "false", "no", "off"):
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes  # noqa: PLC0415

        spi_get_client_area_animation = 0x1042
        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            spi_get_client_area_animation, 0, ctypes.byref(value), 0
        )
        if ok:
            return bool(value.value)
    except Exception:  # noqa: BLE001 — нет доступа к API: анимации по умолчанию
        pass
    return True


# ---------------------------------------------------------------------------
# Tween
# ---------------------------------------------------------------------------


class Tween(QObject):
    """Анимируемое float-значение.

    ``to(target, ms, ease)`` — плавно к цели от ТЕКУЩЕГО значения (переход
    можно перехватить на полпути без рывка); ``set(v)`` — мгновенно.
    При выключенных анимациях любой ``to`` срабатывает мгновенно.
    """

    def __init__(
        self,
        value: float,
        on_change: Callable[[float], None],
        *,
        enabled: Callable[[], bool] = lambda: True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._value = float(value)
        self._from = self._value
        self._to = self._value
        self._ease: Ease = ease_out_cubic
        self._on_change = on_change
        self._on_done: Optional[Callable[[], None]] = None
        self._enabled = enabled

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self._finish)

    @property
    def value(self) -> float:
        return self._value

    @property
    def target(self) -> float:
        return self._to

    @property
    def running(self) -> bool:
        return self._anim.state() == QVariantAnimation.State.Running

    def set(self, value: float) -> None:
        """Мгновенно установить значение (отменяет текущую анимацию)."""
        self._anim.stop()
        self._on_done = None
        self._from = self._to = self._value = float(value)
        self._on_change(self._value)

    def to(
        self,
        target: float,
        duration_ms: int,
        ease: Ease = ease_out_cubic,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        target = float(target)
        self._on_done = on_done
        if self.running and target == self._to:
            return  # уже едем туда же — не перезапускаем (иначе «залипание»)
        if not self.running and self._value == target:
            self._to = target
            self._call_done()
            return
        self._anim.stop()
        self._from = self._value
        self._to = target
        self._ease = ease
        if duration_ms <= 0 or not self._enabled():
            self._value = target
            self._on_change(target)
            self._call_done()
            return
        self._anim.setDuration(int(duration_ms))
        self._anim.start()

    def _step(self, progress) -> None:
        if not self.running:
            return  # valueChanged от setStartValue/stop — не кадр анимации
        t = min(1.0, max(0.0, float(progress)))
        self._value = self._from + (self._to - self._from) * self._ease(t)
        self._on_change(self._value)

    def _finish(self) -> None:
        self._value = self._to
        self._on_change(self._value)
        self._call_done()

    def _call_done(self) -> None:
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback()
