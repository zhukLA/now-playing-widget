"""Тесты на найденный баг и обвязку SMTC-API winrt 3.x.

Воспроизводимый баг: winrt 3.2.1 предоставляет только
``try_get_media_properties_async()``; старый код вызывал несуществующий
``try_get_media_properties()`` → AttributeError → ошибочный no_media()
при живой сессии. Здесь фиксируется, что берутся ПРАВИЛЬНЫЕ имена методов.
"""

from __future__ import annotations

import asyncio

import pytest

from media_controller import (
    COMMAND_METHODS,
    choose_session,
    get_media_properties,
    invoke_command,
    is_playing_status,
    parse_media_properties,
    session_playback_status,
)


class FakeProps:
    def __init__(self, title="", artist="", thumbnail=None) -> None:
        self.title = title
        self.artist = artist
        self.thumbnail = thumbnail


class FakePlaybackInfo:
    def __init__(self, status: int) -> None:
        self.playback_status = status


class Winrt3Session:
    """Фейк с API.winrt 3.x: только async-методы (как в реальном пакете)."""

    def __init__(self, status: int = 4, props: FakeProps | None = None,
                 auid: str = "Chrome") -> None:
        self.source_app_user_model_id = auid
        self._status = status
        self._props = props
        self.calls: list[str] = []

    def get_playback_info(self):
        return FakePlaybackInfo(self._status)

    async def try_get_media_properties_async(self):
        return self._props

    async def try_toggle_play_pause_async(self):
        self.calls.append("toggle_play_pause")

    async def try_skip_next_async(self):
        self.calls.append("skip_next")

    async def try_skip_previous_async(self):
        self.calls.append("skip_previous")


class LegacySession:
    """Фейк старого sync-API (winsdk-подобный)."""

    def __init__(self, props: FakeProps) -> None:
        self._props = props

    def try_get_media_properties(self):
        return self._props


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Найденный баг: async-only свойства (winrt 3.x)
# ---------------------------------------------------------------------------


def test_winrt3_session_has_no_sync_props_method():
    """Воспроизведение бага: у winrt-3.x-сессии НЕТ try_get_media_properties."""
    session = Winrt3Session()
    assert not hasattr(session, "try_get_media_properties")
    assert hasattr(session, "try_get_media_properties_async")


def test_get_media_properties_works_with_winrt3_async():
    """Главный тест: свойства читаются из async-метода (раньше — AttributeError)."""
    props = FakeProps(
        title="lofi hip hop mix 📚 beats to relax/study (Part 1)",
        artist="Lofi Girl",
        thumbnail=object(),
    )
    session = Winrt3Session(props=props)
    got = _run(get_media_properties(session))
    assert got is props
    title, artist = parse_media_properties(got)
    assert "📚" in title  # эмодзи не ломает разбор
    assert len(title) == len("lofi hip hop mix 📚 beats to relax/study (Part 1)")
    assert artist == "Lofi Girl"


def test_get_media_properties_falls_back_to_sync():
    props = FakeProps(title="Old API", artist="Legacy")
    session = LegacySession(props=props)
    assert _run(get_media_properties(session)) is props


def test_get_media_properties_raises_when_no_method():
    class Empty:
        pass

    with pytest.raises(AttributeError):
        _run(get_media_properties(Empty()))


# ---------------------------------------------------------------------------
# Найденный баг №2: winrt возвращает _IAsyncOperation, а не coroutine
# ---------------------------------------------------------------------------


class FakeAsyncOperation:
    """Имитация winrt ``_IAsyncOperation``: awaitable, но НЕ coroutine."""

    def __init__(self, value, fail_flag=None) -> None:
        self._value = value
        self.awaited = False
        self._fail_flag = fail_flag

    def __await__(self):
        async def _coro():
            self.awaited = True
            if self._fail_flag is not None:
                self._fail_flag.append(True)
            return self._value

        return _coro().__await__()


class OpSession:
    """Сессия, чья async-метод отдаёт _IAsyncOperation (как в winrt 3.x)."""

    def __init__(self, props: FakeProps) -> None:
        self._op = FakeAsyncOperation(props)
        self._op_ref = self._op
        self.source_app_user_model_id = "Chrome"

    def try_get_media_properties_async(self):
        return self._op

    def get_playback_info(self):
        return FakePlaybackInfo(4)

    async def try_toggle_play_pause_async(self):
        pass

    def command_op(self):
        return self._op


