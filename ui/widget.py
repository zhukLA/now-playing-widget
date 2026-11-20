"""Окно виджета «Now Playing»: без рамки, прозрачное, со скруглением.

Ключевые решения:
  * вся отрисовка — вручную в ``paintEvent`` (без QGraphicsEffect, чтобы не
    конфликтовать с эффектами и не размывать текст);
  * фон — размытая копия обложки (downscale + upscale со SmoothTransformation),
    результат кэшируется и пересчитывается только при смене обложки;
  * окно перетаскивается за любую область, позиция сохраняется в настройках.

Темы (``settings.theme``):
  * ``neon`` — ночной неон в духе Vice City: светящаяся рамка-трубка с
    «перетекающим» градиентом розовый → оранжевый → фиолетовый → бирюзовый,
    свечение текста, иконок и обложки, мерцание при включении, приглушение
    на паузе, лёгкие «сканлайны»;
  * ``classic`` — спокойная тёмная карточка с тенью.

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
    QActionGroup,
    QBrush,
    QCloseEvent,
    QColor,
    QConicalGradient,
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

from settings import THEME_CLASSIC, THEME_NEON, THEMES, Settings
from ui.anim import (
    Tween,
    ease_in_out_cubic,
    ease_out_cubic,
    neon_flicker,
    spring,
    system_animations_enabled,
)

logger = logging.getLogger("npw.ui")

# Геометрия: карточка 360×110 + поле под тень/неоновое свечение.
WINDOW_MARGIN = 14
CARD_SIZE = QSize(360, 110)
WINDOW_SIZE = QSize(CARD_SIZE.width() + 2 * WINDOW_MARGIN, CARD_SIZE.height() + 2 * WINDOW_MARGIN)
CARD_RADIUS = 14.0
CLASSIC_SHADOW = 6  # ширина тени классической темы
COVER_MARGIN = 10
COVER_SIZE = 90
COVER_RADIUS = 10.0
TEXT_LEFT = 112  # от левого края карточки
TEXT_RIGHT_MARGIN = 14  # отступ текста от правого края карточки
BUTTON_SIZE = 26
BUTTON_GAP = 8

NO_MEDIA_TITLE = "Нет воспроизведения"
NO_MEDIA_ARTIST = "Запустите музыку или видео"

THEME_LABELS = {THEME_NEON: "Неон", THEME_CLASSIC: "Классика"}

# Классическая палитра.
TITLE_COLOR = QColor(255, 255, 255, 245)
ARTIST_COLOR = QColor(255, 255, 255, 165)

# Неоновая палитра (ночь Vice City: закатный розовый, оранжевый, фиолет, бирюза).
NEON_PINK = QColor(255, 46, 136)
NEON_ORANGE = QColor(255, 140, 66)
NEON_VIOLET = QColor(139, 92, 246)
NEON_CYAN = QColor(34, 211, 238)
NEON_NIGHT_TOP = QColor(28, 10, 52)
NEON_NIGHT_BOTTOM = QColor(9, 4, 22)
NEON_TITLE = QColor(255, 238, 248)
NEON_ARTIST = QColor(165, 243, 252)
#: Цвета «трубки» по кругу (конический градиент, 0..1 — полный оборот).
NEON_RING_STOPS = (
    (0.00, NEON_PINK),
    (0.25, NEON_ORANGE),
    (0.50, NEON_VIOLET),
    (0.75, NEON_CYAN),
    (1.00, NEON_PINK),
)
NEON_PAUSED_POWER = 0.6  # яркость неона на паузе
NEON_FLOW_DEG_PER_S = 40.0  # скорость «перетекания» цвета по рамке
NEON_FLOW_FRAME_MS = 33  # ~30 кадров/с: рамка медленная, 60 не нужно
NEON_PULSE_AMPLITUDE = 0.12  # «дыхание» трубки: яркость рамки 88–100 %
NEON_PULSE_HZ = 0.45
NEON_FLICKER_MS = 900
NEON_POWER_MS = 500

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
MARQUEE_STRIP_PAD = 6  # запас слева у полосы бегущей строки под свечение текста

BUTTON_NAMES = ("prev", "play_pause", "next")


def _elide(text: str, font: QFont, width: int) -> str:
    metrics = QFontMetrics(font)
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, width))


def _with_alpha(color: QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


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


def _glyph_path(glyph: str) -> QPainterPath:
    """Значок кнопки как контур с центром в (0, 0)."""
    s = 6.5
    path = QPainterPath()
    if glyph == "pause":
        path.addRoundedRect(QRectF(-5.0, -s, 3.4, 2 * s), 1.0, 1.0)
        path.addRoundedRect(QRectF(1.6, -s, 3.4, 2 * s), 1.0, 1.0)
    elif glyph == "play":
        path.moveTo(-4.5, -s)
        path.lineTo(5.5, 0.0)
        path.lineTo(-4.5, s)
        path.closeSubpath()
    elif glyph == "next":
        path.moveTo(-5.5, -s)
        path.lineTo(2.5, 0.0)
        path.lineTo(-5.5, s)
        path.closeSubpath()
        path.addRoundedRect(QRectF(3.6, -s, 2.4, 2 * s), 0.8, 0.8)
    else:  # prev
        path.moveTo(5.5, -s)
        path.lineTo(-2.5, 0.0)
        path.lineTo(5.5, s)
        path.closeSubpath()
        path.addRoundedRect(QRectF(-6.0, -s, 2.4, 2 * s), 0.8, 0.8)
    return path


def _stroke_glow(
    painter: QPainter, path: QPainterPath, color: QColor, strength: float, layers
) -> None:
    """Неоновое свечение: несколько обводок контура, шире — прозрачнее."""
    if strength <= 0.0:
        return
    for width, alpha in layers:
        pen = QPen(_with_alpha(color, alpha * strength), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(path, pen)


TEXT_GLOW = ((5.0, 0.12), (2.8, 0.22), (1.4, 0.40))
ICON_GLOW = ((6.0, 0.14), (3.2, 0.28), (1.4, 0.5))


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
    theme_selected = Signal(str)
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

        # Кэши отрисовки. Содержимое карточки (фон, обложка, текст, кнопки)
        # перерисовывается только при изменениях; кадры «перетекания» рамки
        # и бегущей строки собираются из готовых картинок.
        self._content_cache: Optional[QPixmap] = None
        self._ring_mask: Optional[QPixmap] = None
        self._ring_buffer: Optional[QPixmap] = None
        self._halo_cache: Optional[QPixmap] = None
        self._scanlines_cache: Optional[QPixmap] = None
        self._marquee_strip: Optional[QPixmap] = None
        self._marquee_strip_key: Optional[tuple] = None

        # --- анимации ---
        enabled = lambda: self._animations  # noqa: E731
        repaint = lambda _v: self._invalidate()  # noqa: E731
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

        # Неон: яркость (приглушается на паузе), мерцание при включении,
        # «перетекание» цвета по рамке и лёгкое «дыхание».
        self._neon = False
        self._neon_power = Tween(NEON_PAUSED_POWER, repaint, enabled=enabled, parent=self)
        self._neon_flicker = Tween(1.0, repaint, enabled=enabled, parent=self)
        self._flow_angle = 0.0
        self._flow_phase = 0.0
        self._flow_clock = QElapsedTimer()
        self._flow_timer = QTimer(self)
        self._flow_timer.setInterval(NEON_FLOW_FRAME_MS)
        self._flow_timer.timeout.connect(self._flow_tick)

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

        self._font_title = QFont()
        self._font_artist = QFont()
        self._apply_theme(settings.theme)

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

    @property
    def theme(self) -> str:
        return THEME_NEON if self._neon else THEME_CLASSIC

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
        if settings.theme != self.theme:
            self._apply_theme(settings.theme)
            if self._neon and self.isVisible():
                self._start_flicker()
        self._apply_visibility()
        self._invalidate()

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
        self._invalidate()

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
        self._invalidate()

    def set_playing(self, is_playing: bool) -> None:
        logger.debug("UI ← playback_state_changed: %s", is_playing)
        self._is_playing = bool(is_playing)
        self._play_morph.to(
            1.0 if self._is_playing else 0.0, PLAY_MORPH_MS, ease_in_out_cubic
        )
        self._cover_scale.to(
            1.0 if self._is_playing else COVER_SCALE_PAUSED, COVER_SCALE_MS, spring
        )
        self._neon_power.to(
            1.0 if self._is_playing else NEON_PAUSED_POWER, NEON_POWER_MS, ease_in_out_cubic
        )
        self._update_flow()
        self._invalidate()

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
        self._invalidate()

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
    # Тема
    # ------------------------------------------------------------------

    def _apply_theme(self, theme: str) -> None:
        self._neon = theme == THEME_NEON
        if self._neon:
            self._font_title = QFont("Segoe UI", 12, QFont.Weight.Bold)
            # Исполнитель — капсом с разрядкой, как подписи в ретро-постерах.
            self._font_artist = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            self._font_artist.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6)
        else:
            self._font_title = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
            self._font_artist = QFont("Segoe UI", 9)
        self._restart_marquee()
        self._update_flow()
        self._invalidate()

    def _start_flicker(self) -> None:
        """«Зажигание» неоновой трубки (при появлении окна и включении темы)."""
        if not (self._neon and self._animations):
            return
        self._neon_flicker.set(0.0)
        self._neon_flicker.to(1.0, NEON_FLICKER_MS, neon_flicker)

    def _invalidate(self, *_args) -> None:
        """Содержимое карточки изменилось — перерисовать его при следующем кадре."""
        self._content_cache = None
        self.update()

    def _neon_level(self) -> float:
        """Яркость неона содержимого (текст, иконки, обложка): пауза × мерцание."""
        return self._neon_power.value * self._neon_flicker.value

    def _ring_level(self) -> float:
        """Яркость рамки и ореола: ещё и медленное «дыхание» трубки."""
        pulse = 1.0
        if self._flow_timer.isActive():
            wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * NEON_PULSE_HZ * self._flow_phase)
            pulse -= NEON_PULSE_AMPLITUDE * wave
        return self._neon_level() * pulse

    def _update_flow(self) -> None:
        """Цвет «течёт» по рамке только когда это видно и музыка играет."""
        run = self._neon and self._animations and self._is_playing and self.isVisible()
        if run and not self._flow_timer.isActive():
            self._flow_clock.start()
            self._flow_timer.start()
        elif not run and self._flow_timer.isActive():
            self._flow_timer.stop()
            self.update()

    def _flow_tick(self) -> None:
        dt = min(0.1, self._flow_clock.restart() / 1000.0)
        self._flow_angle = (self._flow_angle - NEON_FLOW_DEG_PER_S * dt) % 360.0
        self._flow_phase += dt
        self.update()

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
            self._start_flicker()
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
        self._invalidate()

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
        self._background = blur_pixmap(pixmap, self._card_rect().size())

    # ------------------------------------------------------------------
    # Геометрия
    # ------------------------------------------------------------------

    def _card_rect(self) -> QRect:
        return QRect(
            WINDOW_MARGIN,
            WINDOW_MARGIN,
            self.width() - 2 * WINDOW_MARGIN,
            self.height() - 2 * WINDOW_MARGIN,
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

    def _new_layer(self) -> QPixmap:
        """Прозрачный слой размером с окно (с учётом масштаба экрана)."""
        dpr = max(1.0, self.devicePixelRatioF())
        layer = QPixmap(
            QSize(math.ceil(self.width() * dpr), math.ceil(self.height() * dpr))
        )
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.GlobalColor.transparent)
        return layer

    def _layer_fits(self, layer: Optional[QPixmap]) -> bool:
        if layer is None:
            return False
        dpr = max(1.0, self.devicePixelRatioF())
        return (
            layer.devicePixelRatio() == dpr
            and layer.width() == math.ceil(self.width() * dpr)
            and layer.height() == math.ceil(self.height() * dpr)
        )

    @staticmethod
    def _begin(layer: QPixmap) -> QPainter:
        p = QPainter(layer)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        return p

    def _paint(self, painter: QPainter) -> None:
        """Кадр собирается из слоёв: ореол → содержимое → бегущая строка → рамка.

        Содержимое карточки берётся из кэша, поэтому кадры «перетекания»
        рамки (30 fps) и бегущей строки (60 fps) почти ничего не стоят.
        """
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self._neon:
            painter.save()
            painter.setOpacity(min(1.0, self._ring_level()))
            painter.drawPixmap(0, 0, self._halo_layer())
            painter.restore()
        painter.drawPixmap(0, 0, self._content_layer())
        if self._title_overflows():
            self._draw_live_marquee(painter)
        if self._neon:
            self._draw_neon_ring(painter)

    def _content_layer(self) -> QPixmap:
        if self._content_cache is None or not self._layer_fits(self._content_cache):
            layer = self._new_layer()
            p = self._begin(layer)
            try:
                self._paint_content(p)
            finally:
                p.end()
            self._content_cache = layer
        return self._content_cache

    def _paint_content(self, painter: QPainter) -> None:
        card = self._card_rect()
        radius = CARD_RADIUS
        prev = self._prev
        t = self._xfade.value if prev is not None else 1.0
        level = self._neon_level() if self._neon else 0.0

        if not self._neon:
            self._paint_classic_shadow(painter, card)

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

            if self._neon:
                painter.drawPixmap(card.topLeft(), self._scanlines_layer(card.size()))
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(card, radius, radius)

            self._draw_cover(painter, prev, t, level)
            self._draw_texts(painter, prev, t, level)
            self._draw_buttons(painter, level)
        finally:
            painter.restore()

    # -- карточка ------------------------------------------------------------

    def _paint_classic_shadow(self, painter: QPainter, card: QRect) -> None:
        steps = CLASSIC_SHADOW
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(steps, 0, -1):
            alpha = int(90 * ((steps - i + 1) / steps) ** 2)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(card.adjusted(-i, -i, i, i), CARD_RADIUS + i, CARD_RADIUS + i)

    def _halo_layer(self) -> QPixmap:
        """Мягкий цветной ореол вокруг карточки — свет неона на «стене».

        Рисуется один раз на полной яркости; яркость кадра — прозрачностью.
        """
        if self._layer_fits(self._halo_cache):
            return self._halo_cache
        layer = self._new_layer()
        p = self._begin(layer)
        try:
            card = QRectF(self._card_rect())
            p.setPen(Qt.PenStyle.NoPen)
            steps = WINDOW_MARGIN
            for i in range(steps, 0, -1):
                k = (steps - i + 1) / steps
                color = NEON_VIOLET if i > steps // 2 else NEON_PINK
                p.setBrush(_with_alpha(color, 0.055 * k * k))
                p.drawRoundedRect(card.adjusted(-i, -i, i, i), CARD_RADIUS + i, CARD_RADIUS + i)
        finally:
            p.end()
        self._halo_cache = layer
        return layer

    def _ring_rect(self) -> QRectF:
        return QRectF(self._card_rect()).adjusted(0.5, 0.5, -0.5, -0.5)

    def _ring_mask_layer(self) -> QPixmap:
        """Форма трубки со свечением (белым): один раз; цвет — при каждом кадре."""
        if self._layer_fits(self._ring_mask):
            return self._ring_mask
        layer = self._new_layer()
        p = self._begin(layer)
        try:
            rect = self._ring_rect()
            p.setBrush(Qt.BrushStyle.NoBrush)
            for width, alpha in ((10.0, 0.08), (6.5, 0.16), (3.6, 0.34), (1.8, 1.0)):
                pen = QPen(_with_alpha(QColor(255, 255, 255), alpha), width)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.drawRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        finally:
            p.end()
        self._ring_mask = layer
        return layer

    def _draw_neon_ring(self, painter: QPainter) -> None:
        """Рамка-трубка: готовая маска окрашивается вращающимся градиентом."""
        mask = self._ring_mask_layer()
        if not self._layer_fits(self._ring_buffer):
            self._ring_buffer = self._new_layer()
        buffer = self._ring_buffer
        buffer.fill(Qt.GlobalColor.transparent)
        rect = self._ring_rect()
        p = QPainter(buffer)
        try:
            p.drawPixmap(0, 0, mask)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            gradient = QConicalGradient(rect.center(), self._flow_angle)
            for pos, color in NEON_RING_STOPS:
                gradient.setColorAt(pos, color)
            # Градиент нужен только там, где есть трубка: четыре полосы по
            # краям, а не всё окно (середина карточки — самая большая площадь).
            band = WINDOW_MARGIN + CARD_RADIUS
            w, h = float(self.width()), float(self.height())
            for strip in (
                QRectF(0.0, 0.0, w, band),
                QRectF(0.0, h - band, w, band),
                QRectF(0.0, band, band, h - 2 * band),
                QRectF(w - band, band, band, h - 2 * band),
            ):
                p.fillRect(strip, gradient)
        finally:
            p.end()

        level = min(1.0, self._ring_level())
        painter.save()
        painter.setOpacity(level)
        painter.drawPixmap(0, 0, buffer)
        # Раскалённая «нить» в центре трубки.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_with_alpha(QColor(255, 255, 255), 0.45 * level), 0.6))
        painter.drawRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        painter.restore()

    def _scanlines_layer(self, size: QSize) -> QPixmap:
        """Едва заметные строки развёртки — ретро-экран 80-х (кэш)."""
        dpr = max(1.0, self.devicePixelRatioF())
        cached = self._scanlines_cache
        if (
            cached is not None
            and cached.devicePixelRatio() == dpr
            and cached.width() == math.ceil(size.width() * dpr)
            and cached.height() == math.ceil(size.height() * dpr)
        ):
            return cached
        layer = QPixmap(QSize(math.ceil(size.width() * dpr), math.ceil(size.height() * dpr)))
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.GlobalColor.transparent)
        p = QPainter(layer)
        try:
            p.setPen(QPen(QColor(255, 255, 255, 7), 1))
            for y in range(1, size.height(), 3):
                p.drawLine(0, y, size.width(), y)
        finally:
            p.end()
        self._scanlines_cache = layer
        return layer

    def _draw_background(
        self, painter: QPainter, card: QRect, background: Optional[QPixmap], opacity: float
    ) -> None:
        if opacity <= 0.0:
            return
        painter.save()
        painter.setOpacity(opacity)
        has_bg = background is not None and not background.isNull()
        if self._neon:
            night = QLinearGradient(QPointF(card.topLeft()), QPointF(card.bottomLeft()))
            night.setColorAt(0.0, NEON_NIGHT_TOP)
            night.setColorAt(1.0, NEON_NIGHT_BOTTOM)
            painter.fillRect(card, night)
            if has_bg:
                # Обложка лишь подкрашивает ночь — иначе размытие даёт «пятна».
                painter.setOpacity(opacity * 0.3)
                painter.drawPixmap(card.topLeft(), background)
                painter.setOpacity(opacity)
            # Закатное зарево у «горизонта» (нижний край карточки).
            horizon = QLinearGradient(QPointF(card.topLeft()), QPointF(card.bottomLeft()))
            horizon.setColorAt(0.45, _with_alpha(NEON_PINK, 0.0))
            horizon.setColorAt(0.85, _with_alpha(NEON_PINK, 0.16))
            horizon.setColorAt(1.0, _with_alpha(NEON_ORANGE, 0.26))
            painter.fillRect(card, horizon)
        elif has_bg:
            painter.drawPixmap(card.topLeft(), background)
            painter.fillRect(card, QColor(0, 0, 0, 140))
        else:
            gradient = QLinearGradient(QPointF(card.topLeft()), QPointF(card.bottomRight()))
            gradient.setColorAt(0.0, QColor(32, 36, 44))
            gradient.setColorAt(1.0, QColor(18, 20, 25))
            painter.fillRect(card, gradient)
        painter.restore()

    # -- обложка -------------------------------------------------------------

    def _draw_cover(
        self, painter: QPainter, prev: Optional[_Frame], t: float, level: float
    ) -> None:
        rect = QRectF(self._cover_rect())
        scale = self._cover_scale.value
        painter.save()
        center = rect.center()
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)

        # Тень (классика) или розовое свечение (неон) под обложкой.
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(4, 0, -1):
            if self._neon:
                painter.setBrush(_with_alpha(NEON_PINK, (0.05 + (4 - i) * 0.035) * level))
                offset = 0.0
            else:
                painter.setBrush(QColor(0, 0, 0, 14 + (4 - i) * 6))
                offset = 2.0
            painter.drawRoundedRect(
                rect.adjusted(-i, -i + offset, i, i + offset), COVER_RADIUS + i, COVER_RADIUS + i
            )

        painter.save()
        path = QPainterPath()
        path.addRoundedRect(rect, COVER_RADIUS, COVER_RADIUS)
        painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
        if prev is not None and prev.cover is not self._cover_scaled:
            self._draw_cover_layer(painter, rect, prev.cover, 1.0, level)
            self._draw_cover_layer(painter, rect, self._cover_scaled, t, level)
        else:
            self._draw_cover_layer(painter, rect, self._cover_scaled, 1.0, level)
        painter.restore()

        if self._neon:
            # Неоновая окантовка обложки: закатный градиент розовый → оранжевый.
            frame = QPainterPath()
            frame.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), COVER_RADIUS, COVER_RADIUS)
            for width, alpha in ((5.0, 0.14), (2.6, 0.3), (1.2, 0.95)):
                gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
                gradient.setColorAt(0.0, _with_alpha(NEON_PINK, alpha * level))
                gradient.setColorAt(1.0, _with_alpha(NEON_ORANGE, alpha * level))
                painter.strokePath(frame, QPen(QBrush(gradient), width))
        painter.restore()

    def _draw_cover_layer(
        self,
        painter: QPainter,
        rect: QRectF,
        cover: Optional[QPixmap],
        opacity: float,
        level: float,
    ) -> None:
        if opacity <= 0.0:
            return
        painter.save()
        painter.setOpacity(opacity)
        if cover is not None and not cover.isNull():
            x = rect.left() + (rect.width() - cover.width()) / 2
            y = rect.top() + (rect.height() - cover.height()) / 2
            painter.drawPixmap(QPointF(x, y), cover)
        elif self._neon:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(46, 16, 78))
            gradient.setColorAt(1.0, QColor(16, 6, 34))
            painter.fillRect(rect, gradient)
            note = QPainterPath()
            note.addText(0.0, 0.0, QFont("Segoe UI", 28, QFont.Weight.Bold), "♪")
            bounds = note.boundingRect()
            note.translate(rect.center() - bounds.center())
            _stroke_glow(painter, note, NEON_PINK, max(0.35, level), ICON_GLOW)
            painter.fillPath(note, NEON_TITLE)
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

    def _draw_text_line(
        self,
        painter: QPainter,
        pos: QPointF,
        text: str,
        font: QFont,
        color: QColor,
        glow: QColor,
        glow_strength: float,
    ) -> None:
        """Строка текста; в неоне — контур с несколькими слоями свечения."""
        if not text:
            return
        if not self._neon:
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(pos, text)
            return
        path = QPainterPath()
        path.addText(pos, font, text)
        _stroke_glow(painter, path, glow, glow_strength, TEXT_GLOW)
        painter.fillPath(path, color)

    def _text_colors(self) -> tuple[QColor, QColor]:
        if self._neon:
            return NEON_TITLE, NEON_ARTIST
        return TITLE_COLOR, ARTIST_COLOR

    def _draw_texts(
        self, painter: QPainter, prev: Optional[_Frame], t: float, level: float
    ) -> None:
        current = (self._title, self._artist, self._has_media)
        if prev is not None and prev.text_key != current:
            # Старый текст уходит вверх и гаснет, новый въезжает снизу.
            self._draw_text_block(
                painter, *prev.text_key, opacity=1.0 - t, dy=-TEXT_SLIDE_PX * t,
                marquee=False, level=level,
            )
            self._draw_text_block(
                painter, *current, opacity=t, dy=TEXT_SLIDE_PX * (1.0 - t),
                marquee=True, level=level,
            )
        else:
            self._draw_text_block(
                painter, *current, opacity=1.0, dy=0.0, marquee=True, level=level
            )

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
        level: float,
    ) -> None:
        if opacity <= 0.0:
            return
        area = self._text_rect()
        metrics_title = QFontMetrics(self._font_title)
        metrics_artist = QFontMetrics(self._font_artist)
        if not has_media:
            title, artist = NO_MEDIA_TITLE, NO_MEDIA_ARTIST
        if self._neon:
            artist = artist.upper()
        title_color, artist_color = self._text_colors()

        painter.save()
        painter.setOpacity(opacity)
        painter.translate(0.0, dy)

        if marquee and self._title_overflows(title, has_media):
            pass  # бегущая строка рисуется поверх кэша (_draw_live_marquee)
        else:
            self._draw_text_line(
                painter,
                QPointF(area.left(), self._title_baseline()),
                _elide(title, self._font_title, area.width()),
                self._font_title,
                title_color,
                NEON_PINK,
                level,
            )

        artist_y = area.top() + metrics_title.height() + metrics_artist.ascent() + 8
        self._draw_text_line(
            painter,
            QPointF(area.left(), artist_y),
            _elide(artist, self._font_artist, area.width()),
            self._font_artist,
            artist_color,
            NEON_CYAN,
            0.7 * level,
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

    def _draw_live_marquee(self, painter: QPainter) -> None:
        """Бегущая строка поверх кэша содержимого (с учётом перехода трека)."""
        prev = self._prev
        opacity, dy = 1.0, 0.0
        if prev is not None and prev.text_key != (self._title, self._artist, self._has_media):
            t = self._xfade.value
            opacity, dy = t, TEXT_SLIDE_PX * (1.0 - t)
        if opacity <= 0.0:
            return
        level = self._neon_level() if self._neon else 0.0
        painter.save()
        painter.setOpacity(opacity)
        painter.translate(0.0, dy)
        self._draw_marquee(painter, self._title, level)
        painter.restore()

    def _marquee_strip_layer(self, title: str, level: float) -> tuple[QPixmap, float]:
        """Полоса «заголовок · пробел · заголовок» — рисуется один раз.

        Возвращает (полоса, отступ слева под свечение). Кадр прокрутки лишь
        сдвигает готовую картинку — без повторной отрисовки текста.
        """
        rect = self._title_rect()
        dpr = max(1.0, self.devicePixelRatioF())
        key = (title, self._neon, round(level, 2), self._font_title.key(), dpr, rect.height())
        if self._marquee_strip is not None and self._marquee_strip_key == key:
            return self._marquee_strip, MARQUEE_STRIP_PAD
        width = QFontMetrics(self._font_title).horizontalAdvance(title)
        period = width + MARQUEE_GAP
        strip_w = MARQUEE_STRIP_PAD * 2 + period + width
        layer = QPixmap(QSize(math.ceil(strip_w * dpr), math.ceil(rect.height() * dpr)))
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.GlobalColor.transparent)
        title_color, _ = self._text_colors()
        baseline = self._title_baseline() - rect.top()
        p = self._begin(layer)
        try:
            for x in (MARQUEE_STRIP_PAD, MARQUEE_STRIP_PAD + period):
                self._draw_text_line(
                    p, QPointF(x, baseline), title, self._font_title, title_color, NEON_PINK, level
                )
        finally:
            p.end()
        self._marquee_strip = layer
        self._marquee_strip_key = key
        return layer, MARQUEE_STRIP_PAD

    def _draw_marquee(self, painter: QPainter, title: str, level: float) -> None:
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
        strip, pad = self._marquee_strip_layer(title, level)

        p = QPainter(layer)
        try:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawPixmap(QPointF(-offset - pad, 0.0), strip)

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

    def _draw_buttons(self, painter: QPainter, level: float) -> None:
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

            accent = NEON_PINK if name == "play_pause" else NEON_CYAN
            if self._neon:
                self._draw_neon_button_ring(painter, QRectF(rect), name, alpha, hover, press, level)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, int((30 + 55 * hover) * alpha)))
                painter.drawEllipse(QRectF(rect))

            icon_alpha = (0.92 if self._has_media else 0.35) * alpha
            glow = (0.45 + 0.55 * hover + 0.5 * press) * level
            if name == "play_pause":
                m = self._play_morph.value
                self._draw_glyph(painter, "play", center, icon_alpha * (1.0 - m),
                                 0.55 + 0.45 * (1.0 - m), accent, glow)
                self._draw_glyph(painter, "pause", center, icon_alpha * m,
                                 0.55 + 0.45 * m, accent, glow)
            else:
                self._draw_glyph(painter, name, center, icon_alpha, 1.0, accent, glow)
            painter.restore()
        painter.restore()

    def _draw_neon_button_ring(
        self,
        painter: QPainter,
        rect: QRectF,
        name: str,
        alpha: float,
        hover: float,
        press: float,
        level: float,
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 6, 38, int(170 * alpha)))
        painter.drawEllipse(rect)

        ring = QPainterPath()
        ring.addEllipse(rect.adjusted(0.8, 0.8, -0.8, -0.8))
        if name == "play_pause":
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, NEON_PINK)
            gradient.setColorAt(1.0, NEON_ORANGE)
            core = QBrush(gradient)
            glow_color = NEON_PINK
        else:
            core = QBrush(NEON_CYAN)
            glow_color = NEON_CYAN
        intensity = min(1.0, 0.25 + 0.75 * hover + 0.6 * press)
        _stroke_glow(painter, ring, glow_color, intensity * alpha * level, ((6.0, 0.16), (3.0, 0.32)))
        painter.save()
        painter.setOpacity(painter.opacity() * (0.45 + 0.55 * intensity) * alpha)
        painter.strokePath(ring, QPen(core, 1.4))
        painter.restore()

    def _draw_glyph(
        self,
        painter: QPainter,
        glyph: str,
        center: QPointF,
        alpha: float,
        scale: float,
        glow_color: QColor,
        glow: float,
    ) -> None:
        if alpha <= 0.002:
            return
        path = _glyph_path(glyph)
        painter.save()
        painter.translate(center)
        painter.scale(scale, scale)
        if self._neon:
            _stroke_glow(painter, path, glow_color, glow * alpha, ICON_GLOW)
            painter.fillPath(path, _with_alpha(NEON_TITLE, alpha))
        else:
            painter.fillPath(path, _with_alpha(QColor(255, 255, 255), alpha))
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
        # Перерисовываем только полосу заголовка, а не всю карточку.
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

        theme_menu = menu.addMenu("Тема")
        theme_group = QActionGroup(theme_menu)
        theme_actions = {}
        for theme in THEMES:
            action = theme_menu.addAction(THEME_LABELS[theme])
            action.setCheckable(True)
            action.setChecked(theme == self.theme)
            theme_group.addAction(action)
            theme_actions[action] = theme

        menu.addSeparator()
        settings_action = menu.addAction("Настройки")
        close_action = menu.addAction("Закрыть")

        chosen = menu.exec(event.globalPos())
        if chosen is top_action:
            self.always_on_top_toggled.emit(top_action.isChecked())
        elif chosen is autostart_action:
            self.autostart_toggled.emit(autostart_action.isChecked())
        elif chosen in theme_actions:
            self.theme_selected.emit(theme_actions[chosen])
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
            self._background = blur_pixmap(self._cover, self._card_rect().size())
        self._restart_marquee()
        self._invalidate()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._restart_marquee()
        self._update_flow()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._marquee_frame.stop()
        self._marquee_pause.stop()
        self._marquee_offset = 0.0
        self._update_flow()

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
