"""Диалог настроек виджета."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from settings import MIN_SCALE, Settings


def _row(*widgets: QWidget) -> QWidget:
    """Несколько элементов в одну строку (флажок + число рядом)."""
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        layout.addWidget(w)
    layout.addStretch(1)
    return box


class SettingsDialog(QDialog):
    """Поведение, вид и перемещение виджета."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки — Now Playing")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._original = settings

        # --- Поведение ---
        self._top = QCheckBox("Поверх всех окон", self)
        self._hide = QCheckBox("Прятать виджет, когда нет медиа", self)
        self._autostart = QCheckBox("Запускать вместе с Windows", self)
        self._hide_pause = QCheckBox("Прятать на паузе через", self)
        self._hide_pause_delay = QSpinBox(self)
        self._hide_pause_delay.setRange(1, 600)
        self._hide_pause_delay.setSuffix(" с")
        self._no_media_delay = QDoubleSpinBox(self)
        self._no_media_delay.setRange(0.0, 10.0)
        self._no_media_delay.setSingleStep(0.5)
        self._no_media_delay.setDecimals(1)
        self._no_media_delay.setSuffix(" с")
        self._no_media_delay.setToolTip(
            "Между треками плеер на миг сообщает «нет медиа». "
            "Столько виджет ждёт, прежде чем спрятаться или показать заглушку."
        )

        behaviour = QGroupBox("Поведение", self)
        b = QVBoxLayout(behaviour)
        b.addWidget(self._top)
        b.addWidget(self._hide)
        b.addWidget(self._autostart)
        b.addWidget(_row(self._hide_pause, self._hide_pause_delay))
        b.addWidget(_row(QLabel("Задержка «нет медиа» между треками:"), self._no_media_delay))

        # --- Вид ---
        self._scale = QSlider(Qt.Orientation.Horizontal, self)
        self._scale.setRange(round(MIN_SCALE * 100), 100)
        self._scale.setSingleStep(5)
        self._scale.setPageStep(10)
        self._scale_label = QLabel(self)
        self._scale_label.setMinimumWidth(40)
        self._scale.valueChanged.connect(lambda v: self._scale_label.setText(f"{v}%"))
        self._mini = QCheckBox("Мини-режим (только кнопки)", self)
        self._mini.toggled.connect(lambda on: self._scale.setEnabled(not on))
        self._translucent = QCheckBox("Полупрозрачный, когда мышь не над виджетом:", self)
        self._opacity = QSpinBox(self)
        self._opacity.setRange(20, 100)
        self._opacity.setSingleStep(5)
        self._opacity.setSuffix(" %")
        self._translucent.toggled.connect(self._opacity.setEnabled)
        self._ring_color = QCheckBox("Цвет кольца прогресса из обложки", self)

        look = QGroupBox("Вид", self)
        form = QFormLayout(look)
        form.addRow("Размер:", _row(self._scale, self._scale_label))
        form.addRow("", self._mini)
        form.addRow("", _row(self._translucent, self._opacity))
        form.addRow("", self._ring_color)

        # --- Перемещение ---
        self._snap = QCheckBox("Прилипать к краям экрана (Alt — временно выключить)", self)
        moving = QGroupBox("Перемещение", self)
        QVBoxLayout(moving).addWidget(self._snap)

        self._load(settings)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(behaviour)
        layout.addWidget(look)
        layout.addWidget(moving)
        layout.addWidget(buttons)

    def _load(self, s: Settings) -> None:
        self._top.setChecked(s.always_on_top)
        self._hide.setChecked(s.hide_when_no_media)
        self._autostart.setChecked(s.autostart_enabled)
        self._hide_pause.setChecked(s.hide_on_pause)
        self._hide_pause_delay.setValue(s.hide_on_pause_delay)
        self._no_media_delay.setValue(s.no_media_delay)
        self._scale.setValue(round(s.scale * 100))
        self._scale_label.setText(f"{self._scale.value()}%")
        self._mini.setChecked(s.mini)
        self._scale.setEnabled(not s.mini)
        self._translucent.setChecked(s.idle_translucent)
        self._opacity.setValue(s.idle_opacity)
        self._opacity.setEnabled(s.idle_translucent)
        self._ring_color.setChecked(s.ring_from_cover)
        self._snap.setChecked(s.snap_to_edges)

    @property
    def result_settings(self) -> Settings:
        """Настройки с учётом выбора пользователя (позиция сохраняется исходной)."""
        s = Settings.from_dict(self._original.to_dict())
        s.always_on_top = self._top.isChecked()
        s.hide_when_no_media = self._hide.isChecked()
        s.autostart_enabled = self._autostart.isChecked()
        s.hide_on_pause = self._hide_pause.isChecked()
        s.hide_on_pause_delay = self._hide_pause_delay.value()
        s.no_media_delay = round(self._no_media_delay.value(), 1)
        s.mini = self._mini.isChecked()
        # Масштаб, заданный мышью (например 0.677), не округляем, если
        # ползунок не трогали.
        if not s.mini and self._scale.value() != round(self._original.scale * 100):
            s.scale = self._scale.value() / 100.0
        s.idle_translucent = self._translucent.isChecked()
        s.idle_opacity = self._opacity.value()
        s.ring_from_cover = self._ring_color.isChecked()
        s.snap_to_edges = self._snap.isChecked()
        return s
