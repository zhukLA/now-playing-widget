"""Повтор и перемешивание: разбор SMTC, команды, контроллер и MOCK."""

from __future__ import annotations

import asyncio
import enum

import pytest

import media_controller as mc
from media_controller import (
    REPEAT_ALL,
    REPEAT_ONE,
    REPEAT_OFF,
    MediaController,
    MockSession,
    MockTrack,
    PlaybackModes,
    next_repeat_mode,
    parse_playback_modes,
    repeat_mode_from_winrt,
    repeat_mode_to_winrt,
    set_repeat_mode,
    set_shuffle,
)


class FakeRepeatEnum(enum.IntEnum):
    """Как winrt.windows.media.MediaPlaybackAutoRepeatMode."""

    NONE = 0
    TRACK = 1
    LIST = 2


class FakeControls:
    def __init__(self, repeat: bool = True, shuffle: bool = True) -> None:
        self.is_repeat_enabled = repeat
        self.is_shuffle_enabled = shuffle


class FakePlaybackInfo:
    def __init__(self, repeat=None, shuffle=None, controls=None, status: int = 4) -> None:
        self.auto_repeat_mode = repeat
        self.is_shuffle_active = shuffle
        self.controls = controls
        self.playback_status = status


class Boxed:
    """IReference<T> в виде обёртки с .value."""

    def __init__(self, value) -> None:
        self.value = value


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (FakeRepeatEnum.NONE, REPEAT_OFF),
        (FakeRepeatEnum.TRACK, REPEAT_ONE),
        (FakeRepeatEnum.LIST, REPEAT_ALL),
        (0, REPEAT_OFF),
        (2, REPEAT_ALL),
        (Boxed(1), REPEAT_ONE),
        (None, None),
        (7, None),
    ],
)
def test_repeat_mode_from_winrt(raw, expected) -> None:
    assert repeat_mode_from_winrt(raw) == expected


def test_repeat_mode_round_trip_without_winrt() -> None:
    # winrt не установлен — значения передаются как int (IntEnum-совместимо).
    for mode in (REPEAT_OFF, REPEAT_ONE, REPEAT_ALL):
        value = repeat_mode_to_winrt(mode)
        assert repeat_mode_from_winrt(value) == mode
    assert int(repeat_mode_to_winrt(REPEAT_ONE)) == 1  # TRACK
    assert int(repeat_mode_to_winrt(REPEAT_ALL)) == 2  # LIST


def test_next_repeat_mode_cycle() -> None:
    assert next_repeat_mode(REPEAT_OFF) == REPEAT_ALL
    assert next_repeat_mode(REPEAT_ALL) == REPEAT_ONE
    assert next_repeat_mode(REPEAT_ONE) == REPEAT_OFF
    assert next_repeat_mode(None) == REPEAT_ALL  # неизвестно → включаем


def test_parse_playback_modes_full() -> None:
    info = FakePlaybackInfo(FakeRepeatEnum.LIST, True, FakeControls(True, True))
    assert parse_playback_modes(info) == PlaybackModes(REPEAT_ALL, True, True, True)


def test_parse_playback_modes_boxed_and_unsupported() -> None:
    info = FakePlaybackInfo(Boxed(0), Boxed(False), FakeControls(False, False))
    modes = parse_playback_modes(info)
    assert modes.repeat == REPEAT_OFF and modes.shuffle is False
    assert not modes.repeat_supported and not modes.shuffle_supported


def test_parse_playback_modes_without_controls() -> None:
    """Нет controls — поддержка определяется по наличию значения."""
    modes = parse_playback_modes(FakePlaybackInfo(repeat=None, shuffle=True))
    assert modes.repeat is None and not modes.repeat_supported
    assert modes.shuffle is True and modes.shuffle_supported


def test_parse_playback_modes_none() -> None:
    assert parse_playback_modes(None) == PlaybackModes()


# ---------------------------------------------------------------------------
# Команды SMTC
# ---------------------------------------------------------------------------


class FakeAsyncOperation:
    """Как winrt _IAsyncOperation: awaitable, но не coroutine."""

    def __init__(self, value) -> None:
        self._value = value

    def __await__(self):
        async def _coro():
            return self._value

        return _coro().__await__()


class ModeSession:
    def __init__(self, info: FakePlaybackInfo | None = None, accept: bool = True) -> None:
        self.info = info or FakePlaybackInfo(0, False, FakeControls())
        self.accept = accept
        self.calls: list[tuple[str, object]] = []
        self.source_app_user_model_id = "Spotify"

    def get_playback_info(self):
        return self.info

    def try_change_auto_repeat_mode_async(self, mode):
        self.calls.append(("repeat", int(mode)))
        return FakeAsyncOperation(self.accept)

    def try_change_shuffle_active_async(self, active):
        self.calls.append(("shuffle", active))
        return FakeAsyncOperation(self.accept)


