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
    QPainterPathStroker,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import QMenu, QWidget

from audio_volume import MasterVolume
from settings import MIN_SCALE, Settings
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
WINDOW_SIZE = QSize(372, 122)  # карточка 360×110 (максимальный размер)
#: Мини-режим: карточка 134×44 — только prev / play / next и крестик.
MINI_SIZE = QSize(146, 56)
#: Мини-режим включается, когда окно тянут уже меньше этой доли максимума.
MINI_SWITCH_SCALE = 0.5
CARD_RADIUS = 14.0
COVER_MARGIN = 14
COVER_SIZE = 82
COVER_RADIUS = 10.0
TEXT_LEFT = 116  # от левого края карточки
TEXT_RIGHT_MARGIN = 28  # отступ текста от правого края (там крестик)

# Кольцо прогресса вокруг обложки.
RING_MARGIN = 8  # от края карточки до кольца
RING_SIZE = 94
RING_WIDTH = 2.5
RING_RADIUS = 16.0
RING_TRACK = QColor(255, 255, 255, 38)
RING_FILL = QColor(255, 255, 255, 225)
RING_TICK_MS = 200  # частота перерисовки кольца

# Крестик «закрыть» и громкость.
CLOSE_SIZE = 17
VOLUME_ICON_SIZE = 22
VOLUME_POPUP_W = 26
VOLUME_POPUP_H = 62
VOLUME_STEP = 0.05
GRIP_SIZE = 14  # угол для изменения размера (правый нижний)
BUTTON_SIZE = 26
BUTTON_GAP = 8
#: Второстепенные кнопки (перемешивание, повтор) — меньше и без подложки.
SECONDARY_SIZE = 22
SECONDARY_GAP = 6
SECONDARY_BUTTONS = ("shuffle", "repeat")
#: Яркость значков второстепенных кнопок: выкл / вкл / недоступно.
MODE_ICON_OFF = 0.5
MODE_ICON_DISABLED = 0.22
MODE_MS = 200

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

#: Сколько ждать, прежде чем признать «медиа нет» (пауза между треками), мс.
NO_MEDIA_GRACE_MS = 2500

BUTTON_NAMES = ("shuffle", "prev", "play_pause", "next", "repeat")
#: Кнопки, которые есть в мини-режиме.
MINI_BUTTONS = ("prev", "play_pause", "next")
#: Остальные «цели» мыши: крестик и значок громкости (не в _button_rects).
EXTRA_NAMES = ("close", "volume")
ALL_NAMES = BUTTON_NAMES + EXTRA_NAMES

REPEAT_LABELS = {"off": "выкл", "all": "все треки", "one": "один трек"}

#: Размеры из контекстного меню: (название, масштаб, мини-режим).
SIZE_PRESETS = (
    ("Максимальный", 1.0, False),
    ("Средний", 0.8, False),
    ("Маленький", MIN_SCALE, False),
    ("Мини (только кнопки)", 1.0, True),
)


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


# ---------------------------------------------------------------------------
# Значки кнопок в духе SF Symbols (macOS): тонкие, со скруглёнными углами
# ---------------------------------------------------------------------------

#: Половина высоты значков play/pause, px (значок ~11 px — как SF Symbol 13 pt).
GLYPH_HALF = 5.5
#: Радиус скругления углов треугольников, px.
GLYPH_CORNER = 1.4


def _rounded_polygon(points: list[tuple[float, float]], radius: float) -> QPainterPath:
    """Многоугольник со скруглёнными углами (каждый угол — квадратичная кривая)."""
    n = len(points)

    def toward(a: tuple[float, float], b: tuple[float, float]) -> QPointF:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1.0
        r = min(radius, length / 2.0)
        return QPointF(a[0] + dx / length * r, a[1] + dy / length * r)

    path = QPainterPath()
    for i, corner in enumerate(points):
        prev_pt = points[i - 1]
        next_pt = points[(i + 1) % n]
        start = toward(corner, prev_pt)
        end = toward(corner, next_pt)
        if i == 0:
            path.moveTo(start)
        else:
            path.lineTo(start)
        path.quadTo(QPointF(*corner), end)
    path.closeSubpath()
    return path


def _triangle_right(left: float, right: float, half: float) -> QPainterPath:
    return _rounded_polygon([(left, -half), (right, 0.0), (left, half)], GLYPH_CORNER)


def _triangle_left(left: float, right: float, half: float) -> QPainterPath:
    return _rounded_polygon([(right, -half), (left, 0.0), (right, half)], GLYPH_CORNER)


