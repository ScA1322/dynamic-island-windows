"""Generate assets/icon.ico (multi-size, PNG-compressed entries) from the same drawing as the tray icon."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from app.resources import draw_icon_pixmap  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def png_bytes(size: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    draw_icon_pixmap(size).save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def main() -> None:
    _app = QGuiApplication(sys.argv[:1])
    images = [(size, png_bytes(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    out = ROOT / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(header + entries + blobs)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
