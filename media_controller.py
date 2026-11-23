"""Работа с Windows System Media Transport Controls (SMTC) и MOCK-режим.

Архитектура:
  * ``MediaController`` (QObject) — публикует Qt-сигналы в GUI-поток;
  * ``_LoopThread`` (QThread) — отдельный поток с собственным asyncio-циклом,
    в котором выполняются ВСЕ WinRT-вызовы;
  * колбэки WinRT приходят из чужих потоков и пробрасываются в свой цикл через
    ``loop.call_soon_threadsafe``; команды из GUI — через
    ``asyncio.run_coroutine_threadsafe``;
  * импорты winrt ленивые (внутри async-методов), поэтому MOCK-режим и тесты
    работают без установленных winrt-пакетов.

ВАЖНО (причина найденного бага): в winrt-пакетах 3.x методы SMTC-сессии имеют
другие имена, чем в старом ``winsdk``:
  * ``try_get_media_properties_async()`` (await), НЕ ``try_get_media_properties()``;
  * ``try_toggle_play_pause_async()`` / ``try_skip_next_async()`` /
    ``try_skip_previous_async()``.
Вызов несуществующего имени бросал AttributeError, который ошибочно трактовался
как «сессия закрылась» и приводил к no_media() при живой сессии.
Второй уровень того же бага: async-методы возвращают ``_IAsyncOperation``,
который awaitable, НО не является coroutine — проверка ``asyncio.iscoroutine``
пропускала await, и наружу утекал объект операции без ``title``/``thumbnail``
(title_len=0). Используйте ``inspect.isawaitable`` (см. ``_await_if_needed``).

Среда: ``NOW_PLAYING_DEBUG=1`` — DEBUG-лог; ``NOW_PLAYING_MOCK=1`` — MOCK-режим.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import logging
import os
import random
import struct
import threading
import zlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

from PySide6.QtCore import QObject, QThread, Signal

from app_log import debug_enabled
from settings import default_config_dir

logger = logging.getLogger("npw.media")


def _sha1(data: Optional[bytes]) -> Optional[str]:
    """Хеш байтов обложки (для сравнения «та же картинка или новая»)."""
    if not data:
        return None
    return hashlib.sha1(data).hexdigest()

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

MOCK_ENV_VAR = "NOW_PLAYING_MOCK"

#: GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
PLAYBACK_STATUS_PLAYING = 4

#: Сколько попыток перечитать обложку после смены трека, если она пуста
#: или совпала с обложкой предыдущего трека (Chrome отдаёт запасную картинку).
COVER_RETRY_DELAYS = (0.3, 0.6, 1.0, 2.0, 3.0)  # сек, нарастающая пауза
#: Таймаут одного чтения обложки.
COVER_READ_TIMEOUT = 2.0  # сек
#: Дебаунс refresh: события пачкой в пределах этого окна → один refresh.
REFRESH_DEBOUNCE = 0.15  # сек

#: Период смены трека в MOCK-режиме, сек.
MOCK_INTERVAL = 6.0
#: Как часто перечитывать позицию трека у плеера, сек (между опросами
#: виджет сам двигает кольцо прогресса по своим часам).
TIMELINE_POLL = 2.0

_MOCK_OFF_VALUES = ("", "0", "false", "no", "off")

#: Имена методов winrt 3.x для команд управления.
COMMAND_METHODS = {
    "play_pause": "try_toggle_play_pause_async",
    "next": "try_skip_next_async",
    "previous": "try_skip_previous_async",
}

#: Методы получения свойств: сначала современный async-вариант winrt 3.x.
PROPS_METHODS = ("try_get_media_properties_async", "try_get_media_properties")

#: Режимы повтора в порядке переключения кнопкой (как в Apple Music):
#: выкл → весь список → один трек → выкл.
REPEAT_OFF = "off"
REPEAT_ALL = "all"
REPEAT_ONE = "one"
REPEAT_CYCLE = (REPEAT_OFF, REPEAT_ALL, REPEAT_ONE)
#: Windows.Media.MediaPlaybackAutoRepeatMode: NONE = 0, TRACK = 1, LIST = 2.
_REPEAT_TO_WINRT = {REPEAT_OFF: 0, REPEAT_ONE: 1, REPEAT_ALL: 2}
_REPEAT_FROM_WINRT = {v: k for k, v in _REPEAT_TO_WINRT.items()}

REPEAT_METHOD = "try_change_auto_repeat_mode_async"
SHUFFLE_METHOD = "try_change_shuffle_active_async"


# ---------------------------------------------------------------------------
# Чистые данные и функции разбора (без Qt и без winrt)
# ---------------------------------------------------------------------------


def is_mock_mode(env: Optional[Mapping[str, str]] = None) -> bool:
    """True, если включён MOCK-режим (NOW_PLAYING_MOCK=1)."""
    source = os.environ if env is None else env
    raw = str(source.get(MOCK_ENV_VAR, "")).strip().lower()
    return raw not in _MOCK_OFF_VALUES


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


@dataclass(frozen=True)
class TrackInfo:
    """Разобранные данные трека. Не зависит от Qt — удобно для pytest."""

    title: str = ""
    artist: str = ""
    cover_bytes: Optional[bytes] = None

    @property
    def key(self) -> tuple[str, str]:
        """Ключ дедупликации: ``(title, artist)``."""
        return (self.title, self.artist)

    @classmethod
    def from_mapping(cls, data: Any) -> "TrackInfo":
        """Строит TrackInfo из словаря (используется MOCK и в тестах).

        Обложка — bytes/bytearray/memoryview или base64-строка; некорректные
        значения отбрасываются в None.
        """
        if not isinstance(data, dict):
            return cls()
        title = _text(data.get("title"))
        artist = _text(data.get("artist"))
        cover: Any = data.get("cover_bytes", data.get("cover"))
        cover_bytes: Optional[bytes] = None
        if isinstance(cover, (bytes, bytearray, memoryview)):
            cover_bytes = bytes(cover) or None
        elif isinstance(cover, str) and cover:
            try:
                cover_bytes = base64.b64decode(cover, validate=True) or None
            except (binascii.Error, ValueError):
                cover_bytes = None
        return cls(title=title, artist=artist, cover_bytes=cover_bytes)


def parse_media_properties(props: Any) -> tuple[str, str]:
    """(title, artist) из WinRT MediaProperties — по duck typing, без импорта winrt."""
    if props is None:
        return "", ""
    title = _text(getattr(props, "title", None))
    artist = _text(getattr(props, "artist", None))
    return title, artist


def has_thumbnail(props: Any) -> bool:
    """Есть ли у properties непустой thumbnail (по duck typing)."""
    if props is None:
        return False
    try:
        return getattr(props, "thumbnail", None) is not None
    except Exception:
        logger.warning("чтение свойства thumbnail", exc_info=True)
        return False


def is_playing_status(status: Any) -> bool:
    """True, если playback_status == PLAYING (значение 4 в WinRT-перечислении)."""
    if status is None:
        return False
    try:
        return int(status) == PLAYBACK_STATUS_PLAYING
    except (TypeError, ValueError):
        return str(getattr(status, "name", "")) == "PLAYING"


def session_playback_status(session: Any) -> Any:
    """playback_status сессии или None, если прочитать не удалось."""
    if session is None:
        return None
    try:
        playback_info = session.get_playback_info()
    except Exception:
        logger.warning("get_playback_info не удался", exc_info=True)
        return None
    return getattr(playback_info, "playback_status", None)


@dataclass(frozen=True)
class PlaybackModes:
    """Повтор и перемешивание текущей сессии.

    ``repeat``/``shuffle`` — None, если плеер их не сообщает;
    ``*_supported`` — можно ли переключать режим (решает сам плеер).
    """

    repeat: Optional[str] = None
    shuffle: Optional[bool] = None
    repeat_supported: bool = False
    shuffle_supported: bool = False


def _unbox(value: Any) -> Any:
    """IReference<T> может прийти как значение или как обёртка с ``.value``."""
    if value is None:
        return None
    if isinstance(value, (bool, int)):
        return value
    inner = getattr(value, "value", value)
    return inner


def repeat_mode_from_winrt(value: Any) -> Optional[str]:
    """MediaPlaybackAutoRepeatMode (enum/int/None) → "off" | "one" | "all" | None."""
    value = _unbox(value)
    if value is None:
        return None
    try:
        return _REPEAT_FROM_WINRT.get(int(value))
    except (TypeError, ValueError):
        name = str(getattr(value, "name", "")).upper()
        return {"NONE": REPEAT_OFF, "TRACK": REPEAT_ONE, "LIST": REPEAT_ALL}.get(name)


def repeat_mode_to_winrt(mode: str) -> Any:
    """"off" | "one" | "all" → значение MediaPlaybackAutoRepeatMode.

    Перечисление берётся из winrt-Windows.Media (ленивый импорт); без пакета
    — целое число (IntEnum совместим с int).
    """
    index = _REPEAT_TO_WINRT[mode]
    try:
        from winrt.windows.media import MediaPlaybackAutoRepeatMode  # noqa: PLC0415

        return MediaPlaybackAutoRepeatMode(index)
    except Exception:  # noqa: BLE001 — нет пакета/другая версия: передаём int
        return index


def next_repeat_mode(current: Optional[str]) -> str:
    """Следующий режим по кругу выкл → все → один → выкл."""
    if current not in REPEAT_CYCLE:
        return REPEAT_ALL
    return REPEAT_CYCLE[(REPEAT_CYCLE.index(current) + 1) % len(REPEAT_CYCLE)]


def parse_playback_modes(playback_info: Any) -> PlaybackModes:
    """PlaybackModes из GlobalSystemMediaTransportControlsSessionPlaybackInfo."""
    if playback_info is None:
        return PlaybackModes()
    try:
        repeat = repeat_mode_from_winrt(getattr(playback_info, "auto_repeat_mode", None))
        shuffle_raw = _unbox(getattr(playback_info, "is_shuffle_active", None))
        shuffle = None if shuffle_raw is None else bool(shuffle_raw)
        controls = getattr(playback_info, "controls", None)
    except Exception:
        logger.warning("режимы повтора/перемешивания не прочитаны", exc_info=True)
        return PlaybackModes()
    if controls is not None:
        repeat_supported = bool(getattr(controls, "is_repeat_enabled", False))
        shuffle_supported = bool(getattr(controls, "is_shuffle_enabled", False))
    else:
        repeat_supported = repeat is not None
        shuffle_supported = shuffle is not None
    return PlaybackModes(
        repeat=repeat,
        shuffle=shuffle,
        repeat_supported=repeat_supported,
        shuffle_supported=shuffle_supported,
    )


async def set_repeat_mode(session: Any, mode: str) -> bool:
    """Просит плеер сменить режим повтора. True — плеер согласился."""
    method = getattr(session, REPEAT_METHOD, None)
    if method is None:
        raise AttributeError(f"у сессии нет метода {REPEAT_METHOD}")
    return bool(await _await_if_needed(method(repeat_mode_to_winrt(mode))))


async def set_shuffle(session: Any, active: bool) -> bool:
    """Просит плеер включить/выключить перемешивание. True — плеер согласился."""
    method = getattr(session, SHUFFLE_METHOD, None)
    if method is None:
        raise AttributeError(f"у сессии нет метода {SHUFFLE_METHOD}")
    return bool(await _await_if_needed(method(bool(active))))


def _seconds(value: Any) -> float:
    """timedelta / WinRT TimeSpan → секунды (0.0, если прочитать нельзя)."""
    if value is None:
        return 0.0
    try:
        return float(value.total_seconds())
    except AttributeError:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def parse_timeline(
    props: Any, playing: bool, now: Optional[datetime] = None
) -> tuple[float, float]:
    """(позиция, длительность) в секундах из TimelineProperties.

    Позиция в SMTC — на момент ``last_updated_time``; пока трек играет,
    добавляем прошедшее с тех пор время. Длительность 0.0 — неизвестна
    (стримы, плеер не сообщает).
    """
    if props is None:
        return 0.0, 0.0
    try:
        start = _seconds(getattr(props, "start_time", None))
        end = _seconds(getattr(props, "end_time", None))
        position = _seconds(getattr(props, "position", None)) - start
        duration = end - start
        updated = getattr(props, "last_updated_time", None)
    except Exception:
        logger.warning("тайминг трека не прочитан", exc_info=True)
        return 0.0, 0.0
    if duration <= 0.5:
        return 0.0, 0.0
    if playing and isinstance(updated, datetime) and updated.year > 2000:
        current = now or datetime.now(timezone.utc)
        if updated.tzinfo is None:
            current = current.replace(tzinfo=None)
        elapsed = (current - updated).total_seconds()
        if 0.0 <= elapsed < 6 * 3600:
            position += elapsed
    return max(0.0, min(duration, position)), duration


def choose_session(current: Any, sessions: Iterable[Any]) -> Any:
    """Выбор сессии для отображения.

    Правило: если текущая сессия PLAYING — оставляем её; иначе первая PLAYING
    из списка; иначе текущая (или None).
    """
    if current is not None and is_playing_status(session_playback_status(current)):
        return current
    for session in sessions or ():
        if session is None:
            continue
        if session is current:
            continue
        if is_playing_status(session_playback_status(session)):
            logger.info("текущая сессия не PLAYING — выбрана другая PLAYING-сессия")
            return session
    return current


async def _await_if_needed(result: Any) -> Any:
    """Await для любых awaitable, включая winrt ``_IAsyncOperation``.

    ВАЖНО: ``asyncio.iscoroutine()`` НЕ распознаёт winrt-операции (они имеют
    только ``__await__``) — из-за этого наружу утекал сам объект операции,
    у которого нет ``title``/``thumbnail`` (пустой трек при живой сессии).
    """
    if inspect.isawaitable(result):
        return await result
    return result


async def get_media_properties(session: Any) -> Any:
    """properties сессии: поддерживает winrt 3.x (async) и старый sync-API."""
    last_error: Optional[Exception] = None
    for name in PROPS_METHODS:
        method = getattr(session, name, None)
        if method is None:
            continue
        try:
            return await _await_if_needed(method())
        except Exception as exc:
            last_error = exc
            logger.warning("%s завершился ошибкой", name, exc_info=True)
    if last_error is not None:
        raise last_error
    raise AttributeError(
        "у сессии нет ни одного метода свойств из " + ", ".join(PROPS_METHODS)
    )


async def invoke_command(session: Any, action: str) -> None:
    """Выполняет команду управления по правильному имени метода winrt 3.x."""
    name = COMMAND_METHODS.get(action)
    if name is None:
        raise ValueError(f"неизвестная команда: {action}")
    method = getattr(session, name, None)
    if method is None:
        raise AttributeError(f"у сессии нет метода {name}")
    await _await_if_needed(method())


def make_solid_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    """Валидный PNG заданного размера одного цвета (без внешних библиотек)."""
    size = max(1, int(size))
    r, g, b = (max(0, min(255, int(c))) for c in rgb)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8 бит, RGB
    row = b"\x00" + bytes((r, g, b)) * size  # фильтр 0 + пиксели
    idat = zlib.compress(row * size, 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# MOCK-сессия (чистый Python, без Qt/winrt)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockTrack:
    title: str
    artist: str
    color: tuple[int, int, int]
    cover_size: int = 96


MOCK_TRACKS: tuple[MockTrack, ...] = (
    MockTrack("Midnight Drive", "Neon Waves", (45, 90, 190)),
    MockTrack("Северный ветер", "Города Дождя", (180, 60, 60)),
    MockTrack("Solar Flare", "Analog Dreams", (220, 140, 30)),
    MockTrack("Тихая гавань", "Море Внутри", (30, 140, 110)),
)


class MockSession:
    """Имитация SMTC-сессии: ротация треков, play/pause, готовые обложки."""

    def __init__(
        self,
        tracks: Optional[Iterable[MockTrack]] = None,
        index: int = 0,
        playing: bool = True,
    ) -> None:
        self._tracks = tuple(tracks) if tracks else MOCK_TRACKS
        if not self._tracks:
            raise ValueError("MockSession: пустой список треков")
        self._index = int(index) % len(self._tracks)
        self._playing = bool(playing)
        self._covers: dict[int, bytes] = {}
        self._repeat = REPEAT_OFF
        self._shuffle = False
        self._rng = random.Random(0)  # детерминированно — удобно в тестах
        # Позиция трека: накопленное + время с последнего запуска.
        self._pos_base = 0.0
        self._pos_since: Optional[float] = time.monotonic() if self._playing else None

    @property
    def duration(self) -> float:
        """Длительность mock-трека = период смены, чтобы кольцо «замыкалось»."""
        return MOCK_INTERVAL

    @property
    def position(self) -> float:
        extra = 0.0 if self._pos_since is None else time.monotonic() - self._pos_since
        return min(self.duration, self._pos_base + extra)

    def _restart_position(self) -> None:
        self._pos_base = 0.0
        self._pos_since = time.monotonic() if self._playing else None

    @property
    def index(self) -> int:
        return self._index

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def modes(self) -> PlaybackModes:
        return PlaybackModes(
            repeat=self._repeat,
            shuffle=self._shuffle,
            repeat_supported=True,
            shuffle_supported=True,
        )

    def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_CYCLE:
            raise ValueError(f"неизвестный режим повтора: {mode}")
        self._repeat = mode

    def set_shuffle(self, active: bool) -> None:
        self._shuffle = bool(active)

    def auto_advance(self) -> None:
        """Трек «доиграл»: повтор одного — тот же; без повтора в конце списка — стоп."""
        if self._repeat == REPEAT_ONE:
            self._restart_position()
            return
        last = self._index == len(self._tracks) - 1
        if self._repeat == REPEAT_OFF and last and not self._shuffle:
            self._playing = False
            self._pos_base = self.duration
            self._pos_since = None
            return
        self.next()

    @property
    def track(self) -> TrackInfo:
        mock = self._tracks[self._index]
        return TrackInfo(
            title=mock.title,
            artist=mock.artist,
            cover_bytes=self._cover(self._index),
        )

    def _cover(self, index: int) -> bytes:
        cached = self._covers.get(index)
        if cached is None:
            mock = self._tracks[index]
            cached = make_solid_png(mock.cover_size, mock.color)
            self._covers[index] = cached
        return cached

    def toggle(self) -> bool:
        now = time.monotonic()
        if self._playing and self._pos_since is not None:
            self._pos_base = min(self.duration, self._pos_base + now - self._pos_since)
            self._pos_since = None
        elif not self._playing:
            self._pos_since = now
        self._playing = not self._playing
        return self._playing

    def next(self) -> None:
        self._restart_position()
        if self._shuffle and len(self._tracks) > 1:
            choices = [i for i in range(len(self._tracks)) if i != self._index]
            self._index = self._rng.choice(choices)
            return
        self._index = (self._index + 1) % len(self._tracks)

    def previous(self) -> None:
        self._restart_position()
        self._index = (self._index - 1) % len(self._tracks)


# ---------------------------------------------------------------------------
# Поток с asyncio-циклом
# ---------------------------------------------------------------------------


class _LoopThread(QThread):
    """Отдельный поток: создаёт asyncio-цикл и крутит его до остановки."""

    def __init__(self, controller: "MediaController") -> None:
        super().__init__(controller)
        self._controller = controller
        self.ready = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def run(self) -> None:  # noqa: D102
        logger.info("asyncio-цикл: запуск потока")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._controller._loop = loop
        self.ready.set()
        try:
            loop.run_forever()
        except Exception:
            logger.exception("asyncio-цикл упал")
            raise
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.exception("ошибка при остановке asyncio-цикла")
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                logger.info("asyncio-цикл: поток остановлен")


# ---------------------------------------------------------------------------
# Контроллер
# ---------------------------------------------------------------------------


class MediaController(QObject):
    """Мост между SMTC (или MOCK) и UI. Все сигналы — только в GUI-поток."""

    #: (title, artist, cover_bytes | None)
    track_changed = Signal(str, str, object)
    #: (cover_bytes | None) — обложка обновилась БЕЗ смены трека
    #: (Chrome сначала отдаёт запасную картинку, настоящую — позже)
    cover_changed = Signal(object)
    #: (is_playing,)
    playback_state_changed = Signal(bool)
    #: медиа отсутствует (нет сессии / сессия закрыта / winrt недоступен)
    no_media = Signal()
    #: (PlaybackModes,) — повтор/перемешивание и их поддержка плеером
    modes_changed = Signal(object)
    #: (позиция, длительность) в секундах; длительность 0 — неизвестна
    timeline_changed = Signal(float, float)

    def __init__(
        self,
        *,
        mock: Optional[bool] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._mock = is_mock_mode() if mock is None else mock
        self._thread: Optional[_LoopThread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Сильные ссылки на WinRT-объекты, обработчики и токены подписок.
        self._manager: Any = None
        self._current_handler: Any = None
        self._current_token: Any = None
        self._session: Any = None
        self._mp_handler: Any = None
        self._mp_token: Any = None
        self._pi_handler: Any = None
        self._pi_token: Any = None

        self._mock_session: Optional[MockSession] = MockSession() if self._mock else None
        self._mock_task: Optional[asyncio.Task] = None

        # Сериализация refresh: счётчик поколений + отмена предыдущей задачи.
        self._refresh_seq = 0
        self._refresh_task: Optional[asyncio.Task] = None
        self._debounce_task: Optional[asyncio.Task] = None
        self._pending_cover = False
        self._pending_force = False

        # Фоновые повторные попытки чтения обложки после смены трека.
        self._cover_retry_task: Optional[asyncio.Task] = None
        # Периодический опрос позиции трека.
        self._timeline_task: Optional[asyncio.Task] = None

        self._last_key: Optional[tuple[str, str]] = None
        self._last_cover: Optional[bytes] = None
        self._last_cover_sha1: Optional[str] = None
        self._last_playing: Optional[bool] = None
        self._last_modes: Optional[PlaybackModes] = None

    # -- свойства для отладки ------------------------------------------------

    @property
    def mock(self) -> bool:
        return self._mock

    # -- жизненный цикл ------------------------------------------------------

    def start(self) -> None:
        """Запускает поток с циклом и инициализирует бэкенд (SMTC или MOCK)."""
        if self._thread is not None:
            return
        logger.info("старт MediaController (mock=%s)", self._mock)
        thread = _LoopThread(self)
        self._thread = thread
        thread.start()
        if not thread.ready.wait(timeout=5.0):
            logger.error("asyncio-цикл не поднялся за 5 секунд")
            self._thread = None
            self._loop = None
            return
        self._submit(self._run_backend())

    def shutdown(self) -> None:
        """Корректная остановка: снять подписки, остановить цикл, ждать поток."""
        thread = self._thread
        if thread is None:
            return
        logger.info("остановка MediaController")
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._cleanup(), loop)
                future.result(timeout=5.0)
            except Exception:
                logger.exception("ошибка при очистке перед остановкой")
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                logger.debug("цикл уже остановлен к моменту loop.stop()")
        if not thread.wait(10_000):
            logger.warning("поток не остановился за 10 с — terminate")
            thread.terminate()
            thread.wait(2_000)
        self._thread = None
        self._loop = None
        self._manager = None
        self._session = None
        self._mock_task = None
        self._refresh_task = None
        self._debounce_task = None
        self._cover_retry_task = None
        self._pending_cover = False
        self._pending_force = False
        logger.info("MediaController остановлен")

    # -- команды из GUI ------------------------------------------------------

    def play_pause(self) -> None:
        logger.info("команда: play_pause")
        self._submit(self._command("play_pause"))

    def next(self) -> None:
        logger.info("команда: next")
        self._submit(self._command("next"))

    def previous(self) -> None:
        logger.info("команда: previous")
        self._submit(self._command("previous"))

    def cycle_repeat(self) -> None:
        """Повтор по кругу: выкл → весь список → один трек → выкл."""
        logger.info("команда: repeat")
        self._submit(self._command("repeat"))

    def toggle_shuffle(self) -> None:
        logger.info("команда: shuffle")
        self._submit(self._command("shuffle"))

    # -- внутреннее: поток/цикл ---------------------------------------------

    def _submit(self, coro: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("цикл не готов — корутина не выполнена")
            try:
                coro.close()
            except AttributeError:
                pass
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            logger.warning("не удалось поставить корутину в цикл", exc_info=True)
            try:
                coro.close()
            except AttributeError:
                pass

    def _call_in_loop(self, fn: Callable[[], None]) -> None:
        """Пробросить колбэк из чужого потока в свой цикл."""
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("цикл закрыт — колбэк пропущен")
            return
        try:
            loop.call_soon_threadsafe(fn)
        except RuntimeError:
            logger.warning("call_soon_threadsafe не прошёл", exc_info=True)

    # -- бэкенд --------------------------------------------------------------

    async def _run_backend(self) -> None:
        if self._mock:
            self._mock_task = asyncio.ensure_future(self._mock_loop())
        else:
            self._timeline_task = asyncio.ensure_future(self._timeline_loop())
            await self._init_smtc()

    def _emit_timeline(self) -> None:
        """Читает позицию/длительность у текущей сессии и отдаёт в UI."""
        session = self._session
        if session is None:
            return
        try:
            props = session.get_timeline_properties()
            playing = bool(self._last_playing)
        except Exception:
            logger.debug("get_timeline_properties не удался", exc_info=True)
            return
        position, duration = parse_timeline(props, playing)
        self.timeline_changed.emit(position, duration)

    async def _timeline_loop(self) -> None:
        while True:
            await asyncio.sleep(TIMELINE_POLL)
            if self._session is not None and self._last_playing:
                self._emit_timeline()

    async def _init_smtc(self) -> None:
        try:
            from winrt.windows.media.control import (  # noqa: PLC0415
                GlobalSystemMediaTransportControlsSessionManager as SessionManager,
            )
        except Exception:
            logger.exception("импорт winrt не удался — SMTC недоступен")
            self.no_media.emit()
            return
        try:
            logger.info("запрос SMTC-менеджера (request_async)")
            manager = await SessionManager.request_async()
        except Exception:
            logger.exception("не удалось получить SMTC-менеджер")
            self.no_media.emit()
            return
        logger.info("SMTC-менеджер получен: %s", type(manager).__name__)
        # Подписка на менеджер — строго однократная: если вдруг вызван повторно,
        # сначала снимаем предыдущую по сохранённому токену (до присваивания).
        self._detach_manager()
        self._manager = manager
        try:
            self._current_handler = self._on_current_session_changed
            self._current_token = manager.add_current_session_changed(
                self._current_handler
            )
            logger.info("подписка на current_session_changed выполнена")
        except Exception:
            logger.exception("подписка на current_session_changed не удалась")
            self.no_media.emit()
            return

        # Сессия могла существовать ДО запуска — читаем её сразу, не дожидаясь событий.
        try:
            sessions = manager.get_sessions()
            count = len(sessions)
        except Exception:
            logger.exception("get_sessions не удался")
            sessions, count = [], 0
        try:
            current = manager.get_current_session()
        except Exception:
            logger.exception("get_current_session не удался")
            current = None
        logger.info(
            "сессий при старте: %d, current=%s",
            count,
            _auid(current),
        )
        chosen = choose_session(current, sessions)
        if chosen is not None and chosen is not current:
            logger.info("выбрана PLAYING-сессия вместо current: %s", _auid(chosen))
        self._set_session(chosen)

    # -- колбэки WinRT (чужие потоки!) ---------------------------------------

    def _on_current_session_changed(self, sender: Any, args: Any) -> None:
        logger.info("событие: current_session_changed")

        def apply() -> None:
            try:
                sessions = sender.get_sessions()
                current = sender.get_current_session()
            except Exception:
                logger.exception("не удалось прочитать сессии в current_session_changed")
                return
            logger.info(
                "сессий: %d, current=%s", len(sessions), _auid(current)
            )
            self._set_session(choose_session(current, sessions))

        self._call_in_loop(apply)

    def _on_media_properties_changed(self, sender: Any, args: Any) -> None:
        # Свойства изменились: трек мог смениться, обложка может быть ещё не готова.
        logger.info("событие: media_properties_changed")
        self._call_in_loop(lambda: self._schedule_refresh(with_cover=True))

    def _on_playback_info_changed(self, sender: Any, args: Any) -> None:
        # Сменилось состояние play/pause — обложку перечитывать не нужно.
        logger.info("событие: playback_info_changed")
        self._call_in_loop(lambda: self._schedule_refresh(with_cover=False))

    # -- работа с сессией (в своём цикле) ------------------------------------

    def _set_session(self, session: Any) -> None:
        if session is self._session:
            logger.debug("сессия не изменилась — пропуск")
            return
        self._detach_session()
        self._cancel_cover_retry()  # попытки обложки старой сессии не нужны
        self._session = session
        if session is None:
            logger.info("сессии нет — no_media")
            self._reset_track_state()
            self.no_media.emit()
            return
        logger.info(
            "подписка на сессию: auid=%s, status=%s",
            _auid(session),
            session_playback_status(session),
        )
        try:
            self._mp_handler = self._on_media_properties_changed
            self._pi_handler = self._on_playback_info_changed
            self._mp_token = session.add_media_properties_changed(self._mp_handler)
            self._pi_token = session.add_playback_info_changed(self._pi_handler)
        except Exception:
            logger.exception("подписка на события сессии не удалась")
            self._session = None
            self._reset_track_state()
            self.no_media.emit()
            return
        self._schedule_refresh(with_cover=True)

    def _detach_session(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        logger.info("отписка от сессии: auid=%s", _auid(session))
        if self._mp_token is not None:
            try:
                session.remove_media_properties_changed(self._mp_token)
            except Exception:
                logger.warning("remove_media_properties_changed", exc_info=True)
            self._mp_token = None
            self._mp_handler = None
        if self._pi_token is not None:
            try:
                session.remove_playback_info_changed(self._pi_token)
            except Exception:
                logger.warning("remove_playback_info_changed", exc_info=True)
            self._pi_token = None
            self._pi_handler = None

    def _reset_track_state(self) -> None:
        self._last_key = None
        self._last_cover = None
        self._last_cover_sha1 = None
        self._last_playing = None
        self._last_modes = None

    def _emit_modes(self, modes: PlaybackModes) -> None:
        if modes != self._last_modes:
            self._last_modes = modes
            logger.info(
                "modes_changed: repeat=%s shuffle=%s (поддержка: %s/%s)",
                modes.repeat,
                modes.shuffle,
                modes.repeat_supported,
                modes.shuffle_supported,
            )
            self.modes_changed.emit(modes)

    def _schedule_refresh(self, *, with_cover: bool, force: bool = False) -> None:
        """Планирует refresh с дебаунсом: пачка событий в пределах
        REFRESH_DEBOUNCE превращается в ОДИН refresh по свежайшим свойствам."""
        self._pending_cover = self._pending_cover or with_cover
        self._pending_force = self._pending_force or force
        if self._debounce_task is not None and not self._debounce_task.done():
            logger.debug("refresh уже запланирован — событие слито в него")
            return
        self._debounce_task = asyncio.ensure_future(self._debounced_refresh())

    async def _debounced_refresh(self) -> None:
        await asyncio.sleep(REFRESH_DEBOUNCE)
        with_cover = self._pending_cover
        force = self._pending_force
        self._pending_cover = False
        self._pending_force = False

        # Счётчик поколений: устаревший refresh не сможет ничего эмитнуть.
        self._refresh_seq += 1
        seq = self._refresh_seq

        # Предыдущую незавершённую задачу отменяем — гонки обновлений нет.
        previous = self._refresh_task
        if previous is not None and not previous.done():
            previous.cancel()
            logger.info("refresh: предыдущая незавершённая задача отменена")

        task = asyncio.ensure_future(
            self._refresh(with_cover=with_cover, force=force, seq=seq)
        )
        self._refresh_task = task
        task.add_done_callback(self._task_done_logged)

    def _task_done_logged(self, task: asyncio.Task) -> None:
        """Исключение задачи иначе «зависает» и никогда никуда не доходит."""
        if task.cancelled():
            logger.debug("задача отменена: %r", task.get_name())
            return
        exc = task.exception()
        if exc is not None:
            logger.error("задача упала: %r", exc, exc_info=exc)

    def _refresh_is_current(self, seq: Optional[int], session: Any) -> bool:
        """Проверка перед КАЖДЫМ эмитом: результат принадлежит последнему
        refresh и текущей сессии, иначе он устарел и отбрасывается."""
        if session is not self._session:
            logger.debug("refresh: сессия сменилась — результат отброшен")
            return False
        if seq is not None and seq != self._refresh_seq:
            logger.info(
                "refresh: устаревший результат отброшен (seq=%s, актуален %s)",
                seq,
                self._refresh_seq,
            )
            return False
        return True

    async def _refresh(
        self,
        *,
        with_cover: bool,
        force: bool = False,
        seq: Optional[int] = None,
    ) -> None:
        session = self._session
        if session is None:
            logger.info("refresh: сессии нет — no_media")
            self.no_media.emit()
            return
        try:
            props = await get_media_properties(session)
        except Exception:
            # Сессия закрылась во время чтения (или API неожиданного вида).
            logger.warning("свойства не прочитаны — проверяем сессию", exc_info=True)
            self._handle_session_gone()
            return
        if props is None:
            logger.info("props is None — no_media")
            self._reset_track_state()
            self.no_media.emit()
            return
        # Пока читали свойства, мог запуститься более свежий refresh.
        if not self._refresh_is_current(seq, session):
            return

        title, artist = parse_media_properties(props)
        key = (title, artist)
        thumb = has_thumbnail(props)
        new_track = key != self._last_key or force
        logger.debug(
            "refresh: title_len=%d artist_len=%d thumb=%s new_track=%s",
            len(title),
            len(artist),
            thumb,
            new_track,
        )

        if new_track:
            self._cancel_cover_retry()
            prev_sha = self._last_cover_sha1
            # Обложку нового трека читаем ВСЕГДА, даже если refresh пришёл от
            # playback_info_changed (with_cover=False): иначе трек уходил в UI
            # без обложки, повторы не запускались, и заглушка оставалась
            # до следующего media_properties_changed (которого может не быть).
            cover = await self._read_cover_if(props, enabled=True)
            if not self._refresh_is_current(seq, session):
                return
            # Обложка прошлого трека НЕ должна остаться: если её нет —
            # state сбрасывается, UI по track_changed(None) покажет заглушку.
            self._last_key = key
            self._set_cover_state(cover)
            logger.info(
                "track_changed: title_len=%d artist_len=%d cover=%s sha1=%s",
                len(title),
                len(artist),
                f"{len(cover)}B" if cover else None,
                (_sha1(cover) or "")[:8] or None,
            )
            self.track_changed.emit(title, artist, cover)

            same_as_prev = (
                cover is not None
                and prev_sha is not None
                and _sha1(cover) == prev_sha
            )
            if cover is None or same_as_prev:
                reason = (
                    "обложка отсутствует"
                    if cover is None
                    else "хеш совпал с обложкой прошлого трека"
                )
                logger.info(
                    "обложка: %s — повторные попытки (%d, паузы %s)",
                    reason,
                    len(COVER_RETRY_DELAYS),
                    COVER_RETRY_DELAYS,
                )
                self._start_cover_retry(session, key)

        elif with_cover:
            # Тот же трек: на КАЖДОМ media_properties_changed перечитываем
            # thumbnail и сравниваем хеш — Chrome сначала отдаёт запасную
            # картинку, настоящую обложку добавляет позже.
            cover = await self._read_cover_if(props, enabled=True)
            if not self._refresh_is_current(seq, session):
                return
            if cover is None:
                if self._last_cover_sha1 is None:
                    logger.info("обложки ещё нет — повторные попытки")
                    self._start_cover_retry(session, key)
                else:
                    logger.debug("thumbnail пока не прочитан")
            else:
                sha = _sha1(cover)
                if sha != self._last_cover_sha1:
                    logger.info(
                        "cover_changed: size=%d sha1=%s", len(cover), sha[:8]
                    )
                    self._set_cover_state(cover)
                    self.cover_changed.emit(cover)
                else:
                    logger.debug("обложка не изменилась (sha1=%s)", sha[:8])
        else:
            logger.debug("без чтения обложки (with_cover=False)")

        if not self._refresh_is_current(seq, session):
            return
        try:
            playback_info = session.get_playback_info()
            playing = is_playing_status(getattr(playback_info, "playback_status", None))
        except Exception:
            logger.warning("get_playback_info", exc_info=True)
            self._handle_session_gone()
            return
        if playing != self._last_playing:
            self._last_playing = playing
            logger.info("playback_state_changed: %s", playing)
            self.playback_state_changed.emit(playing)
        # Повтор/перемешивание приходят тем же playback_info_changed.
        self._emit_modes(parse_playback_modes(playback_info))
        # Позиция: после любого refresh (смена трека, пауза, перемотка).
        self._emit_timeline()

    def _handle_session_gone(self) -> None:
        """Сессия могла закрыться: уточняем у менеджера либо переходим в no_media."""
        current: Any = None
        if self._manager is not None:
            try:
                current = self._manager.get_current_session()
            except Exception:
                logger.warning("get_current_session при проверке сессии", exc_info=True)
                current = None
        # ВАЖНО: winrt на каждый get_current_session() отдаёт НОВУЮ
        # Python-обёртку, поэтому проверки ``is`` недостаточно. Если менеджер
        # вернул ту же сессию (тот же AUMID), что только что не прочиталась,
        # переподписка на неё дала бы бесконечный цикл
        # refresh → ошибка → _set_session → refresh … каждые REFRESH_DEBOUNCE.
        if current is not None and not _same_session(current, self._session):
            logger.info("сессия сменилась: %s → %s", _auid(self._session), _auid(current))
            self._set_session(current)
            return
        logger.info("сессия недоступна — no_media")
        self._detach_session()
        self._reset_track_state()
        self.no_media.emit()

    # -- чтение обложки ------------------------------------------------------

    async def _read_cover_if(self, props: Any, *, enabled: bool) -> Optional[bytes]:
        """Одно чтение thumbnail с таймаутом (или None, если чтение не нужно)."""
        if not enabled:
            return None
        try:
            thumbnail = getattr(props, "thumbnail", None)
        except Exception:
            logger.warning("чтение свойства thumbnail", exc_info=True)
            return None
        if thumbnail is None:
            logger.debug("thumbnail отсутствует")
            return None
        try:
            return await asyncio.wait_for(
                self._read_cover(thumbnail), timeout=COVER_READ_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                "чтение обложки не уложилось в %.1f с", COVER_READ_TIMEOUT
            )
            return None
        except Exception:
            logger.warning("чтение обложки завершилось ошибкой", exc_info=True)
            return None

    def _start_cover_retry(self, session: Any, key: tuple[str, str]) -> None:
        """Фоновые повторы после смены трека: обложка None или хеш равен
        обложке прошлого трека (Chrome отдаёт запасную картинку)."""
        self._cancel_cover_retry()
        task = asyncio.ensure_future(self._cover_retry_loop(session, key))
        self._cover_retry_task = task
        task.add_done_callback(self._task_done_logged)

    def _cancel_cover_retry(self) -> None:
        """Отмена повторов при смене трека или сессии."""
        task, self._cover_retry_task = self._cover_retry_task, None
        if task is not None and not task.done():
            task.cancel()
            logger.debug("повторные попытки обложки отменены")

    async def _cover_retry_loop(self, session: Any, key: tuple[str, str]) -> None:
        total = len(COVER_RETRY_DELAYS)
        for attempt, delay in enumerate(COVER_RETRY_DELAYS, start=1):
            await asyncio.sleep(delay)
            if session is not self._session or key != self._last_key:
                logger.debug(
                    "повторы обложки: трек/сессия сменились — стоп (попытка %d)",
                    attempt,
                )
                return
            try:
                props = await get_media_properties(session)
            except Exception:
                logger.debug(
                    "повтор %d/%d: свойства не прочитаны", attempt, total,
                    exc_info=True,
                )
                continue
            cover = await self._read_cover_if(props, enabled=True)
            if session is not self._session or key != self._last_key:
                logger.debug("повторы обложки: состояние изменилось — стоп")
                return
            if not cover:
                logger.debug("повтор %d/%d: обложки нет", attempt, total)
                continue
            sha = _sha1(cover)
            if sha == self._last_cover_sha1:
                logger.debug(
                    "повтор %d/%d: хеш не изменился (sha1=%s)", attempt, total, sha[:8]
                )
                continue
            logger.info(
                "обложка обновлена повтором %d/%d: size=%d sha1=%s",
                attempt,
                total,
                len(cover),
                sha[:8],
            )
            self._set_cover_state(cover)
            self.cover_changed.emit(cover)
            return
        logger.info("обложка не обновилась после %d попыток", total)

    def _set_cover_state(self, cover: Optional[bytes]) -> None:
        """Запоминает последнюю отправленную обложку (байты + sha1)."""
        self._last_cover = cover
        self._last_cover_sha1 = _sha1(cover) if cover else None
        if cover:
            self._dump_debug_cover(cover)

    def _dump_debug_cover(self, cover: bytes) -> None:
        """Только в DEBUG-режиме: копия последней обложки для проверки."""
        if not debug_enabled():
            return
        path = default_config_dir() / "debug_cover.bin"
        try:
            path.write_bytes(cover)
            logger.debug("debug_cover.bin сохранён: %d байт → %s", len(cover), path)
        except OSError:
            logger.warning("не удалось сохранить %s", path, exc_info=True)

    async def _read_cover(self, thumbnail: Any) -> Optional[bytes]:
        """thumbnail → open_read_async → DataReader → bytes."""
        try:
            from winrt.windows.storage.streams import DataReader  # noqa: PLC0415
        except Exception:
            logger.exception("импорт DataReader не удался")
            return None
        stream = None
        reader = None
        try:
            stream = await thumbnail.open_read_async()
            size = int(getattr(stream, "size", 0) or 0)
            if size <= 0:
                logger.debug("поток обложки пуст (size=%d)", size)
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            buffer = bytearray(size)
            reader.read_bytes(buffer)
            return bytes(buffer) or None
        except Exception:
            # Сессия/поток могли закрыться во время чтения.
            logger.warning("чтение потока обложки прервано", exc_info=True)
            return None
        finally:
            for obj in (reader, stream):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        logger.debug("закрытие потока/ридера", exc_info=True)

    # -- команды -------------------------------------------------------------

    async def _command(self, action: str) -> None:
        if self._mock:
            mock = self._mock_session
            if mock is None:
                return
            if action == "play_pause":
                mock.toggle()
            elif action == "next":
                mock.next()
            elif action == "previous":
                mock.previous()
            elif action == "repeat":
                mock.set_repeat(next_repeat_mode(mock.modes.repeat))
            elif action == "shuffle":
                mock.set_shuffle(not mock.modes.shuffle)
            logger.debug("mock-команда %s → index=%d playing=%s",
                         action, mock.index, mock.playing)
            self._emit_mock()
            return
        session = self._session
        if session is None:
            logger.warning("команда %s: сессии нет", action)
            return
        if action in ("repeat", "shuffle"):
            await self._mode_command(session, action)
            return
        try:
            await invoke_command(session, action)
            logger.info("команда %s выполнена (%s)", action, COMMAND_METHODS[action])
        except Exception:
            logger.exception("команда %s не выполнена", action)

    async def _mode_command(self, session: Any, action: str) -> None:
        """Повтор/перемешивание: новое значение считается от текущего."""
        modes = self._last_modes or PlaybackModes()
        try:
            if action == "repeat":
                if not modes.repeat_supported:
                    logger.info("плеер не поддерживает повтор — команда пропущена")
                    return
                target = next_repeat_mode(modes.repeat)
                ok = await set_repeat_mode(session, target)
                logger.info("повтор → %s: %s", target, "принято" if ok else "отклонено плеером")
            else:
                if not modes.shuffle_supported:
                    logger.info("плеер не поддерживает перемешивание — команда пропущена")
                    return
                target = not bool(modes.shuffle)
                ok = await set_shuffle(session, target)
                logger.info(
                    "перемешивание → %s: %s", target, "принято" if ok else "отклонено плеером"
                )
        except Exception:
            logger.exception("команда %s не выполнена", action)
            return
        # Не все плееры шлют playback_info_changed после смены режима —
        # перечитываем состояние сами, чтобы кнопка показала правду.
        self._schedule_refresh(with_cover=False)

    # -- MOCK ----------------------------------------------------------------

    async def _mock_loop(self) -> None:
        self._emit_mock()
        while True:
            await asyncio.sleep(MOCK_INTERVAL)
            mock = self._mock_session
            if mock is None:
                return
            if mock.playing:
                mock.auto_advance()
            self._emit_mock()

    def _emit_mock(self) -> None:
        mock = self._mock_session
        if mock is None:
            self.no_media.emit()
            return
        info = mock.track
        if info.key != self._last_key:
            self._last_key = info.key
            self._last_cover = info.cover_bytes
            logger.info(
                "track_changed (mock): title_len=%d artist_len=%d cover=%dB",
                len(info.title),
                len(info.artist),
                len(info.cover_bytes or b""),
            )
            self.track_changed.emit(info.title, info.artist, info.cover_bytes)
        if mock.playing != self._last_playing:
            self._last_playing = mock.playing
            self.playback_state_changed.emit(mock.playing)
        self._emit_modes(mock.modes)
        self.timeline_changed.emit(mock.position, mock.duration)

    # -- очистка -------------------------------------------------------------

    async def _cleanup(self) -> None:
        if self._mock_task is not None:
            self._mock_task.cancel()
            try:
                await self._mock_task
            except (asyncio.CancelledError, Exception):
                logger.debug("mock-задача остановлена", exc_info=True)
            self._mock_task = None
        tasks = [
            t
            for t in (
                self._refresh_task,
                self._debounce_task,
                self._cover_retry_task,
                self._timeline_task,
            )
            if t is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_task = None
        self._debounce_task = None
        self._cover_retry_task = None
        self._timeline_task = None
        self._pending_cover = False
        self._pending_force = False
        if not self._mock:
            self._detach_session()
            self._detach_manager()
            logger.info("подписки сняты")

    def _detach_manager(self) -> None:
        """Однократная отписка от событий менеджера по сохранённому токену."""
        manager, self._manager = self._manager, None
        token, self._current_token = self._current_token, None
        self._current_handler = None
        if manager is None or token is None:
            return
        try:
            manager.remove_current_session_changed(token)
        except Exception:
            logger.warning("remove_current_session_changed", exc_info=True)
        logger.debug("подписка на менеджер снята")


def _auid(session: Any) -> Optional[str]:
    """source_app_user_model_id сессии для логов (без названий треков)."""
    if session is None:
        return None
    try:
        return session.source_app_user_model_id
    except Exception:
        logger.debug("source_app_user_model_id недоступен", exc_info=True)
        return None


def _same_session(a: Any, b: Any) -> bool:
    """Одна и та же SMTC-сессия: тот же объект или тот же непустой AUMID."""
    if a is b:
        return True
    if a is None or b is None:
        return False
    aumid = _auid(a)
    return bool(aumid) and aumid == _auid(b)
