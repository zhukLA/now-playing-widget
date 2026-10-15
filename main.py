"""Точка входа: single-instance (QLocalServer/QLocalSocket), трей, запуск UI.

MOCK-режим: ``NOW_PLAYING_MOCK=1 python main.py`` — виджет работает без
Windows API (имитация треков), удобно для отладки интерфейса.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from app_log import setup_logging
from media_controller import MediaController, is_mock_mode
from settings import Settings, SettingsStore, set_autostart
from ui.settings_dialog import SettingsDialog
from ui.widget import NowPlayingWidget

logger = logging.getLogger("npw.main")

#: Имя сервера для single-instance (локальное для машины).
INSTANCE_SERVER_NAME = "NowPlayingWidget-instance"


def base_dir() -> Path:
    """Каталог ресурсов: sys._MEIPASS в frozen-режиме, иначе каталог main.py."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def load_icon() -> QIcon:
    icon_path = base_dir() / "assets" / "icon.ico"
    if icon_path.is_file():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    return QIcon()


class Application(QObject):
    """Владеет окном, контроллером, треем и настройками."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.store = SettingsStore()
        self.settings: Settings = self.store.load()
        self.icon = load_icon()

        if not self.icon.isNull():
            app.setWindowIcon(self.icon)

        self.widget = NowPlayingWidget(self.settings)
        self.controller = MediaController()
        if self.controller.mock:
            self.widget.setWindowTitle("Now Playing (MOCK)")

        self.tray: Optional[QSystemTrayIcon] = None
        self._server: Optional[QLocalServer] = None

        self._wire_widget()
        self._wire_controller()
        self._setup_tray()

        # Стартовое состояние: позиция, затем видимость (заглушка/скрытие).
        self.widget.restore_position(self.settings.x, self.settings.y)
        self.widget.set_no_media()
        self.controller.start()
        self.app.aboutToQuit.connect(self._shutdown)

    # ------------------------------------------------------------------
    # Связывание
    # ------------------------------------------------------------------

    def _wire_widget(self) -> None:
        self.widget.set_button_handlers(
            {
                "prev": self.controller.previous,
                "play_pause": self.controller.play_pause,
                "next": self.controller.next,
            }
        )
        self.widget.always_on_top_toggled.connect(self._toggle_always_on_top)
        self.widget.autostart_toggled.connect(self._toggle_autostart)
        self.widget.settings_requested.connect(self._open_settings)
        self.widget.quit_requested.connect(self.app.quit)
        self.widget.position_changed.connect(self._save_position)

    def _wire_controller(self) -> None:
        self.controller.track_changed.connect(self.widget.set_track)
        self.controller.cover_changed.connect(self.widget.set_cover)
        self.controller.playback_state_changed.connect(self.widget.set_playing)
        self.controller.no_media.connect(self.widget.set_no_media)

    # ------------------------------------------------------------------
    # Трей
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self.icon, self)
        tray.setToolTip("Now Playing")

        menu = QMenu()
        toggle_action = QAction("Показать/Скрыть", menu)
        toggle_action.triggered.connect(self._toggle_visibility)
        exit_action = QAction("Выход", menu)
        exit_action.triggered.connect(self.app.quit)
        menu.addAction(toggle_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray = tray

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _toggle_visibility(self) -> None:
        if self.widget.isVisible():
            self.widget.set_user_hidden(True)
        else:
            self.widget.set_user_hidden(False)
            self.widget.raise_()
            self.widget.activateWindow()

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        self.store.save(self.settings)

    def _save_position(self, x: int, y: int) -> None:
        if (self.settings.x, self.settings.y) != (x, y):
            self.settings.x = x
            self.settings.y = y
            self._persist()

    def _toggle_always_on_top(self, value: bool) -> None:
        self.settings.always_on_top = bool(value)
        self._persist()
        self.widget.set_settings(self.settings)

    def _toggle_autostart(self, value: bool) -> None:
        self.settings.autostart_enabled = bool(value)
        self._persist()
        # В dev-режиме реестр не трогается (см. settings.autostart_command).
        set_autostart(self.settings.autostart_enabled)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.widget)
        if dialog.exec():
            new_settings = dialog.result_settings
            autostart_changed = (
                new_settings.autostart_enabled != self.settings.autostart_enabled
            )
            self.settings = new_settings
            self._persist()
            if autostart_changed:
                set_autostart(self.settings.autostart_enabled)
            self.widget.set_settings(self.settings)
            # Если «прятать без медиа» выключили — вернём окно.
            if not self.settings.hide_when_no_media and not self.widget.isVisible():
                self.widget.set_user_hidden(False)

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        self.widget.save_position()
        self._persist()
        self.controller.shutdown()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self.tray is not None:
            self.tray.hide()


# ---------------------------------------------------------------------------
# Single-instance
# ---------------------------------------------------------------------------


def _send_show_command() -> bool:
    """Второй экземпляр: сообщить первому «показать окно» и выйти."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_SERVER_NAME)
    if not socket.waitForConnected(400):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(400)
    socket.disconnectFromServer()
    return True


def _start_instance_server(app_object: Application) -> bool:
    """Создаёт сервер первого экземпляра. False — уже запущен другой экземпляр."""
    # Залипший сервер прошлого запуска — снимаем.
    QLocalServer.removeServer(INSTANCE_SERVER_NAME)
    server = QLocalServer()
    if not server.listen(INSTANCE_SERVER_NAME):
        return False
    server.newConnection.connect(lambda: _on_new_connection(server, app_object))
    app_object._server = server
    return True


def _on_new_connection(server: QLocalServer, app_object: Application) -> None:
    while server.hasPendingConnections():
        connection = server.nextPendingConnection()
        if connection is None:
            break

        def handle(ready_socket: QLocalSocket = connection) -> None:
            data = bytes(ready_socket.readAll())
            if b"show" in data:
                app_object.widget.set_user_hidden(False)
                app_object.widget.raise_()
                app_object.widget.activateWindow()
            ready_socket.disconnectFromServer()

        if bytes(connection.readAll()):
            handle()
        else:
            connection.readyRead.connect(handle)


def main() -> int:
    log_file = setup_logging()
    logger.info("=== Now Playing Widget: запуск ===")
    logger.info("python %s | log=%s", sys.version.split()[0], log_file)

    QApplication.setApplicationName("NowPlayingWidget")
    QApplication.setOrganizationName("NowPlayingWidget")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Single-instance: если сервер уже жив — «поднимаем» первое окно и выходим.
    if _send_show_command():
        logger.info("найден запущенный экземпляр — показано его окно, выход")
        return 0

    app_object = Application(app)
    if not _start_instance_server(app_object):
        # Между проверкой и созданием сервера успел стартовать другой экземпляр.
        logger.warning("сервер инстанса не поднялся — повторная отправка show")
        _send_show_command()
        app_object._shutdown()
        return 0

    if is_mock_mode():
        logger.info("MOCK-режим активен (NOW_PLAYING_MOCK=1)")
        print("[NowPlayingWidget] MOCK-режим активен (NOW_PLAYING_MOCK=1)")
    code = app.exec()
    logger.info("=== приложение завершено, код=%s ===", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