def test_set_repeat_and_shuffle_call_winrt_methods() -> None:
    session = ModeSession()
    assert asyncio.run(set_repeat_mode(session, REPEAT_ONE)) is True
    assert asyncio.run(set_shuffle(session, True)) is True
    assert session.calls == [("repeat", 1), ("shuffle", True)]


def test_set_repeat_reports_player_refusal() -> None:
    assert asyncio.run(set_repeat_mode(ModeSession(accept=False), REPEAT_ALL)) is False


def test_set_mode_methods_missing() -> None:
    with pytest.raises(AttributeError):
        asyncio.run(set_repeat_mode(object(), REPEAT_ALL))
    with pytest.raises(AttributeError):
        asyncio.run(set_shuffle(object(), True))


# ---------------------------------------------------------------------------
# Контроллер
# ---------------------------------------------------------------------------


class FakeProps:
    title = "T"
    artist = "A"
    thumbnail = None


class RefreshSession(ModeSession):
    async def try_get_media_properties_async(self):
        return FakeProps()


def _controller(session) -> MediaController:
    c = MediaController(mock=False)
    c._session = session
    return c


def _collect(signal) -> list:
    got: list = []
    signal.connect(lambda *args: got.append(args))
    return got


def test_refresh_emits_modes_once_and_on_change() -> None:
    session = RefreshSession(FakePlaybackInfo(FakeRepeatEnum.NONE, False, FakeControls()))
    controller = _controller(session)
    got = _collect(controller.modes_changed)

    async def main():
        for seq in (1, 2):
            controller._refresh_seq = seq
            await controller._refresh(with_cover=False, seq=seq)
        session.info = FakePlaybackInfo(FakeRepeatEnum.TRACK, True, FakeControls())
        controller._refresh_seq = 3
        await controller._refresh(with_cover=False, seq=3)

    asyncio.run(main())
    assert [args[0] for args in got] == [
        PlaybackModes(REPEAT_OFF, False, True, True),
        PlaybackModes(REPEAT_ONE, True, True, True),
    ]


def test_repeat_command_cycles_from_current_state(monkeypatch) -> None:
    session = ModeSession()
    controller = _controller(session)
    controller._last_modes = PlaybackModes(REPEAT_ALL, False, True, True)
    refreshed: list[bool] = []
    monkeypatch.setattr(controller, "_schedule_refresh", lambda **kw: refreshed.append(True))

    asyncio.run(controller._command("repeat"))
    asyncio.run(controller._command("shuffle"))
    assert session.calls == [("repeat", 1), ("shuffle", True)]  # all → one; выкл → вкл
    assert refreshed == [True, True], "после команды состояние перечитывается"


def test_mode_command_skipped_when_unsupported(monkeypatch) -> None:
    session = ModeSession()
    controller = _controller(session)
    controller._last_modes = PlaybackModes(REPEAT_OFF, False, False, False)
    monkeypatch.setattr(controller, "_schedule_refresh", lambda **kw: None)
    asyncio.run(controller._command("repeat"))
    asyncio.run(controller._command("shuffle"))
    assert session.calls == []


# ---------------------------------------------------------------------------
# MOCK
# ---------------------------------------------------------------------------


def _tracks(n: int = 4):
    return tuple(MockTrack(f"T{i}", "A", (i, i, i)) for i in range(n))


def test_mock_repeat_one_keeps_track() -> None:
    session = MockSession(tracks=_tracks(), index=1)
    session.set_repeat(REPEAT_ONE)
    session.auto_advance()
    assert session.index == 1 and session.playing


def test_mock_repeat_off_stops_at_end_and_all_wraps() -> None:
    session = MockSession(tracks=_tracks(3), index=2)
    session.auto_advance()
    assert session.playing is False and session.index == 2

    session = MockSession(tracks=_tracks(3), index=2)
    session.set_repeat(REPEAT_ALL)
    session.auto_advance()
    assert session.playing and session.index == 0


def test_mock_shuffle_picks_other_track() -> None:
    session = MockSession(tracks=_tracks(5), index=0)
    session.set_shuffle(True)
    seen = set()
    for _ in range(20):
        before = session.index
        session.next()
        assert session.index != before
        seen.add(session.index)
    assert len(seen) > 2, "перемешивание должно ходить по разным трекам"


def test_mock_controller_mode_commands() -> None:
    controller = MediaController(mock=True)
    got = _collect(controller.modes_changed)
    asyncio.run(controller._command("repeat"))
    asyncio.run(controller._command("shuffle"))
    asyncio.run(controller._command("repeat"))
    modes = [args[0] for args in got]
    assert [m.repeat for m in modes] == [REPEAT_ALL, REPEAT_ALL, REPEAT_ONE]
    assert [m.shuffle for m in modes] == [False, True, True]
    assert all(m.repeat_supported and m.shuffle_supported for m in modes)


def test_repeat_methods_are_winrt3_names() -> None:
    """Имена зафиксированы по заглушкам winrt-Windows.Media.Control 3.2.1."""
    assert mc.REPEAT_METHOD == "try_change_auto_repeat_mode_async"
    assert mc.SHUFFLE_METHOD == "try_change_shuffle_active_async"
