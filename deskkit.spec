# -*- mode: python ; coding: utf-8 -*-
# DeskKit を単体 exe(onefile・windowed)にする PyInstaller 設定。build.ps1 から使う。
# マニフェストは既定の asInvoker(管理者昇格を要求しない。INV-3)。
# v0.3: 追加ライブラリ(pi-heif・winrt・psutil・numpy)の隠れた import と、ffmpeg の同梱物・ライセンス表示を足す(H-7)。
import glob
import importlib.util

from PyInstaller.utils.hooks import collect_submodules

# 3モジュールは追加ライブラリを関数の中で import する(NFR-5)。静的解析の取りこぼしが無いよう名前で挙げる
WINRT_NAMESPACES = [
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.networking",
    "winrt.windows.networking.connectivity",
    "winrt.windows.storage.streams",
]
# winrt.windows.media などの途中の階層は __init__.py の無い名前空間パッケージで、collect_submodules では辿れない
missing = [m for m in WINRT_NAMESPACES + ["pi_heif", "psutil", "numpy", "PIL"] if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit(f"venv に無いモジュールがあります: {missing}(pip install -e . などで入れてください)")

hidden = (
    collect_submodules("deskkit")
    + collect_submodules("overlaykit")
    + collect_submodules("pi_heif")
    + collect_submodules("winrt")  # winrt.runtime・winrt.system と各名前空間の拡張モジュール(_winrt_*.pyd)
    + WINRT_NAMESPACES
    + collect_submodules("psutil", filter=lambda n: ".tests" not in n)
    + ["numpy", "PIL.Image", "PIL.ImageOps", "PIL.ExifTags"]
)

# ffmpeg の同梱物(tools/make_ffmpeg_bundle.py が作る。build.ps1 が無ければ止まる)とライセンス表示(H-5)
bundled = sorted(p for p in glob.glob("deskkit/_bundled/*") if not p.endswith(".part"))
if not {"deskkit/_bundled/ffmpeg.zip", "deskkit/_bundled/ffmpeg.sha256"} <= {p.replace("\\", "/") for p in bundled}:
    raise SystemExit("deskkit/_bundled/ffmpeg.zip と ffmpeg.sha256 がありません(build.ps1 から実行してください)")
datas = [(p, "deskkit/_bundled") for p in bundled] + [("THIRD_PARTY_LICENSES.txt", ".")]

a = Analysis(
    ["run_deskkit.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
              "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtDBus", "PySide6.QtDesigner",
              "psutil.tests", "PIL.ImageQt", "PIL.ImageTk"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DeskKit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon="build/deskkit.ico",
    version="build/version_info.txt",
    uac_admin=False,
)
