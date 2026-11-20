"""Тесты анимаций: кривые, Tween и поведение виджета с включёнными анимациями."""

from __future__ import annotations

import pytest

import ui.widget as widget_mod
from media_controller import make_solid_png
from settings import Settings
from ui.anim import (
    REDUCE_MOTION_ENV_VAR,
    Tween,
    ease_in_out_cubic,
    ease_out_cubic,
    linear,
    spring,
    system_animations_enabled,
)

LONG_TITLE = "A very very long title that definitely needs to scroll " * 2


# ---------------------------------------------------------------------------
# Кривые
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ease", [linear, ease_out_cubic, ease_in_out_cubic, spring])
def test_easing_endpoints(ease) -> None:
    assert ease(0.0) == pytest.approx(0.0, abs=1e-9)
    assert ease(1.0) == pytest.approx(1.0, abs=2e-3)


def test_ease_in_out_is_monotonic_and_symmetric() -> None:
    values = [ease_in_out_cubic(i / 100) for i in range(101)]
    assert values == sorted(values)
    assert ease_in_out_cubic(0.5) == pytest.approx(0.5)


def test_spring_overshoots_slightly_then_settles() -> None:
    peak = max(spring(i / 1000) for i in range(1001))
    assert 1.02 < peak < 1.12, "лёгкий перелёт, как у пружины macOS"
    assert spring(0.95) == pytest.approx(1.0, abs=0.01)


def test_reduce_motion_env() -> None:
    assert system_animations_enabled({REDUCE_MOTION_ENV_VAR: "1"}) is False
    assert system_animations_enabled({REDUCE_MOTION_ENV_VAR: "yes"}) is False


# ---------------------------------------------------------------------------
# Tween
# ---------------------------------------------------------------------------


def test_tween_reaches_target_and_calls_done(qapp, pump) -> None:
    seen: list[float] = []
    done: list[bool] = []
    tween = Tween(0.0, seen.append)
    tween.to(10.0, 60, ease_out_cubic, on_done=lambda: done.append(True))
    assert tween.running
    assert pump(lambda: done == [True])
    assert tween.value == 10.0
    assert seen[-1] == 10.0
    assert all(0.0 <= v <= 10.0 for v in seen)
    assert len(seen) >= 2, "должны быть промежуточные кадры"


def test_tween_retarget_starts_from_current_value(qapp, pump) -> None:
    """Перехват на полпути — без рывка: новый переход стартует с текущего значения."""
    seen: list[float] = []
    tween = Tween(0.0, seen.append)
    tween.to(1.0, 400, linear)
    assert pump(lambda: tween.value > 0.2)
    midway = tween.value
    seen.clear()
    tween.to(0.0, 400, linear)
    assert pump(lambda: len(seen) > 0)
    assert seen[0] <= midway + 0.05
    assert pump(lambda: not tween.running)
    assert tween.value == 0.0


def test_tween_disabled_is_instant() -> None:
    seen: list[float] = []
    done: list[bool] = []
    tween = Tween(0.0, seen.append, enabled=lambda: False)
    tween.to(5.0, 500, on_done=lambda: done.append(True))
    assert tween.value == 5.0 and not tween.running
    assert seen == [5.0] and done == [True]


def test_tween_same_target_calls_done_immediately() -> None:
    done: list[bool] = []
    tween = Tween(1.0, lambda v: None)
    tween.to(1.0, 300, on_done=lambda: done.append(True))
    assert done == [True]


# ---------------------------------------------------------------------------
# Виджет с анимациями
# ---------------------------------------------------------------------------


@pytest.fixture
def awidget(qapp):
    from ui.widget import NowPlayingWidget

    w = NowPlayingWidget(Settings(), animations=True)
    yield w
    w.hide()
    w.deleteLater()


def _cover(rgb=(200, 50, 50)) -> bytes:
    return make_solid_png(64, rgb)


def test_window_fades_in(awidget, pump) -> None:
    awidget.set_track("T", "A", None)
    assert awidget.isVisible() and awidget.is_shown
    assert awidget._window_opacity.running
    assert pump(lambda: not awidget._window_opacity.running)
    assert awidget._window_opacity.value == 1.0


def test_window_fades_out_then_hides(awidget, pump) -> None:
    awidget.set_track("T", "A", None)
    pump(lambda: not awidget._window_opacity.running)
    awidget.set_user_hidden(True)
    assert awidget.is_shown is False
    assert awidget.isVisible(), "окно ещё догорает"
    assert pump(lambda: not awidget.isVisible())


