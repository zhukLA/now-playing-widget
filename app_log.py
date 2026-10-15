"""Логирование приложения: файл %APPDATA%\\NowPlayingWidget\\app.log (UTF-8).

Ротация: 1 МБ × 3 резервных копии. Уровень INFO; с NOW_PLAYING_DEBUG=1 — DEBUG.
Настройка выполняется в main.py; остальные модули берут логгеры через getLogger.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from settings import default_config_dir

LOG_FILE_NAME = "app.log"
DEBUG_ENV_VAR = "NOW_PLAYING_DEBUG"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

_configured = False


def debug_enabled(env: Optional[dict] = None) -> bool:
    """True, если включён DEBUG-режим (NOW_PLAYING_DEBUG=1)."""
    source = os.environ if env is None else env
    raw = str(source.get(DEBUG_ENV_VAR, "")).strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def log_path(config_dir: Optional[Path] = None) -> Path:
    base = Path(config_dir) if config_dir is not None else default_config_dir()
    return base / LOG_FILE_NAME


def setup_logging(
    config_dir: Optional[Path] = None,
    *,
    env: Optional[dict] = None,
    force: bool = False,
) -> Path:
    """Создаёт файловый обработчик с ротацией. Возвращает путь к логу."""
    global _configured
    path = log_path(config_dir)
    level = logging.DEBUG if debug_enabled(env) else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    if _configured and not force:
        return path

    has_file_handler = any(
        isinstance(h, RotatingFileHandler) for h in root.handlers
    )
    if not has_file_handler:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",  # не-ASCII и эмодзи в названиях — без проблем
            )
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            handler.setLevel(level)
            root.addHandler(handler)
        except OSError:
            # Нет доступа к каталогу — приложение работает без файла лога.
            root.warning("не удалось открыть файл лога: %s", path, exc_info=True)

    _configured = True
    return path
