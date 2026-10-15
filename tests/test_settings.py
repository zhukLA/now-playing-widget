"""Тесты settings.py: хранилище JSON и работа с автозапуском (winreg замокан)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import settings as settings_mod
from settings import Settings, SettingsStore, set_autostart, is_autostart_enabled


# ---------------------------------------------------------------------------
# Хранилище настроек
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "cfg")
    loaded = store.load()
    assert loaded == Settings()
    assert loaded.always_on_top is True
    assert loaded.hide_when_no_media is False
    assert loaded.autostart_enabled is False
    assert loaded.x is None and loaded.y is None


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    original = Settings(
        always_on_top=False,
        hide_when_no_media=True,
        autostart_enabled=True,
        x=100,
        y=200,
    )
    store.save(original)
    assert store.path.exists()
    assert store.load() == original


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "a" / "b")
    store.save(Settings())
    assert (tmp_path / "a" / "b" / "settings.json").exists()


def test_load_corrupt_json_returns_defaults(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.config_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not valid json", encoding="utf-8")
    assert store.load() == Settings()


def test_from_dict_ignores_unknown_and_bad_values() -> None:
    s = Settings.from_dict(
        {"unknown": 1, "x": "oops", "always_on_top": 0, "y": None, "hide_when_no_media": 1}
    )
    assert s.x is None
    assert s.y is None
    assert s.always_on_top is False
    assert s.hide_when_no_media is True


def test_from_dict_non_dict_returns_defaults() -> None:
    assert Settings.from_dict(None) == Settings()
    assert Settings.from_dict([1, 2]) == Settings()


def test_to_dict_shape() -> None:
    d = Settings().to_dict()
    assert set(d) == {
        "always_on_top",
        "hide_when_no_media",
        "autostart_enabled",
        "x",
        "y",
    }
    json.dumps(d)  # должен быть сериализуем


def test_default_config_dir_uses_appdata(monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", r"C:\Tmp\AppData")
    assert settings_mod.default_config_dir() == Path(r"C:\Tmp\AppData\NowPlayingWidget")


def test_default_config_dir_fallback_home(monkeypatch) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    assert settings_mod.default_config_dir() == Path.home() / "NowPlayingWidget"


# ---------------------------------------------------------------------------
# Автозапуск: winreg замокан, реальный реестр не трогаем
# ---------------------------------------------------------------------------


class FakeWinreg:
    """Минимальная замена модуля winreg в памяти."""

    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed: list[object] = []

    def CreateKey(self, root, path):  # noqa: N802
        return ("key", path)

    def OpenKey(self, root, path, reserved=0, access=None):  # noqa: N802
        if path not in self.values and path != settings_mod.AUTOSTART_RUN_KEY:
            raise FileNotFoundError(path)
        if path == settings_mod.AUTOSTART_RUN_KEY and not self.values:
            raise FileNotFoundError(path)
        return ("key", path)

    def SetValueEx(self, key, name, reserved, typ, value):  # noqa: N802
        self.values[name] = value

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def CloseKey(self, key) -> None:  # noqa: N802
        self.closed.append(key)


def test_set_autostart_dev_mode_does_not_touch_registry() -> None:
    fake = FakeWinreg()
    changed = set_autostart(True, winreg_module=fake, command=None)
    assert changed is False
    assert fake.values == {}
    assert autostart_command_dev_is_none()


def autostart_command_dev_is_none() -> bool:
    # В тестах приложение не frozen — команда не формируется.
    return not getattr(sys, "frozen", False) and settings_mod.autostart_command() is None


def test_set_autostart_enable_and_disable() -> None:
    fake = FakeWinreg()
    cmd = '"C:\\App\\NowPlayingWidget.exe"'
    assert set_autostart(True, winreg_module=fake, command=cmd) is True
    assert fake.values[settings_mod.AUTOSTART_RUN_VALUE_NAME] == cmd
    assert is_autostart_enabled(winreg_module=fake) is True

    assert set_autostart(False, winreg_module=fake, command=cmd) is True
    assert settings_mod.AUTOSTART_RUN_VALUE_NAME not in fake.values
    assert is_autostart_enabled(winreg_module=fake) is False


def test_set_autostart_disable_missing_value_is_safe() -> None:
    fake = FakeWinreg()
    assert set_autostart(False, winreg_module=fake, command='"x"') is False
    assert is_autostart_enabled(winreg_module=fake) is False


def test_autostart_value_name_matches_installer() -> None:
    iss = Path(__file__).resolve().parents[1] / "installer.iss"
    if not iss.exists():
        return  # installer.iss ещё может не существовать на ранних шагах
    text = iss.read_text(encoding="utf-8", errors="replace")
    assert settings_mod.AUTOSTART_RUN_VALUE_NAME in text