def test_show_during_fade_out_cancels_hide(awidget, pump) -> None:
    awidget.set_track("T", "A", None)
    pump(lambda: not awidget._window_opacity.running)
    awidget.set_user_hidden(True)
    pump(lambda: awidget._window_opacity.value < 0.8)
    awidget.set_user_hidden(False)
    assert pump(lambda: awidget._window_opacity.value == 1.0)
    assert awidget.isVisible() and awidget.is_shown


def test_track_change_crossfades(awidget, pump) -> None:
    awidget.set_track("One", "A", _cover())
    pump(lambda: not awidget._xfade.running and not awidget._window_opacity.running)
    awidget.set_track("Two", "B", _cover((20, 90, 200)))
    prev = awidget._prev
    assert prev is not None and prev.title == "One"
    assert awidget._xfade.running
    image = awidget.grab()  # отрисовка на середине перехода не падает
    assert not image.isNull()
    assert pump(lambda: awidget._prev is None)
    assert awidget._xfade.value == 1.0


def test_late_cover_keeps_text_steady(awidget, pump) -> None:
    """Поздняя обложка того же трека: меняется картинка, текст не «мигает»."""
    awidget.set_track("One", "A", None)
    pump(lambda: not awidget._xfade.running)
    awidget.set_cover(_cover())
    prev = awidget._prev
    assert prev is not None
    assert prev.text_key == (awidget._title, awidget._artist, awidget._has_media)
    assert prev.cover is None and awidget._cover_scaled is not None


def test_same_cover_does_not_restart_transition(awidget, pump) -> None:
    cover = _cover()
    awidget.set_track("One", "A", cover)
    pump(lambda: awidget._prev is None)
    awidget.set_cover(cover)
    assert awidget._prev is None


def test_play_pause_morph_and_cover_spring(awidget, pump) -> None:
    awidget.set_track("T", "A", None)
    assert awidget._cover_scale.value == pytest.approx(widget_mod.COVER_SCALE_PAUSED)
    awidget.set_playing(True)
    assert pump(lambda: not awidget._play_morph.running and not awidget._cover_scale.running)
    assert awidget._play_morph.value == 1.0
    assert awidget._cover_scale.value == 1.0
    awidget.set_playing(False)
    assert pump(lambda: not awidget._play_morph.running)
    assert awidget._play_morph.value == 0.0


def test_marquee_pauses_then_scrolls(awidget, pump, monkeypatch) -> None:
    monkeypatch.setattr(widget_mod, "MARQUEE_PAUSE_MS", 50)
    awidget.set_track(LONG_TITLE, "Artist", None)
    assert awidget._title_overflows()
    assert awidget._marquee_pause.isActive(), "сначала строка стоит"
    assert awidget._marquee_offset == 0.0
    assert pump(lambda: awidget._marquee_offset > 3.0)
    awidget.grab()  # отрисовка с маской краёв не падает


def test_marquee_wraps_and_pauses_again(awidget, pump, monkeypatch) -> None:
    monkeypatch.setattr(widget_mod, "MARQUEE_PAUSE_MS", 10)
    monkeypatch.setattr(widget_mod, "MARQUEE_SPEED", 4000.0)
    awidget.set_track(LONG_TITLE, "Artist", None)
    assert pump(lambda: awidget._marquee_offset > 0.0)
    # Полный круг → сброс в 0 и новая пауза.
    assert pump(lambda: awidget._marquee_offset == 0.0 and awidget._marquee_pause.isActive())


def test_track_change_restarts_marquee(awidget, pump, monkeypatch) -> None:
    monkeypatch.setattr(widget_mod, "MARQUEE_PAUSE_MS", 10)
    awidget.set_track(LONG_TITLE, "Artist", None)
    assert pump(lambda: awidget._marquee_offset > 1.0)
    awidget.set_track(LONG_TITLE + " (Remix)", "Artist", None)
    assert awidget._marquee_offset == 0.0


def test_hover_fade_and_button_press(awidget, pump) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QEnterEvent, QMouseEvent

    clicks: list[str] = []
    awidget.set_button_handlers({"next": lambda: clicks.append("next")})
    awidget.set_track("T", "A", None)
    pump(lambda: not awidget._window_opacity.running)

    center = QPointF(awidget._button_rects()["next"].center())
    awidget.enterEvent(QEnterEvent(center, center, center))
    assert pump(lambda: awidget._buttons.value == 1.0)

    def mouse(kind, button=Qt.MouseButton.LeftButton):
        buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
        return QMouseEvent(kind, center, center, button, buttons, Qt.KeyboardModifier.NoModifier)

    awidget.mouseMoveEvent(mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton))
    assert awidget._hover_button == "next"
    assert pump(lambda: awidget._hover["next"].value == 1.0)

    awidget.mousePressEvent(mouse(QEvent.Type.MouseButtonPress))
    assert pump(lambda: awidget._press["next"].value > 0.5), "кнопка «проседает»"
    awidget.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease))
    assert clicks == ["next"]
    assert pump(lambda: not awidget._press["next"].running)
    assert awidget._press["next"].value == 0.0, "пружина вернула кнопку"


