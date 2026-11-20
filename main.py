"""Точка входа: single-instance (QLocalServer/QLocalSocket), трей, запуск UI.

MOCK-режим: ``NOW_PLAYING_MOCK=1 python main.py`` — виджет работает без
Windows API (имитация треков), удобно для отладки интерфейса.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from app_log import install_excepthook, setup_logging
from media_controller import MediaController, is_mock_mode
from settings import (
    Settings,
    SettingsStore,
    autostart_command,
    is_autostart_enabled,
    set_autostart,
)
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
        self._sync_autostart_from_registry()
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
        # is_shown — логическая видимость: во время плавного угасания окно
        # ещё isVisible(), но повторный клик должен вернуть его, а не «скрыть».
        if self.widget.is_shown:
            self.widget.set_user_hidden(True)
        else:
            self.show_widget()

    def show_widget(self) -> None:
        """Показать и поднять окно (трей, повторный запуск приложения)."""
        self.widget.set_user_hidden(False)
        # При «прятать без медиа» окна может не быть — поднимать нечего.
        if self.widget.is_shown:
            self.widget.raise_()
            self.widget.activateWindow()

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------

    def _sync_autostart_from_registry(self) -> None:
        """В frozen-сборке источник правды — реестр, а не settings.json.

        Установщик (галочка «Запускать вместе с Windows») и деинсталлятор
        пишут/удаляют значение Run напрямую; без синхронизации галочка
        «Автозапуск» в меню показывала бы устаревшее состояние.
        """
        if autostart_command() is None:
            return  # dev-режим: реестр не используется
        try:
            actual = is_autostart_enabled()
        except Exception:
            logger.warning("не удалось прочитать автозапуск из реестра", exc_info=True)
            return
        if actual != self.settings.autostart_enabled:
            logger.info("автозапуск: синхронизирован с реестром (%s)", actual)
            self.settings.autostart_enabled = actual
            self._persist()

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
            # Видимость («прятать без медиа») виджет пересчитывает сам.
            self.widget.set_settings(self.settings)

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


SHOW_COMMAND = b"show"


def _send_show_command(name: str = INSTANCE_SERVER_NAME, timeout_ms: int = 400) -> bool:
    """Второй экземпляр: сообщить первому «показать окно». True — сервер ответил."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(SHOW_COMMAND)
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


def _create_instance_server(
    name: str = INSTANCE_SERVER_NAME,
) -> tuple[Optional[QLocalServer], bool]:
    """Сервер первого экземпляра.

    Возвращает ``(server, already_running)``:
      * ``(server, False)`` — мы первый экземпляр;
      * ``(None, True)``    — имя занято живым экземпляром (ему отправлено show);
      * ``(None, False)``   — сервер поднять не удалось, но и другого экземпляра
        нет: приложение работает без защиты от второй копии.

    ``removeServer`` вызывается ТОЛЬКО после того, как никто не ответил на
    подключение: на Unix он удаляет файл сокета, и безусловный вызов
    «отбирал» имя у работающего экземпляра.
    """
    server = QLocalServer()
    if server.listen(name):
        return server, False
    # Имя занято: живой экземпляр (гонка одновременных запусков) или
    # залипший сокет после аварийного завершения.
    if _send_show_command(name):
        return None, True
    QLocalServer.removeServer(name)
    if server.listen(name):
        return server, False
    logger.warning("сервер инстанса не поднялся: %s", server.errorString())
    return None, False


def _attach_show_handler(server: QLocalServer, on_show: Callable[[], None]) -> None:
    """Подключает обработку команды show к серверу инстанса."""
    server.newConnection.connect(lambda: _on_new_connection(server, on_show))


def _on_new_connection(server: QLocalServer, on_show: Callable[[], None]) -> None:
    while server.hasPendingConnections():
        connection = server.nextPendingConnection()
        if connection is None:
            break
        connection.disconnected.connect(connection.deleteLater)
        received = bytearray()

        # Данные копятся между вызовами: раньше первый readAll() «съедал»
        # уже пришедшую команду, а handle() читал пустоту, и окно не
        # поднималось, если байты успевали прийти до newConnection.
        def handle(sock: QLocalSocket = connection, buf: bytearray = received) -> None:
            buf.extend(bytes(sock.readAll()))
            if SHOW_COMMAND in buf:
                buf.clear()
                on_show()
                sock.disconnectFromServer()

        connection.readyRead.connect(handle)
        if connection.bytesAvailable() > 0:
            handle()


def main() -> int:
    log_file = setup_logging()
    install_excepthook()
    logger.info("=== Now Playing Widget: запуск ===")
    logger.info("python %s | log=%s", sys.version.split()[0], log_file)

    QApplication.setApplicationName("NowPlayingWidget")
    QApplication.setOrganizationName("NowPlayingWidget")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Single-instance: сначала спрашиваем живой экземпляр (на Windows именованный
    # канал может принять второй listen, поэтому одной проверки listen мало).
    # Сервер поднимается ДО создания окна и контроллера, чтобы второй
    # экземпляр не успевал мигнуть окном и запустить SMTC.
    if _send_show_command():
        logger.info("найден запущенный экземпляр — показано его окно, выход")
        return 0
    server, already_running = _create_instance_server()
    if already_running:
        logger.info("найден запущенный экземпляр — показано его окно, выход")
        return 0

    app_object = Application(app)
    if server is not None:
        app_object._server = server
        _attach_show_handler(server, app_object.show_widget)

    if is_mock_mode():
        logger.info("MOCK-режим активен (NOW_PLAYING_MOCK=1)")
        print("[NowPlayingWidget] MOCK-режим активен (NOW_PLAYING_MOCK=1)")

    # Ctrl+C в консоли (dev-режим): Python обрабатывает сигналы только между
    # своими инструкциями, а цикл Qt крутится в C++ — периодический пустой
    # таймер даёт интерпретатору шанс вызвать обработчик.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sigint_timer = QTimer()
    sigint_timer.timeout.connect(lambda: None)
    sigint_timer.start(250)

    code = app.exec()
    logger.info("=== приложение завершено, код=%s ===", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
