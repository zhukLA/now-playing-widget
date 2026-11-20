"""Окно виджета «Now Playing»: без рамки, прозрачное, со скруглением и тенью.

Ключевые решения:
  * тень и скругление рисуются вручную в ``paintEvent`` (без QGraphicsEffect,
    чтобы не конфликтовать с эффектами и не размывать текст);
  * фон — размытая копия обложки (downscale + upscale со SmoothTransformation),
    результат кэшируется и пересчитывается только при смене обложки;
  * обложка 90×90 рисуется поверх фона со скруглением и мягкой тенью;
  * окно перетаскивается за любую область, позиция сохраняется в настройках.

Анимации (в духе macOS, см. ``ui/anim.py``):
  * окно плавно проявляется/гаснет (прозрачность окна);
  * смена трека — перекрёстное затухание фона, обложки и текста, текст
    слегка «въезжает» снизу;
  * обложка чуть уменьшается на паузе и пружинит обратно при воспроизведении;
  * длинный заголовок — бегущая строка: пауза в начале, плавная прокрутка
    по времени (а не по тикам), растворение текста у краёв;
  * кнопки: проявление при наведении, плавная подсветка, «нажатие» с
    пружиной, морфинг значка play ↔ pause.
Все анимации отключаются системной настройкой «Эффекты анимации» Windows
или ``NOW_PLAYING_REDUCE_MOTION=1``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import (
    QElapsedTimer,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import QMenu, QWidget

from settings import Settings
from ui.anim import (
    Tween,
    ease_in_out_cubic,
    ease_out_cubic,
    spring,
    system_animations_enabled,
)

logger = logging.getLogger("npw.ui")

# Геометрия (окно ~360×110 + поле под тень).
SHADOW_MARGIN = 6
WINDOW_SIZE = QSize(372, 122)  # карточка 360×110
CARD_RADIUS = 14.0
COVER_MARGIN = 10
COVER_SIZE = 90
COVER_RADIUS = 10.0
TEXT_LEFT = 112  # от левого края карточки
TEXT_RIGHT_MARGIN = 14  # отступ текста от правого края карточки
BUTTON_SIZE = 26
BUTTON_GAP = 8

NO_MEDIA_TITLE = "Нет воспроизведения"
NO_MEDIA_ARTIST = "Запустите музыку или видео"

TITLE_COLOR = QColor(255, 255, 255, 245)
ARTIST_COLOR = QColor(255, 255, 255, 165)

# Тайминги анимаций, мс.
WINDOW_FADE_IN_MS = 240
WINDOW_FADE_OUT_MS = 200
TRACK_XFADE_MS = 420
TEXT_SLIDE_PX = 5.0
BUTTONS_IN_MS = 200
BUTTONS_OUT_MS = 280
HOVER_IN_MS = 120
HOVER_OUT_MS = 220
PRESS_MS = 90
RELEASE_MS = 480
PLAY_MORPH_MS = 240
COVER_SCALE_MS = 600
COVER_SCALE_PAUSED = 0.94
BUTTON_PRESS_SCALE = 0.12  # насколько «проседает» нажатая кнопка

# Бегущая строка заголовка.
MARQUEE_SPEED = 32.0  # px/с
MARQUEE_PAUSE_MS = 2200  # пауза в начале каждого круга
MARQUEE_GAP = 48  # отступ между концом и началом текста
MARQUEE_FADE = 18  # ширина растворения текста у краёв, px
MARQUEE_FRAME_MS = 16  # ~60 кадров/с

BUTTON_NAMES = ("prev", "play_pause", "next")


def _elide(text: str, font: QFont, width: int) -> str:
    metrics = QFontMetrics(font)
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, width))


def blur_pixmap(src: QPixmap, size: QSize) -> QPixmap:
    """«Размытие» без внешних библиотек: резкое уменьшение, затем увеличение."""
    if src.isNull() or size.width() <= 0 or size.height() <= 0:
        return QPixmap()
    small = src.scaled(
        QSize(14, 14),
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    return small.scaled(
        size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


@dataclass(frozen=True)
class _Frame:
    """Снимок отображаемого содержимого — «старый» слой при переходе."""

    title: str
    artist: str
    has_media: bool
    cover: Optional[QPixmap]
    background: Optional[QPixmap]

    @property
    def text_key(self) -> tuple[str, str, bool]:
        return (self.title, self.artist, self.has_media)


class NowPlayingWidget(QWidget):
    #: Сигналы к main.py
    always_on_top_toggled = Signal(bool)
    autostart_toggled = Signal(bool)
    settings_requested = Signal()
    quit_requested = Signal()
    position_changed = Signal(int, int)

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
        *,
        animations: Optional[bool] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._animations = (
            system_animations_enabled() if animations is None else bool(animations)
        )

        self.setWindowTitle("Now Playing")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.always_on_top)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.resize(WINDOW_SIZE)

        # Данные трека
        self._title = ""
        self._artist = ""
        self._has_media = False
        self._is_playing = False
        self._cover_bytes: Optional[bytes] = None
        self._cover: Optional[QPixmap] = None
        # Обложка, уже отмасштабированная под COVER_SIZE (не пересчитывать
        # SmoothTransformation на каждом кадре анимации).
        self._cover_scaled: Optional[QPixmap] = None
        self._background: Optional[QPixmap] = None

        # Видимость: логическая цель (окно может ещё догорать на fade-out).
        self._target_visible = False
        self._user_hidden = False

        # Состояние мыши/кнопок
        self._mouse_inside = False
        self._hover_button: Optional[str] = None
        self._pressed_button: Optional[str] = None
        self._dragging = False
        self._drag_offset = QPoint()
        self._drag_moved = False
        self._press_global = QPoint()
        self._button_handlers: dict = {}

        # --- анимации ---
        enabled = lambda: self._animations  # noqa: E731
        repaint = lambda _v: self.update()  # noqa: E731
        self._window_opacity = Tween(
            1.0, self._on_window_opacity, enabled=enabled, parent=self
        )
        # Переход между треками: 0 → 1, старый слой — self._prev.
        self._prev: Optional[_Frame] = None
        self._xfade = Tween(1.0, repaint, enabled=enabled, parent=self)
        self._buttons = Tween(0.0, repaint, enabled=enabled, parent=self)
        self._hover = {n: Tween(0.0, repaint, enabled=enabled, parent=self) for n in BUTTON_NAMES}
        self._press = {n: Tween(0.0, repaint, enabled=enabled, parent=self) for n in BUTTON_NAMES}
        # 0 — значок play, 1 — pause.
        self._play_morph = Tween(0.0, repaint, enabled=enabled, parent=self)
        self._cover_scale = Tween(COVER_SCALE_PAUSED, repaint, enabled=enabled, parent=self)

        # Бегущая строка: пауза → прокрутка по времени → пауза …
        self._marquee_offset = 0.0
        self._marquee_clock = QElapsedTimer()
        self._marquee_frame = QTimer(self)
        self._marquee_frame.setInterval(MARQUEE_FRAME_MS)
        self._marquee_frame.setTimerType(Qt.TimerType.PreciseTimer)
        self._marquee_frame.timeout.connect(self._marquee_tick)
        self._marquee_pause = QTimer(self)
        self._marquee_pause.setSingleShot(True)
        self._marquee_pause.timeout.connect(self._marquee_resume)

        self._font_title = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
        self._font_artist = QFont("Segoe UI", 9)

    # ------------------------------------------------------------------
    # Публичный API (вызывается из main.py)
    # ------------------------------------------------------------------

    @property
    def animations_enabled(self) -> bool:
        return self._animations

    @property
    def is_shown(self) -> bool:
        """Логическая видимость (во время fade-out уже False)."""
        return self._target_visible

    def set_settings(self, settings: Settings) -> None:
        """Применяет настройки (может пересоздать оконные флаги).

        Видимость пересчитывается заново: смена флага окна скрывает его,
        а изменение «прятать без медиа» должно сработать сразу.
        """
        self.settings = settings
        on_top = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if on_top != settings.always_on_top:
            was_shown = self._target_visible
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.always_on_top)
            if was_shown and self._should_be_visible():
                # Окно пересоздано — вернуть сразу, без повторного проявления.
                self._window_opacity.set(1.0)
                self.show()
        self._apply_visibility()
        self.update()

    def set_track(self, title: str, artist: str, cover_bytes: Optional[bytes]) -> None:
        """Новые данные трека. Обложка пересчитывается только при её наличии."""
        logger.info(
            "UI ← track_changed: title_len=%d artist_len=%d cover=%s hidden=%s",
            len(title),
            len(artist),
            f"{len(cover_bytes)}B" if cover_bytes else None,
            self._user_hidden,
        )
        track_changed = (
            title != self._title or artist != self._artist or not self._has_media
        )
        cover_changed = bool(cover_bytes) and cover_bytes != self._cover_bytes
        # Новый трек без обложки — старую не показываем (заглушка).
        clear_cover = not cover_bytes and track_changed and self._cover_bytes is not None
        if track_changed or cover_changed or clear_cover:
            self._begin_transition()
        self._title = title
        self._artist = artist
        self._has_media = True
        if cover_changed:
            self._apply_cover(cover_bytes)
        elif clear_cover:
            self._apply_cover(None)
        if track_changed:
            self._restart_marquee()
        self._apply_visibility()
        self.update()

    def set_cover(self, cover_bytes: Optional[bytes]) -> None:
        """Обложка обновилась без смены трека (или сброс на None → заглушка)."""
        logger.info(
            "UI ← cover_changed: %s hidden=%s",
            f"{len(cover_bytes)}B" if cover_bytes else None,
            self._user_hidden,
        )
        if (cover_bytes or None) == self._cover_bytes:
            return
        self._begin_transition()
        self._apply_cover(cover_bytes)
        self.update()

    def set_playing(self, is_playing: bool) -> None:
        logger.debug("UI ← playback_state_changed: %s", is_playing)
        self._is_playing = bool(is_playing)
        self._play_morph.to(
            1.0 if self._is_playing else 0.0, PLAY_MORPH_MS, ease_in_out_cubic
        )
        self._cover_scale.to(
            1.0 if self._is_playing else COVER_SCALE_PAUSED, COVER_SCALE_MS, spring
        )
        self.update()

    def set_no_media(self) -> None:
        """Медиа нет: скрыться (по настройке) или показать заглушку."""
        logger.info(
            "UI ← no_media: hide_when_no_media=%s hidden=%s",
            self.settings.hide_when_no_media,
            self._user_hidden,
        )
        if self._has_media or self._cover_bytes is not None:
            self._begin_transition()
        self._has_media = False
        self._title = ""
        self._artist = ""
        self._apply_cover(None)
        self.set_playing(False)
        self._restart_marquee()
        self._apply_visibility()
        self.update()

    def set_user_hidden(self, hidden: bool) -> None:
        """Скрытие/показ из трея (побеждает над настройкой «прятать без медиа»)."""
        self._user_hidden = bool(hidden)
        self._apply_visibility()

    @property
    def user_hidden(self) -> bool:
        return self._user_hidden

    def restore_position(self, x: Optional[int], y: Optional[int]) -> None:
        """Восстанавливает позицию с клампом к экрану, на котором она лежит.

        Экран ищется по сохранённой точке (а не ``self.screen()``, который до
        первого показа всегда основной) — иначе на нескольких мониторах окно
        «перепрыгивало» на основной экран при каждом запуске. Если монитор
        с сохранённой позицией отключён — окно переносится на основной.
        """
        if x is None or y is None:
            self._center_bottom_right()
            return
        x, y = int(x), int(y)
        screen = (
            QGuiApplication.screenAt(QPoint(x + self.width() // 2, y + self.height() // 2))
            or QGuiApplication.screenAt(QPoint(x, y))
            or QGuiApplication.primaryScreen()
        )
        if screen is None:
            self.move(x, y)
            return
        area = screen.availableGeometry()
        nx = min(max(x, area.left()), area.right() - self.width() + 1)
        ny = min(max(y, area.top()), area.bottom() - self.height() + 1)
        self.move(nx, ny)

    def save_position(self) -> None:
        self.position_changed.emit(self.x(), self.y())

    def set_button_handlers(self, handlers: dict) -> None:
        """Ставит коллбэки {name: callable} для кнопок prev/play_pause/next."""
        self._button_handlers = dict(handlers)

    # ------------------------------------------------------------------
    # Видимость окна (с плавным проявлением/угасанием)
    # ------------------------------------------------------------------

    def _should_be_visible(self) -> bool:
        if self._user_hidden:
            return False
        return self._has_media or not self.settings.hide_when_no_media

    def _apply_visibility(self) -> None:
        """Единое правило видимости: скрыт пользователем > нет медиа > показ."""
        if self._should_be_visible():
            self._fade_window_in()
        else:
            self._fade_window_out()

    def _fade_window_in(self) -> None:
        self._target_visible = True
        if not self.isVisible():
            self._window_opacity.set(0.0)
            self.show()
        self._window_opacity.to(1.0, WINDOW_FADE_IN_MS, ease_out_cubic)

    def _fade_window_out(self) -> None:
        self._target_visible = False
        if not self.isVisible():
            return
        self._window_opacity.to(
            0.0, WINDOW_FADE_OUT_MS, ease_in_out_cubic, on_done=self._finish_hide
        )

    def _finish_hide(self) -> None:
        if not self._target_visible:
            self.hide()

    def _on_window_opacity(self, value: float) -> None:
        self.setWindowOpacity(max(0.0, min(1.0, value)))

    # ------------------------------------------------------------------
    # Обложка и переход между треками
    # ------------------------------------------------------------------

    def _snapshot(self) -> _Frame:
        return _Frame(
            title=self._title,
            artist=self._artist,
            has_media=self._has_media,
            cover=self._cover_scaled,
            background=self._background,
        )

    def _begin_transition(self) -> None:
        """Запомнить текущее содержимое как «старый» слой и начать переход."""
        if not (self._animations and self.isVisible()):
            self._prev = None
            self._xfade.set(1.0)
            return
        self._prev = self._snapshot()
        self._xfade.set(0.0)
        self._xfade.to(1.0, TRACK_XFADE_MS, ease_in_out_cubic, on_done=self._end_transition)

    def _end_transition(self) -> None:
        self._prev = None
        self.update()

    def _clear_cover(self) -> None:
        self._cover_bytes = None
        self._cover = None
        self._cover_scaled = None
        self._background = None

    def _apply_cover(self, cover_bytes: Optional[bytes]) -> None:
        """Загружает pixmap, масштабированную копию и размытый фон; None — заглушка."""
        if not cover_bytes:
            self._clear_cover()
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(cover_bytes):
            logger.warning("не удалось декодировать обложку (%dB)", len(cover_bytes))
            self._clear_cover()
            return
        self._cover_bytes = cover_bytes
        self._cover = pixmap
        self._cover_scaled = pixmap.scaled(
            QSize(COVER_SIZE, COVER_SIZE),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._background = blur_pixmap(pixmap, self.size())

    # ------------------------------------------------------------------
    # Геометрия
    # ------------------------------------------------------------------

    def _card_rect(self) -> QRect:
        return QRect(
            SHADOW_MARGIN,
            SHADOW_MARGIN,
            self.width() - 2 * SHADOW_MARGIN,
            self.height() - 2 * SHADOW_MARGIN,
        )

    def _cover_rect(self) -> QRect:
        card = self._card_rect()
        return QRect(
            card.left() + COVER_MARGIN,
            card.top() + COVER_MARGIN,
            COVER_SIZE,
            COVER_SIZE,
        )

    def _text_rect(self) -> QRect:
        # Текст занимает всю ширину справа от обложки: кнопки живут в нижней
        # части карточки и с двумя строками текста не пересекаются.
        card = self._card_rect()
        left = card.left() + TEXT_LEFT
        return QRect(
            left,
            card.top() + 14,
            card.right() - TEXT_RIGHT_MARGIN - left,
            card.height() - 28,
        )

    def _title_baseline(self) -> int:
        return self._text_rect().top() + QFontMetrics(self._font_title).ascent() + 6

    def _title_rect(self) -> QRect:
        """Полоса заголовка — её перерисовывает бегущая строка."""
        area = self._text_rect()
        return QRect(
            area.left(), area.top(), area.width(), QFontMetrics(self._font_title).height() + 10
        )

    def _button_rects(self) -> dict[str, QRect]:
        card = self._card_rect()
        right = card.right() - 10
        bottom = card.bottom() - 12 - BUTTON_SIZE
        rects = {}
        for i, name in enumerate(("next", "play_pause", "prev")):
            x = right - i * (BUTTON_SIZE + BUTTON_GAP) - BUTTON_SIZE
            rects[name] = QRect(x, bottom, BUTTON_SIZE, BUTTON_SIZE)
        return rects

    def _button_at(self, pos: QPoint) -> Optional[str]:
        for name, rect in self._button_rects().items():
            if rect.contains(pos):
                return name
        return None

    def _center_bottom_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(100, 100)
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 16, area.bottom() - self.height() - 16)

    # ------------------------------------------------------------------
    # Отрисовка
    # ------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            if painter.isActive():
                painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        card = self._card_rect()
        radius = CARD_RADIUS
        prev = self._prev
        t = self._xfade.value if prev is not None else 1.0

        # --- тень (рисуется вручную, слоями) ---
        steps = SHADOW_MARGIN
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(steps, 0, -1):
            alpha = int(90 * ((steps - i + 1) / steps) ** 2)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(card.adjusted(-i, -i, i, i), radius + i, radius + i)

        painter.save()
        try:
            path = QPainterPath()
            path.addRoundedRect(QRectF(card), radius, radius)
            painter.setClipPath(path)

            # --- фон: старый слой целиком, новый проявляется поверх ---
            if prev is not None and prev.background is not self._background:
                self._draw_background(painter, card, prev.background, 1.0)
                self._draw_background(painter, card, self._background, t)
            else:
                self._draw_background(painter, card, self._background, 1.0)

            # лёгкая рамка
            painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card, radius, radius)

            self._draw_cover(painter, prev, t)
            self._draw_texts(painter, prev, t)
            self._draw_buttons(painter)
        finally:
            painter.restore()

    def _draw_background(
        self, painter: QPainter, card: QRect, background: Optional[QPixmap], opacity: float
    ) -> None:
        if opacity <= 0.0:
            return
        painter.save()
        painter.setOpacity(opacity)
        if background is not None and not background.isNull():
            painter.drawPixmap(card.topLeft(), background)
            painter.fillRect(card, QColor(0, 0, 0, 140))
        else:
            gradient = QLinearGradient(QPointF(card.topLeft()), QPointF(card.bottomRight()))
            gradient.setColorAt(0.0, QColor(32, 36, 44))
            gradient.setColorAt(1.0, QColor(18, 20, 25))
            painter.fillRect(card, gradient)
        painter.restore()

    # -- обложка -------------------------------------------------------------

    def _draw_cover(self, painter: QPainter, prev: Optional[_Frame], t: float) -> None:
        rect = QRectF(self._cover_rect())
        scale = self._cover_scale.value
        painter.save()
        center = rect.center()
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)

        # Мягкая тень под обложкой — «приподнята» над фоном, как в macOS.
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(4, 0, -1):
            painter.setBrush(QColor(0, 0, 0, 14 + (4 - i) * 6))
            painter.drawRoundedRect(
                rect.adjusted(-i, -i + 2, i, i + 2), COVER_RADIUS + i, COVER_RADIUS + i
            )

        path = QPainterPath()
        path.addRoundedRect(rect, COVER_RADIUS, COVER_RADIUS)
        painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
        if prev is not None and prev.cover is not self._cover_scaled:
            self._draw_cover_layer(painter, rect, prev.cover, 1.0)
            self._draw_cover_layer(painter, rect, self._cover_scaled, t)
        else:
            self._draw_cover_layer(painter, rect, self._cover_scaled, 1.0)
        painter.restore()

    def _draw_cover_layer(
        self, painter: QPainter, rect: QRectF, cover: Optional[QPixmap], opacity: float
    ) -> None:
        if opacity <= 0.0:
            return
        painter.save()
        painter.setOpacity(opacity)
        if cover is not None and not cover.isNull():
            x = rect.left() + (rect.width() - cover.width()) / 2
            y = rect.top() + (rect.height() - cover.height()) / 2
            painter.drawPixmap(QPointF(x, y), cover)
        else:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(48, 54, 66))
            gradient.setColorAt(1.0, QColor(30, 34, 42))
            painter.fillRect(rect, gradient)
            painter.setPen(QColor(255, 255, 255, 120))
            painter.setFont(QFont("Segoe UI", 26))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "♪")
        painter.restore()

    # -- текст ---------------------------------------------------------------

    def _draw_texts(self, painter: QPainter, prev: Optional[_Frame], t: float) -> None:
        current = (self._title, self._artist, self._has_media)
        if prev is not None and prev.text_key != current:
            # Старый текст уходит вверх и гаснет, новый въезжает снизу.
            self._draw_text_block(
                painter, *prev.text_key, opacity=1.0 - t, dy=-TEXT_SLIDE_PX * t, marquee=False
            )
            self._draw_text_block(
                painter, *current, opacity=t, dy=TEXT_SLIDE_PX * (1.0 - t), marquee=True
            )
        else:
            self._draw_text_block(painter, *current, opacity=1.0, dy=0.0, marquee=True)

    def _draw_text_block(
        self,
        painter: QPainter,
        title: str,
        artist: str,
        has_media: bool,
        *,
        opacity: float,
        dy: float,
        marquee: bool,
    ) -> None:
        if opacity <= 0.0:
            return
        area = self._text_rect()
        metrics_title = QFontMetrics(self._font_title)
        metrics_artist = QFontMetrics(self._font_artist)
        if not has_media:
            title, artist = NO_MEDIA_TITLE, NO_MEDIA_ARTIST

        painter.save()
        painter.setOpacity(opacity)
        painter.translate(0.0, dy)

        if marquee and self._title_overflows(title, has_media):
            self._draw_marquee(painter, title)
        else:
            painter.setFont(self._font_title)
            painter.setPen(TITLE_COLOR)
            painter.drawText(
                QPointF(area.left(), self._title_baseline()),
                _elide(title, self._font_title, area.width()),
            )

        painter.setFont(self._font_artist)
        painter.setPen(ARTIST_COLOR)
        artist_y = area.top() + metrics_title.height() + metrics_artist.ascent() + 8
        painter.drawText(
            QPointF(area.left(), artist_y),
            _elide(artist, self._font_artist, area.width()),
        )
        painter.restore()

    def _title_overflows(self, title: Optional[str] = None, has_media: Optional[bool] = None) -> bool:
        """Нужна ли бегущая строка (при выключенных анимациях — elide)."""
        title = self._title if title is None else title
        has_media = self._has_media if has_media is None else has_media
        if not (self._animations and has_media and title):
            return False
        width = QFontMetrics(self._font_title).horizontalAdvance(title)
        return width > self._text_rect().width()

    def _draw_marquee(self, painter: QPainter, title: str) -> None:
        """Заголовок со сдвигом и растворением у краёв (маска через слой)."""
        rect = self._title_rect()
        dpr = max(1.0, self.devicePixelRatioF())
        layer = QPixmap(
            QSize(math.ceil(rect.width() * dpr), math.ceil(rect.height() * dpr))
        )
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.GlobalColor.transparent)

        width = QFontMetrics(self._font_title).horizontalAdvance(title)
        period = width + MARQUEE_GAP
        offset = self._marquee_offset
        baseline = self._title_baseline() - rect.top()

        p = QPainter(layer)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            p.setFont(self._font_title)
            p.setPen(TITLE_COLOR)
            p.drawText(QPointF(-offset, baseline), title)
            p.drawText(QPointF(-offset + period, baseline), title)

            # Левый край растворяется, только пока у края нет начала строки —
            # иначе первая буква «мигала» бы при каждом новом круге.
            near_start = min(offset, period - offset)
            left_alpha = int(255 * (1.0 - min(1.0, near_start / MARQUEE_FADE)))
            w = float(rect.width())
            f = min(0.45, MARQUEE_FADE / w)
            mask = QLinearGradient(0.0, 0.0, w, 0.0)
            mask.setColorAt(0.0, QColor(0, 0, 0, left_alpha))
            mask.setColorAt(f, QColor(0, 0, 0, 255))
            mask.setColorAt(1.0 - f, QColor(0, 0, 0, 255))
            mask.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            p.fillRect(QRectF(0.0, 0.0, w, float(rect.height())), mask)
        finally:
            p.end()
        painter.drawPixmap(rect.topLeft(), layer)

    # -- кнопки --------------------------------------------------------------

    def _draw_buttons(self, painter: QPainter) -> None:
        alpha = self._buttons.value
        if alpha <= 0.001:
            return
        painter.save()
        # Кнопки слегка всплывают при проявлении.
        painter.translate(0.0, 3.0 * (1.0 - alpha))
        for name, rect in self._button_rects().items():
            hover = self._hover[name].value
            press = self._press[name].value
            scale = 1.0 - BUTTON_PRESS_SCALE * press
            center = QRectF(rect).center()

            painter.save()
            painter.translate(center)
            painter.scale(scale, scale)
            painter.translate(-center)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, int((30 + 55 * hover) * alpha)))
            painter.drawEllipse(QRectF(rect))

            icon_alpha = (235 if self._has_media else 90) * alpha
            if name == "play_pause":
                m = self._play_morph.value
                self._draw_glyph(painter, "play", center, icon_alpha * (1.0 - m), 0.55 + 0.45 * (1.0 - m))
                self._draw_glyph(painter, "pause", center, icon_alpha * m, 0.55 + 0.45 * m)
            else:
                self._draw_glyph(painter, name, center, icon_alpha, 1.0)
            painter.restore()
        painter.restore()

    def _draw_glyph(
        self, painter: QPainter, glyph: str, center: QPointF, alpha: float, scale: float
    ) -> None:
        if alpha <= 0.5:
            return
        color = QColor(255, 255, 255, int(min(255.0, alpha)))
        painter.save()
        painter.translate(center)
        painter.scale(scale, scale)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        s = 6.5
        if glyph == "pause":
            painter.drawRoundedRect(QRectF(-5.0, -s, 3.4, 2 * s), 1.0, 1.0)
            painter.drawRoundedRect(QRectF(1.6, -s, 3.4, 2 * s), 1.0, 1.0)
        elif glyph == "play":
            path = QPainterPath()
            path.moveTo(-4.5, -s)
            path.lineTo(5.5, 0.0)
            path.lineTo(-4.5, s)
            path.closeSubpath()
            painter.drawPath(path)
        elif glyph == "next":
            path = QPainterPath()
            path.moveTo(-5.5, -s)
            path.lineTo(2.5, 0.0)
            path.lineTo(-5.5, s)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawRoundedRect(QRectF(3.6, -s, 2.4, 2 * s), 0.8, 0.8)
        else:  # prev
            path = QPainterPath()
            path.moveTo(5.5, -s)
            path.lineTo(-2.5, 0.0)
            path.lineTo(5.5, s)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawRoundedRect(QRectF(-6.0, -s, 2.4, 2 * s), 0.8, 0.8)
        painter.restore()

    # ------------------------------------------------------------------
    # Бегущая строка
    # ------------------------------------------------------------------

    def _restart_marquee(self) -> None:
        """С начала: строка стоит MARQUEE_PAUSE_MS, затем поехали."""
        self._marquee_frame.stop()
        self._marquee_pause.stop()
        self._marquee_offset = 0.0
        if self.isVisible() and self._title_overflows():
            self._marquee_pause.start(MARQUEE_PAUSE_MS)

    def _marquee_resume(self) -> None:
        if not (self.isVisible() and self._title_overflows()):
            return
        self._marquee_clock.start()
        self._marquee_frame.start()

    def _marquee_tick(self) -> None:
        if not (self.isVisible() and self._title_overflows()):
            self._marquee_frame.stop()
            self._marquee_offset = 0.0
            self.update(self._title_rect())
            return
        # Сдвиг по реальному времени: скорость не зависит от частоты таймера
        # и «подлагиваний» цикла событий (кламп — без скачка после зависания).
        dt = min(0.1, self._marquee_clock.restart() / 1000.0)
        self._marquee_offset += MARQUEE_SPEED * dt
        period = QFontMetrics(self._font_title).horizontalAdvance(self._title) + MARQUEE_GAP
        if self._marquee_offset >= period:
            # Вторая копия ровно на месте первой — сброс незаметен.
            self._marquee_offset = 0.0
            self._marquee_frame.stop()
            self._marquee_pause.start(MARQUEE_PAUSE_MS)
        # Перерисовываем только полосу заголовка, а не всю карточку с тенью.
        self.update(self._title_rect())

    # ------------------------------------------------------------------
    # Мышь: hover, кнопки, перетаскивание
    # ------------------------------------------------------------------

    def enterEvent(self, event) -> None:  # noqa: N802
        self._mouse_inside = True
        self._buttons.to(1.0, BUTTONS_IN_MS, ease_out_cubic)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._mouse_inside = False
        self._set_hover(None)
        self._buttons.to(0.0, BUTTONS_OUT_MS, ease_in_out_cubic)
        super().leaveEvent(event)

    def _set_hover(self, name: Optional[str]) -> None:
        if name == self._hover_button:
            return
        if self._hover_button is not None:
            self._hover[self._hover_button].to(0.0, HOVER_OUT_MS, ease_in_out_cubic)
        if name is not None:
            self._hover[name].to(1.0, HOVER_IN_MS, ease_out_cubic)
        self._hover_button = name

    def _release_press(self) -> None:
        if self._pressed_button is not None:
            self._press[self._pressed_button].to(0.0, RELEASE_MS, spring)
            self._pressed_button = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging:
            delta = event.globalPosition().toPoint() - self._press_global
            if not self._drag_moved and manhattan(delta) > 3:
                self._drag_moved = True
                self._release_press()  # начали тащить — это не нажатие кнопки
                self._set_hover(None)
            if self._drag_moved:
                self.move(self._drag_offset + delta)
            event.accept()
            return

        pos = event.position().toPoint()
        self._set_hover(self._button_at(pos) if self._mouse_inside else None)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_moved = False
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = self.pos()
            name = self._button_at(event.position().toPoint())
            if name is not None and self._has_media:
                self._pressed_button = name
                self._press[name].to(1.0, PRESS_MS, ease_out_cubic)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            pressed = self._pressed_button
            self._release_press()
            if self._drag_moved:
                self.save_position()
            elif pressed is not None and self._button_at(event.position().toPoint()) == pressed:
                self._on_button_clicked(pressed)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_button_clicked(self, name: str) -> None:
        # Подключается из main.py к MediaController.
        handler = self._button_handlers.get(name)
        if handler is not None:
            handler()

    # ------------------------------------------------------------------
    # Контекстное меню
    # ------------------------------------------------------------------

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)

        top_action = menu.addAction("Поверх всех окон")
        top_action.setCheckable(True)
        top_action.setChecked(self.settings.always_on_top)

        autostart_action = menu.addAction("Автозапуск")
        autostart_action.setCheckable(True)
        autostart_action.setChecked(self.settings.autostart_enabled)

        menu.addSeparator()
        settings_action = menu.addAction("Настройки")
        close_action = menu.addAction("Закрыть")

        chosen = menu.exec(event.globalPos())
        if chosen is top_action:
            self.always_on_top_toggled.emit(top_action.isChecked())
        elif chosen is autostart_action:
            self.autostart_toggled.emit(autostart_action.isChecked())
        elif chosen is settings_action:
            self.settings_requested.emit()
        elif chosen is close_action:
            self.quit_requested.emit()

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._cover is not None and not self._cover.isNull():
            self._background = blur_pixmap(self._cover, self.size())
        self._restart_marquee()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._restart_marquee()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._marquee_frame.stop()
        self._marquee_pause.stop()
        self._marquee_offset = 0.0

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # Крестика у безрамочного окна нет, но Alt+F4 работает. Считаем это
        # скрытием пользователем: иначе окно само всплывало бы на следующем
        # треке. Вернуть — из трея или повторным запуском приложения.
        self.save_position()
        self._user_hidden = True
        self._target_visible = False
        super().closeEvent(event)


def manhattan(point: QPoint) -> int:
    return abs(point.x()) + abs(point.y())
