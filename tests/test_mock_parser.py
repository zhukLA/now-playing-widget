"""Тесты разбора данных MOCK-режима: TrackInfo, статусы, PNG, MockSession.

Qt-виджеты и winrt не создаются — QApplication не требуется.
"""

from __future__ import annotations

import base64
import struct
import zlib

from media_controller import (
    MOCK_ENV_VAR,
    PLAYBACK_STATUS_PLAYING,
    MockSession,
    MockTrack,
    TrackInfo,
    is_mock_mode,
    is_playing_status,
    make_solid_png,
    parse_media_properties,
)


# ---------------------------------------------------------------------------
# TrackInfo.from_mapping
# ---------------------------------------------------------------------------


def test_from_mapping_basic() -> None:
    info = TrackInfo.from_mapping({"title": "Song", "artist": "Band"})
    assert info.title == "Song"
    assert info.artist == "Band"
    assert info.cover_bytes is None
    assert info.key == ("Song", "Band")


def test_from_mapping_cover_bytes() -> None:
    info = TrackInfo.from_mapping({"title": "T", "artist": "A", "cover_bytes": b"\x89PNG"})
    assert info.cover_bytes == b"\x89PNG"


def test_from_mapping_cover_base64() -> None:
    raw = b"hello cover"
    info = TrackInfo.from_mapping(
        {"title": "T", "artist": "A", "cover": base64.b64encode(raw).decode()}
    )
    assert info.cover_bytes == raw


def test_from_mapping_invalid_base64_cover_is_none() -> None:
    info = TrackInfo.from_mapping({"title": "T", "artist": "A", "cover": "!!!"})
    assert info.cover_bytes is None


def test_from_mapping_coerces_non_str_and_ignores_garbage() -> None:
    info = TrackInfo.from_mapping(
        {"title": 42, "artist": None, "unknown": "x", "cover": 123}
    )
    assert info.title == "42"
    assert info.artist == ""
    assert info.cover_bytes is None
    assert not hasattr(info, "unknown")


def test_from_mapping_non_dict_returns_empty() -> None:
    assert TrackInfo.from_mapping(None) == TrackInfo()
    assert TrackInfo.from_mapping(["title"]) == TrackInfo()
    assert TrackInfo.from_mapping("x") == TrackInfo()


def test_trackinfo_is_immutable_value() -> None:
    a = TrackInfo("T", "A", b"1")
    b = TrackInfo("T", "A", b"1")
    assert a == b
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# Окружение MOCK
# ---------------------------------------------------------------------------


def test_is_mock_mode_parsing() -> None:
    assert is_mock_mode({MOCK_ENV_VAR: "1"}) is True
    assert is_mock_mode({MOCK_ENV_VAR: "true"}) is True
    assert is_mock_mode({MOCK_ENV_VAR: "YES"}) is True
    assert is_mock_mode({MOCK_ENV_VAR: "0"}) is False
    assert is_mock_mode({MOCK_ENV_VAR: "off"}) is False
    assert is_mock_mode({MOCK_ENV_VAR: ""}) is False
    assert is_mock_mode({}) is False


# ---------------------------------------------------------------------------
# Статус воспроизведения
# ---------------------------------------------------------------------------


def test_is_playing_status_values() -> None:
    assert is_playing_status(PLAYBACK_STATUS_PLAYING) is True
    assert is_playing_status(4) is True
    assert is_playing_status(5) is False  # PAUSED
    assert is_playing_status(0) is False
    assert is_playing_status(None) is False


class _FakeEnum:
    """Имитация WinRT-перечисления с атрибутом name."""

    def __init__(self, value: int, name: str) -> None:
        self._value = value
        self.name = name

    def __int__(self) -> int:
        return self._value


def test_is_playing_status_enum_like() -> None:
    assert is_playing_status(_FakeEnum(4, "PLAYING")) is True
    assert is_playing_status(_FakeEnum(5, "PAUSED")) is False


# ---------------------------------------------------------------------------
# parse_media_properties (duck typing)
# ---------------------------------------------------------------------------


class _FakeProps:
    def __init__(self, title="", artist="", thumbnail=None) -> None:
        self.title = title
        self.artist = artist
        self.thumbnail = thumbnail


def test_parse_media_properties() -> None:
    assert parse_media_properties(_FakeProps("T", "A")) == ("T", "A")
    assert parse_media_properties(None) == ("", "")
    assert parse_media_properties(object()) == ("", "")


# ---------------------------------------------------------------------------
# make_solid_png
# ---------------------------------------------------------------------------


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks = []
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF
        chunks.append((tag, payload))
        offset += 12 + length
    return chunks


def test_make_solid_png_valid() -> None:
    png = make_solid_png(4, (10, 20, 30))
    chunks = _png_chunks(png)
    tags = [tag for tag, _ in chunks]
    assert tags == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, color = struct.unpack(">IIBB", chunks[0][1][:10])
    assert (width, height, depth, color) == (4, 4, 8, 2)  # 8-bit RGB
    raw = zlib.decompress(chunks[1][1])
    # 4 строки: фильтр 0 + 4 пикселя RGB
    assert len(raw) == 4 * (1 + 4 * 3)
    assert raw[:7] == b"\x00" + bytes((10, 20, 30)) * 2


def test_make_solid_png_clamps_colors() -> None:
    png = make_solid_png(1, (999, -5, 200))
    chunks = _png_chunks(png)
    raw = zlib.decompress(chunks[1][1])
    assert raw == b"\x00" + bytes((255, 0, 200))


# ---------------------------------------------------------------------------
# MockSession
# ---------------------------------------------------------------------------


def test_mock_session_track_contains_valid_cover() -> None:
    session = MockSession()
    info = session.track
    assert info.title and info.artist
    assert info.cover_bytes is not None
    _png_chunks(info.cover_bytes)  # валидный PNG


def test_mock_session_rotation_wraps() -> None:
    session = MockSession()
    first = session.track.key
    seen = {first}
    for _ in range(3):  # 4 трека в MOCK_TRACKS
        session.next()
        seen.add(session.track.key)
    assert len(seen) == 4
    session.next()
    assert session.track.key == first  # замкнулось


def test_mock_session_previous_wraps_backwards() -> None:
    session = MockSession()
    first = session.track.key
    session.previous()
    assert session.track.key != first
    session.next()
    assert session.track.key == first


def test_mock_session_toggle() -> None:
    session = MockSession(playing=True)
    assert session.playing is True
    assert session.toggle() is False
    assert session.toggle() is True


def test_mock_session_custom_tracks_and_index() -> None:
    tracks = (
        MockTrack("One", "A", (1, 2, 3)),
        MockTrack("Two", "B", (4, 5, 6)),
    )
    session = MockSession(tracks=tracks, index=1, playing=False)
    assert session.track.title == "Two"
    assert session.playing is False
    session.next()
    assert session.track.title == "One"  # wrap на 2 треках


def test_mock_session_covers_cached() -> None:
    session = MockSession()
    first = session.track.cover_bytes
    session.next()
    session.previous()
    assert session.track.cover_bytes is first