# ---------------------------------------------------------------------------
# Неоновая тема
# ---------------------------------------------------------------------------


def test_neon_flicker_curve() -> None:
    from ui.anim import neon_flicker

    assert neon_flicker(0.0) == 0.0 and neon_flicker(1.0) == 1.0
    samples = [neon_flicker(i / 100) for i in range(101)]
    assert all(0.0 <= v <= 1.0 for v in samples)
    # Есть «провалы» — трубка мигает, а не просто плавно разгорается.
    peak, deepest_dip = 0.0, 0.0
    for v in samples:
        peak = max(peak, v)
        deepest_dip = max(deepest_dip, peak - v)
    assert deepest_dip > 0.5


def test_neon_flickers_on_appear(awidget, pump) -> None:
    assert awidget.theme == "neon"
    awidget.set_track("T", "A", None)
    assert awidget._neon_flicker.running
    assert pump(lambda: not awidget._neon_flicker.running)
    assert awidget._neon_flicker.value == 1.0


def test_neon_flow_runs_only_while_playing(awidget, pump) -> None:
    awidget.set_track("T", "A", _cover())
    assert not awidget._flow_timer.isActive(), "на паузе цвет по рамке не течёт"
    awidget.set_playing(True)
    assert awidget._flow_timer.isActive()
    start = awidget._flow_angle
    assert pump(lambda: awidget._flow_angle != start)
    assert pump(lambda: awidget._neon_power.value == 1.0)
    awidget.set_playing(False)
    assert not awidget._flow_timer.isActive()
    assert pump(lambda: awidget._neon_power.value == pytest.approx(widget_mod.NEON_PAUSED_POWER))


def test_neon_flow_stops_when_hidden(awidget, pump) -> None:
    awidget.set_track("T", "A", None)
    awidget.set_playing(True)
    assert awidget._flow_timer.isActive()
    awidget.set_user_hidden(True)
    assert pump(lambda: not awidget.isVisible())
    assert not awidget._flow_timer.isActive()


def test_theme_switch_at_runtime(awidget, pump) -> None:
    awidget.set_track(LONG_TITLE, "Artist", _cover())
    awidget.set_playing(True)
    awidget.set_settings(Settings(theme="classic"))
    assert awidget.theme == "classic"
    assert not awidget._flow_timer.isActive()
    assert not awidget.grab().isNull()
    awidget.set_settings(Settings(theme="neon"))
    assert awidget.theme == "neon"
    assert awidget._flow_timer.isActive()
    assert awidget._neon_flicker.running, "включение неона — с «зажиганием»"
    assert not awidget.grab().isNull()


@pytest.mark.parametrize("theme", ["neon", "classic"])
def test_paint_all_states(qapp, theme) -> None:
    """Все состояния рисуются без исключений в обеих темах."""
    from ui.widget import NowPlayingWidget

    w = NowPlayingWidget(Settings(theme=theme), animations=True)
    try:
        w.set_no_media()
        assert not w.grab().isNull()
        w.set_track(LONG_TITLE, "Artist", _cover())
        w._marquee_offset = 37.5
        w._buttons.set(1.0)
        w._hover["play_pause"].set(1.0)
        w._press["next"].set(0.7)
        w._play_morph.set(0.5)
        assert not w.grab().isNull()
    finally:
        w.hide()
        w.deleteLater()


def test_drag_cancels_button_press(awidget, pump) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    clicks: list[str] = []
    awidget.set_button_handlers({"next": lambda: clicks.append("next")})
    awidget.set_track("T", "A", None)
    start = QPointF(awidget._button_rects()["next"].center())
    moved = start + QPointF(30, 0)

    def mouse(kind, pos, buttons):
        return QMouseEvent(
            kind, pos, awidget.mapToGlobal(pos), Qt.MouseButton.LeftButton
            if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton,
            buttons, Qt.KeyboardModifier.NoModifier,
        )

    awidget.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton))
    assert awidget._pressed_button == "next"
    awidget.mouseMoveEvent(mouse(QEvent.Type.MouseMove, moved, Qt.MouseButton.LeftButton))
    assert awidget._pressed_button is None
    awidget.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, moved, Qt.MouseButton.NoButton))
    assert clicks == []
