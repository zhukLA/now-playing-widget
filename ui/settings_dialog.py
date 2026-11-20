"""Минимальный диалог настроек виджета."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QVBoxLayout,
)

from settings import THEMES, Settings

THEME_LABELS = {"neon": "Неон", "classic": "Классика"}


class SettingsDialog(QDialog):
    """Флажки поведения (поверх окон / прятать без медиа / автозапуск) и тема."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки — Now Playing")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self._original = settings
        self._top = QCheckBox("Поверх всех окон", self)
        self._hide = QCheckBox("Прятать виджет, когда нет медиа", self)
        self._autostart = QCheckBox("Запускать вместе с Windows", self)

        self._top.setChecked(settings.always_on_top)
        self._hide.setChecked(settings.hide_when_no_media)
        self._autostart.setChecked(settings.autostart_enabled)

        self._theme = QComboBox(self)
        for theme in THEMES:
            self._theme.addItem(THEME_LABELS.get(theme, theme), theme)
        index = self._theme.findData(settings.theme)
        self._theme.setCurrentIndex(max(0, index))

        behaviour = QGroupBox("Поведение", self)
        inner = QVBoxLayout(behaviour)
        inner.addWidget(self._top)
        inner.addWidget(self._hide)
        inner.addWidget(self._autostart)

        look = QGroupBox("Оформление", self)
        form = QFormLayout(look)
        form.addRow("Тема:", self._theme)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(behaviour)
        layout.addWidget(look)
        layout.addWidget(buttons)

    @property
    def result_settings(self) -> Settings:
        """Настройки с учётом выбора пользователя (позиция сохраняется исходной)."""
        s = Settings.from_dict(self._original.to_dict())
        s.always_on_top = self._top.isChecked()
        s.hide_when_no_media = self._hide.isChecked()
        s.autostart_enabled = self._autostart.isChecked()
        s.theme = self._theme.currentData()
        return s
