"""Тесты багов из лога: (1) поздняя обложка Chrome, (2) гонка refresh.

Баг 1: Chrome сначала отдаёт запасную картинку, настоящую обложку добавляет
позже; на том же (title, artist) обложка должна перечитаться и дойти до UI
через cover_changed, даже если хеш изменился.

Баг 2: параллельные refresh завершаются в произвольном порядке; устаревший
результат (seq < актуального) обязан отбрасываться до любого эмита.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

import media_controller as mc
from media_controller import MediaController


class FakeProps:
    def __init__(self, title="", artist="", thumbnail=None) -> None:
        self.title = title
        self.artist = artist
        self.thumbnail = thumbnail


class FakePlaybackInfo:
    def __init__(self, status: int = 4) -> None:
        self.playback_status = status


class FakeSession:
    """Сессия с подменяемыми свойствами (для имитации смены трека)."""

    def __init__(self, props: FakeProps) -> None:
        self._props = props
        self.source_app_user_model_id = "Chrome"

    def get_playback_info(self):
        return FakePlaybackInfo(4)

    async def try_get_media_properties_async(self):
        return self._props


def _controller(session: FakeSession) -> MediaController:
    """Контроллер без start(): цикл не нужен, refresh зовём напрямую."""
    c = MediaController(mock=False)
    c._session = session
    return c


def _collect(signal) -> list:
    got: list = []
    signal.connect(lambda *args: got.append(args))
    return got


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ---------------------------------------------------------------------------
# Баг 1а: тот же трек, обложка пришла позже → cover_changed
# ---------------------------------------------------------------------------


def test_cover_changed_emitted_when_cover_arrives_later():
    props = FakeProps(title="Track", artist="Artist", thumbnail=object())
    session = FakeSession(props)
    controller = _controller(session)
    covers = iter([b"cover-v1", b"cover-v2"])

    async def fake_read(thumbnail):
        return next(covers)

    controller._read_cover = fake_read
    tracks = _collect(controller.track_changed)
    changed = _collect(controller.cover_changed)

    async def main():
        controller._refresh_seq = 1
        await controller._refresh(with_cover=True, seq=1)
        controller._refresh_seq = 2
        await controller._refresh(with_cover=True, seq=2)

    asyncio.run(main())

    # Трек один и тот же → track_changed только один раз (с первой обложкой).
    assert len(tracks) == 1
    assert tracks[0][2] == b"cover-v1"
    # Но новая обложка дошла отдельным сигналом с новым хешем.
    assert changed == [(b"cover-v2",)]
    assert _sha(b"cover-v1") != _sha(b"cover-v2")
    assert controller._last_cover == b"cover-v2"


def test_cover_not_emitted_when_hash_unchanged():
    """Перечитывание на каждом событии не плодит лишних эмитов."""
    props = FakeProps(title="T", artist="A", thumbnail=object())
    controller = _controller(FakeSession(props))

    async def fake_read(thumbnail):
        return b"same-cover"

    controller._read_cover = fake_read
    changed = _collect(controller.cover_changed)

    async def main():
        controller._refresh_seq = 1
        await controller._refresh(with_cover=True, seq=1)
        controller._refresh_seq = 2
        await controller._refresh(with_cover=True, seq=2)

    asyncio.run(main())
    assert changed == [], "та же обложка (по хешу) не должна эмититься"


# ---------------------------------------------------------------------------
# Баг 2: гонка — медленный refresh №1 завершается после №2 и отбрасывается
# ---------------------------------------------------------------------------


def test_stale_refresh_result_is_discarded():
    """Telegram-сценарий: устаревший refresh не должен перезаписать трек."""
    props_a = FakeProps(title="Old track", artist="Old artist", thumbnail=object())
    props_b = FakeProps(title="New track", artist="New artist", thumbnail=object())
    session = FakeSession(props_a)
    controller = _controller(session)

    reads = 0

    async def fake_read(thumbnail):
        nonlocal reads
        reads += 1
        if reads == 1:  # первый (устаревающий) refresh — медленный
            await asyncio.sleep(0.2)
            return b"slow-cover"
        return b"fast-cover"

    controller._read_cover = fake_read
    tracks = _collect(controller.track_changed)

    async def main():
        controller._refresh_seq = 1
        slow = asyncio.ensure_future(controller._refresh(with_cover=True, seq=1))
        await asyncio.sleep(0.05)  # №1 уже читает свойства, ждём в read
        # Новая сессия/трек → запускается более свежий refresh №2.
        session._props = props_b
        controller._refresh_seq = 2
        await controller._refresh(with_cover=True, seq=2)
        await slow  # №1 завершается ПОСЛЕ №2

    asyncio.run(main())

    assert len(tracks) == 1, "устаревший refresh не должен ничего эмитнуть"
    assert tracks[0][0] == "New track"
    assert tracks[0][2] == b"fast-cover"


def test_refresh_with_old_session_is_discarded():
    """Результат, посчитанный для снятой сессии, отбрасывается."""
    props = FakeProps(title="T", artist="A", thumbnail=object())
    session = FakeSession(props)
    controller = _controller(session)

    async def fake_read(thumbnail):
        await asyncio.sleep(0.05)  # сессия «уходит» во время чтения обложки
        return b"cover"

    controller._read_cover = fake_read
    tracks = _collect(controller.track_changed)

    async def main():
        controller._refresh_seq = 1
        task = asyncio.ensure_future(controller._refresh(with_cover=True, seq=1))
        await asyncio.sleep(0.01)  # refresh начался, сессия ещё актуальна
        controller._session = None  # сессия ушла в момент чтения
        await task

    asyncio.run(main())
    assert tracks == [], "для чужой сессии эмитов быть не должно"


# ---------------------------------------------------------------------------
# Баг 1б: обложка совпала с прошлым треком → повторные попытки до новой
# ---------------------------------------------------------------------------


def test_cover_retry_until_new_cover(monkeypatch):
    monkeypatch.setattr(mc, "COVER_RETRY_DELAYS", (0.01, 0.01))
    props_a = FakeProps(title="Track A", artist="Artist", thumbnail=object())
    props_b = FakeProps(title="Track B", artist="Artist", thumbnail=object())
    session = FakeSession(props_a)
    controller = _controller(session)

    shared = b"chrome-placeholder-cover"
    real = b"real-artwork-of-track-b"
    reads: list[bytes] = []

    async def fake_read(thumbnail):
        # Трек A и первое чтение трека B → запасная картинка Chrome,
        # повторные попытки → настоящая обложка (как в реальном Chrome).
        cover = real if reads.count(shared) >= 2 else shared
        reads.append(cover)
        return cover

    controller._read_cover = fake_read
    changed = _collect(controller.cover_changed)

    async def main():
        # Трек A: обложка shared.
        controller._refresh_seq = 1
        await controller._refresh(with_cover=True, seq=1)
        assert controller._last_cover == shared
        assert controller._last_cover_sha1 == _sha(shared)

        # Трек B: на первом чтении обложка та же самая → нужен повтор.
        session._props = props_b
        controller._refresh_seq = 2
        await controller._refresh(with_cover=True, seq=2)

        # Повторная задача запущена и ещё не завершена — ждём её.
        assert controller._cover_retry_task is not None
        await controller._cover_retry_task

    asyncio.run(main())

    assert changed == [(real,)]
    assert controller._last_cover == real


def test_cover_retry_stops_on_track_change(monkeypatch):
    """Повторы отменяются, когда трек сменился ещё до их завершения."""
    monkeypatch.setattr(mc, "COVER_RETRY_DELAYS", (0.05, 0.05, 0.05))
    props_a = FakeProps(title="A", artist="X", thumbnail=object())
    props_b = FakeProps(title="B", artist="X", thumbnail=object())
    session = FakeSession(props_a)
    controller = _controller(session)

    async def read_none(thumbnail):
        return None

    controller._read_cover = read_none
    changed = _collect(controller.cover_changed)

    async def main():
        controller._refresh_seq = 1
        await controller._refresh(with_cover=True, seq=1)
        task = controller._cover_retry_task
        assert task is not None and not task.done()
        # Трек сменился → повторы должны встать (по key).
        session._props = props_b
        controller._last_key = ("B", "X")
        await asyncio.sleep(0.1)
        assert task.done() and task.cancelled() is False
        assert changed == []

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Дебаунс: пачка событий → один refresh
# ---------------------------------------------------------------------------


def test_debounce_merges_event_burst(monkeypatch):
    monkeypatch.setattr(mc, "REFRESH_DEBOUNCE", 0.05)
    props = FakeProps(title="T", artist="A", thumbnail=object())
    controller = _controller(FakeSession(props))

    calls = 0

    async def fake_read(thumbnail):
        return b"c"

    controller._read_cover = fake_read
    real_refresh = controller._refresh

    async def counting_refresh(*, with_cover, force=False, seq=None):
        nonlocal calls
        calls += 1
        assert with_cover is True, "пачка событий должна слиться в with_cover=True"
        return await real_refresh(with_cover=with_cover, force=force, seq=seq)

    controller._refresh = counting_refresh

    async def main():
        # Три события в пределах одного окна дебаунса.
        controller._schedule_refresh(with_cover=False)
        controller._schedule_refresh(with_cover=False)
        controller._schedule_refresh(with_cover=True)
        assert controller._debounce_task is not None
        await controller._debounce_task
        assert controller._refresh_task is not None
        await controller._refresh_task

    asyncio.run(main())
    assert calls == 1, "пачка событий в пределах дебаунса → один refresh"


def test_debounce_cancels_previous_refresh(monkeypatch):
    """Новый refresh отменяет незавершённый предыдущий (гонки нет)."""
    monkeypatch.setattr(mc, "REFRESH_DEBOUNCE", 0.01)
    props = FakeProps(title="T", artist="A", thumbnail=object())
    controller = _controller(FakeSession(props))

    async def slow_read(thumbnail):
        await asyncio.sleep(1.0)
        return b"cover"

    controller._read_cover = slow_read

    async def main():
        controller._schedule_refresh(with_cover=True)
        await controller._debounce_task
        first_task = controller._refresh_task
        assert first_task is not None and not first_task.done()
        # Не дожидаясь медленного read, планируем новый refresh.
        controller._schedule_refresh(with_cover=True)
        await controller._debounce_task
        # Незавершённая предыдущая задача должна быть отменена новой.
        await asyncio.sleep(0.05)
        assert first_task.cancelled(), "прошлый refresh обязан быть отменён"
        second_task = controller._refresh_task
        assert second_task is not None and second_task is not first_task
        await asyncio.gather(second_task, return_exceptions=True)

    asyncio.run(main())
