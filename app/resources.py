"""Resource paths (source tree or PyInstaller bundle) and the application icon."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def draw_icon_pixmap(size: int) -> QPixmap:
    """A black pill with a small green 'playing' dot."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    h = size * 0.46
    rect = QRectF(size * 0.04, (size - h) / 2, size * 0.92, h)
    p.setPen(QColor(255, 255, 255, 90) if size >= 24 else Qt.PenStyle.NoPen)
    p.setBrush(QColor(8, 8, 10))
    p.drawRoundedRect(rect, h / 2, h / 2)
    d = h * 0.42
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(30, 215, 96))
    p.drawEllipse(QRectF(rect.right() - h / 2 - d / 2, rect.center().y() - d / 2, d, d))
    p.end()
    return pm


def app_icon() -> QIcon:
    ico = resource_path("assets/icon.ico")
    if ico.exists():
        icon = QIcon(str(ico))
        if not icon.isNull():
            return icon
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 256):
        icon.addPixmap(draw_icon_pixmap(size))
    return icon