def glyph_path(glyph: str) -> QPainterPath:
    """Контур значка с центром в (0, 0).

    * play  — play.fill: скруглённый треугольник, сдвинут вправо для
      оптического центра (у треугольника «центр тяжести» левее середины);
    * pause — pause.fill: две узкие полоски с круглыми концами;
    * next/prev — forward.fill / backward.fill: два касающихся треугольника,
      как в «Сейчас играет» macOS.
    """
    h = GLYPH_HALF
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    if glyph == "play":
        path.addPath(_triangle_right(-3.8, 5.6, h))
    elif glyph == "pause":
        bar_w, gap = 2.6, 3.0
        for x in (-gap / 2 - bar_w, gap / 2):
            path.addRoundedRect(QRectF(x, -h, bar_w, 2 * h), bar_w / 2, bar_w / 2)
    elif glyph == "next":
        half = h * 0.78
        path.addPath(_triangle_right(-6.0, 0.4, half))
        path.addPath(_triangle_right(-0.4, 6.0, half))
    else:  # prev
        half = h * 0.78
        path.addPath(_triangle_left(-6.0, 0.4, half))
        path.addPath(_triangle_left(-0.4, 6.0, half))
    if glyph in LINE_GLYPHS:
        path = _line_glyph(glyph)
    if glyph != "play":
        # Скругление «съедает» остриё — выравниваем по фактическим границам.
        path.translate(-path.boundingRect().center().x(), 0.0)
    return path


#: Линейные значки (обводка, как SF Symbols shuffle / repeat / repeat.1).
LINE_GLYPHS = ("shuffle", "repeat", "repeat_one")
GLYPH_STROKE = 1.5


def _stroked(path: QPainterPath, width: float) -> QPainterPath:
    """Обводка → заливаемый контур (рисуется тем же fillPath, что и остальные)."""
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    outline = stroker.createStroke(path)
    outline.setFillRule(Qt.FillRule.WindingFill)
    return outline


def _line_glyph(glyph: str) -> QPainterPath:
    line = QPainterPath()
    if glyph == "shuffle":
        # Две пересекающиеся стрелки слева направо.
        for y in (-3.0, 3.0):
            line.moveTo(-6.0, y)
            line.lineTo(-3.2, y)
            line.cubicTo(-0.4, y, -0.4, -y, 2.4, -y)
            line.lineTo(5.4, -y)
            line.moveTo(3.4, -y - 2.0)
            line.lineTo(5.5, -y)
            line.lineTo(3.4, -y + 2.0)
    else:
        # Петля из двух стрелок: верхняя вправо, нижняя влево. Высота петли
        # с запасом — внутри должна поместиться «1» у repeat_one.
        y = 3.6
        line.moveTo(-5.6, 1.3)
        line.lineTo(-5.6, -y + 2.0)
        line.quadTo(-5.6, -y, -3.6, -y)
        line.lineTo(5.2, -y)
        line.moveTo(3.3, -y - 1.9)
        line.lineTo(5.3, -y)
        line.lineTo(3.3, -y + 1.9)
        line.moveTo(5.6, -1.3)
        line.lineTo(5.6, y - 2.0)
        line.quadTo(5.6, y, 3.6, y)
        line.lineTo(-5.2, y)
        line.moveTo(-3.3, y - 1.9)
        line.lineTo(-5.3, y)
        line.lineTo(-3.3, y + 1.9)
    path = _stroked(line, GLYPH_STROKE)
    if glyph == "repeat_one":
        # Маленькая «1» по центру петли (repeat.1).
        one = QPainterPath()
        one.moveTo(-0.8, -0.8)
        one.lineTo(0.3, -1.6)
        one.lineTo(0.3, 1.6)
        path = path.united(_stroked(one, 1.0))
        path.setFillRule(Qt.FillRule.WindingFill)
    return path