def test_iasyncoperation_is_not_a_coroutine():
    """Воспроизведение бага: asyncio.iscoroutine не видит winrt-операцию."""
    import asyncio as _asyncio

    op = FakeAsyncOperation(object())
    assert not _asyncio.iscoroutine(op)
    assert hasattr(op, "__await__")


def test_get_media_properties_awaits_iasyncoperation():
    """Главный тест бага №2: наружу должны вернуться props, а не сама операция."""
    props = FakeProps(
        title="lofi hip hop mix 📚 beats to relax/study to (Part 1)",
        artist="Lofi Girl",
        thumbnail=object(),
    )
    session = OpSession(props)
    got = _run(get_media_properties(session))
    assert got is props, "должны получить MediaProperties, а не _IAsyncOperation"
    assert session._op_ref.awaited, "операция должна быть await-нута"
    # именно этот баг давал title_len=0 при живой сессии:
    title, artist = parse_media_properties(got)
    assert len(title) == len("lofi hip hop mix 📚 beats to relax/study to (Part 1)")
    assert artist == "Lofi Girl"


def test_invoke_command_awaits_iasyncoperation():
    flags: list[bool] = []

    class CmdSession:
        def try_toggle_play_pause_async(self):
            return FakeAsyncOperation(None, fail_flag=flags)

    _run(invoke_command(CmdSession(), "play_pause"))
    assert flags == [True], "команда должна быть await-нута"


# ---------------------------------------------------------------------------
# Команды управления: имена методов winrt 3.x
# ---------------------------------------------------------------------------


def test_command_method_names_are_winrt3():
    """Зафиксированы имена именно winrt 3.x (баг: старые имена не существовали)."""
    assert COMMAND_METHODS == {
        "play_pause": "try_toggle_play_pause_async",
        "next": "try_skip_next_async",
        "previous": "try_skip_previous_async",
    }
    session = Winrt3Session()
    for name in COMMAND_METHODS.values():
        assert hasattr(session, name), name


def test_invoke_command_calls_correct_methods():
    session = Winrt3Session()
    for action in ("play_pause", "next", "previous"):
        _run(invoke_command(session, action))
    assert session.calls == ["toggle_play_pause", "skip_next", "skip_previous"]


def test_invoke_command_unknown_action():
    session = Winrt3Session()
    with pytest.raises(ValueError):
        _run(invoke_command(session, "stop"))


def test_invoke_command_missing_method():
    class OnlyToggle:
        async def try_toggle_play_pause_async(self):
            return None

    with pytest.raises(AttributeError):
        _run(invoke_command(OnlyToggle(), "next"))


# ---------------------------------------------------------------------------
# Выбор сессии (требование: PLAYING приоритетнее current)
# ---------------------------------------------------------------------------


def _session(status: int, auid: str) -> Winrt3Session:
    return Winrt3Session(status=status, auid=auid)


def test_choose_session_keeps_playing_current():
    current = _session(4, "Chrome")
    other = _session(4, "Spotify")
    assert choose_session(current, [other]) is current


def test_choose_session_picks_playing_over_paused_current():
    current = _session(5, "Chrome")  # PAUSED
    playing = _session(4, "Spotify")
    paused = _session(5, "VLC")
    assert choose_session(current, [paused, playing]) is playing


def test_choose_session_falls_back_to_current_when_none_playing():
    current = _session(5, "Chrome")
    other = _session(4 if False else 2, "VLC")  # CHANGED, не PLAYING
    assert choose_session(current, [other]) is current


def test_choose_session_with_no_current():
    playing = _session(4, "Spotify")
    assert choose_session(None, [playing]) is playing


def test_choose_session_no_sessions_returns_current():
    current = _session(5, "Chrome")
    assert choose_session(current, []) is current
    assert choose_session(None, []) is None


def test_choose_session_handles_broken_session():
    class Broken:
        source_app_user_model_id = "Broken"

        def get_playback_info(self):
            raise RuntimeError("closed")

    broken = Broken()
    assert session_playback_status(broken) is None
    assert is_playing_status(None) is False
    # Битая сессия не роняет выбор: она просто не PLAYING.
    assert choose_session(broken, []) is broken
