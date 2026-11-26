"""Перемотка по кольцу, время на обложке, цвет кольца, прятание на паузе,
полупрозрачность, выбор плеера и расширенные настройки."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent

from media_controller import (
    MockSession,
    PlaybackModes,
    SessionInfo,
    TICKS_PER_SECOND,
    app_label,
    make_solid_png,
    parse_playback_modes,
    pick_session,
    seek_session,
    session_keys,
)
from settings import Settings


class FakeVolume:
    available = True
    muted = False

    def get(self):
        return 0.5

    def set(self, level):
        return True

    def set_muted(self, muted):
        return True


@pytest.fixture
def make_widget(qapp):
    from ui.widget import NowPlayingWidget

    created = []

    def _make(settings=None, cover=(200, 40, 40)):
        w = NowPlayingWidget(settings or Settings(), animations=False, volume=FakeVolume())
        created.append(w)
        w.set_track("Title", "Artist", make_solid_png(64, cover) if cover else None)
        w.show()
        return w

    yield _make
    for w in created:
        w.hide()
        w.deleteLater()


def _mouse(kind, widget, pos: QPoint, button=Qt.MouseButton.LeftButton, global_pos=None):
    gpos = QPointF(widget.mapToGlobal(pos) if global_pos is None else global_pos)
    return QMouseEvent(kind, QPointF(pos), gpos, button, button, Qt.KeyboardModifier.NoModifier)


def _click(w, pos: QPoint) -> None:
    w.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, w, pos))
    w.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, w, pos))


def _seekable(w, duration=200.0, position=20.0):
    w.set_modes(PlaybackModes(seek_supported=True))
    w.set_playing(False)
    w.set_timeline(position, duration)
    return w


def _ring_point(w, fraction: float) -> QPoint:
    from ui.widget import RING_SAMPLES, _ring_samples

    samples = _ring_samples(*w._ring_cache_key())
    x, y = samples[int(fraction * RING_SAMPLES) % RING_SAMPLES]
    return QPoint(round(x), round(y))


# --- перемотка кликом по кольцу --------------------------------------------


def test_ring_hit_test_maps_points_to_track_fraction(make_widget) -> None:
    w = _seekable(make_widget())
    top = w._ring_fraction_at(_ring_point(w, 0.0))
    assert top is not None and (top < 0.02 or top > 0.98)  # верх по центру — начало
    half = w._ring_fraction_at(_ring_point(w, 0.5))
    assert half == pytest.approx(0.5, abs=0.02)
    # Середина обложки — не кольцо.
    assert w._ring_fraction_at(w._cover_rect().center()) is None


def test_click_on_ring_requests_seek(make_widget) -> None:
    w = _seekable(make_widget(), duration=200.0)
    seen = []
    w.seek_requested.connect(seen.append)
    _click(w, _ring_point(w, 0.25))
    assert len(seen) == 1 and seen[0] == pytest.approx(50.0, abs=2.0)
    # Кольцо сразу передвинулось, не дожидаясь ответа плеера.
    assert w.progress_position() == pytest.approx(50.0, abs=2.0)


def test_no_seek_when_player_does_not_support_it(make_widget) -> None:
    w = make_widget()
    w.set_modes(PlaybackModes(seek_supported=False))
    w.set_timeline(20.0, 200.0)
    seen = []
    w.seek_requested.connect(seen.append)
    _click(w, _ring_point(w, 0.25))
    assert seen == []


def test_dragging_window_from_ring_does_not_seek(make_widget) -> None:
    w = _seekable(make_widget())
    seen = []
    w.seek_requested.connect(seen.append)
    start = _ring_point(w, 0.25)
    g = w.mapToGlobal(start)
    w.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, w, start, global_pos=g))
    w.mouseMoveEvent(
        _mouse(QMouseEvent.Type.MouseMove, w, start, Qt.MouseButton.NoButton, g + QPoint(40, 0))
    )
    w.mouseReleaseEvent(
        _mouse(QMouseEvent.Type.MouseButtonRelease, w, start, global_pos=g + QPoint(40, 0))
    )
    assert seen == []


# --- время на обложке ------------------------------------------------------


def test_hovering_cover_shows_time_overlay(make_widget) -> None:
    w = _seekable(make_widget())
    w._mouse_inside = True
    w._update_cover_hover(w._cover_rect().center())
    assert w._cover_hover.value == 1.0 and w._ring_hover is None
    w._update_cover_hover(_ring_point(w, 0.5))
    assert w._ring_hover == pytest.approx(0.5, abs=0.02)
    w.leaveEvent(None)
    assert w._cover_hover.value == 0.0 and w._ring_hover is None


def test_format_time() -> None:
    from ui.widget import format_time

    assert format_time(0) == "0:00"
    assert format_time(83.9) == "1:23"
    assert format_time(3723) == "1:02:03"


# --- цвет кольца из обложки ------------------------------------------------


def test_cover_accent_takes_hue_from_cover(qapp) -> None:
    from PySide6.QtGui import QPixmap

    from ui.widget import cover_accent

    red = QPixmap()
    red.loadFromData(make_solid_png(32, (220, 30, 30)))
    accent = cover_accent(red)
    assert accent is not None
    hue = accent.hsvHueF()
    assert hue < 0.05 or hue > 0.95
    assert accent.valueF() == pytest.approx(1.0)  # светлый — виден на тёмном фоне

    gray = QPixmap()
    gray.loadFromData(make_solid_png(32, (128, 128, 128)))
    assert cover_accent(gray) is None  # серая обложка — кольцо белое


def test_ring_color_follows_setting(make_widget) -> None:
    from ui.widget import RING_FILL

    w = make_widget(cover=(30, 160, 60))
    assert w._ring_color() != RING_FILL
    w.set_settings(Settings(ring_from_cover=False))
    assert w._ring_color() == RING_FILL


# --- прятать на паузе ------------------------------------------------------


def test_hide_on_pause_after_delay_and_return_on_play(make_widget) -> None:
    w = make_widget(Settings(hide_on_pause=True, hide_on_pause_delay=5))
    w.set_playing(True)
    w.set_playing(False)
    assert w._pause_timer.isActive() and w._pause_timer.interval() == 5000
    w._on_pause_timeout()  # время вышло
    assert not w.isVisible()
    w.set_playing(True)
    assert w.isVisible() and not w._pause_timer.isActive()


def test_hide_on_pause_waits_while_mouse_over_widget(make_widget) -> None:
    w = make_widget(Settings(hide_on_pause=True))
    w.set_playing(False)
    w._mouse_inside = True
    w._on_pause_timeout()
    assert w.isVisible() and w._pause_timer.isActive()


def test_hide_on_pause_off_by_default(make_widget) -> None:
    w = make_widget()
    w.set_playing(False)
    assert not w._pause_timer.isActive()


# --- полупрозрачность без мыши ---------------------------------------------


def test_idle_translucency(make_widget) -> None:
    w = make_widget(Settings(idle_translucent=True, idle_opacity=60))
    assert w.windowOpacity() == pytest.approx(0.6, abs=0.02)
    w.enterEvent(None)
    assert w.windowOpacity() == pytest.approx(1.0, abs=0.02)
    w.leaveEvent(None)
    assert w.windowOpacity() == pytest.approx(0.6, abs=0.02)
    w.set_settings(Settings(idle_translucent=False))
    assert w.windowOpacity() == pytest.approx(1.0, abs=0.02)


# --- выбор плеера ----------------------------------------------------------


def _wheel(w, pos: QPoint, delta: int) -> QWheelEvent:
    return QWheelEvent(
        QPointF(pos), QPointF(w.mapToGlobal(pos)), QPoint(0, 0), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


def test_wheel_over_cover_switches_player(make_widget) -> None:
    w = make_widget()
    steps = []
    w.session_cycle_requested.connect(steps.append)
    center = w._cover_rect().center()
    w.wheelEvent(_wheel(w, center, -120))  # одна сессия — некуда переключать
    assert steps == []
    w.set_sessions([SessionInfo("Chrome#0", "Chrome"), SessionInfo("Spotify#0", "Spotify")], False)
    w.wheelEvent(_wheel(w, center, -120))
    w.wheelEvent(_wheel(w, center, 120))
    assert steps == [1, -1]
    # Тачпад: мелкие шаги копятся до одного «щелчка».
    w.wheelEvent(_wheel(w, center, -60))
    assert steps == [1, -1]
    w.wheelEvent(_wheel(w, center, -60))
    assert steps == [1, -1, 1]


def test_app_labels() -> None:
    assert app_label("Chrome") == "Chrome"
    assert app_label("Spotify.exe") == "Spotify"
    assert app_label("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "Spotify"
    assert app_label("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == "Медиаплеер"
    assert app_label("C:\\Apps\\Strange Player.exe") == "Strange Player"
    assert app_label(None) == "Плеер"


def _session(aumid: str, status: int = 5):
    info = SimpleNamespace(playback_status=status)
    return SimpleNamespace(source_app_user_model_id=aumid, get_playback_info=lambda: info)


def test_session_keys_tell_apart_two_tabs_of_same_app() -> None:
    a, b, c = _session("Chrome"), _session("Chrome"), _session("Spotify")
    assert [k for k, _ in session_keys([a, b, c])] == ["Chrome#0", "Chrome#1", "Spotify#0"]


def test_pinned_player_wins_until_it_disappears() -> None:
    chrome, spotify = _session("Chrome", status=4), _session("Spotify", status=5)
    keyed = session_keys([chrome, spotify])
    # Авто: играет Chrome — показываем его.
    chosen, pinned = pick_session(keyed, chrome, None)
    assert chosen is chrome and pinned is None
    # Закрепили Spotify — показываем его, хоть он и на паузе.
    chosen, pinned = pick_session(keyed, chrome, "Spotify#0")
    assert chosen is spotify and pinned == "Spotify#0"
    # Spotify закрыли — закрепление снято, снова авто.
    chosen, pinned = pick_session(session_keys([chrome]), chrome, "Spotify#0")
    assert chosen is chrome and pinned is None


# --- перемотка в контроллере -----------------------------------------------


def test_parse_playback_modes_reads_seek_support() -> None:
    controls = SimpleNamespace(
        is_repeat_enabled=False, is_shuffle_enabled=False, is_playback_position_enabled=True
    )
    info = SimpleNamespace(auto_repeat_mode=None, is_shuffle_active=None, controls=controls)
    assert parse_playback_modes(info).seek_supported is True


def test_seek_session_sends_ticks() -> None:
    sent = []

    async def change(ticks):
        sent.append(ticks)
        return True

    session = SimpleNamespace(try_change_playback_position_async=change)
    assert asyncio.run(seek_session(session, 12.5)) is True
    assert sent == [int(12.5 * TICKS_PER_SECOND)]


def test_mock_session_seek() -> None:
    mock = MockSession(playing=False)
    mock.seek(3.0)
    assert mock.position == pytest.approx(3.0)
    mock.seek(999)
    assert mock.position == pytest.approx(mock.duration)


# --- настройки -------------------------------------------------------------


def test_new_settings_parse_and_clamp() -> None:
    s = Settings.from_dict(
        {
            "snap_to_edges": 0,
            "no_media_delay": 99,
            "hide_on_pause": 1,
            "hide_on_pause_delay": 0,
            "idle_opacity": 5,
            "ring_from_cover": False,
        }
    )
    assert s.snap_to_edges is False
    assert s.no_media_delay == 10.0
    assert s.hide_on_pause is True and s.hide_on_pause_delay == 1
    assert s.idle_opacity == 20
    assert s.ring_from_cover is False
    assert Settings.from_dict({"idle_opacity": "мусор"}).idle_opacity == 60
    assert Settings.from_dict(Settings(idle_opacity=75).to_dict()).idle_opacity == 75


def test_settings_dialog_roundtrip(qapp) -> None:
    from ui.settings_dialog import SettingsDialog

    original = Settings(scale=0.677, hide_on_pause=True, hide_on_pause_delay=30, idle_opacity=45)
    dialog = SettingsDialog(original)
    result = dialog.result_settings
    # Ничего не трогали — масштаб, заданный мышью, не округлился.
    assert result.scale == pytest.approx(0.677)
    assert result.hide_on_pause and result.hide_on_pause_delay == 30
    assert result.idle_opacity == 45
    dialog._scale.setValue(80)
    dialog._snap.setChecked(False)
    dialog._no_media_delay.setValue(1.5)
    result = dialog.result_settings
    assert result.scale == pytest.approx(0.8)
    assert result.snap_to_edges is False
    assert result.no_media_delay == pytest.approx(1.5)
    dialog.deleteLater()


def test_snap_can_be_disabled(make_widget, qapp) -> None:
    from PySide6.QtGui import QGuiApplication

    from ui.widget import SHADOW_MARGIN

    w = make_widget(Settings(snap_to_edges=False))
    area = QGuiApplication.primaryScreen().availableGeometry()
    w._dragging = True
    w._drag_moved = True
    w._drag_offset = QPoint(area.left() - SHADOW_MARGIN + 6, area.center().y())
    w._press_global = QPoint(0, 0)
    w.mouseMoveEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseMove, QPointF(5, 5), QPointF(0, 0),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
    )
    assert w.pos() == QPoint(area.left() - SHADOW_MARGIN + 6, area.center().y())
    w._dragging = False


def test_zero_no_media_delay_reacts_immediately(make_widget) -> None:
    w = make_widget(Settings(hide_when_no_media=True, no_media_delay=0.0))
    w.set_no_media()
    assert not w.isVisible()