def volume_glyph(level: float, muted: bool) -> QPainterPath:
    """Динамик (speaker.wave) с центром в (0, 0): 0–2 «волны» по уровню."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    body = _rounded_polygon(
        [(-6.0, -2.2), (-3.2, -2.2), (0.8, -5.6), (0.8, 5.6), (-3.2, 2.2), (-6.0, 2.2)], 1.0
    )
    path.addPath(body)
    waves = QPainterPath()
    if muted or level <= 0.001:
        # Крестик вместо волн.
        waves.moveTo(3.4, -2.6)
        waves.lineTo(7.4, 2.6)
        waves.moveTo(7.4, -2.6)
        waves.lineTo(3.4, 2.6)
    else:
        count = 1 if level < 0.5 else 2
        for i in range(count):
            r = 3.4 + 3.2 * i
            waves.arcMoveTo(QRectF(1.2 - r, -r, 2 * r, 2 * r), 40.0)
            waves.arcTo(QRectF(1.2 - r, -r, 2 * r, 2 * r), 40.0, -80.0)
    path.addPath(_stroked(waves, 1.3))
    path.translate(-0.5, 0.0)
    return path


def close_glyph() -> QPainterPath:
    line = QPainterPath()
    d = 3.2
    line.moveTo(-d, -d)
    line.lineTo(d, d)
    line.moveTo(d, -d)
    line.lineTo(-d, d)
    return _stroked(line, 1.5)


def ring_path(rect: QRectF, radius: float) -> QPainterPath:
    """Контур скруглённого прямоугольника: старт сверху по центру, по часовой."""
    r = min(radius, rect.width() / 2, rect.height() / 2)
    cx = rect.center().x()
    path = QPainterPath()
    path.moveTo(cx, rect.top())
    path.lineTo(rect.right() - r, rect.top())
    path.arcTo(QRectF(rect.right() - 2 * r, rect.top(), 2 * r, 2 * r), 90.0, -90.0)
    path.lineTo(rect.right(), rect.bottom() - r)
    path.arcTo(QRectF(rect.right() - 2 * r, rect.bottom() - 2 * r, 2 * r, 2 * r), 0.0, -90.0)
    path.lineTo(rect.left() + r, rect.bottom())
    path.arcTo(QRectF(rect.left(), rect.bottom() - 2 * r, 2 * r, 2 * r), 270.0, -90.0)
    path.lineTo(rect.left(), rect.top() + r)
    path.arcTo(QRectF(rect.left(), rect.top(), 2 * r, 2 * r), 180.0, -90.0)
    path.lineTo(cx, rect.top())
    return path


def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


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
    #: (масштаб, мини-режим) — пользователь изменил размер окна
    scale_changed = Signal(float, bool)

    def __init__(
        self,
        settings: Settings,
        parent: Optional[QWidget] = None,
        *,
        animations: Optional[bool] = None,
        volume: Optional[MasterVolume] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._volume_backend = volume if volume is not None else MasterVolume()
        self._volume_level = 0.5
        self._volume_muted = False
        self._volume_dragging = False
        self._volume_wanted = False
        self._scale =min(1.0, max(MIN_SCALE, float(getattr(settings, "scale", 1.0))))
        self._mini = bool(getattr(settings, "mini", False))
        self._resizing = False
        self._resize_start_global = QPoint()
        self._resize_start_width = 0
        self._bg_size = QSize()
        # Позиция трека (для кольца прогресса): значение на момент _tl_clock.
        self._tl_position = 0.0
        self._tl_duration = 0.0
        self._tl_clock = QElapsedTimer()
        self._tl_clock.start()
        self._animations = (
            system_animations_enabled() if animations is None else bool(animations)
        )

        self.setWindowTitle("Now Playing")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.always_on_top)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self._apply_size()

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
        self._hover = {n: Tween(0.0, repaint, enabled=enabled, parent=self) for n in ALL_NAMES}
        self._press = {n: Tween(0.0, repaint, enabled=enabled, parent=self) for n in ALL_NAMES}
        # Всплывающий вертикальный ползунок громкости и «ручка» изменения размера.
        self._volume_open = Tween(0.0, repaint, enabled=enabled, parent=self)
        self._grip_hover = Tween(0.0, repaint, enabled=enabled, parent=self)
        self._grip_active = False
        # Отложенное «нет медиа» (см. set_no_media).
        self._no_media_timer = QTimer(self)
        self._no_media_timer.setSingleShot(True)
        self._no_media_timer.timeout.connect(self._apply_no_media)
        # Кольцо прогресса двигается по таймеру, пока трек играет.
        self._ring_timer = QTimer(self)
        self._ring_timer.setInterval(RING_TICK_MS)
        self._ring_timer.timeout.connect(self._ring_tick)
        # 0 — значок play, 1 — pause.
        self._play_morph = Tween(0.0, repaint, enabled=enabled, parent=self)
        # Повтор/перемешивание: что сообщает плеер и плавная индикация.
        self._repeat: Optional[str] = None
        self._shuffle: Optional[bool] = None
        self._repeat_supported = False
        self._shuffle_supported = False
        self._shuffle_on = Tween(0.0, repaint, enabled=enabled, parent=self)
        self._repeat_on = Tween(0.0, repaint, enabled=enabled, parent=self)
        self._repeat_one = Tween(0.0, repaint, enabled=enabled, parent=self)
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
        self.set_display_mode(
            getattr(settings, "scale", 1.0), getattr(settings, "mini", False), notify=False
        )
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
        self._no_media_timer.stop()  # провал между треками закончился
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
            # Новый трек — кольцо с нуля; настоящая позиция придёт из плеера.
            self.set_timeline(0.0, 0.0)
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
        # На паузе кольцо замирает там, где остановился трек.
        self._tl_position = self.progress_position()
        self._tl_clock.restart()
        self._is_playing = bool(is_playing)
        self._sync_ring_timer()
        self._play_morph.to(
            1.0 if self._is_playing else 0.0, PLAY_MORPH_MS, ease_in_out_cubic
        )
        self._cover_scale.to(
            1.0 if self._is_playing else COVER_SCALE_PAUSED, COVER_SCALE_MS, spring
        )
        self.update()

    def set_modes(self, modes) -> None:
        """Повтор/перемешивание (PlaybackModes из media_controller, duck typing)."""
        repeat = getattr(modes, "repeat", None)
        shuffle = getattr(modes, "shuffle", None)
        self._repeat = repeat if repeat in REPEAT_LABELS else None
        self._shuffle = None if shuffle is None else bool(shuffle)
        self._repeat_supported = bool(getattr(modes, "repeat_supported", False))
        self._shuffle_supported = bool(getattr(modes, "shuffle_supported", False))
        logger.debug(
            "UI ← modes_changed: repeat=%s shuffle=%s", self._repeat, self._shuffle
        )
        self._shuffle_on.to(1.0 if self._shuffle else 0.0, MODE_MS, ease_in_out_cubic)
        self._repeat_on.to(
            1.0 if self._repeat in ("all", "one") else 0.0, MODE_MS, ease_in_out_cubic
        )
        self._repeat_one.to(1.0 if self._repeat == "one" else 0.0, MODE_MS, ease_in_out_cubic)
        self._update_tooltip()
        self.update()

    # -- прогресс трека ------------------------------------------------------

    def set_timeline(self, position: float, duration: float) -> None:
        """Позиция и длительность трека в секундах (0 — длительность неизвестна)."""
        self._tl_duration = max(0.0, float(duration))
        self._tl_position = max(0.0, float(position))
        self._tl_clock.restart()
        self._sync_ring_timer()
        self.update(self._ring_update_rect())

    def progress_position(self) -> float:
        """Текущая позиция с учётом времени, прошедшего с последнего обновления."""
        position = self._tl_position
        if self._is_playing and self._tl_duration > 0:
            position += self._tl_clock.elapsed() / 1000.0
        if self._tl_duration > 0:
            position = min(position, self._tl_duration)
        return max(0.0, position)

    def progress_fraction(self) -> float:
        if self._tl_duration <= 0:
            return 0.0
        return min(1.0, self.progress_position() / self._tl_duration)

    def _sync_ring_timer(self) -> None:
        want = (
            self._is_playing
            and self._tl_duration > 0
            and self._has_media
            and not self._mini
            and self.isVisible()
        )
        if want and not self._ring_timer.isActive():
            self._ring_timer.start()
        elif not want and self._ring_timer.isActive():
            self._ring_timer.stop()

    def _ring_tick(self) -> None:
        if not (self.isVisible() and self._is_playing and self._tl_duration > 0):
            self._ring_timer.stop()
            return
        self.update(self._ring_update_rect())

    def _ring_update_rect(self) -> QRect:
        return self._to_device(self._ring_rect().adjusted(-4, -4, 4, 4).toAlignedRect())

    # -- размер окна ---------------------------------------------------------

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def mini(self) -> bool:
        return self._mini

    def _effective_scale(self) -> float:
        return 1.0 if self._mini else self._scale

    def _logical_size(self) -> QSize:
        return MINI_SIZE if self._mini else WINDOW_SIZE

    def _apply_size(self) -> None:
        s = self._effective_scale()
        size = self._logical_size()
        self.resize(round(size.width() * s), round(size.height() * s))

    def _to_device(self, rect: QRect) -> QRect:
        """Логический прямоугольник → координаты окна (с учётом масштаба)."""
        s = self._effective_scale()
        return QRect(
            math.floor(rect.x() * s) - 1,
            math.floor(rect.y() * s) - 1,
            math.ceil(rect.width() * s) + 3,
            math.ceil(rect.height() * s) + 3,
        )

    def _to_logical(self, point: QPointF | QPoint) -> QPoint:
        s = self._effective_scale()
        return QPoint(round(point.x() / s), round(point.y() / s))

    def set_display_mode(
        self, scale: float, mini: bool, *, notify: bool = True, clamp: bool = True
    ) -> None:
        """Размер: ``scale`` от MIN_SCALE до 1.0 (максимум) или мини-режим."""
        scale = min(1.0, max(MIN_SCALE, float(scale)))
        mini = bool(mini)
        if scale == self._scale and mini == self._mini:
            return
        self._scale = scale
        self._mini = mini
        self._apply_size()
        if clamp:
            self._clamp_to_screen()
        self._set_volume_open(False)
        self._sync_ring_timer()
        self._restart_marquee()
        self.update()
        if notify:
            self.scale_changed.emit(self._scale, self._mini)

    def _clamp_to_screen(self) -> None:
        """После смены размера не даём окну уехать за край экрана."""
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        nx = min(max(self.x(), area.left()), max(area.left(), area.right() - self.width() + 1))
        ny = min(max(self.y(), area.top()), max(area.top(), area.bottom() - self.height() + 1))
        if (nx, ny) != (self.x(), self.y()):
            self.move(nx, ny)

    def set_no_media(self) -> None:
        """Медиа нет: скрыться (по настройке) или показать заглушку.

        Между треками плеер (браузер) на ~1 с сообщает «сессии нет». Если
        пока играл трек, реакция откладывается на NO_MEDIA_GRACE_MS: придёт
        новый трек — виджет даже не мигнёт.
        """
        if self._has_media and self.isVisible():
            if not self._no_media_timer.isActive():
                self._no_media_timer.start(NO_MEDIA_GRACE_MS)
            return
        self._apply_no_media()

    def _apply_no_media(self) -> None:
        self._no_media_timer.stop()
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
        self.set_modes(None)  # нет плеера — нет и режимов
        self.set_timeline(0.0, 0.0)
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
        self._background = blur_pixmap(pixmap, self._logical_size())
        self._bg_size = self._logical_size()

    # ------------------------------------------------------------------
    # Геометрия (всё в логических координатах; при рисовании и обработке
    # мыши применяется масштаб окна)
    # ------------------------------------------------------------------

    def _card_rect(self) -> QRect:
        size = self._logical_size()
        return QRect(
            SHADOW_MARGIN,
            SHADOW_MARGIN,
            size.width() - 2 * SHADOW_MARGIN,
            size.height() - 2 * SHADOW_MARGIN,
        )

    def _ring_rect(self) -> QRectF:
        card = self._card_rect()
        return QRectF(
            card.left() + RING_MARGIN,
            card.top() + (card.height() - RING_SIZE) / 2.0,
            RING_SIZE,
            RING_SIZE,
        )

    def _close_rect(self) -> QRect:
        card = self._card_rect()
        if self._mini:
            return QRect(
                card.right() - 8 - CLOSE_SIZE,
                card.center().y() - CLOSE_SIZE // 2,
                CLOSE_SIZE,
                CLOSE_SIZE,
            )
        return QRect(card.right() - 8 - CLOSE_SIZE, card.top() + 7, CLOSE_SIZE, CLOSE_SIZE)

    def _volume_icon_rect(self) -> QRect:
        """Значок громкости — внизу, под текстом, по левому краю текста."""
        card = self._card_rect()
        center_y = card.bottom() - 12 - BUTTON_SIZE // 2
        return QRect(
            card.left() + TEXT_LEFT - 3,
            center_y - VOLUME_ICON_SIZE // 2,
            VOLUME_ICON_SIZE,
            VOLUME_ICON_SIZE,
        )

    def _volume_popup_rect(self) -> QRect:
        icon = self._volume_icon_rect()
        return QRect(
            icon.center().x() - VOLUME_POPUP_W // 2,
            icon.top() - 4 - VOLUME_POPUP_H,
            VOLUME_POPUP_W,
            VOLUME_POPUP_H,
        )

    def _volume_track_rect(self) -> QRectF:
        pop = self._volume_popup_rect()
        return QRectF(pop.center().x() - 1.5, pop.top() + 10, 3.0, pop.height() - 20)

    def _grip_rect(self) -> QRect:
        card = self._card_rect()
        return QRect(card.right() - GRIP_SIZE + 1, card.bottom() - GRIP_SIZE + 1, GRIP_SIZE, GRIP_SIZE)

    def _extra_rects(self) -> dict[str, QRect]:
        rects = {"close": self._close_rect()}
        if not self._mini:
            rects["volume"] = self._volume_icon_rect()
        return rects

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
        """Ряд кнопок справа внизу: ⤮  ⏪ ▶ ⏩  🔁 (как в Apple Music)."""
        card = self._card_rect()
        if self._mini:
            # Мини: три кнопки по центру-слева, справа место под крестик.
            top = card.center().y() - BUTTON_SIZE // 2
            x = card.left() + 12
            mini: dict[str, QRect] = {}
            for name in MINI_BUTTONS:
                mini[name] = QRect(x, top, BUTTON_SIZE, BUTTON_SIZE)
                x += BUTTON_SIZE + BUTTON_GAP
            return mini
        top = card.bottom() - 12 - BUTTON_SIZE
        center_y = top + BUTTON_SIZE // 2
        secondary_top = center_y - SECONDARY_SIZE // 2
        rects: dict[str, QRect] = {}
        x = card.right() - 10 - SECONDARY_SIZE
        rects["repeat"] = QRect(x, secondary_top, SECONDARY_SIZE, SECONDARY_SIZE)
        x -= SECONDARY_GAP
        for name in ("next", "play_pause", "prev"):
            x -= BUTTON_SIZE
            rects[name] = QRect(x, top, BUTTON_SIZE, BUTTON_SIZE)
            x -= BUTTON_GAP
        x += BUTTON_GAP - SECONDARY_GAP - SECONDARY_SIZE
        rects["shuffle"] = QRect(x, secondary_top, SECONDARY_SIZE, SECONDARY_SIZE)
        return {name: rects[name] for name in BUTTON_NAMES}

    def _button_enabled(self, name: str) -> bool:
        if name in EXTRA_NAMES:
            return True  # закрыть и громкость работают и без медиа
        if not self._has_media:
            return False
        if name == "shuffle":
            return self._shuffle_supported
        if name == "repeat":
            return self._repeat_supported
        return True

    def _button_tooltip(self, name: str) -> str:
        if name == "close":
            return "Закрыть виджет"
        if name == "volume":
            if not self._volume_backend.available:
                return "Громкость недоступна"
            if self._volume_muted:
                return "Включить звук"
            return f"Громкость: {round(self._volume_level * 100)}% (клик — без звука)"
        if name == "shuffle":
            if not self._shuffle_supported:
                return "Перемешивание — плеер не поддерживает"
            return "Перемешивание: " + ("вкл" if self._shuffle else "выкл")
        if name == "repeat":
            if not self._repeat_supported:
                return "Повтор — плеер не поддерживает"
            return "Повтор: " + REPEAT_LABELS.get(self._repeat or "off", "выкл")
        if name == "play_pause":
            return "Пауза" if self._is_playing else "Воспроизвести"
        return "Предыдущий трек" if name == "prev" else "Следующий трек"

    def _update_tooltip(self) -> None:
        name = self._hover_button
        show = name is not None and (self._has_media or name in EXTRA_NAMES)
        self.setToolTip(self._button_tooltip(name) if show else "")

    def _button_at(self, pos: QPoint) -> Optional[str]:
        """Цель под курсором; ``pos`` — в логических координатах."""
        for name, rect in self._button_rects().items():
            if rect.contains(pos):
                return name
        for name, rect in self._extra_rects().items():
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
        s = self._effective_scale()
        if s != 1.0:
            painter.scale(s, s)

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

            if not self._mini:
                self._draw_ring(painter)
                self._draw_cover(painter, prev, t)
                self._draw_texts(painter, prev, t)
            self._draw_buttons(painter)
            if not self._mini:
                self._draw_volume(painter)
            self._draw_close(painter)
            self._draw_grip(painter)
        finally:
            painter.restore()

    # -- кольцо прогресса ----------------------------------------------------

    def _draw_ring(self, painter: QPainter) -> None:
        """Прогресс трека: кольцо вокруг обложки, старт сверху, по часовой."""
        rect = self._ring_rect().adjusted(
            RING_WIDTH / 2, RING_WIDTH / 2, -RING_WIDTH / 2, -RING_WIDTH / 2
        )
        path = ring_path(rect, RING_RADIUS - RING_WIDTH / 2)
        total = path.length()
        if total <= 1.0:
            return
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        track = QPen(RING_TRACK, RING_WIDTH)
        track.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(track)
        painter.drawPath(path)

        fraction = self.progress_fraction() if self._has_media else 0.0
        length = total * fraction
        if length >= 0.5:
            pen = QPen(RING_FILL, RING_WIDTH)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            # Штрих длиной «прогресс» и пропуск до конца контура (в единицах ширины).
            pen.setDashPattern([length / RING_WIDTH, (total + 10.0) / RING_WIDTH])
            painter.setPen(pen)
            painter.drawPath(path)
            # «Головка» на конце дуги.
            head = path.pointAtPercent(path.percentAtLength(length))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 255))
            painter.drawEllipse(head, RING_WIDTH * 0.95, RING_WIDTH * 0.95)
        painter.restore()

    # -- крестик, громкость, ручка размера -----------------------------------

    def _draw_close(self, painter: QPainter) -> None:
        rect = QRectF(self._close_rect())
        hover = self._hover["close"].value
        press = self._press["close"].value
        # Всегда чуть виден (полупрозрачный), ярче при наведении на окно/значок.
        alpha = 0.32 + 0.28 * self._buttons.value + 0.40 * hover
        painter.save()
        center = rect.center()
        scale = 1.0 - BUTTON_PRESS_SCALE * press
        painter.translate(center)
        painter.scale(scale, scale)
        painter.setPen(Qt.PenStyle.NoPen)
        if hover > 0.01:
            painter.setBrush(QColor(255, 255, 255, int(60 * hover)))
            painter.drawEllipse(QPointF(0, 0), rect.width() / 2, rect.height() / 2)
        painter.fillPath(close_glyph(), QColor(255, 255, 255, int(255 * min(1.0, alpha))))
        painter.restore()

    def _volume_value(self) -> float:
        return 0.0 if self._volume_muted else self._volume_level

    def _draw_volume(self, painter: QPainter) -> None:
        alpha = self._buttons.value
        popup = self._volume_open.value
        if alpha <= 0.001 and popup <= 0.001:
            return
        icon = QRectF(self._volume_icon_rect())
        hover = max(self._hover["volume"].value, popup)
        brightness = 0.5 + 0.5 * hover
        painter.save()
        painter.translate(0.0, 3.0 * (1.0 - alpha))
        glyph_alpha = 255.0 * brightness * max(alpha, popup)
        if glyph_alpha > 0.5:
            painter.fillPath(
                volume_glyph(self._volume_level, self._volume_muted).translated(icon.center()),
                QColor(255, 255, 255, int(min(255.0, glyph_alpha))),
            )
        painter.restore()

        if popup <= 0.001:
            return
        # Вертикальный ползунок: «таблетка» над значком.
        pop = QRectF(self._volume_popup_rect())
        painter.save()
        painter.setOpacity(popup)
        painter.translate(0.0, 4.0 * (1.0 - popup))
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        painter.setBrush(QColor(18, 20, 26, 225))
        painter.drawRoundedRect(pop, pop.width() / 2, pop.width() / 2)

        track = self._volume_track_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 55))
        painter.drawRoundedRect(track, 1.5, 1.5)
        level = max(0.0, min(1.0, self._volume_value()))
        fill_h = track.height() * level
        fill = QRectF(track.left(), track.bottom() - fill_h, track.width(), fill_h)
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRoundedRect(fill, 1.5, 1.5)
        painter.drawEllipse(QPointF(track.center().x(), track.bottom() - fill_h), 4.5, 4.5)
        painter.restore()

    def _draw_grip(self, painter: QPainter) -> None:
        """Три косые чёрточки в правом нижнем углу при наведении — «тяни, чтобы изменить размер»."""
        value = self._grip_hover.value
        if value <= 0.01:
            return
        grip = self._grip_rect()
        painter.save()
        pen = QPen(QColor(255, 255, 255, int(150 * value)), 1.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        x1, y1 = grip.right() - 1.5, grip.bottom() - 1.5
        for d in (3.0, 6.5, 10.0):
            painter.drawLine(QPointF(x1, y1 - d), QPointF(x1 - d, y1))
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
        # Слой рисуется сразу в масштабе окна — текст остаётся чётким.
        dpr = max(1.0, self.devicePixelRatioF()) * self._effective_scale()
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
        # В мини-режиме кнопки — единственное содержимое и видны всегда.
        alpha = 1.0 if self._mini else self._buttons.value
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

            if name in SECONDARY_BUTTONS:
                self._draw_mode_button(painter, name, QRectF(rect), center, alpha, hover)
                painter.restore()
                continue

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

    def _draw_mode_button(
        self,
        painter: QPainter,
        name: str,
        rect: QRectF,
        center: QPointF,
        alpha: float,
        hover: float,
    ) -> None:
        """Перемешивание/повтор: без подложки, яркий значок и точка — «включено»."""
        enabled = self._button_enabled(name)
        on = (self._shuffle_on if name == "shuffle" else self._repeat_on).value
        if enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, int(55 * hover * alpha)))
            painter.drawEllipse(rect)
            brightness = MODE_ICON_OFF + (1.0 - MODE_ICON_OFF) * max(on, 0.35 * hover)
        else:
            brightness = MODE_ICON_DISABLED
            on = 0.0
        icon_alpha = 255.0 * brightness * alpha

        if name == "shuffle":
            self._draw_glyph(painter, "shuffle", center, icon_alpha, 1.0)
        else:
            one = self._repeat_one.value if enabled else 0.0
            self._draw_glyph(painter, "repeat", center, icon_alpha * (1.0 - one), 1.0)
            self._draw_glyph(painter, "repeat_one", center, icon_alpha * one, 1.0)

        if on > 0.01:
            # Точка под значком — режим включён (как в Apple Music).
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, int(235 * on * alpha)))
            radius = 1.4 * (0.6 + 0.4 * on)
            painter.drawEllipse(QPointF(center.x(), rect.bottom() - 1.5), radius, radius)

    def _draw_glyph(
        self, painter: QPainter, glyph: str, center: QPointF, alpha: float, scale: float
    ) -> None:
        if alpha <= 0.5:
            return
        color = QColor(255, 255, 255, int(min(255.0, alpha)))
        painter.save()
        painter.translate(center)
        painter.scale(scale, scale)
        painter.fillPath(glyph_path(glyph), color)
        painter.restore()

    # ------------------------------------------------------------------
    # Бегущая строка
    # ------------------------------------------------------------------

    def _restart_marquee(self) -> None:
        """С начала: строка стоит MARQUEE_PAUSE_MS, затем поехали."""
        self._marquee_frame.stop()
        self._marquee_pause.stop()
        self._marquee_offset = 0.0
        if self.isVisible() and not self._mini and self._title_overflows():
            self._marquee_pause.start(MARQUEE_PAUSE_MS)

    def _marquee_resume(self) -> None:
        if not (self.isVisible() and not self._mini and self._title_overflows()):
            return
        self._marquee_clock.start()
        self._marquee_frame.start()

    def _marquee_tick(self) -> None:
        if not (self.isVisible() and self._title_overflows()):
            self._marquee_frame.stop()
            self._marquee_offset = 0.0
            self.update(self._to_device(self._title_rect()))
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
        self.update(self._to_device(self._title_rect()))

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
        if not self._volume_dragging:
            self._set_volume_open(False)
        if not self._resizing:
            self._set_grip_hover(False)
        super().leaveEvent(event)

    # -- громкость -----------------------------------------------------------

    def _refresh_volume(self) -> None:
        level = self._volume_backend.get()
        if level is not None:
            self._volume_level = level
            self._volume_muted = self._volume_backend.muted

    def _set_volume_open(self, wanted: bool) -> None:
        if wanted == self._volume_wanted:
            return
        self._volume_wanted = wanted
        if wanted:
            self._refresh_volume()
            self._volume_open.to(1.0, HOVER_IN_MS, ease_out_cubic)
        else:
            self._volume_open.to(0.0, HOVER_OUT_MS, ease_in_out_cubic)

    def _volume_area(self) -> QRect:
        """Значок + ползунок + зазор между ними — пока курсор тут, ползунок открыт."""
        return self._volume_popup_rect().united(self._volume_icon_rect())

    def _update_volume_hover(self, lpos: QPoint) -> None:
        if self._mini or not self._volume_backend.available:
            self._set_volume_open(False)
            return
        if self._volume_wanted:
            inside = self._volume_area().adjusted(-4, -4, 4, 4).contains(lpos)
        else:
            inside = self._volume_icon_rect().contains(lpos)
        self._set_volume_open(inside)

    def _set_volume(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        self._volume_level = level
        self._volume_backend.set(level)
        if self._volume_muted and level > 0.0:
            self._volume_muted = False
            self._volume_backend.set_muted(False)
        self.update()

    def _volume_from_y(self, y: float) -> float:
        track = self._volume_track_rect()
        return (track.bottom() - y) / max(1.0, track.height())

    def _toggle_mute(self) -> None:
        self._refresh_volume()
        self._volume_muted = not self._volume_muted
        self._volume_backend.set_muted(self._volume_muted)
        self._update_tooltip()
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802
        lpos = self._to_logical(event.position())
        if (
            not self._mini
            and self._volume_backend.available
            and self._volume_area().adjusted(-4, -4, 4, 4).contains(lpos)
        ):
            self._refresh_volume()
            step = VOLUME_STEP if event.angleDelta().y() > 0 else -VOLUME_STEP
            self._set_volume(self._volume_level + step)
            self._set_volume_open(True)
            event.accept()
            return
        super().wheelEvent(event)

    # -- размер окна: тянем правый нижний угол -------------------------------

    def _set_grip_hover(self, on: bool) -> None:
        if on == self._grip_active:
            return
        self._grip_active = on
        self._grip_hover.to(1.0 if on else 0.0, HOVER_IN_MS if on else HOVER_OUT_MS, ease_out_cubic)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor if on else Qt.CursorShape.ArrowCursor)

    def _resize_to_width(self, width: float) -> None:
        """Ширина → масштаб; слишком узко — мини-режим (только три кнопки)."""
        eq = width / WINDOW_SIZE.width()
        if eq < MINI_SWITCH_SCALE:
            self.set_display_mode(self._scale, True, notify=False, clamp=False)
        else:
            self.set_display_mode(eq, False, notify=False, clamp=False)

    def _set_hover(self, name: Optional[str]) -> None:
        if name == self._hover_button:
            return
        if self._hover_button is not None:
            self._hover[self._hover_button].to(0.0, HOVER_OUT_MS, ease_in_out_cubic)
        if name is not None:
            self._hover[name].to(1.0, HOVER_IN_MS, ease_out_cubic)
        self._hover_button = name
        self._update_tooltip()

    def _release_press(self) -> None:
        if self._pressed_button is not None:
            self._press[self._pressed_button].to(0.0, RELEASE_MS, spring)
            self._pressed_button = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._resizing:
            dx = event.globalPosition().toPoint().x() - self._resize_start_global.x()
            self._resize_to_width(self._resize_start_width + dx)
            event.accept()
            return
        if self._volume_dragging:
            self._set_volume(self._volume_from_y(self._to_logical(event.position()).y()))
            event.accept()
            return
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

        pos = self._to_logical(event.position())
        self._set_hover(self._button_at(pos) if self._mouse_inside else None)
        self._update_volume_hover(pos)
        self._set_grip_hover(self._mouse_inside and self._grip_rect().contains(pos))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            pos = self._to_logical(event.position())
            if self._grip_rect().contains(pos):
                # Угол: тянем — меняется размер (а не двигается окно).
                self._resizing = True
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_width = self.width()
                event.accept()
                return
            if (
                not self._mini
                and self._volume_open.value > 0.5
                and self._volume_popup_rect().adjusted(-3, -3, 3, 3).contains(pos)
            ):
                self._volume_dragging = True
                self._set_volume(self._volume_from_y(pos.y()))
                event.accept()
                return
            self._dragging = True
            self._drag_moved = False
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = self.pos()
            name = self._button_at(pos)
            if name is not None and self._button_enabled(name):
                self._pressed_button = name
                self._press[name].to(1.0, PRESS_MS, ease_out_cubic)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._resizing:
            self._resizing = False
            self._clamp_to_screen()
            self.scale_changed.emit(self._scale, self._mini)
            self.save_position()
            self._set_grip_hover(self._grip_rect().contains(self._to_logical(event.position())))
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._volume_dragging:
            self._volume_dragging = False
            self._update_volume_hover(self._to_logical(event.position()))
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            pressed = self._pressed_button
            self._release_press()
            if self._drag_moved:
                self.save_position()
            elif pressed is not None and self._button_at(self._to_logical(event.position())) == pressed:
                self._on_button_clicked(pressed)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_button_clicked(self, name: str) -> None:
        if name == "close":
            self.quit_requested.emit()
            return
        if name == "volume":
            self._toggle_mute()
            return
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

        size_menu = menu.addMenu("Размер")
        size_actions = {}
        for label, scale, mini in SIZE_PRESETS:
            action = size_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(
                mini == self._mini and (mini or abs(scale - self._scale) < 0.02)
            )
            size_actions[action] = (scale, mini)

        menu.addSeparator()
        settings_action = menu.addAction("Настройки")
        close_action = menu.addAction("Закрыть")

        chosen = menu.exec(event.globalPos())
        if chosen in size_actions:
            scale, mini = size_actions[chosen]
            self.set_display_mode(scale, mini)
        elif chosen is top_action:
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
        # Фон считается в логических размерах: при смене масштаба (тот же
        # режим) пересчитывать его не нужно.
        if (
            self._cover is not None
            and not self._cover.isNull()
            and self._bg_size != self._logical_size()
        ):
            self._background = blur_pixmap(self._cover, self._logical_size())
            self._bg_size = self._logical_size()
        self._restart_marquee()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._restart_marquee()
        self._sync_ring_timer()
        self._refresh_volume()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._ring_timer.stop()
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
