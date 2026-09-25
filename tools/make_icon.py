# ビルド用: アプリのアイコンを描いて build/deskkit.ico(PNG 埋め込み形式の ICO)を書き出す。
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from deskkit.ui.icons import render  # noqa: E402


def main(out: Path) -> None:
    _app = QGuiApplication(sys.argv[:1])
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    blobs: list[bytes] = []
    for s in sizes:
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        render(s).save(buf, "PNG")
        blobs.append(bytes(ba.data()))
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, b in zip(sizes, blobs, strict=True):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(b), offset)
        offset += len(b)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + entries + b"".join(blobs))
    render(512).save(str(out.with_suffix(".png")))
    print("wrote", out)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "build/deskkit.ico"))
