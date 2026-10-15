"""Окно виджета «Now Playing»: без рамки, прозрачное, со скруглением и тенью.

Ключевые решения (по согласованному плану):
  * тень и скругление рисуются вручную в ``paintEvent`` (без QGraphicsEffect,
    чтобы не конфликтовать с эффектами и не размывать текст);
  * фон — размытая копия обложки (downscale + upscale со SmoothTransformation),
    результат кэшируется и пересчитывается только при смене трека;
  * обложка 90×90 рисуется поверх фона со скруглением через clip-path;
  * заголовок прокручивается при переполнении, исполнитель обрезается elide;
  * кнопки управления проявляются при наведении с fade-анимацией;
  * окно перетаскивается за любую область, позиция сохраняется в настройках.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
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

logger = logging.getLogger("npw.ui")

# Геометрия (согласовано: окно ~360×110 + поле под тень).
SHADOW_MARGIN = 6
WINDOW_SIZE = QSize(372, 122)  # карточка 360×110
CARD_RADIUS = 14.0
COVER_MARGIN = 10
COVER_SIZE = 90
TEXT_LEFT = 112  # от левого края карточки
BUTTON_SIZE = 26
BUTTON_GAP = 8

SCROLL_SPEED = 0.8  # px за тик
SCROLL_GAP = 48  # отступ между концом и началом текста


def _elide(text: str, font: QFont, width: int) -> str:
    metrics = QFontMetrics(font)
    return metrics.elidedText(text, Qt.ElideMiddle, max(0, width))


def blur_pixmap(src: QPixmap, size: QSize) -> QPixmap:
    """«Размытие» без внешних библиотек: резкое уменьшение, затем увеличение."""
    if src.isNull() or size.width() <= 0 or size.height() <= 0:
        return QPixmap()
    small = src.scaled(
        QSize(14, 14), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    return small.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


class NowPlayingWidget(QWidget):
    #: Сигналы к main.py
    always_on_top_toggled = Signal(bool)
    autostart_toggled = Signal(bool)
    settings_requested = Signal()
    quit_requested = Signal()
    position_changed = Signal(int, int)

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings

        self.setWindowTitle("Now Playing")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, settings.always_on_top)
        self.setWindowFlag(Qt.Tool, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.resize(WINDOW_SIZE)

        # Данные трека
        self._title = ""
        self._artist = ""
        self._has_media = False
        self._is_playing = False
        self._cover: Optional[QPixmap] = None
        self._background: Optional[QPixmap] = None

        # Fade кнопок
        self._fade = 0
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(160)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.valueChanged.connect(self._on_fade_changed)

        # Состояние мыши/кнопок
        self._hover_button: Optional[str] = None
        self._dragging = False
        self._drag_offset = QPoint()
        self._drag_moved = False
        self._press_global = QPoint()
        self._user_hidden = False
        self._button_handlers: dict = {}

        # Прокрутка заголовка
        self._scroll_offset = 0.0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(35)
        self._scroll_timer.timeout.connect(self._on_scroll_tick)

        self._font_title = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
        self._font_artist = QFont("Segoe UI", 9)
        self._font_small = QFont("Segoe UI", 8)

    # ------------------------------------------------------------------
    # Публичный API (вызывается из main.py)
    # ------------------------------------------------------------------

    def set_settings(self, settings: Settings) -> None:
        """Применяет настройки (может пересоздать оконные флаги)."""
        was_visible = self.isVisible()
        self.settings = settings
        if self.windowFlags() & Qt.WindowStaysOnTopHint != bool_to_flag(
            settings.always_on_top
        ):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, settings.always_on_top)
        if was_visible:
            self.show()
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
        track_changed = title != self._title or artist != self._artist
        self._title = title
        self._artist = artist
        self._has_media = True
        if track_changed:
            self._scroll_offset = 0.0
        if cover_bytes:
            self._apply_cover(cover_bytes)
        elif track_changed:
            # Новый трек без обложки — старую не показываем (заглушка).
            self._apply_cover(None)
        if self._user_hidden:
            return
        if not self.isVisible():
            self.show()
        self.update()

    def set_cover(self, cover_bytes: Optional[bytes]) -> None:
        """Обложка обновилась без смены трека (или сброс на None → заглушка)."""
        logger.info(
            "UI ← cover_changed: %s hidden=%s",
            f"{len(cover_bytes)}B" if cover_bytes else None,
            self._user_hidden,
        )
        self._apply_cover(cover_bytes)
        if self._user_hidden:
            return
        self.update()

    def _apply_cover(self, cover_bytes: Optional[bytes]) -> None:
        """Загружает pixmap и размытый фон; None — очистка (заглушка)."""
        if not cover_bytes:
            self._cover = None
            self._background = None
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(cover_bytes):
            self._cover = pixmap
            self._background = blur_pixmap(pixmap, self.size())
        else:
            logger.warning("не удалось декодировать обложку (%dB)", len(cover_bytes))
            self._cover = None
            self._background = None

    def set_playing(self, is_playing: bool) -> None:
        logger.debug("UI ← playback_state_changed: %s", is_playing)
        self._is_playing = bool(is_playing)
        self.update()

    def set_no_media(self) -> None:
        """Медиа нет: скрыться (по настройке) или показать заглушку."""
        logger.info(
            "UI ← no_media: hide_when_no_media=%s hidden=%s",
            self.settings.hide_when_no_media,
            self._user_hidden,
        )
        self._has_media = False
        self._title = ""
        self._artist = ""
        self._cover = None
        self._background = None
        self._is_playing = False
        self._scroll_offset = 0.0
        if self._user_hidden:
            return
        if self.settings.hide_when_no_media:
            self.hide()
        else:
            self.show()
            self.update()

    def set_user_hidden(self, hidden: bool) -> None:
        """Скрытие/показ из трея (побеждает над настройкой «прятать без медиа»)."""
        self._user_hidden = bool(hidden)
        if hidden:
            self.hide()
        elif not (self.settings.hide_when_no_media and not self._has_media):
            self.show()

    @property
    def user_hidden(self) -> bool:
        return self._user_hidden

    def restore_position(self, x: Optional[int], y: Optional[int]) -> None:
        """Восстанавливает позицию с клампом к доступным экранам."""
        if x is None or y is None:
            self._center_bottom_right()
            return
        screen = self.screen() or None
        if screen is None:
            self.move(int(x), int(y))
            return
        area = screen.availableGeometry()
        nx = min(max(int(x), area.left()), area.right() - self.width() + 1)
        ny = min(max(int(y), area.top()), area.bottom() - self.height() + 1)
        self.move(nx, ny)

    def save_position(self) -> None:
        self.position_changed.emit(self.x(), self.y())

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
        card = self._card_rect()
        return QRect(
            card.left() + TEXT_LEFT,
            card.top() + 14,
            card.right() - 110 - (card.left() + TEXT_LEFT),
            card.height() - 28,
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

    def _center_bottom_right(self) -> None:
        screen = self.screen()
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

        card = self._card_rect()
        radius = CARD_RADIUS

        # --- тень (рисуется вручную, слоями) ---
        steps = SHADOW_MARGIN
        for i in range(steps, 0, -1):
            alpha = int(90 * ((steps - i + 1) / steps) ** 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(card.adjusted(-i, -i, i, i), radius + i, radius + i)

        # --- фон карточки (размытая обложка + затемнение) ---
        painter.save()
        try:
            path = QPainterPath()
            path.addRoundedRect(card, radius, radius)
            painter.setClipPath(path)

            if self._background is not None and not self._background.isNull():
                painter.drawPixmap(card.topLeft(), self._background)
                painter.fillRect(card, QColor(0, 0, 0, 140))
            else:
                gradient = QLinearGradient(card.topLeft(), card.bottomRight())
                gradient.setColorAt(0.0, QColor(32, 36, 44))
                gradient.setColorAt(1.0, QColor(18, 20, 25))
                painter.fillRect(card, gradient)

            # лёгкая рамка
            painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card, radius, radius)

            # --- обложка ---
            self._draw_cover(painter)

            # --- текст ---
            self._draw_texts(painter)

            # --- кнопки ---
            self._draw_buttons(painter)
        finally:
            painter.restore()

    def _draw_cover(self, painter: QPainter) -> None:
        rect = self._cover_rect()
        radius = 10.0
        painter.save()
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        if self._cover is not None and not self._cover.isNull():
            scaled = self._cover.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = rect.left() + (rect.width() - scaled.width()) // 2
            y = rect.top() + (rect.height() - scaled.height()) // 2
            painter.drawPixmap(QPoint(x, y), scaled)
        else:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(48, 54, 66))
            gradient.setColorAt(1.0, QColor(30, 34, 42))
            painter.fillRect(rect, gradient)
            # нотка-заглушка
            painter.setPen(QColor(255, 255, 255, 120))
            note_font = QFont("Segoe UI", 26)
            painter.setFont(note_font)
            metrics = painter.fontMetrics()
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, "\u266a"
            )
        painter.restore()

    def _draw_texts(self, painter: QPainter) -> None:
        area = self._text_rect()
        metrics_title = QFontMetrics(self._font_title)
        metrics_artist = QFontMetrics(self._font_artist)

        title = self._title if self._has_media else "Нет воспроизведения"
        artist = (
            self._artist
            if self._has_media
            else "Запустите музыку или видео"
        )

        painter.setFont(self._font_title)
        painter.setPen(QColor(255, 255, 255, 245))
        title_y = area.top() + metrics_title.ascent() + 6

        title_width = metrics_title.horizontalAdvance(title)
        if title and title_width > area.width() and self._has_media:
            # Прокрутка: рисуем два раза со сдвигом.
            painter.save()
            painter.setClipRect(area)
            x = area.left() - self._scroll_offset
            painter.drawText(QPoint(x, title_y), title)
            painter.drawText(
                QPoint(int(x + title_width + SCROLL_GAP), title_y), title
            )
            painter.restore()
        else:
            painter.drawText(
                QPoint(area.left(), title_y),
                _elide(title, self._font_title, area.width()),
            )

        painter.setFont(self._font_artist)
        painter.setPen(QColor(255, 255, 255, 165))
        artist_y = area.top() + metrics_title.height() + metrics_artist.ascent() + 8
        painter.drawText(
            QPoint(area.left(), artist_y),
            _elide(artist, self._font_artist, area.width()),
        )

    def _draw_buttons(self, painter: QPainter) -> None:
        if self._fade <= 0:
            return
        alpha = self._fade
        rects = self._button_rects()
        for name, rect in rects.items():
            hovered = self._hover_button == name
            base = QColor(255, 255, 255, int(38 * alpha / 255))
            if hovered:
                base = QColor(255, 255, 255, int(85 * alpha / 255))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(base)
            painter.drawRoundedRect(rect, rect.width() // 2, rect.height() // 2)

            color = QColor(255, 255, 255, int(235 * alpha / 255))
            if not self._has_media:
                color = QColor(255, 255, 255, int(90 * alpha / 255))
            self._draw_button_icon(painter, name, rect, color)

    def _draw_button_icon(
        self, painter: QPainter, name: str, rect: QRect, color: QColor
    ) -> None:
        painter.save()
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(color)
        cx = rect.center().x()
        cy = rect.center().y()
        s = 6.5
        if name == "play_pause":
            if self._is_playing:
                w = 3.4
                painter.drawRect(QRectF(cx - 5.0, cy - s, w, 2 * s))
                painter.drawRect(QRectF(cx + 1.6, cy - s, w, 2 * s))
            else:
                path = QPainterPath()
                path.moveTo(cx - 4.5, cy - s)
                path.lineTo(cx + 5.5, cy)
                path.lineTo(cx - 4.5, cy + s)
                path.closeSubpath()
                painter.drawPath(path)
        elif name == "next":
            path = QPainterPath()
            path.moveTo(cx - 5.5, cy - s)
            path.lineTo(cx + 2.5, cy)
            path.lineTo(cx - 5.5, cy + s)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawRect(QRectF(cx + 3.6, cy - s, 2.4, 2 * s))
        else:  # prev
            path = QPainterPath()
            path.moveTo(cx + 5.5, cy - s)
            path.lineTo(cx - 2.5, cy)
            path.lineTo(cx + 5.5, cy + s)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawRect(QRectF(cx - 6.0, cy - s, 2.4, 2 * s))
        painter.restore()

    # ------------------------------------------------------------------
    # Анимации/таймеры
    # ------------------------------------------------------------------

    def _on_fade_changed(self, value) -> None:
        self._fade = int(value)
        self.update()

    def _start_fade(self, target: int) -> None:
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._fade)
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def _on_scroll_tick(self) -> None:
        if not self.isVisible() or not self._has_media:
            return
        area = self._text_rect()
        metrics = QFontMetrics(self._font_title)
        width = metrics.horizontalAdvance(self._title)
        if width <= area.width():
            if self._scroll_offset:
                self._scroll_offset = 0.0
                self.update()
            return
        self._scroll_offset += SCROLL_SPEED * (self._scroll_timer.interval() / 35)
        if self._scroll_offset > width + SCROLL_GAP:
            self._scroll_offset = 0.0
        self.update()

    # ------------------------------------------------------------------
    # Мышь: hover, кнопки, перетаскивание
    # ------------------------------------------------------------------

    def enterEvent(self, event) -> None:  # noqa: N802
        self._start_fade(255)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_button = None
        self._start_fade(0)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging:
            delta = event.globalPosition().toPoint() - self._press_global
            if not self._drag_moved and manhattan(delta) > 3:
                self._drag_moved = True
            if self._drag_moved:
                self.move(self._drag_offset + delta)
            event.accept()
            return

        pos = event.position().toPoint()
        hovered = None
        if self._fade > 0:
            for name, rect in self._button_rects().items():
                if rect.contains(pos):
                    hovered = name
                    break
        if hovered != self._hover_button:
            self._hover_button = hovered
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_moved = False
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            if self._drag_moved:
                self.save_position()
            else:
                # Клик без движения: возможно, по кнопке.
                pos = event.position().toPoint()
                if self._has_media:
                    for name, rect in self._button_rects().items():
                        if rect.contains(pos):
                            self._on_button_clicked(name)
                            break
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_button_clicked(self, name: str) -> None:
        # Подключается из main.py к MediaController.
        handler = self._button_handlers.get(name)
        if handler is not None:
            handler()

    def set_button_handlers(self, handlers: dict) -> None:
        """Ставит коллбэки {name: callable} для кнопок prev/play_pause/next."""
        self._button_handlers = dict(handlers)

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
        self._scroll_timer.start()  # пересчитает необходимость прокрутки

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._scroll_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._scroll_timer.stop()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Крестик у безрамочного окна нет, но на всякий случай — сохраняем позицию.
        self.save_position()
        super().closeEvent(event)


def bool_to_flag(value: bool) -> Qt.WindowType:
    return Qt.WindowType.WindowStaysOnTopHint if value else Qt.WindowType(0)


def manhattan(point: QPoint) -> int:
    return abs(point.x()) + abs(point.y())
