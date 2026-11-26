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


#: Минимальный масштаб полного режима (дальше — мини-режим из трёх кнопок).
MIN_SCALE = 0.6

#: Булевы поля настроек.
_BOOL_FIELDS = (
    "always_on_top",
    "hide_when_no_media",
    "autostart_enabled",
    "mini",
    "snap_to_edges",
    "hide_on_pause",
    "idle_translucent",
    "ring_from_cover",
)
#: Числовые поля: (тип, минимум, максимум) — значения вне диапазона зажимаются.
_NUMBER_FIELDS: dict[str, tuple[type, float, float]] = {
    "scale": (float, MIN_SCALE, 1.0),
    "no_media_delay": (float, 0.0, 10.0),
    "hide_on_pause_delay": (int, 1, 600),
    "idle_opacity": (int, 20, 100),
}


@dataclass
class Settings:
    always_on_top: bool = True
    hide_when_no_media: bool = False
    autostart_enabled: bool = False
    x: int | None = None
    y: int | None = None
    #: Масштаб виджета: 1.0 — максимальный размер, меньше — уменьшенный.
    scale: float = 1.0
    #: Мини-режим: только кнопки prev / play / next.
    mini: bool = False
    #: Прилипать к краям экрана при перетаскивании.
    snap_to_edges: bool = True
    #: Сколько ждать (с), прежде чем признать «медиа нет» — провал между треками.
    no_media_delay: float = 2.5
    #: Прятать виджет, если трек стоит на паузе дольше hide_on_pause_delay секунд.
    hide_on_pause: bool = False
    hide_on_pause_delay: int = 10
    #: Полупрозрачность, когда мышь не над виджетом (idle_opacity — в процентах).
    idle_translucent: bool = True
    idle_opacity: int = 60
    #: Цвет кольца прогресса берётся из обложки.
    ring_from_cover: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        """Строит настройки из (возможно битого) словаря: неизвестные ключи игнорируются."""
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in _BOOL_FIELDS:
                values[key] = bool(value)
            elif key in _NUMBER_FIELDS:
                kind, low, high = _NUMBER_FIELDS[key]
                try:
                    number = kind(float(value))
                except (TypeError, ValueError, OverflowError):
                    continue  # битое значение — остаётся значение по умолчанию
                values[key] = kind(min(high, max(low, number)))
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
        return {f.name: getattr(self, f.name) for f in fields(self)}


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
