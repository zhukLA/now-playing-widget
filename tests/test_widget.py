"""Тесты окна виджета (offscreen): видимость, геометрия, отрисовка."""

from __future__ import annotations

import pytest

from media_controller import make_solid_png
from settings import Settings


@pytest.fixture
def widget(qapp):
    from ui.widget import NowPlayingWidget

    w = NowPlayingWidget(Settings())
    yield w
    w.hide()
    w.deleteLater()


def _cover() -> bytes:
    return make_solid_png(64, (200, 50, 50))


# ---------------------------------------------------------------------------
# Видимость
# ---------------------------------------------------------------------------


def test_no_media_shows_placeholder_by_default(widget) -> None:
    widget.set_no_media()
    assert widget.isVisible()


def test_no_media_hides_when_setting_enabled(widget) -> None:
    widget.set_settings(Settings(hide_when_no_media=True))
    widget.set_no_media()
    assert not widget.isVisible()
    widget.set_track("T", "A", None)
    assert widget.isVisible()


def test_enabling_hide_setting_applies_immediately(widget) -> None:
    """Раньше галочка «прятать без медиа» срабатывала только на следующем no_media."""
    widget.set_no_media()
    assert widget.isVisible()
    widget.set_settings(Settings(hide_when_no_media=True))
    assert not widget.isVisible()
    widget.set_settings(Settings(hide_when_no_media=False))
    assert widget.isVisible()


def test_user_hidden_wins_over_track_change(widget) -> None:
    widget.set_track("T", "A", None)
    widget.set_user_hidden(True)
    widget.set_track("T2", "A2", None)
    assert not widget.isVisible()
    widget.set_settings(Settings())  # смена настроек не «будит» окно
    assert not widget.isVisible()
    widget.set_user_hidden(False)
    assert widget.isVisible()


def test_close_event_marks_user_hidden(widget) -> None:
    widget.set_track("T", "A", None)
    widget.close()
    assert widget.user_hidden
    widget.set_track("T2", "A2", None)
    assert not widget.isVisible(), "после Alt+F4 окно не должно всплывать само"


# ---------------------------------------------------------------------------
# Геометрия и отрисовка
# ---------------------------------------------------------------------------


def test_text_area_uses_card_width(widget) -> None:
    """Текст занимает всё место справа от обложки (раньше ~137 px из 360)."""
    text = widget._text_rect()
    card = widget._card_rect()
    assert text.right() >= card.right() - 20
    assert text.width() > 200


def test_text_does_not_overlap_buttons_vertically(widget) -> None:
    from PySide6.QtGui import QFontMetrics

    area = widget._text_rect()
    m_title = QFontMetrics(widget._font_title)
    m_artist = QFontMetrics(widget._font_artist)
    artist_bottom = area.top() + m_title.height() + 8 + m_artist.height()
    top_of_buttons = min(r.top() for r in widget._button_rects().values())
    assert artist_bottom <= top_of_buttons


def test_cover_is_prescaled_once(widget) -> None:
    from ui.widget import COVER_SIZE

    widget.set_track("T", "A", _cover())
    scaled = widget._cover_scaled
    assert scaled is not None
    assert scaled.width() == COVER_SIZE and scaled.height() == COVER_SIZE
    widget.set_cover(None)
    assert widget._cover_scaled is None and widget._background is None


def test_long_title_is_elided_without_animations(widget) -> None:
    """При «уменьшить движение» бегущей строки нет — заголовок обрезается."""
    widget.set_track("A very very long title " * 4, "Artist", None)
    assert widget._title_overflows() is False
    assert not widget._marquee_pause.isActive()
    assert not widget._marquee_frame.isActive()


@pytest.mark.parametrize("glyph", ["play", "pause", "next", "prev"])
def test_glyphs_fit_button_and_are_centered(qapp, glyph) -> None:
    """Значки помещаются в кнопку и отцентрованы (play — с оптическим сдвигом)."""
    from ui.widget import BUTTON_SIZE, glyph_path

    bounds = glyph_path(glyph).boundingRect()
    assert not bounds.isEmpty()
    limit = BUTTON_SIZE / 2 - 4  # отступ от края круга
    assert bounds.left() >= -limit and bounds.right() <= limit
    assert bounds.top() >= -limit and bounds.bottom() <= limit
    assert abs(bounds.center().y()) < 0.01
    tolerance = 1.2 if glyph == "play" else 0.01
    assert abs(bounds.center().x()) <= tolerance


def test_prev_next_are_mirrored(qapp) -> None:
    from ui.widget import glyph_path

    nxt = glyph_path("next").boundingRect()
    prv = glyph_path("prev").boundingRect()
    assert nxt.left() == pytest.approx(-prv.right())
    assert nxt.width() == pytest.approx(prv.width())


def test_restore_position_clamps_to_screen(widget, qapp) -> None:
    from PySide6.QtGui import QGuiApplication

    area = QGuiApplication.primaryScreen().availableGeometry()
    widget.restore_position(area.right() + 5000, area.bottom() + 5000)
    assert widget.x() + widget.width() - 1 <= area.right()
    assert widget.y() + widget.height() - 1 <= area.bottom()
    widget.restore_position(area.left() + 10, area.top() + 20)
    assert (widget.x(), widget.y()) == (area.left() + 10, area.top() + 20)
