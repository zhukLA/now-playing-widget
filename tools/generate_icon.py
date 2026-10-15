"""Разовый скрипт: генерация assets/icon.ico (PySide6, без Pillow).

Запуск из корня проекта:
    .venv\\Scripts\\python.exe tools\\generate_icon.py

ICO собирается вручную из PNG-кадров (поддерживается Windows Vista+).
В runtime-зависимости скрипт не входит.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

SIZES = (16, 24, 32, 48, 64, 128)
WHITE = QColor(255, 255, 255)


def render_icon(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    margin = size * 0.05
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = size * 0.22

    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor(58, 118, 232))
    gradient.setColorAt(1.0, QColor(26, 44, 96))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(rect, radius, radius)

    # Музыкальная нота
    head_cx, head_cy = size * 0.37, size * 0.68
    head_rx = size * 0.155
    head_ry = size * 0.115
    painter.save()
    painter.translate(head_cx, head_cy)
    painter.rotate(-18.0)
    painter.setBrush(WHITE)
    painter.drawEllipse(QRectF(-head_rx, -head_ry, head_rx * 2, head_ry * 2))
    painter.restore()

    stem_x = head_cx + head_rx * 0.92
    top_y = size * 0.24
    pen = QPen(WHITE, size * 0.055)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(stem_x, head_cy), QPointF(stem_x, top_y))

    flag = QPainterPath(QPointF(stem_x, top_y))
    flag.cubicTo(
        QPointF(stem_x + size * 0.20, top_y + size * 0.03),
        QPointF(stem_x + size * 0.20, top_y + size * 0.16),
        QPointF(stem_x + size * 0.06, top_y + size * 0.24),
    )
    painter.drawPath(flag)

    painter.end()
    return image


def to_png(image: QImage) -> bytes:
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("не удалось открыть QBuffer")
    if not image.save(buffer, "PNG"):
        raise RuntimeError("не удалось сохранить PNG")
    return bytes(buffer.data())


def write_ico(path: Path, sizes: tuple[int, ...] = SIZES) -> None:
    images = [render_icon(size) for size in sizes]
    blobs = [to_png(image) for image in images]

    count = len(blobs)
    header = struct.pack("<HHH", 0, 1, count)  # ICONDIR
    offset = 6 + 16 * count
    entries = b""
    for size, blob in zip(sizes, blobs):
        width = size if size < 256 else 0
        height = size if size < 256 else 0
        entries += struct.pack(
            "<BBBBHHII", width, height, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + b"".join(blobs))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "assets" / "icon.ico"
    write_ico(target)
    print(f"Создана иконка: {target} ({target.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
