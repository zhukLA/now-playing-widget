"""Настройки приложения: JSON-файл в %APPDATA%\\NowPlayingWidget\\settings.json.

Модуль не зависит от Qt, поэтому тесты могут работать без QApplication.
Для тестов каталог хранения принимается через конструктор (tmp_path),
а обращения к реестру — через подменяемый модуль winreg.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger("npw.settings")

APP_DIR_NAME = "NowPlayingWidget"
SETTINGS_FILE_NAME = "settings.json"

# Имя значения в ветке Run. Должно совпадать с installer.iss.
AUTOSTART_RUN_VALUE_NAME = "NowPlayingWidget"
AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def default_config_dir() -> Path:
    """Каталог настроек: %APPDATA%\\NowPlayingWidget (с фолбэком на домашнюю папку)."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / APP_DIR_NAME


@dataclass
class Settings:
    always_on_top: bool = True
    hide_when_no_media: bool = False
    autostart_enabled: bool = False
    x: int | None = None
    y: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        """Строит настройки из (возможно битого) словаря: неизвестные ключи игнорируются."""
        if not isinstance(data, dict):
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("always_on_top", "hide_when_no_media", "autostart_enabled"):
                values[key] = bool(value)
            elif key in ("x", "y"):
                if value is None:
                    values[key] = None
                else:
                    try:
                        values[key] = int(value)
                    except (TypeError, ValueError):
                        values[key] = None
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "always_on_top": self.always_on_top,
            "hide_when_no_media": self.hide_when_no_media,
            "autostart_enabled": self.autostart_enabled,
            "x": self.x,
            "y": self.y,
        }


class SettingsStore:
    """Чтение/запись настроек. Запись атомарная (через временный файл)."""

    def __init__(self, config_dir: Path | str | None = None) -> None:
        self.config_dir = Path(config_dir) if config_dir is not None else default_config_dir()

    @property
    def path(self) -> Path:
        return self.config_dir / SETTINGS_FILE_NAME

    def load(self) -> Settings:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.debug("файла настроек ещё нет: %s", self.path)
            return Settings()
        except OSError:
            logger.warning("не удалось прочитать настройки: %s", self.path, exc_info=True)
            return Settings()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "битый JSON настроек (%s) — значения по умолчанию", self.path,
                exc_info=True,
            )
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            logger.warning("не удалось сохранить настройки: %s", self.path, exc_info=True)


# ---------------------------------------------------------------------------
# Автозапуск (HKCU\\...\\Run)
# ---------------------------------------------------------------------------


def autostart_command() -> str | None:
    """Команда для ключа Run.

    В frozen-режиме (PyInstaller) — путь к sys.executable.
    В dev-режиме возвращаем None: python main.py в реестр не пишем.
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if not exe:
            return None
        return f'"{exe}"'
    return None


def _import_winreg(winreg_module: Any | None):
    if winreg_module is not None:
        return winreg_module
    import winreg  # noqa: PLC0415 — ленивый импорт, чтобы тесты не зависели от winreg

    return winreg


def set_autostart(
    enabled: bool,
    *,
    winreg_module: Any | None = None,
    command: str | None | object = ...,
) -> bool:
    """Включает/выключает автозапуск.

    Возвращает True, если реестр реально изменён.
    В dev-режиме (command is None) реестр не трогаем — возвращаем False.
    """
    if command is ...:
        command = autostart_command()
    if command is None:
        # Dev-режим: не регистрируем `python main.py`.
        return False
    winreg = _import_winreg(winreg_module)
    if enabled:
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY)
            try:
                winreg.SetValueEx(
                    key, AUTOSTART_RUN_VALUE_NAME, 0, winreg.REG_SZ, command
                )
            finally:
                winreg.CloseKey(key)
        except OSError:
            logger.warning("не удалось записать автозапуск в реестр", exc_info=True)
            return False
        logger.info("автозапуск включён в реестре")
        return True
    try:
        # Нужен доступ KEY_SET_VALUE: с KEY_READ DeleteValue падает с PermissionError.
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY, 0, winreg.KEY_SET_VALUE
        )
    except FileNotFoundError:
        logger.debug("ветки Run нет — удалять нечего")
        return False
    try:
        winreg.DeleteValue(key, AUTOSTART_RUN_VALUE_NAME)
    except FileNotFoundError:
        logger.debug("значения автозапуска нет — удалять нечего")
        return False
    except OSError:
        logger.warning("не удалось удалить автозапуск из реестра", exc_info=True)
        return False
    finally:
        winreg.CloseKey(key)
    logger.info("автозапуск удалён из реестра")
    return True


def is_autostart_enabled(*, winreg_module: Any | None = None) -> bool:
    """Есть ли значение Run с нашим именем (независимо от режима dev/frozen)."""
    winreg = _import_winreg(winreg_module)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY)
    except FileNotFoundError:
        return False
    try:
        winreg.QueryValueEx(key, AUTOSTART_RUN_VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("не удалось прочитать автозапуск из реестра", exc_info=True)
        return False
    finally:
        winreg.CloseKey(key)
    return True
