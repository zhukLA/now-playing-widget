"""Минимальный диалог настроек виджета."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QVBoxLayout,
)

from settings import Settings


class SettingsDialog(QDialog):
    """Три флагка: поверх всех окон / прятать без медиа / автозапуск."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки — Now Playing")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self._original = settings
        self._top = QCheckBox("Поверх всех окон", self)
        self._hide = QCheckBox("Прятать виджет, когда нет медиа", self)
        self._autostart = QCheckBox("Запускать вместе с Windows", self)

        self._top.setChecked(settings.always_on_top)
        self._hide.setChecked(settings.hide_when_no_media)
        self._autostart.setChecked(settings.autostart_enabled)

        group = QGroupBox("Поведение", self)
        inner = QVBoxLayout(group)
        inner.addWidget(self._top)
        inner.addWidget(self._hide)
        inner.addWidget(self._autostart)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(group)
        layout.addWidget(buttons)

    @property
    def result_settings(self) -> Settings:
        """Настройки с учётом выбора пользователя (позиция сохраняется исходной)."""
        s = Settings.from_dict(self._original.to_dict())
        s.always_on_top = self._top.isChecked()
        s.hide_when_no_media = self._hide.isChecked()
        s.autostart_enabled = self._autostart.isChecked()
        return s
