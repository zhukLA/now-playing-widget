"""Размер окна, крестик, громкость и кольцо прогресса."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from media_controller import make_solid_png, parse_timeline
from settings import MIN_SCALE, Settings


class FakeVolume:
    """Вместо системной громкости — не трогаем настройки звука на машине."""

    available = True

    def __init__(self, level: float = 0.4) -> None:
        self.level = level
        self.muted = False

    def get(self):
        return self.level

    def set(self, level: float) -> bool:
        self.level = level
        return True

    def set_muted(self, muted: bool) -> bool:
        self.muted = muted
        return True


@pytest.fixture
def make_widget(qapp):
    from ui.widget import NowPlayingWidget

    created = []

    def _make(settings=None, volume=None):
        w = NowPlayingWidget(settings or Settings(), animations=False, volume=volume or FakeVolume())
        created.append(w)
        w.set_track("Title", "Artist", make_solid_png(64, (200, 50, 50)))
        w.show()
        return w

    yield _make
    for w in created:
        w.hide()
        w.deleteLater()


def _event(kind, widget, pos: QPoint, global_x: int | None = None):
    point = QPointF(pos)
    gpos = QPointF(widget.mapToGlobal(pos) if global_x is None else QPoint(global_x, 0))
    return QMouseEvent(
        kind, point, gpos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


# --- настройки -------------------------------------------------------------


def test_settings_scale_roundtrip_and_clamp() -> None:
    s = Settings.from_dict({"scale": 0.8, "mini": True})
    assert (s.scale, s.mini) == (0.8, True)
    assert Settings.from_dict({"scale": 5}).scale == 1.0
    assert Settings.from_dict({"scale": 0.01}).scale == MIN_SCALE
    assert Settings.from_dict({"scale": "мусор"}).scale == 1.0


# --- размер ----------------------------------------------------------------


def test_default_is_maximum_size(make_widget) -> None:
    from ui.widget import WINDOW_SIZE

    w = make_widget()
    assert w.size() == WINDOW_SIZE and not w.mini


def test_scale_shrinks_window_proportionally(make_widget) -> None:
    from ui.widget import WINDOW_SIZE

    w = make_widget()
    w.set_display_mode(0.6, False)
    assert w.width() == round(WINDOW_SIZE.width() * 0.6)
    assert w.height() == round(WINDOW_SIZE.height() * 0.6)


def test_mini_mode_has_only_three_buttons(make_widget) -> None:
    w = make_widget()
    w.set_display_mode(1.0, True)
    assert w.mini
    assert set(w._button_rects()) == {"prev", "play_pause", "next"}
    assert w.width() < 160


def test_size_is_restored_from_settings(make_widget) -> None:
    w = make_widget(Settings(scale=0.7, mini=False))
    assert w.scale == 0.7
    mini = make_widget(Settings(mini=True))
    assert mini.mini


def test_scale_change_emits_signal(make_widget) -> None:
    w = make_widget()
    seen = []
    w.scale_changed.connect(lambda s, m: seen.append((s, m)))
    w.set_display_mode(0.8, False)
    assert seen == [(0.8, False)]


def test_drag_corner_resizes_and_snaps_to_mini(make_widget) -> None:
    from ui.widget import WINDOW_SIZE

    w = make_widget()
    seen = []
    w.scale_changed.connect(lambda s, m: seen.append((s, m)))
    grip = w._grip_rect().center()
    w.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, w, grip, 1000))
    # Тянем влево на ~100 px: масштаб уменьшился, но это ещё полный режим.
    w.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, w, grip, 900))
    assert not w.mini and w.scale < 1.0
    # Тянем совсем узко — мини-режим.
    w.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, w, grip, 1000 - 250))
    assert w.mini
    w.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, w, grip, 750))
    assert seen and seen[-1][1] is True
    assert WINDOW_SIZE.width() > w.width()


# --- крестик ---------------------------------------------------------------


def test_close_button_requests_quit(make_widget) -> None:
    w = make_widget()
    quits = []
    w.quit_requested.connect(lambda: quits.append(1))
    pos = w._close_rect().center()
    w.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, w, pos))
    w.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, w, pos))
    assert quits == [1]


def test_close_button_works_in_mini_mode(make_widget) -> None:
    w = make_widget(Settings(mini=True))
    quits = []
    w.quit_requested.connect(lambda: quits.append(1))
    pos = w._close_rect().center()
    w.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, w, pos))
    w.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, w, pos))
    assert quits == [1]


def test_close_does_not_overlap_buttons_or_text(make_widget) -> None:
    w = make_widget()
    close = w._close_rect()
    assert not close.intersects(w._text_rect())
    assert all(not close.intersects(r) for r in w._button_rects().values())
    mini = make_widget(Settings(mini=True))
    assert all(not mini._close_rect().intersects(r) for r in mini._button_rects().values())


# --- громкость -------------------------------------------------------------


def test_hover_opens_volume_slider_and_drag_sets_level(make_widget) -> None:
    vol = FakeVolume(0.4)
    w = make_widget(volume=vol)
    icon = w._volume_icon_rect().center()
    w._mouse_inside = True
    w._update_volume_hover(icon)
    assert w._volume_open.value == 1.0

    track = w._volume_track_rect()
    top = QPoint(round(track.center().x()), round(track.top()))
    w.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, w, top))
    assert vol.level == pytest.approx(1.0, abs=0.01)
    bottom = QPoint(round(track.center().x()), round(track.bottom()))
    w.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, w, bottom))
    assert vol.level == pytest.approx(0.0, abs=0.01)
    w.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, w, bottom))


def test_volume_slider_closes_when_cursor_leaves(make_widget) -> None:
    w = make_widget()
    w._mouse_inside = True
    w._update_volume_hover(w._volume_icon_rect().center())
    assert w._volume_open.value == 1.0
    w._update_volume_hover(QPoint(300, 40))
    assert w._volume_open.value == 0.0


def test_click_on_volume_icon_toggles_mute(make_widget) -> None:
    vol = FakeVolume(0.4)
    w = make_widget(volume=vol)
    pos = w._volume_icon_rect().center()
    w.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, w, pos))
    w.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, w, pos))
    assert vol.muted is True


def test_no_volume_icon_in_mini_mode(make_widget) -> None:
    w = make_widget(Settings(mini=True))
    assert "volume" not in w._extra_rects()


# --- кольцо прогресса ------------------------------------------------------


def test_progress_fraction_from_timeline(make_widget) -> None:
    w = make_widget()
    w.set_playing(False)
    w.set_timeline(45.0, 180.0)
    assert w.progress_fraction() == pytest.approx(0.25, abs=0.01)


def test_progress_advances_only_while_playing(make_widget, pump) -> None:
    w = make_widget()
    w.set_timeline(10.0, 100.0)
    w.set_playing(False)
    before = w.progress_position()
    pump(lambda: False, timeout=0.15)
    assert w.progress_position() == pytest.approx(before, abs=0.01)
    w.set_playing(True)
    pump(lambda: False, timeout=0.25)
    assert w.progress_position() > before + 0.1


def test_progress_unknown_duration_is_zero(make_widget) -> None:
    w = make_widget()
    w.set_timeline(30.0, 0.0)
    assert w.progress_fraction() == 0.0


def test_new_track_resets_ring(make_widget) -> None:
    w = make_widget()
    w.set_timeline(90.0, 180.0)
    w.set_track("Other", "Artist", None)
    assert w.progress_fraction() == 0.0


def test_ring_does_not_touch_text_or_cover(make_widget) -> None:
    w = make_widget()
    ring = w._ring_rect().toAlignedRect()
    assert ring.contains(w._cover_rect())
    assert not ring.intersects(w._text_rect())


def test_ring_path_is_closed_loop_from_top_center(make_widget) -> None:
    from PySide6.QtCore import QRectF

    from ui.widget import ring_path

    rect = QRectF(0, 0, 94, 94)
    path = ring_path(rect, 16)
    start = path.pointAtPercent(0.0)
    assert start.x() == pytest.approx(47.0) and start.y() == pytest.approx(0.0)
    quarter = path.pointAtPercent(0.25)  # четверть пути — уже на правой стороне
    assert quarter.x() > 47


# --- разбор SMTC timeline --------------------------------------------------


def _props(position: float, end: float, updated: datetime | None, start: float = 0.0):
    return SimpleNamespace(
        position=timedelta(seconds=position),
        start_time=timedelta(seconds=start),
        end_time=timedelta(seconds=end),
        last_updated_time=updated,
    )


def test_parse_timeline_adds_elapsed_time_while_playing() -> None:
    now = datetime(2026, 1, 1, 12, 0, 10, tzinfo=timezone.utc)
    props = _props(30.0, 200.0, now - timedelta(seconds=5))
    assert parse_timeline(props, True, now) == (pytest.approx(35.0), 200.0)
    assert parse_timeline(props, False, now) == (pytest.approx(30.0), 200.0)


def test_parse_timeline_clamps_and_handles_missing() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_timeline(_props(500.0, 200.0, now), False, now)[0] == 200.0
    assert parse_timeline(_props(5.0, 0.0, now), True, now) == (0.0, 0.0)  # длительность неизвестна
    assert parse_timeline(None, True) == (0.0, 0.0)


def test_parse_timeline_respects_start_offset() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pos, dur = parse_timeline(_props(40.0, 160.0, now, start=10.0), False, now)
    assert (pos, dur) == (pytest.approx(30.0), pytest.approx(150.0))


# --- провал «нет медиа» между треками --------------------------------------


def test_gap_between_tracks_does_not_hide_widget(make_widget, pump) -> None:
    """Браузер на ~1 с сообщает «сессии нет» при смене трека — не прятаться."""
    w = make_widget(Settings(hide_when_no_media=True))
    assert w.isVisible()
    w.set_no_media()
    assert w.isVisible(), "реакция должна быть отложена"
    w.set_track("Next", "Artist", None)
    pump(lambda: False, timeout=0.2)
    assert w.isVisible() and w._has_media


def test_real_no_media_still_hides_after_grace(make_widget, pump, monkeypatch) -> None:
    import ui.widget as widget_module

    monkeypatch.setattr(widget_module, "NO_MEDIA_GRACE_MS", 50)
    w = make_widget(Settings(hide_when_no_media=True))
    w.set_no_media()
    assert pump(lambda: not w.isVisible(), timeout=1.0)


def test_volume_icon_hidden_when_system_volume_unavailable(make_widget) -> None:
    vol = FakeVolume()
    vol.available = False
    w = make_widget(volume=vol)
    assert "volume" not in w._extra_rects()
    w._mouse_inside = True
    w._update_volume_hover(w._volume_icon_rect().center())
    assert w._volume_open.value == 0.0
